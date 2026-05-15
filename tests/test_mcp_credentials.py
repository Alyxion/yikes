from __future__ import annotations

from pathlib import Path

import pytest

from yikes import (
    CallbackCredentialProvider,
    CodexCredentialProvider,
    CredentialBroker,
    CredentialUnavailable,
    CredentialValue,
    EnvCredentialProvider,
    StaticCredentialProvider,
    CredentialGrant,
    McpConfig,
    McpServerConfig,
    McpSseProxy,
    ToolFilter,
    build_claude_mcp_json,
    compute_disallowed_tools,
    filter_tools_list,
    parse_inline_mcp,
    resolve_servers,
)
from yikes.credentials import write_api_key_helper_settings


def test_mcp_config_routing_and_claude_json() -> None:
    direct = McpServerConfig(
        command="python",
        args=("-m", "fs"),
        tool_filter=ToolFilter(deny=("delete_file",)),
    )
    proxied = McpServerConfig(
        command="python",
        args=("-m", "db"),
        tool_filter=ToolFilter(allow=("query",)),
    )
    config = McpConfig({"fs": direct, "db": proxied})

    direct_servers, proxied_servers = resolve_servers(config, container_mode=False)

    assert direct_servers == {"fs": direct}
    assert proxied_servers == {"db": proxied}
    assert compute_disallowed_tools(direct_servers) == ["mcp__fs__delete_file"]
    assert build_claude_mcp_json(direct_servers, {"db": "http://localhost:9999/sse"}) == {
        "mcpServers": {
            "fs": {"command": "python", "args": ["-m", "fs"]},
            "db": {"type": "sse", "url": "http://localhost:9999/sse"},
        }
    }


def test_inline_mcp_parser_uses_shell_quoting() -> None:
    name, config = parse_inline_mcp('fs=python -m "my server" --root "/tmp/a b"')

    assert name == "fs"
    assert config.command == "python"
    assert config.args == ("-m", "my server", "--root", "/tmp/a b")


def test_tool_filtering_and_proxy_request_checks() -> None:
    tool_filter = ToolFilter(allow=("read", "write"), deny=("write",))
    tools = [{"name": "read"}, {"name": "write"}, {"name": "delete"}]
    proxy = McpSseProxy("fs", McpServerConfig("python", tool_filter=tool_filter))

    assert filter_tools_list(tools, tool_filter) == [{"name": "read"}]
    assert proxy._filter_response({"id": 1, "result": {"tools": tools}}) == {
        "id": 1,
        "result": {"tools": [{"name": "read"}]},
    }
    assert proxy._check_request({"id": 2, "method": "tools/call", "params": {"name": "write"}}) == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {"code": -32601, "message": "Tool 'write' is not allowed"},
    }
    assert proxy._check_request({"id": 3, "method": "tools/call", "params": {"name": "read"}}) is None


def test_credential_broker_resolves_explicit_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-secret")
    broker = CredentialBroker(
        [
            EnvCredentialProvider({"anthropic": "ANTHROPIC_API_KEY"}),
            StaticCredentialProvider({"github": "static-secret"}),
            CallbackCredentialProvider("callback", lambda name: "dynamic" if name == "dyn" else None),
        ]
    )

    env = broker.build_secret_env(
        (
            CredentialGrant("anthropic", "env"),
            CredentialGrant("github", "static"),
            CredentialGrant("dyn", "callback"),
        ),
        env_names={"anthropic": "ANTHROPIC_API_KEY"},
    )

    assert env == {
        "ANTHROPIC_API_KEY": "env-secret",
        "GITHUB": "static-secret",
        "DYN": "dynamic",
    }
    value = broker.resolve(CredentialGrant("github", "static"))
    assert isinstance(value, CredentialValue)
    assert "static-secret" not in repr(value)


def test_codex_credential_provider_reads_auth_file_without_repr_leak(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text('{"tokens":{"access_token":"secret-token"}}')

    value = CodexCredentialProvider(auth_path).get("codex")

    assert value is not None
    assert value.name == "codex_auth_json"
    assert "secret-token" in value.value
    assert "secret-token" not in repr(value)


def test_credential_broker_errors_on_missing_grant() -> None:
    broker = CredentialBroker([StaticCredentialProvider({})])

    with pytest.raises(CredentialUnavailable):
        broker.resolve(CredentialGrant("missing", "static"))


def test_api_key_helper_writer_is_explicit(tmp_path: Path) -> None:
    key_path, settings_path = write_api_key_helper_settings(
        tmp_path,
        CredentialValue("anthropic", "secret-value", "test"),
    )

    assert key_path.read_text() == "secret-value"
    assert "apiKeyHelper" in settings_path.read_text()
