from __future__ import annotations

import os
from pathlib import Path
import sys

from .attachments import attachable_image_names, extract_image_attachments, read_clipboard_text, save_clipboard_image
from .capabilities import default_driver_registry
from .domain import AgentSettings, Backend, Complexity, Driver, ImageAttachment
from .services import ChatService, Conversation
from .session_inventory import SessionInventory, SessionLifecycle
from .state import AppState, load_app_state, save_app_state


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
            self.restart_requested = False
            self.attach_command: list[str] | None = None
            self.pending_images: list[ImageAttachment] = []
            self.saved_state = load_app_state()
            resolved_backend = backend or self.saved_state.backend
            driver_registry = default_driver_registry()
            resolved_driver = driver_registry.coerce(resolved_backend, driver or self.saved_state.driver)
            resolved_model = model if model is not None else self.saved_state.model
            resolved_complexity = complexity or self.saved_state.complexity
            resolved_settings = settings or self.saved_state.settings
            self.service = ChatService()
            self.active_session_id: str | None = None
            self.session_tab_ids: set[str] = set()
            self.updating_session_tabs = False
            self.close_all_confirmation_pending = False
            self.has_active_session = False
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
                    yield Static(f"CWD: {self.conversation.options.cwd}")
                    yield Static("", id="session-count")
                    yield Button("New Session", id="new-session", classes="sidebar-button")
                    yield Button("Refresh Sessions", id="refresh-sessions", classes="sidebar-button")
                    with Horizontal(id="session-actions"):
                        yield Button("Attach", id="attach-session")
                        yield Button("Close", id="close-session")
                    yield Button("Close All", id="close-all", classes="sidebar-button")
                with Vertical(id="chat"):
                    yield Tabs(id="session-tabs")
                    with Container(id="no-session-panel"):
                        yield Static("", id="no-session-message")
                        yield Button("New Session", id="new-session-panel-button", variant="primary")
                    yield RichLog(id="log", wrap=True, markup=True, highlight=True)
                    yield Static("", id="suggestions")
                    with Horizontal(id="composer"):
                        yield PromptInput(placeholder="Message, /help, /model, /clear...", id="prompt")
                        yield Button("Send", id="send", variant="primary")
            yield Footer()

        async def on_mount(self) -> None:
            log = self.query_one("#log", RichLog)
            log.write("[bold green]Ready.[/bold green] Type a message and press Enter.")
            log.write(
                "[dim]Use /backend, /location, /driver, /model, /web, and /new for configuration. "
                "/models shows valid model options.[/dim]"
            )
            log.write("[dim]Press Ctrl+V to paste text or attach a clipboard image. Ctrl+O forces image-only paste.[/dim]")
            self._refresh_controls()
            self._save_state()
            self._hide_suggestions()
            sessions = await self._refresh_sessions()
            if sessions:
                await self._restore_session(sessions[0].id, announce=True)
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
                await self._new_session()
            if event.button.id == "new-session-panel-button":
                await self._new_session()
            if event.button.id == "refresh-sessions":
                await self._refresh_sessions()
            if event.button.id == "attach-session":
                await self._attach_selected_session()
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
            if self.has_active_session:
                self.active_session_id = options.session_id

        async def _refresh_sessions(self) -> list[object]:
            sessions = SessionInventory().list()
            self.session_tab_ids = {session.id for session in sessions}
            self.query_one("#session-count", Static).update(f"Sessions: {len(sessions)}")
            tabs = self.query_one("#session-tabs", Tabs)
            self.updating_session_tabs = True
            try:
                await tabs.clear()
                active = self.active_session_id or self.conversation.options.session_id
                if sessions:
                    valid = {session.id for session in sessions}
                    selected = active if active in valid else sessions[0].id
                    self.active_session_id = selected
                    for session in sessions:
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
            return sessions

        def _selected_session_id(self) -> str:
            tabs = self.query_one("#session-tabs", Tabs)
            active = str(tabs.active or "")
            if active.startswith("session-"):
                session_id = active.removeprefix("session-")
                return session_id if session_id in self.session_tab_ids else ""
            return self.active_session_id if self.active_session_id in self.session_tab_ids else ""

        async def _restore_session(self, session_id: str, *, announce: bool, refresh_tabs: bool = True) -> None:
            log = self.query_one("#log", RichLog)
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

        async def _new_session(self, *, cwd: Path | None = None) -> None:
            self.conversation.start_new(cwd=cwd)
            self.has_active_session = True
            self.active_session_id = self.conversation.options.session_id
            self.pending_images.clear()
            self.query_one("#log", RichLog).clear()
            self._set_session_view(active=True)
            self._refresh_controls()
            await self._refresh_sessions()
            self._save_state()
            self.query_one("#log", RichLog).write(
                "[bold green]yikes![/bold green] Started new session. Use /backend, /location, /driver, /model, or /web to change defaults."
            )

        def _announce_default_start(self) -> None:
            options = self.conversation.options
            self.has_active_session = False
            self.active_session_id = None
            self._set_session_view(active=False)
            self._show_no_session_message(
                "No active session. Start one with the button or /new. "
                f"Defaults: {options.backend.value}/{options.location.value}/{options.mode.value}, "
                f"model {options.model or 'default'}, cwd {options.cwd}."
            )

        def _set_session_view(self, *, active: bool) -> None:
            self.query_one("#log", RichLog).display = active
            self.query_one("#no-session-panel", Container).display = not active
            prompt = self.query_one("#prompt", Input)
            prompt.placeholder = (
                "Message, /help, /model, /clear..."
                if active
                else "/new, /backend, /location, /driver, /model..."
            )

        def _show_no_session_message(self, message: str) -> None:
            self.query_one("#no-session-message", Static).update(message)

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
                self._show_no_session_message("No active session. Use /new or the New Session button before sending a prompt.")
                return
            text, extracted = extract_image_attachments(text, cwd=self.conversation.options.cwd)
            if extracted:
                self._add_pending_images(extracted)
            if not text:
                return
            attachments = tuple(self.pending_images)
            self.pending_images.clear()
            attachment_note = f" [dim]({len(attachments)} image{'s' if len(attachments) != 1 else ''})[/dim]" if attachments else ""
            log.write(f"[bold cyan]You:[/bold cyan] {text}{attachment_note}")
            self.ask_backend(text, attachments)

        async def _slash_command(self, text: str) -> None:
            log = self.query_one("#log", RichLog)
            command_name = text.strip().split(maxsplit=1)[0].lower()
            if command_name == "/new":
                log.clear()
                self.has_active_session = True
                self._set_session_view(active=True)
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

        @work(thread=True)
        def ask_backend(self, text: str, attachments: tuple[ImageAttachment, ...] = ()) -> None:
            log = self.query_one("#log", RichLog)
            self.call_from_thread(log.write, "[yellow]Working...[/yellow]")
            try:
                answer = self.conversation.ask(text, attachments)
            except Exception as exc:
                self.call_from_thread(log.write, f"[bold red]Error:[/bold red] {exc}")
                return
            self.call_from_thread(log.write, f"[bold magenta]Assistant:[/bold magenta] {answer}")

    app = TerminalApp()
    app.run()
    if app.attach_command:
        os.execvp(app.attach_command[0], app.attach_command)
    if app.restart_requested:
        os.execv(sys.executable, [sys.executable, *sys.argv])
