"""Tests for macOS dialog behavior."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ask_human.dialogs import MACOS_DIALOG_SCRIPT, GUIDialogHandler
from ask_human.macos_prompt import (
    MACOS_PROMPT_DOCUMENT_VERSION,
    build_macos_prompt_document,
)

MACOS_NATIVE = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires the macOS JXA and AppKit runtimes",
)


class FakeProcess:
    """Minimal async subprocess stub for dialog tests."""

    def __init__(self, stdout=b"", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.communicated_input = None

    async def communicate(self, input=None):
        """Return the configured AppleScript response."""
        self.communicated_input = input
        return (self.stdout, b"")


def _run_macos_jxa_probe(tmp_path: Path, filename: str, body: str):
    source = MACOS_DIALOG_SCRIPT.read_text().replace(
        "function run(argv)",
        "function productionRun(argv)",
        1,
    )
    probe_path = tmp_path / filename
    probe_path.write_text(source + body)
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", str(probe_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def test_macos_dialog_invokes_packaged_jxa_with_prompt_document_on_stdin():
    """Pass UI arguments separately and send the prompt document through stdin."""
    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        captured["process"] = FakeProcess(returncode=1)
        return captured["process"]

    handler = GUIDialogHandler("Custom Title")

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        result = asyncio.run(handler._macos_dialog("Question?", 10))

    assert result is None
    assert captured["args"][0] == "osascript"
    assert captured["args"][1:4] == ("-l", "JavaScript", str(MACOS_DIALOG_SCRIPT))
    assert captured["args"][4] == "Custom Title"
    assert captured["args"][5].endswith("agent-asks.icns")
    assert captured["args"][6] == "10"
    assert captured["kwargs"]["stdin"] == asyncio.subprocess.PIPE
    prompt_document = json.loads(captured["process"].communicated_input)
    assert prompt_document == {
        "version": MACOS_PROMPT_DOCUMENT_VERSION,
        "fallback_text": "Question?",
        "show_format_toggle": False,
        "rendered_blocks": [
            {
                "type": "paragraph",
                "spans": [{"text": "Question?"}],
                "paragraph_break": True,
                "blank_line_after": True,
            }
        ],
        "plain_blocks": [
            {
                "type": "paragraph",
                "spans": [{"text": "Question?"}],
                "paragraph_break": True,
                "blank_line_after": True,
            }
        ],
    }
    assert MACOS_DIALOG_SCRIPT.is_file()


def test_macos_dialog_sends_supplied_rendered_document():
    """Keep structured rendered blocks intact across the Python-to-JXA boundary."""
    captured = {}
    document = {
        "version": MACOS_PROMPT_DOCUMENT_VERSION,
        "fallback_text": "**Question?**",
        "show_format_toggle": True,
        "rendered_blocks": [
            {
                "type": "paragraph",
                "spans": [{"text": "Question?", "bold": True}],
                "paragraph_break": True,
                "blank_line_after": True,
            }
        ],
        "plain_blocks": [
            {
                "type": "paragraph",
                "spans": [{"text": "**Question?**"}],
                "paragraph_break": True,
                "blank_line_after": True,
            }
        ],
    }

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["process"] = FakeProcess(returncode=1)
        return captured["process"]

    handler = GUIDialogHandler()
    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        result = asyncio.run(handler._macos_dialog("fallback", 10, prompt_document=document))

    assert result is None
    assert json.loads(captured["process"].communicated_input) == document


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


@MACOS_NATIVE
def test_packaged_macos_dialog_routes_link_clicks_to_workspace(tmp_path):
    """Use an explicit text-view delegate for reliable JXA link handling."""
    probe = r"""
