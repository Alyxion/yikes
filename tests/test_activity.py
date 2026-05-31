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


def test_activity_monitor_detects_streaming_when_terminal_changes() -> None:
    monitor = ActivityMonitor()

    first = monitor.observe("session-1", "Assistant: starting", now=1.0)
    second = monitor.observe("session-1", "Assistant: starting\nAssistant: more output", now=2.0)

    assert first.state == ACTIVITY_IDLE
    assert second.state == ACTIVITY_STREAMING
    assert second.changed is True


def test_activity_monitor_reports_idle_when_snapshot_is_stable() -> None:
    monitor = ActivityMonitor()

    monitor.observe("session-1", "Assistant: done", now=1.0)
    activity = monitor.observe("session-1", "Assistant: done", now=2.0)

    assert activity.state == ACTIVITY_IDLE
    assert activity.changed is False


def test_activity_reports_unknown_without_snapshot() -> None:
    activity = classify_terminal_snapshot("", now=1.0)

    assert activity.state == ACTIVITY_UNKNOWN
