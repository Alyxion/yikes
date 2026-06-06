<p align="center"><img src="https://raw.githubusercontent.com/Alyxion/yikes/main/media/logo-small.png" alt="yikes!" width="400"></p>

# yikes!

[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/Alyxion/yikes/blob/main/LICENSE)
[![PyPI version](https://img.shields.io/pypi/v/yikes.svg)](https://pypi.org/project/yikes/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-purple.svg)](https://github.com/astral-sh/ruff)

### Run Claude Code and Codex like durable, controllable services — from the terminal, the browser, or Python.

`yikes!` is a terminal-first runtime for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and the [Codex CLI](https://github.com/openai/codex). One word — `yikes claude` — starts (or reattaches) a real interactive session for the current project and drops you straight in: as easy as running `claude`, but **durable, reattachable, optionally sandboxed, and reachable from a browser**.

The same runtime is exposed three ways — an interactive terminal app, a login-gated web control surface, and a clean Python/CLI API — all backed by **one shared controller**.

<p align="center"><img src="https://raw.githubusercontent.com/Alyxion/yikes/main/media/runtime-architecture.svg" alt="yikes! runtime architecture — you drive one shared controller (terminal app, web UI, CLI/Python API) that runs Claude Code or Codex via either driver (tmux interactive or one-shot cli, claude -p / codex exec) on either location (host or docker, isolated)" width="820"></p>

---

## Why yikes!

For the cases where a one-shot prompt is not enough:

- **One-word launchers** — `yikes claude` / `yikes codex` start or reattach a real interactive session for the current directory. The session name follows the git project, so re-running resumes it; concurrent work lives in separate projects.
- **Durable sessions** — every session is a real tmux session that survives disconnects. Reattach from any terminal, list and overtake sessions, and drive them from scripts.
- **Docker isolation** — `yikes claude -i` runs the agent in a container with the project mounted and ports published, so risky work stays contained.
- **A web control surface** — `yikes web` serves a login-gated UI with one tab per session and **sub-tabs ("panes")**: the live terminal, the web app the agent is building (embedded), health tables, and links.
- **Project config** — a committed `yikes.toml` declares per-project defaults (backend, isolation, ports, panes); `yikes setup` can write it by inspecting the repo.
- **Python-first core** — the terminal app, web UI, and CLI share the same controller and service layer, so the runtime embeds cleanly elsewhere.

---

## Install

```bash
pip install yikes
yikes
```

`yikes!` drives the backend CLIs you already use, so install and log into the ones you want:
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) · [Codex CLI](https://github.com/openai/codex). `tmux` is required for interactive sessions; Docker is optional (only for `-i`).

> Working from a clone instead? `git clone … && cd yikes && pip install -e .`, then `bash scripts/install-path.sh` to put `yikes` on PATH for new shells.

---

## Quick start

```bash
yikes claude                  # start or reattach an interactive Claude session here
yikes codex                   # same, with the Codex CLI
yikes claude -i               # run it isolated in Docker (ports from yikes.toml)
yikes claude -m "scaffold a FastAPI app"   # seed the first prompt
yikes                         # chooser: claude / codex / terminal overview
```

Each launch shows a short pre-flight panel (mapped ports, how to leave with `Ctrl-b d` and return with `yikes claude`), then drops you into the live session. Per-project defaults live in a committed `yikes.toml` — run `yikes init` to scaffold one or `yikes setup` to have the backend inspect the repo and write it for you.

```bash
yikes sessions                # list durable sessions
yikes attach <name|id>        # overtake an attachable session
yikes close-all               # close every session (confirms first; -y to skip)
yikes web                     # serve the browser control surface
```

See the [CLI reference](docs/cli-wrapper.md) for the full command surface, Docker isolation, and project config.

---

## Web control surface

`yikes web` serves a login-key-gated UI on all interfaces by default — the key is stored user-globally and login is rate-limited against brute force, so it is safe on a LAN. `yikes web --url` prints the ready-to-open login URL so you can reach it from another machine without copying the key by hand.

Each session tab carries **panes** (sub-tabs): the live terminal, an embedded view of the **web app the agent is building** (a full browser bar with reload, history, and a load/unload toggle), auto-refreshing **health/status tables**, and sidebar links. Panes are declared per project in `yikes.toml` `[[panes]]` — by port, never a hardcoded host — or added at runtime with the **＋ web** button. A pane with a `start` command gets a Start/Stop control so yikes can run the dev server for you.

Each tab also has a **🔊 speaker mode**: a per-tab toggle that speaks a short summary when that agent finishes, asks a question, or errors — so you can look away and still know when it needs you. It detects "done/waiting" automatically, never speaks mid-task or repeats itself, and works with a Claude or OpenAI key (OpenAI voice when available, browser voice otherwise). A draggable **🗣 push-to-talk** button over the terminal closes the loop: hold to talk, and a fast model decides whether you dictated text (collected as removable chips) or gave a command — "accept", "option two", "cancel" — which it executes as keystrokes for Claude or Codex.

See the [Web UI docs](docs/cli-wrapper.md#web-ui-yikes-web), [Session panes](docs/cli-wrapper.md#session-panes-sub-tabs), and [Speaker mode](docs/speaker-mode.md).

---

## Runtime model

yikes! separates two orthogonal choices, instead of one overloaded mode switch:

| Choice | Options | Meaning |
| --- | --- | --- |
| **Location** | `host` · `docker` · *(remote, planned)* | Where the agent runs |
| **Driver** | `cli` · `tmux` · *(api, planned)* | How yikes! drives it |

That keeps fast one-shot CLI calls, Docker isolation, and real interactive tmux sessions composable.

---

## Python API

The same service layer that powers the terminal, CLI, and web surfaces is available directly:

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

---

## Documentation

Full documentation lives in [`docs/`](docs) (architecture, CLI reference, tmux layer, Python library, embedding). Serve it locally with `mkdocs serve`; the diagrams are clickable with a zoom view.

---

## Development

```bash
poetry install && poetry run pytest -q
```

See [`docs/development.md`](docs/development.md) for the full contributor setup (git hooks, integration tests, docs, and the release flow).

---

MIT licensed · Copyright © 2026 [Michael Ikemann](https://github.com/Alyxion).
