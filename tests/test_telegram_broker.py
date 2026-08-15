"""Tests for Telegram broker state and broker-mode entrypoint behavior."""

import asyncio
import json
import sys
from typing import Any, cast

from ask_human import server
from ask_human.broker_state import (
    TelegramBrokerIdentity,
    load_broker_state,
    load_or_create_broker_identity,
    persist_broker_listen_url,
    resolve_broker_state_dir,
    resolve_target_broker_state_dir,
)
from ask_human.telegram_broker import (
    build_broker_health_payload,
    build_broker_listen_url,
    create_telegram_broker_app,
    run_telegram_broker,
)
from ask_human.telegram_models import (
    TelegramConfig,
    TelegramPromptError,
    resolve_telegram_target_key,
)


def test_broker_identity_is_stable_for_one_state_dir(tmp_path):
    """Persist one broker identity per state directory."""
    first_identity = load_or_create_broker_identity(tmp_path)
    second_identity = load_or_create_broker_identity(tmp_path)

    assert second_identity == first_identity


def test_broker_label_override_is_persisted(tmp_path):
    """Store an explicit broker label for later reuse."""
    identity = load_or_create_broker_identity(tmp_path, broker_label="office-machine")
    reloaded_state = load_broker_state(tmp_path)

    assert identity.broker_label == "office-machine"
    assert reloaded_state is not None
    assert reloaded_state.identity.broker_label == "office-machine"


def test_broker_state_tracks_listen_url(tmp_path):
    """Persist the discovered local listen URL for health probes and reuse."""
    identity = load_or_create_broker_identity(tmp_path, broker_label="desk")
    persist_broker_listen_url(tmp_path, "http://127.0.0.1:7456")
    state = load_broker_state(tmp_path)

    assert state is not None
    assert state.identity == identity
    assert state.listen_url == "http://127.0.0.1:7456"


def test_resolve_broker_state_dir_expands_placeholders(monkeypatch, tmp_path):
    """Support cwd placeholders in explicit broker state-dir configuration."""
    monkeypatch.chdir(tmp_path)

    resolved = resolve_broker_state_dir("{cwd}/broker-state")

    assert resolved == (tmp_path / "broker-state").resolve()


def test_build_broker_listen_url_normalizes_wildcard_host():
    """Store a loopback URL for local discovery when binding to all interfaces."""
    assert build_broker_listen_url("0.0.0.0", 7456) == "http://127.0.0.1:7456"
    assert build_broker_listen_url("127.0.0.1", 7456) == "http://127.0.0.1:7456"


def test_build_broker_health_payload_contains_identity_and_url(tmp_path):
    """Expose stable identity and listen URL in broker health responses."""
    identity = load_or_create_broker_identity(tmp_path, broker_label="laptop")

    payload = build_broker_health_payload(
        identity,
        listen_url="http://127.0.0.1:7456",
        target_key="feedbeef",
    )

    assert payload["status"] == "ok"
    assert payload["broker_id"] == identity.broker_id
    assert payload["broker_label"] == "laptop"
    assert payload["listen_url"] == "http://127.0.0.1:7456"
    assert payload["target_key"] == "feedbeef"
    assert "version" in payload
    assert payload["supports_guarded_shutdown"] is True


def test_broker_prompt_cancels_when_client_disconnects(monkeypatch, tmp_path):
    """Stop Telegram polling when the local broker client no longer waits."""
    monkeypatch.setattr(
        "ask_human.telegram_broker.BROKER_DISCONNECT_POLL_SECONDS",
        0.01,
    )

    class FakeRequest:
        def __init__(self):
            self.disconnect_checks = 0

        async def json(self):
            return {
                "prompt_text": "Prompt text",
                "prompt_id": "QTEST-1234",
                "timeout_seconds": 300,
                "download_dir": str(tmp_path),
            }

        async def is_disconnected(self):
            self.disconnect_checks += 1
            return self.disconnect_checks >= 1

    class FakeTelegramClient:
        def __init__(self):
            self.cancelled = False

        async def ask_question(self, prompt_text, timeout_seconds, prompt_id, download_dir):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    telegram_client = FakeTelegramClient()
    app = create_telegram_broker_app(
        TelegramBrokerIdentity("abcd1234", "office"),
        listen_url="http://127.0.0.1:7456",
        telegram_client=cast(Any, telegram_client),
        target_key="feedbeef",
    )
    prompts_route = cast(
        Any,
        next(route for route in app.routes if getattr(route, "path", None) == "/prompts"),
    )

    response = asyncio.run(prompts_route.endpoint(FakeRequest()))

    assert response.status_code == 499
    assert b'"status":"cancelled"' in response.body
    assert telegram_client.cancelled is True


