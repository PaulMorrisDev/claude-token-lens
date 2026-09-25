"""Tests for v4-model-swap: the model-swap counterfactual
(``src/claude_token_lens/model_swap.py``).

Most branches are exercised on hand-built ``model.Turn``/
``model.TranscriptResult`` instances (the ``_turn``/``_transcript``
helpers below, matching ``test_ttl.py``'s convention) with hand-computed
costs at the packaged rate card (``load_pricing()`` -- the real
``pricing.toml``, never a fake one, so a rate-card edit that changes
these numbers is caught here too). One test goes through the real
parser (``parse_transcript`` + ``tests/helpers.py``'s JSONL builders) to
exercise ``helpers.assert_privacy`` on a genuine ``TranscriptResult``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_token_lens import model, model_swap
from claude_token_lens.model import (
    Diagnostics,
    PricingMeta,
    ReportMeta,
    ReportModel,
    Section,
    TranscriptMeta,
    TranscriptResult,
)
from claude_token_lens.parse import parse_transcript
from claude_token_lens.pricing import load_pricing
from claude_token_lens.render.tables import format_cell
from claude_token_lens.units import Units

from helpers import assert_privacy, elasticity_with_slope, turn_line, write_jsonl

PRICING = load_pricing()

SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5-20251001"
OPUS = "claude-opus-5"
FABLE = "claude-fable-5-1"


def _turn(**overrides) -> model.Turn:
    """Build a priced ``Turn`` with sane zero defaults, overridable per
    field -- same convention as ``test_ttl.py``'s own ``_turn`` helper."""
    fields = dict(
        message_id="msg_1",
        request_id="req_1",
        turn_index=1,
        ts="2026-09-18T12:00:00.000Z",
        model=SONNET,
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


def _transcript(
    turns: list[model.Turn],
    kind: str = "top-level",
    agent_type: str | None = None,
    agent_model_alias: str | None = None,
) -> TranscriptResult:
    meta = TranscriptMeta(
        path="synthetic.jsonl",
        kind=kind,
        session_id="sess1",
        agent_type=agent_type,
        agent_model_alias=agent_model_alias,
    )
    return TranscriptResult(meta=meta, turns=turns)


def _cost(model_id: str, **fields) -> float:
    """Hand-computed cost of one turn's ``fields`` at ``model_id``'s
    packaged rates, using the exact same formula as
    ``pricing.price_turn`` (input/output/cache_write_5m/cache_write_1h/
    cache_read, each per-million-token) -- an independent restatement,
    not a call into the code under test."""
    rates = PRICING.models[model_id]
    input_tokens = fields.get("input_tokens", 0)
    output_tokens = fields.get("output_tokens", 0)
    cc_5m = fields.get("cc_5m", 0)
    cc_1h = fields.get("cc_1h", 0)
    cache_read_tokens = fields.get("cache_read_tokens", 0)
    return (
        input_tokens / 1_000_000 * rates.input
        + output_tokens / 1_000_000 * rates.output
        + cc_5m / 1_000_000 * rates.cache_write_5m
        + cc_1h / 1_000_000 * rates.cache_write_1h
        + cache_read_tokens / 1_000_000 * rates.cache_read
    )


# -- repricing arithmetic ------------------------------------------------------


def test_reprice_arithmetic_matches_hand_computation_at_two_models():
    fields = dict(input_tokens=100_000, output_tokens=50_000, cc_5m=20_000, cache_read_tokens=10_000)
    tr = _transcript([_turn(model=SONNET, **fields)])

    stats = model_swap.compute_model_swap([tr], PRICING)
    row = stats.by_key["top-level"]

    expected_observed = _cost(SONNET, **fields)
    expected_haiku = _cost(HAIKU, **fields)
    expected_sonnet_alt = _cost(SONNET, **fields)

    assert row.observed_cost == pytest.approx(expected_observed)
    assert row.cost_by_model[HAIKU] == pytest.approx(expected_haiku)
    assert row.cost_by_model[SONNET] == pytest.approx(expected_sonnet_alt)
    # Repricing at the turn's own observed model must equal the observed
    # cost exactly -- both paths call price_turn with the same rates and
    # the same (unmodified) token volumes/write split.
    assert row.cost_by_model[SONNET] == pytest.approx(row.observed_cost)


def test_reprice_holds_token_volumes_and_write_split_constant():
    """A mixed 5m/1h write, repriced at another model, must scale each
    bucket by that model's own 5m/1h rate independently -- never a flat
    blend -- proving the observed write split (not just the totals) is
    preserved across the counterfactual."""
    fields = dict(input_tokens=0, output_tokens=0, cc_5m=40_000, cc_1h=10_000, cache_read_tokens=0)
    tr = _transcript([_turn(model=SONNET, **fields)])

    stats = model_swap.compute_model_swap([tr], PRICING)
    row = stats.by_key["top-level"]

    expected_fable = _cost(FABLE, **fields)
    assert row.cost_by_model[FABLE] == pytest.approx(expected_fable)
    # Sanity: the two write buckets really do carry different rates at
    # Fable (12.5 vs 20.0), so this test would fail if the code under
    # test collapsed cc_5m/cc_1h into one flat bucket.
    fable_rates = PRICING.models[FABLE]
    assert fable_rates.cache_write_5m != fable_rates.cache_write_1h


# -- alias handling -------------------------------------------------------------


def test_alias_handling_bare_alias_resolves_same_as_full_id():
    fields = dict(input_tokens=200_000, output_tokens=100_000)
    tr_alias = _transcript(
        [_turn(model="sonnet", **fields)],
        kind="subagent",
        agent_type="claude-implementer",
        agent_model_alias="sonnet",
    )
    tr_full = _transcript(
        [_turn(model=SONNET, **fields)],
        kind="subagent",
        agent_type="claude-implementer",
        agent_model_alias="sonnet",
    )

    stats_alias = model_swap.compute_model_swap([tr_alias], PRICING)
    stats_full = model_swap.compute_model_swap([tr_full], PRICING)

    row_alias = stats_alias.by_key["claude-implementer"]
    row_full = stats_full.by_key["claude-implementer"]
    # The bare alias "sonnet" resolves (via Pricing.resolve_model) to the
    # exact same rate as the full canonical id -- observed cost agrees.
    assert row_alias.observed_cost == pytest.approx(row_full.observed_cost)
    # The tier verdict (which needs a family match) also agrees, since
    # workstyle.model_tier substring-matches "sonnet" either way.
    assert row_alias.tier_verdict.state == row_full.tier_verdict.state == "cheaper_available"
    assert row_alias.tier_verdict.alt_model == row_full.tier_verdict.alt_model == HAIKU


def test_alias_handling_turn_model_takes_priority_over_meta_alias():
    """Turn.model carries the ground truth actually billed; when it
    disagrees with TranscriptMeta.agent_model_alias, the turn's own
    model wins for the tier lookup (workstyle.model_tier's own documented
    precedence)."""
    tr = _transcript(
        [_turn(model=OPUS, input_tokens=1_000_000, output_tokens=1_000_000)],
        kind="subagent",
        agent_type="claude-implementer",
        agent_model_alias="sonnet",
    )
    stats = model_swap.compute_model_swap([tr], PRICING)
    row = stats.by_key["claude-implementer"]

    # Opus (rank 2) beats what the alias alone ("sonnet", rank 1) would
    # have suggested -- one tier down from Opus is Sonnet, not Haiku.
    assert row.tier_verdict.alt_model == SONNET


# -- unknown model ---------------------------------------------------------------


def test_unknown_model_excluded_from_observed_cost_with_a_note():
    tr = _transcript([_turn(model="not-a-real-claude-model-id", input_tokens=100_000, output_tokens=50_000)])
    stats = model_swap.compute_model_swap([tr], PRICING)
    row = stats.by_key["top-level"]

    assert row.unpriced_turns == 1
    assert row.observed_cost == 0.0
    # Every alternative is still repriced -- the counterfactual doesn't
    # need the observed model to resolve, only the turn's own tokens.
    assert row.cost_by_model[SONNET] > 0.0

    section = model_swap.build_section(stats)
    joined_notes = " ".join(section.notes)
    assert "unpriced" in joined_notes.lower() or "unknown model" in joined_notes.lower()


# -- already-cheapest state -------------------------------------------------------


def test_already_cheapest_state_reports_no_saving():
    tr = _transcript(
        [_turn(model=HAIKU, input_tokens=100_000, output_tokens=50_000)],
        kind="subagent",
        agent_type="general-purpose",
    )
    stats = model_swap.compute_model_swap([tr], PRICING)
    row = stats.by_key["general-purpose"]

    assert row.tier_verdict.state == "already_cheapest"
    assert row.tier_verdict.alt_model is None
    assert row.tier_verdict.saving_usd == 0.0
    assert row.tier_verdict.saving_pct == 0.0
    assert "already on the cheapest model" in row.tier_verdict.label

    section = model_swap.build_section(stats)
    table = next(t for t in section.tables if t.name == "model_swap_by_agent_type")
    alt_model_idx = [c.key for c in table.columns].index("best_cheaper_alternative_model")
    saving_idx = [c.key for c in table.columns].index("saving_usd")
    row_cells = next(r for r in table.rows if r[0] == "general-purpose")
    assert row_cells[alt_model_idx] is None
    assert row_cells[saving_idx] == 0.0


def test_a_sonnet_main_session_is_never_offered_haiku():
    """The main session does the hard, open-ended work, so Sonnet is its
    floor: no alternative, no saving, and so no card, lever or goal."""
    tr = _transcript([_turn(model=SONNET, input_tokens=1_000_000, output_tokens=500_000)], kind="top-level")
    stats = model_swap.compute_model_swap([tr], PRICING)
    row = stats.by_key["top-level"]

    assert row.tier_verdict.state == "main_floor"
    assert row.tier_verdict.alt_model is None
    assert row.tier_verdict.saving_usd == 0.0
    assert "smallest model suggested for your main session" in row.tier_verdict.label
    # What Haiku would have cost is still shown, as information only.
    assert row.cost_by_model[HAIKU] > 0

    report = ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)),
        sections=[model_swap.build_section(stats)],
        diagnostics=Diagnostics(lines=1000),
    )
    th = model_swap.ModelSwapThresholds(min_sessions=1, min_turns=1)
    assert model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None) == []