function run(_argv) {
    var openedLink = null;
    openPromptLink = function (link) {
        openedLink = String(ObjC.unwrap(link.absoluteString));
        return true;
    };

    var controller = $.AskHumanDialogController.alloc.init;
    var textView = makeTextView(640, 180, false);
    textView.setDelegate(controller);
    var url = $.NSURL.URLWithString("file:///tmp/ask-human-link-probe.txt");

    var handled = Boolean(ObjC.unwrap(
        controller.textViewClickedOnLinkAtIndex(textView, url, 7)
    ));
    textView.clickedOnLinkAtIndex(url, 11);
    return JSON.stringify({handled: handled, opened_link: openedLink});
}
"""

    assert _run_macos_jxa_probe(tmp_path, "link-click-probe.js", probe) == {
        "handled": True,
        "opened_link": "file:///tmp/ask-human-link-probe.txt",
    }


@MACOS_NATIVE
def test_packaged_macos_dialog_lays_out_native_code_blocks(tmp_path):
    """Keep valid native blocks, full-width backgrounds, and nested insets."""
    list_first_document = build_macos_prompt_document(
        "- ```text\n  list-first code\n  ```",
        "",
        fallback_text="",
        timeout_seconds=90,
        include_timing_info=False,
    )
    list_first_blocks = json.dumps(list_first_document["rendered_blocks"], ensure_ascii=False)
    wrapped_quote = "list-first quote " + "wrapped continuation " * 20
    inline_first_document = build_macos_prompt_document(
        f"- # list-first heading\n- > {wrapped_quote}\n  >\n  > second quote paragraph",
        "",
        fallback_text="",
        timeout_seconds=90,
        include_timing_info=False,
    )
    inline_first_blocks = json.dumps(inline_first_document["rendered_blocks"], ensure_ascii=False)
    continuation_document = build_macos_prompt_document(
        "- ordinary list first line\n"
        "  ordinary list second line\n\n"
        "> root quote first line\n"
        "> root quote second line",
        "",
        fallback_text="",
        timeout_seconds=90,
        include_timing_info=False,
    )
    continuation_blocks = json.dumps(
        continuation_document["rendered_blocks"],
        ensure_ascii=False,
    )
    quoted_code_document = build_macos_prompt_document(
        "- > ```text\n  > quoted list-first code\n  > ```",
        "",
        fallback_text="",
        timeout_seconds=90,
        include_timing_info=False,
    )
    quoted_code_blocks = json.dumps(quoted_code_document["rendered_blocks"], ensure_ascii=False)
    thematic_break_document = build_macos_prompt_document(
        "- * * *",
        "",
        fallback_text="",
        timeout_seconds=90,
        include_timing_info=False,
    )
    thematic_break_blocks = json.dumps(
        thematic_break_document["rendered_blocks"],
        ensure_ascii=False,
    )
    probe = r"""
function probeNumber(value) {
    return Number(ObjC.unwrap(value));
}

function probeRect(value) {
    return {
        x: probeNumber(value.origin.x),
        y: probeNumber(value.origin.y),
        width: probeNumber(value.size.width),
        height: probeNumber(value.size.height)
    };
}

function paragraphSnapshot(view, characterIndex) {
    var style = view.textStorage.attributeAtIndexEffectiveRange(
        $.NSParagraphStyleAttributeName,
        characterIndex,
        null
    );
    return {
        indent: probeNumber(style.headIndent),
        first_indent: probeNumber(style.firstLineHeadIndent),
        spacing: probeNumber(style.paragraphSpacing)
    };
}

function codeBlockSnapshot(value, view, characterIndex) {
    var style = value.attributeAtIndexEffectiveRange(
        $.NSParagraphStyleAttributeName,
        characterIndex,
        null
    );
    var blocks = style.textBlocks;
    var blockCount = probeNumber(blocks.count);
    var classes = [];
    for (var index = 0; index < blockCount; index += 1) {
        classes.push(String(ObjC.unwrap(blocks.objectAtIndex(index).className)));
    }
    var innerBlock = blocks.objectAtIndex(blockCount - 1);
    var glyphIndex = probeNumber(
        view.layoutManager.glyphIndexForCharacterAtIndex(characterIndex)
    );
    var bounds = view.layoutManager.boundsRectForTextBlockAtIndexEffectiveRange(
        innerBlock,
        glyphIndex,
        null
    );
    var line = view.layoutManager.lineFragmentUsedRectForGlyphAtIndexEffectiveRange(
        glyphIndex,
        null
    );
    var origin = view.textContainerOrigin;
    var backgroundColor = innerBlock.backgroundColor;
    return {
        block_count: blockCount,
        classes: classes,
        bounds: probeRect(bounds),
        view_bounds: {
            x: probeNumber(bounds.origin.x) + probeNumber(origin.x),
            y: probeNumber(bounds.origin.y) + probeNumber(origin.y),
            width: probeNumber(bounds.size.width),
            height: probeNumber(bounds.size.height)
        },
        line: probeRect(line),
        background_matches: Boolean(ObjC.unwrap(
            backgroundColor.isEqual($.NSColor.controlBackgroundColor)
        )),
        content_width: probeNumber(innerBlock.contentWidth),
        left_padding: probeNumber(
            innerBlock.widthForLayerEdge($.NSTextBlockPadding, $.NSMinXEdge)
        )
    };
}

