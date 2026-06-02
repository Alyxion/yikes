"""Project naming helpers shared across the runtime.

Kept dependency-free (stdlib only) so any layer — drivers, inventory, web — can
derive the same human-friendly project label without import cycles.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=256)
def _git_root(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def project_label(cwd: "Path | None") -> str:
    """``<git-repo>/<current-folder>`` for a repo subdir, else the folder name."""
    if cwd is None:
        return ""
    cwd = Path(str(cwd))
    root = _git_root(str(cwd))
    if root:
        root_path = Path(root)
        try:
            same = cwd.resolve() == root_path.resolve()
        except OSError:
            same = str(cwd) == root
        return root_path.name if same else f"{root_path.name}/{cwd.name}"
    return cwd.name
