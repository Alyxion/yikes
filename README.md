<p align="center"><img src="https://raw.githubusercontent.com/Alyxion/yikes/main/media/logo-small.png" alt="Yikes" width="400"></p>

# yikes

[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/Alyxion/yikes/blob/main/LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.0-blueviolet.svg)](https://github.com/Alyxion/yikes)
[![Built with Poetry](https://img.shields.io/badge/build-poetry-60A5FA.svg)](https://python-poetry.org/)

### A terminal-first runtime for Claude Code and Codex CLI.

Yikes gives Python apps, terminal users, and future web backends one consistent way to start, control, inspect, and overtake Claude Code and Codex sessions.

It is built for the cases where a simple one-shot prompt is not enough: persistent sessions, real interactive tmux control, Docker isolation, MCP attachment, readable/writable directory policy, and a clean Python surface for embedding the same runtime elsewhere.

---

## What you get

- **Full terminal app by default** - running `yikes` opens the interactive control surface.
- **Claude Code and Codex support** - switch backend, model, complexity, web search, and runtime from the UI or slash commands.
- **Host and Docker runtimes** - run locally or inside an isolated container.
- **Real tmux sessions** - attach to long-running work and overtake the native interactive CLI when needed.
- **MCP and directory policy** - configure tool servers plus readable and writable directories.
- **Python-first core** - the UI and CLI sit on the same helper classes a web backend can use later.

---

## Quick start

```bash
git clone https://github.com/Alyxion/yikes.git
cd yikes
poetry install
poetry run yikes
```

Yikes expects the backend CLIs you want to use to be installed and logged in for your user:

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- [Codex CLI](https://github.com/openai/codex)

---

## Common commands

```bash
poetry run yikes                         # open the terminal app
poetry run yikes tui --backend claude    # start with Claude Code
poetry run yikes tui --backend codex     # start with Codex CLI
poetry run yikes sessions                # list durable sessions
poetry run yikes attach <session-id>     # overtake an attachable session
poetry run yikes close <session-id>      # close one session
```

Inside the app, slash commands such as `/backend`, `/location`, `/driver`, `/models`, `/web`, `/dirs`, `/mcp`, `/sessions`, and `/restart` are backed by the same command registry used by the Python layer.

---

## Runtime model

Yikes separates two choices:

- **Location** - where the agent runs: `host`, `docker`, and later remote machines.
- **Driver** - how Yikes drives it: `cli`, `tmux`, and later structured API mode.

That keeps fast CLI calls, Docker isolation, and real interactive tmux sessions composable instead of hiding everything behind one overloaded mode switch.

---

## Python usage

```python
from yikes import Backend, ChatService, Driver

conversation = ChatService().create_conversation(
    backend=Backend.CLAUDE,
    driver=Driver.DIRECT,
)

answer = conversation.ask("Hello, my name is Michael. What is 4 + 4?")
print(answer)
```

The same service layer is intended for terminal, CLI, and web-backed workflows.

---

## Documentation

The project documentation lives in [`docs/`](docs). To serve it locally:

```bash
poetry run mkdocs serve
```

The docs include clickable Mermaid diagrams with a zoom view for larger architecture graphics.

---

## Development

```bash
poetry install
poetry run pytest -q
poetry run mkdocs build --strict
```

End-to-end tests that invoke real Claude Code, Codex, Docker, or tmux sessions are opt-in because they depend on local credentials and may spend API credits.

---

MIT licensed. Copyright (c) 2026 [Michael Ikemann](https://github.com/Alyxion).
