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


def test_compaction_goal_picks_the_cheapest_window_over_the_current_one():
    out = goals.draft("compaction", _report(), UNITS, effective={})
    [candidate] = out["candidates"]
    assert candidate["value"] == 200000 and candidate["ticked"]
    assert out["whatif"]["total_usd"] == 20.0


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
