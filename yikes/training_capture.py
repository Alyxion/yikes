"""Labeled terminal-state capture for improving activity detection.

A well-hidden developer aid: when yikes! misreads a session's state (says
"streaming" while it is idle, "idle" while it is working, misses a selection
prompt, …) you can record what the terminal *actually* looked like together
with the true label. Each sample grabs several rapid full-fidelity snapshots
(raw ANSI, colours and all) so spinner animations are captured, not just a
single still frame, and stamps the backend version so stale samples are
identifiable once a CLI changes its rendering.

The resulting dataset lives in a committed ``training_data/`` directory and is
the foundation for tuning the heuristics in :mod:`yikes.activity` (or, later, a
learned classifier) per backend.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import activity
from .naming import project_label
from .runtime import DurableSessionManager, RuntimeKind
from .sandbox import SandboxManager
from .session_inventory import SessionInventory, SessionLifecycle

# The label space mirrors yikes.activity's states so samples are directly usable
# as ground truth for that classifier.
VALID_LABELS: tuple[str, ...] = (
    activity.IDLE,
    activity.AWAITING_SELECTION,
    activity.THINKING,
    activity.STREAMING,
    activity.UNKNOWN,
)

DEFAULT_FRAME_COUNT = 4
DEFAULT_SPAN_SECONDS = 0.5  # 4 frames spread across ~0.5s catches spinner motion.

_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def training_dir() -> Path:
    """Where labeled samples are written (committed to the repo by default)."""
    override = os.environ.get("YIKES_TRAINING_DIR")
    if override:
        return Path(override).expanduser()
    # repo root in a dev checkout = the directory holding the ``yikes`` package.
    return Path(__file__).resolve().parents[1] / "training_data"


@dataclass(frozen=True)
class _Target:
    session_id: str
    backend: str
    location: str  # "host" | "docker"
    driver: str
    cwd: str
    name: str
    capture_cmd: list[str]
    version_cmd: list[str]
    tmux_version_cmd: list[str]
    size_cmd: list[str]


@dataclass(frozen=True)
class CaptureResult:
    path: Path
    label: str
    predicted: str
    backend: str
    backend_version: str
    frame_count: int


class CaptureError(RuntimeError):
    """Raised when a sample cannot be captured (no/ambiguous session, etc.)."""


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _run(cmd: list[str], *, timeout: float = 5.0) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else (proc.stdout or proc.stderr or "")


def _resolve_target(
    session_ref: str | None,
    *,
    cwd: Path,
    runtime_store: Path | None,
    sandbox_store: Path | None,
) -> _Target:
    lifecycle = SessionLifecycle(runtime_store=runtime_store, sandbox_store=sandbox_store)
    inventory = SessionInventory(runtime_store=runtime_store, sandbox_store=sandbox_store)

    if session_ref:
        resolved = lifecycle.resolve_session_id(session_ref)
        if not resolved:
            raise CaptureError(f"no session matches {session_ref!r}")
    else:
        running = [s for s in inventory.list() if s.state == "running"]
        if not running:
            raise CaptureError("no running session to capture; pass a session id/name")
        here = [s for s in running if s.cwd and Path(s.cwd) == cwd]
        pool = here or running
        if len(pool) != 1:
            names = ", ".join(f"{s.name or s.id} [{s.id}]" for s in pool)
            raise CaptureError(f"ambiguous session; pass one of: {names}")
        resolved = pool[0].id

    durable = DurableSessionManager(runtime_store).get(resolved)
    if durable is not None and durable.runtime.kind is RuntimeKind.TMUX and durable.runtime.tmux_socket:
        socket = durable.runtime.tmux_socket
        sess = durable.runtime.tmux_session or resolved
        backend = durable.backend.value
        return _Target(
            session_id=resolved,
            backend=backend,
            location="host",
            driver=durable.driver.value,
            cwd=str(durable.cwd),
            name=durable.user_data.get("name", "") or project_label(durable.cwd),
            capture_cmd=["tmux", "-S", socket, "capture-pane", "-e", "-p", "-t", sess],
            version_cmd=[backend, "--version"],
            tmux_version_cmd=["tmux", "-V"],
            size_cmd=["tmux", "-S", socket, "display-message", "-p", "-t", sess, "#{pane_width}x#{pane_height}"],
        )

    sandbox = SandboxManager(sandbox_store).get(resolved)
    if sandbox is not None:
        socket = sandbox.meta.user_data.get("tmux_socket")
        sess = sandbox.meta.user_data.get("tmux_session")
        if not socket or not sess:
            raise CaptureError(f"session {resolved} has no tmux pane to capture")
        backend = sandbox.meta.user_data.get("backend", "?")
        container = sandbox.container_name
        host_cwd = sandbox.meta.user_data.get("cwd", "")
        base = ["docker", "exec", container]
        return _Target(
            session_id=resolved,
            backend=backend,
            location="docker",
            driver="tmux",
            cwd=host_cwd,
            name=sandbox.meta.user_data.get("name", "") or (project_label(Path(host_cwd)) if host_cwd else container),
            capture_cmd=[*base, "tmux", "-S", socket, "capture-pane", "-e", "-p", "-t", sess],
            version_cmd=[*base, backend, "--version"],
            tmux_version_cmd=[*base, "tmux", "-V"],
            size_cmd=[*base, "tmux", "-S", socket, "display-message", "-p", "-t", sess, "#{pane_width}x#{pane_height}"],
        )

    raise CaptureError(f"session {resolved} is not an attachable tmux session")


def capture_sample(
    label: str,
    session_ref: str | None = None,
    *,
    cwd: Path | None = None,
    notes: str | None = None,
    frames: int = DEFAULT_FRAME_COUNT,
    span: float = DEFAULT_SPAN_SECONDS,
    runtime_store: Path | None = None,
    sandbox_store: Path | None = None,
    now: datetime | None = None,
) -> CaptureResult:
    """Capture ``frames`` rapid ANSI snapshots of a session and store them labeled."""
    label = label.strip().lower()
    if label not in VALID_LABELS:
        raise CaptureError(f"label must be one of: {', '.join(VALID_LABELS)}")
    if frames < 1:
        raise CaptureError("frames must be >= 1")

    cwd = (cwd or Path.cwd()).resolve()
    target = _resolve_target(session_ref, cwd=cwd, runtime_store=runtime_store, sandbox_store=sandbox_store)

    interval = (span / (frames - 1)) if frames > 1 else 0.0
    captured: list[str] = []
    for i in range(frames):
        if i:
            time.sleep(interval)
        captured.append(_run(target.capture_cmd, timeout=5.0))

    backend_version = (_run(target.version_cmd, timeout=8.0).strip().splitlines() or [""])[0].strip()
    tmux_version = _run(target.tmux_version_cmd, timeout=5.0).strip()
    size = _run(target.size_cmd, timeout=5.0).strip()
    cols = rows = None
    if "x" in size:
        try:
            cols, rows = (int(p) for p in size.split("x", 1))
        except ValueError:
            cols = rows = None

    last_plain = strip_ansi(captured[-1]) if captured else ""
    predicted = activity.classify_terminal_snapshot(last_plain).state

    captured_at = now or datetime.now(timezone.utc)
    stamp = captured_at.strftime("%Y%m%dT%H%M%SZ")
    short = re.sub(r"[^A-Za-z0-9_-]+", "", target.session_id)[:12] or "session"
    out_dir = training_dir() / "samples" / target.backend / f"{stamp}__{label}__{short}"
    out_dir.mkdir(parents=True, exist_ok=True)

    frame_files: list[str] = []
    for i, frame in enumerate(captured, 1):
        fname = f"frame-{i}.ansi"
        (out_dir / fname).write_text(frame, encoding="utf-8")
        frame_files.append(fname)

    meta = {
        "schema": 1,
        "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
        "label": label,
        "predicted": predicted,
        "predicted_matches_label": predicted == label,
        "backend": target.backend,
        "backend_version": backend_version or "unknown",
        "tmux_version": tmux_version or "unknown",
        "location": target.location,
        "driver": target.driver,
        "session": {"id": target.session_id, "name": target.name, "cwd": target.cwd},
        "terminal": {"cols": cols, "rows": rows},
        "frames": frame_files,
        "frame_count": len(frame_files),
        "frame_interval_ms": round(interval * 1000),
        "span_seconds": span,
        "notes": notes or "",
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    return CaptureResult(
        path=out_dir,
        label=label,
        predicted=predicted,
        backend=target.backend,
        backend_version=backend_version or "unknown",
        frame_count=len(frame_files),
    )
