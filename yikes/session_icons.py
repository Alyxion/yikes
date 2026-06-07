"""Per-session emoji icons, persisted across runs.

Every session gets a distinct emoji so it is recognizable at a glance in the
tabs, the mobile session rail, and the nav drawer. Sessions that predate this
feature are assigned a random (unused) emoji on first sight; the choice is
persisted so it stays stable. The user can change a session's emoji from the UI.
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

# Single-codepoint, emoji-presentation glyphs (no ZWJ/variation-selector quirks),
# so they render consistently in the browser.
EMOJI_POOL = (
    "🚀", "🦊", "🐙", "🐢", "🦉", "🦁", "🐉", "🦄", "🐳", "🦋",
    "🌵", "🍀", "🔥", "🌙", "🍕", "🎲", "🧩", "🎯", "🤖", "👾",
    "🧠", "💡", "🔭", "🧪", "📦", "🧭", "🌈", "🐝", "🐬", "🦅",
    "🌻", "🍄", "🐸", "🦖", "🐧", "🦦", "🐼", "🦓", "🦛", "🦔",
)


def default_icons_path() -> Path:
    override = os.environ.get("YIKES_SESSION_ICONS")
    if override:
        return Path(override).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "yikes" / "session-icons.json"


class SessionIcons:
    """In-memory cache of session_id → emoji, persisted best-effort to JSON."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_icons_path()
        self._icons: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._icons, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass

    def icon_for(self, session_id: str) -> str:
        """Return this session's emoji, assigning a random unused one if needed."""
        session_id = (session_id or "").strip()
        if not session_id:
            return "🟦"
        existing = self._icons.get(session_id)
        if existing:
            return existing
        used = set(self._icons.values())
        available = [emoji for emoji in EMOJI_POOL if emoji not in used] or list(EMOJI_POOL)
        chosen = random.choice(available)
        self._icons[session_id] = chosen
        self._save()
        return chosen

    def set(self, session_id: str, emoji: str) -> str | None:
        """Set a session's emoji (capped); returns the stored value or None."""
        session_id = (session_id or "").strip()
        emoji = (emoji or "").strip()
        if not session_id or not emoji:
            return None
        # Keep it to a small grapheme; reject obviously-too-long input.
        if len(emoji) > 8:
            return None
        self._icons[session_id] = emoji
        self._save()
        return emoji
