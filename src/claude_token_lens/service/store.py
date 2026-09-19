"""``Store``: the only code in this codebase that talks to the v0.2
service's SQLite file.

Every write goes through an ``upsert_*`` method (idempotent: re-running
the watcher over an unchanged file must not create a duplicate row) and
every read goes through a named query method (``summary``, ``sessions``,
``session``, ``daily_usage``, ``recache``, ``compactions``, ``snapshots``,
``tags``) that returns plain ``dict``/``list[dict]`` data — never a
``sqlite3.Row``, never a dataclass, never a raw local path (see
``service/schema.py``'s and ``service/__init__.py``'s privacy-rule
docstrings; ``tests/test_service_store.py`` enforces the path part of
that by construction).

One :class:`Store` may be shared across threads (the watcher thread and
the API server's request-handling threads both hold the same instance),
but a ``sqlite3.Connection`` may only be used from the thread that
created it. :class:`Store` works around this with one connection per
thread (``threading.local``), all pointed at the same on-disk file, each
opened in WAL journal mode (``PRAGMA journal_mode=WAL``) so a writer
(the watcher) and readers (API requests) don't block each other.

``migrate()`` is idempotent: every statement in ``schema.ALL_STATEMENTS``
is ``CREATE TABLE IF NOT EXISTS``/``CREATE INDEX IF NOT EXISTS``, so
calling it against an already-migrated database at the current
``schema.SCHEMA_VERSION`` is a no-op beyond recording
``meta['schema_version']`` again. When the store's own recorded
``schema_version`` *differs at all* from the running code's
``schema.SCHEMA_VERSION`` (older or newer), ``migrate()`` drops every table first and
recreates them from scratch (see ``schema.py``'s module docstring) --
the store is always a derived cache over transcripts still on disk,
never the source of truth, and the next watcher tick repopulates it
because ``known_files()`` is empty again. There is still no in-place
``ALTER TABLE`` migration path -- this drop-and-rebuild is the only one.

``GLOBAL_PROJECT_SLUG`` is the synthetic project slug the watcher
attributes a machine-wide config snapshot to when the snapshot itself
carries no per-project identity (``hooks/snapshot-config.py`` writes one
``<config_dir>/snapshots/<ts>.json`` per machine, never one per
project). ``Store.snapshots()`` maps this slug back to a ``None``
``project_slug`` in its own read query, so an API/UI consumer sees an
honest "no project" rather than a fabricated one (S1-integration fix
1.c).
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import schema
from ..cache import result_from_jsonable
from ..discovery import redact_slug
from ..model import EventKind

#: ``meta`` key recording the schema version the store's tables were
#: created under. Compared against ``schema.SCHEMA_VERSION`` by callers
#: that want to detect a stale store (see module docstring).
_SCHEMA_VERSION_KEY = "schema_version"

#: See module docstring's "GLOBAL_PROJECT_SLUG" paragraph.
GLOBAL_PROJECT_SLUG = "__global__"

#: Matches every ``CREATE TABLE IF NOT EXISTS <name>`` statement in
#: ``schema.ALL_STATEMENTS``, so :meth:`Store.migrate` can derive the
#: exact set of tables to drop (in reverse -- child-before-parent --
#: order) from the same single source of truth as table creation,
#: rather than hand-maintaining a second list that could drift.
_CREATE_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextlib.contextmanager
def _transaction(conn: sqlite3.Connection):
    """A real, explicit transaction for a connection opened with
    ``isolation_level=None`` (autocommit mode -- see :meth:`Store.
    _connection`). In that mode ``with conn:`` is a silent no-op: Python's
    ``sqlite3`` module only wraps a ``with`` block in an implicit
    transaction when ``isolation_level`` is *not* None, so every writer
    touching more than one table/statement was previously running with
    no atomicity at all -- a failure partway through left whatever had
    already executed committed (review finding 2/5). This issues an
    explicit ``BEGIN IMMEDIATE`` (taking the write lock up front, rather
    than deferring it to the first write statement and risking a
    SQLITE_BUSY upgrade later) and commits on success or rolls back on
    any exception, re-raising it either way.

    Not used around ``Store.migrate``'s own ``executescript`` calls:
    ``executescript`` issues its own implicit ``COMMIT`` of any pending
    transaction before running, which would silently end this one early
    -- and every statement it runs there is an idempotent ``CREATE TABLE
    IF NOT EXISTS``/``CREATE INDEX IF NOT EXISTS`` anyway, so partial
    application on failure is harmless (the next ``migrate()`` call
    finishes the job).
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


