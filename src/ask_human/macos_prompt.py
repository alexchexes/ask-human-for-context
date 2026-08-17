"""Safe Markdown presentation data for the native macOS dialog."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Literal, Optional
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from markdown_it import MarkdownIt
from markdown_it.common.normalize_url import validateLink as validate_default_link
from markdown_it.rules_block import fence as markdown_fence_rule
from markdown_it.rules_block.state_block import StateBlock
from markdown_it.rules_inline import image as markdown_image_rule
from markdown_it.rules_inline.state_inline import StateInline
from markdown_it.tree import SyntaxTreeNode

from .prompt_formatting import build_dialog_footer_text

MACOS_PROMPT_DOCUMENT_VERSION = 2
MACOS_SECTION_DIVIDER_LENGTH = 12
MACOS_METADATA_DIVIDER_LENGTH = 32


class _MacOSMarkdownIt(MarkdownIt):
    def validateLink(self, url: str) -> bool:
        """Allow only local file URLs beyond markdown-it's safe defaults."""
        parsed = urlsplit(url)
        if parsed.scheme.lower() == "file":
            return parsed.hostname in {None, "", "localhost"}
        return validate_default_link(url)


MACOS_MARKDOWN = _MacOSMarkdownIt("commonmark", {"html": False})


def _tracked_fence_rule(
    state: StateBlock,
    start_line: int,
    end_line: int,
    silent: bool,
) -> bool:
    """Record whether markdown-it found an explicit closing fence."""
    if not markdown_fence_rule(state, start_line, end_line, silent):
        return False
    if silent:
        return True

    token = state.tokens[-1]
    closing_line = token.map[1] - 1 if token.map else -1
    token.meta["closed"] = _line_closes_fence(state, closing_line, token.markup)
    return True


def _line_closes_fence(state: StateBlock, line: int, markup: str) -> bool:
    if line < 0 or not markup:
        return False

    position = state.bMarks[line] + state.tShift[line]
    maximum = state.eMarks[line]
    if position >= maximum or state.src[position] != markup[0]:
        return False
    if state.is_code_block(line):
        return False

    marker_start = position
    position = state.skipCharsStr(position, markup[0])
    if position - marker_start < len(markup):
        return False
    position = state.skipSpaces(position)
    return position >= maximum


MACOS_MARKDOWN.block.ruler.at(
    "fence",
    _tracked_fence_rule,
    {"alt": ["paragraph", "reference", "blockquote", "list"]},
)


def _literal_image_rule(state: StateInline, silent: bool) -> bool:
    """Consume valid Markdown images as their exact source text."""
    start = state.pos
    token_count = len(state.tokens)
    if not markdown_image_rule(state, silent):
        return False

    if not silent:
        del state.tokens[token_count:]
        token = state.push("text", "", 0)
        token.content = state.src[start : state.pos]
    return True


MACOS_MARKDOWN.inline.ruler.at("image", _literal_image_rule)


@dataclass(frozen=True)
class _BlockContext:
    list_depth: int = 0
    quote_depth: int = 0


@dataclass
class _RenderState:
    semantic_markdown: bool = False


@dataclass(frozen=True)
class _SpanStyle:
    bold: bool = False
    italic: bool = False
    code: bool = False
    background: bool = False
    secondary: bool = False
    heading: Optional[int] = None
    link: Optional[str] = None

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key in ("bold", "italic", "code", "background", "secondary"):
            if getattr(self, key):
                document[key] = True
        if self.heading is not None:
            document["heading"] = self.heading
        if self.link is not None:
            document["link"] = self.link
        return document


@dataclass
class _Span:
    text: str
    style: _SpanStyle = _SpanStyle()

    def as_document(self) -> dict[str, Any]:
        return {"text": self.text, **self.style.as_document()}


@dataclass(frozen=True)
class _ParagraphStyle:
    alignment: Optional[Literal["center"]] = None
    indent: int = 0
    first_indent: Optional[int] = None
    spacing: int = 0
    spacing_before: int = 0
    line_spacing: int = 0
    container_indent: int = 0

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key in (
            "alignment",
            "indent",
            "first_indent",
            "spacing",
            "spacing_before",
            "line_spacing",
            "container_indent",
        ):
            value = getattr(self, key)
            if value not in (None, 0):
                document[key] = value
        return document


