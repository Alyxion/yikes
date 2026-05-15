# OpenHort Alignment Change

This document records how Yikes changed after reviewing OpenHort's current concept. It exists so future changes explain both the technical delta and the migration reason.

## What Changed

1. Added [OpenHort Parity Contract](../openhort-parity.md) as the migration checklist for moving agent/session functionality out of OpenHort and into Yikes.
2. Added [Embedding Yikes](../embedding.md) to document Python access and the iframe-plus-chatbox website editor use case.
3. Added a real Python `Session` facade in `yikes.services`.
4. Exported `Session` from `yikes.__init__`.
5. Added a test proving `ChatService.create_session()` can be embedded from Python without using the TUI.
6. Added `DurableSessionManager` / `DurableSessionMeta` for file-backed Yikes session metadata.
7. Added Docker sandbox session primitives (`SandboxManager`, `SandboxSession`) and cleanup reaper helpers.
8. Added `TokenStore` for hashed temporary/permanent bearer tokens.
9. Removed Claude Remote Control from the registered Claude driver list and from the real integration matrix.
10. Added MCP config/filtering and stdio-to-SSE proxy primitives.
11. Added `CredentialBroker` with explicit grants and env/static/callback/Claude credential providers.
12. Added `EventLog` and a minimal WebSocket remote command layer (`RemoteCommandHandler`, `YikesRemoteServer`).
13. Added `RemoteClient` for Python callers that attach to a running Yikes server without importing the TUI.
14. Added `yikes token` and `yikes server` CLI commands so remote-server mode can be bootstrapped outside Python code.
15. Remote-created sessions now accept the same runtime settings as local sessions: web search, read roots, write roots, and MCP servers.
16. Promoted Docker to a selectable `Driver.DOCKER` for Claude and Codex.
17. Wired Docker turns through `SandboxManager`, reusable containers, explicit read/write root mounts, `host.docker.internal`, and host MCP stdio-to-SSE proxying.
18. Added `SessionInventory` plus `yikes sessions` and `/sessions` so tmux metadata and Docker sandboxes can be listed from Python, CLI, and UI.
19. Added model selection and session inventory controls to the Textual UI.
20. Added a checked-in default Dockerfile for `yikes-sandbox:latest`, automatic default-image build, Docker stderr-preserving startup errors, and Codex auth-file injection from `~/.codex/auth.json` into the container's temporary HOME.
21. Added `SessionLifecycle` plus `yikes close`, `yikes close-all`, `/switch`, `/close`, `/close-all`, and TUI buttons for switching and closing sessions.
22. Reworked tmux as a transport axis rather than a mutually exclusive runtime: local `driver=tmux` starts the real interactive CLI on the host, while `driver=docker` with `tmux_enabled=True` starts tmux inside the container.
23. Enforced the tmux rule that `claude -p` and `codex exec` are never used inside tmux. Claude tmux starts `claude --permission-mode dontAsk`; Codex tmux starts `codex --no-alt-screen` with a per-session `CODEX_HOME`.
24. Added overtake support for Docker+tmux sessions: `yikes attach <sandbox-id>` returns `docker exec -it <container> tmux -S /workspace/yikes-tmux.sock attach -t <session>`.
25. Added generated workspaces for tmux-backed sessions without explicit `cwd`: local tmux gets a random host temp dir, Docker+tmux gets `/workspace/session-<id>` in the container volume.
26. Added startup handling for generated workspaces: Claude/Codex trust prompts are auto-confirmed only for generated workspaces, and Codex update prompts are suppressed with a per-session home/version file rather than updating or mutating the user's real Codex home.

## How It Changed

The code-level change is intentionally small:

```python
from yikes import ChatService, Backend, Driver

session = ChatService().create_session(Backend.CLAUDE, Driver.DIRECT)
answer = session.prompt("Hello")
```

`Session` wraps the existing `Conversation` object and exposes:

- `id`
- `ask()` / `prompt()`
- `messages`
- `status()`
- slash-command execution and suggestions

This gives Python/web callers a stable session-shaped object now, while the docs define the later durable manager/daemon API.

Durable runtime metadata is now stored with:

