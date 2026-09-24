"""Tests for WP6's usage-window logger (src/claude_token_lens/tools/log_usage.py)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_token_lens.tools import log_usage

# `tests/helpers.assert_privacy` walks a dataclass's fields (TranscriptResult
# shape); log_usage's rows are plain dicts, so it would silently pass
# without checking anything useful. This small local check does the same
# path/email-leak scan directly over each row's string values instead.
_FORBIDDEN_SUBSTRINGS = ("C:\\", "/home/", "\\Users\\")


def _assert_row_privacy(rows: list[dict]) -> None:
    for row in rows:
        for key, value in row.items():
            if not isinstance(value, str):
                continue
            for bad in _FORBIDDEN_SUBSTRINGS:
                assert bad not in value, f"row[{key!r}]={value!r} leaks a path"
            if key not in ("resets_at", "logged_at") and "@" in value:
                pytest.fail(f"row[{key!r}]={value!r} contains '@'")


# -- parse_usage_json: desktop get_usage shape -------------------------------


def test_parse_usage_json_desktop_shape_top_level_windows():
    payload = {
        "session_id": "sess_1",
        "model": "claude-sonnet-5",
        "five_hour": {"used_percentage": 37.5, "resets_at": "2026-09-18T17:00:00Z"},
        "seven_day": {"utilization": 12.0, "resets_at": "2026-09-25T00:00:00Z"},
    }
    rows = log_usage.parse_usage_json(json.dumps(payload))
    assert len(rows) == 2
    by_window = {r["window"]: r for r in rows}
    assert by_window["five_hour"]["used_percentage"] == pytest.approx(37.5)
    assert by_window["five_hour"]["resets_at"] == "2026-09-18T17:00:00Z"
    assert by_window["five_hour"]["source"] == "get_usage"
    assert by_window["five_hour"]["session_id"] == "sess_1"
    assert by_window["five_hour"]["model"] == "claude-sonnet-5"
    # "utilization" is one of the accepted used_percentage variants.
    assert by_window["seven_day"]["used_percentage"] == pytest.approx(12.0)
    assert "spend_limit" not in by_window
    _assert_row_privacy(rows)


def test_parse_usage_json_desktop_shape_percent_used_variant():
    payload = {"spend_limit": {"percent_used": 5.0, "reset_at": "2026-10-01T00:00:00Z"}}
    rows = log_usage.parse_usage_json(json.dumps(payload))
    assert len(rows) == 1
    assert rows[0]["window"] == "spend_limit"
    assert rows[0]["used_percentage"] == pytest.approx(5.0)
    assert rows[0]["resets_at"] == "2026-10-01T00:00:00Z"


# -- parse_usage_json: statusline rate_limits shape --------------------------


def test_parse_usage_json_statusline_shape_rate_limits_wrapper():
    payload = {
        "session_id": "sess_2",
        "rate_limits": {
            "five_hour": {"used_percentage": 55.0, "resets_at": "2026-09-18T18:00:00Z"},
            "seven_day": {"used_percentage": 20.0, "resets_at": "2026-09-25T00:00:00Z"},
            "spend_limit": {"used_percentage": 5.0, "resets_at": "2026-10-01T00:00:00Z"},
        },
    }
    rows = log_usage.parse_usage_json(json.dumps(payload))
    assert len(rows) == 3
    assert all(r["source"] == "statusline" for r in rows)
    assert all(r["session_id"] == "sess_2" for r in rows)
    windows = {r["window"] for r in rows}
    assert windows == {"five_hour", "seven_day", "spend_limit"}
    _assert_row_privacy(rows)


def test_parse_usage_json_statusline_epoch_resets_at():
    epoch = 1_790_000_000  # arbitrary fixed epoch seconds
    payload = {"rate_limits": {"five_hour": {"used_percentage": 10.0, "resets_at": epoch}}}
    rows = log_usage.parse_usage_json(json.dumps(payload))
    assert len(rows) == 1
    resets_at = rows[0]["resets_at"]
    assert resets_at is not None
    assert resets_at.endswith("Z")
    # Round-trips back to the same epoch second.
    parsed = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    assert int(parsed.timestamp()) == epoch


# -- parse_usage_json: robustness ---------------------------------------


def test_parse_usage_json_array_of_objects():
    payload = [
        {"rate_limits": {"five_hour": {"used_percentage": 1.0}}},
        {"rate_limits": {"five_hour": {"used_percentage": 2.0}}},
    ]
    rows = log_usage.parse_usage_json(json.dumps(payload))
    assert len(rows) == 2


def test_parse_usage_json_malformed_returns_empty():
    assert log_usage.parse_usage_json("{not valid json") == []
    assert log_usage.parse_usage_json("") == []
    assert log_usage.parse_usage_json("42") == []
    assert log_usage.parse_usage_json('"just a string"') == []


def test_parse_usage_json_window_missing_used_percentage_skipped():
    payload = {"rate_limits": {"five_hour": {"resets_at": "2026-09-18T18:00:00Z"}}}
    assert log_usage.parse_usage_json(json.dumps(payload)) == []


# -- append_rows / load_usage_log dedupe -------------------------------------


def test_append_rows_dedupes_on_second_append(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    rows = [
        {"session_id": "sess_1", "window": "five_hour", "used_percentage": 37.5,
         "resets_at": "2026-09-18T17:00:00Z", "source": "get_usage"},
        {"session_id": "sess_1", "window": "seven_day", "used_percentage": 12.0,
         "resets_at": "2026-09-25T00:00:00Z", "source": "get_usage"},
    ]
    added_first = log_usage.append_rows(csv_path, rows)
    assert added_first == 2

    added_second = log_usage.append_rows(csv_path, rows)
    assert added_second == 0

    loaded = log_usage.load_usage_log(csv_path)
    assert len(loaded) == 2
    _assert_row_privacy(loaded)


def test_append_rows_distinguishes_by_used_percentage(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    row_a = {"session_id": "sess_1", "window": "five_hour", "used_percentage": 10.0,
              "resets_at": "2026-09-18T17:00:00Z"}
    row_b = dict(row_a, used_percentage=20.0)
    assert log_usage.append_rows(csv_path, [row_a]) == 1
    # A later sample for the same window/reset period, but a different
    # used_percentage, is a genuinely new observation, not a duplicate.
    assert log_usage.append_rows(csv_path, [row_b]) == 1
    assert len(log_usage.load_usage_log(csv_path)) == 2


def test_load_usage_log_missing_file_returns_empty(tmp_path):
    assert log_usage.load_usage_log(tmp_path / "does-not-exist.csv") == []


def test_load_usage_log_parses_used_percentage_as_float(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    log_usage.append_rows(
        csv_path,
        [{"session_id": "s", "window": "five_hour", "used_percentage": 42.5, "resets_at": "r"}],
    )
    rows = log_usage.load_usage_log(csv_path)
    assert rows[0]["used_percentage"] == pytest.approx(42.5)
    assert isinstance(rows[0]["used_percentage"], float)


def test_append_rows_csv_columns_match_spec(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    log_usage.append_rows(
        csv_path,
        [{"session_id": "s", "window": "five_hour", "used_percentage": 1.0, "resets_at": "r"}],
    )
    text = csv_path.read_text(encoding="utf-8")
    header = text.splitlines()[0]
    assert header == "logged_at,session_id,window,used_percentage,resets_at,source"


# -- build_section: latest-per-window ----------------------------------------


def test_build_section_empty_rows_has_note_and_empty_tables():
    section = log_usage.build_section([])
    assert section.key == "usage_windows"
    assert section.tables[0].rows == []
    assert section.tables[1].rows == []
    assert any("No usage-log rows found" in n for n in section.notes)


def test_build_section_latest_per_window():
    rows = [
        {"logged_at": "2026-09-18T10:00:00Z", "window": "five_hour", "used_percentage": "10.0",
         "resets_at": "2026-09-18T17:00:00Z"},
        {"logged_at": "2026-09-18T11:00:00Z", "window": "five_hour", "used_percentage": "20.0",
         "resets_at": "2026-09-18T17:00:00Z"},
        {"logged_at": "2026-09-18T11:00:00Z", "window": "seven_day", "used_percentage": "5.0",
         "resets_at": "2026-09-25T00:00:00Z"},
    ]
    section = log_usage.build_section(rows)
    latest_table = section.tables[0]
    by_window = {row[0]: row for row in latest_table.rows}
    # Latest five_hour sample (by logged_at) is the 20.0 one, with 2 samples.
    assert by_window["five_hour"][1] == pytest.approx(20.0)
    assert by_window["five_hour"][3] == 2
    assert by_window["seven_day"][1] == pytest.approx(5.0)
    assert by_window["seven_day"][3] == 1
    assert any("No token totals supplied" in n for n in section.notes)


def test_build_section_no_token_totals_skips_regression():
    rows = [
        {"logged_at": "t1", "window": "five_hour", "used_percentage": "10.0", "resets_at": "r"},
    ]
    section = log_usage.build_section(rows, token_totals_by_window=None)
    assert section.tables[1].rows == []


def test_build_section_regression_skipped_below_min_samples():
    rows = [
        {"logged_at": "t1", "window": "five_hour", "used_percentage": "10.0", "resets_at": "r"},
        {"logged_at": "t2", "window": "five_hour", "used_percentage": "20.0", "resets_at": "r"},
    ]
    totals = {"five_hour": {"t1": 1_000_000, "t2": 2_000_000}}
    section = log_usage.build_section(rows, token_totals_by_window=totals)
    assert section.tables[1].rows == []
    assert any("Fewer than 3 usage samples" in n for n in section.notes)


# -- build_section: regression on 4 synthetic samples ------------------------


def _append_one(csv_path: Path, *, used_percentage: float, minute: int) -> None:
    now = datetime(2026, 9, 18, 12, minute, 0, tzinfo=timezone.utc)
    log_usage.append_rows(
        csv_path,
        [{
            "session_id": "sess_regress",
            "window": "five_hour",
            "used_percentage": used_percentage,
            "resets_at": "2026-09-18T20:00:00Z",
        }],
        now=now,
    )


def test_build_section_regression_expected_slope(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    # 4 synthetic samples: used% grows exactly 10.0 per extra million tokens
    # (a clean, hand-checkable slope), all within the same reset period.
    samples = [(1_000_000, 10.0), (2_000_000, 20.0), (3_000_000, 30.0), (4_000_000, 40.0)]
    for minute, (_tokens, used) in enumerate(samples):
        _append_one(csv_path, used_percentage=used, minute=minute)

    rows = log_usage.load_usage_log(csv_path)
    assert len(rows) == 4
    token_totals = {"five_hour": {row["logged_at"]: tokens for row, (tokens, _used) in zip(rows, samples)}}

    section = log_usage.build_section(rows, token_totals_by_window=token_totals)
    regression_table = section.tables[1]
    assert len(regression_table.rows) == 1
    window, slope, intercept, samples_used, period = regression_table.rows[0]
    assert window == "five_hour"
    assert slope == pytest.approx(10.0)
    assert intercept == pytest.approx(0.0, abs=1e-9)
    assert samples_used == 4
    assert period == "2026-09-18T20:00:00Z"


# -- main() / CLI entry point -------------------------------------------------


def test_main_reads_stdin_and_appends(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "token-lens-config"
    payload = {"rate_limits": {"five_hour": {"used_percentage": 42.0, "resets_at": "r"}}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    rc = log_usage.main(["--config-dir", str(config_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 row(s) added" in out

    csv_path = config_dir / "usage-log.csv"
    assert csv_path.exists()
    loaded = log_usage.load_usage_log(csv_path)
    assert len(loaded) == 1
    assert loaded[0]["window"] == "five_hour"


def test_main_malformed_stdin_exits_0_and_adds_nothing(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "token-lens-config"
    monkeypatch.setattr("sys.stdin", io.StringIO("{not valid json"))

    rc = log_usage.main(["--config-dir", str(config_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "0 row(s) added" in out


def test_main_empty_stdin_exits_0(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "token-lens-config"
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = log_usage.main(["--config-dir", str(config_dir)])
    assert rc == 0


# -- config dir resolution ----------------------------------------------


def test_resolve_config_dir_cli_arg_wins(tmp_path):
    explicit = tmp_path / "explicit"
    assert log_usage.resolve_config_dir(str(explicit)) == explicit


def test_resolve_config_dir_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-cfg"))
    resolved = log_usage.resolve_config_dir(None)
    assert resolved == tmp_path / "claude-cfg" / "token-lens"


def test_default_usage_log_path(tmp_path):
    path = log_usage.default_usage_log_path(tmp_path)
    assert path == tmp_path / "usage-log.csv"


# -- SIG-5: tail reads and retention pruning ---------------------------------


def _pad_past_tail(csv_path: Path, row: dict) -> None:
    """Append enough filler rows after ``row`` for the file to exceed
    :data:`log_usage._TAIL_BYTES`, pushing ``row`` itself out of
    :func:`log_usage._read_existing_keys`'s bounded tail scan."""
    log_usage.append_rows(csv_path, [row], source="statusline", now=datetime(2026, 9, 1, tzinfo=timezone.utc))
    filler = {"session_id": "filler", "window": "seven_day", "resets_at": "2026-10-01T00:00:00Z"}
    with open(csv_path, "a", encoding="utf-8", newline="") as fh:
        for i in range(4000):
            fh.write(f"2026-09-01T00:00:{i % 60:02d}Z,filler,seven_day,{i % 100}.0,2026-10-01T00:00:00Z,statusline\n")
    assert csv_path.stat().st_size > log_usage._TAIL_BYTES


