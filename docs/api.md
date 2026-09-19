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

## `serve` command-line flags

Flags beyond `--projects-root`/`--config-dir`/`--port`/`--bind`/
`--allow-remote`/`--poll-interval`/`--retention-days`/`--exclude-project`/
`--once` (see `--help`):

- **`--billing-mode {api,subscription}`** (S1-integration fix 1.a) is
  stamped onto every session's `billing_mode` field (see `/api/sessions`
  above). Defaults to `<config-dir>/config.toml`'s own `billing` setting
  when omitted (itself defaulting to `"api"` — `config.py`'s `Config.billing`),
  so a subscription user only has to say so once, in one place, rather
  than on every `serve` invocation.
- **`--monthly-report DIR`** sets `ServeOptions.monthly_report_dir`, a
  directory a monthly report is written into. `None` (the default) means
  no monthly report is written.
- **`--purge`** deletes `<config-dir>/service.db` and its `-wal`/`-shm`
  sidecars and exits (S1-integration fix 2.e) — never starts the watcher
  or the API. Always prints exactly which files it would delete first;
  only actually deletes them when `--yes` is also given. Safe at any
  time: the store is always a derived cache (`service/store.py`'s module
  docstring), so the next `serve` run simply rebuilds it from the
  transcripts already on disk. Exits `2` (and deletes nothing) if
  `--yes` is missing, `0` otherwise (including when there is nothing to
  delete).

## Routes

All `GET` routes accept query-string parameters; all `POST` routes
accept a JSON request body (`Content-Type: application/json`). A route
not listed here returns `404` with `error.code: "not_found"`.

### `GET /api/health`

Liveness/diagnostics probe (also the Docker healthcheck target — plan:
"healthcheck on `/api/health`"). Never fails once the process is up.

`data`: `{"status": "ok", "schema_version": int, "transcripts_missing": int, "watcher": WatcherStats-as-dict}`.

`transcripts_missing` (review finding 3) is `Store.count_missing_transcripts()`
— the current count of transcript rows whose backing file the watcher
can no longer find on disk. A transcript in this state is *marked*, not
deleted: its `digest_json` keeps serving `report.*`/rebuild until it is
actually removed by `--retention-days`/`serve --purge` (see "Retention
and purge" in [docs/deploy.md](deploy.md)). This is also why a report
can still include a session whose transcript file Claude Code's own
`cleanupPeriodDays` retention has already removed — see "Store rebuild"
below.

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

If the session has a stored top-level transcript digest, `data` also
carries `turn_series` and `markers` (S1-integration fix 1.g), sourced
from `Store.turns_for_session` — the timeline chart's exact input
shape, no client-side reconstruction needed:

- `turn_series`: a list of `[turn_index, ctx, cache_creation_tokens,
  is_recache, preceding_primary]` per priced turn (`turn_index >= 1`),
  in turn order. `preceding_primary` is the `EventKind` string value
  (`"human_text"`, `"compact_boundary"`, `"tool_result"`, ...) of the
  event immediately preceding that turn.
- `markers`: `{"compactions": [turn_index, ...], "spawns": [turn_index, ...], "human": [turn_index, ...]}`
  — turn indices where a compaction boundary, an agent spawn
  (`agent_brief_chars` set), or a human prompt (`human_prompt_chars`
  set) preceded that turn.

Both fields are omitted entirely (never present as an empty list) when
no top-level transcript digest is stored yet, or the stored digest
can't be decoded — never fabricated.

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

A snapshot taken outside any recognised project (no project slug on
disk to attribute it to) is still captured — never dropped — under the
store's internal global/machine-wide bucket, but `Store.snapshots()`
reports its `project_slug` as `null` rather than a synthetic project
name (S1-integration fix 1.c). This route treats a `null`-slug snapshot
as a user-level configuration layer, not a project's, matching
`snapshots.py`'s own "(unknown project)" label for it.

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

## Report routes: how they are computed

Implementation notes for `service/api.py` (S1-api), for a future reader
of this frozen contract who needs to know how the report-backed routes
(`/api/ttl`, `/api/config-diff`, `/api/recommendations`,
`/api/report.md`/`.html`/`.json`) get their data, and where the
implementation had to make a call this document didn't spell out.

**Rebuild, not re-parse.** Every report-backed route rebuilds a
`Corpus` via `service.rebuild.corpus_from_store(store, days=window_days)`
(S1-watcher's module — see `service/__init__.py`) and runs it through
the same `report.build_report()` → `recommend.recommend()` →
`render/{json_out,markdown,html}.py` pipeline the CLI's own `report`
subcommand uses. `/api/ttl`, `/api/config-diff` and
`/api/recommendations` all build the *same* full report for the
requested `window_days` and read one section/field back out of it
(`/api/ttl` returns the assembled report's `"ttl"` `Section`;
`/api/config-diff` returns its `"config"` section's
`config-diff-<key>` table(s) — `report.py`'s own
`_build_config_section`, capped at 20 changed keys — rather than
recomputing `snapshots.build_config_diff_table` a second time with a
service-specific session-metrics rebuild the way the CLI's own
`config-diff` subcommand does; `/api/recommendations` returns
`model.recommendations`) rather than each running an independent,
narrower computation. A `key` that names a config key which didn't
change in the requested window returns `{"ok": true, "data": []}`, not
an error.

**Memoization key: `Store.change_token()`.** Rebuilding a full report on
every request would make every tab switch in the UI (`docs/ui.md`)
re-parse the whole corpus. The implementation caches the assembled
`ReportModel` in-process, keyed by `(window_days, change_token)`, where
`change_token` is `Store.change_token()` (S1-integration fix 1.f) — a
single string combining `(COUNT(*), MAX(updated_at))` over `transcripts`
and `(COUNT(*), MAX(ts))` over `snapshots`. A cache hit only requires
this token to be unchanged since the entry was built; any transcript or
snapshot insert/update moves it, forcing a rebuild on the next request.

**`/api/report.json`/`.md`/`.html` are unwrapped on success.** Their
body on `200` is the renderer's own native output (`render_json`/
`render_markdown`/`render_html`), not the `{"ok": ..., "data": ...}`
envelope — this is what makes `/api/report.json` byte-equivalent to
`claude-token-lens report --json` for the same window, and matches this
document's own "the raw rendered document" language for `.md`/`.html`.
A request error on one of these three routes (a bad `window_days`, or
an unexpected exception) still falls back to the normal JSON error
envelope; only the success path is raw.

**`GET /api/session/<id>` returns a superset of the listed fields.**
`Store.session()`'s dict includes `mode_source`/`purpose_source`
alongside every field `/api/sessions` lists — a non-breaking addition,
not a contradiction of the field list above (which describes the
session-summary fields plus `transcripts`/`tags`, not an exact field
count), and dropping fields `Store` already computes for no privacy
reason would only lose information a client might want.

**`/api/profiles/<id>/diff` and `POST /api/profiles` are `501`
stubs.** Both routes' body-shape validation (unknown-key checks, JSON
object checks) runs before the response, but both always return `501`
`not_implemented` — v0.3's `profiles/schema.py` (the profile-file
validator both routes need) does not exist yet at S1-api's own
delivery time. Swapping the final `_not_implemented(...)` for the real
read/write is the only change needed once that module lands.

