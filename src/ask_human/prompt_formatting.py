"""Prompt formatting helpers for dialogs and Telegram delivery."""

import datetime as dt
import html
import locale
import re
import secrets
from dataclasses import dataclass
from typing import Optional

from markdown_it import MarkdownIt
from markdown_it.token import Token

DEFAULT_DIALOG_TITLE = "Agent asks..."
TELEGRAM_DOWNLOAD_LIMIT_LABEL = "20 MB"
TELEGRAM_PROMPT_SEPARATOR = "─" * 12
TELEGRAM_MESSAGE_CHAR_LIMIT = 4096
TELEGRAM_PROMPT_CHUNK_CHAR_LIMIT = 3500
TIMING_INFO_TIMEOUT_NOTE = "client may time out sooner"
TELEGRAM_HTML_BLANK_LINES_PATTERN = re.compile(r"\n{3,}")
TELEGRAM_HTML_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z0-9_+-]+$")
TELEGRAM_HTML_TAG_PATTERN = re.compile(r"<[^>\n]*>")
TELEGRAM_MARKDOWN = MarkdownIt("commonmark", {"html": False})


@dataclass
class _TelegramListState:
    ordered: bool
    next_number: int = 1


def resolve_dialog_title(dialog_title: Optional[str] = None) -> str:
    """Resolve the dialog title from CLI input or default."""
    if dialog_title and dialog_title.strip():
        return dialog_title.strip()

    return DEFAULT_DIALOG_TITLE


def generate_prompt_id(now: Optional[dt.datetime] = None) -> str:
    """Generate a short human-readable prompt identifier for Telegram workflows."""
    moment = (now or dt.datetime.now().astimezone()).astimezone()
    return f"Q{moment.strftime('%y%m%d-%H%M%S')}-{secrets.token_hex(2).upper()}"


def format_dialog_timestamp(moment: dt.datetime) -> str:
    """Format dialog timestamps using the current locale's short date/time format."""
    try:
        formatted = moment.astimezone().strftime("%x %X").strip()
        if formatted:
            return formatted
    except Exception:
        pass

    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def escape_telegram_html(text: str, *, quote: bool = False) -> str:
    """Escape arbitrary text for Telegram HTML parse mode."""
    return html.escape(text, quote=quote)


def initialize_time_locale() -> None:
    """Initialize process-wide time formatting from the OS locale once at startup."""
    try:
        locale.setlocale(locale.LC_TIME, "")
    except Exception:
        pass


def render_markdown_to_telegram_html(markdown_text: str) -> str:
    """Render common Markdown to Telegram-supported HTML."""
    tokens = TELEGRAM_MARKDOWN.parse(_trim_markdown_boundary_blank_lines(markdown_text))
    parts: list[str] = []
    list_stack: list[_TelegramListState] = []
    list_item_depth = 0
    blockquote_depth = 0
    previous_token_type: Optional[str] = None

    def trailing_newline_count() -> int:
        count = 0
        for part in reversed(parts):
            for character in reversed(part):
                if character != "\n":
                    return count
                count += 1
        return count

    def ensure_newline() -> None:
        ensure_newlines(1)

    def ensure_blank_line() -> None:
        ensure_newlines(2)

    def ensure_newlines(count: int) -> None:
        if not parts:
            return
        missing_count = count - trailing_newline_count()
        if missing_count > 0:
            parts.append("\n" * missing_count)

    def append_fenced_code(token: Token) -> None:
        ensure_newline()
        language = _extract_telegram_code_language(token.info)
        escaped_content = escape_telegram_html(token.content)
        if language:
            parts.append(f'<pre><code class="language-{language}">{escaped_content}</code></pre>')
        else:
            parts.append(f"<pre>{escaped_content}</pre>")
        ensure_blank_line()

    for token in tokens:
        token_type = token.type

        if token_type in {"heading_open", "paragraph_open"}:
            previous_token_type = token_type
            continue
        if token_type == "heading_close":
            parts.append("</b>")
            ensure_blank_line()
            previous_token_type = token_type
            continue
        if token_type == "paragraph_close":
            if list_item_depth == 0:
                if blockquote_depth > 0:
                    ensure_newline()
                else:
                    ensure_blank_line()
            previous_token_type = token_type
            continue
        if token_type == "inline":
            if previous_token_type == "heading_open":
                parts.append("<b>")
            parts.append(_render_inline_telegram_html(token.children or []))
            previous_token_type = token_type
            continue
        if token_type == "blockquote_open":
            ensure_newline()
            parts.append("<blockquote>")
            blockquote_depth += 1
            previous_token_type = token_type
            continue
        if token_type == "blockquote_close":
            blockquote_depth = max(0, blockquote_depth - 1)
            parts.append("</blockquote>")
            ensure_blank_line()
            previous_token_type = token_type
            continue
        if token_type == "bullet_list_open":
            ensure_newline()
            list_stack.append(_TelegramListState(ordered=False))
            previous_token_type = token_type
            continue
        if token_type == "ordered_list_open":
            ensure_newline()
            list_stack.append(
                _TelegramListState(ordered=True, next_number=_ordered_list_start(token))
            )
            previous_token_type = token_type
            continue
        if token_type in {"bullet_list_close", "ordered_list_close"}:
            if list_stack:
                list_stack.pop()
            if list_stack or list_item_depth > 0:
                ensure_newline()
            else:
                ensure_blank_line()
            previous_token_type = token_type
            continue
        if token_type == "list_item_open":
            list_item_depth += 1
            ensure_newline()
            parts.append(_list_item_prefix(list_stack, token, list_item_depth))
            previous_token_type = token_type
            continue
        if token_type == "list_item_close":
            list_item_depth = max(0, list_item_depth - 1)
            ensure_newline()
            previous_token_type = token_type
            continue
        if token_type in {"fence", "code_block"}:
            append_fenced_code(token)
            previous_token_type = token_type
            continue
        if token_type == "hr":
            ensure_newline()
            parts.append(TELEGRAM_PROMPT_SEPARATOR)
            ensure_blank_line()
            previous_token_type = token_type
            continue
        if token.content:
            parts.append(escape_telegram_html(token.content))
        previous_token_type = token_type

    return _normalize_telegram_html_text("".join(parts))


