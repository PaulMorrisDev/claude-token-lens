"""Round-trip and privacy tests for ``service.store.Store`` against a
synthetic corpus (two sessions, a top-level + a subagent transcript
each, turns_agg/recache_turns/compactions/events rows, a snapshot, a
profile and a baseline).

The path-leak guard (``test_no_local_path_leaks_from_any_read_query``)
is the sharpest test in this file: it upserts a transcript whose
``path`` is a deliberately distinctive, real-looking Windows path, then
walks the JSON-serialised output of every read query and asserts that
exact string never appears anywhere in it — the concrete regression
``service/schema.py``'s and ``service/__init__.py``'s privacy-rule
docstrings warn against.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from claude_token_lens.service.store import Store
from helpers import assert_privacy

#: A deliberately distinctive fake local path -- if this string (or the
#: username segment alone) ever surfaces in a read-query result, the
#: store has leaked a local filesystem path into API-facing data.
_FAKE_PATH = r"C:\Users\definitely-not-a-real-person\.claude\projects\proj-a\session-a.jsonl"
_FAKE_SUB_PATH = r"C:\Users\definitely-not-a-real-person\.claude\projects\proj-a\session-a\subagents\agent-1.jsonl"
_FAKE_ROOT = r"C:\Users\definitely-not-a-real-person\.claude\projects\proj-a"
_FAKE_PROFILE_PATH = r"C:\Users\definitely-not-a-real-person\.claude\token-lens\profiles\p1.toml"


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    s.open()
    return s


def _seed(store: Store) -> None:
    snapshot_id = store.upsert_snapshot(
        project_slug="proj-a",
        project_root_path=_FAKE_ROOT,
        ts="2026-09-18T12:00:00Z",
        schema_version=2,
        digest_json=json.dumps({"agents": {"claude-implementer": True}}),
    )
    store.upsert_session(
        session_id="session-a",
        project_slug="proj-a",
        project_root_path=_FAKE_ROOT,
        slug="proj-a",
        first_ts="2026-09-18T12:00:00Z",
        last_ts="2026-09-18T13:00:00Z",
        span_s=3600.0,
        archetype="plan-high-implement-low",
        mode="agentic",
        mode_source="tool-signature",
        purpose="refactor",
        purpose_source="intent-signature",
        entrypoint="cli",
        billing_mode="subscription",
        snapshot_id=snapshot_id,
        profile_id="p1",
        total_cost=1.23,
        total_tokens=45000,
    )
    store.upsert_transcript(
        session_id="session-a",
        path=_FAKE_PATH,
        kind="top-level",
        agent_id=None,
        agent_type=None,
        spawn_depth=0,
        parent_agent_id=None,
        mtime_ns=123,
        size_bytes=456,
        parser_version=3,
        digest_json=json.dumps({"turns": 10}),
        turns_agg=[
            {
                "day": "2026-09-18",
                "model": "claude-sonnet-5",
                "turns": 10,
                "input_tokens": 1000,
                "cache_creation_tokens": 500,
                "cache_read_tokens": 2000,
                "output_tokens": 300,
                "thinking_tokens": 50,
                "cc_5m": 0,
                "cc_1h": 500,
                "cost": 1.0,
            }
        ],
        recache_turns=[
            {
                "turn_index": 3,
                "signature": "full-expiry",
                "cache_creation_tokens": 500,
                "preceding_primary": "HUMAN_TEXT",
                "gap_s": 400.0,
            }
        ],
        events=[{"kind": "compact_boundary", "subkind": None, "count": 1, "dropped_tokens_sum": 0, "duration_ms_sum": 0}],
        compactions=[
            {
                "ts": "2026-09-18T12:30:00Z",
                "pre_tokens": 180000,
                "post_tokens": 40000,
                "dropped_tokens": 140000,
                "trigger": "auto",
                "join_delta_s": 5.0,
            }
        ],
    )
    store.upsert_transcript(
        session_id="session-a",
        path=_FAKE_SUB_PATH,
        kind="subagent",
        agent_id="agent-1",
        agent_type="claude-implementer",
        spawn_depth=1,
        parent_agent_id=None,
        mtime_ns=789,
        size_bytes=1011,
        parser_version=3,
        digest_json=json.dumps({"turns": 5}),
        turns_agg=[
            {
                "day": "2026-09-18",
                "model": "claude-sonnet-5",
                "turns": 5,
                "input_tokens": 200,
                "cache_creation_tokens": 100,
                "cache_read_tokens": 400,
                "output_tokens": 60,
                "thinking_tokens": 0,
                "cc_5m": 100,
                "cc_1h": 0,
                "cost": 0.23,
            }
        ],
    )
    store.upsert_profile(profile_id="p1", name="implementation-heavy", toml_path=_FAKE_PROFILE_PATH)
    store.record_baseline(
        project_slug="proj-a",
        project_root_path=_FAKE_ROOT,
        window_start="2026-09-11T00:00:00Z",
        window_end="2026-09-18T00:00:00Z",
        archetype="plan-high-implement-low",
        digest_json=json.dumps({"sessions": 12}),
    )
    store.set_tag("session-a", "purpose", "refactor-override")


# -- migrate / schema --------------------------------------------------

#: The exact v0.2.0 (schema version 4) DDL, taken verbatim from
#: ``git show v0.2.0:src/claude_token_lens/service/schema.py`` --
#: ``profiles``/``baselines`` are one version *before* v5's
#: ``content_hash``/``record_id`` columns. Used only by
#: :func:`test_migrate_upgrades_a_v4_store_without_losing_rows` (review
#: B2) to build a store shaped exactly like a real upgrade would find
#: one, without depending on git tag history being available at test
#: time.
_V4_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        id         INTEGER PRIMARY KEY,
        slug       TEXT NOT NULL UNIQUE,
        root_path  TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen  TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        id             INTEGER PRIMARY KEY,
        project_id     INTEGER REFERENCES projects(id),
        ts             TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        digest_json    TEXT NOT NULL,
        UNIQUE (project_id, ts, schema_version)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id              TEXT PRIMARY KEY,
        project_id      INTEGER NOT NULL REFERENCES projects(id),
        slug            TEXT NOT NULL,
        first_ts        TEXT,
        last_ts         TEXT,
        span_s          REAL NOT NULL DEFAULT 0,
        archetype       TEXT,
        mode            TEXT,
        mode_source     TEXT,
        purpose         TEXT,
        purpose_source  TEXT,
        entrypoint      TEXT,
        billing_mode    TEXT,
        snapshot_id     INTEGER REFERENCES snapshots(id),
        profile_id      TEXT,
        total_cost      REAL NOT NULL DEFAULT 0,
        total_tokens    INTEGER NOT NULL DEFAULT 0,
        updated_at      TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_slug_first_ts ON sessions(slug, first_ts);
    """,
    """
    CREATE TABLE IF NOT EXISTS transcripts (
        id              INTEGER PRIMARY KEY,
        session_id      TEXT NOT NULL REFERENCES sessions(id),
        path            TEXT NOT NULL UNIQUE,
        kind            TEXT NOT NULL,
        agent_id        TEXT,
        agent_type      TEXT,
        spawn_depth     INTEGER NOT NULL DEFAULT 0,
        parent_agent_id TEXT,
        mtime_ns        INTEGER NOT NULL,
        size_bytes      INTEGER NOT NULL,
        parser_version  INTEGER NOT NULL,
        digest_blob     BLOB NOT NULL,
        missing_since   TEXT,
        updated_at      TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_transcripts_session_id ON transcripts(session_id);
    """,
    """
    CREATE TABLE IF NOT EXISTS turns_agg (
        id                     INTEGER PRIMARY KEY,
        transcript_id          INTEGER NOT NULL REFERENCES transcripts(id),
        day                    TEXT NOT NULL,
        model                  TEXT NOT NULL,
        turns                  INTEGER NOT NULL DEFAULT 0,
        input_tokens           INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens  INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens      INTEGER NOT NULL DEFAULT 0,
        output_tokens          INTEGER NOT NULL DEFAULT 0,
        thinking_tokens        INTEGER NOT NULL DEFAULT 0,
        cc_5m                  INTEGER NOT NULL DEFAULT 0,
        cc_1h                  INTEGER NOT NULL DEFAULT 0,
        cost                   REAL NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_turns_agg_day ON turns_agg(day);
    """,
    """
    CREATE TABLE IF NOT EXISTS recache_turns (
        id                     INTEGER PRIMARY KEY,
        transcript_id          INTEGER NOT NULL REFERENCES transcripts(id),
        turn_index             INTEGER NOT NULL,
        signature              TEXT NOT NULL,
        cache_creation_tokens  INTEGER NOT NULL DEFAULT 0,
        preceding_primary      TEXT,
        gap_s                  REAL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS events_agg (
        id                 INTEGER PRIMARY KEY,
        transcript_id      INTEGER NOT NULL REFERENCES transcripts(id),
        kind               TEXT NOT NULL,
        subkind            TEXT,
        count              INTEGER NOT NULL DEFAULT 0,
        dropped_tokens_sum INTEGER NOT NULL DEFAULT 0,
        duration_ms_sum    INTEGER NOT NULL DEFAULT 0,
        UNIQUE (transcript_id, kind, subkind)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS compactions (
        id             INTEGER PRIMARY KEY,
        transcript_id  INTEGER NOT NULL REFERENCES transcripts(id),
        ts             TEXT NOT NULL,
        pre_tokens     INTEGER,
        post_tokens    INTEGER,
        dropped_tokens INTEGER,
        trigger        TEXT,
        join_delta_s   REAL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS session_tags (
        session_id TEXT NOT NULL REFERENCES sessions(id),
        key        TEXT NOT NULL,
        value      TEXT NOT NULL,
        set_at     TEXT NOT NULL,
        PRIMARY KEY (session_id, key)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS profiles (
        id         TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        toml_path  TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS baselines (
        id           INTEGER PRIMARY KEY,
        project_id   INTEGER REFERENCES projects(id),
        window_start TEXT NOT NULL,
        window_end   TEXT NOT NULL,
        archetype    TEXT,
        digest_json  TEXT NOT NULL,
        created_at   TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_runs (
        id            INTEGER PRIMARY KEY,
        session_id    TEXT NOT NULL REFERENCES sessions(id),
        run_id        TEXT NOT NULL,
        agent_count   INTEGER NOT NULL DEFAULT 0,
        phases        TEXT NOT NULL DEFAULT '[]',
        started       TEXT,
        finished      TEXT,
        cost          REAL NOT NULL DEFAULT 0,
        status        TEXT,
        updated_at    TEXT NOT NULL,
        UNIQUE (session_id, run_id)
    );
    CREATE INDEX IF NOT EXISTS idx_workflow_runs_session_id ON workflow_runs(session_id);
    """,
    """
    CREATE TABLE IF NOT EXISTS usage_log (
        id              INTEGER PRIMARY KEY,
        ts              TEXT NOT NULL,
        window_start    TEXT,
        window_end      TEXT,
        utilization_pct REAL,
        raw_json        TEXT NOT NULL
    );
    """,
)