**Static file serving.** `/` and `/static/*` serve
`service/static/index.html`/assets (the UI package's build output,
per `docs/ui.md`) when present, guarded against path traversal
(`Path.resolve()` plus a parent-containment check — a `..` segment or
an escaping resolved path is `404`, not an error). `service/static/`
is empty at S1-api's own delivery time (a sibling work package ships
its contents), so `/` falls back to a small, non-persisted placeholder
page generated at request time rather than anything written to disk or
committed to the repository. `make_handler()` accepts an additional
keyword-only `static_dir` parameter (default: the package's own
`service/static/`) so a test can point it at a directory with real
files without writing into the source tree.

**`make_handler()`/`serve.run()` accept parameters beyond their frozen
signatures.** `service.contracts.MakeHandler` is `(store, options) ->
type[BaseHTTPRequestHandler]`; `make_handler()` additionally accepts
two keyword-only parameters with defaults — `watcher_stats` (a
zero-argument callable returning the current `WatcherStats`, used by
`/api/health`) and `static_dir` (above) — which is still a valid
`MakeHandler` implementation (a Protocol callable is satisfied by
something that accepts extra optional parameters). Similarly,
`service.serve.run(options, *, once=False)` gains `allow_remote:
bool = False`: `ServeOptions` itself carries no such flag, but the
plan's "port bound to localhost only" default posture needs an
explicit opt-in for anything else, so `run()` refuses to bind a
non-loopback `options.bind` unless `allow_remote=True`. `service/serve.py`
also picks `<config_dir>/service.db` as the SQLite store's filename —
`ServeOptions` has no field for it, only `config_dir`.

**Watching a background-thread watcher's stats.** `service.contracts.Watcher`
documents `last_stats: WatcherStats | None` (S1-integration fix 1.e) as
a Protocol attribute every concrete `Watcher` keeps current — `FileWatcher`
sets it at the end of every `run_once()`, including the ones its own
background poll thread runs after `start()`. `/api/health` always has a
real answer: `serve.run()` passes `make_handler` a `watcher_stats`
callable that simply reads `watcher.last_stats`, no fallback guesswork
needed, since `run()` always calls `watcher.run_once()` synchronously
once before serving starts.
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

**A file Claude Code removed is marked, not deleted, in the store**
(review finding 3). `Store.remove_missing` notices its transcript is no
longer on disk and sets `transcripts.missing_since`; the row and its
`digest_json` are left alone, so `corpus_from_store` keeps including it
exactly like a transcript that is still there, and clears the mark again
if a file at the same path reappears. Only `--retention-days`/`serve
--purge` (see [docs/deploy.md](deploy.md)) actually delete a row — the
service store is designed to outlive Claude Code's own retention window,
not mirror it.

**Workflow runs round-trip (S1-integration fix 1.d).** The watcher
persists each `<session>/workflows/wf_*.json` run to a `workflow_runs`
table (`run_id`, `agent_count`, `phases` — phase *titles* only, never
`detail` — `started`, `finished`, `cost`, `status`), and
`corpus_from_store` reads it back into `SessionBundle.workflows`, so a
rebuilt report's `"workflows"`/`"phases"` sections and
`overview.workflow_runs` match a fresh parse. One approximation:
`WorkflowRun.phases` (an int count) is reconstructed as
`len(phase_titles)`, which can differ from a fresh parse's raw phase
count if some phase entries in the source JSON lack a `title` — an
accepted, documented trade-off (`service/schema.py`'s
`CREATE_WORKFLOW_RUNS` comment), not a privacy or correctness concern.

One field still does not survive the round trip, a store-schema gap
rather than a bug in `corpus_from_store` itself: **`SessionBundle.project_dir`**
is always the empty string once rebuilt from the store — nothing in
`report.build_report`'s own code path reads it, so this has no effect
on any route's output.
