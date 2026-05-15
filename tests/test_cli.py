from __future__ import annotations

from pathlib import Path

from yikes import AgentSettings, Backend, Complexity, Driver, DurableSessionManager, RuntimeKind, RuntimeRef
from yikes import cli
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
