"""Three-pricing-fixes batch, parse.py's slice: ``Turn.speed`` from each
turn's own ``usage.speed`` (see model.py's module docstring and
``PARSER_VERSION`` 13's bump note in ``__init__.py``). The pricing-side
behaviour this field drives (``pricing.FastRule``, ``price_turn``'s fast
multiplier, ``PricingCoverage.fast_priced_as_standard``) is covered in
``tests/test_pricing.py`` against hand-built ``Turn`` instances; this
file only covers the parse step, through a full ``parse_transcript``
pass over a small synthetic fixture (matching this codebase's own
convention -- see ``test_capture_improvements.py``).
"""

from __future__ import annotations

from pathlib import Path

from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript

from helpers import turn_line, write_jsonl


def test_speed_fast_is_parsed_onto_the_turn(tmp_path: Path):
    lines = [turn_line(message_id="msg_1", speed="fast")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result.turns[0].speed == "fast"


def test_speed_standard_is_parsed_onto_the_turn(tmp_path: Path):
    lines = [turn_line(message_id="msg_1", speed="standard")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result.turns[0].speed == "standard"


def test_speed_absent_from_usage_leaves_turn_speed_none(tmp_path: Path):
    # Real JSONL from a standard-speed reply simply has no "speed" key
    # at all (see turn_line's docstring) rather than a null one.
    lines = [turn_line(message_id="msg_1")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result.turns[0].speed is None


def test_speed_takes_the_most_recent_lines_value_like_other_usage_fields(tmp_path: Path):
    # A streamed reply is written as one line per content block, each
    # carrying its own usage snapshot; _apply_usage replaces the whole
    # pending turn's usage-derived state on every line (see parse.py's
    # module docstring), so the last line observed wins for speed too.
    lines = [
        turn_line(message_id="msg_1", speed="standard"),
        turn_line(message_id="msg_1", speed="fast"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert len(result.turns) == 1
    assert result.turns[0].speed == "fast"
