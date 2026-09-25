"""Tests for WP (v4-carry-cost): context carry cost per tool
(``src/claudeglass/carry.py``).

Most branches are exercised on hand-built ``model.Turn``/
``model.TranscriptResult`` instances (the ``_turn``/``_transcript``
helpers below, matching ``test_ttl.py``'s own convention) with
hand-computed costs at the packaged Sonnet 5 rates (input 2.0, output
10.0, cache_write_5m 2.5, cache_write_1h 4.0, cache_read 0.2 per million
tokens).
"""

from __future__ import annotations

import pytest

from claudeglass import model
from claudeglass.carry import (
    ASSUMPTIONS,
    RULES,
    CarryThresholds,
    build_section,
    compute_carry,
)
from claudeglass.model import EventKind, ReportModel, Section, TranscriptMeta, TranscriptResult
from claudeglass.pricing import load_pricing
from claudeglass.units import Units

from helpers import assert_privacy, elasticity_with_slope

PRICING = load_pricing()
SONNET_RATES = PRICING.resolve_model("claude-sonnet-5")


def _turn(**overrides) -> model.Turn:
    """Build a priced ``Turn`` with sane zero defaults, overridable per
    field. ``turn_index`` defaults to 1 (priced)."""
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
        tool_result_chars_by_tool={},
        preceding_event_kinds=(),
    )
    fields.update(overrides)
    return model.Turn(**fields)


def _transcript(turns: list[model.Turn], agent_type: str | None = None, session_id: str = "sess1") -> TranscriptResult:
    meta = TranscriptMeta(session_id=session_id, agent_type=agent_type, kind="top-level" if agent_type is None else "subagent")
    return TranscriptResult(meta=meta, turns=turns)


def _ten_turn_transcript_with_one_big_read(compaction_at: int | None = None) -> TranscriptResult:
    """10 priced turns; turn 2 receives a 40,000-char ("Read") result
    (10,000 tokens at chars/4). Turns 3-10 each observe
    cache_read_tokens=100, cache_creation_tokens=0 -- a pure cache hit,
    so each carrying turn's own per-token read rate is
    (100/1e6*0.2)/100 == 2e-7 $/token. ``compaction_at``, when given,
    marks that turn_index's ``preceding_event_kinds`` with
    COMPACT_BOUNDARY.
    """
    turns = []
    for i in range(1, 11):
        kwargs = dict(turn_index=i, message_id=f"msg_{i}")
        if i == 2:
            kwargs["tool_result_chars_by_tool"] = {"Read": 40_000}
        if i >= 3:
            kwargs["cache_read_tokens"] = 100
        if compaction_at is not None and i == compaction_at:
            kwargs["preceding_event_kinds"] = (EventKind.COMPACT_BOUNDARY,)
        turns.append(_turn(**kwargs))
    return _transcript(turns)


# -- turns-carried arithmetic -------------------------------------------------


def test_one_big_result_at_turn_2_of_10_carries_8_turns_no_compaction():
    transcript = _ten_turn_transcript_with_one_big_read()
    stats = compute_carry([transcript], SONNET_RATES)

    assert stats.total_results == 1
    read_row = next(r for r in stats.by_tool if r.key == "Read")
    assert read_row.result_count == 1
    assert read_row.tokens_entered == 10_000
    assert read_row.mean_turns_carried == 8
    assert read_row.carry_tokens == 10_000 * 8

    result = stats.top_results[0]
    assert result.tool == "Read"
    assert result.tokens == 10_000
    assert result.turns_carried == 8
    assert result.carry_tokens == 80_000
    # Each of the 8 carrying turns: cache_volume=100, all read, per-token
    # read rate = (100/1e6*0.2)/100 = 2e-7 $/token.
    expected_cost = 8 * (10_000 * 2e-7)
    assert result.carry_cost_usd == pytest.approx(expected_cost)


def test_compaction_at_turn_6_ends_carry_at_turn_5():
    transcript = _ten_turn_transcript_with_one_big_read(compaction_at=6)
    stats = compute_carry([transcript], SONNET_RATES)

    result = stats.top_results[0]
    assert result.turns_carried == 3  # turns 3, 4, 5 only
    assert result.carry_tokens == 10_000 * 3
    expected_cost = 3 * (10_000 * 2e-7)
    assert result.carry_cost_usd == pytest.approx(expected_cost)