def _build_v4_store(path: str) -> None:
    """Create a SQLite file at ``path`` shaped exactly like a v0.2.0
    store (schema version 4), with one row in every table."""
    conn = sqlite3.connect(path)
    try:
        for statement in _V4_STATEMENTS:
            conn.executescript(statement)
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '4')")
        conn.execute(
            "INSERT INTO projects (id, slug, root_path, first_seen, last_seen) "
            "VALUES (1, 'proj-a', '/root/proj-a', 't', 't')"
        )
        conn.execute(
            "INSERT INTO snapshots (id, project_id, ts, schema_version, digest_json) "
            "VALUES (1, 1, 't', 1, '{}')"
        )
        conn.execute(
            "INSERT INTO sessions (id, project_id, slug, updated_at) "
            "VALUES ('session-a', 1, 'proj-a', 't')"
        )
        conn.execute(
            "INSERT INTO transcripts "
            "(id, session_id, path, kind, mtime_ns, size_bytes, parser_version, digest_blob, updated_at) "
            "VALUES (1, 'session-a', '/root/proj-a/session-a.jsonl', 'top', 1, 1, 1, x'', 't')"
        )
        conn.execute(
            "INSERT INTO turns_agg (id, transcript_id, day, model) VALUES (1, 1, '2026-01-01', 'm')"
        )
        conn.execute(
            "INSERT INTO recache_turns (id, transcript_id, turn_index, signature) "
            "VALUES (1, 1, 0, 'sig')"
        )
        conn.execute("INSERT INTO events_agg (id, transcript_id, kind) VALUES (1, 1, 'k')")
        conn.execute("INSERT INTO compactions (id, transcript_id, ts) VALUES (1, 1, 't')")
        conn.execute(
            "INSERT INTO session_tags (session_id, key, value, set_at) "
            "VALUES ('session-a', 'mode', 'agentic', 't')"
        )
        conn.execute(
            "INSERT INTO profiles (id, name, toml_path, updated_at) VALUES ('p1', 'P1', '/x/p1.toml', 't')"
        )
        conn.execute(
            "INSERT INTO baselines (id, project_id, window_start, window_end, digest_json, created_at) "
            "VALUES (1, 1, 't', 't', '{}', 't')"
        )
        conn.execute(
            "INSERT INTO workflow_runs (id, session_id, run_id, updated_at) "
            "VALUES (1, 'session-a', 'wf-1', 't')"
        )
        conn.execute(
            "INSERT INTO usage_log (id, ts, raw_json) VALUES (1, 't', '{}')"
        )
        conn.commit()
    finally:
        conn.close()


