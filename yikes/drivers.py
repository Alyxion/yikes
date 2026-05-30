from __future__ import annotations

import json
import os
import re
import shutil
import shlex
import secrets
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

from .agent_driver import AgentDriver, DriverRequest
from .domain import AgentSettings, Backend, ChatOptions, Driver, DriverMode, McpServer
from .domain import ImageAttachment
from .errors import BackendRunError, BackendUnavailable, DriverUnavailable
from .attachments import prompt_with_image_references, prompt_with_mapped_image_references
from .credentials import ClaudeCredentialProvider, CodexCredentialProvider
from .mcp import McpConfig, McpServerConfig, resolve_servers
from .mcp_proxy import ProxyManager
from .process import require_binary, run_process
from .prompt_profile import load_prompt_profile
from .runtime import DurableSessionManager, RuntimeKind, RuntimeRef, SessionState
from .sandbox import DEFAULT_IMAGE, SandboxConfig, SandboxManager, SandboxSession
from .tmux_io_log import log_tmux_io

if False:  # pragma: no cover
    from .chatbot import Backend, Driver


@dataclass(frozen=True)
class ResultMarkers:
    start: str
    end: str


class TmuxRuntime:
    """Own local tmux session orchestration for interactive backend UIs."""

    def ask(
        self,
        backend: str,
        prompt: str,
        *,
        cwd: Path,
        cwd_explicit: bool,
        timeout: float,
        model: str | None,
        session_id: str | None,
        settings: AgentSettings,
        attachments: tuple[ImageAttachment, ...] = (),
    ) -> str:
        require_binary("tmux")
        socket_path, session_name = self.ensure_session(
            backend,
            cwd,
            cwd_explicit=cwd_explicit,
            model=model,
            settings=settings,
            session_id=session_id,
        )
        self.prepare_session(backend, socket_path, session_name, cwd=cwd, cwd_explicit=cwd_explicit)
        if not settings.managed_output_enabled:
            self.send_prompt(
                socket_path,
                session_name,
                prompt_with_image_references(prompt, attachments),
                cwd=cwd,
                backend=backend,
            )
            return ""
        markers = _session_result_markers(session_id or session_name)
        if session_id:
            _record_local_capture_markers(session_id, markers)
        setup_turn = _prompt_refreshes_guidance(prompt)
        baseline = self.result_count(socket_path, session_name, markers=markers, cwd=cwd)
        self.send_prompt(
            socket_path,
            session_name,
            _marked_prompt(
                prompt_with_image_references(prompt, attachments),
                markers,
                include_instruction=setup_turn,
            ),
            cwd=cwd,
            backend=backend,
        )
        screen = self.wait_for_result(
            socket_path,
            session_name,
            markers=markers,
            cwd=cwd,
            timeout=timeout,
            min_count=baseline + 1,
        )
        return _extract_marked_result(screen, markers)

    def ensure_session(
        self,
        backend: str,
        cwd: Path,
        *,
        cwd_explicit: bool,
        model: str | None,
        settings: AgentSettings,
        session_id: str | None,
    ) -> tuple[Path, str]:
        return _ensure_local_tmux_session(
            backend,
            cwd,
            model=model,
            settings=settings,
            session_id=session_id,
            cwd_explicit=cwd_explicit,
        )

    def prepare_session(
        self,
        backend: str,
        socket_path: Path,
        session_name: str,
        *,
        cwd: Path,
        cwd_explicit: bool,
    ) -> None:
        if backend == "claude" and not cwd_explicit:
            _confirm_local_workspace_trust_if_needed(socket_path, session_name, cwd=cwd)
        if backend == "codex":
            if not cwd_explicit:
                _confirm_local_codex_workspace_trust_if_needed(socket_path, session_name, cwd=cwd)
            _dismiss_local_codex_update_prompt_if_needed(socket_path, session_name, cwd=cwd)

    def send_prompt(self, socket_path: Path, session_name: str, text: str, *, cwd: Path, backend: str) -> None:
        _tmux_paste(socket_path, session_name, text, cwd=cwd, backend=backend)

    def wait_for_result(
        self,
        socket_path: Path,
        session_name: str,
        *,
        markers: ResultMarkers,
        cwd: Path,
        timeout: float,
        min_count: int = 1,
    ) -> str:
        return _wait_for_tmux_result(
            socket_path,
            session_name,
            markers=markers,
            cwd=cwd,
            timeout=timeout,
            min_count=min_count,
        )

    def result_count(self, socket_path: Path, session_name: str, *, markers: ResultMarkers, cwd: Path) -> int:
        try:
            return _marked_result_count(_capture_tmux(socket_path, session_name, cwd=cwd), markers)
        except BackendRunError:
            return 0