def test_result_at_the_final_priced_turn_carries_zero_turns():
    turns = [_turn(turn_index=1), _turn(turn_index=2, tool_result_chars_by_tool={"Bash": 400})]
    stats = compute_carry([_transcript(turns)], SONNET_RATES)
    assert stats.total_results == 1
    assert stats.top_results[0].turns_carried == 0
    assert stats.top_results[0].carry_tokens == 0
    assert stats.top_results[0].carry_cost_usd == 0.0


def test_zero_char_tool_result_produces_no_carried_result():
    turns = [
        _turn(turn_index=1, tool_result_chars_by_tool={"Grep": 0}),
        _turn(turn_index=2, cache_read_tokens=10),
    ]
    stats = compute_carry([_transcript(turns)], SONNET_RATES)
    assert stats.total_results == 0
    assert stats.by_tool == []


def test_zero_cache_volume_turn_contributes_no_cost_but_still_counts_as_carried():
    """A carrying turn with no observed cache activity at all (e.g. an
    anomalous turn) adds nothing to cost, but the turn still counts
    towards turns_carried -- carry is a temporal count, not conditioned
    on that turn's own cost being nonzero."""
    turns = [
        _turn(turn_index=1, tool_result_chars_by_tool={"Read": 4_000}),  # 1000 tokens
        _turn(turn_index=2),  # no cache activity at all
        _turn(turn_index=3, cache_read_tokens=50),
    ]
    stats = compute_carry([_transcript(turns)], SONNET_RATES)
    result = stats.top_results[0]
    assert result.turns_carried == 2
    # Only turn 3 contributes: (50/1e6*0.2)/50 = 2e-7 $/token * 1000 tokens.
    assert result.carry_cost_usd == pytest.approx(1000 * 2e-7)


def test_mixed_read_and_write_turn_splits_proportionally():
    """A later turn with both cache_read_tokens and cache_creation_tokens
    (a partial re-cache) splits the carried result's tokens between the
    read and write rate in proportion to that turn's own observed mix."""
    turns = [
        _turn(turn_index=1, tool_result_chars_by_tool={"Read": 4_000}),  # 1000 tokens
        _turn(turn_index=2, cache_read_tokens=300, cache_creation_tokens=700, cc_5m=700),
    ]
    stats = compute_carry([_transcript(turns)], SONNET_RATES)
    result = stats.top_results[0]
    assert result.turns_carried == 1
    # cache_volume=1000: read_share=0.3, write_share=0.7.
    # read_rate = (300/1e6*0.2)/300 = 2e-7; write_rate = (700/1e6*2.5)/700 = 2.5e-6.
    expected = 1000 * (0.3 * 2e-7 + 0.7 * 2.5e-6)
    assert result.carry_cost_usd == pytest.approx(expected)


def test_multiple_tools_in_the_same_turn_are_separate_results():
    turns = [
        _turn(turn_index=1, tool_result_chars_by_tool={"Read": 4_000, "Bash": 8_000}),
        _turn(turn_index=2, cache_read_tokens=100),
    ]
    stats = compute_carry([_transcript(turns)], SONNET_RATES)
    assert stats.total_results == 2
    tools = sorted(r.tool for r in stats.top_results)
    assert tools == ["Bash", "Read"]


# -- agent-type aggregation ----------------------------------------------


def test_aggregates_by_agent_type_and_top_level_default():
    top = _transcript(
        [
            _turn(turn_index=1, tool_result_chars_by_tool={"Read": 4_000}),
            _turn(turn_index=2, cache_read_tokens=100),
        ],
        agent_type=None,
    )
    sub = _transcript(
        [
            _turn(turn_index=1, tool_result_chars_by_tool={"Read": 8_000}),
            _turn(turn_index=2, cache_read_tokens=100),
        ],
        agent_type="claude-implementer",
        session_id="sess2",
    )
    stats = compute_carry([top, sub], SONNET_RATES)
    keys = {r.key for r in stats.by_agent_type}
    assert keys == {"top-level", "claude-implementer"}


# -- truncation savings ----------------------------------------------------


