from __future__ import annotations

from dataclasses import dataclass, field
import tempfile
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .capabilities import DriverRegistry, default_driver_registry
from .commands import (
    CommandRegistry,
    CommandResult,
    CommandSuggestion,
    ModelRegistry,
    default_command_registry,
    default_model_registry,
)
from .domain import (
    AgentSettings,
    Backend,
    ChatOptions,
    ChatResult,
    Complexity,
    Driver,
    DriverMode,
    ExecutionLocation,
    ImageAttachment,
    McpServer,
    Message,
    MessageRole,
)
from .drivers import ask_backend


class ChatTransport(Protocol):
    def ask(self, options: ChatOptions, prompt: str, attachments: tuple[ImageAttachment, ...] = ()) -> str: ...


class BackendTransport:
    def ask(self, options: ChatOptions, prompt: str, attachments: tuple[ImageAttachment, ...] = ()) -> str:
        return ask_backend(
            options.backend,
            options.driver,
            prompt,
            cwd=options.cwd,
            cwd_explicit=options.cwd_explicit,
            timeout=options.timeout,
            model=options.model,
            session_id=options.session_id,
            settings=options.settings,
            attachments=attachments,
        )


@dataclass
class Conversation:
    options: ChatOptions
    transport: ChatTransport = field(default_factory=BackendTransport)
    messages: list[Message] = field(default_factory=list)
    command_registry: CommandRegistry = field(default_factory=default_command_registry)
    model_registry: ModelRegistry = field(default_factory=default_model_registry)
    driver_registry: DriverRegistry = field(default_factory=default_driver_registry)

    def ask(self, text: str, attachments: tuple[ImageAttachment, ...] = ()) -> str:
        self.messages.append(Message(MessageRole.USER, text))
        prompt = self.render_interactive_prompt(text) if self._uses_native_session_history() else self.render_prompt()
        answer = self._ask_transport(prompt, attachments).strip()
        self.messages.append(Message(MessageRole.ASSISTANT, answer))
        return answer

    def _ask_transport(self, prompt: str, attachments: tuple[ImageAttachment, ...]) -> str:
        try:
            return self.transport.ask(self.options, prompt, attachments)
        except TypeError:
            return self.transport.ask(self.options, prompt)  # type: ignore[call-arg]

    def clear(self) -> None:
        self.messages.clear()

    def start_new(self, *, cwd: Path | None = None) -> None:
        self.messages.clear()
        options = self.options.with_session_id(uuid4().hex)
        if cwd is not None:
            options = options.with_cwd(cwd)
        self.options = options

    def set_model(self, model: str | None) -> None:
        self.options = self.options.with_model(model)

    def set_backend(self, backend: Backend | str) -> None:
        selected = Backend(backend)
        self.options = self.options.with_backend(selected).with_driver(
            self.driver_registry.coerce(selected, self.options.driver)
        )

    def set_driver(self, driver: Driver | str) -> None:
        self.options = self.options.with_driver(Driver(driver))

    def set_execution_location(self, location: ExecutionLocation | str) -> None:
        selected = ExecutionLocation(location)
        mode = self.options.mode
        if selected is ExecutionLocation.REMOTE:
            raise ValueError("Remote runtime is planned for remote machines, but is not supported yet.")
        if selected is ExecutionLocation.HOST:
            driver = Driver.TMUX if mode is DriverMode.TMUX else Driver.DIRECT
            self.options = self.options.with_driver(driver).with_settings(
                self.options.settings.with_tmux(False)
            )
            return
        if mode is DriverMode.API:
            raise ValueError("API driver mode is not supported yet.")
        self.options = self.options.with_driver(Driver.DOCKER).with_settings(
            self.options.settings.with_tmux(mode is DriverMode.TMUX)
        )

    def set_driver_mode(self, mode: DriverMode | str) -> None:
        selected = DriverMode(mode)
        location = self.options.location
        if selected is DriverMode.API:
            raise ValueError("API driver mode is not supported yet.")
        if location is ExecutionLocation.REMOTE:
            raise ValueError("Remote runtime is planned for remote machines, but is not supported yet.")
        if location is ExecutionLocation.DOCKER:
            self.options = self.options.with_driver(Driver.DOCKER).with_settings(
                self.options.settings.with_tmux(selected is DriverMode.TMUX)
            )
            return
        driver = Driver.TMUX if selected is DriverMode.TMUX else Driver.DIRECT
        self.options = self.options.with_driver(driver).with_settings(
            self.options.settings.with_tmux(False)
        )

    def set_complexity(self, complexity: Complexity | str) -> None:
        self.options = self.options.with_complexity(Complexity(complexity))

    def set_settings(self, settings: AgentSettings) -> None:
        self.options = self.options.with_settings(settings)

    def set_options(self, options: ChatOptions) -> None:
        self.options = options

    def set_web_search(self, enabled: bool) -> None:
        self.set_settings(self.options.settings.with_web_search(enabled))

    def set_tmux_enabled(self, enabled: bool) -> None:
        self.set_settings(self.options.settings.with_tmux(enabled))

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
            "location": self.options.location.value,
            "driver": self.options.mode.value,
            "internal_driver": self.options.driver.value,
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

    def render_interactive_prompt(self, text: str) -> str:
        return (
            "You are a concise chatbot. Follow the latest user instruction exactly. "
            "When asked for a name as a single word, output only that name.\n\n"
            f"{self._render_settings_prompt()}\n\n"
            f"{text}"
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
        tmux = "enabled" if self.options.driver is Driver.TMUX or settings.tmux_enabled else "disabled"
        return (
            "Runtime configuration:\n"
            f"- Web search: {web}.\n"
            f"- tmux UI transport: {tmux}.\n"
            f"- Allowed read directories: {read_roots}.\n"
            f"- Allowed write directories: {write_roots}.\n"
            f"- Attached MCP servers: {mcps}.\n"
            "Respect these limits when using tools or suggesting file operations."
        )

    def _uses_native_session_history(self) -> bool:
        return self.options.driver is Driver.TMUX or (
            self.options.driver is Driver.DOCKER and self.options.settings.tmux_enabled
        )


@dataclass
class Session:
    """Small embeddable facade over a conversation.

    The long-lived async session manager is still a target architecture. This
    class gives Python callers a stable object to keep in memory today, so a
    web app can bind one chat session to one browser/editor session without
    depending on the Textual UI.
    """

    conversation: Conversation
    id: str = field(default_factory=lambda: uuid4().hex)

    @property
    def options(self) -> ChatOptions:
        return self.conversation.options

    @property
    def messages(self) -> list[Message]:
        return self.conversation.messages

    def ask(self, text: str, attachments: tuple[ImageAttachment, ...] = ()) -> str:
        return self.conversation.ask(text, attachments)

    def prompt(self, text: str, attachments: tuple[ImageAttachment, ...] = ()) -> str:
        return self.ask(text, attachments)

    def clear(self) -> None:
        self.conversation.clear()

    def status(self) -> dict[str, str]:
        return self.conversation.status() | {"session_id": self.id}

    def run_slash_command(self, raw: str) -> CommandResult:
        return self.conversation.run_slash_command(raw)

    def handle_slash_command(self, raw: str) -> str | None:
        return self.conversation.handle_slash_command(raw)

    def slash_suggestions(self, raw: str) -> list[CommandSuggestion]:
        return self.conversation.slash_suggestions(raw)


class ChatService:
    """Backend-neutral service usable from CLI, TUI, or a future web API."""

    def create_session(
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
    ) -> Session:
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
        return Session(conversation)

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
        parsed_backend = Backend(backend)
        parsed_driver = Driver(driver)
        resolved_settings = settings or AgentSettings()
        options = ChatOptions(
            backend=parsed_backend,
            driver=parsed_driver,
            cwd=_resolve_conversation_cwd(cwd, parsed_driver, resolved_settings),
            cwd_explicit=cwd is not None,
            timeout=timeout,
            model=model,
            complexity=Complexity(complexity),
            settings=resolved_settings,
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


def _resolve_conversation_cwd(cwd: Path | None, driver: Driver, settings: AgentSettings) -> Path:
    if cwd is not None:
        return cwd.expanduser()
    if driver is Driver.TMUX:
        return Path(tempfile.mkdtemp(prefix="yikes-tmux-"))
    if driver is Driver.DOCKER and settings.tmux_enabled:
        return Path.cwd()
    return Path.cwd()
