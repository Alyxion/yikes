from __future__ import annotations

from pathlib import Path
import asyncio
import sys
import time

from fastapi.testclient import TestClient

from yikes.app_core import YikesAppController
from yikes.domain import ChatOptions, ImageAttachment
from yikes.terminal_bridge import WebTerminalManager
from yikes.web import create_app
from yikes.web_auth import WebAuthConfig, developer_mode_from_env
from yikes.web_handler import WebMessageHandler


class EchoTransport:
    def ask(self, options: ChatOptions, prompt: str, attachments: tuple[ImageAttachment, ...] = ()) -> str:
        return "ok"


class SlowTransport:
    def ask(self, options: ChatOptions, prompt: str, attachments: tuple[ImageAttachment, ...] = ()) -> str:
        time.sleep(0.1)
        return "done"


def test_web_auth_persists_in_env_file_by_default(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"

    first = WebAuthConfig.load(developer_mode=False, env_path=env_path)
    second = WebAuthConfig.load(developer_mode=False, env_path=env_path)

    assert env_path.exists()
    assert first.secret == second.secret
    assert first.login_key == second.login_key
    assert first.developer_mode is False


def test_web_auth_can_be_ephemeral() -> None:
    first = WebAuthConfig.load(developer_mode=False, persist_auth=False)
    second = WebAuthConfig.load(developer_mode=False, persist_auth=False)

    assert first.secret != second.secret
    assert first.login_key != second.login_key


def test_dev_mode_uses_same_persisted_key_but_updates_dev_flag(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"

    stable = WebAuthConfig.load(developer_mode=False, env_path=env_path)
    dev = WebAuthConfig.load(developer_mode=True, env_path=env_path)

    assert stable.secret == dev.secret
    assert stable.login_key == dev.login_key
    assert dev.developer_mode is True
    assert "YIKES_WEB_DEV=1" in env_path.read_text(encoding="utf-8")


def test_developer_mode_defaults_to_false(monkeypatch) -> None:
    monkeypatch.delenv("YIKES_WEB_DEV", raising=False)

    assert developer_mode_from_env() is False


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


def test_web_login_throttles_brute_force(tmp_path: Path) -> None:
    auth = WebAuthConfig(secret="test-secret", login_key="right-key", developer_mode=True)
    app = create_app(YikesAppController(cwd=tmp_path, transport=EchoTransport()), auth=auth, use_stage=False)

    with TestClient(app) as client:
        # loading the page without a key is never throttled
        assert client.get("/login").status_code == 200

        first = client.get("/login?key=wrong", follow_redirects=False)
        assert first.status_code == 401
        assert "retry-after" in {k.lower() for k in first.headers}

        # an immediate second guess is locked out
        second = client.get("/login?key=wrong", follow_redirects=False)
        assert second.status_code == 429
        assert "retry-after" in {k.lower() for k in second.headers}


def test_web_term_open_spawns_attach_bridge() -> None:
    class FakeController:
        def attach_command(self, session_id=None):
            return "session-1", [sys.executable, "-c", "import time; print('attached'); time.sleep(0.2)"]

        def resize_terminal(self, session_id, *, cols, rows):
            return {"ok": True, "session_id": session_id, "message": f"{cols}x{rows}"}

        def state(self):
            return {"brand": "yikes!"}

    manager = WebTerminalManager()
    response = asyncio.run(WebMessageHandler(FakeController(), manager).handle({"type": "term.open"}))

    assert response["type"] == "term.opened"
    assert manager.get(response["terminal_id"]) is not None
    manager.close(response["terminal_id"])


def test_web_submit_streams_immediate_working_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    auth = WebAuthConfig(secret="test-secret", login_key="test-key", developer_mode=True)
    controller = YikesAppController(cwd=tmp_path, transport=SlowTransport())
    controller.open_new_session()
    controller.update_new_session(backend="codex", location="host", driver="cli", root=str(tmp_path))
    controller.confirm_new_session()
    app = create_app(controller, auth=auth, use_stage=False)

    with TestClient(app) as client:
        client.cookies.set(auth.cookie_name, auth.issue_cookie())
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "submit", "text": "hello"})
            immediate = ws.receive_json()
            assert immediate["state"]["submission_active"] is True
            assert "You: hello" in immediate["state"]["output_text"]
            assert "Working..." in immediate["state"]["output_text"]
            final = ws.receive_json()
            assert final["state"]["submission_active"] is False
            assert "Assistant: done" in final["state"]["output_text"]


def test_web_new_session_flow_can_create_codex_cli_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    auth = WebAuthConfig(secret="test-secret", login_key="test-key", developer_mode=True)
    app = create_app(YikesAppController(cwd=tmp_path, transport=EchoTransport()), auth=auth, use_stage=False)

    with TestClient(app) as client:
        client.cookies.set(auth.cookie_name, auth.issue_cookie())
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "new.open"})
            opened = ws.receive_json()["state"]
            assert opened["pending_new"] is not None

            ws.send_json(
                {
                    "type": "new.update",
                    "changes": {
                        "backend": "codex",
                        "location": "host",
                        "driver": "cli",
                        "root": "",
                    },
                }
            )
            updated = ws.receive_json()["state"]
            assert updated["pending_new"]["driver"] == "cli"

            ws.send_json({"type": "new.confirm"})
            created = ws.receive_json()["state"]

            assert created["pending_new"] is None
            assert created["status"]["backend"] == "codex"
            assert created["status"]["location"] == "host"
            assert created["status"]["driver"] == "cli"
            assert created["has_active_session"] is True
            assert created["active_session_id"]
            assert any(session["id"] == created["active_session_id"] for session in created["sessions"])
            assert "New session: codex on host via cli" in created["output_text"]


def test_web_new_confirm_uses_inline_form_changes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(tmp_path / "sessions"))
    auth = WebAuthConfig(secret="test-secret", login_key="test-key", developer_mode=True)
    app = create_app(YikesAppController(cwd=tmp_path, transport=EchoTransport()), auth=auth, use_stage=False)

    with TestClient(app) as client:
        client.cookies.set(auth.cookie_name, auth.issue_cookie())
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "new.open"})
            ws.receive_json()
            ws.send_json(
                {
                    "type": "new.confirm",
                    "changes": {
                        "backend": "codex",
                        "location": "host",
                        "driver": "cli",
                        "model": "default",
                        "complexity": "medium",
                        "web_search": "on",
                        "managed_output": "on",
                    },
                }
            )
            created = ws.receive_json()["state"]

            assert created["pending_new"] is None
            assert created["status"]["backend"] == "codex"
            assert created["status"]["driver"] == "cli"
            assert "New session: codex on host via cli" in created["output_text"]
