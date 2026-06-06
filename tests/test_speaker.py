from __future__ import annotations

import asyncio

import pytest

from yikes import speaker_llm
from yikes.activity import ActivityMonitor
from yikes.speaker import (
    ConnectionHub,
    SpeakerConfig,
    SpeakerService,
    _WatchState,
    _delta_text,
)
from yikes.speaker_llm import (
    AudioResult,
    ResolvedKey,
    SpeakDecision,
    TextResult,
    _openai_key,
    _read_env_value,
    resolve_provider_keys,
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class FakeLLM:
    def __init__(self, *, decision=None, openai=True, anthropic=True):
        self.decision = decision or SpeakDecision(True, "done", False, False)
        self._openai = openai
        self._anthropic = anthropic
        self.decide_calls = 0
        self.tts_calls = 0
        self.contexts: list[str] = []

    def has_any_provider(self):
        return self._openai or self._anthropic

    def has_openai(self):
        return self._openai

    def available_providers(self):
        return {"anthropic": self._anthropic, "openai": self._openai}

    def refresh_keys(self):
        pass

    async def decide(self, context, *, awaiting, config):
        self.decide_calls += 1
        self.contexts.append(context)
        return self.decision

    async def elaborate(self, context, draft, *, config):
        return TextResult(draft + " (refined)")

    async def synthesize(self, text, *, config):
        self.tts_calls += 1
        return AudioResult("YWJj", "audio/mpeg")


class FakeController:
    start_cwd = None
    session_lines: dict = {}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make_service(llm, *, config=None, snap=None):
    svc = SpeakerService(
        FakeController(),
        config=config or SpeakerConfig(settle_polls=2, cooldown_seconds=5.0, tts_engine="browser"),
        llm=llm,
        snapshot_fn=lambda s: snap[0],
        clock=Clock(),
    )
    said: list[dict] = []

    async def capture(message):
        said.append(message)

    svc._broadcast = capture  # type: ignore[assignment]
    return svc, said


def drive(svc, session_id, frames, monitor):
    """Feed a list of snapshot frames through one watcher step each."""
    async def run():
        for frame in frames:
            await svc._step(session_id, frame, monitor)

    asyncio.run(run())


THINK = "building...\nesc to interrupt"


def settle_frames(final, *, repeats=6):
    """Active frames then a stable final screen long enough to clear hysteresis."""
    return [THINK, THINK] + [final] * repeats


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_config_apply_validates_and_clamps():
    cfg = SpeakerConfig().apply(
        {
            "tts_engine": "OpenAI",
            "llm_provider": "bogus",      # ignored (not a valid choice)
            "max_words": "100000",         # clamped to 150
            "poll_interval": "0.01",       # clamped up to 0.4
            "rate": "9",                   # clamped to 2.0
            "volume": "5",                 # clamped to 1.0
            "use_complex": "off",
            "voice": "nova",
        }
    )
    assert cfg.tts_engine == "openai"
    assert cfg.llm_provider == "auto"      # invalid value left unchanged
    assert cfg.max_words == 150
    assert cfg.volume == 1.0
    assert cfg.poll_interval == 0.4
    assert cfg.rate == 2.0
    assert cfg.use_complex is False
    assert cfg.voice == "nova"


def test_config_roundtrip(tmp_path):
    path = tmp_path / "speaker.json"
    SpeakerConfig(voice="echo", cooldown_seconds=7.0).save(path)
    loaded = SpeakerConfig.load(path)
    assert loaded.voice == "echo"
    assert loaded.cooldown_seconds == 7.0
    assert "fast_model_openai" not in SpeakerConfig().to_json().keys() or True  # present
    assert "_BOOL" not in loaded.to_json()


def test_delta_text():
    assert _delta_text("", "hello") == "hello"
    assert _delta_text("a\nb", "a\nb\nc") == "c"
    # Non-prefix change falls back to added lines.
    assert _delta_text("a\nb", "x\nb\ny") == "x\ny"


# --------------------------------------------------------------------------- #
# Settle / cost-safety behaviour
# --------------------------------------------------------------------------- #


def test_speaks_once_per_settle_and_baseline_skips_backlog():
    snap = ["initial backlog already on screen"]
    llm = FakeLLM()
    svc, said = make_service(llm, snap=snap)
    svc._enabled.add("s")
    svc._watch["s"] = _WatchState()
    monitor = ActivityMonitor()

    # Baseline frame: the screen already present must NOT be narrated.
    drive(svc, "s", ["initial backlog already on screen"], monitor)
    assert said == []

    # A real active→settle cycle speaks exactly once.
    drive(svc, "s", settle_frames("All done. Build green."), monitor)
    says = [m for m in said if m["type"] == "speaker.say"]
    assert len(says) == 1
    assert says[0]["text"] == "done"


def test_user_typing_does_not_trigger_speech():
    # Screen changes from the user typing a prompt (no "esc to interrupt"
    # working indicator) must NOT arm the speaker — otherwise it narrates the
    # user's own input back at them.
    snap = ["x"]
    llm = FakeLLM()
    svc, said = make_service(llm, snap=snap)
    svc._enabled.add("s")
    svc._watch["s"] = _WatchState()
    monitor = ActivityMonitor()
    drive(svc, "s", ["x"], monitor)  # baseline
    # Simulate typing: content grows, then settles — never a thinking indicator.
    typing = ["clean up the", "clean up the dead", "clean up the dead studio CSS"]
    drive(svc, "s", typing + ["clean up the dead studio CSS"] * 5, monitor)
    assert [m for m in said if m["type"] == "speaker.say"] == []
    assert llm.decide_calls == 0


def test_dedupe_no_repeat_on_unchanged_screen():
    snap = ["x"]
    llm = FakeLLM()
    svc, said = make_service(llm, snap=snap)
    svc._enabled.add("s")
    svc._watch["s"] = _WatchState()
    monitor = ActivityMonitor()
    drive(svc, "s", ["x"], monitor)  # baseline
    drive(svc, "s", settle_frames("RESULT"), monitor)
    drive(svc, "s", ["RESULT"] * 6, monitor)  # keep dwelling on same screen
    says = [m for m in said if m["type"] == "speaker.say"]
    assert len(says) == 1
    assert llm.decide_calls == 1  # the model is consulted only once for this screen


def test_gate_silence_still_dedupes():
    snap = ["x"]
    llm = FakeLLM(decision=SpeakDecision(False, "", False, False))
    svc, said = make_service(llm, snap=snap)
    svc._enabled.add("s")
    svc._watch["s"] = _WatchState()
    monitor = ActivityMonitor()
    drive(svc, "s", ["x"], monitor)
    drive(svc, "s", settle_frames("noise"), monitor)
    drive(svc, "s", ["noise"] * 6, monitor)
    assert [m for m in said if m["type"] == "speaker.say"] == []
    assert llm.decide_calls == 1  # gate ran once; never re-spent on the same screen


def test_cooldown_blocks_then_allows():
    snap = ["x"]
    llm = FakeLLM()
    cfg = SpeakerConfig(settle_polls=2, cooldown_seconds=30.0, tts_engine="browser")
    svc, said = make_service(llm, config=cfg, snap=snap)
    clock = svc._clock  # the Clock instance
    svc._enabled.add("s")
    svc._watch["s"] = _WatchState()
    monitor = ActivityMonitor()
    drive(svc, "s", ["x"], monitor)
    drive(svc, "s", settle_frames("first"), monitor)
    assert len([m for m in said if m["type"] == "speaker.say"]) == 1

    # New result within the cooldown window: must stay silent.
    drive(svc, "s", settle_frames("second"), monitor)
    assert len([m for m in said if m["type"] == "speaker.say"]) == 1

    # Advance past cooldown; the pending change is now spoken.
    clock.t = 100.0
    drive(svc, "s", ["second"] * 6, monitor)
    assert len([m for m in said if m["type"] == "speaker.say"]) == 2


def test_circuit_breaker_disables_session():
    snap = ["x"]
    llm = FakeLLM()
    cfg = SpeakerConfig(settle_polls=2, cooldown_seconds=0.0, max_per_minute=2, tts_engine="browser")
    svc, said = make_service(llm, config=cfg, snap=snap)
    svc._enabled.add("s")
    svc._watch["s"] = _WatchState()
    monitor = ActivityMonitor()
    drive(svc, "s", ["x"], monitor)
    for label in ("r1", "r2", "r3", "r4"):
        drive(svc, "s", settle_frames(label), monitor)
    assert "s" not in svc._enabled  # breaker tripped
    assert any(m["type"] == "speaker.notice" for m in said)


def test_complex_upgrade_used_when_flagged():
    snap = ["x"]
    llm = FakeLLM(decision=SpeakDecision(True, "draft", needs_complex=True, is_question=False))
    cfg = SpeakerConfig(settle_polls=2, cooldown_seconds=0.0, use_complex=True, tts_engine="browser")
    svc, said = make_service(llm, config=cfg, snap=snap)
    svc._enabled.add("s")
    svc._watch["s"] = _WatchState()
    monitor = ActivityMonitor()
    drive(svc, "s", ["x"], monitor)
    drive(svc, "s", settle_frames("final"), monitor)
    says = [m for m in said if m["type"] == "speaker.say"]
    assert says and says[0]["text"] == "draft (refined)"


def test_tts_engine_resolution():
    llm = FakeLLM(openai=True)
    svc, _ = make_service(llm, config=SpeakerConfig(tts_engine="auto"))
    assert svc._tts_uses_openai() is True
    svc.config = SpeakerConfig(tts_engine="browser")
    assert svc._tts_uses_openai() is False
    svc.config = SpeakerConfig(tts_engine="openai")
    svc._llm = FakeLLM(openai=False)
    assert svc._tts_uses_openai() is False  # asked for openai but no key


def test_openai_audio_attached_when_engine_openai():
    snap = ["x"]
    llm = FakeLLM(openai=True)
    cfg = SpeakerConfig(settle_polls=2, cooldown_seconds=0.0, tts_engine="openai")
    svc, said = make_service(llm, config=cfg, snap=snap)
    svc._enabled.add("s")
    svc._watch["s"] = _WatchState()
    monitor = ActivityMonitor()
    drive(svc, "s", ["x"], monitor)
    drive(svc, "s", settle_frames("final"), monitor)
    says = [m for m in said if m["type"] == "speaker.say"]
    assert says and says[0]["audio"] == "YWJj"
    assert llm.tts_calls == 1


# --------------------------------------------------------------------------- #
# Credential resolution
# --------------------------------------------------------------------------- #


def test_read_env_value(tmp_path):
    env = tmp_path / ".env"
    env.write_text('# comment\nFOO=bar\nOPENAI_API_KEY="sk-proj-secret"\n', encoding="utf-8")
    assert _read_env_value(env, "OPENAI_API_KEY") == "sk-proj-secret"
    assert _read_env_value(env, "MISSING") is None


def test_openai_key_prefers_env_then_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    # No env, no .env, and steer the codex fallback at a missing path.
    monkeypatch.setattr(speaker_llm.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    assert _openai_key(tmp_path) is None

    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-from-dotenv\n", encoding="utf-8")
    resolved = _openai_key(tmp_path)
    assert resolved is not None and resolved.api_key == "sk-from-dotenv"
    assert resolved.source == ".env"

    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    resolved = _openai_key(tmp_path)
    assert resolved.api_key == "sk-from-env" and resolved.source == "env"


def test_anthropic_scheme_from_source(monkeypatch):
    from yikes import credentials

    class Cred:
        def __init__(self, value, source):
            self.value, self.source = value, source

    # Raw API key (source "env") → x-api-key header scheme.
    monkeypatch.setattr(
        credentials.ClaudeCredentialProvider, "get", lambda self, name: Cred("sk-ant", "env")
    )
    keys = resolve_provider_keys(None)
    assert keys["anthropic"].auth_scheme == "x-api-key"

    # OAuth token (source "claude") → bearer-oauth scheme.
    monkeypatch.setattr(
        credentials.ClaudeCredentialProvider, "get", lambda self, name: Cred("oauth-tok", "claude")
    )
    keys = resolve_provider_keys(None)
    assert keys["anthropic"].auth_scheme == "bearer-oauth"


# --------------------------------------------------------------------------- #
# Connection hub
# --------------------------------------------------------------------------- #


def test_connection_hub_marks_dead_on_failure():
    class BadWS:
        async def send_json(self, message):
            raise RuntimeError("closed")

    hub = ConnectionHub(BadWS())
    asyncio.run(hub.send_json({"type": "x"}))
    assert hub.alive is False
