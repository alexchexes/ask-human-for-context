"""Client-side local Telegram broker discovery, startup, and prompt forwarding."""

import asyncio
import datetime as dt
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

import httpx

from . import __version__
from .broker_state import (
    TelegramBrokerHealth,
    TelegramBrokerState,
    acquire_startup_lock,
    load_broker_state,
    resolve_broker_state_dir,
    resolve_target_broker_state_dir,
)
from .debug_logging import TelegramDebugLogger
from .prompt_formatting import build_telegram_prompt_text, build_telegram_prompt_texts
from .telegram_models import (
    DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS,
    TelegramConfig,
    TelegramPromptError,
)

BROKER_SHUTDOWN_TIMEOUT_SECONDS = DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS + 20
PACKAGE_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class _BrokerShutdownResult:
    status: Literal["stopped", "busy", "failed"]
    pending_prompt_count: int = 0


def _compare_package_versions(first: str, second: str) -> Optional[int]:
    """Compare stable x.y.z versions, returning None for an unknown version shape."""
    first_match = PACKAGE_VERSION_PATTERN.fullmatch(first)
    second_match = PACKAGE_VERSION_PATTERN.fullmatch(second)
    if first_match is None or second_match is None:
        return None

    first_parts = tuple(int(part) for part in first_match.groups())
    second_parts = tuple(int(part) for part in second_match.groups())
    return (first_parts > second_parts) - (first_parts < second_parts)


