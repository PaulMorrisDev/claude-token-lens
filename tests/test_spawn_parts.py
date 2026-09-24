"""Tests for the per-part subagent startup recommendations
(``recommend._rule_spawn_parts``), the fixes built from them
(``fixes.py``: explainer, command, prompt) and billing-mode amounts
(``units.py``)."""

from __future__ import annotations

import shlex

import pytest

from claude_token_lens import cli, context_budget, fixes
from claude_token_lens.config import Config
from claude_token_lens.elasticity import ElasticityStats, FitResult
from claude_token_lens.model import (
    Column,
    Diagnostics,
    PricingMeta,
    Recommendation,
    ReportMeta,
    ReportModel,
    Section,
    SettingChange,
    Table,
)
from claude_token_lens.recommend import recommend
from claude_token_lens.snapshots import Snapshot
from claude_token_lens.units import NO_LIMIT_SHARE_HINT, Units

PRICE = 3.75  # USD per million tokens written (a Sonnet-class 5m cache write)


def _acc(agent_type, *, spawns=10, claude_md=3000.0, skills=800.0, task=500.0, tool_lists=1500.0, **counts):
    acc = context_budget._AgentStartupAcc(agent_type=agent_type, spawns=spawns)
    acc.startup_tokens = [20_000] * spawns
    acc.parts = {part: [0.0] * spawns for part in context_budget.STARTUP_PARTS}
    acc.parts["claude_md"] = [claude_md] * spawns
    acc.parts["skills_listing"] = [skills] * spawns
    acc.parts["task_prompt"] = [task] * spawns
    acc.parts["tool_lists"] = [tool_lists] * spawns
    acc.write_prices = [PRICE] * spawns
    acc.skills_listed_spawns = counts.get("skills_listed", spawns)
    acc.skills_used_spawns = counts.get("skills_used", spawns)
    acc.mcp_offered_spawns = counts.get("mcp_offered", 0)
    acc.mcp_used_spawns = counts.get("mcp_used", 0)
    acc.read_only_spawns = counts.get("read_only", 0)
    return acc


def _report(*accs) -> ReportModel:
    stats = context_budget.ContextBudgetStats()
    for acc in accs:
        stats.agents[acc.agent_type] = acc
    overview = Section(
        key="overview",
        title="Overview",
        tables=[
            Table(
                name="totals",
                columns=[Column(key="metric", label="Metric"), Column(key="value", label="Value")],
                rows=[["sessions", 10], ["priced_turns", 400], ["cache_read_cost_share_pct", 10.0]],
            )
        ],
    )
    return ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)),
        sections=[overview, context_budget.build_startup_section(stats)],
        diagnostics=Diagnostics(lines=1000),
    )


def _snapshot(agents: dict) -> Snapshot:
    return Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"agents": agents})


def _recs(report, *, snapshot=None, units=None):
    recs = recommend(report, config=Config(), archetype=None, snapshot=snapshot, units=units)
    fixes.attach_fixes(recs)
    return recs


# -- the rules -----------------------------------------------------------


def test_large_claude_md_on_a_custom_agent_suggests_omit_claude_md():
    snap = _snapshot({"reviewer": {"source": "user"}})
    recs = _recs(_report(_acc("reviewer")), snapshot=snap, units=Units())
    rec = next(r for r in recs if r.id == "spawn-claude-md")
    assert rec.agent_type == "reviewer"
    assert rec.scope == "user"
    change = rec.changes[0]
    assert (change.target, change.key, change.agent, change.value) == ("agent", "omitClaudeMd", "reviewer", True)
    assert change.current is None  # snapshot present, key not set
    # 3,000 tokens x 10 spawns x 3.75 USD per million = 0.1125 USD.
    assert rec.estimated_saving == "0.11 USD across the spawns in this report."
    assert rec.fixes[0]["command"] == (
        "claude-token-lens apply --set omitClaudeMd=true --agent reviewer --scope user --dry-run"
    )
    # The rules the agent needs are moved into its own prompt before the
    # flag is set; the command, which only sets the flag, says so.
    prompt = rec.fixes[0]["prompt"]
    assert prompt.index("List the rules reviewer needs") < prompt.index("set omitClaudeMd to true")
    assert "~/.claude/agents/reviewer.md" in prompt
    assert "only sets the flag" in rec.fixes[0]["command_warning"]
    # The generic spawn-cost advice is not repeated for a covered agent type.
    assert not any(r.id == "spawn-cost" and r.agent_type == "reviewer" for r in recs)