def ensure_interactive_session(options: ChatOptions) -> None:
    """Start the real interactive TUI for tmux-backed sessions without sending a prompt."""

    if options.mode is not DriverMode.TMUX:
        return
    if options.driver is Driver.TMUX:
        require_binary("tmux")
        socket_path, session_name = _TMUX_RUNTIME.ensure_session(
            options.backend.value,
            options.cwd,
            cwd_explicit=options.cwd_explicit,
            model=options.model,
            settings=options.settings,
            session_id=options.session_id,
        )
        _TMUX_RUNTIME.prepare_session(
            options.backend.value,
            socket_path,
            session_name,
            cwd=options.cwd,
            cwd_explicit=options.cwd_explicit,
        )
        return
    if options.driver is Driver.DOCKER and options.settings.tmux_enabled:
        require_binary("docker")
        proxy_manager = ProxyManager()
        try:
            docker_settings = _settings_for_docker(options.settings, proxy_manager)
            sandbox, docker_settings = _docker_session_for(
                options.backend.value,
                options.cwd,
                docker_settings,
                cwd_explicit=options.cwd_explicit,
                session_id=options.session_id,
                use_tmux=True,
            )
            _ensure_docker_tmux_session(
                sandbox,
                options.backend.value,
                model=options.model,
            )
        finally:
            proxy_manager.stop()


class DockerRuntime:
    """Own Docker sandbox orchestration for CLI and tmux-backed sessions."""

    def ask(
        self,
        backend: str,
        prompt: str,
        *,
        cwd: Path,
        cwd_explicit: bool,
        timeout: float,
        model: str | None,
        session_id: str | None,
        settings: AgentSettings,
        attachments: tuple[ImageAttachment, ...] = (),
    ) -> str:
        require_binary("docker")
        proxy_manager = ProxyManager()
        try:
            docker_settings = _settings_for_docker(settings, proxy_manager)
            sandbox, docker_settings = self.ensure_session(
                backend,
                cwd,
                docker_settings,
                cwd_explicit=cwd_explicit,
                session_id=session_id or uuid4().hex,
                use_tmux=settings.tmux_enabled,
            )
            if settings.tmux_enabled:
                return self.ask_tmux(
                    sandbox,
                    backend,
                    prompt,
                    timeout=timeout,
                    model=model,
                    settings=docker_settings,
                    attachments=attachments,
                )
            return self.ask_cli(
                sandbox,
                backend,
                prompt,
                timeout=timeout,
                model=model,
                settings=docker_settings,
                attachments=attachments,
            )
        finally:
            proxy_manager.stop()

    def ensure_session(
        self,
        backend: str,
        cwd: Path,
        settings: AgentSettings,
        *,
        cwd_explicit: bool,
        session_id: str,
        use_tmux: bool,
    ) -> tuple[SandboxSession, AgentSettings]:
        return _docker_session_for(
            backend,
            cwd,
            settings,
            cwd_explicit=cwd_explicit,
            session_id=session_id,
            use_tmux=use_tmux,
        )

    def ask_tmux(
        self,
        sandbox: SandboxSession,
        backend: str,
        prompt: str,
        *,
        timeout: float,
        model: str | None,
        settings: AgentSettings,
        attachments: tuple[ImageAttachment, ...],
    ) -> str:
        return _ask_docker_tmux(
            sandbox,
            backend,
            prompt,
            timeout=timeout,
            model=model,
            settings=settings,
            attachments=attachments,
        )

    def ask_cli(
        self,
        sandbox: SandboxSession,
        backend: str,
        prompt: str,
        *,
        timeout: float,
        model: str | None,
        settings: AgentSettings,
        attachments: tuple[ImageAttachment, ...],
    ) -> str:
        return _ask_inside_sandbox(
            sandbox,
            backend,
            prompt,
            timeout=timeout,
            model=model,
            settings=settings,
            attachments=attachments,
        )


class DirectRuntime:
    """Own direct non-tmux CLI execution."""

    def ask(
        self,
        backend: str,
        prompt: str,
        *,
        cwd: Path,
        timeout: float,
        model: str | None,
        settings: AgentSettings,
        attachments: tuple[ImageAttachment, ...] = (),
    ) -> str:
        if backend == "claude":
            return _ask_claude_direct(prompt, cwd=cwd, timeout=timeout, model=model, settings=settings, attachments=attachments)
        if backend == "codex":
            return _ask_codex_exec(prompt, cwd=cwd, timeout=timeout, model=model, settings=settings, attachments=attachments)
        raise DriverUnavailable(f"unknown backend: {backend}")


