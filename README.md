<p align="center"><img src="https://raw.githubusercontent.com/Alyxion/yikes/main/media/logo-small.png" alt="yikes!" width="400"></p>

# yikes!

[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/Alyxion/yikes/blob/main/LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.0-blueviolet.svg)](https://github.com/Alyxion/yikes)

### A terminal-first runtime for Claude Code and Codex CLI.

yikes! gives Python apps, terminal users, and web interfaces one consistent way to start, control, inspect, and overtake Claude Code and Codex sessions.

It is built for the cases where a simple one-shot prompt is not enough: persistent sessions, real interactive tmux control, Docker isolation, MCP attachment, readable/writable directory policy, and a clean Python surface for embedding the same runtime elsewhere.

---

## What you get

- **Full terminal app by default** - running `yikes` opens the interactive control surface.
- **Claude Code and Codex support** - switch backend, model, complexity, web search, and runtime through slash commands shared with Python/web callers.
- **Host and Docker runtimes** - run locally or inside an isolated container.
- **Real tmux sessions** - attach to long-running work, reconnect from the top session tabs, and replay recent terminal output.
- **Image attachments** - use Ctrl+V for smart paste, drag image file paths into the terminal app, or use Ctrl+O for image-only paste.
- **MCP and directory policy** - configure tool servers plus readable and writable directories.
- **Python-first core** - the terminal app, web UI, and CLI share the same controller and helper classes.

---

## Quick start

Requires Python 3.14 or newer.

```bash
git clone https://github.com/Alyxion/yikes.git
cd yikes
python -m pip install -e .
yikes
```

yikes! expects the backend CLIs you want to use to be installed and logged in for your user:

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- [Codex CLI](https://github.com/openai/codex)

---

## Common commands

```bash
yikes                         # open the terminal app
yikes tui --backend claude    # start with Claude Code
yikes tui --backend codex     # start with Codex CLI
yikes sessions                # list durable sessions
yikes attach <session-id>     # overtake an attachable session
yikes close <session-id>      # close one session
```

For automation, named tmux sessions are addressable without remembering the generated yikes! session ID:

```bash
yikes tmux start site-editor --backend codex --cwd ~/projects/site
yikes tmux state site-editor --json
yikes tmux send site-editor "Update the hero copy" --wait --timeout 120
yikes tmux wait site-editor --timeout 120
yikes tmux kill site-editor
```

Use `--replace` on `tmux start` to kill and recreate an existing session with the same name. State reporting uses the same background activity detector as the UI, so scripts can see whether a session is idle, thinking, streaming output, or waiting for a native Codex/Claude choice.

Inside the app, session tabs sit at the top and detailed configuration stays in slash commands. `/new` opens a question-style session chooser with the normal input bar hidden: use Up/Down to move through backend, location, driver, model, complexity, web, answer capture, and root directory; use Left/Right to change values; press Enter to start. Commands such as `/backend`, `/location`, `/driver`, `/models`, `/web`, `/capture`, `/dirs`, `/mcp`, `/sessions`, and `/restart` are backed by the same command registry used by the Python layer.

For tmux sessions, answer capture is enabled by default so `/view extracted` can show the clean answer while `/view full` shows the captured terminal output where available. Turn capture off in the new-session chooser, with `/capture off`, or with `--no-capture` when launching the terminal app if you want a raw low-level session where only your input is pasted into the native TUI. `/key Down`, `/key Up`, `/key Enter`, and `/paste ...` can answer native TUI prompts without leaving yikes!. `/term` opens an interactive terminal attach, and `/fullscreen` gives that terminal the whole screen. Press `Ctrl-b` or the visible return control to come back to yikes!.

To attach images in the terminal app, use `Ctrl+V` for smart paste: yikes! imports an image from the OS clipboard when one is present, otherwise it inserts clipboard text. Dragged or pasted image file paths attach to the next message. `Ctrl+O` forces image-only paste. Host sessions use the local path directly; Docker sessions copy the image into the sandbox before sending the turn.

For local debugging, tmux I/O tracing is opt-in. Set `YIKES_DEVELOPER_MODE=1` or `YIKES_TMUX_IO_LOG=1` before starting yikes! to write bounded JSONL traces under `~/.yikes/debug/tmux-io`. The trace records text pasted into tmux, keys sent to tmux, resize events, and captured terminal output. Files are capped and self-cleaning; use `YIKES_TMUX_IO_LOG_DIR`, `YIKES_TMUX_IO_LOG_FILE_BYTES`, `YIKES_TMUX_IO_LOG_TOTAL_BYTES`, and `YIKES_TMUX_IO_LOG_MAX_FILES` to tune retention.

The local web UI keeps its login key stable across server restarts by default by storing it in the project `.env` file. Use `yikes web --ephemeral-auth` only when you explicitly want a fresh browser login key for that run.

---

## Runtime model

yikes! separates two choices:

- **Location** - where the agent runs: `host`, `docker`, and later remote machines.
- **Driver** - how yikes! drives it: `cli`, `tmux`, and later structured API mode.

That keeps fast CLI calls, Docker isolation, and real interactive tmux sessions composable instead of hiding everything behind one overloaded mode switch.

---

## Python usage

```python
from pathlib import Path

from yikes import Backend, ChatService, Driver, ImageAttachment

conversation = ChatService().create_conversation(
    backend=Backend.CLAUDE,
    driver=Driver.DIRECT,
)

answer = conversation.ask(
    "What is shown here?",
    attachments=(ImageAttachment(Path("screenshot.png").resolve()),),
)
print(answer)
```

The same service layer powers terminal, CLI, and web-backed workflows.

---

## Documentation

The project documentation lives in [`docs/`](docs). To serve it locally:

```bash
mkdocs serve
```

The docs include clickable Mermaid diagrams with a zoom view for larger architecture graphics.

---

## Development

```bash
python -m pip install -e ".[dev]"
pytest -q
mkdocs build --strict
```

End-to-end tests that invoke real Claude Code, Codex, Docker, or tmux sessions are opt-in because they depend on local credentials and may spend API credits.

---

MIT licensed. Copyright (c) 2026 [Michael Ikemann](https://github.com/Alyxion).
