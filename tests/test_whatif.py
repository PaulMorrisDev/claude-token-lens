"""What if? Estimates looked up in the report's own tables (``whatif``)."""

from __future__ import annotations

from types import SimpleNamespace as NS

from claude_token_lens import whatif
from claude_token_lens.units import Units

UNITS = Units(billing_mode="api", currency="USD")
PERIOD = "over the last 14 days"


def _table(name: str, rows: list[dict]):
    keys = list(rows[0]) if rows else []
    return NS(name=name, columns=[NS(key=k) for k in keys], rows=[[row[k] for k in keys] for row in rows])


def _model(**extra):
    sections = [
        NS(key="model_swap", tables=[_table("model_swap_by_agent_type", [
            {"agent_type": "top-level", "observed_cost": 100.0, "cost_claude-sonnet-4-5": 60.0, "cost_claude-haiku-4-5": 20.0},
            {"agent_type": "Explore", "observed_cost": 10.0, "cost_claude-sonnet-4-5": 6.0, "cost_claude-haiku-4-5": 2.0},
        ])]),
        NS(key="ttl", tables=[_table("ttl_by_agent_type", [
            {"agent_type": "top-level", "cost_observed": 50.0, "cost_all_5m": 55.0, "cost_all_1h": 45.0},
            {"agent_type": "Explore", "cost_observed": 5.0, "cost_all_5m": 4.0, "cost_all_1h": 6.0},
            {"agent_type": "Plan", "cost_observed": 3.0, "cost_all_5m": 2.0, "cost_all_1h": 3.5},
        ])]),
        NS(key="compaction_sim", tables=[_table("compaction_sim_by_window", [
            {"window": "none", "cost": 100.0, "compactions_per_session": 0.2},
            {"window": "200,000", "cost": 80.0, "compactions_per_session": 1.4},
        ])]),
        NS(key="agent_startup", tables=[_table("agent_startup_breakdown", [
            {"agent_type": "Explore", "claude_md": 5000, "spawns": 100, "write_price": 3.75},
        ])]),
        NS(key="agents", tables=[_table("topology_effort_by_agent_type", [
            {"agent_type": "top-level", "thinking_share": 40.0},
        ])]),
    ]
    context_files = {"skills": [
        {"name": "dataviz", "listing_cost_usd": 1.0},
        {"name": "engineering:review", "listing_cost_usd": 0.5},
    ]}
    return NS(sections=sections, context_files=extra.get("context_files", context_files))


def _estimate(settings=None, agents=None, **kw):
    return whatif.estimate(settings or {}, agents or {}, _model(), UNITS, period=PERIOD, **kw)


def test_a_cheaper_main_model_is_repriced_from_the_model_swap_table():
    [row] = _estimate({"model": "sonnet"})["rows"]
    assert row["saving_usd"] == 40.0
    # E3/EST-P1: a model reprice is a ceiling (same tokens, new rate), not
    # a genuine simulation like autoCompactWindow/cache TTL below -- it
    # can't capture a different model needing more or fewer replies.
    assert row["fidelity"] == "ceiling"
    assert row["effect_text"] == f"Saves 40.00 USD {PERIOD}"


def test_model_ceiling_fidelity_text_is_distinct_from_simulated():
    [row] = _estimate({"model": "sonnet"})["rows"]
    row["fidelity_text"] = whatif.FIDELITY_TEXT.get(row["fidelity"], "")
    assert row["fidelity_text"].startswith("Ceiling:")
    assert row["fidelity_text"] != whatif.FIDELITY_TEXT["simulated"]


def test_an_agent_model_uses_that_agent_row():
    [row] = _estimate(agents={"Explore": {"model": "haiku"}})["rows"]
    assert (row["agent"], row["saving_usd"]) == ("Explore", 8.0)


def test_subagent_ttl_sums_every_subagent_and_can_cost_more():
    [row] = _estimate({"subagentPromptCacheTtl": "1h"})["rows"]
    assert row["saving_usd"] == -1.5
    assert row["effect_text"] == f"Costs 1.50 USD more {PERIOD}"


def test_compaction_compares_against_the_current_window():
    [row] = _estimate({"autoCompactWindow": 200000})["rows"]
    assert row["saving_usd"] == 20.0
    [unknown] = _estimate({"autoCompactWindow": 123})["rows"]
    assert unknown["fidelity"] == "none" and "200000" in unknown["basis"]


def test_omit_claude_md_is_measured_per_spawn():
    [row] = _estimate(agents={"Explore": {"omitClaudeMd": True}})["rows"]
    assert row["fidelity"] == "measured"
    assert row["saving_usd"] == 5000 * 100 * 3.75 / 1_000_000


def test_skills_and_plugins_use_the_listing_cost():
    rows = _estimate({"skillOverrides": {"dataviz": "off"}, "enabledPlugins": {"engineering@marketplace": False}})["rows"]
    assert [r["saving_usd"] for r in rows] == [1.0, 0.5]


def test_effort_is_never_estimated_but_says_how_much_was_thinking():
    [row] = _estimate({"effortLevel": "medium"})["rows"]
    assert row["saving_usd"] is None and row["effect_text"] == "Not estimated"
    assert "40%" in row["basis"]


def test_total_counts_only_estimated_rows_and_notes_overlap():
    out = _estimate({"model": "sonnet", "autoCompactWindow": 200000, "effortLevel": "low"})
    assert out["total_usd"] == 60.0
    assert (out["estimated"], out["not_estimated"]) == (2, 1)
    assert out["total_note"]
    assert _estimate({"model": "sonnet"})["total_note"] == ""


def test_unknown_keys_and_empty_reports_are_not_estimated():
    out = whatif.estimate({"model": "sonnet", "cleanupPeriodDays": 7}, {}, NS(sections=[]), UNITS)
    assert [r["fidelity"] for r in out["rows"]] == ["none", "none"]
    assert out["total_text"] == ""


# -- EST-P6: calibration -----------------------------------------------------


def test_a_row_with_no_calibration_entry_is_untouched():
    [row] = _estimate({"model": "sonnet"}, calibration={(None, "promptCacheTtl"): 2.0})["rows"]
    assert row["saving_usd"] == 40.0
    assert row["fidelity"] == "ceiling"
    assert row["uncalibrated_usd"] is None and row["uncalibrated_fidelity"] is None


def test_a_matching_calibration_scales_the_row_and_changes_its_fidelity():
    [row] = _estimate({"model": "sonnet"}, calibration={(None, "model"): 0.5})["rows"]
    assert row["saving_usd"] == 20.0
    assert row["fidelity"] == "calibrated"
    assert row["fidelity_text"] == whatif.FIDELITY_TEXT["calibrated"]
    # The pre-calibration value survives, for a caller (route_whatif's
    # "log": true) that must log the raw estimate, not a calibrated one.
    assert row["uncalibrated_usd"] == 40.0
    assert row["uncalibrated_fidelity"] == "ceiling"


def test_calibration_keys_on_agent_and_the_raw_settings_key():
    [row] = _estimate(agents={"Explore": {"model": "haiku"}}, calibration={("Explore", "model"): 1.5})["rows"]
    assert row["agent"] == "Explore" and row["key"] == "model"
    assert row["saving_usd"] == row["uncalibrated_usd"] * 1.5
    assert row["fidelity"] == "calibrated"


def test_calibration_never_invents_a_saving_for_a_row_that_had_none():
    [row] = _estimate({"effortLevel": "medium"}, calibration={(None, "effortLevel"): 3.0})["rows"]
    assert row["saving_usd"] is None
    assert row["fidelity"] == "none"
    assert row["uncalibrated_usd"] is None
