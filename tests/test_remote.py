from __future__ import annotations

from pathlib import Path

from yikes import Backend, Driver, EventLog, RemoteCommandHandler, TokenStore
from yikes.domain import ChatOptions


class FakeTransport:
    def ask(self, options: ChatOptions, prompt: str) -> str:
        if "What is 4+4?" in prompt:
            return "8"
        return "ok"


def _handler(tmp_path: Path, *, require_token: bool = True) -> tuple[RemoteCommandHandler, str]:
    tokens = TokenStore(tmp_path / "tokens.json")
    token = tokens.create_temporary("test", duration_seconds=60)
    handler = RemoteCommandHandler(
        token_store=tokens,
        event_log=EventLog(tmp_path / "events"),
        require_token=require_token,
        transport=FakeTransport(),
    )
    return handler, token


def test_event_log_replays_after_sequence(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    first = log.append("s1", "session.created", {"a": 1})
    second = log.append("s1", "assistant.message", {"text": "ok"})

    assert first.seq == 1
    assert second.seq == 2
    assert [event.type for event in log.list("s1", after_seq=1)] == ["assistant.message"]


def test_remote_handler_requires_token(tmp_path: Path) -> None:
    handler, _token = _handler(tmp_path)

    response = handler.handle({"command": "session.list", "params": {}})

    assert response["ok"] is False
    assert response["error"] == "unauthorized"


def test_remote_handler_create_prompt_events_and_close(tmp_path: Path) -> None:
    handler, token = _handler(tmp_path)
    created = handler.handle(
        {
            "token": token,
            "command": "session.create",
            "params": {"backend": "claude", "driver": "direct", "cwd": str(tmp_path)},
        }
    )

    assert created["ok"] is True
    session_id = created["session"]["session_id"]
    assert handler.handle({"token": token, "command": "session.list", "params": {}})["sessions"][0]["session_id"] == session_id

    prompt = handler.handle(
        {
            "token": token,
            "command": "session.prompt",
            "params": {"session_id": session_id, "text": "What is 4+4?"},
        }
    )

    assert prompt["ok"] is True
    assert prompt["answer"] == "8"
    events = handler.handle(
        {"token": token, "command": "events.list", "params": {"session_id": session_id}}
    )
    assert [event["type"] for event in events["events"]] == [
        "session.created",
        "user.message",
        "assistant.message",
    ]

    closed = handler.handle(
        {"token": token, "command": "session.close", "params": {"session_id": session_id}}
    )
    assert closed["ok"] is True
    assert closed["removed"] is True


def test_remote_handler_slash_suggestions(tmp_path: Path) -> None:
    handler, token = _handler(tmp_path)
    created = handler.handle(
        {
            "token": token,
            "command": "session.create",
            "params": {"backend": Backend.CLAUDE.value, "driver": Driver.DIRECT.value},
        }
    )
    session_id = created["session"]["session_id"]

    response = handler.handle(
        {
            "token": token,
            "command": "session.suggestions",
            "params": {"session_id": session_id, "raw": "/mo"},
        }
    )

    completions = {suggestion["completion"] for suggestion in response["suggestions"]}
    assert {"/model ", "/models"} <= completions


def test_remote_handler_accepts_runtime_settings(tmp_path: Path) -> None:
    handler, token = _handler(tmp_path)

    created = handler.handle(
        {
            "token": token,
            "command": "session.create",
            "params": {
                "backend": "claude",
                "driver": "direct",
                "settings": {
                    "web_search_enabled": False,
                    "read_roots": [str(tmp_path / "site")],
                    "write_roots": [str(tmp_path / "site" / "dist")],
                    "mcp_servers": [
                        {"name": "fs", "command": "python", "args": ["-m", "server"]},
                        'db=python -m "db server"',
                    ],
                },
            },
        }
    )

    assert created["ok"] is True
    assert created["session"]["web"] == "disabled"
    assert created["session"]["read_roots"] == "1"
    assert created["session"]["write_roots"] == "1"
    assert created["session"]["mcps"] == "2"
