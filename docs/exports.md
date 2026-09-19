# Exports and monthly reports

S1-exports adds two ways to get a corpus's numbers into other tooling
without asking every team member to run claude-token-lens by hand: a
one-shot, privacy-safe `export` (`src/claude_token_lens/exports.py`) for
BI/observability pipelines, and a recurring `monthly-report`
(`src/claude_token_lens/monthly.py`) for a habit-forming finance summary.
Both are read-only over an already-loaded corpus — neither writes
anywhere except the file(s) you point them at.

## `claude-token-lens export`

```bash
claude-token-lens export --format csv-flat --out team-usage.csv
claude-token-lens export --format json --per-session --no-hash-slugs --out my-usage.json
claude-token-lens export --format otel-jsonl --out usage.otel.jsonl
```

### Privacy guarantees (for team leads)

- **Aggregate-only is the default.** Rows are grouped by
  `(day, project, model, entrypoint, agent_type)` — there is no
  `session_id` column unless you opt in with `--per-session`.
- **Project slugs are hashed by default whenever aggregate-only is in
  effect.** A repo/project slug is itself identifying information (it
  can name a client or an internal codename), so an "aggregate-only"
  export that still prints real slugs would leak exactly the kind of
  detail aggregation is meant to hide. Hashing uses the same salted
  `sha256(salt + slug)[:12]` construction, and the same
  `<config_dir>/salt` file, as every other hashed value in this project
  (`parse.load_or_create_salt`) — so the same slug hashes to the same
  value across every export and every report run against the same
  config dir, letting you correlate rows without ever seeing the real
  name. Pass `--no-hash-slugs` to keep raw slugs, even together with
  `--aggregate-only` — that combination is honoured as an explicit,
  informed choice, not the default.
- **No text ever leaves in an export.** Every column is a count, a
  token total, or a cost — never a prompt, a tool result, or a file
  path. This is the same guarantee `report`'s own privacy scan enforces
  (see [SECURITY.md](../SECURITY.md)), just narrower in scope since an
  export has far fewer fields to begin with.
- **`--per-session` is an explicit opt-in.** Only pass it when you
  specifically need per-session drill-down (e.g. debugging one person's
  own usage with their consent) — it exposes `session_id` and, unless
  you also pass `--hash-slugs`, real project slugs.

### `--aggregate-only` / `--hash-slugs` resolution

| `--aggregate-only` / `--per-session` | `--hash-slugs` / `--no-hash-slugs` | Result |
| --- | --- | --- |
| unset (default) | unset (default) | aggregate-only, hashed slugs |
| `--aggregate-only` | unset | aggregate-only, hashed slugs |
| `--aggregate-only` | `--no-hash-slugs` | aggregate-only, **raw** slugs |
| `--per-session` | unset | per-session, **raw** slugs |
| `--per-session` | `--hash-slugs` | per-session, hashed slugs |

### Row grain and columns (`csv-flat` / `json`)

One row per `(day, project, model, entrypoint, agent_type)`, plus a
trailing `session_id` column when `--per-session` is in effect. This
mirrors `usage.py`'s own day/project/entrypoint axes, plus `model` and
`agent_type` split out (rather than pre-summed away) since a BI import
usually wants those as separate dimensions.

