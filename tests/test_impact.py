"""Did it work? Sessions before a change against sessions after it
(``impact``)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from claude_token_lens import impact
from claude_token_lens.change_points import ChangePoint
from claude_token_lens.impact import SessionFacts, _Transcript
from claude_token_lens.units import Units

UNITS = Units(billing_mode="api", currency="USD")
CHANGE = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


def _session(days: float, cost: float, *, startup: int = 20_000, agent_cost: float = 0.0) -> SessionFacts:
    spawns = [("Explore", _Transcript(cost=agent_cost, turns=3, startup_tokens=9000))] if agent_cost else []
    return SessionFacts(
        start=CHANGE + timedelta(days=days),
        main=_Transcript(cost=cost, turns=10, startup_tokens=startup, write_tokens=1000, rebuild_tokens=200),
        spawns=spawns,
    )


def test_measures_follow_the_changed_keys():
    assert [m.key for m in impact.measures_for(ChangePoint(CHANGE, "apply", "x", keys=["model"]))] == [
        "cost_per_turn",
        "cost_per_session",
    ]
    agent = impact.measures_for(ChangePoint(CHANGE, "apply", "x", keys=["Explore: model"]))
    assert [(m.key, m.agent) for m in agent][:2] == [("agent_cost", "Explore"), ("agent_startup", "Explore")]
    ttl = impact.measures_for(ChangePoint(CHANGE, "config", "x", keys=["effective.promptCacheTtl"]))
    assert ttl[0].key == "rebuild_share"
    assert impact.measures_for(ChangePoint(CHANGE, "apply", "x"))[0].key == "startup_tokens"


def test_compare_reports_a_drop_with_counts():
    sessions = [_session(-d, 2.0, agent_cost=1.0) for d in (1, 2, 3)] + [
        _session(d, 1.0, agent_cost=0.5) for d in (0.1, 0.2, 0.3, 0.4)
    ]
    point = ChangePoint(CHANGE, "apply", "Applied profile cheap", keys=["Explore: model"])
    result = impact.compare(point, sessions, UNITS, now=CHANGE + timedelta(days=1))
    assert result["enough"]
    assert (result["before_sessions"], result["after_sessions"]) == (3, 4)
    lead = result["measures"][0]
    assert lead["label"] == "Explore: cost per spawn"
    assert lead["change_pct"] == -50.0 and lead["direction"] == "lower"
    assert result["verdict"].startswith("Explore: cost per spawn fell 50%")


def test_too_few_sessions_after_gives_no_verdict():
    sessions = [_session(-d, 2.0) for d in (1, 2, 3)] + [_session(0.1, 1.0)]
    result = impact.compare(ChangePoint(CHANGE, "apply", "x", keys=["model"]), sessions, UNITS)
    assert not result["enough"]
    assert "1 so far" in result["verdict"]


def test_before_stops_at_the_previous_change_and_after_at_the_next():
    sessions = [_session(-d, 5.0) for d in (4, 5)] + [_session(-d, 2.0) for d in (0.5, 1, 1.5)] + [
        _session(d, 1.0) for d in (0.1, 0.2, 0.3)
    ] + [_session(3, 9.0)]
    points = [
        ChangePoint(CHANGE - timedelta(days=2), "apply", "first"),
        ChangePoint(CHANGE, "apply", "second"),
        ChangePoint(CHANGE + timedelta(days=2), "apply", "third"),
    ]
    newest_first = impact.impact(points, sessions, UNITS)
    second = newest_first[1]
    assert second["change"]["label"] == "second"
    assert (second["before_sessions"], second["after_sessions"]) == (3, 3)
    cost = next(row for row in second["measures"] if row["label"] == "Cost per session")
    assert cost["before"] == "2.00 USD" and cost["after"] == "1.00 USD"


def test_changes_made_together_share_their_before_and_after():
    """One apply that wrote two files gives two change points seconds
    apart; neither cuts the other's comparison to nothing."""
    sessions = [_session(-d, 2.0) for d in (1, 2, 3)] + [_session(d, 1.0) for d in (0.1, 0.2, 0.3)]
    points = [
        ChangePoint(CHANGE, "apply", "first"),
        ChangePoint(CHANGE + timedelta(seconds=1), "apply", "second"),
    ]
    for result in impact.impact(points, sessions, UNITS):
        assert (result["before_sessions"], result["after_sessions"]) == (3, 3), result["change"]["label"]
        assert result["enough"]


def test_quality_compares_the_changed_agents_runs_before_and_after():
    from claude_token_lens import quality

    def runs(errors: int) -> list:
        return [quality.Run(group="Explore", kind="subagent", replies=10, tool_calls=50, tool_errors=errors,
                            cut_off=False)]

    sessions = [_session(-d / 10, 2.0, agent_cost=1.0) for d in range(1, 7)]
    sessions += [_session(d / 10, 1.0, agent_cost=0.5) for d in range(1, 7)]
    for i, session in enumerate(sessions):
        session.runs = runs(1 if i < 6 else 10) + [quality.Run(replies=5)]
    point = ChangePoint(CHANGE, "apply", "Applied profile cheap", keys=["Explore: model"])
    assert impact.quality_groups(point) == ["Explore"]
    assert impact.quality_groups(ChangePoint(CHANGE, "apply", "x", keys=["model"])) == [quality.MAIN]
    group = impact.compare(point, sessions, UNITS, now=CHANGE + timedelta(days=1))["quality"][0]
    assert (group["label"], group["before_runs"], group["after_runs"]) == ("Explore", 6, 6)
    assert group["verdict"].startswith("Quality looks worse: tool calls that failed rose from 2.0% to 20%")
    cost = next(row for row in group["signals"] if row["key"] == "cost")
    assert cost["label_key"] == "no_clear_change"


def test_a_group_with_too_few_runs_is_not_judged():
    from claude_token_lens import quality

    sessions = [_session(-d / 10, 2.0) for d in range(1, 7)] + [_session(0.1, 1.0)]
    for session in sessions:
        session.runs = [quality.Run(replies=5, tool_calls=20)]
    group = impact.compare(ChangePoint(CHANGE, "apply", "x", keys=["model"]), sessions, UNITS)["quality"][0]
    assert group["judged"] is False and group["min_runs"] == quality.MIN_RUNS
    assert "6 before and 1 after" in group["verdict"]
