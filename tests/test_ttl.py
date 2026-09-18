"""Tests for WP4: TTL break-even simulation (``src/claude_token_lens/ttl.py``).

Most branches are exercised on hand-built ``model.Turn`` instances (the
``_turn`` helper below, matching ``test_pricing.py``'s convention) with
hand-computed costs at the packaged Sonnet 5 rates (input 2.0, output
10.0, cache_write_5m 2.5, cache_write_1h 4.0, cache_read 0.2 per million
tokens). A couple of integration tests go through the real parser
(``parse_transcript`` + ``tests/helpers.py``'s JSONL builders) to also
exercise ``helpers.assert_privacy`` and ``TtlStats.add``'s agent-type
keying on a genuine ``TranscriptResult``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from claude_token_lens import model
from claude_token_lens.model import TranscriptMeta, TranscriptResult
from claude_token_lens.parse import parse_transcript
from claude_token_lens.pricing import load_pricing, price_turn
from claude_token_lens.ttl import (
    ASSUMPTIONS,
    POLICY_1H,
    POLICY_5M,
    TtlStats,
    TtlTypeStats,
    build_section,
    cache_economy,
    dominant_ttl,
    fidelity,
    observed,
    simulate,
)

from helpers import assert_privacy, turn_line, write_jsonl

SONNET_RATES = load_pricing().resolve_model("claude-sonnet-5")


def _turn(**overrides) -> model.Turn:
    """Build a priced ``Turn`` with sane zero defaults, overridable per
    field. ``turn_index`` defaults to 1 (priced); pass ``turn_index=0``
    to build a synthetic/usage-less turn for the skip tests."""
    fields = dict(
        message_id="msg_1",
        request_id="req_1",
        turn_index=1,
        ts="2026-09-18T12:00:00.000Z",
        model="claude-sonnet-5",
        input_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        output_tokens=0,
        cc_5m=0,
        cc_1h=0,
        ctx=0,
        gap_s=None,
    )
    fields.update(overrides)
    return model.Turn(**fields)


def _stats(cost_observed: float, cost_all_5m: float, cost_all_1h: float, key: str = "claude-implementer") -> TtlTypeStats:
    """A ``TtlTypeStats`` with only the cost fields the recommendation
    threshold tests care about set to something meaningful."""
    return TtlTypeStats(
        key=key,
        spawns=1,
        priced_turns=1,
        observed_5m_pct=100.0,
        observed_1h_pct=0.0,
        gaps_over_5m=0,
        gaps_over_1h=0,
        gap_p50_s=None,
        gap_p90_s=None,
        cost_observed=cost_observed,
        cost_all_5m=cost_all_5m,
        cost_all_1h=cost_all_1h,
        unsimulatable=0,
        fidelity_pct=0.0,
        gap_buckets={},
    )


# --------------------------------------------------------------------
# Assumptions
# --------------------------------------------------------------------


def test_assumptions_include_gap_definition_sentence():
    assert "gap is measured from the start of one request to the start of the next" in ASSUMPTIONS


def test_assumptions_cover_every_a4_rule():
    joined = " ".join(ASSUMPTIONS)
    for fragment in (
        "TTL-invariant",
        "cache_read_i + cache_creation_i",
        "refreshes TTL",
        "prefix-invalidated",
        "cache_read rate",
        "clamps write at 0",
    ):
        assert fragment in joined


# --------------------------------------------------------------------
# simulate(): each Appendix A4 branch
# --------------------------------------------------------------------


def test_first_turn_always_writes_full_prefix_no_read():
    t = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, gap_s=None)
    result = simulate([t], SONNET_RATES, POLICY_5M)
    assert result.write_tokens == 1000
    assert result.read_tokens == 0
    assert result.turns == 1
    assert result.unsimulatable == 0
    assert result.cost == pytest.approx(1000 / 1_000_000 * 2.5)


def test_gap_within_policy_reads_min_c_writes_remainder():
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0)
    # C2 = cache_read(0) + cache_creation(1500) = 1500; prev_C = 1000
    # -> read = min(1500, 1000) = 1000, write = 1500 - 1000 = 500
    t2 = _turn(turn_index=2, message_id="msg_2", cache_creation_tokens=1500, cc_5m=1500, cache_read_tokens=0, gap_s=100)
    result = simulate([t1, t2], SONNET_RATES, POLICY_5M)
    assert result.write_tokens == 1000 + 500
    assert result.read_tokens == 0 + 1000
    expected_cost = (1000 / 1e6 * 2.5) + (500 / 1e6 * 2.5 + 1000 / 1e6 * 0.2)
    assert result.cost == pytest.approx(expected_cost)


def test_gap_beyond_policy_forces_full_rewrite():
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000)
    t2 = _turn(turn_index=2, message_id="msg_2", cache_creation_tokens=2000, cc_5m=2000, cache_read_tokens=0, gap_s=400)
    result = simulate([t1, t2], SONNET_RATES, POLICY_5M)  # 400 s > 300 s policy
    assert result.write_tokens == 1000 + 2000
    assert result.read_tokens == 0


def test_prefix_invalidated_turn_keeps_observed_split_regardless_of_gap():
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000)
    # gap_s=50 is well inside the 5m policy, so without the signature this
    # would take the "read = min(C, prev_C)" branch instead.
    t2 = _turn(turn_index=2, message_id="msg_2", cache_creation_tokens=300, cc_5m=300, cache_read_tokens=700, gap_s=50)
    t2 = dataclasses.replace(t2, recache_signature="prefix-invalidated")
    result = simulate([t1, t2], SONNET_RATES, POLICY_5M)
    assert result.write_tokens == 1000 + 300
    assert result.read_tokens == 0 + 700


def test_unknown_gap_carries_observed_split_and_counts_unsimulatable():
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000)
    t2 = _turn(turn_index=2, message_id="msg_2", cache_creation_tokens=400, cc_5m=400, cache_read_tokens=600, gap_s=None)
    result = simulate([t1, t2], SONNET_RATES, POLICY_5M)
    assert result.unsimulatable == 1
    assert result.write_tokens == 1000 + 400
    assert result.read_tokens == 0 + 600


def test_compaction_shrink_clamps_write_at_zero():
    t1 = _turn(cache_creation_tokens=200_000, cc_1h=200_000, gap_s=None)
    # C2 = cache_read(50_000) + cache_creation(0) = 50_000; prev_C = 200_000
    # -> read = min(50_000, 200_000) = 50_000, write = max(0, 50_000 - 50_000) = 0
    t2 = _turn(
        turn_index=2,
        message_id="msg_2",
        cache_creation_tokens=0,
        cc_5m=0,
        cc_1h=0,
        cache_read_tokens=50_000,
        gap_s=100,
    )
    result = simulate([t1, t2], SONNET_RATES, POLICY_1H)
    assert result.write_tokens == 200_000 + 0
    assert result.read_tokens == 0 + 50_000


def test_synthetic_and_usageless_turns_are_skipped_in_simulate_and_observed():
    priced = _turn(cache_creation_tokens=1000, cc_5m=1000)
    synthetic = _turn(turn_index=0, is_synthetic=True, message_id="msg_synth")
    usageless = _turn(turn_index=0, message_id="msg_missing")
    turns = [priced, synthetic, usageless]

    sim_result = simulate(turns, SONNET_RATES, POLICY_5M)
    assert sim_result.turns == 1
    assert sim_result.write_tokens == 1000

    obs_result = observed(turns, SONNET_RATES)
    assert obs_result.turns == 1
    assert obs_result.write_tokens == 1000


# --------------------------------------------------------------------
# observed() == price_turn's default path
# --------------------------------------------------------------------


def test_observed_equals_price_turn_default_path_for_three_turns():
    turns = [
        _turn(input_tokens=100, output_tokens=20, cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0),
        _turn(
            turn_index=2,
            message_id="msg_2",
            input_tokens=80,
            output_tokens=15,
            cache_creation_tokens=500,
            cc_5m=500,
            cache_read_tokens=1000,
            gap_s=90,
        ),
        _turn(
            turn_index=3,
            message_id="msg_3",
            input_tokens=60,
            output_tokens=10,
            cache_creation_tokens=0,
            cache_read_tokens=1500,
            gap_s=90,
        ),
    ]
    result = observed(turns, SONNET_RATES)
    expected_total = sum(price_turn(t, SONNET_RATES).total for t in turns)
    assert result.cost == pytest.approx(expected_total)
    assert result.write_tokens == 1000 + 500 + 0
    assert result.read_tokens == 0 + 1000 + 1500


# --------------------------------------------------------------------
# dominant_ttl()
# --------------------------------------------------------------------


def test_dominant_ttl_all_5m():
    assert dominant_ttl([_turn(cc_5m=1000, cc_1h=0)]) == "5m"


def test_dominant_ttl_all_1h():
    assert dominant_ttl([_turn(cc_5m=0, cc_1h=1000)]) == "1h"


def test_dominant_ttl_boundary_ninety_percent_counts_as_dominant():
    assert dominant_ttl([_turn(cc_5m=900, cc_1h=100)]) == "5m"


def test_dominant_ttl_mixed_below_threshold():
    assert dominant_ttl([_turn(cc_5m=600, cc_1h=400)]) == "mixed"


def test_dominant_ttl_none_when_no_cache_write_tokens():
    assert dominant_ttl([_turn(cc_5m=0, cc_1h=0)]) == "none"


# --------------------------------------------------------------------
# fidelity()
# --------------------------------------------------------------------


def test_fidelity_near_zero_when_observed_matches_ideal_5m_policy():
    # Every turn's actual observed split is exactly what a 5m-policy
    # simulation would have produced, so sim(5m) == observed exactly.
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(turn_index=2, message_id="msg_2", cache_creation_tokens=500, cc_5m=500, cache_read_tokens=1000, gap_s=100)
    t3 = _turn(turn_index=3, message_id="msg_3", cache_creation_tokens=500, cc_5m=500, cache_read_tokens=1500, gap_s=100)
    turns = [t1, t2, t3]

    assert dominant_ttl(turns) == "5m"
    fid = fidelity(turns, SONNET_RATES)
    assert fid is not None
    assert fid < 0.01


def test_fidelity_none_when_dominant_is_mixed():
    turns = [_turn(cc_5m=600, cc_1h=400)]
    assert dominant_ttl(turns) == "mixed"
    assert fidelity(turns, SONNET_RATES) is None


def test_fidelity_none_when_observed_cost_is_zero():
    turns = [_turn(cc_5m=0, cc_1h=0, input_tokens=0, output_tokens=0, cache_read_tokens=0)]
    # No cache-write tokens at all: dominant_ttl is "none".
    assert fidelity(turns, SONNET_RATES) is None


# --------------------------------------------------------------------
# Recommendation thresholds (both independently blocking)
# --------------------------------------------------------------------


def test_recommendation_blocked_by_usd_threshold_despite_large_pct_saving():
    # 9.40 vs 10.00: 6% cheaper (passes the 5% bar) but only $0.60 saved.
    s = _stats(cost_observed=10.0, cost_all_5m=10.0, cost_all_1h=9.40)
    assert s.recommendation == "no material difference"


def test_recommendation_blocked_by_pct_threshold_despite_large_usd_saving():
    # 960 vs 1000: $40 saved but only 4% cheaper (fails the 5% bar).
    s = _stats(cost_observed=1000.0, cost_all_5m=1000.0, cost_all_1h=960.0)
    assert s.recommendation == "no material difference"


def test_recommendation_switches_to_1h_when_both_thresholds_clear():
    s = _stats(cost_observed=100.0, cost_all_5m=100.0, cost_all_1h=90.0)
    assert s.recommendation == "switch to 1h"


def test_recommendation_switches_to_5m_symmetrically():
    s = _stats(cost_observed=100.0, cost_all_5m=90.0, cost_all_1h=100.0)
    assert s.recommendation == "switch to 5m"


def test_recommendation_no_material_difference_when_observed_cost_zero():
    s = _stats(cost_observed=0.0, cost_all_5m=0.0, cost_all_1h=0.0)
    assert s.recommendation == "no material difference"


def test_lever_text_top_level_vs_subagent():
    top = _stats(10.0, 10.0, 9.0, key="top-level")
    sub = _stats(10.0, 10.0, 9.0, key="claude-implementer")
    assert top.lever == "promptCacheTtl"
    assert sub.lever == "experimental.cacheTtl in claude-implementer.md (or subagentPromptCacheTtl for all subagents)"


# --------------------------------------------------------------------
# TtlStats: agent-type keying, gap buckets, p50/p90
# --------------------------------------------------------------------


def test_ttl_stats_gap_buckets_and_percentiles():
    turns = [
        _turn(cache_creation_tokens=100, cc_5m=100, gap_s=None),
        _turn(turn_index=2, message_id="m2", cache_creation_tokens=100, cc_5m=100, gap_s=30),  # <1m
        _turn(turn_index=3, message_id="m3", cache_creation_tokens=100, cc_5m=100, gap_s=120),  # 1-5m
        _turn(turn_index=4, message_id="m4", cache_creation_tokens=100, cc_5m=100, gap_s=600),  # 5-15m
        _turn(turn_index=5, message_id="m5", cache_creation_tokens=100, cc_5m=100, gap_s=1800),  # 15-60m
        _turn(turn_index=6, message_id="m6", cache_creation_tokens=100, cc_5m=100, gap_s=5000),  # >60m
    ]
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=turns), SONNET_RATES)

    row = stats.by_key()["top-level"]
    assert row.spawns == 1
    assert row.priced_turns == 6
    assert row.gap_buckets == {"lt_1m": 1, "1_5m": 1, "5_15m": 1, "15_60m": 1, "gt_60m": 1}
    assert row.gaps_over_5m == 3  # 600, 1800, 5000
    assert row.gaps_over_1h == 1  # 5000
    assert row.gap_p50_s == 600
    assert row.gap_p90_s == 5000


def test_ttl_stats_keys_by_top_level_vs_agent_type():
    top_turns = [_turn(cache_creation_tokens=1000, cc_5m=1000)]
    sub_turns = [_turn(cache_creation_tokens=1000, cc_1h=1000)]
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=top_turns), SONNET_RATES)
    stats.add(
        TranscriptResult(meta=TranscriptMeta(kind="subagent", agent_type="claude-implementer"), turns=sub_turns),
        SONNET_RATES,
    )
    stats.add(
        TranscriptResult(meta=TranscriptMeta(kind="subagent", agent_type=None), turns=sub_turns),
        SONNET_RATES,
    )

    by_key = stats.by_key()
    assert set(by_key) == {"top-level", "claude-implementer", "unknown"}
    assert by_key["top-level"].spawns == 1
    assert by_key["claude-implementer"].spawns == 1


def test_ttl_stats_accumulates_spawns_and_priced_turns_across_multiple_adds():
    turns = [_turn(cache_creation_tokens=1000, cc_5m=1000)]
    stats = TtlStats()
    for i in range(3):
        stats.add(
            TranscriptResult(meta=TranscriptMeta(kind="subagent", agent_type="verification-runner"), turns=turns),
            SONNET_RATES,
        )
    row = stats.by_key()["verification-runner"]
    assert row.spawns == 3
    assert row.priced_turns == 3


# --------------------------------------------------------------------
# build_section(): table shape, fidelity warning, subscription suppression
# --------------------------------------------------------------------


def _rewrite_every_time_turns(n: int = 5, c: int = 1_000_000, gap: float = 1000.0) -> list[model.Turn]:
    """``n`` turns, each a full rewrite of the same-sized prefix ``c``
    with a fixed gap between them. Actual observed billing behaves as if
    the TTL were always 5m (every gap invalidates the write), but the
    gap is short enough (< 3600 s) that a real 1h TTL would have kept
    hitting — engineered so cost_all_1h is unambiguously cheaper than
    cost_observed/cost_all_5m by more than both switch thresholds."""
    turns = []
    for i in range(n):
        turns.append(
            _turn(
                turn_index=i + 1,
                message_id=f"msg_{i}",
                cache_creation_tokens=c,
                cc_5m=c,
                cc_1h=0,
                cache_read_tokens=0,
                gap_s=None if i == 0 else gap,
            )
        )
    return turns


def test_build_section_table_shape_and_columns():
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=_rewrite_every_time_turns()), SONNET_RATES)
    section = build_section(stats, billing_mode="api")

    assert section.key == "ttl"
    assert section.title == "Cache TTL break-even"
    assert len(section.tables) == 8
    (
        by_agent_type,
        gap_distribution,
        wasted_writes,
        premium_waste,
        break_even_share,
        near_miss,
        addressable_share,
        cache_economy_table,
    ) = section.tables
    assert by_agent_type.name == "ttl_by_agent_type"
    assert gap_distribution.name == "ttl_gap_distribution"
    assert wasted_writes.name == "ttl_wasted_writes"
    assert premium_waste.name == "ttl_premium_waste"
    assert break_even_share.name == "ttl_break_even_share"
    assert near_miss.name == "ttl_near_miss"
    assert addressable_share.name == "ttl_addressable_share"
    assert cache_economy_table.name == "ttl_cache_economy"
    assert [c.key for c in by_agent_type.columns] == [
        "agent_type",
        "spawns",
        "priced_turns",
        "observed_5m_pct",
        "observed_1h_pct",
        "gaps_over_5m",
        "gaps_over_1h",
        "gap_p50_s",
        "gap_p90_s",
        "cost_observed",
        "cost_all_5m",
        "cost_all_1h",
        "best_policy",
        "delta_usd",
        "delta_pct",
        "fidelity_pct",
        "unsimulatable",
        "recommendation",
        "lever",
    ]
    money_columns = {c.key for c in by_agent_type.columns if c.kind == "money"}
    assert money_columns == {"cost_observed", "cost_all_5m", "cost_all_1h", "delta_usd"}
    pct_columns = {c.key for c in by_agent_type.columns if c.kind == "pct"}
    assert pct_columns == {"observed_5m_pct", "observed_1h_pct", "delta_pct", "fidelity_pct"}


def test_build_section_recommends_switch_to_1h_in_api_mode():
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=_rewrite_every_time_turns()), SONNET_RATES)
    section = build_section(stats, billing_mode="api")

    table = section.tables[0]
    rec_idx = [c.key for c in table.columns].index("recommendation")
    row = next(r for r in table.rows if r[0] == "top-level")
    assert row[rec_idx] == "switch to 1h"


def test_build_section_suppresses_subagent_switch_in_subscription_mode_but_not_top_level():
    turns = _rewrite_every_time_turns()
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=turns), SONNET_RATES)
    stats.add(
        TranscriptResult(meta=TranscriptMeta(kind="subagent", agent_type="claude-implementer"), turns=turns),
        SONNET_RATES,
    )

    section = build_section(stats, billing_mode="subscription")
    table = section.tables[0]
    rec_idx = [c.key for c in table.columns].index("recommendation")
    by_key = {r[0]: r for r in table.rows}

    assert by_key["top-level"][rec_idx] == "switch to 1h"
    assert "suppressed" in by_key["claude-implementer"][rec_idx]
    assert any("usage credits" in note for note in section.notes)


def test_build_section_fidelity_warning_lists_offending_agent_types():
    # turn2 grows far beyond what a naive 5m-policy replay would predict
    # from turn1, so sim(5m) diverges noticeably from observed cost even
    # though the dominant observed TTL is unambiguously "5m".
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(turn_index=2, message_id="msg_2", cache_creation_tokens=5000, cc_5m=5000, cache_read_tokens=0, gap_s=50)
    turns = [t1, t2]

    stats = TtlStats()
    result = TranscriptResult(meta=TranscriptMeta(kind="subagent", agent_type="claude-implementer"), turns=turns)
    stats.add(result, SONNET_RATES)

    row = stats.by_key()["claude-implementer"]
    assert row.fidelity_pct is not None
    assert row.fidelity_pct > 10.0

    section = build_section(stats, billing_mode="api")
    assert any("claude-implementer" in note for note in section.notes)


# --------------------------------------------------------------------
# Real parser integration + privacy
# --------------------------------------------------------------------


def test_real_parsed_transcript_feeds_ttl_stats_and_passes_privacy(tmp_path: Path):
    lines = [
        turn_line(
            message_id="m1",
            model="claude-sonnet-5",
            input_tokens=10,
            cache_creation_input_tokens=1000,
            ephemeral_5m_input_tokens=1000,
            output_tokens=5,
            timestamp="2026-09-18T12:00:00.000Z",
        ),
        turn_line(
            message_id="m2",
            model="claude-sonnet-5",
            input_tokens=10,
            cache_creation_input_tokens=500,
            ephemeral_5m_input_tokens=500,
            cache_read_input_tokens=1000,
            output_tokens=5,
            timestamp="2026-09-18T12:01:30.000Z",
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level"))
    assert_privacy(result)

    stats = TtlStats()
    stats.add(result, SONNET_RATES)
    row = stats.by_key()["top-level"]
    assert row.priced_turns == 2
    assert row.spawns == 1

    section = build_section(stats, billing_mode="api")
    assert section.key == "ttl"
    assert len(section.tables) == 8


# --------------------------------------------------------------------
# Cache utilisation monitoring follow-up (items 1-6): wasted writes,
# 1h premium waste vs 5m expiry loss, break-even share, near-miss
# histogram, TTL-addressable share, cache economy.
# --------------------------------------------------------------------


def _row_for(turns: list[model.Turn], key: str = "top-level") -> TtlTypeStats:
    """Feed ``turns`` as a single top-level transcript into a fresh
    ``TtlStats`` and return its rolled-up row, at ``SONNET_RATES``."""
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=turns), SONNET_RATES)
    return stats.by_key()[key]


# -- Item 1: wasted writes --------------------------------------------


def test_wasted_writes_hand_computed_sonnet_5():
    """Four turns, all 5m writes (Sonnet 5 cache_write_5m = 2.5/M):

    - t1 writes C1=1000 (cc_5m=1000). t2's gap is 100s (<=300) and reads
      back 1000 (>= C1) -> t1's write is USED.
    - t2 writes 200 more (cc_5m=200, C2=1200). t3's gap is 400s (>300)
      -> the chain breaks before t3 even gets checked -> t2's write is
      NEVER READ AGAIN (wasted). USD = 200/1e6 * 2.5 = 0.0005.
    - t3 writes C3=1200 (cc_5m=1200, full rewrite since t2's entry
      expired). t4's gap is 100s (<=300) and reads back 1200 (>= C3)
      -> t3's write is USED.
    - t4 writes 300 more (cc_5m=300) and is the transcript's LAST turn,
      so it is a terminal write: USD = 300/1e6 * 2.5 = 0.00075. It is
      not a candidate for "used"/"wasted" at all (nothing can come
      after it) and must not appear in writes/wasted_writes/tokens
      written/tokens wasted/share.

    Non-terminal totals: writes=3 (t1, t2, t3), wasted_writes=1 (t2),
    tokens_written=1000+200+1200=2400, tokens_wasted=200,
    share=200/2400=8.333...%, usd_wasted=0.0005.
    """
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(
        turn_index=2, message_id="msg_2", cache_creation_tokens=200, cc_5m=200, cache_read_tokens=1000, gap_s=100
    )
    t3 = _turn(
        turn_index=3, message_id="msg_3", cache_creation_tokens=1200, cc_5m=1200, cache_read_tokens=0, gap_s=400
    )
    t4 = _turn(
        turn_index=4, message_id="msg_4", cache_creation_tokens=300, cc_5m=300, cache_read_tokens=1200, gap_s=100
    )
    row = _row_for([t1, t2, t3, t4])

    assert row.waste_writes == 3
    assert row.waste_wasted_writes == 1
    assert row.waste_tokens_written == 2400
    assert row.waste_tokens_wasted == 200
    assert row.waste_share_pct == pytest.approx(100.0 * 200 / 2400)
    assert row.waste_usd_wasted == pytest.approx(200 / 1_000_000 * 2.5)
    assert row.waste_terminal_writes == 1
    assert row.waste_terminal_tokens == 300
    assert row.waste_terminal_usd == pytest.approx(300 / 1_000_000 * 2.5)


def test_terminal_write_excluded_from_waste_share():
    """t1 writes 1000 (cc_5m), used by t2 (gap 100s <= 300, reads back
    1000). t2 is the transcript's LAST turn and itself writes 500 more
    (cc_5m=500): that write is terminal — unavoidable, since nothing can
    come after the last turn — and must not inflate tokens_written or
    tokens_wasted, so waste_share_pct stays 0.0 rather than counting the
    terminal write as "wasted"."""
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(
        turn_index=2, message_id="msg_2", cache_creation_tokens=500, cc_5m=500, cache_read_tokens=1000, gap_s=100
    )
    row = _row_for([t1, t2])

    assert row.waste_writes == 1
    assert row.waste_wasted_writes == 0
    assert row.waste_tokens_written == 1000
    assert row.waste_tokens_wasted == 0
    assert row.waste_share_pct == 0.0
    assert row.waste_terminal_writes == 1
    assert row.waste_terminal_tokens == 500
    assert row.waste_terminal_usd == pytest.approx(500 / 1_000_000 * 2.5)


def test_mixed_write_counted_as_two_separate_writes():
    """t1 is a mixed write: cc_5m=1000 (TTL 300s) and cc_1h=2000 (TTL
    3600s) in the same turn, C1 = 0 + 3000 = 3000. t2's gap is 500s and
    it reads back 3000 (>= C1):

    - The cc_5m portion's chain check uses TTL=300: 500 > 300, so the
      chain breaks before t2 is even considered -> NEVER READ AGAIN.
    - The cc_1h portion's chain check uses TTL=3600: 500 <= 3600, so t2
      is considered, and its read (3000) >= C1 -> USED.

    A single write couldn't have two different outcomes from the same
    next turn; getting a "wasted" 5m portion and a "used" 1h portion out
    of the same t1 proves they're tracked as two independent writes.
    t3 is a filler last turn with no cache_creation of its own (keeps
    this fixture focused; also confirms it contributes nothing to the
    waste table).

    USD wasted = the 5m portion only = 1000/1e6 * 2.5 = 0.0025.
    """
    t1 = _turn(cache_creation_tokens=3000, cc_5m=1000, cc_1h=2000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(
        turn_index=2, message_id="msg_2", cache_creation_tokens=0, cache_read_tokens=3000, gap_s=500
    )
    t3 = _turn(turn_index=3, message_id="msg_3", cache_creation_tokens=0, cache_read_tokens=0, gap_s=200)
    row = _row_for([t1, t2, t3])

    assert row.waste_writes == 2
    assert row.waste_wasted_writes == 1
    assert row.waste_tokens_written == 1000 + 2000
    assert row.waste_tokens_wasted == 1000
    assert row.waste_usd_wasted == pytest.approx(1000 / 1_000_000 * 2.5)
    assert row.waste_terminal_writes == 0


# -- Item 2: 1h premium waste vs 5m expiry loss ------------------------


def test_premium_waste_vs_expiry_loss_hand_computed_sonnet_5():
    """Four turns (Sonnet 5: cache_write_5m=2.5/M, cache_write_1h=4.0/M).
    Each turn's bucket is decided by the gap to the *next* turn
    (t2.gap_s is "t1's next gap", etc.):

    cc_1h side:
    - t1: cc_1h=1000, next gap (t2.gap_s) = 200 (<=300) ->
      "premium paid, not needed". USD = 1000/1e6*(4.0-2.5) = 0.0015.
    - t2: cc_1h=800, next gap (t3.gap_s) = 1000 (300 < 1000 <= 3600) ->
      "premium earned". USD saved = 800/1e6*2.5 = 0.002.
    - t3: cc_1h=500, next gap (t4.gap_s) = 4000 (>3600) ->
      "expired anyway". USD = 500/1e6*(4.0-2.5) = 0.00075.
    - t4: cc_1h=0 (last turn, no next gap) -> contributes nothing.

    cc_5m side (same four turns, same next-gap values):
    - t1: cc_5m=300, next gap 200 (<=300) -> "fine" (no USD given).
    - t2: cc_5m=400, next gap 1000 (in window) -> "5m expiry loss": USD
      = the NEXT turn's (t3's) actual observed cache_creation write
      cost = t3's cc_1h(500)*4.0/1e6 + cc_5m(600)*2.5/1e6
           = 0.002 + 0.0015 = 0.0035.
    - t3: cc_5m=600, next gap 4000 (>3600) ->
      "would have expired under 1h too".
    - t4: cc_5m=0 -> contributes nothing.
    """
    t1 = _turn(cache_creation_tokens=1300, cc_5m=300, cc_1h=1000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(
        turn_index=2,
        message_id="msg_2",
        cache_creation_tokens=1200,
        cc_5m=400,
        cc_1h=800,
        cache_read_tokens=0,
        gap_s=200,
    )
    t3 = _turn(
        turn_index=3,
        message_id="msg_3",
        cache_creation_tokens=1100,
        cc_5m=600,
        cc_1h=500,
        cache_read_tokens=0,
        gap_s=1000,
    )
    t4 = _turn(
        turn_index=4,
        message_id="msg_4",
        cache_creation_tokens=0,
        cc_5m=0,
        cc_1h=0,
        cache_read_tokens=0,
        gap_s=4000,
    )
    row = _row_for([t1, t2, t3, t4])

    assert row.premium_1h_not_needed_tokens == 1000
    assert row.premium_1h_not_needed_usd == pytest.approx(1000 / 1_000_000 * (4.0 - 2.5))
    assert row.premium_1h_earned_tokens == 800
    assert row.premium_1h_earned_usd == pytest.approx(800 / 1_000_000 * 2.5)
    assert row.premium_1h_expired_tokens == 500
    assert row.premium_1h_expired_usd == pytest.approx(500 / 1_000_000 * (4.0 - 2.5))

    assert row.premium_5m_fine_tokens == 300
    assert row.premium_5m_loss_tokens == 400
    expected_t3_write_cost = 500 / 1_000_000 * 4.0 + 600 / 1_000_000 * 2.5
    assert row.premium_5m_loss_usd == pytest.approx(expected_t3_write_cost)
    assert row.premium_5m_would_expire_tokens == 600


# -- Item 3: break-even share -------------------------------------------


def test_break_even_premium_ratio_is_0_6_for_sonnet_5():
    """premium_ratio = (write_1h - write_5m) / write_5m, computed from
    the resolved rate card, never hard-coded: for Sonnet 5,
    (4.0 - 2.5) / 2.5 = 0.6 exactly."""
    row = _row_for([_turn(cache_creation_tokens=100, cc_5m=100)])
    assert row.premium_ratio == pytest.approx(0.6)


def test_break_even_share_hand_computed_sonnet_5():
    """Three turns. t1 is the first write (its gap is excluded from the
    weighted share, per simulate's own i==0 convention). t2's gap is
    500s (in the (300, 3600] window) with prefix C2 = cache_read(1000) +
    cache_creation(2000) = 3000. t3's gap is 100s (NOT in the window,
    <=300) with prefix C3 = cache_read(500) + cache_creation(500) =
    1000.

    in_window_share = C2 / (C2 + C3) = 3000 / 4000 = 0.75 (prefix-
    weighted, per the module's definition — weighted by the prefix size
    of the turn that *follows* each gap).

    premium_ratio = 0.6 (Sonnet 5, see the dedicated ratio test).
    margin_pts = (0.75 - 0.6) * 100 = 15.0 points -> not "marginal"
    (>=5 points) and positive -> verdict "1h pays".
    """
    t1 = _turn(cache_creation_tokens=500, cc_5m=500, cache_read_tokens=0, gap_s=None)
    t2 = _turn(
        turn_index=2, message_id="msg_2", cache_creation_tokens=2000, cc_5m=2000, cache_read_tokens=1000, gap_s=500
    )
    t3 = _turn(
        turn_index=3, message_id="msg_3", cache_creation_tokens=500, cc_5m=500, cache_read_tokens=500, gap_s=100
    )
    row = _row_for([t1, t2, t3])

    assert row.premium_ratio == pytest.approx(0.6)
    assert row.in_window_share == pytest.approx(0.75)
    assert row.margin_pts == pytest.approx(15.0)
    assert row.verdict == "1h pays"


def test_break_even_verdict_marginal_within_five_points():
    s = TtlTypeStats(
        key="top-level",
        spawns=1,
        priced_turns=1,
        observed_5m_pct=100.0,
        observed_1h_pct=0.0,
        gaps_over_5m=0,
        gaps_over_1h=0,
        gap_p50_s=None,
        gap_p90_s=None,
        cost_observed=1.0,
        cost_all_5m=1.0,
        cost_all_1h=1.0,
        unsimulatable=0,
        fidelity_pct=0.0,
        gap_buckets={},
        premium_ratio=0.6,
        in_window_share=0.62,  # margin = (0.62-0.6)*100 = 2.0 points
    )
    assert s.margin_pts == pytest.approx(2.0)
    assert s.verdict == "marginal"


# -- Item 4: near-miss histogram -----------------------------------------


def test_near_miss_histogram_boundaries_hand_computed_sonnet_5():
    """Boundaries are asymmetric on purpose: [240, 300] and [3540, 3600]
    (inclusive both ends) still hit the cache; (300, 360] and
    (3600, 3660] (exclusive lower, inclusive upper) just missed it and
    forced a full rewrite; anything past 360/3660 is neither bucket.

    5m side: gaps 240 and 300 -> hits (2). Gaps 301 and 360 -> misses
    (2), rewriting 100 and 200 tokens respectively
    (tokens=300, USD=(100+200)/1e6*2.5=0.00075). Gap 361 -> neither.

    1h side: gaps 3540 and 3600 -> hits (2). Gaps 3601 and 3660 ->
    misses (2), rewriting 300 and 400 tokens respectively
    (tokens=700, USD=(300+400)/1e6*4.0=0.0028). Gap 3661 -> neither.
    """
    turns = [_turn(cache_creation_tokens=0, cache_read_tokens=0, gap_s=None)]
    gap_values = [240, 300, 301, 360, 361, 3540, 3600, 3601, 3660, 3661]
    # tokens rewritten only matter for the two "just missed" gaps at
    # each boundary (301 -> 100 tokens cc_5m, 360 -> 200 tokens cc_5m,
    # 3601 -> 300 tokens cc_1h, 3660 -> 400 tokens cc_1h); every other
    # gap carries no cache-write tokens (irrelevant to the histogram).
    rewrite_tokens = {301: ("cc_5m", 100), 360: ("cc_5m", 200), 3601: ("cc_1h", 300), 3660: ("cc_1h", 400)}
    for idx, gap in enumerate(gap_values, start=2):
        kwargs = dict(turn_index=idx, message_id=f"msg_{idx}", gap_s=float(gap))
        if gap in rewrite_tokens:
            field_name, tokens = rewrite_tokens[gap]
            kwargs["cache_creation_tokens"] = tokens
            kwargs[field_name] = tokens
        turns.append(_turn(**kwargs))

    row = _row_for(turns)

    assert row.near_5m_hit == 2
    assert row.near_5m_miss == 2
    assert row.near_5m_miss_tokens == 300
    assert row.near_5m_miss_usd == pytest.approx(300 / 1_000_000 * 2.5)
    assert row.near_1h_hit == 2
    assert row.near_1h_miss == 2
    assert row.near_1h_miss_tokens == 700
    assert row.near_1h_miss_usd == pytest.approx(700 / 1_000_000 * 4.0)


# -- Item 5: TTL-addressable share ---------------------------------------


def test_addressable_share_falls_back_to_minimal_rule_when_signatures_none():
    """Every turn's ``recache_signature`` is ``None`` (the default —
    WP3 hasn't run in this worktree), so classification falls back to
    WP3's own minimal rule turn by turn: ``turn_index > 1``,
    ``ctx > 20_000``, ``cache_read < 0.2 * ctx``.

    - t1 (turn_index=1) never qualifies regardless of its ctx/cache_read
      (turn_index > 1 required) -> excluded.
    - t2: ctx=25_000, cache_read=500 (500 < 20% of 25_000=5_000, and
      500 < 2_000) -> "full-expiry". cache_creation_tokens=3000 ->
      USD = 3000/1e6*2.5 = 0.0075.
    - t3: ctx=30_000, cache_read=3_000 (3_000 < 20% of 30_000=6_000, but
      3_000 >= 2_000) -> "prefix-invalidated". cache_creation_tokens=2000
      -> USD = 2000/1e6*2.5 = 0.005.
    - t4: ctx=10_000 (not > 20_000) -> not a re-cache turn at all,
      excluded from both buckets.
    """
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, ctx=1000, gap_s=None)
    t2 = _turn(
        turn_index=2,
        message_id="msg_2",
        cache_creation_tokens=3000,
        cc_5m=3000,
        cache_read_tokens=500,
        ctx=25_000,
        gap_s=100,
    )
    t3 = _turn(
        turn_index=3,
        message_id="msg_3",
        cache_creation_tokens=2000,
        cc_5m=2000,
        cache_read_tokens=3000,
        ctx=30_000,
        gap_s=100,
    )
    t4 = _turn(
        turn_index=4,
        message_id="msg_4",
        cache_creation_tokens=500,
        cc_5m=500,
        cache_read_tokens=200,
        ctx=10_000,
        gap_s=100,
    )
    for t in (t1, t2, t3, t4):
        assert t.recache_signature is None
    row = _row_for([t1, t2, t3, t4])

    assert row.addressable_full_expiry_tokens == 3000
    assert row.addressable_full_expiry_usd == pytest.approx(3000 / 1_000_000 * 2.5)
    assert row.addressable_prefix_invalidated_tokens == 2000
    assert row.addressable_prefix_invalidated_usd == pytest.approx(2000 / 1_000_000 * 2.5)


def test_addressable_share_uses_real_signature_when_set():
    # gap_s=50 and ctx=0 would NOT satisfy the minimal fallback rule at
    # all (ctx not > 20_000), yet the real signature still classifies
    # this turn as full-expiry -- proving the signature is preferred
    # over the fallback whenever it is actually set.
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(turn_index=2, message_id="msg_2", cache_creation_tokens=400, cc_5m=400, cache_read_tokens=100, gap_s=50)
    t2 = dataclasses.replace(t2, recache_signature="full-expiry")
    row = _row_for([t1, t2])

    assert row.addressable_full_expiry_tokens == 400
    assert row.addressable_full_expiry_usd == pytest.approx(400 / 1_000_000 * 2.5)
    assert row.addressable_prefix_invalidated_tokens == 0


# -- Item 6: cache economy -------------------------------------------------


def test_cache_economy_hand_computed_sonnet_5():
    """Four turns (Sonnet 5: input=2.0, output=10.0, write_5m=2.5,
    write_1h=4.0, read=0.2, all per million tokens):

    - t1: input=1000, output=200, cc_5m=5000 (write), read=0.
      cost = 1000/1e6*2 + 200/1e6*10 + 5000/1e6*2.5 + 0
           = 0.002 + 0.002 + 0.0125 = 0.0165.
      uncached-equivalent (cache tokens at input rate) = 5000/1e6*2 = 0.01.
    - t2: input=800, output=150, read=5000, no write.
      cost = 0.0016 + 0.0015 + 0 + 5000/1e6*0.2(=0.001) = 0.0041.
      uncached-equivalent = 5000/1e6*2 = 0.01.
    - t3: input=600, output=100, cc_1h=2000 (write), read=3000.
      cost = 0.0012 + 0.001 + 2000/1e6*4.0(=0.008) + 3000/1e6*0.2(=0.0006)
           = 0.0108.
      uncached-equivalent = (3000+2000)/1e6*2 = 0.01.
    - t4: input=400, output=50, read=5000, no write.
      cost = 0.0008 + 0.0005 + 0 + 0.001 = 0.0023.
      uncached-equivalent = 5000/1e6*2 = 0.01.

    Totals: tokens_written=5000+0+2000+0=7000,
    tokens_read=0+5000+3000+5000=13000,
    write_usd=0.0125+0+0.008+0=0.0205,
    read_usd=0+0.001+0.0006+0.001=0.0026,
    uncached_equivalent_usd=0.01*4=0.04,
    net_saving_usd=0.04-(0.0205+0.0026)=0.0169,
    cache_roi=0.0169/0.0205.
    """
    t1 = _turn(input_tokens=1000, output_tokens=200, cache_creation_tokens=5000, cc_5m=5000, cache_read_tokens=0)
    t2 = _turn(
        turn_index=2, message_id="msg_2", input_tokens=800, output_tokens=150, cache_read_tokens=5000, gap_s=90
    )
    t3 = _turn(
        turn_index=3,
        message_id="msg_3",
        input_tokens=600,
        output_tokens=100,
        cache_creation_tokens=2000,
        cc_1h=2000,
        cache_read_tokens=3000,
        gap_s=90,
    )
    t4 = _turn(
        turn_index=4, message_id="msg_4", input_tokens=400, output_tokens=50, cache_read_tokens=5000, gap_s=90
    )
    turns = [t1, t2, t3, t4]

    write_usd = 0.0125 + 0 + 0.008 + 0
    read_usd = 0 + 0.001 + 0.0006 + 0.001
    uncached_equivalent_usd = 0.01 * 4
    net_saving_usd = uncached_equivalent_usd - (write_usd + read_usd)

    row = _row_for(turns)
    assert row.economy_tokens_written == 7000
    assert row.economy_tokens_read == 13000
    assert row.economy_write_usd == pytest.approx(write_usd)
    assert row.economy_read_usd == pytest.approx(read_usd)
    assert row.economy_uncached_equivalent_usd == pytest.approx(uncached_equivalent_usd)
    assert row.net_saving_usd == pytest.approx(net_saving_usd)
    assert row.cache_roi == pytest.approx(net_saving_usd / write_usd)

    # The standalone helper (for WP10's independent "overall" line) must
    # agree with TtlStats's own roll-up exactly.
    standalone = cache_economy(turns, SONNET_RATES)
    assert standalone["tokens_written"] == row.economy_tokens_written
    assert standalone["tokens_read"] == row.economy_tokens_read
    assert standalone["write_usd"] == pytest.approx(row.economy_write_usd)
    assert standalone["read_usd"] == pytest.approx(row.economy_read_usd)
    assert standalone["uncached_equivalent_usd"] == pytest.approx(row.economy_uncached_equivalent_usd)
    assert standalone["net_saving_usd"] == pytest.approx(row.net_saving_usd)
    assert standalone["cache_roi"] == pytest.approx(row.cache_roi)


def test_cache_economy_zero_write_usd_gives_zero_roi():
    turns = [_turn(input_tokens=100, output_tokens=10, cache_read_tokens=500, cache_creation_tokens=0)]
    result = cache_economy(turns, SONNET_RATES)
    assert result["write_usd"] == 0.0
    assert result["cache_roi"] == 0.0


# -- build_section: new tables, notes, window_start caveat ----------------


def test_build_section_new_tables_have_expected_notes():
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=_rewrite_every_time_turns()), SONNET_RATES)
    section = build_section(stats, billing_mode="api")
    tables_by_name = {t.name: t for t in section.tables}

    assert "prefix-weighted share of gaps" in " ".join(tables_by_name["ttl_break_even_share"].notes)
    assert "statusline countdown" in " ".join(tables_by_name["ttl_near_miss"].notes)


def test_build_section_window_start_caveat_note_only_when_subagent_predates_window():
    import datetime as dt

    turns = [_turn(cache_creation_tokens=100, cc_5m=100)]

    # No caveat: no window_start given at all.
    stats = TtlStats()
    stats.add(
        TranscriptResult(
            meta=TranscriptMeta(kind="subagent", agent_type="claude-implementer", mtime_ns=0), turns=turns
        ),
        SONNET_RATES,
    )
    section = build_section(stats, billing_mode="api")
    assert not any("find_subagents" in note for note in section.notes)

    # No caveat: window_start given, but the subagent's mtime is inside it.
    recent_ns = int(dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc).timestamp() * 1_000_000_000)
    stats2 = TtlStats()
    stats2.add(
        TranscriptResult(
            meta=TranscriptMeta(kind="subagent", agent_type="claude-implementer", mtime_ns=recent_ns),
            turns=turns,
        ),
        SONNET_RATES,
    )
    section2 = build_section(stats2, billing_mode="api", window_start=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
    assert not any("find_subagents" in note for note in section2.notes)

    # Caveat present: window_start given, subagent mtime predates it.
    old_ns = int(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1_000_000_000)
    stats3 = TtlStats()
    stats3.add(
        TranscriptResult(
            meta=TranscriptMeta(kind="subagent", agent_type="claude-implementer", mtime_ns=old_ns), turns=turns
        ),
        SONNET_RATES,
    )
    section3 = build_section(stats3, billing_mode="api", window_start=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
    assert any("find_subagents" in note for note in section3.notes)
