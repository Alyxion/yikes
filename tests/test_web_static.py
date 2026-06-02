from __future__ import annotations

from pathlib import Path


def test_web_layout_does_not_reserve_bottom_spacer_row() -> None:
    css = (Path(__file__).resolve().parents[1] / "yikes" / "web_static" / "yikes-web.css").read_text()

    # topbar / pane-bar / content / composer — no fixed-height bottom spacer row.
    assert "grid-template-rows: 48px auto minmax(0, 1fr) auto;" in css
    assert "grid-template-rows: 48px minmax(0, 1fr) auto 56px;" not in css


def test_web_hides_composer_when_no_session_is_active() -> None:
    js = (Path(__file__).resolve().parents[1] / "yikes" / "web_static" / "yikes-web.js").read_text()

    # Composer only shows for non-interactive (pane-less) chat sessions.
    assert "const composerHidden = noSession || panes.length > 0;" in js
    assert 'els.composer.classList.toggle("hidden", composerHidden);' in js
    assert "els.message.disabled = noSession;" in js


def test_web_url_field_not_clobbered_while_editing() -> None:
    js = (Path(__file__).resolve().parents[1] / "yikes" / "web_static" / "yikes-web.js").read_text()

    assert "if (document.activeElement !== els.webUrl) els.webUrl.value = url;" in js
    assert "function schemeFor(" in js  # public URLs default to https


def test_web_prunes_iframes_for_closed_sessions() -> None:
    js = (Path(__file__).resolve().parents[1] / "yikes" / "web_static" / "yikes-web.js").read_text()

    assert "function pruneWebFrames(next)" in js
    assert "pruneWebFrames(next);" in js  # called from render()
    assert "frame.parentNode.removeChild(frame)" in js


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
    # Fullscreen toggles in place (stays attached) rather than detaching.
    assert "function enterFullscreen()" in js
    assert "function exitFullscreen()" in js


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


def test_web_has_training_capture_button_and_hotkey() -> None:
    root = Path(__file__).resolve().parents[1] / "yikes" / "web_static"
    html = (root / "index.html").read_text()
    js = (root / "yikes-web.js").read_text()

    # Discoverable element + popover + toast.
    assert 'id="capture-btn"' in html
    assert 'id="capture-pop"' in html
    assert 'id="toast"' in html
    # Sends a train.capture message for the active session and handles the reply.
    assert 'send("train.capture"' in js
    assert 'message.type === "train.captured"' in js
    # Ctrl+Alt+L hotkey, registered in the capture phase so it beats the terminal.
    assert "event.ctrlKey && event.altKey" in js
    assert '}, true);' in js


def test_label_button_is_developer_gated() -> None:
    js = (Path(__file__).resolve().parents[1] / "yikes" / "web_static" / "yikes-web.js").read_text()
    # The label button only shows when the server reports developer mode.
    assert "!!next.developer" in js


def test_web_shows_per_state_activity_icons() -> None:
    root = Path(__file__).resolve().parents[1] / "yikes" / "web_static"
    js = (root / "yikes-web.js").read_text()
    css = (root / "yikes-web.css").read_text()
    # Phosphor inline SVGs, not emoji, animated for working/waiting.
    assert "function activityIconSvg" in js
    assert "ICON_PATHS" in js and "ACTIVITY_ICON" in js
    assert 'activityIconSvg(stateName)' in js   # in the activity pill
    assert "activityIconSvg(actState)" in js    # and per tab
    assert "act-pulse" in js and "act-spin" in js
    assert 'fill="currentColor"' in js  # else paths render black on dark tabs
    assert "@keyframes act-spin" in css and "@keyframes act-pulse" in css
    # 3-column tab grid (icon | title | close) so titles are not over-truncated.
    assert "grid-template-columns: 16px minmax(0, 1fr) 18px;" in css