def test_explore_and_plan_never_get_omit_claude_md():
    recs = _recs(_report(_acc("Explore"), _acc("Plan")))
    assert not any(r.id == "spawn-claude-md" for r in recs)


def test_builtin_agent_gets_a_prompt_to_create_an_override_but_no_command():
    recs = _recs(_report(_acc("general-purpose")))
    rec = next(r for r in recs if r.id == "spawn-claude-md")
    assert rec.lever is None
    assert rec.changes[0].new_agent_file
    fix = rec.fixes[0]
    assert fix["command"] is None
    assert "same name" in fix["prompt"]
    assert "unknown (no config snapshot yet)" in dict(map(tuple, fix["explainer"]))["Now and after"]


def test_workflow_subagents_get_no_override_advice():
    recs = _recs(_report(_acc("workflow-subagent", skills_used=0, mcp_offered=10)))
    assert not any(r.agent_type == "workflow-subagent" and r.changes for r in recs)


def test_unused_skills_are_unconfirmed_and_prompt_only_for_an_existing_file():
    snap = _snapshot({"reviewer": {"source": "project", "disallowedTools": ["Bash"]}})
    recs = _recs(_report(_acc("reviewer", claude_md=0.0, skills_used=0)), snapshot=snap, units=Units())
    rec = next(r for r in recs if r.id == "spawn-unused-skills")
    change = rec.changes[0]
    assert change.unconfirmed and change.value is None and change.current == ["Bash"]
    assert rec.scope == "repo"
    assert rec.fixes[0]["command"] is None
    assert "docs don't confirm" in dict(map(tuple, rec.fixes[0]["explainer"]))["Expected effect"]


def test_unused_mcp_and_read_only_tools():
    snap = _snapshot({"searcher": {"source": "user"}})
    acc = _acc("searcher", mcp_offered=10, mcp_used=0, read_only=10)
    recs = _recs(_report(acc), snapshot=snap)
    ids = {r.id for r in recs if r.agent_type == "searcher"}
    assert {"spawn-unused-mcp", "spawn-read-only-tools"} <= ids
    tools_rec = next(r for r in recs if r.id == "spawn-read-only-tools")
    assert "Read" in tools_rec.changes[0].value and "Edit" not in tools_rec.changes[0].value
    assert tools_rec.fixes[0]["command_warning"] == ""


def test_below_min_spawns_nothing_fires():
    recs = _recs(_report(_acc("reviewer", spawns=3, skills_used=0, mcp_offered=3)))
    assert not any(r.id.startswith("spawn-") and r.id != "spawn-cost" for r in recs)


def test_long_task_prompt_is_workflow_advice():
    recs = _recs(_report(_acc("reviewer", claude_md=0.0, task=6000.0)))
    rec = next(r for r in recs if r.id == "spawn-task-prompt")
    assert rec.category == "workflow" and not rec.changes and rec.fixes == []


def test_shared_claude_md_comes_with_a_prompt():
    recs = _recs(_report(_acc("a", claude_md=0.0), _acc("b", claude_md=0.0)))
    # No CLAUDE.md by source was recorded, so nothing is shared.
    assert not any(r.id == "spawn-shared-claude-md" for r in recs)

    one, two = _acc("a"), _acc("b")
    for acc in (one, two):
        acc.claude_md_by_source = {"Project": [3000.0] * acc.spawns}
    recs = _recs(_report(one, two))
    rec = next(r for r in recs if r.id == "spawn-shared-claude-md")
    assert rec.fixes and rec.fixes[0]["command"] is None
    assert "move" in rec.fixes[0]["prompt"]


# -- fix contract ------------------------------------------------------------


def _all_fixes():
    snap = _snapshot({"reviewer": {"source": "user"}, "searcher": {"source": "project"}})
    report = _report(
        _acc("reviewer", skills_used=0, mcp_offered=10, read_only=10, task=6000.0),
        _acc("searcher", skills_used=0, read_only=10),
        _acc("general-purpose", skills_used=0),
    )
    recs = _recs(report, snapshot=snap, units=Units(billing_mode="subscription"))
    return [(rec, fix) for rec in recs for fix in rec.fixes]


def test_every_setting_fix_has_the_six_part_explainer():
    pairs = [(rec, fix) for rec, fix in _all_fixes() if fix["key"]]
    assert pairs
    for rec, fix in pairs:
        headings = [heading for heading, _ in fix["explainer"]]
        assert headings == [
            "What this setting controls",
            "Now and after",
            "Where and who it affects",
            "Expected effect",
            "Trade-off",
            "How to undo it",
        ], rec.id
        assert all(text.strip() for _, text in fix["explainer"])


