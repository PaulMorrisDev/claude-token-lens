"""Tests for WP6's statusline renderer (src/claude_token_lens/statusline.py)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_token_lens import statusline


def _write_transcript(path: Path, assistant_ts_iso: str) -> None:
    lines = [
        {"type": "user", "timestamp": "2026-09-18T11:00:00.000Z", "message": {"content": "hi"}},
        {"type": "assistant", "timestamp": assistant_ts_iso, "message": {"content": []}},
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for d in lines:
            fh.write(json.dumps(d))
            fh.write("\n")


# -- render_status: full and minimal payloads --------------------------------


def test_render_status_full_payload_matches_plan_example(tmp_path):
    now = datetime(2026, 9, 18, 12, 5, 0, tzinfo=timezone.utc)
    # 4m12s = 252s remaining.
    expires_at = now.timestamp() + 252

    payload = {
        "context_window": {"used_tokens": 143000},
        "prompt_cache": {"warm": True, "ttl": "5m", "expires_at": expires_at},
        "rate_limits": {
            "five_hour": {"used_percentage": 37},
            "seven_day": {"used_percentage": 12},
        },
    }
    line = statusline.render_status(payload, now, 300)
    assert line == "ctx 143k | cache warm 5m 04:12 | 5h 37% | 7d 12%"


def test_render_status_minimal_payload_falls_back():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    assert statusline.render_status({}, now, 300) == "token-lens"
    assert statusline.render_status(None, now, 300) == "token-lens"


def test_render_status_partial_payload_only_renders_present_segments():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"context_window": {"used_tokens": 50000}}
    assert statusline.render_status(payload, now, 300) == "ctx 50k"


def test_render_status_ctx_rounds_to_nearest_k():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"context_window": {"used_tokens": 143499}}
    assert statusline.render_status(payload, now, 300) == "ctx 143k"


def test_render_status_ctx_tolerates_missing_used_tokens():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    assert statusline.render_status({"context_window": {}}, now, 300) == "token-lens"
    assert statusline.render_status({"context_window": "not a dict"}, now, 300) == "token-lens"


def test_render_status_cache_warm_without_expires_at_omits_countdown():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": True, "ttl": "5m"}}
    assert statusline.render_status(payload, now, 300) == "cache warm 5m"


def test_render_status_cache_warm_1h_countdown():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": True, "ttl": "1h", "expires_at": now.timestamp() + 3570}}
    assert statusline.render_status(payload, now, 300) == "cache warm 1h 59:30"


def test_render_status_cache_cold_with_recache_hint():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": False, "recache_tokens_if_cold": 12345}}
    assert statusline.render_status(payload, now, 300) == "cache cold recache ~12k tokens"


def test_render_status_cache_cold_without_recache_hint():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": False}}
    assert statusline.render_status(payload, now, 300) == "cache cold"


def test_render_status_rate_limits_tolerate_missing_window():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"rate_limits": {"five_hour": {"used_percentage": 10}}}
    assert statusline.render_status(payload, now, 300) == "5h 10%"


def test_render_status_effective_ttl_none_skips_ttl_segment(tmp_path):
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, now.isoformat().replace("+00:00", "Z"))
    payload = {"transcript_path": str(transcript)}
    assert statusline.render_status(payload, now, None) == "token-lens"


# -- TTL countdown from a constructed tmp transcript -------------------------


def test_render_status_ttl_expired_when_past_ttl(tmp_path):
    now = datetime(2026, 9, 18, 12, 10, 0, tzinfo=timezone.utc)
    last_ts = now - timedelta(seconds=600)  # 10 minutes ago, default estimate TTL is 5m
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, last_ts.isoformat().replace("+00:00", "Z"))
    payload = {"transcript_path": str(transcript)}
    line = statusline.render_status(payload, now, 300)
    assert line == "cache est 5m expired"


def test_render_status_ttl_1h_label_from_transcript_ephemeral_hint(tmp_path):
    """No ``prompt_cache`` on the payload falls back to the estimate,
    whose TTL is read from the transcript's own last assistant line
    (``message.usage.cache_creation.ephemeral_1h_input_tokens > 0``
    implies 1h) rather than from ``effective_ttl_s`` (kept only as an
    on/off gate -- see statusline.py's module docstring)."""
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    last_ts = now - timedelta(seconds=30)
    transcript = tmp_path / "session.jsonl"
    with open(transcript, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "timestamp": last_ts.isoformat().replace("+00:00", "Z"),
            "message": {"usage": {"cache_creation": {"ephemeral_1h_input_tokens": 500, "ephemeral_5m_input_tokens": 0}}},
        }))
        fh.write("\n")
    payload = {"transcript_path": str(transcript)}
    line = statusline.render_status(payload, now, 300)
    assert line == "cache est 1h 59:30"


