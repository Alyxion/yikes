from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from time import time


class Backend(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


class Driver(StrEnum):
    DIRECT = "direct"
    TMUX = "tmux"
    REMOTE_CONTROL = "remote-control"


class Complexity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


@dataclass(frozen=True)
class McpServer:
    name: str
    command: str
    args: tuple[str, ...] = ()
    enabled: bool = True

    @property
    def display_command(self) -> str:
        return " ".join((self.command, *self.args)).strip()


@dataclass(frozen=True)
class AgentSettings:
    web_search_enabled: bool = True
    read_roots: tuple[Path, ...] = ()
    write_roots: tuple[Path, ...] = ()
    mcp_servers: tuple[McpServer, ...] = ()

    def with_web_search(self, enabled: bool) -> "AgentSettings":
        return replace(self, web_search_enabled=enabled)

    def add_read_root(self, path: Path) -> "AgentSettings":
        return replace(self, read_roots=_append_unique_path(self.read_roots, path))

    def remove_read_root(self, path: Path) -> "AgentSettings":
        return replace(self, read_roots=_remove_path(self.read_roots, path))

    def add_write_root(self, path: Path) -> "AgentSettings":
        return replace(self, write_roots=_append_unique_path(self.write_roots, path))

    def remove_write_root(self, path: Path) -> "AgentSettings":
        return replace(self, write_roots=_remove_path(self.write_roots, path))

    def upsert_mcp(self, server: McpServer) -> "AgentSettings":
        servers = tuple(existing for existing in self.mcp_servers if existing.name != server.name)
        return replace(self, mcp_servers=(*servers, server))

    def remove_mcp(self, name: str) -> "AgentSettings":
        return replace(self, mcp_servers=tuple(server for server in self.mcp_servers if server.name != name))

    def set_mcp_enabled(self, name: str, enabled: bool) -> "AgentSettings":
        servers = tuple(
            replace(server, enabled=enabled) if server.name == name else server for server in self.mcp_servers
        )
        return replace(self, mcp_servers=servers)


@dataclass(frozen=True)
class Message:
    role: MessageRole
    text: str
    ts: float = field(default_factory=time)


@dataclass(frozen=True)
class ChatOptions:
    backend: Backend
    driver: Driver
    cwd: Path
    timeout: float = 180.0
    model: str | None = None
    complexity: Complexity = Complexity.MEDIUM
    settings: AgentSettings = field(default_factory=AgentSettings)

    def with_model(self, model: str | None) -> "ChatOptions":
        return replace(self, model=model)

    def with_backend(self, backend: Backend) -> "ChatOptions":
        return replace(self, backend=backend, model=None)

    def with_driver(self, driver: Driver) -> "ChatOptions":
        return replace(self, driver=driver)

    def with_complexity(self, complexity: Complexity) -> "ChatOptions":
        return replace(self, complexity=complexity)

    def with_settings(self, settings: AgentSettings) -> "ChatOptions":
        return replace(self, settings=settings)


def _normalize_path(path: Path) -> Path:
    return path.expanduser()


def _append_unique_path(paths: tuple[Path, ...], path: Path) -> tuple[Path, ...]:
    normalized = _normalize_path(path)
    if normalized in paths:
        return paths
    return (*paths, normalized)


def _remove_path(paths: tuple[Path, ...], path: Path) -> tuple[Path, ...]:
    normalized = _normalize_path(path)
    return tuple(existing for existing in paths if existing != normalized)


@dataclass(frozen=True)
class ChatResult:
    backend: Backend
    driver: Driver
    turns: list[str]

    @property
    def greeting(self) -> str:
        return self.turns[0]

    @property
    def calculation(self) -> str:
        return self.turns[1]

    @property
    def remembered_name(self) -> str:
        return self.turns[2]
