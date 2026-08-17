"""Tests for safe Markdown documents consumed by the macOS AppKit dialog."""

import datetime as dt
from pathlib import Path

from ask_human.macos_prompt import (
    MACOS_METADATA_DIVIDER_LENGTH,
    MACOS_PROMPT_DOCUMENT_VERSION,
    MACOS_SECTION_DIVIDER_LENGTH,
    build_macos_prompt_document,
    build_plain_macos_prompt_document,
)
from ask_human.prompt_formatting import build_prompt_text


def build_document(
    question: str,
    context: str = "",
    *,
    extra_note: str = "",
    include_timing_info: bool = False,
    working_directory: Path | None = None,
):
    issued_at = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.timezone.utc)
    fallback = build_prompt_text(
        question,
        context,
        timeout_seconds=90,
        include_timing_info=include_timing_info,
        extra_note=extra_note,
        issued_at=issued_at,
    )
    return build_macos_prompt_document(
        question,
        context,
        fallback_text=fallback,
        timeout_seconds=90,
        include_timing_info=include_timing_info,
        extra_note=extra_note,
        issued_at=issued_at,
        working_directory=working_directory,
    )


def blocks_text(blocks):
    parts = []
    for index, block in enumerate(blocks):
        parts.extend(span["text"] for span in block["spans"])
        if index + 1 == len(blocks):
            continue
        if block["paragraph_break"]:
            parts.append("\n")
        if block["blank_line_after"]:
            parts.append("\n")
    return "".join(parts)


def rendered_text(document):
    return blocks_text(document["rendered_blocks"])


def plain_text(document):
    return blocks_text(document["plain_blocks"])


def spans(document, presentation="rendered"):
    return [span for block in document[f"{presentation}_blocks"] for span in block["spans"]]


def block_with_text(document, text, presentation="rendered"):
    return next(
        block
        for block in document[f"{presentation}_blocks"]
        if "".join(span["text"] for span in block["spans"]) == text
    )


def test_plain_prompt_uses_versioned_blocks_without_unnecessary_toggle():
    """Do not show the plain/rendered switch for section chrome alone."""
    document = build_document("A plain question", "Plain context")
    divider = "─" * MACOS_SECTION_DIVIDER_LENGTH

    assert document["version"] == MACOS_PROMPT_DOCUMENT_VERSION
    assert document["show_format_toggle"] is False
    assert document["rendered_blocks"][0] == {
        "type": "paragraph",
        "spans": [
            {
                "text": f"{divider}  📋 Context:  {divider}",
                "bold": True,
                "secondary": True,
            }
        ],
        "paragraph_break": True,
        "blank_line_after": False,
        "paragraph": {"alignment": "center", "spacing": 10},
    }
    assert f"Plain context\n\n{divider}  ❓ Question:  {divider}\n" in rendered_text(document)
    assert document["fallback_text"] == build_prompt_text(
        "A plain question",
        "Plain context",
        timeout_seconds=90,
        include_timing_info=False,
        issued_at=dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.timezone.utc),
    )


def test_plain_blocks_keep_tool_chrome_styled_and_agent_markdown_exact():
    """Switch only agent-authored sections while retaining native dialog chrome."""
    context = "> **Context quote** with [local](README.md)"
    question = "## Choose `one`\n\n- first\n- second"
    document = build_document(
        question,
        context,
        extra_note="📨 Also sent to Telegram.",
    )

    assert document["show_format_toggle"] is True
    assert context in plain_text(document)
    assert question in plain_text(document)
    headers = [
        block
        for block in document["plain_blocks"]
        if "Context:" in block["spans"][0]["text"] or "Question:" in block["spans"][0]["text"]
    ]
    assert len(headers) == 2
    assert all(block["paragraph"]["alignment"] == "center" for block in headers)
    assert all(block["spans"][0].get("bold") for block in headers)
    assert block_with_text(document, context, "plain")["spans"] == [{"text": context}]
    assert block_with_text(document, question, "plain")["spans"] == [{"text": question}]
    assert any(
        span.get("secondary") and "Also sent" in span["text"] for span in spans(document, "plain")
    )


