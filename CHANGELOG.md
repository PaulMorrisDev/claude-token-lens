# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Capture-improvements batch** (`PARSER_VERSION` 3 -> 4): seven new
  additive `Turn` fields, all derived from data the transcript already
  carries -- no new raw content is ever retained:
  - `tool_wait_s`/`model_latency_s`: timing either side of a turn's own
    tool calls, from the timestamps of the tool_result line(s) answering
    that turn's `tool_use_id`s -- `tool_wait_s` is the harness/tool
    round-trip, `model_latency_s` is the model's own think time before
    its next turn. Both `None` when the turn made no tool calls or a
    timestamp is missing.
  - `tool_result_chars_by_tool: dict[str, int]`: per-turn breakdown (by
    tool name) of the same lengths `TranscriptResult.tool_result_chars`
    already totals for the whole transcript, enabling per-turn context
    composition.
  - `agent_brief_chars: int | None` / `tool_input_chars_by_tool: dict[str,
    int]`: the total length of an `Agent`/`Task` tool_use's own `prompt`
    input string(s) (never the text itself), and a per-tool total of
    every tool_use's JSON-encoded input size for the turn.
  - `read_target_hashes: tuple[str, ...]`: salted HMAC-SHA256 (16 hex
    chars) of each `Read`/`Edit`/`Write`/`NotebookEdit` tool_use's own
    target path in the turn -- never the path itself. The salt is a
    random 32-byte file at `<config_dir>/salt`, created on first use via
    `parse.load_or_create_salt` (0600 where the OS supports it) and wired
    into the parser via the new module-level `parse.set_salt`, which
    keeps `parse_transcript`'s own signature unchanged so a
    `ProcessPoolExecutor` worker can still initialise it once per
    process. With no salt set, `read_target_hashes` is always empty.
  - `human_prompt_chars: int | None` / `human_prompt_has_paste: bool`: on
    the turn following a HUMAN_TEXT event, the summed length of that
    event's own text content and whether any of it looks pasted (over
    2,000 chars, or a `[Pasted text` marker) -- `events.classify_line`
    now also populates `Event.size_chars`/`detail["has_paste"]` for every
    HUMAN_TEXT event it emits, which this reads from.
  - Every new field is covered by `tests/test_capture_improvements.py`
    (including a same-salt/different-salt stability check for
    `read_target_hashes` and a scan confirming the hash never contains a
    path segment) and passes the existing `assert_privacy` scan.
  - `probe.compare_with_parser(result) -> list[str]`: lists every
    `<line_type>.<key>` a probed transcript actually carries that
    `parse.READ_KEYS` (a new, hand-maintained map of the top-level keys
    `parse_transcript` reads per raw line type) says the parser never
    looks up -- printed by `probe`'s CLI output under a new "unread keys
    (vs parse.py)" section. Run once over
    `tests/fixtures/real/session-a`: the unread keys are almost entirely
    session/process bookkeeping already captured elsewhere by
    `discovery.py` (`sessionId`, `parentUuid`, `cwd`, `gitBranch`,
    `agentId`, `slug`, `userType`) plus a handful of narrower items worth
    a look for a future batch -- `user.toolUseResult` (a possible
    alternate/duplicate tool-result representation), `queue-operation.
    content`/`reason`, and `system.stopReason` (present but empty in
    every observed line in this fixture, consistent with this batch's
    decision not to implement the related `TranscriptMeta` fields below).
  - Investigated but deliberately **not implemented**: the task's
    suggested `TranscriptMeta.endedReason`/`stopReason`/`maxTurnsReached`
    additive fields. None of the three keys exist meaningfully in
    `tests/fixtures/real/session-a` -- every subagent `.meta.json` in
    that fixture carries only `{agentType, description, model,
    spawnDepth, toolUseId}`, and the transcript's own `stopReason` field
    (on `system`/`stop_hook_summary` lines) is present but an empty
    string across all 104 occurrences, unrelated to subagent completion.
    Left for a future batch once a fixture that actually populates one of
    these keys is available.
  - `topology.py`'s spawn-write table (`topology_spawn_write`) gains a
    "Mean briefing chars" column (from the new `agent_brief_chars`,
    joined back to each direct spawn via the same tool_use_id join the
    skill roll-up uses); the cost-per-spawn table (`topology_cost_per_spawn`)
    gains a "Mean tool wait" column (mean `tool_wait_s` across that agent
    type's own priced turns). Existing columns on both tables are
    unchanged.
- **Config snapshot schema 2** (additive over schema 1 — a schema-1
  snapshot still loads unchanged): `hooks/snapshot-config.py` now
  resolves and records every settings layer (`managed` >
  `.claude/settings.local.json` > `.claude/settings.json` >
  `~/.claude/settings.json`) individually as `settings_layers`, plus the
  merged `effective`/`effective_provenance` result across them; a
  redacted read of `~/.claude.json` (`claude_json` — MCP server/plugin
  names, trust-dialog/allowed-tools counts, and per-project `last*`
  session totals, matched to the current project by
  `normcase(realpath(...))`, never the raw matching key); and
  `content_layers` (sizes/counts/names only, never content, for the
  CLAUDE.md family including a bounded nested walk, `.claude/rules/`,
  `.claude/commands/`, skills, agents source/shadow rollup, `.mcp.json`,
  output styles, auto-memory footprint, and installed plugins).
  `agents` entries are now tagged `source` (`user`/`project`) and, on a
  name clash, `shadowed_by_project`. See
  [`docs/config-layers.md`](docs/config-layers.md).
- Widened the settings allowlist (`autoCompactEnabled`, `modelPricing` —
  present flag + overridden model ids, never the numbers — plus a
  dedicated `statusLine` present-flag shape) and the environment-name
  allowlist (`OTEL_*`, plus enough irregular names —
  `MAX_THINKING_TOKENS`, `MAX_MCP_OUTPUT_TOKENS`,
  `BASH_MAX_OUTPUT_LENGTH`, `DISABLE_NON_ESSENTIAL_MODEL_CALLS` — that
  the existing `ANTHROPIC_*`/`CLAUDE_*` prefixes now cover every
  documented Claude Code environment lever by name). Four of those
  names are numeric caps rather than secrets, so their integer value is
  recorded too (`env_numeric_caps`): `MAX_THINKING_TOKENS`,
  `MAX_MCP_OUTPUT_TOKENS`, `BASH_MAX_OUTPUT_LENGTH`,
  `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`.
- `snapshots.py`: `effective_config`, `effective_provenance`, `layers`,
  `latest_snapshot_per_project`, `build_effective_config_table`,
  `build_config_layers_table`, `build_config_groups_table`,
  `detect_drift`, `build_config_drift_table`, `claude_json_cross_check`.
  `build_config_section` gains optional `include_effective=True` and
  `sessions_with_observed=` keyword arguments (both off by default, so
  an existing caller's output is unchanged).
- CLI: `claude-token-lens probe-config [--project-dir PATH]` scans a
  project's config layers without a session and prints the layers +
  effective-config tables as Markdown, never a raw path. `snapshot-config`
  gains `--project-dir PATH` to run the hook for an explicit project
  directory instead of the current one (named `--project-dir` rather
  than `--project`, which every subcommand already uses for "a
  repeatable project slug to filter a report by").

- **S1-context-budget**: new `context_budget.py` section (`Section(key="context_budget")`)
  answering "do we track preloaded skills, the system prompt, and the
  autocompact buffer?" -- `context_budget_baseline` (per-project measured
  mean/median first-turn `cache_creation` next to labelled `(est)`
  buckets for human prompt, skills listing, memory files, custom agents,
  MCP tools and a residual system-prompt-and-tools share, from
  characters/bytes divided by 4), `context_budget_autocompact` (the
  configured `autoCompactWindow` vs. the observed effective threshold --
  median `compactMetadata.preTokens` over `trigger == "auto"`
  compactions, a new `compaction.effective_autocompact_threshold` helper
  -- the implied buffer, and a >10% drift flag), and
  `context_budget_statusline` (real, non-estimated ground truth once a
  usage-log row carries `context_window` fields). `recommend.py`'s
  `baseline-bloat` rule now cites this section's sized buckets as its
  evidence and names the largest one in its action text when the section
  is present. `statusline.py` appends three new trailing columns
  (`context_window_used_tokens`, `context_window_size`,
  `context_window_autocompact_threshold`) to the usage-log CSV whenever
  the payload's `context_window` carries numeric fields -- old-format
  (six-column) rows are still read without error.

### Planned

- **v0.2** — `claude-token-lens serve` (local read-only service: watcher
  thread, SQLite store, `http.server` JSON API, dependency-free static
  web UI), Docker packaging, a live TTL countdown in the statusline, an
  aggregate-only `export` command, and a monthly report.
- **v0.3** — `init`, a `baseline`/onboarding capture window, a profile
  schema and catalogue, `apply`/`--revert` for writing a chosen profile
  into `settings.json`/agent frontmatter, a `compare` command, a team
  aggregate command that imports several machines' hashed-slug exports
  into one store for per-archetype comparisons across people (no text
  ever — for team leads on the work machine), and a reconciliation pass
  against real billing data.

The following are a v0.4 backlog, kept here until they are scheduled into a
milestone:

- **Budget check.** `check --weekly-tokens N --daily-usd N` exits non-zero
  when exceeded; UI banner; uses `log-usage` window data when present.
  Guardrail for overnight runs.
- **Anomaly outliers.** Sessions or spawns whose cost is more than 3 median
  absolute deviations from their mode/purpose group, with the composition
  table attached. Catches runaway agents.
- **Scheduled reports.** `serve --weekly-report DIR` writes the Markdown/HTML
  report every Monday for sharing. Habit-forming review.
- **Opt-in local path view.** `--show-paths` (local only, never in exports)
  lists the top files by Read tokens, as token-dashboard does.

## [0.1.1] - Unreleased

### Fixed

- **A1 — `recommend.py`'s spawn-cost rule.** The `omitClaudeMd`
  frontmatter lever (scope `repo`) is now only offered for agent types
  that actually have a `.claude/agents/<type>.md` frontmatter file —
  read from the latest config snapshot's `agents` map when a snapshot
  is available, else from a built-in list of Claude Code's bundled
  agent types (`claude`, `general-purpose`, `Explore`, `Plan`,
  `claude-code-guide`, `statusline-setup`, `workflow-subagent`).
  A built-in agent type instead gets `category="workflow"` advice to
  shorten the Agent-prompt briefing, with `lever=None` (so it is
  correctly skipped by `render_patch_set()`) — there is no frontmatter
  file to trim for it.
- **A2 — no table row key may be a bare `int`.** `recache.py`'s
  `recache_summary`/`recache_huge_context` tables and `topology.py`'s
  `topology_session_baseline`/`topology_spawn_depth` tables all had a
  numeric (session/turn/depth count) first column standing in as the
  row key. Every one now carries a real string key (`"all"`, or the
  depth as a string) with the count moved into its own `metric`/typed
  column. `tests/test_recommend_contract.py`'s evidence-and-row-key
  check, which previously accepted `int` row keys, now rejects them —
  row 0 of any table must be a non-empty `str`.
- **A3 — recommendation evidence values are formatted by their cited
  column's kind.** The Markdown and HTML renderers used to print a
  `Recommendation.evidence` value raw (`63.749066571507974` instead of
  `63.7%`, `47345.372881355936` instead of `47,345`). `render/tables.py`
  gained `resolve_evidence_column_kind`/`format_evidence_value`, which
  look the cited `source_table`/`row_key` up in the `ReportModel` and
  format the value through the same `format_cell` every table cell
  uses. The JSON renderer is unaffected by design — it keeps raw
  values via `to_jsonable`.

## [0.1.0] - 2026-09-19

### Added

- Repository scaffold: licence, changelog, security policy, `pyproject.toml`,
  the frozen `model.py` data contract, `render/tables.py` cell-formatting
  primitives, and the `cli.py` argparse skeleton with subcommand stubs.
- `workstyle.build_section`, `workflows.build_section`, and
  `snapshots.build_config_section`: report `Section`/`Table` wrappers for
  archetype counts, workflow-run summaries, and the config diff table,
  matching the pattern already used by `classify`/`compaction`/`recache`/
  `ttl`.
- `recommend.py` (WP10b): `recommend()` turns an assembled `ReportModel`
  into evidence-backed `Recommendation`s, implementing every Appendix A5
  rule (`ttl-switch`, `long-tool-waits`, `notification-invalidation`,
  `batch-instructions`, `subagent-volume`, `compaction-churn`,
  `long-context-share`, `cache-read-dominance`, `baseline-bloat`,
  `agent-report-size`, `spawn-cost`, `effort-mismatch`,
  `discovery-share`, `pricing-coverage`, `data-quality`) with archetype
  gating, a minimum-sample gate, and managed-settings-aware scope
  encoding; `render_patch_set()` renders the settings/frontmatter changes
  a recommendation set implies as unified-diff-style text.
- `report.build_report` now accepts `session_overrides` and populates
  `ReportModel.recommendations` via `recommend.recommend()`, using the
  corpus's own archetype and latest config snapshot.
- CLI wiring (WP10c): `report`/`sessions`/`recache`/`ttl`/`compactions` are
  now real, backed by `report.build_report` (the four focused subcommands
  via `include={"overview", <section>}`), with shared `--json`/`--html`/
  `--csv-dir`/`--phases`/`--allow-titles` output flags and `--patch-set`
  on `report` (a no-op until `recommend.py` lands, guarded by
  `importlib.util.find_spec`). `config-diff --key K|--auto-keys` is its
  own standalone consumer of `snapshots.build_config_diff_table`.
  `log-usage`, `scrub-fixture` and `statusline` delegate to their
  existing modules; `probe` (new `probe.py`) is a content-free schema
  histogram (line types, key names, attachment types, system subtypes,
  `version` field values -- every recorded string capped at 64 chars) of
  a project or a single transcript file, for pasting into a bug report
  without leaking transcript content. `init`/`baseline`/`serve` now
  print which future milestone they're planned for. Added `--jobs` to
  the global flag set. `__main__.py` makes `python -m claude_token_lens`
  (and a `python -m zipapp`-built `.pyz`) propagate the real exit code.
- `Recommendation.scope: str = "user"` (`"user"` | `"repo"` | `"managed"`,
  plan "Enterprise use" section) lands as a real `model.py` field on the
  wp10b/wp10c merge, replacing the `"[managed] "` string prefix on
  `Recommendation.lever` that `recommend.py` used as a workaround while
  `model.py` was outside its work package's file list. `render/
  markdown.py` and `render/html.py` show it alongside `Lever:`;
  `render/json_out.py` emits it automatically (generic dataclass-field
  serialisation). `cli.py`'s report-like subcommands now also load
  `<config_dir>/sessions.toml` via `config.load_session_overrides` and
  pass it to `report.build_report(session_overrides=...)`, closing the
  gap `report.py`'s module docstring flagged (WP10a had no `config_dir`
  parameter to load it from).

### Fixed

Independent review of the WP4/WP6/WP8 report-assembly modules found 15
defects (12 from the numbered review pass, 3 added by the coordinator
alongside it), each fixed and landed as its own commit, same
one-commit-per-fix / green-tests discipline throughout:

- **RE-CACHE detection and attribution (`recache.py`, `compaction.py`)**
  — cache-hit signatures are now applied to transcript turns before
  they're scored, instead of leaving every turn unsignatured; added a
  token-weighted control group and a prefix-invalidated primary-cause
  table (invalidation cause was previously undercounted for
  prefix-broken turns); `compaction.py` now marks turns as re-cache via
  the shared `recache.apply()` detector instead of a separate internal
  `is_recache_turn()` heuristic, so its RE-CACHE-flagged write-cost
  figures use the same signature logic as every other module.
- **TTL simulation (`ttl.py`, `compaction.py`)** — simulated turns are
  now priced per-turn via the rates lookup rather than a single blended
  rate; added unconditional counterfactual buckets and a single,
  consistent expiry-loss basis across the 5m/1h comparison; renamed
  `in_window_share`/`break_even_share` to `in_window_pct`/`break_even_pct`
  (stored as percents, turn 0's own prefix included in the shared
  denominator); `delta_usd`/`delta_pct` keep their sign and gained a
  `saving_usd = max(0, delta_usd)` companion; added a `TtlThresholds`
  dataclass (`from_config`/`describe()`, mirroring `RecacheThresholds`)
  threaded through the simulation, fidelity, and report-building
  functions; `TtlTypeStats.recommendation` changed from a property to a
  method taking `th`; `compaction.py`'s RE-CACHE trio now also reads
  from `RecacheThresholds` instead of hardcoding it a second time.
- **Compaction accounting (`compaction.py`)** — added
  `CompactionRecord.join_delta_s` and a module-level `new_tokens()`
  helper (`input_tokens + cache_creation_tokens`); post-compaction
  write/recache cost aggregates and the per-session cost column now
  exclude records whose join to their next turn took longer than 15
  minutes, and the dropped-token share is reported against both the
  `cache_creation` and `new_tokens` denominators.
- **Event and purpose classification (`events.py`, `classify.py`,
  `parse.py`)** — `classify_line` now tests
  `TOOL_DENIAL`/`TOOL_RESULT`/`TASK_NOTIFICATION`/`PEER_MESSAGE` before
  the generic `isMeta` check, so a line carrying both markers gets the
  more specific kind; `META` events gained a `subkind` (the line's
  `origin.kind`, else its leading XML-ish tag name, else `"plain"`).
  `classify_purpose` now checks intent signatures (local-llm-pipeline,
  workflow-run, review, test-triage, planning, docs-or-light-edit,
  refactor) before falling through to the generic
  `agent-fanout`/`general-dev` buckets; `review` relaxed to tolerate up
  to 2 edit turns, `test-triage` relaxed to drop its
  hits-vs-edit-turns comparison once hits reach 5; `build_section` now
  reports its active overnight local-time window as a note.
  `detect_archetype` (`workstyle.py`) now tests `chat-only` before
  `single-model` so a chat-only session with a single resolvable model
  family is no longer misclassified. Pre-split `cache_creation` reads
  (`parse.py`) are now treated as a format difference
  (`Diagnostics.pre_split_turns`) and normalized via
  `ttl.normalize_ttl_split`, rather than silently mis-parsed.
- **Report sections (`workstyle.py`, `workflows.py`, `snapshots.py`)**
  — added `build_section` to all three, matching the pattern already
  used by `classify`/`compaction`/`recache`/`ttl` (see Added above).
- **Statusline robustness (`statusline.py`)** — `_last_assistant_ts`
  now scans the transcript tail with `text.split("\n")` instead of
  `str.splitlines()` (the latter also breaks on `\r`/`\v`/U+2028/U+2029,
  which can legally appear inside a JSON string value and would shear a
  JSONL line into unparsable fragments); `main()` guards
  `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` in a
  `try`/`except`, and every print now goes through a `_safe_print`
  helper that never lets a print failure escape.
- **Test helpers (`tests/helpers.py`)** — `assert_privacy` now inspects
  `Table` cells and `Section` notes, not just top-level dataclass
  fields.

Two deviations from the original plan text, found and reported rather
than silently reconciled:

- **WP4 fidelity bar.** The plan's TTL break-even section (Appendix A4)
  is referenced elsewhere as flagging simulation fidelity above 5%, but
  Appendix A4 itself specifies "flag agent types above 10%" —
  `TtlThresholds.fidelity_warn_pct` implements the 10% figure that
  Appendix A4 actually states.
- **A2 META description.** Plan Appendix A2's detection table describes
  `META` lines without a `subkind`, but observed transcripts carry
  enough structure (an `origin.kind`, or a leading XML-ish tag in the
  content) to sub-classify them usefully — the events.py fix above adds
  `subkind` as an extension of A2's table rather than a literal
  implementation of it.

Further independent review (findings R4/R5/R7/R8/R15/R19/R23), each fixed
and landed as its own commit:

- **R4** — `parse.py`'s `_redact_paths` now also redacts relative
  Windows paths (no drive letter) and `<user@host>`-shaped `@`-tokens,
  not just absolute `C:\Users\<name>\...` paths.
- **R5** — `report.py`'s `--group-by agent`/`model`/`entrypoint` re-fold
  now keys `RecacheStats` by transcript instead of by session, so a
  subagent transcript is grouped under its own agent/model/entrypoint
  rather than inheriting its parent session's.
- **R7** — `report.py`'s scorecard context-hygiene stats
  (`median_ctx`/`median_top_level_ctx`) are now computed from top-level
  transcripts only, not every transcript (subagent ctx values, which run
  much larger, were skewing them).
- **R8** — `compaction.py`'s `compactions_per_session_mean` now divides
  by every session, not just the sessions that compacted; the old value
  is kept as `compactions_per_compacting_session_mean`.
- **R15** — `usage.py`'s five-hour usage blocks now assign each priced
  turn to the block containing that turn's own local timestamp, instead
  of stamping a whole session's turns onto the block its first turn
  landed in (a session spanning several blocks, e.g. 12+ hours, now
  splits across all of them).
- **R19** — stale "WP10 will..." notes in `compaction.py` and
  `topology.py` rewritten to describe current behaviour: the RE-CACHE
  join to the shared `recache.py` detector already landed (WP10b); the
  topology spawn-write/session-baseline tables were never joined against
  a session's snapshot MCP/plugin counts and no such join is planned.
- **R23** — `usage.py` now skips bundles with no top-level transcript
  (an orphaned subagent whose parent session was never discovered) the
  same way `report.py`'s `build_report` always has, so the two sections'
  session/turn counts no longer disagree on a corpus containing one.

Also landed alongside the above, same discipline:

- `report.py` — the "min sample" values printed alongside recommendation
  thresholds are now the ones `recommend()` actually applies
  (`RecommendThresholds`'s own `min_sessions`/`min_turns`, which can be
  overridden independently of `Config.min_sessions`/`min_turns`), not
  `config.min_sessions`/`min_turns` directly.
- `report.py` — the overview `totals` table gained two rows,
  `top_level_median_ctx` and `top_level_turns_ctx_ge_200k_pct`, computed
  from the same top-level-only record set as the R7 fix, giving the
  "long-context share of recent top-level turns" plan anchor a
  turn-count-basis, top-level-only figure to check against.

**Note from the round-3 review, resolved below:** `parse.py`'s redaction
behaviour changed under R4 above in a way that changes parsed output for
previously-cached transcripts (a path that previously leaked through
`_redact_paths` is now redacted) — `PARSER_VERSION` in `__init__.py`
needed bumping to invalidate stale cache entries, per that constant's
own doc comment. Not done in round 3: `__init__.py` was outside that
change's file scope. Done as part of the `0.1.0` release prep (see
"Release prep" below).

Independent review (round 3) fixes, `cli.py`/`recommend.py`/`ttl.py`:

- **Breaking:** removed the `--allow-titles` CLI flag. It implied a
  privacy control that never existed — `report.py`'s own module
  docstring documents `build_report`'s `allow_titles` parameter as a
  permanent no-op, since nothing anywhere in this codebase captures
  `customTitle`/`ai-title` text to gate in the first place.
  `build_report` still accepts the keyword (unused) for signature
  compatibility.
- New `--tz ZONE` flag overrides `config.toml`'s `tz` for a single run,
  threaded through every report-like subcommand and `config-diff`.
- `--quiet` now actually does something: it used to be accepted by
  argparse (mutually exclusive with `--verbose`) but never once
  consulted, so passing it silently changed nothing.
- `scorecard.py`'s `[thresholds.scorecard]` overrides are now validated
  for correct ascending/descending ordering; a misordered tuple used to
  silently score a corpus at the wrong level and now raises a clean,
  named `claude-token-lens report: ...` error (exit 2) instead.
- `init`/`baseline`/`serve`'s `--help` listing now leads with the same
  `(planned)` marker every other not-yet-implemented subcommand uses.

Independent review (round 4) fixes, merged from `fix-review3-a` into
`main` for this release, plus release-prep work:

- **Release prep.** `PARSER_VERSION` bumped `2` -> `3` in `__init__.py`
  (parse-time redaction changed under R4 above, invalidating
  previously-cached digests) and `pyproject.toml`'s version bumped to
  `0.1.0`, resolving the round-3 follow-up note above.
- **`cli.py`** — `report --json --patch-set` used to append the
  patch-set text after the JSON blob, producing invalid JSON on
  stdout. `--json` now embeds the patch set under a top-level
  `patch_set` string key instead of printing anything else to stdout;
  `--html`/`--csv-dir` write a sibling `patch-set.txt` file next to
  their output; Markdown mode is unchanged (still appends the patch
  set after the report text).
- **`recache.py`** — the two "Re-cache primary cause" tables had
  unstable row order for tied all-zero rows, caused by Python's
  randomized `StrEnum`/set-iteration hashing. Every sort in
  `build_section` now ties-break on the row key string, so output is
  deterministic across runs regardless of `PYTHONHASHSEED`; covered by
  a determinism test that builds the section twice from shuffled
  input.
- **Config-dir semantics** — `hooks/snapshot-config.py`'s
  `resolve_config_dir` treated an explicit `--config-dir` as the
  `~/.claude` root and appended `token-lens/snapshots`, while
  `cli.py`/`snapshots.py` already treated an explicit value as the
  token-lens directory itself. Reconciled on the majority convention:
  an explicit `--config-dir X` is the token-lens directory everywhere
  (`X/snapshots`, `X/cache`, `X/config.toml`, `X/usage-log.csv`);
  the default remains `~/.claude/token-lens`, honouring
  `CLAUDE_CONFIG_DIR`. `snapshots.load_snapshots` and the hook script
  (still standalone, no package import) were both updated, with a new
  round-trip test: the hook writes a snapshot with `--config-dir tmp`,
  then `config-diff --auto-keys --config-dir tmp` finds it.

### Documentation

- Retired stale forward-looking "WP8"/"WP10"/"WP12"/"WP3" notes in
  `classify.py`, `corpus.py`, and `ttl.py` now that those work
  packages have landed, replacing them with statements of current
  fact about what populates each field and why `recache_signature` is
  still unset in `report.py`'s TTL path today.
- README's Performance section now carries real timings (`22.1s` cold
  with `--jobs 1`, `7.6s` warm, `12.6s` warm with `--jobs 4`, measured
  2026-09-19 on the owner's own live 1.6 GB corpus: 120 sessions,
  1,644 subagent transcripts, 29 workflow runs, 30-day window)
  replacing the `<cold>`/`<warm>`/`<jobs4>` placeholders; the TTL
  section now also documents that `ttl.py` prints per-agent-type
  simulation fidelity and suppresses TTL-switch advice when the
  projected saving doesn't clear the simulation's own error margin or
  fidelity exceeds the configured bound.
- README rewritten against the code as it actually stands today (WP12b):
  what it measures and cannot (no billing API, user-supplied prices,
  subscription usage-window billing, the JSONL format's observed-not-
  published status and its two stable alternatives), the two token
  totals with a worked example, how Claude Code's prompt cache and its
  5m/1h TTL levers work, the RE-CACHE definitions and signatures, a
  section-by-section report reading guide (moved into
  `docs/sections-reference.md` once it grew past README-length) with a
  real worked example generated from `tests/fixtures/real/session-a`,
  the TTL simulation assumptions verbatim from `ttl.ASSUMPTIONS`,
  SessionStart-hook and statusline installation fragments generated
  from the code (not hand-typed), Windows-specific notes, an honest
  team/enterprise section naming what's implemented (`exclude_projects`,
  `retention_days`, managed-settings capture, Bedrock/Vertex provider
  detection) versus only planned (Foundry detection, per-provider
  pricing, an `export` command), prior-art credits, and licence/
  contributing/roadmap. `SECURITY.md` rewritten as a corporate-review
  sign-off checklist, correcting an earlier claim that an automated
  egress test already exists (it doesn't — there is no `serve` surface
  yet to test; the guarantee today is structural: no networking library
  is imported anywhere in the codebase) and clarifying that
  `--no-cache`/`--rebuild-cache` are parsed but not yet acted on by any
  CLI subcommand. Both files are written to match the CLI's actual
  current state: only `pricing-check` and `snapshot-config` are wired
  up; every other subcommand is a stub.
- README and `docs/sections-reference.md` brought up to date against
  the WP10c CLI wiring and WP10-merge `Recommendation.scope` (WP12c):
  the `<!-- CLI-USAGE -->` placeholder replaced with a subcommand table
  and exit-code reference generated from each subcommand's own
  `--help`; every "not yet built" callout that WP10a/WP10b/WP10c closed
  (`overview`, `usage`, `scorecard`, the `report` command, recommendations
  with `scope`, `--patch-set`, `--no-cache`/`--rebuild-cache` actually
  being wired, the `statusline` and `config-diff`/report `config`
  section distinction) rewritten or removed, verified by running the
  CLI against `tests/fixtures/real/session-a` and reading
  `report.py`/`recommend.py`/`scorecard.py`/`usage.py`/`probe.py`
  directly; the zipapp caveat fixed at the doc level (build against
  `claude_token_lens.__main__:main` instead of `cli:main`, verified with
  a fresh `.pyz` build whose `--version` exits 0 and `serve` exits 2);
  a new Performance subsection recording where the cold/warm/`--jobs`
  timings and the digest-cache-location note live; `docs/sections-reference.md`
  gained `overview`/`usage`/`scorecard` tables and Recommendations/
  Diagnostics blocks. The remaining genuine gaps (Foundry detection,
  per-provider pricing tables, `--allow-titles` being a no-op, and
  `usage_windows` not being wired into the assembled report) are kept,
  stated honestly rather than papered over.