def telegram_html_to_plain_text(prompt_text: str) -> str:
    """Convert Telegram HTML prompt text to a readable plain-text fallback."""
    plain_text = prompt_text.replace("</blockquote>", "\n")
    plain_text = TELEGRAM_HTML_TAG_PATTERN.sub("", plain_text)
    plain_text = html.unescape(plain_text)
    return _normalize_telegram_html_text(plain_text)


def _render_inline_telegram_html(tokens: list[Token]) -> str:
    parts: list[str] = []
    link_stack: list[bool] = []

    for token in tokens:
        token_type = token.type
        if token_type == "text":
            parts.append(escape_telegram_html(token.content))
        elif token_type in {"softbreak", "hardbreak"}:
            parts.append("\n")
        elif token_type == "strong_open":
            parts.append("<b>")
        elif token_type == "strong_close":
            parts.append("</b>")
        elif token_type == "em_open":
            parts.append("<i>")
        elif token_type == "em_close":
            parts.append("</i>")
        elif token_type == "code_inline":
            parts.append(f"<code>{escape_telegram_html(token.content)}</code>")
        elif token_type == "link_open":
            href = _token_attribute(token, "href")
            if href:
                parts.append(f'<a href="{escape_telegram_html(href, quote=True)}">')
                link_stack.append(True)
            else:
                link_stack.append(False)
        elif token_type == "link_close":
            if link_stack and link_stack.pop():
                parts.append("</a>")
        elif token.children:
            parts.append(_render_inline_telegram_html(token.children))
        elif token.content:
            parts.append(escape_telegram_html(token.content))

    return "".join(parts)


def _token_attribute(token: Token, name: str) -> Optional[str]:
    value = token.attrs.get(name) if token.attrs else None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _extract_telegram_code_language(info: str) -> Optional[str]:
    language = info.strip().split(maxsplit=1)[0] if info.strip() else ""
    if TELEGRAM_HTML_LANGUAGE_PATTERN.fullmatch(language):
        return language
    return None


def _ordered_list_start(token: Token) -> int:
    start_value = token.attrs.get("start") if token.attrs else None
    if isinstance(start_value, int):
        return start_value
    if isinstance(start_value, str) and start_value.isdigit():
        return int(start_value)
    return 1


def _list_item_prefix(
    list_stack: list[_TelegramListState], token: Token, list_item_depth: int
) -> str:
    indentation = "  " * max(0, list_item_depth - 1)
    if not list_stack:
        return f"{indentation}- "

    current_list = list_stack[-1]
    if not current_list.ordered:
        return f"{indentation}- "

    if token.info.strip().isdigit():
        number = int(token.info.strip())
        current_list.next_number = number + 1
    else:
        number = current_list.next_number
        current_list.next_number += 1
    return f"{indentation}{number}. "