def test_migrate_upgrades_a_v4_store_without_losing_rows(tmp_path) -> None:
    """Review B2: opening a v0.2.0 (schema version 4) store under the
    current code must migrate additively, not drop every table. Every
    row inserted under the old schema must still be there afterwards,
    and the two new v5 columns must exist."""
    from claude_token_lens.service import schema

    db_path = tmp_path / "v4.db"
    _build_v4_store(str(db_path))

    store = Store(str(db_path))
    store.open()
    try:
        assert store.schema_version() == schema.SCHEMA_VERSION

        conn = store._connection()
        for table in (
            "projects",
            "snapshots",
            "sessions",
            "transcripts",
            "turns_agg",
            "recache_turns",
            "events_agg",
            "compactions",
            "session_tags",
            "profiles",
            "baselines",
            "workflow_runs",
            "usage_log",
        ):
            count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            assert count == 1, f"{table} lost its row(s) across the v4 -> v5 migration"

        profile_columns = {row["name"] for row in conn.execute("PRAGMA table_info(profiles)")}
        assert "content_hash" in profile_columns
        baseline_columns = {row["name"] for row in conn.execute("PRAGMA table_info(baselines)")}
        assert {"record_id", "content_hash"} <= baseline_columns

        # The migrated row's new columns take the documented default,
        # never NULL/missing.
        profile_row = conn.execute("SELECT content_hash FROM profiles WHERE id = 'p1'").fetchone()
        assert profile_row["content_hash"] == ""
        baseline_row = conn.execute("SELECT record_id, content_hash FROM baselines WHERE id = 1").fetchone()
        assert baseline_row["record_id"] is None
        assert baseline_row["content_hash"] == ""

        # The record_id uniqueness that CREATE_BASELINES declares
        # directly for a fresh table is present via the ladder's index
        # too -- a second NULL is fine (SQLite never treats NULLs as
        # conflicting), but a duplicate non-NULL value is rejected.
        conn.execute(
            "INSERT INTO baselines (id, project_id, window_start, window_end, digest_json, "
            "record_id, content_hash, created_at) VALUES (2, 1, 't', 't', '{}', 'rid-1', '', 't')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO baselines (id, project_id, window_start, window_end, digest_json, "
                "record_id, content_hash, created_at) VALUES (3, 1, 't', 't', '{}', 'rid-1', '', 't')"
            )
    finally:
        store.close()


