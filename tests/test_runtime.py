from __future__ import annotations

import json
import tempfile
from pathlib import Path

from yikes import (
    AgentSettings,
    Backend,
    ChatOptions,
    CredentialGrant,
    Driver,
    DurableSessionManager,
    RuntimeKind,
    RuntimeRef,
    SandboxConfig,
    DEFAULT_SERVER_COMMAND,
    SandboxManager,
    SessionState,
    SessionInventory,
    SessionLifecycle,
    TokenStore,
)
from yikes.domain import DriverMode
from yikes.reaper import reap_by_count
from yikes.sandbox import SANDBOX_IMAGE_VERSION, SandboxSession
import yikes.drivers as drivers
import yikes.sandbox as sandbox_module


def test_durable_session_manager_round_trips_metadata(tmp_path: Path) -> None:
    manager = DurableSessionManager(tmp_path)
    meta = manager.create(
        backend=Backend.CLAUDE,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket="/tmp/yikes.sock", tmux_session="s1"),
        cwd=tmp_path,
        model="sonnet",
        settings=AgentSettings(read_roots=(tmp_path,), write_roots=(tmp_path / "out",)),
        credential_grants=(CredentialGrant("anthropic", "keychain"),),
    )

    restored = manager.get(meta.id)

    assert restored is not None
    assert restored.id == meta.id
    assert restored.backend is Backend.CLAUDE
    assert restored.runtime.kind is RuntimeKind.TMUX
    assert restored.runtime.tmux_socket == "/tmp/yikes.sock"
    assert restored.settings.read_roots == (tmp_path,)
    assert restored.credential_grants == (CredentialGrant("anthropic", "keychain"),)
    assert manager.mark_state(meta.id, SessionState.RUNNING).state is SessionState.RUNNING
    assert manager.delete(meta.id) is True
    assert manager.get(meta.id) is None


def test_durable_session_manager_honors_runtime_store_env(tmp_path: Path, monkeypatch) -> None:
    runtime_store = tmp_path / "runtime-env"
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(runtime_store))

    meta = DurableSessionManager().create(
        backend=Backend.CLAUDE,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket="/tmp/yikes.sock", tmux_session="s1"),
        cwd=tmp_path,
    )

    assert (runtime_store / f"{meta.id}.json").exists()


def test_durable_session_manager_can_use_explicit_session_id(tmp_path: Path) -> None:
    manager = DurableSessionManager(tmp_path)

    meta = manager.create(
        backend=Backend.CODEX,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket="/tmp/yikes.sock", tmux_session="named"),
        cwd=tmp_path,
        session_id="session-explicit",
    )

    assert meta.id == "session-explicit"
    assert manager.get("session-explicit") is not None


def test_sandbox_manager_persists_config_without_starting_docker(tmp_path: Path) -> None:
    manager = SandboxManager(tmp_path)
    session = manager.create(
        SandboxConfig(
            image="example:latest",
            memory="1g",
            cpus=2,
            env={"PUBLIC": "yes"},
            secret_env={"SECRET": "no"},
        ),
        user_data={"label": "claude"},
    )

    restored = manager.get(session.id)

    assert restored is not None
    assert restored.container_name.startswith("yksb-")
    assert restored.volume_name.startswith("ykvol-")
    assert restored.meta.config.image == "example:latest"
    assert restored.meta.config.secret_env == {"SECRET": "no"}
    assert restored.meta.user_data == {"label": "claude"}


def test_sandbox_run_command_contains_hardening_flags(tmp_path: Path) -> None:
    session = SandboxManager(tmp_path).create(SandboxConfig(image="example:latest", memory="512m"))

    cmd = session._build_run_cmd()

    assert cmd[:3] == ["docker", "run", "-d"]
    assert "--cap-drop=ALL" in cmd
    assert "--security-opt=no-new-privileges" in cmd
    assert "--read-only" in cmd
    assert "--pids-limit" in cmd
    assert "example:latest" in cmd
    assert cmd[-len(DEFAULT_SERVER_COMMAND):] == list(DEFAULT_SERVER_COMMAND)
    assert "/workspace/home:size=200m,nosuid" in cmd


