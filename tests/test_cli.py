from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from yikes import AgentSettings, Backend, Complexity, Driver, DurableSessionManager, RuntimeKind, RuntimeRef
from yikes import cli
import yikes.interactive
import yikes.session_inventory
import yikes.tui
import yikes.remote


def _stub_select(monkeypatch, returns) -> None:
    """Make interactive.select return the given value(s) in order."""
    seq = iter(returns) if isinstance(returns, (list, tuple)) else iter([returns])
    monkeypatch.setattr(yikes.interactive, "select", lambda *a, **k: next(seq))


def _fake_stdin(*, tty: bool) -> SimpleNamespace:
    return SimpleNamespace(isatty=lambda: tty)


def test_no_args_menu_falls_back_to_tui_without_tty(monkeypatch) -> None:
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

    monkeypatch.setattr(cli.sys, "stdin", _fake_stdin(tty=False))
    monkeypatch.setattr(yikes.tui, "run_tui", fake_run_tui)

    assert cli.main([]) == 0
    assert called["backend"] is None
    assert called["driver"] is None
    assert called["complexity"] is None
    assert called["settings"] is None


def test_menu_routes_choice_to_codex(monkeypatch) -> None:
    monkeypatch.setattr(cli.sys, "stdin", _fake_stdin(tty=True))
    _stub_select(monkeypatch, "codex")
    routed: dict[str, object] = {}
    monkeypatch.setattr(cli, "_launch", lambda backend, **_kw: routed.update(backend=backend))

    assert cli.main(["menu"]) == 0
    assert routed["backend"] == Backend.CODEX


def test_claude_host_launch_starts_and_execs_attach(tmp_path, monkeypatch) -> None:
    started: dict[str, object] = {}

    def fake_start(self, name, *, backend, cwd, model=None, replace=False):
        started.update(name=name, backend=backend, cwd=cwd, replace=replace)
        return SimpleNamespace(
            id="x", name=name, backend=backend.value, socket="s", session=name, created=True, replaced=False
        )

    execd: dict[str, object] = {}
    monkeypatch.setattr(yikes.session_inventory.TmuxSessionController, "start", fake_start)
    monkeypatch.setattr(
        yikes.session_inventory.SessionLifecycle, "attach_command", lambda self, ref: ["tmux", "attach", ref]
    )
    monkeypatch.setattr(cli.os, "execvp", lambda file, args: execd.update(file=file, args=args))

    assert cli.main(["claude", "-n", "demo", "--cwd", str(tmp_path)]) == 0
    assert started["name"] == "demo"
    assert started["backend"] == Backend.CLAUDE
    assert started["replace"] is False
    assert execd["file"] == "tmux"


def test_launch_name_defaults_to_sanitized_directory(tmp_path, monkeypatch) -> None:
    project = tmp_path / "Shop App"
    project.mkdir()
    started: dict[str, object] = {}

    def fake_start(self, name, *, backend, cwd, model=None, replace=False):
        started.update(name=name)
        return SimpleNamespace(
            id="x", name=name, backend=backend.value, socket="s", session=name, created=False, replaced=False
        )

    monkeypatch.setattr(yikes.session_inventory.TmuxSessionController, "start", fake_start)
    monkeypatch.setattr(yikes.session_inventory.SessionLifecycle, "attach_command", lambda self, ref: ["true"])
    monkeypatch.setattr(cli.os, "execvp", lambda *_a: None)

    assert cli.main(["codex", "--cwd", str(project)]) == 0
    assert started["name"] == "Shop-App"


def test_config_isolated_and_ports_route_to_docker(tmp_path, monkeypatch) -> None:
    (tmp_path / "yikes.toml").write_text("isolated = true\nports = [8080]\n")
    captured: dict[str, object] = {}

    def fake_docker(backend, project_dir, name, *, new, model, ports, message=None):
        captured.update(backend=backend, name=name, ports=ports)

    monkeypatch.setattr(cli, "_launch_docker", fake_docker)
    monkeypatch.setattr(cli, "_launch_host", lambda *a, **k: pytest.fail("expected docker path"))

    assert cli.main(["codex", "--cwd", str(tmp_path)]) == 0
    assert captured["backend"] == Backend.CODEX
    assert captured["ports"] == (("8080", "8080"),)


