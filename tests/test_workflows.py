"""``workflows.py``: parsing ``<session>/workflows/wf_*.json`` run files and
linking a run's cost to the subagent transcripts it spawned (WP8).

Fixtures under ``tests/fixtures/topology/`` use the real observed key
names (``runId``, ``agentCount``, ``phases``, ``startTime``, ``durationMs``,
``status``, ``workflowName``, ``defaultModel``, ...) with synthetic
placeholder values only -- never real task content.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_token_lens import workflows
from claude_token_lens.model import TranscriptMeta, WorkflowRun
from claude_token_lens.parse import parse_transcript
from claude_token_lens.pricing import load_pricing, price_turn

from helpers import assert_privacy, turn_line, write_jsonl

FIXTURES = Path(__file__).parent / "fixtures" / "topology"


# -- parse_workflow_file -----------------------------------------------------


def test_parse_workflow_file_reads_documented_keys():
    path = FIXTURES / "wf_sample_session" / "workflows" / "wf_test0000-000.json"
    run = workflows.parse_workflow_file(path)

    assert run.run_id == "wf_test0000-000"
    assert run.session_id == "wf_sample_session"
    assert run.agent_count == 4
    assert run.phases == 2  # len(phases), not the phase titles/details
    assert run.started == "2026-09-18T12:00:00.000Z"
    assert run.finished == "2026-09-18T12:02:00.000Z"  # startTime + durationMs
    assert run.cost == 0.0  # link_workflow_agents fills this in
    assert run.status == "completed"  # batch C addition
    assert run.phase_titles == ("Phase one", "Phase two")  # titles only, never detail


def test_parse_workflow_file_minimal_json_tolerates_missing_fields():
    path = FIXTURES / "wf_minimal_session" / "workflows" / "wf_minimal-000.json"
    run = workflows.parse_workflow_file(path)

    assert run.run_id == "wf_minimal-000"
    assert run.session_id == "wf_minimal_session"
    assert run.agent_count == 1
    assert run.phases == 1
    # No startTime/durationMs/timestamp in this fixture -> both None.
    assert run.started is None
    assert run.finished is None
    assert run.cost == 0.0
    assert run.status == "killed"
    assert run.phase_titles == ("Only phase",)


def test_parse_workflow_file_malformed_json_never_raises(tmp_path):
    session_dir = tmp_path / "sess-bad" / "workflows"
    session_dir.mkdir(parents=True)
    path = session_dir / "wf_broken-000.json"
    path.write_text("{not valid json", encoding="utf-8")

    run = workflows.parse_workflow_file(path)
    assert run.run_id == "wf_broken-000"  # falls back to the filename stem
    assert run.session_id == "sess-bad"
    assert run.agent_count == 0
    assert run.phases == 0
    assert run.started is None
    assert run.status is None
    assert run.phase_titles == ()


def test_parse_workflow_file_missing_file_never_raises(tmp_path):
    session_dir = tmp_path / "sess-missing" / "workflows"
    session_dir.mkdir(parents=True)
    path = session_dir / "wf_ghost-000.json"  # never written

    run = workflows.parse_workflow_file(path)
    assert run.run_id == "wf_ghost-000"
    assert run.agent_count == 0


def test_parse_workflow_file_falls_back_to_timestamp_when_no_start_time(tmp_path):
    session_dir = tmp_path / "sess-ts" / "workflows"
    session_dir.mkdir(parents=True)
    path = session_dir / "wf_ts-000.json"
    path.write_text('{"runId": "wf_ts-000", "timestamp": "2026-09-01T00:00:00.000Z"}', encoding="utf-8")

    run = workflows.parse_workflow_file(path)
    assert run.started == "2026-09-01T00:00:00.000Z"
    assert run.finished is None  # no durationMs to compute an end from


def test_parse_workflow_file_phase_titles_never_carry_detail_text(tmp_path):
    session_dir = tmp_path / "sess-detail" / "workflows"
    session_dir.mkdir(parents=True)
    path = session_dir / "wf_detail-000.json"
    path.write_text(
        '{"runId": "wf_detail-000", "phases": ['
        '{"title": "Discovery", "detail": "full task prompt text that must never be stored"}, '
        '{"detail": "a phase with no title at all"}'
        "]}",
        encoding="utf-8",
    )

    run = workflows.parse_workflow_file(path)
    assert run.phases == 2
    assert run.phase_titles == ("Discovery",)  # untitled entry skipped, no detail text anywhere
    for title in run.phase_titles:
        assert "prompt text" not in title


def test_parse_workflow_file_non_string_status_is_ignored(tmp_path):
    session_dir = tmp_path / "sess-status" / "workflows"
    session_dir.mkdir(parents=True)
    path = session_dir / "wf_status-000.json"
    path.write_text('{"runId": "wf_status-000", "status": 42}', encoding="utf-8")

    run = workflows.parse_workflow_file(path)
    assert run.status is None


# -- link_workflow_agents -----------------------------------------------------


def _write_agent(tmp_path: Path, run_id: str, name: str, output_tokens: int) -> object:
    """Build a workflow-nested subagent transcript at
    ``.../subagents/workflows/<run_id>/<name>.jsonl`` -- the real observed
    layout ``link_workflow_agents`` matches by parent-directory name.
    """
    agent_dir = tmp_path / "subagents" / "workflows" / run_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / f"{name}.jsonl"
    write_jsonl(
        path,
        [
            turn_line(message_id=f"{name}_1", output_tokens=output_tokens, cache_creation_input_tokens=500),
        ],
    )
    meta = TranscriptMeta(path=str(path), kind="workflow-agent", session_id="sess-wf")
    return parse_transcript(path, meta)


def test_link_workflow_agents_matches_by_parent_directory_and_sums_cost(tmp_path):
    from claude_token_lens.model import WorkflowRun

    run = WorkflowRun(run_id="wf_run_a", session_id="sess-wf", agent_count=2, phases=1)

    matched_1 = _write_agent(tmp_path, "wf_run_a", "agent-1", output_tokens=200)
    matched_2 = _write_agent(tmp_path, "wf_run_a", "agent-2", output_tokens=300)
    unrelated = _write_agent(tmp_path, "wf_run_b", "agent-3", output_tokens=999)

    pricing = load_pricing()
    result = workflows.link_workflow_agents(run, [matched_1, matched_2, unrelated], pricing)

    assert result == [matched_1, matched_2]
    assert unrelated not in result

    expected_cost = 0.0
    for sub in (matched_1, matched_2):
        for turn in sub.turns:
            if turn.turn_index == 0:
                continue
            expected_cost += price_turn(turn, pricing.resolve_model(turn.model)).total
    assert run.cost == expected_cost

    for sub in (matched_1, matched_2, unrelated):
        assert_privacy(sub)


def test_link_workflow_agents_no_matches_leaves_cost_zero(tmp_path):
    from claude_token_lens.model import WorkflowRun

    run = WorkflowRun(run_id="wf_lonely", session_id="sess-wf")
    unrelated = _write_agent(tmp_path, "wf_other", "agent-x", output_tokens=100)

    pricing = load_pricing()
    result = workflows.link_workflow_agents(run, [unrelated], pricing)

    assert result == []
    assert run.cost == 0.0


def test_link_workflow_agents_ignores_subs_with_no_path(tmp_path):
    from claude_token_lens.model import TranscriptResult, WorkflowRun

    run = WorkflowRun(run_id="wf_no_path")
    orphan = TranscriptResult(meta=TranscriptMeta(path="", kind="workflow-agent"))

    pricing = load_pricing()
    result = workflows.link_workflow_agents(run, [orphan], pricing)

    assert result == []
    assert run.cost == 0.0


# -- build_section --------------------------------------------------------


def _runs():
    return [
        WorkflowRun(
            run_id="wf_a",
            session_id="sess_a",
            agent_count=3,
            phases=2,
            started="2026-09-18T12:00:00.000Z",
            finished="2026-09-18T12:05:00.000Z",
            cost=1.5,
            status="completed",
        ),
        WorkflowRun(
            run_id="wf_b",
            session_id="sess_b",
            agent_count=1,
            phases=1,
            started="2026-09-18T13:00:00.000Z",
            finished="2026-09-18T13:01:00.000Z",
            cost=0.25,
            status="killed",
        ),
    ]


def test_build_section_shape_and_summary_totals():
    section = workflows.build_section(_runs())
    assert section.key == "workflows"
    assert section.title == "Workflows"
    table_names = [t.name for t in section.tables]
    assert table_names == ["workflows_summary", "workflows_status_mix", "workflows_detail"]

    summary = {row[0]: row[1] for row in section.tables[0].rows}
    assert summary["Total workflow runs"] == 2
    assert summary["Total agents spawned"] == 4
    assert summary["Total cost (USD)"] == pytest.approx(1.75)
    assert summary["Mean agents per run"] == pytest.approx(2.0)
    assert summary["Mean cost per run (USD)"] == pytest.approx(0.875)

    assert_privacy(section)


def test_build_section_status_mix_counts_each_status():
    section = workflows.build_section(_runs())
    status_table = section.tables[1]
    by_status = {row[0]: row for row in status_table.rows}
    assert by_status["completed"][1] == 1
    assert by_status["killed"][1] == 1
    assert by_status["completed"][2] == pytest.approx(50.0)


def test_build_section_status_mix_falls_back_to_unknown():
    run = WorkflowRun(run_id="wf_no_status", agent_count=1, phases=1, cost=0.0, status=None)
    section = workflows.build_section([run])
    status_table = section.tables[1]
    assert status_table.rows[0][0] == "unknown"


def test_build_section_detail_table_sorted_by_cost_descending():
    section = workflows.build_section(_runs())
    detail = section.tables[2]
    assert [row[0] for row in detail.rows] == ["wf_a", "wf_b"]
    assert detail.rows[0][5] == pytest.approx(1.5)


def test_build_section_detail_table_caps_at_limit_with_a_note():
    runs = [
        WorkflowRun(run_id=f"wf_{i}", session_id="sess", agent_count=1, phases=1, cost=float(i))
        for i in range(25)
    ]
    section = workflows.build_section(runs)
    detail = section.tables[2]
    assert len(detail.rows) == 20
    assert detail.notes
    assert "20" in detail.notes[0]


def test_build_section_empty_runs_has_no_data_note():
    section = workflows.build_section([])
    assert section.tables[0].rows  # summary table still has metric rows
    assert any("No workflow runs" in note for note in section.notes)


def test_build_section_never_reads_phase_titles_or_script_fields():
    """phase_titles carries WorkflowRun's own titles, which are safe to
    store on the dataclass (see the module docstring) but build_section
    still never surfaces them -- only counts and identifiers."""
    run = WorkflowRun(
        run_id="wf_titled",
        session_id="sess",
        agent_count=1,
        phases=1,
        cost=0.0,
        phase_titles=("a phase title that must not leak into a table cell",),
    )
    section = workflows.build_section([run])
    for table in section.tables:
        for row in table.rows:
            for cell in row:
                assert "phase title" not in str(cell)
    assert_privacy(section)
