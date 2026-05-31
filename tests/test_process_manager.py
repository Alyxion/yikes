from __future__ import annotations

import time
from pathlib import Path

from yikes.process_manager import ManagedProcessManager


def _wait_status(mgr: ManagedProcessManager, key: str, target: str, timeout: float = 3.0) -> str:
    deadline = time.monotonic() + timeout
    status = mgr.snapshot().get(key, {}).get("status", "")
    while status != target and time.monotonic() < deadline:
        time.sleep(0.05)
        status = mgr.snapshot().get(key, {}).get("status", "")
    return status


def test_managed_process_start_and_stop(tmp_path: Path) -> None:
    mgr = ManagedProcessManager()
    key = ManagedProcessManager.key("sess", "pane-0")

    row = mgr.start(key, "sleep 30", str(tmp_path))
    assert row["status"] == "running"
    assert mgr.snapshot()[key]["status"] == "running"

    mgr.stop(key)
    assert mgr.snapshot()[key]["status"] == "stopped"


def test_managed_process_marks_quick_exit(tmp_path: Path) -> None:
    mgr = ManagedProcessManager()
    key = ManagedProcessManager.key("sess", "pane-1")

    mgr.start(key, "true", str(tmp_path))  # exits 0 immediately
    assert _wait_status(mgr, key, "stopped") == "stopped"


def test_managed_process_marks_failure(tmp_path: Path) -> None:
    mgr = ManagedProcessManager()
    key = ManagedProcessManager.key("sess", "pane-2")

    mgr.start(key, "exit 3", str(tmp_path))  # non-zero exit
    assert _wait_status(mgr, key, "failed") == "failed"
