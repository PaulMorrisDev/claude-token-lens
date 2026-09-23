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

`error.code` is a short, stable, machine-matchable string:
`bad_request` (`400`), `forbidden` (`403`), `not_found` (`404`),
`method_not_allowed` (`405`), `conflict` (`409`) or `internal_error`
(`500`). `error.message` is a one-line human-readable explanation. The
HTTP status code carries the same information for clients that don't
want to parse the body: `200` for `ok: true` on every route except
`POST /api/profiles` and `POST /api/profiles/from-current`, which are
`201` on success. A managed setting is never an error code; see
"Managed-settings routes" below. This is exactly
`service.contracts.ApiError.to_envelope()`'s shape.

## Security headers

Every response from every route carries the same three headers
regardless of method or outcome (`api.py`'s `_SECURITY_HEADERS`,
written once and applied by the single `_write_headers` helper every
response path goes through — including a `404`/`405`/`500` error and a
static-file response, not just a successful `{"ok": true, ...}` one):

- `Cache-Control: no-store` — nothing served here (including a session's
  cost/usage figures) should ever be cached by an intermediary or the
  browser's own disk cache.
- `X-Content-Type-Options: nosniff` — stops a browser from
  MIME-sniffing a JSON or static-asset response into something else.
- `Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'`
  — matches the UI's own "no CDN, no external reference" constraint
  (`docs/ui.md`): nothing may load from another origin, inline `<img>`
  data URIs are allowed (the inline-SVG charts), and inline `<style>`
  is allowed (the UI's static `app.css` plus small inline style
  attributes) but inline `<script>` is not.

Every request method is routed through this same path: `GET`/`HEAD`
succeed or fail through the normal envelope, and `PUT`/`DELETE`/
`PATCH`/`OPTIONS` (nothing in this API accepts them) return a `405`
`method_not_allowed` error built the same way, with the same headers —
never a bare stdlib error page (review finding 9).

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

The context-file routes are the one deliberate exception.
`/api/claude-md`, `/api/claude-md/<id>` and `/api/skills` return the
files' paths with your home folder written as `~`, short excerpts of
CLAUDE.md text (repeated lines) and skill descriptions. That text is
read from disk (or, for skill descriptions, the newest transcript's
skill listing) when the request arrives and is never stored. It is the
content of your own instruction files, never a message or tool
result.

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
`--once` (see `--help`) and `--allowed-host` (see "Host allowlist"
below):

- **`--billing-mode {api,subscription}`** (S1-integration fix 1.a) is
  stamped onto every session's `billing_mode` field (see `/api/sessions`
  below). Defaults to `<config-dir>/config.toml`'s own `billing` setting
  when omitted (itself `"auto"` by default, resolved at start-up by
  `config.resolve_billing`: `"subscription"` when `usage-log.csv` holds
  a usage-limit reading, `"api"` otherwise),
  so a subscription user only has to say so once, in one place, rather
  than on every `serve` invocation.
