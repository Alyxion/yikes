from __future__ import annotations

import os
import re
import shlex
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
import subprocess

from .activity import ActivityMonitor, TerminalActivity
from .domain import Backend, ChatOptions, Driver
from .errors import BackendRunError, DriverUnavailable, YikesError
from .process import run_process
from .runtime import DurableSessionManager, RuntimeKind, RuntimeRef, SessionState
from .sandbox import SandboxManager
from .tmux_io_log import log_tmux_io


@dataclass(frozen=True)
class SessionSummary:
    id: str
    runtime: str
    backend: str
    state: str
    location: str
    detail: str = ""


class SessionInventory:
    """Read-only inventory over durable yikes! sessions and Docker sandboxes."""

    def __init__(
        self,
        *,
        runtime_store: Path | None = None,
        sandbox_store: Path | None = None,
    ) -> None:
        self.runtime_store = runtime_store
        self.sandbox_store = sandbox_store

    def list(self) -> list[SessionSummary]:
        rows: list[SessionSummary] = []
        rows.extend(self._durable_sessions())
        rows.extend(self._docker_sessions())
        return rows

    def format(self) -> str:
        rows = self.list()
        if not rows:
            return "Sessions: (none)"
        lines = ["Sessions:"]
        for row in rows:
            detail = f" - {row.detail}" if row.detail else ""
            lines.append(f"{row.id}: {row.runtime}/{row.backend} {row.state} @ {row.location}{detail}")
        return "\n".join(lines)

    def _durable_sessions(self) -> list[SessionSummary]:
        try:
            manager = DurableSessionManager(self.runtime_store)
            sessions = manager.list()
        except OSError:
            return []
        rows: list[SessionSummary] = []
        for meta in sessions:
            runtime = meta.runtime.kind.value
            if meta.runtime.kind is RuntimeKind.DOCKER:
                continue
            state = meta.state.value
            if meta.runtime.kind is RuntimeKind.TMUX:
                alive = _tmux_runtime_alive(meta.runtime.tmux_socket, meta.runtime.tmux_session, meta.cwd)
                if alive is True:
                    state = SessionState.RUNNING.value
                elif alive is False and meta.state is not SessionState.STOPPED:
                    state = SessionState.DEAD.value
                    meta.state = SessionState.DEAD
                    try:
                        manager.save(meta)
                    except OSError:
                        pass
            rows.append(
                SessionSummary(
                    id=meta.id,
                    runtime=runtime,
                    backend=meta.backend.value,
                    state=state,
                    location=str(meta.cwd),
                    detail=_runtime_detail(meta.runtime),
                )
            )
        return rows

    def _docker_sessions(self) -> list[SessionSummary]:
        try:
            manager = SandboxManager(self.sandbox_store)
            sessions = manager.list_sessions()
        except OSError:
            return []
        rows: list[SessionSummary] = []
        for session in sessions:
            state = "running" if session.is_running() else "stopped"
            socket = session.meta.user_data.get("tmux_socket")
            tmux_session = session.meta.user_data.get("tmux_session")
            if state == "running" and socket and tmux_session and _docker_tmux_alive(
                session.container_name,
                socket,
                tmux_session,
            ) is False:
                state = "dead"
            session_id = session.meta.user_data.get("logical_session_id") or session.id
            rows.append(
                SessionSummary(
                    id=session_id,
                    runtime="docker",
                    backend=session.meta.user_data.get("backend", "?"),
                    state=state,
                    location=session.container_name,
                    detail=f"{session.id} image={session.meta.config.image}",
                )
            )
        return rows


def _runtime_detail(runtime: object) -> str:
    tmux_socket = getattr(runtime, "tmux_socket", None)
    tmux_session = getattr(runtime, "tmux_session", None)
    remote_url = getattr(runtime, "remote_url", None)
    if tmux_socket or tmux_session:
        return " ".join(part for part in (tmux_socket, tmux_session) if part)
    if remote_url:
        return str(remote_url)
    return ""