def test_broker_shutdown_cancels_pending_prompt(monkeypatch, tmp_path):
    """Cancel pending Telegram prompts before shutting the broker down."""
    monkeypatch.setattr(
        "ask_human.telegram_broker.BROKER_DISCONNECT_POLL_SECONDS",
        0.01,
    )

    class FakePromptRequest:
        async def json(self):
            return {
                "prompt_texts": ["Prompt text"],
                "prompt_id": "QTEST-1234",
                "timeout_seconds": 300,
                "download_dir": str(tmp_path),
            }

        async def is_disconnected(self):
            return False

    class FakeShutdownRequest:
        async def json(self):
            return {"force": True}

    class FakeTelegramClient:
        def __init__(self):
            self.cancelled = False
            self.shutdown_called = False

        async def ask_question(self, prompt_texts, timeout_seconds, prompt_id, download_dir):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def shutdown(self):
            self.shutdown_called = True

    shutdown_event = asyncio.Event()
    shutdown_calls = []
    telegram_client = FakeTelegramClient()
    app = create_telegram_broker_app(
        TelegramBrokerIdentity("abcd1234", "office"),
        listen_url="http://127.0.0.1:7456",
        telegram_client=cast(Any, telegram_client),
        target_key="feedbeef",
        shutdown_event=shutdown_event,
        request_shutdown=lambda: shutdown_calls.append("shutdown"),
    )
    prompts_route = cast(
        Any,
        next(route for route in app.routes if getattr(route, "path", None) == "/prompts"),
    )
    shutdown_route = cast(
        Any,
        next(route for route in app.routes if getattr(route, "path", None) == "/shutdown"),
    )

    async def run_shutdown_flow():
        prompt_task = asyncio.create_task(prompts_route.endpoint(FakePromptRequest()))
        await asyncio.sleep(0)
        shutdown_response = await shutdown_route.endpoint(FakeShutdownRequest())
        prompt_response = await prompt_task
        await asyncio.sleep(0.1)
        return shutdown_response, prompt_response

    shutdown_response, prompt_response = asyncio.run(run_shutdown_flow())

    assert shutdown_response.status_code == 200
    assert b'"status":"ok"' in shutdown_response.body
    assert b'"cancelled_prompt_count":1' in shutdown_response.body
    assert prompt_response.status_code == 499
    assert b"Broker shutdown requested" in prompt_response.body
    assert telegram_client.cancelled is True
    assert telegram_client.shutdown_called is True
    assert shutdown_calls == ["shutdown"]


def test_broker_graceful_shutdown_refuses_active_prompt(tmp_path):
    """Keep active prompts running when a graceful shutdown is requested."""

    class FakePromptRequest:
        async def json(self):
            return {
                "prompt_texts": ["Prompt text"],
                "prompt_id": "QTEST-1234",
                "timeout_seconds": 300,
                "download_dir": str(tmp_path),
            }

        async def is_disconnected(self):
            return False

    class FakeShutdownRequest:
        async def json(self):
            return {}

    class FakeTelegramClient:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.cancelled = False
            self.shutdown_called = False

        async def ask_question(self, prompt_texts, timeout_seconds, prompt_id, download_dir):
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return "telegram answer"

        async def shutdown(self):
            self.shutdown_called = True

    shutdown_calls = []
    telegram_client = FakeTelegramClient()
    app = create_telegram_broker_app(
        TelegramBrokerIdentity("abcd1234", "office"),
        listen_url="http://127.0.0.1:7456",
        telegram_client=cast(Any, telegram_client),
        target_key="feedbeef",
        shutdown_event=asyncio.Event(),
        request_shutdown=lambda: shutdown_calls.append("shutdown"),
    )
    prompts_route = cast(
        Any,
        next(route for route in app.routes if getattr(route, "path", None) == "/prompts"),
    )
    shutdown_route = cast(
        Any,
        next(route for route in app.routes if getattr(route, "path", None) == "/shutdown"),
    )

    async def run_shutdown_flow():
        prompt_task = asyncio.create_task(prompts_route.endpoint(FakePromptRequest()))
        await telegram_client.started.wait()
        shutdown_response = await shutdown_route.endpoint(FakeShutdownRequest())
        assert prompt_task.done() is False
        telegram_client.release.set()
        prompt_response = await prompt_task
        return shutdown_response, prompt_response

    shutdown_response, prompt_response = asyncio.run(run_shutdown_flow())

    assert shutdown_response.status_code == 409
    assert b'"status":"busy"' in shutdown_response.body
    assert b'"pending_prompt_count":1' in shutdown_response.body
    assert prompt_response.status_code == 200
    assert b'"response":"telegram answer"' in prompt_response.body
    assert telegram_client.cancelled is False
    assert telegram_client.shutdown_called is False
    assert shutdown_calls == []


