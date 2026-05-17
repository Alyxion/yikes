from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

from yikes import AgentSettings, Backend, Complexity, Driver, DurableSessionManager, RuntimeKind, RuntimeRef
from yikes import cli
import yikes.session_inventory
import yikes.tui
import yikes.remote


def test_no_args_launches_tui_by_default(monkeypatch) -> None:
    called: dict[str, object] = {}

    def fake_run_tui(
        *,
        backend: Backend | None,
        driver: Driver | None,
        cwd: Path,
        timeout: float,
        model: str | None,
        complexity: Complexity | None,
        settings: AgentSettings | None,
    ) -> None:
        called.update(
            backend=backend,
            driver=driver,
            cwd=cwd,
            timeout=timeout,
            model=model,
            complexity=complexity,
            settings=settings,
        )

    monkeypatch.setattr(yikes.tui, "run_tui", fake_run_tui)

    assert cli.main([]) == 0
    assert called["backend"] is None
    assert called["driver"] is None
    assert called["complexity"] is None
    assert called["settings"] is None


def test_tui_rejects_remote_control_chat_mode(monkeypatch) -> None:
    def fake_run_tui(**_kwargs: object) -> None:
        raise AssertionError("run_tui should not be called")

    monkeypatch.setattr(yikes.tui, "run_tui", fake_run_tui)

    assert cli.main(["tui", "--driver", "remote-control"]) == 1


def test_token_command_creates_hashed_token(tmp_path, capsys) -> None:
    store = tmp_path / "tokens.json"

    assert cli.main(["token", "--name", "browser", "--ttl", "60", "--store", str(store)]) == 0

    token = capsys.readouterr().out.strip()
    raw_store = store.read_text()
    assert token
    assert token not in raw_store
    assert "browser" in raw_store