class TelegramBrokerClient:
    """Talk to a per-target local Telegram broker process."""

    def __init__(
        self,
        telegram_target: TelegramConfig,
        download_dir: Path,
        *,
        broker_state_root: Optional[Path] = None,
        broker_label: Optional[str] = None,
        debug_log_path: Optional[str] = None,
    ) -> None:
        self.telegram_target = telegram_target
        self.download_dir = download_dir.resolve()
        self.broker_state_root = (broker_state_root or resolve_broker_state_dir()).resolve()
        self.broker_label = broker_label
        self.debug_log_path = debug_log_path
        self.debug_logger = TelegramDebugLogger.from_config(debug_log_path)
        self.target_state_dir = resolve_target_broker_state_dir(
            self.broker_state_root,
            telegram_target,
        )

    async def ask_question(
        self,
        question: str,
        context: str,
        *,
        prompt_id: str,
        timeout_seconds: int,
        include_timing_info: bool,
        issued_at: dt.datetime,
    ) -> Optional[str]:
        """Ensure a local broker exists, then forward one Telegram prompt through it."""
        self._debug_event("broker_client_prompt_start", prompt_id=prompt_id)
        broker_health = await self._ensure_local_broker()
        self._debug_event(
            "broker_client_health_ready",
            prompt_id=prompt_id,
            listen_url=broker_health.listen_url,
            broker_id=broker_health.broker_id,
            broker_label=broker_health.broker_label,
            target_key=broker_health.target_key,
            version=broker_health.version,
        )
        telegram_issued_at = dt.datetime.now().astimezone() if include_timing_info else issued_at
        prompt_text = build_telegram_prompt_text(
            question,
            context,
            prompt_id=prompt_id,
            timeout_seconds=timeout_seconds,
            include_timing_info=include_timing_info,
            issued_at=telegram_issued_at,
            broker_label=broker_health.broker_label,
            broker_id=broker_health.broker_id,
        )
        prompt_texts = build_telegram_prompt_texts(
            question,
            context,
            prompt_id=prompt_id,
            timeout_seconds=timeout_seconds,
            include_timing_info=include_timing_info,
            issued_at=telegram_issued_at,
            broker_label=broker_health.broker_label,
            broker_id=broker_health.broker_id,
        )

        response = await self._broker_request(
            broker_health.listen_url,
            "prompts",
            {
                "prompt_id": prompt_id,
                "prompt_text": prompt_text,
                "prompt_texts": prompt_texts,
                "timeout_seconds": timeout_seconds,
                "download_dir": str(self.download_dir),
            },
            timeout=timeout_seconds + 30,
        )
        self._debug_event(
            "broker_client_prompt_response",
            prompt_id=prompt_id,
            status=response.get("status"),
        )

        status = response.get("status")
        if status == "ok":
            reply_text = response.get("response")
            if not isinstance(reply_text, str):
                raise TelegramPromptError("Broker prompt response was missing a reply string.")
            return reply_text

        if status == "timeout":
            return None

        error_text = response.get("error", "unknown broker error")
        raise TelegramPromptError(f"Broker prompt failed: {error_text}")

    def _debug_event(self, event: str, **fields: Any) -> None:
        if self.debug_logger is not None:
            self.debug_logger.event(event, **fields)

    async def _ensure_local_broker(self) -> TelegramBrokerHealth:
        """Reuse a healthy broker for this target, or start one if needed."""
        existing_health = await self._probe_persisted_broker(replace_mismatched=False)
        if existing_health is not None:
            return existing_health

        with acquire_startup_lock(self.target_state_dir):
            existing_health = await self._probe_persisted_broker(replace_mismatched=True)
            if existing_health is not None:
                return existing_health

            self._spawn_local_broker()
            return await self._wait_for_local_broker()

    async def _probe_persisted_broker(
        self,
        *,
        replace_mismatched: bool = False,
    ) -> Optional[TelegramBrokerHealth]:
        """Read persisted broker state and verify that the broker is still healthy."""
        state = load_broker_state(self.target_state_dir)
        if state is None or not state.listen_url:
            return None

        try:
            health = await self._fetch_health(state.listen_url)
        except TelegramPromptError:
            return None

        if not self._health_matches_state(health, state):
            return None

        if health.version != __version__:
            if not replace_mismatched:
                return None

            version_relation = _compare_package_versions(__version__, health.version)
            if version_relation is None:
                raise TelegramPromptError(
                    f"Ask Human version mismatch: this MCP server is v{__version__}, but "
                    f"the shared Telegram broker reports v{health.version or 'unknown'}. "
                    "The broker and any pending prompts were left untouched, and this "
                    "question was not sent to Telegram. Ask the user to finish any pending "
                    "Ask Human prompts, follow the documented broker restart procedure, "
                    "reload/restart this session's Ask Human MCP server, then retry."
                )

            if version_relation < 0:
                raise TelegramPromptError(
                    f"Ask Human version mismatch: this MCP server is v{__version__}, but "
                    f"the shared Telegram broker is newer v{health.version}. The broker "
                    "and any pending prompts were left untouched, and this question was "
                    "not sent to Telegram. Ask the user to reload/restart this session's "
                    "Ask Human MCP server, then retry."
                )

            if not health.supports_guarded_shutdown:
                raise TelegramPromptError(
                    f"Ask Human version mismatch: this MCP server is v{__version__}, but "
                    f"the shared Telegram broker is legacy v{health.version} and cannot "
                    "prove that guarded replacement is safe. It was left running, pending "
                    "prompts were not changed, and this question was not sent to Telegram. "
                    "Ask the user to finish any pending Ask Human prompts, follow the "
                    "documented broker restart procedure, then retry."
                )

            shutdown_result = await self._shutdown_broker(state.listen_url)
            if shutdown_result.status == "busy":
                pending_count = shutdown_result.pending_prompt_count
                prompt_word = "prompt" if pending_count == 1 else "prompts"
                raise TelegramPromptError(
                    f"Ask Human version mismatch: this MCP server is v{__version__}, but "
                    f"shared Telegram broker v{health.version} has {pending_count} pending "
                    f"{prompt_word}. Nothing was cancelled, and this question was not sent "
                    "to Telegram. Ask the user to finish the pending Ask Human prompt(s), "
                    "then retry; the idle broker will update automatically."
                )
            if shutdown_result.status == "failed":
                raise TelegramPromptError(
                    f"Ask Human version mismatch: this MCP server is v{__version__}, but "
                    f"safe shutdown of shared Telegram broker v{health.version} could not "
                    "be confirmed. No forced shutdown was requested, so active prompts "
                    "were not deliberately cancelled. This question was not sent to "
                    "Telegram. Ask the user to finish any pending Ask Human prompts, "
                    "follow the documented broker restart procedure, then retry."
                )
            return None

        return health

    async def _wait_for_local_broker(self) -> TelegramBrokerHealth:
        """Poll broker state and health until the spawned process is ready."""
        deadline = asyncio.get_running_loop().time() + 15
        last_error: Optional[str] = None

        while asyncio.get_running_loop().time() < deadline:
            state = load_broker_state(self.target_state_dir)
            if state is not None and state.listen_url:
                try:
                    health = await self._fetch_health(state.listen_url)
                    if self._health_matches_state(health, state):
                        if health.version == __version__:
                            return health
                        last_error = (
                            f"observed broker v{health.version or 'unknown'}, but this MCP "
                            f"server still has v{__version__} loaded. Ask the user to "
                            "reload/restart this session's Ask Human MCP server after a "
                            "package update, then retry"
                        )
                except TelegramPromptError as exc:
                    last_error = str(exc)

            await asyncio.sleep(0.2)

        if last_error is not None:
            raise TelegramPromptError(f"Local Telegram broker did not become healthy: {last_error}")
        raise TelegramPromptError("Local Telegram broker did not become healthy in time.")

    def _spawn_local_broker(self) -> None:
        """Start a detached local broker process for this target."""
        command = [
            sys.executable,
            "-m",
            "ask_human",
            "--telegram-broker",
            "--telegram",
            f"{self.telegram_target.bot_token} {self.telegram_target.chat_id}",
            "--telegram-broker-state-dir",
            str(self.broker_state_root),
            "--telegram-broker-host",
            "127.0.0.1",
            "--telegram-broker-port",
            "0",
        ]
        if self.broker_label:
            command.extend(["--telegram-broker-label", self.broker_label])
        if self.debug_log_path:
            command.extend(["--telegram-debug-log", self.debug_log_path])

        creation_flags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creation_flags |= subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            creationflags=creation_flags,
        )

    async def _fetch_health(self, listen_url: str) -> TelegramBrokerHealth:
        """Read and validate one broker health response."""
        response = await self._broker_request(listen_url, "health", None, timeout=10, method="GET")

        try:
            status = str(response["status"])
            broker_id = str(response["broker_id"])
            broker_label = str(response["broker_label"])
            target_key = str(response["target_key"])
            response_listen_url = str(response["listen_url"])
        except KeyError as exc:
            missing_key = exc.args[0]
            raise TelegramPromptError(
                f"Broker health response was missing {missing_key!r}."
            ) from exc

        if status != "ok":
            raise TelegramPromptError(f"Broker health status was {status!r}.")

        return TelegramBrokerHealth(
            broker_id=broker_id,
            broker_label=broker_label,
            listen_url=response_listen_url,
            target_key=target_key,
            version=str(response.get("version", "")),
            supports_guarded_shutdown=response.get("supports_guarded_shutdown") is True,
        )

    def _health_matches_state(
        self,
        health: TelegramBrokerHealth,
        state: TelegramBrokerState,
    ) -> bool:
        """Verify that a health response belongs to this persisted broker target."""
        return (
            health.broker_id == state.identity.broker_id
            and health.target_key == self.target_state_dir.name
        )

    async def _shutdown_broker(self, listen_url: str) -> _BrokerShutdownResult:
        """Ask a running broker to terminate, preserving active prompts by default."""
        try:
            response = await self._broker_request(
                listen_url,
                "shutdown",
                {"force": False},
                timeout=BROKER_SHUTDOWN_TIMEOUT_SECONDS,
                accepted_status_codes=frozenset({409}),
            )
        except TelegramPromptError:
            return _BrokerShutdownResult("failed")

        if response.get("status") == "ok":
            return _BrokerShutdownResult("stopped")
        if response.get("status") == "busy":
            pending_prompt_count = response.get("pending_prompt_count")
            if isinstance(pending_prompt_count, int) and pending_prompt_count >= 0:
                return _BrokerShutdownResult("busy", pending_prompt_count)

        return _BrokerShutdownResult("failed")

    async def _broker_request(
        self,
        listen_url: str,
        path: str,
        payload: Optional[dict[str, Any]],
        *,
        timeout: int,
        method: str = "POST",
        accepted_status_codes: frozenset[int] = frozenset(),
    ) -> dict[str, Any]:
        """Issue one broker HTTP request and decode the JSON response."""
        started_at = asyncio.get_running_loop().time()
        self._debug_event(
            "broker_client_request_start",
            path=path,
            method=method,
            listen_url=listen_url,
            timeout_seconds=timeout,
        )
        try:
            try:
                request_url = f"{listen_url.rstrip('/')}/{path.lstrip('/')}"
                async with httpx.AsyncClient(timeout=timeout) as client:
                    http_response = await client.request(method, request_url, json=payload)
                    if http_response.status_code not in accepted_status_codes:
                        http_response.raise_for_status()
                response = http_response.json()
            except httpx.HTTPStatusError as exc:
                raise TelegramPromptError(
                    f"Broker {path} request failed with HTTP {exc.response.status_code}: "
                    f"{exc.response.text}"
                ) from exc
            except httpx.RequestError as exc:
                raise TelegramPromptError(f"Broker {path} request failed: {exc}") from exc

            if not isinstance(response, dict):
                raise TelegramPromptError(f"Broker {path} response was not a JSON object.")
        except TelegramPromptError as exc:
            self._debug_event(
                "broker_client_request_error",
                path=path,
                method=method,
                listen_url=listen_url,
                duration_ms=round((asyncio.get_running_loop().time() - started_at) * 1000),
                error=exc,
            )
            raise

        self._debug_event(
            "broker_client_request_ok",
            path=path,
            method=method,
            listen_url=listen_url,
            status=response.get("status"),
            duration_ms=round((asyncio.get_running_loop().time() - started_at) * 1000),
        )
        return response
