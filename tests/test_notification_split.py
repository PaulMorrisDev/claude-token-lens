"""Tests for WP3's notification-vs-attachment precedence, the
co-occurrence table, and the over-representation arithmetic in the
primary-cause table (``src/claudeglass/recache.py``).

Complements ``test_recache.py``'s single regression test (notification
beats a trailing attachment through the real parser) with direct
``events.primary_kind`` precedence checks and hand-computed table
arithmetic.
"""

from __future__ import annotations

import pytest

from claudeglass import events, recache
from claudeglass.model import EventKind, Table, TranscriptMeta, TranscriptResult, Turn
from claudeglass.pricing import load_pricing

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
    assert row[2] == pytest.approx(100.0)
    # control_share: 3 of 4 all priced turns carry that primary -> 75%.
    assert row[3] == pytest.approx(75.0)
    # over-representation: 100 - 75 = 25 points.
    assert row[4] == pytest.approx(25.0)


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

    assert row[2] == pytest.approx(0.0)  # 0 of 1 re-cache turns
    assert row[3] == pytest.approx(75.0)  # 3 of 4 all priced turns
    assert row[4] == pytest.approx(-75.0)


# --------------------------------------------------------------------
# Fix item 4: token-weighted control alongside the turn-count control.
# --------------------------------------------------------------------


def test_primary_cause_token_weighted_columns_hand_computed():
    # One re-cache turn (90k cc), primary TASK_NOTIFICATION.
    t1 = _turn(
        message_id="t1", turn_index=2, ctx=100_000, cache_creation_tokens=90_000, cache_read_tokens=500,
        preceding_primary=EventKind.TASK_NOTIFICATION,
    )
    # Non-re-cache turns, one of which also carries TASK_NOTIFICATION and
    # a large cache_creation_tokens value (below ctx_floor, so it never
    # qualifies as a re-cache turn itself, but still counts in the
    # token-weighted control).
    t2 = _turn(
        message_id="t2", turn_index=1, ctx=100, cache_creation_tokens=10_000, cache_read_tokens=90,
        preceding_primary=EventKind.TASK_NOTIFICATION,
    )
    t3 = _turn(
        message_id="t3", turn_index=3, ctx=100, cache_creation_tokens=0, cache_read_tokens=90,
        preceding_primary=EventKind.UNKNOWN,
    )
    th = recache.RecacheThresholds()
    stats = recache.RecacheStats(th)
    stats.add(TranscriptResult(meta=TranscriptMeta(), turns=[t1, t2, t3]), lambda m: PRICING.resolve_model(m))
    section = recache.build_section(stats, PRICING, th)
    primary = _table(section, "recache_primary_cause")
    row = next(r for r in primary.rows if r[0] == "task_notification")

    # cc_tokens (re-cache only): t1's 90_000.
    assert row[5] == 90_000
    # cc_share_pct: 90_000 of 90_000 total re-cache cc tokens -> 100%.
    assert row[6] == pytest.approx(100.0)
    # control_cc_tokens: t1 + t2 = 90_000 + 10_000 = 100_000, over ALL
    # priced turns (control), not just re-cache turns.
    assert row[7] == 100_000
    # control_cc_share_pct: 100_000 of 100_000 total cc tokens -> 100%.
    assert row[8] == pytest.approx(100.0)
    # over_representation_points_tokens: 100 - 100 = 0 — this cause's
    # token share among re-cache turns exactly matches its token share
    # of the whole corpus, even though its *turn-count* control share
    # (row[3], 2 of 3 = 66.67%) differs from its turn-count re-cache
    # share (row[2], 100%) — the two bases tell a different story.
    assert row[9] == pytest.approx(0.0)


def test_primary_cause_prefix_invalidated_table_hand_computed():
    # A full-expiry re-cache turn (excluded from the restricted table)
    # and a prefix-invalidated one (included), with different primaries.
    full_expiry = _turn(
        message_id="fe", turn_index=2, ctx=100_000, cache_creation_tokens=99_000, cache_read_tokens=100,
        preceding_primary=EventKind.TOOL_RESULT,
    )
    prefix_invalidated = _turn(
        message_id="pi", turn_index=3, ctx=100_000, cache_creation_tokens=80_000, cache_read_tokens=15_000,
        preceding_primary=EventKind.TASK_NOTIFICATION,
    )
    control_only = _turn(
        message_id="c", turn_index=1, ctx=100, cache_creation_tokens=0, cache_read_tokens=90,
        preceding_primary=EventKind.TASK_NOTIFICATION,
    )
    th = recache.RecacheThresholds()
    stats = recache.RecacheStats(th)
    stats.add(
        TranscriptResult(meta=TranscriptMeta(), turns=[full_expiry, prefix_invalidated, control_only]),
        lambda m: PRICING.resolve_model(m),
    )
    section = recache.build_section(stats, PRICING, th)
    pi_table = _table(section, "recache_primary_cause_prefix_invalidated")

    # The full-expiry turn's cause (tool_result) must not appear at all,
    # or must appear with zero turns/tokens - it's excluded from the
    # restricted population entirely.
    tool_result_rows = [r for r in pi_table.rows if r[0] == "tool_result"]
    assert all(r[1] == 0 for r in tool_result_rows)

    notification_row = next(r for r in pi_table.rows if r[0] == "task_notification")
    # turns: 1 of 1 prefix-invalidated turn.
    assert notification_row[1] == 1
    assert notification_row[2] == pytest.approx(100.0)  # share_pct_turns
    # cc_tokens: 80_000 (the prefix-invalidated turn only).
    assert notification_row[4] == 80_000
    assert notification_row[5] == pytest.approx(100.0)  # cc_share_pct (of prefix-invalidated cc)
    # control_cc_tokens: task_notification precedes both the
    # prefix-invalidated turn (80_000) and the control-only turn (0)
    # -> 80_000, over the whole corpus's 179_000 total cc tokens.
    assert notification_row[6] == 80_000
    assert notification_row[7] == pytest.approx(100.0 * 80_000 / 179_000)
