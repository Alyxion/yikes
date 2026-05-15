from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from .domain import Backend, Driver


@dataclass(frozen=True)
class DriverOption:
    driver: Driver
    description: str
    available: bool = True
    unavailable_reason: str = ""


DriverProvider = Callable[[], Iterable[DriverOption]]


@dataclass
class DriverRegistry:
    providers: dict[Backend, DriverProvider] = field(default_factory=dict)

    def register(self, backend: Backend, provider: DriverProvider) -> None:
        self.providers[backend] = provider

    def options(self, backend: Backend, *, include_unavailable: bool = False) -> list[DriverOption]:
        provider = self.providers.get(backend)
        if provider is None:
            return []
        options = list(provider())
        if include_unavailable:
            return options
        return [option for option in options if option.available]

    def is_available(self, backend: Backend, driver: Driver) -> bool:
        return any(option.driver is driver for option in self.options(backend))

    def unavailable_reason(self, backend: Backend, driver: Driver) -> str:
        for option in self.options(backend, include_unavailable=True):
            if option.driver is driver:
                return option.unavailable_reason
        return "This driver is not registered for the active backend."

    def default_driver(self, backend: Backend) -> Driver:
        options = self.options(backend)
        if not options:
            return Driver.DIRECT
        return options[0].driver

    def coerce(self, backend: Backend, driver: Driver) -> Driver:
        if self.is_available(backend, driver):
            return driver
        return self.default_driver(backend)


def default_driver_registry() -> DriverRegistry:
    registry = DriverRegistry()
    registry.register(
        Backend.CLAUDE,
        lambda: [
            DriverOption(Driver.DIRECT, "Run through Claude's prompt/stream API"),
            DriverOption(Driver.TMUX, "Drive the local Claude Code TUI through tmux"),
            DriverOption(Driver.DOCKER, "Run Claude Code inside a managed Docker sandbox"),
        ],
    )
    registry.register(
        Backend.CODEX,
        lambda: [
            DriverOption(Driver.DIRECT, "Run through Codex exec/app-server APIs"),
            DriverOption(Driver.TMUX, "Drive the local Codex TUI through tmux"),
            DriverOption(Driver.DOCKER, "Run Codex inside a managed Docker sandbox"),
            DriverOption(
                Driver.REMOTE_CONTROL,
                "Codex app-server websocket",
                available=False,
                unavailable_reason="Remote-control is not exposed as an interactive chat mode in yikes.",
            ),
        ],
    )
    return registry
