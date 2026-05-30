from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolate_yikes_home(request, tmp_path_factory, monkeypatch):
    """Keep unit tests away from the real ~/.yikes stores.

    The durable-session, sandbox, app-state, and prompt-profile locations all
    default under the user's home, so a leftover real session (e.g. a Docker
    sandbox) would otherwise leak into controller/session tests. Point every
    store at a per-test temp dir. Integration tests opt out — they may rely on
    real local state.
    """
    if request.node.get_closest_marker("integration"):
        return
    home = tmp_path_factory.mktemp("yikes-home")
    monkeypatch.setenv("YIKES_RUNTIME_STORE", str(home / "sessions"))
    monkeypatch.setenv("YIKES_SANDBOX_STORE", str(home / "sandboxes"))
    monkeypatch.setenv("YIKES_STATE_PATH", str(home / "state.json"))
    monkeypatch.setenv("YIKES_PROMPT_PROFILE_PATH", str(home / "prompt-profile.json"))
    monkeypatch.setenv("YIKES_WEB_ENV", str(home / "web-auth.env"))
