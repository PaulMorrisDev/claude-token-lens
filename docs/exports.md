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
- **Project slugs are hashed by default, in every mode — including
  `--per-session`.** A repo/project slug is itself identifying
  information (it can name a client or an internal codename), so an
  export that still prints real slugs would leak exactly the kind of
  detail this tool is meant to protect, whether or not the rows are
  aggregated. Hashing uses a salted HMAC-SHA256 over the slug (domain-
  separated with a `slug:` tag, truncated to 12 hex characters), keyed
  by the same `<config_dir>/salt` file — and the same HMAC-SHA256
  construction, just a different truncation length and domain tag — as
  every other hashed value in this project (`parse.load_or_create_salt`,
  see [SECURITY.md](../SECURITY.md)). The same slug therefore hashes to
  the same value across every export and every report run against the
  same config dir, letting you correlate rows without ever seeing the
  real name. Pass `--no-hash-slugs` to opt out explicitly — see the next
  point for what that actually prints.
- **`--no-hash-slugs` is an explicit, informed opt-out, not a return to
  fully raw slugs.** It replaces just the OS-username segment of a slug
  (the part shaped like `Users-<name>-` / `home-<name>-`) with
  `<user>`, printing a one-line warning to stderr naming the risk. The
  rest of the slug (e.g. the project directory name) is still printed
  verbatim — this is a deliberate, narrower privacy floor under the
  opt-out, not a bug.
- **No text ever leaves in an export.** Every column is a count, a
  token total, or a cost — never a prompt, a tool result, or a file
  path. This is the same guarantee `report`'s own privacy scan enforces
  (see [SECURITY.md](../SECURITY.md)), just narrower in scope since an
  export has far fewer fields to begin with.
- **`--per-session` is an explicit opt-in.** Only pass it when you
  specifically need per-session drill-down (e.g. debugging one person's
  own usage with their consent) — it exposes `session_id`. Project slugs
  are still hashed by default in this mode too (see above).

### `--aggregate-only` / `--hash-slugs` resolution