def test_render_status_ttl_missing_transcript_path_skips_segment():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    assert statusline.render_status({}, now, 300) == "token-lens"


def test_render_status_ttl_nonexistent_transcript_file_skips_segment(tmp_path):
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"transcript_path": str(tmp_path / "does-not-exist.jsonl")}
    assert statusline.render_status(payload, now, 300) == "token-lens"


def test_render_status_ttl_reads_only_tail_of_large_transcript(tmp_path):
    """The countdown must come from the *last* assistant line even when
    the transcript is larger than the 64KB tail window (WP6 brief: "never
    load the whole file")."""
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    last_ts = now - timedelta(seconds=10)
    transcript = tmp_path / "session.jsonl"
    with open(transcript, "w", encoding="utf-8") as fh:
        # Pad with plenty of filler lines so the file exceeds 64KB well
        # before the final, real assistant line.
        filler_content = "x" * 500
        for i in range(300):
            fh.write(json.dumps({
                "type": "assistant",
                "timestamp": "2026-09-18T00:00:00.000Z",
                "message": {"content": filler_content, "seq": i},
            }))
            fh.write("\n")
        fh.write(json.dumps({
            "type": "assistant",
            "timestamp": last_ts.isoformat().replace("+00:00", "Z"),
            "message": {"content": []},
        }))
        fh.write("\n")
    assert transcript.stat().st_size > statusline._TAIL_BYTES

    payload = {"transcript_path": str(transcript)}
    line = statusline.render_status(payload, now, 300)
    assert line == "cache est 5m 04:50"


def test_render_status_ttl_survives_unicode_line_separator_inside_a_json_string(tmp_path):
    """fix(statusline): a U+2028/U+2029 (or bare \\r) embedded in a
    message string is legal JSON but is treated as a line break by
    ``str.splitlines()`` -- that would shear the final assistant line's
    JSON into two unparsable fragments and skip it, wrongly falling back
    to an older timestamp (or none at all). Scanning with
    ``str.split("\\n")`` instead must find the correct, newest timestamp.
    """
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    old_ts = now - timedelta(seconds=200)
    new_ts = now - timedelta(seconds=10)
    transcript = tmp_path / "session.jsonl"
    with open(transcript, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "timestamp": old_ts.isoformat().replace("+00:00", "Z"),
            "message": {"content": "no separators here"},
        }))
        fh.write("\n")
        fh.write(json.dumps({
            "type": "assistant",
            "timestamp": new_ts.isoformat().replace("+00:00", "Z"),
            "message": {"content": "before after"},
        }))
        fh.write("\n")

    payload = {"transcript_path": str(transcript)}
    line = statusline.render_status(payload, now, 300)
    assert line == "cache est 5m 04:50"  # from new_ts, not old_ts


# -- resolve_effective_ttl ----------------------------------------------


def test_resolve_effective_ttl_default_is_300s():
    assert statusline.resolve_effective_ttl({}, None) == 300


def test_resolve_effective_ttl_from_payload_prompt_cache():
    assert statusline.resolve_effective_ttl({"prompt_cache": {"cache_ttl": "1h"}}, None) == 3600


def test_resolve_effective_ttl_from_top_level_cache_ttl():
    assert statusline.resolve_effective_ttl({"cache_ttl": 900}, None) == 900


def test_resolve_effective_ttl_from_config_toml(tmp_path):
    config_dir = tmp_path / "token-lens"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('default_ttl = "1h"\n', encoding="utf-8")
    assert statusline.resolve_effective_ttl({}, config_dir) == 3600


def test_resolve_effective_ttl_payload_wins_over_config(tmp_path):
    config_dir = tmp_path / "token-lens"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('default_ttl = "1h"\n', encoding="utf-8")
    assert statusline.resolve_effective_ttl({"cache_ttl": "5m"}, config_dir) == 300


def test_resolve_effective_ttl_missing_config_file_falls_back(tmp_path):
    assert statusline.resolve_effective_ttl({}, tmp_path / "does-not-exist") == 300


# -- main(): never raises, stdin handling ------------------------------------