@dataclass
class _Block:
    kind: Literal["paragraph", "code_block"]
    spans: list[_Span]
    paragraph: _ParagraphStyle = _ParagraphStyle()
    paragraph_break: bool = True
    blank_line_after: bool = True

    @property
    def text(self) -> str:
        return "".join(span.text for span in self.spans)

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "type": self.kind,
            "spans": [span.as_document() for span in self.spans],
            "paragraph_break": self.paragraph_break,
            "blank_line_after": self.blank_line_after,
        }
        paragraph = self.paragraph.as_document()
        if paragraph:
            document["paragraph"] = paragraph
        return document


class _InlineBuilder:
    """Collect inline spans while keeping Markdown line breaks explicit."""

    def __init__(self) -> None:
        self.lines: list[list[_Span]] = [[]]

    def append(self, text: str, style: _SpanStyle) -> None:
        if not text:
            return
        line = self.lines[-1]
        if line and line[-1].style == style:
            line[-1].text += text
        else:
            line.append(_Span(text, style))

    def break_line(self) -> None:
        self.lines.append([])


def build_macos_prompt_document(
    question: str,
    context: str,
    *,
    fallback_text: str,
    timeout_seconds: int,
    include_timing_info: bool,
    extra_note: str = "",
    issued_at: Optional[dt.datetime] = None,
    working_directory: Optional[Path] = None,
) -> dict[str, Any]:
    """Build the versioned paragraph document consumed by the JXA dialog."""
    rendered_blocks: list[_Block] = []
    plain_blocks: list[_Block] = []
    changed = False
    cwd = (working_directory or Path.cwd()).resolve()
    footer_text = build_dialog_footer_text(
        timeout_seconds=timeout_seconds,
        include_timing_info=include_timing_info,
        extra_note=extra_note,
        issued_at=issued_at,
    )

    cleaned_context = context.strip()
    if cleaned_context:
        _append_section_header(rendered_blocks, "📋", "Context")
        _append_section_header(plain_blocks, "📋", "Context")
        context_blocks, context_changed = _render_segment(cleaned_context, cwd)
        rendered_blocks.extend(context_blocks)
        plain_blocks.append(_plain_block(cleaned_context))
        _ensure_blank_line_after(rendered_blocks)
        _ensure_blank_line_after(plain_blocks)
        changed = changed or context_changed

    _append_section_header(rendered_blocks, "❓", "Question")
    _append_section_header(plain_blocks, "❓", "Question")
    cleaned_question = question.strip()
    question_blocks, question_changed = _render_segment(cleaned_question, cwd)
    rendered_blocks.extend(question_blocks)
    if cleaned_question or not footer_text:
        plain_blocks.append(_plain_block(cleaned_question))
    changed = changed or question_changed

    if footer_text:
        _append_footer(rendered_blocks, footer_text)
        _append_footer(plain_blocks, footer_text)
    elif not question_blocks:
        # Preserve the header's paragraph terminator when CommonMark consumes
        # the entire Question without emitting visible content.
        rendered_blocks.append(
            _Block(
                "paragraph",
                [],
                paragraph_break=False,
                blank_line_after=False,
            )
        )

    return {
        "version": MACOS_PROMPT_DOCUMENT_VERSION,
        "fallback_text": fallback_text,
        "show_format_toggle": changed,
        "rendered_blocks": [block.as_document() for block in rendered_blocks],
        "plain_blocks": [block.as_document() for block in plain_blocks],
    }


def build_plain_macos_prompt_document(prompt: str) -> dict[str, Any]:
    """Build a valid document for direct dialog calls and formatting failures."""
    block = _plain_block(prompt)
    return {
        "version": MACOS_PROMPT_DOCUMENT_VERSION,
        "fallback_text": prompt,
        "show_format_toggle": False,
        "rendered_blocks": [block.as_document()],
        "plain_blocks": [block.as_document()],
    }


def _plain_block(text: str, *, style: _SpanStyle = _SpanStyle()) -> _Block:
    return _Block("paragraph", [_Span(text, style)])


