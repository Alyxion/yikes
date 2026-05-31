from __future__ import annotations

from pathlib import Path

import pytest

from yikes.project_config import (
    append_local_pane,
    find_project_config,
    load_project_config,
    normalize_port,
    starter_toml,
)


def test_local_panes_are_additive_and_allow_literal_host(tmp_path: Path) -> None:
    (tmp_path / "yikes.toml").write_text('[[panes]]\nkind = "web"\ntitle = "App"\nport = 5173\n')
    append_local_pane(tmp_path, {"kind": "web", "title": "Mine", "url": "http://192.168.1.9:3000"})

    config = load_project_config(tmp_path)

    assert [p["title"] for p in config.panes] == ["App", "Mine"]
    assert config.panes[1]["url"] == "http://192.168.1.9:3000"  # literal host allowed in local file


def test_local_only_config_is_loaded_without_committed_yikes_toml(tmp_path: Path) -> None:
    append_local_pane(tmp_path, {"kind": "web", "title": "Solo", "port": 9000})

    config = load_project_config(tmp_path)
    assert len(config.panes) == 1
    assert config.panes[0]["title"] == "Solo"


def test_missing_config_returns_defaults(tmp_path: Path) -> None:
    config = load_project_config(tmp_path)

    assert config.backend is None
    assert config.isolated is False
    assert config.ports == ()
    assert config.source is None


def test_discovery_walks_up_to_parent(tmp_path: Path) -> None:
    (tmp_path / "yikes.toml").write_text('backend = "codex"\n')
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)

    found = find_project_config(nested)
    assert found == tmp_path / "yikes.toml"

    config = load_project_config(nested)
    assert config.backend == "codex"


def test_config_parses_isolated_and_ports(tmp_path: Path) -> None:
    (tmp_path / "yikes.toml").write_text(
        'backend = "claude"\nisolated = true\nports = [8080, "5173:5173", "127.0.0.1"]\nname = "shop"\n'
    )

    config = load_project_config(tmp_path)

    assert config.backend == "claude"
    assert config.isolated is True
    assert config.name == "shop"
    assert config.ports == (("8080", "8080"), ("5173", "5173"), ("127.0.0.1", "127.0.0.1"))


def test_local_overlay_wins(tmp_path: Path) -> None:
    (tmp_path / "yikes.toml").write_text('backend = "claude"\nisolated = false\n')
    (tmp_path / "yikes.local.toml").write_text('backend = "codex"\nisolated = true\n')

    config = load_project_config(tmp_path)

    assert config.backend == "codex"
    assert config.isolated is True


def test_normalize_port_accepts_int_and_string() -> None:
    assert normalize_port(8080) == ("8080", "8080")
    assert normalize_port("3000:80") == ("3000", "80")
    assert normalize_port("9000") == ("9000", "9000")
    with pytest.raises(ValueError):
        normalize_port(True)


def test_config_parses_web_pane_and_links(tmp_path: Path) -> None:
    (tmp_path / "yikes.toml").write_text(
        "[[panes]]\n"
        'kind = "web"\n'
        'title = "App"\n'
        "port = 5173\n"
        "\n"
        "[[panes]]\n"
        'kind = "data"\n'
        'title = "Health"\n'
        'source = "builtin:health"\n'
        "refresh = 3\n"
        "\n"
        "[[links]]\n"
        'title = "Docs"\n'
        "port = 8000\n"
    )

    config = load_project_config(tmp_path)

    assert len(config.panes) == 2
    assert config.panes[0]["kind"] == "web"
    assert config.panes[0]["title"] == "App"
    assert config.panes[0]["port"] == "5173"
    assert config.panes[1]["kind"] == "data"
    assert config.panes[1]["refresh"] == 3
    assert config.links[0]["title"] == "Docs"
    assert config.links[0]["port"] == "8000"


def test_config_rejects_literal_host_in_pane_url(tmp_path: Path) -> None:
    (tmp_path / "yikes.toml").write_text('[[panes]]\nkind = "web"\nurl = "http://192.168.1.5:5173"\n')

    with pytest.raises(ValueError):
        load_project_config(tmp_path)


def test_config_allows_host_placeholder_in_pane_url(tmp_path: Path) -> None:
    (tmp_path / "yikes.toml").write_text('[[panes]]\nkind = "web"\nurl = "http://{host}:{port}/admin"\nport = 8080\n')

    config = load_project_config(tmp_path)
    assert config.panes[0]["url"] == "http://{host}:{port}/admin"


def test_starter_toml_is_commented_template() -> None:
    text = starter_toml()
    assert "backend" in text
    assert text.lstrip().startswith("#")
