"""Quality signals (``quality``): what the parser and event classifier
record for them, how one transcript becomes a run, and how two sets of
runs are compared."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from claude_token_lens import events, parse, quality
from claude_token_lens.model import EventKind, TranscriptMeta
from claude_token_lens.parse import parse_transcript

from helpers import (
    attachment_line,
    queue_operation_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)


def _ts(second: int) -> str:
    return f"2026-09-18T12:{second // 60:02d}:{second % 60:02d}.000Z"


def _reply(second: int, *blocks, stop_reason: str | None = None, **kw) -> dict:
    line = turn_line(content=list(blocks) or [{"type": "text", "text": "done"}], timestamp=_ts(second), **kw)
    line["message"]["stop_reason"] = stop_reason
    return line


def _result(second: int, tool_use_id: str, *, error: bool = False) -> dict:
    return user_block_line([tool_result_block(tool_use_id, "out", is_error=error)], timestamp=_ts(second))


def _parse(tmp_path, lines, name="t.jsonl", **meta):
    path = tmp_path / name
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), **meta))


def _agent(tmp_path, lines, agent_id="abc", agent_type="Explore"):
    return _parse(tmp_path, lines, f"agent-{agent_id}.jsonl", kind="subagent", agent_id=f"agent-{agent_id}",
                  agent_type=agent_type)


# -- what the parser keeps ----------------------------------------------------------


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(b"q" * 32)


def test_turns_keep_tool_calls_errors_edits_and_stop_reason(tmp_path):
    result = _parse(tmp_path, [
        user_str_line("fix the build", timestamp=_ts(0)),
        _reply(1, tool_use_block("Bash", "t1"), tool_use_block("Edit", "t2", {"file_path": "C:/src/a.py"}),
               stop_reason="tool_use"),
        user_block_line([tool_result_block("t1", "boom", is_error=True), tool_result_block("t2", "ok")],
                        timestamp=_ts(2)),
        _reply(3, stop_reason="end_turn"),
    ])
    first = next(t for t in result.turns if t.tool_use_ids)
    assert first.stop_reason == "tool_use"
    assert first.tool_calls_by_tool == {"Bash": 1, "Edit": 1}
    assert first.tool_errors_by_tool == {"Bash": 1}
    assert len(first.edit_target_hashes) == 1
    assert "a.py" not in repr(first.edit_target_hashes)
    assert result.turns[-1].stop_reason == "end_turn"


@pytest.mark.parametrize("text, expected", [
    ("that is wrong, the test still fails", True),
    ("It still doesnt work", True),
    ("why did you delete the config?", True),
    ("please undo that change", True),
    ("No, go ahead", False),
    ("looks good, ship it", False),
    ("x" * 300 + " that's wrong", False),  # only the start of a message is read
])
def test_correction_phrases(text, expected):
    assert events._looks_like_correction([text]) is expected


def test_a_correction_is_a_flag_on_the_next_turn_and_the_text_is_not_kept(tmp_path):
    result = _parse(tmp_path, [
        user_str_line("add a test", timestamp=_ts(0)),
        _reply(1),
        user_str_line("that's wrong, it still fails", timestamp=_ts(2)),
        _reply(3),
    ])
    assert [t.human_correction for t in result.turns if t.turn_index > 0] == [False, True]
    human = [e for e in result.events if e.kind == EventKind.HUMAN_TEXT]
    assert human[-1].detail["correction"] is True
    assert "wrong" not in repr(result)


NOTE = "<task-notification><task-id>abc123</task-id><status>failed</status><summary>x</summary></task-notification>"


def test_task_notifications_carry_id_and_status_in_every_shape():
    as_user = events.classify_line(user_str_line(NOTE))
    queued = events.classify_line(queue_operation_line("enqueue", content=NOTE))
    attached = events.classify_line(attachment_line("queued_command", prompt=NOTE))
    for event in (as_user, queued, attached):
        assert event.detail["task_id"] == "abc123" and event.detail["status"] == "failed"
    assert events._task_notification_detail("no id here") == {}


def test_a_synchronous_agent_result_carries_its_id_and_status_but_a_launch_does_not():
    done = user_block_line([tool_result_block("t1", "ok")], toolUseResult={"agentId": "abc", "status": "completed"})
    launched = user_block_line([tool_result_block("t1", "ok")],
                               toolUseResult={"agentId": "abc", "status": "async_launched", "isAsync": True})
    assert events.classify_line(done).detail == {"agents": [["abc", "completed"]]}
    assert not events.classify_line(launched).detail


# -- one transcript as a run ---------------------------------------------------------


def test_an_agent_that_answers_is_not_cut_off(tmp_path):
    run = quality.run_facts(_agent(tmp_path, [
        user_str_line("brief", timestamp=_ts(0)),
        _reply(1, tool_use_block("Read", "t1")),
        _result(2, "t1"),
        _reply(3),
    ]), None)
    assert run.cut_off is False and not run.turn_limit
    assert (run.replies, run.tool_calls, run.tool_errors) == (2, 1, 0)


def test_ending_on_structured_output_is_an_answer(tmp_path):
    run = quality.run_facts(_agent(tmp_path, [
        user_str_line("brief", timestamp=_ts(0)),
        _reply(1, tool_use_block("StructuredOutput", "t1")),
    ]), None)
    assert run.cut_off is False


def test_ending_on_a_tool_call_is_cut_off_and_after_its_result_likely_out_of_turns(tmp_path):
    waiting = quality.run_facts(_agent(tmp_path, [
        user_str_line("brief", timestamp=_ts(0)),
        _reply(1, tool_use_block("Bash", "t1")),
    ], agent_id="a1"), None)
    assert waiting.cut_off and not waiting.turn_limit
    out_of_turns = quality.run_facts(_agent(tmp_path, [
        user_str_line("brief", timestamp=_ts(0)),
        _reply(1, tool_use_block("Bash", "t1")),
        _result(2, "t1"),
    ], agent_id="a2"), None)
    assert out_of_turns.cut_off and out_of_turns.turn_limit


def test_an_agent_that_never_replied_is_cut_off(tmp_path):
    run = quality.run_facts(_agent(tmp_path, [user_str_line("brief", timestamp=_ts(0))]), None)
    assert run.cut_off is True and run.replies == 0


def test_rework_counts_edits_to_a_file_changed_before_your_last_message(tmp_path):
    edit = lambda sec, tid: _reply(sec, tool_use_block("Edit", tid, {"file_path": "C:/src/a.py"}))  # noqa: E731
    run = quality.run_facts(_parse(tmp_path, [
        user_str_line("change a", timestamp=_ts(0)),
        edit(1, "t1"), _result(2, "t1"), edit(3, "t2"), _result(4, "t2"), _reply(5),
        user_str_line("that's wrong", timestamp=_ts(6)),
        edit(7, "t3"), _result(8, "t3"), _reply(9),
    ]), None)
    assert (run.edits, run.files_edited, run.rework_edits) == (3, 1, 1)
    assert (run.human_messages, run.corrections) == (2, 1)
    assert run.cut_off is None  # the main session is never "cut off"


def test_session_runs_join_notification_outcomes_to_agent_transcripts(tmp_path):
    top = _parse(tmp_path, [
        user_str_line("go", timestamp=_ts(0)),
        _reply(1),
        queue_operation_line("enqueue", content=NOTE, timestamp=_ts(5)),
    ], "top.jsonl")
    agent = _agent(tmp_path, [user_str_line("brief", timestamp=_ts(0)), _reply(1)], agent_id="abc123")
    runs = quality.session_runs(NS(top=top, subs=[agent], session_id="s1"), None)
    assert [r.group for r in runs] == [quality.MAIN, "Explore"]
    assert runs[1].outcome == "failed"
    assert quality.SIGNAL_BY_KEY["unfinished"].num(runs[1]) == 1.0


# -- comparing ----------------------------------------------------------------------


def _runs(n: int, calls: int, errors: int, group="Explore", **kw) -> list[quality.Run]:
    return [quality.Run(group=group, kind="subagent", tool_calls=calls, tool_errors=errors, replies=10,
                        cut_off=False, **kw) for _ in range(n)]


def _row(rows, key):
    return next(r for r in rows if r["key"] == key)


def test_a_clear_rise_in_failures_is_worse_with_its_counts():
    rows = quality.compare_runs(_runs(20, 50, 1), _runs(20, 50, 8), [quality.SIGNAL_BY_KEY["tool_errors"]])
    row = _row(rows, "tool_errors")
    assert row["label_key"] == "worse" and row["verdict"] == "Worse"
    assert row["before_counts"] == "20 of 1,000 tool calls" and row["after_text"] == "16%"
    assert quality.verdict(rows) == "Quality looks worse: tool calls that failed rose from 2.0% to 16%."


def test_too_few_runs_is_too_little_data():
    rows = quality.compare_runs(_runs(3, 50, 1), _runs(20, 50, 8), [quality.SIGNAL_BY_KEY["tool_errors"]])
    assert _row(rows, "tool_errors")["label_key"] == "too_little_data"
    assert quality.verdict([_row(rows, "tool_errors")]) == (
        "Too few runs to judge quality yet: 3 before and 20 after, and at least 5 are needed on each side."
    )


def test_a_real_but_tiny_move_in_a_share_is_no_clear_change():
    before = _runs(200, 1000, 0)
    after = _runs(200, 1000, 0)
    for run in after[:100]:
        run.tool_errors = 3  # 0.15%: consistent across runs, so "significant", but tiny
    row = _row(quality.compare_runs(before, after, [quality.SIGNAL_BY_KEY["tool_errors"]]), "tool_errors")
    assert row["p"] < quality.ALPHA
    assert row["label_key"] == "no_clear_change"


def test_one_bad_run_counts_as_one_run_not_as_many_failures():
    before = _runs(10, 50, 1)
    after = _runs(9, 50, 1) + _runs(1, 50, 40)
    row = _row(quality.compare_runs(before, after, [quality.SIGNAL_BY_KEY["tool_errors"]]), "tool_errors")
    assert row["label_key"] in ("no_clear_change", "possibly_worse")


@pytest.mark.parametrize("p_values, labels", [
    ((0.02, 0.5, 0.5), ["possibly_worse", "no_clear_change", "no_clear_change"]),  # 0.02 > 0.05 / 3
    ((0.001, 0.02, 0.5), ["worse", "worse", "no_clear_change"]),  # 0.02 < 0.05 / 2 once the first holds
])
def test_holm_correction_across_the_signals_compared(monkeypatch, p_values, labels):
    signals = [quality.SIGNAL_BY_KEY[k] for k in ("tool_errors", "shell_errors", "denials")]
    before = _runs(12, 40, 1, shell_calls=20, shell_errors=1, denials=1)
    after = _runs(12, 40, 6, shell_calls=20, shell_errors=4, denials=4)
    for run in after[::2]:
        run.tool_errors, run.shell_errors, run.denials = 2, 2, 2
    queue = iter(p_values)
    monkeypatch.setattr(quality, "_p_value", lambda z: next(queue))
    rows = quality.compare_runs(before, after, signals)
    assert [r["label_key"] for r in rows] == labels


def test_neutral_measures_are_higher_or_lower_not_better_or_worse():
    before = [quality.Run(kind="subagent", replies=10 + i % 3) for i in range(20)]
    after = [quality.Run(kind="subagent", replies=30 + i % 3) for i in range(20)]
    assert _row(quality.compare_runs(before, after, [quality.SIGNAL_BY_KEY["replies"]]), "replies")["label_key"] == "higher"


@pytest.mark.parametrize("value, text", [(0.0, "0%"), (0.0004, "under 0.1%"), (0.012, "1.2%"), (0.25, "25%")])
def test_share_text(value, text):
    assert quality.value_text(quality.SIGNAL_BY_KEY["tool_errors"], value) == text


def test_main_only_signals_are_not_compared_for_agents():
    keys = {s.key for s in quality.signals_for("Explore")}
    assert "corrections" not in keys and "unfinished" in keys
    assert "unfinished" not in {s.key for s in quality.signals_for(quality.MAIN)}


# -- the report section ----------------------------------------------------------------


def _setup_runs(model: str, n: int, errors: int, agent="Explore") -> list[quality.Run]:
    return [quality.Run(group=agent, kind="subagent", model=model, effort="high", replies=10, tool_calls=50,
                        tool_errors=errors, cut_off=False) for _ in range(n)]


def test_setups_are_compared_with_the_most_used_one():
    runs = _setup_runs("claude-sonnet-5", 30, 1) + _setup_runs("claude-haiku-4-5", 20, 10) + _setup_runs(
        "claude-opus-5", 2, 1
    ) + [quality.Run(group="Explore", kind="subagent", cut_off=True)]  # never replied: left out
    rows = {r["model"]: r for r in quality.setup_rows(runs)}
    assert set(rows) == {"claude-sonnet-5", "claude-haiku-4-5", "claude-opus-5"}
    assert rows["claude-sonnet-5"]["setup_verdict"] == "baseline"
    assert rows["claude-haiku-4-5"]["setup_verdict"] == "worse"
    assert rows["claude-haiku-4-5"]["compared_model"] == "claude-sonnet-5"
    assert "Worse: tool calls that failed" in rows["claude-haiku-4-5"]["difference"]
    assert rows["claude-opus-5"]["setup_verdict"] == "too_little_data"


def test_clearly_worse_on_one_signal_and_clearly_better_on_another_is_mixed():
    def row(label_key):
        return {"label_key": label_key, "worse_when": "higher"}

    assert quality.setup_verdict([row("worse"), row("better")]) == "mixed"
    assert quality.setup_verdict([row("worse"), row("possibly_better")]) == "worse"
    assert quality.setup_verdict([row("possibly_worse"), row("better")]) == "possibly_worse"
    # A signal with no direction (replies per run) never makes it mixed.
    assert quality.setup_verdict([row("worse"), {"label_key": "better", "worse_when": None}]) == "worse"


def test_worse_models_names_each_agent_and_model_family_that_did_worse():
    rows = [
        {"agent_type": "claude-implementer", "model": "claude-haiku-4-5-20251001", "setup_verdict": "worse",
         "compared_model": "claude-sonnet-5"},
        {"agent_type": quality.MAIN, "model": "claude-sonnet-5", "setup_verdict": "worse",
         "compared_model": "claude-opus-5"},
        # The same model at another effort: about effort, not the model.
        {"agent_type": "Explore", "model": "claude-haiku-4-5", "setup_verdict": "worse",
         "compared_model": "claude-haiku-4-5-20251001"},
        {"agent_type": "Plan", "model": "claude-sonnet-5", "setup_verdict": "mixed", "compared_model": "claude-opus-5"},
    ]
    assert set(quality.worse_models(rows)) == {("claude-implementer", "haiku"), ("top-level", "sonnet")}


def test_section_tables_and_columns():
    runs = _setup_runs("claude-sonnet-5", 6, 2) + [quality.Run(group=quality.MAIN, replies=5, human_messages=2)]
    runs[0].tool_errors_by_tool = {"Bash": 2}
    runs[0].turn_limit = True
    section = quality.build_section(runs)
    assert section.key == "quality"
    tables = {t.name: t for t in section.tables}
    assert set(tables) == {"quality_by_agent", "quality_by_setup", "quality_failing_tools", "quality_counts"}
    by_agent = tables["quality_by_agent"]
    keys = [c.key for c in by_agent.columns]
    assert {"unfinished_pct", "turn_limit_pct", "tool_errors_pct", "replies_per_run"} <= set(keys)
    groups = [row[0] for row in by_agent.rows]
    assert groups == [quality.MAIN, "Explore"]  # the pooled row only with two agent types or more
    explore = by_agent.rows[groups.index("Explore")]
    assert explore[keys.index("turn_limit_pct")] == pytest.approx(16.7)
    assert tables["quality_failing_tools"].rows[0][:3] == ["Explore", "Bash", 2]
    counts = tables["quality_counts"]
    assert "turn_limit" in [c.key for c in counts.columns]


@pytest.mark.parametrize("state, outcome", [("done", "completed"), ("error", "failed"), ("progress", "stopped")])
def test_a_workflow_agents_run_file_state_is_its_outcome(tmp_path, state, outcome):
    result = _agent(tmp_path, [user_str_line("brief", timestamp=_ts(0)), _reply(1)], agent_type="workflow-subagent")
    result.meta.workflow_agent_state = state
    assert quality.run_facts(result, None).outcome == outcome


def test_a_notification_outcome_wins_over_the_run_file(tmp_path):
    result = _agent(tmp_path, [user_str_line("brief", timestamp=_ts(0)), _reply(1)], agent_id="abc123")
    result.meta.workflow_agent_state = "done"
    assert quality.run_facts(result, None, outcomes={"abc123": "failed"}).outcome == "failed"
