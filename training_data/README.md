# Training data — terminal activity states

Labeled snapshots of real Claude Code / Codex CLI terminal output, used to
improve and regression-test the activity detection in [`yikes/activity.py`](../yikes/activity.py)
(the heuristic that decides whether a session is `idle`, `thinking`,
`streaming`, or `awaiting-selection`). When yikes misreads a state, capture the
moment here so the detector can be tuned against ground truth.

## How to add a sample

**Web UI (easiest):** while viewing a session, click the **◉** button in the top
bar or press **Ctrl+Alt+L**, then pick the true state. The live terminal is
captured server-side at that instant — ideal for transient states.

**CLI:** while a session is in a known state (the `capture` command is
intentionally hidden from `yikes --help`):

```bash
yikes capture awaiting-selection          # auto-picks the session for this dir
yikes capture streaming <session-id>      # or name a session explicitly
yikes capture idle --notes "post-turn prompt"
```

Each invocation grabs **4 rapid full-fidelity snapshots over ~0.5s** (so spinner
animations are captured, not a single still frame), preserving the raw ANSI —
colours, cursor moves, everything tmux emits with `capture-pane -e`.

## Layout

```
training_data/samples/<backend>/<timestamp>__<label>__<session>/
  meta.json        # label, yikes' prediction, versions, terminal size, …
  frame-1.ansi     # raw terminal capture (with colour/escape codes)
  …  frame-4.ansi
```

## `meta.json` fields

- `label` — the true state (ground truth you provided).
- `predicted` / `predicted_matches_label` — what `yikes.activity` inferred, so
  mislabels are easy to find (`predicted_matches_label == false`).
- `backend` + `backend_version` — which CLI and version produced the output, so
  samples can be retired once a backend changes its rendering.
- `tmux_version`, `location` (`host`/`docker`), `driver`, `terminal` size.
- `captured_at` (UTC), `frame_count`, `frame_interval_ms`, `notes`.

Samples are committed deliberately — this directory *is* the dataset.
