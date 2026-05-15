from __future__ import annotations

import os
import re
import shutil

import pytest

from yikes import Backend, Chatbot, Driver
from yikes.errors import BackendUnavailable, DriverUnavailable


pytestmark = pytest.mark.integration


MATRIX = [
    (Backend.CLAUDE, Driver.DIRECT),
    (Backend.CLAUDE, Driver.TMUX),
    (Backend.CLAUDE, Driver.DOCKER),
    (Backend.CODEX, Driver.DIRECT),
    (Backend.CODEX, Driver.TMUX),
    (Backend.CODEX, Driver.DOCKER),
]


def _integration_enabled() -> bool:
    return os.environ.get("YIKES_RUN_E2E") == "1"


def _require_binary(name: str) -> None:
    if not shutil.which(name):
        pytest.skip(f"{name} is not installed")


@pytest.mark.parametrize(("backend", "driver"), MATRIX, ids=lambda x: x.value)
def test_chatbot_remembers_name_and_calculates(backend: Backend, driver: Driver) -> None:
    if not _integration_enabled():
        pytest.skip("set YIKES_RUN_E2E=1 to run real backend integration tests")

    _require_binary(backend.value)
    if driver is Driver.TMUX:
        _require_binary("tmux")
    if driver is Driver.DOCKER:
        _require_binary("docker")

    bot = Chatbot(backend, driver, timeout=float(os.environ.get("YIKES_E2E_TIMEOUT", "240")))
    try:
        result = bot.run_goal_flow()
    except (BackendUnavailable, DriverUnavailable) as exc:
        pytest.skip(str(exc))

    assert result.greeting.strip()
    assert re.search(r"\b8\b", result.calculation), result.calculation
    assert _single_word(result.remembered_name) == "Michael"


def _single_word(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z]", " ", text).strip()
    words = cleaned.split()
    assert len(words) == 1, f"expected one word, got {text!r}"
    return words[0]
