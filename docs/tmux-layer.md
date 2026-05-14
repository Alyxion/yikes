# tmux Layer

The tmux driver is what makes "run claude/codex via tmux" real. It owns the lifecycle of a managed tmux server, the panes that host AI sessions, and the byte stream that flows out of them.

## Design principles

1. **Dedicated socket.** We never use the user's default tmux server. A separate `-L yikes` (or `-S /path`) socket keeps our sessions out of the user's `tmux ls`, and a `-f /dev/null` config ignores their `~/.tmux.conf`.
2. **Stable IDs over names.** Always target by `$<n>` (session), `@<n>` (window), `%<n>` (pane). Names change; IDs don't.
3. **Hybrid I/O.** **Control mode** (`tmux -C attach`) for the event substrate (low-latency `%output` notifications), **`capture-pane`** for on-demand snapshots, **`send-keys` / `paste-buffer`** for input.
4. **One pyte per pane.** The VT emulator runs in our process and consumes `%output` bytes; we never re-parse from tmux's grid (which is already collapsed).

## Topology

```mermaid
flowchart TB
    subgraph yikes[yikes process]
        engine[Engine]
        ctl["TmuxControl<br/>(-C attach)"]
        libtmux["libtmux<br/>(commands)"]
        pyte_a[pyte Screen A]
        pyte_b[pyte Screen B]
    end
    subgraph tmuxserver[tmux server / dedicated socket]
        s1["session $0<br/>pane %0: claude"]
        s2["session $1<br/>pane %1: codex"]
    end

    engine --> libtmux
    engine --> ctl
    libtmux -->|send-keys, paste-buffer,<br/>capture-pane, new-session| tmuxserver
    ctl <-->|%output, %window-*, ...| tmuxserver
    s1 -. bytes .-> ctl
    s2 -. bytes .-> ctl
    ctl --> pyte_a
    ctl --> pyte_b
    pyte_a --> engine
    pyte_b --> engine
```

- One long-lived `tmux -C attach` subprocess multiplexes notifications from **all** panes on our socket.
- `libtmux` issues commands (low frequency, synchronous request/response).
- Each pane gets its own pyte `Screen` in our process; pyte's `dirty` set drives our `LineRevised` events.

## Lifecycle

```mermaid
stateDiagram-v2
    [*] --> Spawning: spawn()
    Spawning --> Starting: new-session created
    Starting --> Ready: prompt sentinel detected
    Ready --> Streaming: user sends prompt
    Streaming --> Ready: turn complete
    Streaming --> ApprovalWait: approval prompt detected
    ApprovalWait --> Streaming: approve/deny
    Ready --> Pausing: pause()
    Pausing --> Paused
    Paused --> Ready: resume()
    Ready --> Killing: kill()
    Streaming --> Killing: kill()
    Killing --> [*]: kill-session
```

Every state transition emits an engine event so callers can observe lifecycle.

## Operations exposed to the engine

The tmux driver presents this internal API:

```python
class TmuxDriver(Driver):
    socket_name: str = "yikes"          # tmux -L yikes
    base_config: Path = Path("/dev/null")

    async def spawn(self, *, name: str, argv: list[str],
                    env: dict[str, str], cwd: Path,
                    cols: int = 200, rows: int = 50) -> PaneHandle: ...

    async def list(self) -> list[PaneHandle]: ...
    async def get(self, session_id: str) -> PaneHandle | None: ...
    async def kill(self, session_id: str) -> None: ...
    async def kill_all(self) -> None: ...                # kill-server on our socket
    async def kill_orphans(self, *, max_age: float | None = None) -> int: ...

    async def attach_command(self, session_id: str) -> list[str]:
        """Return the argv a human can use to attach with their own tmux client."""
        ...

    async def stream(self, pane: PaneHandle) -> AsyncIterator[bytes]: ...
    async def snapshot(self, pane: PaneHandle, *, with_ansi: bool = False) -> str: ...

    async def send_text(self, pane: PaneHandle, text: str,
                        *, bracketed_paste: bool = False) -> None: ...
    async def send_key(self, pane: PaneHandle, key: str) -> None: ...
    async def send_keys(self, pane: PaneHandle, keys: list[str]) -> None: ...

    async def resize(self, pane: PaneHandle, cols: int, rows: int) -> None: ...
```