def test_broker_rejects_new_prompt_after_shutdown_starts(tmp_path):
    """Do not admit prompt requests after an idle shutdown has been accepted."""

    class FakePromptRequest:
        async def json(self):
            return {
                "prompt_texts": ["Prompt text"],
                "prompt_id": "QTEST-1234",
                "timeout_seconds": 300,
                "download_dir": str(tmp_path),
            }

        async def is_disconnected(self):
            return False

    class FakeShutdownRequest:
        async def json(self):
            return {}

    class FakeTelegramClient:
        def __init__(self):
            self.shutdown_started = asyncio.Event()
            self.finish_shutdown = asyncio.Event()
            self.prompt_calls = 0

        async def ask_question(self, prompt_texts, timeout_seconds, prompt_id, download_dir):
            self.prompt_calls += 1
            return "unexpected answer"

        async def shutdown(self):
            self.shutdown_started.set()
            await self.finish_shutdown.wait()

    telegram_client = FakeTelegramClient()
    app = create_telegram_broker_app(
        TelegramBrokerIdentity("abcd1234", "office"),
        listen_url="http://127.0.0.1:7456",
        telegram_client=cast(Any, telegram_client),
        target_key="feedbeef",
    )
    prompts_route = cast(
        Any,
        next(route for route in app.routes if getattr(route, "path", None) == "/prompts"),
    )
    shutdown_route = cast(
        Any,
        next(route for route in app.routes if getattr(route, "path", None) == "/shutdown"),
    )

    async def run_shutdown_flow():
        shutdown_task = asyncio.create_task(shutdown_route.endpoint(FakeShutdownRequest()))
        await telegram_client.shutdown_started.wait()
        prompt_response = await prompts_route.endpoint(FakePromptRequest())
        telegram_client.finish_shutdown.set()
        shutdown_response = await shutdown_task
        return shutdown_response, prompt_response

    shutdown_response, prompt_response = asyncio.run(run_shutdown_flow())

    assert shutdown_response.status_code == 200
    assert prompt_response.status_code == 503
    assert b'"status":"unavailable"' in prompt_response.body
    assert telegram_client.prompt_calls == 0


def test_broker_shutdown_status_wins_over_prompt_error(tmp_path):
    """Return the shutdown status when the prompt task fails during broker shutdown."""

    class FakeRequest:
        async def json(self):
            return {
                "prompt_texts": ["Prompt text"],
                "prompt_id": "QTEST-1234",
                "timeout_seconds": 300,
                "download_dir": str(tmp_path),
            }

        async def is_disconnected(self):
            return False

    class FakeTelegramClient:
        async def ask_question(self, prompt_texts, timeout_seconds, prompt_id, download_dir):
            raise TelegramPromptError("Telegram broker shutdown requested.")

    shutdown_event = asyncio.Event()
    shutdown_event.set()
    app = create_telegram_broker_app(
        TelegramBrokerIdentity("abcd1234", "office"),
        listen_url="http://127.0.0.1:7456",
        telegram_client=cast(Any, FakeTelegramClient()),
        target_key="feedbeef",
        shutdown_event=shutdown_event,
    )
    prompts_route = cast(
        Any,
        next(route for route in app.routes if getattr(route, "path", None) == "/prompts"),
    )

    response = asyncio.run(prompts_route.endpoint(FakeRequest()))

    assert response.status_code == 499
    assert b"Broker shutdown requested" in response.body