def test_port_flag_overrides_config(tmp_path, monkeypatch) -> None:
    (tmp_path / "yikes.toml").write_text("isolated = true\nports = [8080]\n")
    captured: dict[str, object] = {}

    def fake_docker(backend, project_dir, name, *, new, model, ports, message=None):
        captured.update(ports=ports)

    monkeypatch.setattr(cli, "_launch_docker", fake_docker)

    assert cli.main(["claude", "--cwd", str(tmp_path), "-p", "3000:80"]) == 0
    assert captured["ports"] == (("3000", "80"),)


def test_init_writes_then_refuses_then_forces(tmp_path) -> None:
    assert cli.main(["init", "--cwd", str(tmp_path)]) == 0
    assert (tmp_path / "yikes.toml").exists()
    assert cli.main(["init", "--cwd", str(tmp_path)]) == 1
    assert cli.main(["init", "--cwd", str(tmp_path), "--force"]) == 0


def _stub_host_launch(monkeypatch) -> dict[str, object]:
    state: dict[str, object] = {"started": False}

    def fake_start(self, name, *, backend, cwd, model=None, replace=False):
        state["started"] = True
        state["name"] = name
        return SimpleNamespace(
            id="x", name=name, backend=backend.value, socket="s", session=name, created=True, replaced=False
        )

    monkeypatch.setattr(yikes.session_inventory.TmuxSessionController, "start", fake_start)
    monkeypatch.setattr(yikes.session_inventory.SessionLifecycle, "attach_command", lambda self, ref: ["true"])
    monkeypatch.setattr(cli.os, "execvp", lambda *_a: None)
    return state


def test_preflight_panel_prints_without_tty(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.sys, "stdin", _fake_stdin(tty=False))
    state = _stub_host_launch(monkeypatch)

    assert cli.main(["claude", "--cwd", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Ctrl-b d" in out
    assert state["started"] is True


def test_preflight_cancel_does_not_start(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli.sys, "stdin", _fake_stdin(tty=True))
    _stub_select(monkeypatch, "cancel")
    state = _stub_host_launch(monkeypatch)

    assert cli.main(["claude", "--cwd", str(tmp_path)]) == 0
    assert state["started"] is False


def test_preflight_setup_then_start(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli.sys, "stdin", _fake_stdin(tty=True))
    _stub_select(monkeypatch, ["setup", "start"])
    setup_calls: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "_run_setup",
        lambda backend, project_dir, *, assume_yes, goal=None: setup_calls.update(backend=backend) or True,
    )
    state = _stub_host_launch(monkeypatch)

    assert cli.main(["claude", "--cwd", str(tmp_path)]) == 0
    assert setup_calls["backend"] == Backend.CLAUDE
    assert state["started"] is True


def test_yes_flag_skips_prompt(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli.sys, "stdin", _fake_stdin(tty=True))

    def boom(*_a, **_k):  # no menu should appear with --yes
        raise AssertionError("prompt should be skipped with --yes")

    monkeypatch.setattr(yikes.interactive, "select", boom)
    state = _stub_host_launch(monkeypatch)

    assert cli.main(["claude", "--cwd", str(tmp_path), "--yes"]) == 0
    assert state["started"] is True


def test_setup_writes_config_from_scan(tmp_path, monkeypatch) -> None:
    import yikes.drivers

    monkeypatch.setattr(
        yikes.drivers,
        "ask_backend",
        lambda *a, **k: '{"ports": [8080, 5173], "backend": "codex", "notes": "vite app"}',
    )

    assert cli.main(["setup", "--cwd", str(tmp_path), "--yes"]) == 0
    from yikes.project_config import load_project_config

    config = load_project_config(tmp_path)
    assert config.backend == "codex"
    assert config.ports == (("8080", "8080"), ("5173", "5173"))


def _stub_seed(monkeypatch) -> list[tuple[str, object]]:
    sent: list[tuple[str, object]] = []
    monkeypatch.setattr(yikes.session_inventory.TmuxSessionController, "wait", lambda self, ref, **k: None)
    monkeypatch.setattr(
        yikes.session_inventory.TmuxSessionController,
        "send",
        lambda self, ref, text, *, submit=True: sent.append((text, submit)),
    )
    return sent


def test_message_seeds_new_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli.sys, "stdin", _fake_stdin(tty=False))
    _stub_host_launch(monkeypatch)  # start -> created=True
    sent = _stub_seed(monkeypatch)

    assert cli.main(["claude", "--cwd", str(tmp_path), "-m", "make a vite app"]) == 0
    assert sent == [("make a vite app", False)]


