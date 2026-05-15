from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess

from .domain import Backend, ChatOptions, Driver
from .runtime import DurableSessionManager, RuntimeKind, SessionState
from .sandbox import SandboxManager


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
            rows.append(
                SessionSummary(
                    id=meta.id,
                    runtime=runtime,
                    backend=meta.backend.value,
                    state=meta.state.value,
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
            rows.append(
                SessionSummary(
                    id=session.id,
                    runtime="docker",
                    backend=session.meta.user_data.get("backend", "?"),
                    state=state,
                    location=session.container_name,
                    detail=f"image={session.meta.config.image}",
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

    def switch_options(self, current: ChatOptions, session_id: str) -> ChatOptions | None:
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
        settings = current.settings.with_tmux(bool(sandbox.meta.user_data.get("tmux_socket")))
        return ChatOptions(
            backend=Backend(backend),
            driver=Driver.DOCKER,
            cwd=current.cwd,
            timeout=current.timeout,
            model=current.model,
            complexity=current.complexity,
            settings=settings,
            session_id=sandbox.id,
        )

    def snapshot(self, session_id: str, *, lines: int = 400) -> str | None:
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
            return _capture_output(cmd)
        sandbox = SandboxManager(self.sandbox_store).get(session_id)
        if sandbox is None:
            return None
        socket = sandbox.meta.user_data.get("tmux_socket")
        session = sandbox.meta.user_data.get("tmux_session")
        if not socket or not session:
            return None
        return _capture_output(
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

    def attach_command(self, session_id: str) -> list[str] | None:
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

    def send_key(self, session_id: str, key: str) -> CloseResult:
        durable = DurableSessionManager(self.runtime_store).get(session_id)
        if durable is not None and durable.runtime.kind is RuntimeKind.TMUX and durable.runtime.tmux_socket:
            cmd = ["tmux", "-S", durable.runtime.tmux_socket, "send-keys"]
            if durable.runtime.tmux_session:
                cmd.extend(["-t", durable.runtime.tmux_session])
            cmd.append(key)
            return _run_control_command(cmd, session_id=session_id, runtime="tmux", action=f"Sent key {key}")
        sandbox = SandboxManager(self.sandbox_store).get(session_id)
        if sandbox is None:
            return CloseResult(session_id, "unknown", False, f"Session not found: {session_id}")
        socket = sandbox.meta.user_data.get("tmux_socket")
        session = sandbox.meta.user_data.get("tmux_session")
        if not socket or not session:
            return CloseResult(session_id, "docker", False, f"Docker session is not a tmux session: {session_id}")
        return _run_control_command(
            ["docker", "exec", sandbox.container_name, "tmux", "-S", socket, "send-keys", "-t", session, key],
            session_id=session_id,
            runtime="docker",
            action=f"Sent key {key}",
        )

    def paste_text(self, session_id: str, text: str) -> CloseResult:
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


def _capture_output(cmd: list[str]) -> str | None:
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text or None


def _run_control_command(cmd: list[str], *, session_id: str, runtime: str, action: str) -> CloseResult:
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    except OSError as exc:
        return CloseResult(session_id, runtime, False, f"{action} failed for {session_id}: {exc}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
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
        return CloseResult(session_id, runtime, False, f"Paste failed for {session_id}: {detail}")
    cmd = [*prefix, "paste-buffer", "-d", "-b", buffer_name]
    if target:
        cmd.extend(["-t", target])
    paste = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if paste.returncode != 0:
        detail = (paste.stderr or paste.stdout or "").strip()
        return CloseResult(session_id, runtime, False, f"Paste failed for {session_id}: {detail}")
    return CloseResult(session_id, runtime, True, f"Pasted text into {session_id}.")
