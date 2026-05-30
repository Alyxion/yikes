from __future__ import annotations

from pathlib import Path

from yikes import ACTIVITY_THINKING, Backend, Driver, DurableSessionManager, RuntimeKind, RuntimeRef
from yikes.app_core import YikesAppController
from yikes.domain import ChatOptions, DriverMode, ImageAttachment


class EchoTransport:
    def ask(self, options: ChatOptions, prompt: str, attachments: tuple[ImageAttachment, ...] = ()) -> str:
        return "8"


class EmptyTransport:
    def ask(self, options: ChatOptions, prompt: str, attachments: tuple[ImageAttachment, ...] = ()) -> str:
        return ""


def test_controller_blocks_text_without_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    monkeypatch.setenv("YIKES_PROMPT_PROFILE_PATH", str(tmp_path / "prompt-profile.json"))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())

    state = controller.submit("hello")

    assert state["error"] == "No active session. Use /new or the New Session button first."
    assert state["has_active_session"] is False
    assert "Prompt profile ready" in state["output_text"]
    assert state["startup"]["profile_shared_for"] == ["codex", "claude"]


def test_controller_new_session_wizard_then_submit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())

    controller.open_new_session()
    controller.update_new_session(backend="codex", location="host", driver="cli", managed_output="off", root=str(tmp_path))
    state = controller.confirm_new_session()

    assert state["status"]["backend"] == "codex"
    assert state["status"]["driver"] == "cli"
    assert state["status"]["capture"] == "disabled"
    assert state["has_active_session"] is True

    state = controller.submit("4+4")

    assert "You: 4+4" in state["output_text"]
    assert "Assistant: 8" in state["output_text"]
    assert "Working..." not in state["output_text"]


def test_controller_tmux_new_session_defaults_to_interactive_dev_view(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    started: list[ChatOptions] = []
    monkeypatch.setattr("yikes.app_core.ensure_interactive_session", started.append)
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())

    controller.open_new_session()
    controller.update_new_session(backend="codex", location="host", driver="tmux", root=str(tmp_path))
    state = controller.confirm_new_session()

    assert state["status"]["driver"] == "tmux"
    assert state["status"]["capture"] == "disabled"
    assert state["output_view"] == "dev"
    assert "capture off" in state["output_text"]
    assert started and started[0].mode is DriverMode.TMUX


def test_controller_exposes_editable_cli_model_and_web_controls(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())
    controller.open_new_session()
    controller.update_new_session(backend="codex", location="host", driver="cli", root=str(tmp_path))
    created = controller.confirm_new_session()

    assert created["controls"]["editable"] is True
    assert "default" in created["controls"]["model_options"]
    assert created["active_session_activity"]["label"] == "idle"

    updated = controller.update_active_config(model="gpt-5.5", web_search="off")

    assert updated["controls"]["model"] == "gpt-5.5"
    assert updated["controls"]["web_search"] == "off"
    assert updated["status"]["model"] == "gpt-5.5"
    assert updated["status"]["web"] == "disabled"


def test_controller_records_cli_session_as_durable_tab(tmp_path: Path, monkeypatch) -> None:
    runtime_store = tmp_path / "sessions"
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(runtime_store))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())

    controller.open_new_session()
    controller.update_new_session(backend="codex", location="host", driver="cli", root=str(tmp_path))
    state = controller.confirm_new_session()
    session_id = state["active_session_id"]

    meta = DurableSessionManager(runtime_store).get(session_id)
    assert meta is not None
    assert meta.runtime.kind is RuntimeKind.DIRECT
    assert meta.driver is Driver.DIRECT
    assert meta.backend is Backend.CODEX
    assert session_id in {session["id"] for session in state["sessions"]}


def test_controller_can_switch_to_durable_cli_session_after_restart(tmp_path: Path, monkeypatch) -> None:
    runtime_store = tmp_path / "sessions"
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(runtime_store))
    first = YikesAppController(cwd=tmp_path, transport=EchoTransport())
    first.open_new_session()
    first.update_new_session(backend="codex", location="host", driver="cli", root=str(tmp_path))
    created = first.confirm_new_session()
    session_id = created["active_session_id"]

    second = YikesAppController(cwd=tmp_path, transport=EchoTransport())
    switched = second.switch_session(session_id)

    assert switched["error"] is None
    assert switched["active_session_id"] == session_id
    assert switched["status"]["backend"] == "codex"
    assert switched["status"]["driver"] == "cli"
    assert session_id in {session["id"] for session in switched["sessions"]}


