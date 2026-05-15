from __future__ import annotations

import json
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
    SandboxManager,
    SessionState,
    SessionInventory,
    SessionLifecycle,
    TokenStore,
)
from yikes.reaper import reap_by_count
from yikes.sandbox import SandboxSession
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
    assert cmd[-2:] == ["sleep", "infinity"]
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
        user_data={"backend": "codex", "tmux_socket": "/workspace/yikes.sock", "tmux_session": "yikes-codex"}
    )
    lifecycle = SessionLifecycle(runtime_store=runtime_store, sandbox_store=sandbox_store)

    assert lifecycle.attach_command(meta.id) == ["tmux", "-S", "/tmp/yikes.sock", "attach", "-t", "s1"]
    assert lifecycle.attach_command(sandbox.id) == [
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
    assert settings.read_roots == (Path("/workspace/session-abcdef123456"),)
    assert settings.write_roots == (Path("/workspace/session-abcdef123456"),)
    assert settings.tmux_enabled is True


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