def _normalize_telegram_html_text(text: str) -> str:
    without_trailing_spaces = re.sub(r"[ \t]+\n", "\n", text)
    compacted = TELEGRAM_HTML_BLANK_LINES_PATTERN.sub("\n\n", without_trailing_spaces)
    return compacted.strip()


def build_timing_info_lines(issued_at: dt.datetime, timeout_seconds: int) -> list[str]:
    """Build optional timing metadata lines for dialogs or Telegram prompts."""
    answer_until = issued_at + dt.timedelta(seconds=timeout_seconds)
    return [
        f"Issued at: {format_dialog_timestamp(issued_at)}",
        f"Answer until: {format_dialog_timestamp(answer_until)}",
        f"({TIMING_INFO_TIMEOUT_NOTE})",
    ]


def build_timing_info_block(issued_at: dt.datetime, timeout_seconds: int) -> str:
    """Build the optional timing metadata shown in dialogs."""
    timing_lines = build_timing_info_lines(issued_at, timeout_seconds)
    return f"{timing_lines[0]}\n{timing_lines[1]} {timing_lines[2]}"


def build_dialog_telegram_notice(platform_name: str) -> str:
    """Explain that the prompt was also delivered through Telegram."""
    if platform_name == "Windows":
        return (
            "📨 Also sent to Telegram. ⚠️ If you reply there first, this dialog will stay "
            "open. Any later answer here will be ignored."
        )

    return "📨 Also sent to Telegram."


def build_dialog_footer_text(
    *,
    timeout_seconds: int,
    include_timing_info: bool,
    extra_note: str = "",
    issued_at: Optional[dt.datetime] = None,
) -> str:
    """Build the optional plain-text footer shared by native dialog layouts."""
    footer_parts = []
    if extra_note.strip():
        footer_parts.append(extra_note.strip())

    if include_timing_info:
        effective_issued_at = issued_at or dt.datetime.now().astimezone()
        footer_parts.append(build_timing_info_block(effective_issued_at, timeout_seconds))

    return "\n".join(footer_parts)


def build_prompt_text(
    question: str,
    context: str,
    *,
    timeout_seconds: int,
    include_timing_info: bool,
    extra_note: str = "",
    issued_at: Optional[dt.datetime] = None,
) -> str:
    """Build the formatted prompt text for native dialogs."""
    separator = "─" * 39
    section_rule = "─" * 16
    question_block = f"{section_rule} ❓ Question: {section_rule}\n{question.strip()}"

    if context.strip():
        context_block = f"{section_rule}  📋 Context:  {section_rule}\n{context.strip()}"
        full_question = f"{context_block}\n\n{question_block}"
    else:
        full_question = question_block

    footer_text = build_dialog_footer_text(
        timeout_seconds=timeout_seconds,
        include_timing_info=include_timing_info,
        extra_note=extra_note,
        issued_at=issued_at,
    )
    if footer_text:
        return f"{full_question}\n\n{separator}\n{footer_text}"

    return full_question


def build_telegram_prompt_text(
    question: str,
    context: str,
    *,
    prompt_id: str,
    timeout_seconds: int,
    include_timing_info: bool,
    issued_at: Optional[dt.datetime] = None,
    broker_label: Optional[str] = None,
    broker_id: Optional[str] = None,
) -> str:
    """Build a Telegram-specific prompt using HTML parse mode and compact metadata."""
    parts: list[str] = []
    context_markdown = _trim_markdown_boundary_blank_lines(context)
    question_markdown = _trim_markdown_boundary_blank_lines(question)

    if context_markdown.strip():
        parts.extend(
            [
                "<b>📋 Context:</b>",
                render_markdown_to_telegram_html(context_markdown),
                "",
                TELEGRAM_PROMPT_SEPARATOR,
                "",
            ]
        )

    parts.extend(
        [
            "<b>❓ Question:</b>",
            render_markdown_to_telegram_html(question_markdown),
            "",
            TELEGRAM_PROMPT_SEPARATOR,
            "",
        ]
    )

    parts.extend(
        [
            _build_telegram_metadata_block(
                prompt_id=prompt_id,
                timeout_seconds=timeout_seconds,
                include_timing_info=include_timing_info,
                issued_at=issued_at,
                broker_label=broker_label,
                broker_id=broker_id,
            ),
            "",
            '↩️ Use "Reply" on this message to answer.',
        ]
    )

    return "\n".join(parts)


