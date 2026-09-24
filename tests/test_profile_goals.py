"""Create a profile from a goal (``profiles.goals``)."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from claude_token_lens.profiles import goals
from claude_token_lens.units import Units

from test_whatif import _model

UNITS = Units(billing_mode="api", currency="USD")


def _report(recommendations=()):
    model = _model()
    model.recommendations = list(recommendations)
    return model


def _change(key, agent, value):
    return NS(key=key, agent=agent, value=value)


def test_recommendations_goal_ticks_everything_but_the_main_model():
    rec = NS(title="A cheaper model", changes=[
        _change("model", None, "sonnet"),
        _change("model", "reviewer", "sonnet"),
        _change("tools", "reviewer", None),
    ])
    out = goals.draft("recommendations", _report([rec]), UNITS, effective={"model": "opus"})
    ticks = {(c["agent"], c["key"]): c["ticked"] for c in out["candidates"]}
    assert ticks == {(None, "model"): False, ("reviewer", "model"): True}
    main = out["candidates"][0]
    assert main["now"] == "opus" and main["tradeoff"]
    assert out["profile"] == {"settings": {}, "agents": {"reviewer": {"model": "sonnet"}}}


def test_models_goal_reads_the_cheapest_alternative_per_agent():
    model = _report()
    [table] = model.sections[0].tables
    table.columns.append(NS(key="best_cheaper_alternative_model"))
    table.columns.append(NS(key="saving_pct"))
    table.rows[0] += ["claude-sonnet-4-5", 40.0]
    table.rows[1] += ["claude-haiku-4-5-20251001", 80.0]
    out = goals.draft("models", model, UNITS)
    rows = {(c["agent"], c["value"], c["ticked"]) for c in out["candidates"]}
    assert rows == {(None, "sonnet", False), ("Explore", "haiku", True)}
    explore = next(c for c in out["candidates"] if c["agent"] == "Explore")
    assert explore["estimate"]["saving_usd"] == 8.0


def test_models_goal_skips_a_model_the_quality_check_found_worse():
    model = _report()
    [table] = model.sections[0].tables
    table.columns.append(NS(key="best_cheaper_alternative_model"))
    table.columns.append(NS(key="saving_pct"))
    table.rows[0] += ["claude-sonnet-4-5", 40.0]
    table.rows[1] += ["claude-haiku-4-5-20251001", 80.0]
    model.sections.append(NS(key="quality", tables=[NS(
        name="quality_by_setup",
        columns=[NS(key=k) for k in ("agent_type", "model", "setup_verdict", "compared_model")],
        rows=[["Explore", "claude-haiku-4-5-20251001", "worse", "claude-sonnet-4-5"]],
    )]))
    out = goals.draft("models", model, UNITS)
    assert [(c["agent"], c["value"]) for c in out["candidates"]] == [(None, "sonnet")]


def test_compaction_goal_picks_the_cheapest_window_over_the_current_one():
    out = goals.draft("compaction", _report(), UNITS, effective={})
    [candidate] = out["candidates"]
    assert candidate["value"] == 200000 and candidate["ticked"]
    assert out["whatif"]["total_usd"] == 20.0


def test_compaction_goal_never_offers_a_window_that_summarises_more_than_twice_a_session():
    model = _report()
    [table] = next(s for s in model.sections if s.key == "compaction_sim").tables
    table.rows.append(["100,000", 50.0, 3.5])  # window, cost, compactions_per_session
    [candidate] = goals.draft("compaction", model, UNITS, effective={})["candidates"]
    assert candidate["value"] == 200000


def test_thinking_is_offered_unticked_and_settings_already_in_effect_are_skipped():
    out = goals.draft("thinking", _report(), UNITS)
    [candidate] = out["candidates"]
    assert (candidate["key"], candidate["value"], candidate["ticked"]) == ("effortLevel", "medium", False)
    assert goals.draft("thinking", _report(), UNITS, effective={"effortLevel": "medium"})["candidates"] == []


def test_agents_claude_code_starts_itself_are_never_proposed():
    rec = NS(title="x", changes=[_change("model", "workflow-subagent", "haiku"), _change("omitClaudeMd", "Explore", True)])
    assert goals.draft("recommendations", _report([rec]), UNITS)["candidates"] == []


def test_current_goal_hands_over_to_save_current_settings_and_unknown_goals_raise():
    out = goals.draft("current", _report(), UNITS)
    assert out["from_current"] and out["candidates"] == []
    with pytest.raises(KeyError):
        goals.draft("everything", _report(), UNITS)


def test_an_agent_setting_already_in_effect_is_skipped_under_its_snapshot_field_name():
    """effective_agents names the cache lifetime experimental_cache_ttl and
    models by id, so the lookup must translate both before comparing."""
    rec = NS(title="x", changes=[
        _change("experimental.cacheTtl", "verification-runner", "1h"),
        _change("model", "verification-runner", "haiku"),
    ])
    agents = {"verification-runner": {"source": "project", "experimental_cache_ttl": "1h",
                                      "model": "claude-haiku-4-5-20251001"}}
    out = goals.draft("recommendations", _report([rec]), UNITS, effective_agents=agents)
    assert out["candidates"] == []


def test_models_goal_skips_a_model_the_agent_was_often_retried_from():
    model = _report()
    [table] = model.sections[0].tables
    table.columns.append(NS(key="best_cheaper_alternative_model"))
    table.columns.append(NS(key="saving_pct"))
    table.rows[0] += ["claude-sonnet-4-5", 40.0]
    table.rows[1] += ["claude-haiku-4-5-20251001", 80.0]
    model.sections.append(NS(key="quality", tables=[NS(
        name="quality_retried",
        columns=[NS(key=k) for k in ("agent_type", "model", "runs", "retried", "retried_on")],
        rows=[["Explore", "claude-haiku-4-5-20251001", 4, 1, "claude-sonnet-5"]],
    )]))
    out = goals.draft("models", model, UNITS)
    assert [(c["agent"], c["value"]) for c in out["candidates"]] == [(None, "sonnet")]


# -- a profile for one kind of task (metrics capture) ------------------------------

from test_whatif import _table  # noqa: E402

_SETUP_KEYS = ("task", "level", "model", "effort", "cycles", "avg_cost", "ok_pct", "rated", "verdict", "saving_pct")


def _with_setups(*rows):
    model = _report()
    model.sections.append(NS(key="habits", tables=[
        NS(name="habits_setups", columns=[NS(key=k) for k in _SETUP_KEYS], rows=[list(r) for r in rows]),
    ]))
    return model


_SETUPS = (
    ("chat", "all", "opus", "high", 9, 1.0, 90.0, 0, "usual", None),
    ("bugfix", "all", "opus", "high", 12, 2.0, 80.0, 3, "usual", None),
    ("bugfix", "all", "sonnet", "medium", 6, 0.5, 83.0, 1, "cheaper", 75.0),
    ("bugfix", "normal", "sonnet", "medium", 6, 0.5, 83.0, 1, "cheaper", 75.0),
)


def test_the_tasks_goal_drafts_the_cheaper_setup_for_the_first_task_that_has_one():
    out = goals.draft("tasks", _with_setups(*_SETUPS), UNITS, effective={"model": "opus", "effortLevel": "high"})
    assert out["tasks"] == ["chat", "bugfix"] and out["task"] == "bugfix"
    by_key = {c["key"]: c for c in out["candidates"]}
    # The main session's model stays yours to decide; the effort is ticked.
    assert (by_key["model"]["value"], by_key["model"]["ticked"]) == ("sonnet", False)
    assert (by_key["effortLevel"]["value"], by_key["effortLevel"]["ticked"]) == ("medium", True)
    assert by_key["model"]["evidence"].startswith(
        "For bugfix work, sonnet at medium effort cost 75% less a message than your usual opus at high effort"
    )
    assert "went well 83% of the time against 80% (6 and 12 messages)" in by_key["model"]["evidence"]
    assert out["note"].startswith("Save it, then launch Claude with it when you start bugfix work.")
    assert "implementation-heavy" in out["note"]
    assert out["profile"] == {"settings": {"effortLevel": "medium"}, "agents": {}}


def test_a_task_with_no_cheaper_setup_says_what_your_usual_one_is():
    out = goals.draft("tasks", _with_setups(*_SETUPS), UNITS, task="chat")
    assert out["task"] == "chat" and out["candidates"] == []
    assert out["note"].startswith("Your usual setup for chat work is opus at high effort. No cheaper setup")
    assert "interactive-chat" in out["note"]


def test_the_tasks_goal_without_capture_says_how_to_get_the_data():
    out = goals.draft("tasks", _report(), UNITS)
    assert out["tasks"] == [] and out["task"] is None and out["candidates"] == []
    assert "metrics capture" in out["note"]
    # Other goals carry no task.
    other = goals.draft("cache", _report(), UNITS)
    assert (other["tasks"], other["task"], other["note"]) == ([], None, None)


def test_an_unknown_task_falls_back_to_the_first_with_a_cheaper_setup():
    assert goals.draft("tasks", _with_setups(*_SETUPS), UNITS, task="docs")["task"] == "bugfix"


def _with_agent_reports(used, unused):
    model = _report()
    model.sections = [s for s in model.sections if s.key != "agent_startup"]
    model.sections.append(NS(key="agent_startup", tables=[_table("agent_startup_breakdown", [
        {"agent_type": "reviewer", "claude_md": 4000, "spawns": 20, "write_price": 3.75},
    ])]))
    model.sections.append(NS(key="habits", tables=[_table("habits_agents", [
        {"agent_type": "reviewer", "runs": used + unused, "rules_used": used, "rules_unused": unused},
    ])]))
    return model


def test_agents_that_said_they_did_not_use_claude_md_get_it_left_out_ticked():
    [candidate] = [c for c in goals.draft("subagents", _with_agent_reports(1, 3), UNITS)["candidates"]
                   if c["key"] == "omitClaudeMd"]
    assert candidate["agent"] == "reviewer" and candidate["ticked"]
    assert candidate["evidence"].endswith("3 of the 4 runs that said, said they didn't use it.")


def test_agents_that_said_they_used_claude_md_keep_it():
    out = goals.draft("subagents", _with_agent_reports(3, 1), UNITS)
    assert not [c for c in out["candidates"] if c["key"] == "omitClaudeMd"]


def test_without_reports_claude_md_is_offered_unticked():
    [candidate] = [c for c in goals.draft("subagents", _with_agent_reports(0, 0), UNITS)["candidates"]
                   if c["key"] == "omitClaudeMd"]
    assert not candidate["ticked"] and "Not ticked" in candidate["evidence"]
