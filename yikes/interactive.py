"""Tiny arrow-key selection menus for the CLI.

Every discrete choice yikes! asks at the terminal goes through :func:`select`
(or :func:`confirm`), so the user navigates with the arrow keys and Enter rather
than typing a number. The selection logic lives in the pure :func:`run_select`
so it can be unit-tested without a real terminal.
"""

from __future__ import annotations

import os
import sys

Option = tuple[str, str]  # (key, label)


def run_select(options: list[Option], default: int, keys: list[str]) -> str | None:
    """Pure selection loop: fold key names into a chosen option key.

    Returns the chosen option key, or ``None`` if cancelled or the keys run out.
    """
    if not options:
        return None
    index = max(0, min(default, len(options) - 1))
    count = len(options)
    for key in keys:
        if key in ("up", "k"):
            index = (index - 1) % count
        elif key in ("down", "j"):
            index = (index + 1) % count
        elif key == "enter":
            return options[index][0]
        elif key in ("esc", "q"):
            return None
    return None


def select(title: str, options: list[Option], *, default: int = 0) -> str | None:
    """Show an interactive arrow-key menu and return the chosen option key.

    Falls back to the default option's key when no usable TTY is available.
    Returns ``None`` if the user cancels (Esc/q/Ctrl-C).
    """
    if not _usable_tty():
        return options[default][0] if options else None
    try:
        import termios
        import tty
    except ImportError:  # pragma: no cover - non-POSIX
        return options[default][0] if options else None

    fd = sys.stdin.fileno()
    index = max(0, min(default, len(options) - 1))
    if title:
        sys.stdout.write(title + "\n")
    for i, (_, label) in enumerate(options):
        sys.stdout.write(_render_line(i == index, label) + "\n")
    sys.stdout.flush()

    old = termios.tcgetattr(fd)
    result: str | None = None
    try:
        tty.setraw(fd)
        while True:
            key = _read_key(fd)
            if key in ("up", "k"):
                index = (index - 1) % len(options)
            elif key in ("down", "j"):
                index = (index + 1) % len(options)
            elif key == "enter":
                result = options[index][0]
                break
            elif key in ("esc", "q"):
                result = None
                break
            else:
                continue
            sys.stdout.write(f"\x1b[{len(options)}A")
            for i, (_, label) in enumerate(options):
                sys.stdout.write("\r\x1b[K" + _render_line(i == index, label) + "\r\n")
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\n")
        sys.stdout.flush()
    return result


def confirm(question: str, *, default: bool = False) -> bool:
    """A yes/no selection menu. Returns the chosen boolean."""
    options: list[Option] = [("yes", "Yes"), ("no", "No")]
    choice = select(question, options, default=0 if default else 1)
    if choice is None:
        return default
    return choice == "yes"


def _render_line(selected: bool, label: str) -> str:
    if selected:
        return f"\x1b[7m ❯ {label} \x1b[0m"
    return f"   {label}"


def _usable_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _read_key(fd: int) -> str:
    """Read one logical key press from a raw-mode fd."""
    import select as _select

    ch = os.read(fd, 1).decode(errors="ignore")
    if ch == "\x1b":
        ready, _, _ = _select.select([fd], [], [], 0.05)
        if ready:
            rest = os.read(fd, 2).decode(errors="ignore")
            return {"[A": "up", "[B": "down", "[C": "right", "[D": "left"}.get(rest, "esc")
        return "esc"
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x03":  # Ctrl-C
        raise KeyboardInterrupt
    return ch