def test_commonmark_styles_are_encoded_as_blocks_and_spans():
    """Encode supported block and inline Markdown without HTML rendering."""
    document = build_document(
        "## Heading\n\n- **bold** and *italic* with `code`\n\n"
        "> [site](https://example.test)\n\n```py\nprint('ok')\n```"
    )
    rendered_spans = spans(document)

    assert document["show_format_toggle"] is True
    assert any(span.get("heading") == 2 and span.get("bold") for span in rendered_spans)
    assert any(span["text"] == "bold" and span.get("bold") for span in rendered_spans)
    assert any(span["text"] == "italic" and span.get("italic") for span in rendered_spans)
    assert any(
        span["text"] == "code" and span.get("code") and span.get("background")
        for span in rendered_spans
    )
    assert any(
        span["text"] == "site" and span.get("link") == "https://example.test"
        for span in rendered_spans
    )

    list_block = block_with_text(document, "• bold and italic with code")
    assert list_block["paragraph"] == {"indent": 28, "first_indent": 10}
    code_block = block_with_text(document, "print('ok')\n")
    assert code_block["type"] == "code_block"
    assert code_block["spans"] == [{"text": "print('ok')\n", "code": True}]


def test_every_textkit_paragraph_has_one_block_level_style():
    """Keep paragraph ownership explicit instead of repeating it on inline spans."""
    document = build_document(
        "- **first line\n  second line** after\n\n" "> quote\n\n```text\ncode\n```"
    )

    for block in document["rendered_blocks"]:
        assert block["type"] in {"paragraph", "code_block"}
        assert all("paragraph" not in span for span in block["spans"])
        assert isinstance(block.get("paragraph", {}), dict)


def test_code_blocks_preserve_parser_content_and_empty_rows():
    """Keep code exact while emitting only the needed inter-block spacing."""
    document = build_document("```text\nalpha\n\n\n```\n\nafter")
    code_block = block_with_text(document, "alpha\n\n\n")

    assert code_block["type"] == "code_block"
    assert code_block["paragraph_break"] is False
    assert "alpha\n\n\n\nafter" in rendered_text(document)

    final_code = build_document("```text\nomega\n\n```")
    assert rendered_text(final_code).endswith("omega\n\n")

    empty = build_document("before\n\n```text\n```\n\nafter")
    empty_block = block_with_text(empty, "\n")
    assert empty_block["type"] == "code_block"
    assert "before\n\n\n\nafter" in rendered_text(empty)


def test_image_syntax_and_raw_html_remain_literal_without_a_toggle():
    """Never fetch images or interpret raw HTML in the native prompt pane."""
    source = "![alt **text**](https://example.test/image.png)\n\n<script>x()</script>"
    document = build_document(source)

    assert document["show_format_toggle"] is False
    assert source in rendered_text(document)
    assert not any(span.get("link") for span in spans(document))
    assert not any(span.get("bold") and "text" in span["text"] for span in spans(document))


def test_safe_remote_and_local_links_are_clickable(tmp_path):
    """Resolve relative files at prompt creation and allow safe URL schemes."""
    document = build_document(
        "[web](https://example.test) [mail](mailto:user@example.test) "
        "[file](file:///tmp/readme.md) [relative](docs/readme.md)",
        working_directory=tmp_path,
    )
    links = {span["text"]: span["link"] for span in spans(document) if span.get("link")}

    assert links == {
        "web": "https://example.test",
        "mail": "mailto:user@example.test",
        "file": "file:///tmp/readme.md",
        "relative": (tmp_path / "docs/readme.md").resolve().as_uri(),
    }


def test_unsafe_unknown_and_remote_file_links_are_not_clickable():
    """Keep unsupported link labels readable without adding an NSLink attribute."""
    document = build_document(
        "[script](javascript:alert(1)) [custom](other:value) "
        "[remote](file://server/share/readme.md)"
    )

    assert not any(span.get("link") for span in spans(document))
    assert all(label in rendered_text(document) for label in ("script", "custom", "remote"))