def test_migrate_backs_up_and_rebuilds_a_newer_than_code_store(tmp_path) -> None:
    """Review B2: a recorded schema_version newer than the running
    code's own is the one case (besides "no ladder step") a migration
    genuinely can't serve -- but the old file must be copied aside
    first, never just silently dropped."""
    from claude_token_lens.service import schema

    db_path = tmp_path / "newer.db"
    store = Store(str(db_path))
    store.open()
    _seed(store)
    assert store.summary()["sessions"] == 1
    store.close()

    newer_version = schema.SCHEMA_VERSION + 1
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(newer_version),),
    )
    conn.commit()
    conn.close()

    reopened = Store(str(db_path))
    reopened.open()
    try:
        assert reopened.schema_version() == schema.SCHEMA_VERSION
        assert reopened.summary()["sessions"] == 0

        backup_path = db_path.with_name(db_path.name + f".bak-{newer_version}")
        assert backup_path.exists(), "no backup was made before the newer-than-code store was rebuilt"
    finally:
        reopened.close()


def test_migrate_is_idempotent(store: Store) -> None:
    from claude_token_lens.service import schema

    assert store.schema_version() == schema.SCHEMA_VERSION
    store.migrate()
    store.migrate()
    assert store.schema_version() == schema.SCHEMA_VERSION