def test_sandbox_run_command_mounts_configured_host_paths(tmp_path: Path) -> None:
    session = SandboxManager(tmp_path).create(
        SandboxConfig(
            image="example:latest",
            mounts=((str(tmp_path), "/workspace/project", "rw"),),
        )
    )

    cmd = session._build_run_cmd()

    assert "-v" in cmd
    assert f"{tmp_path}:/workspace/project:rw" in cmd


def test_session_display_name_prefers_custom_then_dir() -> None:
    from yikes.session_inventory import _session_display_name

    assert _session_display_name({"name": "shop"}, Path("/srv/whatever")) == "shop"
    assert _session_display_name({}, Path("/Users/me/projects/dashboard")) == "dashboard"
    assert _session_display_name({}, None) == ""


def test_project_label_uses_git_root_and_subfolder(tmp_path: Path) -> None:
    import subprocess

    from yikes.session_inventory import _git_root, _session_display_name, project_label

    repo = tmp_path / "fckten"
    sub = repo / "experiments" / "dashboard"
    sub.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git_root.cache_clear()

    assert project_label(repo) == "fckten"
    assert project_label(sub) == "fckten/dashboard"
    # auto name collapses to the git label; a custom name still wins
    assert _session_display_name({"name": "dashboard"}, sub) == "fckten/dashboard"
    assert _session_display_name({"name": "myapp"}, sub) == "myapp"


def test_session_summary_carries_name(tmp_path: Path) -> None:
    project = tmp_path / "dashboard"
    project.mkdir()
    DurableSessionManager(tmp_path / "sessions").create(
        backend=Backend.CLAUDE,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket=str(tmp_path / "s.sock"), tmux_session="s1"),
        cwd=project,
    )
    rows = SessionInventory(runtime_store=tmp_path / "sessions", sandbox_store=tmp_path / "sb").list()
    assert rows and rows[0].name == "dashboard"


def test_sandbox_run_command_publishes_ports(tmp_path: Path) -> None:
    session = SandboxManager(tmp_path).create(
        SandboxConfig(image="example:latest", ports=(("8080", "8080"), ("49200", "5173"))),
    )

    cmd = session._build_run_cmd()

    assert "127.0.0.1:8080:8080" in cmd
    assert "127.0.0.1:49200:5173" in cmd


def test_sandbox_config_ports_round_trip(tmp_path: Path) -> None:
    from yikes.sandbox import _config_from_json, _config_to_json

    config = SandboxConfig(image="example:latest", ports=(("8080", "80"),))
    restored = _config_from_json(_config_to_json(config))

    assert restored.ports == (("8080", "80"),)


def test_sandbox_write_file_creates_parent_directory_and_reports_failures(tmp_path: Path, monkeypatch) -> None:
    session = SandboxManager(tmp_path).create(SandboxConfig(image="example:latest"))
    calls: list[dict[str, object]] = []

    def fake_run(cmd: list[str], **kwargs: object):
        calls.append({"cmd": cmd, **kwargs})
        return sandbox_module.subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(SandboxSession, "_ensure_running", lambda _self: None)
    monkeypatch.setattr(sandbox_module.subprocess, "run", fake_run)

    session.write_file("/workspace/home/.codex/version.json", "{}")

    assert calls[0]["input"] == b"{}"
    command = calls[0]["cmd"]
    assert command[:5] == ["docker", "exec", "-i", session.container_name, "sh"]
    assert "mkdir -p /workspace/home/.codex" in command[-1]
    assert "cat > /workspace/home/.codex/version.json" in command[-1]