def test_read_existing_keys_finds_a_key_within_the_tail_window(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    row = {"session_id": "s1", "window": "five_hour", "used_percentage": 37.5, "resets_at": "2026-09-18T17:00:00Z"}
    log_usage.append_rows(csv_path, [row], now=datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert log_usage._dedupe_key(row) in log_usage._read_existing_keys(csv_path)


def test_read_existing_keys_is_bounded_to_the_tail_window(tmp_path):
    """SIG-5: this now scans only the final _TAIL_BYTES rather than the
    whole (unboundedly growing) log on every append_rows call. A key that
    sits further back than that is invisible to a fresh scan -- the
    documented, bounded trade-off (see the function's own docstring): the
    same row gets appended again instead of deduped, rather than every
    single statusline refresh reading an ever-growing file in full."""
    csv_path = tmp_path / "usage-log.csv"
    old_row = {"session_id": "s1", "window": "five_hour", "used_percentage": 37.5, "resets_at": "2026-09-18T17:00:00Z"}
    _pad_past_tail(csv_path, old_row)

    assert log_usage._dedupe_key(old_row) not in log_usage._read_existing_keys(csv_path)
    # Concretely: appending the same row again is *not* deduped, unlike
    # the small-file case in the test above.
    assert log_usage.append_rows(csv_path, [old_row]) == 1


def test_prune_usage_log_missing_file_is_a_noop(tmp_path):
    assert log_usage.prune_usage_log(tmp_path / "does-not-exist.csv", 30) == 0


def test_prune_usage_log_header_only_is_a_noop(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    csv_path.write_text("logged_at,session_id,window,used_percentage,resets_at,source\n", encoding="utf-8")
    assert log_usage.prune_usage_log(csv_path, 30) == 0


def test_prune_usage_log_drops_rows_older_than_the_window_keeps_the_rest(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    old = {"session_id": "old", "window": "five_hour", "used_percentage": 1.0, "resets_at": "r"}
    recent = {"session_id": "recent", "window": "five_hour", "used_percentage": 2.0, "resets_at": "r"}
    log_usage.append_rows(csv_path, [old], now=now - timedelta(days=40))
    log_usage.append_rows(csv_path, [recent], now=now - timedelta(days=1))

    removed = log_usage.prune_usage_log(csv_path, 30, now=now)

    assert removed == 1
    rows = log_usage.load_usage_log(csv_path)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "recent"


def test_prune_usage_log_nothing_to_remove_returns_zero_and_leaves_file_untouched(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    log_usage.append_rows(csv_path, [{"session_id": "s1", "window": "five_hour", "resets_at": "r"}], now=now)
    text_before = csv_path.read_text(encoding="utf-8")

    assert log_usage.prune_usage_log(csv_path, 30, now=now) == 0
    assert csv_path.read_text(encoding="utf-8") == text_before


def test_prune_usage_log_keeps_a_row_with_more_columns_than_the_header_verbatim(tmp_path):
    """A statusline ground-truth row (16 trailing columns) must survive a
    prune pass unchanged, not get truncated to the base 6-column shape --
    this function only looks at column 0 to decide keep/drop and writes
    surviving lines back byte-for-byte."""
    csv_path = tmp_path / "usage-log.csv"
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    ground_truth_line = "2026-09-23T00:00:00Z,s1,context_window,50.0,,statusline,1000,2000,,1,300,250,0,,,\n"
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        fh.write("logged_at,session_id,window,used_percentage,resets_at,source\n")
        fh.write(ground_truth_line)

    removed = log_usage.prune_usage_log(csv_path, 30, now=now)

    assert removed == 0
    assert csv_path.read_text(encoding="utf-8").splitlines()[1] + "\n" == ground_truth_line


def test_prune_usage_log_drops_a_row_with_an_unparseable_logged_at(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        fh.write("logged_at,session_id,window,used_percentage,resets_at,source\n")
        fh.write("not-a-timestamp,s1,five_hour,1.0,r,statusline\n")

    removed = log_usage.prune_usage_log(csv_path, 30, now=datetime(2026, 9, 24, tzinfo=timezone.utc))

    assert removed == 1
    assert log_usage.load_usage_log(csv_path) == []
