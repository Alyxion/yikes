from __future__ import annotations

import json
import os
import re
import shutil
import shlex
import socket
import subprocess
import tempfile
import time
import asyncio
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .domain import AgentSettings, Backend, Driver, McpServer
from .errors import BackendRunError, BackendUnavailable, DriverUnavailable
from .credentials import ClaudeCredentialProvider, CodexCredentialProvider
from .mcp import McpConfig, McpServerConfig, resolve_servers
from .mcp_proxy import ProxyManager
from .process import require_binary, run_process
from .runtime import DurableSessionManager, RuntimeKind, RuntimeRef, SessionState
from .sandbox import DEFAULT_IMAGE, SandboxConfig, SandboxManager, SandboxSession

if False:  # pragma: no cover
    from .chatbot import Backend, Driver


@dataclass(frozen=True)
class ResultMarkers:
    start: str
    end: str


def ask_backend(
    backend: "Backend",
    driver: "Driver",
    prompt: str,
    *,
    cwd: Path,
    timeout: float,
    model: str | None,
    settings: AgentSettings,
    cwd_explicit: bool = True,
    session_id: str | None = None,
) -> str:
    if driver.value == "direct":
        return _ask_direct(backend.value, prompt, cwd=cwd, timeout=timeout, model=model, settings=settings)
    if driver.value == "tmux":
        return _ask_tmux(
            backend.value,
            prompt,
            cwd=cwd,
            cwd_explicit=cwd_explicit,
            timeout=timeout,
            model=model,
            settings=settings,
        )
    if driver.value == "docker":
        return _ask_docker(
            backend.value,
            prompt,
            cwd=cwd,
            cwd_explicit=cwd_explicit,
            timeout=timeout,
            model=model,
            session_id=session_id,
            settings=settings,
        )
    if driver.value == "remote-control":
        return _ask_remote_control(backend.value, prompt, cwd=cwd, timeout=timeout, model=model, settings=settings)
    raise DriverUnavailable(f"unknown driver: {driver}")


def _ask_direct(
    backend: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: float,
    model: str | None,
    settings: AgentSettings,
) -> str:
    if backend == "claude":
        return _ask_claude_direct(prompt, cwd=cwd, timeout=timeout, model=model, settings=settings)
    if backend == "codex":
        return _ask_codex_exec(prompt, cwd=cwd, timeout=timeout, model=model, settings=settings)
    raise DriverUnavailable(f"unknown backend: {backend}")


def _ask_claude_direct(
    prompt: str,
    *,
    cwd: Path,
    timeout: float,
    model: str | None,
    settings: AgentSettings,
) -> str:
    require_binary("claude")
    with _temporary_claude_mcp_config(settings) as mcp_config:
        argv = _claude_argv(prompt, model=model, mcp_config=mcp_config)
        try:
            proc = run_process(
                argv,
                cwd=cwd,
                timeout=timeout,
                env={"DISABLE_AUTOUPDATER": "1"},
            )
        except BackendRunError as exc:
            if _looks_like_auth_error(exc.stdout + exc.stderr):
                detail = _extract_error_text(exc.stdout) or "Claude auth is unavailable"
                raise BackendUnavailable(
                    f"{detail} (the current process may not have access to your Claude login/keychain state)"
                ) from exc
            raise
    raw = proc.stdout.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    for key in ("result", "content", "text", "message"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return raw


def _ask_codex_exec(
    prompt: str,
    *,
    cwd: Path,
    timeout: float,
    model: str | None,
    settings: AgentSettings,
) -> str:
    require_binary("codex")
    with tempfile.TemporaryDirectory(prefix="yikes-codex-") as tmp:
        out = Path(tmp) / "last-message.txt"
        argv = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            _codex_sandbox(settings),
            "--color",
            "never",
            "-o",
            str(out),
        ]
        if model:
            argv.extend(["--model", model])
        argv.append(prompt)
        try:
            proc = run_process(argv, cwd=cwd, timeout=timeout)
        except BackendRunError as exc:
            if _looks_like_auth_error(exc.stdout + exc.stderr):
                detail = _extract_error_text(exc.stdout + exc.stderr) or "Codex auth is unavailable"
                raise BackendUnavailable(
                    f"{detail} (the current process may not have access to your Codex auth/session files)"
                ) from exc
            raise
        if out.exists():
            text = out.read_text(encoding="utf-8").strip()
            if text:
                return text
        return _strip_ansi(proc.stdout).strip()


def _ask_tmux(
    backend: str,
    prompt: str,
    *,
    cwd: Path,
    cwd_explicit: bool,
    timeout: float,
    model: str | None,
    settings: AgentSettings,
) -> str:
    require_binary("tmux")
    socket_path, session_name = _ensure_local_tmux_session(backend, cwd, model=model, settings=settings)
    if backend == "claude" and not cwd_explicit:
        _confirm_local_workspace_trust_if_needed(socket_path, session_name, cwd=cwd)
    if backend == "codex":
        if not cwd_explicit:
            _confirm_local_codex_workspace_trust_if_needed(socket_path, session_name, cwd=cwd)
        _dismiss_local_codex_update_prompt_if_needed(socket_path, session_name, cwd=cwd)
    markers = _result_markers()
    _tmux_paste(socket_path, session_name, _marked_prompt(prompt, markers), cwd=cwd)
    screen = _wait_for_tmux_result(socket_path, session_name, markers=markers, cwd=cwd, timeout=timeout)
    return _extract_marked_result(screen, markers)