def test_reattach_does_not_seed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli.sys, "stdin", _fake_stdin(tty=False))

    def fake_start(self, name, *, backend, cwd, model=None, replace=False):
        return SimpleNamespace(
            id="x", name=name, backend=backend.value, socket="s", session=name, created=False, replaced=False
        )

    monkeypatch.setattr(yikes.session_inventory.TmuxSessionController, "start", fake_start)
    monkeypatch.setattr(yikes.session_inventory.SessionLifecycle, "attach_command", lambda self, ref: ["true"])
    monkeypatch.setattr(cli.os, "execvp", lambda *_a: None)
    sent = _stub_seed(monkeypatch)

    assert cli.main(["claude", "--cwd", str(tmp_path), "-m", "hi"]) == 0
    assert sent == []


def test_panel_prompt_key_sets_goal_and_seeds(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli.sys, "stdin", _fake_stdin(tty=True))
    _stub_select(monkeypatch, ["prompt", "start"])  # choose "add a prompt", then start
    monkeypatch.setattr("builtins.input", lambda *_a: "build X")  # the free-text prompt
    _stub_host_launch(monkeypatch)  # start -> created=True
    sent = _stub_seed(monkeypatch)

    assert cli.main(["claude", "--cwd", str(tmp_path)]) == 0
    assert sent == [("build X", False)]


def test_setup_passes_goal_into_scan(tmp_path, monkeypatch) -> None:
    import yikes.drivers

    captured: dict[str, object] = {}

    def fake_ask(backend, driver, prompt, **kwargs):
        captured["prompt"] = prompt
        return '{"ports": [5173], "backend": "claude", "notes": "vite"}'

    monkeypatch.setattr(yikes.drivers, "ask_backend", fake_ask)

    assert cli.main(["setup", "--cwd", str(tmp_path), "-m", "a vite app", "--yes"]) == 0
    assert "a vite app" in captured["prompt"]


def test_setup_creates_agents_md_when_missing(tmp_path, monkeypatch) -> None:
    import yikes.drivers

    monkeypatch.setattr(
        yikes.drivers,
        "ask_backend",
        lambda *a, **k: '{"ports": [8080], "backend": "claude", "summary": "a tiny dashboard", "notes": "flask"}',
    )

    assert cli.main(["setup", "--cwd", str(tmp_path), "-m", "build a dashboard", "--yes"]) == 0
    agents = tmp_path / "AGENTS.md"
    assert agents.exists()
    assert "build a dashboard" in agents.read_text()


def test_setup_keeps_existing_agents_md(tmp_path, monkeypatch) -> None:
    import yikes.drivers

    (tmp_path / "AGENTS.md").write_text("KEEP ME")
    monkeypatch.setattr(
        yikes.drivers,
        "ask_backend",
        lambda *a, **k: '{"ports": [8080], "backend": "claude", "summary": "x", "notes": "y"}',
    )

    assert cli.main(["setup", "--cwd", str(tmp_path), "--yes"]) == 0
    assert (tmp_path / "AGENTS.md").read_text() == "KEEP ME"


def test_setup_asks_for_backend_when_both_present(tmp_path, monkeypatch) -> None:
    import yikes.drivers

    monkeypatch.setattr(cli, "_available_backends", lambda: [Backend.CLAUDE, Backend.CODEX])
    monkeypatch.setattr(cli.sys, "stdin", _fake_stdin(tty=True))
    _stub_select(monkeypatch, "codex")  # backend choice
    monkeypatch.setattr("builtins.input", lambda *_a: "")  # project question -> skip
    monkeypatch.setattr(yikes.interactive, "confirm", lambda *a, **k: True)  # write confirm
    used: dict[str, object] = {}

    def fake_ask(backend, *a, **k):
        used["backend"] = backend
        return '{"ports": [], "backend": null, "summary": "x", "notes": "y"}'

    monkeypatch.setattr(yikes.drivers, "ask_backend", fake_ask)

    assert cli.main(["setup", "--cwd", str(tmp_path)]) == 0
    assert used["backend"] == Backend.CODEX


def test_setup_uses_sole_backend_without_asking(tmp_path, monkeypatch) -> None:
    import yikes.drivers

    monkeypatch.setattr(cli, "_available_backends", lambda: [Backend.CODEX])
    monkeypatch.setattr(cli.sys, "stdin", _fake_stdin(tty=True))

    def boom(*_a, **_k):
        raise AssertionError("should not ask when only one backend is installed")

    monkeypatch.setattr(yikes.interactive, "select", boom)
    used: dict[str, object] = {}

    def fake_ask(backend, *a, **k):
        used["backend"] = backend
        return '{"ports": [], "backend": null, "summary": "x", "notes": "y"}'

    monkeypatch.setattr(yikes.drivers, "ask_backend", fake_ask)

    assert cli.main(["setup", "--cwd", str(tmp_path), "--yes"]) == 0
    assert used["backend"] == Backend.CODEX


