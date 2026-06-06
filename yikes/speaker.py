"""Speaker mode: spoken, per-session summaries of what an agent just did.

When speaker mode is enabled for a session tab, a lightweight background watcher
polls that session's terminal and waits for it to *settle* — the screen stops
changing (the "nothing really changes anymore" check, reusing
:class:`~yikes.activity.ActivityMonitor`). Only on a genuine active→settled edge
does it spend any money: a fast model decides whether anything is worth saying,
optionally a stronger model rewords it, and the line is spoken in the browser
(OpenAI audio when available, otherwise the browser's own voice).

The whole point is to be cheap and quiet. The guards, in order, are:

* **Edge-triggered** — we only consider speaking when the session goes from
  active (thinking/streaming) to settled. We never speak while it is working.
* **Settle confirmation** — the snapshot must be byte-identical for a few polls.
* **Dedupe by digest** — a settled screen is summarized at most once; we never
  re-spend on a screen we already evaluated (even if the gate stayed silent).
* **Cooldown** — a hard minimum interval between spoken lines per session.
* **Single-flight** — the watcher awaits each utterance inline, so a session
  can never have two summaries in flight.
* **Circuit breaker** — if utterances exceed a per-minute cap, the session's
  speaker mode is force-disabled and the user is told.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from .activity import AWAITING_SELECTION, STREAMING, THINKING, ActivityMonitor, _normalize
from .speaker_llm import SpeakerLLM, VoiceAction

_ACCEPT_WORDS = ("accept", "confirm", "yes", "yeah", "yep", "okay", "ok", "enter", "proceed", "approve")
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}


def _local_interpret(transcript: str) -> VoiceAction:
    """Offline fallback for voice-intent routing when no LLM key is available."""
    lower = re.sub(r"[.!?,]", " ", transcript.lower()).strip()
    tokens = lower.split()
    for token in tokens:
        if token.isdigit() and 1 <= int(token) <= 9:
            return VoiceAction("command", action="select", value=int(token))
        if token in _NUMBER_WORDS:
            return VoiceAction("command", action="select", value=_NUMBER_WORDS[token])
    if re.search(r"\b(escape|cancel|dismiss|abort)\b", lower) or lower in {"never mind", "nevermind"}:
        return VoiceAction("command", action="escape")
    if any(token in _ACCEPT_WORDS for token in tokens) or lower in {"go ahead", "do it", "sounds good"}:
        return VoiceAction("command", action="accept")
    return VoiceAction("dictate", text=transcript.strip())

SnapshotFn = Callable[[str], str | None]
PushFn = Callable[[dict[str, Any]], Awaitable[None]]

_TTS_ENGINES = ("auto", "openai", "browser")
_STT_ENGINES = ("auto", "openai", "browser")
_LLM_PROVIDERS = ("auto", "anthropic", "openai")


def default_config_path() -> Path:
    override = os.environ.get("YIKES_SPEAKER_CONFIG")
    if override:
        return Path(override).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "yikes" / "speaker.json"


@dataclass
class SpeakerConfig:
    """User-tunable speaker settings (persisted; no secrets)."""

    tts_engine: str = "auto"          # auto | openai | browser
    llm_provider: str = "auto"        # auto | anthropic | openai
    voice: str = "alloy"
    rate: float = 1.0                 # browser speechSynthesis rate
    volume: float = 0.8               # 0.0–1.0, applied to audio and browser speech
    max_words: int = 60               # spoken length budget (~15-20 seconds)
    use_complex: bool = True          # allow upgrade to the stronger model
    cooldown_seconds: float = 10.0
    poll_interval: float = 1.2
    settle_polls: int = 2
    max_chars: int = 6000
    max_per_minute: int = 5
    fast_model_anthropic: str = "claude-haiku-4-5"
    complex_model_anthropic: str = "claude-sonnet-4-6"
    fast_model_openai: str = "gpt-4o-mini"
    complex_model_openai: str = "gpt-4o"
    tts_model: str = "gpt-4o-mini-tts"
    stt_engine: str = "auto"          # auto | openai | browser
    stt_model: str = "gpt-4o-transcribe"

    # Fields the config window may set, with light validation.
    _BOOL = {"use_complex"}
    _FLOAT = {"rate", "volume", "cooldown_seconds", "poll_interval"}
    _INT = {"max_words", "settle_polls", "max_chars", "max_per_minute"}
    _CHOICES = {"tts_engine": _TTS_ENGINES, "llm_provider": _LLM_PROVIDERS, "stt_engine": _STT_ENGINES}
    _STR = {
        "voice",
        "fast_model_anthropic",
        "complex_model_anthropic",
        "fast_model_openai",
        "complex_model_openai",
        "tts_model",
        "stt_model",
    }

    def to_json(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if not key.startswith("_")}

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "SpeakerConfig":
        config = cls()
        if isinstance(payload, dict):
            config = config.apply(payload)
        return config

    def apply(self, changes: dict[str, Any]) -> "SpeakerConfig":
        updates: dict[str, Any] = {}
        for key, value in changes.items():
            if key in self._CHOICES:
                text = str(value).strip().lower()
                if text in self._CHOICES[key]:
                    updates[key] = text
            elif key in self._BOOL:
                updates[key] = value if isinstance(value, bool) else str(value).lower() in {"1", "true", "on", "yes"}
            elif key in self._FLOAT:
                try:
                    updates[key] = float(value)
                except (TypeError, ValueError):
                    continue
            elif key in self._INT:
                try:
                    updates[key] = int(value)
                except (TypeError, ValueError):
                    continue
            elif key in self._STR:
                text = str(value).strip()
                if text:
                    updates[key] = text
        merged = replace(self, **updates)
        return merged.clamped()

    def clamped(self) -> "SpeakerConfig":
        return replace(
            self,
            rate=_clamp(self.rate, 0.5, 2.0),
            volume=_clamp(self.volume, 0.0, 1.0),
            cooldown_seconds=_clamp(self.cooldown_seconds, 0.0, 600.0),
            poll_interval=_clamp(self.poll_interval, 0.4, 30.0),
            max_words=int(_clamp(self.max_words, 5, 150)),
            settle_polls=int(_clamp(self.settle_polls, 1, 10)),
            max_chars=int(_clamp(self.max_chars, 500, 40000)),
            max_per_minute=int(_clamp(self.max_per_minute, 1, 60)),
        )

    @classmethod
    def load(cls, path: Path | None = None) -> "SpeakerConfig":
        config_path = path or default_config_path()
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return cls()
        return cls.from_json(payload)

    def save(self, path: Path | None = None) -> None:
        config_path = path or default_config_path()
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = config_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(config_path)
        except OSError:
            pass


def _clamp(value: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


def _digest(snapshot: str) -> str:
    return hashlib.sha256(_normalize(snapshot).encode("utf-8")).hexdigest()


def _delta_text(previous: str, current: str) -> str:
    """Best-effort 'what changed' text since the last spoken snapshot."""
    if not previous:
        return current
    if current.startswith(previous):
        return current[len(previous):].strip() or current
    prev_lines = previous.splitlines()
    cur_lines = current.splitlines()
    prev_set = set(prev_lines)
    added = [line for line in cur_lines if line.strip() and line not in prev_set]
    return "\n".join(added).strip() or current


@dataclass
class _WatchState:
    armed: bool = False
    prev_digest: str = ""
    stable_count: int = 0
    last_spoken_digest: str = ""
    last_spoken_text: str = ""
    last_spoken_at: float = -1.0e9
    baseline_set: bool = False


class ConnectionHub:
    """A single browser websocket with serialized, failure-tolerant sends."""

    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket
        self.alive = True
        self._lock = asyncio.Lock()

    async def send_json(self, message: dict[str, Any]) -> None:
        if not self.alive:
            return
        async with self._lock:
            try:
                await self.websocket.send_json(message)
            except Exception:
                self.alive = False


class SpeakerService:
    """Owns speaker-mode watchers, settings, and the browser broadcast."""

    def __init__(
        self,
        controller: Any,
        *,
        config: SpeakerConfig | None = None,
        config_path: Path | None = None,
        llm: SpeakerLLM | None = None,
        snapshot_fn: SnapshotFn | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._controller = controller
        self._config_path = config_path
        self.config = config or SpeakerConfig.load(config_path)
        self._llm = llm
        self._snapshot_fn = snapshot_fn or self._default_snapshot
        self._clock = clock
        self._connections: set[ConnectionHub] = set()
        self._enabled: set[str] = set()
        self._tasks: dict[str, asyncio.Task] = {}
        self._watch: dict[str, _WatchState] = {}
        self._spoken_at: dict[str, list[float]] = {}
        self._last_error: str | None = None
        self._last_spoken: str | None = None

    # -- credentials / status -------------------------------------------

    def _ensure_llm(self) -> SpeakerLLM:
        if self._llm is None:
            cwd = getattr(self._controller, "start_cwd", None)
            self._llm = SpeakerLLM(cwd)
        return self._llm

    def _tts_uses_openai(self) -> bool:
        if self.config.tts_engine == "browser":
            return False
        has_openai = self._ensure_llm().has_openai()
        if self.config.tts_engine == "openai":
            return has_openai
        return has_openai  # auto

    def public_state(self) -> dict[str, Any]:
        llm = self._ensure_llm()
        return {
            "enabled_sessions": sorted(self._enabled),
            "config": self.config.to_json(),
            "providers": llm.available_providers(),
            "tts_active": "openai" if self._tts_uses_openai() else "browser",
            "stt_active": "openai" if self._stt_uses_openai() else "browser",
            "available": llm.has_any_provider(),
            "error": self._last_error,
            "last_spoken": self._last_spoken,
        }

    # -- connection registry --------------------------------------------

    def register_connection(self, hub: ConnectionHub) -> None:
        self._connections.add(hub)

    def unregister_connection(self, hub: ConnectionHub) -> None:
        self._connections.discard(hub)

    async def _broadcast(self, message: dict[str, Any]) -> None:
        for hub in list(self._connections):
            await hub.send_json(message)
            if not hub.alive:
                self._connections.discard(hub)

    # -- enable / disable / config --------------------------------------

    def set_enabled(self, session_id: str, enabled: bool) -> None:
        session_id = (session_id or "").strip()
        if not session_id:
            return
        if enabled:
            self._ensure_llm().refresh_keys()
            self._enabled.add(session_id)
            self._watch[session_id] = _WatchState()
            if session_id not in self._tasks or self._tasks[session_id].done():
                self._tasks[session_id] = asyncio.create_task(self._watch_session(session_id))
        else:
            self._enabled.discard(session_id)
            task = self._tasks.pop(session_id, None)
            if task is not None and not task.done():
                task.cancel()
            self._watch.pop(session_id, None)

    def _stt_uses_openai(self) -> bool:
        if self.config.stt_engine == "browser":
            return False
        has_openai = self._ensure_llm().has_openai()
        return has_openai  # auto / openai

    async def transcribe_and_interpret(self, audio_b64: str, mime: str) -> dict[str, Any]:
        """Whisper-transcribe a recording, then route it (dictate vs command)."""
        llm = self._ensure_llm()
        if not llm.has_openai():
            return {"transcript": "", "error": "No OpenAI key for speech-to-text.", **_local_interpret("").as_dict()}
        result = await llm.transcribe(audio_b64, mime, config=self.config)
        if result.error:
            self._last_error = result.error
            return {"transcript": "", "error": result.error, **_local_interpret("").as_dict()}
        transcript = result.text.strip()
        action = await self.interpret_voice(transcript)
        return {"transcript": transcript, **action}

    async def interpret_voice(self, transcript: str) -> dict[str, Any]:
        """Route a spoken utterance to dictation text or a control command."""
        transcript = (transcript or "").strip()
        if not transcript:
            return {"mode": "dictate", "text": ""}
        llm = self._ensure_llm()
        if llm.has_any_provider():
            action = await llm.interpret(transcript, config=self.config)
            if action.error is None:
                return action.as_dict()
            self._last_error = action.error
        return _local_interpret(transcript).as_dict()

    def update_config(self, **changes: Any) -> None:
        self.config = self.config.apply(changes)
        self.config.save(self._config_path)
        self._ensure_llm().refresh_keys()

    def stop_all(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._enabled.clear()
        self._watch.clear()

    # -- snapshot source -------------------------------------------------

    def _default_snapshot(self, session_id: str) -> str | None:
        try:
            snapshot = self._controller.lifecycle.snapshot(session_id, lines=200)
        except Exception:
            snapshot = None
        if snapshot:
            return snapshot
        lines = getattr(self._controller, "session_lines", {}).get(session_id)
        if lines:
            return "\n".join(lines[-200:])
        return None

    # -- the watcher loop ------------------------------------------------

    async def _watch_session(self, session_id: str) -> None:
        monitor = ActivityMonitor()
        try:
            while session_id in self._enabled:
                await asyncio.sleep(max(0.4, float(self.config.poll_interval)))
                if session_id not in self._enabled:
                    break
                snapshot = await asyncio.to_thread(self._snapshot_fn, session_id)
                if not snapshot:
                    continue
                await self._step(session_id, snapshot, monitor)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let a watcher die silently mid-feature
            self._last_error = f"speaker watcher error: {exc}"

    async def _step(self, session_id: str, snapshot: str, monitor: ActivityMonitor) -> None:
        state = self._watch.setdefault(session_id, _WatchState())
        activity = monitor.observe(session_id, snapshot)
        digest = _digest(snapshot)

        # First observation after enabling: adopt the current screen as the
        # baseline so we narrate only *future* changes, not the backlog.
        if not state.baseline_set:
            state.baseline_set = True
            state.prev_digest = digest
            state.last_spoken_digest = digest
            return

        # Arm ONLY when the agent is genuinely working (the "esc to interrupt"
        # THINKING indicator). Plain screen changes — most importantly the user
        # typing a new prompt — read as STREAMING and must NOT arm, or speaker
        # mode would narrate the user's own input back at them.
        if activity.state == THINKING:
            state.armed = True
        if activity.state in (THINKING, STREAMING):
            state.stable_count = 0
            state.prev_digest = digest
            return

        if digest == state.prev_digest:
            state.stable_count += 1
        else:
            state.stable_count = 1
            state.prev_digest = digest

        if not state.armed or state.stable_count < max(1, int(self.config.settle_polls)):
            return
        if digest == state.last_spoken_digest:
            state.armed = False  # already handled this settled screen
            return
        now = self._clock()
        if now - state.last_spoken_at < float(self.config.cooldown_seconds):
            return  # stay armed; speak once the cooldown elapses

        # Commit to handling this screen exactly once, regardless of outcome,
        # so a "nothing to say" verdict never re-spends on the same screen.
        spoke = await self._speak(session_id, snapshot, state.last_spoken_text, activity)
        state.last_spoken_digest = digest
        state.last_spoken_text = snapshot
        state.armed = False
        if spoke:
            state.last_spoken_at = self._clock()
            if self._trips_breaker(session_id):
                self.set_enabled(session_id, False)
                await self._broadcast(
                    {
                        "type": "speaker.notice",
                        "session_id": session_id,
                        "message": "Speaker mode paused for this tab (too many updates).",
                    }
                )

    async def _speak(self, session_id: str, snapshot: str, prev_text: str, activity: Any) -> bool:
        llm = self._ensure_llm()
        if not llm.has_any_provider():
            self._last_error = "No Claude or OpenAI key available for speaker mode."
            return False
        context = (_delta_text(prev_text, snapshot) or snapshot)[-int(self.config.max_chars):]
        awaiting = activity.state == AWAITING_SELECTION
        decision = await llm.decide(context, awaiting=awaiting, config=self.config)
        if decision.error:
            self._last_error = decision.error
            return False
        if not decision.speak or not decision.utterance.strip():
            return False
        utterance = decision.utterance.strip()
        if decision.needs_complex and self.config.use_complex:
            better = await llm.elaborate(context, utterance, config=self.config)
            if not better.error and better.text.strip():
                utterance = better.text.strip()

        audio_b64: str | None = None
        mime: str | None = None
        if self._tts_uses_openai():
            audio = await llm.synthesize(utterance, config=self.config)
            if not audio.error and audio.b64:
                audio_b64, mime = audio.b64, audio.mime
            elif audio.error:
                self._last_error = audio.error  # fall back to browser speech

        self._last_error = None
        self._last_spoken = utterance
        await self._broadcast(
            {
                "type": "speaker.say",
                "session_id": session_id,
                "text": utterance,
                "is_question": decision.is_question,
                "audio": audio_b64,
                "mime": mime,
                "voice": self.config.voice,
                "rate": self.config.rate,
                "volume": self.config.volume,
            }
        )
        return True

    def _trips_breaker(self, session_id: str) -> bool:
        now = self._clock()
        stamps = [t for t in self._spoken_at.get(session_id, []) if now - t < 60.0]
        stamps.append(now)
        self._spoken_at[session_id] = stamps
        return len(stamps) > int(self.config.max_per_minute)
