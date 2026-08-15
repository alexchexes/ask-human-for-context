"""Real-socket cancellation tests for the local Telegram broker client."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional, cast

import pytest
import uvicorn

from ask_human import __version__, server
from ask_human.broker_state import TelegramBrokerHealth, TelegramBrokerIdentity
from ask_human.telegram_broker import (
    _create_bound_socket,
    build_broker_listen_url,
    create_telegram_broker_app,
)
from ask_human.telegram_broker_client import TelegramBrokerClient
from ask_human.telegram_models import TelegramConfig, TelegramPromptError


class ControllableTelegramClient:
    """Expose per-prompt start, cancellation, and reply controls."""

    def __init__(self) -> None:
        self.started: dict[str, asyncio.Event] = {}
        self.cancelled: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.any_started = asyncio.Event()
        self.any_cancelled = asyncio.Event()
        self.shutdown_called = asyncio.Event()

    def _event(self, events: dict[str, asyncio.Event], prompt_id: str) -> asyncio.Event:
        return events.setdefault(prompt_id, asyncio.Event())

    async def ask_question(
        self,
        _prompt_texts: list[str],
        _timeout_seconds: int,
        prompt_id: str,
        _download_dir: Path,
    ) -> Optional[str]:
        self._event(self.started, prompt_id).set()
        self.any_started.set()
        try:
            await self._event(self.release, prompt_id).wait()
        except asyncio.CancelledError:
            self._event(self.cancelled, prompt_id).set()
            self.any_cancelled.set()
            raise
        return f"reply for {prompt_id}"

    async def shutdown(self) -> None:
        self.shutdown_called.set()


@asynccontextmanager
async def running_broker(
    telegram_client: ControllableTelegramClient,
) -> AsyncIterator[str]:
    """Run a real loopback Uvicorn broker for one test."""
    server_socket = _create_bound_socket("127.0.0.1", 0)
    listen_url = build_broker_listen_url("127.0.0.1", int(server_socket.getsockname()[1]))
    app = create_telegram_broker_app(
        TelegramBrokerIdentity("abcd1234", "test-broker"),
        listen_url=listen_url,
        telegram_client=cast(Any, telegram_client),
        target_key="feedbeef",
    )
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="error",
            lifespan="off",
            timeout_graceful_shutdown=0,
        )
    )
    serve_task = asyncio.create_task(uvicorn_server.serve(sockets=[server_socket]))

    async def wait_until_started() -> None:
        while not uvicorn_server.started:
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_until_started(), timeout=3)
        yield listen_url
    finally:
        uvicorn_server.should_exit = True
        await asyncio.wait_for(serve_task, timeout=3)


def prompt_payload(prompt_id: str, download_dir: Path) -> dict[str, Any]:
    """Build one valid broker prompt payload."""
    return {
        "prompt_id": prompt_id,
        "prompt_texts": [f"Prompt {prompt_id}"],
        "timeout_seconds": 30,
        "download_dir": str(download_dir),
    }


def make_client(tmp_path: Path) -> TelegramBrokerClient:
    """Create a broker client with isolated test paths."""
    return TelegramBrokerClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path / "downloads",
        broker_state_root=tmp_path / "state",
    )


def test_broker_request_cancellation_disconnects_and_cancels_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Close the real HTTP connection when the awaiting client task is cancelled."""
    monkeypatch.setattr("ask_human.telegram_broker.BROKER_DISCONNECT_POLL_SECONDS", 0.01)

    async def scenario() -> None:
        telegram_client = ControllableTelegramClient()
        client = make_client(tmp_path)

        async with running_broker(telegram_client) as listen_url:
            request_task = asyncio.create_task(
                client._broker_request(
                    listen_url,
                    "prompts",
                    prompt_payload("QFIRST", tmp_path),
                    timeout=30,
                )
            )
            await asyncio.wait_for(
                telegram_client._event(telegram_client.started, "QFIRST").wait(),
                timeout=2,
            )

            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task

            await asyncio.wait_for(
                telegram_client._event(telegram_client.cancelled, "QFIRST").wait(),
                timeout=1,
            )

    asyncio.run(scenario())


def test_broker_client_fetches_health_over_async_transport(tmp_path: Path) -> None:
    """Keep the broker discovery GET path working through the async transport."""

    async def scenario() -> None:
        telegram_client = ControllableTelegramClient()
        client = make_client(tmp_path)

        async with running_broker(telegram_client) as listen_url:
            health = await client._fetch_health(listen_url)

        assert health == TelegramBrokerHealth(
            broker_id="abcd1234",
            broker_label="test-broker",
            listen_url=listen_url,
            target_key="feedbeef",
            version=__version__,
            supports_guarded_shutdown=True,
        )

    asyncio.run(scenario())


