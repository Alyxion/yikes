from __future__ import annotations

import json
import os
import random
import secrets
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

from .runtime import DEFAULT_YIKES_DIR


PROFILE_VERSION = 1
DEFAULT_PROMPT_PROFILE_PATH = DEFAULT_YIKES_DIR / "prompt-profile.json"


@dataclass(frozen=True)
class PromptProfile:
    version: int
    seed: str
    setup_variants: tuple[str, ...]
    boundary_templates: tuple[str, ...]
    marker_pairs: tuple[tuple[str, str], ...]

    def setup_for(self, session_id: str) -> str:
        return _pick(self.setup_variants, f"{self.seed}:setup:{session_id}")

    def markers(self, nonce: str) -> tuple[str, str]:
        pairs = tuple(pair for pair in self.marker_pairs if _looks_symbolic_pair(pair)) or self.marker_pairs
        start, end = _pick(pairs, f"{self.seed}:markers:{nonce}")
        return start.format(nonce=nonce), end.format(nonce=nonce)

    def boundary_instruction(self, *, start: str, end: str) -> str:
        template = _pick(self.boundary_templates, f"{self.seed}:boundary:{start}:{end}")
        return Template(template).safe_substitute(start=start, end=end)


def load_prompt_profile(path: Path | None = None) -> PromptProfile:
    target = _profile_path(path)
    if target.exists():
        try:
            profile = _profile_from_json(json.loads(target.read_text(encoding="utf-8")))
            _validate_profile(profile)
            migrated = _with_symbolic_markers(profile)
            if migrated.marker_pairs != profile.marker_pairs:
                save_prompt_profile(migrated, target)
            return migrated
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    profile = _generate_profile()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(_profile_to_json(profile), indent=2), encoding="utf-8")
        target.chmod(0o600)
    except OSError:
        return profile
    return profile


def save_prompt_profile(profile: PromptProfile, path: Path | None = None) -> Path:
    _validate_profile(profile)
    target = _profile_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_profile_to_json(profile), indent=2), encoding="utf-8")
    target.chmod(0o600)
    return target


def prompt_profile_generation_prompt(profile: PromptProfile, *, count: int = 10) -> str:
    count = max(3, min(int(count), 40))
    return (
        "Create additional wording variants for a terminal chat profile. "
        "Keep the meaning equivalent to the existing examples: concise replies, "
        "respecting the newest user request, exact-format answers when requested, "
        "and answer boundary instructions that preserve two placeholders.\n\n"
        "Return strict JSON only with this shape:\n"
        "{\n"
        '  "setup_variants": ["..."],\n'
        '  "boundary_templates": ["... $start ... $end ..."],\n'
        '  "marker_pairs": [["@@@@/{nonce}", "/@@@@/{nonce}"]]\n'
        "}\n\n"
        f"Provide up to {count} setup variants, {count} boundary templates, and {max(3, count // 2)} marker pairs. "
        "Do not include product names. Preserve the exact placeholders $start, $end, and {nonce}. "
        "Use natural wording and keep each item short. Prefer compact symbol-heavy marker pairs "
        "that look like ordinary temporary separators instead of machine labels such as BEGIN or RESULT.\n\n"
        "Existing setup examples:\n"
        f"{json.dumps(list(profile.setup_variants[:5]), indent=2)}\n\n"
        "Existing boundary examples:\n"
        f"{json.dumps(list(profile.boundary_templates[:5]), indent=2)}\n\n"
        "Existing marker examples:\n"
        f"{json.dumps([list(pair) for pair in profile.marker_pairs[:5]], indent=2)}"
    )


def merge_prompt_profile_text(raw: str, *, path: Path | None = None, replace: bool = False) -> PromptProfile:
    existing = load_prompt_profile(path)
    generated = _generated_profile_from_text(raw, seed=existing.seed)
    if replace:
        profile = generated
    else:
        profile = PromptProfile(
            version=PROFILE_VERSION,
            seed=existing.seed,
            setup_variants=_unique((*existing.setup_variants, *generated.setup_variants)),
            boundary_templates=_unique((*existing.boundary_templates, *generated.boundary_templates)),
            marker_pairs=_unique_pairs((*existing.marker_pairs, *generated.marker_pairs)),
        )
    save_prompt_profile(profile, path)
    return profile


