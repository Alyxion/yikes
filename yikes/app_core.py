from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .commands import CommandSuggestion
from .domain import AgentSettings, Backend, ChatOptions, Complexity, Driver, DriverMode, ExecutionLocation
from .errors import YikesError
from .services import BackendTransport, ChatTransport, Conversation
from .session_inventory import SessionInventory, SessionLifecycle, SessionSummary
from .state import AppState, load_app_state, save_app_state


@dataclass
class NewSessionDraft:
    """UI-neutral new-session wizard state.

    Textual, the web app, and a future llming-com session use this object
    instead of carrying separate option lists in each frontend.
    """

    backend: Backend
    location: ExecutionLocation
    driver: DriverMode
    model: str
    complexity: Complexity
    web_search: bool
    root: Path | None = None
    choices: dict[str, list[str]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "backend": self.backend.value,
            "location": self.location.value,
            "driver": self.driver.value,
            "model": self.model,
            "complexity": self.complexity.value,
            "web_search": self.web_search,
            "root": str(self.root) if self.root else "",
            "choices": self.choices,
        }


class YikesAppController:
    """Shared application controller for terminal, web, and embedded sessions."""

    def __init__(
        self,
        *,
        cwd: Path | None = None,
        timeout: float = 180.0,
        transport: ChatTransport | None = None,
    ) -> None:
        self.start_cwd = (cwd or Path.cwd()).expanduser()
        self.timeout = timeout
        self.inventory = SessionInventory()
        self.lifecycle = SessionLifecycle()
        self.conversation = Conversation(
            self._initial_options(),
            transport=transport or BackendTransport(),
        )
        self.active_session_id: str | None = None
        self.output_view = "extracted"
        self.pending_new: NewSessionDraft | None = None
        self.lines: list[str] = []
        self.error: str | None = None
        self._restore_latest_session()

    def state(self) -> dict[str, Any]:
        sessions = self.sessions()
        active = self.active_session_id
        return {
            "brand": "yikes!",
            "status": self.conversation.status(),
            "sessions": [self._summary_json(session) for session in sessions],
            "active_session_id": active,
            "has_active_session": active is not None or bool(self.conversation.messages),
            "output_view": self.output_view,
            "output_text": self.output_text(),
            "pending_new": self.pending_new.to_json() if self.pending_new else None,
            "error": self.error,
        }

    def sessions(self) -> list[SessionSummary]:
        return self.inventory.list()

    def output_text(self) -> str:
        return "\n".join(self.lines[-1200:])

    def suggestions(self, text: str) -> list[dict[str, str]]:
        suggestions = self.conversation.slash_suggestions(text)
        return [
            {
                "value": suggestion.value,
                "description": suggestion.description,
                "completion": suggestion.completion or suggestion.value,
            }
            for suggestion in suggestions
        ]

    def open_new_session(self) -> dict[str, Any]:
        state = load_app_state()
        self.pending_new = NewSessionDraft(
            backend=state.backend,
            location=self.conversation.options.location,
            driver=self.conversation.options.mode,
            model=state.model or "default",
            complexity=state.complexity,
            web_search=state.settings.web_search_enabled,
            root=None,
            choices={
                "backend": [backend.value for backend in Backend],
                "location": [location.value for location in ExecutionLocation],
                "driver": [mode.value for mode in DriverMode],
                "model": [option.name for option in self.conversation.model_registry.options(state.backend)],
                "complexity": [level.value for level in Complexity],
                "web_search": ["on", "off"],
            },
        )
        return self.state()

    def update_new_session(self, **changes: object) -> dict[str, Any]:
        if self.pending_new is None:
            self.open_new_session()
        assert self.pending_new is not None
        draft = self.pending_new
        if "backend" in changes:
            draft.backend = Backend(str(changes["backend"]))
            draft.choices["model"] = [option.name for option in self.conversation.model_registry.options(draft.backend)]
            if draft.model not in draft.choices["model"]:
                draft.model = "default"
        if "location" in changes:
            draft.location = ExecutionLocation(str(changes["location"]))
        if "driver" in changes:
            draft.driver = DriverMode(str(changes["driver"]))
        if "model" in changes:
            draft.model = str(changes["model"]) or "default"
        if "complexity" in changes:
            draft.complexity = Complexity(str(changes["complexity"]))
        if "web_search" in changes:
            value = changes["web_search"]
            draft.web_search = value if isinstance(value, bool) else str(value).lower() in {"on", "true", "1", "yes"}
        if "root" in changes:
            root = str(changes["root"] or "").strip()
            draft.root = Path(root).expanduser() if root else None
        return self.state()

    def confirm_new_session(self) -> dict[str, Any]:
        if self.pending_new is None:
            self.open_new_session()
        assert self.pending_new is not None
        draft = self.pending_new
        settings = AgentSettings(web_search_enabled=draft.web_search)
        driver = self._driver_for(draft.location, draft.driver, settings)
        if draft.location is ExecutionLocation.DOCKER and draft.driver is DriverMode.TMUX:
            settings = settings.with_tmux(True)
        cwd = draft.root or Path(tempfile.mkdtemp(prefix="yikes-session-"))
        options = ChatOptions(
            backend=draft.backend,
            driver=driver,
            cwd=cwd,
            cwd_explicit=draft.root is not None,
            timeout=self.timeout,
            model=None if draft.model == "default" else draft.model,
            complexity=draft.complexity,
            settings=settings,
        )
        self.conversation.set_options(options)
        self.conversation.clear()
        self.active_session_id = options.session_id
        self.lines = [
            "Ready. Type a message and press Enter.",
            f"New session: {draft.location.value}/{draft.driver.value}/{draft.backend.value}",
        ]
        self.pending_new = None
        self._save_current_state()
        return self.state()

    def cancel_new_session(self) -> dict[str, Any]:
        self.pending_new = None
        return self.state()

    def switch_session(self, session_id: str) -> dict[str, Any]:
        options = self.lifecycle.switch_options(self.conversation.options, session_id)
        if options is None:
            self.error = f"Session not found: {session_id}"
            return self.state()
        self.conversation.set_options(options)
        self.active_session_id = session_id
        snapshot = self.lifecycle.snapshot(session_id)
        self.lines = snapshot.splitlines() if snapshot else [f"Switched to {session_id}."]
        self._save_current_state()
        return self.state()

    def close_session(self, session_id: str) -> dict[str, Any]:
        result = self.lifecycle.close(session_id)
        self.lines.append(result.message)
        if self.active_session_id == session_id:
            self.active_session_id = None
            self.conversation.clear()
            self.lines = []
        return self.state()

    def close_all(self) -> dict[str, Any]:
        results = self.lifecycle.close_all(runtime="all")
        self.active_session_id = None
        self.conversation.clear()
        self.lines = []
        self.error = None
        if not results:
            self.lines = []
        return self.state()

    def attach_command(self, session_id: str | None = None) -> tuple[str, list[str]] | None:
        selected = session_id or self._attachable_session_id()
        if not selected:
            return None
        command = self.lifecycle.attach_command(selected)
        if command is None:
            return None
        return selected, command

    def submit(self, text: str) -> dict[str, Any]:
        text = text.strip()
        if not text:
            return self.state()
        self.error = None
        if text.startswith("/"):
            return self._slash(text)
        if self.active_session_id is None and not self.conversation.messages:
            self.error = "No active session. Use /new or the New Session button first."
            return self.state()
        self.lines.append(f"You: {text}")
        self.lines.append("Working...")
        try:
            answer = self.conversation.ask(text)
        except Exception as exc:
            self.lines.append(f"Error: {exc}")
            self.error = str(exc)
            return self.state()
        if self.lines and self.lines[-1] == "Working...":
            self.lines.pop()
        self.lines.append(f"Assistant: {answer}")
        self._save_current_state()
        return self.state()

    def directory_entries(self, root: str | None = None) -> dict[str, Any]:
        base = Path(root).expanduser() if root else self.start_cwd
        try:
            entries = sorted(
                path for path in base.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            )
        except OSError as exc:
            return {"root": str(base), "error": str(exc), "entries": []}
        parent = base.parent if base.parent != base else None
        return {
            "root": str(base),
            "parent": str(parent) if parent else "",
            "entries": [{"name": path.name, "path": str(path)} for path in entries[:200]],
        }

    def _slash(self, text: str) -> dict[str, Any]:
        if text == "/new" or text.startswith("/new "):
            self.open_new_session()
            return self.state()
        if text.startswith("/view "):
            _, _, value = text.partition(" ")
            if value in {"full", "extracted"}:
                self.output_view = value
                self.lines.append(f"yikes!: Output view set to {value}.")
            return self.state()
        result = self.conversation.run_slash_command(text)
        if result.message:
            self.lines.append(f"yikes!: {result.message}")
        if result.exit_requested:
            self.lines.append("yikes!: Exit requested.")
        if result.restart_requested:
            self.lines.append("yikes!: Restart requested.")
        self._save_current_state()
        return self.state()

    def _initial_options(self) -> ChatOptions:
        state = load_app_state()
        return ChatOptions(
            backend=state.backend,
            driver=state.driver,
            cwd=self.start_cwd,
            timeout=self.timeout,
            model=state.model,
            complexity=state.complexity,
            settings=state.settings,
        )

    def _restore_latest_session(self) -> None:
        sessions = self.sessions()
        if not sessions:
            self.lines = []
            return
        latest = sessions[0]
        self.active_session_id = latest.id
        options = self.lifecycle.switch_options(self.conversation.options, latest.id)
        if options is not None:
            self.conversation.set_options(options)
        snapshot = self.lifecycle.snapshot(latest.id)
        self.lines = snapshot.splitlines() if snapshot else [f"Restored session {latest.id}."]

    def _attachable_session_id(self) -> str | None:
        if self.active_session_id and self.lifecycle.attach_command(self.active_session_id):
            return self.active_session_id
        for session in self.sessions():
            if self.lifecycle.attach_command(session.id):
                return session.id
        return None

    def _save_current_state(self) -> None:
        save_app_state(AppState.from_options(self.conversation.options))

    @staticmethod
    def _summary_json(session: SessionSummary) -> dict[str, str]:
        return {
            "id": session.id,
            "runtime": session.runtime,
            "backend": session.backend,
            "state": session.state,
            "location": session.location,
            "detail": session.detail,
        }

    @staticmethod
    def _driver_for(location: ExecutionLocation, mode: DriverMode, settings: AgentSettings) -> Driver:
        if location is ExecutionLocation.REMOTE:
            raise YikesError("Remote runtime is planned for remote machines, but is not supported yet.")
        if mode is DriverMode.API:
            raise YikesError("API driver mode is not supported yet.")
        if location is ExecutionLocation.DOCKER:
            return Driver.DOCKER
        return Driver.TMUX if mode is DriverMode.TMUX else Driver.DIRECT
