from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


McpScope = Literal["inside", "outside", "auto"]


@dataclass(frozen=True)
class ToolFilter:
    allow: tuple[str, ...] | None = None
    deny: tuple[str, ...] | None = None


@dataclass(frozen=True)
class McpServerConfig:
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    scope: McpScope = "auto"
    tool_filter: ToolFilter | None = None


@dataclass(frozen=True)
class McpConfig:
    servers: dict[str, McpServerConfig] = field(default_factory=dict)


def load_mcp_config(path: str | Path) -> McpConfig:
    return mcp_config_from_json(json.loads(Path(path).read_text()))


def mcp_config_from_json(data: dict[str, Any]) -> McpConfig:
    raw_servers = data.get("mcpServers", data.get("servers", {}))
    if not isinstance(raw_servers, dict):
        return McpConfig()
    servers: dict[str, McpServerConfig] = {}
    for name, raw in raw_servers.items():
        if isinstance(raw, dict):
            servers[str(name)] = mcp_server_from_json(raw)
    return McpConfig(servers)


def mcp_server_from_json(data: dict[str, Any]) -> McpServerConfig:
    raw_filter = data.get("toolFilter", data.get("tool_filter"))
    return McpServerConfig(
        command=str(data["command"]),
        args=tuple(str(arg) for arg in data.get("args", ())),
        env={str(key): str(value) for key, value in data.get("env", {}).items()}
        if isinstance(data.get("env"), dict)
        else {},
        scope=_scope(data.get("scope", "auto")),
        tool_filter=tool_filter_from_json(raw_filter) if isinstance(raw_filter, dict) else None,
    )


def tool_filter_from_json(data: dict[str, Any]) -> ToolFilter:
    allow = data.get("allow")
    deny = data.get("deny")
    return ToolFilter(
        allow=tuple(str(item) for item in allow) if isinstance(allow, list) else None,
        deny=tuple(str(item) for item in deny) if isinstance(deny, list) else None,
    )


def parse_inline_mcp(spec: str) -> tuple[str, McpServerConfig]:
    if "=" not in spec:
        raise ValueError(f"Invalid MCP spec {spec!r}. Expected: name=command [args...]")
    name, rest = spec.split("=", 1)
    parts = shlex.split(rest)
    if not name.strip():
        raise ValueError("MCP spec requires a name")
    if not parts:
        raise ValueError(f"MCP spec {spec!r} has no command")
    return name.strip(), McpServerConfig(command=parts[0], args=tuple(parts[1:]))


def needs_proxy(server: McpServerConfig, *, container_mode: bool) -> bool:
    if container_mode and server.scope in ("outside", "auto"):
        return True
    if server.tool_filter and server.tool_filter.allow is not None:
        return True
    return False


def resolve_servers(
    config: McpConfig,
    *,
    container_mode: bool,
) -> tuple[dict[str, McpServerConfig], dict[str, McpServerConfig]]:
    direct: dict[str, McpServerConfig] = {}
    proxied: dict[str, McpServerConfig] = {}
    for name, server in config.servers.items():
        if needs_proxy(server, container_mode=container_mode):
            proxied[name] = server
        else:
            direct[name] = server
    return direct, proxied


def build_claude_mcp_json(
    direct_servers: dict[str, McpServerConfig],
    proxy_urls: dict[str, str] | None = None,
) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    for name, config in direct_servers.items():
        entry: dict[str, Any] = {"command": config.command, "args": list(config.args)}
        if config.env:
            entry["env"] = config.env
        servers[name] = entry
    for name, url in (proxy_urls or {}).items():
        servers[name] = {"type": "sse", "url": url}
    return {"mcpServers": servers}


def compute_disallowed_tools(direct_servers: dict[str, McpServerConfig]) -> list[str]:
    patterns: list[str] = []
    for name, server in direct_servers.items():
        if server.tool_filter and server.tool_filter.deny:
            patterns.extend(f"mcp__{name}__{tool}" for tool in server.tool_filter.deny)
    return patterns


def filter_tools_list(tools: list[dict[str, Any]], tool_filter: ToolFilter) -> list[dict[str, Any]]:
    filtered = tools
    if tool_filter.allow is not None:
        filtered = [tool for tool in filtered if tool.get("name") in tool_filter.allow]
    if tool_filter.deny is not None:
        filtered = [tool for tool in filtered if tool.get("name") not in tool_filter.deny]
    return filtered


def is_tool_allowed(tool_name: str, tool_filter: ToolFilter) -> bool:
    if tool_filter.allow is not None and tool_name not in tool_filter.allow:
        return False
    if tool_filter.deny is not None and tool_name in tool_filter.deny:
        return False
    return True


def _scope(value: object) -> McpScope:
    text = str(value)
    if text in ("inside", "outside", "auto"):
        return text  # type: ignore[return-value]
    return "auto"
