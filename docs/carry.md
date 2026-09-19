# Context carry cost per tool (v4-carry-cost)

Every other analytic in this project prices a tool result once, at the
turn it was produced. That undercounts its real cost: once a result
lands in the transcript, Claude Code's prompt cache carries it forward
on every later turn as part of the cached prefix — re-read at the flat
`cache_read` rate when the prefix survived, or re-written at the
`cache_write_5m`/`cache_write_1h` rate when a TTL expiry or content
change forced a re-cache — until a `COMPACT_BOUNDARY` finally drops it,
or the transcript simply ends first. A single 20k-token `Read` at turn 2
of a 40-turn session is paid for, in the caching sense, up to 38 times
over. `carry.py` puts a dollar figure on that.

## What it measures

For one tool result that entered context at priced turn `i` (one entry
in `Turn.tool_result_chars_by_tool`, sized in chars and converted to
tokens via the project's standing chars/4 approximation):

- **Turns carried** — every priced turn strictly after `i`, up to and
  including the last priced turn *before* the next `COMPACT_BOUNDARY`
  that follows `i` (a boundary drops the whole context, this result
  included), or the transcript's own last priced turn when no such
  boundary exists.
- **Carry tokens** — the result's own token size, multiplied by the
  number of turns it carried.
- **Carry cost** — summed turn by turn. Each later turn's own observed
  `cache_read_tokens`/`cache_creation_tokens` split tells us what
  fraction of *that turn's* whole cached prefix was read back unchanged
  versus rewritten; the carried result's own token count is split in
  the same proportion and priced via `price_turn` at that turn's own
  resolved model rate — the read slice at the flat `cache_read` rate,
  the write slice at that turn's own blended `cache_write_5m`/
  `cache_write_1h` rate. This is an approximation: no field anywhere
  attributes a turn's aggregate cache volume back to which earlier
  write produced which slice of it, so a proportional split is the
  closest the data supports.
- **Avoidable if truncated** — because carry cost is exactly linear in
  a result's own token count (the per-turn rate mix doesn't depend on
  the result's size), the saving from having capped a result at `T`
  tokens is computed exactly: `carry_cost * (1 - T / tokens)` for every
  result whose size exceeds `T`.

## The `carry` report section

| Table | What it shows |
|---|---|
| `carry_by_tool` | Per tool name: carried-result count, tokens entered, mean turns carried, carry tokens, carry cost, and that tool's carry-token share of the corpus's total cache volume. |
| `carry_by_agent_type` | Same roll-up, keyed by agent type (`"top-level"` for the main session). |
| `carry_top_results` | The single most expensive individual carried results, corpus-wide: tool name, agent type, tokens, turns carried, carry cost — no content, no path, no command. |
| `carry_truncation_savings` | For each configured token cap (default 2,000 and 8,000): how many results exceed it, tokens saved, and USD saved if every one of them had been truncated to that cap. |

`share_of_cache_volume_pct` is an **attribution share, not a
partition**: `carry_tokens` double-counts by construction (the same
physical cache read on a given turn also carries every other still-live
result in that same prefix), so the by-tool/by-agent-type rows do not
sum to 100% — a single dominant tool can legitimately show a share well
over 100%.

## Granularity (privacy)

The finest per-call size the frozen `Turn` contract exposes is
`tool_result_chars_by_tool` — tool name → total chars for *that turn's*
calls to that tool, not one entry per individual tool_use_id/call. So
one "result" in this module's tables is one `(turn, tool name)` entry:
two `Read` calls answered within the same turn are already summed
together by the time this module sees them. No content, path, or
command is ever read — only tool names (already privacy-cleared
elsewhere in this codebase) and integers.

## The lever

`recommend.py`'s `tool-output-carry` rule (`carry.RULES`, wired in
separately — see below) fires when a tool's carry cost exceeds
`carry_share_pct` (default 25%) of the corpus's total cache volume, and
that tool has at least `min_sample_results` (default 5) carried
results. There is no settings key for this: the fix is a workflow
change at the point the result is produced — pipe long Bash/PowerShell
output through `head`/`tail` or a digest script, prefer `Grep` over
`Read` for large files, and cap agent report length before it enters
context — so `Recommendation.lever` is `None`, and the action names the
projected saving from `carry_truncation_savings` directly.

## Worked example (synthetic numbers)

A 10-turn transcript where turn 2 receives a 40,000-char `Read` result
(10,000 tokens), and every later turn observes a plain 100-token cache
hit (Sonnet 5 rates: cache_read $0.20/M tokens):

| Turn | Event | Carry cost contribution |
|---|---|---|
| 2 | `Read` result enters (10,000 tokens) | — |
| 3–10 (8 turns) | 100-token cache_read each | 10,000 × (100/1e6 × 0.20 ÷ 100) = $0.002 per turn |

Total carry cost for this one result: 8 × $0.002 = **$0.016** — for a
tool result whose own turn-2 cost (priced once, the old way) was
10,000/1e6 × cache_write_5m ($2.50) = $0.025 to write in the first
place. Carrying it for the rest of the session cost an *additional*
64% on top of writing it.

If a compaction boundary had instead landed at turn 6, the same result
would carry only through turn 5 (3 turns): 3 × $0.002 = $0.006, and
`carry_truncation_savings` would report the saving from having capped
it at, say, 8,000 tokens as `$0.006 × (1 − 8,000/10,000) = $0.0012`.

## API

- `CarryThresholds` — `big_result_tokens`, `carry_share_pct`, `top_n`,
  `truncation_tokens`, `min_sample_results`; `from_config`/`describe`
  follow the same convention as `ttl.TtlThresholds`/
  `limits.LimitThresholds`.
- `compute_carry(results, rates, thresholds=None) -> CarryStats` — a
  plain function (not an incremental accumulator, since
  `carry_top_results` needs one global sort once every transcript has
  been processed), over a corpus's worth of `TranscriptResult`s.
- `build_section(stats, thresholds=None) -> Section` — the `carry`
  report section described above.
- `RULES` — `[_rule_tool_output_carry]`, the `tool-output-carry` rule
  described above, in the same `(report, thresholds) -> list[Recommendation]`
  shape every baseline rule in `recommend.py` uses. Not wired into
  `recommend.recommend()` by this module (it never imports or edits
  `recommend.py`) — a caller folds `carry.RULES` into that function's
  own rule list, and appends `carry.ASSUMPTIONS` to the report's
  assembled `ReportMeta.assumptions` the same way `report.py` already
  does for `ttl.ASSUMPTIONS`/`recache.ASSUMPTIONS`/`limits.ASSUMPTIONS`.
