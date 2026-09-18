"""Independent-review follow-up fixes: the two-buffer preceding-events
attribution rewrite (task 1), uuid-based replay dedup (task 2), and the
cache_creation/TTL-split reconciliation (task 3).

Each test exercises the behaviour through a full ``parse_transcript``
pass over a small synthetic fixture, not by calling private helpers
directly, so it proves the acceptance criterion end to end.
"""

from __future__ import annotations

from pathlib import Path

from claude_token_lens.model import EventKind, TranscriptMeta
from claude_token_lens.parse import parse_transcript

from helpers import (
    attachment_line,
    turn_line,
    user_str_line,
    write_jsonl,
)


# -- Task 1: events attach to the turn they PRECEDE, not the one they
# -- follow; anything after the last turn is counted as trailing. -------


def test_events_attach_to_the_turn_they_precede(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_A"),
        # This task-notification sits between turn A and turn B: it must
        # attach to B (the turn it precedes), not be swallowed into A's
        # finalisation (the pre-fix bug).
        user_str_line("<task-notification>done</task-notification>"),
        turn_line(message_id="msg_B"),
        turn_line(message_id="msg_C"),
        # Nothing follows this: it's trailing, attached to no turn.
        attachment_line("total_tokens_reminder"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert len(result.turns) == 3
    turn_a, turn_b, turn_c = result.turns

    assert turn_a.preceding_primary == EventKind.UNKNOWN
    assert turn_a.preceding_event_kinds == ()

    assert turn_b.preceding_primary == EventKind.TASK_NOTIFICATION
    assert turn_b.preceding_event_kinds == (EventKind.TASK_NOTIFICATION,)

    assert turn_c.preceding_primary == EventKind.UNKNOWN
    assert turn_c.preceding_event_kinds == ()

    assert result.diagnostics.trailing_events == 1


def test_first_turn_has_no_preceding_events_even_with_no_prior_lines(tmp_path: Path):
    lines = [turn_line(message_id="msg_only")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert len(result.turns) == 1
    assert result.turns[0].preceding_event_kinds == ()
    assert result.diagnostics.trailing_events == 0


# -- Task 2: replayed non-assistant lines are deduped by uuid. ----------


def test_replayed_lines_deduped_by_uuid_produce_one_event_each(tmp_path: Path):
    human = user_str_line("do the thing", origin={"kind": "human"})
    human["uuid"] = "dup-uuid-human"
    attach = attachment_line("total_tokens_reminder")
    attach["uuid"] = "dup-uuid-attachment"

    lines = [
        human,
        attach,
        turn_line(message_id="msg_1"),
        # Rewind/resume replays the same two lines verbatim, same uuids.
        dict(human),
        dict(attach),
        turn_line(message_id="msg_2"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result.diagnostics.replayed_lines == 2

    human_events = [e for e in result.events if e.kind == EventKind.HUMAN_TEXT]
    reminder_events = [e for e in result.events if e.kind == EventKind.REMINDER]
    assert len(human_events) == 1
    assert len(reminder_events) == 1


def test_replayed_assistant_line_with_same_uuid_is_also_deduped(tmp_path: Path):
    # A replayed assistant line (same uuid, same message.id) must be
    # skipped by the uuid guard before it ever reaches the turn-merge
    # logic - not merged in twice.
    turn_a = turn_line(message_id="msg_A", input_tokens=100, output_tokens=10)
    turn_b = turn_line(message_id="msg_B", input_tokens=50, output_tokens=5)
    lines = [turn_a, turn_b, dict(turn_a)]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result.diagnostics.replayed_lines == 1
    assert result.diagnostics.assistant_lines == 2
    assert len(result.turns) == 2


# -- Task 3: cache_creation_tokens reconciled against the 5m/1h split. --


def test_cache_creation_reconciled_when_flat_field_undercounts_split(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            input_tokens=100,
            cache_creation_input_tokens=0,
            ephemeral_1h_input_tokens=4260,
            output_tokens=10,
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    turn = result.turns[0]
    assert turn.cc_1h == 4260
    assert turn.cc_5m == 0
    assert turn.cache_creation_tokens == 4260
    assert turn.ctx == turn.input_tokens + turn.cache_creation_tokens + turn.cache_read_tokens
    assert turn.ctx == 100 + 4260
    assert result.diagnostics.ttl_sum_mismatch == 1


def test_cache_creation_matching_split_does_not_flag_mismatch(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            cache_creation_input_tokens=500,
            ephemeral_5m_input_tokens=500,
            ephemeral_1h_input_tokens=0,
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result.turns[0].cache_creation_tokens == 500
    assert result.diagnostics.ttl_sum_mismatch == 0
