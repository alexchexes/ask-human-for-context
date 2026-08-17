import asyncio
import datetime as dt
import locale
from contextlib import AbstractAsyncContextManager, suppress
from typing import Any, Optional, cast

import uvicorn
from mcp.server import Server
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route

from .broker_state import resolve_broker_state_dir, resolve_target_broker_state_dir
from .dialogs import (
    DEFAULT_DIALOG_TIMEOUT_SECONDS,
    GUIDialogHandler,
    UserPromptCancelled,
    UserPromptError,
)
from .macos_prompt import (
    build_macos_prompt_document,
    build_plain_macos_prompt_document,
)
from .prompt_formatting import (
    DEFAULT_DIALOG_TITLE,
    build_dialog_telegram_notice,
    build_prompt_text,
    generate_prompt_id,
    initialize_time_locale,
)
from .telegram_broker import run_telegram_broker
from .telegram_broker_client import TelegramBrokerClient
from .telegram_models import (
    TelegramPromptError,
    parse_telegram_target,
    resolve_telegram_download_dir,
)

MCP_SERVER_INSTRUCTIONS = (
    "ask_human may remain pending for hours. Treat intermediate statuses or yields as "
    "non-terminal; never terminate or abandon the call solely because it is still pending. "
    "Wait until it returns, reaches its configured timeout, the user cancels, or the "
    "transport fails."
)

# Initialize FastMCP server for human input tools.
mcp = FastMCP("ask-human", instructions=MCP_SERVER_INSTRUCTIONS)

DEFAULT_RESPONSE_CHANNEL = "dialog"
MAX_PROMPT_CHARACTERS = 8000


# Custom exception classes for better error handling (Task 1.4)
class UserPromptTimeout(Exception):
    """Raised when user doesn't respond within timeout period."""

    pass


# Global dialog handler instance
dialog_handler = GUIDialogHandler()
dialog_timeout_seconds = DEFAULT_DIALOG_TIMEOUT_SECONDS
show_timing_info = False
response_channel = DEFAULT_RESPONSE_CHANNEL
telegram_client: Optional[TelegramBrokerClient] = None


def _consume_detached_task_result(task: asyncio.Task[Any]) -> None:
    """Suppress result warnings for intentionally detached background tasks."""
    with suppress(asyncio.CancelledError, Exception):
        task.result()


async def _finish_dialog_after_telegram_done(
    dialog_task: asyncio.Task[Optional[str]],
    cancel_event: Optional[asyncio.Event],
) -> None:
    """Close or detach a local dialog after the Telegram side has decided the result."""
    if dialog_task.done():
        return

    if cancel_event is not None:
        cancel_event.set()
        with suppress(asyncio.TimeoutError, UserPromptError):
            await asyncio.wait_for(dialog_task, timeout=3)
    else:
        dialog_task.add_done_callback(_consume_detached_task_result)


async def _cancel_telegram_task(telegram_task: asyncio.Task[Optional[str]]) -> None:
    """Cancel the Telegram response wait after a local dialog answer wins."""
    if telegram_task.done():
        return

    telegram_task.cancel()
    with suppress(asyncio.CancelledError, TelegramPromptError):
        await telegram_task


