"""Independent-review follow-up fix (task 1): the two-buffer rewrite of
``parse_transcript``'s main loop so a non-assistant line attaches to the
turn it chronologically PRECEDES, not the turn whose finalisation it
happened to interrupt.

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
