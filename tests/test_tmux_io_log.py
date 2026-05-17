from __future__ import annotations

import json

from yikes.tmux_io_log import log_tmux_io


def test_tmux_io_log_is_disabled_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("YIKES_DEVELOPER_MODE", raising=False)
    monkeypatch.delenv("YIKES_DEV_MODE", raising=False)
    monkeypatch.delenv("YIKES_TMUX_IO_LOG", raising=False)
    monkeypatch.setenv("YIKES_TMUX_IO_LOG_DIR", str(tmp_path))

    log_tmux_io("session-one", "in", "hello", runtime="tmux", backend="codex", event="paste")

    assert list(tmp_path.glob("*.jsonl")) == []


def test_tmux_io_log_writes_bounded_jsonl_when_developer_mode_is_enabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_DEVELOPER_MODE", "1")
    monkeypatch.setenv("YIKES_TMUX_IO_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("YIKES_TMUX_IO_LOG_EVENT_BYTES", "128")
    monkeypatch.setenv("YIKES_TMUX_IO_LOG_FILE_BYTES", "1024")
    monkeypatch.setenv("YIKES_TMUX_IO_LOG_TOTAL_BYTES", "4096")

    log_tmux_io("session/one", "in", "hello", runtime="tmux", backend="codex", event="paste")
    log_tmux_io("session/one", "out", "world", runtime="tmux", backend="codex", event="capture")

    [path] = list(tmp_path.glob("*.jsonl"))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["payload"] == "hello"
    assert rows[0]["direction"] == "in"
    assert rows[1]["payload"] == "world"
    assert rows[1]["direction"] == "out"


def test_tmux_io_log_self_cleans_by_file_and_directory_limits(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("YIKES_TMUX_IO_LOG", "1")
    monkeypatch.setenv("YIKES_TMUX_IO_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("YIKES_TMUX_IO_LOG_EVENT_BYTES", "64")
    monkeypatch.setenv("YIKES_TMUX_IO_LOG_FILE_BYTES", "600")
    monkeypatch.setenv("YIKES_TMUX_IO_LOG_TOTAL_BYTES", "900")
    monkeypatch.setenv("YIKES_TMUX_IO_LOG_MAX_FILES", "2")

    for index in range(10):
        log_tmux_io(f"session-{index}", "out", "x" * 200, runtime="tmux", event="capture")
    log_tmux_io("session-9", "out", "y" * 200, runtime="tmux", event="capture")

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) <= 2
    assert sum(path.stat().st_size for path in files) <= 900
    assert all(path.stat().st_size <= 600 for path in files)
