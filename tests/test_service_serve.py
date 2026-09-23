"""Tests for ``service.serve.run`` itself (as opposed to ``test_service_cli.py``,
which mocks ``run_serve`` out entirely to test ``cli.py``'s own argv wiring).

Small, synthetic corpus only -- no real ``~/.claude`` data.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from claude_token_lens.service import serve, storelock
from claude_token_lens.service.contracts import ServeOptions
from claude_token_lens.service.storelock import StoreLock

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


def test_once_uses_the_store_path_when_given(tmp_path: Path):
    root = tmp_path / "projects"
    root.mkdir(parents=True)
    store_path = tmp_path / "elsewhere" / "dev.db"
    options = ServeOptions(projects_root=root, config_dir=tmp_path / "config", store_path=store_path)

    assert serve.run(options, once=True) == 0

    assert store_path.exists()
    assert not (tmp_path / "config" / serve.STORE_FILENAME).exists()


def test_a_store_another_serve_holds_is_refused(tmp_path: Path, capsys):
    root = tmp_path / "projects"
    root.mkdir(parents=True)
    config_dir = tmp_path / "config"
    options = ServeOptions(projects_root=root, config_dir=config_dir)
    held = StoreLock(config_dir / serve.STORE_FILENAME)
    held.acquire({"pid": 4242, "bind": "127.0.0.1", "port": 8765, "once": False})
    try:
        rc = serve.run(options, once=True)
    finally:
        held.release()

    assert rc == 1
    err = capsys.readouterr().err
    assert "process 4242" in err
    assert "http://127.0.0.1:8765" in err
    assert "--store" in err
    # Refused before touching the database.
    assert not (config_dir / serve.STORE_FILENAME).exists()


def test_the_lock_is_released_when_serve_returns(tmp_path: Path):
    root = tmp_path / "projects"
    root.mkdir(parents=True)
    options = ServeOptions(projects_root=root, config_dir=tmp_path / "config")

    assert serve.run(options, once=True) == 0
    assert storelock.holder(tmp_path / "config" / serve.STORE_FILENAME) is None
    assert serve.run(options, once=True) == 0


def test_the_dashboard_answers_while_the_first_scan_is_still_running(tmp_path: Path, monkeypatch):
    """The port is bound before the first scan, which runs on the
    watcher's thread; until it finishes, /api/health says so."""
    import http.client
    import json
    import threading

    from claude_token_lens.service import watcher as watcher_mod

    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", [turn_line(timestamp="2026-09-18T12:00:00.000Z")])
    release_scan = threading.Event()
    real_run_once = watcher_mod.FileWatcher._run_once

    def _slow_first_scan(self, stats):
        release_scan.wait(10)
        return real_run_once(self, stats)

    monkeypatch.setattr(watcher_mod.FileWatcher, "_run_once", _slow_first_scan)

    servers = []

    class _RecordingServer(serve.ThreadingHTTPServer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            servers.append(self)

    monkeypatch.setattr(serve, "ThreadingHTTPServer", _RecordingServer)
    # No real schtasks/systemctl probe from a test.
    from claude_token_lens import installer

    monkeypatch.setattr(installer, "is_registered", lambda *a, **k: None)

    options = ServeOptions(projects_root=root, config_dir=tmp_path / "config", port=0, poll_interval_s=60)
    result: dict = {}
    thread = threading.Thread(target=lambda: result.setdefault("rc", serve.run(options)), daemon=True)
    thread.start()

    def _health() -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", servers[0].server_port, timeout=10)
        try:
            conn.request("GET", "/api/health")
            return json.loads(conn.getresponse().read())["data"]
        finally:
            conn.close()

    try:
        deadline = time.monotonic() + 10
        while not servers and time.monotonic() < deadline:
            time.sleep(0.01)
        assert servers, "serve never bound its port"
        during = _health()
        assert during["status"] == "starting"
        assert during["scan"]["running"] is True
        lock_info = storelock.holder(tmp_path / "config" / serve.STORE_FILENAME)
        assert lock_info["port"] == servers[0].server_port

        release_scan.set()
        deadline = time.monotonic() + 10
        while _health()["status"] != "ok" and time.monotonic() < deadline:
            time.sleep(0.05)
        after = _health()
        assert after["status"] == "ok"
        assert after["watcher"]["sessions_upserted"] == 1
    finally:
        release_scan.set()
        if servers:
            servers[0].shutdown()
        thread.join(timeout=15)
    assert result.get("rc") == 0
    assert storelock.holder(tmp_path / "config" / serve.STORE_FILENAME) is None


def test_a_port_in_use_is_reported_not_raised(tmp_path: Path, capsys):
    import socket

    root = tmp_path / "projects"
    root.mkdir(parents=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        options = ServeOptions(projects_root=root, config_dir=tmp_path / "config", port=port)
        rc = serve.run(options)
    assert rc == 1
    assert f"port {port}" in capsys.readouterr().err
    assert storelock.holder(tmp_path / "config" / serve.STORE_FILENAME) is None


def test_the_lock_is_dropped_when_its_holder_dies(tmp_path: Path):
    import subprocess
    import sys

    store_path = tmp_path / "service.db"
    holder_script = (
        "import sys, time\n"
        "from claude_token_lens.service.storelock import StoreLock\n"
        f"StoreLock({str(store_path)!r}).acquire({{'pid': 1}})\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", holder_script], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        assert storelock.holder(store_path) == {"pid": 1}
        with pytest.raises(storelock.StoreLockedError):
            StoreLock(store_path).acquire({"pid": 2})
    finally:
        proc.kill()
        proc.wait(timeout=10)
    # Windows lets go of a dead process's locks shortly after, not at once.
    lock = StoreLock(store_path)
    lock.acquire({"pid": 2}, wait_s=10)
    lock.release()
