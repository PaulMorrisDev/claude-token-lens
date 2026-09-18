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
    assert len(section.tables) == 2
    by_agent_type, gap_distribution = section.tables
    assert by_agent_type.name == "ttl_by_agent_type"
    assert gap_distribution.name == "ttl_gap_distribution"
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
    assert len(section.tables) == 2