class RemoteRuntime:
    """Own the legacy local remote-control driver surface."""

    def ask(
        self,
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
                "Claude remote-control is not a supported yikes! driver. Use direct or tmux "
                "for Claude chat, and use the future remote-server runtime for remote yikes! sessions."
            )
        raise DriverUnavailable(f"unknown backend: {backend}")


class DirectAgentDriver:
    def ask(self, request: DriverRequest) -> str:
        return _DIRECT_RUNTIME.ask(
            request.backend.value,
            request.prompt,
            cwd=request.cwd,
            timeout=request.timeout,
            model=request.model,
            settings=request.settings,
            attachments=request.attachments,
        )


class TmuxAgentDriver:
    def ask(self, request: DriverRequest) -> str:
        return _TMUX_RUNTIME.ask(
            request.backend.value,
            request.prompt,
            cwd=request.cwd,
            cwd_explicit=request.cwd_explicit,
            timeout=request.timeout,
            model=request.model,
            session_id=request.session_id,
            settings=request.settings,
            attachments=request.attachments,
        )


class DockerAgentDriver:
    def ask(self, request: DriverRequest) -> str:
        return _DOCKER_RUNTIME.ask(
            request.backend.value,
            request.prompt,
            cwd=request.cwd,
            cwd_explicit=request.cwd_explicit,
            timeout=request.timeout,
            model=request.model,
            session_id=request.session_id,
            settings=request.settings,
            attachments=request.attachments,
        )


class RemoteControlAgentDriver:
    def ask(self, request: DriverRequest) -> str:
        return _REMOTE_RUNTIME.ask(
            request.backend.value,
            request.prompt,
            cwd=request.cwd,
            timeout=request.timeout,
            model=request.model,
            settings=request.settings,
        )


_TMUX_RUNTIME = TmuxRuntime()
_DOCKER_RUNTIME = DockerRuntime()
_DIRECT_RUNTIME = DirectRuntime()
_REMOTE_RUNTIME = RemoteRuntime()


_AGENT_DRIVERS: dict[Driver, AgentDriver] = {
    Driver.DIRECT: DirectAgentDriver(),
    Driver.TMUX: TmuxAgentDriver(),
    Driver.DOCKER: DockerAgentDriver(),
    Driver.REMOTE_CONTROL: RemoteControlAgentDriver(),
}


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
    attachments: tuple[ImageAttachment, ...] = (),
) -> str:
    try:
        selected = Driver(driver)
        selected_backend = Backend(backend)
    except ValueError as exc:
        raise DriverUnavailable(f"unknown driver/backend: {driver}/{backend}") from exc
    agent_driver = _AGENT_DRIVERS.get(selected)
    if agent_driver is None:
        raise DriverUnavailable(f"unknown driver: {driver}")
    return agent_driver.ask(
        DriverRequest(
            backend=selected_backend,
            prompt=prompt,
            cwd=cwd,
            cwd_explicit=cwd_explicit,
            timeout=timeout,
            model=model,
            settings=settings,
            session_id=session_id,
            attachments=attachments,
        )
    )


def _ask_direct(
    backend: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: float,
    model: str | None,
    settings: AgentSettings,
    attachments: tuple[ImageAttachment, ...] = (),
) -> str:
    return _DIRECT_RUNTIME.ask(
        backend,
        prompt,
        cwd=cwd,
        timeout=timeout,
        model=model,
        settings=settings,
        attachments=attachments,
    )