def test_docker_direct_turn_creates_ephemeral_workspace_before_running(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    class FakeSandbox:
        id = "abcdef123456"

        def __init__(self) -> None:
            self.meta = type(
                "Meta",
                (),
                {"user_data": {"workspace": "/workspace/session-abcdef123456"}},
            )()

        def exec(self, cmd: list[str], **_kwargs: object):
            calls.append(cmd)
            if cmd[:2] == ["sh", "-lc"] and "cat /tmp/yikes-result-" in cmd[-1]:
                return type("Result", (), {"returncode": 0, "stdout": "Hi\n", "stderr": ""})()
            if cmd[:2] == ["sh", "-lc"] and "cat /tmp/yikes-stderr-" in cmd[-1]:
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        def write_file(self, path: str, content: object) -> None:
            calls.append(["write_file", path])

    fake = FakeSandbox()
    monkeypatch.setattr(drivers, "_prepare_container_auth", lambda *_args, **_kwargs: None)

    answer = drivers._ask_inside_sandbox(
        fake,  # type: ignore[arg-type]
        "codex",
        "Say hi",
        timeout=5,
        model=None,
        settings=AgentSettings(),
    )

    assert answer == "Hi"
    assert ["sh", "-lc", "mkdir -p /workspace/session-abcdef123456"] in calls
    run_call = next(call for call in calls if call[:2] == ["sh", "-lc"] and "codex exec" in call[-1])
    assert run_call[-1].startswith("cd /workspace/session-abcdef123456 && ")


def test_sandbox_start_reports_docker_stderr(tmp_path: Path, monkeypatch) -> None:
    session = SandboxManager(tmp_path).create(SandboxConfig(image="example:latest"))

    monkeypatch.setattr(sandbox_module, "ensure_image", lambda _image: None)

    def fake_run_docker(cmd: list[str], **_kwargs: object) -> object:
        raise RuntimeError(f"Failed to run Docker command: {' '.join(cmd)}\nimage missing")

    monkeypatch.setattr(sandbox_module, "_run_docker", fake_run_docker)

    try:
        session.start()
    except RuntimeError as exc:
        assert "image missing" in str(exc)
    else:
        raise AssertionError("expected Docker startup failure")


def test_default_image_build_uses_checked_in_dockerfile(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object):
        class Result:
            returncode = 1

        return Result()

    def fake_run_docker(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        return object()

    monkeypatch.setattr(sandbox_module.subprocess, "run", fake_run)
    monkeypatch.setattr(sandbox_module, "_run_docker", fake_run_docker)

    sandbox_module.ensure_image(sandbox_module.DEFAULT_IMAGE)

    assert calls
    assert calls[0][:3] == ["docker", "build", "-f"]
    assert str(sandbox_module.DEFAULT_DOCKERFILE) in calls[0]


def test_default_sandbox_image_rebuilds_when_version_label_is_stale(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object):
        return sandbox_module.subprocess.CompletedProcess(cmd, 0, stdout="old-version\n", stderr="")

    monkeypatch.setattr(sandbox_module.subprocess, "run", fake_run)
    monkeypatch.setattr(sandbox_module, "_run_docker", lambda cmd, **_kwargs: calls.append(cmd))

    sandbox_module.ensure_image(sandbox_module.DEFAULT_IMAGE)

    assert any(call[:2] == ["docker", "build"] for call in calls)


def test_default_sandbox_image_reuses_current_version_label(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object):
        return sandbox_module.subprocess.CompletedProcess(cmd, 0, stdout=f"{SANDBOX_IMAGE_VERSION}\n", stderr="")

    monkeypatch.setattr(sandbox_module.subprocess, "run", fake_run)
    monkeypatch.setattr(sandbox_module, "_run_docker", lambda cmd, **_kwargs: calls.append(cmd))

    sandbox_module.ensure_image(sandbox_module.DEFAULT_IMAGE)

    assert not any(call[:2] == ["docker", "build"] for call in calls)


def test_session_inventory_lists_tmux_and_docker_sessions(tmp_path: Path, monkeypatch) -> None:
    runtime_store = tmp_path / "runtime"
    sandbox_store = tmp_path / "sandboxes"
    DurableSessionManager(runtime_store).create(
        backend=Backend.CLAUDE,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket="/tmp/yikes.sock", tmux_session="s1"),
        cwd=tmp_path,
    )
    sandbox = SandboxManager(sandbox_store).create(user_data={"backend": "codex"})

    monkeypatch.setattr(SandboxSession, "is_running", lambda _self: True)

    text = SessionInventory(runtime_store=runtime_store, sandbox_store=sandbox_store).format()

    assert "tmux/claude" in text
    assert "docker/codex running" in text
    assert sandbox.container_name in text


def test_session_inventory_marks_running_docker_with_dead_tmux_as_dead(tmp_path: Path, monkeypatch) -> None:
    sandbox_store = tmp_path / "sandboxes"
    sandbox = SandboxManager(sandbox_store).create(
        user_data={
            "backend": "codex",
            "tmux_socket": "/workspace/yikes-tmux.sock",
            "tmux_session": "yikes-codex",
        }
    )

    class Result:
        returncode = 1
        stdout = ""
        stderr = "no server running on /workspace/yikes-tmux.sock"

    monkeypatch.setattr(SandboxSession, "is_running", lambda _self: True)
    monkeypatch.setattr("yikes.session_inventory.subprocess.run", lambda *_args, **_kwargs: Result())

    rows = SessionInventory(runtime_store=tmp_path / "runtime", sandbox_store=sandbox_store).list()

    assert {row.id: row.state for row in rows}[sandbox.id] == "dead"


def test_session_inventory_marks_missing_tmux_sessions_dead(tmp_path: Path) -> None:
    runtime_store = tmp_path / "runtime"
    sandbox_store = tmp_path / "sandboxes"
    meta = DurableSessionManager(runtime_store).create(
        backend=Backend.CODEX,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket=str(tmp_path / "missing.sock"), tmux_session="gone"),
        cwd=tmp_path,
    )

    rows = SessionInventory(runtime_store=runtime_store, sandbox_store=sandbox_store).list()

    assert {row.id: row.state for row in rows}[meta.id] == "dead"


