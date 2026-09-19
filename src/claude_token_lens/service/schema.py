"""SQLite schema for the v0.2 local read-only service (``serve``).

This module is pure data: SQL ``CREATE TABLE``/``CREATE INDEX`` strings
plus :data:`SCHEMA_VERSION`. It has no behaviour of its own —
``service/store.py``'s :class:`~claude_token_lens.service.store.Store`
is the only thing that executes these statements. Keeping the schema
separate from the code that runs it lets a migration (a future
``SCHEMA_VERSION`` bump) diff cleanly against this file's history.

:data:`SCHEMA_VERSION` is independent of the top-level
``claude_token_lens.SCHEMA_VERSION`` (the frozen ``model.py`` dataclass
contract that gates the on-disk digest *cache*) and of ``PARSER_VERSION``
(``parse.py``'s parsing-logic version). This one versions the SQLite
*store*'s own table shapes. A mismatch between the version recorded in
the ``meta`` table and this module's :data:`SCHEMA_VERSION` means the
store's tables are stale relative to the code that opened them; v0.2's
first cut treats that as "drop and rebuild" (the store is a derived
cache over transcripts on disk, never the source of truth), so there is
no ``ALTER TABLE`` migration path yet — a later version may add one if
rebuilding becomes too slow for a large corpus.

Privacy rule (binding on every table below, restated from ``model.py``'s
own module docstring and enforced here for the store specifically): no
column may ever hold message text, a path *found inside a transcript*
(a file a tool read/wrote, a working directory mentioned in output), or
a shell command. The one narrow exception is ``transcripts.path`` (and
``projects.root_path``, ``profiles.toml_path``) — the local filesystem
location of a file *this machine's own store* needs to reopen it (to
re-parse on change, to know a project's scan root, to load a profile
overlay). Those columns exist for the watcher's own bookkeeping only.
The API layer (``service/contracts.py``'s ``ApiHandler``, and every
``/api/*`` route documented in ``docs/api.md``) must never select or
forward them — ``Store``'s own read queries (``summary``, ``sessions``,
``session``, ``daily_usage``, ``recache``, ``compactions``,
``snapshots``, ``tags``) are written to leave those columns out of their
result dicts entirely, and ``tests/test_service_store.py`` asserts by
construction (a distinctive fake path round-tripped through every read
query) that none of them ever surface it.

``digest_json`` columns (``transcripts.digest_json``,
``snapshots.digest_json``, ``baselines.digest_json``) hold the output of
``cache.encode_result`` (or the equivalent flattened/redacted encoding
for snapshots/baselines) — numeric digests and short enum-like strings
only, already subject to ``model.py``'s own no-message-text contract
before it ever reaches this schema.
"""

from __future__ import annotations

#: Bump when a table or index below changes shape. See module docstring.
SCHEMA_VERSION = 1

CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: One row per discovered project directory (``~/.claude/projects/<slug>``).
#: ``root_path`` is the local scan root the watcher polls — never
#: API-returned (see module docstring).
CREATE_PROJECTS = """
CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,
    root_path  TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);
"""

#: One row per top-level session (``SessionRecord``). Cost/token totals
#: are folded in at upsert time from the session's own transcripts so
#: reads never have to re-aggregate ``turns_agg`` for a session listing.
CREATE_SESSIONS = """
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
"""

#: One row per parsed transcript file (top-level, subagent or
#: workflow-agent). ``path`` is local-store-only (see module docstring).
CREATE_TRANSCRIPTS = """
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
    digest_json     TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcripts_session_id ON transcripts(session_id);
"""

#: Per-transcript, per-day, per-model rollups — the basis for
#: ``Store.daily_usage`` and the Usage report section without re-reading
#: every turn on each request.
CREATE_TURNS_AGG = """
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
"""