def test_migrate_drops_and_rebuilds_a_stale_store(store: Store) -> None:
    """A store whose recorded schema_version is older than the running
    code's is dropped and recreated from scratch on the next open() --
    the store is a derived cache, so this is safe, and the next watcher
    tick repopulates it (S1-integration fix 1.b)."""
    from claude_token_lens.service import schema

    _seed(store)
    assert store.summary()["sessions"] == 1

    conn = store._connection()
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', '0') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    assert store.schema_version() == 0

    store.migrate()

    assert store.schema_version() == schema.SCHEMA_VERSION
    # The old session/transcript rows are gone -- a fresh, empty store.
    assert store.summary()["sessions"] == 0
    assert store.summary()["transcripts"] == 0
    assert store.known_files() == {}


# -- writer round trips --------------------------------------------------


def test_upsert_session_round_trips(store: Store) -> None:
    _seed(store)
    result = store.session("session-a")
    assert result is not None
    assert result["archetype"] == "plan-high-implement-low"
    assert result["total_cost"] == pytest.approx(1.23)
    assert result["total_tokens"] == 45000
    assert result["profile_id"] == "p1"


def test_upsert_transcript_is_idempotent_on_path(store: Store) -> None:
    _seed(store)
    # Re-upserting the same path (a re-parse after the file changed)
    # must update in place, not create a second transcript row.
    store.upsert_transcript(
        session_id="session-a",
        path=_FAKE_PATH,
        kind="top-level",
        mtime_ns=999,
        size_bytes=999,
        parser_version=3,
        digest_json=json.dumps({"turns": 11}),
    )
    detail = store.session("session-a")
    assert len(detail["transcripts"]) == 2  # top-level + subagent, not 3


def test_upsert_transcript_replaces_child_rows_wholesale(store: Store) -> None:
    _seed(store)
    # A re-parse with a different recache_turns set must replace, not
    # accumulate alongside, the previous set.
    store.upsert_transcript(
        session_id="session-a",
        path=_FAKE_PATH,
        kind="top-level",
        digest_json=json.dumps({"turns": 10}),
        recache_turns=[
            {"turn_index": 7, "signature": "prefix-invalidated", "cache_creation_tokens": 10},
        ],
    )
    recache = store.recache()
    assert "full-expiry" not in recache["by_signature"]
    assert recache["by_signature"]["prefix-invalidated"]["turns"] == 1


def test_known_files_reports_every_transcript(store: Store) -> None:
    _seed(store)
    files = store.known_files()
    assert files[_FAKE_PATH] == (123, 456)
    assert files[_FAKE_SUB_PATH] == (789, 1011)


def test_remove_missing_marks_transcripts_not_in_known_set(store: Store) -> None:
    # Review finding 3: the store must outlive `cleanupPeriodDays` --
    # `remove_missing` only marks a vanished transcript's `missing_since`,
    # it never deletes the row. Only `retention_prune`/`--purge` do that.
    _seed(store)
    newly_missing = store.remove_missing({_FAKE_PATH})  # subagent path dropped
    assert newly_missing == 1
    assert store.count_missing_transcripts() == 1
    detail = store.session("session-a")
    # Both transcripts are still present -- a reader must include a
    # missing-but-not-yet-pruned transcript by default.
    assert len(detail["transcripts"]) == 2
    kinds = {trow["kind"] for trow in detail["transcripts"]}
    assert kinds == {"top-level", "subagent"}
    # Calling it again with the same known set is a no-op: already-missing
    # rows don't get re-marked or double-counted.
    assert store.remove_missing({_FAKE_PATH}) == 0
    assert store.count_missing_transcripts() == 1