- **`--monthly-report DIR`** sets `ServeOptions.monthly_report_dir`.
  Nothing in the service reads that field yet, so `serve` writes no
  monthly report today. Run `claude-token-lens monthly-report --out DIR`
  instead ([docs/exports.md](exports.md#claude-token-lens-monthly-report)).
- **`--purge`** deletes `<config-dir>/service.db` and its `-wal`/`-shm`
  sidecars and exits (S1-integration fix 2.e) — never starts the watcher
  or the API. Always prints exactly which files it would delete first;
  only actually deletes them when `--yes` is also given. Safe at any
  time: the store is always a derived cache (`service/store.py`'s module
  docstring), so the next `serve` run simply rebuilds it from the
  transcripts already on disk. Exits `2` (and deletes nothing) if
  `--yes` is missing, `0` otherwise (including when there is nothing to
  delete).

## Host allowlist (DNS rebinding)

Every request's `Host` header is checked before routing. A page on
another site that re-points its own domain name at `127.0.0.1` (DNS
rebinding) reaches this server as "same origin", but its requests still
carry `Host: <that domain>`, so they get `403 forbidden` on every
`GET`, `HEAD` and `POST` route, the static files included.

Allowed names: `127.0.0.1`, `localhost`, `::1`, the `--bind` address
when it is a specific address (not `0.0.0.0`/`::`), and each
`serve --allowed-host NAME` (repeatable, `ServeOptions.allowed_hosts`).
The port is ignored, so a container published on another host port
still works. A request with no `Host` header at all (HTTP/1.0, never a
browser) is allowed.

## Cross-site protection (review S3)

The `POST` routes below are mutating (all but `POST /api/whatif`), and
— without a same-origin check — a `Content-Type: text/plain` POST is a preflight-free "simple"
cross-site request a browser will send blind. The response is opaque to
a cross-site attacker (no CORS headers are ever sent, so it can't read
`ok`/`data` back), but a profile written this way is exactly what
`apply` later reads and acts on, so the write itself is the risk, not
exfiltration. Every `POST` request, `POST /api/whatif` included, is
checked before its body is even parsed:

- **`Content-Type` must be `application/json`** (a parameter such as
  `; charset=utf-8` is ignored) — `400 bad_request` otherwise. This
  alone forces a real browser to preflight the request, which this
  service already fails for a cross-origin caller (no
  `Access-Control-Allow-Origin` is ever sent).
- **`Origin`, when the request carries one, must match this server's own
  `Host`** (compared as `http://<Host>` — this service is `http`-only) —
  `403 forbidden` otherwise.
- **`Sec-Fetch-Site`, when the request carries one, must be
  `same-origin` or `none`** — `403 forbidden` otherwise.

A request carrying neither `Origin` nor `Sec-Fetch-Site` (e.g. a
same-machine CLI tool such as `curl`) is allowed — this API has no
authentication of its own (see "Local only" above), so that posture is
unchanged; the guard targets a *browser* silently issuing the request on
a victim's behalf, not a deliberate local caller. `service/static/app.js`
sends `Content-Type: application/json` on every one of its own `POST`
calls, so the UI itself is unaffected. A `POST` whose `Host` is not on
the allowlist above is also `403 forbidden`.

## Routes

All `GET` routes accept query-string parameters; all `POST` routes
accept a JSON request body (`Content-Type: application/json`, now
enforced — see "Cross-site protection" above). A route not listed here
returns `404` with `error.code: "not_found"`.

### `GET /api/health`

Liveness/diagnostics probe (also the Docker healthcheck target — plan:
"healthcheck on `/api/health`"). Never fails once the process is up.

`data`: `{"status": "ok", "schema_version": int, "transcripts_missing": int, "watcher": WatcherStats-as-dict, "service_registered": true|false|null}`.

`service_registered` (v3) is whether `serve` is currently registered to
start at logon/boot (`claude-token-lens install-service` — see
[docs/deploy.md](deploy.md)): `true`/`false` when the platform's own
query command (`schtasks`/`systemctl --user is-enabled`/`launchctl
print`) ran and gave a clear answer, `null` when it couldn't be run at
all (no probe wired up — e.g. `serve --once` — an unsupported platform,
or the query tool itself missing). `null` always means "unknown", never
"not registered". Computed at most once every ten minutes and cached
in-process — the probe shells out to a real system command, so a UI
polling `/api/health` doesn't spawn one on every refresh.

`transcripts_missing` (review finding 3) is `Store.count_missing_transcripts()`
— the current count of transcript rows whose backing file the watcher
can no longer find on disk. A transcript in this state is *marked*, not
deleted: its `digest_blob` keeps serving `report.*`/rebuild until it is
actually removed by `--retention-days`/`serve --purge` (see "Retention
and purge" in [docs/deploy.md](deploy.md)). This is also why a report
can still include a session whose transcript file Claude Code's own
`cleanupPeriodDays` retention has already removed — see "Store rebuild"
below.

`watcher` (S1-perf) additionally carries a per-tick timing breakdown of
its own `duration_s`: `discovery_s` (filesystem walk + diffing against
the store's known files), `parse_s` (cache lookups, on-miss parsing,
and the parallel bulk-prewarm pool's own wall-clock time — see
"Performance" in [docs/deploy.md](deploy.md)) and `store_s` (every
SQLite reader/writer call the tick made). The three don't sum to
exactly `duration_s` — session/workflow folding and fixed per-tick
overhead are counted in none of them — but each is a real,
non-overlapping measurement of its own phase, so a slow tick's
dominant cost is visible here rather than only as one opaque total.
See `service/contracts.py`'s `WatcherStats` for the exact field list.

### `GET /api/summary`

Corpus-wide totals — `Store.summary`.

Query: `window_days` (int, optional; no default, so all time when
omitted) or `window` (a named window, as for the report-backed routes
below; it takes precedence, and `window_days` is then `null` in the
response).

`data`: `{"window_days": int|null, "sessions": int, "transcripts": int, "total_cost": float, "total_tokens": int}`.
`total_cost` is at list price, whatever the billing mode.

With `window_days` given, a session qualifies for the window by its
*top-level transcript's* `mtime` — the same `window_by="mtime"` rule
`discovery.find_sessions`/`corpus.load_corpus`/
`service.rebuild.corpus_from_store` already share — and every
transcript belonging to a qualifying session (top-level and every
subagent) counts once the session itself qualifies. This is the same
windowing the CLI's `report` overview section uses, so
`sessions`/`transcripts` here always agree with a fresh
`report --days <window_days>`'s own `sessions`/
`top_level_transcripts + subagent_transcripts` totals for the identical
window (v0.3 fix — this route used to window `sessions` by the session
row's own `last_ts` and never window `transcripts` at all).

### `GET /api/sessions`

Recent sessions — `Store.sessions`.

Query: `limit` (default 50), `offset` (default 0). Newest first (by
`first_ts`); no window.

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
  set) preceded that turn. Always computed from every priced turn, never
  thinned by the downsampling below.
- `truncated`: `bool` (review finding 11) — `true` when the session has
  more than `Store.MAX_TURN_SERIES_POINTS` (5,000) priced turns and
  `turn_series` above was downsampled to that cap (every marker turn is
  kept; the rest are evenly sampled across the full session). `false`
  for every session at or under the cap.
- `limit_markers` (v3-limits wiring): `[{"ts", "kind", "detail"}, ...]`
  — every `LIMIT_HIT`/`LIMIT_RESUME`/`AGENT_TERMINATED` event on this
  session's top-level transcript, sourced from `limits.limit_markers`
  (see [`docs/limits.md`](limits.md#session-timeline-marker-contract)
  for the exact contract) and never downsampled. `kind` is one of
  `"limit_hit"`, `"limit_resume"`, `"agent_terminated"`; `detail` carries
  a `subkind` key when the event has one (`session_limit`/`weekly_limit`
  for `limit_hit`; `rate_limit`/`other` for `agent_terminated`).

All four fields are omitted entirely (never present as an empty list)
when no top-level transcript digest is stored yet, or the stored digest
can't be decoded — never fabricated.

### `GET /api/session/<id>/explain`

"Why was this session expensive?" for the session detail view
(`service/explain.py`). Every sentence is a fixed template filled with
this session's own aggregates from `Store.session_parts`; no model is
asked. `404` if `<id>` is unknown.

`data`: `{"session_id": str, "headline": str, "sentences": [str, ...], "cost_split": [{"part", "label", "cost", "share_pct"}, ...]}`.

- `headline`: the session's cost (in the billing mode's units), replies
  and tokens.
- `sentences`: how it compares with your median session, which part of
  the cost led and what that means, how much went on subagents and the
  costliest agent type, cache rebuilds and their commonest cause, and
  conversation summaries. A sentence is left out when its data is.
- `cost_split`: `part` is `cache_read`, `cache_write`, `output` or
  `input`, always in that order. `cost` is at list price from the rate
  card, whatever the billing mode; models the rate card doesn't know
  are left out.

### `GET /api/recache`

Corpus-wide RE-CACHE breakdown — `Store.recache`.

`data`: `{"by_signature": {"full-expiry": {"turns", "cache_creation_tokens"}, "prefix-invalidated": {...}, "limit-expiry": {...}}}`.
A signature with no rebuilds is absent, not zero. Always all history:
this route takes no window.

### Daily usage: `GET /api/daily-usage`

Per-day, per-model token and cost totals — `Store.daily_usage`. The
dashboard does not call this route (so its heading is not in the
`GET /api/...` form `tests/test_service_static.py` checks against
`app.js`); it is here for other clients.

Query: `days` (int, default 30, at least 1). Days are UTC calendar days.

`data`: `[{"day", "model", "turns", "input_tokens", "cache_creation_tokens", "cache_read_tokens", "output_tokens", "thinking_tokens", "cc_5m", "cc_1h", "cost"}, ...]`,
ordered by day, then model. `cost` is at list price.

### Report-backed routes: windowing query params

`/api/ttl`, `/api/carry`, `/api/compaction-sim`, `/api/model-swap`,
`/api/waste`, `/api/config-diff`, `/api/recommendations`,
`/api/diagnostics`, `/api/claude-md`, `/api/claude-md/<id>`,
`/api/skills`, `/api/profile-goals` (with `goal`), `/api/quick-actions`,
`/api/quick-actions/<id>`, `POST /api/whatif` and
`/api/report.md`/`.html`/`.json` (below) all accept the same windowing
query params, mirroring the CLI `report` subcommand's own
`--days`/`--since`/`--until` (`discovery._resolve_window`'s exact
resolution). `/api/summary` accepts `window` and `window_days` only.
Every other route (`/api/health`, `/api/sessions`, `/api/session/<id>`,
`/api/recache`, `/api/compactions`, `/api/baseline`, `/api/profiles*`,
`/api/impact`, `/api/setup`) ignores them.

- **`window`** (optional) — a named window, used by the dashboard's
  header picker: `1h` (the last hour), `today` (since midnight in
  `config.toml`'s `tz`, else the machine's zone), `24h`, `change` (since
  your latest `apply`, its undo, or a settings change the config hook
  saw; `400` when none is recorded yet) or `all` (no limit). Anything
  else is `400`. A named window takes precedence over the other three
  params. It is turned into a `since` rounded down to the minute, so
  repeat requests share one cached report. A session counts when its
  main transcript was last written inside the window (so it was active
  then), and it then counts in full.
- **`window_days`** (int, at least 1, optional) — the last N days;
  defaults to 30 when neither `since` nor `until` is given.
- **`since`** / **`until`** (ISO 8601, optional) — when either is
  present, `window_days` is *not* defaulted to 30 (matching the CLI's
  own `--days`/`--since` mutually-exclusive argparse group), so a
  `since`/`until` request windows the report exactly the way
  `report --since ... --until ...` does rather than being silently
  additionally clamped to the last 30 days. A malformed `since`/`until`
  is a `400 bad_request`. `report.meta.window` in the response is
  rendered identically to the CLI's own `_window_description` (`"since
  <since> until <until>"`, `"since the beginning until <until>"`, etc.)
  so the two are byte-equivalent for the same window, not just
  numerically equal.

### `GET /api/ttl`

TTL simulation summary (per agent type: observed vs. simulated 5m/1h
cost, fidelity, recommendation) — same shape as the CLI's `ttl` section
tables (`render/json_out.py`'s `Section`/`Table` encoding), sourced by
re-running `ttl.py`'s simulation over the store's `turns_agg`/
`recache_turns` rows rather than a fresh parse.

Query: `window`, `window_days`, or `since`/`until` (see "Report-backed
routes: windowing query params" above).

### `GET /api/carry`

Context carry-cost summary (per tool and per agent type: carried-result
count, tokens entered, mean turns carried, carry tokens/cost, cache-volume
share; plus the top individually-carried results and the truncation-cap
savings table) — same shape as the CLI's `carry` section tables, sourced
from the assembled report's `"carry"` section (`carry.py`).

Query: `window`, `window_days`, or `since`/`until` (see "Report-backed
routes: windowing query params" above).

### `GET /api/compaction-sim`

`autoCompactWindow` sweep summary (per candidate window: simulated
compactions/session, mean ctx, total cost and delta vs. observed; plus
the per-agent-type best window and the fidelity check against each
session's actually-configured window) — same shape as the CLI's
`compaction-sim` section tables, sourced from the assembled report's
`"compaction_sim"` section (`compaction_sim.py`).

Query: `window`, `window_days`, or `since`/`until` (see "Report-backed
routes: windowing query params" above).

### `GET /api/model-swap`

Model-swap counterfactual summary (per agent type: observed cost, cost
at every model the rate card carries, the best cheaper alternative and
the ceiling saving; plus the corpus-wide summary if every eligible
Fable/Opus subagent type moved one tier down) — same shape as the CLI's
`model-swap` section tables, sourced from the assembled report's
`"model_swap"` section (`model_swap.py`).

Query: `window`, `window_days`, or `since`/`until` (see "Report-backed
routes: windowing query params" above).

### `GET /api/waste`

Wasted-turn spend summary (total wasted turns/cost and their share of
the corpus, a per-cause breakdown with each cause's lever, a
per-agent-type roll-up, and the top wasted-cost sessions by a salted
session hash) — same shape as the CLI's `waste` section tables, sourced
from the assembled report's `"waste"` section (`waste.py`).

Query: `window`, `window_days`, or `since`/`until` (see "Report-backed
routes: windowing query params" above).

### `GET /api/compactions`

Every recorded compaction — `Store.compactions`.

`data`: `[{"transcript_id", "ts", "pre_tokens", "post_tokens", "dropped_tokens", "trigger", "join_delta_s"}, ...]`.

### `GET /api/config-diff`

Effective-config comparison across projects (plan "Configuration
layers" section: `config_groups`/`config_drift`), computed from the
latest `snapshots` row per project.

Query: `key` (a specific settings key) or `auto_keys=1` (the whole
config section; `400` when neither is given), plus `window`/`window_days`/`since`/
`until` (see "Report-backed routes: windowing query params" above).
Mirrors the CLI's `config-diff` subcommand.

`data`: with `auto_keys=1`, a list of every `config` section table (the
dashboard's Config tab uses this); with `key`, that key's
`config-diff-<key>` `Table`, or `[]` when it didn't change in the
window.

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

Query: `window`, `window_days`, or `since`/`until` (see "Report-backed
routes: windowing query params" above).

`data`: `[{"id", "severity", "category", "title", "action", "lever", "scope", "evidence": [[label, value, source_table, row_key], ...], "agent_type", "why", "estimated_saving", "saving_basis", "changes": [{"target", "key", "agent", "value", "suggested", "note", "unconfirmed", "current", "new_agent_file"}, ...], "fixes": [{"key", "agent", "explainer": [[heading, text], ...], "command", "command_warning", "prompt"}, ...]}, ...]` —
exactly `render/json_out.py`'s existing `Recommendation` encoding.
`fixes` (from `fixes.py`) holds, per change, the six-part explainer, an
`apply --set ... --dry-run` command (`null` when the value needs
judgement) and a prompt for Claude.

### `GET /api/diagnostics`

The report's parse-quality counters (`ReportModel.diagnostics`) as one
plain-English `Table` — `helptext.diagnostics_table`. Used by the Data
quality tab.

Query: `window`, `window_days`, or `since`/`until` (see "Report-backed
routes: windowing query params" above).

`data`: a `Table` (`name: "data_quality"`). Each row is `[field, value,
meaning]`; `field` is the raw `Diagnostics` field name and
`value_labels` maps it to its display label. Dict counters are joined
into one `"key: count, ..."` string. The first row, `snapshot_hook`, is
`hook_health.check` on `<config-dir>/../settings.json`: whether a
SessionStart hook runs `snapshot-config.py`, whether its path exists,
and how long ago the last snapshot was taken. The second, `statusline`,
is `hook_health.statusline_check`: whether the statusline that records
usage limits is running, given where your sessions run. Both have the
value `working` or `needs attention`.

### `GET /api/profiles`

Every profile the service knows about (v0.3): the seven shipped
catalogue profiles (`profiles.catalogue`, package data — never a row in
the store) plus every user profile written under
`<config_dir>/profiles/*.toml` (`Store.profiles`, ingested by the
watcher's `_scan_profiles`), each tagged with which of the two it came
from.

`data`: `{"profiles": [{"id", "name", "source": "catalogue"|"user", "archetype": str|null, "for": [str, ...], "updated_at": str|null}, ...], "suggested_profile_id": str|null}`.

A catalogue entry's `archetype`/`for` come straight from its shipped
TOML document; a user entry never carries them (the `profiles` table
only indexes `id`/`name`/`updated_at` — no `toml_path`, never
API-returned). `updated_at` is `null` for a catalogue entry (nothing to
timestamp). `suggested_profile_id` is the latest recorded baseline's own
`suggested_profile` field (`null` if no baseline has been captured yet),
so the UI can mark that entry in the list without a second round trip.

### `GET /api/profile-schema`

Every key a profile may set, for the dashboard's profile form.

`data`: `{"settings": [Lever, ...], "agents": [Lever, ...], "env": [str, ...], "archetypes": [str, ...], "scopes": [{"key", "label"}, ...]}`,
where a `Lever` is `{"key", "label", "kind", "values", "min", "max", "description", "tradeoff"}`.
`kind` is `str`, `enum`, `int`, `bool` or `list[str]`; `values` is the
allowed list for an `enum` (else `null`), `min`/`max` the range for an
`int`. `label`, `description` and `tradeoff` come from
`fixes.LEVER_LABELS`/`fixes.SETTING_TEXT`, the same text the
recommendation explainers use. `env` lists the environment variable
names a profile may set (`profiles.schema.ENV_ALLOWLIST`).

### `GET /api/profiles/<id>`

One profile's contents. `404` if `<id>` names neither a catalogue id
nor an existing `<config_dir>/profiles/<id>.toml`.

`data`: `{"id", "name", "source": "catalogue"|"user", "archetype", "for": [str, ...], "notes", "settings": {key: value}, "agents": {name: {key: value}}, "env": {NAME: value}, "setting_count": int}`.
`setting_count` counts settings, agent keys and environment variables
together.

### `GET /api/profiles/<id>/diff`

The real diff (v0.3, `profiles.diff.diff_against_effective`/
`render_unified_diff`) between profile `<id>` (a catalogue id or a user
profile written by `POST /api/profiles`) and the store's own *latest*
recorded config snapshot's effective config (`--dry-run` equivalent,
read-only — this route never writes anything, matching the plan's "the
service never calls `apply`; it renders the diff and the command"). A
store with no snapshot at all diffs against an empty effective config
(nothing currently set, nothing managed) and says so in `notes`, rather
than erroring. `404` if `<id>` names neither a catalogue id nor an
existing `<config_dir>/profiles/<id>.toml`.

Query: `scope` — one of `user` (default), `project-local`, `repo` (same
three scopes `profiles.diff`/`apply` use); `400` for anything else. This
route never accepts a client-supplied project directory — doing so
would put a raw filesystem path in the response body, which this
service's privacy rule forbids regardless of who supplied it — so a
`project-local`/`repo` scope's `apply_command` always omits
`--project-dir`; fill it in yourself when you run the command.

`data`: `{"profile_id": str, "scope": str, "diff": str, "settings": [DiffRow, ...], "agents": [DiffRow, ...], "env": [DiffRow, ...], "apply_command": str, "dry_run_command": str, "launch_command": str, "prompt": str, "notes": [str, ...]}`,
where a `DiffRow` is `{"key", "setting", "agent", "label", "description", "where", "current_value", "current_provenance", "proposed_value", "target_file", "managed"}`
(`profiles.diff.DiffRow`'s own fields, split by key prefix into the
three lists rather than left as one flat `rows` array — `settings.*` /
`agents.<name>.*` / `env.*`). The display fields: `setting` is the key
without its prefix (dotted agent keys such as `experimental.cacheTtl`
stay whole), `agent` the agent's name for an `agents.*` row (else
`null`), `label`/`description` its plain name and what it controls, and
`where` the file the change is written to under the chosen `scope`
(`fixes.profile_change_where`). `target_file` is where the key is set
today.

`apply_command`/`launch_command` are the two lines
`profiles.diff.apply_command` returns, split apart — the exact
host-side `claude-token-lens apply` invocation and the `--launch`
one-session-overlay alternative respectively. `dry_run_command` is
`apply_command` plus `--dry-run`, which the dashboard shows first.
`prompt` (`fixes.profile_prompt`) asks Claude to make the same changes
by hand: one line per changed, unmanaged key, naming the file and the
old and new values, and asking Claude to show the diff before saving.

"Latest snapshot" here and in `POST /api/profiles/from-current` means
the newest snapshot that records config: `apply` writes a
`{ts, schema_version, profile_id}` stamp into the snapshots folder to
mark the active profile, and the service skips those stamps
(`snapshots.records_config`) wherever it reads snapshots.

### `GET /api/baseline`

The latest stored baseline capture, its full history, and the
onboarding capture window's own status (v0.3,
`baseline.capture_status`/`format_capture_status`) — so the UI can mark
a baseline-derived suggestion as provisional while a capture window is
still open.

`data`: `{"baseline": Baseline|null, "history": [Baseline, ...], "capture_status": {"started": bool, "window_days": int|null, "elapsed_days": float|null, "remaining_days": float|null, "complete": bool, "summary": str}}`,
where a `Baseline` is `{"id", "project_slug", "window_start", "window_end", "archetype", "created_at", "record": dict|null}`
(`"record"` — only present on `"baseline"`, not on `history` entries —
is the captured baseline JSON record itself, already redacted the same
way `claude-token-lens baseline`'s own on-disk record is: no message
text, no raw paths, `projects` a list of already-redacted slugs).
`"baseline"` is `null` and `"history"` is `[]` when no baseline has ever
been captured. `capture_status.summary` is the same one-line status
`init`/`baseline` print to the terminal.

### `GET /api/quick-actions`

One answer per way of saving tokens (`quick_actions.CHECKS`): models,
effort, compaction, cache, tools, skills, claude-md, tool-output and
habits. Each check always answers, including "nothing to do".

Query: the windowing params above.

`data`: `{"period", "checks": [{"id", "question", "why", "status", "summary", "fix_count", "tip_count"}, ...]}`.
`period` is the window as a phrase ("over the last 30 days", "in the
last hour"). `status` is `act` (worth a look), `ok` (nothing to do) or
`no_data`.

### `GET /api/quick-actions/<id>`

One check in full. `404` for an unknown id.

Query: the windowing params above.

`data`: `{"id", "question", "why", "period", "status", "summary", "table": {"columns": [{"key", "label"}, ...], "rows": [[cell, ...], ...]}|null, "fixes": [Fix, ...], "tips": [{"title", "text"}, ...]}`,
where each row is a list of display values in column order, `table` is
`null` when there is nothing to show, and a `Fix` is the `fixes.py`
shape `/api/recommendations` uses, plus an optional `title`. Environment-variable fixes (`BASH_MAX_OUTPUT_LENGTH`,
`MAX_MCP_OUTPUT_TOKENS`) carry a prompt and no command: this tool never
writes the `env` block.

### `GET /api/claude-md`

Every CLAUDE.md-family file on disk (user, project, local, `.claude/rules`
and nested files seen in transcripts), with how often it was sent in the
window and what that cost. File text is read now and never stored.

Query: the windowing params above.

`data`: `{"period", "transcripts", "files": [{"id", "path", "name", "level", "project", "who", "tokens", "scoped", "sections", "seen", "sends", "reach", "reach_text", "cost_usd", "cost_text", "findings": [str, ...], "fix_count"}, ...]}`.
`id` is a 16-character hex hash of the path. `path` is the file's path
with your home folder written as `~` (`footprint.home_label`): the one
kind of path this API returns (see "Privacy" above).

### `GET /api/claude-md/<id>`

One file's sections, duplicates, stale references and fixes. `404` for
an unknown id, and for an id that is not 16 hex characters.

Query: the windowing params above.

`data`: `period`, the list entry, plus `section_rows` (`heading`,
`level`, `line`, `tokens`, `share` as a fraction of the file, `cost_text`,
`agents`), `imports` (paths, `~`-relative), `duplicates` (`line`,
`excerpt`, `tokens`, `also_in: [{"file", "line"}]`), `stale` (`line`,
`reference`, `kind`), `cost_by_reach` and `fixes`.

### `GET /api/skills`

Every skill Claude Code listed in the window: its description (read now
from the newest transcript's skill listing, never stored), where it
comes from, how often it was listed and used, and what the listing cost.

Query: the windowing params above.

`data`: `{"period", "skills": [{"name", "description", "source", "source_label", "path", "listing_tokens", "listed", "listed_text", "invoked", "invoked_by", "listing_cost_usd", "listing_cost_text", "use_cost_text", "use_text", "resent_tokens", "status", "fixes"}, ...], "listing_tokens", "listing_cost_text", "unused", "fixes"}`.
Skills come unused first, then by listing cost. `status` is `unused`,
`used`, `listed` or `not listed`. `path` is `~`-relative, or `""` when
the skill has no file on disk. Each skill's own `fixes` hide it or
shorten its description; the top-level `fixes` holds one change that
hides every unused skill at once, when there are two or more.

### `GET /api/profile-goals`

Without `goal`: `{"goals": [{"id", "title", "what"}, ...]}`, the goals a
profile can start from (`profiles/goals.py`): `recommendations`,
`subagents`, `models`, `cache`, `compaction`, `thinking` and `current`.
With `goal=<id>`: that goal's draft. An unknown goal is `400`.

Query: `goal`, plus the windowing params above (used only with `goal`).

`data` (with `goal`): `{"goal": {"id", "title", "what"}, "period", "from_current", "candidates": [{"key", "agent", "label", "now", "value", "ticked", "evidence", "what", "tradeoff", "note", "estimate"}, ...], "profile": {"settings", "agents"}, "whatif"}`.
A candidate is ticked only when the data supports it; the main model is
never pre-ticked. `estimate` is that one change's `POST /api/whatif`
row; `profile` holds the ticked changes and `whatif` their combined
estimate. `current` returns no candidates (`from_current: true`): the
dashboard saves your current settings with
`POST /api/profiles/from-current` instead.

### `GET /api/impact`

Each change you made (an `apply`, its undo, or a settings change the
config hook saw), with the sessions before it against those after it,
on the measures that change should move.

Takes no window: each change is compared over its own before and after
periods, looking back at most `lookback_days`.

`data`: `{"changes": [{"change": {"ts", "source", "label", "keys", "changes", "backup_ts", "reverted"}, "before_sessions", "after_sessions", "enough", "verdict", "measures": [{"label", "before", "after", "before_n", "after_n", "change_pct", "direction"}, ...]}, ...], "caveat", "min_sessions", "lookback_days"}`.
Newest change first, at most ten. `change.source` is `apply`, `revert`
or `config` (a settings change the hook saw). `enough` is false until each side has
`min_sessions` sessions. `before`/`after` are display text in the
billing mode's units; `direction` is `lower`, `higher`, `same` or
`null`. For an `apply` that is not yet undone, `backup_ts` is what
`claude-token-lens apply --revert <backup_ts>` takes.

### `GET /api/setup`

What this tool installed and changed on this machine, what each piece
costs in tokens and how to undo it, plus what to expect
(`footprint.py`). Used by the Data quality tab.

`data`: `{"items": [{"key", "title", "status", "where", "what_it_does", "token_cost", "undo"}, ...], "expectations": [{"title", "text"}, ...], "uninstall_command"}`.

### `GET /api/report.md` / `GET /api/report.html` / `GET /api/report.json`

The full report in each format, built from the store instead of a fresh
parse — byte-equivalent in content to running the CLI's `report`
subcommand with `--json`/`--html`/(default) over the same window,
modulo the "verified against CLI JSON" test the plan's Milestone v0.2
Tests bullet requires (`tests/test_service_api.py`, built alongside
`api.py`). All three return the raw rendered document on success, not
the envelope above: `report.json` as `application/json`, and
`report.md`/`report.html` as `text/markdown`/`text/html` (UTF-8),
matching the CLI's own stdout. Errors still use the JSON envelope.

Query: `window`, `window_days`, or `since`/`until` (see "Report-backed routes:
windowing query params" above) — this is what makes `/api/report.json?
since=...&until=...` byte-equivalent to `report --since ... --until
...`, not just to `report --days N`.

## Mutating routes

The `POST` routes. All but `POST /api/whatif` write something, each
scoped to a single row or file and never touching `~/.claude` proper
(plan: "neither touches `~/.claude` proper"). Nothing here changes
Claude Code's settings: a saved profile takes effect only when you run
the `apply` command or give Claude the prompt that
`GET /api/profiles/<id>/diff` returns.

### `POST /api/sessions/<id>/tags`

Body: `{"key": "mode"|"purpose", "value": str}`. Calls `Store.set_tag`
(the same override `config.sessions.toml` holds for the CLI). `404` if
`<id>` is unknown; `400` if the body is not a JSON object, `key` isn't
`mode`/`purpose` or `value` isn't a string (the cross-site checks above
run first — see "Cross-site protection"). The tag is merged into the
session overrides every report-backed route classifies sessions with,
taking precedence over `sessions.toml`.

`data`: `{"session_id": str, "tags": {key: value}}` (the session's full
tag set after the write).

### `POST /api/profiles`

Body: a profile document's JSON form (the same shape a TOML profile
round-trips to — `id`, optional `name`/`for`/`archetype`/`notes`,
optional `settings`/`agents`/`env` tables), validated by
`profiles.schema.load_dict` before anything is written. The
cross-site checks above run first (`403 forbidden` for a cross-site
request, `400 bad_request` for a wrong `Content-Type` — see "Cross-site
protection"). `400` (`error.code: "bad_request"`) if the body is not a
JSON object, or if the schema rejects an unknown key or
an out-of-range value — the schema's own problem text, joined with
`"; "` (plan: "the schema rejects anything else so a profile can never
promise an effect the harness cannot deliver"). `409`
(`error.code: "conflict"`) if `id` names one of the seven shipped
catalogue profiles — a catalogue id can never be created or overwritten
this way, regardless of `?replace=1` — or if a user profile with that
`id` already exists and `?replace=1` was not given.

On success, writes `<config_dir>/profiles/<id>.toml` atomically (temp
file + rename — never a half-written file) and re-ingests it into the
store immediately via `Store.upsert_profile`, so the very next
`GET /api/profiles` reflects the write without waiting for the
watcher's next tick.

Query: `replace` — `1` allows overwriting an existing *user* profile's
file (never a catalogue one).

`data`: `{"id": str, "name": str, "source": "user", "updated_at": str}` — `201` on success.

### `POST /api/profiles/from-current`

Saves your current settings as a user profile ("Save my current
settings as a profile" on the Profiles tab). It reads the latest config
snapshot's `effective` settings and `effective_agents`, keeps only the
keys a profile may set (each checked on its own with
`profiles.schema.validate`, so one out-of-range value drops only
itself), leaves out keys your organisation's managed settings control,
and writes the result exactly as `POST /api/profiles` does. It writes
only this tool's own profile folder, never Claude Code's config.

Body (optional): `{"id": str, "name": str}`. Defaults:
`my-current-settings` and "My current settings".

Query: `replace` — `1` overwrites an earlier save with the same `id`.
Without it, a second save is `409` (`error.code: "conflict"`, message
"... already exists ..."); the dashboard then asks before replacing.
`409` also when no config snapshot has been recorded yet.

`data`: the `POST /api/profiles` result plus `skipped_managed`: the
allowlisted setting names left out because managed settings control
them. `201` on success.

### `POST /api/whatif`

The estimated effect of a set of changes on the window, looked up in the
report's own tables (`whatif.py`). It writes nothing; it is a POST only
because the changes travel in the body. Behind the cross-site guard like
the other POST routes.

Body: `{"settings": {...}, "agents": {"<agent>": {...}}}`, checked with
`profiles.schema.validate` (`400` on a bad key or value, or when the
body, `settings` or `agents` is not a JSON object).

Query: the windowing params above.

`data`: `{"period", "rows": [{"key", "agent", "value", "saving_usd", "fidelity", "fidelity_text", "basis", "effect_text"}, ...], "total_usd", "total_text", "estimated", "not_estimated", "total_note"}`.
`saving_usd` is `null` when a change is not estimated. `fidelity` says
how it was worked out (`fidelity_text` in plain words) and `basis`
explains it in a sentence. `effect_text` and `total_text` are in the
billing mode's units; `estimated`/`not_estimated` are counts of rows.

## Managed-settings routes

Any route whose `data` would include a recommendation or a diff whose
`lever` targets a managed-settings key still returns `200`/`ok: true` —
the managed-ness is carried in the payload (`scope: "managed"`, per
`model.py`'s `Recommendation.scope`) rather than as an HTTP error, so
the UI can render "managed by policy, raise with your administrator"
inline (plan "Enterprise use"). No route writes a managed key into
Claude Code's config, because no route writes Claude Code's config at
all. `POST /api/profiles` may save a managed key into a profile file;
`apply` skips it when the profile is applied, and the profile diff
marks the row `managed`. `POST /api/profiles/from-current` leaves
managed keys out and lists them in `skipped_managed`.

## Report routes: how they are computed

Implementation notes for `service/api.py` (S1-api), for a future reader
of this frozen contract who needs to know how the report-backed routes
(`/api/ttl`, `/api/carry`, `/api/compaction-sim`, `/api/model-swap`,
`/api/waste`, `/api/config-diff`, `/api/recommendations`,
`/api/diagnostics`, `/api/claude-md*`, `/api/skills`,
`/api/profile-goals`, `/api/quick-actions*`, `POST /api/whatif`,
`/api/report.md`/`.html`/`.json`) get their data, and where the
implementation had to make a call this document didn't spell out.

**Rebuild, not re-parse.** Every report-backed route rebuilds a
`Corpus` via `service.rebuild.corpus_from_store(store, days=window_days,
since=since, until=until)` (S1-watcher's module — see
`service/__init__.py`) and runs it through the same
`report.build_report()` → `recommend.recommend()` →
`render/{json_out,markdown,html}.py` pipeline the CLI's own `report`
subcommand uses. `/api/ttl`, `/api/carry`, `/api/compaction-sim`,
`/api/model-swap`, `/api/waste`, `/api/config-diff` and
`/api/recommendations` all build the *same* full report for the
requested window (`window_days`, or `since`/`until` — see "Report-backed
routes: windowing query params" above) and read one section/field back
out of it
(`/api/ttl` returns the assembled report's `"ttl"` `Section`;
`/api/carry`, `/api/compaction-sim`, `/api/model-swap` and `/api/waste`
likewise each return their own like-named `Section` (`"carry"`,
`"compaction_sim"`, `"model_swap"`, `"waste"`) — all four `null` rather
than an error when the section is absent from the assembled report;
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

`_build_report_model` also passes `config_dir=options.config_dir` to
`build_report()` (v4 wiring round) so that `waste.py`'s salted
session-id hash reads/writes its salt file inside this service's own
`config_dir` rather than falling back to `report.py`'s
`_default_waste_config_dir()` OS-temp-directory default — the fallback
exists only for callers (tests, `baseline.py`, `team.py`) that never
had a `config_dir` of their own to give it.

**Memoization key: `Store.change_token()`.** Rebuilding a full report on
every request would make every tab switch in the UI (`docs/ui.md`)
re-parse the whole corpus. The implementation caches the assembled
`ReportModel` in-process, keyed by `(window_days, since, until,
change_token)` (a named `window` is first turned into its `since`,
rounded to the minute), where `change_token` is `Store.change_token()` (S1-integration fix 1.f) — a
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
A request error on one of these three routes (a bad `window_days`,
`since` or `until`, or an unexpected exception) still falls back to the
normal JSON error envelope; only the success path is raw.

**`GET /api/session/<id>` returns a superset of the listed fields.**
`Store.session()`'s dict includes `mode_source`/`purpose_source`
alongside every field `/api/sessions` lists — a non-breaking addition,
not a contradiction of the field list above (which describes the
session-summary fields plus `transcripts`/`tags`, not an exact field
count), and dropping fields `Store` already computes for no privacy
reason would only lose information a client might want.

**`/api/profiles/<id>/diff` and `POST /api/profiles` are real routes as
of v0.3**, no longer the `501 not_implemented` stubs an earlier version
of this document described at S1-api's own delivery time (before
`profiles/schema.py`/`profiles/diff.py` existed). See their own
sections above for the shipped shapes.

**`report.meta.projects` can differ from the CLI's for the identical
window (release-verification finding, accepted, not a bug to fix
here).** The CLI's `--all-projects` passes `build_report` every project
*directory it resolved on disk* (`discovery`'s own directory scan,
filtered by `--project-family`/`exclude_projects` but never by whether
that project has any sessions at all), while a report-backed route
derives `projects` from `{bundle.slug for bundle in corpus.sessions if
bundle.slug}` — only projects the store actually has session rows for.
A project directory that exists under `--projects-root` but has never
had a single parseable transcript in it (an empty/leftover directory —
confirmed against a real corpus during v0.2 release verification, e.g.
a stray directory containing no `.jsonl` files at all) shows up in the
CLI's `meta.projects` and never in the API's, for *any* window,
independent of `since`/`until`/`window_days`. Matching this exactly
would mean a report-backed route reading live directory names from
`options.projects_root` — a live-filesystem dependency the whole
store-rebuild design (`service/rebuild.py`'s module docstring) exists
to avoid, for one purely cosmetic field. Left as-is rather than
special-cased.

**Static file serving.** `/` and `/static/*` serve
`service/static/index.html`/assets (the UI package's build output,
per `docs/ui.md`) when present, guarded against path traversal
(`Path.resolve()` plus a parent-containment check — a `..` segment or
an escaping resolved path is `404`, not an error). When
`service/static/index.html` is missing or unreadable, `/` falls back to
a small, non-persisted placeholder page generated at request time
rather than anything written to disk or committed to the repository. `make_handler()` accepts an additional
keyword-only `static_dir` parameter (default: the package's own
`service/static/`) so a test can point it at a directory with real
files without writing into the source tree.

**`make_handler()`/`serve.run()` accept parameters beyond their frozen
signatures.** `service.contracts.MakeHandler` is `(store, options) ->
type[BaseHTTPRequestHandler]`; `make_handler()` additionally accepts
three keyword-only parameters with defaults — `watcher_stats` (a
zero-argument callable returning the current `WatcherStats`, used by
`/api/health`), `service_registered` (a zero-argument probe for
`/api/health`'s field of that name; omitted, it reports `null`) and
`static_dir` (above) — which is still a valid
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
`TranscriptResult` into `transcripts.digest_blob` (the same lossless
JSON encoding `cache.py`'s on-disk digest cache uses, zlib-compressed
before storage — S1-perf), and `corpus_from_store` decodes those
digests straight back into a `Corpus` shaped exactly as
`corpus.load_corpus` would have produced from the live files, so
`report.build_report(corpus, ...)` runs unmodified against either one.
`days`/`since`/`until`/`window_by` mirror `discovery.find_sessions`'s own
parameters and windowing semantics.

**A file Claude Code removed is marked, not deleted, in the store**
(review finding 3). `Store.remove_missing` notices its transcript is no
longer on disk and sets `transcripts.missing_since`; the row and its
`digest_blob` are left alone, so `corpus_from_store` keeps including it
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