def test_session_inventory_does_not_mark_permission_denied_tmux_dead(tmp_path: Path, monkeypatch) -> None:
    runtime_store = tmp_path / "runtime"
    sandbox_store = tmp_path / "sandboxes"
    meta = DurableSessionManager(runtime_store).create(
        backend=Backend.CODEX,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket=str(tmp_path / "restricted.sock"), tmux_session="live"),
        cwd=tmp_path,
    )

    class Result:
        returncode = 1
        stderr = "error connecting to socket (Operation not permitted)"

    monkeypatch.setattr("yikes.session_inventory.subprocess.run", lambda *_args, **_kwargs: Result())

    rows = SessionInventory(runtime_store=runtime_store, sandbox_store=sandbox_store).list()

    assert {row.id: row.state for row in rows}[meta.id] == "created"


def test_session_lifecycle_closes_docker_session(tmp_path: Path, monkeypatch) -> None:
    sandbox_store = tmp_path / "sandboxes"
    sandbox = SandboxManager(sandbox_store).create(user_data={"backend": "claude"})
    destroyed: list[str] = []

    def fake_destroy(self: SandboxSession) -> None:
        destroyed.append(self.id)
        (sandbox_store / f"{self.id}.json").unlink(missing_ok=True)

    monkeypatch.setattr(SandboxSession, "destroy", fake_destroy)

    result = SessionLifecycle(sandbox_store=sandbox_store, runtime_store=tmp_path / "runtime").close(sandbox.id)

    assert result.closed is True
    assert result.runtime == "docker"
    assert destroyed == [sandbox.id]


