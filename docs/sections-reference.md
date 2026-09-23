# Report sections, in detail

This file is the field-by-field companion to
[`README.md`](../README.md#3-reading-the-report-sections). It lists every
table each `build_section(...)` function produces today, and expands the
two topics the README only summarises: the TTL section's utilisation
metrics, and a worked example against a real, scrubbed transcript.

`report.build_report` assembles these sections into one `ReportModel`,
in this order: `overview`, `usage`, `elasticity` (only under
subscription billing with usage-log readings), `sessions`, `recache`, `ttl`,
`limits`, `carry`, `compaction_sim`, `model_swap`, `waste`,
`compactions`, `agent_startup`, `agents`, `quality`, `workstyle`, `workflows`,
`phases` (only with `--phases`), `config` (only when config snapshots
exist), `context_budget`, `scorecard`, and `baseline_comparison` (only
with `--baseline`). `claude-token-lens report` prints it. This file
groups sections by topic, so its order differs.

These sections are not part of the assembled report: `config_diff`,
`compare`, `reconcile` and `team_report` (each printed by its own
subcommand), and `usage_windows` and `savers` (no subcommand prints
them yet; call their `build_section` directly).

Table names below are the exact `Table.name` values. A table's CSV
export is `<section key>__<table name>.csv`, and a recommendation's
evidence cites it by section key and table name. Each section can also
be produced by calling the named module's own `build_section` function
directly against `TranscriptResult`/`SessionRecord` objects from
`parse_transcript`, which is what the worked example at the end of this
file does.

## `overview` (`report.py`)

- `totals` — one `metric`/`value` row per corpus-wide total: sessions,
  top-level/subagent transcripts, workflow runs, priced turns, the four
  raw token counts (`input_tokens`, `cache_creation_tokens`,
  `cache_read_tokens`, `output_tokens`), the two derived totals from
  [Concepts section 1](concepts.md#1-the-two-token-totals)
  (`usage_tokens`, `new_tokens`), `total_cost_usd`,
  `cache_read_cost_share_pct` (cache-read cost as a percentage of total
  cost), `cache_roi` (from `ttl.py`'s cache-economy totals — see
  [`ttl_cache_economy`](#ttl-utilisation-metrics) below), and two
  top-level-only context figures: `top_level_median_ctx` and
  `top_level_turns_ctx_ge_200k_pct` (the share of top-level turns at
  200,000 tokens of context or more).
- `by_model` — turns, the four raw token counts and cost, one row per
  model id, sorted by cost descending.

## `usage` (`usage.py`)

Finance/enterprise-facing breakdowns by calendar period, project,
entrypoint, and (subscription billing only) a fixed 5-hour local-calendar
block grid — see [README section 1](../README.md#1-what-it-is-what-it-measures-and-what-it-cannot)
for the billing-mode distinction every money column in this section
respects.

- `by_day` / `by_week` / `by_month` — period x model:
  turns, tokens, cost. The period key is computed in `config.tz` (falling
  back to the machine's own local zone).
- `by_project` — sessions and cost per project slug.
- `by_entrypoint` — transcripts, turns, tokens, cost per
  `entrypoint` (e.g. `claude-desktop`, `claude-code`).
- `five_hour_blocks` — sessions, turns, tokens, cost per fixed
  00:00/05:00/10:00/15:00/20:00-local block, populated only when
  `config.billing == "subscription"` (a genuine per-account rolling
  5-hour window can't be observed from transcripts alone, so this is a
  documented, deterministic proxy grid instead); under `"api"` billing
  the table is empty with a one-line note explaining the skip.
- `pricing_unknown_models` — one row per model id that `pricing.toml`
  has no price for: `model_id`, `turns`, `tokens`. Those replies are
  priced at zero. Built by `pricing.PricingCoverage.as_table` from the
  same coverage count behind `meta.pricing.coverage_pct`, and appended
  onto this section by `report.build_report` only when at least one
  reply was unpriced. The `pricing-coverage` recommendation names these
  model ids. Shown under the Usage tab's advanced detail.
- `pricing_closest_match` — one row per model id that resolved only via
  `pricing.Pricing.resolve_model`'s longest-registered-id *prefix* step
  (`ResolvedRates.approximate`; not `"exact"`/`"alias"`/`"strip_1m"`, and
  not a `"cloud_strip"` that itself landed on an exact/alias id):
  `model_id`, `priced_as` (the registered id whose rate was used),
  `turns`, `tokens`. These replies count as priced (`coverage_pct`
  treats them as covered), but only at another, similar model's rate —
  make this visible instead of letting a report read as "every model has
  its own price" when `coverage_pct` is 100%. Built by
  `pricing.PricingCoverage.as_closest_match_table`, appended onto this
  section only when at least one reply matched this way, and named by
  the `pricing-coverage` recommendation alongside (or instead of) any
  unpriced model ids. Shown under the Usage tab's advanced detail.
- `pricing_fast_priced_as_standard` — one row per model id seen with at
  least one reply flagged `usage.speed == "fast"` whose rate card entry
  has no `[models."<id>".fast]` table, so it was priced at that model's
  standard rate instead: `model_id`, `turns`, `tokens`. Built by
  `pricing.PricingCoverage.as_fast_priced_as_standard_table`, appended
  onto this section only when at least one such reply exists. Shown
  under the Usage tab's advanced detail.
- `cache_ground_truth` (S1-exports) — one row per session: `session_id`,
  `rows_logged`, `warm_share` (percentage of *logged rows* — statusline
  refreshes, not wall-clock time — where `statusline.py`'s real,
  non-estimated `prompt_cache.warm` was `true`; refreshes aren't evenly
  spaced in time, so this can diverge from the share of wall-clock
  session time spent warm), `misses` (the peak/max `prompt_cache.misses`
  observed), `top_miss_causes` (a short `cause:count` summary, e.g.
  `ttl:2, tools:1`), and `mean_recache_tokens_if_cold`. Built by
  `statusline.py`'s `build_cache_ground_truth_table` from the usage-log
  CSV's `cache_*` trailing columns and appended onto this section by
  `report.build_report` only when it is given `usage_log_rows` (see
  below) — absent otherwise, same as `context_budget_statusline`.
  `top_miss_causes` (fix for review finding 5) is read from the wire's
  own cumulative `prompt_cache.miss_causes` per-cause counts (persisted
  as the `cache_miss_causes` column) whenever a session's log carries
  that field, rather than counting the sticky `last_miss_cause` once per
  logged row — the old approach re-counted one real miss on every quiet
  subsequent refresh, since `last_miss_cause` stays set until the next
  miss. Logs with no `cache_miss_causes` data at all (old-format rows)
  fall back to counting `last_miss_cause` only on a row where
  `misses` genuinely increased over the previous row for that session.

Every money column's label switches to "Cost (list-price equivalent
USD)" under subscription billing, and a section note repeats that these
are not real invoice lines.

## `sessions` (`classify.py`)

- `sessions_by_mode` — sessions, turns, subagents, median span and
  median human prompts per `mode`. First match wins, in this order:
  `overnight` (span over 4h once usage-limit pauses are discounted, a
  human gap over 60min, and real activity in the local 22:00-07:00
  window), `long-agentic` (a self-chained run, or subagents or at least
  30 turns with at most 10 human prompts), `interactive` (median human
  gap under 5min with at most 2 subagents), else `mixed`. A section note
  states the overnight window in use.
- `sessions_by_purpose` — the same columns by `purpose`. First match
  wins, in this order: `local-llm-pipeline`, `workflow-run`, `review`,
  `test-triage`, `planning`, `docs-or-light-edit`, `refactor`,
  `agent-fanout`, else `general-dev`.
- `sessions_detail` — the 50 most recently started sessions, one row
  each: project, mode, purpose, sources (whether each came from a
  `sessions.toml` override or the rule engine), start time, span, turns
  and subagent count.

Per-session `mode`/`purpose` overrides live in
`<config-dir>/sessions.toml` (`<config-dir>` defaults to
`~/.claude/token-lens`) and always win over the rule engine
(`config.load_session_overrides`).

## `recache` (`recache.py`)

Definitions: [Concepts section 3](concepts.md#3-cache-rebuild-definitions-and-signatures).

- `recache_summary` — transcripts, priced turns, re-cache turns and
  share, cache-creation tokens (re-cache vs. all), avoidable cost, and
  `unavoidable_limit_expiry_cost_usd` (re-cache turns right after a
  usage-limit pause, kept out of avoidable cost).
- `recache_signature_split` — `full-expiry`, `prefix-invalidated` and
  `limit-expiry` (the gap spanned a usage-limit pause): turns,
  cache-creation tokens, avoidable cost, median ctx, median gap. Every
  cause table below leaves `limit-expiry` turns out.
- `recache_gap_buckets` — re-cache turns and their control-group share
  (all priced turns), bucketed by inter-turn gap (`<1m`, `1-5m`, `5-15m`,
  `15-60m`, `>60m`, `unknown`), both by turn count and by cache-creation
  token volume — the control columns are what make an over-representation
  claim evidence rather than noise.
- `recache_preceding_tool` — the same shape, bucketed by the tool that
  immediately preceded the re-cache turn.
- `recache_top_command_prefixes` — the top preceding Bash/PowerShell
  command prefixes (≤40 chars) among re-cache turns.
- `recache_primary_cause` — re-cache turns and cache-creation tokens by
  `preceding_primary` event kind, each row's turn/token share compared
  against its control share, with an explicit
  "over-representation" column (`share - control_share`).
- `recache_primary_cause_prefix_invalidated` — the same, restricted to
  `prefix-invalidated` turns, with shares taken within that subset.
  Full-expiry turns are left out because their cache had fully expired
  whatever preceded them; a prefix-invalidated turn is the one a
  preceding event can actually explain.
- `recache_event_cooccurrence` — every event kind's presence (not just
  the precedence-resolved primary) among re-cache turns vs. control, so
  the precedence order never hides a contributing cause.
- `recache_attachment_subsplit` — attachment `subkind` breakdown for
  `prefix-invalidated` turns specifically.
- `recache_by_agent_type` — priced turns, re-cache turns and share,
  cache-creation tokens, avoidable cost, per agent type (`"top-level"`
  for the main conversation).
- `recache_huge_context` — turns at or above `huge_ctx` (default
  200,000 tokens) and their share of total cache-read volume: a
  context-hygiene metric, not a pricing surcharge (current-generation
  models bill the full context window at standard rates).
- `recache_by_group` — only with `report --group-by`: the
  `recache_summary` columns, one row per group.
- `measured_miss_causes` — only when the usage-log CSV carries the
  statusline's cache-miss causes: the main session's cache misses by the
  cause Claude Code itself reported (misses, share, sessions), to set
  beside the causes inferred above.

## `ttl` (`ttl.py`)

Simulation assumptions: [Concepts section 4](concepts.md#4-ttl-simulation-assumptions).

- `ttl_by_agent_type` — per agent type and `"top-level"`: spawns, priced
  turns, observed 5m/1h mix, gaps > 5min, gaps > 60min, limit gaps, gap
  p50/p90, cost observed / all-5m / all-1h, best policy, delta vs. best
  (USD and %), saving if switched, fidelity, unsimulatable and unpriced
  turns, a recommendation string, and the lever (`promptCacheTtl` for
  the top-level row; otherwise `experimental.cacheTtl` in `<agent>.md`,
  or `subagentPromptCacheTtl` for all subagents).
- `ttl_gap_distribution` — inter-turn gap histogram per agent type.

### TTL utilisation metrics

- `ttl_wasted_writes` — per agent type: `writes`, `wasted_writes`
  (never read back before the entry expired), `tokens_written`,
  `tokens_wasted`, `share` (wasted tokens as a percentage of tokens
  written, leaving out each transcript's last write), `usd_wasted`, and
  `terminal_writes` (those last writes, counted separately).
- `ttl_premium_waste` — for turns using a 1h TTL: tokens/USD where the
  extra write premium was never earned back by a hit a 5m TTL would have
  missed (`h1_not_needed_*`) vs. tokens/USD where it was earned
  (`h1_earned_*`) vs. tokens/USD where the 1h entry expired anyway
  (`h1_expired_*`); symmetrically for 5m turns, `m5_fine_tokens` (no
  loss) vs. `m5_loss_*` (expired when a 1h TTL would have survived) and
  `m5_would_expire_tokens` (would have expired under 1h too).
- `ttl_break_even_share` — `premium_all_1h`/`expiry_loss_all_5m` (what
  every write would have cost end to end under each fixed policy),
  `margin` (`expiry_loss_all_5m - premium_all_1h`, positive means 1h
  wins), `in_window_pct` (the prefix-weighted share of gaps between 5
  and 60 minutes) against `break_even_pct` (the share at which 1h starts
  to pay), and a `verdict`: `"marginal"` when the margin is within 5% of
  the larger side or under $1.00, else `"1h pays"` or `"5m pays"`.
- `ttl_near_miss` — turns whose gap fell within `near_miss_window_s`
  (default 60s) of a TTL boundary, on the side that just missed and the
  side that just made it, for both the 5m and 1h boundaries.
- `ttl_addressable_share` — per agent type, re-cache tokens, USD and
  share split by signature: `full_expiry_*` (TTL-addressable: the entry
  expired, so a longer TTL could have kept it) vs.
  `prefix_invalidated_*` (content-addressable: something upstream of
  the cached prefix changed, which no TTL choice fixes).
- `ttl_cache_economy` — per agent type plus an `overall` row:
  `tokens_written`, `tokens_read`, `write_usd`, `read_usd`,
  `uncached_equivalent_usd` (every cache token priced as plain input),
  `net_saving_usd` (uncached-equivalent minus write and read USD), and
  `cache_roi` (net saving divided by write USD) — a standalone "is
  caching worth it at all" number.

Fidelity self-check: `ttl.dominant_ttl`/`ttl.fidelity` replay the
simulation at a transcript's own dominant observed TTL and compare it to
observed cost; `TtlThresholds.fidelity_warn_pct` (default 10.0) is the
flag threshold `build_section` applies per agent type.

## `limits` (`limits.py`)

Full field-by-field contract: [`docs/limits.md`](limits.md#the-limits-report-section).

A usage-cap pause (the harness pausing when the account hits its 5-hour/
weekly limit), a harness-forced early subagent termination, and the
desktop app's resume ping, turned into first-class facts rather than
behavioural noise (see `docs/limits.md`'s module-docstring summary for
why an unattributed pause otherwise misreads as an ordinary long idle
gap in `recache`/`ttl`/`sessions`).

- `limits_summary` — one "all" row: transcripts, sessions affected,
  limit hits (session + weekly split), resumes, agents terminated (and
  by rate limit specifically), pause count/total time, and the
  cache-creation tokens/write cost paid by the turn immediately
  following each pause.
- `limits_hits_by_kind` — `session_limit`/`weekly_limit` hit counts and
  share.
- `limits_agent_terminated` — `rate_limit`/`other` termination counts
  and share.
- `limits_pauses` — corpus-wide pause count/total/mean duration.
- `limits_reset_hour_histogram` — count and share of `LIMIT_HIT` resets
  by local hour of day (0-23).
- `limits_by_agent_type` — per-agent-type roll-up: hits, resumes,
  terminations, pause count/total/median/max, and the post-pause
  cache-creation tokens/cost.
- `limits_csv_cross_check` — transcript-derived hit counts vs.
  `usage-log.csv`'s own exhaustion-row counts for `five_hour`/
  `seven_day`, appended as an extra table on this section only when
  `report.build_report` is given `usage_log_rows` (same "extra table
  bolted on" convention `cache_ground_truth` uses for the `usage`
  section above).

`scorecard.py`'s `cache_efficiency` dimension excludes the portion of
re-cache share already known to be pause-forced
(`ScorecardInputs.limit_recache_share_pct`); `data_quality` notes the
count of sessions with at least one pause
(`ScorecardInputs.limit_pause_sessions`) and, separately, the count of
turns counted as priced above that were only priced by closest match
rather than their own model's rate
(`ScorecardInputs.closest_match_turns`, from
`pricing.PricingCoverage.closest_match_turns` — see the `usage`
section's `pricing_closest_match` table above). Neither note changes
the `pricing_coverage_pct` metric or level itself, which already counts
a closest-match turn as priced. `recommend.py`'s `limit-pressure` rule
fires off this section's own `limits_summary` counts.

## `carry` (`carry.py`)

Full field-by-field contract: [`docs/carry.md`](carry.md#the-carry-report-section).

Every other section prices a tool result once, at the turn it entered
context. `carry` prices it again for every later turn it keeps riding
along inside the cached prefix — re-read at the flat `cache_read` rate,
or re-written at a `cache_write_5m`/`cache_write_1h` rate on a re-cache
— until a `COMPACT_BOUNDARY` drops it or the transcript ends.

- `carry_by_tool` — per tool name: carried-result count, tokens
  entered, mean turns carried, carry tokens, carry cost, and that
  tool's carry-token share of the corpus's total cache volume (an
  attribution share, not a partition — rows need not sum to 100%,
  since one physical cache read carries every still-live result in
  that turn's prefix at once).
- `carry_by_agent_type` — the same roll-up keyed by agent type
  (`"top-level"` for the main session).
- `carry_top_results` — the single most expensive individual carried
  results corpus-wide: tool name, agent type, tokens, turns carried,
  cost — no content, path, or command.
- `carry_truncation_savings` — for each configured cap in
  `CarryThresholds.truncation_tokens` (default 2,000 and 8,000 tokens):
  how many carried results exceed it and the exact tokens/USD saved had
  every one been capped there, computed by linear scaling rather than
  re-simulation (carry cost is exactly proportional to a result's own
  token size for a fixed run of later turns).
- `carry_output_cap_savings` — the same saving for each output-cap
  setting the tool-output check suggests (`BASH_MAX_OUTPUT_LENGTH` at
  15,000 characters over Bash and PowerShell results,
  `MAX_MCP_OUTPUT_TOKENS` at 10,000 tokens over MCP results), with what
  carrying the results it covers cost. `carry_by_tool` and
  `carry_by_agent_type` also carry `saving_if_capped_usd`, each row's own
  saving at `big_result_tokens`.

`recommend.recommend()` runs the `tool-output-carry` rule
(`carry.RULES`). It fires when a tool's carry-token share of cache
volume is more than `CarryThresholds.carry_share_pct` (default 25%) on
at least `min_sample_results` (default 5) carried results. Its action
names a workflow lever (truncate long Bash/PowerShell output, prefer
`Grep` over `Read`, cap agent report length) and cites that tool's own
`saving_if_capped_usd` (its results capped at `big_result_tokens`,
default 8,000) as the projected saving.

## `compaction_sim` (`compaction_sim.py`)

The `autoCompactWindow` sweep: full write-up and worked example in
[`docs/compaction-sim.md`](compaction-sim.md).

- `compaction_sim_by_window` — top-level sessions only, one row per
  candidate window (100k/150k/200k/250k/300k/400k/500k/`none`):
  simulated compactions per session, mean ctx, total cost, and delta vs.
  the observed (`none`) cost in USD and percent — **negative delta means
  cheaper**, the opposite sign convention to `ttl`'s own delta columns
  (see the module docstring for why).
- `compaction_sim_by_agent_type` — every agent type's (`"top-level"` and
  each subagent type) sessions, observed cost, best candidate window,
  its cost, the saving vs. observed (0 floor), the delta in percent, and
  a recommendation string naming the window.
- `compaction_sim_fidelity` — for each top-level session whose project
  snapshot carries a known configured `autoCompactWindow`: that
  configured window, the simulated cost at it, the observed cost, and
  the fidelity gap in percent. A gap above
  `CompactionSimThresholds.fidelity_warn_pct` (default 10%) says the
  model's assumptions don't hold for that session.

A simulated compaction fires at the window less this corpus's own
trigger reserve (median `window − preTokens` across real auto
compactions under a known window), and resets context to the session's
own starting context plus a summary of this corpus's median `postTokens`.
It charges the summary request (never logged in the transcript) and the
reply after it re-caching its whole context, with the share of the
starting context real compactions still read from cache read, not
written. Files re-read after a summary aren't charged by the sweep. A
real, already-observed compaction is kept as-is under every candidate
window rather than re-simulated.

`recommend.recommend()` runs the `compaction-window` rule (lever
`autoCompactWindow`, category `settings`). It names a floor ("at least
W"), not a single best window: the smallest window with at most 2
simulated compactions per session whose saving, after a rediscovery
cost (this corpus's median post-compaction re-cache write cost per
redundant read in `topology_redundant_reads`), is still more
than 5% of observed cost (`1 - switch_pct`) and more than $1.00
(`switch_usd`). The action says the figure is modelled, not observed.
See [`docs/compaction-sim.md`](compaction-sim.md#the-report-section).

## `model_swap` (`model_swap.py`)

Full contract: [`docs/model-swap.md`](model-swap.md).

For each agent type (and the top-level conversation), reprices every
already-observed priced turn at every model `pricing.toml` carries —
same tokens, same observed 5m/1h cache-write split, same `price_turn`
the rest of the engine uses — and reports the ceiling saving from
moving one tier down (fable -> opus -> sonnet -> haiku, via
`workstyle.model_tier` and `Pricing.aliases`, never a hardcoded id).
Every figure is a price ceiling at today's usage shape, not a
prediction: a smaller model may need more turns or fail the task
outright, and neither possibility is represented here.

- `model_swap_by_agent_type` — spawns, priced turns, unpriced turns
  (unknown model), observed model, observed cost, a `Cost at
  <model-id>` column per model in the rate card, the best cheaper
  alternative (model id and label), and the ceiling saving in USD and
  %. A row's alternative is empty and its saving `0.0` whenever the
  observed model is already the cheapest available, its own volumes
  already beat the next tier down, or the family/tier can't be
  determined — the table never implies a saving where none exists.
  Each row also carries the `lever` to change.
- `model_swap_summary` — `scope`, `agent_types`, `observed_cost_usd`,
  `cost_after_tier_down_usd`, `saving_usd` and `saving_pct`: the
  corpus-wide ceiling if every subagent type currently on Fable or Opus
  moved one tier down (excludes top-level and any Fable/Opus type
  already cheaper than its next tier).

`recommend.recommend()` runs the `model-tier` rule
(`model_swap.RULES`). It fires per qualifying row (real cheaper
alternative, sample and saving thresholds cleared) and names the exact
lever: `settings.json`'s `"model"` key for the top-level conversation,
or the subagent's `.claude/agents/<type>.md` frontmatter `model:` line.

## `waste` (`waste.py`)

Full field-by-field contract: [`docs/waste.md`](waste.md#the-waste-report-section).

Prices the turns whose output the user never actually benefited from —
a failed tool call, a turn the user interrupted, one stopped by a tool
denial, or every turn in a subagent transcript the harness killed
before it could report back — and attributes each to a cause with a
lever, so a "recoverable spend ceiling" always points at what to change
to stop paying for it again. Purely a reader of state `parse.py`/
`events.py` already produce (the v4-wasted-turns parser addition,
`Turn.tool_error_count`/`Turn.tool_error_chars`, plus the existing
`EventKind.INTERRUPT`/`TOOL_DENIAL`/`API_ERROR` and `TranscriptMeta.
stopped_by_user`) — it detects nothing new. A turn following a
usage-cap pause (`Turn.gap_cause == "limit"`) is excluded outright,
since `limits.py` already owns that attribution.

- `waste_summary` — one "all" row: total priced turns/cost, wasted
  turns and their share of all priced turns, wasted cost (the
  recoverable spend ceiling) and its share of all priced cost, wasted
  tokens, the limit-pause-excluded count, and the api-error-retry count
  (frequency only, never priced).
- `waste_by_cause` — one row per cause (`tool-error`, `interrupt`,
  `tool-denial`, `max-turns`, fixed order) plus an `api-error-retry`
  row: turns, share of all priced turns, cost, share of all priced
  cost, tokens, and that cause's own lever text.
- `waste_by_agent_type` — per-agent-type roll-up: turns, share of
  turns, cost, share of cost, tokens; sorted descending by cost.
- `waste_top_sessions` — the 20 sessions with the highest wasted cost:
  a salted, non-reversible session hash, turns, cost, share of cost,
  and a cause-mix string.

Every `share_pct` column here is against the whole corpus's priced
turns/cost, not just the wasted subset, so `waste_by_cause`'s shares
sum to `waste_summary`'s own totals. `recommend.recommend()` runs the
`wasted-turns` rule (`waste.RULES`). It fires when `waste_summary`'s own
`wasted_cost_share_pct` clears `WasteThresholds.share_pct` (default
10%) and the corpus meets the usual minimum-sample gate, naming the
dominant cause and its lever.

## `compactions` (`compaction.py`)

- `compactions_summary` — one metric/value row each: sessions with ≥1
  compaction, total sessions, compactions per session (mean/max),
  compactions per compacting session, pre/post-compaction tokens
  (median), dropped tokens in total, dropped tokens as a share of
  `cache_creation` and of `new_tokens` (`input_tokens +
  cache_creation_tokens`), mean compaction duration, total
  post-compaction write cost, and the part of it on turns flagged as a
  re-cache.
- `compactions_trigger_mix` — trigger value (`auto`/`manual`/`unknown`)
  counts and share.
- `compactions_per_session` — top 20 sessions by dropped tokens:
  session, compaction count, dropped tokens, post-compaction write cost.

Post-compaction
write/recache cost aggregates exclude any join to the next turn that
took longer than 15 minutes (the join is presumed stale, not a genuine
immediate-post-compaction cost).

## `agents` (`topology.py`)

Answers "how do tokens, cost and information flow between a session and
the agents/skills/workflows it spawns" with numbers only:

- `topology_spawn_write` — downward: mean/median first-turn
  `cache_creation` per agent type (the briefing + system prompt + preloaded
  skills a new spawn pays for), plus the mean `agent_brief_chars` (mean
  briefing chars) the spawning turn handed that agent type.
- `topology_session_baseline` — the top-level session's own first-turn
  `cache_creation` (system prompt + `CLAUDE.md` + prefix-loaded tool
  schemas) across sessions, for baseline-bloat comparison.
- `topology_upward_tool_result` — upward: `Agent`/`Workflow` tool_result
  sizes, the report that lands back in the parent's context, by agent
  type.
- `topology_report_proxy` — a per-subagent report-size proxy by agent
  type, for transcripts where the parent-side tool_result isn't directly
  available.
- `topology_skills_rollup` — a skill's own direct cost plus every agent
  (and workflow) it spawned, recursively via `parentAgentId`: invocations,
  direct cost, spawned cost, mean spawns per invocation, mean report
  size.
- `topology_spawn_depth` — spawn-depth histogram.
- `topology_cost_per_spawn` — cost per spawn by agent type, plus the mean
  `tool_wait_s` (mean tool wait) across that agent type's priced turns.
- `topology_chains_summary` — `stoppedByUser`/`maxTurns` truncation
  signals.
- `topology_reminder_hook_pressure` — attachment/hook-output counts per
  turn, by transcript kind.
- `topology_cache_signal_histogram` — `CACHE_SIGNAL` subkind counts
  (model switches, thinking-stripped, ultra-effort enter/exit, deferred/
  prefix-loaded tool deltas, plan-mode/auto-mode transitions, output
  style changes).
- `topology_mcp_cost` — cost by `attribution_mcp_server`.
- `topology_effort_tokens` — output and thinking tokens by the
  session's `effort` setting.
- `topology_per_turn_effort_tokens` — the same, by `per_turn_effort`.
- `topology_effort_by_agent_type` — output and thinking tokens and the
  thinking share, by agent type.
- `topology_context_composition` — context composition per turn
  (baseline / tool results by tool / assistant output / notifications
  and attachments / compaction summaries), averaged per transcript kind.
- `topology_redundant_work` — repeated Bash/PowerShell command prefixes
  and post-compaction rediscovery signals ("how much am I paying to
  re-learn").
- `topology_redundant_reads` — the same file (by salted
  `Turn.read_target_hashes`, never a path) read more than once in a
  session, and how many of those repeats land within a compaction's
  rediscovery window; every count is 0 unless the corpus load wired up a
  hashing salt (see `parse.load_or_create_salt`).

## `quality` (`quality.py`)

Whether the work went well, not just what it cost. One *run* is one
transcript (a main session or one subagent run); every signal is a
ratio of two counts summed over runs. Definitions, the significance
test and privacy are in [concepts](concepts.md#7-quality-signals).

- `quality_by_agent` — per group (the main session, each agent type,
  and all subagents pooled when there are two types or more): runs,
  then as shares didn't finish, likely out of turns, retried on a
  larger model, failed tool calls,
  failed shell commands, denied, stopped by you, corrections, edited
  again and hit the output limit, then replies and cost per run. A
  signal that doesn't apply to the group (corrections for a subagent,
  say) is blank.
- `quality_by_setup` — per agent type, model and effort (the model and
  effort most of a run's replies used; runs that never replied are left
  out): the main shares and per-run measures, the setup compared with
  (the one that agent used most), a verdict (`only`, `baseline`,
  `worse`, `possibly_worse`, `better`, `possibly_better`,
  `no_clear_difference`, `too_little_data`) and the difference in
  words. Setups ran at different times on possibly different work. The
  retried share is shown but not compared (the largest model can never
  be retried on a larger one).
- `quality_retried` — per agent type and model with at least one run
  retried on a larger model (`quality.retried_rows`; the rule is in
  [concepts](concepts.md#7-quality-signals)): runs that edited files,
  retried runs and their share, files edited again against files those
  runs edited, how many of the retries said `[retry: model]`, the model
  the retries most often used, and the day of the latest.
  `quality.retried_models` turns it into the
  `{(agent, family): row}` guard the models recommendation, the Models
  quick action and the Profiles models goal check, alongside
  `quality.worse_models`.
- `quality_retry_reasons` — per agent type and model whose runs were
  retried with a brief that said why (`quality.retry_reason_rows`; the
  markers are in [concepts](concepts.md#7-quality-signals)): retries
  that said why, then how many said the model, the brief, tools or
  other, and the day of the latest.
- `quality_failing_tools` (advanced) — agent type, tool, failed calls
  and runs with a failure, top 25.
- `quality_counts` (advanced) — the raw counts behind every share:
  replies, tool calls, failures, denials, messages, corrections, edits,
  summaries, files edited again on a larger model, the recorded
  outcomes (reported done, failure, stopped, other, none recorded), cut
  off, likely out of turns, retried on a larger model, ended early,
  never replied, and runs whose last reply said `[result: done]`,
  `partial` or `blocked`.
- `quality_markers` (advanced) — one row per marker (`[retry: ...]`,
  `[result: ...]`): what it records, agent runs with it, agent runs that
  could have (Explore and Plan, which start without CLAUDE.md, can't
  write a result marker), the share, how many said each word, and about
  how many output tokens writing them took and what that cost at the
  writing model's output price.

## `workstyle` (`workstyle.py`)

- `workstyle_archetypes` — one row per detected archetype
  (`overseer-fanout`, `plan-high-implement-low`, `workflow-heavy`,
  `effort-varied`, `chat-only`, `single-model`, else `mixed`): sessions,
  share and a one-sentence description. Each session gets the first
  archetype whose evidence (model by role, effort spread, spawn counts,
  plan-mode-then-lower-model-implementer sequences) it matches, in that
  order. `recommend.py` conditions
  on this archetype so an overseer session is never told to "stop
  spawning agents" and a chat-only session is never told about subagent
  TTLs — see [Recommendations](#recommendations-recommendpy) below.

## `workflows` (`workflows.py`)

From `<session>/workflows/wf_*.json` run files:

- `workflows_summary` — one metric/value row each: total runs, total
  agents spawned, total cost, mean agents per run, mean cost per run.
- `workflows_status_mix` — run `status` (`completed`/`killed`/other
  observed values) counts and share.
- `workflows_detail` — the costliest runs, one row each: run id,
  session id, status, agent count, phase count (never a phase's
  `detail`, which can carry workflow source or prompt text), cost,
  started/finished.

## `phases` (`phases.py`)

Classification rule (`classify_turn_phase`, first match wins): **OTHER**
if the turn used no tools; **DISCOVERY** if every tool used is one of
Read/Grep/Glob/WebFetch/WebSearch/ListAgents and the turn made no edit;
**IMPLEMENTATION** if the turn made a "real" (non-scratch) edit or ran a
Bash/PowerShell command that isn't a recognised test/build tool;
**VERIFICATION** if the turn ran a recognised test/build tool (`pytest`,
`python -m pytest`, `dotnet test`/`build`, `npm test`, `npx
vitest`/`playwright`, `go test`, `cargo test`, `make`, `mvn`, `gradle`)
or made a scratch (temp-dir) edit; **OTHER** otherwise. A turn that both
edits *and* runs a test/build command in the same turn lands in
IMPLEMENTATION, since DISCOVERY/IMPLEMENTATION are checked first — noted
here since a turn usually does one or the other, not both.

- `phases_summary` — turns, new tokens, cache-read tokens, output
  tokens, cost and cost share per phase.
- `phases_by_transcript_kind` — the same, cross-tabbed by transcript
  kind (`top-level`/`subagent`/`workflow-agent`).
- `phases_by_agent_type` — the same, cross-tabbed by agent type.

A DISCOVERY cost share above the module's threshold (default 35%, only
evaluated when `--phases` was given) feeds the `discovery-share`
recommendation — see [Recommendations](#recommendations-recommendpy)
below.

## `config` (`report.py` via `snapshots.py`) and `config-diff` (CLI-only)

The assembled report's own `config` section renders one
`config-diff-<key>` table per config key that changed across the
window's `snapshot-config` snapshots (the first 20 keys alphabetically —
`report._MAX_CONFIG_DIFF_KEYS`), automatically, with no key to name.
When snapshots exist it also adds:

- `effective-config` — per project, each key's value in the latest
  snapshot and the settings layer it came from.
- `config-layers` — per project and settings layer: whether the layer
  file is present, and its agents, skills, rules, CLAUDE.md bytes,
  commands and MCP servers.
- `config-groups` — projects that share the same effective config
  (hash, project count and list, sessions).
- `config-drift` — only when observed session values exist: sessions
  whose observed value (for example the model) differs from the
  snapshot's.

The standalone `claude-token-lens config-diff` subcommand, described
next, is a separate, narrower consumer of the same underlying table
function for when you want exactly one key (or every changed key)
outside a full report run.

## `config_diff` (`snapshots.py`)

Reads the JSON files `hooks/snapshot-config.py` writes (see the
README's [installation section](../README.md#4-installing-the-sessionstart-hook-and-the-statusline)).

- `config-diff-<key>` — `config-diff --key KEY` prints one, and
  `config-diff --auto-keys` prints one per changed key. Per distinct
  value of that config key across a window: sessions, turns, cost, cost
  per session, re-cache share, compactions per session, median span. A
  table note lists the keys that also changed in the same snapshot,
  since a before/after comparison across two different snapshots can't
  isolate one key's effect from everything else that changed alongside
  it.

The subcommand prints these tables directly as Markdown.
`snapshots.build_config_section` wraps the same table in a
`config_diff` section for a library caller; nothing in the CLI or the
report calls it.

`snapshot_for(session, snapshots)` joins a session to the latest snapshot
whose timestamp is at or before the session's start; `diff_keys` and
`co_changed_keys` are the lower-level functions this table is built from.

## `compare` (`compare.py`) — CLI-only

`claude-token-lens compare --a <spec> --b <spec>` (v0.3 "Feature
expansion" item 6). Not part of `report.build_report`'s fixed section
list — a standalone comparison of two independently-selected arms of
sessions, each named by a `window:<since>..<until>`, `key:<key>=<value>`
(a flattened `snapshots.flatten_snapshot` key), `profile:<id>`, or
`project:<slug>[,<slug>...]` spec (see [`docs/compare.md`](compare.md)
for the full grammar). A session can match both arms, neither, or
exactly one — the specs are independent membership tests, not a
partition.

- `compare_overview` — twelve metrics, one row per metric, Arm A/Arm
  B/delta/delta-%% pre-formatted as display text rather than raw numbers
  (see the module docstring: the `Table` contract's one-`kind`-per-column
  rule can't otherwise fit five different metric kinds in one narrow
  table). The headline nine are per-session means or ratios — sessions,
  priced turns per session, cost per session, new tokens per session,
  cache-read share, re-cache share, compactions per session, median
  session span, mean first-turn cache-creation write — so a delta
  reflects a real behavioural difference rather than one arm simply
  having more sessions than the other (review finding S4: arm *totals*
  used to lead the table, so a bigger arm always showed a large delta
  regardless of any per-session change). The three arm totals (priced
  turns, cost, new tokens) are kept as separate rows labelled "...
  (informational)" further down the table rather than dropped. A
  `sample_ok` column (`yes`/`no`) flags whether *both* arms cleared
  `--min-sessions`.
- `compare_by_stratum` — the same two arms split by `--stratify`
  (`purpose`, `mode`, or both — default `purpose,mode`), with a reduced,
  raw-valued metric set (session counts, a `sample_ok` flag, cost and
  new tokens per session, cache-read share, and a note) so this table's
  own CSV/JSON export stays numeric. A stratum
  below `--min-sessions` in either arm shows its session counts only,
  every metric cell blank, and a note explaining the suppression.
- `compare_co_changed` — only populated when *both* arms are
  `key:`-selected: the other flattened config keys that differed between
  each arm's "representative" snapshot (the snapshot most of that arm's
  sessions actually joined to), excluding the arm's own compared key.
  Empty with an explanatory note for any other arm-kind combination.

Every table's notes always carry the plan's "observed, not controlled"
caveat (Risks and gaps item 2: correlation is not causation) plus each
arm's own exact selection rule, so a delta is never presented as
evidence the arm's own setting *caused* it. The `profile:<id>` arm form
is implemented and tested, but nothing copies the `profile_id` the
`snapshot-config` hook records onto `SessionRecord.profile_id`, so it
currently matches zero sessions in any real corpus.

## `reconcile` (`reconcile.py`) — CLI-only

`claude-token-lens reconcile --admin-csv <file> [--by day|model|day,model]`
(plan "Enterprise use"/"Finance"). Also not part of the assembled
report — an entirely offline comparison of this tool's own per-turn
accounting against a CSV export you already pulled from the Anthropic
Console/Admin API. No network call is ever made.

- `reconcile_by_period` — one row per distinct day (and/or model, per
  `--by`), each token metric (input, cache-creation, cache-read, output)
  and cost as four columns, `<metric>_local`, `<metric>_admin`,
  `<metric>_delta` and `<metric>_delta_pct` (delta is always local
  minus Admin; delta-% is against the Admin figure), plus a fixed
  `TOTAL` row. `--days`/`--since`/`--until` (the same common-parser flags
  every other subcommand uses) restrict both the local and the Admin
  side to the same window before grouping.

The Admin CSV's header row is mapped tolerantly (see
[`docs/compare.md`](compare.md#admin-csv-column-mapping) for the exact
mapping table) — every header spelling and the `_5m`/`_1h`
cache-creation-split convention are this module's own assumption, not
confirmed against Anthropic's published export schema. A column the
mapper can't place is listed, not silently dropped, in a section note;
another note always lists the fixed set of reasons a correct local
figure and a correct Admin figure can still legitimately differ
(subscription usage having no Admin cost, a shared API key used by
other tools, workspace filters on the Admin export, UTC-day-boundary
disagreement, and an unknown model priced at zero locally).

## `usage_windows` (`tools/log_usage.py`)

Not to be confused with the report's own [`usage`](#usage-usagepy)
section above (`usage.py`, day/week/month/project/entrypoint cost from
transcripts) — this section is about your Claude Code *subscription's*
5-hour/7-day plan percentages, from an entirely different data source:
`~/.claude/token-lens/usage-log.csv` rows appended by the statusline
logger, or by `python -m claude_token_lens.tools.log_usage` from a
pasted `get_usage` result, deduped by session/reset-time/used
percentage. It has a `build_section` function like every other section
here, but nothing in `report.py`/`cli.py` calls it yet, so it doesn't
appear in `claude-token-lens report`'s output — call it directly:

- `usage_windows_latest` — per window (`five_hour`/`seven_day`/
  `spend_limit`), the
  latest used percentage, its reset time, and sample count. Comes back
  empty (with a note) when no usage-log rows exist yet.
- `usage_windows_regression` — a simple linear fit of used-percentage
  against cumulative new-token volume within a reset period, per window:
  slope (`% per million tokens`), intercept, sample count, and the reset
  period it was fit against. Requires `token_totals_by_window` to be
  supplied by the caller; skipped with a note when not enough samples
  exist.

## `agent_startup` (`context_budget.py`)

What each subagent type is given before its first turn. Built from each
subagent transcript's events before its first priced turn
(`ContextBudgetStats.add_subagent`); sizes are characters / 4. A fork
(its first turn reads most of the parent's context from cache, or its
agent type is `fork`) is counted in `fork_spawns` and kept out of every
average. No tables and one note when nothing was measured.

- `agent_startup_breakdown` — per agent type: `spawns`, `fork_spawns`,
  `startup_tokens` (the first turn's whole input), the mean per spawn of
  `task_prompt`, `claude_md`, `skills_listing`, `tool_lists`,
  `hook_context`, `other_attachments`, `system_prompt` and
  `tool_definitions` (the last two only when a system-prompt snapshot
  was recorded), `not_recorded` (the rest), `measured_pct`, and
  `write_price` (the first turn's model's 5-minute cache-write list
  price per million tokens, used to price each part).
- `agent_startup_unused` — per agent type: spawns measured, the skills
  list size, spawns given it and spawns that called the Skill tool,
  spawns offered MCP tools and spawns that called one, the CLAUDE.md
  size and spawns that only used search and read tools.
- `agent_startup_shared` — parts (CLAUDE.md by source, and the other
  parts) that at least half the agent types receive at about the same
  size, with where they come from and the total across spawns.

## `context_budget` (`context_budget.py`)

Answers the owner question "do we track preloaded skills, the system
prompt, and the autocompact buffer?" A transcript never carries those
sizes directly, so every column ending `(est)` is a clearly labelled
*estimate* built from what is captured (first-turn `cache_creation`, a
HUMAN_TEXT/`skill_listing` attachment's own `size_chars`, a schema-2
config snapshot's `content_layers`) — Claude Code's own `/context` view
remains the authoritative breakdown; treat every `(est)` figure here as a
rough proxy, never as ground truth. Skipped cleanly (no tables, one note)
when the corpus has no top-level transcripts at all.

- `context_budget_baseline` — per project, plus one `all` row summing
  every project: the measured mean/median top-level first-turn
  `cache_creation` (the same metric `agents`' `topology_session_baseline`
  reports, computed independently here rather than read back off that
  table), next to estimated buckets in tokens for `human_prompt` (the
  first HUMAN_TEXT event's `size_chars`, or the first turn's own
  `human_prompt_chars`, divided by 4), `skills_listing` (every
  `skill_listing` attachment's `size_chars` preceding the first turn,
  divided by 4), `memory_files` (the joined schema-2 snapshot's
  `content_layers` CLAUDE.md family + rules bytes, divided by 4; `null`
  without a snapshot), `custom_agents` (the snapshot's agent count times
  a labelled 60-tokens-per-agent-listing constant; `null` without a
  snapshot), `mcp_tools` (`"present, size unknown"` when the snapshot
  names at least one MCP server, else `null` — this module has no way to
  measure an MCP server's own tool-schema size), and
  `system_prompt_and_tools` — the residual: mean baseline minus every
  other known `(est)` bucket, floored at 0. The `all` row's
  snapshot-derived buckets are always `null` (they can't be meaningfully
  combined across different projects' own snapshots).
- `context_budget_autocompact` — per project: the configured
  `autoCompactWindow` from the latest schema-2 snapshot's effective
  settings (`null` if absent), the model's context window size (from a
  statusline ground-truth row when one is available for the project,
  else assumed as 1,000,000 for a `"[1m]"` model alias or 200,000
  otherwise — `context_window_source` names which), the *observed*
  effective autocompact threshold (median `compactMetadata.preTokens`
  over this project's own `trigger == "auto"` compactions —
  `compaction.effective_autocompact_threshold`), the implied buffer
  (window minus threshold), how many auto compactions were observed, and
  whether the observed threshold drifted more than 10% from the
  configured window (`null` when either figure is unavailable).
- `context_budget_statusline` — one real, non-estimated line per
  session (last reported used tokens, window size, used percentage),
  present only once at least one usage-log row carries `context_window`
  fields (see `statusline.py`'s module docstring for how those columns
  get there — `python -m claude_token_lens.statusline` appends them to
  the same usage-log CSV `usage_windows` already reads, as three new
  trailing columns old-format rows simply don't have). Empty with a note
  otherwise. `claude-token-lens report` (S1-exports) now loads
  `<config_dir>/usage-log.csv`, when present, with a tolerant reader and
  passes the resulting rows into `build_report` as `usage_log_rows` —
  so both this table and `cache_ground_truth` above populate for the
  ordinary CLI report too, not only for a caller that constructs
  `usage_log_rows` itself. These rows are scoped to the report's own
  `--days`/`--since`/`--until`/`--project` window (fix for review
  finding 8) — the usage-log CSV can span a session's entire history, so
  without this scoping a narrow-window report would silently mix in
  ground-truth rows logged long before or after the reported period.

`recommend.py`'s `baseline-bloat` rule (see
[Recommendations](#recommendations-recommendpy) below) cites this
section's sized buckets as its evidence, and names the largest one in
its action text, whenever `context_budget` is present in the report —
falling back to its older single-mean-baseline evidence otherwise.

## `savers` (`savers.py`)

Full field-by-field contract: [`docs/savers.md`](savers.md#the-savers-report-section).

Whether an installed third-party token-saver tool (an MCP server,
plugin, or skill claiming to save tokens) actually nets a saving once
its own overhead is paid for. Detection merges an explicit
`config.toml` `[savers] names = [...]` allowlist with auto-detection: a
case-insensitive regex (`token|saver|savior|optimi[sz]|compress|
context|memory|cache|lean|trim|condens`) over MCP server names
(`Turn.attribution_mcp_server`, `mcp__<server>__<tool>` tool-name
prefixes), config-snapshot `mcp_servers`/`enabled_plugins` entries, and
`Turn.attribution_skill`.

- `savers_detected` — one row per candidate: name, whether it came from
  the explicit `[savers]` list, and every auto-detection source
  matched.
- `savers_overhead` — attributed turns and cost, tool-call count, mean
  result size, and a distinct-tool-name count as a schema/prefix-load
  footprint proxy.
- `savers_effect_by_stratum` — cost/session, new tokens/session,
  cache-creation/turn, mean tool-result tokens/turn, re-cache share,
  compactions/session and turns/session, compared between sessions with
  the saver present versus absent, overall and by purpose/mode stratum,
  gated on a minimum 5 sessions per arm (`SaverThresholds.
  min_sessions_per_arm`).
- `savers_search_substitution` — for the working theory that a saver's
  real job is a code-search replacement: native `Grep`/`Glob`/`Read`/
  shell-search calls per session versus the saver's own calls per
  session, mean result size on each side, and each side's carry-cost
  implication (reusing `carry.compute_carry` per arm).
- `savers_verdict` — net saving per session = (cost/session absent −
  cost/session present) − overhead/session, labelled "observed, not
  controlled", plus any other config key that co-changed between the
  two arms' representative snapshots.

Nothing calls `savers.py` yet: the report, `recommend.recommend()`, the
CLI and the dashboard all leave it out, on purpose. Its name-based
detection treats ordinary tools whose names contain "context", "memory"
or "cache" as savers; a saver configured for every session leaves no
sessions to compare against, and the two groups differ in workload
anyway; and its rule's lever (`mcpServers.<name>`) is not a settings key
the report's fixes can change, with dollar amounts written directly
rather than in the billing mode. Its `saver-tool-roi` rule lives
in `savers.RULES` (same `(report, thresholds, snapshot=None) ->
list[Recommendation]` shape as `model_swap.RULES`). It would fire per
candidate whose verdict row clears the
minimum sample and whose net saving per session clears
`SaverThresholds.net_saving_usd_min` (default $0.01) in either
direction — keep (naming a result-size lever when overhead eats too
much of the gross saving, and citing displaced native search calls plus
smaller mean result size when the search-substitution table supports
it) or disable.

## `elasticity` (`elasticity.py`)

Full field-by-field contract: [`docs/elasticity.md`](elasticity.md).

How many percentage points of a `five_hour`/`seven_day`/`spend_limit`
usage window one million tokens (or one list-price dollar) is actually
worth, fit empirically from consecutive `tools/log_usage.py` CSV samples
paired with the token volume this machine's own transcripts (top-level
and subagent, every project) consumed between them. A weighted (`1/x`)
least-squares fit through the origin — `slope = sum(deltas) /
sum(volumes)` — per window kind and per volume metric (new tokens,
cache-read tokens, list-price USD); a fit below 8 pairs or an R² of 0.5
is refused outright rather than reported with a caveat.

- `elasticity_fit` — one row per window kind × metric: unit, the fitted
  window-percent-per-unit slope (blank when refused), R², pairs used,
  residual spread, whether it was accepted, and the reason when not.
- `elasticity_budget` — one row per window kind: the derived million
  new tokens a full window is worth (`100 / slope`), blank unless that
  window's new-tokens fit was accepted with a positive slope.
- `elasticity_recent_burn` — the last 24h's new-token volume expressed
  as a share of `ElasticityThresholds.weekly_window` (`seven_day` by
  default, "your weekly window").

Under subscription billing with rows in `<config-dir>/usage-log.csv`,
`report.build_report` fits elasticity once (thresholds from
`config.toml`'s `[thresholds.elasticity]`), adds this section right
after `usage`, and hands the same fit to `units.Units`, whose `money`
phrases every amount as "about x% of your weekly usage limit"
(`express_in_window`). Under API billing, or with no readings, the
section is left out. On the dashboard it sits on the Usage tab:
`elasticity_budget` and `elasticity_recent_burn` are shown, and
`elasticity_fit` is under the advanced detail.

`recommend.recommend()` runs the `window-budget` rule
(`elasticity.RULES`) last, after the other recommendations are
finished. It fires only under subscription billing with an accepted
weekly-limit fit, states how many million new tokens a full weekly
limit holds and the last 24 hours' share of it, and names the most
important other recommendation by its title, never repeating its
numbers.

## `team_report` (`team.py`) — CLI-only

`claude-token-lens team-report` compares the team documents each
machine saved (see [`docs/team.md`](team.md)). One column per machine,
keyed by its hashed machine id, never a hostname.

- `team_by_archetype` — one row per archetype: each machine's cost per
  session and session count.
- `team_by_agent_type` — the same, one row per agent type.

A cell reads `n<N` when that machine has fewer than `N` sessions for
the row (default 5), and `-` when it has none. The section notes carry
the "observed, not controlled" caveat.

## `baseline_comparison` (`report.py`)

Only with `report --baseline <id>`: this window against a saved
baseline.

- `baseline_comparison_overview` — `metric`, `baseline`, `current`,
  `delta`, `delta_pct` (pre-formatted text): cost per session, re-cache
  share, compactions per session, session baseline size, the TTL mix and
  mean spawn write per agent type, and each scorecard level.
- `baseline_comparison_by_mode` — per mode: sessions in each window, a
  `sample_ok` flag, and baseline, current and delta-% for cost per
  session, re-cache share and compactions per session. A mode with fewer
  than 5 sessions on either side shows its session counts only.

## `scorecard` (`scorecard.py`)

Five 1-5 levels (1 poor, 5 excellent) summarising a corpus's cache
efficiency, context hygiene, agent efficiency, config fit and data
quality — see [README section 3](../README.md#3-reading-the-report-sections).

- `dimensions` — one row per scored dimension: `dimension`,
  `level` (1-5), `label` (headed "Rating": `very poor`/`poor`/`fair`/
  `good`/`excellent`, from `LEVEL_LABELS`), the one representative
  `metric` name and its
  `value`, and the `threshold` band it was scored against. A dimension with
  nothing to measure in this corpus (e.g. `agent_efficiency` when no
  session ever spawned an agent) is left out of the table entirely
  rather than guessing a level.
- `overall` — one row (`metric`, `level`, `label`): the minimum level
  across
  `cache_efficiency`/`context_hygiene`/`agent_efficiency`/`config_fit`
  (never an average, and never including `data_quality`, which is
  reported alongside but deliberately excluded from `overall` — a
  low-fidelity measurement shouldn't be conflated with a genuinely poor
  working pattern).

`config_fit` is a proxy for config stability: how many keys changed
across the window's config snapshots (`changed_config_keys`), not a
match against a profile. With no snapshot it is rated 5 with a note,
rather than marked down. `agent_efficiency` is similarly a proxy
(`agent_cost_variance_ratio`): the ratio of the costliest agent type's
mean cost to the median across agent types.

## Recommendations (`recommend.py`)

Not a `Section`/`Table` like the others — `ReportModel.recommendations`
is a dedicated `list[Recommendation]` field, rendered by every renderer
in its own way (a Markdown/HTML block per recommendation; a JSON array;
excluded from CSV, which is table-shaped only). `recommend.recommend()`
builds it by reading back cells from the report's own already-rendered
tables — never a raw accumulator — so every recommendation's evidence
is guaranteed to cite a real, checkable number. See
[README section 3](../README.md#the-recommendations-block) for the
`Recommendation` field table (`id`, `severity`, `category`, `scope`,
`lever`, `evidence`).

Rules implemented today, in the order they run. From `recommend.py`'s
own `_rule_*` functions: `ttl-switch`, `long-tool-waits`,
`notification-invalidation`, `batch-instructions`, `subagent-volume`,
`compaction-churn`, `long-context-share`, `cache-read-dominance`,
`baseline-bloat`, `agent-report-size`, `spawn-cost` (for agent types
without `agent_startup` data; otherwise the per-part `spawn-claude-md`,
`spawn-unused-skills`, `spawn-unused-mcp`, `spawn-read-only-tools`,
`spawn-task-prompt` and `spawn-shared-claude-md`), `effort-mismatch`,
`discovery-share` (only with `--phases`), `pricing-coverage`,
`data-quality`, `limit-pressure`. Then each module's own rule:
`tool-output-carry` (`carry.RULES`), `compaction-window`
(`compaction_sim.RULES`), `model-tier` (`model_swap.RULES`) and
`wasted-turns` (`waste.RULES`). Last, `window-budget`
(`elasticity.RULES`, subscription billing only). Rules are gated by
archetype (a `ttl-switch` recommendation for a `chat-only` session's
subagents is suppressed, since a chat-only session barely has any), a
minimum-sample size (`min_sessions`/`min_turns` in `config.toml`'s
`[thresholds]` table), and managed-settings awareness (see
[README section 6](../README.md#6-for-team-leads-and-enterprise)).
`report --patch-set` renders the whole set as unified-diff-style text
via `recommend.render_patch_set`.

## Diagnostics (`ReportModel.diagnostics`)

Also not a `Section`/`Table` — `Diagnostics` (`model.py`) is a
dataclass of parse-quality counters accumulated across every transcript
in the corpus, exposed as `ReportModel.diagnostics` and rendered
directly by every renderer (a dedicated block, not a table). Fields
include `lines`, `unparsable_lines`, `truncated_final_line`,
`assistant_lines`, `distinct_turns`, `synthetic_turns`,
`turns_missing_usage`, `ttl_sum_mismatch`, `late_duplicate_ids`,
`ignored_line_types` (a count per ignored line type), `oversized_lines`,
`trailing_events`, `replayed_lines`, `timestamp_parse_failures`,
`agent_settings`, `modes`, `attachment_catch_all`, `limit_hits`,
`limit_resumes`, `agents_terminated`,
`pre_split_turns` (pre-split `cache_creation` reads normalised at parse
time — see the CHANGELOG), and `pricing_closest_match_turns` /
`pricing_fast_priced_as_standard_turns` (corpus-wide totals mirroring
the `usage` section's `pricing_closest_match`/
`pricing_fast_priced_as_standard` tables above — unlike every other
field here, these two are set once by `report.build_report` from the
finished `pricing.PricingCoverage` accumulator rather than merged
per-transcript, since they only exist once every turn has been priced
against the rate card, not at parse time). `recommend.py`'s
`data-quality` rule reads this field directly to decide whether its
unparsable-lines/ttl-mismatch clauses additionally fire, alongside the
`scorecard.dimensions` `data_quality` row it cites as evidence.

## Worked example

The tables below are the real output of `recache.build_section` and
`compaction.build_section`, run against
[`tests/fixtures/real/session-a`](../tests/fixtures/real/session-a) — a
genuine Claude Code session (1 top-level transcript, 25 subagent
transcripts) that was put through `tools/scrub.py`'s whitelist rewrite:
every id (including the session id below) is HMAC-rehashed with a random
key, and every string field is either a small documented-safe value or
an `x`-filled, length-preserving placeholder. Nothing here is invented,
and nothing here is the original session's real identifier.

**Re-cache summary** (`recache_summary`):

| Metric | Transcripts | Priced turns | Re-cache turns | Re-cache turn share | Cache-creation tokens (re-cache) | Cache-creation tokens (all) | Cache-creation share | Avoidable cost | Unavoidable cost (limit-expiry) |
|---|---|---|---|---|---|---|---|---|---|
| all | 26 | 2,037 | 7 | 0.3% | 1,003,923 | 10,234,286 | 9.8% | 5.80 USD | 0.00 USD |

**Re-cache signature split** (`recache_signature_split`):

| Signature | Turns | Cache-creation tokens | Avoidable cost | Median ctx | Median gap |
|---|---|---|---|---|---|
| full-expiry | 7 | 1,003,923 | 5.80 USD | 130,555 | 10m 21s |
| prefix-invalidated | 0 | 0 | 0.00 USD | - | - |
| limit-expiry | 0 | 0 | 0.00 USD | - | - |

Every re-cache turn in this session was a genuine TTL expiry
(`full-expiry`), not a broken-prefix invalidation or a usage-limit
pause.

**Compaction summary** (`compactions_summary`):

| Metric | Value |
|---|---|
| Sessions with ≥1 compaction | 1 |
| Total sessions | 1 |
| Compactions per session (mean) | 45 |
| Compactions per compacting session (mean) | 45 |
| Compactions per session (max) | 45 |
| Pre-compaction tokens (median) | 121,010 |
| Post-compaction tokens (median) | 17,798 |
| Dropped tokens (total) | 6,370,510 |
| Dropped tokens (share of cache_creation) | 62.2% |
| Dropped tokens (share of new_tokens: input+cache_creation) | 62.2% |
| Mean duration (ms) | 170,334 |
| Total post-compaction write cost (USD) | 18.12 |
| Total post-compaction RE-CACHE-flagged write cost (USD) | 1.87 |

**Compaction trigger mix** (`compactions_trigger_mix`):

| Trigger | Count | Share |
|---|---|---|
| unknown | 45 | 100.0% |

This particular scrubbed fixture's compaction lines didn't carry a
`trigger` value the parser could read (see `tools/scrub.py`'s own
deviation note: `compactMetadata.trigger` isn't on the scrub tool's
keep-list, so a scrubbed fixture's trigger comes back blank/`unknown` by
construction — a property of the scrub tool, not evidence about how
compactions are triggered in general).

To reproduce these tables (or generate your own against a real session),
call the same functions directly:

```python
from pathlib import Path
from claude_token_lens import discovery, recache, compaction
from claude_token_lens.model import TranscriptMeta
from claude_token_lens.parse import parse_transcript
from claude_token_lens.pricing import load_pricing

fixture_dir = Path("tests/fixtures/real/session-a")
top_path = next(fixture_dir.glob("*.jsonl"))
session_id = top_path.stem
top = parse_transcript(top_path, TranscriptMeta(path=str(top_path), kind="top-level", session_id=session_id))

subs = []
for jsonl_path, _meta in discovery.find_subagents(fixture_dir, session_id):
    meta = discovery.load_meta(jsonl_path.with_name(jsonl_path.stem + ".meta.json"))
    subs.append(parse_transcript(jsonl_path, meta))

pricing = load_pricing()
th = recache.RecacheThresholds()
stats = recache.RecacheStats(th)
for result in [top, *subs]:
    stats.add(result, lambda m: pricing.resolve_model(m))

section = recache.build_section(stats, pricing, th)
for table in section.tables:
    print(table.name, [c.label for c in table.columns])
    for row in table.rows:
        print(row)
```
