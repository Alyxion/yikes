from __future__ import annotations

import os
from pathlib import Path
import sys

from .capabilities import default_driver_registry
from .domain import AgentSettings, Backend, Complexity, Driver
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
        from textual import work
        from textual.app import App, ComposeResult
        from textual.containers import Container, Horizontal, Vertical
        from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Select, Static
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional runtime install
        raise RuntimeError("Textual is required for the full terminal app. Run: poetry install") from exc

    class YikesApp(App[None]):
        CSS = """
        Screen {
            background: $surface;
        }

        #layout {
            height: 1fr;
            width: 100%;
        }

        #sidebar {
            width: 32;
            border-right: solid $primary;
            padding: 1 1;
        }

        #chat {
            width: 1fr;
            padding: 1 2;
        }

        #log {
            height: 1fr;
            border: solid $panel;
            padding: 1;
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
        """

        BINDINGS = [
            ("ctrl+q", "quit", "Quit"),
            ("ctrl+l", "clear", "Clear"),
            ("tab", "accept_suggestion", "Complete"),
            ("escape", "hide_suggestions", "Hide"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.restart_requested = False
            self.attach_command: list[str] | None = None
            self.saved_state = load_app_state()
            resolved_backend = backend or self.saved_state.backend
            driver_registry = default_driver_registry()
            resolved_driver = driver_registry.coerce(resolved_backend, driver or self.saved_state.driver)
            resolved_model = model if model is not None else self.saved_state.model
            resolved_complexity = complexity or self.saved_state.complexity
            resolved_settings = settings or self.saved_state.settings
            self.service = ChatService()
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
                    yield Label("Yikes")
                    yield Static("", id="backend-status")
                    yield Static("", id="driver-status")
                    yield Static("", id="model-status")
                    yield Static("", id="complexity-status")
                    yield Static("", id="web-status")
                    yield Static("", id="tmux-status")
                    yield Static(f"CWD: {self.conversation.options.cwd}")
                    yield Select(
                        [(b.value, b.value) for b in Backend],
                        value=self.conversation.options.backend.value,
                        id="backend",
                    )
                    yield Select(
                        self._driver_select_options(),
                        value=self.conversation.options.driver.value,
                        id="driver",
                    )
                    yield Select(
                        self._model_select_options(),
                        value=self.conversation.options.model or "default",
                        id="model",
                    )
                    yield Select(
                        [(level.value, level.value) for level in Complexity],
                        value=self.conversation.options.complexity.value,
                        id="complexity",
                    )
                    yield Select(
                        [("web on", "on"), ("web off", "off")],
                        value="on" if self.conversation.options.settings.web_search_enabled else "off",
                        id="web",
                    )
                    yield Select(
                        [("tmux on", "on"), ("tmux off", "off")],
                        value="on" if self.conversation.options.settings.tmux_enabled else "off",
                        id="tmux",
                    )
                    yield Button("Refresh Sessions", id="refresh-sessions")
                    yield Select([("no sessions", "")], value="", id="session-select")
                    with Horizontal(id="session-actions"):
                        yield Button("Switch", id="switch-session")
                        yield Button("Attach", id="attach-session")
                        yield Button("Close", id="close-session")
                    with Horizontal(id="session-bulk-actions"):
                        yield Button("Close Docker", id="close-docker")
                        yield Button("Close tmux", id="close-tmux")
                    yield Button("Close All", id="close-all")
                    yield Static("", id="sessions")
                with Vertical(id="chat"):
                    yield RichLog(id="log", wrap=True, markup=True, highlight=True)
                    yield Static("", id="suggestions")
                    with Horizontal(id="composer"):
                        yield Input(placeholder="Message, /help, /model, /clear...", id="prompt")
                        yield Button("Send", id="send", variant="primary")
            yield Footer()

        def on_mount(self) -> None:
            log = self.query_one("#log", RichLog)
            log.write("[bold green]Ready.[/bold green] Type a message and press Enter.")
            log.write(
                "[dim]Use the left controls or /backend and /driver to switch backend and mode. "
                "/models shows valid model options.[/dim]"
            )
            self._refresh_controls()
            self._save_state()
            self._hide_suggestions()
            self._refresh_sessions()
            self.query_one("#prompt", Input).focus()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            self._submit(event.value)

        def on_input_changed(self, event: Input.Changed) -> None:
            self._update_suggestions(event.value)

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "send":
                prompt = self.query_one("#prompt", Input)
                self._submit(prompt.value)
            if event.button.id == "refresh-sessions":
                self._refresh_sessions()
            if event.button.id == "switch-session":
                self._switch_selected_session()
            if event.button.id == "attach-session":
                self._attach_selected_session()
            if event.button.id == "close-session":
                self._close_selected_session()
            if event.button.id == "close-docker":
                self._close_sessions(runtime="docker")
            if event.button.id == "close-tmux":
                self._close_sessions(runtime="tmux")
            if event.button.id == "close-all":
                self._close_sessions(runtime=None)

        def on_select_changed(self, event: Select.Changed) -> None:
            log = self.query_one("#log", RichLog)
            if event.select.id == "backend":
                value = str(event.value)
                if value not in {item.value for item in Backend}:
                    return
                selected = Backend(value)
                if selected is self.conversation.options.backend:
                    return
                self.conversation.set_backend(selected)
                self._refresh_controls()
                self._save_state()
                log.write(f"[bold green]Yikes:[/bold green] Backend set to {selected.value}. Model reset to default.")
                return
            if event.select.id == "model":
                value = str(event.value)
                if value == "default":
                    selected_model = None
                else:
                    valid_models = {option.name for option in self.conversation.model_registry.options(self.conversation.options.backend)}
                    if value not in valid_models:
                        return
                    selected_model = value
                if selected_model == self.conversation.options.model:
                    return
                self.conversation.set_model(selected_model)
                self._refresh_controls()
                self._save_state()
                log.write(f"[bold green]Yikes:[/bold green] Model set to {value}.")
                return
            if event.select.id == "driver":
                value = str(event.value)
                if value not in {item.value for item in Driver}:
                    return
                selected = Driver(value)
                if selected is self.conversation.options.driver:
                    return
                self.conversation.set_driver(selected)
                self._refresh_controls()
                self._save_state()
                log.write(f"[bold green]Yikes:[/bold green] Driver set to {selected.value}.")
                return
            if event.select.id == "web":
                value = str(event.value)
                if value not in {"on", "off"}:
                    return
                enabled = value == "on"
                if enabled is self.conversation.options.settings.web_search_enabled:
                    return
                self.conversation.set_web_search(enabled)
                self._refresh_controls()
                self._save_state()
                state = "enabled" if enabled else "disabled"
                log.write(f"[bold green]Yikes:[/bold green] Web search {state}.")
                return
            if event.select.id == "tmux":
                value = str(event.value)
                if value not in {"on", "off"}:
                    return
                enabled = value == "on"
                if enabled is self.conversation.options.settings.tmux_enabled:
                    return
                self.conversation.set_tmux_enabled(enabled)
                self._refresh_controls()
                self._save_state()
                state = "enabled" if enabled else "disabled"
                log.write(f"[bold green]Yikes:[/bold green] tmux UI transport {state}.")
                return
            if event.select.id == "complexity":
                value = str(event.value)
                if value not in {item.value for item in Complexity}:
                    return
                selected = Complexity(value)
                if selected is self.conversation.options.complexity:
                    return
                self.conversation.set_complexity(selected)
                self._refresh_controls()
                self._save_state()
                log.write(f"[bold green]Yikes:[/bold green] Complexity set to {selected.value}.")

        def action_clear(self) -> None:
            self.query_one("#log", RichLog).clear()

        def action_hide_suggestions(self) -> None:
            self._hide_suggestions()

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

        def _refresh_controls(self) -> None:
            options = self.conversation.options
            self.query_one("#backend-status", Static).update(f"Backend: {options.backend.value}")
            self.query_one("#driver-status", Static).update(f"Mode: {options.driver.value}")
            self.query_one("#model-status", Static).update(f"Model: {options.model or 'default'}")
            self.query_one("#complexity-status", Static).update(f"Complexity: {options.complexity.value}")
            web_state = "on" if options.settings.web_search_enabled else "off"
            tmux_state = "on" if options.settings.tmux_enabled else "off"
            self.query_one("#web-status", Static).update(f"Web: {web_state}")
            self.query_one("#tmux-status", Static).update(f"tmux: {tmux_state}")
            backend_select = self.query_one("#backend", Select)
            driver_select = self.query_one("#driver", Select)
            model_select = self.query_one("#model", Select)
            complexity_select = self.query_one("#complexity", Select)
            web_select = self.query_one("#web", Select)
            tmux_select = self.query_one("#tmux", Select)
            model_value = options.model or "default"
            if backend_select.value != options.backend.value:
                backend_select.value = options.backend.value
            driver_select.set_options(self._driver_select_options())
            if driver_select.value != options.driver.value:
                driver_select.value = options.driver.value
            model_select.set_options(self._model_select_options())
            if model_select.value != model_value:
                model_select.value = model_value
            if complexity_select.value != options.complexity.value:
                complexity_select.value = options.complexity.value
            if web_select.value != web_state:
                web_select.value = web_state
            if tmux_select.value != tmux_state:
                tmux_select.value = tmux_state

        def _driver_select_options(self) -> list[tuple[str, str]]:
            return [
                (option.driver.value, option.driver.value)
                for option in self.conversation.driver_registry.options(self.conversation.options.backend)
            ]

        def _model_select_options(self) -> list[tuple[str, str]]:
            options = self.conversation.model_registry.options(self.conversation.options.backend)
            if not options:
                return [("default", "default")]
            return [(option.name, option.name) for option in options]

        def _refresh_sessions(self) -> None:
            inventory = SessionInventory()
            sessions = inventory.list()
            self.query_one("#sessions", Static).update(inventory.format())
            select = self.query_one("#session-select", Select)
            select_options = [
                (f"{session.runtime}/{session.backend} {session.id}", session.id)
                for session in sessions
            ] or [("no sessions", "")]
            current = str(select.value or "")
            select.set_options(select_options)
            valid = {str(value) for _label, value in select_options}
            select.value = current if current in valid else str(select_options[0][1])

        def _selected_session_id(self) -> str:
            return str(self.query_one("#session-select", Select).value or "")

        def _switch_selected_session(self) -> None:
            log = self.query_one("#log", RichLog)
            session_id = self._selected_session_id()
            if not session_id:
                log.write("[bold yellow]Yikes:[/bold yellow] No session selected.")
                return
            options = SessionLifecycle().switch_options(self.conversation.options, session_id)
            if options is None:
                log.write(f"[bold red]Yikes:[/bold red] Session not found: {session_id}")
                self._refresh_sessions()
                return
            self.conversation.set_options(options)
            self._refresh_controls()
            self._save_state()
            log.write(f"[bold green]Yikes:[/bold green] Switched to {options.driver.value}/{options.backend.value} session {session_id}.")

        def _close_selected_session(self) -> None:
            log = self.query_one("#log", RichLog)
            session_id = self._selected_session_id()
            if not session_id:
                log.write("[bold yellow]Yikes:[/bold yellow] No session selected.")
                return
            result = SessionLifecycle().close(session_id)
            style = "bold green" if result.closed else "bold red"
            log.write(f"[{style}]Yikes:[/{style}] {result.message}")
            self._refresh_sessions()

        def _attach_selected_session(self) -> None:
            log = self.query_one("#log", RichLog)
            session_id = self._selected_session_id()
            if not session_id:
                log.write("[bold yellow]Yikes:[/bold yellow] No session selected.")
                return
            command = SessionLifecycle().attach_command(session_id)
            if command is None:
                log.write(f"[bold red]Yikes:[/bold red] Session not found or not attachable: {session_id}")
                self._refresh_sessions()
                return
            self.attach_command = command
            self.exit()

        def _close_sessions(self, *, runtime: str | None) -> None:
            log = self.query_one("#log", RichLog)
            results = SessionLifecycle().close_all(runtime=runtime)
            if not results:
                target = runtime or "known"
                log.write(f"[bold yellow]Yikes:[/bold yellow] No matching {target} sessions.")
                self._refresh_sessions()
                return
            closed = sum(1 for result in results if result.closed)
            log.write(f"[bold green]Yikes:[/bold green] Closed {closed}/{len(results)} sessions.")
            self._refresh_sessions()

        def _save_state(self) -> None:
            save_app_state(AppState.from_options(self.conversation.options))

        def _submit(self, text: str) -> None:
            text = text.strip()
            if not text:
                return
            prompt = self.query_one("#prompt", Input)
            prompt.value = ""
            self._hide_suggestions()
            log = self.query_one("#log", RichLog)
            if text.startswith("/"):
                self._slash_command(text)
                return
            log.write(f"[bold cyan]You:[/bold cyan] {text}")
            self.ask_backend(text)

        def _slash_command(self, text: str) -> None:
            log = self.query_one("#log", RichLog)
            log.write(f"[bold blue]Command:[/bold blue] {text}")
            result = self.conversation.run_slash_command(text)
            if result.exit_requested:
                self.exit()
                return
            if result.message:
                log.write(f"[bold green]Yikes:[/bold green] {result.message}")
            if result.restart_requested:
                self._save_state()
                self.restart_requested = True
                self.exit()
                return
            self._refresh_controls()
            self._refresh_sessions()
            self._save_state()

        @work(thread=True)
        def ask_backend(self, text: str) -> None:
            log = self.query_one("#log", RichLog)
            self.call_from_thread(log.write, "[yellow]Working...[/yellow]")
            try:
                answer = self.conversation.ask(text)
            except Exception as exc:
                self.call_from_thread(log.write, f"[bold red]Error:[/bold red] {exc}")
                return
            self.call_from_thread(log.write, f"[bold magenta]Assistant:[/bold magenta] {answer}")

    app = YikesApp()
    app.run()
    if app.attach_command:
        os.execvp(app.attach_command[0], app.attach_command)
    if app.restart_requested:
        os.execv(sys.executable, [sys.executable, *sys.argv])