def test_session_lifecycle_closes_all_by_runtime_and_backend(tmp_path: Path, monkeypatch) -> None:
    sandbox_store = tmp_path / "sandboxes"
    manager = SandboxManager(sandbox_store)
    claude = manager.create(user_data={"backend": "claude"})
    codex = manager.create(user_data={"backend": "codex"})
    destroyed: list[str] = []

    def fake_destroy(self: SandboxSession) -> None:
        destroyed.append(self.id)
        (sandbox_store / f"{self.id}.json").unlink(missing_ok=True)

    monkeypatch.setattr(SandboxSession, "destroy", fake_destroy)
    monkeypatch.setattr(SandboxSession, "is_running", lambda _self: True)

    results = SessionLifecycle(sandbox_store=sandbox_store, runtime_store=tmp_path / "runtime").close_all(
        runtime="docker",
        backend="claude",
    )

    assert [result.id for result in results] == [claude.id]
    assert destroyed == [claude.id]
    assert manager.get(codex.id) is not None


def test_session_lifecycle_switches_to_durable_session_options(tmp_path: Path) -> None:
    runtime_store = tmp_path / "runtime"
    meta = DurableSessionManager(runtime_store).create(
        backend=Backend.CODEX,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket="/tmp/yikes.sock", tmux_session="s1"),
        cwd=tmp_path,
        model="gpt-5.5",
    )

    current = AgentSettings()
    options = SessionLifecycle(runtime_store=runtime_store, sandbox_store=tmp_path / "sandboxes").switch_options(
        ChatOptions(Backend.CLAUDE, Driver.DIRECT, tmp_path, settings=current),
        meta.id,
    )

    assert options is not None
    assert options.backend is Backend.CODEX
    assert options.driver is Driver.TMUX
    assert options.model == "gpt-5.5"
    assert options.session_id == meta.id


def test_session_lifecycle_snapshots_tmux_history(tmp_path: Path, monkeypatch) -> None:
    runtime_store = tmp_path / "runtime"
    meta = DurableSessionManager(runtime_store).create(
        backend=Backend.CLAUDE,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket="/tmp/yikes.sock", tmux_session="s1"),
        cwd=tmp_path,
    )
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object):
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = "old line\nlatest line\n"

        return Result()

    monkeypatch.setattr("yikes.session_inventory.subprocess.run", fake_run)

    text = SessionLifecycle(runtime_store=runtime_store, sandbox_store=tmp_path / "sandboxes").snapshot(meta.id, lines=25)

    assert text == "old line\nlatest line"
    assert calls[0] == [
        "tmux",
        "-S",
        "/tmp/yikes.sock",
        "capture-pane",
        "-p",
        "-J",
        "-S",
        "-25",
        "-E",
        "-",
        "-t",
        "s1",
    ]


def test_session_lifecycle_auto_accepts_generated_codex_trust_prompt(tmp_path: Path, monkeypatch) -> None:
    runtime_store = tmp_path / "runtime"
    generated = Path(tempfile.mkdtemp(prefix="yikes-debug-", dir=tempfile.gettempdir()))
    meta = DurableSessionManager(runtime_store).create(
        backend=Backend.CODEX,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket="/tmp/yikes.sock", tmux_session="s1"),
        cwd=generated,
        user_data={"cwd_explicit": "false"},
    )
    calls: list[list[str]] = []
    captures = [
        "> You are in /tmp/yikes-debug-x\n\nDo you trust the contents of this directory?\n\n› 1. Yes, continue\n  2. No, quit",
        "codex ready",
    ]

    def fake_run(cmd: list[str], **_kwargs: object):
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = captures.pop(0) if "capture-pane" in cmd else ""

        return Result()

    monkeypatch.setattr("yikes.session_inventory.subprocess.run", fake_run)

    text = SessionLifecycle(runtime_store=runtime_store, sandbox_store=tmp_path / "sandboxes").snapshot(meta.id)

    assert text == "codex ready"
    assert any(call[-1] == "Enter" for call in calls)


