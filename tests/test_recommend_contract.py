"""Cross-module contract test (final task of the 12-item review fix list):
every ``Table`` produced by every ``build_section``/``build_config_section``
entry point must have a non-empty first column usable as a row key --
``row[0]`` is a ``str`` or an ``int`` on every row, for every table. A
report renderer (Markdown/HTML, or a future ``--group-by`` drill-down)
needs to key off column zero uniformly, without a per-table special case
for "this one's first column might be ``None``/a float/a tuple".

Exercises every module that defines one of these entry points as of this
task: ``classify``, ``compaction``, ``recache``, ``ttl``, ``workstyle``,
``workflows``, and ``snapshots``. Each gets the smallest hand-built fixture
that produces at least one real (non-empty) row per table where the
module's own logic allows it, following the same ``_turn``/``write_jsonl``
construction patterns already used in each module's own test file.

WP10a addition: ``report.build_report``, ``usage.build_section`` and
``scorecard.build_section`` (and, transitively through ``build_report``,
every section ``report.py`` assembles, including ``topology``'s and
``phases``'s, which had no dedicated check here before) are exercised the
same way.
"""

from __future__ import annotations

from pathlib import Path

from claude_token_lens import classify, compaction, recache, report, scorecard, snapshots, ttl, usage, workflows, workstyle
from claude_token_lens.config import Config
from claude_token_lens.corpus import load_corpus
from claude_token_lens.model import (
    Classification,
    EventKind,
    Section,
    SessionRecord,
    TranscriptMeta,
    TranscriptResult,
    Turn,
    WorkflowRun,
)
from claude_token_lens.parse import parse_transcript
from claude_token_lens.pricing import load_pricing

from helpers import system_line, tool_use_block, turn_line, write_jsonl

PRICING = load_pricing()
SONNET_RATES = PRICING.resolve_model("claude-sonnet-5")


def _assert_row_keys_are_valid(section: Section) -> None:
    """Every row, in every table, in ``section`` has a non-empty
    ``str``/``int`` first column. ``bool`` is excluded even though it is
    technically an ``int`` subclass -- a True/False row key is never
    what a caller means by "row key"."""
    for table in section.tables:
        for row in table.rows:
            assert row, f"{section.key}/{table.name}: a row is empty"
            key = row[0]
            assert isinstance(key, (str, int)) and not isinstance(key, bool), (
                f"{section.key}/{table.name}: row[0]={key!r} "
                f"({type(key).__name__}) is not a str/int row key"
            )
            if isinstance(key, str):
                assert key != "", f"{section.key}/{table.name}: row[0] is an empty string"


# -- classify.build_section ---------------------------------------------


def _classify_records() -> list[SessionRecord]:
    return [
        SessionRecord(
            session_id="s1",
            slug="proj-a",
            first_ts="2026-09-18T10:00:00.000Z",
            span_s=120.0,
            classification=Classification(mode="interactive", purpose="general-dev"),
        ),
        SessionRecord(
            session_id="s2",
            slug="proj-b",
            first_ts="2026-09-18T23:30:00.000Z",
            span_s=600.0,
            classification=Classification(mode="overnight", purpose="review"),
        ),
    ]


def test_classify_build_section_row_keys_are_all_str_or_int():
    section = classify.build_section(_classify_records())
    _assert_row_keys_are_valid(section)


# -- compaction.build_section ---------------------------------------------


