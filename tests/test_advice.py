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


def test_model_tier_leaves_out_agents_already_on_the_cheaper_model():
    """The period's saving still counts runs from before the change, so an
    agent already moved must not be offered it again."""
    report = _model_swap_report(
        [
            ["top-level", "claude-opus-5-5", "claude-sonnet-5", 30.0],
            ["reviewer", "claude-sonnet-5", "claude-haiku-4-5-20251001", 50.0],
            ["implementer", "claude-opus-5-5", "claude-sonnet-5", 20.0],
        ]
    )
    snap = Snapshot(
        path=None,
        ts="2026-09-20T00:00:00Z",
        data={
            "effective": {"model": "claude-sonnet-5"},
            "agents": {"reviewer": {"source": "project", "model": "haiku"}, "implementer": {"source": "project"}},
        },
    )
    recs = [_tier(a) for a in ("top-level", "reviewer", "implementer")]
    (tier,) = [r for r in advice.finish(recs, report, snap, Units()) if r.id == "model-tier"]
    assert [(c.agent, c.value) for c in tier.changes] == [("implementer", "sonnet")]
    assert tier.saving_usd == 20.0


def test_model_tier_leaves_out_an_agent_that_did_worse_on_the_cheaper_model():
    report = _model_swap_report(
        [
            ["reviewer", "claude-sonnet-5", "claude-haiku-4-5-20251001", 50.0],
            ["implementer", "claude-opus-5-5", "claude-sonnet-5", 20.0],
        ]
    )
    report.sections.append(
        Section(
            key="quality",
            title="Quality",
            tables=[
                Table(
                    name="quality_by_setup",
                    columns=[Column(key=k, label=k) for k in ("agent_type", "model", "setup_verdict", "compared_model")],
                    rows=[["reviewer", "claude-haiku-4-5-20251001", "worse", "claude-sonnet-5"]],
                )
            ],
        )
    )
    snap = Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"agents": {}})
    recs = [_tier("reviewer"), _tier("implementer")]
    (tier,) = [r for r in advice.finish(recs, report, snap, Units()) if r.id == "model-tier"]
    assert [c.agent for c in tier.changes] == ["implementer"]
    assert "Left out: reviewer (did worse on haiku)." in tier.why


def test_model_tier_is_dropped_when_every_agent_is_already_moved():
    report = _model_swap_report([["reviewer", "claude-sonnet-5", "claude-haiku-4-5-20251001", 50.0]])
    snap = Snapshot(
        path=None,
        ts="2026-09-20T00:00:00Z",
        data={"agents": {"reviewer": {"source": "user", "model": "claude-haiku-4-5-20251001"}}},
    )
    out = advice.finish([_tier("reviewer")], report, snap, Units())
    assert not any(r.id == "model-tier" for r in out)


def test_a_custom_agent_missing_from_the_snapshot_is_not_called_built_in():
    """The hook records only the agents of the project a session started
    in, so an absent custom agent may still have a file."""
    report = _model_swap_report([["implementer", "claude-opus-5-5", "claude-sonnet-5", 20.0]])
    snap = Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"agents": {}})
    (tier,) = [r for r in advice.finish([_tier("implementer")], report, snap, Units()) if r.id == "model-tier"]
    assert not tier.changes[0].new_agent_file


def test_cache_ttl_and_effort_cards_are_dropped_when_already_set():
    report = _model_swap_report([])
    snap = Snapshot(
        path=None,
        ts="2026-09-20T00:00:00Z",
        data={
            "effective": {"effortLevel": "Medium"},
            "agents": {"verification-runner": {"source": "user", "experimental.cacheTtl": "1h"}},
        },
    )
    recs = [
        Recommendation(
            id="ttl-switch",
            severity="advice",
            agent_type="verification-runner",
            evidence=[("TTL recommendation", "switch to 1h", "ttl", "verification-runner")],
        ),
        Recommendation(id="effort-mismatch", severity="advice"),
    ]
    assert advice.finish(recs, report, snap, Units()) == []


def test_already_set_matches_model_aliases_and_maps():
    assert fixes.already_set("model", "sonnet", "claude-sonnet-5")
    assert not fixes.already_set("model", "haiku", "claude-sonnet-5")
    assert not fixes.already_set("model", "sonnet", None)
    assert fixes.already_set("skillOverrides", {"pdf": "off"}, {"pdf": "off", "xlsx": "on"})
    assert not fixes.already_set("skillOverrides", {"pdf": "off", "xlsx": "off"}, {"pdf": "off"})
    assert fixes.already_set("omitClaudeMd", True, True)
    assert not fixes.already_set("maxTurns", 1, True)


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


# -- pricing-coverage (fix 2): title/why depend on which of the two usage
# tables the rule cites have rows -----------------------------------------


def _usage_report(*tables: Table) -> ReportModel:
    return ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=90.0)),
        sections=[Section(key="usage", title="Usage", tables=list(tables))],
        diagnostics=Diagnostics(lines=1000),
    )


