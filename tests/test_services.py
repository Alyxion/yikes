from __future__ import annotations

from pathlib import Path

from yikes import AgentSettings, Backend, ChatService, Complexity, Driver, McpServer
from yikes.commands import CommandRegistry, CommandSpec, CommandResult, ModelOption, ModelRegistry
import yikes.commands as commands_module
from yikes.capabilities import default_driver_registry
from yikes.domain import ChatOptions


class FakeTransport:
    def ask(self, options: ChatOptions, prompt: str) -> str:
        assert options.backend is Backend.CLAUDE
        assert options.driver is Driver.DIRECT
        if "What is my name?" in prompt:
            assert "my name is Michael" in prompt
            return "Michael"
        if "What is 4+4?" in prompt:
            return "8"
        return "I am doing well."


class CaptureTransport:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def ask(self, options: ChatOptions, prompt: str) -> str:
        self.prompts.append(prompt)
        return "OK"


def test_chat_service_goal_flow_is_backend_neutral() -> None:
    result = ChatService().run_goal_flow(
        Backend.CLAUDE,
        Driver.DIRECT,
        cwd=Path.cwd(),
        transport=FakeTransport(),
    )

    assert result.turns == ["I am doing well.", "8", "Michael"]


def test_conversation_keeps_history_for_later_frontends() -> None:
    conversation = ChatService().create_conversation(
        Backend.CLAUDE,
        Driver.DIRECT,
        cwd=Path.cwd(),
        transport=FakeTransport(),
    )

    conversation.ask("Hello, my name is Michael. How are you doing?")
    prompt = conversation.render_prompt()

    assert "User: Hello, my name is Michael" in prompt
    assert "Assistant: I am doing well." in prompt


def test_tmux_conversation_uses_native_session_history_prompt() -> None:
    transport = CaptureTransport()
    conversation = ChatService().create_conversation(
        Backend.CLAUDE,
        Driver.TMUX,
        cwd=Path.cwd(),
        transport=transport,
    )

    conversation.ask("Say OK only.")

    assert "Say OK only." in transport.prompts[0]
    assert "Assistant:" not in transport.prompts[0]
    assert "tmux UI transport: enabled" in transport.prompts[0]


def test_session_facade_is_easy_to_embed_from_python() -> None:
    session = ChatService().create_session(
        Backend.CLAUDE,
        Driver.DIRECT,
        cwd=Path.cwd(),
        transport=FakeTransport(),
    )

    answer = session.prompt("Hello, my name is Michael. How are you doing?")

    assert answer == "I am doing well."
    assert session.id
    assert session.status()["session_id"] == session.id
    assert session.status()["messages"] == "2"
    assert session.messages[0].text.startswith("Hello")


def test_local_tmux_without_cwd_gets_random_host_workspace() -> None:
    conversation = ChatService().create_conversation(
        Backend.CLAUDE,
        Driver.TMUX,
        transport=FakeTransport(),
    )

    assert conversation.options.cwd_explicit is False
    assert conversation.options.cwd.exists()
    assert conversation.options.cwd.name.startswith("yikes-tmux-")


def test_docker_tmux_without_cwd_keeps_container_workspace_implicit() -> None:
    conversation = ChatService().create_conversation(
        Backend.CLAUDE,
        Driver.DOCKER,
        settings=AgentSettings(tmux_enabled=True),
        transport=FakeTransport(),
    )

    assert conversation.options.cwd_explicit is False
    assert conversation.options.cwd == Path.cwd()
    assert conversation.options.session_id


def test_service_preserves_explicit_remote_control_for_integration_slots() -> None:
    conversation = ChatService().create_conversation(
        Backend.CODEX,
        Driver.REMOTE_CONTROL,
        cwd=Path.cwd(),
        transport=FakeTransport(),
    )

    assert conversation.options.driver is Driver.REMOTE_CONTROL


def test_claude_remote_control_is_not_registered() -> None:
    registry = default_driver_registry()

    assert [option.driver for option in registry.options(Backend.CLAUDE)] == [Driver.DIRECT, Driver.TMUX, Driver.DOCKER]
    assert registry.is_available(Backend.CLAUDE, Driver.REMOTE_CONTROL) is False


def test_slash_commands_are_handled_locally() -> None:
    conversation = ChatService().create_conversation(
        Backend.CLAUDE,
        Driver.DIRECT,
        cwd=Path.cwd(),
        transport=FakeTransport(),
    )

    assert conversation.handle_slash_command("/model sonnet") == "Model set to sonnet."
    assert conversation.options.model == "sonnet"
    assert "Available models for claude:" in conversation.handle_slash_command("/models")
    assert "sonnet (current)" in conversation.handle_slash_command("/models")
    assert conversation.handle_slash_command("/backend") == "Backend: claude"
    assert conversation.handle_slash_command("/driver tmux") == "Driver set to tmux."
    assert conversation.options.driver is Driver.TMUX
    assert "not supported yet" in conversation.handle_slash_command("/mode remote")
    assert conversation.options.driver is Driver.TMUX
    assert conversation.handle_slash_command("/location docker") == "Location set to docker."
    assert conversation.options.driver is Driver.DOCKER
    assert conversation.options.settings.tmux_enabled is True
    assert conversation.handle_slash_command("/driver cli") == "Driver set to cli."
    assert conversation.options.driver is Driver.DOCKER
    assert conversation.options.settings.tmux_enabled is False
    assert conversation.handle_slash_command("/complexity high") == "Complexity set to high."
    assert conversation.options.complexity is Complexity.HIGH
    assert conversation.handle_slash_command("/backend codex") == "Backend set to codex. Model reset to default."
    assert conversation.options.backend is Backend.CODEX
    assert conversation.options.model is None
    status = conversation.handle_slash_command("/status")
    assert "location: docker" in status
    assert "driver: cli" in status
    assert "complexity: high" in status
    assert "web: enabled" in status
    restart_result = conversation.run_slash_command("/restart")
    assert restart_result.message == "Restarting yikes..."
    assert restart_result.restart_requested is True
    assert conversation.handle_slash_command("/clear") == "Conversation cleared."


