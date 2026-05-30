from __future__ import annotations

from yikes.interactive import accent, header, muted, paint, run_select


def test_paint_and_palette_wrap_and_reset() -> None:
    assert paint("x", "36") == "\x1b[36mx\x1b[0m"
    for styler in (header, accent, muted):
        out = styler("hello")
        assert out.startswith("\x1b[")
        assert out.endswith("\x1b[0m")
        assert "hello" in out

OPTIONS = [("a", "A"), ("b", "B"), ("c", "C")]


def test_enter_selects_default() -> None:
    assert run_select(OPTIONS, 0, ["enter"]) == "a"


def test_down_then_enter() -> None:
    assert run_select(OPTIONS, 0, ["down", "enter"]) == "b"


def test_up_wraps_around() -> None:
    assert run_select(OPTIONS, 0, ["up", "enter"]) == "c"


def test_jk_navigation() -> None:
    assert run_select(OPTIONS, 0, ["j", "j", "enter"]) == "c"
    assert run_select(OPTIONS, 2, ["k", "enter"]) == "b"


def test_escape_and_q_cancel() -> None:
    assert run_select(OPTIONS, 0, ["esc"]) is None
    assert run_select(OPTIONS, 0, ["q"]) is None


def test_exhausted_keys_returns_none() -> None:
    assert run_select(OPTIONS, 0, ["down"]) is None


def test_empty_options_returns_none() -> None:
    assert run_select([], 0, ["enter"]) is None