def _compaction_stats(tmp_path: Path) -> compaction.CompactionStats:
    lines = [
        turn_line(
            model="claude-sonnet-5",
            timestamp="2026-09-18T12:00:00.000Z",
            ephemeral_5m_input_tokens=1000,
            input_tokens=100,
            output_tokens=50,
            content=[tool_use_block("Bash", "tu1", {"command": "echo hi"})],
        ),
        system_line(
            "compact_boundary",
            timestamp="2026-09-18T12:00:30.000Z",
            compactMetadata={
                "trigger": "auto",
                "preTokens": 100000,
                "postTokens": 20000,
                "cumulativeDroppedTokens": 80000,
                "durationMs": 1200,
            },
        ),
        turn_line(
            model="claude-sonnet-5",
            timestamp="2026-09-18T12:01:00.000Z",
            ephemeral_1h_input_tokens=25000,
            cache_read_input_tokens=1000,
            input_tokens=0,
            output_tokens=80,
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    meta = TranscriptMeta(path=str(path), session_id="sess_contract")
    result = parse_transcript(path, meta)
    return compaction.CompactionStats.build([(result, SONNET_RATES)])


def test_compaction_build_section_row_keys_are_all_str_or_int(tmp_path):
    stats = _compaction_stats(tmp_path)
    section = compaction.build_section(stats)
    _assert_row_keys_are_valid(section)


# -- recache.build_section ---------------------------------------------


def _recache_turn(**overrides) -> Turn:
    fields = dict(
        message_id="msg",
        request_id="req",
        turn_index=1,
        ts="2026-09-18T12:00:00.000Z",
        gap_s=None,
        model="claude-sonnet-5",
        is_synthetic=False,
        input_tokens=100,
        cache_creation_tokens=100_000,
        cache_read_tokens=500,
        output_tokens=50,
        cc_5m=100_000,
        cc_1h=0,
        ctx=100_600,
        preceding_tool="none",
        preceding_primary=EventKind.UNKNOWN,
        preceding_event_kinds=(),
        preceding_attachment_types=(),
        preceding_cmd_prefix=None,
    )
    fields.update(overrides)
    return Turn(**fields)


def test_recache_build_section_row_keys_are_all_str_or_int():
    th = recache.RecacheThresholds()
    stats = recache.RecacheStats(th)
    result = TranscriptResult(
        meta=TranscriptMeta(agent_type="claude-implementer"),
        turns=[_recache_turn()],
    )
    stats.add(result, lambda model: PRICING.resolve_model(model))
    section = recache.build_section(stats, PRICING, th)
    _assert_row_keys_are_valid(section)


# -- ttl.build_section ---------------------------------------------------


def _ttl_turn(**overrides) -> Turn:
    fields = dict(
        message_id="msg_1",
        request_id="req_1",
        turn_index=1,
        ts="2026-09-18T12:00:00.000Z",
        model="claude-sonnet-5",
        input_tokens=100,
        cache_creation_tokens=1000,
        cache_read_tokens=500,
        output_tokens=50,
        cc_5m=1000,
        cc_1h=0,
        ctx=1600,
        gap_s=None,
    )
    fields.update(overrides)
    return Turn(**fields)


def test_ttl_build_section_row_keys_are_all_str_or_int():
    stats = ttl.TtlStats()
    stats.add(
        TranscriptResult(meta=TranscriptMeta(kind="top-level", agent_type="top-level"), turns=[_ttl_turn()]),
        SONNET_RATES,
    )
    section = ttl.build_section(stats, billing_mode="api")
    _assert_row_keys_are_valid(section)


# -- workstyle.build_section ---------------------------------------------


def test_workstyle_build_section_row_keys_are_all_str_or_int():
    records = [
        SessionRecord(session_id="s1", archetype="single-model"),
        SessionRecord(session_id="s2", archetype="chat-only"),
        SessionRecord(session_id="s3", archetype=None),  # excluded, not a row
    ]
    section = workstyle.build_section(records)
    _assert_row_keys_are_valid(section)


# -- workflows.build_section ----------------------------------------------


def test_workflows_build_section_row_keys_are_all_str_or_int():
    runs = [
        WorkflowRun(run_id="wf_a", session_id="s1", agent_count=2, phases=1, cost=1.0, status="completed"),
        WorkflowRun(run_id="wf_b", session_id="s2", agent_count=1, phases=1, cost=0.5, status=None),
    ]
    section = workflows.build_section(runs)
    _assert_row_keys_are_valid(section)


# -- snapshots.build_config_section ---------------------------------------


def _snapshot(tmp_path: Path, ts: str, data: dict) -> snapshots.Snapshot:
    return snapshots.Snapshot(path=tmp_path / f"{ts}.json", ts=ts, data=data)


def test_snapshots_build_config_section_row_keys_are_all_str_or_int(tmp_path):
    snaps = [
        _snapshot(tmp_path, "20260901T000000Z", {"user_settings": {"model": "sonnet"}}),
        _snapshot(tmp_path, "20260910T000000Z", {"user_settings": {"model": "opus"}}),
    ]
    sessions_with_metrics = [
        {
            "session_id": "s1",
            "first_ts": "2026-09-05T00:00:00.000Z",
            "turns": 10,
            "cost": 1.0,
            "recache_cc": 0,
            "cc_total": 100,
            "compactions": 0,
            "span_s": 60,
        },
        {
            "session_id": "s2",
            "first_ts": "2026-09-12T00:00:00.000Z",
            "turns": 5,
            "cost": 0.5,
            "recache_cc": 0,
            "cc_total": 50,
            "compactions": 0,
            "span_s": 30,
        },
    ]
    section = snapshots.build_config_section(sessions_with_metrics, snaps, "user_settings.model")
    _assert_row_keys_are_valid(section)


# -- usage.build_section ---------------------------------------------------


def test_usage_build_section_row_keys_are_all_str_or_int(tmp_path):
    project_dir = tmp_path / "proj-usage"
    project_dir.mkdir()
    write_jsonl(
        project_dir / "session-usage.jsonl",
        [turn_line(timestamp="2026-09-18T12:00:00.000Z", input_tokens=100, output_tokens=20)],
    )
    corpus = load_corpus([project_dir])
    section = usage.build_section(corpus, PRICING, Config())
    _assert_row_keys_are_valid(section)


# -- scorecard.build_section ------------------------------------------------


def test_scorecard_build_section_row_keys_are_all_str_or_int():
    inputs = scorecard.ScorecardInputs(
        recache_share_pct=10.0,
        p90_top_level_ctx=60_000,
        has_spawns=True,
        agent_cost_variance_ratio=1.4,
        has_snapshot=True,
        changed_config_keys=2,
        pricing_coverage_pct=98.0,
    )
    section = scorecard.build_section(inputs)
    _assert_row_keys_are_valid(section)


# -- report.build_report (every assembled section) --------------------------


def test_build_report_every_section_row_keys_are_all_str_or_int(tmp_path):
    project_dir = tmp_path / "proj-report"
    project_dir.mkdir()
    write_jsonl(
        project_dir / "session-report.jsonl",
        [
            turn_line(
                timestamp="2026-09-18T12:00:00.000Z",
                input_tokens=100,
                output_tokens=20,
                ephemeral_5m_input_tokens=1000,
                cache_read_input_tokens=100,
            )
        ],
    )
    agent_dir = project_dir / "session-report" / "subagents"
    agent_dir.mkdir(parents=True)
    write_jsonl(agent_dir / "agent-report.jsonl", [turn_line(input_tokens=50, output_tokens=10)])
    (agent_dir / "agent-report.meta.json").write_text(
        '{"agentType": "claude-implementer", "model": "claude-sonnet-5"}', encoding="utf-8"
    )

    corpus = load_corpus([project_dir])
    model = report.build_report(
        corpus, PRICING, Config(), projects=("proj-report",), window="contract test", phases=True
    )
    for section in model.sections:
        _assert_row_keys_are_valid(section)
