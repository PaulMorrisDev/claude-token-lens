"""Tests for ``advice.finish``: the plain-language pass over
``recommend()``'s rules (consolidation, wording, ordering)."""

from __future__ import annotations

from claude_token_lens import advice, fixes
from claude_token_lens.model import (
    Column,
    Diagnostics,
    PricingMeta,
    Recommendation,
    ReportMeta,
    ReportModel,
    Section,
    Table,
)
from claude_token_lens.snapshots import Snapshot
from claude_token_lens.units import Units


def _model_swap_report(rows) -> ReportModel:
    table = Table(
        name="model_swap_by_agent_type",
        columns=[
            Column(key="agent_type", label="Agent type"),
            Column(key="observed_model", label="Observed model"),
            Column(key="best_cheaper_alternative_model", label="Alternative"),
            Column(key="saving_usd", label="Saving", kind="money"),
        ],
        rows=rows,
    )
    return ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)),
        sections=[Section(key="model_swap", title="Model swap", tables=[table])],
        diagnostics=Diagnostics(lines=1000),
    )


def _tier(agent_type: str, scope: str = "user") -> Recommendation:
    return Recommendation(
        id="model-tier",
        severity="advice",
        category="settings",
        title=f"{agent_type} could run a cheaper model tier",
        lever="model",
        scope=scope,
        agent_type=agent_type,
        evidence=[("Ceiling saving (%)", 40.0, "model_swap.model_swap_by_agent_type", agent_type)],
    )


def test_model_tier_cards_merge_into_one_with_a_change_per_agent_type():
    report = _model_swap_report(
        [
            ["top-level", "claude-opus-5-5", "claude-sonnet-5", 30.0],
            ["reviewer", "claude-sonnet-5", "claude-haiku-4-5-20251001", 50.0],
            ["general-purpose", "claude-sonnet-5", "claude-haiku-4-5-20251001", 10.0],
            ["workflow-subagent", "claude-opus-5-5", "claude-sonnet-5", 99.0],
        ]
    )
    snap = Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"agents": {"reviewer": {"source": "project"}}})
    recs = [_tier(a) for a in ("top-level", "reviewer", "general-purpose", "workflow-subagent")]
    out = advice.finish(recs, report, snap, Units())
    tier = [r for r in out if r.id == "model-tier"]
    assert len(tier) == 1
    changes = tier[0].changes
    # Largest saving first; workflow subagents can't be changed.
    assert [(c.agent, c.value) for c in changes] == [
        ("reviewer", "haiku"),
        (None, "sonnet"),
        ("general-purpose", "haiku"),
    ]
    reviewer, main, builtin = changes
    assert reviewer.target == "agent" and reviewer.scope == "repo" and not reviewer.new_agent_file
    assert main.target == "settings" and main.key == "model"
    assert builtin.new_agent_file
    assert tier[0].saving_usd == 90.0
    assert tier[0].estimated_saving.startswith("At most 90.00 USD")
    fixes.attach_fixes(tier)
    assert fixes.command_for(reviewer, tier[0].scope).startswith("claude-token-lens apply --set model=haiku")
    assert tier[0].fixes[2]["command"] is None  # a built-in needs a new agent file


def _compaction(id_, **kw) -> Recommendation:
    return Recommendation(id=id_, severity="advice", category="settings", lever="autoCompactWindow", **kw)


def test_compaction_window_wins_over_churn_and_takes_the_setting_from_long_context():
    report = _model_swap_report([])
    recs = [
        _compaction(
            "compaction-window",
            title="Set autoCompactWindow to at least 250,000",
            evidence=[("Candidate 250,000: modelled saving after rediscovery correction", 12.0, "x", "250,000")],
        ),
        _compaction("compaction-churn", title="churn"),
        _compaction("long-context-share", title="long"),
    ]
    out = advice.finish(recs, report, None, Units())
    ids = [r.id for r in out]
    assert "compaction-churn" not in ids
    window = next(r for r in out if r.id == "compaction-window")
    assert window.changes[0].value == 250_000
    assert window.saving_usd == 12.0
    long_ctx = next(r for r in out if r.id == "long-context-share")
    assert long_ctx.lever is None and long_ctx.changes == [] and long_ctx.category == "workflow"
    # Sorted by saving within a severity.
    assert ids[0] == "compaction-window"


def test_compaction_window_is_dropped_when_already_at_or_below_the_floor():
    report = _model_swap_report([])
    snap = Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"effective": {"autoCompactWindow": 200_000}})
    recs = [_compaction("compaction-window", title="Set autoCompactWindow to at least 250,000")]
    out = advice.finish(recs, report, snap, Units())
    assert not any(r.id == "compaction-window" for r in out)


def test_spawn_cost_is_dropped_for_agents_no_file_can_change():
    report = _model_swap_report([])
    recs = [
        Recommendation(id="spawn-cost", severity="advice", agent_type="workflow-subagent"),
        Recommendation(id="spawn-cost", severity="advice", agent_type="reviewer"),
    ]
    out = advice.finish(recs, report, None, None)
    assert [r.agent_type for r in out] == ["reviewer"]


def test_severity_orders_before_saving():
    report = _model_swap_report([])
    recs = [
        Recommendation(id="a", severity="info", saving_usd=500.0),
        Recommendation(id="b", severity="advice", saving_usd=1.0),
        Recommendation(id="c", severity="advice", saving_usd=10.0),
        Recommendation(id="d", severity="action"),
    ]
    assert [r.id for r in advice.finish(recs, report, None, None)] == ["d", "c", "b", "a"]