function run(_argv) {
    var blocks = [
        {
            type: "paragraph",
            spans: [{text: "before root"}],
            paragraph_break: true,
            blank_line_after: false
        },
        {
            type: "code_block",
            spans: [{text: "root code\n", code: true}],
            paragraph_break: false,
            blank_line_after: false,
            paragraph: {indent: 14, first_indent: 14, container_indent: 0}
        },
        {
            type: "paragraph",
            spans: [{text: "after root"}],
            paragraph_break: true,
            blank_line_after: false
        },
        {
            type: "paragraph",
            spans: [{text: "before nested"}],
            paragraph_break: true,
            blank_line_after: false
        },
        {
            type: "code_block",
            spans: [{text: "nested code\n", code: true}],
            paragraph_break: false,
            blank_line_after: false,
            paragraph: {indent: 32, first_indent: 32, container_indent: 18}
        },
        {
            type: "paragraph",
            spans: [{text: "after nested"}],
            paragraph_break: true,
            blank_line_after: false
        },
        {
            type: "paragraph",
            spans: [{text: "before empty"}],
            paragraph_break: true,
            blank_line_after: false
        },
        {
            type: "code_block",
            spans: [{text: "\n", code: true}],
            paragraph_break: false,
            blank_line_after: false,
            paragraph: {indent: 14, first_indent: 14, container_indent: 0}
        },
        {
            type: "paragraph",
            spans: [{text: "after empty"}],
            paragraph_break: true,
            blank_line_after: true
        }
    ].concat(
        __LIST_FIRST_BLOCKS__,
        __INLINE_FIRST_BLOCKS__,
        __CONTINUATION_BLOCKS__,
        __QUOTED_CODE_BLOCKS__,
        __THEMATIC_BREAK_BLOCKS__
    );
    var value = makeAttributedBlocks(blocks, "");
    var text = String(ObjC.unwrap(value.string));
    var contentHeight = measurePromptContentHeight(value);
    var view = makeTextView(640, Math.max(500, contentHeight), false);
    view.textStorage.setAttributedString(value);
    var scrollView = makeScrollView(
        $.NSMakeRect(0, 0, 640, 500),
        view,
        true
    );
    var glyphRange = view.layoutManager.glyphRangeForTextContainer(
        view.textContainer
    );
    view.layoutManager.ensureLayoutForGlyphRange(glyphRange);
    var characterRange = view.layoutManager.characterRangeForGlyphRangeActualGlyphRange(
        glyphRange,
        null
    );
    var root = codeBlockSnapshot(value, view, text.indexOf("root code"));
    var nested = codeBlockSnapshot(value, view, text.indexOf("nested code"));
    var empty = codeBlockSnapshot(value, view, text.indexOf("after empty") - 1);
    var listCode = codeBlockSnapshot(value, view, text.indexOf("list-first code"));
    var quotedListCode = codeBlockSnapshot(
        value,
        view,
        text.indexOf("quoted list-first code")
    );
    var listMarkerIndex = probeNumber(
        view.layoutManager.glyphIndexForCharacterAtIndex(text.indexOf("• "))
    );
    var listMarkerLine = view.layoutManager.lineFragmentUsedRectForGlyphAtIndexEffectiveRange(
        listMarkerIndex,
        null
    );
    var headingIndex = text.indexOf("list-first heading");
    var headingMarkerIndex = text.lastIndexOf("• ", headingIndex);
    var headingMarkerGlyph = probeNumber(
        view.layoutManager.glyphIndexForCharacterAtIndex(headingMarkerIndex)
    );
    var headingGlyph = probeNumber(
        view.layoutManager.glyphIndexForCharacterAtIndex(headingIndex)
    );
    var headingMarkerLine = view.layoutManager.lineFragmentUsedRectForGlyphAtIndexEffectiveRange(
        headingMarkerGlyph,
        null
    );
    var headingLine = view.layoutManager.lineFragmentUsedRectForGlyphAtIndexEffectiveRange(
        headingGlyph,
        null
    );
    var quoteIndex = text.indexOf("list-first quote");
    var quoteMarkerIndex = text.lastIndexOf("• ", quoteIndex);
    var quotePrefixIndex = text.lastIndexOf("│ ", quoteIndex);
    var secondQuoteIndex = text.indexOf("second quote paragraph");
    var listFirstLineIndex = text.indexOf("ordinary list first line");
    var listSecondLineIndex = text.indexOf("ordinary list second line");
    var rootQuoteFirstLineIndex = text.indexOf("root quote first line");
    var rootQuoteSecondLineIndex = text.indexOf("root quote second line");
    var quoteMarkerGlyph = probeNumber(
        view.layoutManager.glyphIndexForCharacterAtIndex(quoteMarkerIndex)
    );
    var quoteGlyph = probeNumber(
        view.layoutManager.glyphIndexForCharacterAtIndex(quoteIndex)
    );
    var quoteMarkerLine = view.layoutManager.lineFragmentUsedRectForGlyphAtIndexEffectiveRange(
        quoteMarkerGlyph,
        null
    );
    var quoteLine = view.layoutManager.lineFragmentUsedRectForGlyphAtIndexEffectiveRange(
        quoteGlyph,
        null
    );
    var quoteEnd = text.indexOf("\n", quoteIndex);
    var quoteGlyphRange = view.layoutManager.glyphRangeForCharacterRangeActualCharacterRange(
        $.NSMakeRange(quoteIndex, quoteEnd - quoteIndex),
        null
    );
    var quoteBounds = view.layoutManager.boundingRectForGlyphRangeInTextContainer(
        quoteGlyphRange,
        view.textContainer
    );
    var thematicBreak = Array(33).join("─");
    var thematicBreakIndex = text.indexOf(thematicBreak);
    var thematicMarkerIndex = text.lastIndexOf("• ", thematicBreakIndex);
    var thematicMarkerGlyph = probeNumber(
        view.layoutManager.glyphIndexForCharacterAtIndex(thematicMarkerIndex)
    );
    var thematicBreakGlyph = probeNumber(
        view.layoutManager.glyphIndexForCharacterAtIndex(thematicBreakIndex)
    );
    var thematicMarkerLine = view.layoutManager.lineFragmentUsedRectForGlyphAtIndexEffectiveRange(
        thematicMarkerGlyph,
        null
    );
    var thematicBreakLine = view.layoutManager.lineFragmentUsedRectForGlyphAtIndexEffectiveRange(
        thematicBreakGlyph,
        null
    );
    var afterIndex = probeNumber(
        view.layoutManager.glyphIndexForCharacterAtIndex(text.indexOf("after nested"))
    );
    var afterLine = view.layoutManager.lineFragmentUsedRectForGlyphAtIndexEffectiveRange(
        afterIndex,
        null
    );
    return JSON.stringify({
        text_length: text.length,
        character_range: {
            location: probeNumber(characterRange.location),
            length: probeNumber(characterRange.length)
        },
        view_width: probeNumber(view.frame.size.width),
        view_height: probeNumber(view.frame.size.height),
        text_container_width: probeNumber(view.textContainer.containerSize.width),
        line_fragment_padding: probeNumber(view.textContainer.lineFragmentPadding),
        scroll_view_attached: Boolean(ObjC.unwrap(view.enclosingScrollView.isEqual(scrollView))),
        used_rect: probeRect(view.layoutManager.usedRectForTextContainer(view.textContainer)),
        root: root,
        nested: nested,
        empty: empty,
        list_code: listCode,
        quoted_list_code: quotedListCode,
        list_marker_line: probeRect(listMarkerLine),
        heading_marker_line: probeRect(headingMarkerLine),
        heading_line: probeRect(headingLine),
        quote_marker_line: probeRect(quoteMarkerLine),
        quote_line: probeRect(quoteLine),
        quote_bounds: probeRect(quoteBounds),
        quote_marker_paragraph: paragraphSnapshot(view, quoteMarkerIndex),
        quote_prefix_paragraph: paragraphSnapshot(view, quotePrefixIndex),
        quote_paragraph: paragraphSnapshot(view, quoteIndex),
        second_quote_paragraph: paragraphSnapshot(view, secondQuoteIndex),
        list_first_line_paragraph: paragraphSnapshot(view, listFirstLineIndex),
        list_second_line_paragraph: paragraphSnapshot(view, listSecondLineIndex),
        root_quote_first_line_paragraph: paragraphSnapshot(view, rootQuoteFirstLineIndex),
        root_quote_second_line_paragraph: paragraphSnapshot(view, rootQuoteSecondLineIndex),
        thematic_marker_line: probeRect(thematicMarkerLine),
        thematic_break_line: probeRect(thematicBreakLine),
        after_line: probeRect(afterLine)
    });
}
"""
    probe = probe.replace("__LIST_FIRST_BLOCKS__", list_first_blocks)
    probe = probe.replace("__INLINE_FIRST_BLOCKS__", inline_first_blocks)
    probe = probe.replace("__CONTINUATION_BLOCKS__", continuation_blocks)
    probe = probe.replace("__QUOTED_CODE_BLOCKS__", quoted_code_blocks)
    probe = probe.replace("__THEMATIC_BREAK_BLOCKS__", thematic_break_blocks)
    layout = _run_macos_jxa_probe(tmp_path, "code-block-layout-probe.js", probe)
    root = layout["root"]
    nested = layout["nested"]
    empty = layout["empty"]
    list_code = layout["list_code"]
    quoted_list_code = layout["quoted_list_code"]

    assert layout["character_range"] == {
        "location": 0,
        "length": layout["text_length"],
    }
    assert layout["scroll_view_attached"] is True
    assert layout["text_container_width"] <= layout["view_width"]
    assert layout["used_rect"]["height"] <= layout["view_height"]
    assert root["block_count"] == 1
    assert nested["block_count"] == 2
    assert empty["block_count"] == 1
    assert list_code["block_count"] == 2
    assert root["classes"] == ["NSTextBlock"]
    assert nested["classes"] == ["NSTextBlock", "NSTextBlock"]
    assert root["content_width"] == nested["content_width"] == 100
    assert root["left_padding"] == nested["left_padding"] == 6
    assert all(
        block["background_matches"] for block in (root, nested, empty, list_code, quoted_list_code)
    )
    assert root["view_bounds"]["x"] >= 0
    assert root["view_bounds"]["x"] + root["view_bounds"]["width"] <= (
        layout["view_width"] + layout["line_fragment_padding"]
    )
    assert nested["bounds"]["x"] - root["bounds"]["x"] == 18
    assert nested["bounds"]["x"] + nested["bounds"]["width"] == (
        root["bounds"]["x"] + root["bounds"]["width"]
    )
    assert nested["line"]["x"] - root["line"]["x"] == 18
    assert empty["bounds"]["height"] > 0
    assert list_code["bounds"]["width"] > 0
    assert list_code["bounds"]["height"] > 0
    assert list_code["line"]["y"] > layout["list_marker_line"]["y"]
    assert quoted_list_code["bounds"]["width"] > 0
    assert quoted_list_code["bounds"]["height"] > 0
    assert layout["heading_line"]["y"] == layout["heading_marker_line"]["y"]
    assert layout["quote_line"]["y"] == layout["quote_marker_line"]["y"]
    assert layout["quote_bounds"]["height"] > layout["quote_line"]["height"]
    assert (
        layout["quote_marker_paragraph"]
        == layout["quote_prefix_paragraph"]
        == layout["quote_paragraph"]
        == {"indent": 46, "first_indent": 10, "spacing": 6}
    )
    assert layout["second_quote_paragraph"] == {
        "indent": 46,
        "first_indent": 46,
        "spacing": 6,
    }
    assert layout["list_first_line_paragraph"] == {
        "indent": 28,
        "first_indent": 10,
        "spacing": 0,
    }
    assert layout["list_second_line_paragraph"] == {
        "indent": 28,
        "first_indent": 28,
        "spacing": 0,
    }
    assert layout["root_quote_first_line_paragraph"] == {
        "indent": 20,
        "first_indent": 8,
        "spacing": 6,
    }
    assert layout["root_quote_second_line_paragraph"] == {
        "indent": 20,
        "first_indent": 20,
        "spacing": 6,
    }
    assert layout["thematic_break_line"]["y"] > layout["thematic_marker_line"]["y"]
    expected_thematic_x = (
        layout["text_container_width"] - layout["thematic_break_line"]["width"]
    ) / 2
    assert abs(layout["thematic_break_line"]["x"] - expected_thematic_x) < 1
    assert layout["after_line"]["y"] >= (nested["bounds"]["y"] + nested["bounds"]["height"])


@MACOS_NATIVE
def test_packaged_macos_dialog_uses_drawable_document_fonts_for_unicode(tmp_path):
    """Let AppKit's document fonts retain drawable Unicode through view replacement."""
    probe = r"""
function probeCharacter(textView, character) {
    var nativeText = textView.textStorage.string;
    var range = nativeText.rangeOfString($(character));
    var characterIndex = Number(ObjC.unwrap(range.location));
    var layoutManager = textView.layoutManager;
    var glyphIndex = Number(
        layoutManager.glyphIndexForCharacterAtIndex(characterIndex)
    );
    var font = textView.textStorage.attributeAtIndexEffectiveRange(
        $.NSFontAttributeName,
        characterIndex,
        null
    );
    return {
        character: character,
        font: String(ObjC.unwrap(font.fontName)),
        glyph: Number(layoutManager.CGGlyphAtIndex(glyphIndex)),
        property: Number(layoutManager.propertyForGlyphAtIndex(glyphIndex))
    };
}

function run(_argv) {
    var symbols = ["│", "∇", "∗", "∘", "∼", "≡", "⋅", "⌀", "♭", "♮", "♯", "➤"];
    var sequences = ["👨‍👩‍👧‍👦", "🇺🇳", "👍🏽", "❤️", "1️⃣"];
    var value = makeAttributedBlocks([
        {
            type: "paragraph",
            spans: [{text: "Header"}],
            paragraph_break: true,
            blank_line_after: false
        },
        {
            type: "paragraph",
            spans: [{
                text: symbols.join(" ") + " 日本語 العربية 🤖 é x " + sequences.join(" "),
                italic: true
            }],
            paragraph_break: true,
            blank_line_after: true
        }
    ], "");
    var targets = symbols.concat(["日", "ع", "🤖", "é", "x"]).concat(sequences);
    promptViewportHeight = 180;
    promptTextView = makeTextView(640, 180, false);
    promptTextView.textStorage.setAttributedString(value);
    promptScrollView = makeScrollView(
        $.NSMakeRect(0, 0, 640, promptViewportHeight),
        promptTextView,
        true
    );
    var glyphRange = promptTextView.layoutManager.glyphRangeForTextContainer(
        promptTextView.textContainer
    );
    promptTextView.layoutManager.ensureLayoutForGlyphRange(glyphRange);
    var initial = targets.map(function (character) {
        return probeCharacter(promptTextView, character);
    });

    var plain = makeAttributedBlocks([plainPromptBlock("> Plain 🤖")], "");
    applyPromptValue(plain, measurePromptContentHeight(plain));
    applyPromptValue(value, measurePromptContentHeight(value));
    var afterToggle = targets.map(function (character) {
        return probeCharacter(promptTextView, character);
    });
    var fallbackValue = makeFallbackAttributedString("Fallback 🤖");
    var fallbackView = makeTextView(640, 60, false);
    fallbackView.textStorage.setAttributedString(fallbackValue);
    fallbackView.layoutManager.ensureLayoutForTextContainer(fallbackView.textContainer);
    return JSON.stringify({
        initial: initial,
        after_toggle: afterToggle,
        fallback: probeCharacter(fallbackView, "🤖")
    });
}
"""
    probe_result = _run_macos_jxa_probe(tmp_path, "document-font-probe.js", probe)
    snapshots = probe_result["initial"]

    assert all(snapshot["glyph"] > 0 for snapshot in snapshots)
    assert all(snapshot["property"] == 0 for snapshot in snapshots)
    assert probe_result["after_toggle"] == snapshots
    assert probe_result["fallback"]["glyph"] > 0
    assert probe_result["fallback"]["property"] == 0
