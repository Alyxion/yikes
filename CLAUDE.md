# Claude Project Instructions

## Docs And README Sync

Documentation must never drift from the code.

Required:

- Whenever you change user-facing behavior, flags, commands, defaults, or
  helper scripts, update the docs under `docs/` and `README.md` in the same
  change. Code and docs land together; do not leave a follow-up.
- `docs/` holds the full detail. Each public surface (CLI flags, Python API,
  runtime model, install steps) must be documented there.
- `README.md` stays compact. It is the entry point, not the manual: short
  feature list, quick start, common commands, and pointers into `docs/`. Add
  only the few lines a newcomer needs and link to `docs/` for depth rather than
  expanding the README.
- When in doubt, put detail in `docs/` and a one-line summary in `README.md`.

## Restart Long-Running Services After Major Changes

The web UI (`yikes web` → `python -m yikes.web_server`) is a persistent process
with no hot-reload: it serves whatever code was imported when it started, so it
keeps running stale code until restarted.

Required:

- After landing a major change (anything touching the web server, services,
  drivers, session model, or shared modules it imports), restart any running
  yikes web server so it serves current code. Do not leave a stale server up.
- `yikes web` will NOT restart a live port — it only starts when the port is
  free. To restart: kill the `yikes-web-<port>` tmux session (or the detached
  `python -m yikes.web_server` for that port), confirm the port is free, then
  relaunch on the same host/port. Keep it durable (a detached
  `tmux new-session -d -s yikes-web-<port> ...`) so it survives the shell.
- Only restart servers that are actually running; never start one that the user
  did not already have up.

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