def test_controller_keeps_cli_output_per_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())

    controller.open_new_session()
    controller.update_new_session(backend="codex", location="host", driver="cli", root=str(tmp_path))
    first = controller.confirm_new_session()["active_session_id"]
    controller.submit("first question")

    controller.open_new_session()
    controller.update_new_session(backend="codex", location="host", driver="cli", root=str(tmp_path))
    second = controller.confirm_new_session()

    assert second["active_session_id"] != first
    assert "first question" not in second["output_text"]
    assert "Assistant: 8" not in second["output_text"]
    assert "New session: codex on host via cli" in second["output_text"]

    switched = controller.switch_session(first)

    assert "You: first question" in switched["output_text"]
    assert "Assistant: 8" in switched["output_text"]


def test_switching_from_tmux_snapshot_back_to_cli_does_not_leak_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())
    controller.open_new_session()
    controller.update_new_session(backend="codex", location="host", driver="cli", root=str(tmp_path))
    created = controller.confirm_new_session()
    cli_id = created["active_session_id"]
    controller.submit("cli question")
    controller.output_view = "dev"

    class FakeLifecycle:
        def summary(self, session_id: str):
            return None

        def switch_options(self, current: ChatOptions, session_id: str) -> ChatOptions:
            driver = Driver.TMUX if session_id == "tmux-session" else Driver.DIRECT
            return current.with_driver(driver).with_session_id(session_id)

        def snapshot(self, session_id: str, *, lines: int = 1200) -> str | None:
            if session_id == "tmux-session":
                return "Assistant: stale tmux sentence"
            return None

        def capture_markers(self, session_id: str):
            return ()

    controller.lifecycle = FakeLifecycle()  # type: ignore[assignment]

    tmux_state = controller.switch_session("tmux-session")
    cli_state = controller.switch_session(cli_id)

    assert "stale tmux sentence" in tmux_state["output_text"]
    assert "stale tmux sentence" not in cli_state["output_text"]
    assert "You: cli question" in cli_state["output_text"]


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


def test_controller_exposes_terminal_activity_for_python_callers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())

    class FakeLifecycle:
        def snapshot(self, session_id: str, *, lines: int = 120) -> str:
            return "Working (3s • esc to interrupt)"

    controller.lifecycle = FakeLifecycle()  # type: ignore[assignment]
    controller.active_session_id = "session-1"

    activity = controller.session_activity()

    assert activity.state == ACTIVITY_THINKING
    assert controller.state()["active_session_activity"]["state"] == ACTIVITY_THINKING


def test_confirm_new_session_keeps_existing_session_as_separate_tab(tmp_path: Path, monkeypatch) -> None:
    runtime_store = tmp_path / "sessions"
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(runtime_store))
    previous = DurableSessionManager(runtime_store).create(
        backend=Backend.CLAUDE,
        driver=Driver.DIRECT,
        runtime=RuntimeRef(RuntimeKind.DIRECT),
        cwd=tmp_path,
    )
    monkeypatch.setattr("yikes.app_core.ensure_interactive_session", lambda *a, **k: None)
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())

    controller.open_new_session()
    controller.update_new_session(backend="codex", location="host", driver="tmux", root=str(tmp_path))
    state = controller.confirm_new_session()

    ids = {session["id"] for session in state["sessions"]}
    assert previous.id in ids
    assert state["active_session_id"] in ids
    assert state["active_session_id"] != previous.id
    assert "Started new session" not in state["output_text"]
    assert "/backend" not in state["output_text"]
    assert "New session: codex on host via tmux" in state["output_text"]


def test_controller_blocks_submit_to_dead_tmux_session(tmp_path: Path, monkeypatch) -> None:
    runtime_store = tmp_path / "sessions"
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(runtime_store))
    meta = DurableSessionManager(runtime_store).create(
        backend=Backend.CODEX,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket=str(tmp_path / "missing.sock"), tmux_session="gone"),
        cwd=tmp_path,
    )
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())
    controller.active_session_id = meta.id

    state = controller.submit("hello")

    assert state["error"] == f"Session {meta.id} is dead. Close it and create or select another session."
    assert state["active_session_id"] is None


def test_controller_refuses_switch_to_dead_tmux_session(tmp_path: Path, monkeypatch) -> None:
    runtime_store = tmp_path / "sessions"
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(runtime_store))
    meta = DurableSessionManager(runtime_store).create(
        backend=Backend.CODEX,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket=str(tmp_path / "missing.sock"), tmux_session="gone"),
        cwd=tmp_path,
    )
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())

    state = controller.switch_session(meta.id)

    assert state["error"] == f"Session {meta.id} is dead. Close it and create or select another session."
    assert state["active_session_id"] is None


