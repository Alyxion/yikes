from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from .domain import AgentSettings, Backend, Complexity, Driver, McpServer


class RuntimeKind(StrEnum):
    DIRECT = "direct"
    TMUX = "tmux"
    DOCKER = "docker"
    REMOTE_SERVER = "remote-server"


class SessionState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    STOPPED = "stopped"
    DEAD = "dead"


DEFAULT_YIKES_DIR = Path.home() / ".yikes"
DEFAULT_RUNTIME_STORE = DEFAULT_YIKES_DIR / "sessions"


@dataclass(frozen=True)
class RuntimeRef:
    kind: RuntimeKind
    tmux_socket: str | None = None
    tmux_session: str | None = None
    container_name: str | None = None
    volume_name: str | None = None
    remote_url: str | None = None
    remote_session_id: str | None = None


@dataclass(frozen=True)
class CredentialGrant:
    name: str
    source: str


@dataclass
class DurableSessionMeta:
    id: str
    backend: Backend
    driver: Driver
    runtime: RuntimeRef
    cwd: Path
    state: SessionState = SessionState.CREATED
    model: str | None = None
    complexity: Complexity = Complexity.MEDIUM
    settings: AgentSettings = field(default_factory=AgentSettings)
    native_session_id: str | None = None
    credential_grants: tuple[CredentialGrant, ...] = ()
    created_at: str = field(default_factory=lambda: _now())
    updated_at: str = field(default_factory=lambda: _now())
    user_data: dict[str, str] = field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = _now()


class DurableSessionManager:
    """File-backed registry for yikes! sessions.

    This owns metadata only. Runtime drivers still own the actual process,
    tmux pane, Docker container, or remote server connection.
    """

    def __init__(self, store_dir: Path | None = None) -> None:
        configured_store = store_dir or Path(os.environ.get("YIKES_RUNTIME_STORE", str(DEFAULT_RUNTIME_STORE)))
        self.store_dir = configured_store.expanduser()
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        backend: Backend,
        driver: Driver,
        runtime: RuntimeRef,
        cwd: Path,
        session_id: str | None = None,
        model: str | None = None,
        complexity: Complexity = Complexity.MEDIUM,
        settings: AgentSettings | None = None,
        native_session_id: str | None = None,
        credential_grants: tuple[CredentialGrant, ...] = (),
        user_data: dict[str, str] | None = None,
    ) -> DurableSessionMeta:
        sid = session_id or f"yik_{uuid4().hex[:12]}"
        meta = DurableSessionMeta(
            id=sid,
            backend=backend,
            driver=driver,
            runtime=runtime,
            cwd=cwd.expanduser(),
            model=model,
            complexity=complexity,
            settings=settings or AgentSettings(),
            native_session_id=native_session_id,
            credential_grants=credential_grants,
            user_data=user_data or {},
        )
        self.save(meta)
        return meta

    def get(self, session_id: str) -> DurableSessionMeta | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        try:
            return _meta_from_json(json.loads(path.read_text()))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def list(self) -> list[DurableSessionMeta]:
        sessions: list[DurableSessionMeta] = []
        for path in self.store_dir.glob("*.json"):
            meta = self.get(path.stem)
            if meta is not None:
                sessions.append(meta)
        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        return sessions

    def save(self, meta: DurableSessionMeta, *, touch: bool = True) -> None:
        if touch:
            meta.touch()
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._path(meta.id).write_text(json.dumps(_meta_to_json(meta), indent=2))

    def mark_state(self, session_id: str, state: SessionState) -> DurableSessionMeta | None:
        meta = self.get(session_id)
        if meta is None:
            return None
        meta.state = state
        self.save(meta)
        return meta

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def _path(self, session_id: str) -> Path:
        return self.store_dir / f"{session_id}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _settings_to_json(settings: AgentSettings) -> dict[str, object]:
    return {
        "web_search_enabled": settings.web_search_enabled,
        "tmux_enabled": settings.tmux_enabled,
        "managed_output_enabled": settings.managed_output_enabled,
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


def _settings_from_json(data: object) -> AgentSettings:
    if not isinstance(data, dict):
        return AgentSettings()
    return AgentSettings(
        web_search_enabled=bool(data.get("web_search_enabled", True)),
        tmux_enabled=bool(data.get("tmux_enabled", False)),
        managed_output_enabled=bool(data.get("managed_output_enabled", True)),
        read_roots=tuple(Path(str(path)).expanduser() for path in data.get("read_roots", [])),
        write_roots=tuple(Path(str(path)).expanduser() for path in data.get("write_roots", [])),
        mcp_servers=tuple(
            McpServer(
                str(item.get("name", "")),
                str(item.get("command", "")),
                tuple(str(arg) for arg in item.get("args", [])),
                bool(item.get("enabled", True)),
            )
            for item in data.get("mcp_servers", [])
            if isinstance(item, dict) and item.get("name") and item.get("command")
        ),
    )


def _meta_to_json(meta: DurableSessionMeta) -> dict[str, object]:
    data = asdict(meta)
    data["backend"] = meta.backend.value
    data["driver"] = meta.driver.value
    data["runtime"]["kind"] = meta.runtime.kind.value
    data["cwd"] = str(meta.cwd)
    data["state"] = meta.state.value
    data["complexity"] = meta.complexity.value
    data["settings"] = _settings_to_json(meta.settings)
    return data


def _meta_from_json(data: dict[str, object]) -> DurableSessionMeta:
    runtime_data = data.get("runtime")
    if not isinstance(runtime_data, dict):
        runtime_data = {}
    grants = data.get("credential_grants", [])
    return DurableSessionMeta(
        id=str(data["id"]),
        backend=Backend(str(data["backend"])),
        driver=Driver(str(data["driver"])),
        runtime=RuntimeRef(
            kind=RuntimeKind(str(runtime_data["kind"])),
            tmux_socket=_optional_str(runtime_data.get("tmux_socket")),
            tmux_session=_optional_str(runtime_data.get("tmux_session")),
            container_name=_optional_str(runtime_data.get("container_name")),
            volume_name=_optional_str(runtime_data.get("volume_name")),
            remote_url=_optional_str(runtime_data.get("remote_url")),
            remote_session_id=_optional_str(runtime_data.get("remote_session_id")),
        ),
        cwd=Path(str(data["cwd"])).expanduser(),
        state=SessionState(str(data.get("state", SessionState.CREATED.value))),
        model=_optional_str(data.get("model")),
        complexity=Complexity(str(data.get("complexity", Complexity.MEDIUM.value))),
        settings=_settings_from_json(data.get("settings")),
        native_session_id=_optional_str(data.get("native_session_id")),
        credential_grants=tuple(
            CredentialGrant(str(item.get("name")), str(item.get("source")))
            for item in grants
            if isinstance(item, dict) and item.get("name") and item.get("source")
        ),
        created_at=str(data.get("created_at", _now())),
        updated_at=str(data.get("updated_at", _now())),
        user_data={
            str(key): str(value)
            for key, value in (data.get("user_data") or {}).items()
        } if isinstance(data.get("user_data"), dict) else {},
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
