# Agent Instructions

## Runtime Model

yikes! has two user-facing runtime axes:

- **Location** defines where the backend runs: `host`, `docker`, or future `remote`.
- **Driver** defines how yikes! drives that backend: `cli`, `tmux`, or future `api`.

Do not collapse these concepts back into one flat mode selector. Docker and tmux are not mutually exclusive; Docker is a location and tmux is a driver/transport.

## tmux Enforcement

When the selected driver is `tmux`, yikes! must run the real interactive backend UI.

Required:

- Claude tmux sessions start `claude` as an interactive UI.
- Codex tmux sessions start `codex` as an interactive UI.
- `host + tmux` runs the interactive UI in host tmux.
- `docker + tmux` runs the interactive UI in tmux inside the container.
- Overtake/attach must attach to that same tmux session.

Forbidden in any tmux path:

- `claude -p`
- `claude --print`
- `codex exec`
- `codex e`
- Any other headless one-shot command used as a substitute for the interactive TUI.

Headless commands belong only to non-tmux `cli`/direct paths.

## tmux Prompt Behavior

When driving an interactive tmux session, act like a normal human user inside
that existing terminal. A normal user does not paste a full system prompt before
every utterance.

Required:

- Treat tmux session history as durable context.
- Send the full behavioral/setup prompt only when creating or reestablishing a
  session, or when the session has had enough intense activity that context
  drift is a real risk.
- For ordinary turns, send only the user's actual message. Clean answer
  extraction may rely on a per-session capture agreement established earlier;
  do not restate it on every turn.
- Support raw interactive sessions where managed answer capture is disabled.
  In that mode, paste only user input and let the native TUI own the screen.
- Never mention yikes! inside prompts sent to Claude or Codex.
- Use a local per-user prompt profile for setup and extraction wording so
  repeated terminal interactions remain natural and do not read like one fixed
  template.
- Store generated prompt profiles in local user state only. They must not be
  committed to the repository.
- Keep result extraction clean and neutral, with stable meaning across wording
  variants.

## Developer tmux Tracing

Developer-mode tmux I/O tracing is opt-in and must stay disabled by default.
When `YIKES_DEVELOPER_MODE=1` or `YIKES_TMUX_IO_LOG=1` is set, every tmux
paste, key, resize, and capture boundary should be logged through the bounded
JSONL ring buffer in `yikes.tmux_io_log`; do not create ad-hoc debug files.

## Web UI Dialogs

When building yikes! as a llming-stage web app, dialogs and confirmations must
use Quasar dialog components or an app-owned dialog component. Do not use native
browser dialogs such as `alert`, `confirm`, or `prompt`; they are not themeable,
not test-friendly, and do not fit the llming-stage/Quasar interaction model.
