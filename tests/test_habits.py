"""Work habits (``habits.py``): the facts worked out per message and per
agent run, the playbook of habits worth trying, how each habit's trend
and confidence are judged, and the tables the Work habits tab shows.

Amounts in the transcript tests use ``tests/fixtures/pricing_min.toml``'s
``claude-widget-9``; the playbook tests build the facts directly, so
each saving can be worked out by hand.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claude_token_lens import capture_catalogue as catalogue, habits, parse
from claude_token_lens.habits import AgentFact, CycleFact, Habits, Item, Piece
from claude_token_lens.model import CaptureTag, TranscriptMeta
from claude_token_lens.parse import parse_transcript
from claude_token_lens.pricing import load_pricing

from helpers import tool_result_block, tool_use_block, turn_line, user_block_line, user_str_line, write_jsonl

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MODEL = "claude-widget-9"
WEEKS = ["2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24", "2026-08-31"]


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(b"h" * 32)


@pytest.fixture()
def pricing():
    return load_pricing(path=FIXTURES / "pricing_min.toml")


def _cycle(week: str = WEEKS[0], cost: float = 1.0, tag: CaptureTag | None = None, **kw) -> CycleFact:
    ts = datetime.fromisoformat(week).replace(tzinfo=timezone.utc) if week else None
    return CycleFact(session_id="s1", ts=ts, week=week, cost=cost, turns=1, tag=tag, **kw)


def _agent(**kw) -> AgentFact:
    return AgentFact(**{"session_id": "s1", "agent_type": "general-purpose", "week": WEEKS[0], "cost": 1.0, **kw})


def _by_key(items) -> dict[str, Item]:
    return {item.key: item for item in items}


def _rows(table) -> list[dict]:
    return [{c.key: v for c, v in zip(table.columns, row)} for row in table.rows]


def _table(section, name):
    return next(t for t in section.tables if t.name == name)


# -- trend and confidence ---------------------------------------------------------


def _weekly(waste_per_message: list[float | None]) -> tuple[Habits, Item]:
    """Three messages a week (one where the rate is ``None``, so the week
    is too thin to count), and an item addressing ``rate`` USD of each."""
    h = Habits()
    waste = {}
    for week, rate in zip(WEEKS, waste_per_message):
        h.cycles.extend(_cycle(week) for _ in range(habits.TREND_MIN_CYCLES if rate is not None else 1))
        waste[week] = 3 * (rate or 0.0)
    return h, Item("tool_loops", 1.0, 1, ("inferred",), "", waste=waste)


def test_a_steady_fall_is_improving_and_prices_what_the_habit_already_saves():
    h, item = _weekly([1.0, 1.0, 0.1, 0.1])
    word, weeks, adopted = habits.trend(h, item)
    assert (word, weeks) == ("falling", "100 100 10 10")
    # (1.0 - 0.1) a message, over the three messages a recent week holds.
    assert adopted == pytest.approx(0.9 * 3)


def test_a_fall_seen_over_too_few_weeks_prices_nothing_yet():
    h, item = _weekly([1.0, 0.1, 0.1])
    assert habits.TREND_MIN_ADOPTED == 4
    assert habits.trend(h, item) == ("falling", "100 10 10", 0.0)


@pytest.mark.parametrize(
    "rates, word",
    [
        ([0.1, 0.1, 1.0, 1.0], "rising"),
        ([0.5, 0.5, 0.5, 0.5], "steady"),
        # The latest week back above the earlier level isn't a fall.
        ([1.0, 1.0, 0.1, 0.1, 1.2], "steady"),
        ([1.0, 0.1], "new"),
    ],
)
def test_trend_words(rates, word):
    h, item = _weekly(rates)
    assert habits.trend(h, item)[0] == word


def test_a_thin_week_is_a_dash_and_does_not_count():
    h, item = _weekly([1.0, None, 1.0, 0.1, 0.1])
    word, weeks, _ = habits.trend(h, item)
    assert weeks == "100 - 100 10 10" and word == "falling"


@pytest.mark.parametrize(
    "n, sources, level",
    [
        (20, ("reported",), "high"),
        (20, ("inferred",), "medium"),
        (20, ("reported", "inferred"), "high"),
        (8, ("your feedback",), "medium"),
        (7, ("reported",), "low"),
    ],
)
def test_confidence_rises_with_evidence_and_inference_alone_never_reaches_high(n, sources, level):
    assert habits.confidence(Item("tool_loops", None, n, sources, "")) == level


# -- the playbook ---------------------------------------------------------------------


def test_the_playbook_puts_the_largest_saving_first_and_unpriced_habits_last():
    easy = CaptureTag(level="easy")
    h = Habits(cycles=[
        *(_cycle(tag=easy, effort="high", thinking_cost=0.2) for _ in range(3)),
        _cycle(loops=1, loop_cost=1.0),
        *(_cycle(tag=CaptureTag(skill="unneeded"), skill_calls=[("lint", False, 0, 0.0)]) for _ in range(2)),
    ])
    items = habits.playbook(h)
    assert [i.key for i in items] == ["tool_loops", "effort_fit", "skill_unneeded"]
    loops, effort, skill = items
    assert loops.saving == pytest.approx(1.0) and loops.sources == ("inferred",)
    # Half the thinking on each easy ask at high effort.
    assert effort.saving == pytest.approx(3 * 0.1) and effort.sources == ("reported",)
    assert skill.saving is None and "(lint)" in skill.evidence


def test_large_asks_count_whether_reported_or_seen_and_say_which():
    h = Habits(cycles=[
        _cycle(tag=CaptureTag(size="xl"), cost=5.0, growth_cost=2.0),
        _cycle(cost=4.0, growth_cost=1.0, compactions=1, redone=True),
        _cycle(tag=CaptureTag(size="s"), cost=1.0, growth_cost=3.0),
    ])
    item = _by_key(habits.playbook(h))["split_large"]
    assert item.n == 2 and item.saving == pytest.approx(0.5 * 2.0 + 0.5 * 1.0)
    assert item.sources == ("reported", "inferred") and item.source == "reported + inferred"
    assert "1 were compacted part-way" in item.evidence and "1 had to be redone" in item.evidence


def test_a_new_task_on_old_context_is_worth_a_clear_unless_the_work_built_on_it():
    stale = habits.STALE_TOKENS
    h = Habits(cycles=[
        _cycle(tag=CaptureTag(shift="new"), stale_tokens=stale, stale_cost=0.4),
        _cycle(stale_tokens=stale, stale_cost=0.2, stale_rewrite=0.2, gap_s=habits.LONG_BREAK_S),
        _cycle(tag=CaptureTag(shift="build"), stale_tokens=stale, stale_cost=9.0, gap_s=habits.LONG_BREAK_S),
        _cycle(tag=CaptureTag(shift="new"), stale_tokens=stale - 1, stale_cost=9.0),
    ])
    item = _by_key(habits.playbook(h))["clear_between"]
    assert item.saving == pytest.approx(0.4 + 0.5 * (0.2 + 0.2))
    assert item.sources == ("reported", "inferred")


def test_vague_asks_are_compared_with_clear_ones_of_the_same_kind():
    clear = CaptureTag(task="bugfix", brief="clear")
    vague = CaptureTag(task="bugfix", brief="vague", missing=("repro", "files"))
    h = Habits(cycles=[*(_cycle(tag=clear) for _ in range(3)), *(_cycle(tag=vague, cost=3.0) for _ in range(5))])
    item = _by_key(habits.playbook(h))["brief_clearly"]
    assert item.saving == pytest.approx(5 * 0.5 * (3.0 - 1.0)) and item.n == 5
    assert "costing 3.0x a clear ask of the same kind" in item.evidence
    assert "most often missing: reproduce, files" in item.evidence
    # The example is the line your asks most often lacked.
    assert item.example == catalogue.BRIEF_LINES["repro"][1]


def test_too_few_vague_asks_suggest_nothing():
    vague = CaptureTag(task="bugfix", brief="vague")
    h = Habits(cycles=[_cycle(tag=vague) for _ in range(habits.MIN_GROUP - 1)])
    assert "brief_clearly" not in _by_key(habits.playbook(h))


def test_long_agent_reports_that_were_not_capped_are_priced_down_to_a_short_one():
    h = Habits(agents=[
        _agent(report_tokens=4_000, report_carry=1.0),
        _agent(report_tokens=4_000, report_carry=1.0, capped=True),
        _agent(report_tokens=500, report_carry=0.1),
    ])
    item = _by_key(habits.playbook(h))["short_reports"]
    assert item.saving == pytest.approx(1.0 * (4_000 - habits.SHORT_REPORT_TOKENS) / 4_000)
    assert item.n == 1 and "33% of briefs asked for a short one" in item.evidence


def test_misses_you_reported_name_the_kind_of_work_and_what_slowed_it():
    h = Habits(pieces=[
        Piece("missed", 4.0, 2, "refactor", ("rework",), (), "your feedback"),
        Piece("stopped", 2.0, 1, "refactor", ("rework", "unclear"), (), "dashboard rating"),
        Piece("met", 1.0, 1, "bugfix", (), (), "your feedback"),
    ])
    item = _by_key(habits.playbook(h))["outcome_misses"]
    assert item.sources == ("your feedback",) and item.saving is None and item.n == 2
    assert item.evidence == (
        "2 pieces of work missed their goal or were stopped, costing 3.0x one that met it; mostly refactor work; "
        "slowed most by: wrong approach or rework."
    )


# -- tables -----------------------------------------------------------------------------


def test_every_table_is_there_even_with_nothing_to_show():
    section = habits.section_from(Habits())
    assert section.key == "habits" and section.notes == []
    assert [t.name for t in section.tables] == [
        "habits_digest",
        "habits_playbook",
        "habits_by_task",
        "habits_briefs",
        "habits_brief_templates",
        "habits_agents",
        "habits_effort_fit",
        "habits_setups",
        "habits_outcomes",
        "habits_prompt_flags",
        "habits_skills",
        "habits_tool_output",
    ]
    assert _table(section, "habits_playbook").notes


def test_untagged_unrated_work_says_how_to_get_more():
    notes = habits.section_from(Habits(cycles=[_cycle()])).notes
    assert any("turn on metrics capture" in n for n in notes)
    assert any("run /tl-feedback" in n for n in notes)


def test_the_digest_leads_with_the_habits_worth_most_then_what_met_goals_cost():
    h = Habits(
        cycles=[_cycle(loops=1, loop_cost=2.0), _cycle(WEEKS[1], loops=1, loop_cost=0.0)],
        agents=[_agent(report_tokens=4_000, report_carry=1.0)],
        pieces=[Piece("met", 3.0, 1, None, (), (), "your feedback"), Piece("missed", 1.0, 1, None, (), (), "x")],
    )
    rows = _rows(habits.digest_table(h))
    assert [r["item"] for r in rows] == ["top_1", "top_2", "cost_per_met"]
    assert rows[0]["what"] == habits.ITEMS["tool_loops"][1]
    # Savings are spread over the weeks the messages cover.
    assert rows[0]["value"] == pytest.approx(2.0 / h.span_weeks)
    assert rows[1]["what"] == habits.ITEMS["short_reports"][1]
    assert rows[2]["value"] == 3.0 and rows[2]["detail"] == "1 of 2 pieces you gave feedback on"


def test_the_playbook_table_carries_the_example_the_basis_and_the_trend():
    h = Habits(cycles=[_cycle(loops=1, loop_cost=1.0)])
    row = _rows(habits.playbook_table(h, habits.playbook(h)))[0]
    assert row["habit"] == "tool_loops" and row["theme"] == "verification"
    assert row["example"] == habits.EXAMPLES["tool_loops"] and row["basis"] == habits.BASES["tool_loops"]
    assert (row["source"], row["confidence"], row["trend"]) == ("inferred", "low", "new")


def test_brief_templates_start_from_the_checklist_and_put_what_you_leave_out_first():
    keys, why = habits.template_lines("bugfix")
    assert tuple(keys) == catalogue.BRIEF_CHECKLISTS["bugfix"] and why.startswith("A starting point")
    tag = CaptureTag(task="bugfix", missing=("constraints",))
    keys, why = habits.template_lines("bugfix", [_cycle(tag=tag) for _ in range(3)])
    assert keys == ["constraints", "repro", "files", "done"]
    assert why == "Constraints was missing in 3 of 3 bugfix asks."
    # With no tagged work, the common kinds of task get a template each.
    rows = _rows(_table(habits.section_from(Habits()), "habits_brief_templates"))
    assert [r["task"] for r in rows] == ["bugfix", "feature", "refactor", "research"]
    research = rows[-1]
    assert research["checklist"] == "Goal / Files / Report"
    assert research["template"].splitlines()[-1] == catalogue.BRIEF_LINES["report"][1]


def test_the_checklists_are_the_ones_the_brief_skill_holds():
    assert habits.DEFAULT_CHECKLISTS is catalogue.BRIEF_CHECKLISTS
    assert habits.MISSING_LINES == {k: v for k, v in catalogue.BRIEF_LINES.items() if k != "report"}


@pytest.mark.parametrize(
    "row, reason",
    [
        ({"agent_type": "a", "fit_larger": 2, "fit_smaller": 1}, "Claude said 2 of its runs needed a larger model"),
        ({"agent_type": "a", "fit_larger": 1, "fit_smaller": 3, "hard_pct": 60.0}, "60% of its work was reported hard"),
        ({"agent_type": "a", "retried_model": 1}, "a run was retried because the model wasn't enough"),
        ({"agent_type": "a", "fit_smaller": 3, "hard_pct": 10.0}, None),
        ({"agent_type": "", "fit_larger": 5}, None),
    ],
)
def test_unfit_agents_hold_a_cheaper_model_back_with_the_reason(row, reason):
    assert habits.unfit_agents([row]).get(row["agent_type"]) == reason


# -- from transcripts ---------------------------------------------------------------------


def _ts(second: int) -> str:
    return f"2026-09-18T12:{second // 60:02d}:{second % 60:02d}.000Z"


def _parse(tmp_path, name, lines, **meta):
    path = tmp_path / name
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), **meta))


def _reply(second: int, *blocks, text: str = "ok") -> dict:
    content = list(blocks) or [{"type": "text", "text": text}]
    return turn_line(content=content, model=MODEL, timestamp=_ts(second))


def _tagged_session(tmp_path):
    top = _parse(tmp_path, "top.jsonl", [
        user_str_line("fix the login bug", origin={"kind": "human"}, timestamp=_ts(0)),
        _reply(1, tool_use_block("Agent", "toolu_A", {"prompt": "find where the cookie is set"}),
               {"type": "text", "text": "Looking.\n[tl: task=bugfix brief=vague level=hard]"}),
        user_block_line([tool_result_block("toolu_A", "src/auth.py")], timestamp=_ts(5)),
        _reply(6, text="Fixed.\n[tl: task=bugfix brief=vague level=hard]"),
        user_str_line("do it again properly", origin={"kind": "human"}, timestamp=_ts(10)),
        _reply(11, text="Redone.\n[tl: task=bugfix shift=redo]"),
    ], kind="top-level")
    sub = _parse(tmp_path, "agent-a1.jsonl", [
        user_str_line("find where the cookie is set", timestamp=_ts(2)),
        _reply(3, text="src/auth.py\n[result: done fit=larger rules=unused]"),
    ], kind="subagent", agent_id="agent-a1", agent_type="Explore", tool_use_id="toolu_A")
    return NS(sessions=[NS(top=top, subs=[sub], session_id="s1", project_dir="p")])


def test_collect_turns_tags_ratings_and_agent_reports_into_facts(tmp_path, pricing):
    rating = {"outcome": "partly", "slow": ["rework"], "worth": "fair", "helped": ["none"]}
    h = habits.collect(_tagged_session(tmp_path), pricing, ratings={"s1": rating})
    first, second = h.cycles
    assert (first.tag.task, first.tag.brief, first.tag.level) == ("bugfix", "vague", "hard")
    assert first.redone and first.redo_cost == pytest.approx(second.cost) and not second.redone
    assert first.explore_agents == 1 and first.cost > 0
    assert (first.outcome, first.outcome_source) == ("partly", "rating")
    (piece,) = h.pieces
    assert (piece.outcome, piece.source, piece.task, piece.slow, piece.helped) == (
        "partly", "dashboard rating", "bugfix", ("rework",), ()
    )
    (agent,) = h.agents
    assert (agent.agent_type, agent.result, agent.fit, agent.rules, agent.level) == (
        "Explore", "done", "larger", "unused", "hard"
    )


def test_the_agents_table_feeds_the_model_veto(tmp_path, pricing):
    section = habits.build_section(_tagged_session(tmp_path), pricing)
    rows = {r["agent_type"]: r for r in _rows(_table(section, "habits_agents"))}
    assert rows["Explore"]["fit_larger"] == 1 and rows["Explore"]["rules_unused"] == 1
    assert habits.unfit_agents(list(rows.values()))["Explore"] == "Claude said 1 of its runs needed a larger model"
    by_task = {r["task"]: r for r in _rows(_table(section, "habits_by_task"))}
    assert by_task["all"]["cycles"] == 2 and by_task["bugfix"]["redo_pct"] == pytest.approx(50.0)


def test_the_capture_section_says_what_capture_cost_and_since_when(tmp_path, pricing):
    config = NS(level="standard", enabled_at="2026-09-01T08:00:00+00:00")
    section = habits.capture_section(_tagged_session(tmp_path), pricing, config)
    rows = dict(_table(section, "capture_usage").rows)
    assert rows["level"] == catalogue.LEVEL_TITLES["standard"] and rows["since"] == "2026-09-01"
    assert section.key == "capture"
    off = dict(habits.capture_section(NS(sessions=[]), pricing, NS(level="off", enabled_at="")).tables[0].rows)
    assert off["level"] == catalogue.LEVEL_TITLES["off"] and off["since"] == ""


# -- the best setup per kind of task -----------------------------------------------


def _setup_cycles(n, *, model, effort, cost, task="bugfix", level="normal", redone=0, outcome=None):
    tag = CaptureTag(task=task, level=level)
    return [
        _cycle(tag=tag, cost=cost, model=model, effort=effort, redone=i < redone, outcome=outcome)
        for i in range(n)
    ]


@pytest.mark.parametrize(
    "model_id, name",
    [("claude-haiku-4-5-20251001", "haiku"), ("claude-sonnet-5", "sonnet"), ("claude-opus-5-5", "opus"),
     ("claude-fable-5-1", "fable"), ("claude-widget-9", "claude-widget-9"), (None, "unknown")],
)
def test_family_names_the_model_family(model_id, name):
    assert habits.family(model_id) == name


def test_feedback_decides_went_well_before_the_next_message_does():
    assert habits.went_well(_cycle(outcome="met", redone=True))
    assert not habits.went_well(_cycle(outcome="missed"))
    assert habits.went_well(_cycle()) and not habits.went_well(_cycle(redone=True))


def test_the_cheapest_setup_that_went_as_well_as_your_usual_one_is_named():
    h = Habits(cycles=[
        *_setup_cycles(8, model="claude-opus-5-5", effort="high", cost=2.0, redone=1),
        *_setup_cycles(6, model="claude-sonnet-5", effort="medium", cost=0.5, redone=1),
        # Cheaper still, but redone too often.
        *_setup_cycles(5, model="claude-haiku-4-5-20251001", effort="low", cost=0.1, redone=3),
    ])
    rows = [r for r in _rows(_table(habits.section_from(h), "habits_setups")) if r["level"] == "all"]
    by_setup = {(r["model"], r["effort"]): r for r in rows}
    usual = by_setup[("opus", "high")]
    cheaper = by_setup[("sonnet", "medium")]
    assert usual["verdict"] == "usual" and usual["cycles"] == 8 and usual["ok_pct"] == pytest.approx(87.5)
    assert cheaper["verdict"] == "cheaper" and cheaper["saving_pct"] == pytest.approx(75.0)
    assert by_setup[("haiku", "low")]["verdict"] == "" and by_setup[("haiku", "low")]["saving_pct"] is None
    # The levels Claude reported get rows of their own, after all of them.
    assert {r["level"] for r in _rows(_table(habits.section_from(h), "habits_setups"))} == {"all", "normal"}


def test_a_setup_within_the_tolerance_still_counts_as_doing_as_well():
    h = Habits(cycles=[
        *_setup_cycles(22, model="claude-opus-5-5", effort="high", cost=2.0),
        *_setup_cycles(20, model="claude-sonnet-5", effort="high", cost=1.0, redone=1),
    ])
    rows = {r["model"]: r for r in _rows(_table(habits.section_from(h), "habits_setups")) if r["level"] == "all"}
    assert rows["sonnet"]["ok_pct"] == pytest.approx(95.0)
    assert rows["sonnet"]["verdict"] == "cheaper"


def test_too_few_messages_on_either_side_name_no_cheaper_setup():
    few_usual = Habits(cycles=[
        *_setup_cycles(habits.MIN_GROUP - 1, model="claude-opus-5-5", effort="high", cost=2.0),
        *_setup_cycles(habits.MIN_GROUP - 2, model="claude-sonnet-5", effort="high", cost=1.0),
    ])
    few_cheaper = Habits(cycles=[
        *_setup_cycles(10, model="claude-opus-5-5", effort="high", cost=2.0),
        *_setup_cycles(habits.MIN_GROUP - 1, model="claude-sonnet-5", effort="high", cost=1.0),
    ])
    for h in (few_usual, few_cheaper):
        verdicts = [r["verdict"] for r in _rows(_table(habits.section_from(h), "habits_setups"))]
        assert "cheaper" not in verdicts and "usual" in verdicts


def test_untagged_messages_have_no_setup_rows():
    h = Habits(cycles=[_cycle(model="claude-opus-5-5", effort="high") for _ in range(10)])
    assert _table(habits.section_from(h), "habits_setups").rows == []


def test_a_setup_that_only_saw_easy_work_is_compared_on_easy_work():
    h = Habits(cycles=[
        *_setup_cycles(6, model="claude-opus-5-5", effort="high", cost=1.0, level="easy"),
        *_setup_cycles(6, model="claude-opus-5-5", effort="high", cost=3.0, level="hard"),
        *_setup_cycles(6, model="claude-sonnet-5", effort="high", cost=0.8, level="easy"),
    ])
    rows = {r["model"]: r for r in _rows(_table(habits.section_from(h), "habits_setups")) if r["level"] == "all"}
    # Raw, sonnet looks 60% cheaper; on the easy work both ran it is 20%.
    assert rows["sonnet"]["avg_cost"] == pytest.approx(0.8) and rows["opus"]["avg_cost"] == pytest.approx(2.0)
    assert rows["sonnet"]["verdict"] == "cheaper" and rows["sonnet"]["saving_pct"] == pytest.approx(20.0)


def test_too_little_shared_work_gives_no_verdict():
    h = Habits(cycles=[
        *_setup_cycles(2, model="claude-opus-5-5", effort="high", cost=1.0, level="easy"),
        *_setup_cycles(10, model="claude-opus-5-5", effort="high", cost=3.0, level="hard"),
        *_setup_cycles(6, model="claude-sonnet-5", effort="high", cost=0.8, level="easy"),
    ])
    rows = {r["model"]: r for r in _rows(_table(habits.section_from(h), "habits_setups")) if r["level"] == "all"}
    assert rows["sonnet"]["verdict"] == ""