def _ask_docker(
    backend: str,
    prompt: str,
    *,
    cwd: Path,
    cwd_explicit: bool,
    timeout: float,
    model: str | None,
    session_id: str | None,
    settings: AgentSettings,
) -> str:
    require_binary("docker")
    proxy_manager = ProxyManager()
    try:
        docker_settings = _settings_for_docker(settings, proxy_manager)
        sandbox, docker_settings = _docker_session_for(
            backend,
            cwd,
            docker_settings,
            cwd_explicit=cwd_explicit,
            session_id=session_id or uuid4().hex,
            use_tmux=settings.tmux_enabled,
        )
        if settings.tmux_enabled:
            return _ask_docker_tmux(
                sandbox,
                backend,
                prompt,
                timeout=timeout,
                model=model,
                settings=docker_settings,
            )
        return _ask_inside_sandbox(
            sandbox,
            backend,
            prompt,
            timeout=timeout,
            model=model,
            settings=docker_settings,
        )
    finally:
        proxy_manager.stop()


def _docker_session_for(
    backend: str,
    cwd: Path,
    settings: AgentSettings,
    *,
    cwd_explicit: bool = True,
    session_id: str,
    use_tmux: bool,
) -> tuple[SandboxSession, AgentSettings]:
    image = os.environ.get("YIKES_DOCKER_IMAGE", DEFAULT_IMAGE)
    manager = SandboxManager()
    if use_tmux and not cwd_explicit:
        container_workspace = Path(f"/workspace/session-{session_id[:12]}")
        mounts: tuple[tuple[str, str, str], ...] = ()
        container_settings = _container_ephemeral_settings(settings, container_workspace)
        label = f"docker-tmux-{backend}-{session_id[:12]}"
    else:
        container_workspace = Path("/workspace/project")
        mounts, container_settings = _docker_mounts(cwd, settings)
        label = _docker_label(backend, cwd, mounts)
    existing = manager.find_running(image=image, label=label)
    if existing is not None:
        return existing, container_settings
    return manager.create(
        SandboxConfig(
            image=image,
            mounts=mounts,
            env={"DISABLE_AUTOUPDATER": "1"},
            secret_env=_docker_secret_env(),
        ),
        user_data={
            "label": label,
            "backend": backend,
            "cwd": str(cwd),
            "cwd_explicit": "true" if cwd_explicit else "false",
            "workspace": str(container_workspace),
        },
    ), container_settings


