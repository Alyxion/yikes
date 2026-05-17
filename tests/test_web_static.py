from __future__ import annotations

from pathlib import Path


def test_web_layout_does_not_reserve_bottom_spacer_row() -> None:
    css = (Path(__file__).resolve().parents[1] / "yikes" / "web_static" / "yikes-web.css").read_text()

    assert "grid-template-rows: 48px minmax(0, 1fr) auto;" in css
    assert "grid-template-rows: 48px minmax(0, 1fr) auto 56px;" not in css


def test_web_hides_composer_when_no_session_is_active() -> None:
    js = (Path(__file__).resolve().parents[1] / "yikes" / "web_static" / "yikes-web.js").read_text()

    assert 'els.composer.classList.toggle("hidden", state.terminalExclusive || noSession);' in js
    assert "els.message.disabled = noSession;" in js


def test_web_new_session_shows_immediate_creation_feedback() -> None:
    js = (Path(__file__).resolve().parents[1] / "yikes" / "web_static" / "yikes-web.js").read_text()

    assert "function showCreatingSession(changes)" in js
    assert 'state.term.write("Creating session...\\r\\n");' in js
    assert 'document.getElementById("wizard-create").onclick = () => showCreatingSession(wizardFormChanges());' in js


def test_web_terminal_resize_reaches_attached_pty() -> None:
    js = (Path(__file__).resolve().parents[1] / "yikes" / "web_static" / "yikes-web.js").read_text()

    assert "window.addEventListener(\"resize\", debounce(fitAfterWindowResize, 120));" in js
    assert "function fitAfterWindowResize()" in js
    assert "resizeActiveTerminal();" in js
    assert "function resizeActiveTerminalRepeatedly()" in js


def test_web_terminal_has_no_inner_padding_that_breaks_fit() -> None:
    css = (Path(__file__).resolve().parents[1] / "yikes" / "web_static" / "yikes-web.css").read_text()

    assert ".xterm {\n  height: 100%;\n  padding: 0;\n}" in css
    assert "body.terminal-exclusive .xterm {\n  padding: 0;\n}" in css


def test_fullscreen_terminal_uses_single_grid_row_and_restores_tab() -> None:
    root = Path(__file__).resolve().parents[1]
    css = (root / "yikes" / "web_static" / "yikes-web.css").read_text()
    js = (root / "yikes" / "web_static" / "yikes-web.js").read_text()

    assert "body.terminal-exclusive .topbar" in css
    assert "body.terminal-exclusive .terminal-panel {\n  grid-row: 1;" in css
    assert "state.returnSessionId = state.app.active_session_id || \"\";" in js
    assert 'send("session.switch", { session_id: returnSessionId });' in js


def test_web_polls_active_sessions_fast_enough_for_tmux_streaming() -> None:
    js = (Path(__file__).resolve().parents[1] / "yikes" / "web_static" / "yikes-web.js").read_text()

    assert "}, 650);" in js


def test_web_terminal_appends_streaming_output_without_full_reset() -> None:
    js = (Path(__file__).resolve().parents[1] / "yikes" / "web_static" / "yikes-web.js").read_text()

    assert "output.startsWith(state.renderedOutputText)" in js
    assert "const delta = output.slice(state.renderedOutputText.length);" in js
    assert "function resetRenderedOutputState()" in js


def test_terminal_sidebar_has_no_manual_refresh_or_attach_buttons() -> None:
    source = (Path(__file__).resolve().parents[1] / "yikes" / "tui.py").read_text()

    assert 'Button("Refresh Sessions"' not in source
    assert 'Button("Attach"' not in source
