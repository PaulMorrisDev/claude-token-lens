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
