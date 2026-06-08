from __future__ import annotations

from yikes.domain import Backend, Driver
from yikes.runtime import DurableSessionManager, RuntimeKind, RuntimeRef
from yikes.session_icons import EMOJI_POOL, SessionIcons, _stable_icon


def _manager(tmp_path):
    return DurableSessionManager(tmp_path / "sessions")


def _new_session(manager, session_id):
    return manager.create(
        backend=Backend.CLAUDE,
        driver=Driver.DIRECT,
        runtime=RuntimeRef(kind=RuntimeKind.DIRECT),
        cwd=manager.store_dir,
        session_id=session_id,
    )


def test_assigns_and_persists_in_session_record(tmp_path):
    manager = _manager(tmp_path)
    _new_session(manager, "yik_a")
    icons = SessionIcons(manager)
    first = icons.icon_for("yik_a")
    assert first in EMOJI_POOL
    # Stored in the session's own durable record (user_data), and stable.
    assert manager.get("yik_a").user_data["icon"] == first
    assert SessionIcons(_manager(tmp_path)).icon_for("yik_a") == first


def test_distinct_emojis_until_pool_exhausts(tmp_path):
    manager = _manager(tmp_path)
    icons = SessionIcons(manager)
    for i in range(len(EMOJI_POOL)):
        _new_session(manager, f"s{i}")
    assigned = [icons.icon_for(f"s{i}") for i in range(len(EMOJI_POOL))]
    assert len(set(assigned)) == len(EMOJI_POOL)


def test_update_name_description_and_icon_persist(tmp_path):
    manager = _manager(tmp_path)
    _new_session(manager, "s1")
    icons = SessionIcons(manager)
    icons.update("s1", icon="🚀", name="Cost Scanner", description="Azure cost audit")
    reloaded = SessionIcons(_manager(tmp_path)).meta_for("s1")
    assert reloaded["icon"] == "🚀" and reloaded["name"] == "Cost Scanner" and reloaded["description"] == "Azure cost audit"


def test_update_empty_name_clears_override(tmp_path):
    manager = _manager(tmp_path)
    _new_session(manager, "s1")
    icons = SessionIcons(manager)
    icons.update("s1", name="Custom")
    assert icons.meta_for("s1").get("name") == "Custom"
    icons.update("s1", name="   ")
    assert "name" not in icons.meta_for("s1")


def test_auto_icon_assignment_does_not_reorder(tmp_path):
    # Assigning an icon must not bump updated_at (it would reshuffle the tabs).
    manager = _manager(tmp_path)
    _new_session(manager, "s1")
    before = manager.get("s1").updated_at
    SessionIcons(manager).icon_for("s1")
    assert manager.get("s1").updated_at == before


def test_unknown_session_gets_stable_icon_without_persisting(tmp_path):
    icons = SessionIcons(_manager(tmp_path))
    # No durable record → deterministic icon, and nothing is written.
    assert icons.icon_for("ghost") == _stable_icon("ghost")
    assert icons.update("ghost", name="x") is None


def test_set_back_compat(tmp_path):
    manager = _manager(tmp_path)
    _new_session(manager, "s1")
    icons = SessionIcons(manager)
    assert icons.set("s1", "🎧") == "🎧"
    assert icons.set("s1", "") is None
    assert icons.set("nope", "🎧") is None  # no such session