def test_a_sonnet_subagent_is_still_offered_haiku():
    tr = _transcript(
        [_turn(model=SONNET, input_tokens=1_000_000, output_tokens=500_000)],
        kind="subagent",
        agent_type="reviewer",
    )
    stats = model_swap.compute_model_swap([tr], PRICING)
    assert stats.by_key["reviewer"].tier_verdict.state == "cheaper_available"
    assert stats.by_key["reviewer"].tier_verdict.alt_model == HAIKU


def test_no_priced_turns_reports_no_data_not_a_false_already_cheapest():
    tr = _transcript([], kind="subagent", agent_type="claude-implementer")
    stats = model_swap.compute_model_swap([tr], PRICING)
    row = stats.by_key["claude-implementer"]
    assert row.tier_verdict.state == "no_data"
    assert row.tier_verdict.alt_model is None


# -- corpus-wide summary --------------------------------------------------------


def _agent(model_id: str, agent_type: str, tokens: int = 1_000_000) -> TranscriptResult:
    return _transcript(
        [_turn(model=model_id, input_tokens=tokens, output_tokens=tokens)],
        kind="subagent",
        agent_type=agent_type,
    )


def test_corpus_summary_only_counts_qualifying_fable_or_opus_subagent_rows():
    top = _transcript([_turn(model=FABLE, input_tokens=1_000_000, output_tokens=1_000_000)], kind="top-level")
    fable_agent = _agent(FABLE, "overseer")
    opus_agent = _agent(OPUS, "claude-implementer")
    sonnet_agent = _agent(SONNET, "Explore")
    haiku_agent = _agent(HAIKU, "statusline-setup")

    stats = model_swap.compute_model_swap(
        [top, fable_agent, opus_agent, sonnet_agent, haiku_agent], PRICING
    )
    section = model_swap.build_section(stats)
    summary = next(t for t in section.tables if t.name == "model_swap_summary")
    row = summary.rows[0]
    columns = [c.key for c in summary.columns]

    # Only the Fable (-> Opus) and Opus (-> Sonnet) subagent rows count;
    # top-level is excluded by scope, sonnet/haiku rows aren't Fable/Opus.
    assert row[columns.index("agent_types")] == 2

    expected_saving = (
        stats.by_key["overseer"].tier_verdict.saving_usd
        + stats.by_key["claude-implementer"].tier_verdict.saving_usd
    )
    expected_observed = stats.by_key["overseer"].observed_cost + stats.by_key["claude-implementer"].observed_cost
    assert row[columns.index("saving_usd")] == pytest.approx(expected_saving)
    assert row[columns.index("observed_cost_usd")] == pytest.approx(expected_observed)
    assert row[columns.index("cost_after_tier_down_usd")] == pytest.approx(expected_observed - expected_saving)


