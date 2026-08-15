"""HTTP broker process for safe Telegram prompt concurrency on one machine."""

import asyncio
import socket
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable, Optional

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__
from .broker_state import (
    TelegramBrokerIdentity,
    load_or_create_broker_identity,
    persist_broker_listen_url,
)
from .debug_logging import TelegramDebugLogger
from .telegram_client import TelegramPromptClient
from .telegram_models import (
    TelegramConfig,
    TelegramPromptError,
    resolve_telegram_download_dir,
    resolve_telegram_target_key,
)

BROKER_DISCONNECT_POLL_SECONDS = 0.25


class BrokerPromptClientDisconnected(Exception):
    """Raised when the local broker client disconnects before a Telegram reply arrives."""

    pass


def build_broker_listen_url(host: str, port: int) -> str:
    """Build the local discovery URL for a running broker."""
    if host in {"0.0.0.0", "::"}:
        resolved_host = "127.0.0.1"
    else:
        resolved_host = host
    return f"http://{resolved_host}:{port}"


def build_broker_health_payload(
    identity: TelegramBrokerIdentity,
    *,
    listen_url: str,
    target_key: str,
) -> dict[str, Any]:
    """Build the broker health response."""
    return {
        "status": "ok",
        "broker_id": identity.broker_id,
        "broker_label": identity.broker_label,
        "listen_url": listen_url,
        "target_key": target_key,
        "version": __version__,
        "supports_guarded_shutdown": True,
    }


async def _wait_for_prompt_or_client_disconnect(
    request: Request,
    prompt_task: asyncio.Task[Optional[str]],
    shutdown_event: Optional[asyncio.Event] = None,
) -> Optional[str]:
    """Wait for one Telegram prompt while cancelling it if the caller or broker stops."""
    while True:
        done, _pending = await asyncio.wait(
            {prompt_task},
            timeout=BROKER_DISCONNECT_POLL_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if shutdown_event is not None and shutdown_event.is_set():
            prompt_task.cancel()
            with suppress(asyncio.CancelledError, TelegramPromptError):
                await prompt_task
            raise BrokerPromptClientDisconnected(
                "Broker shutdown requested before Telegram reply arrived."
            )

        if prompt_task in done:
            return await prompt_task

        if await request.is_disconnected():
            prompt_task.cancel()
            with suppress(asyncio.CancelledError):
                await prompt_task
            raise BrokerPromptClientDisconnected(
                "Broker prompt client disconnected before Telegram reply arrived."
            )


def create_telegram_broker_app(
    identity: TelegramBrokerIdentity,
    *,
    listen_url: str,
    telegram_client: TelegramPromptClient,
    target_key: str,
    shutdown_event: Optional[asyncio.Event] = None,
    request_shutdown: Optional[Callable[[], None]] = None,
    debug_logger: Optional[TelegramDebugLogger] = None,
) -> Starlette:
    """Create the broker HTTP app."""

    active_prompt_count = 0
    shutdown_started = False

    def debug_event(event: str, **fields: Any) -> None:
        if debug_logger is not None:
            debug_logger.event(event, **fields)

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            build_broker_health_payload(
                identity,
                listen_url=listen_url,
                target_key=target_key,
            )
        )

    async def prompts(request: Request) -> JSONResponse:
        nonlocal active_prompt_count
        request_started_at = asyncio.get_running_loop().time()
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse(
                {"status": "error", "error": "Request body must be a JSON object."},
                status_code=400,
            )

        prompt_texts = _parse_prompt_texts_payload(payload)
        prompt_id = payload.get("prompt_id")
        timeout_seconds = payload.get("timeout_seconds")
        download_dir_raw = payload.get("download_dir")

        if not prompt_texts:
            return JSONResponse(
                {
                    "status": "error",
                    "error": "prompt_texts must contain at least one non-empty string.",
                },
                status_code=400,
            )
        if not isinstance(prompt_id, str) or not prompt_id.strip():
            return JSONResponse(
                {"status": "error", "error": "prompt_id must be a non-empty string."},
                status_code=400,
            )
        if not isinstance(timeout_seconds, int) or timeout_seconds < 1:
            return JSONResponse(
                {"status": "error", "error": "timeout_seconds must be a positive integer."},
                status_code=400,
            )
        if not isinstance(download_dir_raw, str) or not download_dir_raw.strip():
            return JSONResponse(
                {"status": "error", "error": "download_dir must be a non-empty string."},
                status_code=400,
            )

        download_dir = resolve_telegram_download_dir(download_dir_raw)
        download_dir.mkdir(parents=True, exist_ok=True)

        # These checks and the counter increment contain no await. Starlette runs them
        # on the broker's asyncio event loop, so graceful shutdown cannot pass its own
        # check between accepting this request and recording it as active.
        if shutdown_started:
            return JSONResponse(
                {
                    "status": "unavailable",
                    "error": "Broker shutdown is already in progress.",
                },
                status_code=503,
            )
        active_prompt_count += 1
        try:
            debug_event(
                "broker_prompt_request_start",
                prompt_id=prompt_id,
                prompt_message_count=len(prompt_texts),
                timeout_seconds=timeout_seconds,
            )
            prompt_task = asyncio.create_task(
                telegram_client.ask_question(
                    prompt_texts,
                    timeout_seconds,
                    prompt_id,
                    download_dir,
                )
            )
            try:
                response = await _wait_for_prompt_or_client_disconnect(
                    request,
                    prompt_task,
                    shutdown_event,
                )
            except BrokerPromptClientDisconnected as exc:
                debug_event(
                    "broker_prompt_request_cancelled",
                    prompt_id=prompt_id,
                    duration_ms=round(
                        (asyncio.get_running_loop().time() - request_started_at) * 1000
                    ),
                    error=exc,
                )
                return JSONResponse(
                    {"status": "cancelled", "error": str(exc)},
                    status_code=499,
                )
            except TelegramPromptError as exc:
                debug_event(
                    "broker_prompt_request_error",
                    prompt_id=prompt_id,
                    duration_ms=round(
                        (asyncio.get_running_loop().time() - request_started_at) * 1000
                    ),
                    error=exc,
                )
                return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

            if response is None:
                debug_event(
                    "broker_prompt_request_timeout",
                    prompt_id=prompt_id,
                    duration_ms=round(
                        (asyncio.get_running_loop().time() - request_started_at) * 1000
                    ),
                )
                return JSONResponse({"status": "timeout"})

            debug_event(
                "broker_prompt_request_ok",
                prompt_id=prompt_id,
                duration_ms=round((asyncio.get_running_loop().time() - request_started_at) * 1000),
            )
            return JSONResponse({"status": "ok", "response": response})
        finally:
            active_prompt_count -= 1

    async def shutdown(request: Request) -> JSONResponse:
        nonlocal shutdown_started

        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse(
                {"status": "error", "error": "Request body must be a JSON object."},
                status_code=400,
            )
        force = payload.get("force") is True

        # As with prompt admission above, do not await between checking active work and
        # entering shutdown. New prompt requests observe shutdown_started and are refused.
        if shutdown_started:
            return JSONResponse({"status": "ok"})
        pending_prompt_count = active_prompt_count
        if pending_prompt_count > 0 and not force:
            return JSONResponse(
                {
                    "status": "busy",
                    "pending_prompt_count": pending_prompt_count,
                    "error": "Broker has active prompt requests and was left running.",
                },
                status_code=409,
            )
        shutdown_started = True

        if shutdown_event is not None:
            shutdown_event.set()
            await asyncio.sleep(BROKER_DISCONNECT_POLL_SECONDS)
        try:
            await telegram_client.shutdown()
        except TelegramPromptError as exc:
            return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

        if request_shutdown is not None:
            asyncio.create_task(_request_shutdown_soon(request_shutdown))

        return JSONResponse(
            {
                "status": "ok",
                "cancelled_prompt_count": pending_prompt_count if force else 0,
            }
        )

    return Starlette(
        debug=False,
        routes=[
            Route("/health", endpoint=health, methods=["GET"]),
            Route("/prompts", endpoint=prompts, methods=["POST"]),
            Route("/shutdown", endpoint=shutdown, methods=["POST"]),
        ],
    )


