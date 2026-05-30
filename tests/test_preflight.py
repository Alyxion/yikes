from __future__ import annotations

from pathlib import Path

import pytest

from yikes.preflight import (
    SCAN_PROMPT,
    parse_scan_result,
    ports_from_scan,
    render_panel,
    scan_prompt,
    synthesize_config,
)
from yikes.project_config import load_project_config


def test_panel_always_offers_initial_prompt_and_echoes_goal() -> None:
    panel = render_panel(
        backend="claude",
        name="api",
        location="host",
        cwd="/srv/api",
        reused=False,
        config_source=None,
        ports=(),
        isolated=False,
        goal="build a NiceGUI dashboard",
    )

    assert "[p] add an initial prompt" in panel
    assert "build a NiceGUI dashboard" in panel


def test_scan_prompt_folds_in_goal() -> None:
    assert scan_prompt(None) == SCAN_PROMPT
    biased = scan_prompt("a vite app on 5173")
    assert SCAN_PROMPT in biased
    assert "a vite app on 5173" in biased


def test_render_panel_shows_commands_and_ports() -> None:
    panel = render_panel(
        backend="claude",
        name="shop",
        location="docker",
        cwd="/home/me/shop",
        reused=True,
        config_source="/home/me/shop/yikes.toml",
        ports=(("8080", "8080"), ("5173", "5173")),
        isolated=True,
    )

    assert "Ctrl-b d" in panel
    assert "yikes claude" in panel
    assert "yikes close shop" in panel
    assert "reattach" in panel
    assert "8080" in panel and "5173" in panel


def test_render_panel_marks_unconfigured_host() -> None:
    panel = render_panel(
        backend="codex",
        name="api",
        location="host",
        cwd="/srv/api",
        reused=False,
        config_source=None,
        ports=(),
        isolated=False,
    )

    assert "not configured yet" in panel
    assert "new" in panel
    assert "ports" not in panel  # host launches do not show a ports line


def test_parse_scan_result_extracts_json_from_noise() -> None:
    text = 'Sure! Here is what I found:\n{"ports": [8080, 5173], "backend": "codex", "notes": "vite"}\nHope that helps.'
    data = parse_scan_result(text)

    assert ports_from_scan(data) == (8080, 5173)
    assert data["backend"] == "codex"


def test_parse_scan_result_rejects_non_json() -> None:
    with pytest.raises(ValueError):
        parse_scan_result("no json here")


def test_ports_from_scan_dedupes_and_filters() -> None:
    assert ports_from_scan({"ports": [8080, 8080, "5173", 0, 99999, "x"]}) == (8080, 5173)
    assert ports_from_scan({}) == ()


def test_synthesize_config_round_trips(tmp_path: Path) -> None:
    content = synthesize_config((8080, 5173), "codex")
    (tmp_path / "yikes.toml").write_text(content)

    config = load_project_config(tmp_path)
    assert config.backend == "codex"
    assert config.ports == (("8080", "8080"), ("5173", "5173"))


def test_synthesize_config_without_ports(tmp_path: Path) -> None:
    content = synthesize_config((), None)
    (tmp_path / "yikes.toml").write_text(content)

    config = load_project_config(tmp_path)
    assert config.ports == ()
    assert config.isolated is False
