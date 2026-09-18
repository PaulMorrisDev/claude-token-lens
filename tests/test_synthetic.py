"""Synthetic (``<synthetic>`` model, or ``isApiErrorMessage``) turns are
kept in ``turns`` — not dropped — so downstream code can count them, but
are excluded from ``turn_index`` (stays 0) and from ``gap_s`` accounting
for the non-synthetic turns around them.
"""

from __future__ import annotations

from pathlib import Path

from claude_token_lens.model import TranscriptMeta
from claude_token_lens.parse import parse_transcript

from helpers import turn_line, write_jsonl


def test_synthetic_model_turn_is_kept_but_not_indexed(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", input_tokens=100, output_tokens=10),
        turn_line(message_id="msg_synth", model="<synthetic>", input_tokens=0, output_tokens=0),
        turn_line(message_id="msg_2", input_tokens=50, output_tokens=5),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert len(result.turns) == 3
    assert result.diagnostics.synthetic_turns == 1
    assert result.diagnostics.distinct_turns == 3

    turn_1, turn_synth, turn_2 = result.turns
    assert turn_1.is_synthetic is False
    assert turn_1.turn_index == 1
    assert turn_synth.is_synthetic is True
    assert turn_synth.turn_index == 0
    assert turn_synth.gap_s is None
    # turn_2 is the next *non-synthetic* turn: still 1-based turn_index 2,
    # and its gap is measured from turn_1, not from the synthetic turn.
    assert turn_2.is_synthetic is False
    assert turn_2.turn_index == 2


def test_is_api_error_message_flag_marks_synthetic_even_without_synthetic_model(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", isApiErrorMessage=True, input_tokens=0, output_tokens=0),
        turn_line(message_id="msg_2", input_tokens=50, output_tokens=5),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    turn_1, turn_2 = result.turns
    assert turn_1.is_synthetic is True
    assert turn_1.turn_index == 0
    assert turn_2.is_synthetic is False
    assert turn_2.turn_index == 1


def test_synthetic_turns_excluded_from_gap_calculation(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", input_tokens=10, output_tokens=1, timestamp="2026-09-18T12:00:00.000Z"),
        turn_line(
            message_id="msg_synth",
            model="<synthetic>",
            input_tokens=0,
            output_tokens=0,
            timestamp="2026-09-18T12:00:05.000Z",
        ),
        turn_line(message_id="msg_2", input_tokens=10, output_tokens=1, timestamp="2026-09-18T12:01:00.000Z"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    turn_1, turn_synth, turn_2 = result.turns
    assert turn_1.gap_s is None  # first non-synthetic turn
    assert turn_synth.gap_s is None  # synthetic turns never get a gap
    # 60s from turn_1 (12:00:00) to turn_2 (12:01:00), NOT from the
    # synthetic turn's timestamp (12:00:05).
    assert turn_2.gap_s == 60.0
