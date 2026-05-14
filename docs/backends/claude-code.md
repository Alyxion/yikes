# Backend: Claude Code

The Claude Code adapter (`yikes.backends.claude`) drives the `claude` CLI in two modes:

- **TUI mode** — `claude` (REPL) inside a tmux pane. Keystrokes for everything.
- **Headless mode** — `claude -p ... --output-format stream-json --verbose`. Parsed NDJSON straight into the event bus.

## I/O reference (summary)

Full reference is in the research notes; here's what the adapter actually uses.

### Input

| Need | Mechanism |
|---|---|
| One-shot prompt | `claude -p "..."` |
| Multi-turn (resume) | `claude --resume <session-id>` |
| Continue most-recent | `claude --continue` |
| Streaming input/output | `--input-format stream-json --output-format stream-json` |
| File reference in prompt | `@path/to/file` (glob OK) |
| System prompt | `--system-prompt`, `--append-system-prompt` (and `*-file` variants) |
| Settings override | `--settings '{...}'` or `--settings ./file.json` |
| Tool gating | `--allowedTools "Bash(git *),Read"`, `--disallowedTools "WebFetch"` |
| Permission mode | `--permission-mode auto\|acceptEdits\|plan\|default\|dontAsk\|bypassPermissions` |
| Run-cost limits | `--max-turns N`, `--max-budget-usd 5.00` |
| Skip auto-discovery | `--bare` |
| Structured output | `--output-format json --json-schema '<JSON Schema>'` |
| Disable session save | `--no-session-persistence` |

### Output (stream-json)

NDJSON. Event types we consume:

```mermaid
flowchart LR
    init["system / init"] --> status["system / status"]
    status --> mstart["stream_event: message_start"]
    mstart --> cbstart["stream_event: content_block_start"]
    cbstart --> cbdelta["stream_event: content_block_delta<br/>(text_delta | input_json_delta)"]
    cbdelta --> cbdelta
    cbdelta --> cbstop["stream_event: content_block_stop"]
    cbstop --> mstop["stream_event: message_stop"]
    mstop --> tool["tool_use → tool_result"]
    tool --> mstart
    mstop --> result["result (final)"]
```

Key fields:

```json
{ "type": "stream_event",
  "event": { "type": "content_block_delta", "index": 0,
             "delta": { "type": "text_delta", "text": "Hello" } },
  "session_id": "..." }
```

The adapter maps:

| Native event | Engine event |
|---|---|
| `content_block_delta` (text_delta) | `StreamDelta(text=..., block_id=index)` |
| `content_block_delta` (input_json_delta) | accumulated to `ToolUse` at `content_block_stop` |
| `tool_use` | `ToolUse(tool=..., input=..., tool_use_id=...)` |
| `tool_result` | `ToolResult(...)` |
| `permission_request` | `ApprovalRequest(...)` |
| `result` (final) | `TurnComplete(stop_reason, usage)` |

### Exit codes & session storage

- Exit 0 success; 1 runtime error; 2 invalid args.
- Transcripts: `~/.claude/projects/<project-id>/<session-id>.jsonl`. We record the session ID in our sidecar so `--resume` works.

## Driving Claude Code in **tmux mode**

```mermaid
sequenceDiagram
    participant adp as claude adapter
    participant drv as tmux driver
    participant cl as claude (TUI)

    adp->>drv: start(["claude"])
    drv->>cl: spawn in pane
    Note over drv,cl: wait for prompt sentinel
    drv-->>adp: ready

    adp->>drv: send_text(prompt, bracketed_paste=True)
    adp->>drv: send_key("Enter")

    loop streaming
        cl-->>drv: bytes (text + ANSI redraws)
        drv-->>adp: bytes
        adp->>adp: feed pyte, detect block boundaries
        adp-->>engine: StreamDelta, LineRevised
    end

    cl->>cl: prompts y/n for Bash
    drv-->>adp: ApprovalRequest detected (sentinel match)
    adp-->>engine: ApprovalRequest
    engine-->>caller: event
    caller->>engine: approve()
    engine->>adp: send "y" + Enter
    adp->>drv: send_text("y")
    adp->>drv: send_key("Enter")
```

### Keystroke vocabulary

The adapter exposes a verb table that the tmux driver applies:

| Verb | tmux send-keys |
|---|---|
| send prompt | `load-buffer` → `paste-buffer -p -d` → `Enter` |
| submit | `Enter` |
| newline in prompt | `Shift+Enter` or `Alt+Enter` depending on terminal |
| cancel turn | `C-c` (one press) |
| exit | `C-d` (twice — first cancels, second exits) |
| approve | `y` then `Enter` |
| deny | `n` then `Enter` |
| slash command | literal `/<name> args` + `Enter` |
| paste image | path via `@path/to/img.png` in prompt, or stdin paste |
| switch model | `/model` slash command |
| compact context | `/compact` slash command |

### Detecting approval prompts

Claude Code surfaces approval prompts with a recognisable layout: a box with "Do you want to" text and option lines (`1. Yes`, `2. No, and tell Claude what to do differently`). The adapter watches `LineRevised` events in the bottom rows and triggers an `ApprovalRequest` when a known-shape modal appears.

A safer fallback is to **run with `--permission-mode acceptEdits` or `dontAsk`** when we *don't* want approval flows, which removes the modal entirely.

## Driving Claude Code in **direct (headless) mode**

```python
argv = [
    "claude", "-p", prompt,
    "--output-format", "stream-json",
    "--include-partial-messages",
    "--verbose",
    *resume_args,
    *system_prompt_args,
    *tool_args,
]
```

The adapter consumes NDJSON line by line and emits events. No tmux, no pyte. ~3–10 ms latency floor over a direct PTY (no measurable overhead).

For a streaming **bidirectional** session in headless mode, use `--input-format stream-json --output-format stream-json` and write user messages as JSON to stdin:

```json
{"type":"user_message","content":"hello"}
{"type":"user_message","content":"now please refactor"}
```

This is the closest direct-mode analogue to a long-lived TUI session, but mid-turn slash commands and approval flows are not available — for those, switch to `tmux` mode.

## Adapter responsibilities

```python
class ClaudeAdapter(BackendAdapter):
    backend = "claude"

    async def start_direct_session(self, opts: SessionOptions) -> None:
        # build argv, spawn via direct driver, parse stream-json
        ...

    async def start_tmux_session(self, opts: SessionOptions) -> None:
        # start "claude" or "claude --resume <id>" in a tmux pane,
        # wait for prompt sentinel, register keystroke vocabulary
        ...

    def parse(self, raw: bytes) -> Iterator[Event]:
        # direct mode: NDJSON → events
        # tmux mode: pyte dirty-line diff → events + approval-prompt detection
        ...
```

## Known gotchas

- **`stream-json` requires `--verbose`.** Otherwise you get a single `result` event with no deltas.
- **`stream-json` requires `--include-partial-messages`** for fine-grained `content_block_delta` events; otherwise messages arrive in one chunk.
- **`--bare` skips CLAUDE.md, hooks, plugins, MCP.** Use it for deterministic CI; *don't* use it for the tmux mode where the user likely wants their config.
- **Multi-line paste via tmux can collapse to one line** if the user has `extended-keys-format csi-u`. The tmux layer runs on a dedicated socket with that off — see [tmux Layer](../tmux-layer.md#isolation).
- **Approval-prompt detection is heuristic.** If Claude Code changes its modal rendering, our regex needs updating. Wrap detection in a versioned matcher with a fallback test fixture.
