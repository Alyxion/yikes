from __future__ import annotations

import asyncio

import yikes.session_inventory as si
from yikes.app_core import YikesAppController
from yikes.session_inventory import _send_keys
from yikes.web_handler import WebMessageHandler


def test_send_keys_literal_then_named(monkeypatch):
    calls: list[list[str]] = []

    class _Result:
        returncode = 0

    monkeypatch.setattr(si.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _Result())
    ok = _send_keys(["tmux", "-S", "/sock", "send-keys"], "sess", "hello world", ("Enter",), cwd=None)
    assert ok
    # Literal text uses -l so spaces/specials are typed verbatim; key follows.
    assert calls[0] == ["tmux", "-S", "/sock", "send-keys", "-t", "sess", "-l", "--", "hello world"]
    assert calls[1] == ["tmux", "-S", "/sock", "send-keys", "-t", "sess", "Enter"]


def test_send_keys_reports_failure(monkeypatch):
    class _Result:
        returncode = 1

    monkeypatch.setattr(si.subprocess, "run", lambda cmd, **kw: _Result())
    assert _send_keys(["tmux", "send-keys"], None, "x", (), cwd=None) is False


class _FakeLifecycle:
    def __init__(self):
        self.calls: list[tuple] = []

    def send_input(self, session_id, *, text=None, keys=()):
        self.calls.append((session_id, text, tuple(keys)))
        return True


def _controller_with(lifecycle, *, active="s1"):
    ctl = YikesAppController.__new__(YikesAppController)  # skip heavy __init__
    ctl.lifecycle = lifecycle
    ctl.active_session_id = active
    return ctl


def test_send_terminal_input_maps_accept_and_escape():
    life = _FakeLifecycle()
    ctl = _controller_with(life)
    assert ctl.send_terminal_input(None, key="accept") is True
    assert ctl.send_terminal_input(None, key="escape") is True
    assert life.calls == [("s1", None, ("Enter",)), ("s1", None, ("Escape",))]


def test_send_terminal_input_digit_is_literal_text():
    life = _FakeLifecycle()
    ctl = _controller_with(life)
    ctl.send_terminal_input("s9", text="2")
    assert life.calls == [("s9", "2", ())]


def test_send_terminal_input_text_and_enter_in_one_call():
    # "Send ⏎" / auto-accept: type text then confirm with Enter in one message.
    life = _FakeLifecycle()
    ctl = _controller_with(life)
    ctl.send_terminal_input("s1", text="fix the parser", key="accept")
    assert life.calls == [("s1", "fix the parser", ("Enter",))]


def test_send_terminal_input_rejects_unknown_key_and_no_session():
    life = _FakeLifecycle()
    ctl = _controller_with(life)
    assert ctl.send_terminal_input("s1", key="format-disk") is False
    ctl_no_session = _controller_with(life, active=None)
    assert ctl_no_session.send_terminal_input(None, text="hi") is False
    assert life.calls == []  # neither reached the lifecycle


class _FakeController:
    def __init__(self):
        self.got = None

    def send_terminal_input(self, session_id, *, text=None, key=None):
        self.got = (session_id, text, key)
        return True

    def state(self):
        return {}


def test_handler_routes_term_input():
    ctl = _FakeController()
    handler = WebMessageHandler(ctl, None)
    result = asyncio.run(handler.handle({"type": "term.input", "session_id": "s", "text": "hi"}))
    assert result == {"type": "term.input.ack", "ok": True}
    assert ctl.got == ("s", "hi", None)


# --- voice interpretation -------------------------------------------------- #


def test_local_interpret_routes_commands_and_dictation():
    from yikes.speaker import _local_interpret

    assert _local_interpret("accept that").action == "accept"
    assert _local_interpret("go with option two").value == 2
    assert _local_interpret("use number 3").value == 3
    assert _local_interpret("never mind cancel").action == "escape"
    d = _local_interpret("write a test for the parser")
    assert d.mode == "dictate" and d.text == "write a test for the parser"


class _NoProviderLLM:
    def has_any_provider(self):
        return False

    def has_openai(self):
        return False

    def available_providers(self):
        return {"anthropic": False, "openai": False}

    def refresh_keys(self):
        pass


def test_interpret_voice_falls_back_to_local_without_keys():
    from yikes.speaker import SpeakerConfig, SpeakerService

    class _Ctl:
        start_cwd = None
        session_lines: dict = {}

    svc = SpeakerService(_Ctl(), config=SpeakerConfig(), llm=_NoProviderLLM())
    assert asyncio.run(svc.interpret_voice("accept")) == {"mode": "command", "text": "", "action": "accept", "value": 0}
    assert asyncio.run(svc.interpret_voice("option two")) == {"mode": "command", "text": "", "action": "select", "value": 2}
    dictate = asyncio.run(svc.interpret_voice("add a docstring"))
    assert dictate["mode"] == "dictate" and dictate["text"] == "add a docstring"


class _FakeSpeaker:
    async def interpret_voice(self, transcript):
        return {"mode": "command", "text": "", "action": "select", "value": 2}


def test_handler_routes_voice_interpret():
    handler = WebMessageHandler(_FakeController(), None, _FakeSpeaker())
    result = asyncio.run(handler.handle({"type": "voice.interpret", "req_id": "v1", "transcript": "pick two"}))
    assert result == {"type": "voice.interpret.result", "req_id": "v1", "mode": "command", "text": "", "action": "select", "value": 2}


def test_config_has_stt_fields_and_validates():
    from yikes.speaker import SpeakerConfig

    cfg = SpeakerConfig()
    assert cfg.stt_model == "gpt-4o-transcribe"
    assert "stt_engine" in cfg.to_json() and "stt_model" in cfg.to_json()
    assert SpeakerConfig().apply({"stt_engine": "Browser"}).stt_engine == "browser"
    assert SpeakerConfig().apply({"stt_engine": "bogus"}).stt_engine == "auto"
    assert SpeakerConfig().apply({"stt_model": "whisper-1"}).stt_model == "whisper-1"


def test_transcribe_and_interpret_without_openai_key():
    from yikes.speaker import SpeakerConfig, SpeakerService

    class _Ctl:
        start_cwd = None
        session_lines: dict = {}

    svc = SpeakerService(_Ctl(), config=SpeakerConfig(), llm=_NoProviderLLM())
    out = asyncio.run(svc.transcribe_and_interpret("AAAA", "audio/webm"))
    assert out["transcript"] == "" and out["error"] and out["mode"] == "dictate"


class _FakeTranscribeSpeaker:
    async def transcribe_and_interpret(self, audio, mime):
        return {"transcript": "open the file", "mode": "dictate", "text": "open the file", "action": "", "value": 0}


def test_handler_routes_voice_utterance():
    # voice.utterance is dispatched as a background task (transcribe_voice), not
    # via handle(); verify the pushed payload shape.
    handler = WebMessageHandler(_FakeController(), None, _FakeTranscribeSpeaker())
    sent = {}

    class _Sink:
        async def send_json(self, m):
            sent.update(m)

    asyncio.run(handler.transcribe_voice(_Sink(), {"req_id": "u1", "audio": "x", "mime": "audio/webm"}))
    assert sent["type"] == "voice.utterance.result"
    assert sent["transcript"] == "open the file" and sent["req_id"] == "u1"