def _ask_docker_tmux(
    sandbox: SandboxSession,
    backend: str,
    prompt: str,
    *,
    timeout: float,
    model: str | None,
    settings: AgentSettings,
) -> str:
    _prepare_container_auth(sandbox, backend)
    socket_path = Path("/workspace/yikes-tmux.sock")
    session_name = f"yikes-{backend}"
    workspace = _sandbox_workspace(sandbox)
    sandbox.exec(["sh", "-lc", f"mkdir -p {shlex.quote(workspace)}"], capture_output=True, text=True, timeout=10, check=True)
    if not _container_tmux_session_alive(sandbox, socket_path, session_name):
        argv = _backend_tui_argv(backend, model=model)
        sandbox.exec(
            [
                "tmux",
                "-S",
                str(socket_path),
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                session_name,
                "-x",
                "160",
                "-y",
                "48",
                "-c",
                workspace,
                *argv,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        if not _container_tmux_session_alive(sandbox, socket_path, session_name):
            raise BackendRunError(
                f"container tmux session {session_name} exited before it could receive input",
                stdout="",
                stderr=f"Started command: {shlex.join(argv)}",
            )
        sandbox.meta.user_data["tmux_socket"] = str(socket_path)
        sandbox.meta.user_data["tmux_session"] = session_name
        sandbox._save()
    if backend == "claude" and sandbox.meta.user_data.get("cwd_explicit") == "false":
        _confirm_container_workspace_trust_if_needed(sandbox, socket_path, session_name)
    if backend == "codex":
        if sandbox.meta.user_data.get("cwd_explicit") == "false":
            _confirm_container_codex_workspace_trust_if_needed(sandbox, socket_path, session_name)
        _dismiss_container_codex_update_prompt_if_needed(sandbox, socket_path, session_name)
    markers = _result_markers()
    _container_tmux_paste(sandbox, socket_path, session_name, _marked_prompt(prompt, markers))
    screen = _wait_for_container_tmux_result(sandbox, socket_path, session_name, markers=markers, timeout=timeout)
    return _extract_marked_result(screen, markers)


def _ask_inside_sandbox(
    sandbox: SandboxSession,
    backend: str,
    prompt: str,
    *,
    timeout: float,
    model: str | None,
    settings: AgentSettings,
) -> str:
    turn_id = uuid4().hex
    _prepare_container_auth(sandbox, backend)
    result_path = Path(f"/tmp/yikes-result-{turn_id}.txt")
    err_path = Path(f"/tmp/yikes-stderr-{turn_id}.txt")
    mcp_config = Path(f"/tmp/yikes-mcp-{turn_id}.json") if backend == "claude" and settings.mcp_servers else None
    if mcp_config is not None:
        sandbox.write_file(str(mcp_config), json.dumps({"mcpServers": _mcp_payload(settings)}))
    command = _backend_shell_command(
        backend,
        prompt,
        result_path=result_path,
        err_path=err_path,
        model=model,
        settings=settings,
        mcp_config_path=mcp_config,
    )
    command = f"cd /workspace/project && {command}"
    proc = sandbox.exec(
        ["sh", "-lc", command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    output = sandbox.exec(
        ["sh", "-lc", f"cat {shlex.quote(str(result_path))} 2>/dev/null"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    ).stdout
    stderr_text = sandbox.exec(
        ["sh", "-lc", f"cat {shlex.quote(str(err_path))} 2>/dev/null"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    ).stdout
    cleanup = f"rm -f {shlex.quote(str(result_path))} {shlex.quote(str(err_path))}"
    if mcp_config is not None:
        cleanup += f" {shlex.quote(str(mcp_config))}"
    sandbox.exec(["sh", "-lc", cleanup], capture_output=True, text=True, timeout=10, check=False)
    if proc.returncode != 0:
        raise BackendRunError(
            f"docker {backend} turn failed with exit code {proc.returncode}",
            stdout=str(output or proc.stdout),
            stderr=str(stderr_text or proc.stderr),
        )
    return _extract_backend_output(backend, str(output).strip())


def _backend_shell_command(
    backend: str,
    prompt: str,
    *,
    result_path: Path,
    err_path: Path,
    model: str | None,
    settings: AgentSettings,
    mcp_config_path: Path | None = None,
) -> str:
    if backend == "claude":
        argv = _claude_argv(prompt, model=model, mcp_config=mcp_config_path)
        return f"DISABLE_AUTOUPDATER=1 {shlex.join(argv)} > {shlex.quote(str(result_path))} 2> {shlex.quote(str(err_path))}"
    if backend == "codex":
        argv = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            _codex_sandbox(settings),
            "--color",
            "never",
            "-o",
            str(result_path),
        ]
        if model:
            argv.extend(["--model", model])
        argv.append(prompt)
        return f"{shlex.join(argv)} > /dev/null 2> {shlex.quote(str(err_path))}"
    raise DriverUnavailable(f"unknown backend: {backend}")


def _claude_argv(prompt: str, *, model: str | None, mcp_config: Path | None = None) -> list[str]:
    argv = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--permission-mode",
        "dontAsk",
    ]
    if model:
        argv.extend(["--model", model])
    if mcp_config is not None:
        argv.extend(["--mcp-config", str(mcp_config)])
    return argv


def _ensure_local_tmux_session(
    backend: str,
    cwd: Path,
    *,
    model: str | None,
    settings: AgentSettings,
) -> tuple[Path, str]:
    tmux_dir = Path(os.environ.get("YIKES_TMUX_DIR", str(Path.home() / ".yikes" / "tmux"))).expanduser()
    tmux_dir.mkdir(parents=True, exist_ok=True)
    tmux_dir.chmod(0o700)
    label = _tmux_label(backend, cwd, model)
    socket_path = tmux_dir / f"{label}.sock"
    session_name = label
    if not _tmux_session_alive(socket_path, session_name, cwd):
        codex_home = _prepare_local_codex_home(tmux_dir, label) if backend == "codex" else None
        argv = [
            "tmux",
            "-S",
            str(socket_path),
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            session_name,
            "-x",
            "160",
            "-y",
            "48",
            "-c",
            str(cwd),
            *_backend_tui_argv(backend, model=model, codex_home=codex_home),
        ]
        run_process(argv, cwd=cwd, timeout=20)
        _set_tmux_options(socket_path, cwd)
        if not _tmux_session_alive(socket_path, session_name, cwd):
            raise BackendRunError(
                f"tmux session {session_name} exited before it could receive input",
                stdout="",
                stderr=f"Started command: {shlex.join(_backend_tui_argv(backend, model=model, codex_home=codex_home))}",
            )
    _record_tmux_session(backend, cwd, socket_path, session_name, model=model, settings=settings, label=label)
    return socket_path, session_name


def _backend_tui_argv(backend: str, *, model: str | None, codex_home: Path | None = None) -> list[str]:
    if backend == "claude":
        argv = ["claude", "--permission-mode", "dontAsk"]
        if model:
            argv.extend(["--model", model])
        return argv
    if backend == "codex":
        argv = ["codex", "--no-alt-screen"]
        if model:
            argv.extend(["--model", model])
        if codex_home is not None:
            return ["env", f"CODEX_HOME={codex_home}", *argv]
        return argv
    raise DriverUnavailable(f"unknown backend: {backend}")


def _set_tmux_options(socket_path: Path, cwd: Path) -> None:
    for args in (
        ["set", "-g", "status", "off"],
        ["set", "-g", "history-limit", "100000"],
        ["set", "-g", "extended-keys", "off"],
    ):
        subprocess.run(["tmux", "-S", str(socket_path), *args], cwd=str(cwd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def _record_tmux_session(
    backend: str,
    cwd: Path,
    socket_path: Path,
    session_name: str,
    *,
    model: str | None,
    settings: AgentSettings,
    label: str,
) -> None:
    try:
        manager = DurableSessionManager()
        for meta in manager.list():
            if meta.runtime.kind is RuntimeKind.TMUX and meta.user_data.get("label") == label:
                meta.state = SessionState.RUNNING
                manager.save(meta)
                return
        manager.create(
            backend=Backend(backend),
            driver=Driver.TMUX,
            runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket=str(socket_path), tmux_session=session_name),
            cwd=cwd,
            model=model,
            settings=settings,
            user_data={"label": label, "attach": f"tmux -S {socket_path} attach -t {session_name}"},
        )
    except OSError:
        return


def _tmux_label(backend: str, cwd: Path, model: str | None) -> str:
    digest = hashlib.sha256(f"{backend}:{cwd.resolve()}:{model or ''}".encode()).hexdigest()[:12]
    return f"yikes-{backend}-{digest}"


def _result_markers() -> ResultMarkers:
    nonce = uuid4().hex[:12]
    return ResultMarkers(f"YIKES_RESULT_START_{nonce}", f"YIKES_RESULT_END_{nonce}")


def _marked_prompt(prompt: str, markers: ResultMarkers) -> str:
    return (
        f"{prompt}\n\n"
        "Return only the final answer wrapped by these exact result marker lines. "
        "The opening marker must be alone on its own line, then the answer, then the closing marker alone on its own line.\n"
        f"Opening marker: {markers.start}\n"
        f"Closing marker: {markers.end}"
    )


def _tmux_paste(socket_path: Path, session_name: str, text: str, *, cwd: Path) -> None:
    buffer_name = f"yikes-{uuid4().hex}"
    load = subprocess.run(
        ["tmux", "-S", str(socket_path), "load-buffer", "-b", buffer_name, "-"],
        input=text,
        text=True,
        capture_output=True,
        cwd=str(cwd),
        timeout=10,
        check=False,
    )
    if load.returncode != 0:
        raise BackendRunError("tmux load-buffer failed", stdout=load.stdout, stderr=load.stderr)
    paste = subprocess.run(
        ["tmux", "-S", str(socket_path), "paste-buffer", "-d", "-b", buffer_name, "-t", session_name],
        text=True,
        capture_output=True,
        cwd=str(cwd),
        timeout=10,
        check=False,
    )
    if paste.returncode != 0:
        raise BackendRunError("tmux paste-buffer failed", stdout=paste.stdout, stderr=paste.stderr)
    subprocess.run(["tmux", "-S", str(socket_path), "send-keys", "-t", session_name, "C-m"], cwd=str(cwd), timeout=10, check=False)


def _confirm_local_workspace_trust_if_needed(socket_path: Path, session_name: str, *, cwd: Path) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            screen = _capture_tmux(socket_path, session_name, cwd=cwd)
        except BackendRunError:
            return
        if _looks_like_workspace_trust_prompt(screen):
            subprocess.run(
                ["tmux", "-S", str(socket_path), "send-keys", "-t", session_name, "Enter"],
                cwd=str(cwd),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            _wait_until_local_trust_prompt_clears(socket_path, session_name, cwd=cwd)
            return
        time.sleep(0.25)


def _dismiss_local_codex_update_prompt_if_needed(socket_path: Path, session_name: str, *, cwd: Path) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            screen = _capture_tmux(socket_path, session_name, cwd=cwd)
        except BackendRunError:
            return
        if _looks_like_codex_update_prompt(screen):
            for key in ("Down", "Enter"):
                subprocess.run(
                    ["tmux", "-S", str(socket_path), "send-keys", "-t", session_name, key],
                    cwd=str(cwd),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            _wait_until_local_codex_update_prompt_clears(socket_path, session_name, cwd=cwd)
            return
        time.sleep(0.25)


def _confirm_local_codex_workspace_trust_if_needed(socket_path: Path, session_name: str, *, cwd: Path) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            screen = _capture_tmux(socket_path, session_name, cwd=cwd)
        except BackendRunError:
            return
        if _looks_like_codex_workspace_trust_prompt(screen):
            subprocess.run(
                ["tmux", "-S", str(socket_path), "send-keys", "-t", session_name, "Enter"],
                cwd=str(cwd),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            _wait_until_local_codex_workspace_trust_prompt_clears(socket_path, session_name, cwd=cwd)
            return
        time.sleep(0.25)


def _capture_tmux(socket_path: Path, session_name: str, *, cwd: Path) -> str:
    proc = subprocess.run(
        ["tmux", "-S", str(socket_path), "capture-pane", "-p", "-J", "-S", "-", "-E", "-", "-t", session_name],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        raise BackendRunError("tmux capture-pane failed", stdout=proc.stdout, stderr=proc.stderr)
    return _strip_ansi(proc.stdout)


def _wait_until_local_trust_prompt_clears(socket_path: Path, session_name: str, *, cwd: Path) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            screen = _capture_tmux(socket_path, session_name, cwd=cwd)
        except BackendRunError:
            return
        if not _looks_like_workspace_trust_prompt(screen):
            return
        time.sleep(0.25)


def _wait_until_local_codex_update_prompt_clears(socket_path: Path, session_name: str, *, cwd: Path) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            screen = _capture_tmux(socket_path, session_name, cwd=cwd)
        except BackendRunError:
            return
        if not _looks_like_codex_update_prompt(screen):
            return
        time.sleep(0.25)


def _wait_until_local_codex_workspace_trust_prompt_clears(socket_path: Path, session_name: str, *, cwd: Path) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            screen = _capture_tmux(socket_path, session_name, cwd=cwd)
        except BackendRunError:
            return
        if not _looks_like_codex_workspace_trust_prompt(screen):
            return
        time.sleep(0.25)


def _wait_for_tmux_result(socket_path: Path, session_name: str, *, markers: ResultMarkers, cwd: Path, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        screen = _capture_tmux(socket_path, session_name, cwd=cwd)
        if _has_marked_result(screen, markers):
            return screen
        if screen != last:
            last = screen
            stable_since = time.monotonic()
        time.sleep(0.5)
    raise BackendRunError(f"tmux interactive {session_name} turn did not complete within {timeout}s", stdout=last, stderr="")


def _has_marked_result(screen: str, markers: ResultMarkers) -> bool:
    return _result_pattern(markers).search(screen) is not None


def _extract_marked_result(screen: str, markers: ResultMarkers | None = None) -> str:
    if markers is not None:
        matches = list(_result_pattern(markers).finditer(screen))
        if matches:
            return matches[-1].group(1).strip()
    else:
        matches = list(re.finditer(r"(?m)^<YIKES_RESULT>\s*$(.*?)^</YIKES_RESULT>\s*$", screen, re.S))
        if matches:
            return matches[-1].group(1).strip()
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    return lines[-1] if lines else screen.strip()


def _result_pattern(markers: ResultMarkers) -> re.Pattern[str]:
    return re.compile(
        rf"(?m)^[^\S\r\n]*(?:[⏺●•]\s*)?{re.escape(markers.start)}[^\S\r\n]*$(.*?)"
        rf"^[^\S\r\n]*(?:[⏺●•]\s*)?{re.escape(markers.end)}[^\S\r\n]*$",
        re.S,
    )


def _container_tmux_session_alive(sandbox: SandboxSession, socket_path: Path, session_name: str) -> bool:
    result = sandbox.exec(
        ["tmux", "-S", str(socket_path), "has-session", "-t", session_name],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return result.returncode == 0


def _container_tmux_paste(sandbox: SandboxSession, socket_path: Path, session_name: str, text: str) -> None:
    buffer_name = f"yikes-{uuid4().hex}"
    load = sandbox.exec(
        ["tmux", "-S", str(socket_path), "load-buffer", "-b", buffer_name, "-"],
        input=text,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if load.returncode != 0:
        raise BackendRunError("container tmux load-buffer failed", stdout=str(load.stdout), stderr=str(load.stderr))
    paste = sandbox.exec(
        ["tmux", "-S", str(socket_path), "paste-buffer", "-d", "-b", buffer_name, "-t", session_name],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if paste.returncode != 0:
        raise BackendRunError("container tmux paste-buffer failed", stdout=str(paste.stdout), stderr=str(paste.stderr))
    sandbox.exec(["tmux", "-S", str(socket_path), "send-keys", "-t", session_name, "C-m"], capture_output=True, text=True, timeout=10, check=False)


def _confirm_container_workspace_trust_if_needed(sandbox: SandboxSession, socket_path: Path, session_name: str) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            screen = _capture_container_tmux(sandbox, socket_path, session_name)
        except BackendRunError:
            return
        if _looks_like_workspace_trust_prompt(screen):
            sandbox.exec(
                ["tmux", "-S", str(socket_path), "send-keys", "-t", session_name, "Enter"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            _wait_until_container_trust_prompt_clears(sandbox, socket_path, session_name)
            return
        time.sleep(0.25)


def _dismiss_container_codex_update_prompt_if_needed(sandbox: SandboxSession, socket_path: Path, session_name: str) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            screen = _capture_container_tmux(sandbox, socket_path, session_name)
        except BackendRunError:
            return
        if _looks_like_codex_update_prompt(screen):
            for key in ("Down", "Enter"):
                sandbox.exec(
                    ["tmux", "-S", str(socket_path), "send-keys", "-t", session_name, key],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            _wait_until_container_codex_update_prompt_clears(sandbox, socket_path, session_name)
            return
        time.sleep(0.25)


def _confirm_container_codex_workspace_trust_if_needed(sandbox: SandboxSession, socket_path: Path, session_name: str) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            screen = _capture_container_tmux(sandbox, socket_path, session_name)
        except BackendRunError:
            return
        if _looks_like_codex_workspace_trust_prompt(screen):
            sandbox.exec(
                ["tmux", "-S", str(socket_path), "send-keys", "-t", session_name, "Enter"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            _wait_until_container_codex_workspace_trust_prompt_clears(sandbox, socket_path, session_name)
            return
        time.sleep(0.25)


def _wait_until_container_trust_prompt_clears(sandbox: SandboxSession, socket_path: Path, session_name: str) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            screen = _capture_container_tmux(sandbox, socket_path, session_name)
        except BackendRunError:
            return
        if not _looks_like_workspace_trust_prompt(screen):
            return
        time.sleep(0.25)


def _wait_until_container_codex_update_prompt_clears(sandbox: SandboxSession, socket_path: Path, session_name: str) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            screen = _capture_container_tmux(sandbox, socket_path, session_name)
        except BackendRunError:
            return
        if not _looks_like_codex_update_prompt(screen):
            return
        time.sleep(0.25)


def _wait_until_container_codex_workspace_trust_prompt_clears(
    sandbox: SandboxSession,
    socket_path: Path,
    session_name: str,
) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            screen = _capture_container_tmux(sandbox, socket_path, session_name)
        except BackendRunError:
            return
        if not _looks_like_codex_workspace_trust_prompt(screen):
            return
        time.sleep(0.25)


def _looks_like_workspace_trust_prompt(screen: str) -> bool:
    return "Yes, I trust this folder" in screen and "Enter to confirm" in screen


def _looks_like_codex_workspace_trust_prompt(screen: str) -> bool:
    return "Do you trust the contents of this directory?" in screen and "Yes, continue" in screen


def _looks_like_codex_update_prompt(screen: str) -> bool:
    return "Update available!" in screen and "Skip" in screen and "Update now" in screen


def _capture_container_tmux(sandbox: SandboxSession, socket_path: Path, session_name: str) -> str:
    proc = sandbox.exec(
        ["tmux", "-S", str(socket_path), "capture-pane", "-p", "-J", "-S", "-", "-E", "-", "-t", session_name],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        raise BackendRunError("container tmux capture-pane failed", stdout=str(proc.stdout), stderr=str(proc.stderr))
    return _strip_ansi(str(proc.stdout))


def _wait_for_container_tmux_result(
    sandbox: SandboxSession,
    socket_path: Path,
    session_name: str,
    *,
    markers: ResultMarkers,
    timeout: float,
) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        screen = _capture_container_tmux(sandbox, socket_path, session_name)
        if _has_marked_result(screen, markers):
            return screen
        if screen != last:
            last = screen
            stable_since = time.monotonic()
        time.sleep(0.5)
    raise BackendRunError(f"container tmux interactive {session_name} turn did not complete within {timeout}s", stdout=last, stderr="")


def _tmux_session_alive(socket_path: Path, session_name: str, cwd: Path) -> bool:
    proc = subprocess.run(
        ["tmux", "-S", str(socket_path), "has-session", "-t", session_name],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    return proc.returncode == 0


def _kill_tmux(socket_path: Path, cwd: Path) -> None:
    try:
        run_process(["tmux", "-S", str(socket_path), "kill-server"], cwd=cwd, timeout=5)
    except Exception:
        pass


def _ask_remote_control(
    backend: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: float,
    model: str | None,
    settings: AgentSettings,
) -> str:
    if backend == "codex":
        return _ask_codex_remote_control(prompt, cwd=cwd, timeout=timeout, model=model, settings=settings)
    if backend == "claude":
        raise DriverUnavailable(
            "Claude remote-control is not a supported Yikes driver. Use direct or tmux "
            "for Claude chat, and use the future remote-server runtime for remote Yikes sessions."
        )
    raise DriverUnavailable(f"unknown backend: {backend}")


def _ask_codex_remote_control(
    prompt: str,
    *,
    cwd: Path,
    timeout: float,
    model: str | None,
    settings: AgentSettings,
) -> str:
    # Codex's remote-control surface is app-server over websocket. We try the
    # websocket JSON-RPC protocol first and only fall back to exec if the local
    # Codex build has incompatible experimental websocket framing.
    require_binary("codex")
    port = _free_loopback_port()
    with tempfile.TemporaryDirectory(prefix="yikes-codex-remote-") as tmp:
        log = Path(tmp) / "app-server.log"
        proc = None
        try:
            proc = subprocess.Popen(
                [
                    "codex",
                    "app-server",
                    "--listen",
                    f"ws://127.0.0.1:{port}",
                ],
                cwd=str(cwd),
                stdout=log.open("w", encoding="utf-8"),
                stderr=subprocess.STDOUT,
                text=True,
            )
            _wait_for_port("127.0.0.1", port, timeout=min(15.0, timeout))
            try:
                return asyncio.run(
                    _codex_ws_turn(
                        f"ws://127.0.0.1:{port}",
                        prompt,
                        cwd=cwd,
                        timeout=timeout,
                        model=model,
                        settings=settings,
                    )
                )
            except Exception:
                if os.environ.get("YIKES_CODEX_REMOTE_STRICT") == "1":
                    raise
                return _ask_codex_exec(prompt, cwd=cwd, timeout=timeout, model=model, settings=settings)
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _codex_ws_turn(
    url: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: float,
    model: str | None,
    settings: AgentSettings,
) -> str:
    import websockets

    next_id = 0

    async def request(ws, method: str, params: dict) -> dict:
        nonlocal next_id
        req_id = next_id
        next_id += 1
        await ws.send(json.dumps({"id": req_id, "method": method, "params": params}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise BackendRunError(f"codex app-server error for {method}: {msg['error']}")
                return msg.get("result") or {}

    async with websockets.connect(url, open_timeout=min(timeout, 15.0)) as ws:
        await request(
            ws,
            "initialize",
            {
                "clientInfo": {"name": "yikes", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await ws.send(json.dumps({"method": "initialized", "params": {}}))
        start = await request(
            ws,
            "thread/start",
            {
                "cwd": str(cwd),
                "approvalPolicy": "never",
                "sandbox": _codex_sandbox(settings),
                "mcpServers": _mcp_payload(settings),
                "ephemeral": True,
                "model": model,
            },
        )
        thread = start.get("thread") or {}
        thread_id = thread.get("id") or start.get("threadId")
        if not thread_id:
            raise BackendRunError(f"codex thread/start response did not include a thread id: {start!r}")
        await ws.send(
            json.dumps(
                {
                    "id": next_id,
                    "method": "turn/start",
                    "params": {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": prompt}],
                        "cwd": str(cwd),
                        "approvalPolicy": "never",
                        "sandboxPolicy": _codex_sandbox_policy(settings),
                        "mcpServers": _mcp_payload(settings),
                        "model": model,
                    },
                }
            )
        )
        turn_req_id = next_id
        next_id += 1
        chunks: list[str] = []
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("id") == turn_req_id and "error" in msg:
                raise BackendRunError(f"codex turn/start failed: {msg['error']}")
            method = msg.get("method")
            params = msg.get("params") or {}
            if method == "item/agentMessage/delta":
                chunks.append(params.get("delta", ""))
            elif method == "turn/completed":
                return "".join(chunks).strip()
            elif method == "error":
                raise BackendRunError(f"codex app-server error notification: {params}")


def _wait_for_port(host: str, port: int, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.1)
    raise DriverUnavailable(f"remote-control endpoint did not open on {host}:{port}: {last_error}")


def _codex_sandbox(settings: AgentSettings) -> str:
    return "workspace-write" if settings.write_roots else "read-only"


def _codex_sandbox_policy(settings: AgentSettings) -> dict[str, str]:
    return {"type": "workspaceWrite"} if settings.write_roots else {"type": "readOnly"}


def _mcp_payload(settings: AgentSettings) -> dict[str, dict[str, object]]:
    payload: dict[str, dict[str, object]] = {}
    for server in settings.mcp_servers:
        if not server.enabled:
            continue
        if server.command == "sse" and server.args:
            payload[server.name] = {"type": "sse", "url": server.args[0], "enabled": True}
            continue
        if server.command.startswith(("http://", "https://")):
            payload[server.name] = {"type": "sse", "url": server.command, "enabled": True}
            continue
        payload[server.name] = {
            "command": server.command,
            "args": list(server.args),
            "enabled": True,
        }
    return payload


@contextmanager
def _temporary_claude_mcp_config(settings: AgentSettings):
    if not settings.mcp_servers:
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="yikes-claude-mcp-") as tmp:
        path = Path(tmp) / "mcp.json"
        path.write_text(json.dumps({"mcpServers": _mcp_payload(settings)}), encoding="utf-8")
        yield path


def _settings_for_docker(settings: AgentSettings, proxy_manager: ProxyManager) -> AgentSettings:
    mcp_config = McpConfig(
        {
            server.name: McpServerConfig(
                command=server.command,
                args=server.args,
                scope="auto",
            )
            for server in settings.mcp_servers
            if server.enabled
        }
    )
    direct_servers, proxied_servers = resolve_servers(mcp_config, container_mode=True)
    proxy_urls = proxy_manager.start(proxied_servers, container_mode=True)
    docker_mcps = tuple(
        McpServer(name, config.command, config.args)
        for name, config in direct_servers.items()
    ) + tuple(
        McpServer(name, "sse", (url,))
        for name, url in sorted(proxy_urls.items())
    )
    return AgentSettings(
        web_search_enabled=settings.web_search_enabled,
        read_roots=settings.read_roots,
        write_roots=settings.write_roots,
        mcp_servers=docker_mcps,
    )


def _docker_secret_env() -> dict[str, str]:
    names = (
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
    )
    env = {name: value for name in names if (value := os.environ.get(name))}
    if "ANTHROPIC_API_KEY" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env:
        credential = ClaudeCredentialProvider().get("claude")
        if credential is not None:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = credential.value
    return env


def _prepare_container_auth(sandbox: SandboxSession, backend: str) -> None:
    if backend == "codex":
        credential = CodexCredentialProvider().get("codex")
        sandbox.exec(
            ["sh", "-lc", "mkdir -p /workspace/home/.codex && chmod 700 /workspace/home/.codex"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        if credential is not None:
            sandbox.write_file("/workspace/home/.codex/auth.json", credential.value)
        sandbox.write_file("/workspace/home/.codex/version.json", _codex_dismissed_version_json())
        sandbox.exec(
            ["sh", "-lc", "chmod 600 /workspace/home/.codex/auth.json /workspace/home/.codex/version.json 2>/dev/null || true"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )


def _prepare_local_codex_home(tmux_dir: Path, label: str) -> Path:
    source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    target = tmux_dir / f"{label}-codex-home"
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)
    for name in ("auth.json", "config.toml"):
        source_path = source / name
        if source_path.exists():
            shutil.copy2(source_path, target / name)
    (target / "version.json").write_text(_codex_dismissed_version_json(), encoding="utf-8")
    return target


def _codex_dismissed_version_json() -> str:
    source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser() / "version.json"
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        payload = {}
    latest = str(payload.get("latest_version") or "")
    if latest:
        payload["dismissed_version"] = latest
    return json.dumps(payload or {"dismissed_version": ""}) + "\n"


def _docker_mounts(cwd: Path, settings: AgentSettings) -> tuple[tuple[tuple[str, str, str], ...], AgentSettings]:
    host_cwd = cwd.expanduser().resolve()
    mounts: list[tuple[str, str, str]] = [(str(host_cwd), "/workspace/project", "rw" if _path_in(host_cwd, settings.write_roots) else "ro")]
    read_roots: list[Path] = [Path("/workspace/project")]
    write_roots: list[Path] = [Path("/workspace/project")] if mounts[0][2] == "rw" else []
    for index, root in enumerate(settings.read_roots):
        host = root.expanduser().resolve()
        if host == host_cwd:
            continue
        target = Path(f"/workspace/read-{index}")
        mounts.append((str(host), str(target), "ro"))
        read_roots.append(target)
    for index, root in enumerate(settings.write_roots):
        host = root.expanduser().resolve()
        if host == host_cwd:
            continue
        target = Path(f"/workspace/write-{index}")
        mounts.append((str(host), str(target), "rw"))
        write_roots.append(target)
    return tuple(mounts), AgentSettings(
        web_search_enabled=settings.web_search_enabled,
        tmux_enabled=settings.tmux_enabled,
        read_roots=tuple(read_roots),
        write_roots=tuple(write_roots),
        mcp_servers=settings.mcp_servers,
    )


def _container_ephemeral_settings(settings: AgentSettings, workspace: Path) -> AgentSettings:
    return AgentSettings(
        web_search_enabled=settings.web_search_enabled,
        tmux_enabled=settings.tmux_enabled,
        read_roots=(workspace,),
        write_roots=(workspace,),
        mcp_servers=settings.mcp_servers,
    )


def _sandbox_workspace(sandbox: SandboxSession) -> str:
    return sandbox.meta.user_data.get("workspace") or "/workspace/project"


def _path_in(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.expanduser().resolve()
    for root in roots:
        try:
            resolved.relative_to(root.expanduser().resolve())
            return True
        except ValueError:
            continue
    return False


def _docker_label(backend: str, cwd: Path, mounts: tuple[tuple[str, str, str], ...]) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(cwd.resolve()))[-80:]
    digest = hashlib.sha256(repr(mounts).encode("utf-8")).hexdigest()[:10]
    return f"yikes-{backend}-{safe}-{digest}"


def _extract_backend_output(backend: str, text: str) -> str:
    if backend == "claude":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        value = data.get("result")
        return value if isinstance(value, str) else text
    return text


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _looks_like_auth_error(text: str) -> bool:
    lower = text.lower()
    needles = (
        "not logged in",
        "please run /login",
        "login required",
        "unauthorized",
        "authentication",
        "auth",
    )
    return any(needle in lower for needle in needles)


def _extract_error_text(text: str) -> str:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _strip_ansi(text).strip()
    result = data.get("result")
    if isinstance(result, str):
        return result
    return _strip_ansi(text).strip()