def _make_direct_session(runtime_store: Path) -> str:
    meta = DurableSessionManager(runtime_store).create(
        backend=Backend.CLAUDE,
        driver=Driver.DIRECT,
        runtime=RuntimeRef(RuntimeKind.DIRECT),
        cwd=runtime_store.parent,
    )
    return meta.id


def _close_all_args(tmp_path: Path) -> list[str]:
    return [
        "close-all",
        "--runtime-store",
        str(tmp_path / "sessions"),
        "--sandbox-store",
        str(tmp_path / "sandboxes"),
    ]


def test_close_all_no_sessions(tmp_path, capsys) -> None:
    assert cli.main(_close_all_args(tmp_path)) == 0
    assert "No matching sessions" in capsys.readouterr().out


def test_close_all_confirms_then_closes(tmp_path, monkeypatch) -> None:
    sid = _make_direct_session(tmp_path / "sessions")
    monkeypatch.setattr(cli.sys, "stdin", _fake_stdin(tty=True))
    monkeypatch.setattr(yikes.interactive, "confirm", lambda *a, **k: True)

    assert cli.main(_close_all_args(tmp_path)) == 0
    assert DurableSessionManager(tmp_path / "sessions").get(sid) is None


def test_close_all_aborts_when_declined(tmp_path, monkeypatch) -> None:
    sid = _make_direct_session(tmp_path / "sessions")
    monkeypatch.setattr(cli.sys, "stdin", _fake_stdin(tty=True))
    monkeypatch.setattr(yikes.interactive, "confirm", lambda *a, **k: False)

    assert cli.main(_close_all_args(tmp_path)) == 0
    assert DurableSessionManager(tmp_path / "sessions").get(sid) is not None


def test_close_all_non_tty_refuses_without_yes(tmp_path, monkeypatch) -> None:
    sid = _make_direct_session(tmp_path / "sessions")
    monkeypatch.setattr(cli.sys, "stdin", _fake_stdin(tty=False))

    assert cli.main(_close_all_args(tmp_path)) == 1
    assert DurableSessionManager(tmp_path / "sessions").get(sid) is not None


def test_close_all_yes_skips_prompt(tmp_path, monkeypatch) -> None:
    sid = _make_direct_session(tmp_path / "sessions")

    def boom(*_a, **_k):
        raise AssertionError("confirm should be skipped with --yes")

    monkeypatch.setattr(yikes.interactive, "confirm", boom)

    assert cli.main([*_close_all_args(tmp_path), "--yes"]) == 0
    assert DurableSessionManager(tmp_path / "sessions").get(sid) is None


def test_relaunch_tui_argv_returns_to_dashboard(monkeypatch) -> None:
    import yikes.tui as tui

    monkeypatch.setattr(tui.sys, "argv", ["/bin/yikes"])  # bare yikes -> must force tui, not menu
    assert tui._relaunch_tui_argv()[2:] == ["tui"]

    monkeypatch.setattr(tui.sys, "argv", ["/bin/yikes", "menu"])
    assert tui._relaunch_tui_argv()[2:] == ["tui"]

    monkeypatch.setattr(tui.sys, "argv", ["/bin/yikes", "tui", "--backend", "codex"])
    assert tui._relaunch_tui_argv()[2:] == ["tui", "--backend", "codex"]


def test_tui_rejects_remote_control_chat_mode(monkeypatch) -> None:
    def fake_run_tui(**_kwargs: object) -> None:
        raise AssertionError("run_tui should not be called")

    monkeypatch.setattr(yikes.tui, "run_tui", fake_run_tui)

    assert cli.main(["tui", "--driver", "remote-control"]) == 1


def test_tui_tmux_defaults_to_raw_capture_off(monkeypatch) -> None:
    called: dict[str, object] = {}

    def fake_run_tui(**kwargs: object) -> None:
        called.update(kwargs)

    monkeypatch.setattr(yikes.tui, "run_tui", fake_run_tui)

    assert cli.main(["tui", "--tmux"]) == 0
    settings = called["settings"]
    assert isinstance(settings, AgentSettings)
    assert settings.tmux_enabled is True
    assert settings.managed_output_enabled is False


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
                "--yes",
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