def build_telegram_prompt_texts(
    question: str,
    context: str,
    *,
    prompt_id: str,
    timeout_seconds: int,
    include_timing_info: bool,
    issued_at: Optional[dt.datetime] = None,
    broker_label: Optional[str] = None,
    broker_id: Optional[str] = None,
) -> list[str]:
    """Build one or more Telegram prompt messages, splitting only when needed."""
    context_markdown = _trim_markdown_boundary_blank_lines(context)
    question_markdown = _trim_markdown_boundary_blank_lines(question)
    full_prompt = build_telegram_prompt_text(
        question_markdown,
        context_markdown,
        prompt_id=prompt_id,
        timeout_seconds=timeout_seconds,
        include_timing_info=include_timing_info,
        issued_at=issued_at,
        broker_label=broker_label,
        broker_id=broker_id,
    )
    if _telegram_message_fits(full_prompt):
        return [full_prompt]

    messages: list[str] = []
    if context_markdown.strip():
        messages.extend(_build_telegram_section_messages("📋 Context", context_markdown))

    messages.extend(_build_telegram_section_messages("❓ Question", question_markdown))
    messages.append(
        "\n".join(
            [
                _build_telegram_metadata_block(
                    prompt_id=prompt_id,
                    timeout_seconds=timeout_seconds,
                    include_timing_info=include_timing_info,
                    issued_at=issued_at,
                    broker_label=broker_label,
                    broker_id=broker_id,
                ),
                "",
                '↩️ Use "Reply" on this message to answer.',
            ]
        )
    )
    return messages


def _build_telegram_metadata_block(
    *,
    prompt_id: str,
    timeout_seconds: int,
    include_timing_info: bool,
    issued_at: Optional[dt.datetime] = None,
    broker_label: Optional[str] = None,
    broker_id: Optional[str] = None,
) -> str:
    """Build compact Telegram metadata as an expandable block."""
    effective_issued_at = issued_at or dt.datetime.now().astimezone()
    metadata_lines: list[str] = []
    if include_timing_info:
        metadata_lines.extend(build_timing_info_lines(effective_issued_at, timeout_seconds))

    metadata_lines.extend(
        [
            f"Answers support text or files up to {TELEGRAM_DOWNLOAD_LIMIT_LABEL}.",
            f"Prompt ID: {prompt_id}",
        ]
    )
    if broker_label and broker_id:
        metadata_lines.append(f"Broker: {broker_label} [{broker_id}]")

    metadata_block = "\n".join(escape_telegram_html(line) for line in metadata_lines)
    return f"<blockquote expandable>{metadata_block}</blockquote>"


def _telegram_message_fits(prompt_text: str) -> bool:
    """Check Telegram's post-entity message length limit."""
    return len(telegram_html_to_plain_text(prompt_text)) <= TELEGRAM_MESSAGE_CHAR_LIMIT


def _build_telegram_section_messages(label: str, markdown_text: str) -> list[str]:
    """Build naturally split Telegram section messages for one prompt section."""
    chunks = _split_text_naturally(markdown_text, TELEGRAM_PROMPT_CHUNK_CHAR_LIMIT)
    if len(chunks) == 1:
        headings = [label]
    else:
        headings = [f"{label} ({index}/{len(chunks)})" for index in range(1, len(chunks) + 1)]

    return [
        f"<b>{heading}:</b>\n{render_markdown_to_telegram_html(chunk)}"
        for heading, chunk in zip(headings, chunks)
    ]


def _split_text_naturally(text: str, max_chars: int) -> list[str]:
    """Split text on paragraph, line, or word boundaries, with hard fallback splits."""
    remaining = _trim_markdown_boundary_blank_lines(text)
    if not remaining:
        return []

    chunks: list[str] = []
    while len(remaining) > max_chars:
        split_at = _find_natural_split(remaining, max_chars)
        chunk = _trim_markdown_boundary_blank_lines(remaining[:split_at])
        if not chunk:
            chunk = remaining[:max_chars]
            split_at = max_chars
        chunks.append(chunk)
        remaining = _trim_markdown_boundary_blank_lines(remaining[split_at:])

    if remaining:
        chunks.append(remaining)
    return chunks


def _trim_markdown_boundary_blank_lines(text: str) -> str:
    """Trim surrounding blank lines without changing content-line indentation."""
    without_leading_blank_lines = re.sub(r"\A(?:[ \t]*\r?\n)+", "", text)
    return re.sub(r"(?:\r?\n[ \t]*)+\Z", "", without_leading_blank_lines)


def _find_natural_split(text: str, max_chars: int) -> int:
    """Find a useful split point near max_chars."""
    search_window = text[: max_chars + 1]
    candidates = [
        search_window.rfind("\n\n"),
        search_window.rfind("\n"),
        search_window.rfind(" "),
    ]
    best = max(candidates)
    if best >= max_chars // 2:
        return best
    return max_chars