def test_missing_transcript_survives_until_retention_prune_deletes_it(store: Store) -> None:
    """Regression test for review finding 3 (blocking): the store must
    outlive Claude Code's own ``cleanupPeriodDays`` retention. A
    transcript whose file has vanished is marked (``missing_since``), not
    deleted -- it keeps serving reports/rebuild regardless of how long
    ago it went missing, until its *session* actually ages past
    ``--retention-days``/``--purge``. Fails against a pre-fix
    ``remove_missing`` that deleted the row outright.
    """
    _seed(store)
    store.remove_missing({_FAKE_PATH})  # subagent path dropped -> marked missing
    assert store.count_missing_transcripts() == 1
    assert store.session("session-a") is not None

    # A generous retention window leaves a recently active session
    # (missing transcript or not) untouched.
    removed = store.retention_prune(retention_days=3650)
    assert removed == 0
    assert store.session("session-a") is not None
    assert store.count_missing_transcripts() == 1

    # Only once the session itself ages past the retention window does
    # the row -- and its missing transcript -- actually get deleted.
    removed = store.retention_prune(retention_days=0)
    assert removed == 1
    assert store.session("session-a") is None
    assert store.count_missing_transcripts() == 0


def test_retention_prune_removes_old_sessions(store: Store) -> None:
    _seed(store)
    store.upsert_session(
        session_id="session-old",
        project_slug="proj-a",
        slug="proj-a",
        first_ts="2000-01-01T00:00:00Z",
        last_ts="2000-01-01T01:00:00Z",
    )
    removed = store.retention_prune(retention_days=30)
    assert removed == 1
    assert store.session("session-old") is None
    assert store.session("session-a") is not None


# -- read queries --------------------------------------------------------


def test_summary_totals(store: Store) -> None:
    _seed(store)
    summary = store.summary()
    assert summary["sessions"] == 1
    assert summary["transcripts"] == 2
    assert summary["total_cost"] == pytest.approx(1.23)
    assert summary["total_tokens"] == 45000


def test_sessions_listing_has_no_transcripts_key(store: Store) -> None:
    _seed(store)
    rows = store.sessions()
    assert len(rows) == 1
    assert rows[0]["id"] == "session-a"
    assert "transcripts" not in rows[0]


def test_daily_usage_aggregates_across_transcripts(store: Store) -> None:
    _seed(store)
    rows = store.daily_usage(days=30)
    assert len(rows) == 1
    row = rows[0]
    assert row["day"] == "2026-09-18"
    assert row["turns"] == 15  # 10 top-level + 5 subagent
    assert row["input_tokens"] == 1200


def test_compactions_listing(store: Store) -> None:
    _seed(store)
    rows = store.compactions()
    assert len(rows) == 1
    assert rows[0]["dropped_tokens"] == 140000


def test_snapshots_listing(store: Store) -> None:
    _seed(store)
    rows = store.snapshots()
    assert len(rows) == 1
    assert rows[0]["schema_version"] == 2
    assert rows[0]["project_slug"] == "proj-a"


def test_snapshots_reports_global_attribution_as_null_project_slug(store: Store) -> None:
    """A snapshot attributed to Store.GLOBAL_PROJECT_SLUG (the watcher's
    synthetic attribution for a machine-wide capture with no real
    per-project identity) is exposed honestly as project_slug=None, never
    as the internal sentinel string (S1-integration fix 1.c)."""
    from claude_token_lens.service.store import GLOBAL_PROJECT_SLUG

    assert GLOBAL_PROJECT_SLUG == "__global__"
    store.upsert_snapshot(
        project_slug=GLOBAL_PROJECT_SLUG,
        ts="2026-09-19T00:00:00Z",
        schema_version=2,
        digest_json=json.dumps({}),
    )
    rows = store.snapshots()
    assert len(rows) == 1
    assert rows[0]["project_slug"] is None


def test_upsert_snapshot_dedupes_by_natural_key(store: Store) -> None:
    """Re-ingesting the same (project, ts, schema_version) snapshot
    updates the existing row instead of creating a duplicate -- the
    ON CONFLICT dedupe that lets the watcher drop its own pre-check
    workaround (S1-integration fix 1.b)."""
    first_id = store.upsert_snapshot(
        project_slug="proj-a",
        ts="2026-09-18T12:00:00Z",
        schema_version=2,
        digest_json=json.dumps({"a": 1}),
    )
    second_id = store.upsert_snapshot(
        project_slug="proj-a",
        ts="2026-09-18T12:00:00Z",
        schema_version=2,
        digest_json=json.dumps({"a": 2}),
    )
    assert first_id == second_id
    rows = store.snapshots()
    assert len(rows) == 1
    assert json.loads(rows[0]["digest_json"]) == {"a": 2}

    # A different schema_version for the same (project, ts) is a distinct
    # natural key -- a second row, not an update of the first.
    store.upsert_snapshot(
        project_slug="proj-a",
        ts="2026-09-18T12:00:00Z",
        schema_version=1,
        digest_json=json.dumps({"a": 1}),
    )
    assert len(store.snapshots()) == 2


