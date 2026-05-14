from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


def test_cli_chat_smoke_json_contract() -> None:
    if os.environ.get("YIKES_RUN_E2E") != "1":
        pytest.skip("set YIKES_RUN_E2E=1 to run real backend integration tests")

    env = os.environ.copy()
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = f"{root}{os.pathsep}{env.get('PYTHONPATH', '')}"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "yikes.cli",
            "chat-smoke",
            "--backend",
            os.environ.get("YIKES_E2E_BACKEND", "claude"),
            "--driver",
            os.environ.get("YIKES_E2E_DRIVER", "direct"),
            "--json",
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=float(os.environ.get("YIKES_E2E_TIMEOUT", "240")),
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["remembered_name"]
    assert len(payload["turns"]) == 3
