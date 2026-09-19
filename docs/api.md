# v0.2 service JSON API

`claude-token-lens serve` runs a local, read-only HTTP service
(`http.server`, stdlib only — plan Milestone v0.2, "Docker service and
web UI") in front of the SQLite store `service/store.py`'s `Store`
maintains. This file is the frozen contract every `/api/*` route
implements against `service/contracts.py`'s `ApiHandler` shape — the
UI (`docs/ui.md`) and any third-party client are built against this
document, not against `store.py` directly.

No route ever calls out to the network: the service reads transcripts
and the store under `--projects-root`/`--config-dir` and nothing else
(see the "Local only" section below).

## Envelope

Every response is JSON with a `Content-Type: application/json`
header and one of two top-level shapes:

```json
{"ok": true, "data": ...}
```

```json
{"ok": false, "error": {"code": "not_found", "message": "session not found"}}
```

`error.code` is a short, stable, machine-matchable string (`not_found`,
`bad_request`, `managed`, `internal_error`, ...); `error.message` is a
one-line human-readable explanation. The HTTP status code carries the
same information for clients that don't want to parse the body
(`200` for `ok: true`, `400`/`404`/`403`/`500` for the matching
`error.code`, per route below). This is exactly
`service.contracts.ApiError.to_envelope()`'s shape.

## Privacy

**No response body from any route below may ever contain message text,
a path found inside a transcript, or a shell command longer than the
40-character `cmd_prefix` contract `model.py` already enforces** — the
same rule `service/schema.py` and `service/__init__.py` state for the
store itself. Concretely: no route returns `transcripts.path`,
`projects.root_path`, or `profiles.toml_path`, and no route ever passes
a `Store` read query's output through un-redacted if that query's own
contract allows a path-shaped field (none of `Store`'s read queries do,
by construction — see `service/store.py`'s docstrings and
`tests/test_service_store.py`'s path-leak guard). Every table/`Section`
value embedded in a `report.*` route already went through this same
audit for the CLI's own JSON output (`render/json_out.py`) and is
reused verbatim here.

## Local only

The service opens exactly one listening socket, on `--bind` (default
`127.0.0.1`) `--port` (default `8765`), and never opens an outbound
connection — no route proxies to another host, resolves a hostname, or
fetches a URL. This is the same guarantee `SECURITY.md` documents for
the CLI, extended to the service: a test (`tests/test_service_egress.py`,
built alongside `api.py`) asserts no `socket.connect` call targets
anything outside the bound address for the lifetime of a test server.

## Routes

All `GET` routes accept query-string parameters; all `POST` routes
accept a JSON request body (`Content-Type: application/json`). A route
not listed here returns `404` with `error.code: "not_found"`.

### `GET /api/health`

Liveness/diagnostics probe (also the Docker healthcheck target — plan:
"healthcheck on `/api/health`"). Never fails once the process is up.

`data`: `{"status": "ok", "schema_version": int, "watcher": WatcherStats-as-dict}`.

### `GET /api/summary`

Corpus-wide totals — `Store.summary`.

Query: `window_days` (int, optional).

`data`: `{"window_days": int|null, "sessions": int, "transcripts": int, "total_cost": float, "total_tokens": int}`.

### `GET /api/sessions`

Recent sessions — `Store.sessions`.

Query: `limit` (default 50), `offset` (default 0).

`data`: `[{"id", "slug", "first_ts", "last_ts", "span_s", "archetype", "mode", "purpose", "entrypoint", "billing_mode", "profile_id", "total_cost", "total_tokens"}, ...]`.

### `GET /api/session/<id>`

One session's detail — `Store.session`. `404` (`error.code: "not_found"`)
if `<id>` is unknown.

`data`: the session-summary fields above, plus `transcripts` (list of
`{"id", "kind", "agent_id", "agent_type", "spawn_depth", "parent_agent_id"}`
— no `path`) and `tags` (`{key: value}`).

### `GET /api/recache`

Corpus-wide RE-CACHE breakdown — `Store.recache`.

`data`: `{"by_signature": {"full-expiry": {"turns", "cache_creation_tokens"}, "prefix-invalidated": {...}}}`.

### `GET /api/ttl`

TTL simulation summary (per agent type: observed vs. simulated 5m/1h
cost, fidelity, recommendation) — same shape as the CLI's `ttl` section
tables (`render/json_out.py`'s `Section`/`Table` encoding), sourced by
re-running `ttl.py`'s simulation over the store's `turns_agg`/
`recache_turns` rows rather than a fresh parse.

### `GET /api/compactions`

Every recorded compaction — `Store.compactions`.

`data`: `[{"transcript_id", "ts", "pre_tokens", "post_tokens", "dropped_tokens", "trigger", "join_delta_s"}, ...]`.

### `GET /api/config-diff`

Effective-config comparison across projects (plan "Configuration
layers" section: `config_groups`/`config_drift`), computed from the
latest `snapshots` row per project.

Query: `key` (a specific settings key) or `auto_keys=1` (every managed
key). Mirrors the CLI's `config-diff` subcommand.

### `GET /api/recommendations`

The same `Recommendation` list `recommend.recommend()` produces for the
CLI's `report`, computed from the store's latest snapshot and session
window rather than a fresh corpus scan.

`data`: `[{"id", "severity", "category", "title", "action", "lever", "scope", "evidence": [[label, value, source_table, row_key], ...]}, ...]` —
exactly `render/json_out.py`'s existing `Recommendation` encoding.

### `GET /api/profiles`

Indexed profiles — `Store.profiles`. `data`: `[{"id", "name", "updated_at"}, ...]` (no `toml_path`).

### `GET /api/profiles/<id>/diff`

The unified-diff-style text `render_patch_set()` would produce for
applying `<id>` against the requesting project's current effective
config (`--dry-run` equivalent, read-only — this route never writes
anything, matching the plan's "the service never calls `apply`; it
renders the diff and the command").

`data`: `{"diff": str, "apply_command": str}`.

### `GET /api/baseline`

The most recent baseline capture for a project — `Store.baselines`
filtered to the newest row per `project_id`.

### `GET /api/report.md` / `GET /api/report.html` / `GET /api/report.json`

The full report in each format, built from the store instead of a fresh
parse — byte-equivalent in content to running the CLI's `report`
subcommand with `--json`/`--html`/(default) over the same window,
modulo the "verified against CLI JSON" test the plan's Milestone v0.2
Tests bullet requires (`tests/test_service_api.py`, built alongside
`api.py`). `report.md`/`report.html` set `Content-Type: text/markdown`/
`text/html` instead of the envelope shape above (the raw rendered
document, matching the CLI's own stdout for `--html`).

## Mutating routes

The only two routes that write anything, both scoped to a single row
and never touching `~/.claude` proper (plan: "neither touches
`~/.claude` proper"):

### `POST /api/sessions/<id>/tags`

Body: `{"key": "mode"|"purpose", "value": str}`. Calls `Store.set_tag`
(the same override `config.sessions.toml` holds for the CLI). `404` if
`<id>` is unknown; `400` if `key` isn't `mode`/`purpose`.

`data`: `{"session_id": str, "tags": {key: value}}` (the session's full
tag set after the write).

### `POST /api/profiles`

Body: a profile TOML document's already-parsed-and-validated JSON form
(v0.3's `profiles/schema.py` validates it before this route ever writes
a file). Writes `<config_dir>/profiles/<id>.toml` and calls
`Store.upsert_profile`. `400` (`error.code: "bad_request"`) if the
schema rejects an unknown key (plan: "the schema rejects anything else
so a profile can never promise an effect the harness cannot deliver").

`data`: `{"id": str, "name": str}`.

## Managed-settings routes

Any route whose `data` would include a recommendation or a diff whose
`lever` targets a managed-settings key still returns `200`/`ok: true` —
the managed-ness is carried in the payload (`scope: "managed"`, per
`model.py`'s `Recommendation.scope`) rather than as an HTTP error, so
the UI can render "managed by policy, raise with your administrator"
inline (plan "Enterprise use"). `POST /api/profiles`/`/tags` never
write a managed key regardless of what the client sends — that
validation lives in `apply`/`profiles/schema.py`, not this API, since
the service itself never calls `apply`.

## Store rebuild

`GET /api/report.*` above is built from the store instead of a fresh
parse, via `service/rebuild.py`'s `corpus_from_store(store, *, days=None,
since=None, until=None, window_by="mtime") -> Corpus`. This is what lets
a report be served for a session whose transcript file has already been
removed by Claude Code's own `cleanupPeriodDays` retention: the watcher
(`service/watcher.py`) folds every parsed transcript's full
`TranscriptResult` into `transcripts.digest_json` (the same lossless
encoding `cache.py`'s on-disk digest cache uses), and `corpus_from_store`
decodes those digests straight back into a `Corpus` shaped exactly as
`corpus.load_corpus` would have produced from the live files, so
`report.build_report(corpus, ...)` runs unmodified against either one.
`days`/`since`/`until`/`window_by` mirror `discovery.find_sessions`'s own
parameters and windowing semantics.

Two fields do not survive the round trip, both store-schema gaps rather
than bugs in `corpus_from_store` itself:

- **Workflow runs.** The store has no table for a `<session>/workflows/
  wf_*.json` run's own cost/phase/status data, so a rebuilt session's
  `workflows` list is always empty — its report would undercount the
  `"workflows"`/`"phases"` sections and `overview.workflow_runs` for a
  session that ran one. Every workflow-nested subagent's own turns,
  tokens and cost still come through in full, since those are ordinary
  persisted transcripts.
- **`SessionBundle.project_dir`.** Always the empty string once rebuilt
  from the store — nothing in `report.build_report`'s own code path
  reads it, so this has no effect on any route's output.