def test_session_lifecycle_returns_persisted_capture_markers(tmp_path: Path) -> None:
    runtime_store = tmp_path / "runtime"
    sandbox_store = tmp_path / "sandboxes"
    meta = DurableSessionManager(runtime_store).create(
        backend=Backend.CLAUDE,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket="/tmp/yikes.sock", tmux_session="s1"),
        cwd=tmp_path,
        user_data={"capture_start": "@@@@/abc", "capture_end": "/@@@@/abc"},
    )
    sandbox = SandboxManager(sandbox_store).create(
        user_data={"capture_start": "TEXT_BEGIN_old", "capture_end": "TEXT_END_old"}
    )
    lifecycle = SessionLifecycle(runtime_store=runtime_store, sandbox_store=sandbox_store)

    assert lifecycle.capture_markers(meta.id) == (("@@@@/abc", "/@@@@/abc"),)
    assert lifecycle.capture_markers(sandbox.id) == (("TEXT_BEGIN_old", "TEXT_END_old"),)


def test_session_lifecycle_returns_attach_commands_for_tmux_and_docker_tmux(tmp_path: Path) -> None:
    runtime_store = tmp_path / "runtime"
    sandbox_store = tmp_path / "sandboxes"
    meta = DurableSessionManager(runtime_store).create(
        backend=Backend.CLAUDE,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket="/tmp/yikes.sock", tmux_session="s1"),
        cwd=tmp_path,
    )
    sandbox = SandboxManager(sandbox_store).create(
        user_data={
            "backend": "codex",
            "logical_session_id": "logical-docker-session",
            "tmux_socket": "/workspace/yikes.sock",
            "tmux_session": "yikes-codex",
        }
    )
    lifecycle = SessionLifecycle(runtime_store=runtime_store, sandbox_store=sandbox_store)

    assert lifecycle.attach_command(meta.id) == ["tmux", "-S", "/tmp/yikes.sock", "attach", "-t", "s1"]
    assert lifecycle.resolve_session_id("logical-docker-session") == sandbox.id
    assert lifecycle.attach_command("logical-docker-session") == [
        "docker",
        "exec",
        "-it",
        sandbox.container_name,
        "tmux",
        "-S",
        "/workspace/yikes.sock",
        "attach",
        "-t",
        "yikes-codex",
    ]


def test_session_inventory_displays_logical_id_for_docker_sessions(tmp_path: Path, monkeypatch) -> None:
    sandbox_store = tmp_path / "sandboxes"
    sandbox = SandboxManager(sandbox_store).create(
        user_data={"backend": "codex", "logical_session_id": "logical-docker-session"}
    )
    monkeypatch.setattr(SandboxSession, "is_running", lambda _self: True)

    rows = SessionInventory(runtime_store=tmp_path / "runtime", sandbox_store=sandbox_store).list()

    assert rows[0].id == "logical-docker-session"
    assert sandbox.id in rows[0].detail


def test_session_lifecycle_resolves_legacy_docker_tmux_label_prefix(tmp_path: Path) -> None:
    sandbox_store = tmp_path / "sandboxes"
    sandbox = SandboxManager(sandbox_store).create(
        user_data={
            "backend": "codex",
            "label": "docker-tmux-codex-abcdef123456",
            "tmux_socket": "/workspace/yikes.sock",
            "tmux_session": "yikes-codex",
        }
    )
    lifecycle = SessionLifecycle(runtime_store=tmp_path / "runtime", sandbox_store=sandbox_store)

    assert lifecycle.resolve_session_id("abcdef1234567890") == sandbox.id
    assert lifecycle.attach_command("abcdef1234567890") == [
        "docker",
        "exec",
        "-it",
        sandbox.container_name,
        "tmux",
        "-S",
        "/workspace/yikes.sock",
        "attach",
        "-t",
        "yikes-codex",
    ]


def test_session_lifecycle_sends_keys_to_tmux(tmp_path: Path, monkeypatch) -> None:
    runtime_store = tmp_path / "runtime"
    meta = DurableSessionManager(runtime_store).create(
        backend=Backend.CLAUDE,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket="/tmp/yikes.sock", tmux_session="s1"),
        cwd=tmp_path,
    )
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object):
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("yikes.session_inventory.subprocess.run", fake_run)

    result = SessionLifecycle(runtime_store=runtime_store, sandbox_store=tmp_path / "sandboxes").send_key(meta.id, "Down")

    assert result.closed is True
    assert calls == [["tmux", "-S", "/tmp/yikes.sock", "send-keys", "-t", "s1", "Down"]]


