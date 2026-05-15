from __future__ import annotations

from pathlib import Path

from yikes.app_core import YikesAppController
from yikes.domain import ChatOptions, ImageAttachment


class EchoTransport:
    def ask(self, options: ChatOptions, prompt: str, attachments: tuple[ImageAttachment, ...] = ()) -> str:
        return "8"


def test_controller_blocks_text_without_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())

    state = controller.submit("hello")

    assert state["error"] == "No active session. Use /new or the New Session button first."
    assert state["has_active_session"] is False


def test_controller_new_session_wizard_then_submit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())

    controller.open_new_session()
    controller.update_new_session(backend="codex", location="host", driver="cli", root=str(tmp_path))
    state = controller.confirm_new_session()

    assert state["status"]["backend"] == "codex"
    assert state["status"]["driver"] == "cli"
    assert state["has_active_session"] is True

    state = controller.submit("4+4")

    assert "You: 4+4" in state["output_text"]
    assert "Assistant: 8" in state["output_text"]
    assert "Working..." not in state["output_text"]


def test_controller_exposes_registry_suggestions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())

    suggestions = controller.suggestions("/mo")

    completions = {item["completion"] for item in suggestions}
    assert "/model " in completions
    assert "/models" in completions


def test_controller_reports_no_attach_command_without_tmux_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())

    assert controller.attach_command() is None
