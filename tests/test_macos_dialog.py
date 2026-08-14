"""Tests for macOS dialog behavior."""

import asyncio
import json
from unittest.mock import patch

from ask_human.dialogs import MACOS_DIALOG_SCRIPT, GUIDialogHandler


class FakeProcess:
    """Minimal async subprocess stub for dialog tests."""

    def __init__(self, stdout=b"", returncode=0):
        self.stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        """Return the configured AppleScript response."""
        return (self.stdout, b"")


def test_macos_dialog_invokes_packaged_jxa_with_prompt_arguments():
    """Pass the title, prompt, icon, and timeout to the packaged AppKit script."""
    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess(returncode=1)

    handler = GUIDialogHandler("Custom Title")

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        result = asyncio.run(handler._macos_dialog("Question?", 10))

    assert result is None
    assert captured["args"][0] == "osascript"
    assert captured["args"][1:4] == ("-l", "JavaScript", str(MACOS_DIALOG_SCRIPT))
    assert captured["args"][4] == "Custom Title"
    assert captured["args"][5] == "Question?"
    assert captured["args"][6].endswith("agent-asks.icns")
    assert captured["args"][7] == "10"
    assert MACOS_DIALOG_SCRIPT.is_file()


def test_macos_dialog_preserves_commas_in_response():
    """Return the entered text directly instead of parsing AppleScript's result record."""

    async def fake_create_subprocess_exec(*args, **kwargs):
        payload = json.dumps({"status": "ok", "value": "ok, thanks, works"})
        return FakeProcess(stdout=payload.encode())

    handler = GUIDialogHandler()

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        result = asyncio.run(handler._macos_dialog("Question?", 10))

    assert result == "ok, thanks, works"


def test_macos_dialog_preserves_multiline_and_outer_whitespace():
    """Decode the structured result without stripping the user's response."""

    async def fake_create_subprocess_exec(*args, **kwargs):
        payload = json.dumps({"status": "ok", "value": "  line one\nline two  "})
        return FakeProcess(stdout=payload.encode())

    handler = GUIDialogHandler()

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        result = asyncio.run(handler._macos_dialog("Question?", 10))

    assert result == "  line one\nline two  "


def test_macos_dialog_preserves_unicode_controls_and_escape_sequences():
    """Decode JSON-escaped text without corrupting valid Unicode or controls."""
    response = (
        'quote " backslash \\ literal \\n '
        "CR\rCRLF\r\nLF\n"
        "tab\tNUL\x00bell\x07 "
        "emoji 🤖 family 👨\u200d👩\u200d👧\u200d👦 combining e\u0301 "
        "separators \u2028\u2029 bidi \u2066LTR\u2069"
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        payload = json.dumps({"status": "ok", "value": response})
        return FakeProcess(stdout=payload.encode())

    handler = GUIDialogHandler()

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        result = asyncio.run(handler._macos_dialog("Question?", 10))

    assert result == response


def test_macos_dialog_preserves_empty_ok_response():
    """Keep an empty submitted answer distinct from cancellation or timeout."""

    async def fake_create_subprocess_exec(*args, **kwargs):
        payload = json.dumps({"status": "ok", "value": ""})
        return FakeProcess(stdout=payload.encode())

    handler = GUIDialogHandler()

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        result = asyncio.run(handler._macos_dialog("Question?", 10))

    assert result == ""


def test_macos_dialog_returns_none_for_cancel_or_timeout():
    """Map non-answer statuses to the existing no-response result."""

    async def run_status(status):
        async def fake_create_subprocess_exec(*args, **kwargs):
            return FakeProcess(stdout=json.dumps({"status": status}).encode())

        handler = GUIDialogHandler()
        with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
            return await handler._macos_dialog("Question?", 10)

    assert asyncio.run(run_status("cancelled")) is None
    assert asyncio.run(run_status("timeout")) is None


def test_packaged_macos_dialog_has_expected_window_behavior():
    """Keep the validated Dock, stacking, multiline, and shortcut mechanisms."""
    script = MACOS_DIALOG_SCRIPT.read_text()

    assert "NSApplicationActivationPolicyRegular" in script
    assert "respondsToSelector" in script
    assert "activateIgnoringOtherApps" in script
    assert "NSFloatingWindowLevel" in script
    assert "NSNormalWindowLevel" in script
    assert "WINDOW_LEVEL_GRACE_SECONDS = 0.5" in script
    assert "NSTextView" in script
    assert "TEXT_AREA_WIDTH = 640" in script
    assert "MIN_PROMPT_AREA_HEIGHT = 150" in script
    assert "MAX_PROMPT_AREA_HEIGHT = 520" in script
    assert "PROMPT_SCREEN_HEIGHT_RATIO = 0.45" in script
    assert "resolvePromptAreaHeight(promptTextView)" in script
    assert "glyphRangeForTextContainer" in script
    assert "ensureLayoutForGlyphRange" in script
    assert "usedRectForTextContainer" in script
    assert "RESPONSE_AREA_HEIGHT = 180" in script
    assert "promptTextView.setString(prompt)" in script
    assert "promptScrollView" in script
    assert "setDrawsBackground(false)" in script
    assert "NSNoBorder" in script
    assert "alert.setIcon(icon)" in script
    assert 'setKeyEquivalent("\\r")' in script