async def _request_shutdown_soon(request_shutdown: Callable[[], None]) -> None:
    """Let the shutdown response flush before asking uvicorn to stop."""
    await asyncio.sleep(0.05)
    request_shutdown()


def _parse_prompt_texts_payload(payload: dict[str, Any]) -> list[str]:
    """Parse the new multipart prompt payload, accepting the old single-text shape."""
    prompt_texts = payload.get("prompt_texts")
    if isinstance(prompt_texts, list):
        return [item for item in prompt_texts if isinstance(item, str) and item.strip()]

    prompt_text = payload.get("prompt_text")
    if isinstance(prompt_text, str) and prompt_text.strip():
        return [prompt_text]

    return []


def _create_bound_socket(host: str, port: int) -> socket.socket:
    """Bind a listening socket so the chosen port is known before serving."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(socket.SOMAXCONN)
    return sock


def run_telegram_broker(
    *,
    host: str,
    port: int,
    state_dir: Path,
    telegram_target: TelegramConfig,
    broker_label: Optional[str] = None,
    debug_log_path: Optional[str] = None,
) -> None:
    """Run the Telegram broker service and persist its local discovery info."""
    identity = load_or_create_broker_identity(state_dir, broker_label=broker_label)
    debug_logger = TelegramDebugLogger.from_config(debug_log_path)
    server_socket = _create_bound_socket(host, port)
    target_key = resolve_telegram_target_key(telegram_target)

    try:
        actual_port = int(server_socket.getsockname()[1])
        listen_url = build_broker_listen_url(host, actual_port)
        persist_broker_listen_url(state_dir, listen_url)
        if debug_logger is not None:
            debug_logger.event(
                "broker_start",
                broker_id=identity.broker_id,
                broker_label=identity.broker_label,
                listen_url=listen_url,
                target_key=target_key,
                version=__version__,
            )
        shutdown_event = asyncio.Event()
        server_holder: dict[str, uvicorn.Server] = {}

        def request_shutdown() -> None:
            server = server_holder.get("server")
            if server is not None:
                server.should_exit = True

        telegram_client = TelegramPromptClient(
            telegram_target,
            broker_identity=identity,
            debug_log_path=debug_log_path,
        )
        app = create_telegram_broker_app(
            identity,
            listen_url=listen_url,
            telegram_client=telegram_client,
            target_key=target_key,
            shutdown_event=shutdown_event,
            request_shutdown=request_shutdown,
            debug_logger=debug_logger,
        )
        config = uvicorn.Config(app, host=host, port=actual_port, log_level="info")
        server = uvicorn.Server(config)
        server_holder["server"] = server
        try:
            asyncio.run(server.serve(sockets=[server_socket]))
        except KeyboardInterrupt:
            # Uvicorn already performs a graceful shutdown on Ctrl+C. Swallow the final
            # wrapper-level KeyboardInterrupt here so the broker exits cleanly without a
            # traceback after shutdown completes.
            return
    finally:
        if debug_logger is not None:
            debug_logger.event(
                "broker_stop",
                broker_id=identity.broker_id,
                broker_label=identity.broker_label,
                target_key=target_key,
                version=__version__,
            )
        server_socket.close()
