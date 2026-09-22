# `autoCompactWindow` sweep (`compaction_sim.py`)

Claude Code auto-compacts a session once its context reaches
`autoCompactWindow` tokens — observed in config snapshots (e.g.
`300000`; a model's own context window is typically 1,000,000 for a
"[1m]"-aliased model, 200,000 otherwise, per `context_budget.py`'s own
assumed-window constants). That single number is a real, user-settable
lever, and it trades off two costs directly against each other:

- **Compacting less often** (a larger window, or `none` at all) means
  every turn keeps carrying a bigger context, priced at the cheap
  `cache_read` rate most of the time — but the context keeps growing
  until the harness's own ceiling forces a much larger rewrite, and every
  turn pays cache-read on a bigger prefix in the meantime.
- **Compacting more often** (a smaller window) pays a summary-write cost
  and a rediscovery cost every time, but every later turn carries a
  smaller context afterwards.

Every number this module reports leads to a lever
(`autoCompactWindow`) and a projected saving in dollars — the project's
stated purpose (see the top-level README).

`compaction_sim.py` answers "what would this session have cost under a
different `autoCompactWindow`" by replaying its own priced turns, in
order, under each of a fixed set of candidate windows, and comparing the
total simulated cost to what the session actually cost.

## What it measures, precisely

A **real** compaction (one already recorded as a `compact_boundary`
event in the transcript) costs, and saves, exactly what it already did —
this module never re-simulates or removes it. A **simulated** compaction
is inserted whenever a candidate window's running context would be
exceeded, and costs:

1. A **summary write**: the simulated post-compaction token count
   (context scaled down by this corpus's own observed compression ratio
   — the median `postTokens`/`preTokens` across real `compact_boundary`
   events, 0.15 default when this corpus has none), priced as a fresh
   5-minute cache write.
2. A **rediscovery allowance**: this corpus's own median post-compaction
   re-cache write cost, drawn from real compactions' own next-turn
   behaviour (`compaction.compaction_records_for_transcript`'s
   `next_turn_write_cost` on a recache-flagged record); $0.00 default
   when this corpus has none.

Every turn after a simulated compaction has the tokens that summary
dropped taken out of its context and cache reads (and out of its cache
writes once the reads are used up), until the next compaction; content
added after the summary is kept whole. A real compaction resets this. Candidate windows swept:
`100k, 150k, 200k, 250k, 300k, 400k, 500k, none` (`none` = never
auto-compact; a real compaction already in the transcript is still kept
under this row — see the "no candidate window" identity below).

## The report section

| Table | Scope | What it shows |
|---|---|---|
| `compaction_sim_by_window` | Top-level sessions only | One row per candidate window: simulated compactions per session, mean ctx, total cost, and delta vs. the observed (`none`) cost, in USD and percent. |
| `compaction_sim_by_agent_type` | `"top-level"` and every subagent type | Each key's own cheapest candidate window, its cost, the saving vs. observed (0 floor), and a recommendation string naming the window. |
| `compaction_sim_fidelity` | Top-level sessions with a known configured window | Simulating at the session's own snapshot-configured `autoCompactWindow` against its true observed cost — a trust check on the simulation itself. |

Recommendation rule `compaction-window` (category `settings`, lever
`autoCompactWindow`) fires when the best candidate window's cost clears
both `CompactionSimThresholds.switch_pct` (default 0.95× observed) and
`switch_usd` (default > $1.00 saved) — the same two-condition gate shape
as `ttl.py`'s own `ttl-switch` rule. Scope is `"user"`
(`~/.claude/settings.json`), `"project"` (`<project>/.claude/settings.json`
or `.settings.local.json`, read via `snapshots.effective_provenance`), or
`"managed"` (named, not offered as user-actionable) depending on which
settings layer actually set the session's effective `autoCompactWindow`.

## Sign convention (differs from `ttl.py`)

**`delta_usd = candidate_cost - observed_cost` throughout this
module — negative means cheaper.** `ttl.py`'s own tables use the
opposite sign (`cost_observed - best_cost`, positive = a saving); that
convention is unchanged there. This module's sweep walks *many*
candidate windows per key rather than one best-vs-observed pair, so a
uniform "candidate minus observed" avoids re-deriving the sign on every
row. `saving_usd = max(0, -delta_usd)` is always non-negative in both
modules.

## Worked example

A 20-turn synthetic transcript with linearly growing context (20,000
new tokens written per turn, everything earlier read back from cache —
`tests/test_compaction_sim.py`'s `_synthetic_20_turn_transcript`, priced
at the packaged Sonnet 5 rates) costs **$1.76** with no compaction at
all (`window=none`). Under `window=100,000`, compactions fire at turns
6, 11 and 16 (each time the context, grown by 20,000 a turn since the
last summary, passes 100,000 again), and the total drops to
**$1.138** — a **$0.622 saving (35% cheaper)**. That is three summaries
a session, so the rule skips 100,000 and names 150,000 (two summaries,
$0.501 saved) as the floor, once the saving clears the switch
thresholds. The full turn-by-turn arithmetic is spelled out in that test
file's docstrings.

## The "no candidate window" identity

`window=None` never opens the synthetic-compaction guard, so the
per-transcript dropped-token offset never leaves `0` — every turn is priced
via its own unmodified, real values, including any real compaction
already in the transcript. The `window=None` row is therefore *exactly*
the transcript's true observed cost; no separate "observed cost" code
path exists in this module. Asserted directly in
`test_window_none_has_zero_synthetic_compactions_and_matches_true_observed_cost`.

## Assumptions

Printed verbatim in the report section's own notes (`ASSUMPTIONS`):

- A simulated compaction resets context to this corpus's own observed
  compression ratio (0.15 default).
- A simulated compaction charges a summary-write cost equal to the
  simulated post-compaction token count, at the 5-minute cache-write
  rate.
- A simulated compaction also charges a rediscovery allowance — this
  corpus's own median post-compaction re-cache write cost ($0.00
  default, noted).
- Every later turn's context and cache reads shrink by the tokens the
  simulated summary dropped (its cache writes too, once the reads are
  used up), until the next compaction, real or simulated. Content added
  after the summary is kept whole. (Earlier versions
  scaled later turns down by the compression ratio instead, which also shrank new
  content and so overstated savings at small windows.)
- A real, observed compaction already in a transcript is kept as-is
  under every candidate window — never re-simulated, never removed.
- `delta_usd = candidate_cost - observed_cost` (see the sign-convention
  section above).

## Wiring into the report and CLI (integration note)

`compaction_sim.py` cannot be imported by `report.py`/`cli.py`/
`recommend.py` from within this module (out of this work's file
ownership) — the module's own docstring in
`src/claude_token_lens/compaction_sim.py` spells out the exact call
sites and snippets an integrating change needs:

1. `report.py`: build `snapshot_windows: dict[session_id, int | None]`
   (reusing `context_budget.py`'s own `session_to_project` reverse
   lookup plus `snapshots.effective_config(snapshot).get("autoCompactWindow")`,
   just keyed by session id instead of project), then call
   `compaction_sim.simulate_compaction_windows(all_results, rates, snapshot_windows)`
   and `compaction_sim.build_section(stats)`, and append the section.
2. `recommend.py`: add
   `recs.extend(compaction_sim.RULES[0](report, CompactionSimThresholds(), snapshot))`
   alongside the module's other `recs.extend(_rule_xxx(...))` calls,
   after step 1 has added the `compaction_sim` section to `report`.
