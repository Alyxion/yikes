# yikes

A unified driver for **Claude Code** and **Codex CLI** — runs them directly, inside tmux, or through each backend's remote-control surface, exposes them as a Python library and a thin CLI wrapper, and turns their streaming output into a clean event stream.

!!! warning "Design-only"
    This site is the concept. **Nothing has been implemented yet.** It captures the plan so we can iterate on the approach before writing code. Pages reference modules that don't exist; treat them as targets, not as documentation of existing behaviour.

---

## What we're building

Three faces over one engine:

```mermaid
flowchart LR
    user([User / Script]) --> cli["yikes CLI<br/>(thin wrapper)"]
    user2([Python program]) --> lib["yikes Python library<br/>Session object"]
    cli --> engine[yikes engine]
    lib --> engine
    engine --> drv_tmux[tmux driver]
    engine --> drv_direct[direct subprocess driver]
    engine --> drv_remote[remote-control driver]
    drv_tmux --> ai_tmux[("claude / codex<br/>inside tmux pane")]
    drv_direct --> ai_direct[("claude -p --output-format stream-json<br/>codex app-server / codex exec --json")]
    drv_remote --> ai_remote[("Claude Remote Control<br/>Codex app-server websocket / --remote")]
    classDef face fill:#eef,stroke:#669
    classDef driver fill:#efe,stroke:#696
    classDef agent fill:#fee,stroke:#966
    class cli,lib face
    class drv_tmux,drv_direct,drv_remote driver
    class ai_tmux,ai_direct,ai_remote agent
```

1. **CLI app** — accepts essentially the same flags as `claude` and `codex` but routes work through `direct`, `tmux`, or `remote-control` depending on the command and explicit flags. Multi-line prompts, image attachments, approval prompts, slash commands, and key presses work where the selected driver supports them.
2. **Python library (3.14+)** — `async with Session(...) as s: ...`, iterate streamed events, take snapshots, send keystrokes.
3. **Shared engine** — owns streaming, line-revision tracking, transcript persistence.

Both flavours (Claude Code, Codex) must work across three drivers. That gives us six combinations to implement and test: `claude/direct`, `claude/tmux`, `claude/remote-control`, `codex/direct`, `codex/tmux`, and `codex/remote-control`.

---

## Why three drivers?

| Driver | When it wins |
|---|---|
| **`direct`** (fast path) | Headless, structured output. `claude -p --output-format stream-json` gives clean token deltas; `codex app-server` gives JSON-RPC with delta events. Lower latency, no screen-scraping, but no access to TUI-only features. |
| **`tmux`** (TUI fallback) | Anything that must drive the local interactive TUI by keystroke: slash commands, prompts, full-screen flows, manual attach, and recovery when native protocols do not expose a feature. |
| **`remote-control`** (native remote) | Native remote surfaces. Claude Code uses `claude --remote-control` / `/remote-control`; Codex uses app-server websocket / `codex --remote`. Best for controlling a local agent from another device or process without screen-scraping, but backend capabilities and security rules differ. |

The library and CLI accept a `--driver` flag (or `Session(driver="direct"|"tmux"|"remote-control")`). Sensible defaults:

- `direct` for one-shot `-p`-style invocations where we just want clean output.
- `tmux` if the request needs a local interactive TUI or human attach.
- `remote-control` only when requested explicitly, or when a higher-level command is clearly about remote/mobile control.
- Explicit override always wins.

---

## Goals

- **One Session abstraction** that works for both Claude Code and Codex.
- **Live streaming** of text deltas with low latency (a few ms over native).
- **Line-revision events** so a caller can observe "this line just changed" — the thing that makes spinners and in-place token streams sensible to consume.
- **Snapshots** of the current screen state, cheap to call.
- **Python 3.14+** async-first (TaskGroup, `asyncio.timeout`, pattern matching).
- **CLI is a thin shell** over the library — behaviour must stay consistent across both faces.

## Non-goals (for v1)

- Daemon that pools sessions across users.
- Re-implementing either CLI's prompt parsing.
- Hosting an MCP server ourselves (we *consume* both CLIs; we may expose one later).
- Building our own TUI — we are a transport, not a UI.

---

## Why tmux still matters?

Native streaming pipes (`claude -p --output-format stream-json`, `codex exec --json`, `codex app-server`) are objectively better for "give me clean tokens." But the user wants to drive the *interactive* CLIs — approve commands with `y`, paste images, use `/model` or `/compact`, send `Ctrl+C` to cancel a turn. That's a TUI surface, and the cleanest stable way to drive a TUI from outside is to put it in a PTY and control that PTY.

tmux gives us:

- A persistent PTY with a controllable size (output doesn't reflow to 80×24).
- A stable API surface (`send-keys`, `capture-pane`, `paste-buffer`, per-session control mode `-C` taps).
- Sessions survive our wrapper crashing — you can attach with a real terminal and inspect.
- Multiplexing: one tmux server can host many AI sessions on isolated sockets.

tmux is not the semantic control plane when a backend exposes a structured protocol. It is the resilient local TUI fallback and the human-inspection path. Direct subprocess is the fast path for headless work, and remote-control is the native remote path.

---

## Vocabulary

| Term | Meaning |
|---|---|
| **Backend** | One of `claude` or `codex`. The agent CLI we drive. |
| **Driver** | How we talk to that backend. `direct`, `tmux`, or `remote-control`. |
| **Session** | A single conversation with a backend. Has a stable ID, a transcript, an event stream. |
| **Pane** | The tmux pane that hosts a session when the driver is `tmux`. |
| **Event** | A typed message we surface to the caller (`StreamDelta`, `LineRevised`, `ToolUse`, `ApprovalRequest`, `TurnComplete`, …). |
| **Snapshot** | The current rendered screen text for a session, returned synchronously. |

---

## How to read these docs

- Read [Architecture](architecture.md) first.
- Then either [Claude Code](backends/claude-code.md) or [Codex](backends/codex.md), depending on which CLI you care about.
- [tmux Layer](tmux-layer.md) and [Streaming & Updates](streaming.md) are the implementation core.
- [Python Library](python-library.md) and [CLI Wrapper](cli-wrapper.md) describe the public faces.
- [Roadmap](roadmap.md) has the phased plan and the open questions we need to resolve before writing code.
