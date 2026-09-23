"""One ``message.id`` spread over several contiguous assistant lines is
one ``Turn``, not several — and a duplicate id that reappears after the
group has already been finalised (out-of-order, "late") is dropped and
counted rather than silently merged or double-counted.
"""

from __future__ import annotations

from pathlib import Path

from claude_token_lens.model import TranscriptMeta
from claude_token_lens.parse import parse_transcript

from helpers import tool_use_block, turn_line, write_jsonl


def test_three_lines_same_id_count_as_one_turn_with_unioned_tools(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_A", input_tokens=100, cache_creation_input_tokens=20, output_tokens=4),
        turn_line(
            message_id="msg_A",
            input_tokens=100,
            cache_creation_input_tokens=20,
            output_tokens=4,
            content=[tool_use_block("Bash", "tu1", {"command": "echo hi"})],
        ),
        turn_line(
            message_id="msg_A",
            input_tokens=100,
            cache_creation_input_tokens=20,
            output_tokens=605,
            content=[tool_use_block("Read", "tu2", {"file_path": "C:/x.txt"})],
        ),
        turn_line(message_id="msg_B", input_tokens=50, output_tokens=5),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result.diagnostics.assistant_lines == 4
    assert result.diagnostics.distinct_turns == 2
    assert len(result.turns) == 2

    turn_a, turn_b = result.turns
    assert turn_a.input_tokens == 100
    assert turn_a.cache_creation_tokens == 20
    assert turn_a.tool_names == ("Bash", "Read")
    assert turn_a.turn_index == 1
    assert turn_b.turn_index == 2
    assert turn_b.input_tokens == 50


def test_streamed_reply_takes_usage_from_its_most_complete_line(tmp_path: Path):
    """Claude Code writes a streamed reply as one line per content block.
    The first line's output_tokens is a partial count and only the last
    carries output_tokens_details, so the turn must use the last line's
    usage, not the first's."""
    first = turn_line(message_id="msg_A", input_tokens=10, cache_creation_input_tokens=49_295, output_tokens=4)
    last = turn_line(
        message_id="msg_A",
        input_tokens=10,
        cache_creation_input_tokens=49_295,
        output_tokens=605,
        content=[tool_use_block("Bash", "tu1", {"command": "echo hi"})],
    )
    last["message"]["usage"]["output_tokens_details"] = {"thinking_tokens": 291}
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [first, last])

    (turn,) = parse_transcript(path, TranscriptMeta(path=str(path))).turns

    assert turn.output_tokens == 605
    assert turn.thinking_tokens == 291
    assert turn.cache_creation_tokens == 49_295
    assert turn.tool_names == ("Bash",)


def test_streamed_reply_ignores_a_less_complete_later_snapshot(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_A", output_tokens=300),
        turn_line(message_id="msg_A", output_tokens=7, content=[{"type": "text", "text": "late"}]),
    ]
    lines[0]["message"]["usage"]["output_tokens_details"] = {"thinking_tokens": 120}
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    (turn,) = parse_transcript(path, TranscriptMeta(path=str(path))).turns

    assert turn.output_tokens == 300
    assert turn.thinking_tokens == 120


def test_late_out_of_order_duplicate_id_is_dropped_and_counted(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_A", input_tokens=100, output_tokens=10),
        turn_line(message_id="msg_A", content=[tool_use_block("Bash", "tu1", {"command": "echo hi"})]),
        turn_line(message_id="msg_B", input_tokens=50, output_tokens=5),
        # msg_A reappears after msg_B has already started — out of order,
        # dropped and counted rather than reopening the finalised turn.
        turn_line(message_id="msg_A", input_tokens=1, output_tokens=1),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result.diagnostics.late_duplicate_ids == 1
    assert result.diagnostics.distinct_turns == 2
    assert [t.turn_index for t in result.turns] == [1, 2]
    # The late duplicate must not have perturbed turn A's already-finalised state.
    assert result.turns[0].input_tokens == 100


def test_preceding_tool_prefers_bash_then_powershell_over_first_tool(tmp_path: Path):
    lines = [
        # Turn A: Read called before Bash — Bash must still win the scan.
        turn_line(
            message_id="msg_A",
            content=[
                tool_use_block("Read", "tu1", {"file_path": "C:/x.txt"}),
                tool_use_block("Bash", "tu2", {"command": "echo hi"}),
            ],
        ),
        # Turn B: Edit called before PowerShell, no Bash — PowerShell wins.
        turn_line(
            message_id="msg_B",
            content=[
                tool_use_block("Edit", "tu3", {"file_path": "C:/y.txt"}),
                tool_use_block("PowerShell", "tu4", {"command": "Get-ChildItem"}),
            ],
        ),
        # Turn C: no shell tool at all — falls back to the first tool name.
        turn_line(message_id="msg_C", content=[tool_use_block("Read", "tu5", {"file_path": "C:/z.txt"})]),
        # Turn D: no tools at all.
        turn_line(message_id="msg_D", content=[{"type": "text", "text": "ok"}]),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert len(result.turns) == 4
    turn_a, turn_b, turn_c, turn_d = result.turns

    # First turn in the transcript has no previous turn.
    assert turn_a.preceding_tool == "n/a"
    assert turn_a.preceding_cmd_prefix is None

    assert turn_b.preceding_tool == "Bash"
    assert turn_b.preceding_cmd_prefix == "echo hi"

    assert turn_c.preceding_tool == "PowerShell"
    assert turn_c.preceding_cmd_prefix == "Get-ChildItem"

    assert turn_d.preceding_tool == "Read"
    assert turn_d.preceding_cmd_prefix is None


def test_preceding_tool_is_none_when_previous_turn_used_no_tools(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_A", content=[{"type": "text", "text": "ok"}]),
        turn_line(message_id="msg_B", content=[{"type": "text", "text": "ok"}]),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.turns[1].preceding_tool == "none"
