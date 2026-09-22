"""Tests for WP6's statusline renderer (src/claude_token_lens/statusline.py)."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_token_lens import installer, statusline


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


# -- v3-limits: near-cap "!" warning marker ----------------------------------


def test_render_status_rate_segment_gets_warning_marker_at_90_pct():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"rate_limits": {"five_hour": {"used_percentage": 92}}}
    assert statusline.render_status(payload, now, 300) == "5h 92%!"


def test_render_status_rate_segment_no_warning_marker_below_90_pct():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"rate_limits": {"five_hour": {"used_percentage": 89}}}
    assert statusline.render_status(payload, now, 300) == "5h 89%"


def test_render_status_rate_segment_warning_marker_at_exactly_90_pct():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"rate_limits": {"seven_day": {"used_percentage": 90}}}
    assert statusline.render_status(payload, now, 300) == "7d 90%!"


def test_render_status_both_rate_segments_can_carry_warning_markers():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {
        "rate_limits": {
            "five_hour": {"used_percentage": 100},
            "seven_day": {"used_percentage": 95},
        }
    }
    assert statusline.render_status(payload, now, 300) == "5h 100%! | 7d 95%!"


def test_render_status_line_length_stays_within_bound_with_warning_markers():
    now = datetime(2026, 9, 18, 12, 5, 0, tzinfo=timezone.utc)
    payload = {
        "context_window": {"used_tokens": 143000},
        "prompt_cache": {"warm": True, "ttl": "5m", "expires_at": now.timestamp() + 252},
        "rate_limits": {
            "five_hour": {"used_percentage": 100},
            "seven_day": {"used_percentage": 95},
        },
    }
    line = statusline.render_status(payload, now, 300)
    assert len(line) <= statusline._MAX_LINE_LEN
    assert line.endswith("5h 100%! | 7d 95%!")


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


# -- v3-limits: tagging an exhausted usage-log row as "limit_hit" -----------


def test_tag_limit_hit_rows_overrides_source_at_100_pct():
    rows = [{"window": "five_hour", "used_percentage": 100.0, "source": "statusline"}]
    tagged = statusline._tag_limit_hit_rows(rows)
    assert tagged[0]["source"] == "limit_hit"


def test_tag_limit_hit_rows_overrides_source_above_100_pct():
    rows = [{"window": "seven_day", "used_percentage": 103.0, "source": "statusline"}]
    tagged = statusline._tag_limit_hit_rows(rows)
    assert tagged[0]["source"] == "limit_hit"


def test_tag_limit_hit_rows_leaves_source_below_100_pct():
    rows = [{"window": "five_hour", "used_percentage": 99.9, "source": "statusline"}]
    tagged = statusline._tag_limit_hit_rows(rows)
    assert tagged[0]["source"] == "statusline"


def test_tag_limit_hit_rows_ignores_non_cap_windows():
    # spend_limit is a WINDOW_NAMES entry but not one of the two
    # account-wide-pause windows limits.py cross-checks against.
    rows = [{"window": "spend_limit", "used_percentage": 100.0, "source": "statusline"}]
    tagged = statusline._tag_limit_hit_rows(rows)
    assert tagged[0]["source"] == "statusline"


def test_tag_limit_hit_rows_does_not_mutate_input_rows():
    original = {"window": "five_hour", "used_percentage": 100.0, "source": "statusline"}
    rows = [original]
    statusline._tag_limit_hit_rows(rows)
    assert original["source"] == "statusline"


def test_tag_limit_hit_rows_tolerates_non_numeric_used_percentage():
    rows = [{"window": "five_hour", "used_percentage": None, "source": "statusline"}]
    tagged = statusline._tag_limit_hit_rows(rows)
    assert tagged[0]["source"] == "statusline"


def test_main_full_window_logs_limit_hit_source(monkeypatch, capsys, tmp_path):
    config_dir = tmp_path / "token-lens"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    payload = {"rate_limits": {"five_hour": {"used_percentage": 100}}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = statusline.main([])
    assert rc == 0

    from claude_token_lens.tools import log_usage

    csv_path = config_dir / "usage-log.csv"
    rows = log_usage.load_usage_log(csv_path)
    five_hour_rows = [r for r in rows if r["window"] == "five_hour"]
    assert len(five_hour_rows) == 1
    assert five_hour_rows[0]["source"] == "limit_hit"


def test_main_partial_window_keeps_statusline_source(monkeypatch, capsys, tmp_path):
    config_dir = tmp_path / "token-lens"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    payload = {"rate_limits": {"five_hour": {"used_percentage": 50}}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = statusline.main([])
    assert rc == 0

    from claude_token_lens.tools import log_usage

    csv_path = config_dir / "usage-log.csv"
    rows = log_usage.load_usage_log(csv_path)
    five_hour_rows = [r for r in rows if r["window"] == "five_hour"]
    assert len(five_hour_rows) == 1
    assert five_hour_rows[0]["source"] == "statusline"


def test_main_explicit_config_dir_flag_wins_over_env_var(monkeypatch, capsys, tmp_path):
    """``--config-dir PATH`` (forwarded by cli.py's ``_cmd_statusline`` --
    see ``docs/api.md``-adjacent ``resolve_config_dir`` contract: "
    ``--config-dir`` wins; else ``$CLAUDE_CONFIG_DIR``; else
    ``~/.claude``") must actually be honoured, not silently dropped in
    favour of ``$CLAUDE_CONFIG_DIR`` -- a real v0.2 release bug where the
    CLI's own ``--help`` advertised the flag but ``main()`` never parsed
    it out of argv, so it always wrote to the env-var/home-dir location
    regardless of what the caller passed.
    """
    env_config_dir = tmp_path / "env-dir"
    explicit_config_dir = tmp_path / "explicit-dir"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(env_config_dir))
    payload = {"rate_limits": {"five_hour": {"used_percentage": 5}}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    rc = statusline.main(["--config-dir", str(explicit_config_dir)])

    assert rc == 0
    assert (explicit_config_dir / "usage-log.csv").exists()
    assert not (env_config_dir / "token-lens" / "usage-log.csv").exists()
    assert not (env_config_dir / "usage-log.csv").exists()


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


def _extract_commands(text: str) -> tuple[str, str]:
    """(windows_command, posix_command) parsed out of a
    ``print_install_fragment``-shaped report -- each platform's JSON
    block decoded properly rather than string-matched, since a Windows
    path's backslashes are JSON-escaped in the fragment text itself
    (``"C:\\\\...\\\\claude-token-lens.pyz"``).
    """
    _, _, rest = text.partition("Windows:\n")
    windows_json, _, posix_block = rest.partition("POSIX (Linux/macOS):\n")
    windows_command = json.loads(windows_json.strip())["statusLine"]["command"]
    posix_command = json.loads(posix_block.strip())["statusLine"]["command"]
    return windows_command, posix_command


def test_print_install_fragment_pyz_mode_uses_archive_path_not_dash_m(tmp_path):
    """When invoked from a ``.pyz`` build, ``python -m
    claude_token_lens.statusline`` does not work -- the package lives
    inside the archive, not on ``sys.path``. Passing ``pyz_path`` explicitly
    (mirroring ``installer.plan_service_install``'s own parameter) must
    produce the archive-path form instead, matching
    ``installer._serve_argv``'s ``[exe, str(pyz_path), *args]`` shape.
    """
    pyz_path = tmp_path / "claude-token-lens.pyz"
    text = statusline.print_install_fragment(pyz_path=pyz_path)

    assert "-m claude_token_lens.statusline" not in text
    windows_command, posix_command = _extract_commands(text)
    assert windows_command == f'py -3 "{pyz_path}" statusline'
    assert posix_command == f'python3 "{pyz_path}" statusline'


def test_print_install_fragment_pyz_mode_resolves_relative_path(tmp_path, monkeypatch):
    """An explicit ``pyz_path`` that isn't already absolute is still
    embedded as an absolute path -- the fragment ends up pasted into
    settings.json and run from an arbitrary working directory later."""
    monkeypatch.chdir(tmp_path)
    text = statusline.print_install_fragment(pyz_path=Path("claude-token-lens.pyz"))
    windows_command, posix_command = _extract_commands(text)
    abs_path = str((tmp_path / "claude-token-lens.pyz").resolve())
    assert windows_command == f'py -3 "{abs_path}" statusline'
    assert posix_command == f'python3 "{abs_path}" statusline'


def test_print_install_fragment_auto_detects_pyz_from_sys_argv(monkeypatch, tmp_path):
    """With no explicit ``pyz_path``, the fragment auto-detects the same
    way ``installer.detect_pyz_path``/``install-service`` already does --
    from ``sys.argv[0]`` -- so a plain ``claude-token-lens.pyz init`` run
    (which calls this with no arguments, see ``cli._cmd_init``) still gets
    the pyz-aware fragment without any extra wiring.
    """
    archive = tmp_path / "claude-token-lens.pyz"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("__main__.py", "print('hi')\n")
    monkeypatch.setattr(installer.sys, "argv", [str(archive)])

    text = statusline.print_install_fragment()

    assert "-m claude_token_lens.statusline" not in text
    windows_command, posix_command = _extract_commands(text)
    abs_path = str(archive.resolve())
    assert windows_command == f'py -3 "{abs_path}" statusline'
    assert posix_command == f'python3 "{abs_path}" statusline'


def test_print_install_fragment_no_pyz_keeps_dash_m_form():
    text = statusline.print_install_fragment(pyz_path=None)
    assert "py -3 -m claude_token_lens.statusline" in text
    assert "python3 -m claude_token_lens.statusline" in text


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
        "cache_miss_causes",
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


def test_report_cli_scopes_cache_ground_truth_to_the_report_window(tmp_path, capsys):
    """Regression test for review finding 8 (should-fix): cache_ground_truth
    used to include every ground-truth row ever logged, from every
    session, ignoring the invocation's own --days/--since/--until window.
    Two rows logged for the same session -- one just now, one over a
    year ago -- must only count the recent one once the report is
    scoped to a recent --days window.
    """
    from helpers import turn_line, write_jsonl

    from claude_token_lens import cli as cli_mod

    projects_root = tmp_path / "projects"
    project_dir = projects_root / "proj-a"
    project_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat().replace("+00:00", "Z")
    write_jsonl(project_dir / "s1.jsonl", [turn_line(timestamp=now_iso, input_tokens=100)])

    config_dir = tmp_path / "token-lens"
    config_dir.mkdir()
    csv_path = config_dir / "usage-log.csv"

    old = now - timedelta(days=400)
    statusline._append_context_window_row(
        csv_path, {"session_id": "s1", "prompt_cache": {"warm": True, "misses": 1}}, old
    )
    statusline._append_context_window_row(
        csv_path, {"session_id": "s1", "prompt_cache": {"warm": False, "misses": 2}}, now
    )
    # Sanity: both rows really did land on disk before scoping.
    all_rows = statusline.load_usage_log_ground_truth(csv_path)
    assert len(all_rows) == 2

    rc = cli_mod.main(
        [
            "report",
            "--projects-root",
            str(projects_root),
            "--all-projects",
            "--config-dir",
            str(config_dir),
            "--days",
            "30",
            "--json",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    usage_section = next(s for s in payload["report"]["sections"] if s["key"] == "usage")
    cache_table = next(t for t in usage_section["tables"] if t["name"] == "cache_ground_truth")
    by_session = {row[0]: row for row in cache_table["rows"]}
    # Only the recent row (inside the 30-day window) counts -- the
    # year-old row must be excluded, so rows_logged is 1, not 2.
    assert by_session["s1"][1] == 1


# -- review finding 3: statusline length bound -------------------------------


def test_render_status_bounds_line_length_against_huge_expires_at():
    """Regression test for review finding 3 (should-fix): an unbounded
    ``expires_at`` (e.g. 1e308) used to produce a 300+ character line by
    feeding straight into arithmetic with no clamp. render_status must
    now stay at or under statusline._MAX_LINE_LEN for every payload."""
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": True, "ttl": "5m", "expires_at": 1e308}}
    line = statusline.render_status(payload, now, 300)
    assert len(line) <= statusline._MAX_LINE_LEN


def test_render_status_bounds_line_length_against_huge_recache_tokens():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": False, "recache_tokens_if_cold": 1e300}}
    line = statusline.render_status(payload, now, 300)
    assert len(line) <= statusline._MAX_LINE_LEN
    # Capped at _MAX_RECACHE_TOKENS before formatting, not left as 1e300.
    assert "recache ~10000k tokens" in line


def test_render_status_treats_huge_expires_at_as_epoch_milliseconds():
    """A payload shaped like ``(now + 252) * 1000`` (a plausible epoch-ms
    variant per the module docstring, not just adversarial input) must
    degrade to a sane countdown rather than a multi-digit garbage value."""
    now = datetime(2026, 9, 18, 12, 5, 0, tzinfo=timezone.utc)
    expires_at_ms = (now.timestamp() + 252) * 1000
    payload = {"prompt_cache": {"warm": True, "ttl": "5m", "expires_at": expires_at_ms}}
    line = statusline.render_status(payload, now, 300)
    assert line == "cache warm 5m 04:12"
    assert len(line) <= statusline._MAX_LINE_LEN


def test_render_status_full_line_never_exceeds_max_len_with_all_segments_hostile():
    """Every segment hostile at once -- the assembled line (even after
    per-segment clamps) must still respect the hard cap, and must never
    contain an embedded newline (finding 4's "one line" guarantee)."""
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {
        "context_window": {"used_tokens": 999999999},
        "prompt_cache": {"warm": True, "ttl": "5m\nEVIL", "expires_at": 1e308},
        "rate_limits": {
            "five_hour": {"used_percentage": 37},
            "seven_day": {"used_percentage": 12},
        },
    }
    line = statusline.render_status(payload, now, 300)
    assert len(line) <= statusline._MAX_LINE_LEN
    assert "\n" not in line
    assert "\r" not in line


# -- review finding 4: ttl injection -----------------------------------------


def test_render_status_ttl_with_embedded_newline_never_emits_second_line():
    """Regression test for review finding 4 (should-fix): a ``ttl`` string
    containing a newline used to be echoed verbatim, breaking the "one
    line" contract. It must now fall back to the numeric/label handling
    (or "?"), never carry the newline through."""
    now = datetime(2026, 9, 18, 12, 5, 0, tzinfo=timezone.utc)
    payload = {
        "prompt_cache": {"warm": True, "ttl": "5m\nEVIL SECOND LINE", "expires_at": now.timestamp() + 252},
    }
    line = statusline.render_status(payload, now, 300)
    assert "\n" not in line
    assert "EVIL" not in line


def test_render_status_ttl_non_shape_string_falls_back_to_placeholder():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": True, "ttl": "not-a-real-ttl-value-that-is-way-too-long"}}
    line = statusline.render_status(payload, now, 300)
    assert line == "cache warm ?"


def test_render_status_ttl_valid_shape_string_still_echoed():
    """A ``ttl`` matching ``^\\d+[smh]$`` is still accepted and echoed --
    the hardening only rejects everything else."""
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": True, "ttl": "900s"}}
    assert statusline.render_status(payload, now, 300) == "cache warm 900s"


# -- nit 13: expired warm countdown renders "expiring", not stuck 00:00 -----


def test_render_status_warm_countdown_past_expiry_renders_expiring():
    now = datetime(2026, 9, 18, 12, 10, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": True, "ttl": "5m", "expires_at": now.timestamp() - 30}}
    assert statusline.render_status(payload, now, 300) == "cache warm 5m expiring"


# -- review finding 5: top_miss_causes from the wire's cumulative counts ----


def test_cache_row_values_reads_miss_causes_cumulative_counts():
    """``prompt_cache.miss_causes`` (the wire's own cumulative
    per-cause counts) is mapped through the same short-token allowlist
    and formatted as a compact ``cause:count;cause:count`` string."""
    payload = {
        "prompt_cache": {
            "warm": True,
            "miss_causes": {"tools_changed": 3, "ttl_expired_5m": 2, "some_unknown_cause": 1},
        }
    }
    values = statusline._cache_row_values(payload)
    assert values is not None
    miss_causes_str = values[6]
    assert miss_causes_str == "other:1;tools:3;ttl:2"


def test_build_cache_ground_truth_table_uses_last_row_cumulative_miss_causes():
    """Regression test for review finding 5 (should-fix): the *wire's*
    cumulative cache_miss_causes snapshot must be taken from the last row
    per session (an overwrite), not summed once per logged row -- summing
    would double the true counts across repeated refreshes."""
    rows = [
        {"session_id": "s1", "cache_warm": True, "cache_misses": 1, "cache_miss_causes": "ttl:1"},
        {"session_id": "s1", "cache_warm": True, "cache_misses": 1, "cache_miss_causes": "ttl:1"},
        {"session_id": "s1", "cache_warm": True, "cache_misses": 1, "cache_miss_causes": "ttl:1"},
    ]
    table = statusline.build_cache_ground_truth_table(rows)
    by_session = {row[0]: row for row in table.rows}
    assert by_session["s1"][4] == "ttl:1"  # not "ttl:3"


def test_build_cache_ground_truth_table_falls_back_to_counting_genuine_misses():
    """Reproduces the review's own repro: one real miss (cause 'ttl'),
    followed by nine quiet warm turns where prompt_cache.last_miss_cause
    stays sticky (still 'ttl') but cache_misses does not increase, and no
    row in the session ever carries cache_miss_causes data at all (the
    old-format-log fallback case). top_miss_causes must report 'ttl:1',
    matching the misses column beside it -- not 'ttl:10' from naively
    counting the sticky field once per row."""
    rows = [{"session_id": "s1", "cache_warm": True, "cache_misses": 0, "cache_last_miss_cause": None}]
    rows.append({"session_id": "s1", "cache_warm": True, "cache_misses": 1, "cache_last_miss_cause": "ttl"})
    for _ in range(8):
        rows.append({"session_id": "s1", "cache_warm": True, "cache_misses": 1, "cache_last_miss_cause": "ttl"})
    table = statusline.build_cache_ground_truth_table(rows)
    by_session = {row[0]: row for row in table.rows}
    s1 = by_session["s1"]
    assert s1[1] == 10  # rows_logged
    assert s1[3] == 1  # misses: peak counter
    assert s1[4] == "ttl:1"  # not "ttl:10"


# -- review finding 6: usage-log header upgrade ------------------------------


def test_append_context_window_row_upgrades_a_legacy_6_column_header(tmp_path):
    """Regression test for review finding 6 (should-fix): a file created
    by log_usage.append_rows first (6-column CSV_FIELDS header) followed
    by a ground-truth row appended positionally used to leave a
    16-column row sitting under a 6-column header forever. Appending a
    ground-truth row must now upgrade the header once, atomically,
    padding every existing row out to the new width."""
    from claude_token_lens.tools import log_usage as log_usage_mod

    csv_path = tmp_path / "usage-log.csv"
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    # Simulate the real main() ordering: append_rows creates the file
    # with the plain 6-column header and one rate_limits row first.
    log_usage_mod.append_rows(
        csv_path,
        [{"session_id": "s1", "window": "five_hour", "used_percentage": 37.0, "resets_at": "x"}],
        source="statusline",
        now=now,
    )
    with open(csv_path, encoding="utf-8", newline="") as fh:
        header_before = fh.readline().strip().split(",")
    assert len(header_before) == 6

    statusline._append_context_window_row(
        csv_path, {"session_id": "s1", "context_window": {"used_tokens": 5000}}, now
    )

    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        import csv as _csv

        rows = list(_csv.reader(fh))
    header_after = rows[0]
    assert header_after == list(statusline._GROUND_TRUTH_HEADER)
    # The pre-existing rate_limits row was padded out to the new width,
    # not truncated or dropped.
    legacy_row = rows[1]
    assert len(legacy_row) == len(header_after)
    assert legacy_row[:6] == [now.isoformat().replace("+00:00", "Z"), "s1", "five_hour", "37.0", "x", "statusline"]
    assert legacy_row[6:] == [""] * (len(header_after) - 6)

    # The new ground-truth row landed after the (now-padded) legacy row,
    # with its own real values.
    new_row = rows[2]
    assert new_row[2] == "context_window"
    assert new_row[6] == "5000.0" or new_row[6] == "5000"


def test_load_usage_log_upgraded_file_is_readable_by_dict_reader_without_none_key(tmp_path):
    """Fix for review finding 6's defence-in-depth (log_usage.py's
    ``restkey="_extra"``): even before any header upgrade runs, a
    ground-truth row with more fields than a legacy 6-column header
    must not corrupt log_usage.load_usage_log's dict rows with a
    literal ``None`` key -- the overflow lands under "_extra" instead.
    """
    from claude_token_lens.tools import log_usage as log_usage_mod

    csv_path = tmp_path / "usage-log.csv"
    # Write a legacy-shaped header directly, then a longer row under it,
    # without going through _ensure_ground_truth_header, to exercise the
    # reader's own defence independently of the writer-side fix.
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        fh.write("logged_at,session_id,window,used_percentage,resets_at,source\n")
        fh.write("2026-09-18T00:00:00Z,s1,context_window,,,statusline,5000,,,1\n")

    rows = log_usage_mod.load_usage_log(csv_path)
    assert len(rows) == 1
    assert None not in rows[0]
    assert rows[0]["_extra"] == ["5000", "", "", "1"]


# -- nit 16: context_window field-name fallbacks -----------------------------


def test_render_status_ctx_segment_falls_back_to_total_input_tokens():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"context_window": {"total_input_tokens": 42000}}
    assert statusline.render_status(payload, now, 300) == "ctx 42k"


def test_render_status_ctx_segment_falls_back_to_current_usage_sum():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {
        "context_window": {
            "current_usage": {
                "input_tokens": 1000,
                "cache_creation_input_tokens": 500,
                "cache_read_input_tokens": 250,
            }
        }
    }
    assert statusline.render_status(payload, now, 300) == "ctx 2k"


def test_context_window_row_values_field_name_fallbacks():
    payload = {
        "session_id": "s1",
        "context_window": {
            "total_input_tokens": 1000,
            "total_tokens": 200000,
            "remaining_percentage": 75,
        },
    }
    values = statusline._context_window_row_values(payload)
    assert values is not None
    session_id, used_percentage, used_tokens, size, autocompact = values
    assert used_tokens == 1000
    assert size == 200000
    assert used_percentage == 25  # 100 - remaining_percentage


def test_context_window_size_falls_back_to_size_field():
    assert statusline._context_window_size({"size": 100000}) == 100000


# -- payload key-name recording -----------------------------------------


def test_record_payload_keys_writes_dotted_names_only(tmp_path):
    config_dir = tmp_path / "token-lens"
    config_dir.mkdir()
    payload = {
        "context_window": {"used_tokens": 1000},
        "prompt_cache": {"warm": True, "expires_at": 123.0},
        "session_id": "sess-super-secret-value",
    }
    statusline.record_payload_keys(payload, config_dir)
    keys_path = config_dir / "statusline-keys.json"
    assert keys_path.exists()
    data = json.loads(keys_path.read_text(encoding="utf-8"))
    assert set(data["keys"]) == {
        "context_window",
        "context_window.used_tokens",
        "prompt_cache",
        "prompt_cache.warm",
        "prompt_cache.expires_at",
        "session_id",
    }
    # Names only -- the secret-looking session id value must never appear.
    assert "sess-super-secret-value" not in keys_path.read_text(encoding="utf-8")


def test_record_payload_keys_does_not_rewrite_when_unchanged(tmp_path):
    config_dir = tmp_path / "token-lens"
    config_dir.mkdir()
    payload = {"a": 1, "b": {"c": 2}}
    statusline.record_payload_keys(payload, config_dir)
    keys_path = config_dir / "statusline-keys.json"
    first_mtime = keys_path.stat().st_mtime_ns
    statusline.record_payload_keys(payload, config_dir)
    assert keys_path.stat().st_mtime_ns == first_mtime


def test_record_payload_keys_caps_at_max_recorded_keys(tmp_path):
    config_dir = tmp_path / "token-lens"
    config_dir.mkdir()
    payload = {f"key_{i}": i for i in range(500)}
    statusline.record_payload_keys(payload, config_dir)
    data = json.loads((config_dir / "statusline-keys.json").read_text(encoding="utf-8"))
    assert len(data["keys"]) <= statusline._MAX_RECORDED_KEYS


def test_main_records_payload_keys(monkeypatch, capsys, tmp_path):
    config_dir = tmp_path / "token-lens"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    payload = {"context_window": {"used_tokens": 10000}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = statusline.main([])
    assert rc == 0
    keys_path = config_dir / "statusline-keys.json"
    assert keys_path.exists()
    data = json.loads(keys_path.read_text(encoding="utf-8"))
    assert "context_window.used_tokens" in data["keys"]


# -- measured cache-miss causes (Cache tab) ----------------------------------


def test_build_measured_miss_causes_table_sums_final_counts_per_session():
    rows = [
        # s1 carries the cumulative field: only its last value counts.
        {"session_id": "s1", "cache_warm": True, "cache_misses": 1, "cache_miss_causes": "ttl:1"},
        {"session_id": "s1", "cache_warm": False, "cache_misses": 3, "cache_miss_causes": "tools:1;ttl:2"},
        # s2 has only the sticky last cause: one count per increase.
        {"session_id": "s2", "cache_warm": False, "cache_misses": 1, "cache_last_miss_cause": "ttl"},
        {"session_id": "s2", "cache_warm": False, "cache_misses": 1, "cache_last_miss_cause": "ttl"},
        {"session_id": "s2", "cache_warm": False, "cache_misses": 2, "cache_last_miss_cause": "sysprompt"},
    ]
    table = statusline.build_measured_miss_causes_table(rows)
    by_cause = {row[0]: row for row in table.rows}
    assert by_cause["ttl"][1] == 3 and by_cause["ttl"][3] == 2
    assert by_cause["tools"][1] == 1 and by_cause["sysprompt"][1] == 1
    assert round(sum(row[2] for row in table.rows), 6) == 100.0
    assert table.rows[0][0] == "ttl"  # most misses first
    assert table.value_labels["ttl"].startswith("Cache expired")


def test_build_measured_miss_causes_table_is_none_without_cause_data():
    assert statusline.build_measured_miss_causes_table(None) is None
    assert statusline.build_measured_miss_causes_table([{"session_id": "s1", "cache_warm": True, "cache_misses": 0}]) is None


def test_scoped_usage_log_rows_filters_sessions_and_window(tmp_path):
    assert statusline.scoped_usage_log_rows(tmp_path / "missing.csv", {"s1"}, None, None) is None
    rows = [
        {"session_id": "s1", "logged_at": "2026-09-20T10:00:00Z"},
        {"session_id": "s1", "logged_at": "2026-09-01T10:00:00Z"},
        {"session_id": "other", "logged_at": "2026-09-20T10:00:00Z"},
    ]
    since = datetime(2026, 9, 10, tzinfo=timezone.utc)
    kept = [r for r in rows if r["session_id"] in {"s1"} and statusline.usage_log_row_in_window(r, since, None)]
    assert kept == [rows[0]]
