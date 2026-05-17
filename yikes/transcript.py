from __future__ import annotations

import re


_MARKER_INSTRUCTION = re.compile(
    r"(?m)^(?:Opening marker|Start line|Begin line|Begin|First boundary|Start marker|Answer begins|Response start|Leading marker|Delimiter start):\s*(\S[^\r\n]*?)\s*$\n"
    r"^(?:Closing marker|End line|Finish line|Finish|Final boundary|End marker|Answer ends|Response end|Trailing marker|Delimiter end):\s*(\S[^\r\n]*?)\s*$"
)


def high_level_transcript(
    snapshot: str,
    *,
    fallback_to_raw: bool = True,
    markers: tuple[tuple[str, str], ...] = (),
) -> str:
    """Extract the user-facing transcript from an interactive tmux pane.

    The raw pane contains yikes!' internal prompt wrapper and result markers.
    The high-level view keeps only the user prompt and assistant result for
    turns that used those wrappers. If no wrappers are visible, the caller gets
    the original snapshot by default so native interactive sessions remain
    inspectable. Control surfaces that need a strictly user-facing transcript
    can disable that fallback.
    """

    turns: list[tuple[str, str]] = []
    pairs = _ordered_marker_pairs(snapshot, markers)
    for start_at, end_at, start_marker, end_marker in pairs:
        previous_end = 0
        results = _extract_results(snapshot, start_at, end_at, start_marker, end_marker)
        if not results:
            continue
        for answer, result_start, result_end in results:
            user_text = _extract_user_text(snapshot[previous_end:result_start])
            if user_text or answer:
                turns.append((user_text, answer))
            previous_end = result_end

    if not turns:
        return snapshot if fallback_to_raw else ""

    lines: list[str] = []
    for user_text, answer in turns:
        if user_text:
            lines.append(f"You: {user_text}")
        if answer:
            lines.append(f"Assistant: {answer}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _ordered_marker_pairs(
    snapshot: str,
    explicit: tuple[tuple[str, str], ...],
) -> list[tuple[int, int, str, str]]:
    ordered: list[tuple[int, int, str, str]] = []
    instructions = list(_MARKER_INSTRUCTION.finditer(snapshot))
    for index, marker in enumerate(instructions):
        result_limit = instructions[index + 1].start() if index + 1 < len(instructions) else len(snapshot)
        ordered.append((marker.end(), result_limit, marker.group(1), marker.group(2)))
    seen = {(start, end) for _, _, start, end in ordered}
    for start, end in explicit:
        if (start, end) not in seen:
            ordered.append((0, len(snapshot), start, end))
            seen.add((start, end))
    for start, end in _infer_marker_pairs(snapshot):
        if (start, end) not in seen:
            ordered.append((0, len(snapshot), start, end))
            seen.add((start, end))
    return ordered


def _infer_marker_pairs(snapshot: str) -> tuple[tuple[str, str], ...]:
    lines = [_marker_line(line) for line in snapshot.splitlines()]
    line_set = {line for line in lines if line}
    pairs: list[tuple[str, str]] = []
    for marker in lines:
        if not marker or not _looks_like_start_marker(marker):
            continue
        for end in _end_candidates(marker):
            if end in line_set and (marker, end) not in pairs:
                pairs.append((marker, end))
                break
    return tuple(pairs)


def _marker_line(line: str) -> str:
    stripped = line.strip()
    stripped = re.sub(r"^[⏺●•]\s*", "", stripped).strip()
    if re.fullmatch(r"[A-Z][A-Z0-9_]*_[A-Za-z0-9]+", stripped):
        return stripped
    return ""


def _looks_like_start_marker(marker: str) -> bool:
    return any(token in marker for token in ("_START_", "_BEGIN_", "BEGIN_", "START_"))


def _end_candidates(marker: str) -> tuple[str, ...]:
    candidates = {
        marker.replace("_START_", "_END_"),
        marker.replace("_BEGIN_", "_END_"),
        marker.replace("_BEGIN_", "_DONE_"),
        marker.replace("START_", "END_"),
        marker.replace("BEGIN_", "END_"),
        marker.replace("BEGIN_", "DONE_"),
    }
    candidates.discard(marker)
    return tuple(candidates)


def _extract_results(
    snapshot: str,
    start_at: int,
    end_at: int,
    start_marker: str,
    end_marker: str,
) -> list[tuple[str, int, int]]:
    pattern = re.compile(
        rf"(?ms)^[^\S\r\n]*(?:[⏺●•]\s*)?{re.escape(start_marker)}[^\S\r\n]*$\n"
        rf"(.*?)"
        rf"^[^\S\r\n]*(?:[⏺●•]\s*)?{re.escape(end_marker)}[^\S\r\n]*$"
    )
    segment = snapshot[start_at:end_at]
    return [
        (_clean_text(match.group(1)), start_at + match.start(), start_at + match.end())
        for match in pattern.finditer(segment)
    ]


def _extract_user_text(segment: str) -> str:
    before_wrapper = _before_extraction_instruction(segment)
    if "Runtime configuration:" in before_wrapper:
        before_wrapper = before_wrapper.split("Runtime configuration:", 1)[1]
        match = re.search(r"Respect these limits[^\n]*\n(?P<body>.*)", before_wrapper, re.S)
        if match:
            before_wrapper = match.group("body")
    lines = [_strip_prompt_prefix(line) for line in before_wrapper.splitlines()]
    paragraphs = _paragraphs(lines)
    if not paragraphs:
        return ""
    return _clean_text(paragraphs[-1])


def _before_extraction_instruction(segment: str) -> str:
    delimiters = (
        "Use these answer bounds for replies in this session",
        "Return only the final answer wrapped",
        "Place the final answer between",
        "Place only the answer between",
        "Please put the final answer between",
        "For this reply, wrap only the answer",
        "Use the two exact lines below as answer boundaries",
        "Use the following answer bounds",
        "For this response, place only the answer inside these boundaries",
        "Wrap the answer with these exact lines",
        "Return the answer between the two lines below",
        "Use these exact delimiters for the answer body",
        "Use these exact delimiter lines around the answer",
        "Put the final response after the first line",
    )
    cut = len(segment)
    for delimiter in delimiters:
        index = segment.find(delimiter)
        if index >= 0:
            cut = min(cut, index)
    return segment[:cut]


def _paragraphs(lines: list[str]) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line)
            continue
        if current:
            paragraphs.append("\n".join(current).strip())
            current = []
    if current:
        paragraphs.append("\n".join(current).strip())
    return paragraphs


def _strip_prompt_prefix(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("> "):
        return stripped[2:]
    if stripped.startswith("› "):
        return stripped[2:]
    return line.rstrip()


def _clean_text(text: str) -> str:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    return "\n".join(lines).strip()
