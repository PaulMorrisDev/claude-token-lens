"""Tests for the ``autoCompactWindow`` sweep
(``src/claude_token_lens/compaction_sim.py``).

Replay arithmetic is exercised on hand-built ``model.Turn`` instances
(the ``_turn`` helper below, matching ``test_ttl.py``'s convention) with
hand-computed costs at the packaged Sonnet 5 rates (input 2.0, output
10.0, cache_write_5m 2.5, cache_write_1h 4.0, cache_read 0.2 per million
tokens; see ``_synthetic_20_turn_transcript``'s docstring for the by-hand
derivation). The fidelity and rule tests build a small hand-crafted
``TranscriptResult`` with a real ``COMPACT_BOUNDARY`` event rather than
going through the real parser, since only the event/turn timestamp
correlation and cost arithmetic are under test here.
"""

from __future__ import annotations

import pytest

from claude_token_lens import model
from claude_token_lens.compaction_sim import (
    ASSUMPTIONS,
    CANDIDATE_WINDOWS,
    RULES,
    CompactionSimThresholds,
    build_section,
    simulate_compaction_windows,
)
from claude_token_lens.model import Event, EventKind, ReportModel, ReportMeta, TranscriptMeta, TranscriptResult
from claude_token_lens.pricing import load_pricing

from helpers import assert_privacy

PRICING = load_pricing()
SONNET_RATES = PRICING.resolve_model("claude-sonnet-5")


def _turn(**overrides) -> model.Turn:
    """Build a priced ``Turn`` with sane zero defaults, overridable per
    field -- same convention as ``test_ttl.py``'s ``_turn``."""
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


def _top_level_transcript(session_id: str, turns: list[model.Turn], events: list[Event] | None = None) -> TranscriptResult:
    return TranscriptResult(
        meta=TranscriptMeta(path=f"{session_id}.jsonl", kind="top-level", session_id=session_id),
        turns=turns,
        events=events or [],
    )


def _ts(minute: int) -> str:
    return f"2026-09-18T12:{minute:02d}:00.000Z"


def _synthetic_20_turn_transcript() -> list[model.Turn]:
    """20 priced turns with linearly growing ctx and NO real compaction
    event: turn i (1-indexed) carries K=20,000 new cache-creation
    tokens, reads back everything written by every earlier turn
    (cache_read_i = (i-1)*K), so ctx_i = i*K, up to ctx_20 = 400,000.
    input/output tokens are 0 throughout so every dollar is cache-write
    or cache-read, keeping the by-hand arithmetic in this file's tests
    tractable. All cc_5m = cache_creation_tokens, cc_1h = 0 (every write
    priced at the 5-minute rate).
    """
    turns = []
    k = 20_000
    for i in range(1, 21):
        ctx = i * k
        cache_read = (i - 1) * k
        turns.append(
            _turn(
                message_id=f"msg_{i}",
                request_id=f"req_{i}",
                turn_index=i,
                ts=_ts(i),
                cache_creation_tokens=k,
                cache_read_tokens=cache_read,
                cc_5m=k,
                cc_1h=0,
                ctx=ctx,
            )
        )
    return turns


# -- replay arithmetic ------------------------------------------------------