def test_broker_prompt_forwards_multipart_prompt_texts(monkeypatch, tmp_path):
    """Forward every prompt message from the broker request to Telegram."""

    class FakeRequest:
        async def json(self):
            return {
                "prompt_texts": ["Prompt part 1", "Prompt part 2"],
                "prompt_id": "QTEST-1234",
                "timeout_seconds": 300,
                "download_dir": str(tmp_path),
            }

        async def is_disconnected(self):
            return False

    class FakeTelegramClient:
        def __init__(self):
            self.calls = []

        async def ask_question(self, prompt_texts, timeout_seconds, prompt_id, download_dir):
            self.calls.append((prompt_texts, timeout_seconds, prompt_id, download_dir))
            return "telegram answer"

    telegram_client = FakeTelegramClient()
    app = create_telegram_broker_app(
        TelegramBrokerIdentity("abcd1234", "office"),
        listen_url="http://127.0.0.1:7456",
        telegram_client=cast(Any, telegram_client),
        target_key="feedbeef",
    )
    prompts_route = cast(
        Any,
        next(route for route in app.routes if getattr(route, "path", None) == "/prompts"),
    )

    response = asyncio.run(prompts_route.endpoint(FakeRequest()))

    assert response.status_code == 200
    assert b'"status":"ok"' in response.body
    assert telegram_client.calls == [
        (["Prompt part 1", "Prompt part 2"], 300, "QTEST-1234", tmp_path.resolve())
    ]


def test_main_runs_telegram_broker_mode(monkeypatch, tmp_path):
    """Dispatch to broker mode before MCP transport setup."""
    captured = {}

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ask-human",
            "--telegram-broker",
            "--telegram",
            "123456:ABCDEF -1009876543210",
            "--telegram-broker-label",
            "office",
            "--telegram-broker-state-dir",
            str(tmp_path),
            "--telegram-debug-log",
            str(tmp_path / "telegram-debug.jsonl"),
            "--telegram-broker-host",
            "127.0.0.1",
            "--telegram-broker-port",
            "7456",
        ],
    )
    monkeypatch.setattr(
        server,
        "run_telegram_broker",
        lambda **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(server.mcp, "run", lambda transport: captured.setdefault("mcp_run", True))

    server.main()

    telegram_target = TelegramConfig("123456:ABCDEF", "-1009876543210")
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 7456
    assert captured["broker_label"] == "office"
    assert captured["debug_log_path"] == str(tmp_path / "telegram-debug.jsonl")
    assert captured["telegram_target"] == telegram_target
    assert captured["state_dir"] == resolve_target_broker_state_dir(
        tmp_path.resolve(), telegram_target
    )
    assert "mcp_run" not in captured


def test_run_telegram_broker_exits_cleanly_on_keyboard_interrupt(monkeypatch, tmp_path):
    """Treat Ctrl+C as a clean shutdown after Uvicorn finishes stopping."""

    class FakeSocket:
        def __init__(self):
            self.closed = False

        def setsockopt(self, *args):
            return None

        def bind(self, _address):
            return None

        def listen(self, _backlog):
            return None

        def getsockname(self):
            return ("127.0.0.1", 7456)

        def close(self):
            self.closed = True

    fake_socket = FakeSocket()

    class FakeServer:
        def __init__(self, config):
            self.config = config

        async def serve(self, sockets):
            assert sockets == [fake_socket]

    monkeypatch.setattr(
        "ask_human.telegram_broker.load_or_create_broker_identity",
        lambda state_dir, broker_label=None: load_or_create_broker_identity(
            state_dir, broker_label=broker_label
        ),
    )
    monkeypatch.setattr(
        "ask_human.telegram_broker._create_bound_socket",
        lambda host, port: fake_socket,
    )
    monkeypatch.setattr(
        "ask_human.telegram_broker.persist_broker_listen_url",
        lambda state_dir, listen_url: None,
    )
    monkeypatch.setattr(
        "ask_human.telegram_broker.create_telegram_broker_app",
        lambda identity, **kwargs: object(),
    )
    monkeypatch.setattr(
        "ask_human.telegram_broker.uvicorn.Config",
        lambda app, host, port, log_level: {
            "app": app,
            "host": host,
            "port": port,
            "log_level": log_level,
        },
    )
    monkeypatch.setattr(
        "ask_human.telegram_broker.uvicorn.Server",
        FakeServer,
    )
    monkeypatch.setattr(
        asyncio,
        "run",
        lambda coroutine: (
            coroutine.close(),
            (_ for _ in ()).throw(KeyboardInterrupt()),
        )[1],
    )

    run_telegram_broker(
        host="127.0.0.1",
        port=0,
        state_dir=tmp_path,
        telegram_target=TelegramConfig("123456:ABCDEF", "-1009876543210"),
        broker_label="office",
        debug_log_path=str(tmp_path / "telegram-debug.jsonl"),
    )

    assert fake_socket.closed is True
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "telegram-debug.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events == ["broker_start", "broker_stop"]
