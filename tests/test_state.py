from __future__ import annotations

import json

from yikes import AgentSettings, Backend, Complexity, Driver, McpServer
from yikes.state import AppState, load_app_state, save_app_state


def test_app_state_round_trips_last_terminal_choices(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    save_app_state(
        AppState(
            backend=Backend.CODEX,
            driver=Driver.REMOTE_CONTROL,
            model="gpt-5.5",
            complexity=Complexity.HIGH,
            settings=AgentSettings(
                web_search_enabled=False,
                managed_output_enabled=False,
                read_roots=(tmp_path / "read",),
                write_roots=(tmp_path / "write",),
                mcp_servers=(McpServer("fs", "python", ("-m", "server")),),
            ),
        ),
        state_path,
    )

    restored = load_app_state(state_path)

    assert restored.backend is Backend.CODEX
    assert restored.driver is Driver.REMOTE_CONTROL
    assert restored.model == "gpt-5.5"
    assert restored.complexity is Complexity.HIGH
    assert restored.settings.web_search_enabled is False
    assert restored.settings.managed_output_enabled is False
    assert restored.settings.read_roots == (tmp_path / "read",)
    assert restored.settings.write_roots == (tmp_path / "write",)
    assert restored.settings.mcp_servers == (McpServer("fs", "python", ("-m", "server")),)


def test_app_state_ignores_invalid_values(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "backend": "missing",
                "driver": "invalid",
                "model": "",
                "complexity": "too-much",
                "settings": {
                    "web_search_enabled": "nope",
                    "read_roots": [""],
                    "write_roots": {},
                    "mcp_servers": [{"name": "", "command": ""}],
                },
            }
        ),
        encoding="utf-8",
    )

    restored = load_app_state(state_path)

    assert restored == AppState()
