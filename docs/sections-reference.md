# Report sections, in detail

This file is the field-by-field companion to
[`README.md`](../README.md#6-reading-the-report-sections). It lists every
table each `build_section(...)` function produces today, and expands the
two topics the README only summarises: the TTL section's utilisation
metrics, and a worked example against a real, scrubbed transcript.

Nothing here describes an assembled `report` command — there isn't one
yet (see the README's Status note). Every table below is produced by
calling the named module's `build_section` function directly against
`TranscriptResult`/`SessionRecord` objects from `parse_transcript`.

## `sessions` (`classify.py`)

- `sessions_by_mode` — session count, cost, span by `mode` (`interactive`,
  `long-agentic`, `overnight`, `mixed`), first match wins in that order
  from the plan's classification rules (chain/subagent-fanout with few
  prompts, span > 4h with a >60min human gap, median human gap < 5min
  with ≤2 subagents, else mixed).
- `sessions_by_purpose` — the same, by `purpose` (`local-llm-pipeline`,
  `agent-fanout`, `workflow-run`, `review`, `test-triage`, `planning`,
  `docs`, `refactor`, `general-dev`), with intent signatures checked
  before falling through to the generic buckets.
- `sessions_per_session` — one row per session: mode, purpose, cost,
  span, subagent count, max spawn depth, evidence source
  (`sessions.toml` override vs. rule-derived).

Per-session `mode`/`purpose` overrides live in
`<config-dir>/token-lens/sessions.toml` and always win over the rule
engine (`config.load_session_overrides`).

## `recache` (`recache.py`)

Definitions: [README section 5](../README.md#5-re-cache-definitions-and-signatures).

- `recache_summary` — transcripts, priced turns, re-cache turns and
  share, cache-creation tokens (re-cache vs. all), avoidable cost.
- `recache_signature_split` — `full-expiry` vs. `prefix-invalidated`:
  turns, cache-creation tokens, avoidable cost, median ctx, median gap.
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
  `prefix-invalidated` turns only (the signature a TTL change can
  actually address — see the README's TTL utilisation section).
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

## `ttl` (`ttl.py`)

Simulation assumptions: [README section 7](../README.md#7-ttl-simulation-assumptions).

- `ttl_by_agent_type` — per agent type and `"top-level"`: spawns, priced
  turns, observed 5m/1h mix, gaps > 5min, gaps > 60min, cost observed /
  all-5m / all-1h, best policy, delta, fidelity, and a recommendation
  string naming the concrete lever (`subagentPromptCacheTtl`,
  `experimental.cacheTtl` in `<agent>.md`, or `promptCacheTtl`).
- `ttl_gap_distribution` — inter-turn gap histogram per agent type.

### TTL utilisation metrics

- `ttl_wasted_writes` — cache-creation writes that were never read back
  before the entry expired: count, tokens, USD, and a wasted-share
  percentage (`waste_pct`).
- `ttl_premium_waste` — for turns using a 1h TTL: tokens/USD where the
  extra write premium was never earned back by a hit a 5m TTL would have
  missed (`premium_1h_not_needed_*`) vs. tokens/USD where it was earned
  (`premium_1h_earned_*`) vs. tokens where the 1h entry expired anyway
  (`premium_1h_expired_*`); symmetrically for 5m turns,
  `premium_5m_fine_tokens` (no loss) vs. `premium_5m_loss_*` (expired
  when a 1h TTL would have survived) and `premium_5m_would_expire_tokens`.
- `ttl_break_even_share` — `premium_ratio` (the rate-card-derived
  break-even point) against `break_even_pct` (the same figure expressed
  as a percentage of the shared cacheable-prefix denominator, `Σ C_i`
  across all gaps), `premium_all_1h`/`expiry_loss_all_5m` (what every
  write would have cost end to end under each fixed policy), `margin`
  (`expiry_loss_all_5m - premium_all_1h`, positive means 1h wins) and a
  one-word verdict (`"marginal"` when the larger side is within a small
  band of the smaller, else `"1h"`/`"5m"`).
- `ttl_near_miss` — turns whose gap fell within `near_miss_window_s`
  (default 60s) of a TTL boundary, on the side that just missed and the
  side that just made it, for both the 5m and 1h boundaries.
- `ttl_addressable_share` — of all re-cache turns, the share whose
  signature is `prefix-invalidated` (TTL-addressable: a longer TTL could
  have survived the gap) vs. `full-expiry` (content-addressable only: no
  TTL choice below the gap length would have helped).
- `ttl_cache_economy` — `cache_economy`/`cache_roi` per agent type: the
  "uncached-equivalent" cost (as if every token had been priced as plain
  input) minus what was actually paid for writes and reads
  (`net_saving_usd`), and that saving as a ratio to what was spent on
  writes (`cache_roi`) — a standalone "is caching worth it at all"
  number, plus `unpriced_turns` for coverage.

Fidelity self-check: `ttl.dominant_ttl`/`ttl.fidelity` replay the
simulation at a transcript's own dominant observed TTL and compare it to
observed cost; `TtlThresholds.fidelity_warn_pct` (default 10.0) is the
flag threshold `build_section` applies per agent type.

## `compactions` (`compaction.py`)

- `compactions_summary` — sessions with ≥1 compaction, total sessions,
  compactions per session (mean/max), pre/post-compaction tokens
  (median).
- `compactions_trigger_mix` — trigger value (`auto`/`manual`/`unknown`)
  counts and share.
- `compactions_per_session` — top 20 sessions by dropped tokens:
  compaction count, dropped tokens, post-compaction write cost.

`dropped_share_of_new_tokens` (in the summary notes) reports dropped
tokens against both the `cache_creation` and `new_tokens`
(`input_tokens + cache_creation_tokens`) denominators. Post-compaction
write/recache cost aggregates exclude any join to the next turn that
took longer than 15 minutes (the join is presumed stale, not a genuine
immediate-post-compaction cost).

## `agents` (`topology.py`)

Answers "how do tokens, cost and information flow between a session and
the agents/skills/workflows it spawns" with numbers only:

- `topology_spawn_write` — downward: mean/median first-turn
  `cache_creation` per agent type (the briefing + system prompt + preloaded
  skills a new spawn pays for).
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
- `topology_cost_per_spawn` — cost per spawn by agent type.
- `topology_chains_summary` — `stoppedByUser`/`maxTurns` truncation
  signals.
- `topology_reminder_hook_pressure` — attachment/hook-output counts per
  turn, by transcript kind.
- `topology_cache_signal_histogram` — `CACHE_SIGNAL` subkind counts
  (model switches, thinking-stripped, ultra-effort enter/exit, deferred/
  prefix-loaded tool deltas, plan-mode/auto-mode transitions, output
  style changes).
- `topology_mcp_cost` — cost by `attribution_mcp_server`.
- `topology_effort_by_agent_type` — output/thinking token share by
  `effort`/`per_turn_effort`, by agent type.
- `topology_context_composition` — context composition per turn
  (baseline / tool results by tool / assistant output / notifications
  and attachments / compaction summaries), averaged per transcript kind.
- `topology_redundant_work` — repeated Bash/PowerShell command prefixes
  and post-compaction rediscovery signals ("how much am I paying to
  re-learn").

## `workstyle` (`workstyle.py`)

- `workstyle_archetypes` — one row per detected archetype
  (`overseer-fanout`, `plan-high-implement-low`, `workflow-heavy`,
  `effort-varied`, `chat-only`, `single-model`), with the evidence
  features that produced it (model-by-role, effort distribution, spawn
  counts, plan-mode-then-lower-model-implementer sequences). First match
  wins, in the order the module checks them; the recommendation engine
  this feeds (not yet built) is meant to condition on archetype so an
  overseer session is never told to "stop spawning agents" and a
  chat-only session is never told about subagent TTLs.

## `workflows` (`workflows.py`)

From `<session>/workflows/wf_*.json` run files:

- `workflows_summary` — run count, agent count, phase count, duration,
  cost.
- `workflows_status_mix` — run `status` (`completed`/`killed`/other
  observed values) counts and share.
- `workflows_detail` — one row per run: run id, session id, agent count,
  phase titles (never phase `detail`, which can carry workflow source or
  prompt text), started/finished, cost.

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

A DISCOVERY cost share above the module's threshold is the intended
input to a future `discovery-share` recommendation (not yet built).

## `config_diff` (`snapshots.py`)

Reads the JSON files `hooks/snapshot-config.py` writes (see the
README's [installation section](../README.md#8-installing-the-sessionstart-hook-and-the-statusline)).

- `config_diff` (the section's one table) — per distinct value of one
  chosen config key across a window: sessions, turns, cost, cost per
  session, re-cache share, compactions per session, median span, plus a
  computed "keys that also changed in the same snapshot" caveat, since a
  before/after comparison across two different snapshots can't isolate
  one key's effect from everything else that changed alongside it.

`snapshot_for(session, snapshots)` joins a session to the latest snapshot
whose timestamp is at or before the session's start; `diff_keys` and
`co_changed_keys` are the lower-level functions this table is built from.

## `usage_windows` (`tools/log_usage.py`)

Built from `~/.claude/token-lens/usage-log.csv` rows (appended by the
statusline logger, or by `python -m claude_token_lens.tools.log_usage`
from a pasted `get_usage` result), deduped by session/reset-time/used
percentage:

- `usage_windows_latest` — per window (`five_hour`/`seven_day`), the
  latest used percentage, its reset time, and sample count. Comes back
  empty (with a note) when no usage-log rows exist yet.
- `usage_windows_regression` — a simple linear fit of used-percentage
  against cumulative new-token volume within a reset period, per window:
  slope (`% per million tokens`), intercept, sample count, and the reset
  period it was fit against. Requires `token_totals_by_window` to be
  supplied by the caller (there is no `report` command wiring this in
  yet); skipped with a note when not enough samples exist.

## Worked example

Both tables below are the real output of `recache.build_section` and
`compaction.build_section`, run against
[`tests/fixtures/real/session-a`](../tests/fixtures/real/session-a) — a
genuine Claude Code session (1 top-level transcript, 25 subagent
transcripts) that was put through `tools/scrub.py`'s whitelist rewrite:
every id (including the session id below) is HMAC-rehashed with a random
key, and every string field is either a small documented-safe value or
an `x`-filled, length-preserving placeholder. Nothing here is invented,
and nothing here is the original session's real identifier.

**Re-cache summary** (`recache_summary`):

| Transcripts | Priced turns | Re-cache turns | Re-cache turn share | Cache-creation tokens (re-cache) | Cache-creation tokens (all) | Cache-creation share | Avoidable cost |
|---|---|---|---|---|---|---|---|
| 26 | 2,037 | 7 | 0.3% | 1,003,923 | 10,234,286 | 9.8% | 5.80 USD |

**Re-cache signature split** (`recache_signature_split`):

| Signature | Turns | Cache-creation tokens | Avoidable cost | Median ctx | Median gap |
|---|---|---|---|---|---|
| full-expiry | 7 | 1,003,923 | 5.80 USD | 130,555 | 10m 21s |
| prefix-invalidated | 0 | 0 | 0.00 USD | - | - |

Every re-cache turn in this session was a genuine TTL expiry
(`full-expiry`), not a broken-prefix invalidation — consistent with this
scrubbed session showing no `prefix-invalidated` rows anywhere.

**Compaction summary** (`compactions_summary`):

| Metric | Value |
|---|---|
| Sessions with ≥1 compaction | 1 |
| Total sessions | 1 |
| Compactions per session (mean) | 45 |
| Compactions per session (max) | 45 |
| Pre-compaction tokens (median) | 121,010 |
| Post-compaction tokens (median) | 17,798 |

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