# -- RULES / recommendation ------------------------------------------------------


def _report_with_section(section: Section, units: Units | None = None) -> ReportModel:
    report = ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)),
        sections=[section],
        recommendations=[],
        diagnostics=Diagnostics(),
    )
    report.units = units
    return report


def _assert_evidence_resolves(report: ReportModel, rec) -> None:
    for label, value, source_table, row_key in rec.evidence:
        section_key, table_name = source_table.split(".", 1)
        section = next(s for s in report.sections if s.key == section_key)
        table = next(t for t in section.tables if t.name == table_name)
        row = next(r for r in table.rows if r[0] == row_key)
        # Find a column whose cell equals the cited value (mirrors
        # test_recommend_contract.py's own tolerant match: int/float and
        # str/number crossings are fine, the cited number must be real).
        assert any(cell == value for cell in row), (label, value, row)


def test_rule_fires_when_saving_exceeds_thresholds_and_evidence_resolves():
    tr = _agent(FABLE, "claude-implementer")
    stats = model_swap.compute_model_swap([tr], PRICING)
    th = model_swap.ModelSwapThresholds(saving_pct_min=10.0, saving_usd_min=1.0, min_sessions=1, min_turns=1)
    section = model_swap.build_section(stats, th)
    report = _report_with_section(section)

    recs = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == "model-tier"
    assert rec.agent_type == "claude-implementer"
    assert rec.lever == "model"
    assert rec.scope == "repo"
    assert ".claude/agents/claude-implementer.md" in rec.action
    assert OPUS in rec.action
    _assert_evidence_resolves(report, rec)


