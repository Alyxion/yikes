from __future__ import annotations

from pathlib import Path

from yikes import AgentSettings
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