def test_truncation_saving_is_exact_for_a_single_result():
    turns = [
        _turn(turn_index=1, tool_result_chars_by_tool={"Read": 40_000}),  # 10,000 tokens
        _turn(turn_index=2, cache_read_tokens=100),
        _turn(turn_index=3, cache_read_tokens=100),
    ]
    stats = compute_carry([_transcript(turns)], SONNET_RATES, CarryThresholds(truncation_tokens=(8_000,)))
    result = stats.top_results[0]
    assert result.tokens == 10_000
    assert result.turns_carried == 2

    saving = stats.truncation_savings[0]
    assert saving.truncate_to_tokens == 8_000
    assert saving.results_affected == 1
    assert saving.tokens_saved == (10_000 - 8_000) * 2
    expected_usd = result.carry_cost_usd * (1.0 - 8_000 / 10_000)
    assert saving.usd_saved == pytest.approx(expected_usd)


def test_truncation_saving_ignores_results_at_or_below_the_cap():
    turns = [
        _turn(turn_index=1, tool_result_chars_by_tool={"Read": 4_000}),  # 1000 tokens
        _turn(turn_index=2, cache_read_tokens=100),
    ]
    stats = compute_carry([_transcript(turns)], SONNET_RATES, CarryThresholds(truncation_tokens=(2_000,)))
    saving = stats.truncation_savings[0]
    assert saving.results_affected == 0
    assert saving.tokens_saved == 0
    assert saving.usd_saved == 0.0


def test_output_cap_savings_price_each_setting_on_the_results_it_caps():
    """BASH_MAX_OUTPUT_LENGTH at 15,000 characters (3,750 tokens) cuts
    only shell output over that size; a large Read is never counted."""
    turns = [
        _turn(turn_index=1, tool_result_chars_by_tool={"Bash": 20_000, "Read": 80_000}),  # 5,000 and 20,000 tokens
        _turn(turn_index=2, tool_result_chars_by_tool={"PowerShell": 8_000}),  # 2,000 tokens: under the cap
        _turn(turn_index=3, cache_read_tokens=100),
        _turn(turn_index=4, cache_read_tokens=100),
    ]
    stats = compute_carry([_transcript(turns)], SONNET_RATES)
    shell, mcp = stats.cap_savings
    bash = next(r for r in stats.top_results if r.tool == "Bash")
    assert (shell.setting, shell.value, shell.cap_tokens) == ("BASH_MAX_OUTPUT_LENGTH", "15000", 3_750)
    assert (shell.results, shell.results_affected) == (2, 1)
    assert shell.tokens_saved == (5_000 - 3_750) * bash.turns_carried
    assert shell.usd_saved == pytest.approx(bash.carry_cost_usd * (1 - 3_750 / 5_000))
    assert (mcp.setting, mcp.results, mcp.usd_saved) == ("MAX_MCP_OUTPUT_TOKENS", 0, 0.0)

    section = build_section(stats)
    table = next(t for t in section.tables if t.name == "carry_output_cap_savings")
    assert [row[0] for row in table.rows] == ["BASH_MAX_OUTPUT_LENGTH", "MAX_MCP_OUTPUT_TOKENS"]


# -- share of cache volume -------------------------------------------------


def test_share_of_cache_volume_pct_uses_corpus_wide_denominator():
    turns = [
        _turn(turn_index=1, tool_result_chars_by_tool={"Read": 4_000}),  # 1000 tokens
        _turn(turn_index=2, cache_read_tokens=1_000),
    ]
    stats = compute_carry([_transcript(turns)], SONNET_RATES)
    # total_cache_volume_tokens = turn2's cache_read_tokens (1000) -- the
    # only priced-turn cache activity in this corpus.
    assert stats.total_cache_volume_tokens == 1_000
    row = stats.by_tool[0]
    # carry_tokens = 1000 tokens carried for 1 turn = 1000.
    assert row.carry_tokens == 1_000
    assert row.share_of_cache_volume_pct == pytest.approx(100.0)


# -- multi-model corpus ------------------------------------------------------