def test_rule_action_and_table_label_have_no_bare_dollar_under_a_subscription():
    """UX-2 / finding F1-F2: a subscription's ``model-tier`` action and
    the section table's "cheaper_available" label must route through
    Units, never a raw f"${...:.2f}"."""
    units = Units(billing_mode="subscription", currency="USD", elasticity=elasticity_with_slope())
    tr = _agent(FABLE, "claude-implementer")
    stats = model_swap.compute_model_swap([tr], PRICING)
    th = model_swap.ModelSwapThresholds(saving_pct_min=10.0, saving_usd_min=1.0, min_sessions=1, min_turns=1)
    section = model_swap.build_section(stats, th, units=units)
    report = _report_with_section(section, units=units)

    recs = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    assert len(recs) == 1
    rec = recs[0]
    assert "$" not in rec.action
    assert "about about" not in rec.action.lower()

    table = next(t for t in section.tables if t.name == "model_swap_by_agent_type")
    label_idx = [c.key for c in table.columns].index("best_cheaper_alternative")
    labels = " ".join(str(row[label_idx]) for row in table.rows if row[label_idx])
    assert "$" not in labels
    assert "about about" not in labels.lower()


def test_rule_uses_settings_json_and_user_scope_for_top_level():
    tr = _transcript([_turn(model=FABLE, input_tokens=1_000_000, output_tokens=1_000_000)], kind="top-level")
    stats = model_swap.compute_model_swap([tr], PRICING)
    th = model_swap.ModelSwapThresholds(saving_pct_min=10.0, saving_usd_min=1.0, min_sessions=1, min_turns=1)
    section = model_swap.build_section(stats, th)
    report = _report_with_section(section)

    recs = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.agent_type == "top-level"
    assert rec.scope == "user"
    assert "settings.json" in rec.action


def test_rule_does_not_fire_below_default_min_sample():
    tr = _agent(FABLE, "claude-implementer")
    stats = model_swap.compute_model_swap([tr], PRICING)
    th = model_swap.ModelSwapThresholds()  # default min_sessions=5, min_turns=200
    section = model_swap.build_section(stats, th)
    report = _report_with_section(section)

    recs = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    assert recs == []


def test_rule_does_not_fire_below_saving_floors():
    tr = _agent(FABLE, "claude-implementer")
    stats = model_swap.compute_model_swap([tr], PRICING)
    th = model_swap.ModelSwapThresholds(saving_pct_min=99.0, saving_usd_min=1_000_000.0, min_sessions=1, min_turns=1)
    section = model_swap.build_section(stats, th)
    report = _report_with_section(section)

    recs = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    assert recs == []