def _profile_path(path: Path | None) -> Path:
    if path is not None:
        return path.expanduser()
    override = os.environ.get("YIKES_PROMPT_PROFILE_PATH")
    if override:
        return Path(override).expanduser()
    return DEFAULT_PROMPT_PROFILE_PATH


def _generate_profile() -> PromptProfile:
    seed = secrets.token_urlsafe(18)
    rng = random.Random(seed)
    setup_variants = tuple(rng.sample(_SETUP_CANDIDATES, k=min(5, len(_SETUP_CANDIDATES))))
    boundary_templates = tuple(rng.sample(_BOUNDARY_CANDIDATES, k=min(6, len(_BOUNDARY_CANDIDATES))))
    marker_pairs = tuple(rng.sample(_MARKER_PAIR_CANDIDATES, k=min(5, len(_MARKER_PAIR_CANDIDATES))))
    profile = PromptProfile(
        version=PROFILE_VERSION,
        seed=seed,
        setup_variants=setup_variants,
        boundary_templates=boundary_templates,
        marker_pairs=marker_pairs,
    )
    _validate_profile(profile)
    return profile


def _generated_profile_from_text(raw: str, *, seed: str) -> PromptProfile:
    data = _extract_json_object(raw)
    profile = PromptProfile(
        version=PROFILE_VERSION,
        seed=seed,
        setup_variants=tuple(str(item).strip() for item in data.get("setup_variants", []) if str(item).strip()),
        boundary_templates=tuple(str(item).strip() for item in data.get("boundary_templates", []) if str(item).strip()),
        marker_pairs=tuple(
            (str(pair[0]).strip(), str(pair[1]).strip())
            for pair in data.get("marker_pairs", [])
            if isinstance(pair, list | tuple) and len(pair) == 2
        ),
    )
    _validate_profile(profile)
    return profile


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("prompt profile generation must return a JSON object")
    return data


def _profile_to_json(profile: PromptProfile) -> dict[str, object]:
    return {
        "version": profile.version,
        "seed": profile.seed,
        "setup_variants": list(profile.setup_variants),
        "boundary_templates": list(profile.boundary_templates),
        "marker_pairs": [list(pair) for pair in profile.marker_pairs],
    }


def _profile_from_json(data: dict[str, object]) -> PromptProfile:
    pairs = data.get("marker_pairs", [])
    return PromptProfile(
        version=int(data.get("version", 0)),
        seed=str(data.get("seed", "")),
        setup_variants=tuple(str(item) for item in data.get("setup_variants", []) if str(item).strip()),
        boundary_templates=tuple(str(item) for item in data.get("boundary_templates", []) if str(item).strip()),
        marker_pairs=tuple(
            (str(pair[0]), str(pair[1]))
            for pair in pairs
            if isinstance(pair, list | tuple) and len(pair) == 2
        ),
    )


def _validate_profile(profile: PromptProfile) -> None:
    if profile.version != PROFILE_VERSION:
        raise ValueError("unsupported prompt profile version")
    if not profile.seed or len(profile.setup_variants) < 3 or len(profile.boundary_templates) < 3:
        raise ValueError("incomplete prompt profile")
    if not profile.marker_pairs:
        raise ValueError("prompt profile has no marker pairs")
    for text in (*profile.setup_variants, *profile.boundary_templates):
        lowered = text.lower()
        if "yikes" in lowered:
            raise ValueError("prompt profile must not include project branding")
    for template in profile.boundary_templates:
        if "$start" not in template or "$end" not in template:
            raise ValueError("boundary template must preserve start/end placeholders")
    for start, end in profile.marker_pairs:
        if "{nonce}" not in start or "{nonce}" not in end:
            raise ValueError("marker pair must preserve nonce placeholders")
        if start == end:
            raise ValueError("marker pair must use distinct start/end markers")
        if "\n" in start or "\r" in start or "\n" in end or "\r" in end:
            raise ValueError("marker pair must be single-line")


