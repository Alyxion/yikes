from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runtime import DEFAULT_YIKES_DIR

_TRUE_VALUES = {"1", "true", "yes", "on", "dev", "debug", "developer"}
_DEFAULT_FILE_BYTES = 2 * 1024 * 1024
_DEFAULT_TOTAL_BYTES = 32 * 1024 * 1024
_DEFAULT_MAX_FILES = 64
_DEFAULT_EVENT_BYTES = 64 * 1024


def tmux_io_logging_enabled() -> bool:
    return any(
        os.environ.get(name, "").strip().lower() in _TRUE_VALUES
        for name in ("YIKES_DEVELOPER_MODE", "YIKES_DEV_MODE", "YIKES_TMUX_IO_LOG")
    )


def tmux_io_log_dir() -> Path:
    return Path(os.environ.get("YIKES_TMUX_IO_LOG_DIR", str(DEFAULT_YIKES_DIR / "debug" / "tmux-io"))).expanduser()


def log_tmux_io(
    session_key: str | None,
    direction: str,
    payload: str,
    *,
    runtime: str,
    backend: str | None = None,
    event: str = "data",
    meta: dict[str, Any] | None = None,
) -> None:
    """Append one tmux I/O event to the developer-only bounded JSONL trace."""
    if not tmux_io_logging_enabled():
        return
    try:
        _append_event(
            session_key or "unknown",
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "runtime": runtime,
                "backend": backend,
                "session": session_key,
                "direction": direction,
                "event": event,
                "payload": _limit_text(payload, _int_env("YIKES_TMUX_IO_LOG_EVENT_BYTES", _DEFAULT_EVENT_BYTES)),
                "meta": meta or {},
            },
        )
    except Exception:
        return


def _append_event(session_key: str, event: dict[str, Any]) -> None:
    log_dir = tmux_io_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        log_dir.chmod(0o700)
    except OSError:
        pass
    path = log_dir / f"{_safe_name(session_key)}.jsonl"
    line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8", errors="replace"))
    _trim_file(path, _int_env("YIKES_TMUX_IO_LOG_FILE_BYTES", _DEFAULT_FILE_BYTES))
    _clean_directory(
        log_dir,
        max_files=_int_env("YIKES_TMUX_IO_LOG_MAX_FILES", _DEFAULT_MAX_FILES),
        max_bytes=_int_env("YIKES_TMUX_IO_LOG_TOTAL_BYTES", _DEFAULT_TOTAL_BYTES),
    )


def _trim_file(path: Path, max_bytes: int) -> None:
    max_bytes = max(256, max_bytes)
    try:
        if path.stat().st_size <= max_bytes:
            return
        with path.open("rb") as handle:
            handle.seek(-max_bytes, os.SEEK_END)
            data = handle.read()
        newline = data.find(b"\n")
        if newline != -1 and newline + 1 < len(data):
            data = data[newline + 1 :]
        with path.open("wb") as handle:
            handle.write(data[-max_bytes:])
    except OSError:
        return


def _clean_directory(log_dir: Path, *, max_files: int, max_bytes: int) -> None:
    max_files = max(1, max_files)
    max_bytes = max(256, max_bytes)
    try:
        files = sorted(log_dir.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return
    for path in files[max_files:]:
        _unlink(path)
    files = [path for path in files[:max_files] if path.exists()]
    total = 0
    sized: list[tuple[Path, int, float]] = []
    for path in files:
        try:
            size = path.stat().st_size
            mtime = path.stat().st_mtime
        except OSError:
            continue
        sized.append((path, size, mtime))
        total += size
    for path, size, _mtime in sorted(sized, key=lambda item: item[2]):
        if total <= max_bytes:
            break
        _unlink(path)
        total -= size


def _safe_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())[:120].strip("._-")
    return name or "unknown"


def _limit_text(value: str, max_bytes: int) -> str:
    max_bytes = max(256, max_bytes)
    data = value.encode("utf-8", errors="replace")
    if len(data) <= max_bytes:
        return value
    omitted = len(data) - max_bytes
    tail = data[-max_bytes:].decode("utf-8", errors="replace")
    return f"[truncated {omitted} bytes]\n{tail}"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        return