def test_profiles_and_baselines_listing(store: Store) -> None:
    _seed(store)
    profiles = store.profiles()
    assert profiles == [{"id": "p1", "name": "implementation-heavy", "updated_at": profiles[0]["updated_at"]}]
    baselines = store.baselines()
    assert len(baselines) == 1
    assert baselines[0]["archetype"] == "plan-high-implement-low"


def test_tags_round_trip(store: Store) -> None:
    _seed(store)
    assert store.tags("session-a") == {"purpose": "refactor-override"}
    store.set_tag("session-a", "purpose", "docs")
    assert store.tags("session-a") == {"purpose": "docs"}


# -- change_token ------------------------------------------------------


def test_change_token_changes_when_a_transcript_is_added_or_reparsed(store: Store) -> None:
    before = store.change_token()
    _seed(store)
    after_seed = store.change_token()
    assert after_seed != before

    # A brand-new transcript (distinct natural key) changes the row count,
    # which the token always reflects regardless of timestamp resolution.
    store.upsert_transcript(
        session_id="session-a",
        path=_FAKE_PATH + ".extra",
        kind="subagent",
        digest_json=json.dumps({"turns": 999}),
    )
    after_new_transcript = store.change_token()
    assert after_new_transcript != after_seed


def test_change_token_stable_when_nothing_changed(store: Store) -> None:
    _seed(store)
    assert store.change_token() == store.change_token()


# -- turns_for_session ---------------------------------------------------


def test_turns_for_session_returns_none_without_a_top_level_transcript(store: Store) -> None:
    store.upsert_session(session_id="ghost", project_slug="proj-a", slug="proj-a")
    assert store.turns_for_session("ghost") is None
    assert store.turns_for_session("does-not-exist") is None


def test_turns_for_session_builds_series_and_markers(store: Store) -> None:
    from claude_token_lens.cache import encode_result
    from claude_token_lens.model import EventKind, Turn, TranscriptMeta, TranscriptResult

    result = TranscriptResult(
        meta=TranscriptMeta(path=_FAKE_PATH, kind="top-level", session_id="session-b"),
        turns=[
            Turn(turn_index=0, ctx=0),  # synthetic -- excluded
            Turn(turn_index=1, ctx=1000, cache_creation_tokens=500, is_recache=False,
                 preceding_primary=EventKind.HUMAN_TEXT, human_prompt_chars=42),
            Turn(turn_index=2, ctx=1500, cache_creation_tokens=0, is_recache=True,
                 preceding_primary=EventKind.COMPACT_BOUNDARY),
            Turn(turn_index=3, ctx=2000, cache_creation_tokens=300, is_recache=False,
                 preceding_primary=EventKind.TOOL_RESULT, agent_brief_chars=120),
        ],
    )
    store.upsert_session(session_id="session-b", project_slug="proj-a", slug="proj-a")
    store.upsert_transcript(
        session_id="session-b",
        path=_FAKE_PATH + ".b",
        kind="top-level",
        digest_json=json.dumps(encode_result(result)),
    )

    data = store.turns_for_session("session-b")
    assert data is not None
    assert data["turn_series"] == [
        [1, 1000, 500, False, "human_text"],
        [2, 1500, 0, True, "compact_boundary"],
        [3, 2000, 300, False, "tool_result"],
    ]
    assert data["markers"] == {"compactions": [2], "spawns": [3], "human": [1]}
    assert data["truncated"] is False
    assert_privacy(data)


