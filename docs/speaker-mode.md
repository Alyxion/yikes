# Speaker Mode

Speaker mode gives the web UI a **voice**. Turn it on for a session tab and yikes!
watches that session in the background; when the agent finishes something,
asks a question, or hits an error, a short spoken summary plays in your browser —
so you can step away from the screen and still know when an agent needs you.

It is built to be **quiet and cheap**. Nothing is spoken while an agent is still
working, the same screen is never summarized twice, and the model calls are gated
behind a hard set of cost guards (below). With no session enabled it costs
nothing at all.

---

## Turning it on

Each session tab has a **🔊 speaker button** in the top bar.

- Click it to enable spoken summaries **for that tab**. The icon turns green
  (`🔊`); muted is `🔈`. The toggle is per session, so you can narrate one tab
  and leave the others silent.
- Click it again to mute the tab. Muting also stops any line currently playing.
- The **⚙ button** next to it opens the settings window.
- A **🔇 Stop speaking** button appears while a line is playing — click it, or
  press **Esc**, to cut the current utterance without disabling the tab.

The button is disabled when no session is selected, or when no Claude/OpenAI key
is available (hover for the reason).

---

## How it decides when to speak

The pipeline runs entirely in the background, per enabled session:

1. **Settle detection (free).** A watcher polls the session's terminal snapshot
   and feeds it to the same [`ActivityMonitor`](architecture.md) the UI uses for
   the activity pill. Speaker mode only acts on an **active → settled edge** —
   the screen went from `thinking`/`streaming` to stable (the "nothing really
   changes anymore" check). Nothing is ever spoken while the agent is working.
2. **Fast gate (cheap model).** On a genuine settle, a small, fast model
   (Claude Haiku or an OpenAI mini model) looks at *what changed* and returns a
   strict JSON verdict: should we speak, a one-sentence draft (≤ ~15 s of
   speech), whether it is a question, and whether a stronger model should reword
   it. If the verdict is "nothing worth saying," the pipeline stops here — no
   speech, no further cost.
3. **Optional upgrade.** Only when the gate sets `needs_complex` (and the
   *Upgrade wording* setting is on) does a stronger model rewrite the sentence.
4. **Speech.** The text is spoken — see [Voice engines](#voice-engines).

### Cost guards

The "**absolutely must not run repeatedly**" requirement is enforced by several
independent guards:

| Guard | What it prevents |
| --- | --- |
| **Edge-triggered** | Speaking while the agent is mid-task. We only consider speaking on an active→settled transition. |
| **Settle confirmation** | Acting on a half-rendered frame. The snapshot must be byte-identical for `settle_polls` consecutive polls. |
| **Digest dedupe** | Re-spending on a screen we already handled. Each settled screen is evaluated **at most once** — even if the gate stayed silent. |
| **Cooldown** | Rapid-fire chatter. A hard minimum interval (`cooldown_seconds`) between spoken lines per session. |
| **Single-flight** | Two summaries in flight for one session. The watcher awaits each utterance inline. |
| **Baseline on enable** | Narrating the backlog already on screen. Enabling adopts the current screen as the baseline; only *future* changes are spoken. |
| **Circuit breaker** | Runaway loops. If utterances exceed `max_per_minute`, the tab's speaker mode is force-disabled and you are told. |

Token usage is also bounded: the changed text is truncated to `max_chars`, and
model replies are capped to a short sentence.

---

## Voice engines

Speaker mode produces the **text** with an LLM, then speaks it with one of two
engines:

- **OpenAI TTS** — used by default when an OpenAI key is available. The server
  synthesizes audio (`gpt-4o-mini-tts` by default) and the browser plays it.
- **Browser voice** — the browser's built-in `speechSynthesis`. Free, instant
  to stop, and the fallback whenever OpenAI audio is unavailable (e.g. a
  Claude-only setup, since Anthropic has no TTS API).

The `Voice engine` setting is `auto` (OpenAI if a key is present, else browser),
`openai`, or `browser`. While the agent is speaking, a floating **waveform
visualizer** (with the spoken text and a stop button) appears at the bottom of
the screen; press its **✕** or **Esc** to cut the current line.

---

## Providers and keys

Speaker mode reuses yikes!'s credential resolution and works with **either**
provider:

- **Anthropic** — `ANTHROPIC_API_KEY`, or the Claude Code login token from the OS
  credential store. Used for the fast/complex models (`claude-haiku-4-5` /
  `claude-sonnet-4-6` by default).
- **OpenAI** — `OPENAI_API_KEY` / `CODEX_API_KEY` in the environment, the project
  `.env`, or `~/.codex/auth.json`. Used for the fast/complex models
  (`gpt-4o-mini` / `gpt-4o`) **and** for TTS.

The `Model provider` setting is `auto` (prefer OpenAI when present, else
Anthropic), `anthropic`, or `openai`. The settings window shows which keys were
detected and which engine will actually speak. No key value is ever persisted.

---

## Settings window

The ⚙ window exposes the common knobs; everything else has a sensible default.
Settings persist to `~/.config/yikes/speaker.json` (override with
`YIKES_SPEAKER_CONFIG`).

| Setting | Default | Meaning |
| --- | --- | --- |
| `Volume` (`volume`) | 0.8 | 0–100 %, applied to both OpenAI audio and the browser voice |
| `Voice engine` (`tts_engine`) | `auto` | `auto` · `openai` · `browser` |
| `Model provider` (`llm_provider`) | `auto` | `auto` · `anthropic` · `openai` |
| `OpenAI voice` (`voice`) | `alloy` | OpenAI TTS voice |
| `Upgrade wording` (`use_complex`) | on | Allow the stronger model to reword a flagged line |
| `Max words` (`max_words`) | 60 | Spoken length budget (~15–20 s); the model prefers a complete sentence over a clipped one |
| `Cooldown (s)` (`cooldown_seconds`) | 10 | Minimum gap between spoken lines per session |

Advanced fields in the JSON file (not shown in the window): `poll_interval`,
`settle_polls`, `max_chars`, `max_per_minute`, `rate`, and the per-provider model
names (`fast_model_anthropic`, `complex_model_anthropic`, `fast_model_openai`,
`complex_model_openai`, `tts_model`).

---

---

## Talking back: voice input

Speaker mode is one half of hands-free use; the other is **talking to the
agent**. A draggable **🗣 push-to-talk button** floats over the live terminal:

- **Push to talk** — *hold* the button and speak; *release* to finish. Drag it
  with your thumb to reposition it (its spot is remembered, and it is always
  kept on-screen even after a window resize); **starting a drag cancels** the
  current recording. While the panel is open the talk button moves *into* the
  panel ("Hold to talk") and the floating button is hidden.
- **The intent is detected for you.** On release, a fast model decides whether
  you were **dictating** text or giving a **control command**, then acts:
  - *Dictation* → the words become a removable chip in the panel. Record several
    (hold again), drop any with its **×**, then **Send ⏎** types them into the
    prompt **and confirms with Enter**.
  - *Command* → it issues the keystroke directly: "accept"/"confirm"/"yes" →
    Enter; "option two" / "number three" / "1"–"9" → that menu option;
    "cancel"/"escape" → Escape. (There are no manual confirm buttons — say it.)
- **Auto-send after release** — tick this in the panel to skip the chips: each
  dictated utterance is typed into the prompt and submitted as soon as you
  release.

Keystrokes are injected into the live tmux pane by the server (the same channel
as typing), so it works for Claude and Codex **whether or not** a browser
terminal is attached. For a non-interactive chat session, dictation fills the
composer instead.

**Speech-to-text** uses **OpenAI transcription** (`gpt-4o-transcribe`) when an
OpenAI key is present — it records your held audio and transcribes it
server-side, which is far more reliable on short utterances than the browser's
own recognizer. Without an OpenAI key it falls back to the browser's built-in
`SpeechRecognition` (Chrome). The `Mic (input)` setting (`auto` · `openai` ·
`browser`) controls this. Intent routing uses the same Claude/OpenAI key as
speaker mode (with an offline keyword fallback).

While you hold the button a **live waveform of your voice** is drawn in the
panel (real audio when using OpenAI/mic capture); while the agent speaks, its
own waveform is shown in the bottom bar (a real waveform for OpenAI audio, an
animated one for the browser voice).

The push-to-talk button is built on a small reusable **`FlyingButton`** base
(draggable, press-vs-drag aware, position persisted) so other floating controls
can reuse it.

---

## Notes

- Speaker mode and voice input are **web UI** features; they have no effect on
  the terminal app or the Python API.
- Enablement is per session and resets when the web server restarts; the
  settings (above) persist.
- If several browser tabs are connected, each speaks the event independently —
  use the per-tab mute or **Esc** to silence the one you are looking at.
