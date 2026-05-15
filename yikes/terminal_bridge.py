from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import secrets
import signal
import struct
import subprocess
import termios
import time
from dataclasses import dataclass
from typing import Any

try:
    from starlette.websockets import WebSocket, WebSocketDisconnect
except ModuleNotFoundError:  # pragma: no cover - dependency guard
    WebSocket = Any  # type: ignore[assignment]
    WebSocketDisconnect = Exception  # type: ignore[assignment]


MAX_SCROLLBACK = 300 * 1024
DEFAULT_COLS = 120
DEFAULT_ROWS = 34


@dataclass(frozen=True)
class WebTerminalInfo:
    terminal_id: str
    session_id: str
    title: str
    cols: int
    rows: int
    alive: bool
    created_at: float


class WebTerminalSession:
    """Server-side PTY for browser attach to an existing tmux session."""

    def __init__(
        self,
        *,
        terminal_id: str,
        session_id: str,
        command: list[str],
        cols: int = DEFAULT_COLS,
        rows: int = DEFAULT_ROWS,
    ) -> None:
        self.terminal_id = terminal_id
        self.session_id = session_id
        self.title = " ".join(command)
        self.cols = cols
        self.rows = rows
        self.created_at = time.monotonic()
        self._scrollback = bytearray()
        self._viewers: set[WebSocket] = set()

        master_fd, slave_fd = pty.openpty()
        self._set_winsize(slave_fd, rows, cols)
        self._master_fd = master_fd

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        self._process = subprocess.Popen(
            command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            preexec_fn=os.setsid,
            close_fds=True,
        )
        os.close(slave_fd)

        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    @property
    def alive(self) -> bool:
        return self._process.poll() is None

    @property
    def scrollback(self) -> bytes:
        return bytes(self._scrollback)

    def info(self) -> WebTerminalInfo:
        return WebTerminalInfo(
            terminal_id=self.terminal_id,
            session_id=self.session_id,
            title=self.title,
            cols=self.cols,
            rows=self.rows,
            alive=self.alive,
            created_at=self.created_at,
        )

    def write(self, data: bytes) -> None:
        if not self.alive:
            return
        try:
            os.write(self._master_fd, data)
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        self.cols = cols
        self.rows = rows
        try:
            self._set_winsize(self._master_fd, rows, cols)
        except OSError:
            pass

    def close(self) -> None:
        try:
            if self.alive:
                os.killpg(os.getpgid(self._process.pid), signal.SIGHUP)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self._process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            os.close(self._master_fd)
        except OSError:
            pass

    def add_viewer(self, websocket: WebSocket) -> None:
        self._viewers.add(websocket)

    def remove_viewer(self, websocket: WebSocket) -> None:
        self._viewers.discard(websocket)

    @property
    def viewer_count(self) -> int:
        return len(self._viewers)

    @staticmethod
    def _set_winsize(fd: int, rows: int, cols: int) -> None:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


class WebTerminalManager:
    """Owns browser attach PTYs for the current web server process."""

    def __init__(self) -> None:
        self._sessions: dict[str, WebTerminalSession] = {}

    def spawn(
        self,
        *,
        session_id: str,
        command: list[str],
        cols: int = DEFAULT_COLS,
        rows: int = DEFAULT_ROWS,
    ) -> WebTerminalSession:
        terminal_id = f"term_{secrets.token_hex(8)}"
        session = WebTerminalSession(
            terminal_id=terminal_id,
            session_id=session_id,
            command=command,
            cols=cols,
            rows=rows,
        )
        self._sessions[terminal_id] = session
        return session

    def get(self, terminal_id: str) -> WebTerminalSession | None:
        return self._sessions.get(terminal_id)

    def close(self, terminal_id: str) -> None:
        session = self._sessions.pop(terminal_id, None)
        if session is not None:
            session.close()


async def handle_terminal_ws(websocket: WebSocket, manager: WebTerminalManager, terminal_id: str) -> None:
    session = manager.get(terminal_id)
    if session is None or not session.alive:
        await websocket.close(code=4004, reason="Terminal not found")
        return

    await websocket.accept()
    session.add_viewer(websocket)

    cols = _int_query(websocket, "cols")
    rows = _int_query(websocket, "rows")
    if cols and rows:
        session.resize(cols, rows)

    if session.scrollback:
        await websocket.send_bytes(session.scrollback)

    async def pty_to_browser() -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()

        def on_readable() -> None:
            try:
                data = os.read(session._master_fd, 65536)
            except OSError:
                data = b""
            queue.put_nowait(data)

        loop.add_reader(session._master_fd, on_readable)
        try:
            while session.alive:
                data = await queue.get()
                if not data:
                    break
                session._scrollback.extend(data)
                if len(session._scrollback) > MAX_SCROLLBACK:
                    del session._scrollback[: len(session._scrollback) - MAX_SCROLLBACK]
                dead: list[WebSocket] = []
                for viewer in session._viewers:
                    try:
                        await viewer.send_bytes(data)
                    except Exception:
                        dead.append(viewer)
                for viewer in dead:
                    session.remove_viewer(viewer)
        finally:
            loop.remove_reader(session._master_fd)

    async def browser_to_pty() -> None:
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg["type"] != "websocket.receive":
                    continue
                if msg.get("bytes"):
                    session.write(msg["bytes"])
                elif msg.get("text"):
                    _handle_text_frame(session, msg["text"])
        except WebSocketDisconnect:
            pass

    try:
        done, pending = await asyncio.wait(
            [asyncio.create_task(pty_to_browser()), asyncio.create_task(browser_to_pty())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    finally:
        session.remove_viewer(websocket)
        if session.viewer_count == 0:
            manager.close(terminal_id)


def _handle_text_frame(session: WebTerminalSession, text: str) -> None:
    import json

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return
    if payload.get("type") == "resize":
        try:
            session.resize(int(payload.get("cols", session.cols)), int(payload.get("rows", session.rows)))
        except (TypeError, ValueError):
            pass


def _int_query(websocket: WebSocket, name: str) -> int | None:
    value = websocket.query_params.get(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None