#: One row per detected RE-CACHE turn (``Turn.is_recache``), feeding
#: ``Store.recache``.
CREATE_RECACHE_TURNS = """
CREATE TABLE IF NOT EXISTS recache_turns (
    id                     INTEGER PRIMARY KEY,
    transcript_id          INTEGER NOT NULL REFERENCES transcripts(id),
    turn_index             INTEGER NOT NULL,
    signature              TEXT NOT NULL,
    cache_creation_tokens  INTEGER NOT NULL DEFAULT 0,
    preceding_primary      TEXT,
    gap_s                  REAL
);
"""

#: Non-priced structural events (``Event``), content-free (kind/subkind
#: only — never the attachment/tool content itself).
CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY,
    transcript_id  INTEGER NOT NULL REFERENCES transcripts(id),
    kind           TEXT NOT NULL,
    subkind        TEXT,
    ts             TEXT,
    dropped_tokens INTEGER,
    duration_ms    INTEGER
);
"""

#: One row per ``COMPACT_BOUNDARY`` event, feeding ``Store.compactions``.
CREATE_COMPACTIONS = """
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
"""

#: One row per captured config snapshot (``snapshots.py``'s ``Snapshot``,
#: already flattened/redacted before it ever reaches this table).
CREATE_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY,
    project_id     INTEGER REFERENCES projects(id),
    ts             TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    digest_json    TEXT NOT NULL
);
"""

#: User-set ``mode``/``purpose`` overrides (mirrors ``config.
#: sessions.toml``, but keyed for fast API reads/writes instead of a
#: TOML round trip on every request).
CREATE_SESSION_TAGS = """
CREATE TABLE IF NOT EXISTS session_tags (
    session_id TEXT NOT NULL REFERENCES sessions(id),
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    set_at     TEXT NOT NULL,
    PRIMARY KEY (session_id, key)
);
"""

#: Index of profile files on disk (v0.3's ``profiles/<id>.toml``, tracked
#: here from v0.2 so the service can list/diff them). ``toml_path`` is
#: local-store-only (see module docstring).
CREATE_PROFILES = """
CREATE TABLE IF NOT EXISTS profiles (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    toml_path  TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

#: One row per captured baseline window (``claude-token-lens baseline``).
CREATE_BASELINES = """
CREATE TABLE IF NOT EXISTS baselines (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER REFERENCES projects(id),
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    archetype    TEXT,
    digest_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""

#: Raw ``get_usage`` snapshots logged by ``log-usage`` (5-hour/weekly
#: window utilisation), kept verbatim as JSON for the Usage tab's
#: window-overlay view.
CREATE_USAGE_LOG = """
CREATE TABLE IF NOT EXISTS usage_log (
    id              INTEGER PRIMARY KEY,
    ts              TEXT NOT NULL,
    window_start    TEXT,
    window_end      TEXT,
    utilization_pct REAL,
    raw_json        TEXT NOT NULL
);
"""

#: Every ``CREATE TABLE``/``CREATE INDEX`` statement, in dependency
#: order (a table referencing another via ``REFERENCES`` is listed
#: after it) — ``Store.migrate`` executes these in this order.
ALL_STATEMENTS: tuple[str, ...] = (
    CREATE_META,
    CREATE_PROJECTS,
    CREATE_SNAPSHOTS,
    CREATE_SESSIONS,
    CREATE_TRANSCRIPTS,
    CREATE_TURNS_AGG,
    CREATE_RECACHE_TURNS,
    CREATE_EVENTS,
    CREATE_COMPACTIONS,
    CREATE_SESSION_TAGS,
    CREATE_PROFILES,
    CREATE_BASELINES,
    CREATE_USAGE_LOG,
)

__all__ = [
    "SCHEMA_VERSION",
    "CREATE_META",
    "CREATE_PROJECTS",
    "CREATE_SESSIONS",
    "CREATE_TRANSCRIPTS",
    "CREATE_TURNS_AGG",
    "CREATE_RECACHE_TURNS",
    "CREATE_EVENTS",
    "CREATE_COMPACTIONS",
    "CREATE_SNAPSHOTS",
    "CREATE_SESSION_TAGS",
    "CREATE_PROFILES",
    "CREATE_BASELINES",
    "CREATE_USAGE_LOG",
    "ALL_STATEMENTS",
]
