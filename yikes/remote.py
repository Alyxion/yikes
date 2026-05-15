from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import AgentSettings, Backend, Complexity, Driver, McpServer
from .events import EventLog
from .mcp import parse_inline_mcp
from .services import ChatService, ChatTransport, Session
from .tokens import TokenStore


class RemoteProtocolError(ValueError):
    pass


@dataclass
class RemoteServerConfig:
    host: str = "127.0.0.1"
    port: int = 8989
    require_token: bool = True

    @property
    def websocket_url(self) -> str:
        return f"ws://{self.host}:{self.port}"


@dataclass(frozen=True)
class RemoteClientConfig:
    url: str = "ws://127.0.0.1:8989"
    token: str | None = None


class RemoteSessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def add(self, session: Session) -> None:
        self._sessions[session.id] = session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list(self) -> list[Session]:
        return list(self._sessions.values())

    def remove(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None


class RemoteCommandHandler:
    """JSON command handler for remote yikes! clients."""

    def __init__(
        self,
        *,
        chat_service: ChatService | None = None,
        token_store: TokenStore | None = None,
        event_log: EventLog | None = None,
        registry: RemoteSessionRegistry | None = None,
        require_token: bool = True,
        transport: ChatTransport | None = None,
    ) -> None:
        self.chat_service = chat_service or ChatService()
        self.token_store = token_store or TokenStore()
        self.event_log = event_log or EventLog()
        self.registry = registry or RemoteSessionRegistry()
        self.require_token = require_token
        self.transport = transport

    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        try:
            self._authorize(message)
            command = str(message.get("command", ""))
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise RemoteProtocolError("params must be an object")
            if command == "session.create":
                return self._create_session(params)
            if command == "session.list":
                return self._list_sessions()
            if command == "session.status":
                return self._session_status(params)
            if command == "session.prompt":
                return self._prompt(params)
            if command == "session.command":
                return self._slash_command(params)
            if command == "session.suggestions":
                return self._suggestions(params)
            if command == "events.list":
                return self._events(params)
            if command == "session.close":
                return self._close(params)
            raise RemoteProtocolError(f"unknown command: {command}")
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _authorize(self, message: dict[str, Any]) -> None:
        if not self.require_token:
            return
        token = message.get("token")
        if not isinstance(token, str) or not self.token_store.verify(token):
            raise RemoteProtocolError("unauthorized")

    def _create_session(self, params: dict[str, Any]) -> dict[str, Any]:
        backend = Backend(str(params.get("backend", Backend.CLAUDE.value)))
        driver = Driver(str(params.get("driver", Driver.DIRECT.value)))
        cwd = Path(str(params.get("cwd", Path.cwd()))).expanduser()
        session = self.chat_service.create_session(
            backend,
            driver,
            cwd=cwd,
            timeout=float(params.get("timeout", 180.0)),
            model=_optional_str(params.get("model")),
            complexity=Complexity(str(params.get("complexity", Complexity.MEDIUM.value))),
            settings=_settings_from_params(params.get("settings")),
            transport=self.transport,
        )
        self.registry.add(session)
        event = self.event_log.append(session.id, "session.created", session.status())
        return {"ok": True, "session": session.status(), "event": event.to_json()}

    def _list_sessions(self) -> dict[str, Any]:
        return {"ok": True, "sessions": [session.status() for session in self.registry.list()]}

    def _session_status(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        return {"ok": True, "session": session.status()}

    def _prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        text = str(params.get("text", ""))
        self.event_log.append(session.id, "user.message", {"text": text})
        answer = session.prompt(text)
        event = self.event_log.append(session.id, "assistant.message", {"text": answer})
        return {"ok": True, "answer": answer, "event": event.to_json(), "session": session.status()}

    def _slash_command(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        raw = str(params.get("raw", ""))
        result = session.run_slash_command(raw)
        event = self.event_log.append(
            session.id,
            "slash.command",
            {
                "raw": raw,
                "message": result.message,
                "exit_requested": result.exit_requested,
                "restart_requested": result.restart_requested,
            },
        )
        return {"ok": True, "result": result.message, "event": event.to_json()}

    def _suggestions(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        raw = str(params.get("raw", ""))
        return {
            "ok": True,
            "suggestions": [
                {
                    "value": suggestion.value,
                    "description": suggestion.description,
                    "completion": suggestion.completion,
                }
                for suggestion in session.slash_suggestions(raw)
            ],
        }

    def _events(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("session_id", ""))
        after_seq = params.get("after_seq")
        return {
            "ok": True,
            "events": [
                event.to_json()
                for event in self.event_log.list(
                    session_id,
                    after_seq=int(after_seq) if after_seq is not None else None,
                )
            ],
        }

    def _close(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        removed = self.registry.remove(session.id)
        event = self.event_log.append(session.id, "session.closed", {"removed": removed})
        return {"ok": True, "removed": removed, "event": event.to_json()}

    def _require_session(self, params: dict[str, Any]) -> Session:
        session_id = str(params.get("session_id", ""))
        session = self.registry.get(session_id)
        if session is None:
            raise RemoteProtocolError(f"session not found: {session_id}")
        return session


class YikesRemoteServer:
    """Small WebSocket wrapper around RemoteCommandHandler."""

    def __init__(self, handler: RemoteCommandHandler, config: RemoteServerConfig | None = None) -> None:
        self.handler = handler
        self.config = config or RemoteServerConfig()
        self._server: Any = None

    async def start(self) -> None:
        import websockets

        self._server = await websockets.serve(self._handle_ws, self.config.host, self.config.port)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def serve_forever(self) -> None:
        await self.start()
        await asyncio.Future()

    async def _handle_ws(self, websocket: Any) -> None:
        async for raw in websocket:
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise RemoteProtocolError("message must be an object")
                response = self.handler.handle(message)
            except Exception as exc:
                response = {"ok": False, "error": str(exc)}
            await websocket.send(json.dumps(response))


class RemoteClient:
    """Small async client for Python callers that attach to a yikes! server."""

    def __init__(self, config: RemoteClientConfig | None = None) -> None:
        self.config = config or RemoteClientConfig()

    async def request(self, command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        import websockets

        payload: dict[str, Any] = {
            "command": command,
            "params": params or {},
        }
        if self.config.token is not None:
            payload["token"] = self.config.token
        async with websockets.connect(self.config.url) as websocket:
            await websocket.send(json.dumps(payload))
            raw = await websocket.recv()
        response = json.loads(raw)
        if not isinstance(response, dict):
            raise RemoteProtocolError("server response must be an object")
        return response

    async def create_session(
        self,
        *,
        backend: Backend | str = Backend.CLAUDE,
        driver: Driver | str = Driver.DIRECT,
        cwd: Path | str | None = None,
        model: str | None = None,
        complexity: Complexity | str = Complexity.MEDIUM,
        settings: AgentSettings | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            "session.create",
            {
                "backend": Backend(backend).value,
                "driver": Driver(driver).value,
                "cwd": str(cwd or Path.cwd()),
                "model": model,
                "complexity": Complexity(complexity).value,
                "settings": _settings_to_params(settings) if settings is not None else None,
            },
        )

    async def prompt(self, session_id: str, text: str) -> dict[str, Any]:
        return await self.request("session.prompt", {"session_id": session_id, "text": text})

    async def command(self, session_id: str, raw: str) -> dict[str, Any]:
        return await self.request("session.command", {"session_id": session_id, "raw": raw})

    async def events(self, session_id: str, *, after_seq: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"session_id": session_id}
        if after_seq is not None:
            params["after_seq"] = after_seq
        return await self.request("events.list", params)

    async def close(self, session_id: str) -> dict[str, Any]:
        return await self.request("session.close", {"session_id": session_id})


def _settings_from_params(value: object) -> AgentSettings | None:
    if not isinstance(value, dict):
        return None
    return AgentSettings(
        web_search_enabled=bool(value.get("web_search_enabled", True)),
        tmux_enabled=bool(value.get("tmux_enabled", False)),
        read_roots=tuple(Path(str(path)).expanduser() for path in value.get("read_roots", [])),
        write_roots=tuple(Path(str(path)).expanduser() for path in value.get("write_roots", [])),
        mcp_servers=_mcp_servers_from_params(value.get("mcp_servers", value.get("mcps", []))),
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _mcp_servers_from_params(value: object) -> tuple[McpServer, ...]:
    if not isinstance(value, list):
        return ()
    servers: list[McpServer] = []
    for item in value:
        if isinstance(item, str):
            name, config = parse_inline_mcp(item)
            servers.append(
                McpServer(
                    name,
                    config.command,
                    config.args,
                    enabled=True,
                )
            )
            continue
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        command = str(item.get("command", "")).strip()
        if not name or not command:
            continue
        args_value = item.get("args", [])
        args = tuple(str(arg) for arg in args_value) if isinstance(args_value, list) else ()
        servers.append(McpServer(name, command, args, enabled=bool(item.get("enabled", True))))
    return tuple(servers)


def _settings_to_params(settings: AgentSettings) -> dict[str, Any]:
    return {
        "web_search_enabled": settings.web_search_enabled,
        "tmux_enabled": settings.tmux_enabled,
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