def test_per_turn_rate_lookup_prices_each_turn_at_its_own_model():
    haiku_rates = PRICING.resolve_model("claude-haiku-4-5-20251001")

    def lookup(model_id: str):
        return haiku_rates if model_id == "claude-haiku-4-5-20251001" else SONNET_RATES

    turns = [
        _turn(turn_index=1, model="claude-sonnet-5", tool_result_chars_by_tool={"Read": 4_000}),
        _turn(turn_index=2, model="claude-haiku-4-5-20251001", cache_read_tokens=100),
    ]
    stats = compute_carry([_transcript(turns)], lookup)
    result = stats.top_results[0]
    expected_rate = (100 / 1e6 * haiku_rates.rates.cache_read) / 100
    assert result.carry_cost_usd == pytest.approx(1000 * expected_rate)


def test_unresolved_model_prices_carry_contribution_at_zero_and_is_counted():
    """A bare single rate (``SONNET_RATES``) is applied to every turn
    regardless of its own ``model`` (the pre-existing "single rate
    ignores the turn's model" convention -- see ``_as_lookup``'s
    docstring, mirroring ``ttl._as_lookup``), so this test uses a real
    per-model lookup (``PRICING.resolve_model``) to actually exercise an
    unresolvable model."""
    turns = [
        _turn(turn_index=1, tool_result_chars_by_tool={"Read": 4_000}),
        _turn(turn_index=2, model="totally-unknown-model", cache_read_tokens=100),
    ]
    stats = compute_carry([_transcript(turns)], PRICING.resolve_model)
    assert stats.top_results[0].carry_cost_usd == 0.0
    assert stats.unpriced_turns == 1


# -- P10a perf rewrite: cross-check against an independent, naive O(k*T) reference --


def _naive_carried_costs(turns: list[model.Turn], lookup) -> dict[tuple[int, str], float]:
    """An independent, deliberately un-optimized re-derivation of
    carry.py's documented model (module docstring's "The model,
    precisely" section) -- (turn.turn_index, tool) -> carry_cost_usd,
    computed by walking every later turn one at a time for every result
    (the same shape ``_extract_results`` used before P10a's prefix-sum/
    bisect rewrite). Used to cross-check :func:`compute_carry`'s output
    is unchanged (equal within 1e-12) after that rewrite, independently
    of carry.py's own (now optimized) internals."""
    from claudeglass.pricing import price_turn

    priced = [t for t in turns if t.turn_index > 0]
    by_index = {t.turn_index: t for t in priced}
    boundary_indices = sorted(t.turn_index for t in priced if EventKind.COMPACT_BOUNDARY in t.preceding_event_kinds)
    last_index = priced[-1].turn_index

    out: dict[tuple[int, str], float] = {}
    for turn in priced:
        if not turn.tool_result_chars_by_tool:
            continue
        end_index = last_index
        for boundary in boundary_indices:
            if boundary > turn.turn_index:
                end_index = boundary - 1
                break
        for tool_name, chars in turn.tool_result_chars_by_tool.items():
            if chars <= 0:
                continue
            tokens = round(chars / 4)
            if tokens <= 0:
                continue
            cost = 0.0
            for idx in range(turn.turn_index + 1, end_index + 1):
                later = by_index.get(idx)
                if later is None:
                    continue
                cache_volume = later.cache_read_tokens + later.cache_creation_tokens
                if cache_volume <= 0:
                    continue
                rates = lookup(later.model)
                breakdown = price_turn(later, rates)
                read_rate = breakdown.cache_read_cost / later.cache_read_tokens if later.cache_read_tokens > 0 else 0.0
                write_rate = (
                    breakdown.cache_write_cost / later.cache_creation_tokens
                    if later.cache_creation_tokens > 0
                    else 0.0
                )
                read_share = later.cache_read_tokens / cache_volume
                write_share = later.cache_creation_tokens / cache_volume
                cost += tokens * (read_share * read_rate + write_share * write_rate)
            out[(turn.turn_index, tool_name)] = cost
    return out


