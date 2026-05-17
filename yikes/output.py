from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .activity import ActivityMonitor, TerminalActivity
from .transcript import high_level_transcript


class SnapshotSource(Protocol):
    def snapshot(self, session_id: str, *, lines: int = 400) -> str | None: ...
    def capture_markers(self, session_id: str) -> tuple[tuple[str, str], ...]: ...


@dataclass(frozen=True)
class OutputContext:
    active_session_id: str | None
    output_view: str
    lines: list[str]
    submission_active: bool = False
    live_follow_active: bool = False
    pending_prompt: str | None = None
    limit: int = 1200


class SessionOutputService:
    """Render session output for TUI, web, and embedded controllers."""

    def __init__(self, activity_monitor: ActivityMonitor | None = None) -> None:
        self.activity_monitor = activity_monitor or ActivityMonitor()

    def render(self, lifecycle: SnapshotSource, context: OutputContext) -> str:
        if context.active_session_id:
            snapshot = lifecycle.snapshot(context.active_session_id, lines=context.limit)
            if snapshot:
                if context.output_view == "dev":
                    return self._with_pending_raw(snapshot, context)
                capture_markers = getattr(lifecycle, "capture_markers", None)
                markers = capture_markers(context.active_session_id) if callable(capture_markers) else ()
                transcript = high_level_transcript(snapshot, fallback_to_raw=False, markers=markers)
                return self._with_pending_turn(transcript, context)
        return "\n".join(context.lines[-context.limit:])

    def activity(self, lifecycle: SnapshotSource, session_id: str | None) -> TerminalActivity:
        snapshot = lifecycle.snapshot(session_id, lines=120) if session_id else None
        return self.activity_monitor.observe(session_id or "", snapshot)

    @staticmethod
    def _with_pending_turn(transcript: str, context: OutputContext) -> str:
        if not (context.submission_active or context.live_follow_active) or not context.pending_prompt:
            return transcript
        assistant = "Working..." if context.submission_active else ""
        pending = f"You: {context.pending_prompt}"
        if assistant:
            pending = f"{pending}\nAssistant: {assistant}"
        if pending in transcript:
            return transcript
        if transcript:
            return f"{transcript}\n\n{pending}"
        return pending

    @staticmethod
    def _with_pending_raw(snapshot: str, context: OutputContext) -> str:
        if not (context.submission_active or context.live_follow_active) or not context.pending_prompt:
            return snapshot
        if context.pending_prompt in snapshot:
            return snapshot
        pending = f"> {context.pending_prompt}"
        if context.submission_active:
            pending = f"{pending}\nWorking..."
        if pending in snapshot:
            return snapshot
        return f"{snapshot.rstrip()}\n\n{pending}"
