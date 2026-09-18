"""Tests for WP3: RE-CACHE detection and reporting
(``src/claude_token_lens/recache.py``).

Two styles of fixture are used, matching the rest of the suite:

- ``_turn`` builds ``model.Turn`` instances directly (like
  ``test_pricing.py``'s ``_turn`` helper) for pure ``detect``/
  ``gap_bucket``/``RecacheStats`` arithmetic tests that don't need a
  parser round-trip.
- ``tests/helpers.py``'s JSONL builders + ``parse_transcript`` for the
  one regression test that specifically exercises event precedence
  through the real parser (the last-attachment attribution bug).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from claude_token_lens import recache
from claude_token_lens.model import EventKind, Table, TranscriptMeta, TranscriptResult, Turn
from claude_token_lens.parse import parse_transcript
from claude_token_lens.pricing import load_pricing

from helpers import attachment_line, turn_line, user_str_line, write_jsonl

PRICING = load_pricing()


def _turn(**overrides) -> Turn:
    """Build a ``Turn`` with sane zero defaults, overridable per field."""
    fields = dict(
        message_id="msg",
        request_id="req",
        turn_index=1,
        ts="2026-09-18T12:00:00.000Z",
        gap_s=None,
        model="claude-sonnet-5",
        is_synthetic=False,
        input_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        output_tokens=0,
        cc_5m=0,
        cc_1h=0,
        ctx=0,
        preceding_tool="none",
        preceding_primary=EventKind.UNKNOWN,
        preceding_event_kinds=(),
        preceding_attachment_types=(),
        preceding_cmd_prefix=None,
    )
    fields.update(overrides)
    return Turn(**fields)


def _result(turns: list[Turn], agent_type: str | None = None) -> TranscriptResult:
    return TranscriptResult(meta=TranscriptMeta(agent_type=agent_type), turns=turns)


def _stats_for(turns: list[Turn], th: recache.RecacheThresholds | None = None, agent_type: str | None = None) -> recache.RecacheStats:
    th = th or recache.RecacheThresholds()
    stats = recache.RecacheStats(th)
    stats.add(_result(turns, agent_type=agent_type), lambda model: PRICING.resolve_model(model))
    return stats


def _table(section, name: str) -> Table:
    for table in section.tables:
        if table.name == name:
            return table
    raise AssertionError(f"no table named {name!r} in section {section.key!r}")


# --------------------------------------------------------------------
# detect(): pure classifier
# --------------------------------------------------------------------


def test_full_expiry_after_high_ctx_low_read():
    turn = _turn(message_id="m2", turn_index=2, ctx=100_600, cache_creation_tokens=100_000, cache_read_tokens=500)
    detected = recache.detect([turn], recache.RecacheThresholds())
    assert len(detected) == 1
    assert detected[0].is_recache is True
    assert detected[0].recache_signature == "full-expiry"
    # Purity: the input turn itself is never mutated, and a fresh copy is returned.
    assert turn.is_recache is False
    assert detected[0] is not turn


def test_prefix_invalidated_at_full_expiry_boundary():
    # cache_read_tokens == full_expiry_cr exactly: the "<" check means this
    # is NOT full-expiry, so it must fall through to prefix-invalidated.
    turn = _turn(message_id="m2", turn_index=2, ctx=100_000, cache_creation_tokens=90_000, cache_read_tokens=2_000)
    detected = recache.detect([turn], recache.RecacheThresholds())
    assert len(detected) == 1
    assert detected[0].recache_signature == "prefix-invalidated"


def test_first_turn_never_flagged_regardless_of_ctx_or_read_ratio():
    turn = _turn(message_id="m1", turn_index=1, ctx=500_000, cache_creation_tokens=500_000, cache_read_tokens=0)
    assert recache.detect([turn], recache.RecacheThresholds()) == []


def test_ctx_at_or_below_floor_never_flagged():
    turn = _turn(message_id="m2", turn_index=2, ctx=20_000, cache_creation_tokens=20_000, cache_read_tokens=0)
    assert recache.detect([turn], recache.RecacheThresholds()) == []


def test_synthetic_turn_excluded_even_if_thresholds_are_met():
    turn = _turn(message_id="m2", turn_index=0, is_synthetic=True, ctx=500_000, cache_creation_tokens=500_000, cache_read_tokens=0)
    assert recache.detect([turn], recache.RecacheThresholds()) == []


def test_high_read_ratio_never_flagged():
    turn = _turn(message_id="m2", turn_index=2, ctx=100_000, cache_creation_tokens=1_000, cache_read_tokens=90_000)
    assert recache.detect([turn], recache.RecacheThresholds()) == []


def test_thresholds_override_changes_detection():
    turn = _turn(message_id="m2", turn_index=2, ctx=10_000, cache_creation_tokens=9_000, cache_read_tokens=1_000)
    assert recache.detect([turn], recache.RecacheThresholds()) == []
    custom = recache.RecacheThresholds(ctx_floor=5_000)
    detected = recache.detect([turn], custom)
    assert len(detected) == 1


def test_thresholds_from_config_flat_dict():
    th = recache.RecacheThresholds.from_config(
        {"ctx_floor": 1_000, "cr_ratio": 0.5, "full_expiry_cr": 100, "huge_ctx": 50_000}
    )
    assert th.ctx_floor == 1_000
    assert th.cr_ratio == 0.5
    assert th.full_expiry_cr == 100
    assert th.huge_ctx == 50_000


def test_thresholds_from_config_nested_thresholds_table():
    th = recache.RecacheThresholds.from_config({"thresholds": {"ctx_floor": 1234}})
    default = recache.RecacheThresholds()
    assert th.ctx_floor == 1234
    # Unspecified keys keep the class defaults.
    assert th.cr_ratio == default.cr_ratio
    assert th.full_expiry_cr == default.full_expiry_cr
    assert th.huge_ctx == default.huge_ctx


def test_thresholds_from_config_none_or_empty_keeps_defaults():
    assert recache.RecacheThresholds.from_config(None) == recache.RecacheThresholds()
    assert recache.RecacheThresholds.from_config({}) == recache.RecacheThresholds()


def test_thresholds_describe_mentions_every_value():
    th = recache.RecacheThresholds()
    lines = th.describe()
    assert len(lines) == 4
    joined = " ".join(lines)
    assert "20,000" in joined
    assert "0.20" in joined
    assert "2,000" in joined
    assert "200,000" in joined


# --------------------------------------------------------------------
# gap_bucket(): boundaries
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "gap_s,expected",
    [
        (None, "unknown"),
        (0, "<1m"),
        (59.9, "<1m"),
        (60, "1-5m"),
        (299, "1-5m"),
        (300, "5-15m"),  # inclusive lower boundary, per the plan
        (899, "5-15m"),
        (900, "15-60m"),
        (3599, "15-60m"),
        (3600, ">60m"),
        (10_000, ">60m"),
    ],
)
def test_gap_bucket_boundaries(gap_s, expected):
    assert recache.gap_bucket(gap_s) == expected


# --------------------------------------------------------------------
# RecacheStats / build_section: avoidable cost
# --------------------------------------------------------------------


def test_avoidable_cost_hand_computed_for_sonnet_5_turn():
    # cc=100k, all in the 5m bucket: write 100k*2.5/1e6 - read 100k*0.2/1e6 = 0.23
    turn = _turn(
        message_id="m2",
        turn_index=2,
        model="claude-sonnet-5",
        ctx=100_600,
        input_tokens=100,
        cache_creation_tokens=100_000,
        cache_read_tokens=500,
        cc_5m=100_000,
        cc_1h=0,
    )
    stats = _stats_for([turn])
    section = recache.build_section(stats, PRICING, recache.RecacheThresholds())
    summary = _table(section, "recache_summary")
    avoidable = summary.rows[0][-1]
    assert avoidable == pytest.approx(0.23, abs=1e-9)


def test_avoidable_cost_is_zero_for_non_recache_turns():
    turn = _turn(message_id="m2", turn_index=2, ctx=100, cache_creation_tokens=10, cache_read_tokens=90)
    stats = _stats_for([turn])
    section = recache.build_section(stats, PRICING, recache.RecacheThresholds())
    summary = _table(section, "recache_summary")
    assert summary.rows[0][-1] == 0.0


# --------------------------------------------------------------------
# RecacheStats / build_section: control columns
# --------------------------------------------------------------------


def test_gap_bucket_control_column_sums_to_all_priced_turns():
    turns = [
        _turn(message_id="m1", turn_index=1, ctx=100, cache_read_tokens=100, gap_s=None),
        _turn(message_id="m2", turn_index=2, ctx=100_000, cache_read_tokens=100, cache_creation_tokens=90_000, gap_s=30),
        _turn(message_id="m3", turn_index=3, ctx=100_000, cache_read_tokens=90_000, cache_creation_tokens=1_000, gap_s=120),
        _turn(message_id="m4", turn_index=4, ctx=50, cache_read_tokens=10, gap_s=4_000),
    ]
    stats = _stats_for(turns)
    section = recache.build_section(stats, PRICING, recache.RecacheThresholds())
    gap_table = _table(section, "recache_gap_buckets")
    # columns: bucket, turns, share_pct_turns, control_turns, control_share_pct_turns, ...
    control_total = sum(row[3] for row in gap_table.rows)
    assert control_total == len(turns)


def test_preceding_tool_control_column_sums_to_all_priced_turns():
    turns = [
        _turn(message_id="m1", turn_index=1, ctx=100, cache_read_tokens=100, preceding_tool="n/a"),
        _turn(message_id="m2", turn_index=2, ctx=100_000, cache_read_tokens=100, cache_creation_tokens=90_000, preceding_tool="Bash"),
        _turn(message_id="m3", turn_index=3, ctx=100, cache_read_tokens=90, preceding_tool="none"),
    ]
    stats = _stats_for(turns)
    section = recache.build_section(stats, PRICING, recache.RecacheThresholds())
    tool_table = _table(section, "recache_preceding_tool")
    # columns: preceding_tool, turns, share_pct_turns, control_turns, control_share_pct_turns, ...
    control_total = sum(row[3] for row in tool_table.rows)
    assert control_total == len(turns)


def test_huge_context_table_population_is_all_priced_turns():
    turns = [
        _turn(message_id="m1", turn_index=1, ctx=250_000, cache_read_tokens=200_000),
        _turn(message_id="m2", turn_index=2, ctx=100, cache_read_tokens=50),
    ]
    stats = _stats_for(turns)
    section = recache.build_section(stats, PRICING, recache.RecacheThresholds())
    huge_table = _table(section, "recache_huge_context")
    row = huge_table.rows[0]
    assert row[0] == 1  # huge_ctx_turns
    assert row[1] == 2  # total_priced_turns
    assert row[2] == 200_000  # huge_ctx_cache_read_tokens
    assert row[3] == 200_050  # total_cache_read_tokens
    assert row[4] == pytest.approx(100.0 * 200_000 / 200_050)


def test_by_group_filtering():
    turns_a = [_turn(message_id="a2", turn_index=2, ctx=100_000, cache_creation_tokens=90_000, cache_read_tokens=100)]
    turns_b = [_turn(message_id="b2", turn_index=2, ctx=100, cache_read_tokens=90)]
    th = recache.RecacheThresholds()
    stats = recache.RecacheStats(th, group_key=lambda result: result.meta.project_slug or "?")
    stats.add(TranscriptResult(meta=TranscriptMeta(project_slug="proj-a"), turns=turns_a), lambda m: PRICING.resolve_model(m))
    stats.add(TranscriptResult(meta=TranscriptMeta(project_slug="proj-b"), turns=turns_b), lambda m: PRICING.resolve_model(m))

    assert stats.groups() == ("proj-a", "proj-b")

    section_a = recache.build_section(stats, PRICING, th, group="proj-a")
    summary_a = _table(section_a, "recache_summary")
    assert summary_a.rows[0][0] == 1  # transcripts folded into this group
    assert summary_a.rows[0][2] == 1  # recache_turns

    section_b = recache.build_section(stats, PRICING, th, group="proj-b")
    summary_b = _table(section_b, "recache_summary")
    assert summary_b.rows[0][2] == 0

    section_all = recache.build_section(stats, PRICING, th, group=None)
    summary_all = _table(section_all, "recache_summary")
    assert summary_all.rows[0][0] == 2
    assert summary_all.rows[0][2] == 1


# --------------------------------------------------------------------
# Regression: primary cause via the real parser, not the last attachment
# --------------------------------------------------------------------


def test_regression_primary_is_notification_not_last_attachment(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", input_tokens=100, output_tokens=20),
        user_str_line("<task-notification>Agent X finished</task-notification>"),
        attachment_line("environment", rendered="cwd=<path>"),
        attachment_line("file", rendered="contents"),
        turn_line(
            message_id="msg_2",
            timestamp="2026-09-18T12:02:00.000Z",
            input_tokens=100,
            cache_creation_input_tokens=100_000,
            cache_read_input_tokens=5_000,
            ephemeral_5m_input_tokens=100_000,
            output_tokens=20,
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    turn_2 = result.turns[1]
    # The notification out-ranks both attachments regardless of stream
    # order — a regression guard against attributing the cause to
    # whichever attachment happened to arrive last.
    assert turn_2.preceding_primary == EventKind.TASK_NOTIFICATION
    assert set(turn_2.preceding_attachment_types) == {"environment", "file"}

    detected = recache.detect(result.turns, recache.RecacheThresholds())
    assert len(detected) == 1
    assert detected[0].recache_signature == "prefix-invalidated"
    assert detected[0].preceding_primary == EventKind.TASK_NOTIFICATION

    stats = recache.RecacheStats(recache.RecacheThresholds())
    stats.add(result, lambda m: PRICING.resolve_model(m))
    section = recache.build_section(stats, PRICING, recache.RecacheThresholds())
    primary_table = _table(section, "recache_primary_cause")
    assert primary_table.rows[0][0] == "task_notification"

    attach_table = _table(section, "recache_attachment_subsplit")
    attach_types = {row[0] for row in attach_table.rows}
    assert attach_types == {"environment", "file"}


# --------------------------------------------------------------------
# Privacy
# --------------------------------------------------------------------

_PRIVACY_DRIVE_RE = re.compile(r"[A-Za-z]:\\")
_PRIVACY_WIN_USERS_RE = re.compile(r"\\Users\\")
_PRIVACY_POSIX_HOME_RE = re.compile(r"/home/")
_MAX_CELL_LEN = 200  # generous: cmd prefixes are <=40 chars, but labels/notes are prose


def _assert_table_rows_privacy_clean(table: Table) -> None:
    for row in table.rows:
        for cell in row:
            if not isinstance(cell, str):
                continue
            assert not _PRIVACY_DRIVE_RE.search(cell), f"{table.name}: drive path leaked in {cell!r}"
            assert not _PRIVACY_WIN_USERS_RE.search(cell), f"{table.name}: \\Users\\ path leaked in {cell!r}"
            assert not _PRIVACY_POSIX_HOME_RE.search(cell), f"{table.name}: /home/ path leaked in {cell!r}"


def test_assert_privacy_on_every_table_row(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", input_tokens=100, output_tokens=20, content=[
            {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "cat C:\\Users\\paulm\\secret.txt"}}
        ]),
        turn_line(
            message_id="msg_2",
            timestamp="2026-09-18T13:00:00.000Z",
            input_tokens=100,
            cache_creation_input_tokens=100_000,
            cache_read_input_tokens=500,
            ephemeral_5m_input_tokens=100_000,
            output_tokens=20,
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    stats = recache.RecacheStats(recache.RecacheThresholds())
    stats.add(result, lambda m: PRICING.resolve_model(m))
    section = recache.build_section(stats, PRICING, recache.RecacheThresholds())

    assert len(section.tables) == 11
    for table in section.tables:
        _assert_table_rows_privacy_clean(table)


# --------------------------------------------------------------------
# apply() (fix item 1): signatures land on the real, returned turns
# --------------------------------------------------------------------


def test_apply_sets_is_recache_and_signature_on_qualifying_turns():
    t1 = _turn(message_id="msg_1", turn_index=1, ctx=0, cache_read_tokens=0, cache_creation_tokens=1000)
    # Qualifies: turn_index > 1, ctx > 20_000, cache_read (5_000) < 0.2 * ctx (20_000),
    # cache_read (5_000) >= full_expiry_cr (2_000) -> prefix-invalidated.
    t2 = _turn(message_id="msg_2", turn_index=2, ctx=100_000, cache_read_tokens=5_000, cache_creation_tokens=95_000)
    t3 = _turn(message_id="msg_3", turn_index=3, ctx=0, cache_read_tokens=0, cache_creation_tokens=100)
    result = _result([t1, t2, t3])

    updated = recache.apply(result, recache.RecacheThresholds())

    assert updated is not result
    assert [t.message_id for t in updated.turns] == ["msg_1", "msg_2", "msg_3"]
    assert updated.turns[0].is_recache is False
    assert updated.turns[0] is t1  # non-qualifying turns pass through unchanged
    assert updated.turns[1].is_recache is True
    assert updated.turns[1].recache_signature == "prefix-invalidated"
    assert updated.turns[2] is t3
    # apply() never mutates the input.
    assert t2.is_recache is False
    assert t2.recache_signature is None