def _ask_claude_direct(
    prompt: str,
    *,
    cwd: Path,
    timeout: float,
    model: str | None,
    settings: AgentSettings,
    attachments: tuple[ImageAttachment, ...] = (),
) -> str:
    require_binary("claude")
    prompt = prompt_with_image_references(prompt, attachments)
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
    attachments: tuple[ImageAttachment, ...] = (),
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
        for attachment in attachments:
            argv.extend(["--image", str(attachment.path)])
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
    session_id: str | None,
    settings: AgentSettings,
    attachments: tuple[ImageAttachment, ...] = (),
) -> str:
    return _TMUX_RUNTIME.ask(
        backend,
        prompt,
        cwd=cwd,
        cwd_explicit=cwd_explicit,
        timeout=timeout,
        model=model,
        session_id=session_id,
        settings=settings,
        attachments=attachments,
    )


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
    attachments: tuple[ImageAttachment, ...] = (),
) -> str:
    return _DOCKER_RUNTIME.ask(
        backend,
        prompt,
        cwd=cwd,
        cwd_explicit=cwd_explicit,
        timeout=timeout,
        model=model,
        session_id=session_id,
        settings=settings,
        attachments=attachments,
    )


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
    mode = "tmux" if use_tmux else "cli"
    label = f"docker-{mode}-{backend}-{session_id[:12]}"
    if not cwd_explicit:
        container_workspace = Path(f"/workspace/session-{session_id[:12]}")
        mounts: tuple[tuple[str, str, str], ...] = ()
        container_settings = _container_ephemeral_settings(settings, container_workspace)
    else:
        container_workspace = Path("/workspace/project")
        mounts, container_settings = _docker_mounts(cwd, settings)
    existing = manager.find_running(image=image, label=label)
    if existing is not None:
        if existing.meta.user_data.get("logical_session_id") != session_id:
            existing.meta.user_data["logical_session_id"] = session_id
        existing.meta.user_data["managed_output_enabled"] = "true" if settings.managed_output_enabled else "false"
        existing._save()
        return existing, container_settings
    server_token = secrets.token_urlsafe(32)
    secret_env = _docker_secret_env() | {"YIKES_SERVER_TOKEN": server_token}
    ports = _resolve_host_ports(settings.docker_ports)
    return manager.create(
        SandboxConfig(
            image=image,
            mounts=mounts,
            ports=ports,
            env={"DISABLE_AUTOUPDATER": "1"},
            secret_env=secret_env,
        ),
        user_data={
            "label": label,
            "backend": backend,
            "logical_session_id": session_id,
            "cwd": str(cwd),
            "cwd_explicit": "true" if cwd_explicit else "false",
            "workspace": str(container_workspace),
            "server_port": "8989",
            "published_ports": ",".join(f"{host}:{container}" for host, container in ports),
            "managed_output_enabled": "true" if settings.managed_output_enabled else "false",
        },
    ), container_settings