def test_turns_for_session_downsamples_above_the_point_cap(store: Store) -> None:
    """Regression test for review finding 11 (should-fix): a very long
    session's turn_series must be capped at
    ``Store.MAX_TURN_SERIES_POINTS`` rather than shipping every single
    turn to the browser (the original unbounded list is also what fed
    app.js's ``Math.max.apply`` -- finding 10). Every marker turn must
    still survive the downsampling.
    """
    from claude_token_lens.cache import encode_result
    from claude_token_lens.model import EventKind, Turn, TranscriptMeta, TranscriptResult

    total_turns = Store.MAX_TURN_SERIES_POINTS + 500
    marker_turn_index = total_turns - 1  # deliberately outside any stride sample
    turns = []
    for i in range(1, total_turns + 1):
        is_marker = i == marker_turn_index
        turns.append(
            Turn(
                turn_index=i,
                ctx=i * 10,
                preceding_primary=EventKind.COMPACT_BOUNDARY if is_marker else None,
            )
        )
    result = TranscriptResult(
        meta=TranscriptMeta(path=_FAKE_PATH, kind="top-level", session_id="session-huge"),
        turns=turns,
    )
    store.upsert_session(session_id="session-huge", project_slug="proj-a", slug="proj-a")
    store.upsert_transcript(
        session_id="session-huge",
        path=_FAKE_PATH + ".huge",
        kind="top-level",
        digest_json=json.dumps(encode_result(result)),
    )

    data = store.turns_for_session("session-huge")
    assert data is not None
    assert data["truncated"] is True
    assert len(data["turn_series"]) <= Store.MAX_TURN_SERIES_POINTS
    # markers are always computed from the full turn list, never thinned.
    assert data["markers"]["compactions"] == [marker_turn_index]
    # the marker turn itself must survive into the downsampled series.
    kept_turn_indices = {row[0] for row in data["turn_series"]}
    assert marker_turn_index in kept_turn_indices


# -- privacy guard ---------------------------------------------------------


def test_slug_username_segment_is_redacted_from_every_read_query(store: Store) -> None:
    """Regression test for review finding 6 (should-fix): a project slug
    is derived from Claude Code's own project-directory naming, which
    embeds the caller's OS username -- a ``C:\\Users\\someone\\repo``
    project directory becomes the slug ``"C--Users-someone-repo"``. Every
    read query returning a slug/``project_slug`` must redact that
    username segment to ``"<user>"`` before it leaves the store layer.
    Fails against a pre-fix store that returned the raw slug unchanged.
    """
    raw_slug = "C--Users-someone-repo"
    store.upsert_session(
        session_id="session-user",
        project_slug=raw_slug,
        project_root_path=_FAKE_ROOT,
        slug=raw_slug,
        first_ts="2026-09-18T12:00:00Z",
        last_ts="2026-09-18T13:00:00Z",
    )
    store.upsert_snapshot(
        project_slug=raw_slug,
        project_root_path=_FAKE_ROOT,
        ts="2026-09-18T12:30:00Z",
        schema_version=2,
        digest_json=json.dumps({}),
    )
    store.record_baseline(
        project_slug=raw_slug,
        project_root_path=_FAKE_ROOT,
        window_start="2026-09-11T00:00:00Z",
        window_end="2026-09-18T00:00:00Z",
        archetype="plan-high-implement-low",
        digest_json=json.dumps({"sessions": 1}),
    )

    outputs = {
        "sessions": store.sessions(),
        "session": store.session("session-user"),
        "snapshots": store.snapshots(),
        "baselines": store.baselines(),
    }
    blob = json.dumps(outputs, default=str)
    assert "someone" not in blob, f"raw username segment leaked into a read-query result: {blob}"
    assert raw_slug not in blob, f"unredacted slug leaked into a read-query result: {blob}"
    assert "<user>" in blob, "redact_slug should have substituted the <user> placeholder"


def test_no_local_path_leaks_from_any_read_query(store: Store) -> None:
    _seed(store)

    outputs = {
        "summary": store.summary(),
        "sessions": store.sessions(),
        "session": store.session("session-a"),
        "daily_usage": store.daily_usage(),
        "recache": store.recache(),
        "compactions": store.compactions(),
        "snapshots": store.snapshots(),
        "profiles": store.profiles(),
        "baselines": store.baselines(),
        "tags": store.tags("session-a"),
    }
    blob = json.dumps(outputs, default=str)
    for needle in (_FAKE_PATH, _FAKE_SUB_PATH, _FAKE_ROOT, _FAKE_PROFILE_PATH, "definitely-not-a-real-person"):
        assert needle not in blob, f"{needle!r} leaked into read-query output"

    # known_files() is explicitly local-only -- confirm it DOES carry the
    # path (proving the guard above isn't vacuously passing because no
    # method ever stored the path at all).
    assert _FAKE_PATH in store.known_files()


__all__: list[str] = []
