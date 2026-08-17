# Ask Human MCP

<!-- mcp-name: io.github.alexchexes/ask-human -->

Simple [MCP](https://modelcontextprotocol.io/) server that lets AI agents ask humans for input and wait for an answer.

Supports Telegram (including files) and local OS dialogs.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)

It gives MCP-capable agents a focused tool for cases where guessing is the wrong move. The agent can pause, show the question and relevant context, wait for your answer, then continue the same workflow.

<!-- TOC depthfrom:2 depthto:2 -->

- [Why](#why)
- [Features](#features)
- [Installation](#installation)
- [MCP client setup](#mcp-client-setup)
- [AGENTS.md instructions](#agentsmd-instructions)
- [Configuration](#configuration)
- [Tool reference](#tool-reference)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Security and Privacy](#security-and-privacy)
- [License](#license)

<!-- /TOC -->

## Why

Despite agents improving every month, they still tend to make assumptions and produce "perfectly working nonsense" when all they needed to do was pause and ask the human who issued the task for clarification.

Codex (as of May 2026) even has this in its system instructions:

> ... strongly prefer making **reasonable assumptions** and executing the user's request **rather than stopping to ask questions**.

In general, agents often hit decisions that are not knowable from the repository or local environment:

- product or design preferences
- risky implementation tradeoffs
- missing credentials, deployment constraints, or domain rules
- ambiguous requirements that should not be guessed
- offline or real-world context that only the human can provide

_Ask Human_ exposes the `ask_human` MCP tool, so the agent can ask directly instead of silently making assumptions.

Another useful application is enabling an "interactive mode" when an agent needs to ask several follow-up questions while working on a task. You prompt your agent:

> \- Using the `ask_human` tool, guide me step by step through BIOS debug on my other laptop. I will send you photos of the screen after each step

And Codex (or your agent of choice) will do just that, because it now has a well-suited tool for that workflow.

## Features

- Native local dialogs on macOS, Linux, and Windows
- Optional Telegram response channel for mobile/away-from-keyboard replies with support for files and other media (up to 20 MB)
- Configurable timeouts. When the MCP tool is called, the agent waits for your reply for as long as the client allows. Tested up to 24h with Codex.

## Installation

### uvx (no installation; recommended)

`uvx` is the recommended way to run Ask Human from an MCP client. It does not require a prior install: it downloads the `ask-human` package on first invocation, caches it, and runs it in an isolated environment.

Any MCP client that can launch a stdio command can use it that way. Example MCP configuration shape:

```text
command: uvx
args: ask-human --transport stdio
```

See [MCP Client Setup](#mcp-client-setup) for exact setup instructions with `uvx` for popular clients.

> To manually run the CLI directly:
>
> ```bash
> uvx ask-human --help
> ```

### Persistent Install via `pipx` or `pip`

If you prefer a persistent CLI install instead of `uvx`, you can use `pipx` or `pip install --user`:

```bash
pipx install ask-human
```

or `pip`:

```bash
python -m pip install --user ask-human
```

For the `pip` path, **make sure your Python user scripts directory is on `PATH`.**

After a persistent install, MCP clients can run the installed executable directly. The MCP config shape is:

```text
command: ask-human
args: --transport stdio
```

### Updating

If your MCP config uses `uvx` for `ask-human`, there is no persistent install to upgrade. `uvx` resolves the package when it runs and reuses a cached environment; to force that cache to refresh to the latest published version, run:

```bash
uvx --upgrade ask-human --help
```

If you installed Ask Human persistently with `pipx`, run:

```bash
pipx upgrade ask-human
```

If you installed it with `pip --user`, run:

```bash
python -m pip install --upgrade --user ask-human
```

After updating, restart any active MCP client sessions so they launch the new Ask Human process.

## MCP client setup

Codex, Claude Code, Cursor, and other MCP-capable agent clients can use Ask Human by adding the MCP server to their config.

See [Command args reference](#command-args-reference).

> **NOTE**: Examples below use `ask-human` consistently as the package name, executable name, and MCP server name. Keeping those names aligned is intentional and recommended, though the MCP server name is configurable in your client.

> **NOTE**: It is also recommended to increase your client's MCP tool-call timeout as much as practical, so you avoid a situation where the agent asks something important, the MCP call times out, and the agent goes back to assumptions / inferring.

### Codex

> Codex MCP docs: <https://developers.openai.com/codex/mcp>

Add MCP config to your `~/.codex/config.toml`:

```toml
[mcp_servers.ask-human]
command = "uvx"
args = [
  "ask-human",
  "--transport", "stdio",

  "--timeout-seconds", "86400", # human reply window; 86400 = 24 h

  # configure Telegram if needed:
  # "--response-channel", "both", # dialog | telegram | both
  # "--telegram", "<bot_token> <chat_id>", # creds for your personal tg bot used ONLY ON THIS MACHINE

  # Customize OS dialogue title
  # "--dialog-title", "Codex asks..."

  # Show remaining time to answer
  # "--show-timing-info",
]

tool_timeout_sec = 86460 # client timeout. Should be larger than the reply window

# Work around sometimes-broken Codex thread `cwd`, or a macOS VS Code permissions problem
# cwd = "C:\\" # Windows
# cwd = "/" # macOS/Linux

# Prevent Codex startup if Ask Human cannot initialize
required = true

# For Codex CLI v0.142+ / VS Code extension v26.616.30709+ - prevent this MCP
# from running through "code_mode exec" mode
[features.code_mode]
direct_only_tool_namespaces = ["mcp__ask_human"]
```

Restart Codex after changing the config. In VS Code, use "Reload Window" or "Restart Extension Host".

### Claude Code

> Claude Code MCP docs: <https://code.claude.com/docs/en/mcp>

Add to `~/.claude.json`:

```jsonc
{
  // your existing config...
  "mcpServers": {
    // your existing MCP servers...
    // ...
    "ask-human": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "ask-human",
        "--transport", "stdio",
        "--timeout-seconds", "86400",
        "--show-timing-info",
        "--dialog-title", "Claude asks...",

        // Set up Telegram
        // "--telegram", "<bot_token> <chat_id>",
        // "--response-channel", "both" // or "telegram" only.
      ],
      "timeout": 86460000
    }
  }
}
```

`timeout` is Claude's per-server tool-call timeout in milliseconds. Its value should be slightly larger than `--timeout-seconds`, which uses seconds. The example above configures a 24-hour reply window and a client timeout one minute longer.

Additionally, [Claude now automatically moves long MCP calls to the background](https://code.claude.com/docs/en/mcp#automatic-backgrounding-of-long-tool-calls), which allows it to continue before your reply arrives. To make Claude keep waiting for MCP calls until they return or reach their configured timeout, add this to `~/.claude/settings.json`:

```json
{
  "env": {
    "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS": "0"
  }
}
```

Note that this disables automatic backgrounding of any MCP calls, not just `ask-human`.

### Cursor

> Cursor MCP docs: <https://cursor.com/docs/mcp>

Example for `~/.cursor/mcp.json`:

```jsonc
{
  "mcpServers": {
    "ask-human": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "ask-human",
        "--transport", "stdio",
        "--timeout-seconds", "86400",
        "--show-timing-info",
        "--dialog-title", "Agent asks...",

        // Set up Telegram
        // "--telegram", "<bot_token> <chat_id>",
        // "--response-channel", "both" // or "telegram" only.
      ]
    }
  }
}
```

Cursor's current MCP documentation does not document a per-server tool timeout.

## AGENTS.md instructions

To make an agent prefer asking questions via this tool instead of making assumptions, add an instruction to your global or workspace `AGENTS.md` file, such as:

```md
If a required fact or preference cannot be discovered locally and a wrong
assumption could affect correctness, safety, architecture, or user intent, use
the `ask_human` tool before proceeding.
```

<details>

<summary><b>Battle-tested AGENTS.md instruction example</b></summary>

```md
## Ask human tool

If a missing fact, design choice, or user preference is not 100% clear, and a wrong
assumption could materially affect correctness, safety, architecture, or user intent,
use the `ask_human` MCP/tool before proceeding.

If the tool is unavailable or times out without a human response, do not proceed
and do not roll back changes unless it is absolutely necessary (e.g. a broken
live/production system, runaway resource consumption, etc.). Instead, stop, report
the current state, repeat the context and question, and let the user answer normally.

If you run `ask_human` through a wrapper that yields intermediate results while waiting,
never terminate the call until the tool returns a user response or an error, the user
explicitly cancels it, or its configured timeout expires.

Do not optimize for completing the task in one uninterrupted run if clarification
would lead to a better decision. Making correct design decisions is more important
than finishing a subtask without interruptions.

Use `ask_human` especially for ambiguous requirements, risky tradeoffs, irreversible
actions, external side effects, and situations where multiple reasonable approaches
exist and the preferred one is not 100% clear. Keep the question concise where possible,
but include necessary context details
so the user is properly informed.

When a task requires many decisions from the user, or when the user explicitly asks
you to ask questions or use `ask_human`, do not limit that to the initial planning phase.
Continue talking with the user via that tool during implementation whenever a new assumption,
design choice, external value, or behavior decision appears that was not already answered.
Do not treat early answers as broad permission to infer the remaining details silently.

## Contradictions and questionable requests

If the user's request appears to contradict earlier instructions, previous work,
the current state, or a known constraint, do not silently choose one interpretation.
Briefly explain the conflict using `ask_human`.

If the request seems technically wrong, unsafe, or likely to cause unintended consequences,
always use `ask_human` to confirm that the user really means it before proceeding.

A common source of confusion: the user may think they're on one branch or workspace
when they're actually on another.
```

</details>

Even a carefully written `AGENTS.md` can still hit intrinsic agent limitations: system instructions may override it, or the agent may have learned to provide a "complete solution" instead of asking questions. Whatever the reason, the agent may sometimes ignore the instruction to use this tool in the intended scenarios (true at least for Codex as of May 2026).

To increase the chance that the agent asks before making a wrong assumption, add a reminder like this directly to your prompt when setting a task:

```
...<your normal prompt>...

P.S. Remember to use the ask_human tool whenever you hit any ambiguity, uncertainty,
non-obvious implications, something that is not 100% explicitly agreed, or anything
else that requires or might require my input. Never infer or make assumptions
(even "conservative" ones) in such cases; use the `ask_human` tool instead (or stop if
the tool is unavailable or does not return usable output).
```

## Configuration

### Telegram as a response channel

To make your agent message you via Telegram when it needs your input, add:

```sh
--telegram "<bot_token> <chat_id>" --response-channel telegram # or "--response-channel both"
```

to the MCP config args list.

When you receive an agent prompt, you can respond with text, a photo, another media/file attachment (up to 20 MB), location, etc. Voice auto-transcription is not supported yet. A capable agent will be able to inspect supported files/media. Files are saved to a temporary directory; see [Telegram file download directory](#telegram-file-download-directory).

Telegram prompts render common agent Markdown, such as bold, italic, inline code, fenced code blocks, links, headings, quotes, and lists, through Telegram-supported HTML. The prompt metadata remains in an expandable Telegram quote block. If Telegram rejects the formatting, the same prompt is retried as plain text so the message is still delivered.

<details>
<summary>

#### How to create a Telegram bot and obtain a chat ID

</summary>

1. Open Telegram and message `@BotFather`.
2. Run `/newbot` and follow BotFather's prompts.
3. Copy the bot token.
4. Send any message to your new bot.
5. Open this URL in a browser, replacing `<BOT_TOKEN>`:

   ```text
   https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
   ```

6. Find `message.chat.id` in the JSON response. That is the `<chat_id>`.
7. Keep the bot token secret. Anyone with the token can control the bot.

For a group chat, add the bot to the group, send a message in the group, then call `getUpdates` and use the group's `chat.id`.

</details>

See [Telegram bot icons](https://github.com/alexchexes/ask-human/tree/main/src/ask_human/assets/telegram).

**Important:** If you run agents on different machines or inside different VMs, you must use a **different Telegram bot token for each machine/environment**. That limitation is due to how Telegram's `getUpdates` mechanism works. Using the same bot for different environments may be buggy and unreliable.

<details>

<summary>

#### How Telegram broker works

</summary>

Telegram delivery uses a local auto-started broker process instead of letting each agent session poll `getUpdates` independently.

Current behavior:

- one local broker is created per Telegram target (`bot_token + chat_id`)
- sessions on the same machine that use the same target reuse that broker
- different Telegram targets on the same machine use different brokers
- broker discovery uses persisted local state plus a health check
- the broker binds to `127.0.0.1` on an OS-assigned free port by default

This makes same-machine concurrent Telegram prompts safe.

Package updates are handled conservatively:

- sessions already running the current package version keep reusing the broker normally
- a newer session updates an older broker automatically only when that broker supports guarded shutdown and has no active prompts
- if the broker still has active prompts, the new question is not sent and nothing is cancelled; the tool tells the agent to ask the user to finish those prompts and retry
- a session older than the broker never downgrades it; the tool tells the agent to ask the user to reload or restart that session, then retry
- a legacy broker that cannot prove replacement is safe is left untouched and requires the one-time manual restart below

Current limitation:

- cross-machine or shared-server coordination is not implemented yet
- if two different machines use the same bot target at the same time, replies can still be consumed by the wrong machine

Telegram prompt metadata includes:

- `Prompt ID: ...`
- `Broker: <label> [<id>]`

That helps identify which local broker instance sent a prompt and makes some cross-machine mix-ups easier to diagnose.

Advanced/manual broker mode is mainly for debugging and future remote deployment:

```bash
ask-human --telegram-broker --telegram "<bot_token> <chat_id>"
```

To replace a legacy broker after an ask-human update, first finish every pending Ask Human prompt. Then find and stop only the confirmed broker process. Pending prompts cannot be recovered if their broker is stopped.

Windows (PowerShell):

```powershell
Get-CimInstance Win32_Process |
  Where-Object {
    $_.Name -like 'python*' -and
    $_.CommandLine -like '*ask_human*' -and
    $_.CommandLine -like '*--telegram-broker*'
  } |
  Select-Object ProcessId, CommandLine

Stop-Process -Id 12345  # replace with the confirmed broker PID
```

macOS or Linux:

```bash
pgrep -fl -- '--telegram-broker'
kill 12345  # replace with the confirmed broker PID
```

Reload or restart any session that still has the previous ask-human version loaded. The next Telegram prompt then auto-starts a fresh local broker if one is needed.

In `both` mode:

- macOS and Linux try to close the local dialog when the Telegram reply arrives first
- Windows keeps the current Tk dialog behavior; if Telegram wins first, the local dialog may stay open and any later answer there will be ignored

Telegram reply behavior:

- use Telegram's Reply feature on the bot's question message
- if you select part of the bot message before replying, that selected quote is included in the agent-facing response
- formatting in regular text replies, attachment captions, and selected quotes is restored on a best-effort basis as readable Markdown-like notation for the agent; underline uses `<u>...</u>` and spoilers use `<telegram-spoiler>...</telegram-spoiler>`
- long text replies that Telegram splits into multiple reply messages are recombined when the split parts still reply to the same bot message
- albums/media groups are combined into one agent-facing response
- short bursts of ungrouped file/media replies are combined too, so multiple screenshots or documents can be sent even when Telegram's `Group items` option is disabled
- for many or delayed attachments, reply with `/files_start`, send the items as normal Telegram messages, then send `/files_finish`; use `/files_cancel` to discard the collected items and keep the prompt waiting
- if a local broker is actively waiting and you send a non-reply message, it sends a short warning that the message is ignored and you must use Reply
- if you reply to a message that is not the currently active question, it sends a warning instead of silently consuming the reply
- if you reply to one of this broker's own older inactive prompt messages, it sends a short warning that the old question is no longer active
- successful replies get a `Received [Prompt ID]` acknowledgement
- supported replies include text, files/media messages up to 20 MB each, location, venue, and contact
- files are downloaded locally and returned to the agent as local paths
- if one item in an attachment group is unsupported or too large, the whole group is rejected and the prompt keeps waiting for a valid reply
- replies that appear intended for another broker instance trigger a warning instead of being silently misrouted
- transient Telegram polling transport failures and HTTP 5xx responses are retried briefly with backoff; persistent polling failures are returned to the agent as prompt errors
- Telegram delivery failures for the initial question or retry/warning messages are returned to the agent as prompt errors

Replies created with Telegram's Premium Rich Text Editor are not supported yet. The bot asks you to
retry with regular text or a supported attachment.

</details>

#### Bot commands

Optionally, add Telegram bot command menu: in `@BotFather`, run `/setcommands`, pick your bot, and send:

```text
files_start - Start collecting files/messages
files_finish - Send the collected files/messages
files_cancel - Cancel collecting files/messages
```

### Command args reference

#### Transports

STDIO is the default and is what most local MCP clients use:

```bash
ask-human --transport stdio
```

SSE is available for clients that connect over HTTP:

```bash
ask-human --transport sse --host 0.0.0.0 --port 8080
```

#### Timeout

The internal timeout that affects OS dialog and Telegram prompts. Defaults to 3600 seconds.

```bash
ask-human --transport stdio --timeout-seconds 1200
```

MCP clients may enforce their own tool-call timeout. If your client supports a tool timeout setting, set it above `--timeout-seconds`; otherwise the client may stop waiting before Ask Human does.

| Desired reply window | `--timeout-seconds` | MCP tool timeout |
| --- | ---: | ---: |
| 5 min | `300` | `360s` / `360000ms` |
| 20 min | `1200` | `1260s` / `1260000ms` |
| 1 h | `3600` | `3660s` / `3660000ms` |
| 24 h | `86400` | `86460s` / `86460000ms` |

For short timeouts, such as 2 minutes, Telegram connection problems can make outage errors race with the normal timeout.

#### Response channels

Use `--response-channel` to choose where replies are collected:

- `dialog`: local native dialog only, default
- `telegram`: Telegram only
- `both`: local dialog and Telegram at the same time; first reply wins

```bash
ask-human --transport stdio --response-channel telegram --telegram "<bot_token> <chat_id>"
```

#### Telegram file download directory:

```bash
--telegram-download-dir "~/Downloads/ask-human"
```

Defaults to a folder under the system temp directory. Supports `~`, environment variables such as `%USERPROFILE%`, and `{cwd}`.

#### Timing metadata

Use `--show-timing-info` to include compact timing metadata showing when the prompt was issued and when the tool will time out:

```bash
ask-human --transport stdio --show-timing-info
```

#### OS dialog title

The default dialog title is `Agent asks...`.

```bash
ask-human --transport stdio --dialog-title "Codex Needs Input"
```

## Tool reference

### `ask_human`

Talk to the human without ending the current turn.

Parameters:

- `question` (string, required): specific question or request
- `context` (string, optional): background shown before the question

`question` and `context` may contain up to 8000 characters combined. Long Telegram prompts are split across messages automatically; Windows dialogs wrap long lines on a best-effort basis but are not scrollable yet.

Returns one of:

- `User reply: ...` when the user answers through a local dialog
- `Replied via Telegram: ...` when the user answers through Telegram
- `Empty response received` when the user clicks OK without text
- `Timeout: ...` when no response arrives in time
- `Cancelled: ...` when the user cancels
- `Error: ...` for validation or system failures

Example tool call:

```python
ask_human(
    question="Should this import overwrite an existing session or stop?",
    context="Both behaviors are possible, but choosing wrong could lose user data."
)
```

## Development

Clone the repo:

```bash
git clone https://github.com/alexchexes/ask-human.git
cd ask-human
```

Make sure you have [uv](https://docs.astral.sh/uv/) `>=0.11.25, <0.12`. Check with `uv --version` and install a supported version if needed:

- Windows:
  ```powershell
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/0.11.25/install.ps1 | iex"
  ```

- macOS/Linux:
  ```bash
  curl -LsSf https://astral.sh/uv/0.11.25/install.sh | sh
  ```

Create `.venv` and install the project with its dependencies:

```bash
uv sync --locked --all-extras
```

Configure your MCP client to run from the local checkout. To do that, follow [MCP client setup](#mcp-client-setup), then:

- Replace the `command` value with the absolute path to the `.venv` Python executable:
  - Windows: `D:\PATH\TO\ask-human\.venv\Scripts\python.exe`
  - macOS/Linux: `/PATH/TO/ask-human/.venv/bin/python`
- Replace the first argument (`"ask-human"`) with `"-m", "ask_human"`.

For example:

```toml
# ~/.codex/config.toml

[mcp_servers.ask-human]
command = "D:\\PATH\\TO\\ask-human\\.venv\\Scripts\\python.exe" # macOS/Linux: .../.venv/bin/python
args = [
  "-m", "ask_human",
  "--transport", "stdio",
  # ...the remaining args...
]
```

### Local dialog development

The `dialog` and `both` response channels use AppKit through `osascript` on macOS and `tkinter` on Windows; these normally require no separate installation. Linux uses `zenity`, which requires a graphical desktop session and may need to be installed:

```bash
sudo apt install zenity     # Debian/Ubuntu
sudo dnf install zenity     # Fedora
sudo pacman -S zenity       # Arch
```

These platform tools are not needed for Telegram-only development, checks, or builds.

> Native macOS dialog behavior is tested locally on macOS 26.6 and exercised in CI by GitHub's `macos-latest` runner. Older macOS releases were not tested yet.

When changing Telegram broker/client code during local development, stop any running local Telegram broker before retesting. Otherwise the detached broker may keep running old code from before your edit. See [How Telegram broker works](#how-telegram-broker-works) for the guarded update behavior and manual stop commands.

Run checks:

```bash
uv run --locked black --check .
uv run --locked isort --check-only .
uv run --locked mypy src
uv run --locked pyright
uv run --locked pytest
```

Build locally:

```bash
uv run --locked python -m build
uv run --locked python -m twine check dist/*
```

To update a dependency intentionally, edit its reviewed bounds in `pyproject.toml`, then run
`uv lock --upgrade-package <package>`.

## Troubleshooting

### Telegram delays

If Telegram messages appear delayed, that may be a Telegram-side issue; see [[1]](https://github.com/openclaw/openclaw/issues/98773), [[2]](https://github.com/openclaw/openclaw/issues/91456), [[3]](https://github.com/NousResearch/hermes-agent/issues/58236), [[4]](https://statusgator.com/services/telegram).

You can enable Telegram debug logging either by adding a CLI argument to your MCP config:

```bash
--telegram-debug-log "~/.ask-human/telegram-debug.jsonl"
```

or by launching the broker manually with `ASK_HUMAN_TELEGRAM_DEBUG_LOG=/path/to/ask-human-telegram-debug.jsonl`. The debug log omits bot tokens, chat IDs, prompt text, and reply text.

## Security and Privacy

- Local dialog prompts stay on your machine.
- Telegram prompts and replies go through Telegram when that channel is enabled.
- Telegram files are downloaded to a local directory and returned as paths.
- Bot tokens should be treated as secrets.
- Ask Human does not run a remote server by default.

## License

MIT License. See [LICENSE](https://github.com/alexchexes/ask-human/blob/main/LICENSE).

Based on [galprz/ask-human-for-context](https://github.com/galprz/ask-human-for-context).
