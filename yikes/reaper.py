from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta, timezone

from .sandbox import VOLUME_PREFIX, SandboxManager


def reap_expired(manager: SandboxManager) -> list[str]:
    destroyed: list[str] = []
    now = datetime.now(timezone.utc)
    for session in manager.list_sessions():
        last = datetime.fromisoformat(session.meta.last_active)
        if now - last > timedelta(minutes=session.meta.config.timeout_minutes):
            session.destroy()
            destroyed.append(session.id)
    return destroyed


def reap_by_count(manager: SandboxManager, max_sessions: int) -> list[str]:
    sessions = manager.list_sessions()
    if len(sessions) <= max_sessions:
        return []
    destroyed: list[str] = []
    for session in list(reversed(sessions))[: len(sessions) - max_sessions]:
        session.destroy()
        destroyed.append(session.id)
    return destroyed


def reap_by_space(manager: SandboxManager, max_bytes: int) -> list[str]:
    volumes = _get_volume_sizes()
    total = sum(volumes.values())
    if total <= max_bytes:
        return []
    destroyed: list[str] = []
    for session in reversed(manager.list_sessions()):
        if total <= max_bytes:
            break
        size = volumes.get(session.volume_name, 0)
        session.destroy()
        destroyed.append(session.id)
        total -= size
    return destroyed


def reap(
    manager: SandboxManager,
    *,
    max_sessions: int | None = None,
    max_bytes: int | None = None,
) -> list[str]:
    destroyed = reap_expired(manager)
    if max_sessions is not None:
        destroyed.extend(reap_by_count(manager, max_sessions))
    if max_bytes is not None:
        destroyed.extend(reap_by_space(manager, max_bytes))
    return destroyed


def _get_volume_sizes() -> dict[str, int]:
    result = subprocess.run(["docker", "system", "df", "-v"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return {}
    sizes: dict[str, int] = {}
    in_volumes = False
    for line in result.stdout.splitlines():
        if "VOLUME NAME" in line:
            in_volumes = True
            continue
        if not in_volumes:
            continue
        if not line.strip():
            break
        parts = line.split()
        if len(parts) >= 3 and parts[0].startswith(VOLUME_PREFIX):
            sizes[parts[0]] = _parse_size(parts[-1])
    return sizes


def _parse_size(value: str) -> int:
    match = re.match(r"([\d.]+)\s*([KMGT]?B)", value, re.IGNORECASE)
    if not match:
        return 0
    amount = float(match.group(1))
    unit = match.group(2).upper()
    multipliers = {
        "B": 1,
        "KB": 1024,
        "MB": 1024**2,
        "GB": 1024**3,
        "TB": 1024**4,
    }
    return int(amount * multipliers.get(unit, 1))