def test_cancelling_one_broker_request_keeps_another_prompt_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancel only the Telegram prompt owned by the disconnected request."""
    monkeypatch.setattr("ask_human.telegram_broker.BROKER_DISCONNECT_POLL_SECONDS", 0.01)

    async def scenario() -> None:
        telegram_client = ControllableTelegramClient()
        client = make_client(tmp_path)

        async with running_broker(telegram_client) as listen_url:
            first_task = asyncio.create_task(
                client._broker_request(
                    listen_url,
                    "prompts",
                    prompt_payload("QFIRST", tmp_path),
                    timeout=30,
                )
            )
            second_task = asyncio.create_task(
                client._broker_request(
                    listen_url,
                    "prompts",
                    prompt_payload("QSECOND", tmp_path),
                    timeout=30,
                )
            )
            await asyncio.gather(
                asyncio.wait_for(
                    telegram_client._event(telegram_client.started, "QFIRST").wait(),
                    timeout=2,
                ),
                asyncio.wait_for(
                    telegram_client._event(telegram_client.started, "QSECOND").wait(),
                    timeout=2,
                ),
            )

            first_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first_task
            await asyncio.wait_for(
                telegram_client._event(telegram_client.cancelled, "QFIRST").wait(),
                timeout=1,
            )
            assert telegram_client._event(telegram_client.cancelled, "QSECOND").is_set() is False

            telegram_client._event(telegram_client.release, "QSECOND").set()
            assert await second_task == {
                "status": "ok",
                "response": "reply for QSECOND",
            }

    asyncio.run(scenario())


def test_graceful_shutdown_preserves_real_broker_prompts(tmp_path: Path) -> None:
    """Carry the guarded HTTP 409 and active count through the real transport."""

    async def scenario() -> None:
        telegram_client = ControllableTelegramClient()
        client = make_client(tmp_path)

        async with running_broker(telegram_client) as listen_url:
            first_task = asyncio.create_task(
                client._broker_request(
                    listen_url,
                    "prompts",
                    prompt_payload("QFIRST", tmp_path),
                    timeout=30,
                )
            )
            second_task = asyncio.create_task(
                client._broker_request(
                    listen_url,
                    "prompts",
                    prompt_payload("QSECOND", tmp_path),
                    timeout=30,
                )
            )
            await asyncio.gather(
                asyncio.wait_for(
                    telegram_client._event(telegram_client.started, "QFIRST").wait(),
                    timeout=2,
                ),
                asyncio.wait_for(
                    telegram_client._event(telegram_client.started, "QSECOND").wait(),
                    timeout=2,
                ),
            )

            busy_result = await client._shutdown_broker(listen_url)
            assert busy_result.status == "busy"
            assert busy_result.pending_prompt_count == 2
            assert telegram_client.any_cancelled.is_set() is False
            assert telegram_client.shutdown_called.is_set() is False

            telegram_client._event(telegram_client.release, "QFIRST").set()
            telegram_client._event(telegram_client.release, "QSECOND").set()
            assert await asyncio.gather(first_task, second_task) == [
                {"status": "ok", "response": "reply for QFIRST"},
                {"status": "ok", "response": "reply for QSECOND"},
            ]

            stopped_result = await client._shutdown_broker(listen_url)
            assert stopped_result.status == "stopped"
            assert telegram_client.shutdown_called.is_set() is True

    asyncio.run(scenario())


def test_both_mode_local_dialog_win_cancels_real_broker_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Propagate a local-dialog win through HTTP disconnect to Telegram cancellation."""
    monkeypatch.setattr("ask_human.telegram_broker.BROKER_DISCONNECT_POLL_SECONDS", 0.01)

    async def scenario() -> None:
        telegram_client = ControllableTelegramClient()
        client = make_client(tmp_path)

        class DialogHandler:
            platform = "Darwin"

            async def get_user_input(
                self,
                _prompt: str,
                _timeout_seconds: int,
                *,
                cancel_event: Optional[asyncio.Event] = None,
                run_in_thread: bool = False,
            ) -> Optional[str]:
                del cancel_event, run_in_thread
                await asyncio.wait_for(telegram_client.any_started.wait(), timeout=2)
                return "local answer"

        async with running_broker(telegram_client) as listen_url:

            async def ensure_local_broker() -> TelegramBrokerHealth:
                return TelegramBrokerHealth(
                    broker_id="abcd1234",
                    broker_label="test-broker",
                    listen_url=listen_url,
                    target_key="feedbeef",
                    version=__version__,
                )

            monkeypatch.setattr(client, "_ensure_local_broker", ensure_local_broker)
            monkeypatch.setattr(server, "telegram_client", client)
            monkeypatch.setattr(server, "dialog_handler", DialogHandler())
            monkeypatch.setattr(server, "response_channel", "both")
            monkeypatch.setattr(server, "show_timing_info", False)
            monkeypatch.setattr(server, "dialog_timeout_seconds", 30)

            assert await server.ask_human("Question?", "Context.") == (
                "✅ User reply:\nlocal answer"
            )
            await asyncio.wait_for(telegram_client.any_cancelled.wait(), timeout=1)

    asyncio.run(scenario())


def test_broker_http_error_remains_a_telegram_prompt_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Preserve broker status and response details after changing HTTP transports."""
    monkeypatch.setattr("ask_human.telegram_broker.BROKER_DISCONNECT_POLL_SECONDS", 0.01)

    async def scenario() -> None:
        telegram_client = ControllableTelegramClient()
        client = make_client(tmp_path)

        async with running_broker(telegram_client) as listen_url:
            with pytest.raises(TelegramPromptError, match=r"HTTP 400: .*prompt_texts"):
                await client._broker_request(
                    listen_url,
                    "prompts",
                    {},
                    timeout=5,
                )

    asyncio.run(scenario())