def _unique(items: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = " ".join(item.split())
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return tuple(result)


def _unique_pairs(items: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for start, end in items:
        key = (start.upper(), end.upper())
        if key in seen:
            continue
        seen.add(key)
        result.append((start, end))
    return tuple(result)


def _with_symbolic_markers(profile: PromptProfile) -> PromptProfile:
    if any(_looks_symbolic_pair(pair) for pair in profile.marker_pairs):
        return profile
    return PromptProfile(
        version=profile.version,
        seed=profile.seed,
        setup_variants=profile.setup_variants,
        boundary_templates=profile.boundary_templates,
        marker_pairs=_unique_pairs((*_SYMBOL_MARKER_PAIR_CANDIDATES, *profile.marker_pairs)),
    )


def _looks_symbolic_pair(pair: tuple[str, str]) -> bool:
    return any(char in f"{pair[0]}{pair[1]}" for char in "@>/<-_=~#")


def _pick[T](items: tuple[T, ...], key: str) -> T:
    if not items:
        raise ValueError("cannot pick from an empty prompt profile section")
    index = sum(ord(char) for char in key) % len(items)
    return items[index]


_SETUP_CANDIDATES = (
    "Keep the exchange concise and follow the latest user request closely. If the user asks for a one-word name, return only that word.",
    "Use the running context in this terminal. Prefer direct answers, and let the newest user request take priority. For a single-word name request, answer with only the name.",
    "Continue from the current session context. Be concise, follow the present request precisely, and keep one-word name answers to just the name.",
    "Answer as part of this ongoing conversation. Keep responses focused; when asked for only a name, provide exactly the name.",
    "Stay with the context already established here. Respond directly and keep strict single-word requests to a single word.",
    "Treat this as a continuing terminal conversation. Be brief unless more detail is requested, and respect exact-format instructions from the user.",
    "Work from the current conversation state. Follow exact wording constraints carefully, especially when the user asks for a single-word answer.",
)

_BOUNDARY_CANDIDATES = (
    "Please put the final answer between these two standalone lines:\nStart line: $start\nEnd line: $end",
    "Use the following answer bounds, with nothing outside them:\nBegin line: $start\nFinish line: $end",
    "For this response, place only the answer inside these boundaries:\nFirst boundary: $start\nFinal boundary: $end",
    "Wrap the answer with these exact lines, each on its own line:\nOpening marker: $start\nClosing marker: $end",
    "Return the answer between the two lines below. Keep the lines unchanged:\nStart marker: $start\nEnd marker: $end",
    "Use these exact delimiters for the answer body:\nAnswer begins: $start\nAnswer ends: $end",
    "Put the final response after the first line and before the second line:\nResponse start: $start\nResponse end: $end",
)

_MARKER_PAIR_CANDIDATES = (
    ("@@@@/{nonce}", "/@@@@/{nonce}"),
    ("--->> {nonce}", "<<--- {nonce}"),
    ("//// {nonce}", "/////{nonce}"),
    ("==== {nonce}", "====/{nonce}"),
    ("#### {nonce}", "####/{nonce}"),
    ("~~~~ {nonce}", "~~~~/{nonce}"),
    ("RESULT_START_{nonce}", "RESULT_END_{nonce}"),
    ("ANSWER_BEGIN_{nonce}", "ANSWER_DONE_{nonce}"),
    ("FINAL_BEGIN_{nonce}", "FINAL_END_{nonce}"),
    ("OUTPUT_START_{nonce}", "OUTPUT_END_{nonce}"),
    ("REPLY_BEGIN_{nonce}", "REPLY_END_{nonce}"),
    ("CONTENT_START_{nonce}", "CONTENT_END_{nonce}"),
)

_SYMBOL_MARKER_PAIR_CANDIDATES = (
    ("@@@@/{nonce}", "/@@@@/{nonce}"),
    ("--->> {nonce}", "<<--- {nonce}"),
    ("//// {nonce}", "/////{nonce}"),
    ("==== {nonce}", "====/{nonce}"),
    ("#### {nonce}", "####/{nonce}"),
    ("~~~~ {nonce}", "~~~~/{nonce}"),
)
