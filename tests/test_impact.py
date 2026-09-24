"""Did it work? Sessions before a change against sessions after it
(``impact``)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from claude_token_lens import impact
from claude_token_lens.change_points import ChangePoint
from claude_token_lens.corpus import load_corpus
from claude_token_lens.impact import Measure, SessionFacts, _Transcript
from claude_token_lens.pricing import load_pricing
from claude_token_lens.units import Units

from helpers import turn_line, write_jsonl

UNITS = Units(billing_mode="api", currency="USD")
CHANGE = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


def _session(days: float, cost: float, *, startup: int = 20_000, agent_cost: float = 0.0) -> SessionFacts:
    spawns = [("Explore", _Transcript(cost=agent_cost, turns=3, startup_tokens=9000))] if agent_cost else []
    return SessionFacts(
        start=CHANGE + timedelta(days=days),
        main=_Transcript(cost=cost, turns=10, startup_tokens=startup, write_tokens=1000, rebuild_tokens=200),
        spawns=spawns,
    )


def _tasked(days: float, cost: float, task: str) -> SessionFacts:
    facts = _session(days, cost)
    facts.task = task
    return facts


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


# -- metrics capture changes ---------------------------------------------------


def _captured(days: float, chars: int, messages: int, tagged: int) -> SessionFacts:
    return SessionFacts(
        start=CHANGE + timedelta(days=days),
        main=_Transcript(cost=1.0, turns=messages, capture_chars=chars),
        spawns=[("Explore", _Transcript(cost=0.1, turns=2, capture_chars=chars // 2))],
        messages=messages,
        tagged=tagged,
    )


def test_a_capture_change_is_measured_by_what_capture_adds_and_how_much_was_tagged():
    point = ChangePoint(CHANGE, "capture", "Turned metrics capture on: Essentials", keys=["capture.level"])
    assert [m.key for m in impact.measures_for(point)] == ["capture_tokens", "tagged_share", "cost_per_session"]
    after = [_captured(d, 800, 4, 3) for d in (0.1, 0.2, 0.3)]
    # 800 characters in the main session and 400 in its agent: 300 tokens.
    assert impact._value(impact._CAPTURE, after) == (300.0, 3)
    assert impact._value(impact._TAGGED, after) == (75.0, 3)
    sessions = [_captured(-d, 0, 4, 0) for d in (1, 2, 3)] + after
    result = impact.compare(point, sessions, UNITS, now=CHANGE + timedelta(days=1))
    assert [m["label"] for m in result["measures"]][:2] == [
        "Metrics capture notes and tags per session",
        "Messages Claude tagged",
    ]


# -- EST-P3: task/purpose/mode, the ratio test, and stratification -------------


def test_session_facts_populates_session_id_purpose_and_mode(tmp_path):
    """session_facts() classifies each session standalone (SessionBundle
    has no pre-built classification) -- session_id from the transcript,
    purpose/mode from classify.classify_session, task left None without
    at least two agreeing capture tags."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    write_jsonl(
        project_dir / "session-abc.jsonl",
        [turn_line(timestamp=ts) for ts in ("2026-09-18T12:00:00.000Z", "2026-09-18T12:05:00.000Z")],
    )
    corpus = load_corpus([project_dir])
    facts = impact.session_facts(corpus, load_pricing())
    assert len(facts) == 1
    assert facts[0].session_id == "session-abc"
    assert facts[0].purpose and facts[0].mode
    assert facts[0].task is None


def test_stratum_prefers_task_then_purpose_then_a_catch_all():
    facts = _session(0, 1.0)
    assert impact.stratum(facts) == "(unspecified)"
    facts.purpose = "refactor"
    assert impact.stratum(facts) == "refactor"
    facts.task = "test"
    assert impact.stratum(facts) == "test"


def test_stratified_after_estimate_matches_before_task_mix():
    """"Before" is all "code" work. "After" mixes a little more "code"
    work with several much pricier "review" sessions -- a shift in the
    kind of work, not a real cost change. The pooled after-average reads
    that mix shift as a huge rise; reweighted to before's all-"code" mix
    (EST-P3), the "review" sessions (0% of before) drop out and the
    estimate reflects "code" alone, unchanged."""
    before = [_tasked(-d, 2.0, "code") for d in (1, 2, 3)]
    after = [_tasked(d, 1.0, "code") for d in (0.1, 0.2)] + [
        _tasked(d, 100.0, "review") for d in (0.3, 0.4, 0.5, 0.6, 0.7)
    ]
    pooled = impact._ratio_estimate(impact._pairs(impact._COST, after))
    stratified = impact._stratified_estimate(impact._COST, before, after)
    assert pooled.value > 50.0
    assert stratified.value == 1.0


def test_ratio_test_flags_a_clear_drop_and_leaves_noise_unlabelled():
    before_clear = [_session(-d, 2.0) for d in (1, 2, 3)]
    after_clear = [_session(d, 1.0) for d in (0.1, 0.2, 0.3, 0.4)]
    clear_row = impact._measure_row(impact._COST, before_clear, after_clear, UNITS)
    assert clear_row["p"] == 0.0
    impact._label_rows([clear_row])
    assert clear_row["label_key"] == "lower"
    assert clear_row["label_text"] == "Lower"

    before_noisy = [_session(-1, 1.0), _session(-2, 5.0), _session(-3, 3.0)]
    after_noisy = [_session(1, 2.0), _session(2, 6.0), _session(3, 4.0)]
    noisy_row = impact._measure_row(impact._COST, before_noisy, after_noisy, UNITS)
    impact._label_rows([noisy_row])
    assert noisy_row["label_key"] == "no_clear_change"


def test_ratio_test_needs_enough_sessions_per_row_not_just_overall():
    """An agent-specific measure can have too few of its own data points
    to test even when the overall session counts clear MIN_SESSIONS."""
    before = [_session(-d, 2.0, agent_cost=1.0 if d == 1 else 0.0) for d in (1, 2, 3)]
    after = [_session(d, 1.0, agent_cost=1.0 if d == 0.1 else 0.0) for d in (0.1, 0.2, 0.3)]
    row = impact._measure_row(Measure("agent_cost", "Explore: cost per spawn", "money", "Explore"), before, after, UNITS)
    assert row["before_n"] == 1 and row["after_n"] == 1
    assert row["label_key"] == "too_little_data"
    assert row["p"] is None
