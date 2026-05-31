from __future__ import annotations

import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from .activity import TerminalActivity
from .commands import CommandSuggestion
from .domain import AgentSettings, Backend, ChatOptions, Complexity, Driver, DriverMode, ExecutionLocation
from .errors import YikesError
from .drivers import ensure_interactive_session
from .output import OutputContext, SessionOutputService
from .prompt_profile import DEFAULT_PROMPT_PROFILE_PATH, load_prompt_profile
from .services import BackendTransport, ChatTransport, Conversation
from .project_config import load_project_config
from .session_inventory import SessionInventory, SessionLifecycle, SessionSummary, project_label, record_direct_session
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
    managed_output: bool
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
            "managed_output": self.managed_output,
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
        self.output_view = "high"
        self.pending_new: NewSessionDraft | None = None
        self.lines: list[str] = []
        self.session_lines: dict[str, list[str]] = {}
        self.error: str | None = None
        self.submission_active = False
        self.pending_prompt: str | None = None
        self.live_follow_until = 0.0
        self._lock = RLock()
        self.output_service = SessionOutputService()
        self.prompt_profile = load_prompt_profile()
        self.process_manager = None  # set by the web layer; manages pane processes
        self._restore_latest_session()

    def state(self) -> dict[str, Any]:
        with self._locked():
            self._expire_live_follow_locked()
            sessions = self.sessions()
            active = self.active_session_id
            activity = self.session_activity(active) if active else None
            error = self.error
            self.error = None
            return {
                "brand": "yikes!",
                "status": self.conversation.status(),
                "start_cwd": str(self.start_cwd),
                "startup": self.startup_status(),
                "sessions": [self._summary_json(session, activity=activity if session.id == active else None) for session in sessions],
                "active_session_id": active,
                "active_session_activity": activity.to_json() if activity else None,
                "has_active_session": active is not None or bool(self.conversation.messages),
                "output_view": self.output_view,
                "output_text": self.output_text(),
                "controls": self.controls(),
                "links": _links_for(next((s for s in sessions if s.id == active), None)),
                "processes": self.process_manager.snapshot() if self.process_manager else {},
                "pending_new": self.pending_new.to_json() if self.pending_new else None,
                "submission_active": self.submission_active,
                "error": error,
            }

    def sessions(self) -> list[SessionSummary]:
        sessions = [session for session in self.inventory.list() if session.state not in {"dead", "stopped"}]
        active = self.active_session_id
        if active and all(session.id != active for session in sessions):
            options = self.conversation.options
            sessions.insert(
                0,
                SessionSummary(
                    id=active,
                    runtime=options.driver.value,
                    backend=options.backend.value,
                    state="created",
                    location=str(options.cwd),
                    detail="pending first turn",
                    name=project_label(options.cwd),
                ),
            )
        return sessions

    def output_text(self) -> str:
        live_follow_active = self.live_follow_until > time.monotonic()
        return self.output_service.render(
            self.lifecycle,
            OutputContext(
                active_session_id=self.active_session_id,
                output_view=self.output_view,
                lines=self._current_lines(),
                submission_active=self.submission_active,
                live_follow_active=live_follow_active,
                pending_prompt=self.pending_prompt,
            ),
        )

    def session_activity(self, session_id: str | None = None) -> TerminalActivity:
        selected = session_id or self.active_session_id
        if selected == self.active_session_id and self.submission_active:
            return TerminalActivity("thinking", "working", 0.72, "request is running or terminal output is being followed")
        if selected == self.active_session_id and self.conversation.options.mode is DriverMode.CLI:
            if selected in self.session_lines or self.conversation.messages:
                return TerminalActivity("idle", "idle", 0.65, "direct CLI session is ready")
        return self.output_service.activity(self.lifecycle, selected)

    def startup_status(self) -> dict[str, object]:
        return {
            "prompt_profile_path": str(DEFAULT_PROMPT_PROFILE_PATH.expanduser()),
            "setup_variants": len(self.prompt_profile.setup_variants),
            "boundary_templates": len(self.prompt_profile.boundary_templates),
            "marker_pairs": len(self.prompt_profile.marker_pairs),
            "profile_shared_for": ["codex", "claude"],
        }

    def controls(self) -> dict[str, Any]:
        model = self.conversation.options.model or "default"
        model_options = [option.name for option in self.conversation.model_registry.options(self.conversation.options.backend)]
        if model not in model_options:
            model_options.append(model)
        return {
            "model": model,
            "model_options": model_options,
            "web_search": "on" if self.conversation.options.settings.web_search_enabled else "off",
            "editable": self.active_session_id is not None,
        }

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
        driver = self.conversation.options.mode
        managed_output = False if driver is DriverMode.TMUX else state.settings.managed_output_enabled
        self.pending_new = NewSessionDraft(
            backend=state.backend,
            location=self.conversation.options.location,
            driver=driver,
            model=state.model or "default",
            complexity=state.complexity,
            web_search=state.settings.web_search_enabled,
            managed_output=managed_output,
            root=None,
            choices={
                "backend": [backend.value for backend in Backend],
                "location": [location.value for location in ExecutionLocation],
                "driver": [mode.value for mode in DriverMode],
                "model": [option.name for option in self.conversation.model_registry.options(state.backend)],
                "complexity": [level.value for level in Complexity],
                "web_search": ["on", "off"],
                "managed_output": ["on", "off"],
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
            if draft.driver is DriverMode.TMUX:
                draft.managed_output = False
        if "model" in changes:
            draft.model = str(changes["model"]) or "default"
        if "complexity" in changes:
            draft.complexity = Complexity(str(changes["complexity"]))
        if "web_search" in changes:
            value = changes["web_search"]
            draft.web_search = value if isinstance(value, bool) else str(value).lower() in {"on", "true", "1", "yes"}
        if "managed_output" in changes:
            value = changes["managed_output"]
            draft.managed_output = value if isinstance(value, bool) else str(value).lower() in {"on", "true", "1", "yes"}
        if "root" in changes:
            root = str(changes["root"] or "").strip()
            draft.root = Path(root).expanduser() if root else None
        return self.state()

    def update_active_config(self, **changes: object) -> dict[str, Any]:
        if "model" in changes:
            model = str(changes["model"] or "default")
            self.conversation.set_model(None if model == "default" else model)
        if "web_search" in changes:
            value = changes["web_search"]
            enabled = value if isinstance(value, bool) else str(value).lower() in {"on", "true", "1", "yes"}
            self.conversation.set_web_search(enabled)
        if self.conversation.options.driver is Driver.DIRECT:
            record_direct_session(
                self.conversation.options,
                user_data={
                    "location": self.conversation.options.location.value,
                    "mode": self.conversation.options.mode.value,
                },
            )
        self._save_current_state()
        return self.state()

    def confirm_new_session(self) -> dict[str, Any]:
        if self.pending_new is None:
            self.open_new_session()
        assert self.pending_new is not None
        draft = self.pending_new
        settings = AgentSettings(
            web_search_enabled=draft.web_search,
            managed_output_enabled=draft.managed_output,
        )
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
        if draft.driver is DriverMode.TMUX:
            self.output_view = "dev"
            try:
                ensure_interactive_session(options)
            except Exception as exc:
                self.error = f"Failed to start interactive tmux session: {exc}"
        self.conversation.clear()
        record_direct_session(
            options,
            user_data={
                "location": draft.location.value,
                "mode": draft.driver.value,
            },
        )
        self.active_session_id = options.session_id
        self._set_current_lines([
            "Ready. Type a message and press Enter.",
            (
                "New session: "
                f"{draft.backend.value} on {draft.location.value} via {draft.driver.value}; "
                f"model {draft.model}; complexity {draft.complexity.value}; "
                f"web {'on' if draft.web_search else 'off'}; "
                f"capture {'on' if draft.managed_output else 'off'}; "
                f"root {cwd}"
            ),
        ])
        self.pending_new = None
        self._save_current_state()
        return self.state()

    def cancel_new_session(self) -> dict[str, Any]:
        self.pending_new = None
        return self.state()

    def switch_session(self, session_id: str) -> dict[str, Any]:
        if session_id == self.active_session_id:
            return self.state()
        summary = self.lifecycle.summary(session_id)
        if summary is not None and summary.state in {"dead", "stopped"}:
            self.error = f"Session {session_id} is {summary.state}. Close it and create or select another session."
            return self.state()
        options = self.lifecycle.switch_options(self.conversation.options, session_id)
        if options is None:
            self.error = f"Session not found: {session_id}"
            return self.state()
        self.conversation.set_options(options)
        self._sync_output_view_for_options(options)
        self.active_session_id = session_id
        snapshot = self.lifecycle.snapshot(session_id)
        if snapshot:
            self._set_current_lines(snapshot.splitlines())
        elif session_id not in self.session_lines:
            self._set_current_lines([f"Switched to {session_id}."])
        self._save_current_state()
        return self.state()

    def close_session(self, session_id: str) -> dict[str, Any]:
        result = self.lifecycle.close(session_id)
        if self.active_session_id == session_id:
            self.active_session_id = None
            self.conversation.clear()
            self.lines = []
            self.session_lines.pop(session_id, None)
        else:
            self._append_line(result.message)
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

    def add_pane(self, session_id: str, value: str, *, title: str | None = None) -> dict[str, Any]:
        session = next((s for s in self.sessions() if s.id == session_id), None)
        value = (value or "").strip()
        if session is None or not value:
            return self.state()
        pane: dict[str, Any] = {"kind": "web"}
        if value.isdigit():
            pane["port"] = int(value)
            pane["title"] = title or f"Port {value}"
        else:
            url = value if "://" in value else f"http://{value}"
            pane["url"] = url
            pane["title"] = title or url.split("://", 1)[-1]
        from .project_config import append_local_pane

        append_local_pane(Path(session.location), pane)
        return self.state()

    def start_pane_process(self, session_id: str, pane_id: str) -> dict[str, Any]:
        spec = self._pane_process_spec(session_id, pane_id)
        if self.process_manager is not None and spec and spec.get("start"):
            from .process_manager import ManagedProcessManager

            self.process_manager.start(
                ManagedProcessManager.key(session_id, pane_id),
                spec["start"],
                spec["cwd"],
                stop_command=spec.get("stop"),
            )
        return self.state()

    def stop_pane_process(self, session_id: str, pane_id: str) -> dict[str, Any]:
        if self.process_manager is not None:
            from .process_manager import ManagedProcessManager

            self.process_manager.stop(ManagedProcessManager.key(session_id, pane_id))
        return self.state()

    def _pane_process_spec(self, session_id: str, pane_id: str) -> dict[str, Any] | None:
        session = next((s for s in self.sessions() if s.id == session_id), None)
        if session is None or not pane_id.startswith("pane-"):
            return None
        config = _session_config(session)
        if config is None:
            return None
        try:
            pane = config.panes[int(pane_id.split("-", 1)[1])]
        except (ValueError, IndexError):
            return None
        return {"start": pane.get("start"), "stop": pane.get("stop"), "cwd": session.location}

    def attach_command(self, session_id: str | None = None) -> tuple[str, list[str]] | None:
        selected = session_id or self._attachable_session_id()
        if not selected:
            return None
        command = self.lifecycle.attach_command(selected)
        if command is None:
            return None
        return selected, command

    def resize_terminal(self, session_id: str | None, *, cols: int, rows: int) -> dict[str, Any]:
        selected = session_id or self._attachable_session_id()
        if not selected:
            return {"ok": False, "message": "No attachable tmux session is selected."}
        result = self.lifecycle.resize(selected, cols=cols, rows=rows)
        return {"ok": result.closed, "message": result.message, "session_id": selected}

    def submit(self, text: str) -> dict[str, Any]:
        if not self.begin_submit(text):
            return self.state()
        return self.finish_submit()

    def begin_submit(self, text: str) -> bool:
        text = text.strip()
        if not text:
            return False
        with self._locked():
            self.error = None
            if self.submission_active:
                self.error = "A request is already running for this session."
                return False
            if text.startswith("/"):
                self._slash(text)
                return False
            if self.active_session_id is None and not self.conversation.messages:
                self.error = "No active session. Use /new or the New Session button first."
                return False
            if self.active_session_id is not None:
                summary = self.lifecycle.summary(self.active_session_id)
                if summary is not None and summary.state in {"dead", "stopped"}:
                    self.error = f"Session {self.active_session_id} is {summary.state}. Close it and create or select another session."
                    self.active_session_id = None
                    return False
            self.pending_prompt = text
            self.submission_active = True
            self.live_follow_until = 0.0
            self._append_line(f"You: {text}")
            self._append_line("Working...")
            return True

    def finish_submit(self) -> dict[str, Any]:
        text = self.pending_prompt
        if not text:
            return self.state()
        try:
            answer = self.conversation.ask(text)
        except Exception as exc:
            with self._locked():
                self._append_line(f"Error: {exc}")
                self.error = str(exc)
                self.submission_active = False
                self.pending_prompt = None
                self.live_follow_until = 0.0
            return self.state()
        with self._locked():
            lines = self._current_lines()
            if lines and lines[-1] == "Working...":
                lines.pop()
            if answer:
                self._append_line(f"Assistant: {answer}")
            if self._is_unmanaged_tmux_session():
                self.submission_active = False
                self.live_follow_until = time.monotonic() + 45.0
            else:
                self.submission_active = False
                self.pending_prompt = None
                self.live_follow_until = 0.0
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
            view = _normalize_output_view(value)
            if view:
                self.output_view = view
                self._append_line(f"yikes!: Output view set to {view}.")
            return self.state()
        result = self.conversation.run_slash_command(text)
        if result.message:
            self._append_line(f"yikes!: {result.message}")
        if result.exit_requested:
            self._append_line("yikes!: Exit requested.")
        if result.restart_requested:
            self._append_line("yikes!: Restart requested.")
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
        live_sessions = [session for session in sessions if session.state not in {"dead", "stopped"}]
        if not live_sessions:
            self.lines = [
                "Ready. No active session.",
                (
                    "Prompt profile ready: "
                    f"{len(self.prompt_profile.setup_variants)} setup variants, "
                    f"{len(self.prompt_profile.boundary_templates)} boundary variants, "
                    "shared for Codex and Claude."
                ),
                "Create a new session to start.",
            ]
            return
        latest = live_sessions[0]
        self.active_session_id = latest.id
        options = self.lifecycle.switch_options(self.conversation.options, latest.id)
        if options is not None:
            self.conversation.set_options(options)
            self._sync_output_view_for_options(options)
        snapshot = self.lifecycle.snapshot(latest.id)
        self._set_current_lines(snapshot.splitlines() if snapshot else [f"Restored session {latest.id}."])

    def _attachable_session_id(self) -> str | None:
        if self.active_session_id and self.lifecycle.attach_command(self.active_session_id):
            return self.active_session_id
        for session in self.sessions():
            if self.lifecycle.attach_command(session.id):
                return session.id
        return None

    def _save_current_state(self) -> None:
        save_app_state(AppState.from_options(self.conversation.options))

    def _expire_live_follow_locked(self) -> None:
        if self.live_follow_until and time.monotonic() >= self.live_follow_until:
            self.live_follow_until = 0.0
            self.submission_active = False
            self.pending_prompt = None

    def _sync_output_view_for_options(self, options: ChatOptions) -> None:
        if options.mode is DriverMode.TMUX and not options.settings.managed_output_enabled:
            self.output_view = "dev"

    def _is_unmanaged_tmux_session(self) -> bool:
        return (
            self.conversation.options.mode is DriverMode.TMUX
            and not self.conversation.options.settings.managed_output_enabled
        )

    def _current_lines(self) -> list[str]:
        if self.active_session_id is None:
            return self.lines
        return self.session_lines.get(self.active_session_id, self.lines)

    def _set_current_lines(self, lines: list[str]) -> None:
        if self.active_session_id is None:
            self.lines = lines
        else:
            self.session_lines[self.active_session_id] = lines

    def _append_line(self, line: str) -> None:
        self._current_lines().append(line)

    @contextmanager
    def _locked(self):
        with self._lock:
            yield

    @staticmethod
    def _summary_json(session: SessionSummary, *, activity: TerminalActivity | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": session.id,
            "name": session.name or session.id[-6:],
            "runtime": session.runtime,
            "backend": session.backend,
            "state": session.state,
            "location": session.location,
            "detail": session.detail,
            "panes": _panes_for(session),
        }
        if activity is not None:
            data["activity"] = activity.to_json()
        return data

    @staticmethod
    def _driver_for(location: ExecutionLocation, mode: DriverMode, settings: AgentSettings) -> Driver:
        if location is ExecutionLocation.REMOTE:
            raise YikesError("Remote runtime is planned for remote machines, but is not supported yet.")
        if mode is DriverMode.API:
            raise YikesError("API driver mode is not supported yet.")
        if location is ExecutionLocation.DOCKER:
            return Driver.DOCKER
        return Driver.TMUX if mode is DriverMode.TMUX else Driver.DIRECT


def _session_config(session: SessionSummary | None):
    """Best-effort project config for a session (from its cwd), never raising."""
    if session is None:
        return None
    try:
        return load_project_config(Path(session.location))
    except Exception:
        return None  # a bad yikes.toml must not break the whole state payload


def _panes_for(session: SessionSummary) -> list[dict]:
    """Sub-tabs for a session: the live terminal first, then configured panes."""
    panes: list[dict] = []
    if session.runtime in {"tmux", "docker"}:
        panes.append({"id": "terminal", "kind": "terminal", "title": "Terminal"})
    config = _session_config(session)
    if config is not None:
        for index, pane in enumerate(config.panes):
            entry = {**pane, "id": f"pane-{index}"}
            entry["canControl"] = bool(pane.get("start"))
            panes.append(entry)
    return panes


def _links_for(session: SessionSummary | None) -> list[dict]:
    config = _session_config(session)
    return [dict(link) for link in config.links] if config is not None else []


def _normalize_output_view(value: str) -> str | None:
    normalized = value.strip().lower()
    if normalized in {"high", "normal", "transcript", "extracted"}:
        return "high"
    if normalized in {"dev", "debug", "raw", "full"}:
        return "dev"
    return None
