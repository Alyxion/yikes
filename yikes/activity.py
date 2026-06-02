from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any


UNKNOWN = "unknown"
IDLE = "idle"
AWAITING_SELECTION = "awaiting-selection"
THINKING = "thinking"
STREAMING = "streaming"

ACTIVITY_UNKNOWN = UNKNOWN
ACTIVITY_IDLE = IDLE
ACTIVITY_AWAITING_SELECTION = AWAITING_SELECTION
ACTIVITY_THINKING = THINKING
ACTIVITY_STREAMING = STREAMING


@dataclass(frozen=True)
class TerminalActivity:
    """Current inferred activity for a terminal-backed agent session."""

    state: str
    label: str
    confidence: float
    reason: str
    changed: bool = False
    updated_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "label": self.label,
            "confidence": self.confidence,
            "reason": self.reason,
            "changed": self.changed,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class _Observation:
    digest: str
    nonblank_lines: int
    updated_at: float
    change_streak: int = 0  # consecutive observations whose snapshot changed
    stable_streak: int = 0  # consecutive observations whose snapshot was identical
    reported_state: str = IDLE


# Streaming uses hysteresis so a single transient repaint (attaching/viewing a
# tab, a resize reflow, a partial capture frame) does NOT flip an idle session to
# "streaming". Real streaming changes the snapshot on consecutive polls; an
# isolated blip does not.
_STREAM_ENTER = 2  # consecutive changed snapshots required to enter streaming
_STREAM_EXIT = 2   # consecutive stable snapshots required to leave streaming


class ActivityMonitor:
    """Stateful classifier over repeated rendered terminal snapshots."""

    def __init__(self) -> None:
        self._observations: dict[str, _Observation] = {}

    def observe(self, session_id: str, snapshot: str | None, *, now: float | None = None) -> TerminalActivity:
        current_time = time.time() if now is None else now
        normalized = _normalize(snapshot or "")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        nonblank_lines = sum(1 for line in normalized.splitlines() if line.strip())
        previous = self._observations.get(session_id)
        changed = previous is not None and previous.digest != digest

        if previous is None:
            change_streak = stable_streak = 0
        elif changed:
            change_streak, stable_streak = previous.change_streak + 1, 0
        else:
            change_streak, stable_streak = 0, previous.stable_streak + 1
        prev_reported = previous.reported_state if previous is not None else IDLE

        base = classify_terminal_snapshot(normalized, now=current_time)
        if base.state in {AWAITING_SELECTION, THINKING, UNKNOWN}:
            result = TerminalActivity(
                state=base.state,
                label=base.label,
                confidence=base.confidence,
                reason=base.reason,
                changed=changed,
                updated_at=current_time,
            )
        else:
            # Debounced idle/streaming decision (hysteresis on both edges).
            if prev_reported == STREAMING:
                streaming = stable_streak < _STREAM_EXIT
            else:
                streaming = change_streak >= _STREAM_ENTER
            if streaming:
                line_delta = nonblank_lines - (previous.nonblank_lines if previous is not None else nonblank_lines)
                reason = f"terminal output grew by {line_delta} lines" if line_delta > 0 else "terminal output changing"
                result = TerminalActivity(STREAMING, "streaming", 0.72, reason, changed=True, updated_at=current_time)
            else:
                result = TerminalActivity(IDLE, "idle", 0.58, "terminal snapshot is stable", changed=changed, updated_at=current_time)

        self._observations[session_id] = _Observation(
            digest, nonblank_lines, current_time, change_streak, stable_streak, result.state
        )
        return result


def classify_terminal_snapshot(snapshot: str, *, now: float | None = None) -> TerminalActivity:
    current_time = time.time() if now is None else now
    normalized = _normalize(snapshot)
    if not normalized.strip():
        return TerminalActivity(UNKNOWN, "unknown", 0.0, "no terminal snapshot", updated_at=current_time)
    # An active run ("esc to interrupt") means the agent is working, not waiting
    # for a choice — check it first so a menu-shaped snapshot mid-turn isn't
    # misread as a selection prompt.
    if _looks_like_thinking(normalized):
        return TerminalActivity(THINKING, "thinking", 0.78, "working indicator visible", updated_at=current_time)
    if _looks_like_selection_prompt(normalized):
        return TerminalActivity(
            AWAITING_SELECTION,
            "awaiting selection",
            0.86,
            "numbered choices or approval prompt visible",
            updated_at=current_time,
        )
    return TerminalActivity(IDLE, "idle", 0.50, "no active indicator visible", updated_at=current_time)


def _normalize(text: str) -> str:
    without_ansi = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "\n".join(line.rstrip() for line in without_ansi.replace("\r\n", "\n").replace("\r", "\n").splitlines())


def _looks_like_selection_prompt(text: str) -> bool:
    lower = text.lower()
    # Real menu choices: a single small number at the very start of a line (with
    # an optional cursor marker) — not "(from line 80)" buried in prose/code.
    choice_count = len(re.findall(r"(?m)^\s*(?:[›>❯•]\s*)?[1-9][.)]\s+\S", text))
    strong_phrases = (
        "do you trust",
        "do you want",
        "press enter to continue",
        "yes, continue",
        "no, quit",
        "chat about this",
    )
    weak_phrases = ("allow", "approve", "deny", "choose", "select")
    has_strong = any(phrase in lower for phrase in strong_phrases)
    has_weak = any(phrase in lower for phrase in weak_phrases)
    return has_strong or (choice_count >= 2 and (has_strong or has_weak))


def _looks_like_thinking(text: str) -> bool:
    # Only the backend's live "interrupt" hint reliably means a turn is running.
    # Matching plain keywords (e.g. the idle reply "What are you working on?")
    # produced false positives, so gate strictly on the interrupt indicator.
    lower_tail = "\n".join(line.lower() for line in text.splitlines()[-12:])
    return "esc to interrupt" in lower_tail or "ctrl+c to interrupt" in lower_tail
