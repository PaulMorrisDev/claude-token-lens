"""Tests for ``service.watcher.FileWatcher`` against synthetic transcript
trees under a temporary ``projects_root`` — no real ``~/.claude`` data.

``_STABLE_AGE_S``-backdated files (via ``os.utime``) are the default
posture for every test that isn't specifically exercising the live-file
window, so a slow CI box never flips a test's expectation by accident.
Liveness tests instead drive a :class:`_FakeClock` passed as the
``now`` constructor argument, so no test ever sleeps for real seconds.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from claude_token_lens.service.contracts import ServeOptions
from claude_token_lens.service.store import Store
from claude_token_lens.service.watcher import LIVE_FILE_WINDOW_S, FileWatcher

from helpers import assert_privacy, turn_line, write_jsonl

#: Comfortably outside the live window, for a file meant to look "stable"
#: (finished being written) from the very first tick that sees it.
_STABLE_AGE_S = LIVE_FILE_WINDOW_S * 10


class _FakeClock:
    """A settable ``now()`` callable, so a test can move the watcher's
    clock forward without a real ``time.sleep``."""

    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _backdated(seconds_ago: float) -> float:
    return time.time() - seconds_ago


def _write_session(root: Path, slug: str, session_id: str, lines: list[dict], age_s: float = _STABLE_AGE_S) -> Path:
    project_dir = root / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    write_jsonl(path, lines)
    mtime = _backdated(age_s)
    os.utime(path, (mtime, mtime))
    return path


def _write_subagent(
    root: Path,
    slug: str,
    session_id: str,
    agent_id: str,
    lines: list[dict],
    meta: dict | None = None,
    age_s: float = _STABLE_AGE_S,
) -> Path:
    subagents_dir = root / slug / session_id / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    path = subagents_dir / f"{agent_id}.jsonl"
    write_jsonl(path, lines)
    meta_path = subagents_dir / f"{agent_id}.meta.json"
    meta_path.write_text(json.dumps(meta or {"agentType": "claude-implementer"}), encoding="utf-8")
    mtime = _backdated(age_s)
    os.utime(path, (mtime, mtime))
    os.utime(meta_path, (mtime, mtime))
    return path


def _two_turns() -> list[dict]:
    return [
        turn_line(timestamp="2026-09-18T12:00:00.000Z", input_tokens=100, output_tokens=10),
        turn_line(timestamp="2026-09-18T12:05:00.000Z", input_tokens=120, output_tokens=12),
    ]


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    s.open()
    return s


def _options(tmp_path: Path, **overrides) -> ServeOptions:
    kwargs = {
        "projects_root": tmp_path / "projects",
        "config_dir": tmp_path / "config",
    }
    kwargs.update(overrides)
    return ServeOptions(**kwargs)


# -- basic corpus parse ----------------------------------------------------


def test_run_once_parses_synthetic_two_session_corpus(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())
    _write_session(root, "proj-b", "sess-b1", _two_turns() + [turn_line(timestamp="2026-09-18T12:10:00.000Z")])
    _write_subagent(root, "proj-b", "sess-b1", "agent-1", [turn_line(timestamp="2026-09-18T12:06:00.000Z")])

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    stats = watcher.run_once()

    assert_privacy(stats)
    assert stats.errors == 0
    assert stats.files_scanned == 3
    assert stats.files_parsed == 3
    assert stats.files_skipped_live == 0
    assert stats.sessions_upserted == 2

    summary = store.summary()
    assert_privacy(summary)
    assert summary["sessions"] == 2
    assert summary["transcripts"] == 3

    session_a = store.session("sess-a1")
    assert session_a is not None
    assert len(session_a["transcripts"]) == 1
    session_b = store.session("sess-b1")
    assert session_b is not None
    assert len(session_b["transcripts"]) == 2
    kinds = {t["kind"] for t in session_b["transcripts"]}
    assert kinds == {"top-level", "subagent"}


# -- incremental re-parse ---------------------------------------------------


def test_incremental_reparse_only_touches_the_changed_file(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    path_a = _write_session(root, "proj-a", "sess-a1", _two_turns())
    _write_session(root, "proj-b", "sess-b1", _two_turns())

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    watcher.run_once()

    daily_before = {(r["day"], r["model"]): r["turns"] for r in store.daily_usage(days=3650)}
    assert sum(daily_before.values()) == 4  # 2 turns x 2 sessions

    # Grow session A's file with one more turn, still well outside the
    # live window (a different, later backdated mtime -- "changed").
    lines = _two_turns() + [turn_line(timestamp="2026-09-18T12:10:00.000Z")]
    write_jsonl(path_a, lines)
    new_mtime = _backdated(_STABLE_AGE_S - 1)  # still stable, but a different (newer) mtime than before
    os.utime(path_a, (new_mtime, new_mtime))

    stats2 = watcher.run_once()
    assert_privacy(stats2)
    assert stats2.errors == 0
    assert stats2.files_parsed == 1  # only session A's top-level file

    daily_after = {(r["day"], r["model"]): r["turns"] for r in store.daily_usage(days=3650)}
    assert sum(daily_after.values()) == 5  # A grew from 2 to 3 turns; B unchanged

    session_a = store.session("sess-a1")
    assert len(session_a["transcripts"]) == 1  # no duplicate row for the same path


# -- live-file skip / re-check next tick ------------------------------------


def test_live_file_is_skipped_then_reparsed_once_stable(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    path_a = _write_session(root, "proj-a", "sess-a1", _two_turns())

    clock = _FakeClock(time.time())
    options = _options(tmp_path)
    watcher = FileWatcher(store, options, now=clock)

    stats1 = watcher.run_once()
    assert stats1.files_parsed == 1
    assert stats1.files_skipped_live == 0

    # "Touch" the file (grow it, bump mtime to right now -- live) without
    # advancing the watcher's clock.
    live_mtime = time.time()
    write_jsonl(path_a, _two_turns() + [turn_line(timestamp="2026-09-18T12:10:00.000Z")])
    os.utime(path_a, (live_mtime, live_mtime))
    clock.value = live_mtime  # age 0s -- well inside the live window

    stats2 = watcher.run_once()
    assert_privacy(stats2)
    assert stats2.files_parsed == 0
    assert stats2.files_skipped_live == 1
    # Store still reflects the old (2-turn) content -- never touched.
    daily = {(r["day"], r["model"]): r["turns"] for r in store.daily_usage(days=3650)}
    assert sum(daily.values()) == 2

    # Advance the clock past the live window without touching the file
    # again -- it's re-checked (still "changed" per known_files()) and,
    # now stable, actually re-parsed.
    clock.value = live_mtime + LIVE_FILE_WINDOW_S + 1

    stats3 = watcher.run_once()
    assert stats3.files_parsed == 1
    assert stats3.files_skipped_live == 0
    daily_final = {(r["day"], r["model"]): r["turns"] for r in store.daily_usage(days=3650)}
    assert sum(daily_final.values()) == 3


def test_never_seen_live_file_is_parsed_immediately(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    path_a = _write_session(root, "proj-a", "sess-a1", _two_turns(), age_s=0.0)

    now = time.time()
    clock = _FakeClock(now)
    options = _options(tmp_path)
    watcher = FileWatcher(store, options, now=clock)

    stats = watcher.run_once()
    assert stats.files_parsed == 1  # parsed despite being live, since never seen before
    assert stats.files_skipped_live == 0
    assert str(path_a) in watcher._pending_stabilize


# -- removed-file cleanup ----------------------------------------------------


def test_removed_file_cleanup(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    path_a = _write_session(root, "proj-a", "sess-a1", _two_turns())
    _write_session(root, "proj-b", "sess-b1", _two_turns())

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    watcher.run_once()
    assert store.summary()["transcripts"] == 2

    path_a.unlink()
    stats = watcher.run_once()
    assert_privacy(stats)
    assert stats.files_removed == 1
    assert store.summary()["transcripts"] == 1
    # remove_missing() only ever removes transcripts rows -- a session
    # only ages out via retention_prune(), a separate (unconfigured, in
    # this test) mechanism -- so the orphaned session row itself remains,
    # now with no transcripts.
    session_a = store.session("sess-a1")
    assert session_a is not None
    assert session_a["transcripts"] == []


# -- bad file never raises, never leaks a path ------------------------------


def test_bad_transcript_is_recorded_without_a_path(tmp_path: Path, store: Store, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "projects"
    bad_path = _write_session(root, "proj-a", "sess-bad", _two_turns())
    good_path = _write_session(root, "proj-b", "sess-good", _two_turns())

    import claude_token_lens.service.watcher as watcher_mod

    real_parse = watcher_mod.parse_transcript

    def _boom(path, meta):
        if str(path) == str(bad_path):
            raise ValueError("simulated corrupt transcript")
        return real_parse(path, meta)

    monkeypatch.setattr(watcher_mod, "parse_transcript", _boom)

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    stats = watcher.run_once()

    assert stats.errors == 1
    assert len(stats.error_messages) == 1
    message = stats.error_messages[0]
    assert str(bad_path) not in message
    assert str(root) not in message
    assert "proj-a" not in message
    assert "sess-bad" not in message
    assert_privacy(stats)

    # The good session still got parsed and folded despite the other one
    # blowing up.
    assert store.session("sess-good") is not None
    assert store.session("sess-bad") is None


# -- snapshot ingestion -------------------------------------------------------


def _write_snapshot(config_dir: Path, ts: str, schema: int = 1) -> Path:
    snapshots_dir = config_dir / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    path = snapshots_dir / f"{ts}.json"
    data = {
        "schema": schema,
        "ts": ts,
        "session_id": "sess-x",
        "cwd_hash": "sha256:deadbeef",
        "user_settings": {"model": "claude-sonnet-5"},
        "managed_settings": {},
        "managed_keys": [],
        "project_settings": {},
        "mcp_servers": {"names": [], "enabled_mcpjson_servers": [], "disabled_mcpjson_servers": []},
        "enabled_plugins": [],
        "agents": {},
        "env_names": [],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_snapshot_ingestion_is_deduped_across_ticks(tmp_path: Path, store: Store):
    options = _options(tmp_path)
    _write_snapshot(options.config_dir, "20260918T120000Z", schema=1)
    _write_snapshot(options.config_dir, "20260918T130000Z", schema=2)

    watcher = FileWatcher(store, options)
    watcher.run_once()

    rows = store.snapshots()
    assert_privacy(rows)
    assert len(rows) == 2
    schema_versions = sorted(r["schema_version"] for r in rows)
    assert schema_versions == [1, 2]
    # Snapshots carry no project slug of their own -- the watcher files
    # them under the global/machine-wide attribution, which Store.snapshots()
    # reports honestly as a null project_slug (deliverable 1.c).
    assert all(r["project_slug"] is None for r in rows)

    # A second tick over the same two files must not duplicate them.
    watcher.run_once()
    assert len(store.snapshots()) == 2


# -- billing_mode wiring (deliverable 1.a) -----------------------------------


def test_billing_mode_is_stamped_onto_sessions_from_options(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())

    options = _options(tmp_path, billing_mode="subscription")
    watcher = FileWatcher(store, options)
    watcher.run_once()

    row = store._connection().execute(
        "SELECT billing_mode FROM sessions WHERE id = ?", ("sess-a1",)
    ).fetchone()
    assert row["billing_mode"] == "subscription"


def test_billing_mode_defaults_to_api(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    watcher.run_once()

    row = store._connection().execute(
        "SELECT billing_mode FROM sessions WHERE id = ?", ("sess-a1",)
    ).fetchone()
    assert row["billing_mode"] == "api"


# -- last_stats (deliverable 1.e) ---------------------------------------------


def test_last_stats_is_none_before_run_once_and_set_after(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    assert watcher.last_stats is None

    stats = watcher.run_once()
    assert watcher.last_stats is stats
    assert watcher.last_stats.files_parsed == 1

    stats2 = watcher.run_once()
    assert watcher.last_stats is stats2
    assert watcher.last_stats is not stats


# -- workflow run persistence (deliverable 1.d) -------------------------------


def _write_workflow(
    root: Path,
    slug: str,
    session_id: str,
    run_id: str,
    body: dict,
    agent_lines: list[dict] | None = None,
) -> Path:
    workflows_dir = root / slug / session_id / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    run_path = workflows_dir / f"{run_id}.json"
    run_path.write_text(json.dumps(body), encoding="utf-8")
    mtime = _backdated(_STABLE_AGE_S)
    os.utime(run_path, (mtime, mtime))
    if agent_lines is not None:
        agent_dir = root / slug / session_id / "subagents" / "workflows" / run_id
        agent_dir.mkdir(parents=True, exist_ok=True)
        agent_path = agent_dir / "agent-1.jsonl"
        write_jsonl(agent_path, agent_lines)
        (agent_dir / "agent-1.meta.json").write_text(
            json.dumps({"agentType": "claude-implementer"}), encoding="utf-8"
        )
        os.utime(agent_path, (mtime, mtime))
        os.utime(agent_dir / "agent-1.meta.json", (mtime, mtime))
    return run_path


def test_workflow_run_is_parsed_and_persisted(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())
    _write_workflow(
        root,
        "proj-a",
        "sess-a1",
        "wf_test-000",
        {
            "runId": "wf_test-000",
            "timestamp": "2026-09-18T12:00:00.000Z",
            "agentCount": 1,
            "phases": [{"title": "Only phase", "detail": "never read"}],
            "status": "completed",
        },
        agent_lines=[turn_line(timestamp="2026-09-18T12:00:10.000Z", input_tokens=50, output_tokens=5)],
    )

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    stats = watcher.run_once()
    assert_privacy(stats)
    assert stats.errors == 0

    row = store._connection().execute(
        "SELECT session_id, run_id, agent_count, phases, started, status FROM workflow_runs"
    ).fetchone()
    assert row is not None
    assert row["session_id"] == "sess-a1"
    assert row["run_id"] == "wf_test-000"
    assert row["agent_count"] == 1
    assert json.loads(row["phases"]) == ["Only phase"]
    assert row["started"] == "2026-09-18T12:00:00.000Z"
    assert row["status"] == "completed"


def test_malformed_workflow_file_never_raises_and_still_upserts(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())
    # Genuinely invalid JSON -- parse_workflow_file's own documented
    # tolerant-parsing posture (never raises) is what's under test here,
    # not the watcher's own try/except.
    workflows_dir = root / "proj-a" / "sess-a1" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    bad_path = workflows_dir / "wf_bad-000.json"
    bad_path.write_text("{not valid json", encoding="utf-8")
    mtime = _backdated(_STABLE_AGE_S)
    os.utime(bad_path, (mtime, mtime))

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    stats = watcher.run_once()
    assert_privacy(stats)
    assert stats.errors == 0

    row = store._connection().execute(
        "SELECT run_id, agent_count, phases, status FROM workflow_runs"
    ).fetchone()
    assert row is not None
    assert row["run_id"] == "wf_bad-000"  # falls back to the filename stem
    assert row["agent_count"] == 0
    assert json.loads(row["phases"]) == []
    assert row["status"] is None


def test_workflow_run_is_deduped_across_ticks(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())
    _write_workflow(
        root,
        "proj-a",
        "sess-a1",
        "wf_test-001",
        {"runId": "wf_test-001", "agentCount": 0, "phases": [], "status": "completed"},
    )

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    watcher.run_once()
    watcher.run_once()

    count = store._connection().execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
    assert count == 1


# -- start()/stop() lifecycle -------------------------------------------------


def test_start_twice_starts_only_one_thread(tmp_path: Path):
    db_path = tmp_path / "store.db"
    store_obj = Store(str(db_path))
    store_obj.open()

    options = _options(tmp_path, poll_interval_s=0.05)
    watcher = FileWatcher(store_obj, options)

    before = {t.ident for t in threading.enumerate()}
    watcher.start()
    first_thread = watcher._thread
    watcher.start()  # idempotent
    second_thread = watcher._thread

    assert first_thread is second_thread
    watcher_threads = [t for t in threading.enumerate() if t.name == "claude-token-lens-watcher"]
    assert len(watcher_threads) == 1

    watcher.stop()
    assert watcher._thread is None
    after = {t.ident for t in threading.enumerate()}
    assert after == before

    store_obj.close()


__all__: list[str] = []
