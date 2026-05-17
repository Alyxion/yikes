from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shlex
from typing import Callable, Iterable, TYPE_CHECKING

from .capabilities import DriverRegistry
from .domain import Backend, Complexity, DriverMode, ExecutionLocation, McpServer
from .session_inventory import SessionInventory, SessionLifecycle

if TYPE_CHECKING:  # pragma: no cover
    from .services import Conversation


@dataclass(frozen=True)
class CommandResult:
    message: str | None = None
    exit_requested: bool = False
    restart_requested: bool = False


@dataclass(frozen=True)
class CommandSuggestion:
    value: str
    description: str = ""
    completion: str | None = None


@dataclass(frozen=True)
class ModelOption:
    name: str
    description: str = ""


ModelProvider = Callable[[Backend], Iterable[ModelOption]]


@dataclass
class ModelRegistry:
    providers: dict[Backend, ModelProvider] = field(default_factory=dict)

    def register(self, backend: Backend, provider: ModelProvider) -> None:
        self.providers[backend] = provider

    def options(self, backend: Backend) -> list[ModelOption]:
        provider = self.providers.get(backend)
        if provider is None:
            return []
        return sorted(provider(backend), key=lambda option: option.name)

    def suggestions(
        self,
        backend: Backend,
        prefix: str = "",
        *,
        command_prefix: str | None = None,
    ) -> list[CommandSuggestion]:
        normalized = prefix.lower()
        suggestions: list[CommandSuggestion] = []
        for option in self.options(backend):
            if normalized and not option.name.lower().startswith(normalized):
                continue
            suggestions.append(
                CommandSuggestion(
                    value=option.name,
                    description=option.description,
                    completion=f"{command_prefix}{option.name}" if command_prefix else option.name,
                )
            )
        return suggestions


@dataclass
class CommandContext:
    conversation: Conversation
    registry: CommandRegistry
    model_registry: ModelRegistry
    driver_registry: DriverRegistry


CommandHandler = Callable[[CommandContext, str], CommandResult]
SuggestionProvider = Callable[[CommandContext, str], list[CommandSuggestion]]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    handler: CommandHandler
    usage: str = ""
    aliases: tuple[str, ...] = ()
    argument_suggestions: SuggestionProvider | None = None
    preview_suggestions: SuggestionProvider | None = None

    @property
    def display_name(self) -> str:
        return f"/{self.name}"

    @property
    def display_usage(self) -> str:
        return f"{self.display_name} {self.usage}".strip()

    @property
    def completion(self) -> str:
        suffix = " " if self.argument_suggestions else ""
        return f"{self.display_name}{suffix}"

    def matches(self, token: str) -> bool:
        normalized = token.removeprefix("/").lower()
        return normalized == self.name or normalized in self.aliases

    def starts_with(self, prefix: str) -> bool:
        normalized = prefix.removeprefix("/").lower()
        return self.name.startswith(normalized) or any(alias.startswith(normalized) for alias in self.aliases)


@dataclass
class CommandRegistry:
    specs: list[CommandSpec] = field(default_factory=list)

    def register(self, spec: CommandSpec) -> None:
        self.specs.append(spec)

    def execute(
        self,
        raw: str,
        conversation: Conversation,
        model_registry: ModelRegistry,
        driver_registry: DriverRegistry,
    ) -> CommandResult:
        token, arg = self._parse(raw)
        spec = self.find(token)
        if spec is None:
            command = token or raw.strip()
            return CommandResult(f"Unknown command: {command}. Type /help.")
        return spec.handler(CommandContext(conversation, self, model_registry, driver_registry), arg)

    def suggestions(
        self,
        raw: str,
        conversation: Conversation,
        model_registry: ModelRegistry,
        driver_registry: DriverRegistry,
    ) -> list[CommandSuggestion]:
        text = raw.lstrip()
        if not text.startswith("/"):
            return []

        token, arg = self._parse(text)
        has_arg_boundary = " " in text
        spec = self.find_exact(token)
        if spec is not None:
            context = CommandContext(conversation, self, model_registry, driver_registry)
            if has_arg_boundary and spec.argument_suggestions:
                return spec.argument_suggestions(context, arg)
            if not has_arg_boundary and spec.preview_suggestions:
                return spec.preview_suggestions(context, arg)
            if not has_arg_boundary and spec.argument_suggestions:
                return spec.argument_suggestions(context, "")
            return []

        prefix = token.removeprefix("/").lower()
        matches = [
            CommandSuggestion(
                value=spec.display_usage,
                description=spec.description,
                completion=spec.completion,
            )
            for spec in self.specs
            if spec.name.startswith(prefix) or any(alias.startswith(prefix) for alias in spec.aliases)
        ]
        return matches

    def find(self, token: str) -> CommandSpec | None:
        exact = self.find_exact(token)
        if exact is not None:
            return exact
        prefix = token.removeprefix("/").lower()
        if not prefix:
            return None
        matches = [spec for spec in self.specs if spec.starts_with(prefix)]
        if len(matches) == 1:
            return matches[0]
        return None

    def find_exact(self, token: str) -> CommandSpec | None:
        for spec in self.specs:
            if spec.matches(token):
                return spec
        return None

    def resolve_name(self, token: str) -> str | None:
        spec = self.find(token)
        return spec.name if spec is not None else None

    def help_text(self) -> str:
        return "Commands: " + ", ".join(spec.display_usage for spec in self.specs)

    @staticmethod
    def _parse(raw: str) -> tuple[str, str]:
        text = raw.strip()
        if not text:
            return "", ""
        parts = text.split(maxsplit=1)
        token = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        return token, arg