class Store:
    """One SQLite-backed store, rooted at ``path``.

    ``path`` may be ``":memory:"`` for tests; every real (file-backed)
    store additionally gets WAL journal mode so concurrent readers don't
    block the watcher's writes.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._local = threading.local()

    # -- connection lifecycle ------------------------------------------

    def open(self) -> None:
        """Open (or reuse) this thread's connection and ensure the
        schema exists. Safe to call more than once per thread."""
        self._connection()
        self.migrate()

    def close(self) -> None:
        """Close this thread's connection, if one is open. Other
        threads' connections (if any) are unaffected — each thread must
        call ``close()`` itself, typically at thread exit."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
            self._local.conn = conn
        return conn

    def _table_names_in_creation_order(self) -> list[str]:
        names: list[str] = []
        for statement in schema.ALL_STATEMENTS:
            names.extend(_CREATE_TABLE_RE.findall(statement))
        return names

    def _drop_all_tables(self, conn: sqlite3.Connection) -> None:
        """Drop every table this schema creates, child-before-parent (the
        reverse of ``schema.ALL_STATEMENTS``'s own dependency order), so
        a foreign key never blocks a drop. Used only when the store's
        recorded schema version is older than the running code's (see
        :meth:`migrate`)."""
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for table in reversed(self._table_names_in_creation_order()):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    def migrate(self) -> None:
        """Create every table/index in ``schema.ALL_STATEMENTS`` if
        missing, and record ``schema.SCHEMA_VERSION`` in ``meta``.
        Idempotent when the store is already current. When the store's
        recorded version differs at all from ``schema.SCHEMA_VERSION`` --
        older (an upgrade) or newer (e.g. a downgraded install pointed at
        a store a later version already migrated) -- every table is
        dropped and recreated first (see module docstring) -- the store
        is a derived cache, never the source of truth, so there is
        nothing to preserve either way (nit 24: the original ``<``-only
        check left a newer-than-code store's stale shape in place
        instead of rebuilding it)."""
        conn = self._connection()
        current = self.schema_version()
        if current is not None and current != schema.SCHEMA_VERSION:
            self._drop_all_tables(conn)
        with conn:
            for statement in schema.ALL_STATEMENTS:
                conn.executescript(statement)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_SCHEMA_VERSION_KEY, str(schema.SCHEMA_VERSION)),
            )

    def schema_version(self) -> int | None:
        """The schema version recorded in ``meta``, or ``None`` if this
        store has never been migrated (including the very first call
        ever made against a brand new database, before ``meta`` itself
        exists)."""
        try:
            row = self._connection().execute(
                "SELECT value FROM meta WHERE key = ?", (_SCHEMA_VERSION_KEY,)
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return int(row["value"]) if row is not None else None

    # -- writers ---------------------------------------------------------

    def _upsert_project(self, conn: sqlite3.Connection, slug: str, root_path: str) -> int:
        now = _now()
        conn.execute(
            "INSERT INTO projects (slug, root_path, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET root_path = excluded.root_path, last_seen = excluded.last_seen",
            (slug, root_path, now, now),
        )
        row = conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
        return int(row["id"])

    def upsert_session(
        self,
        *,
        session_id: str,
        project_slug: str,
        project_root_path: str = "",
        slug: str = "",
        first_ts: str | None = None,
        last_ts: str | None = None,
        span_s: float = 0.0,
        archetype: str | None = None,
        mode: str | None = None,
        mode_source: str | None = None,
        purpose: str | None = None,
        purpose_source: str | None = None,
        entrypoint: str | None = None,
        billing_mode: str | None = None,
        snapshot_id: int | None = None,
        profile_id: str | None = None,
        total_cost: float = 0.0,
        total_tokens: int = 0,
    ) -> None:
        """Insert or update one top-level session row (``SessionRecord``
        plus the cost/token totals folded from its transcripts)."""
        conn = self._connection()
        with _transaction(conn):
            project_id = self._upsert_project(conn, project_slug, project_root_path)
            conn.execute(
                """
                INSERT INTO sessions (
                    id, project_id, slug, first_ts, last_ts, span_s, archetype,
                    mode, mode_source, purpose, purpose_source, entrypoint,
                    billing_mode, snapshot_id, profile_id, total_cost,
                    total_tokens, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    project_id = excluded.project_id,
                    slug = excluded.slug,
                    first_ts = excluded.first_ts,
                    last_ts = excluded.last_ts,
                    span_s = excluded.span_s,
                    archetype = excluded.archetype,
                    mode = excluded.mode,
                    mode_source = excluded.mode_source,
                    purpose = excluded.purpose,
                    purpose_source = excluded.purpose_source,
                    entrypoint = excluded.entrypoint,
                    billing_mode = excluded.billing_mode,
                    snapshot_id = excluded.snapshot_id,
                    profile_id = excluded.profile_id,
                    total_cost = excluded.total_cost,
                    total_tokens = excluded.total_tokens,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id, project_id, slug or project_slug, first_ts, last_ts,
                    span_s, archetype, mode, mode_source, purpose, purpose_source,
                    entrypoint, billing_mode, snapshot_id, profile_id, total_cost,
                    total_tokens, _now(),
                ),
            )

    def upsert_transcript(
        self,
        *,
        session_id: str,
        path: str,
        kind: str,
        agent_id: str | None = None,
        agent_type: str | None = None,
        spawn_depth: int = 0,
        parent_agent_id: str | None = None,
        mtime_ns: int = 0,
        size_bytes: int = 0,
        parser_version: int = 0,
        digest_json: str,
        turns_agg: list[dict] | None = None,
        recache_turns: list[dict] | None = None,
        events: list[dict] | None = None,
        compactions: list[dict] | None = None,
    ) -> int:
        """Insert or update one transcript row, replacing its
        ``turns_agg``/``recache_turns``/``events``/``compactions`` child
        rows wholesale (a re-parse always supersedes the previous
        breakdown for that file). Returns the transcript's row id.
        """
        conn = self._connection()
        with _transaction(conn):
            conn.execute(
                """
                INSERT INTO transcripts (
                    session_id, path, kind, agent_id, agent_type, spawn_depth,
                    parent_agent_id, mtime_ns, size_bytes, parser_version,
                    digest_json, missing_since, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(path) DO UPDATE SET
                    session_id = excluded.session_id,
                    kind = excluded.kind,
                    agent_id = excluded.agent_id,
                    agent_type = excluded.agent_type,
                    spawn_depth = excluded.spawn_depth,
                    parent_agent_id = excluded.parent_agent_id,
                    mtime_ns = excluded.mtime_ns,
                    size_bytes = excluded.size_bytes,
                    parser_version = excluded.parser_version,
                    digest_json = excluded.digest_json,
                    missing_since = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id, path, kind, agent_id, agent_type, spawn_depth,
                    parent_agent_id, mtime_ns, size_bytes, parser_version,
                    digest_json, _now(),
                ),
            )
            transcript_id = int(
                conn.execute("SELECT id FROM transcripts WHERE path = ?", (path,)).fetchone()["id"]
            )
            conn.execute("DELETE FROM turns_agg WHERE transcript_id = ?", (transcript_id,))
            conn.execute("DELETE FROM recache_turns WHERE transcript_id = ?", (transcript_id,))
            conn.execute("DELETE FROM events WHERE transcript_id = ?", (transcript_id,))
            conn.execute("DELETE FROM compactions WHERE transcript_id = ?", (transcript_id,))
            for row in turns_agg or []:
                conn.execute(
                    """
                    INSERT INTO turns_agg (
                        transcript_id, day, model, turns, input_tokens,
                        cache_creation_tokens, cache_read_tokens, output_tokens,
                        thinking_tokens, cc_5m, cc_1h, cost
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        transcript_id, row["day"], row["model"], row.get("turns", 0),
                        row.get("input_tokens", 0), row.get("cache_creation_tokens", 0),
                        row.get("cache_read_tokens", 0), row.get("output_tokens", 0),
                        row.get("thinking_tokens", 0), row.get("cc_5m", 0),
                        row.get("cc_1h", 0), row.get("cost", 0.0),
                    ),
                )
            for row in recache_turns or []:
                conn.execute(
                    """
                    INSERT INTO recache_turns (
                        transcript_id, turn_index, signature,
                        cache_creation_tokens, preceding_primary, gap_s
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        transcript_id, row["turn_index"], row["signature"],
                        row.get("cache_creation_tokens", 0),
                        row.get("preceding_primary"), row.get("gap_s"),
                    ),
                )
            for row in events or []:
                conn.execute(
                    """
                    INSERT INTO events (transcript_id, kind, subkind, ts, dropped_tokens, duration_ms)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        transcript_id, row["kind"], row.get("subkind"), row.get("ts"),
                        row.get("dropped_tokens"), row.get("duration_ms"),
                    ),
                )
            for row in compactions or []:
                conn.execute(
                    """
                    INSERT INTO compactions (
                        transcript_id, ts, pre_tokens, post_tokens, dropped_tokens,
                        trigger, join_delta_s
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        transcript_id, row["ts"], row.get("pre_tokens"),
                        row.get("post_tokens"), row.get("dropped_tokens"),
                        row.get("trigger"), row.get("join_delta_s"),
                    ),
                )
        return transcript_id

    def upsert_snapshot(
        self, *, project_slug: str, project_root_path: str = "", ts: str,
        schema_version: int, digest_json: str,
    ) -> int:
        """Insert or update one config-snapshot row (``snapshots.py``'s
        ``Snapshot``, already flattened/redacted), deduped by its natural
        key ``(project_id, ts, schema_version)`` (schema v2) so
        re-ingesting the same on-disk snapshot file on a later watcher
        tick updates the existing row instead of growing a duplicate one
        -- the same idempotent posture every other ``upsert_*`` method
        already has. Returns the snapshot's row id, so a caller can pass
        it as ``upsert_session``'s ``snapshot_id``."""
        conn = self._connection()
        with _transaction(conn):
            project_id = self._upsert_project(conn, project_slug, project_root_path)
            conn.execute(
                """
                INSERT INTO snapshots (project_id, ts, schema_version, digest_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, ts, schema_version) DO UPDATE SET
                    digest_json = excluded.digest_json
                """,
                (project_id, ts, schema_version, digest_json),
            )
            row = conn.execute(
                "SELECT id FROM snapshots WHERE project_id = ? AND ts = ? AND schema_version = ?",
                (project_id, ts, schema_version),
            ).fetchone()
            return int(row["id"])

    def upsert_workflow_run(
        self,
        *,
        session_id: str,
        run_id: str,
        agent_count: int = 0,
        phase_titles: list[str] | None = None,
        started: str | None = None,
        finished: str | None = None,
        cost: float = 0.0,
        status: str | None = None,
    ) -> int:
        """Insert or update one ``<session>/workflows/wf_*.json`` run row
        (``workflows.parse_workflow_file``/``link_workflow_agents``'s
        ``WorkflowRun``, already cost-linked by the caller), deduped by
        ``(session_id, run_id)``. ``phase_titles`` is stored as a JSON
        array of names only -- never ``detail``, which carries workflow
        source/prompt text (see ``workflows.py``'s module docstring)."""
        conn = self._connection()
        with _transaction(conn):
            conn.execute(
                """
                INSERT INTO workflow_runs (
                    session_id, run_id, agent_count, phases, started,
                    finished, cost, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, run_id) DO UPDATE SET
                    agent_count = excluded.agent_count,
                    phases = excluded.phases,
                    started = excluded.started,
                    finished = excluded.finished,
                    cost = excluded.cost,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id, run_id, agent_count,
                    json.dumps(list(phase_titles or [])),
                    started, finished, cost, status, _now(),
                ),
            )
            row = conn.execute(
                "SELECT id FROM workflow_runs WHERE session_id = ? AND run_id = ?",
                (session_id, run_id),
            ).fetchone()
            return int(row["id"])

    def upsert_profile(self, *, profile_id: str, name: str, toml_path: str) -> None:
        """Insert or update one profile's index row (v0.3's
        ``profiles/<id>.toml``, tracked here from v0.2)."""
        conn = self._connection()
        # A single statement is already atomic under autocommit -- no
        # explicit transaction wrapper needed (see _transaction's own
        # docstring; this isn't one of the multi-statement writers finding
        # 2/5 is about).
        conn.execute(
            "INSERT INTO profiles (id, name, toml_path, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, toml_path = excluded.toml_path, "
            "updated_at = excluded.updated_at",
            (profile_id, name, toml_path, _now()),
        )

    def record_baseline(
        self, *, project_slug: str, project_root_path: str = "", window_start: str,
        window_end: str, archetype: str | None, digest_json: str,
    ) -> int:
        """Insert one baseline-capture row. Returns the baseline's row
        id."""
        conn = self._connection()
        with _transaction(conn):
            project_id = self._upsert_project(conn, project_slug, project_root_path)
            cursor = conn.execute(
                "INSERT INTO baselines (project_id, window_start, window_end, archetype, digest_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, window_start, window_end, archetype, digest_json, _now()),
            )
            return int(cursor.lastrowid)

    def known_files(self) -> dict[str, tuple[int, int]]:
        """``{path: (mtime_ns, size_bytes)}`` for every transcript
        currently stored — the watcher's own incremental-diff basis, so
        it never has to re-stat/re-parse an unchanged file. Local-only:
        never exposed through a read query or the API."""
        rows = self._connection().execute("SELECT path, mtime_ns, size_bytes FROM transcripts").fetchall()
        return {row["path"]: (row["mtime_ns"], row["size_bytes"]) for row in rows}

    def remove_missing(self, known_paths: set[str]) -> int:
        """Mark every transcript row whose ``path`` is not in
        ``known_paths`` (a file the watcher can no longer find on disk --
        deleted, or already past Claude Code's own ``cleanupPeriodDays``
        retention) with a ``missing_since`` timestamp, instead of
        deleting it outright (review finding 3: "the store must outlive
        ``cleanupPeriodDays``"). A missing transcript's stored
        ``digest_json`` is still enough to serve it in a report or
        ``rebuild.corpus_from_store`` -- only :meth:`retention_prune` or
        ``claude-token-lens serve --purge`` actually delete a transcript
        row. A transcript whose file has reappeared (``path`` is back in
        ``known_paths``) has its ``missing_since`` cleared again. Returns
        the number of transcripts *newly* marked missing on this call --
        see :meth:`count_missing_transcripts` for the running total."""
        conn = self._connection()
        with _transaction(conn):
            rows = conn.execute("SELECT id, path, missing_since FROM transcripts").fetchall()
            now = _now()
            newly_missing = 0
            for row in rows:
                is_known = row["path"] in known_paths
                if not is_known and row["missing_since"] is None:
                    conn.execute(
                        "UPDATE transcripts SET missing_since = ?, updated_at = ? WHERE id = ?",
                        (now, now, row["id"]),
                    )
                    newly_missing += 1
                elif is_known and row["missing_since"] is not None:
                    conn.execute(
                        "UPDATE transcripts SET missing_since = NULL, updated_at = ? WHERE id = ?",
                        (now, row["id"]),
                    )
        return newly_missing

    def count_missing_transcripts(self) -> int:
        """The running total of transcripts currently marked missing
        (``missing_since`` is set) -- what ``/api/health`` reports as
        ``transcripts_missing``, distinct from :meth:`remove_missing`'s
        own per-tick delta return value."""
        row = self._connection().execute(
            "SELECT COUNT(*) AS n FROM transcripts WHERE missing_since IS NOT NULL"
        ).fetchone()
        return int(row["n"])

    def retention_prune(self, retention_days: int) -> int:
        """Delete every session (and its transcripts/workflow runs/tags/
        child rows) last active more than ``retention_days`` ago. Returns
        the number of sessions removed. A transcript's file may still
        exist on disk (or have already been cleaned up by Claude Code's
        own ``cleanupPeriodDays``, or be marked missing via
        :meth:`remove_missing`) -- either way the store no longer needs
        rows for it once its session ages out of the configured
        retention window (plan "Locked-down installs" / "Retention and
        portability"). This is one of only two ways a session/transcript
        row is ever actually deleted (the other being
        ``claude-token-lens serve --purge``, which drops the whole
        store).

        The whole prune runs inside one explicit transaction (review
        finding 2/5): every child table with a foreign key into
        ``sessions``/``transcripts`` -- including ``workflow_runs``,
        which the original implementation omitted and which would
        otherwise raise ``sqlite3.IntegrityError`` on the ``sessions``
        delete for any session with a recorded workflow run -- is deleted
        before its parent, and a failure partway through rolls back the
        entire prune rather than leaving it half-applied."""
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - retention_days * 86400)
        )
        conn = self._connection()
        with _transaction(conn):
            rows = conn.execute(
                "SELECT id FROM sessions WHERE last_ts IS NOT NULL AND last_ts < ?", (cutoff,)
            ).fetchall()
            session_ids = [row["id"] for row in rows]
            for session_id in session_ids:
                transcript_rows = conn.execute(
                    "SELECT id FROM transcripts WHERE session_id = ?", (session_id,)
                ).fetchall()
                for trow in transcript_rows:
                    transcript_id = trow["id"]
                    conn.execute("DELETE FROM turns_agg WHERE transcript_id = ?", (transcript_id,))
                    conn.execute("DELETE FROM recache_turns WHERE transcript_id = ?", (transcript_id,))
                    conn.execute("DELETE FROM events WHERE transcript_id = ?", (transcript_id,))
                    conn.execute("DELETE FROM compactions WHERE transcript_id = ?", (transcript_id,))
                conn.execute("DELETE FROM transcripts WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM workflow_runs WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM session_tags WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return len(session_ids)

    def log_usage(self, *, ts: str, window_start: str | None, window_end: str | None,
                  utilization_pct: float | None, raw: dict) -> None:
        """Append one ``get_usage`` snapshot (see ``usage.py``'s
        ``log-usage``)."""
        conn = self._connection()
        # Single statement -- see upsert_profile's comment above.
        conn.execute(
            "INSERT INTO usage_log (ts, window_start, window_end, utilization_pct, raw_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, window_start, window_end, utilization_pct, json.dumps(raw, sort_keys=True)),
        )

    # -- read queries (API-facing: never a local path) --------------------

    def change_token(self) -> str:
        """A cheap fingerprint of the store's current content -- changes
        whenever a transcript, snapshot, workflow run or session tag is
        added, removed, re-parsed or set, and only then. Combines each of
        ``transcripts``, ``snapshots``, ``workflow_runs`` and
        ``session_tags``' own row count with its own "latest touched"
        marker (``updated_at`` for transcripts/workflow_runs; ``ts``, the
        closest analogue, for snapshots, which have no ``updated_at``
        column; ``set_at`` for session_tags). ``workflow_runs``/
        ``session_tags`` were added under review finding 8 -- without
        them, a tag write or a freshly-linked workflow run left the
        report-model cache (``api.py``'s ``_get_report_model``) serving a
        stale report until some unrelated transcript/snapshot change
        happened to also invalidate it. Used by ``api.py``'s report-model
        cache to know when a cached report needs rebuilding, without
        exposing anything about *what* changed."""
        conn = self._connection()
        transcripts_row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM transcripts"
        ).fetchone()
        snapshots_row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(ts), '') FROM snapshots"
        ).fetchone()
        workflow_runs_row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM workflow_runs"
        ).fetchone()
        session_tags_row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(set_at), '') FROM session_tags"
        ).fetchone()
        return (
            f"{transcripts_row[0]}:{transcripts_row[1]}:"
            f"{snapshots_row[0]}:{snapshots_row[1]}:"
            f"{workflow_runs_row[0]}:{workflow_runs_row[1]}:"
            f"{session_tags_row[0]}:{session_tags_row[1]}"
        )

    def turns_for_session(self, session_id: str) -> dict | None:
        """Per-turn ``ctx``/cache/marker series for one session's
        top-level transcript, decoded from its stored ``digest_json``
        (the same lossless ``cache.result_from_jsonable`` decode
        ``service/rebuild.py`` uses) -- never re-parses a file, never
        exposes ``path`` or any other store-internal column. ``None``
        when the session has no stored top-level transcript.

        ``turn_series``: one ``[turn_index, ctx, cache_creation_tokens,
        is_recache, preceding_primary]`` row per priced turn
        (``turn_index > 0``), ``preceding_primary`` rendered as its
        enum's ``.value`` string (or ``None``).

        ``markers``: ``{"compactions": [...], "spawns": [...], "human":
        [...]}`` -- the turn indices whose ``preceding_primary`` is
        ``compact_boundary``, whose ``agent_brief_chars`` is set (an
        Agent/Task tool call was made from that turn), or whose
        ``human_prompt_chars`` is set (a human message preceded that
        turn), respectively.
        """
        row = self._connection().execute(
            "SELECT digest_json FROM transcripts WHERE session_id = ? AND kind = 'top-level'",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            result = result_from_jsonable(json.loads(row["digest_json"]))
        except (KeyError, TypeError, ValueError):
            return None

        turn_series: list[list] = []
        compactions: list[int] = []
        spawns: list[int] = []
        human: list[int] = []
        for turn in result.turns:
            if turn.turn_index <= 0:
                continue
            primary = turn.preceding_primary.value if turn.preceding_primary is not None else None
            turn_series.append(
                [turn.turn_index, turn.ctx, turn.cache_creation_tokens, turn.is_recache, primary]
            )
            if primary == EventKind.COMPACT_BOUNDARY.value:
                compactions.append(turn.turn_index)
            if turn.agent_brief_chars is not None:
                spawns.append(turn.turn_index)
            if turn.human_prompt_chars is not None:
                human.append(turn.turn_index)

        return {
            "turn_series": turn_series,
            "markers": {"compactions": compactions, "spawns": spawns, "human": human},
        }

    def summary(self, *, window_days: int | None = None) -> dict:
        """Corpus-wide totals: session/transcript counts and cost/token
        sums, optionally restricted to sessions whose ``last_ts`` falls
        in the trailing ``window_days``."""
        conn = self._connection()
        params: tuple = ()
        where = ""
        if window_days is not None:
            cutoff = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - window_days * 86400)
            )
            where = "WHERE last_ts IS NOT NULL AND last_ts >= ?"
            params = (cutoff,)
        row = conn.execute(
            f"SELECT COUNT(*) AS sessions, COALESCE(SUM(total_cost), 0) AS total_cost, "
            f"COALESCE(SUM(total_tokens), 0) AS total_tokens FROM sessions {where}",
            params,
        ).fetchone()
        transcripts = conn.execute("SELECT COUNT(*) AS n FROM transcripts").fetchone()["n"]
        return {
            "window_days": window_days,
            "sessions": row["sessions"],
            "transcripts": transcripts,
            "total_cost": row["total_cost"],
            "total_tokens": row["total_tokens"],
        }

    def sessions(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        """The most recent ``limit`` sessions (by ``first_ts`` descending),
        one summary dict each — no transcript paths."""
        rows = self._connection().execute(
            """
            SELECT s.id, s.slug, s.first_ts, s.last_ts, s.span_s, s.archetype,
                   s.mode, s.purpose, s.entrypoint, s.billing_mode, s.profile_id,
                   s.total_cost, s.total_tokens
            FROM sessions s
            ORDER BY s.first_ts DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        result = [dict(row) for row in rows]
        for item in result:
            item["slug"] = redact_slug(item["slug"])
        return result

    def session(self, session_id: str) -> dict | None:
        """One session's full detail: its own summary fields plus its
        transcripts (kind/agent_type/spawn_depth only — no ``path``) and
        any tags. ``None`` if ``session_id`` is unknown."""
        conn = self._connection()
        row = conn.execute(
            """
            SELECT id, slug, first_ts, last_ts, span_s, archetype, mode,
                   mode_source, purpose, purpose_source, entrypoint,
                   billing_mode, profile_id, total_cost, total_tokens
            FROM sessions WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["slug"] = redact_slug(result["slug"])
        transcript_rows = conn.execute(
            "SELECT id, kind, agent_id, agent_type, spawn_depth, parent_agent_id "
            "FROM transcripts WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        result["transcripts"] = [dict(trow) for trow in transcript_rows]
        result["tags"] = self.tags(session_id)
        return result

    def daily_usage(self, *, days: int = 30) -> list[dict]:
        """Per-day, per-model token/cost rollups for the trailing
        ``days`` days, joined from ``turns_agg`` (no per-transcript or
        path detail)."""
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
        rows = self._connection().execute(
            """
            SELECT day, model,
                   SUM(turns) AS turns,
                   SUM(input_tokens) AS input_tokens,
                   SUM(cache_creation_tokens) AS cache_creation_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(thinking_tokens) AS thinking_tokens,
                   SUM(cc_5m) AS cc_5m,
                   SUM(cc_1h) AS cc_1h,
                   SUM(cost) AS cost
            FROM turns_agg
            WHERE day >= ?
            GROUP BY day, model
            ORDER BY day, model
            """,
            (cutoff,),
        ).fetchall()
        return [dict(row) for row in rows]

    def recache(self) -> dict:
        """Aggregate RE-CACHE turn counts by signature, corpus-wide."""
        rows = self._connection().execute(
            """
            SELECT signature, COUNT(*) AS turns, COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens
            FROM recache_turns
            GROUP BY signature
            """
        ).fetchall()
        by_signature = {row["signature"]: {"turns": row["turns"], "cache_creation_tokens": row["cache_creation_tokens"]} for row in rows}
        return {"by_signature": by_signature}

    def compactions(self) -> list[dict]:
        """Every recorded compaction event (no transcript path — only
        the opaque, store-local ``transcript_id``)."""
        rows = self._connection().execute(
            """
            SELECT transcript_id, ts, pre_tokens, post_tokens, dropped_tokens, trigger, join_delta_s
            FROM compactions
            ORDER BY ts
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def snapshots(self) -> list[dict]:
        """Every captured config snapshot's identity and digest (already
        flattened/redacted before storage — see ``schema.py``), plus its
        owning project's ``slug`` as ``project_slug``. A snapshot
        attributed to :data:`GLOBAL_PROJECT_SLUG` (the watcher's
        synthetic attribution for a machine-wide, not-per-project
        snapshot file — see module docstring) reports ``project_slug`` as
        ``None`` instead of that internal sentinel, so a caller sees an
        honest "no project" rather than a fabricated one (S1-integration
        fix 1.c)."""
        rows = self._connection().execute(
            """
            SELECT sn.id, sn.project_id, p.slug AS project_slug, sn.ts,
                   sn.schema_version, sn.digest_json
            FROM snapshots sn
            LEFT JOIN projects p ON p.id = sn.project_id
            ORDER BY sn.ts
            """
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if item.get("project_slug") == GLOBAL_PROJECT_SLUG:
                item["project_slug"] = None
            elif item.get("project_slug") is not None:
                item["project_slug"] = redact_slug(item["project_slug"])
            result.append(item)
        return result

    def profiles(self) -> list[dict]:
        """Every indexed profile's id/name (no ``toml_path`` — local
        filesystem location, never API-returned)."""
        rows = self._connection().execute(
            "SELECT id, name, updated_at FROM profiles ORDER BY name"
        ).fetchall()
        return [dict(row) for row in rows]

    def baselines(self) -> list[dict]:
        """Every recorded baseline capture, plus its owning project's
        (redacted) ``slug`` -- joined in (nit 27) so a caller can label a
        baseline row by project name without a second round trip through
        ``sessions()``/a raw ``project_id``."""
        rows = self._connection().execute(
            """
            SELECT b.id, b.project_id, p.slug AS project_slug, b.window_start,
                   b.window_end, b.archetype, b.digest_json, b.created_at
            FROM baselines b
            LEFT JOIN projects p ON p.id = b.project_id
            ORDER BY b.created_at
            """
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if item.get("project_slug") is not None:
                item["project_slug"] = redact_slug(item["project_slug"])
            result.append(item)
        return result

    def tags(self, session_id: str) -> dict:
        """``{key: value}`` of every tag set on ``session_id`` (empty
        dict if none)."""
        rows = self._connection().execute(
            "SELECT key, value FROM session_tags WHERE session_id = ?", (session_id,)
        ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def all_tags(self) -> dict[str, dict[str, str]]:
        """Every session's tags, grouped by ``session_id`` -- the
        whole-store counterpart to :meth:`tags` (one session at a time).
        Used by ``api.py``'s report building to merge ``POST
        /api/sessions/<id>/tags`` writes into the same
        ``session_overrides`` mechanism ``config.load_session_overrides``
        feeds ``classify.classify_session`` (review finding 7: a tag
        write must actually change the built report, not just sit in the
        store inertly)."""
        rows = self._connection().execute("SELECT session_id, key, value FROM session_tags").fetchall()
        result: dict[str, dict[str, str]] = {}
        for row in rows:
            result.setdefault(row["session_id"], {})[row["key"]] = row["value"]
        return result

    def set_tag(self, session_id: str, key: str, value: str) -> None:
        """Set (or overwrite) one ``session_tags`` entry — the only
        mutation the v0.2 API exposes (``POST /api/sessions/<id>/tags``,
        per ``docs/api.md``)."""
        conn = self._connection()
        # Single statement -- see upsert_profile's comment above.
        conn.execute(
            "INSERT INTO session_tags (session_id, key, value, set_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_id, key) DO UPDATE SET value = excluded.value, set_at = excluded.set_at",
            (session_id, key, value, _now()),
        )


__all__ = ["Store"]