async def _get_first_channel_response(
    question: str,
    context: str,
    dialog_prompt: str,
    timeout_seconds: int,
    prompt_id: str,
    issued_at: dt.datetime,
    macos_prompt_document: Optional[dict[str, Any]] = None,
) -> Optional[tuple[str, str]]:
    """Race dialog and Telegram, returning the first successful response."""
    if telegram_client is None:
        raise UserPromptError(
            "Telegram response channel is enabled, but no Telegram target was configured."
        )

    should_thread_windows_dialog = dialog_handler.platform == "Windows"
    cancel_event = None if should_thread_windows_dialog else asyncio.Event()

    dialog_arguments: dict[str, Any] = {
        "cancel_event": cancel_event,
        "run_in_thread": should_thread_windows_dialog,
    }
    if macos_prompt_document is not None:
        dialog_arguments["macos_prompt_document"] = macos_prompt_document
    dialog_task = asyncio.create_task(
        dialog_handler.get_user_input(
            dialog_prompt,
            timeout_seconds,
            **dialog_arguments,
        )
    )
    telegram_task = asyncio.create_task(
        telegram_client.ask_question(
            question,
            context,
            prompt_id=prompt_id,
            timeout_seconds=timeout_seconds,
            include_timing_info=show_timing_info,
            issued_at=issued_at,
        )
    )

    pending_tasks: dict[asyncio.Task[Optional[str]], str] = {
        dialog_task: "dialog",
        telegram_task: "telegram",
    }
    first_error: Optional[UserPromptError] = None

    while pending_tasks:
        done, _ = await asyncio.wait(pending_tasks.keys(), return_when=asyncio.FIRST_COMPLETED)
        completed_results: list[tuple[str, str]] = []
        telegram_error: Optional[UserPromptError] = None

        for task in done:
            channel_name = pending_tasks.pop(task)
            try:
                result = await task
            except TelegramPromptError as exc:
                error = UserPromptError(f"Telegram prompt failed: {exc}")
                if channel_name == "telegram":
                    telegram_error = error
                elif first_error is None:
                    first_error = error
                continue
            except UserPromptError as exc:
                if first_error is None:
                    first_error = exc
                continue
            except asyncio.CancelledError:
                continue

            if result is None:
                continue

            completed_results.append((channel_name, result))

        if completed_results:
            channel_name, result = completed_results[0]
            if channel_name == "telegram":
                await _finish_dialog_after_telegram_done(dialog_task, cancel_event)
            else:
                await _cancel_telegram_task(telegram_task)

            return channel_name, result

        if telegram_error is not None:
            await _finish_dialog_after_telegram_done(dialog_task, cancel_event)
            raise telegram_error

    if first_error is not None:
        raise first_error

    return None


async def get_user_input_from_configured_channel(
    question: str,
    context: str,
    dialog_prompt: str,
    timeout_seconds: int,
    prompt_id: str,
    issued_at: dt.datetime,
    macos_prompt_document: Optional[dict[str, Any]] = None,
) -> Optional[tuple[str, str]]:
    """Use the currently configured response channel(s) to collect user input."""
    if response_channel == "dialog":
        dialog_arguments: dict[str, Any] = {}
        if macos_prompt_document is not None:
            dialog_arguments["macos_prompt_document"] = macos_prompt_document
        result = await dialog_handler.get_user_input(
            dialog_prompt,
            timeout_seconds,
            **dialog_arguments,
        )
        return None if result is None else ("dialog", result)

    if telegram_client is None:
        raise UserPromptError(
            "Telegram response channel is enabled, but no Telegram target was configured."
        )

    if response_channel == "telegram":
        try:
            result = await telegram_client.ask_question(
                question,
                context,
                prompt_id=prompt_id,
                timeout_seconds=timeout_seconds,
                include_timing_info=show_timing_info,
                issued_at=issued_at,
            )
            return None if result is None else ("telegram", result)
        except TelegramPromptError as exc:
            raise UserPromptError(f"Telegram prompt failed: {exc}") from exc

    return await _get_first_channel_response(
        question,
        context,
        dialog_prompt,
        timeout_seconds,
        prompt_id,
        issued_at,
        macos_prompt_document,
    )