def _resolve_host_ports(
    ports: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """Keep requested host ports when free; fall back to an ephemeral free port.

    Concurrent isolated sessions in different projects can request the same host
    port; rather than failing the ``docker run``, the second session is mapped to
    an open ephemeral port so it still comes up.
    """
    resolved: list[tuple[str, str]] = []
    taken: set[int] = set()
    for host_port, container_port in ports:
        chosen = _claim_host_port(host_port, taken)
        resolved.append((str(chosen), container_port))
        taken.add(chosen)
    return tuple(resolved)


def _claim_host_port(host_port: str, taken: set[int]) -> int:
    try:
        desired = int(host_port)
    except ValueError:
        desired = 0
    if desired and desired not in taken and _host_port_free(desired):
        return desired
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _host_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _ask_docker_tmux(
    sandbox: SandboxSession,
    backend: str,
    prompt: str,
    *,
    timeout: float,
    model: str | None,
    settings: AgentSettings,
    attachments: tuple[ImageAttachment, ...] = (),
) -> str:
    socket_path, session_name = _ensure_docker_tmux_session(sandbox, backend, model=model)
    mapped_attachments = _copy_attachments_to_sandbox(sandbox, attachments)
    if not settings.managed_output_enabled:
        _container_tmux_paste(
            sandbox,
            socket_path,
            session_name,
            prompt_with_mapped_image_references(prompt, mapped_attachments),
            backend=backend,
        )
        return ""
    markers = _session_result_markers(sandbox.id)
    _record_sandbox_capture_markers(sandbox, markers)
    setup_turn = _prompt_refreshes_guidance(prompt)
    baseline = _marked_result_count(_capture_container_tmux(sandbox, socket_path, session_name), markers)
    _container_tmux_paste(
        sandbox,
        socket_path,
        session_name,
        _marked_prompt(
            prompt_with_mapped_image_references(prompt, mapped_attachments),
            markers,
            include_instruction=setup_turn,
        ),
        backend=backend,
    )
    screen = _wait_for_container_tmux_result(
        sandbox,
        socket_path,
        session_name,
        markers=markers,
        timeout=timeout,
        min_count=baseline + 1,
    )
    return _extract_marked_result(screen, markers)


def _ask_inside_sandbox(
    sandbox: SandboxSession,
    backend: str,
    prompt: str,
    *,
    timeout: float,
    model: str | None,
    settings: AgentSettings,
    attachments: tuple[ImageAttachment, ...] = (),
) -> str:
    turn_id = uuid4().hex
    _prepare_container_auth(sandbox, backend)
    result_path = Path(f"/tmp/yikes-result-{turn_id}.txt")
    err_path = Path(f"/tmp/yikes-stderr-{turn_id}.txt")
    mcp_config = Path(f"/tmp/yikes-mcp-{turn_id}.json") if backend == "claude" and settings.mcp_servers else None
    mapped_attachments = _copy_attachments_to_sandbox(sandbox, attachments)
    if mcp_config is not None:
        sandbox.write_file(str(mcp_config), json.dumps({"mcpServers": _mcp_payload(settings)}))
    workspace = _sandbox_workspace(sandbox)
    sandbox.exec(["sh", "-lc", f"mkdir -p {shlex.quote(workspace)}"], capture_output=True, text=True, timeout=10, check=True)
    command = _backend_shell_command(
        backend,
        prompt,
        result_path=result_path,
        err_path=err_path,
        model=model,
        settings=settings,
        mcp_config_path=mcp_config,
        attachments=mapped_attachments,
    )
    command = f"cd {shlex.quote(workspace)} && {command}"
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


def _copy_attachments_to_sandbox(
    sandbox: SandboxSession,
    attachments: tuple[ImageAttachment, ...],
) -> tuple[Path, ...]:
    if not attachments:
        return ()
    target_dir = Path("/workspace/yikes-attachments")
    sandbox.exec(["sh", "-lc", f"mkdir -p {shlex.quote(str(target_dir))}"], capture_output=True, text=True, timeout=10, check=True)
    mapped: list[Path] = []
    for attachment in attachments:
        target = target_dir / f"{uuid4().hex[:12]}-{attachment.path.name}"
        sandbox.write_file(str(target), attachment.path.read_bytes())
        mapped.append(target)
    return tuple(mapped)


def _backend_shell_command(
    backend: str,
    prompt: str,
    *,
    result_path: Path,
    err_path: Path,
    model: str | None,
    settings: AgentSettings,
    mcp_config_path: Path | None = None,
    attachments: tuple[Path, ...] = (),
) -> str:
    if backend == "claude":
        prompt = prompt_with_mapped_image_references(prompt, attachments)
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
        for attachment in attachments:
            argv.extend(["--image", str(attachment)])
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
    session_id: str | None = None,
    cwd_explicit: bool = True,
) -> tuple[Path, str]:
    existing = _existing_local_tmux_session(session_id, backend=backend, cwd=cwd)
    if existing is not None:
        return existing
    tmux_dir = Path(os.environ.get("YIKES_TMUX_DIR", str(Path.home() / ".yikes" / "tmux"))).expanduser()
    tmux_dir.mkdir(parents=True, exist_ok=True)
    tmux_dir.chmod(0o700)
    label = _tmux_label(backend, cwd, model, session_id=session_id)
    socket_path = tmux_dir / f"{label}.sock"
    session_name = label
    if not _tmux_session_alive(socket_path, session_name, cwd):
        _start_tmux_server(socket_path, cwd)
        _set_tmux_options(socket_path, cwd)
        codex_home = _prepare_local_codex_home(
            tmux_dir,
            label,
            trusted_cwd=cwd if not cwd_explicit else None,
        ) if backend == "codex" else None
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
    _record_tmux_session(
        backend,
        cwd,
        socket_path,
        session_name,
        model=model,
        settings=settings,
        label=label,
        session_id=session_id,
        cwd_explicit=cwd_explicit,
    )
    return socket_path, session_name