def _unknown_models_table(rows) -> Table:
    return Table(
        name="pricing_unknown_models",
        columns=[
            Column(key="model_id", label="Model"),
            Column(key="turns", label="Turns"),
            Column(key="tokens", label="Tokens"),
        ],
        rows=rows,
    )


def _closest_match_table(rows) -> Table:
    return Table(
        name="pricing_closest_match",
        columns=[
            Column(key="model_id", label="Model"),
            Column(key="priced_as", label="Priced as"),
            Column(key="turns", label="Turns"),
            Column(key="tokens", label="Tokens"),
        ],
        rows=rows,
    )


def _pricing_coverage_rec() -> Recommendation:
    return Recommendation(id="pricing-coverage", severity="info", category="data", action="...")


def test_pricing_coverage_wording_when_only_unknown_models():
    report = _usage_report(_unknown_models_table([["claude-mystery-9", 3, 1000]]))
    out = advice.finish([_pricing_coverage_rec()], report, None, None)
    rec = out[0]
    assert rec.title == "Some usage has no price"
    assert "left out of every cost" in rec.why


def test_pricing_coverage_wording_when_only_closest_match():
    report = _usage_report(_closest_match_table([["claude-widget-9-preview", "claude-widget-9", 3, 1000]]))
    out = advice.finish([_pricing_coverage_rec()], report, None, None)
    rec = out[0]
    assert rec.title == "Some usage is priced by closest match, not its own rate"
    assert "estimated from the closest registered model" in rec.why


def test_pricing_coverage_wording_when_both_tables_present():
    report = _usage_report(
        _unknown_models_table([["claude-mystery-9", 3, 1000]]),
        _closest_match_table([["claude-widget-9-preview", "claude-widget-9", 3, 1000]]),
    )
    out = advice.finish([_pricing_coverage_rec()], report, None, None)
    rec = out[0]
    assert rec.title == "Some usage has no price, some is only an estimate"
    assert "left out of every cost" in rec.why
    assert "a different model's rate" in rec.why


def test_pricing_coverage_wording_falls_back_when_neither_table_present():
    # A report built with `include` leaving out `usage` (or one with no
    # rows in either table) still gets the plain "no price" wording.
    report = _usage_report()
    out = advice.finish([_pricing_coverage_rec()], report, None, None)
    rec = out[0]
    assert rec.title == "Some usage has no price"


def test_model_tier_leaves_out_an_agent_often_retried_on_a_larger_model():
    report = _model_swap_report([["reviewer", "claude-sonnet-5", "claude-haiku-4-5-20251001", 50.0]])
    report.sections.append(
        Section(
            key="quality",
            title="Quality",
            tables=[
                Table(
                    name="quality_retried",
                    columns=[Column(key=k, label=k) for k in ("agent_type", "model", "runs", "retried", "retried_on")],
                    rows=[["reviewer", "claude-haiku-4-5-20251001", 5, 2, "claude-sonnet-5"]],
                )
            ],
        )
    )
    snap = Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"agents": {}})
    assert not any(r.id == "model-tier" for r in advice.finish([_tier("reviewer")], report, snap, Units()))



def test_model_tier_leaves_out_an_agent_whose_runs_said_they_needed_a_larger_model():
    from claude_token_lens import habits

    report = _model_swap_report(
        [
            ["reviewer", "claude-sonnet-5", "claude-haiku-4-5-20251001", 50.0],
            ["implementer", "claude-opus-5-5", "claude-sonnet-5", 20.0],
        ]
    )
    runs = [habits.AgentFact(session_id="s", agent_type="reviewer", week="", cost=1.0, fit=fit)
            for fit in ("larger", "larger", "larger", "smaller", "smaller")]
    report.sections.append(habits.section_from(habits.Habits(agents=runs)))
    snap = Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"agents": {}})
    (tier,) = [r for r in advice.finish([_tier("reviewer"), _tier("implementer")], report, snap, Units())
               if r.id == "model-tier"]
    assert [c.agent for c in tier.changes] == ["implementer"]
    assert "Left out: reviewer (Claude said 3 of its runs needed a larger model)." in tier.why


def test_effort_mismatch_from_reported_work_is_explained_as_measured():
    rec = Recommendation(
        id="effort-mismatch",
        severity="advice",
        category="settings",
        title="x",
        lever="effortLevel",
        saving_usd=1.5,
        evidence=[
            ("Easy messages at high effort", 6, "habits.habits_effort_fit", "easy:high"),
            ("Easy work at high effort, thinking share of output", 55.0, "habits.habits_effort_fit", "easy:high"),
        ],
    )
    report = ReportModel(meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)), sections=[],
                         diagnostics=Diagnostics(lines=1000))
    snap = Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"effective": {}})
    (out,) = [r for r in advice.finish([rec], report, snap, Units()) if r.id == "effort-mismatch"]
    assert out.title == "High effort is being spent on easy work"
    assert out.why == (
        "Claude reported 6 of your messages as easy work, yet they ran at high effort or above, and up to 55% of "
        "their output was thinking."
    )
    assert out.changes[0].key == "effortLevel" and out.changes[0].value == "medium"
    assert out.estimated_saving.startswith("About ")