This is what gets called by both `claude` and `codex` adapters when their driver is `tmux`. The verbs (`kill_all`, `list`, `attach_command`) flow up to the CLI as `yikes ps`, `yikes kill`, `yikes attach`.

## Isolation

`/dev/null` config + explicit options keeps the wrapper deterministic regardless of the user's tmux config:

```bash
tmux -L yikes -f /dev/null new-session -d -s ai_$$ -x 200 -y 50 \
    -P -F '#{session_id} #{pane_id}' \
    -e TERM=tmux-256color \
    "claude"

tmux -L yikes set -g default-terminal "tmux-256color"
tmux -L yikes set -g status off
tmux -L yikes set -g history-limit 100000
tmux -L yikes set -as terminal-features ',xterm*:sync'   # pass-through DECSET 2026
tmux -L yikes set -g extended-keys off                    # avoid csi-u paste bug
```

!!! danger "Why this matters"
    With `extended-keys-format csi-u` enabled (a common modern tmux setting), CR/LF inside a bracketed paste block gets re-encoded as CSI-u sequences that Claude Code's paste tokeniser silently drops — collapsing your multi-line prompt to a single line ([claude-code#43169](https://github.com/anthropics/claude-code/issues/43169)).

## Sending input

### Literal text vs named keys

Two distinct calls, never mixed:

```python
# wrong — tmux interprets {, #, "Enter", "$"
await tmux("send-keys", "-t", pane, f"let x = 1; echo $x Enter")

# right — separate literal payload from control keys
await tmux("send-keys", "-t", pane, "-l", "let x = 1; echo $x")
await tmux("send-keys", "-t", pane, "Enter")
```

The driver enforces this at the API level: `send_text()` uses `-l`, `send_key()` uses the named-key path.

### Multi-line prompts via bracketed paste

```python
async def send_text(self, pane, text, *, bracketed_paste=False):
    if bracketed_paste:
        buf = f"yikes_{pane.id}"
        await tmux("load-buffer", "-b", buf, "-")  # stdin = text
        await tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", pane.id)
    else:
        # split on '\n', emit literal+Enter for each line — only useful for shells
        for line in text.splitlines():
            await tmux("send-keys", "-t", pane.id, "-l", line)
            await tmux("send-keys", "-t", pane.id, "Enter")
```

`-p` wraps the paste in bracketed-paste markers (`ESC[200~ ... ESC[201~`). Claude Code, Codex, and any modern TUI prompt treat that as raw content, not as submission.