def default_model_registry() -> ModelRegistry:
    registry = ModelRegistry()
    registry.register(
        Backend.CLAUDE,
        lambda _backend: [
            ModelOption("default", "Use Claude Code's configured default model"),
            ModelOption("haiku", "Claude fast model alias"),
            ModelOption("opus", "Claude most capable model alias"),
            ModelOption("sonnet", "Claude balanced model alias"),
        ],
    )
    registry.register(
        Backend.CODEX,
        lambda _backend: [
            ModelOption("default", "Use Codex's configured default model"),
            ModelOption("gpt-5.2", "OpenAI GPT-5.2"),
            ModelOption("gpt-5.3-codex", "Codex-optimized GPT-5.3"),
            ModelOption("gpt-5.3-codex-spark", "Fast Codex GPT-5.3"),
            ModelOption("gpt-5.4", "OpenAI GPT-5.4"),
            ModelOption("gpt-5.4-mini", "OpenAI GPT-5.4 mini"),
            ModelOption("gpt-5.5", "OpenAI GPT-5.5"),
        ],
    )
    return registry


def default_command_registry() -> CommandRegistry:
    registry = CommandRegistry()
    def help_command(context: CommandContext, _arg: str) -> CommandResult:
        return CommandResult(context.registry.help_text())

    def clear_command(context: CommandContext, _arg: str) -> CommandResult:
        context.conversation.clear()
        return CommandResult("Conversation cleared.")

    def new_command(context: CommandContext, arg: str) -> CommandResult:
        parts = _split_args(arg)
        cwd = Path(parts[0]).expanduser() if parts else None
        context.conversation.start_new(cwd=cwd)
        options = context.conversation.options
        return CommandResult(
            "New session: "
            f"{options.backend.value} on {options.location.value} via {options.mode.value}; "
            f"model {options.model or 'default'}; "
            f"complexity {options.complexity.value}; "
            f"web {'on' if options.settings.web_search_enabled else 'off'}; "
            f"root {options.cwd}."
        )

    def model_command(context: CommandContext, arg: str) -> CommandResult:
        model = None if arg == "default" else arg
        if arg:
            context.conversation.set_model(model)
            return CommandResult(f"Model set to {arg}.")
        current = context.conversation.options.model or "default"
        return CommandResult(f"Current model: {current}")

    def models_command(context: CommandContext, _arg: str) -> CommandResult:
        backend = context.conversation.options.backend
        options = context.model_registry.options(backend)
        if not options:
            return CommandResult(f"No model options registered for {backend.value}.")
        current = context.conversation.options.model or "default"
        lines = [f"Available models for {backend.value}:"]
        for option in options:
            marker = " (current)" if option.name == current else ""
            detail = f" - {option.description}" if option.description else ""
            lines.append(f"{option.name}{marker}{detail}")
        return CommandResult("\n".join(lines))

    def status_command(context: CommandContext, _arg: str) -> CommandResult:
        status = context.conversation.status()
        return CommandResult(" | ".join(f"{key}: {value}" for key, value in status.items()))

    def sessions_command(_context: CommandContext, _arg: str) -> CommandResult:
        return CommandResult(SessionInventory().format())

    def close_command(_context: CommandContext, arg: str) -> CommandResult:
        session_id = arg.strip()
        if not session_id:
            return CommandResult("Usage: /close <session-id>")
        return CommandResult(SessionLifecycle().close(session_id).message)

    def close_all_command(_context: CommandContext, arg: str) -> CommandResult:
        parts = _split_args(arg)
        runtime = parts[0].lower() if parts else None
        results = SessionLifecycle().close_all(runtime=runtime)
        if not results:
            target = runtime or "sessions"
            return CommandResult(f"No matching {target} sessions.")
        closed = sum(1 for result in results if result.closed)
        return CommandResult(f"Closed {closed}/{len(results)} sessions.")

    def switch_command(context: CommandContext, arg: str) -> CommandResult:
        session_id = arg.strip()
        if not session_id:
            return CommandResult("Usage: /switch <session-id>")
        options = SessionLifecycle().switch_options(context.conversation.options, session_id)
        if options is None:
            return CommandResult(f"Session not found: {session_id}")
        context.conversation.set_options(options)
        return CommandResult(f"Switched to {options.driver.value}/{options.backend.value} session {session_id}.")

    def attach_command(_context: CommandContext, arg: str) -> CommandResult:
        session_id = arg.strip()
        if not session_id:
            return CommandResult("Usage: /attach <session-id>")
        command = SessionLifecycle().attach_command(session_id)
        if command is None:
            return CommandResult(f"Session not found or not attachable: {session_id}")
        return CommandResult("Attach command: " + shlex.join(command))

    def backend_command(context: CommandContext, arg: str) -> CommandResult:
        if not arg:
            return CommandResult(f"Backend: {context.conversation.options.backend.value}")
        try:
            backend = Backend(arg)
        except ValueError:
            valid = ", ".join(backend.value for backend in Backend)
            return CommandResult(f"Unknown backend: {arg}. Valid backends: {valid}.")
        context.conversation.set_backend(backend)
        return CommandResult(f"Backend set to {backend.value}. Model reset to default.")

    def location_command(context: CommandContext, arg: str) -> CommandResult:
        if not arg:
            return CommandResult(f"Location: {context.conversation.options.location.value}")
        normalized = arg.lower()
        try:
            location = ExecutionLocation(normalized)
        except ValueError:
            valid = ", ".join(location.value for location in ExecutionLocation)
            return CommandResult(f"Unknown location: {arg}. Valid locations: {valid}.")
        try:
            context.conversation.set_execution_location(location)
        except ValueError as exc:
            return CommandResult(str(exc))
        return CommandResult(f"Location set to {location.value}.")

    def driver_command(context: CommandContext, arg: str) -> CommandResult:
        if not arg:
            return CommandResult(f"Driver: {context.conversation.options.mode.value}")
        normalized = arg.lower()
        try:
            mode = DriverMode(normalized)
        except ValueError:
            valid = ", ".join(mode.value for mode in DriverMode)
            return CommandResult(f"Unknown driver: {arg}. Valid drivers: {valid}.")
        try:
            context.conversation.set_driver_mode(mode)
        except ValueError as exc:
            return CommandResult(str(exc))
        return CommandResult(f"Driver set to {mode.value}.")

    def complexity_command(context: CommandContext, arg: str) -> CommandResult:
        if not arg:
            return CommandResult(f"Complexity: {context.conversation.options.complexity.value}")
        try:
            complexity = Complexity(arg.lower())
        except ValueError:
            valid = ", ".join(level.value for level in Complexity)
            return CommandResult(f"Unknown complexity: {arg}. Valid levels: {valid}.")
        context.conversation.set_complexity(complexity)
        return CommandResult(f"Complexity set to {complexity.value}.")

    def exit_command(_context: CommandContext, _arg: str) -> CommandResult:
        return CommandResult(exit_requested=True)

    def restart_command(_context: CommandContext, _arg: str) -> CommandResult:
        return CommandResult("Restarting yikes...", restart_requested=True)

    def view_command(_context: CommandContext, _arg: str) -> CommandResult:
        return CommandResult("Usage: /view [high|dev]")

    def key_command(_context: CommandContext, _arg: str) -> CommandResult:
        return CommandResult("Usage: /key <Enter|Up|Down|Left|Right|Escape|C-c|...>")

    def paste_command(_context: CommandContext, _arg: str) -> CommandResult:
        return CommandResult("Usage: /paste <text>")

    def fullscreen_command(_context: CommandContext, _arg: str) -> CommandResult:
        return CommandResult("Fullscreen tmux attach is available in the terminal UI.")

    def term_command(_context: CommandContext, _arg: str) -> CommandResult:
        return CommandResult("Interactive terminal attach is available in the terminal and web UIs.")

    def web_command(context: CommandContext, arg: str) -> CommandResult:
        normalized = arg.lower()
        if not normalized:
            state = "enabled" if context.conversation.options.settings.web_search_enabled else "disabled"
            return CommandResult(f"Web search: {state}. In the terminal UI, /web opens the authenticated web app.")
        if normalized in {"open", "ui", "app"}:
            return CommandResult("Opening the web app is available in the terminal UI.")
        if normalized in {"on", "enable", "enabled", "true", "yes"}:
            context.conversation.set_web_search(True)
            return CommandResult("Web search enabled.")
        if normalized in {"off", "disable", "disabled", "false", "no"}:
            context.conversation.set_web_search(False)
            return CommandResult("Web search disabled.")
        return CommandResult("Usage: /web [on|off]")

    def capture_command(context: CommandContext, arg: str) -> CommandResult:
        normalized = arg.lower()
        if not normalized:
            state = "enabled" if context.conversation.options.settings.managed_output_enabled else "disabled"
            return CommandResult(f"Answer capture: {state}.")
        if normalized in {"on", "enable", "enabled", "true", "yes"}:
            context.conversation.set_settings(context.conversation.options.settings.with_managed_output(True))
            return CommandResult("Answer capture enabled.")
        if normalized in {"off", "disable", "disabled", "false", "no", "raw"}:
            context.conversation.set_settings(context.conversation.options.settings.with_managed_output(False))
            return CommandResult("Answer capture disabled.")
        return CommandResult("Usage: /capture [on|off]")

    def tmux_command(context: CommandContext, arg: str) -> CommandResult:
        normalized = arg.lower()
        if not normalized:
            return CommandResult(
                "tmux is now selected with /driver tmux. "
                f"Current driver: {context.conversation.options.mode.value}."
            )
        if normalized in {"on", "enable", "enabled", "true", "yes"}:
            try:
                context.conversation.set_driver_mode(DriverMode.TMUX)
            except ValueError as exc:
                return CommandResult(str(exc))
            return CommandResult("Driver set to tmux.")
        if normalized in {"off", "disable", "disabled", "false", "no"}:
            try:
                context.conversation.set_driver_mode(DriverMode.CLI)
            except ValueError as exc:
                return CommandResult(str(exc))
            return CommandResult("Driver set to cli.")
        return CommandResult("Usage: /tmux [on|off] or /driver [cli|tmux|api]")

    def dirs_command(context: CommandContext, arg: str) -> CommandResult:
        parts = _split_args(arg)
        if not parts:
            return CommandResult(_render_dirs(context))
        kind = parts[0].lower()
        if kind not in {"read", "write"}:
            return CommandResult("Usage: /dirs [read|write] [add|remove|list] [path]")
        action = parts[1].lower() if len(parts) > 1 else "list"
        if action == "list":
            return CommandResult(_render_dirs(context, kind))
        if len(parts) < 3 or action not in {"add", "remove"}:
            return CommandResult(f"Usage: /dirs {kind} [add|remove|list] [path]")
        path = Path(parts[2]).expanduser()
        if kind == "read" and action == "add":
            context.conversation.add_read_root(path)
            return CommandResult(f"Read directory added: {path}")
        if kind == "read" and action == "remove":
            context.conversation.remove_read_root(path)
            return CommandResult(f"Read directory removed: {path}")
        if kind == "write" and action == "add":
            context.conversation.add_write_root(path)
            return CommandResult(f"Write directory added: {path}")
        context.conversation.remove_write_root(path)
        return CommandResult(f"Write directory removed: {path}")

    def mcp_command(context: CommandContext, arg: str) -> CommandResult:
        parts = _split_args(arg)
        if not parts or parts[0].lower() == "list":
            return CommandResult(_render_mcps(context))
        action = parts[0].lower()
        if action == "add":
            if len(parts) < 3:
                return CommandResult("Usage: /mcp add <name> <command> [args...]")
            server = McpServer(parts[1], parts[2], tuple(parts[3:]), enabled=True)
            context.conversation.upsert_mcp(server)
            return CommandResult(f"MCP attached: {server.name} -> {server.display_command}")
        if action == "remove" and len(parts) >= 2:
            context.conversation.remove_mcp(parts[1])
            return CommandResult(f"MCP removed: {parts[1]}")
        if action in {"enable", "disable"} and len(parts) >= 2:
            context.conversation.set_mcp_enabled(parts[1], action == "enable")
            state = "enabled" if action == "enable" else "disabled"
            return CommandResult(f"MCP {state}: {parts[1]}")
        return CommandResult("Usage: /mcp [list|add|remove|enable|disable] ...")

    def model_suggestions(context: CommandContext, prefix: str) -> list[CommandSuggestion]:
        return context.model_registry.suggestions(
            context.conversation.options.backend,
            prefix,
            command_prefix="/model ",
        )

    def backend_suggestions(_context: CommandContext, prefix: str) -> list[CommandSuggestion]:
        normalized = prefix.lower()
        return [
            CommandSuggestion(
                value=backend.value,
                description="Available backend",
                completion=f"/backend {backend.value}",
            )
            for backend in Backend
            if not normalized or backend.value.startswith(normalized)
        ]

    def location_suggestions(_context: CommandContext, prefix: str) -> list[CommandSuggestion]:
        normalized = prefix.lower()
        descriptions = {
            ExecutionLocation.HOST: "Run on this host",
            ExecutionLocation.DOCKER: "Run in a Docker sandbox",
            ExecutionLocation.REMOTE: "Future remote machine/server runtime",
        }
        return [
            CommandSuggestion(
                value=location.value,
                description=descriptions[location],
                completion=f"/location {location.value}",
            )
            for location in ExecutionLocation
            if not normalized or location.value.startswith(normalized)
        ]

    def driver_suggestions(_context: CommandContext, prefix: str) -> list[CommandSuggestion]:
        normalized = prefix.lower()
        descriptions = {
            DriverMode.CLI: "Drive the backend through the CLI/protocol fast path",
            DriverMode.TMUX: "Drive a real interactive TUI through tmux",
            DriverMode.API: "Future structured API/app-server mode",
        }
        return [
            CommandSuggestion(
                value=mode.value,
                description=descriptions[mode],
                completion=f"/driver {mode.value}",
            )
            for mode in DriverMode
            if not normalized or mode.value.startswith(normalized)
        ]

    def complexity_suggestions(_context: CommandContext, prefix: str) -> list[CommandSuggestion]:
        normalized = prefix.lower()
        descriptions = {
            Complexity.LOW: "Fastest responses with lighter reasoning",
            Complexity.MEDIUM: "Balanced reasoning for everyday work",
            Complexity.HIGH: "Deeper reasoning for complex tasks",
            Complexity.XHIGH: "Maximum reasoning for difficult tasks",
        }
        return [
            CommandSuggestion(
                value=level.value,
                description=descriptions[level],
                completion=f"/complexity {level.value}",
            )
            for level in Complexity
            if not normalized or level.value.startswith(normalized)
        ]

    def web_suggestions(_context: CommandContext, prefix: str) -> list[CommandSuggestion]:
        normalized = prefix.lower()
        options = [
            CommandSuggestion("open", "Open the authenticated web app", "/web"),
            CommandSuggestion("on", "Enable web search", "/web on"),
            CommandSuggestion("off", "Disable web search", "/web off"),
        ]
        return [option for option in options if not normalized or option.value.startswith(normalized)]

    def capture_suggestions(_context: CommandContext, prefix: str) -> list[CommandSuggestion]:
        normalized = prefix.lower()
        options = [
            CommandSuggestion("on", "Enable clean answer capture for tmux chat turns", "/capture on"),
            CommandSuggestion("off", "Use raw interactive tmux turns with no answer boundary", "/capture off"),
        ]
        return [option for option in options if not normalized or option.value.startswith(normalized)]

    def tmux_suggestions(_context: CommandContext, prefix: str) -> list[CommandSuggestion]:
        normalized = prefix.lower()
        options = [
            CommandSuggestion("on", "Enable real interactive tmux transport", "/tmux on"),
            CommandSuggestion("off", "Disable tmux transport", "/tmux off"),
        ]
        return [option for option in options if not normalized or option.value.startswith(normalized)]

    def view_suggestions(_context: CommandContext, prefix: str) -> list[CommandSuggestion]:
        normalized = prefix.lower()
        options = [
            CommandSuggestion("high", "Show user-facing prompts and answers", "/view high"),
            CommandSuggestion("dev", "Show the full raw terminal pane", "/view dev"),
        ]
        return [option for option in options if not normalized or option.value.startswith(normalized)]

    def dirs_suggestions(_context: CommandContext, prefix: str) -> list[CommandSuggestion]:
        normalized = prefix.lower()
        options = [
            CommandSuggestion("read add", "Allow reading from a directory", "/dirs read add "),
            CommandSuggestion("read list", "Show read directories", "/dirs read list"),
            CommandSuggestion("read remove", "Remove a read directory", "/dirs read remove "),
            CommandSuggestion("write add", "Allow writing to a directory", "/dirs write add "),
            CommandSuggestion("write list", "Show write directories", "/dirs write list"),
            CommandSuggestion("write remove", "Remove a write directory", "/dirs write remove "),
        ]
        return [option for option in options if not normalized or option.value.startswith(normalized)]

    def mcp_suggestions(_context: CommandContext, prefix: str) -> list[CommandSuggestion]:
        normalized = prefix.lower()
        options = [
            CommandSuggestion("list", "Show attached MCP servers", "/mcp list"),
            CommandSuggestion("add", "Attach an MCP server", "/mcp add "),
            CommandSuggestion("remove", "Remove an MCP server", "/mcp remove "),
            CommandSuggestion("enable", "Enable an MCP server", "/mcp enable "),
            CommandSuggestion("disable", "Disable an MCP server", "/mcp disable "),
        ]
        return [option for option in options if not normalized or option.value.startswith(normalized)]

    def session_suggestions(_context: CommandContext, prefix: str) -> list[CommandSuggestion]:
        normalized = prefix.lower()
        return [
            CommandSuggestion(
                value=session.id,
                description=f"{session.runtime}/{session.backend} {session.state}",
                completion=f"/switch {session.id}",
            )
            for session in SessionInventory().list()
            if not normalized or session.id.lower().startswith(normalized)
        ]

    def close_suggestions(_context: CommandContext, prefix: str) -> list[CommandSuggestion]:
        normalized = prefix.lower()
        return [
            CommandSuggestion(
                value=session.id,
                description=f"Close {session.runtime}/{session.backend}",
                completion=f"/close {session.id}",
            )
            for session in SessionInventory().list()
            if not normalized or session.id.lower().startswith(normalized)
        ]

    def close_all_suggestions(_context: CommandContext, prefix: str) -> list[CommandSuggestion]:
        normalized = prefix.lower()
        options = [
            CommandSuggestion("docker", "Close all Docker sessions", "/close-all docker"),
            CommandSuggestion("tmux", "Close all tmux sessions", "/close-all tmux"),
            CommandSuggestion("remote-server", "Close all remote-server sessions", "/close-all remote-server"),
            CommandSuggestion("all", "Close all known sessions", "/close-all all"),
        ]
        return [option for option in options if not normalized or option.value.startswith(normalized)]

    registry.register(CommandSpec("help", "Show available commands", help_command, aliases=("?",)))
    registry.register(CommandSpec("clear", "Clear this conversation", clear_command))
    registry.register(CommandSpec("new", "Start a new session with the current defaults", new_command, usage="[cwd]"))
    registry.register(
        CommandSpec(
            "model",
            "Set or show the active model",
            model_command,
            usage="[name]",
            argument_suggestions=model_suggestions,
        )
    )
    registry.register(
        CommandSpec(
            "models",
            "Show valid model options for the active backend",
            models_command,
            preview_suggestions=model_suggestions,
        )
    )
    registry.register(CommandSpec("status", "Show backend, location, driver, model, and message count", status_command))
    registry.register(CommandSpec("sessions", "List known yikes! tmux, Docker, and remote sessions", sessions_command, aliases=("ps",)))
    registry.register(
        CommandSpec(
            "switch",
            "Switch active context to a known session",
            switch_command,
            usage="<session-id>",
            argument_suggestions=session_suggestions,
        )
    )
    registry.register(
        CommandSpec(
            "attach",
            "Show command to overtake/attach to a session",
            attach_command,
            usage="<session-id>",
            argument_suggestions=session_suggestions,
        )
    )
    registry.register(
        CommandSpec(
            "close",
            "Close one known session",
            close_command,
            usage="<session-id>",
            argument_suggestions=close_suggestions,
        )
    )
    registry.register(
        CommandSpec(
            "close-all",
            "Close sessions by runtime",
            close_all_command,
            usage="[docker|tmux|remote-server|all]",
            argument_suggestions=close_all_suggestions,
        )
    )
    registry.register(
        CommandSpec(
            "backend",
            "Set or show the active backend",
            backend_command,
            usage="[name]",
            argument_suggestions=backend_suggestions,
        )
    )
    registry.register(
        CommandSpec(
            "driver",
            "Set or show how the backend is driven",
            driver_command,
            usage="[cli|tmux|api]",
            argument_suggestions=driver_suggestions,
        )
    )
    registry.register(
        CommandSpec(
            "location",
            "Set or show where the backend runs",
            location_command,
            usage="[host|docker|remote]",
            aliases=("where", "runtime"),
            argument_suggestions=location_suggestions,
        )
    )
    registry.register(
        CommandSpec(
            "mode",
            "Alias for /location",
            location_command,
            usage="[host|docker|remote]",
            argument_suggestions=location_suggestions,
        )
    )
    registry.register(
        CommandSpec(
            "complexity",
            "Set or show the reasoning complexity level",
            complexity_command,
            usage="[low|medium|high|xhigh]",
            argument_suggestions=complexity_suggestions,
        )
    )
    registry.register(
        CommandSpec(
            "web",
            "Open the web app, or enable/disable web search",
            web_command,
            usage="[open|on|off]",
            argument_suggestions=web_suggestions,
        )
    )
    registry.register(
        CommandSpec(
            "capture",
            "Enable or disable managed answer capture",
            capture_command,
            usage="[on|off]",
            argument_suggestions=capture_suggestions,
        )
    )
    registry.register(
        CommandSpec(
            "tmux",
            "Enable, disable, or show tmux UI transport",
            tmux_command,
            usage="[on|off]",
            argument_suggestions=tmux_suggestions,
        )
    )
    registry.register(
        CommandSpec(
            "dirs",
            "Configure readable and writable directories",
            dirs_command,
            usage="[read|write] [add|remove|list] [path]",
            argument_suggestions=dirs_suggestions,
        )
    )
    registry.register(
        CommandSpec(
            "mcp",
            "Attach and manage MCP servers",
            mcp_command,
            usage="[list|add|remove|enable|disable]",
            argument_suggestions=mcp_suggestions,
        )
    )
    registry.register(CommandSpec("restart", "Restart the terminal app and reload local code", restart_command))
    registry.register(
        CommandSpec(
            "view",
            "Switch terminal output view",
            view_command,
            usage="[full|extracted]",
            argument_suggestions=view_suggestions,
        )
    )
    registry.register(CommandSpec("key", "Send one raw key to the selected tmux session", key_command, usage="<key>"))
    registry.register(CommandSpec("paste", "Paste text into the selected tmux session", paste_command, usage="<text>"))
    registry.register(
        CommandSpec(
            "fullscreen",
            "Overtake the selected tmux session in fullscreen",
            fullscreen_command,
            aliases=("overtake",),
        )
    )
    registry.register(
        CommandSpec(
            "term",
            "Attach interactively to the selected tmux session",
            term_command,
            aliases=("terminal",),
        )
    )
    registry.register(CommandSpec("exit", "Exit the terminal app", exit_command))
    return registry


def _split_args(arg: str) -> list[str]:
    try:
        return shlex.split(arg)
    except ValueError:
        return arg.split()


def _render_dirs(context: CommandContext, kind: str | None = None) -> str:
    settings = context.conversation.options.settings
    lines: list[str] = []
    if kind in {None, "read"}:
        read = ", ".join(str(path) for path in settings.read_roots) or "(none)"
        lines.append(f"Read directories: {read}")
    if kind in {None, "write"}:
        write = ", ".join(str(path) for path in settings.write_roots) or "(none)"
        lines.append(f"Write directories: {write}")
    return "\n".join(lines)


def _render_mcps(context: CommandContext) -> str:
    servers = context.conversation.options.settings.mcp_servers
    if not servers:
        return "MCP servers: (none)"
    lines = ["MCP servers:"]
    for server in servers:
        state = "enabled" if server.enabled else "disabled"
        lines.append(f"{server.name}: {state} - {server.display_command}")
    return "\n".join(lines)
