"""Per-project yikes configuration loaded from ``yikes.toml``.

A project drops a committed ``yikes.toml`` at its root to set defaults for the
one-word launchers (``yikes claude`` / ``yikes codex``): which backend to
prefer, whether to run isolated in Docker, which HTTP ports to publish, and an
optional session name. A sibling ``yikes.local.toml`` (gitignored) overlays
personal overrides on top of the shared file.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "yikes.toml"
LOCAL_CONFIG_NAME = "yikes.local.toml"


@dataclass(frozen=True)
class ProjectConfig:
    """Resolved per-project defaults. Every field is optional."""

    backend: str | None = None
    isolated: bool = False
    ports: tuple[tuple[str, str], ...] = ()
    name: str | None = None
    model: str | None = None
    source: Path | None = None


def find_project_config(start: Path) -> Path | None:
    """Walk upward from ``start`` and return the first ``yikes.toml`` found."""
    start = start.expanduser().resolve()
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def load_project_config(cwd: Path) -> ProjectConfig:
    """Load ``yikes.toml`` (with a ``yikes.local.toml`` overlay) for ``cwd``.

    A missing file yields a default :class:`ProjectConfig`.
    """
    config_path = find_project_config(cwd)
    if config_path is None:
        return ProjectConfig()
    data = _read_toml(config_path)
    local_path = config_path.with_name(LOCAL_CONFIG_NAME)
    if local_path.is_file():
        data.update(_read_toml(local_path))
    return ProjectConfig(
        backend=_optional_str(data.get("backend")),
        isolated=bool(data.get("isolated", False)),
        ports=_normalize_ports(data.get("ports")),
        name=_optional_str(data.get("name")),
        model=_optional_str(data.get("model")),
        source=config_path,
    )


def normalize_port(value: object) -> tuple[str, str]:
    """Normalize a single port entry to a ``(host, container)`` pair.

    Accepts an int (``8080`` -> ``("8080", "8080")``) or a string that is either
    a bare port or ``"host:container"``.
    """
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise ValueError(f"invalid port: {value!r}")
    if isinstance(value, int):
        port = str(value)
        return port, port
    if isinstance(value, str):
        host, sep, container = value.partition(":")
        host = host.strip()
        container = container.strip() if sep else host
        if host and container:
            return host, container
    raise ValueError(f"invalid port: {value!r}")


def starter_toml() -> str:
    """The template written by ``yikes init``."""
    return (
        "# yikes! project defaults for `yikes claude` / `yikes codex`.\n"
        '# backend  = "claude"      # preferred backend in this project\n'
        "# isolated = false         # run in Docker by default?\n"
        "# ports    = [8080, 5173]  # published 127.0.0.1:PORT -> container when isolated\n"
        '# name     = "shop"        # session name (default: directory basename)\n'
        '# model    = ""            # backend model override\n'
    )


def _read_toml(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _normalize_ports(value: object) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("yikes.toml: 'ports' must be a list")
    return tuple(normalize_port(item) for item in value)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