| Column | Meaning |
| --- | --- |
| `day` | local calendar day (`YYYY-MM-DD`, in `config.tz` or the machine's own zone) |
| `project` | project slug, or its hash (see above) |
| `model` | model id as recorded on the turn |
| `entrypoint` | e.g. `claude-desktop`, `claude-code` |
| `agent_type` | the transcript's agent type, falling back to its `kind` (`top-level`/`subagent`/`workflow-agent`), or `unknown` |
| `turns` | priced turn count in this cell |
| `input_tokens` | summed input tokens |
| `cache_write_5m_tokens` / `cache_write_1h_tokens` | summed `ephemeral_5m`/`ephemeral_1h` cache-creation tokens |
| `cache_read_tokens` | summed cache-read tokens |
| `output_tokens` | summed output tokens |
| `thinking_tokens` | summed thinking tokens |
| `cost` | summed cost (list-price equivalent USD under subscription billing, same convention as every other cost column in this project) |
| `recache_turns` | count of turns flagged as a RE-CACHE event (see [README section 5](../README.md#5-re-cache-definitions-and-signatures)) |
| `recache_cache_creation` | cache-creation tokens summed over just those RE-CACHE turns |
| `session_id` | (only with `--per-session`) the session's id |

`--format csv-flat` writes these as plain, unformatted CSV (raw numbers,
no thousands separators or unit suffixes), matching `render/csv_out.py`'s
own convention. `--format json` writes `{"meta": {...}, "rows": [...]}`,
where `meta` carries `tool_version`, `window`, `pricing_version`,
`generated_at`, `hash_slugs`, and `aggregate_only`.

### `--format otel-jsonl`

One JSON line per `(day, model, token type)`, shaped like an
OpenTelemetry metric data point, using the metric names Claude Code's own
OTel integration documents: `claude_code.token.usage` (with an
`attributes.type` of `input`/`output`/`cacheRead`/`cacheCreation`, plus
`attributes.model`) and `claude_code.cost.usage` (`attributes.model`
only). `time_unix_nano` is the UTC instant of local-day start for the day
the tokens were attributed to.

**This is an offline approximation, not a live OTel exporter** — there is
no resource/scope metadata and no real collector transport, and the
timestamp is the day's start, not the moment the tokens were actually
used. It exists so an existing collector's dashboards built against those
metric names can ingest a claude-token-lens corpus after the fact. This
format carries no project/session dimension at all (the documented
metric names don't have one), so `--aggregate-only`/`--hash-slugs` have
no effect on it.

## `claude-token-lens monthly-report`

```bash
claude-token-lens monthly-report --out ./monthly-reports
claude-token-lens monthly-report --out ./monthly-reports --month 2026-08
```

Writes `DIR/claude-token-lens-YYYY-MM.md` and the matching `.html` for
one calendar month (default: the previous calendar month relative to
today). The body is deliberately just a finance summary, not the full
multi-section `report` output:

1. **Finance summary** — total cost, total tokens, session count, and
   (subscription billing only) five-hour blocks used.
2. **Cost by model**, **Cost by project**, **Cost by entrypoint** —
   regrouped from the `usage` section's own `by_month`/`by_project`/
   `by_entrypoint` tables.
3. The full `usage` section's own tables (by day/week/month, by project,
   by entrypoint, five-hour blocks, and `cache_ground_truth` when a
   usage log is available).

Recache/TTL/compaction/topology and the other optimisation-focused
sections are out of scope for this report — it is a finance artefact,
not a tuning one; use `report`/`ttl`/`recache`/`compactions` for those.

### Month attribution

A session is attributed to the calendar month of its **first** top-level
turn's local timestamp — not sliced per turn. A session whose turns
straddle a month boundary is therefore counted wholly in the month it
started. This is a documented approximation, the same kind `usage.py`'s
own five-hour-block grid already accepts, to avoid a much larger rewrite
of every other analytics module's own per-session assumptions.

### Idempotency

The same `(corpus, pricing, config, month)` always produces
byte-identical `.md`/`.html` files across repeated runs, so it is safe to
schedule (cron, a CI job, `serve`'s own scheduler once it exists) without
producing spurious diffs. The only wall-clock value, a "Generated at:
..." line, is isolated to the last line of the Markdown file and to an
HTML comment immediately before `</body>` of the HTML file — strip that
one line/comment before diffing two runs if you need a byte-for-byte
comparison.

### Service entry point (v0.2 `serve --monthly-report DIR`)

`monthly.write_monthly_report(corpus, pricing, config, month, out_dir) ->
list[Path]` is the function the v0.2 service package wires up to its own
`serve --monthly-report DIR` flag: it takes an already-loaded
`corpus`/`pricing`/`config` rather than loading them itself (matching
`report.build_report`'s own "caller loads, this function only assembles"
contract), resolves the month with `monthly.resolve_month` if the caller
doesn't already have one, and returns the two paths it wrote (Markdown
first, then HTML) so the caller can log or serve them without having to
reconstruct the filenames itself.
