"""Independent-review follow-up fixes: the two-buffer preceding-events
attribution rewrite (task 1), uuid-based replay dedup (task 2), the
cache_creation/TTL-split reconciliation (task 3), absolute-path
redaction inside ``cmd_prefix`` (task 6), the agent-setting/mode/
attachment_catch_all diagnostics counters (task 8), and
``timestamp_parse_failures`` (task 9).

Each test exercises the behaviour through a full ``parse_transcript``
pass over a small synthetic fixture, not by calling private helpers
directly, so it proves the acceptance criterion end to end.
"""

from __future__ import annotations

from pathlib import Path

from claude_token_lens.model import EventKind, TranscriptMeta
from claude_token_lens.parse import parse_transcript

from helpers import (
    assert_privacy,
    attachment_line,
    ignorable_line,
    tool_use_block,
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


# -- Task 6: absolute paths inside a command prefix are redacted. -------


def test_cmd_prefix_redacts_windows_drive_path(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            content=[
                tool_use_block(
                    "Bash", "tu1", {"command": "cd C:\\Users\\paulm\\secret_project && ls -la"}
                )
            ],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    turn = result.turns[0]
    assert turn.cmd_prefix == "cd <path> && ls -la"
    assert_privacy(result)


def test_cmd_prefix_redacts_posix_home_path(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            content=[tool_use_block("Bash", "tu1", {"command": "cat /home/paulm/.ssh/id_rsa"})],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    turn = result.turns[0]
    assert turn.cmd_prefix == "cat <path>"
    assert "id_rsa" not in turn.cmd_prefix
    assert_privacy(result)


def test_cmd_prefix_redacts_msys_drive_path(tmp_path: Path):
    # Git Bash on Windows renders drive-letter paths as /c/Dev/x rather
    # than C:\Dev\x; _redact_paths must catch that form too.
    lines = [
        turn_line(
            message_id="msg_1",
            content=[tool_use_block("Bash", "tu1", {"command": "cd /c/Dev/secret_project && ls -la"})],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    turn = result.turns[0]
    assert turn.cmd_prefix == "cd <path> && ls -la"
    assert_privacy(result)


def test_cmd_prefix_redacts_bare_backslash_users_path(tmp_path: Path):
    # A drive-less \Users\<name> token (no leading "C:") still names a
    # real machine account and must be redacted like the drive-qualified
    # form.
    lines = [
        turn_line(
            message_id="msg_1",
            content=[tool_use_block("Bash", "tu1", {"command": "cat \\Users\\paulm\\.ssh\\id_rsa"})],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    turn = result.turns[0]
    assert turn.cmd_prefix == "cat <path>"
    assert "paulm" not in turn.cmd_prefix
    assert_privacy(result)


def test_preceding_cmd_prefix_inherits_redaction(tmp_path: Path):
    # preceding_cmd_prefix on the NEXT turn is read off the previous
    # turn's already-redacted cmd_prefix, so it must never leak either.
    lines = [
        turn_line(
            message_id="msg_1",
            content=[tool_use_block("Bash", "tu1", {"command": "cd C:\\Users\\paulm\\proj && pytest"})],
        ),
        turn_line(message_id="msg_2"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result.turns[1].preceding_cmd_prefix == "cd <path> && pytest"
    assert_privacy(result)


# -- Task 8: agent-setting/mode values and attachment_catch_all counters. --


def test_agent_setting_and_mode_lines_carry_values_in_diagnostics(tmp_path: Path):
    lines = [
        ignorable_line("agent-setting", agentSetting="fable-overseer"),
        ignorable_line("mode", mode="plan"),
        ignorable_line("mode", mode="plan"),
        turn_line(message_id="msg_1"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result.diagnostics.agent_settings == {"fable-overseer": 1}
    assert result.diagnostics.modes == {"plan": 2}
    # Still ignored outright as Events (not attached to any turn), just
    # with their value additionally counted.
    assert result.diagnostics.ignored_line_types.get("agent-setting") == 1
    assert result.diagnostics.ignored_line_types.get("mode") == 2


def test_attachment_catch_all_counts_unclassified_attachment_types(tmp_path: Path):
    lines = [
        attachment_line("some_new_unclassified_type_x"),
        attachment_line("some_new_unclassified_type_x"),
        attachment_line("another_unclassified_type_y"),
        turn_line(message_id="msg_1"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result.diagnostics.attachment_catch_all == {
        "some_new_unclassified_type_x": 2,
        "another_unclassified_type_y": 1,
    }


# -- Task 9: timestamp_parse_failures. -----------------------------------


def test_timestamp_parse_failure_is_counted(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", timestamp="not-a-valid-timestamp"),
        turn_line(message_id="msg_2", timestamp="2026-09-18T12:00:05.000Z"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result.diagnostics.timestamp_parse_failures == 1
    assert result.turns[0].gap_s is None


def test_missing_timestamp_is_not_a_parse_failure(tmp_path: Path):
    # An empty/missing timestamp is a different condition from "present
    # but unparsable" - only the latter counts as a parse failure.
    lines = [turn_line(message_id="msg_1", timestamp="")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result.diagnostics.timestamp_parse_failures == 0
