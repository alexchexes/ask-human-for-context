"""Telegram configuration, constants, and shared reply model types."""

import asyncio
import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS = 25
DEFAULT_TELEGRAM_DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "ask-human" / "telegram-downloads"
TELEGRAM_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class TelegramConfig:
    """Bot token and target chat for Telegram-based prompting."""

    bot_token: str
    chat_id: str


def resolve_telegram_target_key(config: TelegramConfig) -> str:
    """Derive a stable opaque key for one Telegram bot/chat target."""
    payload = f"{config.bot_token}\0{config.chat_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def parse_telegram_target(telegram_target: Optional[str]) -> Optional[TelegramConfig]:
    """Parse '<bot_token> <chat_id>' from a single CLI argument."""
    if telegram_target is None:
        return None

    target = telegram_target.strip()
    if not target:
        return None

    parts = target.split(maxsplit=1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError(
            "Telegram target must look like '<bot_token> <chat_id>' in a single argument."
        )

    return TelegramConfig(bot_token=parts[0].strip(), chat_id=parts[1].strip())


def resolve_telegram_download_dir(download_dir: Optional[str] = None) -> Path:
    """Resolve and expand the configured Telegram download directory."""
    if download_dir is None or not download_dir.strip():
        return DEFAULT_TELEGRAM_DOWNLOAD_DIR

    expanded = download_dir.replace("{cwd}", os.getcwd())
    expanded = os.path.expandvars(expanded)
    expanded = os.path.expanduser(expanded)
    return Path(expanded).resolve()


class TelegramPromptError(Exception):
    """Error while sending a prompt or waiting for a Telegram reply."""

    pass


@dataclass
class TelegramTextReplyPart:
    """Rendered split-reply text plus its original whitespace boundaries."""

    text: str
    source_starts_with_whitespace: bool
    source_ends_with_whitespace: bool


@dataclass
class TelegramPendingPrompt:
    """Track one sent Telegram prompt until a valid reply arrives."""

    future: asyncio.Future[str]
    prompt_id: str
    download_dir: Path
    text_reply_parts: list[TelegramTextReplyPart] = field(default_factory=list)
    selected_quote_text: Optional[str] = None
    text_reply_ack_message_id: Optional[int] = None
    text_reply_finalize_task: Optional[asyncio.Task[None]] = None
    text_reply_wait_after_poll_sequence: Optional[int] = None
    text_reply_post_part_poll_processed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class TelegramReplyResolution:
    """A Telegram reply successfully turned into an agent-facing response string."""

    agent_response: str


@dataclass(frozen=True)
class TelegramReplyRejection:
    """A Telegram reply matched the prompt but should be rejected and retried."""

    user_message: str