`-d` deletes the buffer after pasting (avoid leaking secrets in tmux's buffer list).

### Timing

We do **not** insert fixed sleeps. Instead, the driver waits for sentinels:

1. **Shell readiness** — if the pane is hosting a shell that launches the AI (uncommon — we usually launch the AI directly), wait for an echoed marker.
2. **TUI readiness** — wait for a known prompt glyph in the bottom rows via `capture-pane`. For Claude Code: `│ >`; for Codex: `╭───`. Configurable.

```python
async def wait_for_prompt(self, pane, *, timeout=10.0):
    async with asyncio.timeout(timeout):
        while True:
            text = await self.snapshot(pane, rows=-5)
            if self.adapter.is_prompt_ready(text):
                return
            await asyncio.sleep(0.05)
```

## Reading output

### Why control mode for the stream

The alternative is polling `capture-pane`. Bad idea:

- `capture-pane` returns the *current rendered grid*. Spinner redraws have collapsed by the time we look. We lose intermediate tokens.
- Each `capture-pane` is a fork+exec — ~5–12 ms — so polling at 20 Hz costs a CPU.

Control mode (`tmux -C attach`) delivers `%output %P data` notifications as bytes are written, with one persistent subprocess for the whole socket. We never miss intermediate state, and the cost is one open pipe.

### The reader loop

```python
async def _reader(self):
    """Run for the lifetime of the driver."""
    async for line in self.proc.stdout:
        line = line.rstrip(b'\r\n')
        match line.split(b' ', 2):
            case [b'%output', pane, payload]:
                data = decode_octal(payload)
                await self._panes[pane.decode()].feed(data)
            case [b'%window-close', wid, *_]:
                await self._on_pane_close(wid.decode())
            case [b'%pause', wid]:
                await self._panes[wid.decode()].on_pause()
            case [b'%continue', wid]:
                await self._panes[wid.decode()].on_continue()
            case [b'%subscription-changed', name, *rest]:
                await self._on_subscription(name.decode(), rest)
            case [b'%exit', *_]:
                return
```

**`%output` decoding**: tmux replaces every byte `<32` and the backslash byte with three-digit octal escapes. `\033` is ESC, `\012` is LF, `\134` is backslash. The decoder is a small regex replacement.

### Flow control via `refresh-client`

For high-throughput streams (long agent reasoning, large diff dumps) we set:

```
refresh-client -f pause-after=30
```

so a slow consumer gets a `%pause` event instead of unbounded buffering, then `refresh-client -A '%P:continue'` to resume.

### Snapshots

`snapshot()` calls `capture-pane`:

```bash
tmux -L yikes capture-pane -p -e -J -S - -E - -t %0
```

- `-p` print to stdout
- `-e` include ANSI
- `-J` join wrapped lines
- `-S - -E -` full scrollback

For a quick "what's on screen right now" we pass `-S -<rows>` to limit cost.

## Session management (cross-cutting)

A first-class requirement: **users can spawn, kill, list, killall sessions uniformly across Claude Code and Codex.**

The tmux driver implements all of these against our socket. The adapter contributes a *label* (`claude` / `codex`) and a *metadata blob* (model, cwd, native session ID) stored as pane environment.

```bash
# Spawn (claude)
tmux -L yikes new-session -d -s yikes-claude-3f9 -x 200 -y 50 \
    -e YIKES_BACKEND=claude -e YIKES_NATIVE_ID=abc123 \
    -e YIKES_MODEL=opus -e YIKES_CWD=/Users/x/proj \
    "claude --resume abc123"

# List
tmux -L yikes list-sessions -F '#{session_id} #{session_name} #{?#{==:#{E:YIKES_BACKEND},claude},claude,codex} #{E:YIKES_MODEL}'

# Kill one
tmux -L yikes kill-session -t '$3'

# Kill all (whole server on our socket)
tmux -L yikes kill-server
```

Pane environment is propagated to the child via `-e KEY=VALUE`. We use this to tag sessions with their backend and native session ID so `yikes list` can show them.

## Pitfalls — checklist

| Pitfall | Mitigation |
|---|---|
| `send-keys` interpreting `$`, `{`, `#`, "Enter" inside literal text | Always `-l` for literal text; named keys in separate call. |
| Multi-line paste collapses to one line | Run our tmux server with `extended-keys-format` unset. |
| Default 80×24 pane reflows TUI | `new-session -x 200 -y 50` + `resize-window -A` to lock. |
| Client attaching changes pane size | Use `resize-window -A` for sticky size. |
| `~/.tmux.conf` interferes | `-f /dev/null` and our explicit `set -g` lines. |
| Decoding `%output` payloads | Octal-unescape `\NNN` and `\134` for backslash before feeding pyte. |
| `pipe-pane -o` toggle in scripts | Always explicit start/stop, never toggle. |
| Echo race (our input shows up in output stream) | We track `send_text` calls and filter the echoed bytes by offset, or set `stty -echo` for non-TUI commands. |
| Sending before TUI is ready | Sentinel-wait; never sleep arbitrarily. |
| TUI uses alternate screen → scrollback lost | Pass `--no-alt-screen` to codex; Claude Code already uses primary screen for most output. |
| Subprocess fork overhead for `capture-pane` polling | Don't poll — use `%output`. `capture-pane` only for on-demand snapshots. |

## When the driver gives up

- Cannot reach tmux binary → `TmuxUnavailable` exception; engine offers `direct` driver instead.
- Cannot create socket → permission / path error, surfaced with the actual error.
- Pane dies unexpectedly → `Stopped(reason="pane_dead", exit_code=...)` event; transcript is preserved.
- Approval-prompt detection fails (modal moved) → falls through to a generic `LineRevised` event so the caller still sees something happened. We log a warning and the test suite catches the regression next CI run.
