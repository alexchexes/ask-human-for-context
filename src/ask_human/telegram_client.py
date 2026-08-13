"""Telegram polling client for prompt delivery, reply parsing, and downloads."""

import asyncio
import json
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, cast

from .broker_state import TelegramBrokerIdentity
from .debug_logging import TelegramDebugLogger, duration_ms
from .prompt_formatting import TELEGRAM_DOWNLOAD_LIMIT_LABEL, telegram_html_to_plain_text
from .telegram_entities import telegram_entities_to_markdown
from .telegram_models import (
    DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS,
    TELEGRAM_DOWNLOAD_LIMIT_BYTES,
    TelegramConfig,
    TelegramPendingPrompt,
    TelegramPromptError,
    TelegramReplyRejection,
    TelegramReplyResolution,
    TelegramTextReplyPart,
)


class TelegramBotApiError(TelegramPromptError):
    """Telegram Bot API failure with enough metadata to classify polling retries."""

    def __init__(
        self,
        message: str,
        *,
        method: str,
        http_status: Optional[int] = None,
        transport_error: bool = False,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.http_status = http_status
        self.transport_error = transport_error


@dataclass
class _PendingAttachmentGroup:
    """Collect file/media reply messages that belong to one agent answer."""

    prompt_message_id: int
    pending_prompt: TelegramPendingPrompt
    group_key: tuple[int, str]
    messages: list[dict[str, Any]] = field(default_factory=list)
    selected_quote_text: Optional[str] = None
    reply_to_message_id: Optional[int] = None
    finalize_task: Optional[asyncio.Task[None]] = None
    series_mode: bool = False


class TelegramPromptClient:
    """Minimal long-polling Telegram client for prompt/response workflows."""

    ISSUE_URL = "https://github.com/alexchexes/ask-human/issues"
    PROMPT_PARSE_MODE = "HTML"
    SHUTDOWN_TIMEOUT_SECONDS = DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS + 15
    IDLE_DRAIN_HTTP_TIMEOUT_SECONDS = 2
    POLL_RETRY_DELAYS_SECONDS = (1.0, 2.0, 5.0, 10.0)
    POLL_RETRY_MAX_ELAPSED_SECONDS = 120.0
    ATTACHMENT_REPLY_DEBOUNCE_SECONDS = 1.0
    TEXT_REPLY_SPLIT_DEBOUNCE_SECONDS = 0.7
    TEXT_REPLY_SPLIT_MIN_LENGTH = 3500
    ATTACHMENT_REPLY_KEYS = (
        "document",
        "video",
        "audio",
        "voice",
        "animation",
        "video_note",
        "sticker",
        "photo",
    )
    NON_REPLY_HINT_TEXT = "⚠️ Message is ignored. Use <b>Reply</b> ↪ on the bot's message."
    SERIES_ATTACHMENT_HINT_TEXT = (
        "\nℹ️ If you tried to send multiple attachments, reply with /files_start, "
        "send the attachments, then send /files_finish."
    )
    UNMATCHED_REPLY_HINT_TEXT = (
        "⚠️ Reply is ignored. It was not a Reply to the currently active question. "
        "Use <b>Reply</b> ↪ on the <b>latest active message from agent</b>."
    )
    STALE_REPLY_HINT_TEMPLATE = (
        "⚠️ Message is ignored. {prompt_target} is no longer active. Ask the agent to send "
        "a new question."
    )

    def __init__(
        self,
        config: TelegramConfig,
        download_dir: Optional[Path] = None,
        *,
        broker_identity: Optional[TelegramBrokerIdentity] = None,
        debug_log_path: Optional[str] = None,
    ) -> None:
        self.bot_token = config.bot_token
        self.chat_id = config.chat_id
        self.download_dir = download_dir
        self.broker_identity = broker_identity
        self.debug_logger = TelegramDebugLogger.from_config(debug_log_path)
        self._lock = asyncio.Lock()
        self._next_update_offset: Optional[int] = None
        self._pending_by_message_id: dict[int, TelegramPendingPrompt] = {}
        self._pending_attachment_groups: dict[tuple[int, str], _PendingAttachmentGroup] = {}
        self._active_series_group: Optional[_PendingAttachmentGroup] = None
        self._poller_task: Optional[asyncio.Task[None]] = None
        self._poll_sequence = 0
        self._background_status_tasks: set[asyncio.Task[None]] = set()
        self._latest_prompt_message_id: Optional[int] = None

    async def ask_question(
        self,
        prompt_text: str | list[str],
        timeout: int,
        prompt_id: str,
        download_dir: Optional[Path] = None,
    ) -> Optional[str]:
        """Send prompt message(s) and wait for one reply to any of them."""
        prompt_texts = self._normalize_prompt_texts(prompt_text)
        resolved_download_dir = download_dir or self.download_dir
        if resolved_download_dir is None:
            raise TelegramPromptError("No Telegram download directory was configured.")

        self._debug_event(
            "telegram_prompt_start",
            prompt_id=prompt_id,
            message_count=len(prompt_texts),
            timeout_seconds=timeout,
        )
        message_ids = await self._send_prompt_messages(prompt_texts, prompt_id=prompt_id)
        response_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        pending_prompt = TelegramPendingPrompt(
            future=response_future,
            prompt_id=prompt_id,
            download_dir=resolved_download_dir,
        )

        async with self._lock:
            for message_id in message_ids:
                self._pending_by_message_id[message_id] = pending_prompt
            self._latest_prompt_message_id = message_ids[-1]
            self._ensure_poller_locked()

        try:
            response = await asyncio.wait_for(response_future, timeout)
            self._debug_event("telegram_prompt_response_returned", prompt_id=prompt_id)
            await asyncio.sleep(0)
            return response
        except asyncio.TimeoutError:
            self._debug_event("telegram_prompt_timeout", prompt_id=prompt_id)
            return None
        finally:
            finalize_task: Optional[asyncio.Task[None]] = None
            async with self._lock:
                finalize_tasks = self._remove_pending_prompt_locked(pending_prompt)
            for finalize_task in finalize_tasks:
                finalize_task.cancel()

    async def shutdown(self, *, timeout: float = SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """Fail pending prompts and wait until Telegram polling has stopped."""
        poller_task: Optional[asyncio.Task[None]]
        finalize_tasks: list[asyncio.Task[None]] = []
        async with self._lock:
            pending_prompts = self._unique_pending_prompts_locked()
            self._pending_by_message_id.clear()
            for attachment_group in self._pending_attachment_groups.values():
                if attachment_group.finalize_task is not None:
                    finalize_tasks.append(attachment_group.finalize_task)
                    attachment_group.finalize_task = None
            self._pending_attachment_groups.clear()
            self._active_series_group = None
            self._latest_prompt_message_id = None
            poller_task = self._poller_task

            for pending_prompt in pending_prompts:
                if pending_prompt.text_reply_finalize_task is not None:
                    finalize_tasks.append(pending_prompt.text_reply_finalize_task)
                    pending_prompt.text_reply_finalize_task = None
                if not pending_prompt.future.done():
                    pending_prompt.future.set_exception(
                        TelegramPromptError("Telegram broker shutdown requested.")
                    )

        for finalize_task in finalize_tasks:
            finalize_task.cancel()

        if poller_task is None or poller_task.done():
            return

        try:
            await asyncio.wait_for(asyncio.shield(poller_task), timeout)
        except asyncio.TimeoutError as exc:
            raise TelegramPromptError(
                "Telegram polling did not stop before the broker shutdown timeout."
            ) from exc

    @staticmethod
    def _normalize_prompt_texts(prompt_text: str | list[str]) -> list[str]:
        """Normalize one prompt string or a multipart prompt list."""
        if isinstance(prompt_text, str):
            prompt_texts = [prompt_text]
        else:
            prompt_texts = prompt_text

        cleaned_prompt_texts = [text for text in prompt_texts if text.strip()]
        if not cleaned_prompt_texts:
            raise TelegramPromptError("Prompt text must contain at least one non-empty message.")
        return cleaned_prompt_texts

    def _debug_event(self, event: str, **fields: Any) -> None:
        if self.debug_logger is not None:
            self.debug_logger.event(event, **fields)

    async def _send_prompt_messages(
        self,
        prompt_texts: list[str],
        *,
        prompt_id: Optional[str] = None,
    ) -> list[int]:
        """Send all outbound prompt messages and return their message ids."""
        message_ids = []
        for index, prompt_text in enumerate(prompt_texts, start=1):
            message_ids.append(
                await self._send_prompt(
                    prompt_text,
                    prompt_id=prompt_id,
                    prompt_part_index=index,
                    prompt_part_count=len(prompt_texts),
                )
            )
        return message_ids

    async def _send_prompt(
        self,
        prompt_text: str,
        *,
        prompt_id: Optional[str] = None,
        prompt_part_index: Optional[int] = None,
        prompt_part_count: Optional[int] = None,
    ) -> int:
        """Send the outbound Telegram message and return its message id."""
        base_payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": prompt_text,
            "disable_web_page_preview": True,
        }
        started_at = time.monotonic()
        self._debug_event(
            "telegram_prompt_send_start",
            prompt_id=prompt_id,
            prompt_part_index=prompt_part_index,
            prompt_part_count=prompt_part_count,
        )
        try:
            result = await self._bot_api_request(
                "sendMessage",
                {
                    **base_payload,
                    "parse_mode": self.PROMPT_PARSE_MODE,
                },
                timeout=20,
            )
        except TelegramPromptError as exc:
            if not self._is_markup_parse_error(exc):
                self._debug_event(
                    "telegram_prompt_send_error",
                    prompt_id=prompt_id,
                    prompt_part_index=prompt_part_index,
                    duration_ms=duration_ms(started_at),
                    error=exc,
                )
                raise
            self._debug_event(
                "telegram_prompt_send_plain_text_fallback",
                prompt_id=prompt_id,
                prompt_part_index=prompt_part_index,
                duration_ms=duration_ms(started_at),
                error=exc,
            )
            result = await self._bot_api_request(
                "sendMessage",
                {
                    **base_payload,
                    "text": telegram_html_to_plain_text(prompt_text),
                },
                timeout=20,
            )

        message_id = result.get("message_id")
        if not isinstance(message_id, int):
            raise TelegramPromptError("Telegram sendMessage did not return a message_id.")

        self._debug_event(
            "telegram_prompt_send_ok",
            prompt_id=prompt_id,
            prompt_part_index=prompt_part_index,
            message_id=message_id,
            message_date=result.get("date"),
            duration_ms=duration_ms(started_at),
        )
        return message_id

    @staticmethod
    def _is_markup_parse_error(error: TelegramPromptError) -> bool:
        """Identify Telegram failures caused by unsupported prompt markup."""
        message = str(error).lower()
        return "parse entities" in message or ("entity" in message and "parse" in message)

    def _ensure_poller_locked(self) -> None:
        """Start the update poller while the lock is held."""
        if self._poller_task is None or self._poller_task.done():
            self._poller_task = asyncio.create_task(self._poll_updates())
            self._poller_task.add_done_callback(self._consume_task_result)

    async def _poll_updates(self) -> None:
        """Long-poll Telegram updates and resolve pending prompt futures."""
        retry_started_at: Optional[float] = None
        retry_attempt = 0
        try:
            while True:
                async with self._lock:
                    has_pending_prompts = bool(self._pending_by_message_id)
                    offset = self._next_update_offset

                    if not has_pending_prompts and offset is None:
                        self._poller_task = None
                        return

                    self._poll_sequence += 1
                    poll_sequence = self._poll_sequence

                poll_timeout = (
                    0 if not has_pending_prompts else DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS
                )
                http_timeout = (
                    self.IDLE_DRAIN_HTTP_TIMEOUT_SECONDS
                    if not has_pending_prompts
                    else poll_timeout + 10
                )
                self._debug_event(
                    "telegram_poll_start",
                    poll_sequence=poll_sequence,
                    has_pending_prompts=has_pending_prompts,
                    offset=offset,
                    poll_timeout_seconds=poll_timeout,
                    http_timeout_seconds=http_timeout,
                )
                try:
                    poll_started_at = time.monotonic()
                    updates = await self._bot_api_request(
                        "getUpdates",
                        {
                            "offset": offset,
                            "timeout": poll_timeout,
                            "allowed_updates": ["message"],
                        },
                        timeout=http_timeout,
                    )
                except TelegramPromptError as exc:
                    if not has_pending_prompts:
                        self._debug_event("telegram_idle_drain_error", error=exc)
                        if await self._should_keep_polling_after_idle_drain():
                            continue
                        return
                    if has_pending_prompts and self._is_retryable_poll_error(exc):
                        now = asyncio.get_running_loop().time()
                        if retry_started_at is None:
                            retry_started_at = now
                        if now - retry_started_at <= self.POLL_RETRY_MAX_ELAPSED_SECONDS:
                            retry_delay = self._poll_retry_delay(retry_attempt)
                            self._debug_event(
                                "telegram_poll_retry",
                                retry_attempt=retry_attempt + 1,
                                retry_delay_ms=round(retry_delay * 1000),
                                retry_elapsed_ms=round((now - retry_started_at) * 1000),
                                error=exc,
                            )
                            await asyncio.sleep(retry_delay)
                            retry_attempt += 1
                            continue
                    self._debug_event("telegram_poll_error", error=exc)
                    raise
                retry_started_at = None
                retry_attempt = 0

                if not isinstance(updates, list):
                    error = TelegramPromptError(
                        "Telegram getUpdates returned an unexpected payload."
                    )
                    if not has_pending_prompts:
                        self._debug_event("telegram_idle_drain_error", error=error)
                        if await self._should_keep_polling_after_idle_drain():
                            continue
                        return
                    raise error

                self._debug_event(
                    "telegram_poll_ok",
                    poll_sequence=poll_sequence,
                    has_pending_prompts=has_pending_prompts,
                    offset=offset,
                    update_count=len(updates),
                    duration_ms=duration_ms(poll_started_at),
                )

                async with self._lock:
                    for update in updates:
                        update_id = update.get("update_id")
                        if isinstance(update_id, int):
                            self._next_update_offset = update_id + 1

                for update in updates:
                    await self._handle_update(update)

                async with self._lock:
                    for pending_prompt in self._unique_pending_prompts_locked():
                        wait_after = pending_prompt.text_reply_wait_after_poll_sequence
                        post_part_poll_processed = (
                            pending_prompt.text_reply_post_part_poll_processed
                        )
                        if (
                            wait_after is not None
                            and poll_sequence > wait_after
                            and not post_part_poll_processed.is_set()
                        ):
                            self._debug_event(
                                "telegram_text_reply_post_part_poll_processed",
                                prompt_id=pending_prompt.prompt_id,
                                poll_sequence=poll_sequence,
                                wait_after_poll_sequence=wait_after,
                            )
                            post_part_poll_processed.set()

                # Telegram only considers processed updates confirmed once we perform another
                # getUpdates call with an offset higher than their update_id. When the last
                # pending prompt completes, keep draining with timeout=0 until Telegram returns
                # an empty page, then stop the poller.
                if not has_pending_prompts and not updates:
                    if await self._should_keep_polling_after_idle_drain():
                        continue
                    return

                # Real getUpdates long-polls, but tests and some failure modes can return
                # empty pages immediately. Back off so debounce/finalizer tasks are not
                # starved by a hot empty-poll loop.
                await asyncio.sleep(0.05 if has_pending_prompts and not updates else 0)
        except Exception as exc:
            async with self._lock:
                pending_items = self._unique_pending_prompts_locked()
                self._pending_by_message_id.clear()
                attachment_groups = list(self._pending_attachment_groups.values())
                self._pending_attachment_groups.clear()
                self._active_series_group = None
                self._poller_task = None

            for attachment_group in attachment_groups:
                if attachment_group.finalize_task is not None:
                    attachment_group.finalize_task.cancel()

            for pending_prompt in pending_items:
                if pending_prompt.text_reply_finalize_task is not None:
                    pending_prompt.text_reply_finalize_task.cancel()
                if not pending_prompt.future.done():
                    pending_prompt.future.set_exception(
                        TelegramPromptError(f"Telegram polling failed: {exc}")
                    )

    async def _should_keep_polling_after_idle_drain(self) -> bool:
        """Return true if a fresh prompt appeared during an idle drain request."""
        async with self._lock:
            if self._pending_by_message_id:
                return True
            self._poller_task = None
            return False

    @classmethod
    def _poll_retry_delay(cls, retry_attempt: int) -> float:
        """Return the progressive backoff delay for a retryable poll failure."""
        index = min(retry_attempt, len(cls.POLL_RETRY_DELAYS_SECONDS) - 1)
        return cls.POLL_RETRY_DELAYS_SECONDS[index]

    @staticmethod
    def _is_retryable_poll_error(error: TelegramPromptError) -> bool:
        """Retry only idempotent Telegram polling failures that are usually transient."""
        if not isinstance(error, TelegramBotApiError):
            return False
        if error.method != "getUpdates":
            return False
        if error.transport_error:
            return True
        return error.http_status is not None and 500 <= error.http_status < 600

    async def _handle_update(self, update: dict[str, Any]) -> None:
        """Process one Telegram update and resolve or reject matching replies."""
        update_id = update.get("update_id")
        message = update.get("message")
        message_id = None
        message_date = None
        reply_to_message_id = None
        reply_to_message_date = None
        if isinstance(message, dict):
            message_id = message.get("message_id")
            message_date = message.get("date")
            reply_to_message = message.get("reply_to_message")
            if isinstance(reply_to_message, dict):
                reply_to_message_id = reply_to_message.get("message_id")
                reply_to_message_date = reply_to_message.get("date")
        self._debug_event(
            "telegram_update_start",
            update_id=update_id,
            message_id=message_id,
            message_date=message_date,
            reply_to_message_id=reply_to_message_id,
            reply_to_message_date=reply_to_message_date,
        )
        matched = await self._match_pending_prompt(update)
        if matched is None:
            matched = await self._match_active_series(update)
        if matched is None:
            self._debug_event(
                "telegram_update_unmatched",
                update_id=update_id,
                message_id=message_id,
                message_date=message_date,
            )
            await self._maybe_hint_on_missing_reply(update)
            return

        prompt_message_id, pending_prompt, message = matched
        matched_message_date = message.get("date")
        matched_reply_to_message = message.get("reply_to_message")
        matched_reply_to_message_date = (
            matched_reply_to_message.get("date")
            if isinstance(matched_reply_to_message, dict)
            else None
        )
        self._debug_event(
            "telegram_update_matched",
            update_id=update_id,
            message_id=message_id,
            message_date=matched_message_date,
            prompt_id=pending_prompt.prompt_id,
            prompt_message_id=prompt_message_id,
            reply_to_message_date=matched_reply_to_message_date,
        )
        selected_quote_text = self._extract_selected_quote_text(message)
        user_message_id = message.get("message_id")
        reply_to_user_message_id = user_message_id if isinstance(user_message_id, int) else None

        response_text = message.get("text")
        if isinstance(response_text, str) and response_text.strip():
            command = self._parse_series_command(response_text)
            if command is not None:
                await self._handle_series_command(
                    command,
                    prompt_message_id,
                    pending_prompt,
                    reply_to_message_id=reply_to_user_message_id,
                )
                return

            if await self._has_pending_attachment_group(prompt_message_id, pending_prompt):
                await self._handle_attachment_reply(
                    prompt_message_id,
                    pending_prompt,
                    message,
                    selected_quote_text=selected_quote_text,
                    reply_to_message_id=reply_to_user_message_id,
                )
                return

            await self._handle_text_reply(
                prompt_message_id,
                pending_prompt,
                telegram_entities_to_markdown(response_text, message.get("entities")),
                source_text=response_text,
                selected_quote_text=selected_quote_text,
                reply_to_message_id=reply_to_user_message_id,
            )
            return

        if self._is_file_or_media_reply(message):
            await self._handle_attachment_reply(
                prompt_message_id,
                pending_prompt,
                message,
                selected_quote_text=selected_quote_text,
                reply_to_message_id=reply_to_user_message_id,
            )
            return

        resolution = await self._build_reply_resolution(
            message,
            pending_prompt.prompt_id,
            download_dir=pending_prompt.download_dir,
        )

        if isinstance(resolution, TelegramReplyRejection):
            await self._send_status_message(
                resolution.user_message,
                reply_to_message_id=reply_to_user_message_id,
            )
            return

        await self._resolve_pending_prompt(
            prompt_message_id,
            pending_prompt,
            self._format_agent_response(
                resolution.agent_response,
                selected_quote_text=selected_quote_text,
            ),
            reply_to_message_id=reply_to_user_message_id,
        )

    async def _handle_text_reply(
        self,
        prompt_message_id: int,
        pending_prompt: TelegramPendingPrompt,
        response_text: str,
        *,
        source_text: str,
        selected_quote_text: Optional[str],
        reply_to_message_id: Optional[int],
    ) -> None:
        """Resolve a text reply, collecting likely Telegram-split follow-up parts."""
        should_wait_for_split_part = len(source_text) >= self.TEXT_REPLY_SPLIT_MIN_LENGTH
        should_resolve_now = False
        async with self._lock:
            current_pending = self._pending_by_message_id.get(prompt_message_id)
            if (
                current_pending is None
                or current_pending is not pending_prompt
                or current_pending.future.done()
            ):
                return

            current_pending.text_reply_parts.append(
                TelegramTextReplyPart(
                    text=response_text,
                    source_starts_with_whitespace=source_text[0].isspace(),
                    source_ends_with_whitespace=source_text[-1].isspace(),
                )
            )
            if selected_quote_text is not None and current_pending.selected_quote_text is None:
                current_pending.selected_quote_text = selected_quote_text
            current_pending.text_reply_ack_message_id = reply_to_message_id
            if should_wait_for_split_part:
                current_pending.text_reply_wait_after_poll_sequence = self._poll_sequence
                current_pending.text_reply_post_part_poll_processed.clear()
                self._debug_event(
                    "telegram_text_reply_split_wait",
                    prompt_id=current_pending.prompt_id,
                    prompt_message_id=prompt_message_id,
                    part_count=len(current_pending.text_reply_parts),
                    wait_after_poll_sequence=self._poll_sequence,
                )
                self._schedule_text_reply_finalize_locked(prompt_message_id, current_pending)
                return

            should_resolve_now = True

        if should_resolve_now:
            await self._resolve_pending_text_reply(prompt_message_id, pending_prompt)

    async def _has_pending_attachment_group(
        self,
        prompt_message_id: int,
        pending_prompt: TelegramPendingPrompt,
    ) -> bool:
        """Check whether a file/media reply burst is already waiting to finalize."""
        async with self._lock:
            return any(
                attachment_group.prompt_message_id == prompt_message_id
                and attachment_group.pending_prompt is pending_prompt
                for attachment_group in self._pending_attachment_groups.values()
            )

    async def _handle_attachment_reply(
        self,
        prompt_message_id: int,
        pending_prompt: TelegramPendingPrompt,
        message: dict[str, Any],
        *,
        selected_quote_text: Optional[str],
        reply_to_message_id: Optional[int],
    ) -> None:
        """Collect file/media replies before resolving the prompt."""
        group_key = self._build_attachment_group_key(prompt_message_id, message)
        async with self._lock:
            current_pending = self._pending_by_message_id.get(prompt_message_id)
            if (
                current_pending is None
                or current_pending is not pending_prompt
                or current_pending.future.done()
            ):
                return

            attachment_group = self._active_series_group
            if attachment_group is not None and (
                attachment_group.prompt_message_id != prompt_message_id
                or attachment_group.pending_prompt is not pending_prompt
            ):
                attachment_group = None

            if attachment_group is not None:
                group_key = attachment_group.group_key
            else:
                attachment_group = self._pending_attachment_groups.get(group_key)
            if attachment_group is None and not self._is_file_or_media_reply(message):
                attachment_group = self._find_attachment_group_for_prompt_locked(
                    prompt_message_id,
                    pending_prompt,
                )
                if attachment_group is not None:
                    group_key = attachment_group.group_key

            if attachment_group is None:
                attachment_group = _PendingAttachmentGroup(
                    prompt_message_id=prompt_message_id,
                    pending_prompt=pending_prompt,
                    group_key=group_key,
                )
                self._pending_attachment_groups[group_key] = attachment_group

            attachment_group.messages.append(message)
            if selected_quote_text is not None and attachment_group.selected_quote_text is None:
                attachment_group.selected_quote_text = selected_quote_text
            attachment_group.reply_to_message_id = reply_to_message_id
            if not attachment_group.series_mode:
                self._schedule_attachment_group_finalize_locked(attachment_group)

    async def _handle_series_command(
        self,
        command: str,
        prompt_message_id: int,
        pending_prompt: TelegramPendingPrompt,
        *,
        reply_to_message_id: Optional[int],
    ) -> None:
        """Start, finish, or abort an explicit attachment series."""
        if command == "begin":
            await self._begin_series(prompt_message_id, pending_prompt, reply_to_message_id)
            return

        if command == "commit":
            await self._commit_series(prompt_message_id, pending_prompt, reply_to_message_id)
            return

        if command == "cancel":
            await self._cancel_series(prompt_message_id, pending_prompt, reply_to_message_id)
            return

    async def _begin_series(
        self,
        prompt_message_id: int,
        pending_prompt: TelegramPendingPrompt,
        reply_to_message_id: Optional[int],
    ) -> None:
        """Enter explicit multi-message attachment mode for one prompt."""
        status_text: Optional[str] = None
        async with self._lock:
            current_pending = self._pending_by_message_id.get(prompt_message_id)
            if (
                current_pending is None
                or current_pending is not pending_prompt
                or current_pending.future.done()
            ):
                return

            if self._active_series_group is not None:
                active_prompt_id = self._active_series_group.pending_prompt.prompt_id
                status_text = (
                    f"⚠️ File collection is already active for [{active_prompt_id}]. "
                    "Send /files_finish or /files_cancel before starting another collection."
                )
            else:
                group_key = self._build_series_group_key(prompt_message_id)
                attachment_group = self._pending_attachment_groups.get(group_key)
                if attachment_group is None:
                    attachment_group = _PendingAttachmentGroup(
                        prompt_message_id=prompt_message_id,
                        pending_prompt=pending_prompt,
                        group_key=group_key,
                        series_mode=True,
                    )
                    self._pending_attachment_groups[group_key] = attachment_group
                else:
                    attachment_group.series_mode = True
                    if attachment_group.finalize_task is not None:
                        attachment_group.finalize_task.cancel()
                        attachment_group.finalize_task = None

                self._active_series_group = attachment_group
                status_text = (
                    f"✅ File collection started for [{pending_prompt.prompt_id}]. "
                    "Send attachments or text, then send /files_finish to finalize and send. "
                    "Send /files_cancel to discard this collection."
                )

        if status_text is not None:
            await self._send_status_message(
                status_text,
                reply_to_message_id=reply_to_message_id,
            )

    async def _commit_series(
        self,
        prompt_message_id: int,
        pending_prompt: TelegramPendingPrompt,
        reply_to_message_id: Optional[int],
    ) -> None:
        """Resolve the active explicit attachment series."""
        status_text: Optional[str] = None
        group_to_resolve: Optional[_PendingAttachmentGroup] = None
        async with self._lock:
            attachment_group = self._active_series_group
            if (
                attachment_group is None
                or attachment_group.prompt_message_id != prompt_message_id
                or attachment_group.pending_prompt is not pending_prompt
            ):
                status_text = (
                    "⚠️ No active file collection for this prompt. Send /files_start first."
                )
            elif not attachment_group.messages:
                status_text = (
                    f"⚠️ File collection [{pending_prompt.prompt_id}] has no items yet. "
                    "Send attachments or text, then send /files_finish to finalize or "
                    "/files_cancel to discard this collection."
                )
            else:
                attachment_group.reply_to_message_id = reply_to_message_id
                group_to_resolve = attachment_group

        if status_text is not None:
            await self._send_status_message(
                status_text,
                reply_to_message_id=reply_to_message_id,
            )
            return

        if group_to_resolve is not None:
            await self._resolve_attachment_group(group_to_resolve.group_key, group_to_resolve)

    async def _cancel_series(
        self,
        prompt_message_id: int,
        pending_prompt: TelegramPendingPrompt,
        reply_to_message_id: Optional[int],
    ) -> None:
        """Discard collected explicit series items and keep the prompt pending."""
        status_text: Optional[str] = None
        async with self._lock:
            attachment_group = self._active_series_group
            if (
                attachment_group is None
                or attachment_group.prompt_message_id != prompt_message_id
                or attachment_group.pending_prompt is not pending_prompt
            ):
                status_text = (
                    "⚠️ No active file collection for this prompt. Send /files_start first."
                )
            else:
                if attachment_group.finalize_task is not None:
                    attachment_group.finalize_task.cancel()
                    attachment_group.finalize_task = None
                self._pending_attachment_groups.pop(attachment_group.group_key, None)
                self._active_series_group = None

                status_text = (
                    f"✅ File collection [{pending_prompt.prompt_id}] cancelled. "
                    "The prompt is still waiting; <b>Reply</b> ↪ normally or start a new file collection. "
                    "Previous files are discarded 🗑️."
                )

        if status_text is not None:
            await self._send_status_message(
                status_text,
                reply_to_message_id=reply_to_message_id,
            )

    def _find_attachment_group_for_prompt_locked(
        self,
        prompt_message_id: int,
        pending_prompt: TelegramPendingPrompt,
    ) -> Optional[_PendingAttachmentGroup]:
        """Find an active attachment group for a prompt."""
        for attachment_group in self._pending_attachment_groups.values():
            if (
                attachment_group.prompt_message_id == prompt_message_id
                and attachment_group.pending_prompt is pending_prompt
            ):
                return attachment_group
        return None

    def _schedule_attachment_group_finalize_locked(
        self,
        attachment_group: _PendingAttachmentGroup,
    ) -> None:
        """Restart the quiet period for one attachment group."""
        if attachment_group.finalize_task is not None:
            attachment_group.finalize_task.cancel()

        finalize_task = asyncio.create_task(
            self._finalize_attachment_group_after_debounce(
                attachment_group.group_key,
                attachment_group,
            )
        )
        finalize_task.add_done_callback(self._consume_task_result)
        attachment_group.finalize_task = finalize_task

    async def _finalize_attachment_group_after_debounce(
        self,
        group_key: tuple[int, str],
        attachment_group: _PendingAttachmentGroup,
    ) -> None:
        """Resolve a collected media group or ungrouped attachment burst."""
        await asyncio.sleep(self.ATTACHMENT_REPLY_DEBOUNCE_SECONDS)
        await self._resolve_attachment_group(group_key, attachment_group)

    async def _resolve_attachment_group(
        self,
        group_key: tuple[int, str],
        attachment_group: _PendingAttachmentGroup,
    ) -> None:
        """Turn collected attachment messages into one prompt response."""
        async with self._lock:
            current_group = self._pending_attachment_groups.get(group_key)
            if current_group is not attachment_group:
                return
            attachment_group.finalize_task = None
            messages = list(attachment_group.messages)
            pending_prompt = attachment_group.pending_prompt
            prompt_message_id = attachment_group.prompt_message_id
            reply_to_message_id = attachment_group.reply_to_message_id
            selected_quote_text = attachment_group.selected_quote_text

        resolution = await self._build_attachment_group_resolution(
            messages,
            pending_prompt.prompt_id,
            download_dir=pending_prompt.download_dir,
        )

        async with self._lock:
            current_group = self._pending_attachment_groups.get(group_key)
            if current_group is not attachment_group:
                return
            if len(attachment_group.messages) != len(messages):
                self._schedule_attachment_group_finalize_locked(attachment_group)
                return
            self._pending_attachment_groups.pop(group_key, None)
            if self._active_series_group is attachment_group:
                self._active_series_group = None

        if isinstance(resolution, TelegramReplyRejection):
            try:
                await self._send_status_message(
                    resolution.user_message,
                    reply_to_message_id=reply_to_message_id,
                )
            except TelegramPromptError as exc:
                await self._fail_pending_prompt(
                    prompt_message_id,
                    pending_prompt,
                    TelegramPromptError(f"Telegram polling failed: {exc}"),
                )
            return

        await self._resolve_pending_prompt(
            prompt_message_id,
            pending_prompt,
            self._format_agent_response(
                resolution.agent_response,
                selected_quote_text=selected_quote_text,
            ),
            reply_to_message_id=reply_to_message_id,
        )

    async def _fail_pending_prompt(
        self,
        prompt_message_id: int,
        pending_prompt: TelegramPendingPrompt,
        error: TelegramPromptError,
    ) -> None:
        """Fail a prompt from a background finalizer task."""
        finalize_tasks: list[asyncio.Task[None]] = []
        async with self._lock:
            current_pending = self._pending_by_message_id.get(prompt_message_id)
            if (
                current_pending is not None
                and current_pending is pending_prompt
                and not current_pending.future.done()
            ):
                current_pending.future.set_exception(error)
                finalize_tasks = self._remove_pending_prompt_locked(current_pending)

        current_task = asyncio.current_task()
        for finalize_task in finalize_tasks:
            if finalize_task is not current_task:
                finalize_task.cancel()

    def _schedule_text_reply_finalize_locked(
        self,
        prompt_message_id: int,
        pending_prompt: TelegramPendingPrompt,
    ) -> None:
        """Restart the minimum grace period for a likely split text reply."""
        if pending_prompt.text_reply_finalize_task is not None:
            pending_prompt.text_reply_finalize_task.cancel()

        finalize_task = asyncio.create_task(
            self._finalize_text_reply_after_debounce(prompt_message_id, pending_prompt)
        )
        finalize_task.add_done_callback(self._consume_task_result)
        pending_prompt.text_reply_finalize_task = finalize_task

    async def _finalize_text_reply_after_debounce(
        self,
        prompt_message_id: int,
        pending_prompt: TelegramPendingPrompt,
    ) -> None:
        """Resolve after the grace period and a later poll page has been processed."""
        await asyncio.sleep(self.TEXT_REPLY_SPLIT_DEBOUNCE_SECONDS)
        await pending_prompt.text_reply_post_part_poll_processed.wait()
        await self._resolve_pending_text_reply(prompt_message_id, pending_prompt)

    async def _resolve_pending_text_reply(
        self,
        prompt_message_id: int,
        pending_prompt: TelegramPendingPrompt,
    ) -> None:
        """Resolve all collected text parts for one pending prompt."""
        agent_response = self._combine_text_reply_parts(pending_prompt.text_reply_parts).strip()
        if not agent_response:
            return

        await self._resolve_pending_prompt(
            prompt_message_id,
            pending_prompt,
            self._format_agent_response(
                agent_response,
                selected_quote_text=pending_prompt.selected_quote_text,
            ),
            reply_to_message_id=pending_prompt.text_reply_ack_message_id,
        )

    @staticmethod
    def _combine_text_reply_parts(parts: list[TelegramTextReplyPart]) -> str:
        """Combine Telegram-split text replies while avoiding glued boundaries."""
        if not parts:
            return ""

        combined_parts = [parts[0].text]
        for previous, part in zip(parts, parts[1:]):
            if (
                previous.text
                and part.text
                and not previous.source_ends_with_whitespace
                and not part.source_starts_with_whitespace
            ):
                combined_parts.append("\n")
            combined_parts.append(part.text)

        return "".join(combined_parts)

    @staticmethod
    def _format_agent_response(
        agent_response: str,
        *,
        selected_quote_text: Optional[str],
    ) -> str:
        """Include Telegram's manual selected quote when the user replied with one."""
        if selected_quote_text is None:
            return agent_response

        return "\n".join(
            [
                "User quoted your prompt:",
                selected_quote_text,
                "",
                "User reply:",
                agent_response,
            ]
        )

    async def _resolve_pending_prompt(
        self,
        prompt_message_id: int,
        pending_prompt: TelegramPendingPrompt,
        agent_response: str,
        *,
        reply_to_message_id: Optional[int],
    ) -> None:
        """Resolve one pending prompt and acknowledge the consumed Telegram reply."""
        should_ack = False
        finalize_tasks: list[asyncio.Task[None]] = []
        self._debug_event(
            "telegram_prompt_resolve_start",
            prompt_id=pending_prompt.prompt_id,
            prompt_message_id=prompt_message_id,
            reply_to_message_id=reply_to_message_id,
        )
        async with self._lock:
            current_pending = self._pending_by_message_id.get(prompt_message_id)
            if (
                current_pending is not None
                and current_pending is pending_prompt
                and not current_pending.future.done()
            ):
                current_pending.future.set_result(agent_response)
                finalize_tasks = self._remove_pending_prompt_locked(current_pending)
                should_ack = True
                self._debug_event(
                    "telegram_prompt_future_resolved",
                    prompt_id=pending_prompt.prompt_id,
                    prompt_message_id=prompt_message_id,
                )

        current_task = asyncio.current_task()
        for finalize_task in finalize_tasks:
            if finalize_task is not current_task:
                finalize_task.cancel()

        if should_ack:
            self._schedule_received_ack(
                pending_prompt.prompt_id,
                reply_to_message_id=reply_to_message_id,
            )

    def _schedule_received_ack(self, prompt_id: str, *, reply_to_message_id: Optional[int]) -> None:
        """Send the non-critical Received status without blocking reply polling."""
        task = asyncio.create_task(
            self._send_received_ack(prompt_id, reply_to_message_id=reply_to_message_id)
        )
        self._background_status_tasks.add(task)
        task.add_done_callback(self._background_status_tasks.discard)
        task.add_done_callback(self._consume_task_result)

    async def _send_received_ack(
        self,
        prompt_id: str,
        *,
        reply_to_message_id: Optional[int],
    ) -> None:
        """Send and time the best-effort Received status message."""
        ack_started_at = time.monotonic()
        self._debug_event(
            "telegram_ack_send_start",
            prompt_id=prompt_id,
            reply_to_message_id=reply_to_message_id,
        )
        ack_sent = await self._safe_send_status_message(
            f"✅ Received [{prompt_id}]",
            reply_to_message_id=reply_to_message_id,
        )
        self._debug_event(
            "telegram_ack_send_done",
            prompt_id=prompt_id,
            reply_to_message_id=reply_to_message_id,
            sent=ack_sent,
            duration_ms=duration_ms(ack_started_at),
        )

    def _remove_pending_prompt_locked(
        self,
        pending_prompt: TelegramPendingPrompt,
    ) -> list[asyncio.Task[None]]:
        """Remove every message-id alias for one pending prompt."""
        finalize_tasks: list[asyncio.Task[None]] = []
        message_ids = [
            message_id
            for message_id, current_pending in self._pending_by_message_id.items()
            if current_pending is pending_prompt
        ]
        for message_id in message_ids:
            self._pending_by_message_id.pop(message_id, None)

        if pending_prompt.text_reply_finalize_task is not None:
            finalize_tasks.append(pending_prompt.text_reply_finalize_task)
        pending_prompt.text_reply_finalize_task = None
        for group_key, attachment_group in list(self._pending_attachment_groups.items()):
            if attachment_group.pending_prompt is pending_prompt:
                if attachment_group.finalize_task is not None:
                    finalize_tasks.append(attachment_group.finalize_task)
                attachment_group.finalize_task = None
                self._pending_attachment_groups.pop(group_key, None)
                if self._active_series_group is attachment_group:
                    self._active_series_group = None

        if not self._pending_by_message_id:
            self._latest_prompt_message_id = None
        elif self._latest_prompt_message_id in message_ids:
            self._latest_prompt_message_id = max(self._pending_by_message_id)
        return finalize_tasks

    def _unique_pending_prompts_locked(self) -> list[TelegramPendingPrompt]:
        """Return unique pending prompts from the message-id index."""
        pending_items: list[TelegramPendingPrompt] = []
        seen_ids: set[int] = set()
        for pending_prompt in self._pending_by_message_id.values():
            pending_identity = id(pending_prompt)
            if pending_identity in seen_ids:
                continue
            seen_ids.add(pending_identity)
            pending_items.append(pending_prompt)
        return pending_items

    async def _match_pending_prompt(
        self, update: dict[str, Any]
    ) -> Optional[tuple[int, TelegramPendingPrompt, dict[str, Any]]]:
        """Find a pending prompt that this Telegram update replies to."""
        message = update.get("message")
        if not isinstance(message, dict):
            return None

        chat = message.get("chat")
        if not isinstance(chat, dict) or str(chat.get("id")) != self.chat_id:
            return None

        reply_to_message = message.get("reply_to_message")
        if not isinstance(reply_to_message, dict):
            return None

        reply_message_id = reply_to_message.get("message_id")
        if not isinstance(reply_message_id, int):
            return None

        async with self._lock:
            pending_prompt = self._pending_by_message_id.get(reply_message_id)

        if pending_prompt is None:
            if await self._message_predates_latest_prompt(message):
                return None
            if await self._warn_on_stale_local_reply(message, reply_to_message):
                return None
            if await self._warn_on_foreign_broker_reply(message, reply_to_message):
                return None
            await self._warn_on_unmatched_reply(message)
            return None

        return reply_message_id, pending_prompt, message

    async def _match_active_series(
        self,
        update: dict[str, Any],
    ) -> Optional[tuple[int, TelegramPendingPrompt, dict[str, Any]]]:
        """Route non-reply messages into an explicit active series."""
        message = update.get("message")
        if not isinstance(message, dict):
            return None

        chat = message.get("chat")
        if not isinstance(chat, dict) or str(chat.get("id")) != self.chat_id:
            return None

        if message.get("from", {}).get("is_bot"):
            return None

        # A reply to another prompt should escape series mode and be handled normally.
        if isinstance(message.get("reply_to_message"), dict):
            return None

        if not self._is_user_reply_candidate(message):
            return None

        async with self._lock:
            attachment_group = self._active_series_group
            if attachment_group is None:
                return None
            current_pending = self._pending_by_message_id.get(attachment_group.prompt_message_id)
            if (
                current_pending is None
                or current_pending is not attachment_group.pending_prompt
                or current_pending.future.done()
            ):
                return None

        return (
            attachment_group.prompt_message_id,
            attachment_group.pending_prompt,
            message,
        )

    async def _maybe_hint_on_missing_reply(self, update: dict[str, Any]) -> None:
        """Warn when the user sends a non-reply message while a local prompt is pending."""
        message = update.get("message")
        if not isinstance(message, dict):
            return

        chat = message.get("chat")
        if not isinstance(chat, dict) or str(chat.get("id")) != self.chat_id:
            return

        if message.get("from", {}).get("is_bot"):
            return

        if isinstance(message.get("reply_to_message"), dict):
            return

        if not self._is_user_reply_candidate(message):
            return

        async with self._lock:
            if not self._pending_by_message_id or self._latest_prompt_message_id is None:
                return

        user_message_id = self._extract_message_id(message)
        if await self._message_predates_latest_prompt(message):
            return

        await self._send_status_message(
            self._with_series_attachment_hint(self.NON_REPLY_HINT_TEXT, message),
            reply_to_message_id=user_message_id if user_message_id else None,
        )

    async def _build_reply_resolution(
        self,
        message: dict[str, Any],
        prompt_id: str,
        *,
        download_dir: Path,
        allow_media_group: bool = False,
    ) -> TelegramReplyResolution | TelegramReplyRejection:
        """Turn one matched Telegram reply into an agent-facing response or a retry prompt."""
        if isinstance(message.get("media_group_id"), str) and not allow_media_group:
            return TelegramReplyRejection(
                f"⚠️ Unsupported reply for [{prompt_id}]. Albums/media groups are not supported yet. "
                "Please reply again with a single text, file, media, location, venue, or contact message."
            )

        response_text = message.get("text")
        if isinstance(response_text, str) and response_text.strip():
            return TelegramReplyResolution(
                telegram_entities_to_markdown(response_text, message.get("entities")).strip()
            )

        caption_text = message.get("caption")
        caption = (
            telegram_entities_to_markdown(caption_text, message.get("caption_entities")).strip()
            if isinstance(caption_text, str) and caption_text.strip()
            else None
        )
        reply_message_id = self._extract_message_id(message)

        if isinstance(message.get("document"), dict):
            return await self._build_file_reply(
                "document",
                cast(dict[str, Any], message["document"]),
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                caption=caption,
                original_file_name=self._clean_optional_text(message["document"].get("file_name")),
            )

        if isinstance(message.get("video"), dict):
            return await self._build_file_reply(
                "video",
                cast(dict[str, Any], message["video"]),
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                caption=caption,
                original_file_name=self._clean_optional_text(message["video"].get("file_name")),
            )

        if isinstance(message.get("audio"), dict):
            return await self._build_file_reply(
                "audio",
                cast(dict[str, Any], message["audio"]),
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                caption=caption,
                original_file_name=self._clean_optional_text(message["audio"].get("file_name")),
            )

        if isinstance(message.get("voice"), dict):
            return await self._build_file_reply(
                "voice",
                cast(dict[str, Any], message["voice"]),
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                caption=caption,
            )

        if isinstance(message.get("animation"), dict):
            return await self._build_file_reply(
                "animation",
                cast(dict[str, Any], message["animation"]),
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                caption=caption,
                original_file_name=self._clean_optional_text(message["animation"].get("file_name")),
            )

        if isinstance(message.get("video_note"), dict):
            return await self._build_file_reply(
                "video_note",
                cast(dict[str, Any], message["video_note"]),
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                maybe_unintended=True,
            )

        if isinstance(message.get("sticker"), dict):
            return await self._build_file_reply(
                "sticker",
                cast(dict[str, Any], message["sticker"]),
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                maybe_unintended=True,
            )

        photo = self._choose_photo_variant(message.get("photo"))
        if photo is not None:
            return await self._build_file_reply(
                "photo",
                photo,
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                caption=caption,
            )

        if isinstance(message.get("location"), dict) and isinstance(message.get("venue"), dict):
            return TelegramReplyResolution(
                self._format_structured_reply(
                    "venue",
                    [
                        f"Title: {cast(dict[str, Any], message['venue']).get('title', '')}",
                        f"Address: {cast(dict[str, Any], message['venue']).get('address', '')}",
                        f"Latitude: {cast(dict[str, Any], message['location']).get('latitude', '')}",
                        f"Longitude: {cast(dict[str, Any], message['location']).get('longitude', '')}",
                    ],
                )
            )

        if isinstance(message.get("location"), dict):
            location = cast(dict[str, Any], message["location"])
            return TelegramReplyResolution(
                self._format_structured_reply(
                    "location",
                    [
                        f"Latitude: {location.get('latitude', '')}",
                        f"Longitude: {location.get('longitude', '')}",
                    ],
                )
            )

        if isinstance(message.get("contact"), dict):
            contact = cast(dict[str, Any], message["contact"])
            return TelegramReplyResolution(
                self._format_structured_reply(
                    "contact",
                    [
                        f"Phone number: {contact.get('phone_number', '')}",
                        f"First name: {contact.get('first_name', '')}",
                        f"Last name: {contact.get('last_name', '')}",
                        f"User ID: {contact.get('user_id', '')}",
                        f"VCard: {contact.get('vcard', '')}",
                    ],
                )
            )

        if isinstance(message.get("poll"), dict):
            poll = cast(dict[str, Any], message["poll"])
            options = poll.get("options")
            option_labels: list[str] = []
            if isinstance(options, list):
                for option in options:
                    if isinstance(option, dict):
                        option_labels.append(
                            f"- {option.get('text', '')} ({option.get('voter_count', '')} votes)"
                        )
            return TelegramReplyResolution(
                self._format_structured_reply(
                    "poll",
                    [
                        "Note: this may be unintended unless explicitly expected.",
                        f"Question: {poll.get('question', '')}",
                        f"Type: {poll.get('type', '')}",
                        *option_labels,
                    ],
                )
            )

        if isinstance(message.get("dice"), dict):
            dice = cast(dict[str, Any], message["dice"])
            return TelegramReplyResolution(
                self._format_structured_reply(
                    "dice",
                    [
                        "Note: this may be unintended unless explicitly expected.",
                        f"Emoji: {dice.get('emoji', '')}",
                        f"Value: {dice.get('value', '')}",
                    ],
                )
            )

        if isinstance(message.get("game"), dict):
            game = cast(dict[str, Any], message["game"])
            return TelegramReplyResolution(
                self._format_structured_reply(
                    "game",
                    [
                        "Note: this may be unintended unless explicitly expected.",
                        f"Title: {game.get('title', '')}",
                        f"Description: {game.get('description', '')}",
                    ],
                )
            )

        return TelegramReplyRejection(
            f"⚠️ Unsupported reply for [{prompt_id}]. Please reply with text, a supported "
            "single file/media message, location, venue, or contact."
        )

    async def _build_attachment_group_resolution(
        self,
        messages: list[dict[str, Any]],
        prompt_id: str,
        *,
        download_dir: Path,
    ) -> TelegramReplyResolution | TelegramReplyRejection:
        """Build one response for a media group or a short attachment burst."""
        if not messages:
            return TelegramReplyRejection(
                f"⚠️ Unsupported reply for [{prompt_id}]. Please reply again with text, "
                "file, media, location, venue, or contact."
            )

        if len(messages) == 1:
            return await self._build_reply_resolution(
                messages[0],
                prompt_id,
                download_dir=download_dir,
                allow_media_group=True,
            )

        item_responses: list[str] = []
        has_media_group_id = False
        for index, message in enumerate(messages, start=1):
            has_media_group_id = has_media_group_id or isinstance(
                message.get("media_group_id"), str
            )
            resolution = await self._build_reply_resolution(
                message,
                prompt_id,
                download_dir=download_dir,
                allow_media_group=True,
            )
            if isinstance(resolution, TelegramReplyRejection):
                return TelegramReplyRejection(
                    f"⚠️ Unsupported attachment group for [{prompt_id}]. One item could not be "
                    f"used: {resolution.user_message}"
                )

            item_responses.append(f"Item {index}/{len(messages)}:\n{resolution.agent_response}")

        reply_type = "media group" if has_media_group_id else "attachment group"
        return TelegramReplyResolution(
            "\n\n".join(
                [
                    f"[telegram {reply_type} reply]",
                    f"Items: {len(messages)}",
                    *item_responses,
                ]
            )
        )

    async def _build_file_reply(
        self,
        reply_type: str,
        file_payload: dict[str, Any],
        prompt_id: str,
        *,
        download_dir: Path,
        reply_message_id: int,
        caption: Optional[str] = None,
        original_file_name: Optional[str] = None,
        maybe_unintended: bool = False,
    ) -> TelegramReplyResolution | TelegramReplyRejection:
        """Download a Telegram file payload and build the agent-facing reply text."""
        file_id = file_payload.get("file_id")
        if not isinstance(file_id, str) or not file_id.strip():
            return TelegramReplyRejection(
                f"⚠️ Unsupported reply for [{prompt_id}]. Telegram did not provide a downloadable "
                f"{reply_type} file ID. Please reply again with text or another supported message type."
            )

        file_size = file_payload.get("file_size")
        if isinstance(file_size, int) and file_size > TELEGRAM_DOWNLOAD_LIMIT_BYTES:
            return TelegramReplyRejection(
                f"⚠️ File too large for [{prompt_id}]. The default Telegram Bot API supports files "
                f"up to {TELEGRAM_DOWNLOAD_LIMIT_LABEL}. Please send a smaller file or a text reply."
            )

        try:
            saved_path = await self._download_telegram_file(
                file_id,
                reply_type,
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                original_file_name=original_file_name,
                declared_file_size=file_size if isinstance(file_size, int) else None,
            )
        except ValueError:
            return TelegramReplyRejection(
                f"⚠️ File too large for [{prompt_id}]. The default Telegram Bot API supports files "
                f"up to {TELEGRAM_DOWNLOAD_LIMIT_LABEL}. Please send a smaller file or a text reply."
            )
        except TelegramPromptError as exc:
            return TelegramReplyRejection(
                f"⚠️ Could not consume your reply for [{prompt_id}]. {exc}. Please reply again with "
                "text or another supported message type."
            )

        lines: list[str] = []
        if maybe_unintended:
            lines.append("Note: this may be unintended unless explicitly expected.")
        if caption:
            lines.append(f"Caption:\n{caption}")
        lines.append(f"User attached file: {saved_path}")
        if original_file_name and Path(saved_path).name != original_file_name:
            lines.append(f"Original file name: {original_file_name}")

        return TelegramReplyResolution(self._format_structured_reply(reply_type, lines))

    async def _download_telegram_file(
        self,
        file_id: str,
        reply_type: str,
        prompt_id: str,
        *,
        download_dir: Path,
        reply_message_id: int,
        original_file_name: Optional[str] = None,
        declared_file_size: Optional[int] = None,
    ) -> str:
        """Download a Telegram file and return its saved local path."""
        if declared_file_size is not None and declared_file_size > TELEGRAM_DOWNLOAD_LIMIT_BYTES:
            raise ValueError("Telegram file exceeds the supported download limit.")

        file_result = await self._bot_api_request("getFile", {"file_id": file_id}, timeout=20)
        if not isinstance(file_result, dict):
            raise TelegramPromptError("Telegram getFile returned an unexpected payload.")

        file_path = file_result.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            raise TelegramPromptError("Telegram getFile did not return a usable file path.")

        file_size = file_result.get("file_size")
        if isinstance(file_size, int) and file_size > TELEGRAM_DOWNLOAD_LIMIT_BYTES:
            raise ValueError("Telegram file exceeds the supported download limit.")

        target_dir = download_dir / prompt_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target_name = self._build_download_file_name(
            reply_type,
            prompt_id,
            reply_message_id,
            original_file_name=original_file_name,
            telegram_file_path=file_path,
        )
        target_path = self._resolve_unique_download_path(target_dir / target_name)

        await asyncio.to_thread(self._download_telegram_file_sync, file_path, target_path)
        return str(target_path.resolve())

    def _download_telegram_file_sync(self, telegram_file_path: str, target_path: Path) -> None:
        """Download one Telegram file through the standard file endpoint."""
        file_url = (
            f"https://api.telegram.org/file/bot{self.bot_token}/"
            f"{urllib.parse.quote(telegram_file_path, safe='/')}"
        )

        try:
            with urllib.request.urlopen(file_url, timeout=60) as response:
                with target_path.open("wb") as output_file:
                    shutil.copyfileobj(response, output_file)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise TelegramPromptError(
                f"Telegram file download failed with HTTP {exc.code}: {error_body}"
            ) from exc
        except OSError as exc:
            raise TelegramPromptError(f"Telegram file download failed: {exc}") from exc

    def _build_download_file_name(
        self,
        reply_type: str,
        prompt_id: str,
        reply_message_id: int,
        *,
        original_file_name: Optional[str],
        telegram_file_path: str,
    ) -> str:
        """Create a safe local filename for one downloaded Telegram artifact."""
        preferred_name = self._clean_optional_text(original_file_name)
        if preferred_name:
            base_name = Path(preferred_name).name
        else:
            base_name = Path(telegram_file_path).name

        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', "_", base_name).strip(" .")
        if not safe_name:
            suffix = Path(telegram_file_path).suffix
            safe_name = f"{reply_type}-{prompt_id}-{reply_message_id}{suffix}"

        return safe_name

    @staticmethod
    def _resolve_unique_download_path(target_path: Path) -> Path:
        """Avoid overwriting earlier files from the same Telegram prompt."""
        if not target_path.exists():
            return target_path

        stem = target_path.stem
        suffix = target_path.suffix
        parent = target_path.parent
        counter = 2
        while True:
            candidate = parent / f"{stem}-{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    @staticmethod
    def _choose_photo_variant(photo_payload: Any) -> Optional[dict[str, Any]]:
        """Pick the largest Telegram photo variant from the Message.photo list."""
        if not isinstance(photo_payload, list):
            return None

        candidates = [item for item in photo_payload if isinstance(item, dict)]
        if not candidates:
            return None

        def _photo_rank(photo: dict[str, Any]) -> tuple[int, int]:
            file_size = photo.get("file_size")
            width = photo.get("width")
            height = photo.get("height")
            return (
                file_size if isinstance(file_size, int) else -1,
                (width * height) if isinstance(width, int) and isinstance(height, int) else -1,
            )

        return max(candidates, key=_photo_rank)

    @staticmethod
    def _clean_optional_text(value: Any) -> Optional[str]:
        """Normalize an optional Telegram text/caption value."""
        if not isinstance(value, str):
            return None

        cleaned = value.strip()
        return cleaned or None

    @staticmethod
    def _extract_message_id(message: dict[str, Any]) -> int:
        """Read the Telegram message id or return 0 if missing."""
        message_id = message.get("message_id")
        return message_id if isinstance(message_id, int) else 0

    @staticmethod
    def _extract_selected_quote_text(message: dict[str, Any]) -> Optional[str]:
        """Read Telegram's manual selected quote, if the reply includes one."""
        quote = message.get("quote")
        if not isinstance(quote, dict):
            return None

        quote_text = quote.get("text")
        if not isinstance(quote_text, str):
            return None

        cleaned_quote_text = telegram_entities_to_markdown(
            quote_text, quote.get("entities")
        ).strip()
        return cleaned_quote_text or None

    async def _message_predates_latest_prompt(self, message: dict[str, Any]) -> bool:
        """Ignore backlog messages that Telegram delivers after a fresh prompt starts."""
        message_id = self._extract_message_id(message)
        if not message_id:
            return False

        async with self._lock:
            latest_prompt_message_id = self._latest_prompt_message_id

        if latest_prompt_message_id is None:
            return False

        return message_id <= latest_prompt_message_id

    @staticmethod
    def _format_structured_reply(reply_type: str, lines: list[str]) -> str:
        """Format a non-text Telegram reply for the agent as plain text."""
        cleaned_lines = [line for line in lines if line]
        if not cleaned_lines:
            return f"[telegram {reply_type} reply]"

        return f"[telegram {reply_type} reply]\n" + "\n".join(cleaned_lines)

    @staticmethod
    def _parse_series_command(text: str) -> Optional[str]:
        """Parse explicit multi-message reply commands."""
        cleaned = text.strip().lower()
        if any(char.isspace() for char in cleaned):
            return None

        command, separator, bot_username = cleaned.partition("@")
        if separator and not bot_username:
            return None
        if bot_username and not bot_username.replace("_", "").isalnum():
            return None

        if command == "/files_start":
            return "begin"
        if command == "/files_finish":
            return "commit"
        if command == "/files_cancel":
            return "cancel"
        return None

    @classmethod
    def _is_file_or_media_reply(cls, message: dict[str, Any]) -> bool:
        """Return whether this message should be debounced as an attachment reply."""
        if isinstance(message.get("media_group_id"), str):
            return True
        return any(key in message for key in cls.ATTACHMENT_REPLY_KEYS)

    @staticmethod
    def _build_attachment_group_key(
        prompt_message_id: int,
        message: dict[str, Any],
    ) -> tuple[int, str]:
        """Group attachment replies by the prompt they answer."""
        return prompt_message_id, "ungrouped"

    @staticmethod
    def _build_series_group_key(prompt_message_id: int) -> tuple[int, str]:
        """Build the explicit series group key for one prompt."""
        return prompt_message_id, "series"

    @staticmethod
    def _is_user_reply_candidate(message: dict[str, Any]) -> bool:
        """Decide whether a non-reply Telegram message looks like an attempted answer."""
        candidate_keys = (
            "text",
            "document",
            "video",
            "audio",
            "voice",
            "animation",
            "video_note",
            "sticker",
            "photo",
            "location",
            "venue",
            "contact",
            "poll",
            "dice",
            "game",
            "media_group_id",
        )
        return any(key in message for key in candidate_keys)

    async def _send_status_message(
        self, text: str, *, reply_to_message_id: Optional[int] = None
    ) -> None:
        """Send a Telegram status/warning message and surface failures to the prompt."""
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
        }
        if self._status_text_uses_html(text):
            payload["parse_mode"] = self.PROMPT_PARSE_MODE
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id

        try:
            await self._bot_api_request("sendMessage", payload, timeout=20)
        except TelegramPromptError as exc:
            if "parse_mode" not in payload or not self._is_markup_parse_error(exc):
                raise
            fallback_payload = {
                **payload,
                "text": telegram_html_to_plain_text(text),
            }
            fallback_payload.pop("parse_mode", None)
            await self._bot_api_request("sendMessage", fallback_payload, timeout=20)

    @staticmethod
    def _status_text_uses_html(text: str) -> bool:
        """Return whether a fixed status/warning message uses Telegram HTML markup."""
        return "<b>" in text or "</b>" in text

    async def _safe_send_status_message(
        self, text: str, *, reply_to_message_id: Optional[int] = None
    ) -> bool:
        """Best-effort Telegram status/ack message after a successful reply was captured."""
        try:
            await self._send_status_message(text, reply_to_message_id=reply_to_message_id)
        except TelegramPromptError:
            return False
        return True

    async def _warn_on_foreign_broker_reply(
        self,
        message: dict[str, Any],
        reply_to_message: dict[str, Any],
    ) -> bool:
        """Warn if this broker consumed a reply that appears intended for another broker."""
        if self.broker_identity is None:
            return False

        replied_text = reply_to_message.get("text")
        if not isinstance(replied_text, str):
            return False

        broker_reference = self._parse_broker_reference(replied_text)
        if broker_reference is None:
            return False

        foreign_label, foreign_id = broker_reference
        if foreign_id == self.broker_identity.broker_id:
            return False

        reply_message_id = self._extract_message_id(message)
        warning_text = (
            f"⚠️ Instance [{self.broker_identity.broker_label} [{self.broker_identity.broker_id}]] "
            f"just consumed your reply, but it appears intended for instance "
            f"[{foreign_label} [{foreign_id}]]. If you use the same bot from multiple machines "
            f"or apps at the same time, avoid doing that. Otherwise, please open an issue: "
            f"{self.ISSUE_URL}"
        )
        await self._send_status_message(
            warning_text,
            reply_to_message_id=reply_message_id if reply_message_id else None,
        )
        return True

    async def _warn_on_unmatched_reply(self, message: dict[str, Any]) -> bool:
        """Warn for replies that target no prompt this broker can currently match."""
        if message.get("from", {}).get("is_bot"):
            return False

        if not self._is_user_reply_candidate(message):
            return False

        user_message_id = self._extract_message_id(message)
        await self._send_status_message(
            self.UNMATCHED_REPLY_HINT_TEXT,
            reply_to_message_id=user_message_id if user_message_id else None,
        )
        return True

    async def _warn_on_stale_local_reply(
        self,
        message: dict[str, Any],
        reply_to_message: dict[str, Any],
    ) -> bool:
        """Warn when a reply targets one of this broker's own no-longer-active prompts."""
        if self.broker_identity is None:
            return False

        replied_text = reply_to_message.get("text")
        if not isinstance(replied_text, str):
            return False

        broker_reference = self._parse_broker_reference(replied_text)
        if broker_reference is None or broker_reference[1] != self.broker_identity.broker_id:
            return False

        stale_prompt_id = self._parse_prompt_id_reference(replied_text)

        if stale_prompt_id is None:
            prompt_target = "That question"
        else:
            prompt_target = f"Prompt [{stale_prompt_id}]"

        warning_text = self._with_series_attachment_hint(
            self.STALE_REPLY_HINT_TEMPLATE.format(prompt_target=prompt_target),
            message,
        )
        user_message_id = self._extract_message_id(message)
        await self._send_status_message(
            warning_text,
            reply_to_message_id=user_message_id if user_message_id else None,
        )
        return True

    @classmethod
    def _with_series_attachment_hint(cls, warning_text: str, message: dict[str, Any]) -> str:
        """Append the explicit series hint only for ignored attachment attempts."""
        if not cls._is_file_or_media_reply(message):
            return warning_text
        return warning_text + cls.SERIES_ATTACHMENT_HINT_TEXT

    @staticmethod
    def _parse_broker_reference(prompt_text: str) -> Optional[tuple[str, str]]:
        """Extract broker label/id metadata from one broker-formatted prompt."""
        match = re.search(r"Broker:\s*(.+?)\s*\[([a-f0-9]+)\]", prompt_text)
        if not match:
            return None

        return match.group(1).strip(), match.group(2).strip()

    @staticmethod
    def _parse_prompt_id_reference(prompt_text: str) -> Optional[str]:
        """Extract one prompt id from broker-formatted prompt text."""
        match = re.search(r"Prompt ID:\s*(\S+)", prompt_text)
        if not match:
            return None

        prompt_id = match.group(1).strip()
        return prompt_id or None

    async def _bot_api_request(self, method: str, payload: dict[str, Any], *, timeout: int) -> Any:
        """Issue a Telegram Bot API request through the standard library HTTP stack."""
        started_at = time.monotonic()
        self._debug_event("telegram_api_request_start", method=method, timeout_seconds=timeout)
        try:
            result = await asyncio.to_thread(self._bot_api_request_sync, method, payload, timeout)
        except TelegramPromptError as exc:
            self._debug_event(
                "telegram_api_request_error",
                method=method,
                duration_ms=duration_ms(started_at),
                error=exc,
            )
            raise

        result_size = len(result) if isinstance(result, list) else None
        self._debug_event(
            "telegram_api_request_ok",
            method=method,
            duration_ms=duration_ms(started_at),
            result_size=result_size,
        )
        return result

    def _bot_api_request_sync(self, method: str, payload: dict[str, Any], timeout: int) -> Any:
        """Perform a blocking Telegram Bot API request."""
        request_url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        request_data = json.dumps(payload).encode("utf-8")
        request_obj = urllib.request.Request(
            request_url,
            data=request_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request_obj, timeout=timeout) as response:
                payload_json = json.load(response)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise TelegramBotApiError(
                f"Telegram {method} failed with HTTP {exc.code}: {error_body}",
                method=method,
                http_status=exc.code,
            ) from exc
        except OSError as exc:
            raise TelegramBotApiError(
                f"Telegram {method} request failed: {exc}",
                method=method,
                transport_error=True,
            ) from exc

        if not isinstance(payload_json, dict) or not payload_json.get("ok"):
            raise TelegramBotApiError(
                f"Telegram {method} failed: {payload_json.get('description', 'unknown error')}",
                method=method,
            )

        return payload_json.get("result")

    @staticmethod
    def _consume_task_result(task: asyncio.Task[None]) -> None:
        """Avoid noisy unhandled task warnings for background pollers."""
        with suppress(asyncio.CancelledError):
            task.result()