@mcp.tool()
async def ask_human(question: str, context: str = "") -> str:
    """Talk to the human without ending the current turn.

    Use this as a collaboration tool: ask a question, clarify intent or preferences,
    request a decision, coordinate an action, or work through something interactively.

    When missing information or an unconfirmed assumption could affect the outcome, always use
    this tool before proceeding. Keep each call focused and include enough context and tradeoffs for
    an informed answer.

    Use normal multiline Markdown and preserve paragraph and list breaks.

    Args:
        question: The question or request shown to the human
        context: Context or additional information shown before the question
            (max 8000 characters total across question and context)

    Returns:
        The human's response with a status prefix, or an error description.
    """

    timeout_seconds = dialog_timeout_seconds

    # Parameter validation with clear error messages
    if not question:
        return "❌ Error: 'question' parameter is required and must be a non-empty string"

    if len(question.strip()) == 0:
        return "❌ Error: 'question' cannot be empty or only whitespace"

    if len(question) + len(context) > MAX_PROMPT_CHARACTERS:
        return (
            "❌ Error: prompt is too long "
            f"(max {MAX_PROMPT_CHARACTERS} characters total across question and context). "
            "Please shorten it."
        )

    if not isinstance(timeout_seconds, int):
        return "❌ Error: 'timeout_seconds' must be an integer"

    if timeout_seconds < 1:
        return "❌ Error: 'timeout_seconds' must be at least 1 second"

    try:
        issued_at = dt.datetime.now().astimezone()
        prompt_id = generate_prompt_id(issued_at)
        telegram_notice = ""
        if response_channel == "both":
            telegram_notice = build_dialog_telegram_notice(dialog_handler.platform)

        dialog_prompt = build_prompt_text(
            question,
            context,
            timeout_seconds=timeout_seconds,
            include_timing_info=show_timing_info,
            extra_note=telegram_notice,
            issued_at=issued_at,
        )
        macos_prompt_document = None
        if getattr(dialog_handler, "platform", None) == "Darwin" and response_channel in {
            "dialog",
            "both",
        }:
            try:
                macos_prompt_document = build_macos_prompt_document(
                    question,
                    context,
                    fallback_text=dialog_prompt,
                    timeout_seconds=timeout_seconds,
                    include_timing_info=show_timing_info,
                    extra_note=telegram_notice,
                    issued_at=issued_at,
                )
            except Exception:
                # Markdown presentation is optional; the complete plain prompt must
                # still reach the dialog (and must not block a Telegram race).
                macos_prompt_document = build_plain_macos_prompt_document(dialog_prompt)
        # Get user input via the configured channel(s)
        channel_response = await get_user_input_from_configured_channel(
            question,
            context,
            dialog_prompt,
            timeout_seconds,
            prompt_id,
            issued_at,
            macos_prompt_document,
        )

        # Handle different response scenarios with custom exceptions
        if channel_response is None:
            # Timeout occurred
            timeout_minutes = timeout_seconds // 60
            timeout_display = f"{timeout_minutes} minute{'s' if timeout_minutes != 1 else ''}"
            raise UserPromptTimeout(f"No response received within {timeout_display}")

        response_channel_name, response = channel_response
        if not response.strip():
            # Empty response (user clicked OK without entering text)
            return "⚠️ Empty response received. The user clicked OK but didn't enter any text. Please ask again if a response is needed."

        # Successful response
        clean_response = response.strip()

        # Format response with clear indicator
        if response_channel_name == "telegram":
            return f"✅ Replied via Telegram:\n{clean_response}"
        return f"✅ User reply:\n{clean_response}"

    except UserPromptTimeout as e:
        # Handle timeout with user-friendly message
        return f"⚠️ Timeout: {str(e)}. The dialog may have timed out or been cancelled. Please try asking again if needed."

    except UserPromptCancelled as e:
        # Handle cancellation
        return f"⚠️ Cancelled: {str(e)}. The user cancelled the prompt. Please try again or rephrase your question."

    except KeyboardInterrupt:
        # Handle Ctrl+C gracefully without crashing the server
        raise UserPromptCancelled("User interrupted the prompt with Ctrl+C")

    except UserPromptError as e:
        # Handle custom user prompt errors
        return f"❌ User Prompt Error: {str(e)}"

    except Exception as e:
        # Comprehensive error handling with helpful context
        error_context = f"Question: {question[:100]}{'...' if len(question) > 100 else ''}"
        return f"❌ Error getting user input: {str(e)}\n\nContext: {error_context}\n\nThe GUI dialog system may not be available. Check that the required dependencies are installed (zenity on Linux, osascript on macOS, tkinter on Windows)."


