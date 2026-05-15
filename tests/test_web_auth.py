from __future__ import annotations

from pathlib import Path
import asyncio
import sys

from fastapi.testclient import TestClient

from yikes.app_core import YikesAppController
from yikes.domain import ChatOptions, ImageAttachment
from yikes.terminal_bridge import WebTerminalManager
from yikes.web import _handle_message, create_app
from yikes.web_auth import WebAuthConfig


class EchoTransport:
    def ask(self, options: ChatOptions, prompt: str, attachments: tuple[ImageAttachment, ...] = ()) -> str:
        return "ok"


def test_dev_auth_persists_in_env_file(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"

    first = WebAuthConfig.load(developer_mode=True, env_path=env_path)
    second = WebAuthConfig.load(developer_mode=True, env_path=env_path)

    assert env_path.exists()
    assert first.secret == second.secret
    assert first.login_key == second.login_key


def test_non_dev_auth_rotates_on_start() -> None:
    first = WebAuthConfig.load(developer_mode=False)
    second = WebAuthConfig.load(developer_mode=False)

    assert first.secret != second.secret
    assert first.login_key != second.login_key


def test_web_requires_cookie_and_login_key_sets_cookie(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    auth = WebAuthConfig(secret="test-secret", login_key="test-key", developer_mode=True)
    app = create_app(
        YikesAppController(cwd=tmp_path, transport=EchoTransport()),
        auth=auth,
        use_stage=False,
    )

    with TestClient(app) as client:
        unauthenticated = client.get("/api/state")
        assert unauthenticated.status_code == 401

        login = client.get("/login?key=test-key&next=/", follow_redirects=False)
        assert login.status_code == 303
        assert auth.cookie_name in login.cookies

        client.cookies.set(auth.cookie_name, login.cookies[auth.cookie_name])
        state = client.get("/api/state")
        assert state.status_code == 200
        assert state.json()["brand"] == "yikes!"

        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["state"]["brand"] == "yikes!"


def test_web_term_open_spawns_attach_bridge() -> None:
    class FakeController:
        def attach_command(self, session_id=None):
            return "session-1", [sys.executable, "-c", "import time; print('attached'); time.sleep(0.2)"]

        def state(self):
            return {"brand": "yikes!"}

    manager = WebTerminalManager()
    response = asyncio.run(_handle_message(FakeController(), manager, {"type": "term.open"}))

    assert response["type"] == "term.opened"
    assert manager.get(response["terminal_id"]) is not None
    manager.close(response["terminal_id"])
