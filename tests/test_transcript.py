from __future__ import annotations

from yikes.transcript import high_level_transcript


def test_high_level_transcript_extracts_yikes_marker_turns() -> None:
    snapshot = """
> You are a concise chatbot. Follow the latest user instruction exactly.

Runtime configuration:
- Web search: enabled.
Respect these limits when using tools or suggesting file operations.

lets gooo 123

Return only the final answer wrapped by these exact result marker lines.
Opening marker: YIKES_RESULT_START_dd6e0a047ed0
Closing marker: YIKES_RESULT_END_dd6e0a047ed0

• YIKES_RESULT_START_dd6e0a047ed0
Let’s go.
YIKES_RESULT_END_dd6e0a047ed0
"""

    assert high_level_transcript(snapshot) == "You: lets gooo 123\nAssistant: Let’s go."


def test_high_level_transcript_leaves_plain_native_pane_alone() -> None:
    snapshot = "› hi!\n\n• Hi. What would you like to do next?"

    assert high_level_transcript(snapshot) == snapshot


def test_high_level_transcript_can_hide_plain_native_pane() -> None:
    snapshot = "› hi!\n\n• Hi. What would you like to do next?"

    assert high_level_transcript(snapshot, fallback_to_raw=False) == ""


def test_high_level_transcript_extracts_neutral_marker_turns() -> None:
    snapshot = """
Keep replies concise.

hello

Place the final answer between these exact boundary lines.
Opening marker: RESULT_START_dd6e0a047ed0
Closing marker: RESULT_END_dd6e0a047ed0

• RESULT_START_dd6e0a047ed0
Hi.
RESULT_END_dd6e0a047ed0
"""

    assert high_level_transcript(snapshot, fallback_to_raw=False) == "You: hello\nAssistant: Hi."


def test_high_level_transcript_extracts_varied_boundary_labels() -> None:
    snapshot = """
hello

Use the following answer bounds, with nothing outside them:
Begin line: ANSWER_BEGIN_ab12
Finish line: ANSWER_DONE_ab12

ANSWER_BEGIN_ab12
Hi.
ANSWER_DONE_ab12
"""

    assert high_level_transcript(snapshot, fallback_to_raw=False) == "You: hello\nAssistant: Hi."


def test_high_level_transcript_extracts_reused_session_markers() -> None:
    snapshot = """
Answer using the established context.

Runtime configuration:
- Web search: enabled.
Respect these limits when using tools or suggesting file operations.

hello

Use these answer bounds for replies in this session unless they are changed later:
Use these exact delimiter lines around the answer:
Delimiter start: REPLY_BEGIN_abc123
Delimiter end: REPLY_END_abc123

• REPLY_BEGIN_abc123
Hi.
REPLY_END_abc123

how are you?

• REPLY_BEGIN_abc123
Doing well.
REPLY_END_abc123
"""

    assert high_level_transcript(snapshot, fallback_to_raw=False) == (
        "You: hello\nAssistant: Hi.\n\n"
        "You: how are you?\nAssistant: Doing well."
    )


def test_high_level_transcript_extracts_symbolic_markers() -> None:
    snapshot = """
hello

Use these answer bounds for replies in this session unless they are changed later:
Use these exact delimiter lines around the answer:
Delimiter start: @@@@/abc123
Delimiter end: /@@@@/abc123

• @@@@/abc123
Hi.
/@@@@/abc123
"""

    assert high_level_transcript(snapshot, fallback_to_raw=False) == "You: hello\nAssistant: Hi."


def test_high_level_transcript_infers_legacy_markers_without_instruction() -> None:
    snapshot = """
gpt-5.5 high

› how are you doing?

• TEXT_BEGIN_cfd95cff53a1
Doing well, thanks. How can I help?
TEXT_END_cfd95cff53a1

› thats amazing

• TEXT_BEGIN_cfd95cff53a1
Glad to hear it.
TEXT_END_cfd95cff53a1
"""

    assert high_level_transcript(snapshot, fallback_to_raw=False) == (
        "You: how are you doing?\nAssistant: Doing well, thanks. How can I help?\n\n"
        "You: thats amazing\nAssistant: Glad to hear it."
    )


def test_high_level_transcript_uses_persisted_markers_when_setup_scrolled_away() -> None:
    snapshot = """
› hello

• @@@@/abc123
Hi.
/@@@@/abc123
"""

    assert high_level_transcript(
        snapshot,
        fallback_to_raw=False,
        markers=(("@@@@/abc123", "/@@@@/abc123"),),
    ) == "You: hello\nAssistant: Hi."