```python
from yikes import Backend, Driver, DurableSessionManager, RuntimeKind, RuntimeRef

manager = DurableSessionManager()
meta = manager.create(
    backend=Backend.CLAUDE,
    driver=Driver.TMUX,
    runtime=RuntimeRef(RuntimeKind.TMUX, tmux_session="example"),
    cwd=Path.cwd(),
)
```

Sandbox and token primitives are also importable:

```python
from yikes import SandboxManager, TokenStore

sandbox = SandboxManager().create()
token = TokenStore().create_temporary("OpenHort", duration_seconds=3600)
```

Session inventory is available without attaching to any runtime:

```python
from yikes import SessionInventory

print(SessionInventory().format())
```

Session lifecycle is available from Python and mirrors the CLI/UI actions:

```python
from yikes import SessionLifecycle

SessionLifecycle().close("session-id")
SessionLifecycle().close_all(runtime="docker")
```

Docker chat mode is now a normal driver:

```python
from yikes import Backend, ChatService, Driver

session = ChatService().create_session(Backend.CLAUDE, Driver.DOCKER)
```

tmux can also be enabled inside Docker:

```python
from yikes import AgentSettings, Backend, ChatService, Driver

session = ChatService().create_session(
    Backend.CODEX,
    Driver.DOCKER,
    settings=AgentSettings(tmux_enabled=True),
)
```

The attach command for that session points at tmux inside the container:

```bash
yikes attach <sandbox-id> --print-only
# docker exec -it yksb-... tmux -S /workspace/yikes-tmux.sock attach -t yikes-codex
```

When a Docker session has host MCPs attached, Yikes starts host-side SSE proxies and passes `http://host.docker.internal:<port>/sse` endpoints into the container. The working directory and configured read/write roots are mounted into `/workspace/project`, `/workspace/read-*`, and `/workspace/write-*`.

Docker auth handling is deliberately ephemeral:

- Claude receives host env keys/tokens such as `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` when available.
- Codex receives a copy of `~/.codex/auth.json` at `/workspace/home/.codex/auth.json` inside the container.
- Codex also receives a generated `version.json` with the current update prompt dismissed so the TUI does not block or run an updater in automation.
- `/workspace/home` is tmpfs, so those files are not stored in the Docker image or Yikes metadata.

MCP routing and credential grants now have standalone APIs:

```python
from yikes import CredentialBroker, CredentialGrant, McpConfig, resolve_servers

direct, proxied = resolve_servers(McpConfig(...), container_mode=True)
secret_env = CredentialBroker().build_secret_env(
    (CredentialGrant("anthropic", "claude"),),
    env_names={"anthropic": "ANTHROPIC_API_KEY"},
)
```

Remote Yikes command handling is now available without introducing an OpenHort adapter:

```python
from yikes import RemoteCommandHandler, TokenStore

token = TokenStore().create_temporary("browser", duration_seconds=3600)
handler = RemoteCommandHandler()
response = handler.handle({
    "token": token,
    "command": "session.create",
    "params": {"backend": "claude", "driver": "direct"},
})
```

Remote clients can also attach from Python:

```python
from yikes import RemoteClient, RemoteClientConfig

client = RemoteClient(RemoteClientConfig("ws://127.0.0.1:8989", token="..."))
created = await client.create_session(backend="claude", driver="direct")
answer = await client.prompt(created["session"]["session_id"], "Change the HTML title")
```

The CLI bootstrap path is:

```bash
yikes token --name browser --ttl 3600
yikes server --host 127.0.0.1 --port 8989
```

The token command prints plaintext token material only once. The token store keeps only hashes.

## Design Correction

OpenHort shows that the central abstraction cannot be a terminal app owning a subprocess. The central abstraction must be:

```text
client -> Yikes session manager -> runtime backend -> Claude/Codex
```

Clients include the terminal UI, CLI commands, Python code, a web backend, and OpenHort. Disconnecting a client must not kill the runtime session.

## Note On Remote Control

Claude Remote Control is not treated as a Yikes chat runtime. It is a Claude human remote UI. The replacement concept for OpenHort integration is `remote-server`: a Yikes-owned HTTP/WebSocket control surface with scoped bearer tokens.
