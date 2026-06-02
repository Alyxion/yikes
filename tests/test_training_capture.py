from __future__ import annotations

import json

import pytest

from yikes import training_capture as tc


def _target() -> "tc._Target":
    return tc._Target(
        session_id="yik_abc123def",
        backend="claude",
        location="host",
        driver="tmux",
        cwd="/projects/repo/sub",
        name="repo/sub",
        capture_cmd=["CAP"],
        version_cmd=["VER"],
        tmux_version_cmd=["TV"],
        size_cmd=["SZ"],
    )


def _fake_run(cmd, timeout=5.0):
    if cmd == ["CAP"]:
        # A thinking spinner with colour codes + the active-run marker.
        return "\x1b[33m✶ Working…\x1b[0m (esc to interrupt)"
    if cmd == ["VER"]:
        return "claude 1.2.3 (Claude Code)\n"
    if cmd == ["TV"]:
        return "tmux 3.4\n"
    if cmd == ["SZ"]:
        return "160x48\n"
    return ""


def test_capture_writes_labeled_sample(tmp_path, monkeypatch):
    monkeypatch.setenv("YIKES_TRAINING_DIR", str(tmp_path))
    monkeypatch.setattr(tc, "_resolve_target", lambda *a, **k: _target())
    monkeypatch.setattr(tc, "_run", _fake_run)

    result = tc.capture_sample("thinking", "yik_abc123def", frames=4, span=0.0)

    assert result.frame_count == 4
    assert result.predicted == "thinking"  # spinner + "esc to interrupt"
    assert result.path.parent.name == "claude"  # grouped by backend

    frames = sorted(result.path.glob("frame-*.ansi"))
    assert len(frames) == 4
    assert "\x1b[33m" in frames[0].read_text()  # raw colour codes preserved

    meta = json.loads((result.path / "meta.json").read_text())
    assert meta["label"] == "thinking"
    assert meta["predicted_matches_label"] is True
    assert meta["backend"] == "claude"
    assert meta["backend_version"] == "claude 1.2.3 (Claude Code)"
    assert meta["tmux_version"] == "tmux 3.4"
    assert meta["terminal"] == {"cols": 160, "rows": 48}
    assert meta["frame_count"] == 4
    assert meta["session"]["name"] == "repo/sub"


def test_capture_flags_mismatch_between_label_and_prediction(tmp_path, monkeypatch):
    monkeypatch.setenv("YIKES_TRAINING_DIR", str(tmp_path))
    monkeypatch.setattr(tc, "_resolve_target", lambda *a, **k: _target())
    monkeypatch.setattr(tc, "_run", _fake_run)

    # Terminal shows a thinking spinner, but the human knows it is actually idle.
    result = tc.capture_sample("idle", frames=2, span=0.0)

    meta = json.loads((result.path / "meta.json").read_text())
    assert meta["label"] == "idle"
    assert meta["predicted"] == "thinking"
    assert meta["predicted_matches_label"] is False


def test_capture_rejects_unknown_label(tmp_path, monkeypatch):
    monkeypatch.setenv("YIKES_TRAINING_DIR", str(tmp_path))
    with pytest.raises(tc.CaptureError):
        tc.capture_sample("definitely-not-a-state")


def test_strip_ansi_removes_escape_sequences():
    assert tc.strip_ansi("\x1b[33mhi\x1b[0m \x1b[2Kthere") == "hi there"