| `--aggregate-only` / `--per-session` | `--hash-slugs` / `--no-hash-slugs` | Result |
| --- | --- | --- |
| unset (default) | unset (default) | aggregate-only, hashed slugs |
| `--aggregate-only` | unset | aggregate-only, hashed slugs |
| `--aggregate-only` | `--no-hash-slugs` | aggregate-only, **username-redacted** slugs |
| `--per-session` | unset | per-session, **hashed slugs** |
| `--per-session` | `--no-hash-slugs` | per-session, **username-redacted** slugs |

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
| `cache_write_tokens` | summed **total** cache-creation tokens (see the note below — this is the reconciliation-safe total, not just the sum of the two split columns) |
| `cache_read_tokens` | summed cache-read tokens |
| `output_tokens` | summed output tokens |
| `thinking_tokens` | summed thinking tokens |
| `cost` | summed cost (list-price equivalent USD under subscription billing, same convention as every other cost column in this project) |
| `recache_turns` | count of turns flagged as a RE-CACHE event (see [README section 5](../README.md#5-re-cache-definitions-and-signatures)) |
| `recache_cache_creation` | cache-creation tokens summed over just those RE-CACHE turns |
| `session_id` | (only with `--per-session`) the session's id |

**On older, pre-TTL-split transcripts** (recorded before Claude Code
split cache-creation tokens into separate 5m/1h counters), a turn can
carry a real, non-zero cache-creation total with `cache_write_5m_tokens`
and `cache_write_1h_tokens` both `0` — the split simply wasn't recorded
yet. Always use `cache_write_tokens` (not the sum of the two split
columns) when reconciling totals against another format or against
`report`'s own overview total; the two split columns are provided for
transcripts where the split *is* known, not as an alternate way to
recover the total. `--format otel-jsonl`'s `cacheCreation` data point and
`report`'s own `cache_creation_tokens` overview total both key off the
same underlying `cache_write_tokens`/`cache_creation_tokens` field, so
all three agree for the same corpus.

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
the tokens were attributed to. The `cacheCreation` point's value is the
same total `cache_creation_tokens` figure `csv-flat`'s `cache_write_tokens`
column and `report`'s overview totals use — see the note above.

**This is an offline approximation, not a live OTel exporter** — there is
no resource/scope metadata and no real collector transport, and the
timestamp is the day's start, not the moment the tokens were actually
used. It exists so an existing collector's dashboards built against those
metric names can ingest a claude-token-lens corpus after the fact. This
format carries no project/session dimension at all (the documented
metric names don't have one), so `--aggregate-only`/`--hash-slugs` have
no effect on it.

### `--aggregate` (team documents)

```bash
claude-token-lens export --aggregate --out my-machine.json
claude-token-lens export --aggregate --include-projects --out my-machine.json
```

`--aggregate` writes a different, fixed shape from every other
`--format`: a **team document** (`src/claude_token_lens/team.py`), built
for `claude-token-lens import`/`team-report` on a team lead's machine
rather than a BI pipeline. It is always JSON regardless of `--format`,
and `--aggregate-only`/`--per-session`/`--hash-slugs`/`--no-hash-slugs`
have no effect on it — a team document is aggregate-only and hashes
project slugs by construction, the same way `--format otel-jsonl`
ignores those flags for its own reason.

A team document carries:

- `tool_version`, `generated_at`, `window`.
- `machine_id` — a stable-but-non-reversible id for this machine: the
  first 12 hex characters of a salted HMAC-SHA256 over the machine's
  hostname (`platform.node()`), keyed by the same `<config_dir>/salt`
  file every other hashed value in this project uses, with its own
  `machine:` domain tag so its namespace can never collide with the
  project-slug namespace `_hash_slug` uses for `--hash-slugs` above.
  Stable across runs on the same machine and config dir; never reveals
  or reverses to the hostname.
- `by_archetype`, `by_mode`, `by_purpose`, `by_agent_type`, `by_model`
  — one row per group value on each axis: `sessions`, `priced_turns`,
  `tokens` (input/cache_creation/cache_read/output), `cost_usd`,
  `recache_share_pct`, `compaction_rate`, `ttl_mix` (`5m_pct`/`1h_pct`),
  `mean_spawn_write`, `mean_report_size`. Never a session id, never a
  slug.
- `scorecard` — the corpus-wide scorecard level (1-5) per dimension,
  read from the same `scorecard` section every report renders (never
  independently recomputed).
- `projects` — present **only** with `--include-projects`: a sorted
  list of hashed project slugs (the same `_hash_slug` construction
  `--hash-slugs` uses above), never the plaintext slug. Omitted by
  default — this is opt-in per person, on top of the aggregate's
  already-hashed-or-absent posture.

See [docs/team.md](team.md) for the full `export --aggregate` ->
`import` -> `team-report` flow, and the README's "For team leads"
section for the guarantees in one place.

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
   usage log is available). `cache_ground_truth` is scoped to sessions
   attributed to the reported month, the same window-scoping this
   report already applies to every other table — a usage-log row logged
   for a session outside the month never leaks into an unrelated
   report, the same way `report`'s own `--days`/`--since`/`--until`
   scoping keeps `cache_ground_truth` bounded to the requested window
   there.

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

The default month (when `--month` is omitted) is the previous calendar
month relative to *now in `config.tz`* — not the machine's own local
zone. On the 1st of a month, a machine whose own zone is ahead of
`config.tz` would otherwise silently resolve to the wrong month.

**An empty target month is not an error.** If no session is attributed
to the requested (or defaulted) month, `monthly-report` still writes
both files with zeroed finance tables and exits `0` — a reasonable
choice for an unattended scheduled job, which should not fail just
because nothing happened that month — but prints a one-line note to
stderr saying so, so the asymmetry with "no sessions found at all under
the given project root(s)" (which does exit non-zero) is visible rather
than silent.

### Idempotency

The same `(corpus, pricing, config, month)` produces `.md`/`.html` files
that are identical apart from a single trailing "Generated at: ..."
line (Markdown) or HTML comment immediately before `</body>` (HTML) —
strip that one line/comment before diffing two runs if you need a
byte-for-byte comparison without pinning `generated_at`.

For a **genuinely** byte-identical run — no stripping needed — pass a
fixed `--generated-at` (ISO 8601) or set `SOURCE_DATE_EPOCH`, the same
reproducible-build convention `export` already offers. This makes it
safe to schedule (cron, a CI job, `serve`'s own scheduler once it
exists) with a deterministic timestamp (e.g. the run's own scheduled
time) without producing spurious diffs even at the byte level.

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

## Statusline payload key recording (`statusline-keys.json`)

Several of the field-name fallback chains this module and `statusline.py`
implement (see the `context_window` fallbacks above, and the
`prompt_cache` ground-truth fields `statusline.py`'s own module
docstring documents) are this project's own best reconciliation of
partially-overlapping, undocumented field-name lists — not a restatement
of a single published contract. To make the *real* payload shape ground
truth for future releases rather than relying on research captures going
stale, every statusline invocation now writes the payload's own key
names — recursively, dotted (e.g. `context_window.used_tokens`), **names
only, never values**, capped at 200 names — to
`<config_dir>/statusline-keys.json`, and only rewrites that file when the
recorded key set actually differs from what a real invocation just saw.
This file contains no prompt text, no token counts, no paths, and no
usernames — only the shape of the payload, which is safe to attach to a
bug report or commit into a fixture corpus. See also
[SECURITY.md](../SECURITY.md).
