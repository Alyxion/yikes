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
