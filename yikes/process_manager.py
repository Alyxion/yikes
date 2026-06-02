"""Managed auxiliary processes for session panes (e.g. a dev server).

A web pane may declare a ``start`` (and optional ``stop``) command. yikes runs
it as a tracked subprocess in the session's working directory so the UI can show
a start/stop control and a live status, separate from the agent's own terminal.
The processes live in the web-server process and are keyed by session+pane.
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass


@dataclass
class _Managed:
    command: str
    cwd: str
    stop_command: str | None = None
    process: subprocess.Popen | None = None
    status: str = "stopped"  # stopped | running | failed
    error: str = ""


class ManagedProcessManager:
    """Start/stop/track per-pane auxiliary processes."""

    def __init__(self) -> None:
        self._procs: dict[str, _Managed] = {}

    @staticmethod
    def key(session_id: str, pane_id: str) -> str:
        return f"{session_id}:{pane_id}"

    def start(self, key: str, command: str, cwd: str, *, stop_command: str | None = None) -> dict:
        current = self._procs.get(key)
        if current and current.process is not None and current.process.poll() is None:
            return self._row(key, current)  # already running
        managed = _Managed(command=command, cwd=cwd, stop_command=stop_command)
        self._procs[key] = managed
        try:
            managed.process = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd or None,
                start_new_session=True,  # own process group so stop() kills children too
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            managed.status = "running"
        except OSError as exc:
            managed.status = "failed"
            managed.error = str(exc)
        return self._row(key, managed)

    def stop(self, key: str) -> dict:
        managed = self._procs.get(key)
        if managed is None or managed.process is None:
            return {"status": "stopped"}
        if managed.process.poll() is None:
            try:
                os.killpg(os.getpgid(managed.process.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    managed.process.terminate()
                except OSError:
                    pass
        managed.status = "stopped"
        return self._row(key, managed)

    def stop_all(self) -> None:
        for key in list(self._procs):
            self.stop(key)

    def snapshot(self) -> dict[str, dict]:
        return {key: self._row(key, managed) for key, managed in self._procs.items()}

    def _row(self, key: str, managed: _Managed) -> dict:
        proc = managed.process
        if proc is not None and managed.status == "running" and proc.poll() is not None:
            managed.status = "stopped" if proc.returncode == 0 else "failed"
        row = {"status": managed.status}
        if managed.error:
            row["error"] = managed.error
        return row