def test_every_command_parses_with_the_real_parser():
    commands = [fix["command"] for _, fix in _all_fixes() if fix["command"]]
    assert commands
    parser = cli._make_parser()
    for command in commands:
        argv = shlex.split(command)
        assert argv[0] == "claude-token-lens"
        args = parser.parse_args(argv[1:])
        assert args.dry_run and args.set_values
        profile, err = cli._one_off_profile(args.set_values, args.agent)
        assert err is None, (command, err)


def test_prompts_are_self_contained_and_carry_no_absolute_paths():
    for rec, fix in _all_fixes():
        prompt = fix["prompt"]
        assert "permission" in prompt
        assert ":\\" not in prompt and "/Users/" not in prompt and "/home/" not in prompt, rec.id
        if fix["key"]:
            assert fix["key"] in prompt


def test_subscription_without_readings_says_list_price_and_how_to_get_the_share():
    rec = next(rec for rec, _ in _all_fixes() if rec.id == "spawn-claude-md")
    assert "list-price equivalent" in rec.estimated_saving
    assert NO_LIMIT_SHARE_HINT in rec.saving_basis


# -- units -------------------------------------------------------------------


def _fitted(slope: float) -> ElasticityStats:
    stats = ElasticityStats()
    stats.fits = {"seven_day": {"usd": FitResult(window="seven_day", metric="usd", slope=slope, accepted=True)}}
    return stats


def test_units_api_mode_is_dollars():
    amount = Units().money(12.5)
    assert amount.primary == "12.50 USD" and amount.basis == "at list price"


def test_units_subscription_uses_the_weekly_share_when_fitted():
    amount = Units(billing_mode="subscription", elasticity=_fitted(0.4)).money(10.0, period="per week")
    assert amount.primary == "about 4.0% of your weekly usage limit per week"
    assert amount.secondary == "10.00 USD list-price equivalent"
    small = Units(billing_mode="subscription", elasticity=_fitted(0.004)).money(10.0)
    assert small.primary.startswith("about 0.04%")


def test_units_subscription_falls_back_without_an_accepted_fit():
    stats = _fitted(0.4)
    stats.fits["seven_day"]["usd"].accepted = False
    amount = Units(billing_mode="subscription", elasticity=stats).money(3.0)
    assert amount.primary == "3.00 USD list-price equivalent"
    assert amount.basis == NO_LIMIT_SHARE_HINT


@pytest.mark.parametrize("value", [0, -1.0, float("nan"), None])
def test_units_refuses_non_positive_amounts(value):
    assert Units().money(value) is None


def test_fixes_for_workflow_advice_without_a_prompt_are_empty():
    assert fixes.build_fixes(Recommendation(id="cache-read-dominance")) == []


def test_command_for_repo_scope_names_the_project_dir():
    change = SettingChange(target="agent", key="omitClaudeMd", agent="x", value=True)
    assert fixes.command_for(change, "repo").endswith("--scope repo --project-dir . --dry-run")


# -- runs that said whether they used CLAUDE.md (metrics capture) ------------


def _with_rules(report, agent_type: str, used: int, unused: int):
    from claude_token_lens import habits

    runs = [habits.AgentFact(session_id="s", agent_type=agent_type, week="", cost=1.0, rules=word)
            for word, n in (("used", used), ("unused", unused)) for _ in range(n)]
    report.sections.append(habits.section_from(habits.Habits(agents=runs)))
    return report


def test_omit_claude_md_is_held_back_when_most_runs_said_they_used_it():
    snap = _snapshot({"reviewer": {"source": "user"}})
    recs = _recs(_with_rules(_report(_acc("reviewer")), "reviewer", used=2, unused=1), snapshot=snap)
    assert not any(r.id == "spawn-claude-md" for r in recs)


def test_runs_that_said_they_did_not_use_claude_md_are_cited():
    snap = _snapshot({"reviewer": {"source": "user"}})
    report = _with_rules(_report(_acc("reviewer")), "reviewer", used=1, unused=3)
    rec = next(r for r in _recs(report, snapshot=snap) if r.id == "spawn-claude-md")
    assert ("Runs that said they didn't use CLAUDE.md", 3, "habits.habits_agents", "reviewer") in rec.evidence
    assert "3 of the 4 runs that said, said they didn't use your CLAUDE.md." in rec.why