def test_main_malformed_stdin_exits_0_with_fallback_line(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("{not valid json"))
    rc = statusline.main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "token-lens"


def test_main_empty_stdin_exits_0(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = statusline.main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "token-lens"


def test_main_full_payload_prints_line_and_logs_usage_row(monkeypatch, capsys, tmp_path):
    config_dir = tmp_path / "token-lens"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    payload = {
        "context_window": {"used_tokens": 10000},
        "rate_limits": {"five_hour": {"used_percentage": 50, "resets_at": "2026-09-18T20:00:00Z"}},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = statusline.main([])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "ctx 10k | 5h 50%"

    from claude_token_lens.tools import log_usage
    csv_path = config_dir / "usage-log.csv"
    assert csv_path.exists()
    rows = log_usage.load_usage_log(csv_path)
    # S1-context-budget addition: a payload whose context_window also
    # carries used_tokens now logs a *second*, independent
    # "context_window" ground-truth row alongside the rate_limits row --
    # see statusline.py's module docstring.
    assert len(rows) == 2
    assert rows[0]["window"] == "five_hour"
    assert rows[1]["window"] == "context_window"


def test_main_no_rate_limits_still_logs_context_window_row(monkeypatch, capsys, tmp_path):
    """S1-context-budget: a payload with no ``rate_limits`` at all still
    gets its own ``context_window`` row logged, independently of the
    rate_limits-driven append -- see statusline.py's module docstring."""
    config_dir = tmp_path / "token-lens"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    payload = {"context_window": {"used_tokens": 1000}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = statusline.main([])
    assert rc == 0
    csv_path = config_dir / "usage-log.csv"
    assert csv_path.exists()

    from claude_token_lens import context_budget

    rows = context_budget.load_context_window_rows(csv_path)
    assert len(rows) == 1
    assert rows[0]["context_window_used_tokens"] == 1000


def test_main_no_context_window_and_no_rate_limits_does_not_create_usage_log(monkeypatch, capsys, tmp_path):
    config_dir = tmp_path / "token-lens"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    payload = {}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = statusline.main([])
    assert rc == 0
    assert not (config_dir / "usage-log.csv").exists()


def test_main_exception_during_stdin_read_falls_back(monkeypatch, capsys):
    class _RaisingStdin:
        def read(self):
            raise OSError("boom")

    monkeypatch.setattr("sys.stdin", _RaisingStdin())
    rc = statusline.main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "token-lens"


def test_main_stdout_reconfigure_failure_is_swallowed(monkeypatch, capsys):
    """A stdout that doesn't support ``reconfigure`` at all (e.g. a plain
    ``io.StringIO`` swapped in by a stricter test/embedding harness than
    capsys) must not blank the status line."""
    class _NoReconfigureStdout(io.StringIO):
        def reconfigure(self, *args, **kwargs):
            raise AttributeError("no reconfigure on this stream")

    fake_stdout = _NoReconfigureStdout()
    monkeypatch.setattr("sys.stdout", fake_stdout)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = statusline.main([])
    assert rc == 0
    assert fake_stdout.getvalue().strip() == "token-lens"


def test_main_never_raises_when_print_itself_fails(monkeypatch, capsys):
    """``_safe_print`` must swallow a broken-pipe-style failure from
    ``print()`` (e.g. the host already closed stdout) rather than let it
    escape main() -- the WP6 "never blank the status line" contract
    applies to the print call itself, not just upstream parsing."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    def _raising_print(*args, **kwargs):
        raise BrokenPipeError("downstream closed")

    monkeypatch.setattr("builtins.print", _raising_print)
    rc = statusline.main([])
    assert rc == 0


def test_safe_print_swallows_any_exception(capsys):
    class _Boom:
        def __str__(self):
            raise ValueError("boom")

    statusline._safe_print(_Boom())  # must not raise
    statusline._safe_print("fine")
    assert capsys.readouterr().out.strip() == "fine"


# -- print_install_fragment / --install flag ---------------------------------


def test_print_install_fragment_contains_both_platforms():
    text = statusline.print_install_fragment()
    assert "Windows:" in text
    assert "POSIX" in text
    assert "py -3 -m claude_token_lens.statusline" in text
    assert "python3 -m claude_token_lens.statusline" in text
    assert '"statusLine"' in text
    # Each platform's JSON fragment must itself be valid JSON.
    for block in text.split("Windows:\n", 1)[1].split("\n\nPOSIX"):
        candidate = block.strip()
        if candidate.startswith("{"):
            json.loads(candidate)


def test_main_print_install_fragment_flag(monkeypatch, capsys):
    rc = statusline.main(["--print-install-fragment"])
    assert rc == 0
    assert "statusLine" in capsys.readouterr().out


def test_main_install_flag_alias(monkeypatch, capsys):
    rc = statusline.main(["--install"])
    assert rc == 0
    assert "statusLine" in capsys.readouterr().out


# -- S1-exports: cache trailing CSV columns ----------------------------------


def test_append_context_window_row_writes_cache_columns(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    payload = {
        "session_id": "sess_cache",
        "prompt_cache": {
            "warm": True,
            "ttl": "5m",
            "expires_at": 1_800_000_300,
            "misses": 2,
            "last_miss_cause": {"causes": ["tools_changed"]},
            "recache_tokens_if_cold": 4000,
        },
    }
    now = datetime.fromtimestamp(1_800_000_000, tz=timezone.utc)
    statusline._append_context_window_row(csv_path, payload, now)

    with open(csv_path, encoding="utf-8", newline="") as fh:
        header = fh.readline().strip().split(",")
    assert header[:6] == list(statusline.log_usage.CSV_FIELDS)
    assert header[9:] == [
        "cache_warm",
        "cache_ttl_s",
        "cache_expires_in_s",
        "cache_misses",
        "cache_last_miss_cause",
        "cache_recache_tokens_if_cold",
    ]

    rows = statusline.load_usage_log_ground_truth(csv_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "sess_cache"
    assert row["cache_warm"] is True
    assert row["cache_ttl_s"] == 300
    assert row["cache_expires_in_s"] == 300  # expires_at - now, both fixed above
    assert row["cache_misses"] == 2
    assert row["cache_last_miss_cause"] == "tools"
    assert row["cache_recache_tokens_if_cold"] == 4000
    # A cache-only row carries no context-window data.
    assert row["context_window_used_tokens"] is None


def test_append_context_window_row_cache_only_payload_still_writes(tmp_path):
    """A payload with prompt_cache but no context_window at all must
    still get a row -- the "should I log?" gate is broadened to context
    OR cache data present, per the module docstring."""
    csv_path = tmp_path / "usage-log.csv"
    payload = {"session_id": "s1", "prompt_cache": {"warm": False}}
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    statusline._append_context_window_row(csv_path, payload, now)
    assert csv_path.exists()
    rows = statusline.load_usage_log_ground_truth(csv_path)
    assert len(rows) == 1
    assert rows[0]["cache_warm"] is False


def test_append_context_window_row_dedupes_on_cache_warm_change(tmp_path):
    """Per the task spec, a change in cache_warm alone counts as a new
    row even when the context-window columns are unchanged."""
    csv_path = tmp_path / "usage-log.csv"
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    base = {"session_id": "s1", "context_window": {"used_tokens": 100}}

    statusline._append_context_window_row(csv_path, {**base, "prompt_cache": {"warm": True}}, now)
    statusline._append_context_window_row(csv_path, {**base, "prompt_cache": {"warm": True}}, now)
    rows = statusline.load_usage_log_ground_truth(csv_path)
    assert len(rows) == 1  # identical repeat is deduped

    statusline._append_context_window_row(csv_path, {**base, "prompt_cache": {"warm": False}}, now)
    rows = statusline.load_usage_log_ground_truth(csv_path)
    assert len(rows) == 2  # cache_warm changed -> new row despite identical context columns


def test_append_context_window_row_dedupes_on_cache_misses_change(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    base = {"session_id": "s1", "context_window": {"used_tokens": 100}}

    statusline._append_context_window_row(csv_path, {**base, "prompt_cache": {"warm": True, "misses": 1}}, now)
    statusline._append_context_window_row(csv_path, {**base, "prompt_cache": {"warm": True, "misses": 2}}, now)
    rows = statusline.load_usage_log_ground_truth(csv_path)
    assert len(rows) == 2


def test_load_usage_log_ground_truth_tolerates_old_9_column_rows(tmp_path):
    """A file written by S1-context-budget alone (9 columns, no cache_*
    trailing columns yet) must be tolerated: cache fields simply read as
    ``None``."""
    csv_path = tmp_path / "usage-log.csv"
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    statusline._append_context_window_row(
        csv_path, {"session_id": "old", "context_window": {"used_tokens": 55}}, now
    )
    rows = statusline.load_usage_log_ground_truth(csv_path)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "old"
    assert rows[0]["context_window_used_tokens"] == 55
    assert rows[0]["cache_warm"] is None
    assert rows[0]["cache_misses"] is None


def test_load_usage_log_ground_truth_missing_file_returns_empty(tmp_path):
    assert statusline.load_usage_log_ground_truth(tmp_path / "does-not-exist.csv") == []


def test_load_usage_log_ground_truth_ignores_non_ground_truth_rows(tmp_path):
    from claude_token_lens.tools import log_usage as log_usage_mod

    csv_path = tmp_path / "usage-log.csv"
    log_usage_mod.append_rows(
        csv_path,
        [{"session_id": "s1", "window": "five_hour", "used_percentage": 42.0, "resets_at": ""}],
        source="statusline",
    )
    assert statusline.load_usage_log_ground_truth(csv_path) == []


# -- S1-exports: build_cache_ground_truth_table ------------------------------


def test_build_cache_ground_truth_table_empty_rows():
    table = statusline.build_cache_ground_truth_table(None)
    assert table.name == "cache_ground_truth"
    assert table.rows == []
    table2 = statusline.build_cache_ground_truth_table([])
    assert table2.rows == []


def test_build_cache_ground_truth_table_excludes_rows_without_cache_data():
    rows = [{"session_id": "s1", "cache_warm": None}]
    table = statusline.build_cache_ground_truth_table(rows)
    assert table.rows == []


def test_build_cache_ground_truth_table_summarises_per_session():
    rows = [
        {"session_id": "s1", "cache_warm": True, "cache_misses": 1, "cache_last_miss_cause": None, "cache_recache_tokens_if_cold": None},
        {"session_id": "s1", "cache_warm": False, "cache_misses": 2, "cache_last_miss_cause": "ttl", "cache_recache_tokens_if_cold": 1000},
        {"session_id": "s1", "cache_warm": False, "cache_misses": 3, "cache_last_miss_cause": "ttl", "cache_recache_tokens_if_cold": 3000},
        {"session_id": "s2", "cache_warm": True, "cache_misses": 0, "cache_last_miss_cause": None, "cache_recache_tokens_if_cold": None},
    ]
    table = statusline.build_cache_ground_truth_table(rows)
    by_session = {row[0]: row for row in table.rows}

    s1 = by_session["s1"]
    assert s1[1] == 3  # rows_logged
    assert round(s1[2], 3) == round(100.0 / 3, 3)  # warm_share: 1 of 3 warm
    assert s1[3] == 3  # misses: peak counter
    assert s1[4] == "ttl:2"
    assert s1[5] == 2000  # mean of 1000 and 3000

    s2 = by_session["s2"]
    assert s2[1] == 1
    assert s2[2] == 100.0
    assert s2[5] is None  # no recache values logged


# -- S1-exports (deliverable 2): report.build_report renders old + new format CSVs --


def test_report_cli_renders_with_old_and_new_format_usage_log(tmp_path):
    """cli.py's ``report`` command loads <config_dir>/usage-log.csv when
    present and passes it through to build_report -- both an old-format
    (S1-context-budget only, no cache_* columns) and a new-format
    (with cache_* columns) row must render the usage and context_budget
    tables without error."""
    from helpers import turn_line, write_jsonl

    from claude_token_lens import cli as cli_mod

    projects_root = tmp_path / "projects"
    project_dir = projects_root / "proj-a"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "session-1.jsonl",
        [turn_line(input_tokens=100 + i, output_tokens=20, cache_read_input_tokens=10) for i in range(3)],
    )

    config_dir = tmp_path / "token-lens"
    config_dir.mkdir()

    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    # Old-format row (S1-context-budget: 9 columns, no cache_* columns).
    statusline._append_context_window_row(
        config_dir / "usage-log.csv", {"session_id": "old-sess", "context_window": {"used_tokens": 1000}}, now
    )
    # New-format row (S1-exports: adds the 6 cache_* trailing columns).
    statusline._append_context_window_row(
        config_dir / "usage-log.csv",
        {
            "session_id": "new-sess",
            "context_window": {"used_tokens": 2000},
            "prompt_cache": {"warm": True, "ttl": "5m", "misses": 1},
        },
        now,
    )

    rc = cli_mod.main(
        [
            "report",
            "--projects-root",
            str(projects_root),
            "--all-projects",
            "--config-dir",
            str(config_dir),
        ]
    )
    assert rc == 0
