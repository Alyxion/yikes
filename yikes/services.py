from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .capabilities import DriverRegistry, default_driver_registry
from .commands import (
    CommandRegistry,
    CommandResult,
    CommandSuggestion,
    ModelRegistry,
    default_command_registry,
    default_model_registry,
)
from .domain import AgentSettings, Backend, ChatOptions, ChatResult, Complexity, Driver, McpServer, Message, MessageRole
from .drivers import ask_backend


class ChatTransport(Protocol):
    def ask(self, options: ChatOptions, prompt: str) -> str: ...


class BackendTransport:
    def ask(self, options: ChatOptions, prompt: str) -> str:
        return ask_backend(
            options.backend,
            options.driver,
            prompt,
            cwd=options.cwd,
            timeout=options.timeout,
            model=options.model,
            settings=options.settings,
        )


@dataclass
class Conversation:
    options: ChatOptions
    transport: ChatTransport = field(default_factory=BackendTransport)
    messages: list[Message] = field(default_factory=list)
    command_registry: CommandRegistry = field(default_factory=default_command_registry)
    model_registry: ModelRegistry = field(default_factory=default_model_registry)
    driver_registry: DriverRegistry = field(default_factory=default_driver_registry)

    def ask(self, text: str) -> str:
        self.messages.append(Message(MessageRole.USER, text))
        prompt = self.render_prompt()
        answer = self.transport.ask(self.options, prompt).strip()
        self.messages.append(Message(MessageRole.ASSISTANT, answer))
        return answer

    def clear(self) -> None:
        self.messages.clear()

    def set_model(self, model: str | None) -> None:
        self.options = self.options.with_model(model)

    def set_backend(self, backend: Backend | str) -> None:
        selected = Backend(backend)
        self.options = self.options.with_backend(selected).with_driver(
            self.driver_registry.coerce(selected, self.options.driver)
        )

    def set_driver(self, driver: Driver | str) -> None:
        self.options = self.options.with_driver(Driver(driver))

    def set_complexity(self, complexity: Complexity | str) -> None:
        self.options = self.options.with_complexity(Complexity(complexity))

    def set_settings(self, settings: AgentSettings) -> None:
        self.options = self.options.with_settings(settings)

    def set_web_search(self, enabled: bool) -> None:
        self.set_settings(self.options.settings.with_web_search(enabled))

    def add_read_root(self, path: Path) -> None:
        self.set_settings(self.options.settings.add_read_root(path))

    def remove_read_root(self, path: Path) -> None:
        self.set_settings(self.options.settings.remove_read_root(path))

    def add_write_root(self, path: Path) -> None:
        self.set_settings(self.options.settings.add_write_root(path))

    def remove_write_root(self, path: Path) -> None:
        self.set_settings(self.options.settings.remove_write_root(path))

    def upsert_mcp(self, server: McpServer) -> None:
        self.set_settings(self.options.settings.upsert_mcp(server))

    def remove_mcp(self, name: str) -> None:
        self.set_settings(self.options.settings.remove_mcp(name))

    def set_mcp_enabled(self, name: str, enabled: bool) -> None:
        self.set_settings(self.options.settings.set_mcp_enabled(name, enabled))

    def status(self) -> dict[str, str]:
        settings = self.options.settings
        return {
            "backend": self.options.backend.value,
            "driver": self.options.driver.value,
            "model": self.options.model or "(default)",
            "complexity": self.options.complexity.value,
            "web": "enabled" if settings.web_search_enabled else "disabled",
            "read_roots": str(len(settings.read_roots)),
            "write_roots": str(len(settings.write_roots)),
            "mcps": str(len(settings.mcp_servers)),
            "cwd": str(self.options.cwd),
            "messages": str(len(self.messages)),
        }

    def run_slash_command(self, raw: str) -> CommandResult:
        return self.command_registry.execute(raw, self, self.model_registry, self.driver_registry)

    def handle_slash_command(self, raw: str) -> str | None:
        return self.run_slash_command(raw).message

    def slash_suggestions(self, raw: str) -> list[CommandSuggestion]:
        return self.command_registry.suggestions(raw, self, self.model_registry, self.driver_registry)

    def render_prompt(self) -> str:
        turns: list[str] = []
        for message in self.messages:
            if message.role is MessageRole.USER:
                turns.append(f"User: {message.text}")
            elif message.role is MessageRole.ASSISTANT:
                turns.append(f"Assistant: {message.text}")
        transcript = "\n".join(turns)
        settings = self._render_settings_prompt()
        return (
            "You are a concise chatbot. Follow the latest user instruction exactly. "
            "When asked for a name as a single word, output only that name.\n\n"
            f"{settings}\n\n"
            f"{transcript}\nAssistant:"
        )

    def _render_settings_prompt(self) -> str:
        settings = self.options.settings
        read_roots = ", ".join(str(path) for path in settings.read_roots) or "(none configured)"
        write_roots = ", ".join(str(path) for path in settings.write_roots) or "(none configured)"
        mcps = ", ".join(
            f"{server.name}={'enabled' if server.enabled else 'disabled'}:{server.display_command}"
            for server in settings.mcp_servers
        ) or "(none attached)"
        web = "enabled" if settings.web_search_enabled else "disabled"
        return (
            "Runtime configuration:\n"
            f"- Web search: {web}.\n"
            f"- Allowed read directories: {read_roots}.\n"
            f"- Allowed write directories: {write_roots}.\n"
            f"- Attached MCP servers: {mcps}.\n"
            "Respect these limits when using tools or suggesting file operations."
        )


class ChatService:
    """Backend-neutral service usable from CLI, TUI, or a future web API."""

    def create_conversation(
        self,
        backend: Backend | str,
        driver: Driver | str,
        *,
        cwd: Path | None = None,
        timeout: float = 180.0,
        model: str | None = None,
        complexity: Complexity | str = Complexity.MEDIUM,
        settings: AgentSettings | None = None,
        transport: ChatTransport | None = None,
    ) -> Conversation:
        options = ChatOptions(
            backend=Backend(backend),
            driver=Driver(driver),
            cwd=cwd or Path.cwd(),
            timeout=timeout,
            model=model,
            complexity=Complexity(complexity),
            settings=settings or AgentSettings(),
        )
        return Conversation(options, transport or BackendTransport(), driver_registry=default_driver_registry())

    def run_goal_flow(
        self,
        backend: Backend | str,
        driver: Driver | str,
        *,
        cwd: Path | None = None,
        timeout: float = 180.0,
        model: str | None = None,
        complexity: Complexity | str = Complexity.MEDIUM,
        settings: AgentSettings | None = None,
        transport: ChatTransport | None = None,
    ) -> ChatResult:
        conversation = self.create_conversation(
            backend,
            driver,
            cwd=cwd,
            timeout=timeout,
            model=model,
            complexity=complexity,
            settings=settings,
            transport=transport,
        )
        turns = [
            conversation.ask("Hello, my name is Michael. How are you doing? Reply in one short sentence."),
            conversation.ask("What is 4+4? Answer with only the number."),
            conversation.ask("What is my name? Answer with exactly one word and no punctuation."),
        ]
        return ChatResult(conversation.options.backend, conversation.options.driver, turns)