def test_prompt_profile_ensure_creates_shared_profile(tmp_path, capsys) -> None:
    profile = tmp_path / "prompt-profile.json"

    assert cli.main(["prompt-profile", "ensure", "--path", str(profile), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == str(profile)
    assert payload["shared_for"] == ["codex", "claude"]
    assert profile.exists()


def test_prompt_profile_generate_extends_same_profile_for_any_backend(monkeypatch, tmp_path, capsys) -> None:
    profile = tmp_path / "prompt-profile.json"

    def fake_ask_backend(*_args: object, **_kwargs: object) -> str:
        return json.dumps(
            {
                "setup_variants": [
                    "Keep the terminal exchange focused and answer exact one-word requests with one word.",
                    "Continue from this session, answer directly, and respect exact output constraints.",
                    "Use the current context naturally; keep strict short-answer requests strict.",
                ],
                "boundary_templates": [
                    "Place the answer between these lines:\nStart line: $start\nEnd line: $end",
                    "Keep only the reply body within these bounds:\nBegin line: $start\nFinish line: $end",
                    "Use these response edges exactly:\nResponse start: $start\nResponse end: $end",
                ],
                "marker_pairs": [["LOCAL_BEGIN_{nonce}", "LOCAL_END_{nonce}"]],
            }
        )

    monkeypatch.setattr(cli, "ask_backend", fake_ask_backend)

    assert (
        cli.main(
            [
                "prompt-profile",
                "generate",
                "--backend",
                "claude",
                "--path",
                str(profile),
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["backend"] == "claude"
    assert payload["shared_for"] == ["codex", "claude"]
    assert "LOCAL_BEGIN_{nonce}" in profile.read_text()


def test_server_command_wires_websocket_server(monkeypatch, tmp_path, capsys) -> None:
    captured: dict[str, object] = {}

    class FakeServer:
        def __init__(self, handler: object, config: object) -> None:
            captured["handler"] = handler
            captured["config"] = config

        async def serve_forever(self) -> None:
            captured["served"] = True

    monkeypatch.setattr(yikes.remote, "YikesRemoteServer", FakeServer)

    assert (
        cli.main(
            [
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                "8999",
                "--no-auth",
                "--token-store",
                str(tmp_path / "tokens.json"),
                "--event-store",
                str(tmp_path / "events"),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    config = captured["config"]
    assert getattr(config, "websocket_url") == "ws://127.0.0.1:8999"
    assert getattr(config, "require_token") is False
    assert captured["served"] is True
    assert "yikes! server listening" in output


def test_server_command_hashes_bootstrap_token_from_env(monkeypatch, tmp_path, capsys) -> None:
    captured: dict[str, object] = {}

    class FakeServer:
        def __init__(self, handler: object, config: object) -> None:
            captured["handler"] = handler
            captured["config"] = config

        async def serve_forever(self) -> None:
            captured["served"] = True

    monkeypatch.setattr(yikes.remote, "YikesRemoteServer", FakeServer)
    monkeypatch.setenv("YIKES_SERVER_TOKEN", "local-secret")
    store = tmp_path / "tokens.json"

    assert (
        cli.main(
            [
                "server",
                "--token-store",
                str(store),
                "--event-store",
                str(tmp_path / "events"),
                "--bootstrap-token-env",
                "YIKES_SERVER_TOKEN",
            ]
        )
        == 0
    )

    raw = store.read_text()
    assert "local-secret" not in raw
    assert "bootstrap:YIKES_SERVER_TOKEN" in raw
    assert "No bearer tokens found" not in capsys.readouterr().err


def test_sessions_command_lists_durable_sessions(tmp_path, capsys) -> None:
    runtime_store = tmp_path / "runtime"
    sandbox_store = tmp_path / "sandboxes"
    DurableSessionManager(runtime_store).create(
        backend=Backend.CLAUDE,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket="/tmp/yikes.sock", tmux_session="s1"),
        cwd=tmp_path,
    )

    assert (
        cli.main(
            [
                "sessions",
                "--runtime-store",
                str(runtime_store),
                "--sandbox-store",
                str(sandbox_store),
            ]
        )
        == 0
    )

    assert "tmux/claude" in capsys.readouterr().out


def test_close_command_closes_durable_session(tmp_path, capsys) -> None:
    runtime_store = tmp_path / "runtime"
    sandbox_store = tmp_path / "sandboxes"
    meta = DurableSessionManager(runtime_store).create(
        backend=Backend.CLAUDE,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket=str(tmp_path / "missing.sock"), tmux_session="s1"),
        cwd=tmp_path,
    )

    assert (
        cli.main(
            [
                "close",
                meta.id,
                "--runtime-store",
                str(runtime_store),
                "--sandbox-store",
                str(sandbox_store),
            ]
        )
        == 0
    )

    assert "Closed tmux session" in capsys.readouterr().out
    assert DurableSessionManager(runtime_store).get(meta.id) is None


def test_close_all_command_filters_runtime(tmp_path, capsys) -> None:
    runtime_store = tmp_path / "runtime"
    sandbox_store = tmp_path / "sandboxes"
    manager = DurableSessionManager(runtime_store)
    tmux = manager.create(
        backend=Backend.CLAUDE,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket=str(tmp_path / "missing.sock"), tmux_session="s1"),
        cwd=tmp_path,
    )
    direct = manager.create(
        backend=Backend.CODEX,
        driver=Driver.DIRECT,
        runtime=RuntimeRef(RuntimeKind.DIRECT),
        cwd=tmp_path,
    )

    assert (
        cli.main(
            [
                "close-all",
                "--runtime",
                "tmux",
                "--runtime-store",
                str(runtime_store),
                "--sandbox-store",
                str(sandbox_store),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Closed 1/1 sessions" in output
    assert DurableSessionManager(runtime_store).get(tmux.id) is None
    assert DurableSessionManager(runtime_store).get(direct.id) is not None


def test_tmux_start_names_session_and_replace_recreates(monkeypatch, tmp_path, capsys) -> None:
    runtime_store = tmp_path / "runtime"
    sandbox_store = tmp_path / "sandboxes"
    tmux_dir = tmp_path / "tmux"
    monkeypatch.setenv("YIKES_TMUX_DIR", str(tmux_dir))
    monkeypatch.setattr(yikes.session_inventory, "run_process", lambda *_args, **_kwargs: SimpleNamespace(stdout="", stderr=""))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(yikes.session_inventory.subprocess, "run", fake_run)

    assert (
        cli.main(
            [
                "tmux",
                "start",
                "site-editor",
                "--backend",
                "codex",
                "--cwd",
                str(tmp_path),
                "--runtime-store",
                str(runtime_store),
                "--sandbox-store",
                str(sandbox_store),
                "--json",
            ]
        )
        == 0
    )
    first = capsys.readouterr().out
    assert '"name": "site-editor"' in first

    assert (
        cli.main(
            [
                "tmux",
                "start",
                "site-editor",
                "--backend",
                "codex",
                "--cwd",
                str(tmp_path),
                "--replace",
                "--runtime-store",
                str(runtime_store),
                "--sandbox-store",
                str(sandbox_store),
                "--json",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert '"replaced": true' in output
    sessions = DurableSessionManager(runtime_store).list()
    assert len(sessions) == 1
    assert sessions[0].user_data["name"] == "site-editor"
    assert any("kill-session" in call for call in calls)


def test_tmux_state_reports_activity_for_named_session(monkeypatch, tmp_path, capsys) -> None:
    runtime_store = tmp_path / "runtime"
    sandbox_store = tmp_path / "sandboxes"
    DurableSessionManager(runtime_store).create(
        backend=Backend.CODEX,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket=str(tmp_path / "tmux.sock"), tmux_session="site-editor"),
        cwd=tmp_path,
        user_data={"name": "site-editor"},
    )

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout="Working (3s • esc to interrupt)\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(yikes.session_inventory.subprocess, "run", fake_run)

    assert (
        cli.main(
            [
                "tmux",
                "state",
                "site-editor",
                "--runtime-store",
                str(runtime_store),
                "--sandbox-store",
                str(sandbox_store),
                "--json",
            ]
        )
        == 0
    )

    assert '"state": "thinking"' in capsys.readouterr().out


def test_tmux_send_pastes_text_and_submits_by_name(monkeypatch, tmp_path, capsys) -> None:
    runtime_store = tmp_path / "runtime"
    sandbox_store = tmp_path / "sandboxes"
    DurableSessionManager(runtime_store).create(
        backend=Backend.CLAUDE,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket=str(tmp_path / "tmux.sock"), tmux_session="agent"),
        cwd=tmp_path,
        user_data={"name": "agent"},
    )
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((cmd, kwargs.get("input") if isinstance(kwargs.get("input"), str) else None))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(yikes.session_inventory.subprocess, "run", fake_run)

    assert (
        cli.main(
            [
                "tmux",
                "send",
                "agent",
                "hello",
                "--runtime-store",
                str(runtime_store),
                "--sandbox-store",
                str(sandbox_store),
            ]
        )
        == 0
    )

    assert any("load-buffer" in call[0] and call[1] == "hello" for call in calls)
    assert any("paste-buffer" in call[0] for call in calls)
    assert any(call[0][-1] == "Enter" for call in calls)
    assert "Sent key Enter" in capsys.readouterr().out