def create_starlette_app(mcp_server: Server, *, debug: bool = False) -> Starlette:
    """Create a Starlette application that can serve the provided MCP server with SSE.

    Sets up a Starlette web application with routes for SSE (Server-Sent Events)
    communication with the MCP server.

    Args:
        mcp_server: The MCP server instance to connect
        debug: Whether to enable debug mode for the Starlette app

    Returns:
        A configured Starlette application
    """
    # Create an SSE transport with a base path for messages
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> None:
        """Handler for SSE connections.

        Establishes an SSE connection and connects it to the MCP server.

        Args:
            request: The incoming HTTP request
        """
        # Connect the SSE transport to the request
        # `connect_sse` is decorated with `@asynccontextmanager` upstream, but it is not
        # annotated that way in the installed `mcp` package, so Pyright/Pylance sees the
        # raw generator type. Cast it to the actual async context-manager protocol here.
        sse_connection = cast(
            AbstractAsyncContextManager[tuple[Any, Any]],
            sse.connect_sse(
                request.scope,
                request.receive,
                request._send,  # noqa: SLF001
            ),
        )
        async with sse_connection as (read_stream, write_stream):
            # Run the MCP server with the SSE streams
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )

    # Create and return the Starlette application with routes
    return Starlette(
        debug=debug,
        routes=[
            Route("/sse", endpoint=handle_sse),  # Endpoint for SSE connections
            Mount("/messages/", app=sse.handle_post_message),  # Endpoint for posting messages
        ],
    )


