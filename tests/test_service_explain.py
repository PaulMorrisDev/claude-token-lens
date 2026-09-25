"""Tests for ``service/explain.py``: the "why was this session
expensive?" sentences."""

from __future__ import annotations

from claudeglass.pricing import load_pricing
from claudeglass.service.explain import cost_split, explain_session
from claudeglass.units import Units


def _parts(**overrides):
    parts = {
        "by_agent": [
            {"kind": "top-level", "agent_type": None, "runs": 1, "turns": 10, "cost": 2.0},
            {"kind": "subagent", "agent_type": "reviewer", "runs": 3, "turns": 30, "cost": 6.0},
            {"kind": "subagent", "agent_type": "Explore", "runs": 1, "turns": 5, "cost": 2.0},
        ],
        "by_model": [
            {
                "model": "claude-sonnet-5",
                "input_tokens": 1_000,
                "cache_creation_tokens": 100_000,
                "cc_5m": 100_000,
                "cc_1h": 0,
                "cache_read_tokens": 5_000_000,
                "output_tokens": 20_000,
            }
        ],
        "rebuilds": [
            {"signature": "full-expiry", "turns": 3, "tokens": 90_000},
            {"signature": "prefix-invalidated", "turns": 1, "tokens": 10_000},
        ],
        "compactions": 2,
    }
    parts.update(overrides)
    return parts


def test_explain_session_names_the_leading_cost_subagents_and_rebuilds():
    detail = {"total_cost": 10.0, "total_tokens": 5_121_000}
    result = explain_session(detail, _parts(), load_pricing(), Units(), median_cost=2.0)
    assert result["headline"] == "This session cost 10.00 USD over 45 replies (5,121,000 tokens)."
    text = " ".join(result["sentences"])
    assert "5.0 times your typical session" in text
    assert "Most of the cost went on re-reading the conversation from the cache" in text
    assert "4 subagent runs made 80% of the cost; the costliest type was reviewer (60%)" in text
    assert "rebuilt 4 times (100,000 tokens written again), most often because the cache had expired" in text
    assert "summarised the conversation 2 times" in text


def test_the_costliest_type_sums_its_runs_under_a_workflow_too():
    by_agent = [
        {"kind": "top-level", "agent_type": None, "runs": 1, "turns": 10, "cost": 3.0},
        {"kind": "subagent", "agent_type": "Explore", "runs": 1, "turns": 5, "cost": 3.0},
        {"kind": "subagent", "agent_type": "reviewer", "runs": 1, "turns": 5, "cost": 2.0},
        {"kind": "workflow-agent", "agent_type": "reviewer", "runs": 2, "turns": 10, "cost": 2.0},
    ]
    result = explain_session({"total_cost": 10.0}, _parts(by_agent=by_agent), load_pricing(), Units())
    text = " ".join(result["sentences"])
    assert "4 subagent runs made 70% of the cost; the costliest type was reviewer (40%)" in text


def test_subscription_headline_is_a_list_price_equivalent():
    result = explain_session({"total_cost": 1.0}, _parts(), load_pricing(), Units(billing_mode="subscription"))
    assert "list-price equivalent" in result["headline"]


def test_cost_split_skips_unknown_models_and_sums_to_100():
    split = cost_split(_parts()["by_model"] + [{"model": "not-a-model", "output_tokens": 10**9}], load_pricing())
    assert round(sum(row["share_pct"] for row in split)) == 100
    assert [row["part"] for row in split] == ["cache_read", "cache_write", "output", "input"]
