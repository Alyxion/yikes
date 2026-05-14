from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import tempfile
import time
import asyncio
from pathlib import Path

from .domain import AgentSettings
from .errors import BackendRunError, BackendUnavailable, DriverUnavailable
from .process import require_binary, run_process

if False:  # pragma: no cover
    from .chatbot import Backend, Driver


def ask_backend(
    backend: "Backend",
    driver: "Driver",
    prompt: str,
    *,
    cwd: Path,
    timeout: float,
    model: str | None,
    settings: AgentSettings,
) -> str:
    if driver.value == "direct":
        return _ask_direct(backend.value, prompt, cwd=cwd, timeout=timeout, model=model, settings=settings)
    if driver.value == "tmux":
        return _ask_tmux(backend.value, prompt, cwd=cwd, timeout=timeout, model=model, settings=settings)
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
        return _ask_claude_direct(prompt, cwd=cwd, timeout=timeout, model=model)
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
    timeout: float,
    model: str | None,
    settings: AgentSettings,
) -> str:
    require_binary("tmux")
    # This is a real tmux end-to-end path: each turn is executed inside an
    # isolated tmux server/session and collected from a file written by the pane.
    with tempfile.TemporaryDirectory(prefix="yikes-tmux-") as tmp:
        tmp_path = Path(tmp)
        socket_path = tmp_path / "tmux.sock"
        result_path = tmp_path / "result.txt"
        err_path = tmp_path / "stderr.txt"
        session_name = f"yikes-{backend}-{os.getpid()}-{time.monotonic_ns()}"
        command = _backend_shell_command(
            backend,
            prompt,
            result_path=result_path,
            err_path=err_path,
            model=model,
            settings=settings,
        )
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
            "sh",
            "-lc",
            command,
        ]
        run_process(argv, cwd=cwd, timeout=20)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            alive = _tmux_session_alive(socket_path, session_name, cwd)
            if result_path.exists() and not alive:
                text = result_path.read_text(encoding="utf-8").strip()
                _kill_tmux(socket_path, cwd)
                return _extract_backend_output(backend, text)
            if not alive:
                break
            time.sleep(0.2)
        _kill_tmux(socket_path, cwd)
        stderr = err_path.read_text(encoding="utf-8") if err_path.exists() else ""
        stdout = result_path.read_text(encoding="utf-8") if result_path.exists() else ""
        raise BackendRunError(
            f"tmux {backend} turn did not complete within {timeout}s",
            stdout=stdout,
            stderr=stderr,
        )


def _backend_shell_command(
    backend: str,
    prompt: str,
    *,
    result_path: Path,
    err_path: Path,
    model: str | None,
    settings: AgentSettings,
) -> str:
    if backend == "claude":
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
        if os.environ.get("YIKES_CLAUDE_REMOTE_FALLBACK") == "direct":
            return _ask_claude_direct(prompt, cwd=cwd, timeout=timeout, model=model)
        raise DriverUnavailable(
            "Claude Remote Control is a human remote UI and does not expose a local "
            "programmatic turn API. Set YIKES_CLAUDE_REMOTE_FALLBACK=direct to run "
            "the chatbot smoke through Claude's direct protocol while still keeping "
            "the remote-control test slot explicit."
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
    return {
        server.name: {
            "command": server.command,
            "args": list(server.args),
            "enabled": server.enabled,
        }
        for server in settings.mcp_servers
    }


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
