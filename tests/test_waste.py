"""Tests for ``waste.py``: wasted-turn cause detection (tool-error,
interrupt, tool-denial, max-turns, api-error-retry count-only,
limit-pause exclusion), the ``waste`` report section's accumulation/
rendering, and the ``wasted-turns`` recommendation rule.

Fixtures are built with ``turn_line``/``user_str_line``/
``user_block_line``/``tool_use_block``/``tool_result_block``/
``system_line`` (never real transcript text — see tests/helpers.py and
SECURITY.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_token_lens import waste
from claude_token_lens.model import Column, Recommendation, ReportModel, Section, Table, TranscriptMeta
from claude_token_lens.parse import parse_transcript
from claude_token_lens.pricing import ModelRates, Pricing

from helpers import (
    assert_privacy,
    system_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)


# -- fixtures --------------------------------------------------------------


def _pricing() -> Pricing:
    """A minimal rate card with every component priced at $1 per million
    tokens, so a turn's cost is simply its own token total / 1e6 -- easy
    to hand-compute."""
    rates = ModelRates(
        canonical_id="claude-widget-9",
        input=1.0,
        output=1.0,
        cache_write_5m=1.0,
        cache_write_1h=1.0,
        cache_read=1.0,
    )
    return Pricing(
        path="<test>",
        version="test-2026",
        currency="USD",
        source_url=None,
        retrieved=None,
        notes=None,
        sha256="0" * 64,
        models={"claude-widget-9": rates},
    )


_MODEL = "claude-widget-9"


def _parse(tmp_path: Path, lines: list[dict], name: str = "session.jsonl", **meta_kwargs) -> object:
    path = tmp_path / name
    write_jsonl(path, lines)
    meta_kwargs.setdefault("session_id", "sess1")
    return parse_transcript(path, TranscriptMeta(path=str(path), **meta_kwargs))


# -- tool-error --------------------------------------------------------------


def test_tool_error_cause_detected_and_priced(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
    ]
    result = _parse(tmp_path, lines)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats._by_cause["tool-error"].turns == 1
    assert stats._by_cause["tool-error"].cost_usd == pytest.approx(1.0)  # 1M input tokens @ $1/M
    assert stats._by_cause["tool-error"].tokens == 1_000_000
    assert stats.wasted_turns == 1
    assert stats.wasted_cost_usd == pytest.approx(1.0)


def test_tool_error_absent_when_no_is_error_result(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls"})],
        ),
        user_block_line([tool_result_block("tu_a", "file1\nfile2")]),
    ]
    result = _parse(tmp_path, lines)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats.wasted_turns == 0
    assert stats.wasted_cost_usd == 0.0


# -- interrupt ---------------------------------------------------------------


def test_interrupt_cause_detected_on_the_turn_before_the_interrupt(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=2_000_000, output_tokens=0),
        user_str_line("[Request interrupted by user]"),
        turn_line(message_id="msg_2", model=_MODEL, input_tokens=500_000, output_tokens=0),
    ]
    result = _parse(tmp_path, lines)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    # msg_1 (the turn the user cut off) is wasted, not msg_2 (the turn
    # that merely reports the interruption).
    assert stats._by_cause["interrupt"].turns == 1
    assert stats._by_cause["interrupt"].cost_usd == pytest.approx(2.0)
    assert stats._by_cause["tool-denial"].turns == 0


def test_interrupt_not_flagged_when_turn_has_no_next_turn(tmp_path: Path):
    lines = [turn_line(message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0)]
    result = _parse(tmp_path, lines)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats.wasted_turns == 0


# -- tool-denial ---------------------------------------------------------------


def test_tool_denial_cause_detected_on_the_turn_before_the_denial(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=3_000_000, output_tokens=0),
        user_str_line("(denied)", toolDenialKind="user-rejected"),
        turn_line(message_id="msg_2", model=_MODEL, input_tokens=100_000, output_tokens=0),
    ]
    result = _parse(tmp_path, lines)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats._by_cause["tool-denial"].turns == 1
    assert stats._by_cause["tool-denial"].cost_usd == pytest.approx(3.0)
    assert stats._by_cause["interrupt"].turns == 0


# -- max-turns / stopped_by_user ----------------------------------------------


def test_max_turns_wastes_every_priced_turn_in_a_stopped_by_user_subagent(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0),
        turn_line(message_id="msg_2", model=_MODEL, input_tokens=500_000, output_tokens=0),
    ]
    result = _parse(tmp_path, lines, kind="subagent", agent_type="claude-implementer", stopped_by_user=True)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats._by_cause["max-turns"].turns == 2
    assert stats._by_cause["max-turns"].cost_usd == pytest.approx(1.5)
    assert stats._by_agent_type["claude-implementer"].turns == 2


def test_max_turns_not_applied_to_a_stopped_by_user_top_level_transcript(tmp_path: Path):
    """topology.py's own convention only ever reads stopped_by_user off
    subagent transcripts -- a top-level transcript's own stopped_by_user
    is not a meaningful signal (see the module docstring's ASSUMPTIONS
    entry)."""
    lines = [turn_line(message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0)]
    result = _parse(tmp_path, lines, kind="top-level", stopped_by_user=True)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats.wasted_turns == 0


def test_max_turns_overrides_tool_error_on_the_same_turn(tmp_path: Path):
    """A transcript-level override: even a turn that also has its own
    tool error is attributed to max-turns, not tool-error, once the
    whole transcript was killed."""
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
    ]
    result = _parse(tmp_path, lines, kind="subagent", stopped_by_user=True)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats._by_cause["max-turns"].turns == 1
    assert stats._by_cause["tool-error"].turns == 0


# -- limit-pause exclusion -----------------------------------------------------


def _limit_pause_fixture(tmp_path: Path) -> object:
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=30_000, timestamp="2026-09-18T12:00:00.000Z"),
        turn_line(
            message_id="msg_synth",
            model="<synthetic>",
            isApiErrorMessage=True,
            input_tokens=0,
            output_tokens=0,
            content=[{"type": "text", "text": "You've hit your session limit · resets 3pm (Europe/London)"}],
            timestamp="2026-09-18T12:00:05.000Z",
        ),
        user_str_line(
            "I hit my usage limit while you were working, but it has reset now.",
            promptSource="sdk",
            origin={"kind": "human"},
            timestamp="2026-09-18T15:00:00.000Z",
        ),
        # The post-pause turn also has its own tool error -- proving the
        # limit-pause exclusion outranks tool-error detection too.
        turn_line(
            message_id="msg_2",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            timestamp="2026-09-18T15:00:10.000Z",
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line(
            [tool_result_block("tu_a", "no such directory", is_error=True)],
            timestamp="2026-09-18T15:00:11.000Z",
        ),
    ]
    return _parse(tmp_path, lines)


def test_limit_pause_turn_excluded_from_every_cause(tmp_path: Path):
    result = _limit_pause_fixture(tmp_path)
    assert any(t.gap_cause == "limit" for t in result.turns)  # sanity: fixture actually produces one

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats.wasted_turns == 0
    assert stats.limit_pause_excluded_turns == 1


# -- api-error-retry (count only) ---------------------------------------------


def test_api_error_retry_counted_but_not_costed(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0),
        system_line("api_error", error={"status": 529}),
        turn_line(message_id="msg_2", model=_MODEL, input_tokens=1_000_000, output_tokens=0),
    ]
    result = _parse(tmp_path, lines)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    # msg_2 is "preceded by" the API_ERROR -- it is the one counted.
    assert stats.api_error_retry_turns == 1
    assert stats.wasted_turns == 0
    assert stats.wasted_cost_usd == 0.0


# -- corpus totals / share_pct -------------------------------------------------


def test_total_priced_turns_and_cost_include_non_wasted_turns(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0),  # ordinary turn
        turn_line(
            message_id="msg_2",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
    ]
    result = _parse(tmp_path, lines)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats.total_priced_turns == 2
    assert stats.total_priced_cost_usd == pytest.approx(2.0)
    assert stats.wasted_turns == 1
    assert stats.wasted_cost_usd == pytest.approx(1.0)


def test_by_cause_share_pct_sums_to_summary_share_pct(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0),
        turn_line(
            message_id="msg_2",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
        turn_line(message_id="msg_3", model=_MODEL, input_tokens=1_000_000, output_tokens=0),
        user_str_line("[Request interrupted by user]"),
        turn_line(message_id="msg_4", model=_MODEL, input_tokens=1_000_000, output_tokens=0),
    ]
    result = _parse(tmp_path, lines)

    stats = waste.compute_waste([result], _pricing(), config_dir=tmp_path / "cfg")
    section = waste.build_section(stats)

    summary = _table(section, "waste_summary")
    by_cause = _table(section, "waste_by_cause")

    summary_turns_share = summary.rows[0][_col(summary, "wasted_turns_share_pct")]
    summary_cost_share = summary.rows[0][_col(summary, "wasted_cost_share_pct")]

    turns_idx = _col(by_cause, "share_of_turns_pct")
    cost_idx = _col(by_cause, "share_of_cost_pct")
    costed_causes = set(waste.CAUSES)
    turns_sum = sum(row[turns_idx] for row in by_cause.rows if row[0] in costed_causes)
    cost_sum = sum(row[cost_idx] for row in by_cause.rows if row[0] in costed_causes)

    assert turns_sum == pytest.approx(summary_turns_share)
    assert cost_sum == pytest.approx(summary_cost_share)


# -- waste_top_sessions / session hashing --------------------------------------


def test_top_sessions_hashes_session_id_never_raw(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
    ]
    result = _parse(tmp_path, lines, session_id="a-very-identifiable-session-id")

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())
    section = waste.build_section(stats)

    top_sessions = _table(section, "waste_top_sessions")
    assert len(top_sessions.rows) == 1
    session_hash = top_sessions.rows[0][0]
    assert session_hash != "a-very-identifiable-session-id"
    assert "a-very-identifiable-session-id" not in session_hash
    assert len(session_hash) == 12
    int(session_hash, 16)  # must be valid hex


def test_top_sessions_hash_stable_for_same_session_and_config_dir(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
    ]
    cfg = tmp_path / "cfg"
    result_a = _parse(tmp_path, lines, name="a.jsonl", session_id="sess-x")
    stats_a = waste.WasteStats(config_dir=cfg)
    stats_a.add(result_a, _pricing())
    hash_a = _table(waste.build_section(stats_a), "waste_top_sessions").rows[0][0]

    result_b = _parse(tmp_path, lines, name="b.jsonl", session_id="sess-x")
    stats_b = waste.WasteStats(config_dir=cfg)
    stats_b.add(result_b, _pricing())
    hash_b = _table(waste.build_section(stats_b), "waste_top_sessions").rows[0][0]

    assert hash_a == hash_b


def test_top_sessions_cause_mix_and_sorted_by_cost_descending(tmp_path: Path):
    big = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=5_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
    ]
    small = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
    ]
    result_big = _parse(tmp_path, big, name="big.jsonl", session_id="sess-big")
    result_small = _parse(tmp_path, small, name="small.jsonl", session_id="sess-small")

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result_big, _pricing())
    stats.add(result_small, _pricing())
    top_sessions = _table(waste.build_section(stats), "waste_top_sessions")

    assert len(top_sessions.rows) == 2
    assert top_sessions.rows[0][2] > top_sessions.rows[1][2]  # cost_usd descending
    assert "tool-error:1" in top_sessions.rows[0][4]


# -- privacy -------------------------------------------------------------------


def test_waste_section_passes_privacy_scan(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "cat /c/Users/paulm/secret.txt"})],
        ),
        user_block_line(
            [tool_result_block("tu_a", "cat: /c/Users/paulm/secret.txt: No such file or directory", is_error=True)]
        ),
    ]
    result = _parse(tmp_path, lines, session_id="a-very-identifiable-session-id")

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())
    section = waste.build_section(stats)

    assert_privacy(section)


# -- compute_waste convenience entry point -------------------------------------


def test_compute_waste_equals_incremental_add(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
    ]
    result = _parse(tmp_path, lines)

    one_shot = waste.compute_waste([result], _pricing(), config_dir=tmp_path / "cfg1")

    incremental = waste.WasteStats(config_dir=tmp_path / "cfg2")
    incremental.add(result, _pricing())

    assert one_shot.wasted_turns == incremental.wasted_turns
    assert one_shot.wasted_cost_usd == pytest.approx(incremental.wasted_cost_usd)


# -- rule: wasted-turns --------------------------------------------------------


def _table(section: Section, name: str) -> Table:
    return next(t for t in section.tables if t.name == name)


def _col(table: Table, key: str) -> int:
    return next(i for i, c in enumerate(table.columns) if c.key == key)


def _overview_section(sessions: int, priced_turns: int) -> Section:
    return Section(
        key="overview",
        title="Overview",
        tables=[
            Table(
                name="totals",
                title="Overview totals",
                columns=[Column(key="metric", label="Metric", kind="str"), Column(key="value", label="Value", kind="str")],
                rows=[["sessions", sessions], ["priced_turns", priced_turns]],
            )
        ],
    )


def _report_with_waste_section(waste_section: Section, sessions: int = 10, priced_turns: int = 500) -> ReportModel:
    return ReportModel(sections=[_overview_section(sessions, priced_turns), waste_section])


def _built_section_for_high_waste_share(tmp_path: Path) -> Section:
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=9_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
        turn_line(message_id="msg_2", model=_MODEL, input_tokens=1_000_000, output_tokens=0),
    ]
    result = _parse(tmp_path, lines)
    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())
    return waste.build_section(stats)


def test_rule_fires_when_share_exceeds_threshold_and_min_sample_met(tmp_path: Path):
    section = _built_section_for_high_waste_share(tmp_path)  # 90% wasted cost share
    report = _report_with_waste_section(section, sessions=10, priced_turns=500)

    th = waste.WasteThresholds(share_pct=10.0, min_sessions=5, min_turns=200)
    recs = waste.RULES[0](report, th)

    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == "wasted-turns"
    assert isinstance(rec, Recommendation)
    assert "tool-error" in rec.action


def test_rule_does_not_fire_below_threshold(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=10_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
        turn_line(message_id="msg_2", model=_MODEL, input_tokens=9_990_000, output_tokens=0),
    ]
    result = _parse(tmp_path, lines)
    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())
    section = waste.build_section(stats)
    report = _report_with_waste_section(section, sessions=10, priced_turns=500)

    th = waste.WasteThresholds(share_pct=10.0, min_sessions=5, min_turns=200)
    recs = waste.RULES[0](report, th)

    assert recs == []


def test_rule_does_not_fire_below_minimum_sample(tmp_path: Path):
    section = _built_section_for_high_waste_share(tmp_path)
    report = _report_with_waste_section(section, sessions=1, priced_turns=2)

    th = waste.WasteThresholds(share_pct=10.0, min_sessions=5, min_turns=200)
    recs = waste.RULES[0](report, th)

    assert recs == []


def test_rule_returns_empty_when_waste_section_absent():
    report = ReportModel(sections=[_overview_section(10, 500)])
    th = waste.WasteThresholds()
    assert waste.RULES[0](report, th) == []


def test_rule_evidence_cites_real_table_cells(tmp_path: Path):
    section = _built_section_for_high_waste_share(tmp_path)
    report = _report_with_waste_section(section, sessions=10, priced_turns=500)

    th = waste.WasteThresholds(share_pct=10.0, min_sessions=5, min_turns=200)
    recs = waste.RULES[0](report, th)
    assert len(recs) == 1

    for label, value, source_table, row_key in recs[0].evidence:
        section_key, table_name = source_table.split(".", 1)
        cited = _table(next(s for s in report.sections if s.key == section_key), table_name)
        row = next(r for r in cited.rows if r[0] == row_key)
        assert value in row, f"{label}: {value!r} not found in row {row!r} for {source_table}/{row_key}"


def test_untyped_subagent_is_not_filed_under_top_level():
    from claude_token_lens.model import TranscriptMeta, TranscriptResult, agent_type_label

    assert agent_type_label(TranscriptResult(meta=TranscriptMeta(kind="subagent"))) == "unknown"
    assert agent_type_label(TranscriptResult(meta=TranscriptMeta(kind="top-level"))) == "top-level"