def test_slash_command_suggestions_come_from_registry() -> None:
    conversation = ChatService().create_conversation(
        Backend.CLAUDE,
        Driver.DIRECT,
        cwd=Path.cwd(),
        transport=FakeTransport(),
    )

    command_suggestions = conversation.slash_suggestions("/mo")
    assert {suggestion.completion for suggestion in command_suggestions} >= {"/model ", "/models"}

    restart_suggestions = conversation.slash_suggestions("/res")
    assert [suggestion.completion for suggestion in restart_suggestions] == ["/restart"]

    model_suggestions = conversation.slash_suggestions("/model s")
    assert [suggestion.completion for suggestion in model_suggestions] == ["/model sonnet"]

    driver_suggestions = conversation.slash_suggestions("/driver")
    assert {suggestion.completion for suggestion in driver_suggestions} == {
        "/driver cli",
        "/driver tmux",
        "/driver api",
    }

    mode_suggestions = conversation.slash_suggestions("/mode d")
    assert [suggestion.completion for suggestion in mode_suggestions] == ["/location docker"]

    backend_suggestions = conversation.slash_suggestions("/backend c")
    assert {suggestion.completion for suggestion in backend_suggestions} == {"/backend claude", "/backend codex"}

    complexity_suggestions = conversation.slash_suggestions("/complexity h")
    assert [suggestion.completion for suggestion in complexity_suggestions] == ["/complexity high"]

    web_suggestions = conversation.slash_suggestions("/web o")
    assert {suggestion.completion for suggestion in web_suggestions} == {"/web on", "/web off"}

    models_preview = conversation.slash_suggestions("/models")
    assert {suggestion.value for suggestion in models_preview} >= {"default", "sonnet", "opus", "haiku"}


def test_runtime_settings_are_configurable_through_slash_commands(tmp_path) -> None:
    conversation = ChatService().create_conversation(
        Backend.CLAUDE,
        Driver.DIRECT,
        cwd=Path.cwd(),
        transport=FakeTransport(),
    )

    read_root = tmp_path / "read"
    write_root = tmp_path / "write"

    assert conversation.handle_slash_command("/web off") == "Web search disabled."
    assert conversation.options.settings.web_search_enabled is False
    assert conversation.handle_slash_command(f"/dirs read add {read_root}") == f"Read directory added: {read_root}"
    assert conversation.handle_slash_command(f"/dirs write add {write_root}") == f"Write directory added: {write_root}"
    assert conversation.options.settings.read_roots == (read_root,)
    assert conversation.options.settings.write_roots == (write_root,)
    assert conversation.handle_slash_command('/mcp add fs "python" "-m" "server"') == "MCP attached: fs -> python -m server"
    assert conversation.options.settings.mcp_servers == (McpServer("fs", "python", ("-m", "server")),)
    assert conversation.handle_slash_command("/mcp disable fs") == "MCP disabled: fs"
    assert conversation.options.settings.mcp_servers[0].enabled is False


def test_session_management_slash_commands_use_lifecycle(monkeypatch, tmp_path) -> None:
    conversation = ChatService().create_conversation(
        Backend.CLAUDE,
        Driver.DIRECT,
        cwd=Path.cwd(),
        transport=FakeTransport(),
    )
    switched = ChatOptions(Backend.CODEX, Driver.DOCKER, tmp_path)

    class FakeLifecycle:
        def close(self, session_id: str):
            return type("Result", (), {"message": f"closed {session_id}"})()

        def close_all(self, *, runtime=None, backend=None):
            assert runtime == "docker"
            assert backend is None
            return [
                type("Result", (), {"closed": True})(),
                type("Result", (), {"closed": False})(),
            ]

        def switch_options(self, current, session_id: str):
            assert current is conversation.options
            assert session_id == "abc"
            return switched

    monkeypatch.setattr(commands_module, "SessionLifecycle", FakeLifecycle)

    assert conversation.handle_slash_command("/close abc") == "closed abc"
    assert conversation.handle_slash_command("/close-all docker") == "Closed 1/2 sessions."
    assert conversation.handle_slash_command("/switch abc") == "Switched to docker/codex session abc."
    assert conversation.options.backend is Backend.CODEX
    assert conversation.options.driver is Driver.DOCKER


def test_command_registry_is_extensible_for_future_frontends() -> None:
    command_registry = CommandRegistry()
    model_registry = ModelRegistry()
    model_registry.register(Backend.CLAUDE, lambda _backend: [ModelOption("custom-model")])

    command_registry.register(
        CommandSpec(
            "ping",
            "Registry-provided command",
            lambda _context, _arg: CommandResult("pong"),
        )
    )

    conversation = ChatService().create_conversation(
        Backend.CLAUDE,
        Driver.DIRECT,
        cwd=Path.cwd(),
        transport=FakeTransport(),
    )
    conversation.command_registry = command_registry
    conversation.model_registry = model_registry

    assert conversation.handle_slash_command("/ping") == "pong"
    assert conversation.slash_suggestions("/p")[0].completion == "/ping"
