"""Per-project yikes configuration loaded from ``yikes.toml``.

A project drops a committed ``yikes.toml`` at its root to set defaults for the
one-word launchers (``yikes claude`` / ``yikes codex``): which backend to
prefer, whether to run isolated in Docker, which HTTP ports to publish, and an
optional session name. A sibling ``yikes.local.toml`` (gitignored) overlays
personal overrides on top of the shared file.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
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
    panes: tuple[dict, ...] = ()
    links: tuple[dict, ...] = ()
    source: Path | None = None


def find_project_config(start: Path) -> Path | None:
    """Walk upward from ``start`` and return the first ``yikes.toml`` found."""
    start = start.expanduser().resolve()
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def _find_config_dir(start: Path) -> Path | None:
    start = start.expanduser().resolve()
    for directory in (start, *start.parents):
        if (directory / CONFIG_NAME).is_file() or (directory / LOCAL_CONFIG_NAME).is_file():
            return directory
    return None


_config_cache: dict[str, tuple[tuple, "ProjectConfig"]] = {}


def load_project_config(cwd: Path) -> ProjectConfig:
    """Load ``yikes.toml`` plus a ``yikes.local.toml`` overlay for ``cwd``.

    Scalars in the gitignored local file win; ``panes``/``links`` are additive
    (committed entries first, then local). A missing file yields defaults.
    Results are cached by file mtimes so repeated polls don't re-parse.
    """
    directory = _find_config_dir(cwd)
    if directory is None:
        return ProjectConfig()
    main_path = directory / CONFIG_NAME
    local_path = directory / LOCAL_CONFIG_NAME
    signature = (
        main_path.stat().st_mtime_ns if main_path.is_file() else None,
        local_path.stat().st_mtime_ns if local_path.is_file() else None,
    )
    cached = _config_cache.get(str(directory))
    if cached is not None and cached[0] == signature:
        return cached[1]
    config = _build_project_config(directory, main_path, local_path)
    _config_cache[str(directory)] = (signature, config)
    return config


def _build_project_config(directory: Path, main_path: Path, local_path: Path) -> ProjectConfig:
    base = _read_toml(main_path) if main_path.is_file() else {}
    local = _read_toml(local_path) if local_path.is_file() else {}
    scalars = {**base, **local}
    # Committed config may not hardcode a host; the gitignored local file may.
    panes = _normalize_panes(base.get("panes")) + _normalize_panes(local.get("panes"), allow_literal_host=True)
    links = _normalize_links(base.get("links")) + _normalize_links(local.get("links"), allow_literal_host=True)
    return ProjectConfig(
        backend=_optional_str(scalars.get("backend")),
        isolated=bool(scalars.get("isolated", False)),
        ports=_normalize_ports(scalars.get("ports")),
        name=_optional_str(scalars.get("name")),
        model=_optional_str(scalars.get("model")),
        panes=panes,
        links=links,
        source=directory / CONFIG_NAME,
    )


def append_local_pane(cwd: Path, pane: dict) -> Path:
    """Append a pane to the gitignored ``yikes.local.toml`` next to ``cwd``."""
    path = Path(cwd).expanduser() / LOCAL_CONFIG_NAME
    chunk: list[str] = []
    if not path.exists():
        chunk.append("# yikes! personal panes (gitignored). Added at runtime.\n")
    chunk.append("\n[[panes]]\n")
    for key in ("kind", "title", "url", "port", "path", "start", "stop", "source", "refresh"):
        value = pane.get(key)
        if value is None:
            continue
        if isinstance(value, int) and not isinstance(value, bool):
            chunk.append(f"{key} = {value}\n")
        else:
            escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
            chunk.append(f'{key} = "{escaped}"\n')
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(chunk))
    return path


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
        "\n"
        "# Web UI sub-tabs (panes). Use a port or {host}/{port} — never a literal IP.\n"
        "# [[panes]]\n"
        '# kind  = "web"\n'
        '# title = "App"\n'
        "# port  = 5173\n"
        "# start = \"npm run dev\"   # optional: yikes runs/stops this process\n"
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


# A committed config must never hardcode a host/IP; web targets are declared by
# port (or a {host} template) and resolved at runtime to the browser's host.
def _has_literal_host(url: str) -> bool:
    match = re.match(r"^[a-zA-Z][\w+.-]*://([^/:\s]+)", url)
    if not match:
        return False  # relative path or no scheme -> no host to leak
    return match.group(1) != "{host}"


def _normalize_panes(value: object, *, allow_literal_host: bool = False) -> tuple[dict, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("yikes.toml: 'panes' must be an array of tables ([[panes]])")
    panes: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "web")).strip().lower()
        title = _optional_str(item.get("title")) or kind.capitalize()
        url = _optional_str(item.get("url"))
        if url and not allow_literal_host and _has_literal_host(url):
            raise ValueError(
                "yikes.toml: a pane 'url' must not contain a literal host/IP "
                "(it is committed and machine-specific). Use 'port' or the {host} placeholder."
            )
        pane: dict = {
            "kind": kind,
            "title": title,
            "url": url,
            "port": _optional_str(item.get("port")),
            "path": _optional_str(item.get("path")),
        }
        if kind == "web":
            pane["autostart"] = bool(item.get("autostart", False))
            pane["start"] = _optional_str(item.get("start"))
            pane["stop"] = _optional_str(item.get("stop"))
        elif kind == "data":
            pane["source"] = _optional_str(item.get("source"))
            pane["refresh"] = max(1, int(item.get("refresh", 5)))
        panes.append(pane)
    return tuple(panes)


def _normalize_links(value: object, *, allow_literal_host: bool = False) -> tuple[dict, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("yikes.toml: 'links' must be an array of tables ([[links]])")
    links: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = _optional_str(item.get("url"))
        if url and not allow_literal_host and _has_literal_host(url):
            raise ValueError(
                "yikes.toml: a link 'url' must not contain a literal host/IP. Use 'port' or {host}."
            )
        links.append(
            {
                "title": _optional_str(item.get("title")) or "Link",
                "url": url,
                "port": _optional_str(item.get("port")),
            }
        )
    return tuple(links)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
