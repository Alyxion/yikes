from __future__ import annotations

import fcntl
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import tty
from collections.abc import Callable

from .attachments import attachable_image_names, extract_image_attachments, read_clipboard_text, save_clipboard_image
from .activity import ActivityMonitor
from .capabilities import default_driver_registry
from .domain import AgentSettings, Backend, Complexity, Driver, DriverMode, ExecutionLocation, ImageAttachment
from .drivers import ensure_interactive_session
from .prompt_profile import DEFAULT_PROMPT_PROFILE_PATH, load_prompt_profile
from .services import ChatService, Conversation
from .session_inventory import SessionInventory, SessionLifecycle, record_direct_session
from .state import AppState, load_app_state, save_app_state
from .transcript import high_level_transcript


def run_tui(
    *,
    backend: Backend | None,
    driver: Driver | None,
    cwd: Path | None,
    timeout: float,
    model: str | None,
    complexity: Complexity | None,
    settings: AgentSettings | None,
) -> None:
    try:
        from textual import events, work
        from textual.app import App, ComposeResult
        from textual.containers import Container, Horizontal, Vertical
        from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Static, Tab, Tabs
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional runtime install
        raise RuntimeError("Textual is required for the full terminal app. Run: poetry install") from exc

    class PromptInput(Input):
        def action_paste(self) -> None:
            self.app.action_smart_paste()  # type: ignore[attr-defined]

    class TerminalApp(App[None]):
        TITLE = "yikes!"

        CSS = """
        Screen {
            background: $surface;
        }

        #layout {
            height: 1fr;
            width: 100%;
        }

        #sidebar {
            width: 24;
            border-right: solid $primary;
            padding: 1 1;
        }

        #chat {
            width: 1fr;
            padding: 1 2;
        }

        #session-tabs {
            height: 3;
        }

        #log {
            height: 1fr;
            border: solid $panel;
            padding: 1;
        }

        #no-session-panel {
            height: 1fr;
            border: solid $panel;
            padding: 2;
            content-align: center middle;
        }

        #no-session-message {
            height: auto;
            text-align: center;
            margin-bottom: 1;
        }

        #new-session-panel-button {
            width: 24;
        }

        #question-panel {
            height: 1fr;
            border: solid $primary;
            padding: 1 2;
        }

        #question-title {
            height: auto;
            margin-bottom: 1;
            color: $accent;
        }

        #question-body {
            height: 1fr;
        }

        #composer {
            height: 3;
            dock: bottom;
        }

        #suggestions {
            height: 4;
            padding: 0 1;
            color: $text-muted;
        }

        Input {
            width: 1fr;
        }

        .sidebar-button {
            width: 100%;
            margin-top: 1;
        }
        """

        BINDINGS = [
            ("ctrl+q", "quit", "Quit"),
            ("ctrl+l", "clear", "Clear"),
            ("ctrl+v", "smart_paste", "Paste"),
            ("super+v", "smart_paste", "Paste"),
            ("ctrl+o", "paste_image", "Paste Image"),
            ("tab", "accept_suggestion", "Complete"),
            ("escape", "hide_suggestions", "Hide"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.start_cwd = (cwd or Path.cwd()).expanduser()
            self.restart_requested = False
            self.attach_command: list[str] | None = None
            self.attach_session_id: str | None = None
            self.activity_monitor = ActivityMonitor()
            self.pending_images: list[ImageAttachment] = []
            self.saved_state = load_app_state()
            resolved_backend = backend or self.saved_state.backend
            driver_registry = default_driver_registry()
            resolved_driver = driver_registry.coerce(resolved_backend, driver or self.saved_state.driver)
            resolved_model = model if model is not None else self.saved_state.model
            resolved_complexity = complexity or self.saved_state.complexity
            resolved_settings = settings or self.saved_state.settings
            self.service = ChatService()
            self.preferred_session_id = os.environ.pop("YIKES_RETURN_SESSION_ID", "")
            self.active_session_id: str | None = None
            self.session_tab_ids: set[str] = set()
            self.live_snapshot_text = ""
            self.updating_session_tabs = False
            self.close_all_confirmation_pending = False
            self.has_active_session = False
            self.output_view = "high"
            self.submission_active = False
            self.question_mode = False
            self.question_browse_mode = False
            self.question_index = 0
            self.question_choices: dict[str, str] = {}
            self.question_browse_path = self.start_cwd
            self.question_browse_entries: list[Path] = []
            self.question_browse_index = 0
            self.conversation: Conversation = self.service.create_conversation(
                resolved_backend,
                resolved_driver,
                cwd=cwd,
                timeout=timeout,
                model=resolved_model,
                complexity=resolved_complexity,
                settings=resolved_settings,
            )

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Horizontal(id="layout"):
                with Container(id="sidebar"):
                    yield Label("yikes!")
                    yield Static("", id="backend-status")
                    yield Static("", id="location-status")
                    yield Static("", id="driver-status")
                    yield Static("", id="model-status")
                    yield Static("", id="complexity-status")
                    yield Static("", id="web-status")
                    yield Static("", id="capture-status")
                    yield Static("", id="activity-status")
                    yield Static("", id="session-count")
                    yield Button("New Session", id="new-session", classes="sidebar-button")
                    yield Button("Close", id="close-session", classes="sidebar-button")
                    yield Button("Fullscreen tmux", id="fullscreen-session", classes="sidebar-button")
                    yield Button("Close All", id="close-all", classes="sidebar-button")
                with Vertical(id="chat"):
                    yield Tabs(id="session-tabs")
                    with Container(id="no-session-panel"):
                        yield Static("", id="no-session-message")
                        yield Button("New Session", id="new-session-panel-button", variant="primary")
                    with Container(id="question-panel"):
                        yield Static("", id="question-title")
                        yield Static("", id="question-body")
                    yield RichLog(id="log", wrap=True, markup=True, highlight=True)
                    yield Static("", id="suggestions")
                    with Horizontal(id="composer"):
                        yield PromptInput(placeholder="Message, /help, /model, /clear...", id="prompt")
                        yield Button("Send", id="send", variant="primary")
            yield Footer()

        async def on_mount(self) -> None:
            profile = load_prompt_profile()
            log = self.query_one("#log", RichLog)
            log.write("[bold green]Ready.[/bold green] Type a message and press Enter.")
            log.write(
                "[dim]Prompt profile ready: "
                f"{len(profile.setup_variants)} setup variants, "
                f"{len(profile.boundary_templates)} boundary variants, "
                f"shared for Codex and Claude @ {DEFAULT_PROMPT_PROFILE_PATH.expanduser()}.[/dim]"
            )
            log.write("[dim]Create a session with the dialog, then chat or attach to the terminal.[/dim]")
            log.write("[dim]Press Ctrl+V to paste text or attach a clipboard image. Ctrl+O forces image-only paste.[/dim]")
            self._refresh_controls()
            self.set_interval(1.6, self._refresh_activity_status)
            self.set_interval(1.6, self._refresh_live_tmux_output)
            self._save_state()
            self._hide_suggestions()
            sessions = await self._refresh_sessions()
            if sessions:
                preferred = next(
                    (session for session in sessions if session.id == self.preferred_session_id),
                    sessions[0],
                )
                await self._restore_session(preferred.id, announce=True)
            else:
                self._announce_default_start()
            self.query_one("#prompt", Input).focus()

        async def on_input_submitted(self, event: Input.Submitted) -> None:
            await self._submit(event.value)

        def on_input_changed(self, event: Input.Changed) -> None:
            if event.value.strip().lower() not in {"close all", "confirm close all", "yes"}:
                self.close_all_confirmation_pending = False
            self._update_suggestions(event.value)

        def on_paste(self, event: events.Paste) -> None:
            self._handle_pasted_text(event.text)

        async def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "send":
                prompt = self.query_one("#prompt", Input)
                await self._submit(prompt.value)
            if event.button.id == "new-session":
                self._open_new_session_question()
            if event.button.id == "new-session-panel-button":
                self._open_new_session_question()
            if event.button.id == "refresh-sessions":
                await self._refresh_sessions()
            if event.button.id == "attach-session":
                await self._attach_selected_session()
            if event.button.id == "fullscreen-session":
                await self._fullscreen_selected_session()
            if event.button.id == "close-session":
                await self._close_selected_session()
            if event.button.id == "close-all":
                await self._close_sessions(runtime=None)

        async def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
            if self.updating_session_tabs:
                return
            session_id = str(event.tab.id or "").removeprefix("session-")
            if session_id in self.session_tab_ids and session_id != self.active_session_id:
                await self._restore_session(session_id, announce=True, refresh_tabs=False)

        async def on_key(self, event: events.Key) -> None:
            if not self.question_mode:
                return
            event.stop()
            key = event.key
            if self.question_browse_mode:
                if key == "up":
                    self.question_browse_index = max(0, self.question_browse_index - 1)
                    self._render_question()
                    return
                if key == "down":
                    self.question_browse_index = min(max(0, len(self.question_browse_entries) - 1), self.question_browse_index + 1)
                    self._render_question()
                    return
                if key == "left":
                    self.question_browse_path = self.question_browse_path.parent
                    self.question_browse_index = 0
                    self._render_question()
                    return
                if key in {"right", "enter"}:
                    await self._choose_browse_entry()
                    return
                if key == "escape":
                    self.question_browse_mode = False
                    self._render_question()
                    return
                return
            if key == "up":
                self.question_index = max(0, self.question_index - 1)
                self._render_question()
                return
            if key == "down":
                self.question_index = min(len(self._question_rows()) - 1, self.question_index + 1)
                self._render_question()
                return
            if key == "left":
                self._cycle_question_value(-1)
                self._render_question()
                return
            if key == "right":
                self._cycle_question_value(1)
                self._render_question()
                return
            if key == "enter":
                row = self._question_rows()[self.question_index]
                if row == "root":
                    self.question_browse_mode = True
                    self.question_browse_path = self._initial_browse_path()
                    self.question_browse_index = 0
                    self._render_question()
                    return
                await self._confirm_new_session_question()
                return
            if key == "escape":
                self._close_question_mode()
                return

        def action_clear(self) -> None:
            self.query_one("#log", RichLog).clear()

        def action_hide_suggestions(self) -> None:
            self._hide_suggestions()

        def action_smart_paste(self) -> None:
            attachment = save_clipboard_image()
            if attachment is not None:
                self._add_pending_images((attachment,))
                return
            text = read_clipboard_text()
            if not text:
                self.query_one("#log", RichLog).write("[bold yellow]yikes![/bold yellow] Clipboard is empty or unsupported.")
                return
            self._insert_prompt_text(text)
            self._handle_pasted_text(text)

        def action_paste_image(self) -> None:
            log = self.query_one("#log", RichLog)
            attachment = save_clipboard_image()
            if attachment is None:
                log.write("[bold yellow]yikes![/bold yellow] No image found in the OS clipboard. Paste or drag an image file path instead.")
                return
            self._add_pending_images((attachment,))

        def action_accept_suggestion(self) -> None:
            prompt = self.query_one("#prompt", Input)
            suggestions = self.conversation.slash_suggestions(prompt.value)
            completion = next((suggestion.completion for suggestion in suggestions if suggestion.completion), None)
            if completion is None:
                return
            prompt.value = completion
            if hasattr(prompt, "cursor_position"):
                prompt.cursor_position = len(completion)
            self._update_suggestions(prompt.value)

        def _update_suggestions(self, text: str) -> None:
            target = self.query_one("#suggestions", Static)
            suggestions = self.conversation.slash_suggestions(text)
            if not suggestions:
                self._hide_suggestions()
                return
            rows = []
            for suggestion in suggestions[:6]:
                detail = f" [dim]- {suggestion.description}[/dim]" if suggestion.description else ""
                rows.append(f"[bold]{suggestion.value}[/bold]{detail}")
            target.display = True
            target.update("\n".join(rows))

        def _hide_suggestions(self) -> None:
            target = self.query_one("#suggestions", Static)
            target.update("")
            target.display = False

        def _handle_pasted_text(self, text: str) -> None:
            remaining, attachments = extract_image_attachments(text, cwd=self.conversation.options.cwd)
            if not attachments:
                return
            self._add_pending_images(attachments)
            prompt = self.query_one("#prompt", Input)
            if not remaining:
                current = prompt.value
                prompt.value = current.replace(text, "").strip()

        def _insert_prompt_text(self, text: str) -> None:
            prompt = self.query_one("#prompt", Input)
            position = int(getattr(prompt, "cursor_position", len(prompt.value)))
            prompt.value = f"{prompt.value[:position]}{text}{prompt.value[position:]}"
            if hasattr(prompt, "cursor_position"):
                prompt.cursor_position = position + len(text)

        def _add_pending_images(self, attachments: tuple[ImageAttachment, ...]) -> None:
            if not self.has_active_session:
                self._show_no_session_message("Start a session before attaching images. /new also works from the prompt.")
                return
            for attachment in attachments:
                if attachment not in self.pending_images:
                    self.pending_images.append(attachment)
            log = self.query_one("#log", RichLog)
            names = attachable_image_names(tuple(attachments))
            log.write(f"[bold green]yikes![/bold green] Attached image: {names}")

        def _refresh_controls(self) -> None:
            options = self.conversation.options
            self.query_one("#backend-status", Static).update(f"Backend: {options.backend.value}")
            self.query_one("#location-status", Static).update(f"Location: {options.location.value}")
            self.query_one("#driver-status", Static).update(f"Driver: {options.mode.value}")
            self.query_one("#model-status", Static).update(f"Model: {options.model or 'default'}")
            self.query_one("#complexity-status", Static).update(f"Complexity: {options.complexity.value}")
            web_state = "on" if options.settings.web_search_enabled else "off"
            self.query_one("#web-status", Static).update(f"Web: {web_state}")
            capture_state = "on" if options.settings.managed_output_enabled else "off"
            self.query_one("#capture-status", Static).update(f"Capture: {capture_state}")
            self._refresh_activity_status()
            if self.has_active_session:
                self.active_session_id = options.session_id

        def _refresh_activity_status(self) -> None:
            target = self.query_one("#activity-status", Static)
            session_id = self._selected_session_id() if self.has_active_session else None
            if not session_id:
                target.update("Activity: unknown")
                return
            snapshot = SessionLifecycle().snapshot(session_id, lines=120)
            activity = self.activity_monitor.observe(session_id, snapshot)
            target.update(f"Activity: {activity.label}")

        async def _refresh_sessions(self) -> list[object]:
            sessions = SessionInventory().list()
            visible_sessions = [session for session in sessions if session.state not in {"dead", "stopped"}]
            dead_count = len(sessions) - len(visible_sessions)
            self.session_tab_ids = {session.id for session in visible_sessions}
            suffix = f" ({dead_count} dead hidden)" if dead_count else ""
            self.query_one("#session-count", Static).update(f"Sessions: {len(visible_sessions)}{suffix}")
            tabs = self.query_one("#session-tabs", Tabs)
            self.updating_session_tabs = True
            try:
                await tabs.clear()
                active = self.active_session_id or self.conversation.options.session_id
                if visible_sessions:
                    live = {session.id for session in visible_sessions}
                    selected = active if active in live else visible_sessions[0].id
                    self.active_session_id = selected
                    for session in visible_sessions:
                        label = f"{session.backend}/{session.runtime}/{session.id[-6:]}"
                        await tabs.add_tab(Tab(label, id=f"session-{session.id}"))
                    tabs.active = f"session-{selected}"
                elif self.has_active_session:
                    self.active_session_id = self.conversation.options.session_id
                    await tabs.add_tab(Tab("new session", id=f"session-{self.conversation.options.session_id}"))
                    tabs.active = f"session-{self.conversation.options.session_id}"
                else:
                    self.active_session_id = None
            finally:
                self.updating_session_tabs = False
            return visible_sessions

        def _refresh_sessions_soon(self) -> None:
            self.run_worker(self._refresh_sessions(), exclusive=False)

        def _selected_session_id(self) -> str:
            tabs = self.query_one("#session-tabs", Tabs)
            active = str(tabs.active or "")
            if active.startswith("session-"):
                session_id = active.removeprefix("session-")
                return session_id if session_id in self.session_tab_ids else ""
            return self.active_session_id if self.active_session_id in self.session_tab_ids else ""

        async def _restore_session(self, session_id: str, *, announce: bool, refresh_tabs: bool = True) -> None:
            log = self.query_one("#log", RichLog)
            summary = SessionLifecycle().summary(session_id)
            if summary is not None and summary.state in {"dead", "stopped"}:
                self.has_active_session = False
                self.active_session_id = None
                self._set_session_view(active=False)
                self._show_no_session_message(
                    f"Session {session_id} is {summary.state}. Close it and create or select another session."
                )
                await self._refresh_sessions()
                return
            options = SessionLifecycle().switch_options(self.conversation.options, session_id)
            if options is None:
                log.write(f"[bold red]yikes![/bold red] Session not found: {session_id}")
                await self._refresh_sessions()
                return
            self.conversation.set_options(options)
            self.has_active_session = True
            self.active_session_id = session_id
            self._refresh_controls()
            self._set_session_view(active=True)
            self._save_state()
            if refresh_tabs:
                await self._refresh_sessions()
            if announce:
                log.write(
                    f"[bold green]yikes![/bold green] Restored {options.driver.value}/{options.backend.value} session {session_id}."
                )
            snapshot = SessionLifecycle().snapshot(session_id)
            if snapshot:
                log.write("[dim]Last terminal output:[/dim]")
                log.write(snapshot)

        async def _new_session(self, *, cwd: Path | None = None, cwd_explicit: bool = True) -> None:
            self.conversation.start_new(cwd=cwd, cwd_explicit=cwd_explicit)
            startup_error: str | None = None
            if self.conversation.options.mode is DriverMode.TMUX:
                self.output_view = "dev"
                try:
                    ensure_interactive_session(self.conversation.options)
                except Exception as exc:
                    startup_error = f"Failed to start interactive tmux session: {exc}"
            record_direct_session(
                self.conversation.options,
                user_data={
                    "location": self.conversation.options.location.value,
                    "mode": self.conversation.options.mode.value,
                },
            )
            self.has_active_session = True
            self.active_session_id = self.conversation.options.session_id
            self.pending_images.clear()
            self.query_one("#log", RichLog).clear()
            self._set_session_view(active=True)
            self._refresh_controls()
            await self._refresh_sessions()
            self._save_state()
            self.query_one("#log", RichLog).write(
                (
                    "[bold green]yikes![/bold green] Started new session: "
                    f"{self.conversation.options.backend.value} on {self.conversation.options.location.value} "
                    f"via {self.conversation.options.mode.value}; "
                    f"model {self.conversation.options.model or 'default'}; "
                    f"complexity {self.conversation.options.complexity.value}; "
                    f"web {'on' if self.conversation.options.settings.web_search_enabled else 'off'}."
                )
            )
            snapshot = SessionLifecycle().snapshot(self.active_session_id) if self.active_session_id else None
            if snapshot:
                self.query_one("#log", RichLog).write(snapshot)
            if startup_error:
                self.query_one("#log", RichLog).write(f"[bold red]Error:[/bold red] {startup_error}")

        def _announce_default_start(self) -> None:
            options = self.conversation.options
            self.has_active_session = False
            self.active_session_id = None
            self._set_session_view(active=False)
            self._show_no_session_message(
                "No active session. Start one with the New Session button. "
                f"Defaults: {options.backend.value}/{options.location.value}/{options.mode.value}, "
                f"model {options.model or 'default'}."
            )

        def _set_session_view(self, *, active: bool) -> None:
            self.query_one("#log", RichLog).display = active
            self.query_one("#no-session-panel", Container).display = not active
            self.query_one("#question-panel", Container).display = False
            self.query_one("#composer", Horizontal).display = True
            prompt = self.query_one("#prompt", Input)
            prompt.placeholder = (
                "Message, /help, /model, /clear..."
                if active
                else "Create a session to start chatting"
            )

        def _show_no_session_message(self, message: str) -> None:
            self.query_one("#no-session-message", Static).update(message)

        def _write_status(self, message: str, *, style: str = "bold yellow") -> None:
            if self.has_active_session:
                self.query_one("#log", RichLog).write(f"[{style}]yikes![/{style}] {message}")
            else:
                self._show_no_session_message(message)

        def _sync_log_from_tmux(self, session_id: str | None = None) -> bool:
            selected = session_id or self._selected_session_id()
            if not selected:
                return False
            snapshot = SessionLifecycle().snapshot(selected)
            if not snapshot:
                return False
            if self.output_view != "dev":
                snapshot = high_level_transcript(snapshot, markers=SessionLifecycle().capture_markers(selected))
            log = self.query_one("#log", RichLog)
            log.clear()
            log.write(snapshot)
            self.live_snapshot_text = snapshot
            return True

        def _refresh_live_tmux_output(self) -> None:
            if self.question_mode or not self.has_active_session:
                return
            if self.submission_active and self.output_view != "dev":
                return
            selected = self._selected_session_id()
            if not selected:
                return
            snapshot = SessionLifecycle().snapshot(selected)
            if not snapshot or snapshot == self.live_snapshot_text:
                return
            self.live_snapshot_text = snapshot
            markers = SessionLifecycle().capture_markers(selected)
            rendered = snapshot if self.output_view == "dev" else high_level_transcript(snapshot, markers=markers)
            log = self.query_one("#log", RichLog)
            log.clear()
            log.write(rendered)

        def _open_new_session_question(self) -> None:
            options = self.conversation.options
            self.question_mode = True
            self.question_browse_mode = False
            self.question_index = 0
            self.question_choices = {
                "backend": options.backend.value,
                "location": options.location.value if options.location is not ExecutionLocation.REMOTE else ExecutionLocation.HOST.value,
                "driver": options.mode.value if options.mode is not DriverMode.API else DriverMode.CLI.value,
                "model": options.model or "default",
                "complexity": options.complexity.value,
                "web": "on" if options.settings.web_search_enabled else "off",
                "capture": "off" if options.mode is DriverMode.TMUX else ("on" if options.settings.managed_output_enabled else "off"),
                "root": "none",
            }
            self.query_one("#log", RichLog).display = False
            self.query_one("#no-session-panel", Container).display = False
            self.query_one("#question-panel", Container).display = True
            self.query_one("#composer", Horizontal).display = False
            self._render_question()

        def _close_question_mode(self) -> None:
            self.question_mode = False
            self.question_browse_mode = False
            self.query_one("#question-panel", Container).display = False
            self.query_one("#composer", Horizontal).display = True
            if self.has_active_session:
                self.query_one("#log", RichLog).display = True
            else:
                self.query_one("#no-session-panel", Container).display = True
            self.query_one("#prompt", Input).focus()

        def _question_rows(self) -> list[str]:
            return ["backend", "location", "driver", "model", "complexity", "web", "capture", "root"]

        def _question_options(self, row: str) -> list[str]:
            if row == "backend":
                return [backend.value for backend in Backend]
            if row == "location":
                return [ExecutionLocation.HOST.value, ExecutionLocation.DOCKER.value]
            if row == "driver":
                return [DriverMode.CLI.value, DriverMode.TMUX.value]
            if row == "model":
                backend = Backend(self.question_choices.get("backend", Backend.CLAUDE.value))
                names = [option.name for option in self.conversation.model_registry.options(backend)]
                return names or ["default"]
            if row == "complexity":
                return [level.value for level in Complexity]
            if row == "web":
                return ["on", "off"]
            if row == "capture":
                return ["on", "off"]
            if row == "root":
                return ["none", "start dir", "home", "browse"]
            return []

        def _cycle_question_value(self, direction: int) -> None:
            row = self._question_rows()[self.question_index]
            options = self._question_options(row)
            current = self.question_choices.get(row, options[0])
            if current not in options:
                current = options[0]
            index = options.index(current)
            self.question_choices[row] = options[(index + direction) % len(options)]
            if row == "backend":
                model_options = self._question_options("model")
                if self.question_choices.get("model") not in model_options:
                    self.question_choices["model"] = "default"
            if row == "driver" and self.question_choices.get("driver") == DriverMode.TMUX.value:
                self.question_choices["capture"] = "off"

        def _render_question(self) -> None:
            title = self.query_one("#question-title", Static)
            body = self.query_one("#question-body", Static)
            if self.question_browse_mode:
                self.question_browse_entries = self._directory_entries(self.question_browse_path)
                title.update("New session: choose root directory")
                lines = [
                    f"[dim]{self.question_browse_path}[/dim]",
                    "[dim]Up/Down move, Right/Enter open/select, Left parent, Escape back[/dim]",
                    "",
                ]
                for index, entry in enumerate(self.question_browse_entries[:18]):
                    marker = ">" if index == self.question_browse_index else " "
                    label = ".." if entry == self.question_browse_path.parent else entry.name
                    suffix = "/" if entry.is_dir() else ""
                    lines.append(f"[bold]{marker}[/bold] {label}{suffix}")
                body.update("\n".join(lines))
                return
            title.update("New session")
            lines = ["[dim]Up/Down choose field, Left/Right change, Enter starts, Escape cancels[/dim]", ""]
            labels = {
                "backend": "Backend",
                "location": "Where",
                "driver": "How",
                "model": "Model",
                "complexity": "Complexity",
                "web": "Web",
                "capture": "Capture",
                "root": "Root dir",
            }
            for index, row in enumerate(self._question_rows()):
                marker = ">" if index == self.question_index else " "
                value = self.question_choices.get(row, "")
                if row == "root" and value == "browse":
                    value = "browse..."
                lines.append(f"[bold]{marker} {labels[row]:<11}[/bold] {value}")
            lines.append("")
            lines.append("[dim]Root dir defaults to none, so no project directory is mounted or trusted unless selected.[/dim]")
            body.update("\n".join(lines))

        def _directory_entries(self, path: Path) -> list[Path]:
            try:
                resolved = path.expanduser().resolve()
                children = sorted(
                    [item for item in resolved.iterdir() if item.is_dir() and not item.name.startswith(".")],
                    key=lambda item: item.name.lower(),
                )
            except OSError:
                resolved = Path.home()
                children = []
            return [resolved.parent, *children]

        def _initial_browse_path(self) -> Path:
            root = self.question_choices.get("root", "none")
            if root == "home":
                return Path.home()
            if root == "start dir":
                return Path.cwd()
            if root.startswith("/"):
                return Path(root)
            return Path.cwd()

        async def _choose_browse_entry(self) -> None:
            if not self.question_browse_entries:
                return
            selected = self.question_browse_entries[self.question_browse_index]
            if selected == self.question_browse_path.parent:
                self.question_browse_path = selected
                self.question_browse_index = 0
                self._render_question()
                return
            self.question_choices["root"] = str(selected)
            self.question_browse_mode = False
            self._render_question()

        async def _confirm_new_session_question(self) -> None:
            root = self.question_choices.get("root", "none")
            cwd_choice: Path | None
            cwd_explicit = True
            if root == "none":
                cwd_choice = Path(tempfile.mkdtemp(prefix="yikes-session-"))
                cwd_explicit = False
            elif root == "start dir":
                cwd_choice = Path.cwd()
            elif root == "home":
                cwd_choice = Path.home()
            elif root == "browse":
                self.question_browse_mode = True
                self.question_browse_path = self._initial_browse_path()
                self._render_question()
                return
            else:
                cwd_choice = Path(root)
            try:
                self.conversation.set_backend(Backend(self.question_choices["backend"]))
                self.conversation.set_execution_location(ExecutionLocation(self.question_choices["location"]))
                self.conversation.set_driver_mode(DriverMode(self.question_choices["driver"]))
                model = self.question_choices.get("model", "default")
                self.conversation.set_model(None if model == "default" else model)
                self.conversation.set_complexity(Complexity(self.question_choices["complexity"]))
                self.conversation.set_web_search(self.question_choices.get("web") == "on")
                self.conversation.set_settings(
                    self.conversation.options.settings.with_managed_output(
                        self.question_choices.get("capture") == "on"
                    )
                )
            except ValueError as exc:
                self.query_one("#question-body", Static).update(f"[bold red]{exc}[/bold red]")
                return
            self._close_question_mode()
            if self.conversation.options.mode is DriverMode.TMUX:
                self.output_view = "dev"
            await self._new_session(cwd=cwd_choice, cwd_explicit=cwd_explicit)

        async def _close_selected_session(self) -> None:
            log = self.query_one("#log", RichLog)
            session_id = self._selected_session_id()
            if not session_id:
                log.write("[bold yellow]yikes![/bold yellow] No session selected.")
                return
            result = SessionLifecycle().close(session_id)
            style = "bold green" if result.closed else "bold red"
            log.write(f"[{style}]yikes![/{style}] {result.message}")
            sessions = await self._refresh_sessions()
            if result.closed and sessions:
                await self._restore_session(sessions[0].id, announce=True)
            if result.closed and not sessions:
                self.conversation.start_new()
                self.has_active_session = False
                self.active_session_id = None
                self.pending_images.clear()
                log.clear()
                self._refresh_controls()
                self._set_session_view(active=False)
                self._show_no_session_message("Closed the last session. Start a new session to send prompts.")
                await self._refresh_sessions()

        async def _attach_selected_session(self) -> None:
            log = self.query_one("#log", RichLog)
            session_id = self._selected_session_id()
            if not session_id:
                log.write("[bold yellow]yikes![/bold yellow] No session selected.")
                return
            command = SessionLifecycle().attach_command(session_id)
            if command is None:
                log.write(f"[bold red]yikes![/bold red] Session not found or not attachable: {session_id}")
                await self._refresh_sessions()
                return
            self.attach_session_id = session_id
            self.attach_command = command
            self.exit()

        async def _fullscreen_selected_session(self) -> None:
            log = self.query_one("#log", RichLog)
            session_id = self._selected_session_id()
            if not session_id:
                self._write_status("No tmux session selected.")
                return
            command = SessionLifecycle().attach_command(session_id)
            if command is None:
                self._write_status(f"Session is not attachable: {session_id}")
                await self._refresh_sessions()
                return
            log.write("[bold green]yikes![/bold green] Entering fullscreen tmux. Press Ctrl-b to return to yikes!.")
            self.attach_session_id = session_id
            self.attach_command = command
            self.exit()

        async def _close_sessions(self, *, runtime: str | None) -> None:
            log = self.query_one("#log", RichLog)
            if runtime is None and not self.close_all_confirmation_pending:
                self.close_all_confirmation_pending = True
                message = (
                    "Close all sessions? Type close all and press Enter, or press Close All again to confirm."
                )
                if self.has_active_session:
                    log.write(f"[bold yellow]yikes![/bold yellow] {message}")
                else:
                    self._show_no_session_message(message)
                return
            self.close_all_confirmation_pending = False
            results = SessionLifecycle().close_all(runtime=runtime)
            if not results:
                target = runtime or "known"
                message = f"No matching {target} sessions."
                if self.has_active_session:
                    log.write(f"[bold yellow]yikes![/bold yellow] {message}")
                else:
                    self._show_no_session_message(message)
                await self._refresh_sessions()
                return
            closed = sum(1 for result in results if result.closed)
            if runtime is None:
                self.conversation.start_new()
                self.has_active_session = False
                self.active_session_id = None
                self.pending_images.clear()
                log.clear()
                self._refresh_controls()
                self._set_session_view(active=False)
                self._show_no_session_message(f"Closed {closed}/{len(results)} sessions. Start a new session to send prompts.")
            else:
                log.write(f"[bold green]yikes![/bold green] Closed {closed}/{len(results)} sessions.")
            await self._refresh_sessions()

        def _save_state(self) -> None:
            save_app_state(AppState.from_options(self.conversation.options))

        async def _submit(self, text: str) -> None:
            text = text.strip()
            if not text:
                self.query_one("#prompt", Input).value = ""
                return
            prompt = self.query_one("#prompt", Input)
            prompt.value = ""
            self._hide_suggestions()
            log = self.query_one("#log", RichLog)
            if text.startswith("/"):
                await self._slash_command(text)
                return
            if self.close_all_confirmation_pending:
                normalized = text.lower()
                if normalized in {"close all", "confirm close all", "yes"}:
                    await self._close_sessions(runtime=None)
                    return
                self.close_all_confirmation_pending = False
            if not self.has_active_session:
                self._show_no_session_message("No active session. Create one with the New Session button or /new.")
                return
            selected = self._selected_session_id()
            if selected:
                summary = SessionLifecycle().summary(selected)
                if summary is not None and summary.state in {"dead", "stopped"}:
                    self.has_active_session = False
                    self.active_session_id = None
                    self._set_session_view(active=False)
                    self._show_no_session_message(
                        f"Session {selected} is {summary.state}. Close it and create or select another session."
                    )
                    await self._refresh_sessions()
                    return
            text, extracted = extract_image_attachments(text, cwd=self.conversation.options.cwd)
            if extracted:
                self._add_pending_images(extracted)
            if not text:
                return
            attachments = tuple(self.pending_images)
            self.pending_images.clear()
            attachment_note = f" [dim]({len(attachments)} image{'s' if len(attachments) != 1 else ''})[/dim]" if attachments else ""
            if self.output_view == "dev":
                self._sync_log_from_tmux()
            log.write(f"[bold cyan]You:[/bold cyan] {text}{attachment_note}")
            log.write("[yellow]Working...[/yellow]")
            self.submission_active = True
            self.ask_backend(text, attachments)

        async def _slash_command(self, text: str) -> None:
            log = self.query_one("#log", RichLog)
            command_name = text.strip().split(maxsplit=1)[0].lower()
            if command_name == "/new":
                self._open_new_session_question()
                return
            if await self._handle_tui_command(text):
                return
            if self.has_active_session:
                log.write(f"[bold blue]Command:[/bold blue] {text}")
            result = self.conversation.run_slash_command(text)
            if result.exit_requested:
                self.exit()
                return
            if result.message:
                if self.has_active_session:
                    log.write(f"[bold green]yikes![/bold green] {result.message}")
                else:
                    self._show_no_session_message(result.message)
            if result.restart_requested:
                self._save_state()
                self.restart_requested = True
                self.exit()
                return
            self._refresh_controls()
            await self._refresh_sessions()
            self._save_state()

        async def _handle_tui_command(self, text: str) -> bool:
            parts = text.strip().split(maxsplit=1)
            raw_command = parts[0].lower() if parts else ""
            resolved = self.conversation.resolve_slash_command(raw_command)
            command = f"/{resolved}" if resolved else raw_command
            arg = parts[1] if len(parts) > 1 else ""
            if command == "/web" and arg.strip().lower() in {"", "open", "ui", "app"}:
                from .web_launcher import launch_web_ui

                result = launch_web_ui(cwd=self.start_cwd, developer_mode=True)
                self._write_status(result.message, style="bold green")
                return True
            if command in {"/fullscreen", "/overtake", "/term", "/terminal"}:
                await self._fullscreen_selected_session()
                return True
            if command == "/view":
                value = arg.strip().lower()
                normalized = self._normalize_output_view(value)
                if normalized is None:
                    self._write_status("Usage: /view [high|dev]")
                    return True
                self.output_view = normalized
                self.live_snapshot_text = ""
                self._refresh_live_tmux_output()
                self._write_status(f"Output view set to {normalized}.", style="bold green")
                return True
            if command == "/key":
                key = arg.strip()
                if not key:
                    self._write_status("Usage: /key <Enter|Up|Down|Left|Right|Escape|C-c|...>")
                    return True
                session_id = self._selected_session_id()
                if not session_id:
                    self._write_status("No tmux session selected.")
                    return True
                result = SessionLifecycle().send_key(session_id, key)
                style = "bold green" if result.closed else "bold red"
                self._write_status(result.message, style=style)
                return True
            if command == "/paste":
                if not arg:
                    self._write_status("Usage: /paste <text>")
                    return True
                session_id = self._selected_session_id()
                if not session_id:
                    self._write_status("No tmux session selected.")
                    return True
                result = SessionLifecycle().paste_text(session_id, arg)
                style = "bold green" if result.closed else "bold red"
                self._write_status(result.message, style=style)
                return True
            return False

        @work(thread=True)
        def ask_backend(self, text: str, attachments: tuple[ImageAttachment, ...] = ()) -> None:
            log = self.query_one("#log", RichLog)
            try:
                answer = self.conversation.ask(text, attachments)
            except Exception as exc:
                self.call_from_thread(self._mark_submission_done)
                self.call_from_thread(log.write, f"[bold red]Error:[/bold red] {exc}")
                self.call_from_thread(self._refresh_sessions_soon)
                return
            self.call_from_thread(self._mark_submission_done)
            self.call_from_thread(self._refresh_sessions_soon)
            if self.output_view == "dev":
                full_output = self._full_output(answer)
                self.call_from_thread(log.write, f"[bold magenta]Full output:[/bold magenta]\n{full_output}")
                return
            if self.conversation.options.mode is DriverMode.TMUX:
                session_id = self._snapshot_session_id()
                snapshot = SessionLifecycle().snapshot(session_id) if session_id else None
                if snapshot:
                    if self.output_view != "dev":
                        snapshot = high_level_transcript(snapshot, markers=SessionLifecycle().capture_markers(session_id)) if session_id else snapshot
                    self.call_from_thread(log.clear)
                    self.call_from_thread(log.write, snapshot)
                    return
            if answer:
                self.call_from_thread(log.write, f"[bold magenta]Assistant:[/bold magenta] {answer}")

        def _mark_submission_done(self) -> None:
            self.submission_active = False

        def _full_output(self, answer: str) -> str:
            session_id = self._snapshot_session_id()
            if session_id:
                snapshot = SessionLifecycle().snapshot(session_id)
                if snapshot:
                    return snapshot
            return f"{self.conversation.render_prompt()}\n\nAssistant: {answer}"

        def _snapshot_session_id(self) -> str | None:
            if self.active_session_id and self.active_session_id in self.session_tab_ids:
                return self.active_session_id
            sessions = SessionInventory().list()
            options = self.conversation.options
            for session in sessions:
                if session.backend != options.backend.value:
                    continue
                if session.runtime == "tmux" and session.location == str(options.cwd):
                    return session.id
                if session.runtime == "docker" and options.driver.value == "docker":
                    return session.id
            return None

        @staticmethod
        def _normalize_output_view(value: str) -> str | None:
            if value in {"high", "normal", "transcript", "extracted"}:
                return "high"
            if value in {"dev", "debug", "raw", "full"}:
                return "dev"
            return None

    app = TerminalApp()
    app.run()
    if app.attach_command:
        if app.attach_session_id:
            size = shutil.get_terminal_size(fallback=(120, 34))
            SessionLifecycle().resize(app.attach_session_id, cols=size.columns, rows=size.lines)
        _attach_until_ctrl_b(
            app.attach_command,
            on_resize=(
                lambda cols, rows: SessionLifecycle().resize(app.attach_session_id, cols=cols, rows=rows)
                if app.attach_session_id
                else None
            ),
        )
        if app.attach_session_id:
            os.environ["YIKES_RETURN_SESSION_ID"] = app.attach_session_id
        os.execv(sys.executable, [sys.executable, *sys.argv])
    if app.restart_requested:
        os.execv(sys.executable, [sys.executable, *sys.argv])


def _attach_until_ctrl_b(command: list[str], *, on_resize: Callable[[int, int], object] | None = None) -> None:
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    master_fd, slave_fd = pty.openpty()
    cols, rows = _current_terminal_size(stdout_fd)
    _set_fd_size(slave_fd, cols=cols, rows=rows)
    if on_resize is not None:
        on_resize(cols, rows)

    process = subprocess.Popen(
        command,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)

    old_attrs = termios.tcgetattr(stdin_fd) if os.isatty(stdin_fd) else None
    previous_winch = signal.getsignal(signal.SIGWINCH)

    def resize_child(_signum: int | None = None, _frame: object | None = None) -> None:
        new_cols, new_rows = _current_terminal_size(stdout_fd)
        _set_fd_size(master_fd, cols=new_cols, rows=new_rows)
        if on_resize is not None:
            on_resize(new_cols, new_rows)
        try:
            process.send_signal(signal.SIGWINCH)
        except OSError:
            pass

    try:
        if old_attrs is not None:
            tty.setraw(stdin_fd)
        signal.signal(signal.SIGWINCH, resize_child)
        resize_child()
        while process.poll() is None:
            readable, _, _ = select.select([stdin_fd, master_fd], [], [], 0.1)
            if master_fd in readable:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                os.write(stdout_fd, data)
            if stdin_fd in readable:
                try:
                    data = os.read(stdin_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                os.write(master_fd, data.replace(b"\x02", b"\x02d"))
    finally:
        if old_attrs is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        signal.signal(signal.SIGWINCH, previous_winch)
        try:
            os.close(master_fd)
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
        else:
            process.wait()


def _current_terminal_size(fd: int) -> tuple[int, int]:
    try:
        size = os.get_terminal_size(fd)
        return max(20, size.columns), max(5, size.lines)
    except OSError:
        size = shutil.get_terminal_size(fallback=(120, 34))
        return max(20, size.columns), max(5, size.lines)


def _set_fd_size(fd: int, *, cols: int, rows: int) -> None:
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass
