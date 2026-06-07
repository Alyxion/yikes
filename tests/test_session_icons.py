from __future__ import annotations

from yikes.session_icons import EMOJI_POOL, SessionIcons


def test_assigns_and_persists_random_emoji(tmp_path):
    path = tmp_path / "icons.json"
    icons = SessionIcons(path)
    first = icons.icon_for("sess-a")
    assert first in EMOJI_POOL
    # Stable across calls and across reloads (persisted).
    assert icons.icon_for("sess-a") == first
    assert SessionIcons(path).icon_for("sess-a") == first


def test_distinct_emojis_until_pool_exhausts(tmp_path):
    icons = SessionIcons(tmp_path / "icons.json")
    assigned = [icons.icon_for(f"s{i}") for i in range(len(EMOJI_POOL))]
    assert len(set(assigned)) == len(EMOJI_POOL)  # no collisions while the pool lasts


def test_set_overrides_and_persists(tmp_path):
    path = tmp_path / "icons.json"
    icons = SessionIcons(path)
    icons.icon_for("s1")
    assert icons.set("s1", "🎧") == "🎧"
    assert icons.icon_for("s1") == "🎧"
    assert SessionIcons(path).icon_for("s1") == "🎧"


def test_set_rejects_empty_or_oversized(tmp_path):
    icons = SessionIcons(tmp_path / "icons.json")
    assert icons.set("s1", "") is None
    assert icons.set("", "🎧") is None
    assert icons.set("s1", "x" * 20) is None


def test_icon_for_blank_session_is_safe(tmp_path):
    icons = SessionIcons(tmp_path / "icons.json")
    assert isinstance(icons.icon_for(""), str) and icons.icon_for("")
