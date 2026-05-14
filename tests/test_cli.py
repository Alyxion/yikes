from __future__ import annotations

from pathlib import Path

from yikes import AgentSettings, Backend, Complexity, Driver
from yikes import cli
import yikes.tui


def test_no_args_launches_tui_by_default(monkeypatch) -> None:
    called: dict[str, object] = {}

    def fake_run_tui(
        *,
        backend: Backend | None,
        driver: Driver | None,
        cwd: Path,
        timeout: float,
        model: str | None,
        complexity: Complexity | None,
        settings: AgentSettings | None,
    ) -> None:
        called.update(
            backend=backend,
            driver=driver,
            cwd=cwd,
            timeout=timeout,
            model=model,
            complexity=complexity,
            settings=settings,
        )

    monkeypatch.setattr(yikes.tui, "run_tui", fake_run_tui)

    assert cli.main([]) == 0
    assert called["backend"] is None
    assert called["driver"] is None
    assert called["complexity"] is None
    assert called["settings"] is None


def test_tui_rejects_remote_control_chat_mode(monkeypatch) -> None:
    def fake_run_tui(**_kwargs: object) -> None:
        raise AssertionError("run_tui should not be called")

    monkeypatch.setattr(yikes.tui, "run_tui", fake_run_tui)

    assert cli.main(["tui", "--driver", "remote-control"]) == 1
