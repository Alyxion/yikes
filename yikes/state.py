from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any

from .domain import AgentSettings, Backend, ChatOptions, Complexity, Driver, McpServer


@dataclass(frozen=True)
class AppState:
    backend: Backend = Backend.CLAUDE
    driver: Driver = Driver.DIRECT
    model: str | None = None
    complexity: Complexity = Complexity.MEDIUM
    settings: AgentSettings = field(default_factory=AgentSettings)

    @classmethod
    def from_options(cls, options: ChatOptions) -> "AppState":
        return cls(
            backend=options.backend,
            driver=options.driver,
            model=options.model,
            complexity=options.complexity,
            settings=options.settings,
        )

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "AppState":
        return cls(
            backend=_parse_enum(Backend, payload.get("backend"), Backend.CLAUDE),
            driver=_parse_enum(Driver, payload.get("driver"), Driver.DIRECT),
            model=_parse_model(payload.get("model")),
            complexity=_parse_enum(Complexity, payload.get("complexity"), Complexity.MEDIUM),
            settings=_parse_settings(payload.get("settings")),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "backend": self.backend.value,
            "driver": self.driver.value,
            "model": self.model,
            "complexity": self.complexity.value,
            "settings": _settings_to_json(self.settings),
        }


def default_state_path() -> Path:
    override = os.environ.get("YIKES_STATE_PATH")
    if override:
        return Path(override).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "yikes" / "state.json"


def load_app_state(path: Path | None = None) -> AppState:
    state_path = path or default_state_path()
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return AppState()
    if not isinstance(payload, dict):
        return AppState()
    return AppState.from_json(payload)


def save_app_state(state: AppState, path: Path | None = None) -> None:
    state_path = path or default_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_suffix(f"{state_path.suffix}.tmp")
    temp_path.write_text(json.dumps(state.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(state_path)


def _parse_enum[T: str](enum_type: type[T], value: object, fallback: T) -> T:
    if not isinstance(value, str):
        return fallback
    try:
        return enum_type(value)
    except ValueError:
        return fallback


def _parse_model(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_settings(value: object) -> AgentSettings:
    if not isinstance(value, dict):
        return AgentSettings()
    return AgentSettings(
        web_search_enabled=_parse_bool(value.get("web_search_enabled"), True),
        read_roots=_parse_paths(value.get("read_roots")),
        write_roots=_parse_paths(value.get("write_roots")),
        mcp_servers=_parse_mcps(value.get("mcp_servers")),
    )


def _settings_to_json(settings: AgentSettings) -> dict[str, object]:
    return {
        "web_search_enabled": settings.web_search_enabled,
        "read_roots": [str(path) for path in settings.read_roots],
        "write_roots": [str(path) for path in settings.write_roots],
        "mcp_servers": [
            {
                "name": server.name,
                "command": server.command,
                "args": list(server.args),
                "enabled": server.enabled,
            }
            for server in settings.mcp_servers
        ],
    }


def _parse_bool(value: object, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    return fallback


def _parse_paths(value: object) -> tuple[Path, ...]:
    if not isinstance(value, list):
        return ()
    paths: list[Path] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            paths.append(Path(item).expanduser())
    return tuple(paths)


def _parse_mcps(value: object) -> tuple[McpServer, ...]:
    if not isinstance(value, list):
        return ()
    servers: list[McpServer] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        command = item.get("command")
        args = item.get("args")
        if not isinstance(name, str) or not name.strip() or not isinstance(command, str) or not command.strip():
            continue
        parsed_args = tuple(arg for arg in args if isinstance(arg, str)) if isinstance(args, list) else ()
        servers.append(
            McpServer(
                name=name.strip(),
                command=command.strip(),
                args=parsed_args,
                enabled=_parse_bool(item.get("enabled"), True),
            )
        )
    return tuple(servers)
