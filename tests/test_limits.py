"""Tests for ``limits.py``: usage-cap pause intervals/markers feeding
classify.py and the session-timeline API, and the ``limits`` report
section's accumulation/rendering.

Fixtures are built with ``turn_line``/``user_str_line`` (never real
transcript text — see tests/helpers.py and SECURITY.md).
"""

from __future__ import annotations

from pathlib import Path

from claudeglass import limits
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript

from helpers import assert_privacy, turn_line, user_str_line, write_jsonl


def _session_limit_fixture(tmp_path: Path, session_id: str = "sess1") -> Path:
    lines = [
        turn_line(message_id="msg_1", input_tokens=30_000, timestamp="2026-09-18T12:00:00.000Z"),
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
        turn_line(
            message_id="msg_2",
            input_tokens=30_000,
            cache_creation_input_tokens=25_000,
            cache_read_input_tokens=0,
            timestamp="2026-09-18T15:00:10.000Z",
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    return path


def test_limit_pause_intervals_reads_gap_backward_from_the_post_pause_turn(tmp_path: Path):
    path = _session_limit_fixture(tmp_path)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))

    intervals = limits.limit_pause_intervals(result)
    assert len(intervals) == 1
    start, end = intervals[0]
    assert end.isoformat().startswith("2026-09-18T15:00:10")
    # From the last real turn (12:00:00), not the synthetic limit notice.
    assert start.isoformat().startswith("2026-09-18T12:00:00")
    assert (end - start).total_seconds() == 3 * 3600 + 10


def test_limit_markers_carries_kind_and_detail(tmp_path: Path):
    path = _session_limit_fixture(tmp_path)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))

    markers = limits.limit_markers(result)
    kinds = [m[1] for m in markers]
    assert "limit_hit" in kinds
    assert "limit_resume" in kinds
    hit_marker = next(m for m in markers if m[1] == "limit_hit")
    assert hit_marker[2]["subkind"] == "session_limit"
    assert hit_marker[2]["reset_minutes_of_day"] == 15 * 60


def test_limit_stats_accumulates_hits_resumes_and_pause(tmp_path: Path):
    path = _session_limit_fixture(tmp_path)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))

    stats = limits.LimitStats()
    stats.add(result)

    rows = stats.by_key()
    assert len(rows) == 1
    row = rows[0]
    assert row.key == "top-level"
    assert row.session_limit_hits == 1
    assert row.weekly_limit_hits == 0
    assert row.resumes == 1
    assert row.pause_count == 1
    assert row.pause_total_s > 0
    assert row.limit_turn_cc_tokens == 25_000
    assert stats.sessions_affected == {"sess1"}


def test_reset_hour_counts_from_reset_minutes_of_day(tmp_path: Path):
    path = _session_limit_fixture(tmp_path)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))

    stats = limits.LimitStats()
    stats.add(result)
    counts = stats.reset_hour_counts()
    assert counts[15] == 1
    assert sum(counts.values()) == 1


def test_agent_terminated_split_by_subkind(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", timestamp="2026-09-18T12:00:00.000Z"),
        user_str_line(
            "Agent terminated early due to an API error: rate limit hit (error type rate_limit, HTTP 429).",
            origin={"kind": "task-notification"},
            timestamp="2026-09-18T12:00:05.000Z",
        ),
        user_str_line(
            "Agent terminated early due to a network error.",
            origin={"kind": "task-notification"},
            timestamp="2026-09-18T12:00:06.000Z",
        ),
        turn_line(message_id="msg_2", timestamp="2026-09-18T12:05:00.000Z"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess2"))

    stats = limits.LimitStats()
    stats.add(result)
    rows = stats.by_key()
    row = rows[0]
    assert row.terminated_rate_limit == 1
    assert row.terminated_other == 1
    assert stats.sessions_affected == {"sess2"}


def test_build_section_shape_and_privacy(tmp_path: Path):
    path = _session_limit_fixture(tmp_path)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))

    stats = limits.LimitStats()
    stats.add(result, rates_lookup=lambda _model: None)
    section = limits.build_section(stats)

    assert section.key == "limits"
    table_names = {t.name for t in section.tables}
    assert table_names == {
        "limits_summary",
        "limits_hits_by_kind",
        "limits_agent_terminated",
        "limits_pauses",
        "limits_reset_hour_histogram",
        "limits_by_agent_type",
    }
    summary = next(t for t in section.tables if t.name == "limits_summary")
    assert summary.rows[0][0] == "all"
    assert_privacy(section)


def test_csv_cross_check_counts_exhaustion_rows_against_transcript_hits(tmp_path: Path):
    path = _session_limit_fixture(tmp_path)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))
    stats = limits.LimitStats()
    stats.add(result)

    rows = [
        {"window": "five_hour", "used_percentage": 100.0},
        {"window": "five_hour", "used_percentage": 42.0},
        {"window": "seven_day", "used_percentage": 12.0},
    ]
    table = limits.csv_cross_check(rows, stats)
    by_window = {row[0]: row for row in table.rows}
    assert by_window["five_hour"][1] == 1  # one row >= 100%
    assert by_window["five_hour"][2] == 1  # one transcript-derived session_limit hit
    assert by_window["seven_day"][1] == 0
    assert by_window["seven_day"][2] == 0


def test_signals_cross_check_counts_quota_waits_and_turn_failures(tmp_path: Path):
    from claudeglass import signals

    path = _session_limit_fixture(tmp_path)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))
    stats = limits.LimitStats()
    stats.add(result)  # one session_limit hit

    session_signals = {
        "sess1": signals.SessionSignals(waits={"quota": 2, "idle": 1}, failures={"rate_limit": 1, "invalid_request": 3}),
        "sess2": signals.SessionSignals(failures={"overloaded": 1}),
    }
    table = limits.signals_cross_check(session_signals, stats)
    by_signal = {row[0]: row for row in table.rows}
    assert by_signal["quota wait signals (Notification)"][1:] == [2, 1, -1]
    assert by_signal["rate_limit/overloaded turn failures (StopFailure)"][1:] == [2, 1, -1]
    assert limits.signals_cross_check({}, stats).rows[0][1] == 0


def test_read_usage_log_rows_missing_file_returns_empty(tmp_path: Path):
    assert limits.read_usage_log_rows(tmp_path / "nope.csv") == []
