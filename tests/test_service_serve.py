"""Tests for ``service.serve.run`` itself (as opposed to ``test_service_cli.py``,
which mocks ``run_serve`` out entirely to test ``cli.py``'s own argv wiring).

Small, synthetic corpus only -- no real ``~/.claude`` data.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from claude_token_lens.service import serve
from claude_token_lens.service.contracts import ServeOptions

from helpers import turn_line, write_jsonl


def _write_session(root: Path, slug: str, session_id: str, lines: list[dict]) -> Path:
    project_dir = root / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    write_jsonl(path, lines)
    # Comfortably outside the watcher's live-file window so this tick
    # treats it as a stable, finished transcript.
    old = time.time() - 3600
    os.utime(path, (old, old))
    return path


def test_once_prints_watcher_stats_line(tmp_path: Path, capsys):
    """``serve --once`` (S1-perf/S1-api release-verification finding):
    before this fix the ``--once`` code path ran the watcher tick and
    exited without printing anything, discarding the exact
    ``WatcherStats`` fields (``discovery_s``/``parse_s``/``store_s``,
    ``errors``, ...) ``docs/api.md`` documents as a first-class
    diagnostic surface. A one-shot/cron/verification run had no way to
    see what the tick did short of opening the store directly.
    """
    root = tmp_path / "projects"
    _write_session(
        root,
        "proj-a",
        "sess-a1",
        [
            turn_line(timestamp="2026-09-18T12:00:00.000Z", input_tokens=100, output_tokens=10),
            turn_line(timestamp="2026-09-18T12:05:00.000Z", input_tokens=120, output_tokens=12),
        ],
    )
    options = ServeOptions(projects_root=root, config_dir=tmp_path / "config")

    rc = serve.run(options, once=True)

    assert rc == 0
    out = capsys.readouterr().out
    assert "claude-token-lens serve --once:" in out
    assert "duration_s=" in out
    assert "discovery_s=" in out
    assert "parse_s=" in out
    assert "store_s=" in out
    assert "files_parsed=1" in out
    assert "sessions_upserted=1" in out
    assert "errors=0" in out


def test_once_with_no_sessions_still_prints_a_clean_stats_line(tmp_path: Path, capsys):
    root = tmp_path / "projects"
    root.mkdir(parents=True)
    options = ServeOptions(projects_root=root, config_dir=tmp_path / "config")

    rc = serve.run(options, once=True)

    assert rc == 0
    out = capsys.readouterr().out
    assert "files_parsed=0" in out
    assert "errors=0" in out
