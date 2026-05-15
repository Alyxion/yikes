# Claude Project Instructions

## Runtime Model

Yikes separates **where** a backend runs from **how** it is driven:

- Location: `host`, `docker`, future `remote`.
- Driver: `cli`, `tmux`, future `api`.

Keep this split intact. Do not reintroduce a single ambiguous mode menu where `docker` and `tmux` compete with each other. Docker is the execution location; tmux is the interactive UI transport.

## Hard Rule For tmux

tmux always means a real interactive UI session.

Allowed tmux behavior:

- `host + tmux`: start the real Claude/Codex interactive UI in host tmux.
- `docker + tmux`: start the real Claude/Codex interactive UI in tmux inside the container.
- `yikes attach` must overtake the same tmux session.

Never use these inside a tmux path:

- `claude -p`
- `claude --print`
- `codex exec`
- `codex e`
- Any headless one-shot command pretending to be an interactive session.

Those commands are only valid for non-tmux `cli`/direct paths.