def main() -> None:
    """Main entry point for the User Prompt MCP server.

    This function serves as the primary entry point when the server is launched
    via uvx or direct Python execution. It handles argument parsing and server startup.
    """
    # Get the underlying MCP server from the FastMCP instance
    mcp_server = mcp._mcp_server  # noqa: WPS437

    import argparse

    # Set up command-line argument parsing
    parser = argparse.ArgumentParser(description="Run Ask Human MCP server")
    # Allow choosing between stdio and SSE transport modes
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport mode (stdio or sse)",
    )
    # Host configuration for SSE mode
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (for SSE mode)")
    # Port configuration for SSE mode
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (for SSE mode)")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_DIALOG_TIMEOUT_SECONDS,
        help=(
            "Dialog response timeout in seconds. Defaults to "
            f"{DEFAULT_DIALOG_TIMEOUT_SECONDS} seconds."
        ),
    )
    parser.add_argument(
        "--dialog-title",
        default=None,
        help="Dialog window title. Defaults to the built-in title.",
    )
    parser.add_argument(
        "--show-timing-info",
        action="store_true",
        help=(
            "Add Issued at / Answer until lines to dialog text. The MCP client may time "
            "out sooner than the dialog itself."
        ),
    )
    parser.add_argument(
        "--response-channel",
        choices=["dialog", "telegram", "both"],
        default=DEFAULT_RESPONSE_CHANNEL,
        help="Where to collect the reply: dialog, telegram, or both.",
    )
    parser.add_argument(
        "--telegram",
        default=None,
        help=(
            "Telegram bot target as a single argument: '<bot_token> <chat_id>'. "
            "Required for --response-channel telegram or both."
        ),
    )
    parser.add_argument(
        "--telegram-download-dir",
        default=None,
        help=(
            "Optional local directory for downloaded Telegram reply files. Defaults to a folder "
            "under the system temp directory. Supports ~, environment variables, and {cwd}."
        ),
    )
    parser.add_argument(
        "--telegram-broker-root-state-dir",
        default=None,
        help=(
            "Optional root state directory used by local auto-started Telegram brokers. "
            "Defaults to a platform-specific per-user state location. Supports ~, "
            "environment variables, and {cwd}."
        ),
    )
    parser.add_argument(
        "--telegram-broker",
        action="store_true",
        help="Run the local Telegram broker service instead of the MCP server.",
    )
    parser.add_argument(
        "--telegram-broker-label",
        default=None,
        help="Optional human-friendly label for the Telegram broker instance.",
    )
    parser.add_argument(
        "--telegram-broker-state-dir",
        default=None,
        help=(
            "Optional persistent state directory for the Telegram broker. Defaults to a "
            "platform-specific per-user state location. Supports ~, environment variables, "
            "and {cwd}."
        ),
    )
    parser.add_argument(
        "--telegram-debug-log",
        default=None,
        help=(
            "Optional JSONL file for Telegram timing diagnostics. Can also be set with "
            "ASK_HUMAN_TELEGRAM_DEBUG_LOG. Does not log bot tokens, chat ids, prompt text, "
            "or reply text. Supports ~, environment variables, and {cwd}."
        ),
    )
    parser.add_argument(
        "--telegram-broker-host",
        default="127.0.0.1",
        help="Host to bind to when running in --telegram-broker mode.",
    )
    parser.add_argument(
        "--telegram-broker-port",
        type=int,
        default=0,
        help=(
            "Port to bind to when running in --telegram-broker mode. Defaults to 0 so the OS "
            "assigns a free port."
        ),
    )
    args = parser.parse_args()

    if args.telegram_broker:
        try:
            telegram_target = parse_telegram_target(args.telegram)
        except ValueError as exc:
            parser.error(str(exc))

        if telegram_target is None:
            parser.error("--telegram is required when --telegram-broker is used.")
        if args.telegram_broker_port < 0 or args.telegram_broker_port > 65535:
            parser.error("--telegram-broker-port must be between 0 and 65535.")

        broker_state_root = resolve_broker_state_dir(args.telegram_broker_state_dir)
        broker_state_dir = resolve_target_broker_state_dir(broker_state_root, telegram_target)
        run_telegram_broker(
            host=args.telegram_broker_host,
            port=args.telegram_broker_port,
            state_dir=broker_state_dir,
            telegram_target=telegram_target,
            broker_label=args.telegram_broker_label,
            debug_log_path=args.telegram_debug_log,
        )
        return

    try:
        telegram_target = parse_telegram_target(args.telegram)
    except ValueError as exc:
        parser.error(str(exc))

    if args.response_channel != "dialog" and telegram_target is None:
        parser.error("--telegram is required when --response-channel is telegram or both.")

    telegram_download_dir = resolve_telegram_download_dir(args.telegram_download_dir)
    if telegram_target is not None:
        try:
            telegram_download_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            parser.error(f"Could not create telegram download directory: {exc}")

    global dialog_handler
    global dialog_timeout_seconds
    initialize_time_locale()
    global show_timing_info
    global response_channel
    global telegram_client
    dialog_handler = GUIDialogHandler(dialog_title=args.dialog_title)
    dialog_timeout_seconds = args.timeout_seconds
    show_timing_info = args.show_timing_info
    response_channel = args.response_channel
    telegram_client = (
        TelegramBrokerClient(
            telegram_target,
            telegram_download_dir,
            broker_state_root=resolve_broker_state_dir(args.telegram_broker_root_state_dir),
            broker_label=args.telegram_broker_label,
            debug_log_path=args.telegram_debug_log,
        )
        if telegram_target is not None
        else None
    )

    # Launch the server with the selected transport mode
    if args.transport == "stdio":
        # Run with stdio transport (default)
        # This mode communicates through standard input/output
        mcp.run(transport="stdio")
    else:
        # Run with SSE transport (web-based)
        # Create a Starlette app to serve the MCP server
        starlette_app = create_starlette_app(mcp_server, debug=True)
        # Start the web server with the configured host and port
        uvicorn.run(starlette_app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
