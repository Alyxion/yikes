# Claude Project Instructions

## Runtime Model

yikes! separates **where** a backend runs from **how** it is driven:

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

## tmux Prompt Behavior

When Claude or Codex is driven through tmux, behave like a normal user typing
into a durable interactive terminal. Do not paste the full setup prompt before
every ordinary turn.

Required:

- Rely on the persistent tmux session context after the initial setup.
- Re-send broader guidance only when creating/reestablishing a session or after
  enough intense activity that context drift is likely.
- For normal turns, send only the user's real message. Clean answer extraction
  may rely on a per-session capture agreement established earlier; do not
  restate it on every turn.
- Support raw interactive sessions where managed answer capture is disabled.
  In that mode, paste only user input and let the native TUI own the screen.
- Never mention yikes! inside prompts sent to Claude or Codex.
- Use local per-user prompt profiles for setup and extraction wording so
  repeated terminal interactions remain natural and do not read like one fixed
  template.
- Generated prompt profiles belong in local user state only and must not be
  committed.
