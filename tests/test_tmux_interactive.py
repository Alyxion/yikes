from __future__ import annotations

from pathlib import Path

from yikes import AgentSettings, Backend, Driver, DurableSessionManager, RuntimeKind, RuntimeRef
import yikes.drivers as drivers


def test_local_tmux_starts_real_claude_tui_not_headless(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    alive = iter((False, True))

    monkeypatch.setenv("YIKES_TMUX_DIR", str(tmp_path / "tmux"))
    monkeypatch.setattr(drivers, "_tmux_session_alive", lambda *_args, **_kwargs: next(alive))
    monkeypatch.setattr(drivers, "_set_tmux_options", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(drivers, "_record_tmux_session", lambda *_args, **_kwargs: None)

    def fake_run_process(argv: list[str], **_kwargs: object) -> object:
        calls.append(argv)
        return object()

    monkeypatch.setattr(drivers, "run_process", fake_run_process)

    drivers._ensure_local_tmux_session("claude", tmp_path, model="sonnet", settings=AgentSettings())

    command = calls[0]
    assert command[-5:] == ["claude", "--permission-mode", "dontAsk", "--model", "sonnet"]
    assert "-p" not in command
    assert "--output-format" not in command


def test_local_tmux_starts_real_codex_tui_not_exec(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    alive = iter((False, True))

    monkeypatch.setenv("YIKES_TMUX_DIR", str(tmp_path / "tmux"))
    monkeypatch.setattr(drivers, "_tmux_session_alive", lambda *_args, **_kwargs: next(alive))
    monkeypatch.setattr(drivers, "_set_tmux_options", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(drivers, "_record_tmux_session", lambda *_args, **_kwargs: None)

    def fake_run_process(argv: list[str], **_kwargs: object) -> object:
        calls.append(argv)
        return object()

    monkeypatch.setattr(drivers, "run_process", fake_run_process)

    drivers._ensure_local_tmux_session("codex", tmp_path, model=None, settings=AgentSettings())

    command = calls[0]
    assert command[-2:] == ["codex", "--no-alt-screen"]
    assert "exec" not in command


def test_local_codex_tmux_trusts_generated_workspace_in_session_config(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    (source_home / "auth.json").write_text("{}", encoding="utf-8")
    (source_home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
    generated = tmp_path / "generated"
    generated.mkdir()
    alive = iter((False, True))

    monkeypatch.setenv("CODEX_HOME", str(source_home))
    monkeypatch.setenv("YIKES_TMUX_DIR", str(tmp_path / "tmux"))
    monkeypatch.setattr(drivers, "_tmux_session_alive", lambda *_args, **_kwargs: next(alive))
    monkeypatch.setattr(drivers, "_set_tmux_options", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(drivers, "_record_tmux_session", lambda *_args, **_kwargs: None)

    def fake_run_process(argv: list[str], **_kwargs: object) -> object:
        calls.append(argv)
        return object()

    monkeypatch.setattr(drivers, "run_process", fake_run_process)

    drivers._ensure_local_tmux_session(
        "codex",
        generated,
        model=None,
        settings=AgentSettings(),
        cwd_explicit=False,
    )

    env_arg = next(part for part in calls[0] if part.startswith("CODEX_HOME="))
    config = (Path(env_arg.removeprefix("CODEX_HOME=")) / "config.toml").read_text(encoding="utf-8")
    assert 'model = "gpt-5.5"' in config
    assert f'[projects."{generated}"]' in config
    assert 'trust_level = "trusted"' in config


def test_local_tmux_reuses_existing_durable_session_socket(monkeypatch, tmp_path: Path) -> None:
    runtime_store = tmp_path / "runtime"
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(runtime_store))
    existing = DurableSessionManager(runtime_store).create(
        backend=Backend.CODEX,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket=str(tmp_path / "existing.sock"), tmux_session="existing"),
        cwd=tmp_path,
    )

    monkeypatch.setattr(drivers, "_tmux_session_alive", lambda socket, session, _cwd: socket == tmp_path / "existing.sock" and session == "existing")

    def fail_run_process(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("existing live tmux session should be reused")

    monkeypatch.setattr(drivers, "run_process", fail_run_process)

    socket, session = drivers._ensure_local_tmux_session(
        "codex",
        tmp_path,
        model=None,
        settings=AgentSettings(),
        session_id=existing.id,
    )

    assert socket == tmp_path / "existing.sock"
    assert session == "existing"


def test_tmux_result_markers_ignore_prompt_template() -> None:
    markers = drivers.ResultMarkers("YIKES_RESULT_START_abc123", "YIKES_RESULT_END_abc123")
    prompt_screen = (
        "Opening marker: YIKES_RESULT_START_abc123\n"
        "Closing marker: YIKES_RESULT_END_abc123\n"
    )
    result_screen = (
        f"{prompt_screen}\n"
        "⏺ YIKES_RESULT_START_abc123\n"
        "  OK\n"
        "  YIKES_RESULT_END_abc123\n"
    )

    assert drivers._has_marked_result(prompt_screen, markers) is False
    assert drivers._extract_marked_result(result_screen, markers) == "OK"


def test_tmux_result_markers_are_not_project_branded(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("YIKES_PROMPT_PROFILE_PATH", str(tmp_path / "prompt-profile.json"))
    markers = drivers._result_markers()
    prompt = drivers._marked_prompt("Say OK.", markers)
    ordinary = drivers._marked_prompt("Say OK.", markers, include_instruction=False)

    assert markers.start != markers.end
    assert "abc123" not in markers.start
    assert any(char in f"{markers.start}{markers.end}" for char in "@>/<-_=~#")
    assert "yikes" not in prompt.lower()
    assert markers.start in prompt
    assert markers.end in prompt
    assert ordinary == "Say OK."


def test_session_result_markers_are_stable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("YIKES_PROMPT_PROFILE_PATH", str(tmp_path / "prompt-profile.json"))

    first = drivers._session_result_markers("session-a")
    second = drivers._session_result_markers("session-a")
    other = drivers._session_result_markers("session-b")

    assert first == second
    assert first != other


def test_wait_for_tmux_result_waits_for_next_marker_count(monkeypatch, tmp_path: Path) -> None:
    markers = drivers.ResultMarkers("RESULT_START_same", "RESULT_END_same")
    old_screen = "RESULT_START_same\nold\nRESULT_END_same\n"
    new_screen = old_screen + "\nRESULT_START_same\nnew\nRESULT_END_same\n"
    screens = [old_screen, new_screen]

    def fake_capture(*_args: object, **_kwargs: object) -> str:
        return screens.pop(0) if screens else new_screen

    monkeypatch.setattr(drivers, "_capture_tmux", fake_capture)
    monkeypatch.setattr(drivers.time, "sleep", lambda _seconds: None)

    screen = drivers._wait_for_tmux_result(
        tmp_path / "sock",
        "session",
        markers=markers,
        cwd=tmp_path,
        timeout=1,
        min_count=2,
    )

    assert drivers._extract_marked_result(screen, markers) == "new"


def test_tmux_runtime_raw_mode_sends_without_marker_gate(monkeypatch, tmp_path: Path) -> None:
    runtime = drivers.TmuxRuntime()
    sent: list[str] = []

    monkeypatch.setattr(drivers, "require_binary", lambda _name: None)
    monkeypatch.setattr(runtime, "ensure_session", lambda *_args, **_kwargs: (tmp_path / "sock", "session"))
    monkeypatch.setattr(runtime, "prepare_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "send_prompt", lambda _socket, _session, text, **_kwargs: sent.append(text))

    answer = runtime.ask(
        "codex",
        "hello",
        cwd=tmp_path,
        cwd_explicit=True,
        timeout=1,
        model=None,
        session_id="session-id",
        settings=AgentSettings(managed_output_enabled=False),
    )

    assert answer == ""
    assert sent == ["hello"]


def test_codex_tmux_submit_sends_linefeed_then_carriage_return(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(drivers.time, "sleep", lambda _seconds: None)

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        return object()

    monkeypatch.setattr(drivers.subprocess, "run", fake_run)

    drivers._tmux_submit(tmp_path / "sock", "session", cwd=tmp_path, backend="codex")

    assert calls[0][-1] == "C-j"
    assert calls[1][-1] == "C-m"


def test_tmux_options_keep_exited_panes_inspectable(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        return object()

    monkeypatch.setattr(drivers.subprocess, "run", fake_run)

    drivers._set_tmux_options(tmp_path / "sock", tmp_path)

    assert ["tmux", "-S", str(tmp_path / "sock"), "set", "-g", "remain-on-exit", "on"] in calls


def test_claude_tmux_submit_keeps_single_carriage_return(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        return object()

    monkeypatch.setattr(drivers.subprocess, "run", fake_run)

    drivers._tmux_submit(tmp_path / "sock", "session", cwd=tmp_path, backend="claude")

    assert len(calls) == 1
    assert calls[0][-1] == "C-m"


def test_terminal_fullscreen_attach_sets_child_pty_size() -> None:
    source = (Path(__file__).resolve().parents[1] / "yikes" / "tui.py").read_text(encoding="utf-8")

    assert "pty.spawn" not in source
    assert "pty.openpty()" in source
    assert "_set_fd_size(slave_fd" in source
    assert "signal.signal(signal.SIGWINCH, resize_child)" in source
    assert "on_resize(new_cols, new_rows)" in source


def test_terminal_fullscreen_return_preserves_selected_session() -> None:
    source = (Path(__file__).resolve().parents[1] / "yikes" / "tui.py").read_text(encoding="utf-8")

    assert 'os.environ["YIKES_RETURN_SESSION_ID"] = app.attach_session_id' in source
    assert 'self.preferred_session_id = os.environ.pop("YIKES_RETURN_SESSION_ID", "")' in source
    assert "session.id == self.preferred_session_id" in source