def _existing_local_tmux_session(session_id: str | None, *, backend: str, cwd: Path) -> tuple[Path, str] | None:
    if not session_id:
        return None
    try:
        meta = DurableSessionManager().get(session_id)
    except OSError:
        return None
    if meta is None or meta.backend.value != backend or meta.runtime.kind is not RuntimeKind.TMUX:
        return None
    socket = meta.runtime.tmux_socket
    session = meta.runtime.tmux_session
    if not socket or not session:
        return None
    socket_path = Path(socket)
    if _tmux_session_alive(socket_path, session, cwd):
        return socket_path, session
    return None


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
        ["set", "-g", "remain-on-exit", "on"],
        ["set", "-g", "history-limit", "100000"],
        ["set", "-g", "extended-keys", "off"],
    ):
        subprocess.run(["tmux", "-S", str(socket_path), *args], cwd=str(cwd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def _start_tmux_server(socket_path: Path, cwd: Path) -> None:
    subprocess.run(
        ["tmux", "-S", str(socket_path), "-f", "/dev/null", "start-server"],
        cwd=str(cwd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )


def _record_tmux_session(
    backend: str,
    cwd: Path,
    socket_path: Path,
    session_name: str,
    *,
    model: str | None,
    settings: AgentSettings,
    label: str,
    session_id: str | None = None,
    cwd_explicit: bool = True,
) -> None:
    try:
        manager = DurableSessionManager()
        if session_id:
            existing = manager.get(session_id)
            if existing is not None:
                existing.runtime = RuntimeRef(RuntimeKind.TMUX, tmux_socket=str(socket_path), tmux_session=session_name)
                existing.backend = Backend(backend)
                existing.driver = Driver.TMUX
                existing.cwd = cwd
                existing.model = model
                existing.settings = settings
                existing.state = SessionState.RUNNING
                existing.user_data = existing.user_data | {
                    "label": label,
                    "attach": f"tmux -S {socket_path} attach -t {session_name}",
                    "cwd_explicit": "true" if cwd_explicit else "false",
                }
                manager.save(existing)
                return
        for meta in manager.list():
            if meta.runtime.kind is RuntimeKind.TMUX and meta.user_data.get("label") == label:
                meta.state = SessionState.RUNNING
                meta.settings = settings
                meta.model = model
                meta.runtime = RuntimeRef(RuntimeKind.TMUX, tmux_socket=str(socket_path), tmux_session=session_name)
                meta.user_data = meta.user_data | {"cwd_explicit": "true" if cwd_explicit else "false"}
                manager.save(meta)
                return
        meta = manager.create(
            backend=Backend(backend),
            driver=Driver.TMUX,
            runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket=str(socket_path), tmux_session=session_name),
            cwd=cwd,
            session_id=session_id,
            model=model,
            settings=settings,
            user_data={
                "label": label,
                "attach": f"tmux -S {socket_path} attach -t {session_name}",
                "cwd_explicit": "true" if cwd_explicit else "false",
            },
        )
        meta.state = SessionState.RUNNING
        manager.save(meta)
    except OSError:
        return


def _record_local_capture_markers(session_id: str, markers: ResultMarkers) -> None:
    try:
        manager = DurableSessionManager()
        meta = manager.get(session_id)
        if meta is None:
            return
        meta.user_data = meta.user_data | {
            "capture_start": markers.start,
            "capture_end": markers.end,
        }
        manager.save(meta)
    except OSError:
        return


def _record_sandbox_capture_markers(sandbox: SandboxSession, markers: ResultMarkers) -> None:
    sandbox.meta.user_data["capture_start"] = markers.start
    sandbox.meta.user_data["capture_end"] = markers.end
    sandbox._save()


def _tmux_label(backend: str, cwd: Path, model: str | None, *, session_id: str | None = None) -> str:
    identity = session_id or str(cwd.resolve())
    digest = hashlib.sha256(f"{backend}:{identity}:{model or ''}".encode()).hexdigest()[:12]
    return f"yikes-{backend}-{digest}"


def _result_markers() -> ResultMarkers:
    nonce = uuid4().hex[:12]
    start, end = load_prompt_profile().markers(nonce)
    return ResultMarkers(start, end)


def _session_result_markers(session_key: str) -> ResultMarkers:
    nonce = hashlib.sha256(f"{session_key}:result-markers".encode()).hexdigest()[:12]
    start, end = load_prompt_profile().markers(nonce)
    return ResultMarkers(start, end)


def _prompt_refreshes_guidance(prompt: str) -> bool:
    return "Runtime configuration:" in prompt


def _marked_prompt(prompt: str, markers: ResultMarkers, *, include_instruction: bool = True) -> str:
    if not include_instruction:
        return prompt
    instruction = load_prompt_profile().boundary_instruction(start=markers.start, end=markers.end)
    return (
        f"{prompt}\n\n"
        "Use these answer bounds for replies in this session unless they are changed later:\n"
        f"{instruction}"
    )


def _tmux_paste(socket_path: Path, session_name: str, text: str, *, cwd: Path, backend: str) -> None:
    log_tmux_io(session_name, "in", text, runtime="tmux", backend=backend, event="paste")
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
        log_tmux_io(
            session_name,
            "out",
            f"stdout:\n{load.stdout}\nstderr:\n{load.stderr}",
            runtime="tmux",
            backend=backend,
            event="load-buffer-error",
        )
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
        log_tmux_io(
            session_name,
            "out",
            f"stdout:\n{paste.stdout}\nstderr:\n{paste.stderr}",
            runtime="tmux",
            backend=backend,
            event="paste-buffer-error",
        )
        raise BackendRunError("tmux paste-buffer failed", stdout=paste.stdout, stderr=paste.stderr)
    _tmux_submit(socket_path, session_name, cwd=cwd, backend=backend)


def _tmux_submit(socket_path: Path, session_name: str, *, cwd: Path, backend: str) -> None:
    keys = ("C-j", "C-m") if backend == "codex" else ("C-m",)
    for key in keys:
        log_tmux_io(session_name, "in", key, runtime="tmux", backend=backend, event="key")
        subprocess.run(["tmux", "-S", str(socket_path), "send-keys", "-t", session_name, key], cwd=str(cwd), timeout=10, check=False)
        if backend == "codex":
            time.sleep(0.08)


def _confirm_local_workspace_trust_if_needed(socket_path: Path, session_name: str, *, cwd: Path) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            screen = _capture_tmux(socket_path, session_name, cwd=cwd)
        except BackendRunError:
            return
        if _looks_like_workspace_trust_prompt(screen):
            log_tmux_io(session_name, "in", "Enter", runtime="tmux", event="auto-key", meta={"reason": "workspace-trust"})
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
                log_tmux_io(session_name, "in", key, runtime="tmux", event="auto-key", meta={"reason": "codex-update-prompt"})
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
            log_tmux_io(session_name, "in", "Enter", runtime="tmux", event="auto-key", meta={"reason": "codex-workspace-trust"})
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
        log_tmux_io(
            session_name,
            "out",
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
            runtime="tmux",
            event="capture-error",
        )
        raise BackendRunError("tmux capture-pane failed", stdout=proc.stdout, stderr=proc.stderr)
    screen = _strip_ansi(proc.stdout)
    log_tmux_io(session_name, "out", screen, runtime="tmux", event="capture")
    return screen


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


def _wait_for_tmux_result(
    socket_path: Path,
    session_name: str,
    *,
    markers: ResultMarkers,
    cwd: Path,
    timeout: float,
    min_count: int = 1,
) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        screen = _capture_tmux(socket_path, session_name, cwd=cwd)
        if _marked_result_count(screen, markers) >= min_count:
            return screen
        if screen != last:
            last = screen
            stable_since = time.monotonic()
        time.sleep(0.5)
    raise BackendRunError(f"tmux interactive {session_name} turn did not complete within {timeout}s", stdout=last, stderr="")


def _has_marked_result(screen: str, markers: ResultMarkers) -> bool:
    return _marked_result_count(screen, markers) > 0


def _marked_result_count(screen: str, markers: ResultMarkers) -> int:
    return len(list(_result_pattern(markers).finditer(screen)))


def _extract_marked_result(screen: str, markers: ResultMarkers | None = None) -> str:
    if markers is not None:
        matches = list(_result_pattern(markers).finditer(screen))
        if matches:
            return matches[-1].group(1).strip()
    else:
        matches = list(re.finditer(r"(?m)^<(?:YIKES_)?RESULT>\s*$(.*?)^</(?:YIKES_)?RESULT>\s*$", screen, re.S))
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


def _ensure_docker_tmux_session(
    sandbox: SandboxSession,
    backend: str,
    *,
    model: str | None,
) -> tuple[Path, str]:
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
    return socket_path, session_name


def _container_tmux_session_alive(sandbox: SandboxSession, socket_path: Path, session_name: str) -> bool:
    result = sandbox.exec(
        ["tmux", "-S", str(socket_path), "has-session", "-t", session_name],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return result.returncode == 0


def _container_tmux_paste(sandbox: SandboxSession, socket_path: Path, session_name: str, text: str, *, backend: str) -> None:
    log_tmux_io(session_name, "in", text, runtime="docker-tmux", backend=backend, event="paste", meta={"container": sandbox.container_name})
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
        log_tmux_io(
            session_name,
            "out",
            f"stdout:\n{load.stdout}\nstderr:\n{load.stderr}",
            runtime="docker-tmux",
            backend=backend,
            event="load-buffer-error",
            meta={"container": sandbox.container_name},
        )
        raise BackendRunError("container tmux load-buffer failed", stdout=str(load.stdout), stderr=str(load.stderr))
    paste = sandbox.exec(
        ["tmux", "-S", str(socket_path), "paste-buffer", "-d", "-b", buffer_name, "-t", session_name],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if paste.returncode != 0:
        log_tmux_io(
            session_name,
            "out",
            f"stdout:\n{paste.stdout}\nstderr:\n{paste.stderr}",
            runtime="docker-tmux",
            backend=backend,
            event="paste-buffer-error",
            meta={"container": sandbox.container_name},
        )
        raise BackendRunError("container tmux paste-buffer failed", stdout=str(paste.stdout), stderr=str(paste.stderr))
    _container_tmux_submit(sandbox, socket_path, session_name, backend=backend)


def _container_tmux_submit(sandbox: SandboxSession, socket_path: Path, session_name: str, *, backend: str) -> None:
    keys = ("C-j", "C-m") if backend == "codex" else ("C-m",)
    for key in keys:
        log_tmux_io(session_name, "in", key, runtime="docker-tmux", backend=backend, event="key", meta={"container": sandbox.container_name})
        sandbox.exec(["tmux", "-S", str(socket_path), "send-keys", "-t", session_name, key], capture_output=True, text=True, timeout=10, check=False)
        if backend == "codex":
            time.sleep(0.08)


def _confirm_container_workspace_trust_if_needed(sandbox: SandboxSession, socket_path: Path, session_name: str) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            screen = _capture_container_tmux(sandbox, socket_path, session_name)
        except BackendRunError:
            return
        if _looks_like_workspace_trust_prompt(screen):
            log_tmux_io(session_name, "in", "Enter", runtime="docker-tmux", event="auto-key", meta={"container": sandbox.container_name, "reason": "workspace-trust"})
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
                log_tmux_io(session_name, "in", key, runtime="docker-tmux", event="auto-key", meta={"container": sandbox.container_name, "reason": "codex-update-prompt"})
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
            log_tmux_io(session_name, "in", "Enter", runtime="docker-tmux", event="auto-key", meta={"container": sandbox.container_name, "reason": "codex-workspace-trust"})
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
        log_tmux_io(
            session_name,
            "out",
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
            runtime="docker-tmux",
            event="capture-error",
            meta={"container": sandbox.container_name},
        )
        raise BackendRunError("container tmux capture-pane failed", stdout=str(proc.stdout), stderr=str(proc.stderr))
    screen = _strip_ansi(str(proc.stdout))
    log_tmux_io(session_name, "out", screen, runtime="docker-tmux", event="capture", meta={"container": sandbox.container_name})
    return screen


def _wait_for_container_tmux_result(
    sandbox: SandboxSession,
    socket_path: Path,
    session_name: str,
    *,
    markers: ResultMarkers,
    timeout: float,
    min_count: int = 1,
) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        screen = _capture_container_tmux(sandbox, socket_path, session_name)
        if _marked_result_count(screen, markers) >= min_count:
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
    return _REMOTE_RUNTIME.ask(
        backend,
        prompt,
        cwd=cwd,
        timeout=timeout,
        model=model,
        settings=settings,
    )


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
        managed_output_enabled=settings.managed_output_enabled,
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
        sandbox.write_file("/workspace/home/.codex/config.toml", _container_codex_config(_sandbox_workspace(sandbox)))
        sandbox.write_file("/workspace/home/.codex/version.json", _codex_dismissed_version_json())
        sandbox.exec(
            [
                "sh",
                "-lc",
                "chmod 600 /workspace/home/.codex/auth.json /workspace/home/.codex/config.toml /workspace/home/.codex/version.json 2>/dev/null || true",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )


def _prepare_local_codex_home(tmux_dir: Path, label: str, *, trusted_cwd: Path | None = None) -> Path:
    source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    target = tmux_dir / f"{label}-codex-home"
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)
    for name in ("auth.json", "config.toml"):
        source_path = source / name
        if source_path.exists():
            shutil.copy2(source_path, target / name)
    if trusted_cwd is not None:
        _trust_codex_project(target / "config.toml", trusted_cwd)
    (target / "version.json").write_text(_codex_dismissed_version_json(), encoding="utf-8")
    return target


def _trust_codex_project(config_path: Path, cwd: Path) -> None:
    try:
        current = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    config_path.write_text(_trusted_codex_config_text(current, cwd), encoding="utf-8")


def _container_codex_config(workspace: str) -> str:
    source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser() / "config.toml"
    try:
        current = source.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        current = ""
    return _trusted_codex_config_text(current, Path(workspace))


def _trusted_codex_config_text(current: str, cwd: Path) -> str:
    trusted = str(cwd.expanduser().resolve())
    block_header = f'[projects."{_toml_basic_string_content(trusted)}"]'
    if block_header in current:
        return current
    separator = "\n" if current and current.endswith("\n") else "\n\n" if current else ""
    return f"{current}{separator}{block_header}\ntrust_level = \"trusted\"\n"


def _toml_basic_string_content(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


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
        managed_output_enabled=settings.managed_output_enabled,
        read_roots=tuple(read_roots),
        write_roots=tuple(write_roots),
        mcp_servers=settings.mcp_servers,
    )


def _container_ephemeral_settings(settings: AgentSettings, workspace: Path) -> AgentSettings:
    return AgentSettings(
        web_search_enabled=settings.web_search_enabled,
        tmux_enabled=settings.tmux_enabled,
        managed_output_enabled=settings.managed_output_enabled,
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
