"""Tests for WP3's notification-vs-attachment precedence, the
co-occurrence table, and the over-representation arithmetic in the
primary-cause table (``src/claude_token_lens/recache.py``).

Complements ``test_recache.py``'s single regression test (notification
beats a trailing attachment through the real parser) with direct
``events.primary_kind`` precedence checks and hand-computed table
arithmetic.
"""

from __future__ import annotations

import pytest

from claude_token_lens import events, recache
from claude_token_lens.model import EventKind, Table, TranscriptMeta, TranscriptResult, Turn
from claude_token_lens.pricing import load_pricing

from helpers import attachment_line, system_line, user_str_line

PRICING = load_pricing()


def _turn(**overrides) -> Turn:
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


def _table(section, name: str) -> Table:
    for table in section.tables:
        if table.name == name:
            return table
    raise AssertionError(f"no table named {name!r} in section {section.key!r}")


# --------------------------------------------------------------------
# Precedence: compaction > notification > attachment
# --------------------------------------------------------------------


def test_precedence_compaction_beats_notification_beats_attachment():
    evts = [
        events.classify_line(attachment_line("environment")),
        events.classify_line(user_str_line("<task-notification>done</task-notification>")),
        events.classify_line(system_line("compact_boundary", compactMetadata={"trigger": "auto"})),
    ]
    assert events.primary_kind(evts) == EventKind.COMPACT_BOUNDARY


def test_precedence_notification_beats_attachment_without_compaction():
    evts = [
        events.classify_line(attachment_line("environment")),
        events.classify_line(user_str_line("<task-notification>done</task-notification>")),
    ]
    assert events.primary_kind(evts) == EventKind.TASK_NOTIFICATION


def test_precedence_is_order_independent():
    # Same three events, reversed stream order: the result must be
    # identical — precedence is resolved by rank, never by "last seen".
    forward = [
        events.classify_line(attachment_line("environment")),
        events.classify_line(user_str_line("<task-notification>done</task-notification>")),
        events.classify_line(system_line("compact_boundary", compactMetadata={"trigger": "auto"})),
    ]
    reversed_ = list(reversed(forward))
    assert events.primary_kind(forward) == events.primary_kind(reversed_) == EventKind.COMPACT_BOUNDARY


# --------------------------------------------------------------------
# Co-occurrence table: every kind counted, not just preceding_primary
# --------------------------------------------------------------------


def test_cooccurrence_counts_every_event_kind_not_just_primary():
    recache_turn = _turn(
        message_id="m2",
        turn_index=2,
        ctx=100_000,
        cache_creation_tokens=90_000,
        cache_read_tokens=500,
        preceding_primary=EventKind.TASK_NOTIFICATION,
        preceding_event_kinds=(EventKind.ATTACHMENT, EventKind.TASK_NOTIFICATION, EventKind.TOOL_RESULT),
    )
    other_turn = _turn(
        message_id="m1",
        turn_index=1,
        ctx=100,
        cache_read_tokens=50,
        preceding_primary=EventKind.UNKNOWN,
        preceding_event_kinds=(EventKind.TOOL_RESULT,),
    )
    th = recache.RecacheThresholds()
    stats = recache.RecacheStats(th)
    stats.add(TranscriptResult(meta=TranscriptMeta(), turns=[other_turn, recache_turn]), lambda m: PRICING.resolve_model(m))
    section = recache.build_section(stats, PRICING, th)
    cooc = _table(section, "recache_event_cooccurrence")
    rows_by_kind = {row[0]: row for row in cooc.rows}

    assert rows_by_kind["task_notification"][1] == 1  # recache turns containing it
    assert rows_by_kind["attachment"][1] == 1
    assert rows_by_kind["tool_result"][1] == 1
    # tool_result appears in BOTH turns' preceding_event_kinds, so the
    # control column (all priced turns) must count both, even though
    # only one of them is a re-cache turn.
    assert rows_by_kind["tool_result"][3] == 2


# --------------------------------------------------------------------
# Over-representation arithmetic
# --------------------------------------------------------------------


def test_over_representation_arithmetic():
    # 2 re-cache turns, both with TASK_NOTIFICATION as primary.
    t1 = _turn(
        message_id="t1", turn_index=2, ctx=100_000, cache_creation_tokens=90_000, cache_read_tokens=500,
        preceding_primary=EventKind.TASK_NOTIFICATION,
    )
    t2 = _turn(
        message_id="t2", turn_index=3, ctx=100_000, cache_creation_tokens=90_000, cache_read_tokens=500,
        preceding_primary=EventKind.TASK_NOTIFICATION,
    )
    # 2 more priced turns that are NOT re-cache turns: one also carries
    # TASK_NOTIFICATION as primary, one carries something else.
    t3 = _turn(
        message_id="t3", turn_index=1, ctx=100, cache_creation_tokens=0, cache_read_tokens=90,
        preceding_primary=EventKind.TASK_NOTIFICATION,
    )
    t4 = _turn(
        message_id="t4", turn_index=4, ctx=100, cache_creation_tokens=0, cache_read_tokens=90,
        preceding_primary=EventKind.UNKNOWN,
    )
    th = recache.RecacheThresholds()
    stats = recache.RecacheStats(th)
    stats.add(TranscriptResult(meta=TranscriptMeta(), turns=[t1, t2, t3, t4]), lambda m: PRICING.resolve_model(m))
    section = recache.build_section(stats, PRICING, th)
    primary = _table(section, "recache_primary_cause")
    row = next(r for r in primary.rows if r[0] == "task_notification")

    # share: 2 of 2 re-cache turns -> 100%.
    assert row[3] == pytest.approx(100.0)
    # control_share: 3 of 4 all priced turns carry that primary -> 75%.
    assert row[5] == pytest.approx(75.0)
    # over-representation: 100 - 75 = 25 points.
    assert row[6] == pytest.approx(25.0)


def test_over_representation_is_negative_when_under_represented():
    # A single re-cache turn NOT preceded by TASK_NOTIFICATION, but three
    # of the four total priced turns are: the cause is under-represented
    # among re-cache turns relative to the corpus at large.
    t1 = _turn(
        message_id="t1", turn_index=2, ctx=100_000, cache_creation_tokens=90_000, cache_read_tokens=500,
        preceding_primary=EventKind.TOOL_RESULT,
    )
    t2 = _turn(
        message_id="t2", turn_index=1, ctx=100, cache_creation_tokens=0, cache_read_tokens=90,
        preceding_primary=EventKind.TASK_NOTIFICATION,
    )
    t3 = _turn(
        message_id="t3", turn_index=3, ctx=100, cache_creation_tokens=0, cache_read_tokens=90,
        preceding_primary=EventKind.TASK_NOTIFICATION,
    )
    t4 = _turn(
        message_id="t4", turn_index=4, ctx=100, cache_creation_tokens=0, cache_read_tokens=90,
        preceding_primary=EventKind.TASK_NOTIFICATION,
    )
    th = recache.RecacheThresholds()
    stats = recache.RecacheStats(th)
    stats.add(TranscriptResult(meta=TranscriptMeta(), turns=[t1, t2, t3, t4]), lambda m: PRICING.resolve_model(m))
    section = recache.build_section(stats, PRICING, th)
    primary = _table(section, "recache_primary_cause")
    row = next(r for r in primary.rows if r[0] == "task_notification")

    assert row[3] == pytest.approx(0.0)  # 0 of 1 re-cache turns
    assert row[5] == pytest.approx(75.0)  # 3 of 4 all priced turns
    assert row[6] == pytest.approx(-75.0)
