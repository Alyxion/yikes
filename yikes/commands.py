from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shlex
from typing import Callable, Iterable, TYPE_CHECKING

from .capabilities import DriverRegistry
from .domain import Backend, Complexity, Driver, McpServer

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
        spec = self.find(token)
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
        for spec in self.specs:
            if spec.matches(token):
                return spec
        return None

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

    def driver_command(context: CommandContext, arg: str) -> CommandResult:
        if not arg:
            return CommandResult(f"Driver: {context.conversation.options.driver.value}")
        normalized = arg.lower()
        try:
            driver = Driver(normalized)
        except ValueError:
            valid = ", ".join(option.driver.value for option in context.driver_registry.options(context.conversation.options.backend))
            return CommandResult(f"Unknown driver: {arg}. Valid drivers: {valid}.")
        backend = context.conversation.options.backend
        if not context.driver_registry.is_available(backend, driver):
            reason = context.driver_registry.unavailable_reason(backend, driver)
            valid = ", ".join(option.driver.value for option in context.driver_registry.options(backend))
            return CommandResult(f"Driver {driver.value} is not available for {backend.value}: {reason} Valid drivers: {valid}.")
        context.conversation.set_driver(driver)
        return CommandResult(f"Driver set to {driver.value}.")

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

    def web_command(context: CommandContext, arg: str) -> CommandResult:
        normalized = arg.lower()
        if not normalized:
            state = "enabled" if context.conversation.options.settings.web_search_enabled else "disabled"
            return CommandResult(f"Web search: {state}.")
        if normalized in {"on", "enable", "enabled", "true", "yes"}:
            context.conversation.set_web_search(True)
            return CommandResult("Web search enabled.")
        if normalized in {"off", "disable", "disabled", "false", "no"}:
            context.conversation.set_web_search(False)
            return CommandResult("Web search disabled.")
        return CommandResult("Usage: /web [on|off]")

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

    def driver_suggestions(context: CommandContext, prefix: str) -> list[CommandSuggestion]:
        normalized = prefix.lower()
        return [
            CommandSuggestion(
                value=option.driver.value,
                description=option.description,
                completion=f"/driver {option.driver.value}",
            )
            for option in context.driver_registry.options(context.conversation.options.backend)
            if not normalized or option.driver.value.startswith(normalized)
        ]

    def mode_suggestions(context: CommandContext, prefix: str) -> list[CommandSuggestion]:
        suggestions = driver_suggestions(context, prefix)
        return [
            CommandSuggestion(
                value=suggestion.value,
                description=suggestion.description,
                completion=suggestion.completion.replace("/driver ", "/mode ", 1) if suggestion.completion else None,
            )
            for suggestion in suggestions
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
            CommandSuggestion("on", "Enable web search", "/web on"),
            CommandSuggestion("off", "Disable web search", "/web off"),
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

    registry.register(CommandSpec("help", "Show available commands", help_command, aliases=("?",)))
    registry.register(CommandSpec("clear", "Clear this conversation", clear_command))
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
    registry.register(CommandSpec("status", "Show backend, driver, model, cwd, and message count", status_command))
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
            "Set or show the active usage mode",
            driver_command,
            usage="[direct|tmux]",
            argument_suggestions=driver_suggestions,
        )
    )
    registry.register(
        CommandSpec(
            "mode",
            "Alias for /driver",
            driver_command,
            usage="[direct|tmux]",
            argument_suggestions=mode_suggestions,
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
            "Enable, disable, or show web search",
            web_command,
            usage="[on|off]",
            argument_suggestions=web_suggestions,
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
