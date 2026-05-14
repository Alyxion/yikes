from __future__ import annotations

from pathlib import Path

from .domain import AgentSettings, Backend, ChatResult, Complexity, Driver
from .services import ChatService, Conversation


class Chatbot:
    """Compatibility wrapper around the backend-neutral Conversation service."""

    def __init__(
        self,
        backend: Backend,
        driver: Driver,
        *,
        cwd: Path | None = None,
        timeout: float = 180.0,
        model: str | None = None,
        complexity: Complexity | str = Complexity.MEDIUM,
        settings: AgentSettings | None = None,
    ) -> None:
        self.conversation: Conversation = ChatService().create_conversation(
            backend,
            driver,
            cwd=cwd,
            timeout=timeout,
            model=model,
            complexity=complexity,
            settings=settings,
        )

    @property
    def backend(self) -> Backend:
        return self.conversation.options.backend

    @property
    def driver(self) -> Driver:
        return self.conversation.options.driver

    def ask(self, message: str) -> str:
        return self.conversation.ask(message)

    def run_goal_flow(self) -> ChatResult:
        turns = [self.ask("Hello, my name is Michael. How are you doing? Reply in one short sentence.")]
        turns.append(self.ask("What is 4+4? Answer with only the number."))
        turns.append(self.ask("What is my name? Answer with exactly one word and no punctuation."))
        return ChatResult(self.backend, self.driver, turns)


def run_goal_flow(
    backend: Backend | str,
    driver: Driver | str,
    *,
    cwd: Path | None = None,
    timeout: float = 180.0,
    model: str | None = None,
    complexity: Complexity | str = Complexity.MEDIUM,
    settings: AgentSettings | None = None,
) -> ChatResult:
    return ChatService().run_goal_flow(
        backend,
        driver,
        cwd=cwd or Path.cwd(),
        timeout=timeout,
        model=model,
        complexity=complexity,
        settings=settings,
    )
