"""Tests for optional dialog timing metadata."""

import asyncio
import datetime as dt
import sys
from typing import cast

from ask_human import prompt_formatting, server


class StubDialogHandler:
    """Capture question payloads sent to the GUI layer."""

    def __init__(self):
        self.question = None
        self.timeout = None

    async def get_user_input(self, question, timeout):
        self.question = question
        self.timeout = timeout
        return "ok"


def test_build_timing_info_block_contains_note(monkeypatch):
    """Include both timestamps and the external-timeout note."""
    monkeypatch.setattr(
        prompt_formatting,
        "format_dialog_timestamp",
        lambda moment: moment.strftime("%d.%m.%Y %H:%M:%S"),
    )

    block = prompt_formatting.build_timing_info_block(dt.datetime(2026, 5, 10, 9, 30, 0), 90)

    assert block == (
        "Issued at: 10.05.2026 09:30:00\n"
        "Answer until: 10.05.2026 09:31:30"
        " (client may time out sooner)"
    )


def test_tool_keeps_default_prompt_shape_without_timing_info(monkeypatch):
    """Leave the dialog text unchanged when the flag is off."""
    stub = StubDialogHandler()
    monkeypatch.setattr(server, "dialog_handler", stub)
    monkeypatch.setattr(server, "show_timing_info", False)

    result = asyncio.run(server.ask_human("Question?"))

    assert result == "✅ User reply:\nok"
    assert stub.timeout == server.DEFAULT_DIALOG_TIMEOUT_SECONDS
    section_rule = "─" * 16
    assert stub.question == f"{section_rule} ❓ Question: {section_rule}\nQuestion?"


def test_initialize_time_locale_uses_system_default(monkeypatch):
    """Initialize time formatting from the OS locale once at startup."""
    calls = []

    monkeypatch.setattr(
        prompt_formatting.locale,
        "setlocale",
        lambda category, value=None: calls.append((category, value)) or "ok",
    )

    prompt_formatting.initialize_time_locale()

    assert calls == [(prompt_formatting.locale.LC_TIME, "")]


def test_format_dialog_timestamp_uses_locale_short_format():
    """Use locale-driven short date and time formatting when available."""

    class FakeMoment:
        def astimezone(self):
            return self

        def strftime(self, pattern):
            if pattern == "%x %X":
                return "10.05.2026 18:59:32"
            raise AssertionError(f"unexpected pattern: {pattern}")

    fake_moment = cast(dt.datetime, FakeMoment())

    assert prompt_formatting.format_dialog_timestamp(fake_moment) == "10.05.2026 18:59:32"


def test_main_initializes_time_locale_before_running(monkeypatch):
    """Initialize locale once during startup before serving requests."""
    events = []

    monkeypatch.setattr(sys, "argv", ["ask-human", "--transport", "stdio"])
    monkeypatch.setattr(server, "initialize_time_locale", lambda: events.append("locale"))
    monkeypatch.setattr(server.mcp, "run", lambda transport: events.append(("run", transport)))

    server.main()

    assert events == ["locale", ("run", "stdio")]


def test_tool_appends_timing_info_when_enabled(monkeypatch):
    """Append timing metadata only when the CLI flag is enabled."""
    stub = StubDialogHandler()
    monkeypatch.setattr(server, "dialog_handler", stub)
    monkeypatch.setattr(server, "show_timing_info", True)
    monkeypatch.setattr(
        prompt_formatting,
        "build_timing_info_block",
        lambda issued_at, timeout_seconds: "Issued at: 10.05.2026 09:30:00\n"
        "Answer until: 10.05.2026 09:31:30 (client may time out sooner)",
    )

    result = asyncio.run(
        server.ask_human(
            "Should I keep the current API shape?",
            "There are two valid implementation paths and the choice is user-facing.",
        )
    )

    assert result == "✅ User reply:\nok"
    separator = "─" * 39
    section_rule = "─" * 16
    assert stub.question == (
        f"{section_rule}  📋 Context:  {section_rule}\n"
        "There are two valid implementation paths and the choice is user-facing.\n\n"
        f"{section_rule} ❓ Question: {section_rule}\n"
        "Should I keep the current API shape?\n\n"
        f"{separator}\n"
        "Issued at: 10.05.2026 09:30:00\nAnswer until: 10.05.2026 09:31:30 "
        "(client may time out sooner)"
    )


def test_tool_uses_consistent_question_label_without_context(monkeypatch):
    """Keep the Question label consistent when timing info is enabled without context."""
    stub = StubDialogHandler()
    monkeypatch.setattr(server, "dialog_handler", stub)
    monkeypatch.setattr(server, "show_timing_info", True)
    monkeypatch.setattr(
        prompt_formatting,
        "build_timing_info_block",
        lambda issued_at, timeout_seconds: "Issued at: 10.05.2026 09:30:00\n"
        "Answer until: 10.05.2026 09:31:30 (client may time out sooner)",
    )

    result = asyncio.run(server.ask_human("Question without context?"))

    assert result == "✅ User reply:\nok"
    separator = "─" * 39
    section_rule = "─" * 16
    assert stub.question == (
        f"{section_rule} ❓ Question: {section_rule}\n"
        "Question without context?\n\n"
        f"{separator}\n"
        "Issued at: 10.05.2026 09:30:00\nAnswer until: 10.05.2026 09:31:30 "
        "(client may time out sooner)"
    )
