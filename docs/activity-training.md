# Activity detection & training capture

yikes! infers a live session's **activity state** from its terminal output so the
terminal app and web UI can show whether an agent is `idle`, `thinking`,
`streaming`, or `awaiting-selection`. The classifier lives in
[`yikes/activity.py`](python-library.md) and is heuristic: it looks for the
backend's active-run markers (e.g. an "esc to interrupt" spinner line) and
single-digit selection menus.

Heuristics drift — a backend changes its spinner, a new prompt style appears —
and the state is sometimes misread. The **training capture** tool lets you record
ground truth for those moments so the detector can be tuned and regression-tested
against real output.

## States

| State | Meaning |
| --- | --- |
| `idle` | Prompt is ready, nothing running |
| `thinking` | An active run is in progress (spinner / "esc to interrupt") |
| `streaming` | Output is actively changing |
| `awaiting-selection` | A choice/approval menu is waiting for input |
| `unknown` | Could not be determined |

## Capturing a labeled sample

### From the web UI (recommended)

This is the easiest way to catch a misread *as it happens*. The label affordance
is **developer-only** — start the server with `yikes web --dev` (or set
`YIKES_WEB_DEV=1` / `YIKES_DEVELOPER_MODE=1`); it is hidden for normal users.
In developer mode, while viewing a session, click the **◉** button in the top
bar (next to the view toggle) or press **Ctrl+Alt+L**. A small picker appears —
choose the true state (with an optional note) and yikes captures the live
terminal server-side immediately, so transient states like a flickering
selection prompt are caught at the right moment. A toast reports what was saved
and whether yikes' own guess matched.

Each session tab and the activity pill also carry a **per-state icon**
(💤 idle · ✋ awaiting-selection · 🤔 thinking · ✍️ streaming · ◌ unknown) so it is
obvious at a glance which sessions are waiting on you.

### From the CLI

The `capture` command is intentionally **hidden** from `yikes --help` — a
developer aid, not part of the everyday surface. While a session is in a state
you can name with certainty:

```bash
yikes capture awaiting-selection            # auto-picks the running session for this dir
yikes capture streaming <session-id|name>   # or target a session explicitly
yikes capture idle --notes "post-turn prompt"
yikes capture thinking --frames 6 --span 0.8 # tune snapshot count / time window
```

Each run grabs **4 rapid full-fidelity snapshots over ~0.5s** by default
(`tmux capture-pane -e`, preserving colours and escape codes), so spinner
*animation* is captured rather than a single still frame. It also records yikes'
own prediction, so mismatches are easy to mine.

## Where samples go

Samples are written to the committed [`training_data/`](https://github.com/Alyxion/yikes/tree/main/training_data)
directory (override with `YIKES_TRAINING_DIR`):

```
training_data/samples/<backend>/<timestamp>__<label>__<session>/
  meta.json        # label, prediction, backend+tmux version, terminal size, …
  frame-1.ansi …   # raw terminal captures (with colour/escape codes)
```

`meta.json` stamps the **backend version** (`claude --version` / `codex --version`)
and a UTC timestamp, so samples can be retired once a CLI changes how it renders.
`predicted_matches_label: false` flags the cases the current heuristic gets wrong
— the working set for improving detection. See
[`training_data/README.md`](https://github.com/Alyxion/yikes/blob/main/training_data/README.md)
for the full field list.