def test_controller_errors_are_one_shot_for_polling(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())

    first = controller.switch_session("missing-session")
    second = controller.state()

    assert first["error"] == "Session not found: missing-session"
    assert second["error"] is None


def test_switching_active_ephemeral_cli_session_is_noop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())
    controller.open_new_session()
    controller.update_new_session(backend="codex", location="host", driver="cli")
    created = controller.confirm_new_session()
    active_id = created["active_session_id"]

    switched = controller.switch_session(active_id)

    assert switched["error"] is None
    assert switched["active_session_id"] == active_id
    assert "Session not found" not in switched["output_text"]


def test_controller_output_prefers_live_tmux_snapshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())
    controller.active_session_id = "session-1"
    controller.lines = ["local wrapper line"]

    class FakeLifecycle:
        def snapshot(self, session_id: str, *, lines: int = 1200) -> str:
            return "shared tmux pane"

    controller.lifecycle = FakeLifecycle()  # type: ignore[assignment]
    controller.output_view = "dev"

    assert controller.output_text() == "shared tmux pane"


def test_web_state_sessions_carry_intuitive_name(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "dashboard"
    project.mkdir()
    DurableSessionManager().create(
        backend=Backend.CLAUDE,
        driver=Driver.DIRECT,
        runtime=RuntimeRef(RuntimeKind.DIRECT),
        cwd=project,
    )
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())

    names = [session["name"] for session in controller.state()["sessions"]]
    assert "dashboard" in names  # not a raw session id


def test_controller_high_view_hides_unmanaged_tmux_snapshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())
    controller.active_session_id = "session-1"

    class FakeLifecycle:
        def snapshot(self, session_id: str, *, lines: int = 1200) -> str:
            return "raw native prompt noise"

    controller.lifecycle = FakeLifecycle()  # type: ignore[assignment]

    assert controller.output_text() == ""


def test_controller_high_view_shows_pending_prompt_while_running(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())
    controller.active_session_id = "session-1"
    controller.pending_prompt = "hi"
    controller.submission_active = True

    class FakeLifecycle:
        def snapshot(self, session_id: str, *, lines: int = 1200) -> str:
            return "raw native prompt noise"

    controller.lifecycle = FakeLifecycle()  # type: ignore[assignment]

    assert controller.output_text() == "You: hi\nAssistant: Working..."


def test_controller_dev_view_shows_pending_prompt_before_tmux_capture_catches_up(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())
    controller.active_session_id = "session-1"
    controller.output_view = "dev"
    controller.pending_prompt = "explain this"
    controller.submission_active = True

    class FakeLifecycle:
        def snapshot(self, session_id: str, *, lines: int = 1200) -> str:
            return "OpenAI Codex\n› previous prompt"

    controller.lifecycle = FakeLifecycle()  # type: ignore[assignment]

    assert controller.output_text().endswith("> explain this\nWorking...")


def test_unmanaged_tmux_submit_keeps_live_follow_active_after_prompt_is_pasted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    controller = YikesAppController(cwd=tmp_path, transport=EmptyTransport())
    controller.conversation.set_options(
        ChatOptions(
            Backend.CODEX,
            Driver.TMUX,
            tmp_path,
            settings=controller.conversation.options.settings.with_managed_output(False),
            session_id="tmux-session",
        )
    )
    controller.active_session_id = "tmux-session"

    assert controller.begin_submit("explain this") is True
    state = controller.finish_submit()

    assert state["submission_active"] is False
    assert state["active_session_activity"]["label"] != "working"
    assert controller.pending_prompt == "explain this"
    assert "explain this" in state["output_text"]


def test_switching_unmanaged_tmux_session_uses_dev_view(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    controller = YikesAppController(cwd=tmp_path, transport=EchoTransport())
    controller.output_view = "high"

    class FakeLifecycle:
        def summary(self, session_id: str):
            return None

        def switch_options(self, current: ChatOptions, session_id: str) -> ChatOptions:
            return ChatOptions(
                Backend.CODEX,
                Driver.TMUX,
                tmp_path,
                settings=current.settings.with_managed_output(False),
                session_id=session_id,
            )

        def snapshot(self, session_id: str, *, lines: int = 1200) -> str:
            return "OpenAI Codex"

    controller.lifecycle = FakeLifecycle()  # type: ignore[assignment]

    state = controller.switch_session("tmux-session")

    assert state["output_view"] == "dev"
    assert state["output_text"] == "OpenAI Codex"
