"""Per-session display metadata (emoji icon, custom name, description).

This is stored inside the session's own durable record (the yikes session file
under ~/.yikes/sessions/<id>.json, in its ``user_data``) — not a separate file —
so it travels with the session and is shared by the web UI and the CLI.

Every session gets a distinct emoji so it is recognizable at a glance in the
tabs, the mobile session rail, and the nav drawer. Sessions without one are
assigned a random (unused) emoji on first sight; the choice is persisted. The
user can also give a session a custom name and description from either surface.
"""

from __future__ import annotations

import hashlib
import random

from .runtime import DurableSessionManager

ICON_KEY = "icon"
NAME_KEY = "display_name"
DESC_KEY = "description"

# Single-codepoint, emoji-presentation glyphs (no ZWJ/variation-selector quirks),
# so they render consistently in the browser.
EMOJI_POOL = (
    "🚀", "🦊", "🐙", "🐢", "🦉", "🦁", "🐉", "🦄", "🐳", "🦋",
    "🌵", "🍀", "🔥", "🌙", "🍕", "🎲", "🧩", "🎯", "🤖", "👾",
    "🧠", "💡", "🔭", "🧪", "📦", "🧭", "🌈", "🐝", "🐬", "🦅",
    "🌻", "🍄", "🐸", "🦖", "🐧", "🦦", "🐼", "🦓", "🦛", "🦔",
)


def _stable_icon(session_id: str) -> str:
    """A deterministic emoji for sessions without a durable record (transient)."""
    digest = hashlib.sha1(session_id.encode("utf-8")).hexdigest()
    return EMOJI_POOL[int(digest, 16) % len(EMOJI_POOL)]


class SessionIcons:
    """Reads/writes session display metadata from the durable session store."""

    def __init__(self, manager: DurableSessionManager | None = None) -> None:
        self._manager = manager or DurableSessionManager()

    def meta_for(self, session_id: str) -> dict[str, str]:
        """Return {icon, name?, description?}; assign a random icon if missing."""
        session_id = (session_id or "").strip()
        if not session_id:
            return {"icon": "🟦"}
        meta = self._manager.get(session_id)
        if meta is None:
            return {"icon": _stable_icon(session_id)}
        data = meta.user_data
        result: dict[str, str] = {}
        if data.get(ICON_KEY):
            result["icon"] = data[ICON_KEY]
        if data.get(NAME_KEY):
            result["name"] = data[NAME_KEY]
        if data.get(DESC_KEY):
            result["description"] = data[DESC_KEY]
        if not result.get("icon"):
            result["icon"] = self._assign_icon(meta)
        return result

    def icon_for(self, session_id: str) -> str:
        return self.meta_for(session_id).get("icon", "🟦")

    def update(
        self,
        session_id: str,
        *,
        icon: str | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> dict[str, str] | None:
        """Update provided fields in the session record. Returns the new meta."""
        session_id = (session_id or "").strip()
        meta = self._manager.get(session_id) if session_id else None
        if meta is None:
            return None
        data = meta.user_data
        if icon is not None and icon.strip() and len(icon.strip()) <= 8:
            data[ICON_KEY] = icon.strip()
        if name is not None:
            cleaned = name.strip()[:80]
            data[NAME_KEY] = cleaned if cleaned else ""
            if not cleaned:
                data.pop(NAME_KEY, None)
        if description is not None:
            cleaned = description.strip()[:500]
            data[DESC_KEY] = cleaned if cleaned else ""
            if not cleaned:
                data.pop(DESC_KEY, None)
        self._manager.save(meta)
        return self.meta_for(session_id)

    def set(self, session_id: str, emoji: str) -> str | None:
        """Back-compat: set just the emoji; returns it, or None if invalid/no session."""
        emoji = (emoji or "").strip()
        if not emoji or len(emoji) > 8:
            return None
        return emoji if self.update(session_id, icon=emoji) is not None else None

    def _assign_icon(self, meta) -> str:
        used = {m.user_data.get(ICON_KEY) for m in self._manager.list() if m.user_data.get(ICON_KEY)}
        available = [emoji for emoji in EMOJI_POOL if emoji not in used] or list(EMOJI_POOL)
        chosen = random.choice(available)
        meta.user_data[ICON_KEY] = chosen
        self._manager.save(meta, touch=False)  # don't reorder tabs on auto-assign
        return chosen