def test_window_none_has_zero_synthetic_compactions_and_matches_true_observed_cost():
    """window=None never opens the synthetic-compaction guard, so its
    row's cost must equal the sum of pricing every turn's own real,
    unmodified values -- the module docstring's "no candidate window"
    identity. By hand: sum_{i=1..20} [K*2.5 + (i-1)*K*0.2] / 1e6
    = 20*0.05 + 0.004*sum_{k=0..19}k = 1.0 + 0.004*190 = 1.76.
    """
    turns = _synthetic_20_turn_transcript()
    tr = _top_level_transcript("sess-synthetic", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    rows = {r.window: r for r in stats.by_window("top-level")}

    assert rows[None].compactions == 0
    assert rows[None].cost == pytest.approx(1.76)
    # The observed cost baseline every other row's delta is measured
    # against must be this same true observed cost.
    assert rows[None].observed_cost == pytest.approx(1.76)
    assert rows[None].delta_usd == pytest.approx(0.0)


def test_small_window_hand_computed_compaction_count_and_cost():
    """window=100,000 on the same 20-turn transcript (turn i has
    ctx = i*20,000: a 20,000-token write plus the rest read) triggers
    three synthetic compactions. With no real compact_boundary event
    anywhere in this corpus, the compression ratio and rediscovery
    allowance both fall back to their defaults (0.15, $0.00). Each
    summary removes ``ctx - post`` tokens from every later turn's cache
    reads; the 20,000 tokens each later turn adds are kept whole.

    By hand (write 2.5/1e6, read 0.2/1e6 per token):
      turns 1-5 (as observed):        0.05 .. 0.066            -> 0.29
      turn 6 (ctx 120,000 > 100,000): summary write 18,000     -> 0.045
      turns 7-10: write 20,000 + read 18,000/38,000/58,000/78,000
                                     0.0536+0.0576+0.0616+0.0656 -> 0.2384
      turn 11 (context 118,000):      summary write 17,700     -> 0.04425
      turns 12-15: reads 17,700 .. 77,700                      -> 0.23816
      turn 16 (context 117,700):      summary write 17,655     -> 0.0441375
      turns 17-20: reads 17,655 .. 77,655                      -> 0.238124
      total = 1.1380715
    """
    turns = _synthetic_20_turn_transcript()
    tr = _top_level_transcript("sess-synthetic", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    rows = {r.window: r for r in stats.by_window("top-level")}

    row = rows[100_000]
    assert row.compactions == 3
    assert row.cost == pytest.approx(1.1380715)
    assert row.observed_cost == pytest.approx(1.76)
    # Sign convention: candidate - observed, negative = cheaper.
    assert row.delta_usd == pytest.approx(1.1380715 - 1.76)
    assert row.delta_usd < 0
    assert row.saving_usd == pytest.approx(1.76 - 1.1380715)
    assert row.delta_pct == pytest.approx(100.0 * (1.1380715 - 1.76) / 1.76)


def test_every_candidate_window_present_and_ordered():
    turns = _synthetic_20_turn_transcript()
    tr = _top_level_transcript("sess-synthetic", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    rows = stats.by_window("top-level")
    assert [r.window for r in rows] == list(CANDIDATE_WINDOWS)


def test_smaller_window_never_costs_more_than_a_larger_one_on_a_monotonic_series():
    """A sanity property on this specific monotonically-growing-ctx
    fixture (not a general theorem for every transcript shape): forcing
    more, earlier compactions here only ever removes cache-read volume
    it would otherwise have paid for, so cost should be non-increasing
    as the window shrinks."""
    turns = _synthetic_20_turn_transcript()
    tr = _top_level_transcript("sess-synthetic", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    rows = {r.window: r for r in stats.by_window("top-level")}
    ordered = [100_000, 150_000, 200_000, 250_000, 300_000, 400_000, 500_000, None]
    costs = [rows[w].cost for w in ordered]
    assert costs == sorted(costs)


# -- fidelity self-check ------------------------------------------------


def _fidelity_transcript() -> TranscriptResult:
    """A top-level transcript with one real, auto-triggered
    COMPACT_BOUNDARY event correlated to turn 4, and ctx that never
    again approaches the configured window afterwards -- so simulating
    at that same configured window should reproduce the exact observed
    cost (only the one real compaction ever fires, under both
    ``window=None`` and ``window=200_000``)."""
    turns = [
        _turn(turn_index=1, ts=_ts(0), ctx=50_000, cache_creation_tokens=50_000, cache_read_tokens=0, cc_5m=50_000),
        _turn(turn_index=2, ts=_ts(1), ctx=100_000, cache_creation_tokens=50_000, cache_read_tokens=50_000, cc_5m=50_000),
        _turn(turn_index=3, ts=_ts(2), ctx=190_000, cache_creation_tokens=90_000, cache_read_tokens=100_000, cc_5m=90_000),
        # Real compaction correlates here (event ts 12:02:30, this turn's
        # ts 12:03:00 -- a 30s join, well inside the default 900s gate).
        _turn(turn_index=4, ts="2026-09-18T12:03:00.000Z", ctx=30_000, cache_creation_tokens=30_000, cache_read_tokens=0, cc_5m=30_000),
        _turn(turn_index=5, ts=_ts(4), ctx=60_000, cache_creation_tokens=30_000, cache_read_tokens=30_000, cc_5m=30_000),
        _turn(turn_index=6, ts=_ts(5), ctx=90_000, cache_creation_tokens=30_000, cache_read_tokens=60_000, cc_5m=30_000),
    ]
    events = [
        Event(
            kind=EventKind.COMPACT_BOUNDARY,
            ts="2026-09-18T12:02:30.000Z",
            pre_tokens=195_000,
            post_tokens=30_000,
            trigger="auto",
        )
    ]
    return _top_level_transcript("sess-fidelity", turns, events)


def test_fidelity_under_one_percent_when_simulating_the_observed_window():
    tr = _fidelity_transcript()
    stats = simulate_compaction_windows([tr], SONNET_RATES, {"sess-fidelity": 200_000})

    assert len(stats.fidelity_rows) == 1
    row = stats.fidelity_rows[0]
    assert row.session_id == "sess-fidelity"
    assert row.window == 200_000
    assert row.fidelity_pct is not None
    assert row.fidelity_pct < 1.0

    # The real compaction is kept under every candidate window, including
    # the one matching the session's own configured setting.
    by_window = {r.window: r for r in stats.by_window("top-level")}
    assert by_window[200_000].compactions == 1
    assert by_window[None].compactions == 1
    assert by_window[200_000].cost == pytest.approx(by_window[None].cost)


def test_no_fidelity_row_when_snapshot_window_unknown():
    tr = _fidelity_transcript()
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    assert stats.fidelity_rows == []


# -- build_section --------------------------------------------------------


def test_build_section_tables_and_notes():
    turns = _synthetic_20_turn_transcript()
    tr = _top_level_transcript("sess-synthetic", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {"sess-synthetic": 100_000})
    section = build_section(stats)

    assert section.key == "compaction_sim"
    names = [t.name for t in section.tables]
    assert names == ["compaction_sim_by_window", "compaction_sim_by_agent_type", "compaction_sim_fidelity"]

    by_window = next(t for t in section.tables if t.name == "compaction_sim_by_window")
    assert [row[0] for row in by_window.rows] == [
        "100,000", "150,000", "200,000", "250,000", "300,000", "400,000", "500,000", "none",
    ]

    by_type = next(t for t in section.tables if t.name == "compaction_sim_by_agent_type")
    assert [row[0] for row in by_type.rows] == ["top-level"]

    for note in ASSUMPTIONS:
        assert note in section.notes

    assert_privacy(section)


def test_build_section_notes_flag_default_ratio_and_allowance():
    turns = _synthetic_20_turn_transcript()
    tr = _top_level_transcript("sess-synthetic", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    section = build_section(stats)
    joined = " ".join(section.notes)
    assert "0.150" in joined
    assert "no real compact_boundary event found" in joined
    assert "no real post-compaction re-cache turn found" in joined


def test_build_section_empty_corpus_notes_instead_of_crashing():
    stats = simulate_compaction_windows([], SONNET_RATES, {})
    section = build_section(stats)
    by_window = next(t for t in section.tables if t.name == "compaction_sim_by_window")
    assert by_window.rows == []
    assert by_window.notes


# -- thresholds -------------------------------------------------------------


def test_thresholds_from_config_flat_dict():
    th = CompactionSimThresholds.from_config({"switch_usd": 2.5, "fidelity_warn_pct": 20.0})
    assert th.switch_usd == 2.5
    assert th.fidelity_warn_pct == 20.0
    assert th.switch_pct == CompactionSimThresholds().switch_pct


def test_thresholds_from_config_nested_dict():
    th = CompactionSimThresholds.from_config({"thresholds": {"default_compression_ratio": 0.3}})
    assert th.default_compression_ratio == 0.3


def test_thresholds_from_config_none():
    assert CompactionSimThresholds.from_config(None) == CompactionSimThresholds()


def test_thresholds_describe_nonempty():
    assert CompactionSimThresholds().describe()


# -- rule: compaction-window -----------------------------------------------


def _base_report(sections: list[model.Section]) -> ReportModel:
    return ReportModel(meta=ReportMeta(), sections=sections, recommendations=[])


#: The synthetic transcript costs 1.76 USD in all, so the rule tests
#: lower the default 1 USD bar; nothing else changes.
_SMALL_FIXTURE_TH = CompactionSimThresholds(switch_usd=0.1)


def test_rule_fires_when_saving_clears_both_thresholds():
    turns = _synthetic_20_turn_transcript()
    tr = _top_level_transcript("sess-synthetic", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    section = build_section(stats)
    report = _base_report([section])

    recs = RULES[0](report, _SMALL_FIXTURE_TH, None)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == "compaction-window"
    assert rec.category == "settings"
    assert rec.lever == "autoCompactWindow"
    assert rec.scope == "user"
    # 100,000 would summarise 3 times a session; 150,000 is the smallest
    # window with at most 2.
    assert "150,000" in rec.action
    assert_privacy(rec)


def test_rule_does_not_fire_when_no_transcripts():
    stats = simulate_compaction_windows([], SONNET_RATES, {})
    section = build_section(stats)
    report = _base_report([section])
    recs = RULES[0](report, CompactionSimThresholds(), None)
    assert recs == []


def test_rule_does_not_fire_below_switch_usd_threshold():
    """A single, tiny transcript where even the best window's saving
    never clears a high switch_usd bar must not fire."""
    turns = _synthetic_20_turn_transcript()[:3]  # a few small turns only
    tr = _top_level_transcript("sess-small", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    section = build_section(stats)
    report = _base_report([section])

    th = CompactionSimThresholds(switch_usd=1_000_000.0)
    recs = RULES[0](report, th, None)
    assert recs == []


def test_rule_evidence_resolves_against_the_report():
    turns = _synthetic_20_turn_transcript()
    tr = _top_level_transcript("sess-synthetic", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    section = build_section(stats)
    report = _base_report([section])

    recs = RULES[0](report, _SMALL_FIXTURE_TH, None)
    assert recs, "expected the rule to fire on this fixture"
    for rec in recs:
        for label, value, source_table, row_key in rec.evidence:
            section_key, table_name = source_table.split(".", 1)
            found_section = next((s for s in report.sections if s.key == section_key), None)
            assert found_section is not None, f"no section {section_key!r} for evidence {label!r}"
            table = next((t for t in found_section.tables if t.name == table_name), None)
            assert table is not None, f"no table {table_name!r} for evidence {label!r}"
            row = next((r for r in table.rows if r and r[0] == row_key), None)
            assert row is not None, f"no row {row_key!r} in {source_table} for evidence {label!r}"


def test_rule_recommends_a_range_floor_not_a_single_best_window():
    """"compaction-window" now names a floor ("at least W"), not one
    "best" point -- and the action text says so explicitly."""
    turns = _synthetic_20_turn_transcript()
    tr = _top_level_transcript("sess-synthetic", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    section = build_section(stats)
    report = _base_report([section])

    recs = RULES[0](report, _SMALL_FIXTURE_TH, None)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.title == "Set autoCompactWindow to at least 150,000"
    assert "at least 150,000" in rec.action
    assert "modelled, not observed" in rec.action


def test_rule_gates_out_a_candidate_with_more_than_two_compactions_per_session():
    """A window whose modelled compactions/session exceeds 2 must never
    be the recommended floor, even if its raw saving alone would have
    cleared both switch thresholds -- built directly against the
    ``compaction_sim_by_window`` table rather than a real sweep, since
    driving the synthetic fixture's own ctx growth past 2 compactions
    at 100,000 would also change its saving arithmetic."""
    by_window = model.Table(
        name="compaction_sim_by_window",
        title="t",
        columns=[
            model.Column(key="window", label="w", kind="str"),
            model.Column(key="compactions_per_session", label="c", kind="float"),
            model.Column(key="mean_ctx", label="m", kind="tokens"),
            model.Column(key="cost", label="cost", kind="money"),
            model.Column(key="delta_usd", label="d", kind="money"),
            model.Column(key="delta_pct", label="dp", kind="pct"),
        ],
        rows=[
            ["100,000", 3.0, 50_000, 0.20, -5.0, -71.0],
            ["150,000", 1.0, 80_000, 3.50, -3.5, -50.0],
            ["none", 0.0, 200_000, 7.0, 0.0, 0.0],
        ],
    )
    section = model.Section(key="compaction_sim", title="t", tables=[by_window])
    report = _base_report([section])

    recs = RULES[0](report, CompactionSimThresholds(), None)
    assert len(recs) == 1
    assert recs[0].title == "Set autoCompactWindow to at least 150,000"


def test_rule_conservative_correction_suppresses_a_100k_recommendation_backed_only_by_a_tiny_allowance():
    """The trigger case for this rule's conservative rewrite: a corpus
    with no real ``compact_boundary`` event anywhere (so the sweep's own
    rediscovery allowance falls back to ``thresholds.default_rediscovery_allowance_usd``
    exactly, per ``_corpus_rediscovery_allowance``) whose configured
    default allowance is small enough that window=100,000's raw modelled
    saving alone would clear both switch thresholds, but doubling that
    same tiny allowance (the fallback correction, since this report
    carries no ``agents``/``topology_redundant_reads`` table) pushes the
    saving below ``switch_usd`` -- so 100,000 must not be recommended.

    By hand, on ``_synthetic_20_turn_transcript`` (window=100,000: one
    compaction at turn 6; ``observed_cost=1.76``, base cost with a
    zero allowance = 0.545 -- see
    ``test_small_window_hand_computed_compaction_count_and_cost``):
    with ``default_rediscovery_allowance_usd=0.15`` the sweep charges
    that allowance once (one compaction), so cost = 0.545 + 0.15 = 0.695
    and raw_saving = 1.76 - 0.695 = 1.065 (> switch_usd=1.00 -- would
    have fired under the old point-recommendation rule). The
    conservative correction then subtracts a further
    0.15 * 1 compaction/session = 0.15, giving an adjusted saving of
    1.065 - 0.15 = 0.915 (< switch_usd=1.00) -- below threshold.
    """
    th = CompactionSimThresholds(default_rediscovery_allowance_usd=0.15)
    turns = _synthetic_20_turn_transcript()
    tr = _top_level_transcript("sess-synthetic", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {}, th)
    section = build_section(stats, th)
    report = _base_report([section])

    recs = RULES[0](report, th, None)
    assert not any(rec.title == "Set autoCompactWindow to at least 100,000" for rec in recs)
