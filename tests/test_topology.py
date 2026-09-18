"""``topology.py``: the (a)-(i) accumulator metrics and ``build_section``,
against a synthetic top-level transcript plus a small agent/skill tree
(one skill spawning two agents, one non-skill agent) built with
``write_jsonl`` + ``parse_transcript`` per the codebase's own fixture
convention (see ``tests/test_dedupe.py``, ``tests/test_discovery.py``).

Scenario shared by most tests here (``_build_scenario``):

- top-level transcript, 4 priced turns:
    top_1: effort=high, spawns agent-a1 (Agent tool) under skill
           "grill-me", cache_creation=5000 (session baseline)
    top_2: effort=medium, spawns agent-b1 (Agent tool, no skill),
           Bash "npm test", attribution_mcp_server=figma
    [compact_boundary event]
    top_3: effort=medium, Read tool (rediscovery candidate)
    top_4: effort=high, Bash "npm test" (repeats top_2's prefix)
  plus a REMINDER, a HOOK_OUTPUT and a CACHE_SIGNAL(plan_mode) attachment,
  and Agent tool_result blocks for both spawns.

- agent-a1 (claude-implementer, tool_use_id=tu_agentA1, depth 1): direct
  spawn of skill "grill-me" via top_1's tool_use_id.
- agent-a1-child (claude-implementer, parent_agent_id=agent-a1, depth 2):
  spawned *by* agent-a1 -- reached only via the skill roll-up's
  parent_agent_id closure, not by tool_use_id.
- agent-b1 (general-purpose, tool_use_id=tu_agentB1, depth 1,
  stopped_by_user=True): spawned by top_2, which carries no
  attribution_skill, so agent-b1 belongs to no skill's roll-up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_token_lens.model import TranscriptMeta
from claude_token_lens.parse import parse_transcript
from claude_token_lens.pricing import load_pricing, price_turn
from claude_token_lens.topology import TopologyStats, build_section

from helpers import (
    assert_privacy,
    attachment_line,
    system_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    write_jsonl,
)


def _with_thinking(line: dict, thinking_tokens: int) -> dict:
    """``turn_line`` has no convenience kwarg for thinking_tokens (it lives
    at ``message.usage.output_tokens_details.thinking_tokens``); mutate the
    dict it returns instead of hand-building the whole line.
    """
    line["message"]["usage"]["output_tokens_details"] = {"thinking_tokens": thinking_tokens}
    return line


def _parse(tmp_path: Path, name: str, lines: list[dict], **meta_kwargs):
    path = tmp_path / f"{name}.jsonl"
    write_jsonl(path, lines)
    meta = TranscriptMeta(path=str(path), session_id="sess-1", **meta_kwargs)
    return parse_transcript(path, meta)


def _build_scenario(tmp_path: Path):
    top_lines = [
        _with_thinking(
            turn_line(
                message_id="top_1",
                cache_creation_input_tokens=5000,
                ephemeral_5m_input_tokens=5000,
                output_tokens=200,
                effort="high",
                attributionSkill="grill-me",
                content=[tool_use_block("Agent", "tu_agentA1", {"subagent_type": "claude-implementer"})],
            ),
            50,
        ),
        user_block_line([tool_result_block("tu_agentA1", "x" * 400)]),
        attachment_line("total_tokens_reminder", rendered="r" * 40),
        attachment_line("hook_success", rendered="h" * 20),
        attachment_line("plan_mode", rendered="p" * 10),
        _with_thinking(
            turn_line(
                message_id="top_2",
                output_tokens=300,
                effort="medium",
                attributionMcpServer="figma",
                content=[
                    tool_use_block("Agent", "tu_agentB1", {"subagent_type": "general-purpose"}),
                    tool_use_block("Bash", "tu_bash1", {"command": "npm test"}),
                ],
            ),
            30,
        ),
        user_block_line([tool_result_block("tu_agentB1", "y" * 200)]),
        system_line(
            "compact_boundary",
            compactMetadata={
                "preTokens": 100000,
                "postTokens": 20000,
                "cumulativeDroppedTokens": 80000,
                "durationMs": 500,
                "trigger": "auto",
            },
        ),
        _with_thinking(
            turn_line(
                message_id="top_3",
                output_tokens=100,
                effort="medium",
                content=[tool_use_block("Read", "tu_read1", {"file_path": "/tmp/x.txt"})],
            ),
            10,
        ),
        _with_thinking(
            turn_line(
                message_id="top_4",
                output_tokens=150,
                effort="high",
                content=[tool_use_block("Bash", "tu_bash2", {"command": "npm test"})],
            ),
            100,
        ),
    ]
    top = _parse(tmp_path, "top", top_lines, kind="top-level")

    a1_lines = [
        _with_thinking(
            turn_line(
                message_id="a1_1",
                cache_creation_input_tokens=2000,
                ephemeral_5m_input_tokens=2000,
                output_tokens=100,
                effort="medium",
            ),
            20,
        ),
        _with_thinking(turn_line(message_id="a1_2", output_tokens=400, effort="medium"), 40),
    ]
    agent_a1 = _parse(
        tmp_path,
        "agent-a1",
        a1_lines,
        kind="subagent",
        agent_id="agent-a1",
        agent_type="claude-implementer",
        tool_use_id="tu_agentA1",
        spawn_depth=1,
    )

    a1c_lines = [
        _with_thinking(
            turn_line(
                message_id="a1c_1",
                cache_creation_input_tokens=1000,
                ephemeral_5m_input_tokens=1000,
                output_tokens=80,
                effort="low",
            ),
            5,
        ),
        _with_thinking(turn_line(message_id="a1c_2", output_tokens=150, effort="low"), 15),
    ]
    agent_a1_child = _parse(
        tmp_path,
        "agent-a1-child",
        a1c_lines,
        kind="subagent",
        agent_id="agent-a1-child",
        agent_type="claude-implementer",
        parent_agent_id="agent-a1",
        spawn_depth=2,
    )

    b1_lines = [
        _with_thinking(
            turn_line(
                message_id="b1_1",
                cache_creation_input_tokens=3000,
                ephemeral_5m_input_tokens=3000,
                output_tokens=90,
                effort="high",
            ),
            10,
        ),
        _with_thinking(turn_line(message_id="b1_2", output_tokens=250, effort="high"), 60),
    ]
    agent_b1 = _parse(
        tmp_path,
        "agent-b1",
        b1_lines,
        kind="subagent",
        agent_id="agent-b1",
        agent_type="general-purpose",
        tool_use_id="tu_agentB1",
        spawn_depth=1,
        stopped_by_user=True,
    )

    subs = [agent_a1, agent_a1_child, agent_b1]
    pricing = load_pricing()
    return top, subs, pricing


def _cost(result, pricing) -> float:
    total = 0.0
    for turn in result.turns:
        if turn.turn_index == 0:
            continue
        total += price_turn(turn, pricing.resolve_model(turn.model)).total
    return total


# -- privacy -------------------------------------------------------------


def test_scenario_fixtures_pass_privacy_scan(tmp_path):
    top, subs, _pricing = _build_scenario(tmp_path)
    assert_privacy(top)
    for sub in subs:
        assert_privacy(sub)


# -- (a) downward: spawn write, session baseline -----------------------


def test_downward_spawn_write_and_session_baseline(tmp_path):
    top, subs, pricing = _build_scenario(tmp_path)
    stats = TopologyStats()
    stats.add_session("sess-1", top, subs, pricing)

    assert stats.session_baseline_writes == [5000]
    assert stats.spawn_write_by_agent_type["claude-implementer"] == [2000, 1000]
    assert stats.spawn_write_by_agent_type["general-purpose"] == [3000]


# -- (b) upward: Agent/Workflow tool_result, report proxy ----------------


def test_upward_agent_tool_result_and_report_proxy(tmp_path):
    top, subs, pricing = _build_scenario(tmp_path)
    stats = TopologyStats()
    stats.add_session("sess-1", top, subs, pricing)

    assert stats.agent_tool_result_chars == 600  # 400 + 200
    assert stats.agent_tool_result_calls == 2
    assert stats.workflow_tool_result_chars == 0
    assert stats.workflow_tool_result_calls == 0

    assert stats.report_proxy_by_agent_type["claude-implementer"] == [400, 150]
    assert stats.report_proxy_by_agent_type["general-purpose"] == [250]


# -- (c) skill roll-up: direct + spawned = total --------------------------


def test_skill_rollup_direct_plus_spawned_equals_total(tmp_path):
    top, subs, pricing = _build_scenario(tmp_path)
    stats = TopologyStats()
    stats.add_session("sess-1", top, subs, pricing)

    acc = stats.skills["grill-me"]
    assert acc.invocations == 1
    assert acc.direct_spawns == 1  # only agent-a1 (agent-b1 has no skill turn)
    assert acc.report_proxy_values == [400, 150]  # agent-a1 then agent-a1-child

    top_1_turn = next(t for t in top.turns if t.message_id == "top_1")
    a1 = subs[0]
    a1_child = subs[1]
    expected_direct = price_turn(top_1_turn, pricing.resolve_model(top_1_turn.model)).total
    expected_spawned = _cost(a1, pricing) + _cost(a1_child, pricing)

    assert acc.direct_cost == pytest.approx(expected_direct)
    assert acc.spawned_cost == pytest.approx(expected_spawned)
    assert (acc.direct_cost + acc.spawned_cost) == pytest.approx(expected_direct + expected_spawned)

    # agent-b1 was spawned under a non-skill turn -- never rolled into any skill.
    assert "agent-b1" not in [s.meta.agent_id for s in [a1, a1_child]]


def test_skill_rollup_mean_spawns_per_invocation(tmp_path):
    top, subs, pricing = _build_scenario(tmp_path)
    stats = TopologyStats()
    stats.add_session("sess-1", top, subs, pricing)

    acc = stats.skills["grill-me"]
    assert acc.direct_spawns / acc.invocations == 1.0


# -- (d) chains: depth histogram, cost/spawn, stopped_by_user -----------


def test_chains_spawn_depth_histogram_and_totals(tmp_path):
    top, subs, pricing = _build_scenario(tmp_path)
    stats = TopologyStats()
    stats.add_session("sess-1", top, subs, pricing)

    assert stats.spawn_depth_histogram == {1: 2, 2: 1}
    assert stats.total_spawns == 3
    assert stats.spawns_per_session == [3]
    assert stats.stopped_by_user_count == 1


def test_chains_cost_by_agent_type(tmp_path):
    top, subs, pricing = _build_scenario(tmp_path)
    stats = TopologyStats()
    stats.add_session("sess-1", top, subs, pricing)

    a1, a1_child, b1 = subs
    costs = stats.cost_by_agent_type
    assert len(costs["claude-implementer"]) == 2
    assert len(costs["general-purpose"]) == 1
    assert sorted(costs["claude-implementer"]) == pytest.approx(
        sorted([_cost(a1, pricing), _cost(a1_child, pricing)])
    )
    assert costs["general-purpose"][0] == pytest.approx(_cost(b1, pricing))


# -- (e) reminder/hook pressure, CACHE_SIGNAL histogram ------------------


def test_reminder_hook_pressure_and_cache_signal_histogram(tmp_path):
    top, subs, pricing = _build_scenario(tmp_path)
    stats = TopologyStats()
    stats.add_session("sess-1", top, subs, pricing)

    # top-level: 1 REMINDER, 1 HOOK_OUTPUT over 4 priced turns.
    assert stats.reminder_rate_by_kind["top-level"] == [0.25]
    assert stats.hook_rate_by_kind["top-level"] == [0.25]
    # subagents: no reminders/hooks at all.
    assert stats.reminder_rate_by_kind["subagent"] == [0.0, 0.0, 0.0]
    assert stats.hook_rate_by_kind["subagent"] == [0.0, 0.0, 0.0]

    assert stats.cache_signal_subkind_histogram == {"plan_mode": 1}


# -- (f) MCP cost ----------------------------------------------------------


def test_mcp_cost_by_server(tmp_path):
    top, subs, pricing = _build_scenario(tmp_path)
    stats = TopologyStats()
    stats.add_session("sess-1", top, subs, pricing)

    top_2_turn = next(t for t in top.turns if t.message_id == "top_2")
    expected = price_turn(top_2_turn, pricing.resolve_model(top_2_turn.model)).total

    assert stats.mcp_turns_by_server == {"figma": 1}
    assert stats.mcp_cost_by_server["figma"] == pytest.approx(expected)


# -- (g) effort / thinking share -------------------------------------------


def test_effort_output_and_thinking_by_effort_value(tmp_path):
    top, subs, pricing = _build_scenario(tmp_path)
    stats = TopologyStats()
    stats.add_session("sess-1", top, subs, pricing)

    # high: top_1(200,50) + top_4(150,100) + b1_1(90,10) + b1_2(250,60)
    assert stats.effort_turns["high"] == 4
    assert stats.effort_output["high"] == 690
    assert stats.effort_thinking["high"] == 220

    # medium: top_2(300,30) + top_3(100,10) + a1_1(100,20) + a1_2(400,40)
    assert stats.effort_turns["medium"] == 4
    assert stats.effort_output["medium"] == 900
    assert stats.effort_thinking["medium"] == 100

    # low: a1c_1(80,5) + a1c_2(150,15)
    assert stats.effort_turns["low"] == 2
    assert stats.effort_output["low"] == 230
    assert stats.effort_thinking["low"] == 20


def test_effort_by_agent_type(tmp_path):
    top, subs, pricing = _build_scenario(tmp_path)
    stats = TopologyStats()
    stats.add_session("sess-1", top, subs, pricing)

    assert stats.agent_type_output["top-level"] == 750  # 200+300+100+150
    assert stats.agent_type_thinking["top-level"] == 190  # 50+30+10+100

    assert stats.agent_type_output["claude-implementer"] == 730  # 100+400+80+150
    assert stats.agent_type_thinking["claude-implementer"] == 80  # 20+40+5+15

    assert stats.agent_type_output["general-purpose"] == 340  # 90+250
    assert stats.agent_type_thinking["general-purpose"] == 70  # 10+60


# -- (h) context composition -------------------------------------------------


def test_context_composition_per_transcript_kind(tmp_path):
    top, subs, pricing = _build_scenario(tmp_path)
    stats = TopologyStats()
    stats.add_session("sess-1", top, subs, pricing)

    assert stats.composition_transcripts == {"top-level": 1, "subagent": 3}

    assert stats.composition_baseline["top-level"] == [5000]
    assert stats.composition_baseline["subagent"] == [2000, 1000, 3000]

    # top's Agent tool_result chars total 600 -> 600/4 = 150 approx tokens.
    assert stats.composition_tool_result_tokens["top-level"] == [150.0]
    assert stats.composition_tool_result_tokens["subagent"] == [0.0, 0.0, 0.0]

    assert stats.composition_output_tokens["top-level"] == [750]
    assert stats.composition_output_tokens["subagent"] == [500, 230, 340]

    # top's REMINDER(40 chars) + CACHE_SIGNAL(10 chars) = 50 -> 50/4 = 12.5.
    assert stats.composition_attachment_tokens["top-level"] == [12.5]
    assert stats.composition_attachment_tokens["subagent"] == [0.0, 0.0, 0.0]

    assert stats.composition_compactions["top-level"] == [0]  # COMPACT_BOUNDARY != COMPACT_SUMMARY


# -- (i) redundant work: repeated cmd prefix, rediscovery ----------------


def test_redundant_cmd_prefix_and_rediscovery(tmp_path):
    top, subs, pricing = _build_scenario(tmp_path)
    stats = TopologyStats()
    stats.add_session("sess-1", top, subs, pricing)

    # "npm test" appears as cmd_prefix on top_2 and top_4 -> 2 occurrences, 1 repeat.
    assert stats.redundant_cmd_repeats_per_session == [1]
    # compact_boundary precedes top_3; top_3 uses Read within the 10-turn window.
    assert stats.rediscovery_reads_per_session == [1]


# -- build_section -----------------------------------------------------------


def test_build_section_produces_one_table_per_plan_item(tmp_path):
    top, subs, pricing = _build_scenario(tmp_path)
    stats = TopologyStats()
    stats.add_session("sess-1", top, subs, pricing)

    section = build_section(stats)
    assert section.key == "agents"
    assert section.title == "Agents and information flow"
    assert len(section.tables) == 16

    skills_table = next(t for t in section.tables if t.name == "topology_skills_rollup")
    row = next(r for r in skills_table.rows if r[0] == "grill-me")
    # total_cost column (index 4) == direct_cost (2) + spawned_cost (3).
    assert row[4] == pytest.approx(row[2] + row[3])


def test_build_section_empty_stats_never_raises():
    stats = TopologyStats()
    section = build_section(stats)
    assert section.key == "agents"
    assert len(section.tables) == 16
    # These tables always emit fixed rows (Agent/Workflow, the baseline
    # summary row, the truncation-signal summary) regardless of whether
    # any data was ever added; every other table is empty-row.
    always_populated = {
        "topology_session_baseline",
        "topology_chains_summary",
        "topology_upward_tool_result",
        "topology_redundant_work",
    }
    for table in section.tables:
        assert table.rows == [] or table.name in always_populated


# -- multi-session accumulation ---------------------------------------------


def test_add_session_accumulates_across_multiple_sessions(tmp_path):
    s1_dir = tmp_path / "s1"
    s2_dir = tmp_path / "s2"
    s1_dir.mkdir()
    s2_dir.mkdir()
    top1, subs1, pricing = _build_scenario(s1_dir)
    top2, subs2, _ = _build_scenario(s2_dir)

    stats = TopologyStats()
    stats.add_session("sess-1", top1, subs1, pricing)
    stats.add_session("sess-2", top2, subs2, pricing)

    assert stats.sessions_seen == 2
    assert stats.total_spawns == 6
    assert stats.session_baseline_writes == [5000, 5000]
    assert stats.skills["grill-me"].invocations == 2