def _docker_tmux_alive(container_name: str, socket: str, session_name: str) -> bool | None:
    try:
        result = subprocess.run(
            ["docker", "exec", container_name, "tmux", "-S", socket, "has-session", "-t", session_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    stderr = result.stderr.lower()
    if result.returncode != 0 and ("permission denied" in stderr or "operation not permitted" in stderr):
        return None
    return result.returncode == 0


def _capture_markers_from_data(data: dict[str, str]) -> tuple[tuple[str, str], ...]:
    start = data.get("capture_start")
    end = data.get("capture_end")
    if not start or not end:
        return ()
    return ((start, end),)


@dataclass(frozen=True)
class CloseResult:
    id: str
    runtime: str
    closed: bool
    message: str


class SessionLifecycle:
    """Close, bulk-close, and switch known yikes! sessions."""

    def __init__(
        self,
        *,
        runtime_store: Path | None = None,
        sandbox_store: Path | None = None,
    ) -> None:
        self.runtime_store = runtime_store
        self.sandbox_store = sandbox_store

    def close(self, session_id: str) -> CloseResult:
        session_id = self.resolve_session_id(session_id) or session_id
        docker = self._close_docker(session_id)
        if docker is not None:
            return docker
        durable = self._close_durable(session_id)
        if durable is not None:
            return durable
        return CloseResult(session_id, "unknown", False, f"Session not found: {session_id}")

    def close_all(self, *, runtime: str | None = None, backend: str | None = None) -> list[CloseResult]:
        normalized_runtime = None if runtime in (None, "all") else runtime
        normalized_backend = None if backend in (None, "all") else backend
        results: list[CloseResult] = []
        for summary in SessionInventory(runtime_store=self.runtime_store, sandbox_store=self.sandbox_store).list():
            if normalized_runtime and summary.runtime != normalized_runtime:
                continue
            if normalized_backend and summary.backend != normalized_backend:
                continue
            results.append(self.close(summary.id))
        return results

    def summary(self, session_id: str) -> SessionSummary | None:
        resolved = self.resolve_session_id(session_id) or session_id
        for summary in SessionInventory(runtime_store=self.runtime_store, sandbox_store=self.sandbox_store).list():
            if summary.id in {session_id, resolved} or self.resolve_session_id(summary.id) == resolved:
                return summary
        return None

    def is_live(self, session_id: str) -> bool:
        summary = self.summary(session_id)
        return summary is not None and summary.state not in {"dead", "stopped"}

    def switch_options(self, current: ChatOptions, session_id: str) -> ChatOptions | None:
        session_id = self.resolve_session_id(session_id) or session_id
        durable = DurableSessionManager(self.runtime_store).get(session_id)
        if durable is not None:
            return ChatOptions(
                backend=durable.backend,
                driver=durable.driver,
                cwd=durable.cwd,
                timeout=current.timeout,
                model=durable.model,
                complexity=durable.complexity,
                settings=durable.settings,
                session_id=durable.id,
            )
        sandbox = SandboxManager(self.sandbox_store).get(session_id)
        if sandbox is None:
            return None
        backend = sandbox.meta.user_data.get("backend", current.backend.value)
        has_tmux = bool(sandbox.meta.user_data.get("tmux_socket"))
        settings = current.settings.with_tmux(has_tmux)
        if has_tmux:
            managed_value = sandbox.meta.user_data.get("managed_output_enabled")
            managed_output = str(managed_value).lower() in {"1", "true", "yes", "on"} if managed_value is not None else False
            settings = settings.with_managed_output(managed_output)
        return ChatOptions(
            backend=Backend(backend),
            driver=Driver.DOCKER,
            cwd=current.cwd,
            timeout=current.timeout,
            model=current.model,
            complexity=current.complexity,
            settings=settings,
            session_id=sandbox.meta.user_data.get("logical_session_id") or sandbox.id,
        )

    def snapshot(self, session_id: str, *, lines: int = 400) -> str | None:
        session_id = self.resolve_session_id(session_id) or session_id
        durable = DurableSessionManager(self.runtime_store).get(session_id)
        if durable is not None and durable.runtime.kind is RuntimeKind.TMUX and durable.runtime.tmux_socket:
            cmd = [
                "tmux",
                "-S",
                durable.runtime.tmux_socket,
                "capture-pane",
                "-p",
                "-J",
                "-S",
                f"-{lines}",
                "-E",
                "-",
            ]
            if durable.runtime.tmux_session:
                cmd.extend(["-t", durable.runtime.tmux_session])
            text = _capture_output(cmd)
            if text is not None and _should_auto_accept_workspace_prompt(durable.user_data, durable.cwd):
                accepted = _auto_accept_workspace_prompt(
                    ["tmux", "-S", durable.runtime.tmux_socket, "send-keys"],
                    target=durable.runtime.tmux_session,
                    text=text,
                    backend=durable.backend.value,
                    session_key=durable.runtime.tmux_session or session_id,
                    runtime="tmux",
                    cwd=durable.cwd,
                )
                if accepted:
                    time.sleep(0.3)
                    text = _capture_output(cmd)
            if text is not None:
                log_tmux_io(
                    durable.runtime.tmux_session or session_id,
                    "out",
                    text,
                    runtime="tmux",
                    backend=durable.backend.value,
                    event="snapshot",
                    meta={"session_id": session_id},
                )
            return text
        sandbox = SandboxManager(self.sandbox_store).get(session_id)
        if sandbox is None:
            return None
        socket = sandbox.meta.user_data.get("tmux_socket")
        session = sandbox.meta.user_data.get("tmux_session")
        if not socket or not session:
            return None
        text = _capture_output(
            [
                "docker",
                "exec",
                sandbox.container_name,
                "tmux",
                "-S",
                socket,
                "capture-pane",
                "-p",
                "-J",
                "-S",
                f"-{lines}",
                "-E",
                "-",
                "-t",
                session,
            ]
        )
        if text is not None and _should_auto_accept_workspace_prompt(sandbox.meta.user_data, Path(sandbox.meta.user_data.get("cwd", ""))):
            accepted = _auto_accept_workspace_prompt(
                ["docker", "exec", sandbox.container_name, "tmux", "-S", socket, "send-keys"],
                target=session,
                text=text,
                backend=sandbox.meta.user_data.get("backend", ""),
                session_key=session,
                runtime="docker-tmux",
                cwd=None,
            )
            if accepted:
                time.sleep(0.3)
                text = _capture_output(
                    [
                        "docker",
                        "exec",
                        sandbox.container_name,
                        "tmux",
                        "-S",
                        socket,
                        "capture-pane",
                        "-p",
                        "-J",
                        "-S",
                        f"-{lines}",
                        "-E",
                        "-",
                        "-t",
                        session,
                    ]
                )
        if text is not None:
            log_tmux_io(
                session,
                "out",
                text,
                runtime="docker-tmux",
                backend=sandbox.meta.user_data.get("backend"),
                event="snapshot",
                meta={"session_id": session_id, "container": sandbox.container_name},
            )
        return text

    def capture_markers(self, session_id: str) -> tuple[tuple[str, str], ...]:
        session_id = self.resolve_session_id(session_id) or session_id
        durable = DurableSessionManager(self.runtime_store).get(session_id)
        if durable is not None:
            return _capture_markers_from_data(durable.user_data)
        sandbox = SandboxManager(self.sandbox_store).get(session_id)
        if sandbox is None:
            return ()
        return _capture_markers_from_data(sandbox.meta.user_data)

    def attach_command(self, session_id: str) -> list[str] | None:
        session_id = self.resolve_session_id(session_id) or session_id
        durable = DurableSessionManager(self.runtime_store).get(session_id)
        if durable is not None and durable.runtime.kind is RuntimeKind.TMUX and durable.runtime.tmux_socket:
            cmd = ["tmux", "-S", durable.runtime.tmux_socket, "attach"]
            if durable.runtime.tmux_session:
                cmd.extend(["-t", durable.runtime.tmux_session])
            return cmd
        sandbox = SandboxManager(self.sandbox_store).get(session_id)
        if sandbox is None:
            return None
        socket = sandbox.meta.user_data.get("tmux_socket")
        session = sandbox.meta.user_data.get("tmux_session")
        if socket and session:
            return ["docker", "exec", "-it", sandbox.container_name, "tmux", "-S", socket, "attach", "-t", session]
        return ["docker", "exec", "-it", sandbox.container_name, "sh", "-lc", "cd /workspace/project && exec bash"]

    def resize(self, session_id: str, *, cols: int, rows: int) -> CloseResult:
        session_id = self.resolve_session_id(session_id) or session_id
        cols = max(20, int(cols))
        rows = max(5, int(rows))
        durable = DurableSessionManager(self.runtime_store).get(session_id)
        if durable is not None and durable.runtime.kind is RuntimeKind.TMUX and durable.runtime.tmux_socket:
            return _tmux_resize(
                ["tmux", "-S", durable.runtime.tmux_socket],
                session_id=session_id,
                runtime="tmux",
                target=durable.runtime.tmux_session,
                cols=cols,
                rows=rows,
            )
        sandbox = SandboxManager(self.sandbox_store).get(session_id)
        if sandbox is None:
            return CloseResult(session_id, "unknown", False, f"Session not found: {session_id}")
        socket = sandbox.meta.user_data.get("tmux_socket")
        session = sandbox.meta.user_data.get("tmux_session")
        if not socket or not session:
            return CloseResult(session_id, "docker", False, f"Docker session is not a tmux session: {session_id}")
        return _tmux_resize(
            ["docker", "exec", sandbox.container_name, "tmux", "-S", socket],
            session_id=session_id,
            runtime="docker",
            target=session,
            cols=cols,
            rows=rows,
        )

    def send_key(self, session_id: str, key: str) -> CloseResult:
        session_id = self.resolve_session_id(session_id) or session_id
        durable = DurableSessionManager(self.runtime_store).get(session_id)
        if durable is not None and durable.runtime.kind is RuntimeKind.TMUX and durable.runtime.tmux_socket:
            cmd = ["tmux", "-S", durable.runtime.tmux_socket, "send-keys"]
            if durable.runtime.tmux_session:
                cmd.extend(["-t", durable.runtime.tmux_session])
            cmd.append(key)
            log_tmux_io(
                durable.runtime.tmux_session or session_id,
                "in",
                key,
                runtime="tmux",
                backend=durable.backend.value,
                event="key",
                meta={"session_id": session_id},
            )
            return _run_control_command(cmd, session_id=session_id, runtime="tmux", action=f"Sent key {key}")
        sandbox = SandboxManager(self.sandbox_store).get(session_id)
        if sandbox is None:
            return CloseResult(session_id, "unknown", False, f"Session not found: {session_id}")
        socket = sandbox.meta.user_data.get("tmux_socket")
        session = sandbox.meta.user_data.get("tmux_session")
        if not socket or not session:
            return CloseResult(session_id, "docker", False, f"Docker session is not a tmux session: {session_id}")
        log_tmux_io(
            session,
            "in",
            key,
            runtime="docker-tmux",
            backend=sandbox.meta.user_data.get("backend"),
            event="key",
            meta={"session_id": session_id, "container": sandbox.container_name},
        )
        return _run_control_command(
            ["docker", "exec", sandbox.container_name, "tmux", "-S", socket, "send-keys", "-t", session, key],
            session_id=session_id,
            runtime="docker",
            action=f"Sent key {key}",
        )

    def paste_text(self, session_id: str, text: str) -> CloseResult:
        session_id = self.resolve_session_id(session_id) or session_id
        durable = DurableSessionManager(self.runtime_store).get(session_id)
        if durable is not None and durable.runtime.kind is RuntimeKind.TMUX and durable.runtime.tmux_socket:
            return _tmux_paste_text(
                ["tmux", "-S", durable.runtime.tmux_socket],
                session_id=session_id,
                runtime="tmux",
                target=durable.runtime.tmux_session,
                text=text,
            )
        sandbox = SandboxManager(self.sandbox_store).get(session_id)
        if sandbox is None:
            return CloseResult(session_id, "unknown", False, f"Session not found: {session_id}")
        socket = sandbox.meta.user_data.get("tmux_socket")
        session = sandbox.meta.user_data.get("tmux_session")
        if not socket or not session:
            return CloseResult(session_id, "docker", False, f"Docker session is not a tmux session: {session_id}")
        return _tmux_paste_text(
            ["docker", "exec", "-i", sandbox.container_name, "tmux", "-S", socket],
            session_id=session_id,
            runtime="docker",
            target=session,
            text=text,
        )

    def resolve_session_id(self, session_ref: str) -> str | None:
        manager = DurableSessionManager(self.runtime_store)
        if manager.get(session_ref) is not None:
            return session_ref
        for meta in manager.list():
            if meta.user_data.get("name") == session_ref or meta.user_data.get("label") == session_ref:
                return meta.id
        sandbox = SandboxManager(self.sandbox_store)
        if sandbox.get(session_ref) is not None:
            return session_ref
        for session in sandbox.list_sessions():
            label = session.meta.user_data.get("label") or ""
            if (
                session.meta.user_data.get("logical_session_id") == session_ref
                or session.meta.user_data.get("name") == session_ref
                or label == session_ref
                or (len(session_ref) >= 12 and label.endswith(session_ref[:12]))
            ):
                return session.id
        return None

    def _close_docker(self, session_id: str) -> CloseResult | None:
        manager = SandboxManager(self.sandbox_store)
        session = manager.get(session_id)
        if session is None:
            return None
        try:
            session.destroy()
        except Exception as exc:
            return CloseResult(session_id, "docker", False, f"Failed to close Docker session {session_id}: {exc}")
        return CloseResult(session_id, "docker", True, f"Closed Docker session {session_id}.")

    def _close_durable(self, session_id: str) -> CloseResult | None:
        manager = DurableSessionManager(self.runtime_store)
        meta = manager.get(session_id)
        if meta is None:
            return None
        runtime = meta.runtime.kind.value
        if meta.runtime.kind is RuntimeKind.TMUX:
            self._kill_tmux(meta.runtime.tmux_socket, meta.runtime.tmux_session)
        meta.state = SessionState.STOPPED
        manager.save(meta)
        manager.delete(session_id)
        return CloseResult(session_id, runtime, True, f"Closed {runtime} session {session_id}.")

    @staticmethod
    def _kill_tmux(socket: str | None, session: str | None) -> None:
        if not socket:
            return
        cmd = ["tmux", "-S", socket, "kill-session"]
        if session:
            cmd.extend(["-t", session])
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def record_direct_session(
    options: ChatOptions,
    *,
    runtime_store: Path | None = None,
    user_data: dict[str, str] | None = None,
) -> None:
    """Persist a host CLI session so every frontend sees the same tab set."""
    if options.driver is not Driver.DIRECT:
        return
    manager = DurableSessionManager(runtime_store)
    meta = manager.get(options.session_id)
    if meta is None:
        meta = manager.create(
            backend=options.backend,
            driver=Driver.DIRECT,
            runtime=RuntimeRef(RuntimeKind.DIRECT),
            cwd=options.cwd,
            session_id=options.session_id,
            model=options.model,
            complexity=options.complexity,
            settings=options.settings,
            user_data={
                "label": options.session_id[:12],
                "cwd_explicit": str(options.cwd_explicit).lower(),
                **(user_data or {}),
            },
        )
    else:
        meta.backend = options.backend
        meta.driver = Driver.DIRECT
        meta.runtime = RuntimeRef(RuntimeKind.DIRECT)
        meta.cwd = options.cwd.expanduser()
        meta.model = options.model
        meta.complexity = options.complexity
        meta.settings = options.settings
        meta.user_data.update(user_data or {})
        meta.user_data["cwd_explicit"] = str(options.cwd_explicit).lower()
    meta.state = SessionState.RUNNING
    manager.save(meta)


@dataclass(frozen=True)
class TmuxStartResult:
    id: str
    name: str
    backend: str
    socket: str
    session: str
    created: bool
    replaced: bool = False


class TmuxSessionController:
    """Named tmux session controls for automation and external tooling."""

    def __init__(self, *, runtime_store: Path | None = None, sandbox_store: Path | None = None) -> None:
        self.runtime_store = runtime_store
        self.sandbox_store = sandbox_store
        self.lifecycle = SessionLifecycle(runtime_store=runtime_store, sandbox_store=sandbox_store)
        self.activity = ActivityMonitor()

    def start(
        self,
        name: str,
        *,
        backend: Backend,
        cwd: Path,
        model: str | None = None,
        replace: bool = False,
    ) -> TmuxStartResult:
        _validate_tmux_name(name)
        manager = DurableSessionManager(self.runtime_store)
        existing = self._find_named(name)
        replaced = False
        if existing is not None:
            alive = self._is_alive(existing.runtime.tmux_socket, existing.runtime.tmux_session, existing.cwd)
            if alive and not replace:
                return TmuxStartResult(
                    id=existing.id,
                    name=name,
                    backend=existing.backend.value,
                    socket=existing.runtime.tmux_socket or "",
                    session=existing.runtime.tmux_session or name,
                    created=False,
                )
            self.lifecycle.close(existing.id)
            replaced = True

        tmux_dir = Path(os.environ.get("YIKES_TMUX_DIR", str(Path.home() / ".yikes" / "tmux"))).expanduser()
        tmux_dir.mkdir(parents=True, exist_ok=True)
        tmux_dir.chmod(0o700)
        socket_path = tmux_dir / f"{name}.sock"
        session_name = name
        cwd = cwd.expanduser()
        cwd.mkdir(parents=True, exist_ok=True)
        argv = self._backend_argv(backend.value, model=model, tmux_dir=tmux_dir, name=name)
        run_process(
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
                str(cwd),
                *argv,
            ],
            cwd=cwd,
            timeout=20,
        )
        _set_tmux_options(socket_path, cwd)
        if not self._is_alive(str(socket_path), session_name, cwd):
            raise BackendRunError(
                f"tmux session {session_name} exited before it could receive input",
                stdout="",
                stderr=f"Started command: {shlex.join(argv)}",
            )
        meta = manager.create(
            backend=backend,
            driver=Driver.TMUX,
            runtime=RuntimeRef(RuntimeKind.TMUX, tmux_socket=str(socket_path), tmux_session=session_name),
            cwd=cwd,
            model=model,
            user_data={
                "name": name,
                "label": name,
                "attach": f"tmux -S {socket_path} attach -t {session_name}",
            },
        )
        meta.state = SessionState.RUNNING
        manager.save(meta)
        return TmuxStartResult(
            id=meta.id,
            name=name,
            backend=backend.value,
            socket=str(socket_path),
            session=session_name,
            created=True,
            replaced=replaced,
        )

    def state(self, session_ref: str) -> tuple[str, TerminalActivity, str | None]:
        session_id = self.lifecycle.resolve_session_id(session_ref)
        if session_id is None:
            raise YikesError(f"Session not found: {session_ref}")
        snapshot = self.lifecycle.snapshot(session_id, lines=160)
        activity = self.activity.observe(session_id, snapshot)
        return session_id, activity, snapshot

    def send(self, session_ref: str, text: str, *, submit: bool = True) -> CloseResult:
        session_id = self.lifecycle.resolve_session_id(session_ref)
        if session_id is None:
            return CloseResult(session_ref, "unknown", False, f"Session not found: {session_ref}")
        paste = self.lifecycle.paste_text(session_id, text)
        if not paste.closed or not submit:
            return paste
        return self.lifecycle.send_key(session_id, "Enter")

    def wait(self, session_ref: str, *, timeout: float, interval: float = 0.4, stable_for: float = 1.2) -> TerminalActivity:
        session_id = self.lifecycle.resolve_session_id(session_ref)
        if session_id is None:
            raise YikesError(f"Session not found: {session_ref}")
        deadline = time.monotonic() + timeout
        last_digest = ""
        stable_since: float | None = None
        last_activity = TerminalActivity("unknown", "unknown", 0.0, "not observed")
        while True:
            snapshot = self.lifecycle.snapshot(session_id, lines=220) or ""
            digest = str(hash(snapshot))
            activity = self.activity.observe(session_id, snapshot)
            now = time.monotonic()
            if digest != last_digest:
                last_digest = digest
                stable_since = now
            elif stable_since is None:
                stable_since = now
            if activity.state == "awaiting-selection":
                return activity
            if activity.state in {"idle", "unknown"} and stable_since is not None and now - stable_since >= stable_for:
                return activity
            last_activity = activity
            if now >= deadline:
                return TerminalActivity(
                    last_activity.state,
                    last_activity.label,
                    last_activity.confidence,
                    f"timeout after {timeout:g}s; last state: {last_activity.reason}",
                    changed=last_activity.changed,
                    updated_at=last_activity.updated_at,
                )
            time.sleep(interval)

    def kill(self, session_ref: str) -> CloseResult:
        return self.lifecycle.close(session_ref)

    def _find_named(self, name: str):
        for meta in DurableSessionManager(self.runtime_store).list():
            if meta.runtime.kind is RuntimeKind.TMUX and meta.user_data.get("name") == name:
                return meta
        return None

    @staticmethod
    def _is_alive(socket: str | None, session: str | None, cwd: Path) -> bool:
        if not socket or not session:
            return False
        return _tmux_session_alive(Path(socket), session, cwd)

    @staticmethod
    def _backend_argv(backend: str, *, model: str | None, tmux_dir: Path, name: str) -> list[str]:
        if backend == "claude":
            argv = ["claude", "--permission-mode", "dontAsk"]
            if model:
                argv.extend(["--model", model])
            return argv
        if backend == "codex":
            codex_home = tmux_dir / f"codex-home-{name}"
            codex_home.mkdir(parents=True, exist_ok=True)
            argv = ["codex", "--no-alt-screen"]
            if model:
                argv.extend(["--model", model])
            return ["env", f"CODEX_HOME={codex_home}", *argv]
        raise DriverUnavailable(f"unknown backend: {backend}")


def _capture_output(cmd: list[str]) -> str | None:
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text or None


def _should_auto_accept_workspace_prompt(user_data: dict[str, str], cwd: Path) -> bool:
    if user_data.get("cwd_explicit") == "false":
        return True
    try:
        resolved = cwd.expanduser().resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        resolved.relative_to(temp_root)
    except (OSError, ValueError):
        return False
    return resolved.name.startswith("yikes-")


def _auto_accept_workspace_prompt(
    prefix: list[str],
    *,
    target: str | None,
    text: str,
    backend: str,
    session_key: str,
    runtime: str,
    cwd: Path | None,
) -> bool:
    if backend == "codex" and not _looks_like_codex_workspace_trust_prompt(text):
        return False
    if backend == "claude" and not _looks_like_claude_workspace_trust_prompt(text):
        return False
    if backend not in {"codex", "claude"}:
        return False
    cmd = [*prefix]
    if target:
        cmd.extend(["-t", target])
    cmd.append("Enter")
    log_tmux_io(
        session_key,
        "in",
        "Enter",
        runtime=runtime,
        backend=backend,
        event="auto-key",
        meta={"reason": "generated-workspace-trust"},
    )
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def _looks_like_codex_workspace_trust_prompt(text: str) -> bool:
    return "Do you trust the contents of this directory?" in text and "Yes, continue" in text


def _looks_like_claude_workspace_trust_prompt(text: str) -> bool:
    return "Yes, I trust this folder" in text and "Enter to confirm" in text


def _tmux_runtime_alive(socket: str | None, session: str | None, cwd: Path) -> bool | None:
    if not socket:
        return False
    cmd = ["tmux", "-S", socket, "has-session"]
    if session:
        cmd.extend(["-t", session])
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError:
        return False
    stderr = (result.stderr or "").lower()
    if result.returncode != 0 and ("operation not permitted" in stderr or "permission denied" in stderr):
        return None
    return result.returncode == 0


def _run_control_command(cmd: list[str], *, session_id: str, runtime: str, action: str) -> CloseResult:
    trace_runtime = "docker-tmux" if runtime == "docker" else runtime
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    except OSError as exc:
        log_tmux_io(session_id, "out", str(exc), runtime=trace_runtime, event="control-error")
        return CloseResult(session_id, runtime, False, f"{action} failed for {session_id}: {exc}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        log_tmux_io(session_id, "out", detail, runtime=trace_runtime, event="control-error")
        return CloseResult(session_id, runtime, False, f"{action} failed for {session_id}{suffix}")
    return CloseResult(session_id, runtime, True, f"{action} for {session_id}.")


def _tmux_paste_text(
    prefix: list[str],
    *,
    session_id: str,
    runtime: str,
    target: str | None,
    text: str,
) -> CloseResult:
    trace_runtime = "docker-tmux" if runtime == "docker" else runtime
    log_tmux_io(
        target or session_id,
        "in",
        text,
        runtime=trace_runtime,
        event="paste",
        meta={"session_id": session_id},
    )
    buffer_name = f"yikes-{session_id}"
    load = subprocess.run(
        [*prefix, "load-buffer", "-b", buffer_name, "-"],
        input=text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if load.returncode != 0:
        detail = (load.stderr or load.stdout or "").strip()
        log_tmux_io(target or session_id, "out", detail, runtime=trace_runtime, event="load-buffer-error")
        return CloseResult(session_id, runtime, False, f"Paste failed for {session_id}: {detail}")
    cmd = [*prefix, "paste-buffer", "-d", "-b", buffer_name]
    if target:
        cmd.extend(["-t", target])
    paste = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if paste.returncode != 0:
        detail = (paste.stderr or paste.stdout or "").strip()
        log_tmux_io(target or session_id, "out", detail, runtime=trace_runtime, event="paste-buffer-error")
        return CloseResult(session_id, runtime, False, f"Paste failed for {session_id}: {detail}")
    return CloseResult(session_id, runtime, True, f"Pasted text into {session_id}.")


def _tmux_resize(
    prefix: list[str],
    *,
    session_id: str,
    runtime: str,
    target: str | None,
    cols: int,
    rows: int,
) -> CloseResult:
    trace_runtime = "docker-tmux" if runtime == "docker" else runtime
    log_tmux_io(
        target or session_id,
        "in",
        f"{cols}x{rows}",
        runtime=trace_runtime,
        event="resize",
        meta={"session_id": session_id},
    )
    cmd = [*prefix, "resize-window", "-x", str(cols), "-y", str(rows)]
    if target:
        cmd.extend(["-t", target])
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    except OSError as exc:
        log_tmux_io(target or session_id, "out", str(exc), runtime=trace_runtime, event="resize-error")
        return CloseResult(session_id, runtime, False, f"Resize failed for {session_id}: {exc}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        log_tmux_io(target or session_id, "out", detail, runtime=trace_runtime, event="resize-error")
        return CloseResult(session_id, runtime, False, f"Resize failed for {session_id}{suffix}")
    return CloseResult(session_id, runtime, True, f"Resized {session_id} to {cols}x{rows}.")


def _validate_tmux_name(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
        raise YikesError("tmux session names may only contain letters, numbers, dot, dash, and underscore.")


def _tmux_session_alive(socket_path: Path, session_name: str, cwd: Path) -> bool:
    result = subprocess.run(
        ["tmux", "-S", str(socket_path), "has-session", "-t", session_name],
        cwd=str(cwd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _set_tmux_options(socket_path: Path, cwd: Path) -> None:
    for args in (
        ["set", "-g", "status", "off"],
        ["set", "-g", "history-limit", "100000"],
        ["set", "-g", "extended-keys", "off"],
    ):
        subprocess.run(
            ["tmux", "-S", str(socket_path), *args],
            cwd=str(cwd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
