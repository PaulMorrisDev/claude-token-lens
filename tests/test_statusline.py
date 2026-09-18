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
    # 4m12s = 252s remaining out of a 300s TTL means the last assistant
    # turn was 48s before `now`.
    last_ts = now - timedelta(seconds=48)
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, last_ts.isoformat().replace("+00:00", "Z"))

    payload = {
        "context_window": {"used_tokens": 143000},
        "prompt_cache": {"hit_percentage": 92},
        "transcript_path": str(transcript),
        "rate_limits": {
            "five_hour": {"used_percentage": 37},
            "seven_day": {"used_percentage": 12},
        },
    }
    line = statusline.render_status(payload, now, 300)
    assert line == "ctx 143k | cache 92% | 5m TTL expires in 4m12s | 5h 37% | 7d 12%"


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


def test_render_status_cache_hit_rate_fraction_variant():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"hit_rate": 0.87}}
    assert statusline.render_status(payload, now, 300) == "cache 87%"


def test_render_status_cache_computed_from_token_counts():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {
        "prompt_cache": {
            "cache_read_tokens": 900,
            "cache_creation_tokens": 100,
            "input_tokens": 0,
        }
    }
    assert statusline.render_status(payload, now, 300) == "cache 90%"


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
    last_ts = now - timedelta(seconds=600)  # 10 minutes ago, TTL is 5m
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, last_ts.isoformat().replace("+00:00", "Z"))
    payload = {"transcript_path": str(transcript)}
    line = statusline.render_status(payload, now, 300)
    assert line == "5m TTL expired"


def test_render_status_ttl_1h_label(tmp_path):
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    last_ts = now - timedelta(seconds=30)
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, last_ts.isoformat().replace("+00:00", "Z"))
    payload = {"transcript_path": str(transcript)}
    line = statusline.render_status(payload, now, 3600)
    assert line == "1h TTL expires in 59m30s"


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
    assert line == "5m TTL expires in 4m50s"


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
    assert len(rows) == 1
    assert rows[0]["window"] == "five_hour"


def test_main_no_rate_limits_does_not_create_usage_log(monkeypatch, capsys, tmp_path):
    config_dir = tmp_path / "token-lens"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    payload = {"context_window": {"used_tokens": 1000}}
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