def test_extract_results_matches_naive_reference_with_gaps_boundary_and_mixed_models():
    """Stress case for the prefix-sum/bisect rewrite: turn_index skips
    integers (5 and 8 are missing -- an unpriced/absent turn in the
    middle of a carry range), two models alternate, several turns carry
    no cache volume at all, and a compaction boundary cuts some carry
    ranges short. Every (entry turn, tool)'s carry_cost_usd must equal
    the naive per-later-turn reference within 1e-12."""
    haiku_rates = PRICING.resolve_model("claude-haiku-4-5-20251001")

    def lookup(model_id: str):
        return haiku_rates if model_id == "claude-haiku-4-5-20251001" else SONNET_RATES

    turn_indices = [1, 2, 3, 4, 6, 7, 9, 10, 11, 12, 13, 14]
    turns = []
    for n, idx in enumerate(turn_indices):
        kwargs = dict(turn_index=idx, message_id=f"msg_{idx}", model="claude-haiku-4-5-20251001" if idx % 2 else "claude-sonnet-5")
        if idx == 1:
            kwargs["tool_result_chars_by_tool"] = {"Read": 4_000}
        elif idx == 3:
            kwargs["tool_result_chars_by_tool"] = {"Bash": 8_000, "Grep": 2_000}
        elif idx == 7:
            kwargs["tool_result_chars_by_tool"] = {"Read": 12_000}
        # A mix of pure-read, pure-write, mixed, and zero-cache-volume
        # later turns (n cycles through 4 cases).
        case = n % 4
        if case == 1:
            kwargs["cache_read_tokens"] = 100 * idx
        elif case == 2:
            kwargs["cache_creation_tokens"] = 50 * idx
            kwargs["cc_5m"] = 50 * idx
        elif case == 3:
            kwargs["cache_read_tokens"] = 40 * idx
            kwargs["cache_creation_tokens"] = 20 * idx
            kwargs["cc_1h"] = 20 * idx
        if idx == 10:
            kwargs["preceding_event_kinds"] = (EventKind.COMPACT_BOUNDARY,)
        turns.append(_turn(**kwargs))

    stats = compute_carry([_transcript(turns)], lookup, CarryThresholds(top_n=100))
    expected = _naive_carried_costs(turns, lookup)
    assert len(stats.top_results) == len(expected)
    for result in stats.top_results:
        want = expected[(result.entry_turn_index, result.tool)]
        assert result.carry_cost_usd == pytest.approx(want, abs=1e-12, rel=1e-12)


# -- privacy ---------------------------------------------------------------


def test_build_section_privacy():
    transcript = _ten_turn_transcript_with_one_big_read()
    stats = compute_carry([transcript], SONNET_RATES)
    section = build_section(stats)
    assert_privacy(section)


def test_carried_result_privacy():
    transcript = _ten_turn_transcript_with_one_big_read()
    stats = compute_carry([transcript], SONNET_RATES)
    assert_privacy(stats.top_results)


def test_build_section_has_expected_tables():
    transcript = _ten_turn_transcript_with_one_big_read()
    stats = compute_carry([transcript], SONNET_RATES)
    section = build_section(stats)
    assert section.key == "carry"
    names = [t.name for t in section.tables]
    assert names == [
        "carry_by_tool",
        "carry_by_agent_type",
        "carry_top_results",
        "carry_truncation_savings",
        "carry_output_cap_savings",
    ]
    # Assumptions text is carried verbatim into the section's own notes.
    for line in ASSUMPTIONS:
        assert line in section.notes


# -- RULES: tool-output-carry recommendation -------------------------------


def _report_with_carry_section(stats, thresholds: CarryThresholds | None = None, units=None) -> ReportModel:
    section = build_section(stats, thresholds)
    report = ReportModel(sections=[section])
    report.units = units
    return report


def _heavy_read_corpus(n_transcripts: int = 6) -> list[TranscriptResult]:
    """A corpus of transcripts each carrying one large Read result for
    many turns, so Read's carry-cost share of cache volume is
    overwhelming and result_count clears any reasonable min-sample."""
    transcripts = []
    for s in range(n_transcripts):
        turns = []
        for i in range(1, 9):
            kwargs = dict(turn_index=i, message_id=f"msg_{s}_{i}")
            if i == 1:
                kwargs["tool_result_chars_by_tool"] = {"Read": 80_000}
            else:
                kwargs["cache_read_tokens"] = 200
            turns.append(_turn(**kwargs))
        transcripts.append(_transcript(turns, session_id=f"sess_{s}"))
    return transcripts


def test_tool_output_carry_rule_fires_when_share_and_sample_clear_thresholds():
    stats = compute_carry(_heavy_read_corpus(), SONNET_RATES)
    th = CarryThresholds(carry_share_pct=10.0, min_sample_results=3)
    report = _report_with_carry_section(stats, th)

    recs = RULES[0](report, th)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == "tool-output-carry"
    assert "Read" in rec.title
    assert rec.lever is None
    assert rec.category == "workflow"


