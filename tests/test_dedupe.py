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
        turn_line(message_id="msg_A", input_tokens=100, cache_creation_input_tokens=20, output_tokens=10),
        turn_line(
            message_id="msg_A",
            # usage on a later block-line in the same group must be ignored:
            # the first line's usage wins.
            input_tokens=999,
            output_tokens=999,
            content=[tool_use_block("Bash", "tu1", {"command": "echo hi"})],
        ),
        turn_line(
            message_id="msg_A",
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
    # First line's usage wins, not the 999/999 on a later block-line.
    assert turn_a.input_tokens == 100
    assert turn_a.output_tokens == 10
    assert turn_a.tool_names == ("Bash", "Read")
    assert turn_a.turn_index == 1
    assert turn_b.turn_index == 2
    assert turn_b.input_tokens == 50


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