def _append_section_header(target: list[_Block], icon: str, label: str) -> None:
    divider = "─" * MACOS_SECTION_DIVIDER_LENGTH
    target.append(
        _Block(
            "paragraph",
            [
                _Span(
                    f"{divider}  {icon} {label}:  {divider}",
                    _SpanStyle(bold=True, secondary=True),
                )
            ],
            paragraph=_ParagraphStyle(alignment="center", spacing=10),
            blank_line_after=False,
        )
    )


def _append_footer(target: list[_Block], footer_text: str) -> None:
    _ensure_blank_line_after(target)
    target.append(
        _Block(
            "paragraph",
            [_Span("─" * MACOS_METADATA_DIVIDER_LENGTH, _SpanStyle(secondary=True))],
            paragraph=_ParagraphStyle(alignment="center"),
            blank_line_after=False,
        )
    )
    target.append(_plain_block(footer_text, style=_SpanStyle(secondary=True)))


def _ensure_blank_line_after(blocks: list[_Block]) -> None:
    if blocks:
        blocks[-1].blank_line_after = True


def _render_segment(source: str, cwd: Path) -> tuple[list[_Block], bool]:
    tokens = MACOS_MARKDOWN.parse(source)
    if any(token.type == "fence" and not token.meta.get("closed") for token in tokens):
        return [_plain_block(source)], False

    state = _RenderState()
    root = SyntaxTreeNode(tokens)
    blocks = _render_blocks(root.children or [], state, _BlockContext(), cwd)
    changed = state.semantic_markdown or _blocks_text(blocks).rstrip("\n") != source
    return blocks, changed


def _blocks_text(blocks: Iterable[_Block]) -> str:
    block_list = list(blocks)
    parts: list[str] = []
    for index, block in enumerate(block_list):
        parts.append(block.text)
        if index == len(block_list) - 1:
            continue
        if block.paragraph_break:
            parts.append("\n")
        if block.blank_line_after:
            parts.append("\n")
    return "".join(parts)


def _render_blocks(
    nodes: Iterable[SyntaxTreeNode],
    state: _RenderState,
    context: _BlockContext,
    cwd: Path,
) -> list[_Block]:
    blocks: list[_Block] = []
    for node in nodes:
        if node.type == "paragraph":
            blocks.extend(_render_paragraph(node, state, context, cwd))
        elif node.type == "heading":
            state.semantic_markdown = True
            blocks.extend(
                _render_inline_paragraph(
                    _inline_children(node),
                    state,
                    _paragraph_style(context),
                    cwd,
                    inline_style=_SpanStyle(bold=True, heading=_heading_level(node.tag)),
                )
            )
        elif node.type in {"bullet_list", "ordered_list"}:
            state.semantic_markdown = True
            blocks.extend(_render_list(node, state, context, cwd))
        elif node.type == "blockquote":
            state.semantic_markdown = True
            quote_context = _BlockContext(
                list_depth=context.list_depth,
                quote_depth=context.quote_depth + 1,
            )
            quote_blocks = _render_blocks(node.children or [], state, quote_context, cwd)
            _ensure_blank_line_after(quote_blocks)
            blocks.extend(quote_blocks)
        elif node.type in {"fence", "code_block"}:
            state.semantic_markdown = True
            blocks.append(
                _Block(
                    "code_block",
                    [_Span(node.content or "\n", _SpanStyle(code=True))],
                    paragraph=_paragraph_style(context, code_block=True),
                    paragraph_break=False,
                )
            )
        elif node.type == "hr":
            state.semantic_markdown = True
            blocks.append(
                _Block(
                    "paragraph",
                    [
                        _Span(
                            "─" * MACOS_METADATA_DIVIDER_LENGTH,
                            _SpanStyle(secondary=True),
                        )
                    ],
                    paragraph=_ParagraphStyle(alignment="center"),
                )
            )
        elif node.children:
            blocks.extend(_render_blocks(node.children, state, context, cwd))
        elif node.content:
            blocks.append(
                _Block(
                    "paragraph",
                    [_Span(node.content)],
                    paragraph=_paragraph_style(context),
                )
            )
    return blocks