def test_session_lifecycle_resizes_tmux_window(tmp_path: Path, monkeypatch) -> None:
    runtime_store = tmp_path / "runtime"
    meta = DurableSessionManager(runtime_store).create(
        backend=Backend.CLAUDE,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket="/tmp/yikes.sock", tmux_session="s1"),
        cwd=tmp_path,
    )
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object):
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("yikes.session_inventory.subprocess.run", fake_run)

    result = SessionLifecycle(runtime_store=runtime_store, sandbox_store=tmp_path / "sandboxes").resize(
        meta.id,
        cols=180,
        rows=52,
    )

    assert result.closed is True
    assert calls == [["tmux", "-S", "/tmp/yikes.sock", "resize-window", "-x", "180", "-y", "52", "-t", "s1"]]


def test_session_lifecycle_pastes_text_to_tmux(tmp_path: Path, monkeypatch) -> None:
    runtime_store = tmp_path / "runtime"
    meta = DurableSessionManager(runtime_store).create(
        backend=Backend.CLAUDE,
        driver=Driver.TMUX,
        runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket="/tmp/yikes.sock", tmux_session="s1"),
        cwd=tmp_path,
    )
    calls: list[tuple[list[str], object]] = []

    def fake_run(cmd: list[str], **kwargs: object):
        calls.append((cmd, kwargs.get("input")))

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("yikes.session_inventory.subprocess.run", fake_run)

    result = SessionLifecycle(runtime_store=runtime_store, sandbox_store=tmp_path / "sandboxes").paste_text(meta.id, "hello")

    assert result.closed is True
    assert calls[0] == (["tmux", "-S", "/tmp/yikes.sock", "load-buffer", "-b", f"yikes-{meta.id}", "-"], "hello")
    assert calls[1] == (["tmux", "-S", "/tmp/yikes.sock", "paste-buffer", "-d", "-b", f"yikes-{meta.id}", "-t", "s1"], None)


def test_docker_tmux_without_explicit_cwd_uses_container_workspace(tmp_path: Path, monkeypatch) -> None:
    sandbox_store = tmp_path / "sandboxes"
    monkeypatch.setattr(drivers, "SandboxManager", lambda: SandboxManager(sandbox_store))

    sandbox, settings = drivers._docker_session_for(
        "claude",
        tmp_path,
        AgentSettings(tmux_enabled=True),
        cwd_explicit=False,
        session_id="abcdef1234567890",
        use_tmux=True,
    )

    assert sandbox.meta.config.mounts == ()
    assert sandbox.meta.user_data["workspace"] == "/workspace/session-abcdef123456"
    assert sandbox.meta.user_data["logical_session_id"] == "abcdef1234567890"
    assert sandbox.meta.user_data["server_port"] == "8989"
    assert sandbox.meta.user_data["managed_output_enabled"] == "true"
    assert "YIKES_SERVER_TOKEN" in sandbox.meta.config.secret_env
    assert settings.read_roots == (Path("/workspace/session-abcdef123456"),)
    assert settings.write_roots == (Path("/workspace/session-abcdef123456"),)
    assert settings.tmux_enabled is True


def test_docker_sessions_with_explicit_cwd_are_unique_per_logical_session(tmp_path: Path, monkeypatch) -> None:
    sandbox_store = tmp_path / "sandboxes"
    monkeypatch.setattr(drivers, "SandboxManager", lambda: SandboxManager(sandbox_store))

    first, _settings = drivers._docker_session_for(
        "codex",
        tmp_path,
        AgentSettings(tmux_enabled=True),
        cwd_explicit=True,
        session_id="1111111111111111",
        use_tmux=True,
    )
    second, _settings = drivers._docker_session_for(
        "codex",
        tmp_path,
        AgentSettings(tmux_enabled=True),
        cwd_explicit=True,
        session_id="2222222222222222",
        use_tmux=True,
    )

    assert first.id != second.id
    assert first.meta.user_data["label"] == "docker-tmux-codex-111111111111"
    assert second.meta.user_data["label"] == "docker-tmux-codex-222222222222"


