"""Tests for WP10b's recommendation engine (recommend.py).

Most rules are exercised with hand-built ``ReportModel``/``Section``/
``Table`` fixtures (``recommend()`` works purely off the rendered report,
never a raw corpus -- see recommend.py's module docstring), which lets
each test cross exactly one rule's threshold without assembling a whole
transcript corpus. The broader evidence-exists check and the real-fixture
check instead go through ``report.build_report()`` on an actual corpus,
so they also prove the wiring in report.py (Deliverable 2) produces
recommendations whose evidence resolves against the report it was built
from.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from claude_token_lens import recommend, report
from claude_token_lens.config import Config
from claude_token_lens.corpus import load_corpus
from claude_token_lens.model import (
    Column,
    Diagnostics,
    PricingMeta,
    ReportMeta,
    ReportModel,
    Section,
    Table,
)
from claude_token_lens.recommend import RecommendThresholds, recommend as recommend_fn, render_patch_set
from claude_token_lens.snapshots import Snapshot

from helpers import assert_privacy, turn_line, write_jsonl

REAL_SESSION_A = Path(__file__).parent / "fixtures" / "real" / "session-a"


# -- fixture builders ------------------------------------------------------


def _overview_totals(sessions: int = 10, priced_turns: int = 400, cache_read_cost_share_pct: float = 10.0) -> Table:
    return Table(
        name="totals",
        title="Totals",
        columns=[Column(key="metric", label="Metric"), Column(key="value", label="Value")],
        rows=[
            ["sessions", sessions],
            ["priced_turns", priced_turns],
            ["cache_read_cost_share_pct", cache_read_cost_share_pct],
        ],
    )


def _base_report(**overview_kwargs) -> ReportModel:
    """A minimal but min-sample-satisfying report: just the ``overview``
    section every rule's ``_meets_min_sample`` gate reads. Individual
    tests add whatever other section/table a given rule needs.
    """
    overview = Section(key="overview", title="Overview", tables=[_overview_totals(**overview_kwargs)])
    return ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)),
        sections=[overview],
        recommendations=[],
        diagnostics=Diagnostics(lines=1000, unparsable_lines=0, ttl_sum_mismatch=0),
    )


def _add_section(report_model: ReportModel, section: Section) -> ReportModel:
    report_model.sections.append(section)
    return report_model


def _config(provider: str | None = None) -> Config:
    return Config(provider=provider)


# -- minimum sample gate -----------------------------------------------------


def test_below_minimum_sample_suppresses_everything():
    small_report = _base_report(sessions=2, priced_turns=50)
    # Add a table that would otherwise clearly fire cache-read-dominance.
    small_report.sections[0].tables[0] = _overview_totals(
        sessions=2, priced_turns=50, cache_read_cost_share_pct=90.0
    )
    recs = recommend_fn(small_report, config=_config(), archetype=None)
    assert recs == []


def test_meets_minimum_sample_via_sessions_only():
    r = _base_report(sessions=5, priced_turns=10, cache_read_cost_share_pct=90.0)
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert any(rec.id == "cache-read-dominance" for rec in recs)


def test_meets_minimum_sample_via_priced_turns_only():
    r = _base_report(sessions=1, priced_turns=200, cache_read_cost_share_pct=90.0)
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert any(rec.id == "cache-read-dominance" for rec in recs)


# -- cache-read-dominance (simplest rule, also used above) -------------------


def test_cache_read_dominance_fires_above_threshold():
    r = _base_report(cache_read_cost_share_pct=60.0)
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "cache-read-dominance")
    assert rec.evidence == [
        ("Cache-read share of cost", 60.0, "overview.totals", "cache_read_cost_share_pct"),
    ]


def test_cache_read_dominance_does_not_fire_below_threshold():
    r = _base_report(cache_read_cost_share_pct=40.0)
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "cache-read-dominance" for rec in recs)


# -- ttl-switch ---------------------------------------------------------


def _ttl_by_agent_type_table(rows: list[list]) -> Table:
    return Table(
        name="ttl_by_agent_type",
        title="TTL by agent type",
        columns=[
            Column(key="agent_type", label="Agent type"),
            Column(key="cost_observed", label="Cost observed", kind="money"),
            Column(key="fidelity_pct", label="Fidelity", kind="pct"),
            Column(key="recommendation", label="Recommendation"),
            Column(key="lever", label="Lever"),
        ],
        rows=rows,
    )


def test_ttl_switch_fires_for_row_recommending_a_switch():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [["top-level", 10.0, 0.0, "switch to 1h", "promptCacheTtl"]]
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "ttl-switch")
    assert rec.lever == "promptCacheTtl"
    assert "1h" in rec.action
    assert rec.evidence == [
        ("TTL recommendation", "switch to 1h", "ttl.ttl_by_agent_type", "top-level"),
    ]


def test_ttl_switch_does_not_fire_for_no_material_difference():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [["top-level", 10.0, 0.0, "no material difference", "promptCacheTtl"]]
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "ttl-switch" for rec in recs)


def test_ttl_switch_suppressed_for_non_anthropic_provider():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [["top-level", 10.0, 0.0, "switch to 1h", "promptCacheTtl"]]
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(provider="bedrock"), archetype=None)
    assert not any(rec.id == "ttl-switch" for rec in recs)


def test_ttl_switch_allowed_for_anthropic_and_none_provider():
    for provider in (None, "anthropic"):
        r = _base_report()
        r = _add_section(
            r,
            Section(
                key="ttl",
                title="TTL",
                tables=[
                    _ttl_by_agent_type_table(
                        [["top-level", 10.0, 0.0, "switch to 1h", "promptCacheTtl"]]
                    )
                ],
            ),
        )
        recs = recommend_fn(r, config=_config(provider=provider), archetype=None)
        assert any(rec.id == "ttl-switch" for rec in recs), provider


def test_ttl_switch_managed_key_gains_managed_scope_and_action_text():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [["top-level", 10.0, 0.0, "switch to 1h", "promptCacheTtl"]]
                )
            ],
        ),
    )
    snapshot = Snapshot(path=Path("snap.json"), ts="20260918T000000Z", data={"managed_keys": ["promptCacheTtl"]})
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    rec = next(rec for rec in recs if rec.id == "ttl-switch")
    assert rec.lever == "promptCacheTtl"
    assert rec.scope == "managed"
    assert "managed by policy" in rec.action


def test_ttl_switch_unmanaged_key_has_user_scope():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [["top-level", 10.0, 0.0, "switch to 1h", "promptCacheTtl"]]
                )
            ],
        ),
    )
    snapshot = Snapshot(path=Path("snap.json"), ts="20260918T000000Z", data={"managed_keys": ["model"]})
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    rec = next(rec for rec in recs if rec.id == "ttl-switch")
    assert rec.lever == "promptCacheTtl"
    assert rec.scope == "user"
    assert "managed by policy" not in rec.action


# -- subagent-volume ------------------------------------------------------


def test_subagent_volume_fires_above_threshold():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [
                        ["top-level", 40.0, 0.0, "no material difference", "promptCacheTtl"],
                        ["claude-implementer", 60.0, 0.0, "no material difference", "promptCacheTtl"],
                    ]
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "subagent-volume")
    assert rec.evidence == [
        ("Share of corpus cost", 60.0, "ttl.ttl_by_agent_type", "claude-implementer"),
    ]


def test_subagent_volume_does_not_fire_below_threshold():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [
                        ["top-level", 70.0, 0.0, "no material difference", "promptCacheTtl"],
                        ["claude-implementer", 30.0, 0.0, "no material difference", "promptCacheTtl"],
                    ]
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "subagent-volume" for rec in recs)


def test_subagent_volume_suppressed_for_overseer_fanout():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [
                        ["top-level", 40.0, 0.0, "no material difference", "promptCacheTtl"],
                        ["claude-implementer", 60.0, 0.0, "no material difference", "promptCacheTtl"],
                    ]
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype="overseer-fanout")
    assert not any(rec.id == "subagent-volume" for rec in recs)


# -- compaction-churn ------------------------------------------------------


def _compactions_summary_table(rows: dict[str, float]) -> Table:
    return Table(
        name="compactions_summary",
        title="Compactions summary",
        columns=[Column(key="metric", label="Metric"), Column(key="value", label="Value")],
        rows=[[k, v] for k, v in rows.items()],
    )


def test_compaction_churn_fires_on_mean_per_session():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="compactions",
            title="Compactions",
            tables=[
                _compactions_summary_table(
                    {
                        "Compactions per session (mean)": 3.0,
                        "Dropped tokens (share of new_tokens: input+cache_creation)": 5.0,
                    }
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "compaction-churn")
    assert rec.lever == "autoCompactWindow"
    assert ("Compactions per session (mean)", 3.0, "compactions.compactions_summary", "Compactions per session (mean)") in rec.evidence


def test_compaction_churn_fires_on_dropped_share():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="compactions",
            title="Compactions",
            tables=[
                _compactions_summary_table(
                    {
                        "Compactions per session (mean)": 0.5,
                        "Dropped tokens (share of new_tokens: input+cache_creation)": 45.0,
                    }
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert any(rec.id == "compaction-churn" for rec in recs)


def test_compaction_churn_does_not_fire_below_both_thresholds():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="compactions",
            title="Compactions",
            tables=[
                _compactions_summary_table(
                    {
                        "Compactions per session (mean)": 0.5,
                        "Dropped tokens (share of new_tokens: input+cache_creation)": 5.0,
                    }
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "compaction-churn" for rec in recs)


def test_compaction_churn_managed_lever():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="compactions",
            title="Compactions",
            tables=[_compactions_summary_table({"Compactions per session (mean)": 3.0})],
        ),
    )
    snapshot = Snapshot(path=Path("s.json"), ts="20260918T000000Z", data={"managed_keys": ["autoCompactWindow"]})
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    rec = next(rec for rec in recs if rec.id == "compaction-churn")
    assert rec.lever == "autoCompactWindow"
    assert rec.scope == "managed"
    assert "managed by policy" in rec.action


# -- long-context-share ---------------------------------------------------


def test_long_context_share_fires_on_huge_context_share():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_huge_context",
                    title="Huge context",
                    columns=[Column(key="count", label="Count"), Column(key="share_pct", label="Share", kind="pct")],
                    rows=[[3, 25.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "long-context-share")
    assert ("Cache-read volume share from huge-context turns", 25.0, "recache.recache_huge_context", 3) in rec.evidence


def test_long_context_share_fires_on_p90_ctx():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[
                Table(
                    name="dimensions",
                    title="Dimensions",
                    columns=[
                        Column(key="dimension", label="Dimension"),
                        Column(key="level", label="Level"),
                        Column(key="label", label="Label"),
                        Column(key="metric", label="Metric"),
                        Column(key="value", label="Value"),
                        Column(key="threshold", label="Threshold"),
                    ],
                    rows=[["context_hygiene", "warn", "Context hygiene", "p90_top_level_ctx", 200_000, 150_000]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "long-context-share")
    assert (
        "p90 top-level context size (proxy for median)",
        200_000,
        "scorecard.dimensions",
        "context_hygiene",
    ) in rec.evidence


def test_long_context_share_does_not_fire_below_both():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_huge_context",
                    title="Huge context",
                    columns=[Column(key="count", label="Count"), Column(key="share_pct", label="Share", kind="pct")],
                    rows=[[0, 5.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "long-context-share" for rec in recs)


# -- baseline-bloat ---------------------------------------------------------


def test_baseline_bloat_fires_with_snapshot_evidence():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_session_baseline",
                    title="Session baseline",
                    columns=[Column(key="sessions", label="Sessions"), Column(key="mean_baseline", label="Mean baseline")],
                    rows=[[10, 50_000]],
                )
            ],
        ),
    )
    snapshot = Snapshot(
        path=Path("s.json"),
        ts="20260918T000000Z",
        data={"mcp_servers": {"a": {}, "b": {}, "c": {}}, "enabled_plugins": {"x": {}, "y": {}}},
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    rec = next(rec for rec in recs if rec.id == "baseline-bloat")
    assert rec.lever == "mcpServers"
    assert rec.evidence == [
        ("Mean session baseline (cache-creation)", 50_000, "agents.topology_session_baseline", 10),
    ]


def test_baseline_bloat_does_not_fire_without_snapshot():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_session_baseline",
                    title="Session baseline",
                    columns=[Column(key="sessions", label="Sessions"), Column(key="mean_baseline", label="Mean baseline")],
                    rows=[[10, 50_000]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=None)
    assert not any(rec.id == "baseline-bloat" for rec in recs)


def test_baseline_bloat_does_not_fire_with_too_few_mcp_servers():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_session_baseline",
                    title="Session baseline",
                    columns=[Column(key="sessions", label="Sessions"), Column(key="mean_baseline", label="Mean baseline")],
                    rows=[[10, 50_000]],
                )
            ],
        ),
    )
    snapshot = Snapshot(path=Path("s.json"), ts="20260918T000000Z", data={"mcp_servers": {"a": {}}})
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    assert not any(rec.id == "baseline-bloat" for rec in recs)


def test_baseline_bloat_suppressed_for_chat_only():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_session_baseline",
                    title="Session baseline",
                    columns=[Column(key="sessions", label="Sessions"), Column(key="mean_baseline", label="Mean baseline")],
                    rows=[[10, 50_000]],
                )
            ],
        ),
    )
    snapshot = Snapshot(path=Path("s.json"), ts="20260918T000000Z", data={"mcp_servers": {"a": {}, "b": {}, "c": {}, "d": {}, "e": {}}})
    recs = recommend_fn(r, config=_config(), archetype="chat-only", snapshot=snapshot)
    assert not any(rec.id == "baseline-bloat" for rec in recs)


# -- agent-report-size ------------------------------------------------------


def test_agent_report_size_fires_per_agent_type():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_report_proxy",
                    title="Report proxy",
                    columns=[Column(key="agent_type", label="Agent type"), Column(key="mean_proxy", label="Mean proxy")],
                    rows=[["claude-implementer", 12_000], ["haiku-sweeper", 2_000]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    fired = [rec for rec in recs if rec.id == "agent-report-size"]
    assert len(fired) == 1
    assert fired[0].evidence == [
        ("Mean report proxy (output tokens)", 12_000, "agents.topology_report_proxy", "claude-implementer"),
    ]


def test_agent_report_size_suppressed_for_chat_only():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_report_proxy",
                    title="Report proxy",
                    columns=[Column(key="agent_type", label="Agent type"), Column(key="mean_proxy", label="Mean proxy")],
                    rows=[["claude-implementer", 12_000]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype="chat-only")
    assert not any(rec.id == "agent-report-size" for rec in recs)


# -- spawn-cost --------------------------------------------------------------


def test_spawn_cost_fires_per_agent_type():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_spawn_write",
                    title="Spawn write",
                    columns=[Column(key="agent_type", label="Agent type"), Column(key="mean_write", label="Mean write")],
                    rows=[["claude-implementer", 50_000]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "spawn-cost")
    assert rec.lever == "omitClaudeMd"
    assert rec.evidence == [
        ("Mean first-turn write", 50_000, "agents.topology_spawn_write", "claude-implementer"),
    ]


def test_spawn_cost_not_suppressed_for_overseer_fanout():
    """spawn-cost's advice ('trim the briefing') is different from
    subagent-volume's ('stop spawning so much') -- an overseer-fanout
    session should still be told to trim an expensive spawn briefing."""
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_spawn_write",
                    title="Spawn write",
                    columns=[Column(key="agent_type", label="Agent type"), Column(key="mean_write", label="Mean write")],
                    rows=[["claude-implementer", 50_000]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype="overseer-fanout")
    assert any(rec.id == "spawn-cost" for rec in recs)


def test_spawn_cost_suppressed_for_chat_only():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_spawn_write",
                    title="Spawn write",
                    columns=[Column(key="agent_type", label="Agent type"), Column(key="mean_write", label="Mean write")],
                    rows=[["claude-implementer", 50_000]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype="chat-only")
    assert not any(rec.id == "spawn-cost" for rec in recs)


def test_spawn_cost_does_not_fire_below_threshold():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_spawn_write",
                    title="Spawn write",
                    columns=[Column(key="agent_type", label="Agent type"), Column(key="mean_write", label="Mean write")],
                    rows=[["claude-implementer", 10_000]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "spawn-cost" for rec in recs)


# -- effort-mismatch ---------------------------------------------------------


def test_effort_mismatch_fires_with_evidence_per_purpose_row():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="sessions",
            title="Sessions",
            tables=[
                Table(
                    name="sessions_by_purpose",
                    title="Sessions by purpose",
                    columns=[Column(key="purpose", label="Purpose"), Column(key="sessions", label="Sessions")],
                    rows=[["docs-or-light-edit", 4], ["general-dev", 3], ["review", 2]],
                )
            ],
        ),
    )
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_effort_tokens",
                    title="Effort tokens",
                    columns=[
                        Column(key="effort", label="Effort"),
                        Column(key="turns", label="Turns"),
                        Column(key="output_tokens", label="Output tokens"),
                        Column(key="thinking_tokens", label="Thinking tokens"),
                        Column(key="thinking_share", label="Thinking share", kind="pct"),
                    ],
                    rows=[["high", 100, 10_000, 4_000, 40.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "effort-mismatch")
    assert rec.lever == "effortLevel"
    # one evidence tuple for the thinking-share condition plus one per
    # contributing purpose row -- each citing its own exact cell (the
    # bug this module's docstring documents fixing).
    assert ("High-effort thinking share of output", 40.0, "agents.topology_effort_tokens", "high") in rec.evidence
    assert ("docs-or-light-edit sessions in corpus", 4, "sessions.sessions_by_purpose", "docs-or-light-edit") in rec.evidence
    assert ("general-dev sessions in corpus", 3, "sessions.sessions_by_purpose", "general-dev") in rec.evidence
    assert len(rec.evidence) == 3


def test_effort_mismatch_does_not_fire_without_docs_purposes():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="sessions",
            title="Sessions",
            tables=[
                Table(
                    name="sessions_by_purpose",
                    title="Sessions by purpose",
                    columns=[Column(key="purpose", label="Purpose"), Column(key="sessions", label="Sessions")],
                    rows=[["review", 5]],
                )
            ],
        ),
    )
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_effort_tokens",
                    title="Effort tokens",
                    columns=[
                        Column(key="effort", label="Effort"),
                        Column(key="thinking_share", label="Thinking share", kind="pct"),
                    ],
                    rows=[["high", 40.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "effort-mismatch" for rec in recs)


def test_effort_mismatch_does_not_fire_below_thinking_share():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="sessions",
            title="Sessions",
            tables=[
                Table(
                    name="sessions_by_purpose",
                    title="Sessions by purpose",
                    columns=[Column(key="purpose", label="Purpose"), Column(key="sessions", label="Sessions")],
                    rows=[["general-dev", 4]],
                )
            ],
        ),
    )
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_effort_tokens",
                    title="Effort tokens",
                    columns=[
                        Column(key="effort", label="Effort"),
                        Column(key="thinking_share", label="Thinking share", kind="pct"),
                    ],
                    rows=[["high", 10.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "effort-mismatch" for rec in recs)


# -- discovery-share ----------------------------------------------------


def _phases_summary_table(discovery_share: float) -> Table:
    return Table(
        name="phases_summary",
        title="Phases",
        columns=[Column(key="phase", label="Phase"), Column(key="cost_share_pct", label="Cost share", kind="pct")],
        rows=[["discovery", discovery_share], ["implementation", 100 - discovery_share]],
    )


def test_discovery_share_fires_when_phases_section_present_and_above_threshold():
    r = _base_report()
    r = _add_section(r, Section(key="phases", title="Phases", tables=[_phases_summary_table(50.0)]))
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "discovery-share")
    assert rec.evidence == [("DISCOVERY cost share", 50.0, "phases.phases_summary", "discovery")]


def test_discovery_share_does_not_fire_without_phases_section():
    r = _base_report()  # no "phases" section at all -- --phases wasn't passed
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "discovery-share" for rec in recs)


def test_discovery_share_does_not_fire_below_threshold():
    r = _base_report()
    r = _add_section(r, Section(key="phases", title="Phases", tables=[_phases_summary_table(20.0)]))
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "discovery-share" for rec in recs)


# -- pricing-coverage ---------------------------------------------------


def _scorecard_dimensions_table(rows: list[list]) -> Table:
    return Table(
        name="dimensions",
        title="Dimensions",
        columns=[
            Column(key="dimension", label="Dimension"),
            Column(key="level", label="Level"),
            Column(key="label", label="Label"),
            Column(key="metric", label="Metric"),
            Column(key="value", label="Value"),
            Column(key="threshold", label="Threshold"),
        ],
        rows=rows,
    )


def test_pricing_coverage_fires_when_below_full_coverage():
    r = _base_report()
    r.meta.pricing.coverage_pct = 95.0
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "warn", "Data quality", "pricing_coverage_pct", 95.0, 100.0]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "pricing-coverage")
    assert rec.evidence == [
        ("Pricing coverage (data-quality dimension)", 95.0, "scorecard.dimensions", "data_quality"),
    ]


def test_pricing_coverage_does_not_fire_at_full_coverage():
    r = _base_report()
    r.meta.pricing.coverage_pct = 100.0
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "ok", "Data quality", "pricing_coverage_pct", 100.0, 100.0]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "pricing-coverage" for rec in recs)


def test_pricing_coverage_fires_from_coverage_pct_alone_with_no_unknown_models_table():
    # R12: report.py never actually attaches a usage.pricing_unknown_models
    # table to any section -- the rule must fire off
    # report.meta.pricing.coverage_pct alone, not a dead table lookup.
    r = _base_report()
    r.meta.pricing.coverage_pct = 42.0
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "warn", "Data quality", "pricing_coverage_pct", 42.0, 100.0]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert any(rec.id == "pricing-coverage" for rec in recs)


def test_pricing_coverage_action_names_unknown_model_ids_when_table_present():
    r = _base_report()
    r.meta.pricing.coverage_pct = 90.0
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "warn", "Data quality", "pricing_coverage_pct", 90.0, 100.0]])],
        ),
    )
    r = _add_section(
        r,
        Section(
            key="usage",
            title="Usage",
            tables=[
                Table(
                    name="pricing_unknown_models",
                    title="Unpriced models",
                    columns=[
                        Column(key="model_id", label="Model"),
                        Column(key="turns", label="Turns"),
                        Column(key="tokens", label="Tokens"),
                    ],
                    rows=[["claude-mystery-9", 3, 1000]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "pricing-coverage")
    assert "claude-mystery-9" in rec.action


# -- data-quality ---------------------------------------------------------


def test_data_quality_fires_on_ttl_fidelity():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[_ttl_by_agent_type_table([["top-level", 10.0, 25.0, "no material difference", "promptCacheTtl"]])],
        ),
    )
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "warn", "Data quality", "pricing_coverage_pct", 100.0, 100.0]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "data-quality")
    assert ("Pricing coverage (data-quality dimension)", 100.0, "scorecard.dimensions", "data_quality") in rec.evidence
    assert ("TTL simulation fidelity", 25.0, "ttl.ttl_by_agent_type", "top-level") in rec.evidence


def test_data_quality_fires_on_unparsable_lines():
    r = _base_report()
    r.diagnostics = Diagnostics(lines=1000, unparsable_lines=5, ttl_sum_mismatch=0)  # 0.5% > 0.1%
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "ok", "Data quality", "pricing_coverage_pct", 100.0, 100.0]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "data-quality")
    assert "unparsable" in rec.action


def test_data_quality_fires_on_ttl_mismatch():
    r = _base_report()
    r.diagnostics = Diagnostics(lines=1000, unparsable_lines=0, ttl_sum_mismatch=2)
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "ok", "Data quality", "pricing_coverage_pct", 100.0, 100.0]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert any(rec.id == "data-quality" for rec in recs)


def test_data_quality_does_not_fire_when_all_clean():
    r = _base_report()
    r.diagnostics = Diagnostics(lines=1000, unparsable_lines=0, ttl_sum_mismatch=0)
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[_ttl_by_agent_type_table([["top-level", 10.0, 1.0, "no material difference", "promptCacheTtl"]])],
        ),
    )
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "ok", "Data quality", "pricing_coverage_pct", 100.0, 100.0]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "data-quality" for rec in recs)


# -- long-tool-waits / notification-invalidation / batch-instructions -------
# (recache-derived rules whose conditions are approximated across two
# tables each -- see recommend.py's module docstring)


def test_long_tool_waits_fires_above_both_thresholds():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_summary",
                    title="Recache summary",
                    columns=[Column(key="metric", label="Metric"), Column(key="recache_cc_tokens", label="Recache cc tokens")],
                    rows=[["all", 100_000]],
                ),
                Table(
                    name="recache_signature_split",
                    title="Signature split",
                    columns=[Column(key="signature", label="Signature"), Column(key="cc_tokens", label="CC tokens")],
                    rows=[["full-expiry", 30_000], ["prefix-invalidated", 70_000]],
                ),
                Table(
                    name="recache_preceding_tool",
                    title="Preceding tool",
                    columns=[Column(key="tool", label="Tool"), Column(key="share_pct_turns", label="Share")],
                    rows=[["Bash", 40.0], ["PowerShell", 30.0]],
                ),
                Table(
                    name="recache_gap_buckets",
                    title="Gap buckets",
                    columns=[Column(key="bucket", label="Bucket"), Column(key="share_pct_turns", label="Share")],
                    rows=[[">60m", 30.0], ["15-60m", 25.0], ["5-15m", 20.0], ["1-5m", 15.0], ["<1m", 10.0]],
                ),
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "long-tool-waits")
    assert ("Full-expiry cache-creation tokens", 30_000, "recache.recache_signature_split", "full-expiry") in rec.evidence
    # R14: no table exposes the true joint count of turns preceded by
    # Bash/PowerShell AND following a long gap, so each of the two
    # independent shares that stand in for it must be cited as its own
    # evidence entry (previously the long-gap-bucket shares weren't
    # cited at all, only used to compute a fake "combined" number).
    for bucket, expected in ((">60m", 30.0), ("15-60m", 25.0), ("5-15m", 20.0)):
        assert (
            f"{bucket} gap-bucket re-cache turn share",
            expected,
            "recache.recache_gap_buckets",
            bucket,
        ) in rec.evidence


def test_long_tool_waits_requires_both_shares_independently_above_threshold():
    # Bash/PowerShell share is well above threshold (90%), but the
    # long-gap share is well below it (10%) -- the two independent
    # turn populations plainly don't overlap enough to justify firing,
    # even though a naive min() of two *different* metrics could be
    # fooled by a badly-chosen pair of inputs. Here both the old and
    # new logic agree the rule should not fire; this pins that a low
    # long-gap share alone is enough to suppress it regardless of how
    # high the tool share runs.
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_summary",
                    title="Recache summary",
                    columns=[Column(key="metric", label="Metric"), Column(key="recache_cc_tokens", label="Recache cc tokens")],
                    rows=[["all", 100_000]],
                ),
                Table(
                    name="recache_signature_split",
                    title="Signature split",
                    columns=[Column(key="signature", label="Signature"), Column(key="cc_tokens", label="CC tokens")],
                    rows=[["full-expiry", 30_000]],
                ),
                Table(
                    name="recache_preceding_tool",
                    title="Preceding tool",
                    columns=[Column(key="tool", label="Tool"), Column(key="share_pct_turns", label="Share")],
                    rows=[["Bash", 60.0], ["PowerShell", 30.0]],
                ),
                Table(
                    name="recache_gap_buckets",
                    title="Gap buckets",
                    columns=[Column(key="bucket", label="Bucket"), Column(key="share_pct_turns", label="Share")],
                    rows=[[">60m", 5.0], ["15-60m", 3.0], ["5-15m", 2.0]],
                ),
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "long-tool-waits" for rec in recs)


def test_long_tool_waits_does_not_fire_below_full_expiry_share():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_summary",
                    title="Recache summary",
                    columns=[Column(key="metric", label="Metric"), Column(key="recache_cc_tokens", label="Recache cc tokens")],
                    rows=[["all", 100_000]],
                ),
                Table(
                    name="recache_signature_split",
                    title="Signature split",
                    columns=[Column(key="signature", label="Signature"), Column(key="cc_tokens", label="CC tokens")],
                    rows=[["full-expiry", 5_000]],
                ),
                Table(
                    name="recache_preceding_tool",
                    title="Preceding tool",
                    columns=[Column(key="tool", label="Tool"), Column(key="share_pct_turns", label="Share")],
                    rows=[["Bash", 40.0]],
                ),
                Table(
                    name="recache_gap_buckets",
                    title="Gap buckets",
                    columns=[Column(key="bucket", label="Bucket"), Column(key="share_pct_turns", label="Share")],
                    rows=[[">60m", 30.0]],
                ),
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "long-tool-waits" for rec in recs)


def test_notification_invalidation_fires_above_both_thresholds():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_primary_cause_prefix_invalidated",
                    title="Primary cause (prefix-invalidated)",
                    columns=[
                        Column(key="primary", label="Primary"),
                        Column(key="cc_share_pct", label="CC share"),
                        Column(key="control_cc_share_pct", label="Control share"),
                        Column(key="over_representation_points_tokens", label="Over-rep points"),
                    ],
                    rows=[["task_notification", 40.0, 20.0, 20.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "notification-invalidation")
    assert (
        "TASK_NOTIFICATION share of prefix-invalidated cc",
        40.0,
        "recache.recache_primary_cause_prefix_invalidated",
        "task_notification",
    ) in rec.evidence


def test_notification_invalidation_does_not_fire_below_thresholds():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_primary_cause_prefix_invalidated",
                    title="Primary cause (prefix-invalidated)",
                    columns=[
                        Column(key="primary", label="Primary"),
                        Column(key="cc_share_pct", label="CC share"),
                        Column(key="over_representation_points_tokens", label="Over-rep points"),
                    ],
                    rows=[["task_notification", 10.0, 2.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "notification-invalidation" for rec in recs)


def test_batch_instructions_fires_above_overrep_threshold():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_primary_cause",
                    title="Primary cause",
                    columns=[
                        Column(key="primary", label="Primary"),
                        Column(key="cc_share_pct", label="CC share"),
                        Column(key="over_representation_points_tokens", label="Over-rep points"),
                    ],
                    rows=[["queue_operation", 30.0, 15.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "batch-instructions")
    assert ("Over-representation vs all-turns control", 15.0, "recache.recache_primary_cause", "queue_operation") in rec.evidence


def test_batch_instructions_does_not_fire_below_overrep_threshold():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_primary_cause",
                    title="Primary cause",
                    columns=[
                        Column(key="primary", label="Primary"),
                        Column(key="cc_share_pct", label="CC share"),
                        Column(key="over_representation_points_tokens", label="Over-rep points"),
                    ],
                    rows=[["queue_operation", 30.0, 2.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "batch-instructions" for rec in recs)


# -- RecommendThresholds.from_config -----------------------------------


def test_from_config_none_returns_defaults():
    th = RecommendThresholds.from_config(None)
    assert th == RecommendThresholds()


def test_from_config_overrides_known_field():
    th = RecommendThresholds.from_config({"cache_read_dominance_pct": 75.0})
    assert th.cache_read_dominance_pct == 75.0
    assert th.subagent_volume_cost_share_pct == RecommendThresholds().subagent_volume_cost_share_pct


def test_from_config_ignores_unknown_and_malformed_keys():
    th = RecommendThresholds.from_config({"not_a_real_field": 1.0, "cache_read_dominance_pct": "not-a-number"})
    assert th == RecommendThresholds()


def test_recommend_honours_explicit_thresholds_override():
    r = _base_report(cache_read_cost_share_pct=60.0)
    default_recs = recommend_fn(r, config=_config(), archetype=None)
    assert any(rec.id == "cache-read-dominance" for rec in default_recs)

    strict = RecommendThresholds(cache_read_dominance_pct=90.0)
    strict_recs = recommend_fn(r, config=_config(), archetype=None, thresholds=strict)
    assert not any(rec.id == "cache-read-dominance" for rec in strict_recs)


# -- render_patch_set ------------------------------------------------------


def test_render_patch_set_per_agent_ttl_lever():
    rec = dataclasses.replace(
        _make_recommendation(),
        lever="experimental.cacheTtl in claude-implementer.md (or subagentPromptCacheTtl for all subagents)",
        action="Switch claude-implementer's prompt cache TTL to 1h.",
    )
    text = render_patch_set([rec])
    assert "--- .claude/agents/claude-implementer.md" in text
    assert "+experimental.cacheTtl: 1h" in text


def test_render_patch_set_top_level_prompt_cache_ttl_lever():
    rec = dataclasses.replace(
        _make_recommendation(),
        lever="promptCacheTtl",
        action="Switch top-level's prompt cache TTL to 5m.",
    )
    text = render_patch_set([rec])
    assert "--- settings (user)" in text
    assert "+promptCacheTtl: 5m" in text
    # No path other than .claude/agents/<agent_type>.md anywhere in output.
    assert ".claude/agents/" not in text


def test_render_patch_set_managed_lever_gets_reference_only_comment():
    rec = dataclasses.replace(_make_recommendation(), lever="autoCompactWindow", scope="managed", action="Raise it.")
    text = render_patch_set([rec])
    assert "# managed by policy -- shown for reference only" in text
    assert "autoCompactWindow" in text


def test_render_patch_set_deduplicates_same_lever():
    rec_a = dataclasses.replace(_make_recommendation(id="a"), lever="autoCompactWindow", action="Raise it.")
    rec_b = dataclasses.replace(_make_recommendation(id="b"), lever="autoCompactWindow", action="Raise it more.")
    text = render_patch_set([rec_a, rec_b])
    assert text.count("--- settings (user)") == 1


def test_render_patch_set_skips_recommendations_with_no_lever():
    rec = dataclasses.replace(_make_recommendation(), lever=None)
    text = render_patch_set([rec])
    assert text == ""


def _make_recommendation(**overrides):
    from claude_token_lens.model import Recommendation

    fields = dict(
        id="x",
        severity="advice",
        category="settings",
        archetypes=(),
        title="Title",
        action="Action.",
        lever=None,
        evidence=[],
    )
    fields.update(overrides)
    return Recommendation(**fields)


# -- privacy --------------------------------------------------------------


def test_recommend_output_has_no_privacy_leaks():
    r = _base_report(cache_read_cost_share_pct=90.0)
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[_ttl_by_agent_type_table([["claude-implementer", 10.0, 0.0, "switch to 1h", "promptCacheTtl"]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert recs, "expected at least one recommendation to scan"
    for rec in recs:
        assert_privacy(rec)
    patch_text = render_patch_set(recs)
    assert_privacy({"patch_set": patch_text})


# -- evidence-exists integration test (real assembled ReportModel) ---------


def test_every_recommendation_evidence_resolves_against_the_report(tmp_path: Path):
    """Build a real corpus through report.build_report() (exercising the
    report.py wiring from Deliverable 2) and confirm every recommendation
    it produces cites evidence that actually exists in the assembled
    report's own sections, with the exact cell value quoted.
    """
    project_dir = tmp_path / "proj-recommend"
    project_dir.mkdir()
    # 220 priced turns in one session clears the "200 priced turns" half of
    # the minimum-sample gate; heavy cache_read relative to input/output
    # tokens is enough to trip cache-read-dominance, so the evidence walk
    # below isn't vacuous.
    lines = [
        turn_line(
            timestamp=f"2026-09-{10 + (i % 15):02d}T12:00:00.000Z",
            input_tokens=100,
            output_tokens=50,
            ephemeral_5m_input_tokens=1000,
            cache_read_input_tokens=5000,
        )
        for i in range(220)
    ]
    write_jsonl(project_dir / "session-recommend.jsonl", lines)

    corpus = load_corpus([project_dir])
    from claude_token_lens.pricing import load_pricing

    pricing = load_pricing()
    model = report.build_report(corpus, pricing, Config(), projects=("proj-recommend",), window="test", phases=True)

    assert model.recommendations, "expected at least one recommendation from this cache-read-heavy corpus"
    for rec in model.recommendations:
        for label, value, source_table, row_key in rec.evidence:
            section_key, table_name = source_table.split(".", 1)
            section = next((s for s in model.sections if s.key == section_key), None)
            assert section is not None, f"{rec.id}: no section {section_key!r} for evidence {label!r}"
            table = next((t for t in section.tables if t.name == table_name), None)
            assert table is not None, f"{rec.id}: no table {table_name!r} for evidence {label!r}"
            row = next((row for row in table.rows if row and row[0] == row_key), None)
            assert row is not None, f"{rec.id}: no row {row_key!r} in {source_table} for evidence {label!r}"


@pytest.mark.skipif(not REAL_SESSION_A.exists(), reason="tests/fixtures/real/session-a not present")
def test_real_fixture_recommendation_evidence_resolves(tmp_path: Path):
    """Same evidence-exists walk as above, against the real anonymised
    fixture corpus when it's present locally (see test_real_fixture.py
    for the same skipif convention).
    """
    corpus = load_corpus([REAL_SESSION_A])
    from claude_token_lens.pricing import load_pricing

    pricing = load_pricing()
    model = report.build_report(corpus, pricing, Config(), projects=("session-a",), window="real fixture")

    for rec in model.recommendations:
        for label, value, source_table, row_key in rec.evidence:
            section_key, table_name = source_table.split(".", 1)
            section = next((s for s in model.sections if s.key == section_key), None)
            assert section is not None, f"{rec.id}: no section {section_key!r} for evidence {label!r}"
            table = next((t for t in section.tables if t.name == table_name), None)
            assert table is not None, f"{rec.id}: no table {table_name!r} for evidence {label!r}"
            row = next((row for row in table.rows if row and row[0] == row_key), None)
            assert row is not None, f"{rec.id}: no row {row_key!r} in {source_table} for evidence {label!r}"