def _render_list(
    node: SyntaxTreeNode,
    state: _RenderState,
    context: _BlockContext,
    cwd: Path,
) -> list[_Block]:
    blocks: list[_Block] = []
    ordered = node.type == "ordered_list"
    start = _integer_attribute(node.attrs.get("start"), 1)
    item_context = _BlockContext(
        list_depth=context.list_depth + 1,
        quote_depth=context.quote_depth,
    )

    for offset, item in enumerate(node.children or []):
        prefix = f"{start + offset}. " if ordered else "• "
        first_paragraph = True
        for child in item.children or []:
            if child.type == "paragraph":
                if not first_paragraph:
                    _ensure_blank_line_after(blocks)
                paragraph = _paragraph_style(
                    item_context,
                    include_list_prefix=first_paragraph,
                )
                prefix_spans = [_Span(prefix)] if first_paragraph else []
                blocks.extend(
                    _render_inline_paragraph(
                        _inline_children(child),
                        state,
                        paragraph,
                        cwd,
                        prefix_spans=prefix_spans,
                        blank_line_after=False,
                    )
                )
                first_paragraph = False
                continue

            child_blocks = _render_blocks([child], state, item_context, cwd)
            if first_paragraph:
                prefix_paragraph = _paragraph_style(
                    item_context,
                    include_list_prefix=True,
                )
                if not _starts_with_separate_list_block(child) and _prepend_list_marker(
                    child_blocks,
                    prefix,
                    prefix_paragraph,
                ):
                    blocks.extend(child_blocks)
                    first_paragraph = False
                    continue

                blocks.append(
                    _Block(
                        "paragraph",
                        [_Span(prefix)],
                        paragraph=prefix_paragraph,
                        blank_line_after=False,
                    )
                )
                first_paragraph = False
            blocks.extend(child_blocks)

        if first_paragraph:
            blocks.append(
                _Block(
                    "paragraph",
                    [_Span(prefix)],
                    paragraph=_paragraph_style(item_context, include_list_prefix=True),
                    blank_line_after=False,
                )
            )

    _ensure_blank_line_after(blocks)
    return blocks


def _prepend_list_marker(
    blocks: list[_Block],
    prefix: str,
    prefix_paragraph: _ParagraphStyle,
) -> bool:
    """Put a list marker into the first paragraph without changing later paragraphs."""
    if not blocks or blocks[0].kind != "paragraph":
        return False

    first = blocks[0]
    first.spans.insert(0, _Span(prefix))
    first.paragraph = replace(
        first.paragraph,
        first_indent=prefix_paragraph.first_indent,
    )
    return True


def _starts_with_separate_list_block(node: SyntaxTreeNode) -> bool:
    if node.type in {"fence", "code_block", "bullet_list", "ordered_list", "hr"}:
        return True
    if node.type == "blockquote" and node.children:
        return _starts_with_separate_list_block(node.children[0])
    return False


def _render_paragraph(
    node: SyntaxTreeNode,
    state: _RenderState,
    context: _BlockContext,
    cwd: Path,
) -> list[_Block]:
    paragraph = _paragraph_style(context, include_quote_prefix=True)
    if context.quote_depth:
        paragraph = replace(paragraph, spacing=6)
    prefix_spans = [_Span("│ ", _SpanStyle(secondary=True))] if context.quote_depth else []
    return _render_inline_paragraph(
        _inline_children(node),
        state,
        paragraph,
        cwd,
        inline_style=_SpanStyle(
            italic=context.quote_depth > 0,
            secondary=context.quote_depth > 0,
        ),
        prefix_spans=prefix_spans,
    )


def _render_inline_paragraph(
    nodes: Iterable[SyntaxTreeNode],
    state: _RenderState,
    paragraph: _ParagraphStyle,
    cwd: Path,
    *,
    inline_style: _SpanStyle = _SpanStyle(),
    prefix_spans: Iterable[_Span] = (),
    blank_line_after: bool = True,
) -> list[_Block]:
    inline = _InlineBuilder()
    _render_inline_nodes(nodes, inline, state, cwd, inline_style)

    blocks: list[_Block] = []
    current_paragraph = paragraph
    for index, line in enumerate(inline.lines):
        spans = list(prefix_spans) + line if index == 0 else line
        blocks.append(
            _Block(
                "paragraph",
                spans,
                paragraph=current_paragraph,
                blank_line_after=False,
            )
        )
        current_paragraph = _continuation_paragraph(current_paragraph)
    blocks[-1].blank_line_after = blank_line_after
    return blocks


