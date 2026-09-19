# claude-token-lens

Config-aware token and prompt-cache analytics for Claude Code transcripts:
where your tokens go, what your caching configuration costs or saves, and
which configuration changes to make. Stdlib-only, MIT-licensed, runs
entirely on your own machine.

**Status: pre-release, v0.3 in progress.** The parsing, pricing, RE-CACHE,
TTL, classification, compaction, config-snapshot, topology, workstyle,
workflow, phase-split, usage, report-assembly, recommendation, scorecard,
onboarding and baseline-capture engines are all implemented and covered
by tests. The command-line surface now matches: `report`, `sessions`,
`recache`, `ttl`, `compactions`, `config-diff`, `log-usage`,
`pricing-check`, `scrub-fixture`, `probe`, `statusline`,
`snapshot-config`, `init` and `baseline` are real subcommands backed by
that engine — see [section 2](#2-quick-start) for the full flag
reference and [`docs/onboarding.md`](docs/onboarding.md) for `init`/
`baseline` specifically. `serve` remains a registered stub that prints
which future milestone it's planned for and exits 2 (see the roadmap in
[section 13](#13-licence-contributing-roadmap)). This README describes
what the code actually does today, not the full plan — see
[`docs/sections-reference.md`](docs/sections-reference.md) for
section-by-section detail and this file's own notes on what's still
missing.

A few things are also usable directly, outside the `report` command:

- `python -m claude_token_lens.tools.scrub` — turn a real transcript into
  a privacy-scrubbed fixture (see [Privacy and security](#11-privacy-and-security)).
- `python -m claude_token_lens.statusline` — a live Claude Code status
  line (see [Installing the hook and statusline](#8-installing-the-sessionstart-hook-and-the-statusline)).
- `python -m claude_token_lens.tools.log_usage` — append a `get_usage`
  paste to a local CSV log.
- Every analytics module (`recache`, `ttl`, `classify`, `compaction`,
  `snapshots`, `topology`, `workstyle`, `workflows`, `phases`, `usage`,
  `scorecard`, `recommend`, `pricing`) can also be called directly from a
  Python shell or a short script against your own transcripts, if you
  want one section or one recommendation in isolation rather than the
  full report; that is how the worked examples in this README were
  produced.

## 1. What it is, what it measures, and what it cannot

claude-token-lens reads Claude Code's local JSONL transcripts
(`~/.claude/projects/<slug>/<session>.jsonl`, plus
`<session>/subagents/agent-*.jsonl` and `<session>/workflows/wf_*.json`),
groups assistant lines into priced turns, and turns them into token,
cache and cost analytics. It never sends anything anywhere: no network
calls, no telemetry, no update check.

What it cannot do:

- **There is no billing API.** Anthropic does not publish a way to read
  back what a Claude Code session actually cost. Every dollar figure
  this tool prints is computed from a rate card **you** supply and edit
  in [`pricing.toml`](src/claude_token_lens/pricing.toml) — it is never
  fetched, and the tool has no code path that could fetch it.
- **Subscription users are not billed in USD per token.** If you run
  Claude Code on a subscription within your plan's usage window, the
  binding constraint is the 5-hour/7-day usage window, not a dollar
  total — and a 1-hour prompt-cache TTL on a subagent is documented as
  being ignored while you're on usage credits. `pricing.toml`-derived
  money columns for a subscription account are a **list-price
  equivalent**: a useful way to compare two configurations against each
  other, not a real invoice. `ReportMeta.billing_mode` and
  `Config.billing` model `"api"` vs `"subscription"` in
  [`model.py`](src/claude_token_lens/model.py) and
  [`config.py`](src/claude_token_lens/config.py), and the report is
  billing-mode aware: the Usage section's "Usage by day/week/month"
  tables label subscription money columns as list-price-equivalent, and
  its five-hour usage blocks table is only populated for
  `billing = "subscription"` — under `"api"` it prints a one-line note
  explaining the skip instead (see [section 6](#6-reading-the-report-sections)).
- **The JSONL format is observed, not a published API.** Every field
  name this tool reads was found by inspecting real transcripts, not
  from documentation, and Claude Code's own docs describe the transcript
  format as internal and unstable. The parser tolerates unknown line
  types and fields rather than failing on them (see
  [Windows notes](#9-windows-notes) and `Diagnostics.ignored_line_types`),
  but a future Claude Code release can still change field names under
  it. Two integration points *are* documented and stable, and are the
  better choice if you need a durable contract instead of a
  best-effort parser: Claude Code's OpenTelemetry metrics
  (`CLAUDE_CODE_ENABLE_TELEMETRY=1`, metrics `claude_code.token.usage`
  and `claude_code.cost.usage`), and `claude -p --output-format json`
  for scripted single-shot invocations.

## 2. Quick start

```bash
pip install .
# or, isolated from your other Python environments:
pipx install .
```

Then, from the project you want to analyse:

```bash
claude-token-lens init
```

`init` is the fastest way to a first report. It detects what's already
on your machine (existing config, config snapshots, a usage log), asks
a handful of short questions it genuinely can't infer on its own
(billing mode, any projects to always exclude, whether you launch
Claude Code with shared settings overlays, your timezone, a default
profile-apply scope, and how long to run its onboarding "capture
window" for — 7 days by default), writes `config.toml`, offers the
SessionStart hook and statusline `settings.json` fragments to install
(skip with `--no-install`), and kicks off that capture window with an
initial baseline for the current project. Answering non-interactively
(e.g. in a script or CI) is supported too:

```bash
claude-token-lens init --non-interactive --no-install
```

— any question not answered from an `--answers FILE` is derived from
what `init` detected, and `init` prints exactly what it derived and
why, rather than guessing silently.

Once the capture window has enough data (or immediately with
`--finalise`), run:

```bash
claude-token-lens baseline
```

to get a suggested workstyle profile, a projected caching saving, and
a few concrete next steps — see [`docs/onboarding.md`](docs/onboarding.md)
for the full question set, the baseline record's fields, and the
Markdown report's shape. `claude-token-lens baseline --list` /
`--show ID` read back a previously captured baseline without
recapturing anything.

Prefer to skip setup entirely? `claude-token-lens report` (the default
subcommand — see below) works standalone, with no `init` step at all.

Because the package has no third-party dependencies (`dependencies = []`
in `pyproject.toml`; `rich` is an optional extra), it can also be built
into a single-file, dependency-free `.pyz` with the standard library's
own `zipapp` module — useful on a locked-down machine that only has a
bare Python 3.11+ interpreter:

```bash
python -m zipapp src -m "claude_token_lens.__main__:main" -o claude-token-lens.pyz
```

Windows:

```powershell
py -3 claude-token-lens.pyz report
```

This build step is not yet wired into CI or attached to GitHub releases
— run it yourself for now. `report` above runs the real report (see the
Status note) because it's the default subcommand.

**Zipapp exit codes.** Point `zipapp -m` at `claude_token_lens.__main__:main`,
not at `claude_token_lens.cli:main`. `zipapp`'s own generated bootstrap for
a `module:function` target never wraps the call in `sys.exit(...)`
(`import {module}; {module}.{fn}()`, verbatim from `zipapp.MAIN_TEMPLATE`)
— pointed at `cli:main` directly, that drops every non-zero exit code
(no-data, bad-input, ...) a caller or CI script depends on.
[`claude_token_lens/__main__.py`](src/claude_token_lens/__main__.py) exists
precisely to close this: it's a module whose *import* already calls
`sys.exit(main())`, so `zipapp`'s generated `import claude_token_lens.__main__`
line raises `SystemExit` with the real code before the bootstrap's second,
never-reached line would have swallowed it. Verified by building a `.pyz`
this way and checking both paths: `--version` exits 0, `serve` (a planned
stub) exits 2. The installed console script (`pip install .`, which points
at `cli:main` — see `pyproject.toml`) is unaffected either way, since
`setuptools`' own console-script wrapper always calls `sys.exit(main())`
regardless of what module it targets.

### Subcommands

Generated against this branch's `--help` output (`claude-token-lens
<subcommand> --help` for the authoritative, always-current list). Every
subcommand also accepts the [global flags](#global-flags-clipy) below;
this table only lists what's specific to each one.

| Subcommand | What it does | Extra flags |
| --- | --- | --- |
| `report` | Full report: every section in [section 6](#6-reading-the-report-sections) (`overview`, `usage`, `sessions`, `recache`, `ttl`, `compactions`, `agents`, `workstyle`, `workflows`, `config` when snapshots exist, `scorecard`, `recommendations`), printed as Markdown by default. This is the default subcommand — `claude-token-lens` with no arguments runs it. | `--json` (print the whole report as JSON instead), `--html PATH` (also write a single-file HTML report), `--csv-dir DIR` (also write one CSV per table plus an index), `--phases` (add the DISCOVERY/IMPLEMENTATION/VERIFICATION phase-split section), `--allow-titles` (include `customTitle`/ai-title text — currently a no-op, see [section 6](#6-reading-the-report-sections)), `--patch-set` (also print the recommendation set as unified-diff-style settings/frontmatter patches) |
| `sessions` | Focused view: just `overview` + `sessions` | Same output flags as `report` except `--patch-set` (recommendations aren't part of a focused view) |
| `recache` | Focused view: just `overview` + `recache` | Same as `sessions` |
| `ttl` | Focused view: just `overview` + `ttl` | Same as `sessions` |
| `compactions` | Focused view: just `overview` + `compactions` | Same as `sessions` |
| `config-diff` | Compare sessions grouped by one (or every changed) config key's value, from captured `snapshot-config` snapshots. Prints its own plain-text table(s), independent of `report`'s renderers. | `--key KEY` **or** `--auto-keys` (mutually exclusive, one required): diff one named flattened config key, or every key that changed across the available snapshots |
| `snapshot-config` | Capture (or print/install) the SessionStart config-snapshot hook — see [section 8](#8-installing-the-sessionstart-hook-and-the-statusline) | `--print-hook` (print the settings.json fragment), `--install-hook` (copy the hook script into `<config-dir>/hooks/`), `--managed-path PATH` (override the platform managed-settings.json path) |
| `log-usage` | Read a pasted `get_usage` JSON payload from stdin and append its rows to the local usage-window CSV log | none beyond the global flags |
| `pricing-check` | Print the resolved rate card's provenance and rate table, and (with `--models`) how specific model ids resolve against it | `--models ID,ID,...` |
| `scrub-fixture` | Turn a real `<project_dir>/<session_id>` directory into a privacy-scrubbed test fixture, or verify an already-scrubbed one | `--session-dir PATH --out PATH` (scrub), or `--verify OUT_DIR` (audit an existing scrub), plus optional `--key-seed SEED` (deterministic HMAC key — tests only) |
| `probe` | Content-free schema histogram (line types, key names, attachment types, `version` values — every string capped at 64 chars) of a project or one transcript file, safe to paste into a bug report | `--file PATH` (probe a single transcript file instead of a project) |
| `statusline` | Claude Code `statusLine` handler — reads a JSON payload from stdin on every refresh (see [section 8](#8-installing-the-sessionstart-hook-and-the-statusline)) | `--print-install-fragment` / `--install` (print the settings.json fragment instead of reading stdin) |
| `export` | Aggregate, privacy-safe export of a corpus for BI/observability tooling (see [section 10](#10-for-team-leads-and-enterprise) and [`docs/exports.md`](docs/exports.md)) | `--format {csv-flat,json,otel-jsonl}` (default `csv-flat`), `--aggregate-only` / `--per-session` (mutually exclusive, default `--aggregate-only`), `--hash-slugs` / `--no-hash-slugs` (mutually exclusive, default hashed whenever `--aggregate-only` is in effect), `--out PATH` (default: stdout) |
| `monthly-report` | Write a habit-forming finance summary (cost/tokens by model/project/entrypoint, five-hour blocks under subscription billing) plus the `usage` section for one calendar month, as both Markdown and HTML (see [section 10](#10-for-team-leads-and-enterprise) and [`docs/exports.md`](docs/exports.md)) | `--out DIR` (required), `--month YYYY-MM` (default: the previous calendar month) |
| `init` | Detect what's already set up, ask (or, non-interactively, derive) a short question set, write `config.toml` and this project's `projects/<slug>.toml`, print the hook/statusline install fragments, and run an initial onboarding baseline — see [`docs/onboarding.md`](docs/onboarding.md) | `--answers FILE` (JSON file supplying any subset of the answers), `--non-interactive` (derive unanswered questions instead of prompting), `--no-install` (skip printing the hook/statusline fragments) |
| `baseline` | Capture (or list/show) an onboarding baseline: mode mix, dominant purposes, suggested profile, projected saving — see [`docs/onboarding.md`](docs/onboarding.md) | `--finalise` (treat the baseline as final even if the capture window hasn't elapsed), `--list` (list saved baselines), `--show ID` (print a previously saved baseline's report) |
| `serve` | **Planned for v0.2** — prints which milestone it's planned for and exits 2 | none |
| `compare` | A/B compare two arms of sessions (`window:`/`key:`/`profile:`/`project:` specs), stratified by purpose/mode with a minimum-sample gate — see [`docs/compare.md`](docs/compare.md) | `--a SPEC` / `--b SPEC` (required), `--stratify purpose,mode` (default), `--min-sessions N` (default: `config.toml`'s `min_sessions`), plus the same `--json`/`--html PATH`/`--csv-dir DIR` output flags as `report` |
| `reconcile` | Compare local usage/cost accounting against an Admin API CSV export, entirely offline — see [`docs/compare.md`](docs/compare.md) | `--admin-csv FILE` (required), `--by {day,model,day,model}` (default `day`), plus the same `--json`/`--html PATH`/`--csv-dir DIR` output flags as `report` (the window comes from the global `--days`/`--since`/`--until` flags, not a separate flag) |
| `init` | **Planned for v0.3** — prints which milestone it's planned for and exits 2 | none |
| `baseline` | **Planned for v0.3** — same stub behaviour as `init` | none |
| `serve` | **Planned for v0.2** — same stub behaviour as `init` | none |

`usage`, `agents`, `workstyle`, `workflows` and `scorecard` are real
report sections (see [section 6](#6-reading-the-report-sections)) but
don't have their own focused subcommand the way `sessions`/`recache`/
`ttl`/`compactions` do today — get them via `report` (or `report --json`
and pull out that section).

### Exit codes

Every subcommand uses the same three codes:

| Code | Meaning |
| --- | --- |
| `0` | Ok — the subcommand ran and printed its output. |
| `1` | No data — an empty corpus for the given projects/window, or (for `config-diff`) no config snapshots found. Always paired with a one-line reason on stderr naming the projects root and window. |
| `2` | Bad input — a `ConfigError`/`PricingError` (e.g. an unreadable `--pricing` file), a bad flag combination argparse itself doesn't already catch, or a not-yet-implemented subcommand (`serve`, or any unrecognised command). |

### Global flags (`cli.py`)

These are the flags every subcommand parses (the per-subcommand table
above lists what each one adds on top):

| Flag | Meaning |
|---|---|
| `--projects-root PATH` | override the `<projects_root>` directory (default: `~/.claude/projects`, or `$CLAUDE_CONFIG_DIR/projects`) |
| `--project NAME` | repeatable; a project slug to include (default: the current directory's own slug) |
| `--all-projects` | include every project under the projects root |
| `--project-family REGEX` | group worktree slugs matching a regex as one project family |
| `--days N` / `--since DATE` | window start (mutually exclusive) |
| `--until DATE` | window end |
| `--limit N` | cap the number of sessions considered |
| `--window-by {mtime,timestamp}` | which timestamp windows and sorts by (default `mtime`) |
| `--pricing PATH` | use a rate card other than the packaged default / config-dir override |
| `--config-dir PATH` | override `~/.claude/token-lens` (or `$CLAUDE_CONFIG_DIR/token-lens`) |
| `--group-by {mode,purpose,agent,project,model,profile}` | grouping axis for tables that support it |
| `--no-cache` / `--rebuild-cache` | mutually exclusive. `--no-cache` skips the on-disk digest cache entirely; `--rebuild-cache` purges it first, then repopulates as it parses. Both are wired through to `corpus.load_corpus` for every subcommand that loads a corpus. |
| `--jobs N` | parallel parsing workers (default: 1) |
| `--quiet` / `--verbose` | verbosity (mutually exclusive); `--verbose` also prints a `[corpus] files=... cache_hits=... cache_misses=... elapsed_s=...` line to stderr |
| `--version` | print the tool version and exit |

### Performance

Timings against a real 1.6 GB corpus (`~/.claude/projects`, `--all-projects`,
30-day window — 120 sessions, 1,644 subagent transcripts, 29 workflow runs),
measured 2026-09-19 on the owner's own machine. The corpus was live (being
actively written to by ordinary Claude Code use) during measurement, so
treat these as representative rather than lab-controlled numbers:

| Run | Time |
|---|---|
| Cold (`--rebuild-cache`, empty digest cache, `--jobs 1`) | `22.1` s |
| Warm (unchanged corpus, digest cache populated) | `7.6` s |
| Warm, `--jobs 4` | `12.6` s |

The digest cache that makes the warm numbers possible lives under
`<config-dir>/cache/` (`<config-dir>` defaults to `~/.claude/token-lens`,
or `$CLAUDE_CONFIG_DIR/token-lens`) — one JSON file per transcript, keyed
by that transcript's path plus `model.py`'s `SCHEMA_VERSION` and
`parse.py`'s own `PARSER_VERSION`, so a future release that changes
either the dataclass contract or the parsing logic invalidates exactly
the entries it needs to and nothing else (see
[`cache.py`](src/claude_token_lens/cache.py)). Delete the directory, or
pass `--rebuild-cache`, to force a full re-parse.

## 3. The two token totals

Every priced `Turn` (see `model.py`) carries four raw token counts:
`input_tokens`, `cache_creation_tokens`, `cache_read_tokens` and
`output_tokens`. Two different totals matter, and confusing them is the
single easiest way to misread a Claude Code session's cost:

- **Usage tokens** — everything a turn actually processed:
  `input_tokens + cache_creation_tokens + cache_read_tokens +
  output_tokens`. This is what gets billed for that specific API call,
  and it repeats every time the same content is resent as part of a
  growing context.
- **New tokens** — `input_tokens + cache_creation_tokens`
  (`compaction.new_tokens(turn)`): tokens that entered the context for
  the *first* time on this turn, whether typed by you, generated by the
  model, or newly written into the prompt cache. Content already served
  from a warm cache entry (`cache_read_tokens`) is deliberately excluded,
  because it was not new — it was a cheap re-read of something already
  paid for.

**Worked example.** Say a tool call reads a 5,000-token file once, early
in a session, and the file's contents stay in context (via the prompt
cache) for the next 99 turns:

- **New tokens** for that file: 5,000 — charged once, as
  `cache_creation_tokens` on the turn that first read it.
- **Usage tokens** attributable to that file across the session: roughly
  5,000 × 100 = 500,000 — the same 5,000 tokens are re-processed as
  `cache_read_tokens` (much cheaper per token, but not free) on every one
  of the following 99 turns, because Claude Code resends the whole
  context on every turn.

A report that only prints usage tokens makes a session look far more
expensive to *write* than it actually was; a report that only prints new
tokens hides how much volume the cache is carrying. `compaction.py`'s
dropped-token accounting and `ttl.py`'s simulations both use *new*
tokens as the denominator for exactly this reason — see
[section 7](#7-ttl-simulation-assumptions).

## 4. How caching works in Claude Code

Claude Code's prompt cache works on **prefixes**: the system prompt,
`CLAUDE.md`, tool schemas, skill listings and the growing conversation
history form a layered prefix, and a cache write is billed once per
layer per TTL entry. A cache entry has a time-to-live: **5 minutes by
default**, with a documented **1-hour opt-in** available at three levels:

- `promptCacheTtl` in `settings.json` — the main conversation.
- `subagentPromptCacheTtl` in `settings.json` — the default for every
  spawned subagent that doesn't set its own.
- `experimental.cacheTtl` in an individual agent's frontmatter (e.g.
  `.claude/agents/verification-runner.md`) — overrides the subagent
  default for that one agent type.
- The equivalent environment variables `CLAUDE_CODE_PROMPT_CACHE_TTL`
  and `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL`.

A cached prefix survives as long as nothing upstream of it changes and
the entry hasn't expired. What invalidates it, as observed and encoded
in [`events.py`](src/claude_token_lens/events.py)'s `EventKind` table:
a compaction (`COMPACT_BOUNDARY`/`COMPACT_SUMMARY`), a model switch
(`MODEL_FALLBACK`), the `CACHE_SIGNAL` family (a model change, thinking
being stripped, entering/exiting ultra-effort, a change to the set of
deferred or prefix-loaded tool schemas, an MCP-instructions delta, an
agent-listing delta, entering/exiting plan mode or auto mode, an output
style change) — and, simply, the TTL running out between two turns.
`recache.py` (see [section 5](#5-re-cache-definitions-and-signatures))
is the module that detects when one of these actually cost you money.

Subagents each get **their own cache** — a subagent's first turn always
pays a fresh write for its own briefing and system prompt, independent
of whatever the parent conversation's cache state is; `topology.py`
measures this as each agent type's mean first-turn `cache_creation`.

Two practical consequences worth designing around:

- A 1-hour TTL is **ignored while the account is on usage credits**
  (subscription billing) rather than an API key — per Claude Code's own
  documentation. Recommending a 1h switch for a subscription account's
  subagents would recommend a lever that does nothing.
- TTL should be chosen **per spawned agent type**, not globally.
  `ttl.py`'s `TtlStats` is keyed by agent type for exactly this reason:
  a long-wait agent (one that sits idle while you read a build or test
  result — a verifier) tends to benefit from 1h, because its gaps
  regularly exceed 5 minutes; a short-gap agent (an implementer you're
  actively steering, turn after turn within seconds) rarely benefits,
  because its cache almost never has time to expire under 5m anyway.

## 5. RE-CACHE definitions and signatures

A turn is a **re-cache** ([`recache.py`](src/claude_token_lens/recache.py))
when all of the following hold:

- it is not the transcript's first priced turn,
- its context (`ctx`) exceeds `ctx_floor` (default 20,000 tokens), and
- the fraction of that context actually served from cache read falls
  below `cr_ratio` (default 0.2, i.e. less than 20% of context came from
  a cache hit) —

in other words: the model had to pay to write most of its own context
again, instead of reading a warm cache entry, for no correctness reason.
Every re-cache turn is assigned one of two **signatures**:

- **`full-expiry`** — `cache_read_tokens` is below `full_expiry_cr`
  (default 2,000 tokens): the cache entry had essentially nothing left
  to hit, consistent with its TTL having simply run out since the
  previous turn.
- **`prefix-invalidated`** — `cache_read_tokens` sits between
  `full_expiry_cr` and `cr_ratio × ctx`: there was a partial hit, so the
  TTL had *not* expired, but something upstream of the cached prefix
  changed anyway (a notification, an attachment, a model switch, a
  compaction, ...) and broke it regardless.

A re-cache turn's **avoidable cost** is what its own cache-creation
tokens cost at the write rate they were actually billed at, minus what
those same tokens would have cost at the flat cache-read rate had the
cache not been invalidated. Every threshold above is overridable via
`config.toml`'s `[thresholds]` table (`RecacheThresholds.from_config`).

## 6. Reading the report sections

`report.build_report` (`report.py`) assembles every section below into
one `ReportModel`, in the fixed order the table follows, and `claude-token-lens
report` prints it (Markdown by default; `--json`/`--html`/`--csv-dir` for
the other renderers — see [section 2](#2-quick-start)). Each section is
still, independently, a `build_section(...)` function returning a
`Section` of `Table`s ([`model.py`](src/claude_token_lens/model.py)),
fully tested and runnable on its own from a short Python script against
your own transcripts — that direct-call path is how the worked example in
[`docs/sections-reference.md`](docs/sections-reference.md#worked-example)
was produced, and is still useful if you want one section in isolation.

| Section key | Title | Module | What it answers |
|---|---|---|---|
| `overview` | Overview | `report.py` | corpus-wide totals (sessions, transcripts, turns, the four raw token counts, cost, cache-read cost share, cache ROI) plus a per-model breakdown |
| `usage` | Usage | `usage.py` | day/week/month/project/entrypoint cost and token breakdowns, plus five-hour usage blocks (subscription billing only — see [section 1](#1-what-it-is-what-it-measures-and-what-it-cannot)) |
| `sessions` | Sessions | `classify.py` | mode (interactive/long-agentic/overnight/mixed) and purpose (docs/refactor/test-triage/...) per session, with the evidence that produced each classification |
| `recache` | Re-cache events | `recache.py` | which turns paid to re-write a prefix that should have been a cache hit, why, and what it cost — see section 5 |
| `ttl` | Cache TTL break-even | `ttl.py` | per agent type: observed cost vs. simulated 5m-only/1h-only cost, plus the utilisation metrics below |
| `compactions` | Compactions | `compaction.py` | compaction count, trigger mix, pre/post/dropped tokens, and the re-cache cost of the turn right after each compaction |
| `agents` | Agents and information flow | `topology.py` | downward cost (briefing/system-prompt writes into each agent type), upward cost (`Agent`/`Workflow` tool-result sizes flowing back), skill roll-ups, spawn-depth chains |
| `workstyle` | Workstyle | `workstyle.py` | one archetype per session/corpus: `overseer-fanout`, `plan-high-implement-low`, `workflow-heavy`, `effort-varied`, `chat-only`, `single-model`, with the evidence features |
| `workflows` | Workflows | `workflows.py` | per-run agent count, phase count, duration and cost from `<session>/workflows/wf_*.json` |
| `phases` | Phases | `phases.py` | cost split across DISCOVERY (read/search only), IMPLEMENTATION (real edits or an ordinary shell command), VERIFICATION (a test/build tool, or a scratch-file edit), OTHER — only in the report when `--phases` is given |
| `config` | Config | `report.py` via `snapshots.py` | one diff table per config key that changed across the window's snapshots (capped at 20 keys) — only present when `snapshot-config` snapshots exist for the window |
| `scorecard` | Scorecard | `scorecard.py` | five 1-5 levels (cache efficiency, context hygiene, agent efficiency, config fit, data quality) plus an overall level (the minimum of the first four, never an average) |

Two further pieces of the report aren't in the table above because
they aren't `Section`s: **Recommendations** (`recommend.py`) is a
dedicated `ReportModel.recommendations: list[Recommendation]` field —
see [the Recommendations block](#the-recommendations-block) below — and
**Diagnostics** (parse-quality counters: unparsable lines, synthetic
turns, TTL-sum mismatches, ...) is `ReportModel.diagnostics`, rendered
directly by every renderer rather than as a table.

Two CLI-only, non-`report` consumers use a different section shape
entirely: `config-diff --key K` prints one standalone plain-text table
from `snapshots.build_config_diff_table` (see [section 2](#2-quick-start)
— it does not go through `build_report`, so it isn't the same code path
as the report's own `config` section above), and `usage_windows`
(`tools/log_usage.py`) — your 5-hour/7-day plan usage-window percentages
over time, from a `log-usage` paste or the statusline logger, with a
token-volume regression per window — has a `build_section` function but
nothing in `report.py`/`cli.py` wires it into the assembled report yet;
call it directly, the same as any other module, until that's closed.

`--allow-titles` is accepted by every report-like subcommand but is
currently a no-op: nothing in `model.py`/`parse.py`/`events.py` captures
`customTitle`/ai-title line text anywhere, even conditionally, so there
is no title data for the flag to gate yet.

### The Recommendations block

`recommend.recommend()` turns the assembled report into a list of
`Recommendation`s (`model.py`), each with:

| Field | Meaning |
|---|---|
| `id` | stable identifier for the rule that fired (e.g. `ttl-switch`, `compaction-churn`) |
| `severity` | `"info"` \| `"advice"` \| `"action"` |
| `category` | `"settings"` \| `"workflow"` \| `"data"` |
| `scope` | where the lever named below applies: `"user"` (`~/.claude/settings.json`), `"repo"` (a project `.claude/settings.json` or agent frontmatter path), or `"managed"` (an org-pushed `managed-settings.json` key the user can't change locally — the action text then also says "raise with your administrator") |
| `lever` | the bare settings key or frontmatter path the recommendation would change (e.g. `promptCacheTtl`, `experimental.cacheTtl` in `<agent>.md`), or `None` |
| `evidence` | one or more `(label, value, source_table, row_key)` tuples, each citing a real cell from a table already in the report — a test walks every recommendation this module produces and confirms the value it cites is genuine, not recomputed |

`report --patch-set` renders the whole recommendation set as
unified-diff-style text (`recommend.render_patch_set`) showing the
before/after value for each lever, with managed-scope levers marked
`# managed by policy -- shown for reference only`.

### The TTL section's utilisation metrics

Beyond "which fixed policy would have been cheaper", `ttl.py` answers
"was the cache I paid for actually used": **wasted writes** (a
cache-creation write never read back before it expired), **1h-premium
waste vs. 5m-expiry loss** (the write premium a 1h TTL paid but never
earned back, and symmetrically what a 5m TTL lost to expiries a 1h TTL
would have survived), **break-even share** (the point, from the
resolved rate card, where the two policies cost the same), a
**near-miss histogram** (turns whose gap just missed or just made a TTL
boundary), and **cache ROI** (total saved by caching at all, as a ratio
to what was spent on writes — independent of the 5m-vs-1h question).
Only **prefix-invalidated** re-cache turns are addressable by a TTL
change; a `full-expiry` turn's cache had genuinely run out, which no
TTL choice below the gap length would have prevented. See
[`docs/sections-reference.md`](docs/sections-reference.md#ttl-utilisation-metrics)
for the full definitions and field names.

### Worked example (real, scrubbed session)

[`docs/sections-reference.md`](docs/sections-reference.md#worked-example) has
a real re-cache and compaction summary produced by running
`recache.build_section`/`compaction.build_section` against
[`tests/fixtures/real/session-a`](tests/fixtures/real/session-a) — a
genuine session put through `tools/scrub.py`'s whitelist rewrite, so
every id is HMAC-rehashed and every string field is either a
documented-safe value or an `x`-filled placeholder. Nothing in it is
invented. The same file also lists the remaining tables each section
produces (gap buckets, primary-cause attribution, per-agent-type
breakdowns, trigger mix, and so on) and how to read each column.

## 7. TTL simulation assumptions

Printed verbatim from `ttl.ASSUMPTIONS` — this is exactly what the 5m/1h
break-even simulation assumes, stated so a reader can judge for
themselves where the model might not hold for their own working style:

- content is TTL-invariant
- cacheable prefix `C_i = cache_read_i + cache_creation_i`
- a hit refreshes TTL so survival depends only on `gap_s`
- prefix-invalidated turns keep their observed split under every policy
  (no double counting)
- reads are priced at the flat cache_read rate
- compaction shrink clamps write at 0
- gap is measured from the start of one request to the start of the next

A per-transcript **fidelity self-check** replays the simulation at the
transcript's own dominant observed TTL and compares it to the actually
observed cost; `ttl.build_section` flags any agent type whose fidelity
error exceeds 10% (`TtlThresholds.fidelity_warn_pct`), so a report never
presents a simulated number as trustworthy when the model's own
assumptions demonstrably don't fit that agent type's transcripts. That
same fidelity is printed per agent type in the `ttl_by_agent_type` table,
and a TTL-switch recommendation is suppressed for an agent type whenever
its projected saving doesn't clear the simulation's own fidelity margin,
or whenever its fidelity exceeds `TtlThresholds.max_fidelity_for_advice_pct`
(5% by default) — the tool would rather stay silent than recommend a
policy change it can't back with a trustworthy number.

## 8. Installing the SessionStart hook and the statusline

### SessionStart config-capture hook

`hooks/snapshot-config.py` is a standalone stdlib script — it
deliberately imports nothing from this package, so it keeps working if
copied on its own onto a machine that only has a bare Python
interpreter. Installed and run today via the working `snapshot-config`
CLI subcommand:

```bash
claude-token-lens snapshot-config --install-hook   # copies the script into <config-dir>/hooks/
claude-token-lens snapshot-config --print-hook      # prints the settings.json fragment below
```

`--print-hook` prints both variants; merge the relevant one into the
`"hooks"` key of `~/.claude/settings.json` (`SessionStart` may already
have other entries — add to the list, don't replace it):

Windows:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "py -3 \"%USERPROFILE%\\.claude\\token-lens\\hooks\\snapshot-config.py\""
          }
        ]
      }
    ]
  }
}
```

POSIX (Linux/macOS):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/.claude/token-lens/hooks/snapshot-config.py\""
          }
        ]
      }
    ]
  }
}
```

The hook always exits 0 and prints nothing on success (or a single
stderr line on failure) so a broken Python can never block a session
start. It writes one snapshot JSON file per run (skipped when an
unchanged snapshot already exists within `--min-interval`, default 300s)
to `<config-dir>/snapshots/<UTC compact timestamp>.json`,
capturing:

- **Environment variable names only, never values**, for every name
  matching `ANTHROPIC_*` / `CLAUDE_*`.
- **Settings values, but only for a small allowlist**: `model`,
  `effortLevel`, `outputStyle`, `autoCompactWindow`, `promptCacheTtl`,
  `subagentPromptCacheTtl`, `cleanupPeriodDays`,
  `desktopSessionCleanupPeriodDays`, `autoUpdatesChannel`,
  `alwaysThinkingEnabled`, plus any plain `bool`/`int` value (a toggle or
  a limit, never content). Every other settings key is replaced by a
  shape-only marker (`dict(n)`, `list(n)`, `str(len)`).
- The same redaction rule applied to every agent's frontmatter (model,
  effort, `maxTurns`, `omitClaudeMd`, nested `experimental.cacheTtl` kept
  verbatim; `description` always reduced to `str(len)`), MCP server
  names, and enabled plugin names (names only).
- **Managed (enterprise-policy) settings**, read the same way from the
  platform's system-wide policy file and redacted identically into
  `managed_settings`, with its raw top-level key names (never values)
  recorded verbatim into `managed_keys` — so a future recommendation can
  say "this lever is managed by policy" for any key that appears there.
  Platform paths: macOS
  `/Library/Application Support/ClaudeCode/managed-settings.json`, Linux
  `/etc/claude-code/managed-settings.json`, Windows
  `%ProgramData%\ClaudeCode\managed-settings.json` (override with
  `--managed-path`).

### Statusline

The statusline is a separate, self-contained entry point. Either
`claude-token-lens statusline --print-install-fragment` (the CLI
subcommand delegates straight to the module) or invoking the module
directly work identically:

```bash
python -m claude_token_lens.statusline --print-install-fragment
```

Merge the result into `~/.claude/settings.json` (replaces any existing
`"statusLine"` key):

Windows:

```json
{ "statusLine": { "type": "command", "command": "py -3 -m claude_token_lens.statusline" } }
```

POSIX (Linux/macOS):

```json
{ "statusLine": { "type": "command", "command": "python3 -m claude_token_lens.statusline" } }
```

On every status-line refresh, Claude Code writes a JSON payload to this
script's stdin; the statusline reads `context_window.used_tokens` (a
compact `ctx NNk` figure), `prompt_cache` (real cache ground truth — see
below), `rate_limits.{five_hour,seven_day}.used_percentage` (plan
usage-window percentages — also appended to
`~/.claude/token-lens/usage-log.csv`, deduped by session/reset-time/
percentage), and the last 64 KB of `transcript_path` (never the whole
file) as a fallback TTL hint when no usable `prompt_cache` is present. It
never raises: any failure anywhere in that path falls back to printing a
minimal `token-lens` line rather than blanking the status bar.

**Cache segment.** When the payload carries a real `prompt_cache` object,
the line prints ground truth, not an estimate: `cache warm 5m 03:12` (a
`MM:SS` countdown to `prompt_cache.expires_at`) while warm, or
`cache cold` with an optional trailing `recache ~12k tokens` hint (from
`prompt_cache.recache_tokens_if_cold`) once it has expired. With no
usable `prompt_cache` at all, the line falls back to an estimate labelled
`cache est`, whose TTL is read from the transcript's own last assistant
line (a positive `message.usage.cache_creation.ephemeral_1h_input_tokens`
implies a 1h TTL, else 5m) rather than guessed. The whole line stays
under 120 characters and never prints message text. The numeric
`prompt_cache` fields (`warm`, TTL, expiry, misses, miss cause,
recache-if-cold tokens) are also appended to `usage-log.csv` as six
trailing columns, so `claude-token-lens report` can render a
`cache_ground_truth` table (see
[`docs/sections-reference.md`](docs/sections-reference.md)) summarising
real cache-warmth across sessions instead of a live-only estimate.

## 9. Windows notes

- **Slug case variants.** A project slug is derived from the working
  directory (`discovery.slug_for`); the projects directory is
  de-duplicated across sessions by
  `os.path.normcase(os.path.realpath(path))`, so `C--Dev-RevIXO` and
  `c--Dev-RevIXO` resolve to the same project on a case-insensitive
  filesystem. `--project-family REGEX` groups worktree slugs of the same
  underlying project together.
- **MSYS / Git Bash paths.** The privacy scan (`helpers.assert_privacy`,
  exercised by `tests/test_privacy.py`) explicitly checks for an
  MSYS-style drive path (`/c/Users/...`) alongside a Windows drive path
  (`C:\...`) and a `\Users\` segment, since a command run from Git Bash
  on Windows produces that third form.
- **Long paths.** Paths at or beyond 255 characters get the `\\?\`
  extended-length prefix (`\\?\UNC\` for a UNC path) before being opened,
  on Windows only (`jsonl._windows_long_path`).
- **Live files.** A transcript whose `mtime` is under 60 seconds old is
  treated as still being actively written and is read with
  `errors="replace"`, never cached (`cache.py`), and tolerated even if
  its final line is truncated mid-write.
- **`CLAUDE_CONFIG_DIR`** moves the whole config tree: both
  `<projects_root>` (`.../projects`) and claude-token-lens's own
  `<config-dir>` (`.../token-lens`) resolve underneath it when set,
  instead of `~/.claude`.
- **`CLAUDE_CODE_PROJECT_DIR_NAME`** overrides the computed slug for the
  *current* project directory only, independently of whether
  `CLAUDE_CONFIG_DIR` also moved the whole tree.

## 10. For team leads and enterprise

What's implemented today:

- **`exclude_projects`** (`config.toml`, a list of slug regexes) excludes
  matching projects from discovery entirely — for a confidential repo
  you never want scanned, not just hidden from output. A malformed regex
  is skipped, never fatal.
- **`retention_days`** (`config.toml`, an integer) is read and validated
  as a setting for the eventual local SQLite store's own retention
  policy; nothing enforces it yet, since that store (the v0.2 service)
  doesn't exist in this codebase.
- **Managed settings** are captured by the snapshot hook (see section 8)
  into `managed_settings` (redacted the same as user settings) and
  `managed_keys` (raw key names only), and `recommend.py` renders them:
  any recommendation whose lever's settings key appears in a window's
  `managed_keys` gets `scope: "managed"` and its action text says "this
  lever is managed by policy, raise with your administrator" instead of
  proposing a change the user can't actually make locally — see
  [the Recommendations block](#the-recommendations-block).
- **Provider detection.** `parse.detect_provider` classifies each turn's
  billing surface from the model id's own shape: an
  `anthropic.`/`us.anthropic.`-prefixed or `-v1:0`-suffixed id is
  Bedrock, an `@<date>`-suffixed id is Vertex, anything else is direct
  Anthropic API. This is recorded on `TranscriptMeta.provider` and
  `Config.provider`. **Not yet implemented:** Foundry detection (only
  `anthropic`/`bedrock`/`vertex` are recognised today), and any
  per-provider rate override in `pricing.toml` — every provider is
  currently priced against the same Anthropic-direct rate card.

### Exports and scheduled reports

`claude-token-lens export --format csv-flat|json|otel-jsonl` (S1-exports)
feeds a corpus into existing BI/observability tooling with the same
privacy posture as the report itself: **aggregate-only is the default**
(one row per day/project/model/entrypoint/agent-type — no session ids)
and **project slugs are hashed by default in every mode**, including
`--per-session`, via a salted HMAC-SHA256 construction (the same
HMAC-SHA256 construction, and the same `load_or_create_salt`/config-dir
salt file, as every other hashed value in this project — just a
different truncation length and domain-separation tag). Per-session
detail is opt-in (`--per-session`); the raw-slug opt-out
(`--no-hash-slugs`, honoured even together with `--aggregate-only` as an
explicit, informed choice) doesn't print the fully raw slug either — it
redacts just the OS-username segment (`Users-<name>-`/`home-<name>-` ->
`<user>`) and warns on stderr. No prompt text, tool output, or file path
is ever in an export — every column is a count, a token total, or a
cost. `otel-jsonl` mirrors Claude Code's own OpenTelemetry metric names
(`claude_code.token.usage`, `claude_code.cost.usage`) as an **offline
approximation** for feeding an existing collector's dashboards, not a
live exporter. Full column reference and format details:
[`docs/exports.md`](docs/exports.md).

`claude-token-lens monthly-report --out DIR [--month YYYY-MM]`
(S1-exports) writes `DIR/claude-token-lens-YYYY-MM.md` and the matching
`.html` for one calendar month (default: the previous month, resolved
against `config.tz`) — a short finance header (total cost, tokens,
sessions, cost by model/project/entrypoint, five-hour blocks used under
subscription billing) followed by the `usage` section, sized for a
recurring habit rather than the full multi-section report. The same
`(corpus, pricing, config, month)` produces files identical apart from a
trailing "Generated at" line/comment; pass `--generated-at` (or set
`SOURCE_DATE_EPOCH`) for a genuinely byte-identical run, so it is safe to
schedule even when diffing raw bytes.

Together these make claude-token-lens usable as a team tool without
running claude-token-lens's own modules by hand against each person's own
`~/.claude/projects` — see [`docs/exports.md`](docs/exports.md) for the
full picture, including the entry point (`monthly.write_monthly_report`)
the v0.2 service wires up to its own `--monthly-report DIR` flag.

## 11. Privacy and security

See [SECURITY.md](SECURITY.md) for the full sign-off checklist. In
short: every dataclass field is length-capped and shape-checked so
message text, tool results, full paths and full commands never reach a
report — enforced today by:

```bash
python -m pytest tests/test_privacy.py tests/test_scrub.py -q
```

`tests/test_privacy.py` walks every field `parse_transcript` produces
across a battery of synthetic fixtures and asserts no `str` field
exceeds 64 characters outside a small, named allowlist (agent type,
model id, tool names, session id, slug, attachment subkind, the
transcript's own file path), that `cmd_prefix`/`preceding_cmd_prefix`
never exceed 40 characters, and — via `tests/helpers.assert_privacy` —
that no field matches a Windows drive path, a POSIX `/home/` path, a
`\Users\` path, an MSYS drive path, a bare `@`, or a URL.

The same audit applies to `report --json`/`report --html PATH` output,
or to JSON/HTML you generate by calling `render.json_out`/`render.html`
directly — run it by hand over the file:

```bash
grep -RnoE '[^"]{65,}|[A-Za-z]:\\\\|/home/|\\\\Users\\\\|/c/Users/|@' report.json report.html
```

Treat any hit as a bug and open an issue with the field name (never the
value).

## 12. Prior art and credits

- **[nateherkai/token-dashboard](https://github.com/nateherkai/token-dashboard)**
  — the closest prior art: stdlib Python + SQLite + a web UI, dedupes by
  `message.id`, per-prompt cost, a tips tab. claude-token-lens adds TTL
  break-even simulation, RE-CACHE cause attribution, per-subagent-type
  accounting, and config snapshot/diff — none of which token-dashboard
  covers.
- **[cebert/cache-ttl-analyzer](https://github.com/cebert/cache-ttl-analyzer)**
  — a 5m-vs-1h replay for the main conversation. claude-token-lens
  extends the same idea to every subagent type independently, adds the
  wasted-write/premium/near-miss utilisation metrics, and ports the
  simulation to Python against the real transcript format rather than a
  fixed-price TypeScript model.
- **[ryoppippi/ccusage](https://github.com/ryoppippi/ccusage)** — daily
  and monthly cost tables across multiple CLIs. claude-token-lens is
  Claude-Code-specific and goes deeper on one client: the 5m/1h split,
  RE-CACHE detection, per-subagent-type attribution and config-aware
  recommendations that ccusage's broader scope doesn't attempt.
- **[Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor)**
  and the wider `claude-usage` ecosystem — real-time quota/5-hour-window/
  burn-rate monitoring. claude-token-lens is a complementary, offline
  analytics tool: it explains *why* a session cost what it did (caching,
  config, workstyle) rather than watching the live burn rate.

## 13. Licence, contributing, roadmap

**Licence:** MIT — see [LICENSE](LICENSE).

**Contributing:**

```bash
pip install -e .[test]
pytest -q
```

Conventional commits (`feat(parse): ...`, `test(ttl): ...`,
`docs(readme): ...`), one focused commit per change. `tests/helpers.py`
has the fixture builders used throughout the test suite
(`turn_line`, `attachment_line`, `system_line`, `write_jsonl`,
`assert_privacy`, ...) — start there before writing a new test fixture
by hand.

**Roadmap** (see the project plan for full detail; report assembly, the
recommendation engine and the optimisation scorecard shipped in v0.1 —
see the Status note above):

- **v0.2** — `claude-token-lens serve` (a local read-only service:
  watcher thread, SQLite store, `http.server` JSON API and a
  dependency-free static web UI), Docker packaging, a live countdown in
  the statusline, an aggregate-only `export` command, and a monthly
  report. Still ahead.
- **v0.3** — a profile schema and catalogue, `init` and a `baseline`/
  onboarding capture window (all shipped — see
  [`docs/onboarding.md`](docs/onboarding.md) and
  [`docs/profiles.md`](docs/profiles.md)); `apply`/`--revert` for
  writing a chosen profile into `settings.json`/agent frontmatter, a
  `compare` command, team aggregate import across machines, and a
  reconciliation pass against real billing data are still ahead.

## 14. Running the service

`claude-token-lens serve` runs a local watcher thread, a SQLite store,
a read-only JSON API and a dependency-free static web UI, so you can
keep a live dashboard open instead of re-running `report` by hand. It
never opens an outbound connection and binds to `127.0.0.1` unless you
explicitly pass `--allow-remote` (see [SECURITY.md](SECURITY.md)).

```bash
claude-token-lens serve --projects-root ~/.claude/projects --config-dir ~/.claude/token-lens
# then open http://127.0.0.1:8765
```

Three ways to keep it running continuously, in the order this project
recommends them (native first, Docker last) — full detail, including
what each path can/cannot touch and how to verify no egress, is in
[docs/deploy.md](docs/deploy.md):

- **Windows, no admin rights:**
  `powershell -ExecutionPolicy Bypass -File scripts\windows\Register-TokenLensTask.ps1`
  registers a logon-triggered Scheduled Task (`-RunLevel Limited`).
  Remove it with `Unregister-TokenLensTask.ps1`.
- **Linux/macOS, no root:**
  `systemctl --user enable --now claude-token-lens.service` after
  copying `scripts/systemd/claude-token-lens.service` to
  `~/.config/systemd/user/` — a hardened user unit
  (`ProtectHome=read-only` plus a carved-out `ReadWritePaths` for its
  own data directory).
- **Docker:** `docker compose up -d` builds and runs the hardened image
  in this repository's `Dockerfile`/`docker-compose.yml` (non-root
  user, read-only root filesystem, `cap_drop: [ALL]`, loopback-only
  published port).

No `pip install`? `python scripts/build-pyz.py` produces a single
dependency-free `dist/claude-token-lens.pyz` you can copy anywhere and
run with `python dist/claude-token-lens.pyz serve ...`.

The store is always a derived cache, never source of truth: delete and
rebuild it any time with `claude-token-lens serve --purge --yes`, and
prune old sessions automatically with `--retention-days N`.

## 15. Applying a profile

`claude-token-lens apply` writes one of the seven shipped catalogue
profiles (or your own profile TOML file) into `settings.json`/agent
frontmatter/env-var guidance for a project or your user config — the
host-side half of v0.3's profile system (`docs/profiles.md` is the full
schema/catalogue/diff reference; this is the applying-it walkthrough).

```bash
# Preview the exact diff, nothing written:
claude-token-lens apply interactive-chat --dry-run

# Apply it to the current project (writes .claude/settings.local.json):
claude-token-lens apply interactive-chat --project-dir .

# Undo it:
claude-token-lens apply --revert 20260919T100252Z

# One-session overlay instead of a persisted apply:
claude-token-lens apply interactive-chat --launch
```

| Flag | Meaning |
|---|---|
| `--scope {user,project-local,repo}` | Which settings file is written (default: `user`, or `project-local` once `--project-dir` is given) |
| `--project-dir PATH` | Project directory for `project-local`/`repo` scope. Named `--project-dir`, not `--project` — the global `--project` flag already means "a repeatable project slug to filter a report by", the same collision `snapshot-config`/`probe-config` resolve the same way |
| `--dry-run` | Print the diff and the exact command to run, without writing anything |
| `--launch` | Write a one-session `<config-dir>/profiles/<id>.settings.json` overlay instead of a persisted apply |
| `--allow-tracked` | Allow writing a target file that a git repository already tracks (refused by default for `project-local`/`repo` scope) |
| `--force` | Create a missing `.claude/agents/<name>.md` file from scratch instead of refusing |
| `--revert TS` | Undo a previous apply, byte for byte, named by the timestamp `apply` printed at the time |
| `--list-backups` | List previous applies (timestamp, profile, scope, file count) and exit |

Every real apply backs up whatever it overwrites first, so `--revert`
always restores the exact prior state; a managed-settings key is never
written regardless of scope or flags; and `env` values are printed as
`export NAME=value` guidance only, never written to any file. See
[docs/profiles.md](docs/profiles.md#applying-a-profile) for the full
detail on scopes, backups, and the tracked-file/missing-agent-file
refusals.
