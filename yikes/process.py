from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from .errors import BackendRunError, BackendUnavailable


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise BackendUnavailable(f"required binary not found on PATH: {name}")
    return path


def merged_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if extra:
        env.update(extra)
    return env


def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            list(argv),
            input=input_text,
            text=True,
            capture_output=True,
            cwd=str(cwd),
            env=merged_env(env),
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BackendUnavailable(str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise BackendRunError(
            f"process timed out after {timeout}s: {' '.join(argv)}",
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
        ) from exc

    if proc.returncode != 0:
        raise BackendRunError(
            f"process exited {proc.returncode}: {' '.join(argv)}",
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    return proc