def test_unclosed_fence_falls_back_per_section():
    """Render only the malformed section literally and parse the other normally."""
    context = "Context before\n```python\nprint('unterminated')"
    document = build_document("Use **this** answer", context)

    assert context in rendered_text(document)
    assert any(span["text"] == "this" and span.get("bold") for span in spans(document))
    assert not any(
        span["text"] == "print('unterminated')" and span.get("code") for span in spans(document)
    )
    assert document["show_format_toggle"] is True


def test_only_unclosed_fence_does_not_offer_a_no_op_toggle():
    question = "```\nunterminated"
    document = build_document(question)

    assert document["show_format_toggle"] is False
    assert rendered_text(document).endswith(question)


def test_container_fence_detection_distinguishes_open_and_closed_fences():
    for question in ("> ```python\n> unterminated", "- ```python\n  unterminated"):
        document = build_document(question)
        assert document["show_format_toggle"] is False
        assert rendered_text(document).endswith(question)
        assert not any(span.get("code") for span in spans(document))

    closed = build_document("> ```python\n> complete\n> ```")
    assert closed["show_format_toggle"] is True
    assert block_with_text(closed, "complete\n")["type"] == "code_block"


def test_nested_code_blocks_encode_background_container_indent():
    document = build_document(
        "> ```text\n> quoted\n> ```\n\n"
        "1. outer\n   - nested\n\n     ```text\n     listed\n     ```"
    )
    code_blocks = [block for block in document["rendered_blocks"] if block["type"] == "code_block"]

    assert [block["paragraph"] for block in code_blocks] == [
        {"indent": 32, "first_indent": 32, "container_indent": 18},
        {"indent": 46, "first_indent": 46, "container_indent": 32},
    ]


def test_list_first_code_and_thematic_break_get_separate_paragraphs():
    code_document = build_document("- ```text\n  code\n  ```")
    content = code_document["rendered_blocks"][1:]
    assert ["".join(span["text"] for span in block["spans"]) for block in content] == [
        "• ",
        "code\n",
    ]
    assert content[0]["paragraph"] == {"indent": 28, "first_indent": 10}
    assert content[1]["type"] == "code_block"
    assert content[1]["paragraph"] == {
        "indent": 28,
        "first_indent": 28,
        "container_indent": 14,
    }

    rule_document = build_document("- * * *")
    rule_blocks = rule_document["rendered_blocks"][1:]
    assert len(rule_blocks) == 2
    assert rule_blocks[0]["spans"] == [{"text": "• "}]
    assert rule_blocks[1]["paragraph"] == {"alignment": "center"}


def test_list_first_nested_list_keeps_outer_structure():
    document = build_document("-\n  - nested\n\n  after")
    content = document["rendered_blocks"][1:]

    assert ["".join(span["text"] for span in block["spans"]) for block in content] == [
        "• ",
        "• nested",
        "after",
    ]
    assert [block.get("paragraph", {}) for block in content] == [
        {"indent": 28, "first_indent": 10},
        {"indent": 46, "first_indent": 28},
        {"indent": 28, "first_indent": 28},
    ]


def test_list_first_heading_and_quote_share_one_styled_paragraph():
    heading = build_document("- # heading")
    quote = build_document("- > first quote paragraph\n  >\n  > second quote paragraph")

    assert "• heading" in rendered_text(heading)
    heading_block = block_with_text(heading, "• heading")
    assert heading_block["paragraph"] == {"indent": 28, "first_indent": 10}
    assert heading_block["spans"][1]["heading"] == 1

    first_quote = block_with_text(quote, "• │ first quote paragraph")
    second_quote = block_with_text(quote, "│ second quote paragraph")
    assert first_quote["paragraph"] == {"indent": 46, "first_indent": 10, "spacing": 6}
    assert second_quote["paragraph"] == {
        "indent": 46,
        "first_indent": 46,
        "spacing": 6,
    }


