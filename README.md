# yikes
Yikes makes it possible to use tools such as Codex or Claude Code like via the classic API

## First implementation

The initial runnable slice is a chatbot smoke flow across the backend/driver matrix:

```bash
poetry run yikes
poetry run yikes chat-smoke --backend claude --driver direct --json
poetry run yikes chat-smoke --backend codex --driver tmux --json
poetry run yikes tui --backend claude --driver direct
poetry run yikes tui --no-web-search --read-dir ./docs --write-dir ./tmp --mcp "fs=python -m server"
```

Running `yikes` with no arguments launches the full terminal app, matching the default interactive behavior of tools like Codex and Claude Code. The TUI is built with Textual and uses the same `ChatService` that backs the CLI smoke command, so the core conversation logic can later be reused from a web backend without depending on terminal UI code.

Slash commands are owned by a shared command registry, not by the Textual view. Typing `/` in the TUI asks that registry for command suggestions, Tab accepts the first available completion, and `/models` always renders the model options registered for the active backend. The active backend, usage mode, model, complexity level, web-search setting, readable directories, writable directories, and attached MCP servers are remembered in `~/.config/yikes/state.json` and restored on restart; set `YIKES_STATE_PATH` to override that location.

The interactive chat app exposes `direct` and `tmux` modes. Remote-control is kept out of this chat selector because Claude Remote Control is a human remote UI rather than a local prompt/response transport. Backend and mode can be changed either with the left-side controls or with `/backend`, `/driver`, and `/mode`; complexity can be changed with the sidebar selector or `/complexity`; web search can be toggled with the sidebar selector or `/web on|off`; directories are managed with `/dirs`; MCP servers are managed with `/mcp`. Use `/restart` while developing to restart the terminal app and reload local code changes. Future CLI and web frontends should use the same registry APIs for command execution, completion, and contextual suggestions instead of copying command lists into each surface.

The same settings are available directly from Python:

```python
from pathlib import Path
from yikes import AgentSettings, Backend, ChatService, Driver, McpServer

settings = AgentSettings(
    web_search_enabled=True,
    read_roots=(Path("docs"),),
    write_roots=(Path("tmp"),),
    mcp_servers=(McpServer("fs", "python", ("-m", "server")),),
)
conversation = ChatService().create_conversation(Backend.CLAUDE, Driver.DIRECT, settings=settings)
```

The flow sends three turns:

1. `Hello, my name is Michael. How are you doing?`
2. `What is 4+4?`
3. `What is my name? Answer with exactly one word and no punctuation.`

Real end-to-end tests are opt-in because they invoke local Claude Code/Codex binaries and may spend API credits:

```bash
YIKES_RUN_E2E=1 poetry run pytest -q
```

Run the E2E suite from the same user/session that can run `claude -p ...` and `codex exec ...`. Claude Code may report `Not logged in` when launched from a restricted sandbox or service process that cannot access your normal login/keychain state, even if your interactive shell is logged in.

Claude's native Remote Control currently exposes a human remote UI rather than a local programmatic turn API. The explicit `claude/remote-control` test slot is present and skipped by default; set `YIKES_CLAUDE_REMOTE_FALLBACK=direct` to exercise that slot through Claude's automatable protocol until a machine API exists.

Codex remote-control starts a loopback `codex app-server --listen ws://...` and speaks JSON-RPC over websocket. Set `YIKES_CODEX_REMOTE_STRICT=1` to fail instead of falling back if a local experimental Codex build changes the websocket protocol.