def _render_inline_nodes(
    nodes: Iterable[SyntaxTreeNode],
    builder: _InlineBuilder,
    state: _RenderState,
    cwd: Path,
    style: _SpanStyle,
) -> None:
    for node in nodes:
        if node.type == "text":
            builder.append(node.content, style)
        elif node.type in {"softbreak", "hardbreak"}:
            if node.type == "hardbreak":
                state.semantic_markdown = True
            builder.break_line()
        elif node.type == "code_inline":
            state.semantic_markdown = True
            builder.append(node.content, replace(style, code=True, background=True))
        elif node.type in {"strong", "em"}:
            state.semantic_markdown = True
            nested_style = replace(
                style,
                bold=style.bold or node.type == "strong",
                italic=style.italic or node.type == "em",
            )
            _render_inline_nodes(node.children or [], builder, state, cwd, nested_style)
        elif node.type == "link":
            state.semantic_markdown = True
            href = _resolve_clickable_link(str(node.attrs.get("href", "")), cwd)
            nested_style = replace(style, link=href) if href else style
            _render_inline_nodes(node.children or [], builder, state, cwd, nested_style)
        elif node.children:
            _render_inline_nodes(node.children, builder, state, cwd, style)
        elif node.content:
            builder.append(node.content, style)


def _continuation_paragraph(paragraph: _ParagraphStyle) -> _ParagraphStyle:
    if paragraph.first_indent in (None, paragraph.indent):
        return paragraph
    return replace(paragraph, first_indent=paragraph.indent)


def _inline_children(node: SyntaxTreeNode) -> list[SyntaxTreeNode]:
    children = node.children or []
    if len(children) == 1 and children[0].type == "inline":
        return children[0].children or []
    return children


def _paragraph_style(
    context: _BlockContext,
    *,
    include_list_prefix: bool = False,
    include_quote_prefix: bool = False,
    code_block: bool = False,
) -> _ParagraphStyle:
    quote_offset = context.quote_depth * 18
    if context.list_depth:
        nested_offset = (context.list_depth - 1) * 18
        indent = quote_offset + nested_offset + 28
        first_indent = quote_offset + nested_offset + (10 if include_list_prefix else 28)
        return _ParagraphStyle(
            indent=indent,
            first_indent=first_indent,
            container_indent=max(0, indent - 14) if code_block else 0,
        )
    if include_quote_prefix and context.quote_depth:
        return _ParagraphStyle(
            indent=quote_offset + 2,
            first_indent=quote_offset - 10,
        )
    if code_block:
        return _ParagraphStyle(
            indent=quote_offset + 14,
            first_indent=quote_offset + 14,
            container_indent=quote_offset,
        )
    if quote_offset:
        return _ParagraphStyle(indent=quote_offset, first_indent=quote_offset)
    return _ParagraphStyle()


def _resolve_clickable_link(href: str, cwd: Path) -> Optional[str]:
    cleaned = href.strip()
    if not cleaned:
        return None

    parsed = urlsplit(cleaned)
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https", "mailto"}:
        return cleaned
    if scheme == "file":
        return cleaned if parsed.hostname in {None, "", "localhost"} else None
    if scheme or cleaned.startswith(("#", "?", "//")):
        return None

    path = Path(unquote(parsed.path))
    resolved = (
        path.resolve(strict=False) if path.is_absolute() else (cwd / path).resolve(strict=False)
    )
    file_url = resolved.as_uri()
    if parsed.query or parsed.fragment:
        file_parts = urlsplit(file_url)
        file_url = urlunsplit(
            (
                file_parts.scheme,
                file_parts.netloc,
                quote(unquote(file_parts.path)),
                parsed.query,
                parsed.fragment,
            )
        )
    return file_url


def _heading_level(tag: str) -> int:
    if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
        return min(3, max(1, int(tag[1])))
    return 3


def _integer_attribute(value: Any, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default
