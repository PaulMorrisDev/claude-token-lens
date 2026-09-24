"""What metrics capture costs (``capture.py``): prompt cycles and the
subagents they started, what capture cost while it ran (measured from the
notes and tags in the transcripts), and what each level or metric would
cost, replayed from your own history.

Amounts use ``tests/fixtures/pricing_min.toml``'s ``claude-widget-9``
(per million tokens: input 1.0, output 2.0, 5-minute cache write 0.5,
cache read 0.1; fast mode doubles every rate), so each expected value can
be worked out by hand.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claude_token_lens import capture, capture_catalogue as catalogue, parse
from claude_token_lens.model import TranscriptMeta
from claude_token_lens.parse import parse_transcript
from claude_token_lens.pricing import load_pricing

from helpers import (
    attachment_line,
    system_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MODEL = "claude-widget-9"
#: USD per character, at 4 characters a token.
OUT = 2.0 / 4 / 1e6
WRITE = 0.5 / 4 / 1e6
READ = 0.1 / 4 / 1e6


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(b"c" * 32)


@pytest.fixture()
def pricing():
    return load_pricing(path=FIXTURES / "pricing_min.toml")


def _ts(second: int) -> str:
    return f"2026-09-18T12:{second // 60:02d}:{second % 60:02d}.000Z"


def _ask(second: int, text: str = "do it") -> dict:
    return user_str_line(text, origin={"kind": "human"}, timestamp=_ts(second))


def _reply(second: int, *blocks, text: str = "ok", **kw) -> dict:
    content = list(blocks) or [{"type": "text", "text": text}]
    return turn_line(content=content, model=MODEL, timestamp=_ts(second), **kw)


def _note(second: int, ids, *, hook: str = "SessionStart", agent_type: str = "") -> dict:
    text = catalogue.note_text(ids, "main" if hook == "SessionStart" else "subagent", agent_type)
    wrapped = f"<system-reminder>\n{hook} hook additional context: {text}\n</system-reminder>"
    line = attachment_line("hook_additional_context", rendered=wrapped, content=[text], hookName=hook,
                           hookEvent=hook, toolUseID=hook)
    line["timestamp"] = _ts(second)
    return line


def _chars(note_line: dict) -> int:
    return len(note_line["rendered"][0]["content"])


def _parse(tmp_path, name: str, lines, **meta):
    path = tmp_path / name
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), **meta))


def _top(tmp_path, lines):
    return _parse(tmp_path, "top.jsonl", lines, kind="top-level")


def _sub(tmp_path, agent: str, lines, *, tool_use_id: str, agent_type: str = "general-purpose", parent: str = ""):
    return _parse(tmp_path, f"agent-{agent}.jsonl", lines, kind="subagent", agent_id=f"agent-{agent}",
                  agent_type=agent_type, tool_use_id=tool_use_id, parent_agent_id=parent or None)


def _corpus(top, *subs):
    return NS(sessions=[NS(top=top, subs=list(subs), session_id="s1")])


# -- prompt cycles ---------------------------------------------------------------


def test_cycles_run_from_each_message_to_the_next_and_collect_nested_agents(tmp_path):
    top = _top(tmp_path, [
        _reply(0, text="left over from a resumed session"),
        _ask(1, "first"),
        _reply(2, tool_use_block("Agent", "toolu_A", {"prompt": "look"})),
        user_block_line([tool_result_block("toolu_A", "found")], timestamp=_ts(8)),
        _reply(9),
        _ask(10, "second"),
        _reply(11, tool_use_block("Agent", "toolu_C", {"prompt": "fix"})),
    ])
    a = _sub(tmp_path, "a1", [
        user_str_line("look", timestamp=_ts(3)),
        _reply(4, tool_use_block("Agent", "toolu_B", {"prompt": "deeper"})),
    ], tool_use_id="toolu_A")
    # A nested agent's parentAgentId is the bare id, without "agent-".
    b = _sub(tmp_path, "b2", [user_str_line("deeper", timestamp=_ts(5)), _reply(6)], tool_use_id="toolu_B",
             parent="a1")
    c = _sub(tmp_path, "c3", [user_str_line("fix", timestamp=_ts(12)), _reply(13)], tool_use_id="toolu_C")
    stray = _sub(tmp_path, "d4", [user_str_line("?", timestamp=_ts(14)), _reply(15)], tool_use_id="toolu_X")

    cycles = capture.prompt_cycles(top, [a, b, c, stray])
    assert [len(cycle.turns) for cycle in cycles] == [2, 1]
    assert [cycle.subs for cycle in cycles] == [[a, b], [c]]


def test_the_last_tag_in_a_cycle_is_the_one_that_counts(tmp_path):
    top = _top(tmp_path, [
        _ask(0),
        _reply(1, text="Looking.\n[tl: task=debug]"),
        _reply(2, text="Found it.\n[tl: task=bugfix]"),
        _ask(3),
        _reply(4, text="untagged"),
    ])
    first, second = capture.prompt_cycles(top)
    assert first.tag.task == "bugfix"
    assert second.tag is None


# -- measured use ------------------------------------------------------------------


def _captured_session(tmp_path, *, speed=None):
    """A captured session: an Essentials note, a tagged reply that starts
    an agent, an untagged reply, a compaction, and one more message."""
    ids = catalogue.level_metrics("essentials")
    note = _note(0, ids)
    top = _top(tmp_path, [
        note,
        _ask(1, "hi"),
        _reply(2, tool_use_block("Agent", "toolu_A", {"prompt": "[spawn: isolate] check it"}),
               {"type": "text", "text": "Started.\n[tl: task=bugfix brief=clear]"}, speed=speed),
        user_block_line([tool_result_block("toolu_A", "Done.")], timestamp=_ts(6)),
        _ask(7, "more"),
        _reply(8),
        system_line("compact_boundary", timestamp=_ts(9)),
        _ask(10, "again"),
        _reply(11),
    ])
    sub_note = _note(3, ids, hook="SubagentStart", agent_type="general-purpose")
    sub = _sub(tmp_path, "a1", [
        sub_note,
        user_str_line("[spawn: isolate] check it", timestamp=_ts(3)),
        _reply(4, text="Checked.\n[result: done]"),
    ], tool_use_id="toolu_A")
    return top, sub, _chars(note), _chars(sub_note)


def test_usage_prices_notes_until_the_compaction_and_tags_at_the_writer_rate(tmp_path, pricing):
    top, sub, note_chars, sub_note_chars = _captured_session(tmp_path)
    use = capture.usage(_corpus(top, sub), pricing)

    # The session note is written with the first turn, read by the next,
    # and dropped by the compaction before the third.
    main = use.scopes["main"]
    assert main.note_cost == pytest.approx(note_chars * (WRITE + READ))
    assert main.note_tokens == round(note_chars / 4)
    tag = len("[tl: task=bugfix brief=clear]") + 1
    assert main.tag_cost == pytest.approx(tag * OUT)
    # The agent's note is written once; its report tag is output.
    agent = use.scopes["subagent"]
    assert agent.note_cost == pytest.approx(sub_note_chars * WRITE)
    assert agent.tag_cost == pytest.approx((len("[result: done]") + 1) * OUT)
    # The spawn word opens the brief, so the main session wrote it.
    assert use.scopes["brief"].tag_cost == pytest.approx((len("[spawn: isolate]") + 1) * OUT)

    assert (use.sessions, use.subagents, use.notes) == (1, 1, 2)
    assert (use.cycles, use.tagged_cycles, use.reports, use.tagged_reports) == (3, 1, 1, 1)
    assert use.coverage == pytest.approx(100 / 3)
    assert use.report_coverage == 100.0
    assert use.cost == pytest.approx(sum(s.cost for s in use.scopes.values()))
    assert sum(use.by_metric.values()) == pytest.approx(use.cost)
    assert sum(use.daily.values()) == pytest.approx(use.cost)
    assert list(use.daily) == ["2026-09-18"]
    assert 0 < use.share < 100
    assert {k: use.answers[k] for k in ("task", "brief", "result", "spawn")} == {
        "task": 1, "brief": 1, "result": 1, "spawn": 1}
    assert "level" not in use.answers


def test_fast_mode_doubles_what_the_fast_turn_wrote_and_carried(tmp_path, pricing):
    top, sub, note_chars, _ = _captured_session(tmp_path, speed="fast")
    use = capture.usage(_corpus(top, sub), pricing)
    # The first turn ran fast: its cache write, its tag and the spawn word
    # it wrote cost double; the next turn's read does not.
    assert use.scopes["main"].note_cost == pytest.approx(note_chars * (2 * WRITE + READ))
    assert use.scopes["main"].tag_cost == pytest.approx((len("[tl: task=bugfix brief=clear]") + 1) * 2 * OUT)
    assert use.scopes["brief"].tag_cost == pytest.approx((len("[spawn: isolate]") + 1) * 2 * OUT)


def test_usage_since_leaves_out_what_came_before(tmp_path, pricing):
    top, sub, _, _ = _captured_session(tmp_path)
    use = capture.usage(_corpus(top, sub), pricing, since="2026-09-18T12:00:07Z")
    assert (use.notes, use.cycles, use.tagged_cycles, use.reports) == (0, 2, 0, 0)
    assert use.cost == 0.0
    assert use.spend > 0


def test_sessions_without_a_capture_note_are_not_counted(tmp_path, pricing):
    top = _top(tmp_path, [_ask(0), _reply(1, text="Done.\n[tl: task=bugfix]")])
    use = capture.usage(_corpus(top), pricing)
    assert (use.sessions, use.cycles, use.cost, use.coverage, use.share) == (0, 0, 0.0, None, None)


def test_a_tool_note_counts_in_the_tool_scope(tmp_path, pricing):
    ids = catalogue.level_metrics("deep")
    text = catalogue.tool_note_text("big_output")
    tool_note = attachment_line("hook_additional_context", rendered=f"<system-reminder>\nPostToolUse hook "
                                f"additional context: {text}\n</system-reminder>", content=[text],
                                hookName="PostToolUse", hookEvent="PostToolUse", toolUseID="toolu_1")
    tool_note["timestamp"] = _ts(3)
    top = _top(tmp_path, [
        _note(0, ids),
        _ask(1),
        _reply(2, tool_use_block("Bash", "toolu_1", {"command": "make"})),
        user_block_line([tool_result_block("toolu_1", "x" * 40_000)], timestamp=_ts(3)),
        tool_note,
        _reply(4, text="Built.\n[tl: task=ops out=part]"),
    ])
    use = capture.usage(_corpus(top), pricing)
    assert use.scopes["tool"].note_cost == pytest.approx(_chars(tool_note) * WRITE)
    assert use.by_metric["big_output"] > 0
    assert use.answers["big_output"] == 1


# -- estimates from your own history ------------------------------------------------


def _history_corpus(tmp_path):
    top = _top(tmp_path, [
        _ask(0),
        _reply(1, tool_use_block("Agent", "toolu_A", {"prompt": "a"}), tool_use_block("Agent", "toolu_E", {"prompt": "e"})),
        user_block_line([tool_result_block("toolu_A", "a"), tool_result_block("toolu_E", "e")], timestamp=_ts(6)),
        _reply(7, tool_use_block("Bash", "toolu_B", {"command": "make"})),
        user_block_line([tool_result_block("toolu_B", "x" * 40_000)], timestamp=_ts(8)),
        _reply(9),
        _ask(10),
        _reply(11),
    ])
    general = _sub(tmp_path, "a1", [user_str_line("a", timestamp=_ts(2)), _reply(3), _reply(4)], tool_use_id="toolu_A")
    explore = _sub(tmp_path, "e1", [user_str_line("e", timestamp=_ts(2)), _reply(3)], tool_use_id="toolu_E",
                   agent_type="Explore")
    setup = _sub(tmp_path, "s1", [user_str_line("s", timestamp=_ts(2)), _reply(3)], tool_use_id="toolu_S",
                 agent_type="statusline-setup")
    return _corpus(top, general, explore, setup)


def test_history_prices_one_character_in_each_place(tmp_path, pricing):
    past = capture.history(_history_corpus(tmp_path), pricing, days=7)
    assert (past.days, past.sessions, past.cycles, past.subagents) == (7, 1, 2, 2)
    assert (past.main_notes, past.sub_notes) == (1, 2)
    # Main: written with the first of 4 turns, read by the other 3.
    assert past.main_note == pytest.approx(WRITE + 3 * READ)
    assert past.sub_note == pytest.approx(WRITE + READ)
    assert past.sub_note_no_rules == pytest.approx(WRITE)
    assert past.reply_tag == pytest.approx(2 * OUT)
    assert past.report_tag == pytest.approx(2 * OUT)
    assert past.brief_tag == pytest.approx(2 * OUT)
    # The 40k-character result arrives with the third turn, carried to the end.
    assert past.big_outputs == 1
    assert past.big_output_note == pytest.approx(WRITE + READ)
    assert past.big_output_tag == pytest.approx(OUT)
    assert past.spend > 0


def test_estimates_rise_with_the_level_and_scale_with_sampling(tmp_path, pricing):
    past = capture.history(_history_corpus(tmp_path), pricing, days=7)
    levels = capture.level_estimates(past)
    assert list(levels) == ["free", "essentials", "standard", "deep"]
    assert levels["free"].cost == 0.0 and levels["free"].note_tokens == 0
    assert 0 < levels["essentials"].cost < levels["standard"].cost < levels["deep"].cost
    half = capture.level_estimates(past, sample=50)["standard"]
    assert half.cost == pytest.approx(levels["standard"].cost / 2)
    assert levels["standard"].per_week == pytest.approx(levels["standard"].cost)
    assert levels["standard"].share == pytest.approx(100 * levels["standard"].cost / past.spend)
    assert capture.estimate(past, ()).cost == 0.0


def test_an_estimate_is_the_note_and_tag_sizes_times_the_unit_costs(tmp_path, pricing):
    past = capture.history(_history_corpus(tmp_path), pricing, days=7)
    ids = ("task",)
    main = len(catalogue.note_text(ids, "main")) + catalogue.NOTE_WRAP_CHARS + len("SessionStart")
    reply = catalogue.METRICS_BY_ID["task"].out_chars + 7
    assert capture.estimate(past, ids).cost == pytest.approx(main * past.main_note + reply * past.reply_tag)


def test_metric_estimates_price_what_each_metric_adds(tmp_path, pricing):
    past = capture.history(_history_corpus(tmp_path), pricing, days=7)
    ids = catalogue.level_metrics("standard")
    parts = capture.metric_estimates(past, ids)
    assert set(parts) == set(ids)
    assert parts["session_end"] == 0.0
    assert parts["task"] > 0 and parts["fit"] > 0
    # Leaving out result also leaves out the subagent extras that need it.
    needs_result = [i for i in ids if "result" in catalogue.METRICS_BY_ID[i].requires]
    assert needs_result
    assert parts["result"] >= max(parts[i] for i in needs_result)


def test_enough_data_counts_answers_against_each_target():
    use = capture.CaptureUsage(answers={"task": 12})
    assert capture.enough_data(use, "task") == (12, capture.ENOUGH["main"])
    assert capture.enough_data(use, "fit") == (0, capture.ENOUGH["subagent"])
    assert capture.enough_data(use, "spawn") == (0, capture.ENOUGH["brief"])
    assert capture.enough_data(use, "big_output") == (0, capture.ENOUGH["tool"])
    assert capture.enough_data(use, "waits", signal_sessions=5) == (5, capture.ENOUGH["signal"])
    assert capture.enough_target("no-such-metric") == 0


def test_estimate_prices_the_feedback_reminder_once_per_message():
    past = capture.History(days=14, cycles=10, subagents=3, main_notes=2, main_note=1e-6, reply_tag=2e-6, brief_tag=5e-6)
    est = capture.estimate(past, ("feedback_reminder",))
    note = len(catalogue.note_text(("feedback_reminder",), "main")) + capture._WRAP["SessionStart"]
    out = catalogue.METRICS_BY_ID["feedback_reminder"].out_chars
    assert est.cost == pytest.approx(note * 1e-6 + out * 2e-6)
    assert est.tag_tokens == round(out * 10 / 4)