def test_rule_never_fires_for_already_cheapest_row():
    tr = _agent(HAIKU, "general-purpose")
    stats = model_swap.compute_model_swap([tr], PRICING)
    th = model_swap.ModelSwapThresholds(saving_pct_min=0.0, saving_usd_min=0.0, min_sessions=1, min_turns=1)
    section = model_swap.build_section(stats, th)
    report = _report_with_section(section)

    recs = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    assert recs == []


def test_rule_suppressed_for_chat_only_archetype_on_subagent_row_but_not_top_level():
    top = _transcript([_turn(model=FABLE, input_tokens=1_000_000, output_tokens=1_000_000)], kind="top-level")
    sub = _agent(FABLE, "claude-implementer")
    stats = model_swap.compute_model_swap([top, sub], PRICING)
    th = model_swap.ModelSwapThresholds(saving_pct_min=10.0, saving_usd_min=1.0, min_sessions=1, min_turns=1)
    section = model_swap.build_section(stats, th)
    report = _report_with_section(section)

    recs = model_swap.RULES["model-tier"](report, th, archetype="chat-only", snapshot=None)
    agent_types = {r.agent_type for r in recs}
    assert agent_types == {"top-level"}


# -- rendering sanity -------------------------------------------------------------


def test_every_cell_renders_without_error():
    tr = _agent(FABLE, "claude-implementer")
    stats = model_swap.compute_model_swap([tr], PRICING)
    section = model_swap.build_section(stats)
    for table in section.tables:
        for row in table.rows:
            for column, cell in zip(table.columns, row):
                format_cell(cell, column.kind)


# -- privacy ----------------------------------------------------------------------


def test_privacy(tmp_path: Path):
    top_lines = [
        turn_line(message_id="msg_1", model=SONNET, input_tokens=1000, output_tokens=500),
        turn_line(message_id="msg_2", model=SONNET, input_tokens=2000, output_tokens=800, cache_creation_input_tokens=500),
    ]
    top_path = tmp_path / "top.jsonl"
    write_jsonl(top_path, top_lines)
    top = parse_transcript(top_path, TranscriptMeta(path=str(top_path), kind="top-level", session_id="sess1"))

    sub_lines = [turn_line(message_id="msg_3", model=FABLE, input_tokens=5000, output_tokens=2000)]
    sub_path = tmp_path / "sub.jsonl"
    write_jsonl(sub_path, sub_lines)
    sub = parse_transcript(
        sub_path,
        TranscriptMeta(
            path=str(sub_path),
            kind="subagent",
            session_id="sess1",
            agent_type="claude-implementer",
            agent_model_alias="fable",
        ),
    )

    stats = model_swap.compute_model_swap([top, sub], PRICING)
    th = model_swap.ModelSwapThresholds(saving_pct_min=0.0, saving_usd_min=0.0, min_sessions=1, min_turns=1)
    section = model_swap.build_section(stats, th)
    assert_privacy(section)

    report = _report_with_section(section)
    recs = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    for rec in recs:
        assert_privacy(rec)



def test_reported_fit_is_cited_but_never_changes_the_saving():
    from claude_token_lens import habits

    tr = _agent(FABLE, "claude-implementer")
    stats = model_swap.compute_model_swap([tr], PRICING)
    th = model_swap.ModelSwapThresholds(saving_pct_min=10.0, saving_usd_min=1.0, min_sessions=1, min_turns=1)
    report = _report_with_section(model_swap.build_section(stats, th))
    (plain,) = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    runs = [habits.AgentFact(session_id="s", agent_type="claude-implementer", week="", cost=1.0, fit=fit, level=level)
            for fit, level in (("smaller", "easy"), ("smaller", "easy"), ("right", "normal"), (None, "easy"))]
    report.sections.append(habits.section_from(habits.Habits(agents=runs)))
    (rec,) = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    assert rec.evidence[: len(plain.evidence)] == plain.evidence
    assert rec.evidence[len(plain.evidence):] == [
        ("Work reported easy (%)", 75.0, "habits.habits_agents", "claude-implementer"),
        ("Runs that said a smaller model would do", 2, "habits.habits_agents", "claude-implementer"),
    ]
    _assert_evidence_resolves(report, rec)