def test_inline_breaks_switch_to_continuation_indent_through_styles():
    list_document = build_document("- **first line\n  second line** after")
    first_list = block_with_text(list_document, "• first line")
    second_list = block_with_text(list_document, "second line after")
    assert first_list["paragraph"] == {"indent": 28, "first_indent": 10}
    assert second_list["paragraph"] == {"indent": 28, "first_indent": 28}
    assert [span.get("bold", False) for span in second_list["spans"]] == [True, False]

    quote_document = build_document("> first line\n> second line")
    first_quote = block_with_text(quote_document, "│ first line")
    second_quote = block_with_text(quote_document, "second line")
    assert first_quote["paragraph"] == {"indent": 20, "first_indent": 8, "spacing": 6}
    assert second_quote["paragraph"] == {"indent": 20, "first_indent": 20, "spacing": 6}


def test_empty_and_definition_only_list_items_keep_markers():
    document = build_document("-\n- next\n- [ref]: /url\n- last")
    assert "• \n• next\n• \n• last" in rendered_text(document)


def test_invisible_context_keeps_question_section_separated():
    document = build_document("Question", "[ref]: https://example.test")
    divider = "─" * MACOS_SECTION_DIVIDER_LENGTH

    assert (
        f"{divider}  📋 Context:  {divider}\n\n" f"{divider}  ❓ Question:  {divider}\nQuestion"
    ) in rendered_text(document)


def test_invisible_question_keeps_section_header_paragraph_terminated():
    document = build_document("[ref]: https://example.test")

    assert rendered_text(document).endswith("Question:  ────────────\n")
    assert document["rendered_blocks"][-1]["spans"] == []


def test_context_terminator_retains_quote_paragraph_style():
    """The block owns both content and its TextKit paragraph terminator."""
    document = build_document("Question", "> Quote immediately before Question")
    quote = block_with_text(document, "│ Quote immediately before Question")
    quote_index = document["rendered_blocks"].index(quote)

    assert quote["paragraph"] == {"indent": 20, "first_indent": 8, "spacing": 6}
    assert quote["paragraph_break"] is True
    assert quote["blank_line_after"] is True
    assert "paragraph" not in quote["spans"][0]
    assert "paragraph" not in quote["spans"][1]
    assert "Question:" in document["rendered_blocks"][quote_index + 1]["spans"][0]["text"]


def test_nested_and_ordered_lists_have_stable_hanging_indents():
    document = build_document("3. outer\n   - nested item that may wrap\n4. next")
    content = document["rendered_blocks"][1:]

    assert [block["spans"][0]["text"].strip() for block in content] == ["3.", "•", "4."]
    assert content[0]["paragraph"] == {"indent": 28, "first_indent": 10}
    assert content[1]["paragraph"] == {"indent": 46, "first_indent": 28}


def test_footer_is_separate_secondary_content_after_centered_divider():
    document = build_document(
        "Question",
        extra_note="📨 Also sent to Telegram.",
        include_timing_info=True,
    )
    divider = "─" * MACOS_METADATA_DIVIDER_LENGTH
    divider_block = block_with_text(document, divider)

    assert divider_block["spans"] == [{"text": divider, "secondary": True}]
    assert divider_block["paragraph"] == {"alignment": "center"}
    assert "📨 Also sent to Telegram." in rendered_text(document)
    assert "Issued at:" in rendered_text(document)
    assert rendered_text(document).endswith("(client may time out sooner)")


def test_unicode_text_is_preserved_exactly_in_spans():
    source = "**🤖 café 日本語 👨\u200d👩\u200d👧\u200d👦 e\u0301**"
    document = build_document(source)

    assert any(
        span["text"] == "🤖 café 日本語 👨\u200d👩\u200d👧\u200d👦 e\u0301" and span.get("bold")
        for span in spans(document)
    )


def test_plain_fallback_document_uses_the_same_block_contract():
    document = build_plain_macos_prompt_document("Complete plain prompt 🤖")

    assert document == {
        "version": MACOS_PROMPT_DOCUMENT_VERSION,
        "fallback_text": "Complete plain prompt 🤖",
        "show_format_toggle": False,
        "rendered_blocks": [
            {
                "type": "paragraph",
                "spans": [{"text": "Complete plain prompt 🤖"}],
                "paragraph_break": True,
                "blank_line_after": True,
            }
        ],
        "plain_blocks": [
            {
                "type": "paragraph",
                "spans": [{"text": "Complete plain prompt 🤖"}],
                "paragraph_break": True,
                "blank_line_after": True,
            }
        ],
    }