def test_docker_tmux_switch_restores_unmanaged_output_mode(tmp_path: Path, monkeypatch) -> None:
    sandbox_store = tmp_path / "sandboxes"
    monkeypatch.setattr(drivers, "SandboxManager", lambda: SandboxManager(sandbox_store))
    sandbox, _settings = drivers._docker_session_for(
        "codex",
        tmp_path,
        AgentSettings(tmux_enabled=True, managed_output_enabled=False),
        cwd_explicit=True,
        session_id="3333333333333333",
        use_tmux=True,
    )
    sandbox.meta.user_data["tmux_socket"] = "/workspace/yikes.sock"
    sandbox.meta.user_data["tmux_session"] = "yikes"
    sandbox._save()

    options = SessionLifecycle(runtime_store=tmp_path / "runtime", sandbox_store=sandbox_store).switch_options(
        ChatOptions(Backend.CODEX, Driver.DOCKER, tmp_path),
        sandbox.id,
    )

    assert options is not None
    assert options.mode is DriverMode.TMUX
    assert options.settings.managed_output_enabled is False


def test_docker_cli_without_explicit_cwd_does_not_mount_host_directory(tmp_path: Path, monkeypatch) -> None:
    sandbox_store = tmp_path / "sandboxes"
    monkeypatch.setattr(drivers, "SandboxManager", lambda: SandboxManager(sandbox_store))

    sandbox, settings = drivers._docker_session_for(
        "codex",
        tmp_path,
        AgentSettings(tmux_enabled=False),
        cwd_explicit=False,
        session_id="fedcba9876543210",
        use_tmux=False,
    )

    assert sandbox.meta.config.mounts == ()
    assert sandbox.meta.user_data["workspace"] == "/workspace/session-fedcba987654"
    assert sandbox.meta.user_data["logical_session_id"] == "fedcba9876543210"
    assert sandbox.meta.user_data["server_port"] == "8989"
    assert "YIKES_SERVER_TOKEN" in sandbox.meta.config.secret_env
    assert settings.read_roots == (Path("/workspace/session-fedcba987654"),)
    assert settings.write_roots == (Path("/workspace/session-fedcba987654"),)
    assert settings.tmux_enabled is False


def test_token_store_hashes_and_verifies_tokens(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    store = TokenStore(path)

    token = store.create_temporary("session", duration_seconds=60)
    permanent = store.create_permanent("server")

    raw = path.read_text()
    assert token not in raw
    assert permanent not in raw
    assert store.verify(token) is True
    assert TokenStore(path).verify(permanent) is True
    store.add_existing("provided-secret", label="docker", permanent=True)
    assert TokenStore(path).verify("provided-secret") is True

    data = json.loads(path.read_text())
    assert "token_hash" in data["tokens"][0]
    assert store.revoke_all_temporary() == 1
    assert store.verify(token) is False


def test_reap_by_count_removes_oldest_sessions(tmp_path: Path, monkeypatch) -> None:
    manager = SandboxManager(tmp_path)
    old = manager.create(user_data={"name": "old"})
    new = manager.create(user_data={"name": "new"})
    destroyed: list[str] = []

    def fake_destroy(self: SandboxSession) -> None:
        destroyed.append(self.id)
        (tmp_path / f"{self.id}.json").unlink(missing_ok=True)

    monkeypatch.setattr(SandboxSession, "destroy", fake_destroy)

    removed = reap_by_count(manager, max_sessions=1)

    assert removed == [old.id]
    assert destroyed == [old.id]
    assert manager.get(new.id) is not None
