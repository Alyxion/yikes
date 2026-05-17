from __future__ import annotations

import json
from pathlib import Path

from yikes.prompt_profile import load_prompt_profile, merge_prompt_profile_text


def test_prompt_profile_is_generated_in_user_local_path(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "prompt-profile.json"
    monkeypatch.setenv("YIKES_PROMPT_PROFILE_PATH", str(path))

    profile = load_prompt_profile()
    again = load_prompt_profile()

    assert path.exists()
    assert profile.seed == again.seed
    assert len(profile.setup_variants) >= 3
    assert len(profile.boundary_templates) >= 3
    assert "yikes" not in json.dumps(json.loads(path.read_text())).lower()


def test_prompt_profile_boundary_variants_preserve_markers(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "prompt-profile.json"
    monkeypatch.setenv("YIKES_PROMPT_PROFILE_PATH", str(path))
    profile = load_prompt_profile()

    start, end = profile.markers("abc123")
    instruction = profile.boundary_instruction(start=start, end=end)

    assert start in instruction
    assert end in instruction
    assert "yikes" not in instruction.lower()
    assert any(char in start + end for char in "@>/<-_=~#")


def test_existing_prompt_profile_is_migrated_to_symbolic_markers(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "prompt-profile.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "seed": "fixed-seed",
                "setup_variants": [
                    "Keep replies concise.",
                    "Use the current context.",
                    "Follow the latest request.",
                ],
                "boundary_templates": [
                    "Start line: $start\nEnd line: $end",
                    "Begin line: $start\nFinish line: $end",
                    "Response start: $start\nResponse end: $end",
                ],
                "marker_pairs": [["TEXT_BEGIN_{nonce}", "TEXT_END_{nonce}"]],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("YIKES_PROMPT_PROFILE_PATH", str(path))

    profile = load_prompt_profile()
    start, end = profile.markers("abc123")

    assert any(char in start + end for char in "@>/<-_=~#")
    assert "TEXT_BEGIN" in path.read_text(encoding="utf-8")


def test_prompt_profile_merge_extends_single_shared_profile(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "prompt-profile.json"
    monkeypatch.setenv("YIKES_PROMPT_PROFILE_PATH", str(path))
    existing = load_prompt_profile()
    raw = json.dumps(
        {
            "setup_variants": [
                "Keep this terminal exchange focused and concise; exact one-word requests should get one word.",
                "Continue naturally from the session and honor the latest exact formatting request.",
                "Use the existing context, answer directly, and keep strict single-token requests strict.",
            ],
            "boundary_templates": [
                "Place the answer between these lines:\nStart line: $start\nEnd line: $end",
                "Keep only the reply body within these bounds:\nBegin line: $start\nFinish line: $end",
                "Use these response edges exactly:\nResponse start: $start\nResponse end: $end",
            ],
            "marker_pairs": [["LOCAL_BEGIN_{nonce}", "LOCAL_END_{nonce}"]],
        }
    )

    updated = merge_prompt_profile_text(raw)

    assert updated.seed == existing.seed
    assert len(updated.setup_variants) > len(existing.setup_variants)
    assert path.exists()
    assert "LOCAL_BEGIN_{nonce}" in path.read_text()