def test_tool_output_carry_rule_cites_that_tools_own_saving():
    """A big Bash result alongside the Reads doesn't count towards the
    saving the rule names for Read."""
    corpus = _heavy_read_corpus()
    corpus[0].turns[0].tool_result_chars_by_tool["Bash"] = 400_000
    stats = compute_carry(corpus, SONNET_RATES)
    th = CarryThresholds(carry_share_pct=10.0, min_sample_results=3)
    [rec] = [r for r in RULES[0](_report_with_carry_section(stats, th), th) if "Read" in r.title]
    read = next(row for row in stats.by_tool if row.key == "Read")
    assert read.saving_if_capped_usd == pytest.approx(
        sum(r.carry_cost_usd * (1 - 8_000 / r.tokens) for r in stats.top_results if r.tool == "Read")
    )
    assert f"would have saved about ${read.saving_if_capped_usd:.2f}" in rec.action


def test_tool_output_carry_rule_action_has_no_bare_dollar_under_a_subscription():
    """UX-2 / finding F1-F2: a subscription's Recommendation.action must
    route through Units, never a raw f"${...:.2f}"."""
    corpus = _heavy_read_corpus()
    corpus[0].turns[0].tool_result_chars_by_tool["Bash"] = 400_000
    stats = compute_carry(corpus, SONNET_RATES)
    th = CarryThresholds(carry_share_pct=10.0, min_sample_results=3)
    units = Units(billing_mode="subscription", currency="USD", elasticity=elasticity_with_slope())
    report = _report_with_carry_section(stats, th, units=units)
    [rec] = [r for r in RULES[0](report, th) if "Read" in r.title]
    assert "$" not in rec.action
    assert "about about" not in rec.action.lower()
    assert "of your weekly usage limit" in rec.action


def test_tool_output_carry_rule_does_not_fire_below_share_threshold():
    stats = compute_carry(_heavy_read_corpus(), SONNET_RATES)
    # share_of_cache_volume_pct is an attribution share, not a partition
    # (carry_tokens double-counts by construction -- see the module
    # docstring), so it can and does exceed 100% here; use a threshold
    # comfortably above the computed share to prove the "does not fire"
    # branch rather than the "fires" one.
    th = CarryThresholds(carry_share_pct=1_000_000.0, min_sample_results=3)
    report = _report_with_carry_section(stats, th)
    assert RULES[0](report, th) == []


def test_tool_output_carry_rule_does_not_fire_below_min_sample():
    stats = compute_carry(_heavy_read_corpus(n_transcripts=1), SONNET_RATES)
    th = CarryThresholds(carry_share_pct=1.0, min_sample_results=50)
    report = _report_with_carry_section(stats, th)
    assert RULES[0](report, th) == []


def test_tool_output_carry_rule_returns_empty_without_carry_section():
    report = ReportModel(sections=[])
    assert RULES[0](report, CarryThresholds()) == []


def test_tool_output_carry_rule_evidence_resolves_against_the_report():
    """Mirrors test_recommend.py's own evidence-exists walk: every
    (label, value, source_table, row_key) tuple must resolve to a real
    cell in the report's own sections."""
    stats = compute_carry(_heavy_read_corpus(), SONNET_RATES)
    th = CarryThresholds(carry_share_pct=10.0, min_sample_results=3)
    report = _report_with_carry_section(stats, th)

    recs = RULES[0](report, th)
    assert recs, "expected the rule to fire for this heavy-Read corpus"
    for rec in recs:
        for label, value, source_table, row_key in rec.evidence:
            section_key, table_name = source_table.split(".", 1)
            section = next((s for s in report.sections if s.key == section_key), None)
            assert section is not None, f"{rec.id}: no section {section_key!r} for evidence {label!r}"
            table = next((t for t in section.tables if t.name == table_name), None)
            assert table is not None, f"{rec.id}: no table {table_name!r} for evidence {label!r}"
            row = next((r for r in table.rows if r and r[0] == row_key), None)
            assert row is not None, f"{rec.id}: no row {row_key!r} in {source_table} for evidence {label!r}"
