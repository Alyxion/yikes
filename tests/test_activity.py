from __future__ import annotations

from yikes import (
    ACTIVITY_AWAITING_SELECTION,
    ACTIVITY_IDLE,
    ACTIVITY_STREAMING,
    ACTIVITY_THINKING,
    ACTIVITY_UNKNOWN,
    ActivityMonitor,
    classify_terminal_snapshot,
)


def test_activity_detects_numbered_selection_prompt() -> None:
    snapshot = """
Do you trust the files in this folder?
  1. Yes, continue
  2. No, quit
"""

    activity = classify_terminal_snapshot(snapshot, now=1.0)

    assert activity.state == ACTIVITY_AWAITING_SELECTION
    assert "choice" in activity.reason or "prompt" in activity.reason


def test_activity_detects_chat_about_this_prompt() -> None:
    snapshot = """
This command wants to edit files.
  1. Allow
  2. Deny
  3. Chat about this
"""

    activity = classify_terminal_snapshot(snapshot, now=1.0)

    assert activity.state == ACTIVITY_AWAITING_SELECTION


def test_activity_numbered_prose_in_scrollback_is_not_selection() -> None:
    # The agent printed a numbered list earlier (scrollback) and the words
    # "allow"/"select" appear in prose, but the live prompt at the bottom is
    # idle. The numbered items are well above the live prompt region, so this
    # must read as idle, not awaiting-selection.
    scrollback = [
        "Here is the plan I would select for you:",
        "  1. Set up the workspace",
        "  2. Migrate the modules",
        "  3. Allow incremental rollout",
        "  4. Optimize the build",
    ]
    filler = [f"  done step {n}" for n in range(20)]
    footer = [
        "I'm here whenever you want to dive back in — just say the word.",
        "✻ Crunched for 4s",
        "─────────────── dynamicslides ──",
        "❯ clean up the dead studio CSS",
        "───────────────",
        "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
    ]
    snapshot = "\n".join(scrollback + filler + footer)

    activity = classify_terminal_snapshot(snapshot, now=1.0)

    assert activity.state == ACTIVITY_IDLE


def test_activity_detects_thinking_indicator() -> None:
    snapshot = """
> Improve documentation in @filename

Working (3s • esc to interrupt)
"""

    activity = classify_terminal_snapshot(snapshot, now=1.0)

    assert activity.state == ACTIVITY_THINKING
    assert activity.label == "thinking"


def test_activity_idle_prompt_with_keywords_is_not_thinking() -> None:
    # An idle Claude prompt whose text merely contains words like "working"
    # must not be mistaken for an active run.
    snapshot = """
> Yep, alive and ready.
  What are you working on in experiments/dashboard? I can help with code.
> what's in this dashboard project?
  don't ask on (shift+tab to cycle) · ← for agents
"""

    activity = classify_terminal_snapshot(snapshot, now=1.0)

    assert activity.state == ACTIVITY_IDLE


def test_activity_working_with_menu_shaped_text_is_thinking_not_selection() -> None:
    # Mid-run output can contain numbered/"confirm" text; the active "esc to
    # interrupt" indicator must win over the selection-prompt heuristic.
    snapshot = """
=== rest of test_p2p_webrtc_integration.py (from line 80) ===
- The 'host' sample — confirm this means the server/proxy path
Musing… (3m 20s · ↓ 12.8k tokens)
  auto mode on (shift+tab to cycle) · esc to interrupt
"""

    activity = classify_terminal_snapshot(snapshot, now=1.0)

    assert activity.state == ACTIVITY_THINKING


def test_typing_into_input_prompt_is_not_streaming() -> None:
    # Editing the agent's bottom input box (Claude/Codex) changes the screen, but
    # it must read as idle — the agent isn't streaming, the user is typing.
    monitor = ActivityMonitor()

    def screen(typed: str) -> str:
        return (
            "Want me to also surface newly-created resources?\n"
            "* Brewed for 5m 59s\n"
            "────────────────────\n"
            f"> {typed}\n"
            "  auto mode on (shift+tab to cycle)"
        )

    monitor.observe("s", screen("a"), now=1.0)
    states = [
        monitor.observe("s", screen(text), now=2.0 + index).state
        for index, text in enumerate(["al", "alri", "alrighty", "alrighty!"])
    ]
    assert all(state == ACTIVITY_IDLE for state in states), states


def test_codex_prompt_typing_is_not_streaming() -> None:
    monitor = ActivityMonitor()

    def screen(typed: str) -> str:
        return (
            "Result: 79 passed. Current worktree is clean.\n"
            "── Worked for 2m 31s ──\n"
            f"> {typed}\n"
            "  gpt-5.5 high · ~/projects/office-connect"
        )

    monitor.observe("s", screen("a"), now=1.0)
    states = [monitor.observe("s", screen(t), now=2.0 + i).state for i, t in enumerate(["am", "amaz", "amazing!"])]
    assert all(state == ACTIVITY_IDLE for state in states), states


def test_activity_monitor_detects_streaming_on_sustained_change() -> None:
    monitor = ActivityMonitor()

    first = monitor.observe("session-1", "Assistant: starting", now=1.0)
    # A single changed snapshot is debounced (could be a transient repaint).
    second = monitor.observe("session-1", "Assistant: starting\nmore", now=2.0)
    # A second consecutive change is real streaming.
    third = monitor.observe("session-1", "Assistant: starting\nmore\nmore2", now=3.0)

    assert first.state == ACTIVITY_IDLE
    assert second.state == ACTIVITY_IDLE
    assert third.state == ACTIVITY_STREAMING
    assert third.changed is True


def test_activity_monitor_ignores_isolated_repaint() -> None:
    """A one-frame change (e.g. attach/resize repaint) must not flip to streaming."""
    monitor = ActivityMonitor()

    monitor.observe("session-1", "prompt", now=1.0)
    blip = monitor.observe("session-1", "prompt (redrawn)", now=2.0)  # single change
    after = monitor.observe("session-1", "prompt (redrawn)", now=3.0)  # stable again

    assert blip.state == ACTIVITY_IDLE
    assert after.state == ACTIVITY_IDLE


def test_activity_monitor_holds_streaming_through_one_stable_frame() -> None:
    """Once streaming, a single identical frame should not drop straight to idle."""
    monitor = ActivityMonitor()

    monitor.observe("s", "a", now=1.0)
    monitor.observe("s", "a\nb", now=2.0)
    streaming = monitor.observe("s", "a\nb\nc", now=3.0)
    held = monitor.observe("s", "a\nb\nc", now=4.0)  # one identical frame
    idle = monitor.observe("s", "a\nb\nc", now=5.0)  # two identical -> idle

    assert streaming.state == ACTIVITY_STREAMING
    assert held.state == ACTIVITY_STREAMING
    assert idle.state == ACTIVITY_IDLE


def test_activity_monitor_reports_idle_when_snapshot_is_stable() -> None:
    monitor = ActivityMonitor()

    monitor.observe("session-1", "Assistant: done", now=1.0)
    activity = monitor.observe("session-1", "Assistant: done", now=2.0)

    assert activity.state == ACTIVITY_IDLE
    assert activity.changed is False


def test_activity_reports_unknown_without_snapshot() -> None:
    activity = classify_terminal_snapshot("", now=1.0)

    assert activity.state == ACTIVITY_UNKNOWN
