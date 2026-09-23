# Wasted-turn spend (v4-wasted-turns)

The project's whole point is to help you save tokens: every number this
tool reports should lead to a lever and a projected saving. A "wasted
turn" is the most direct instance of that idea — money spent on a turn
whose output the user never actually got any benefit from, because the
tool call it made failed, the user cut it off, a permission denial
stopped it, or the harness killed the whole subagent before it could
report back. `waste.py` prices exactly those turns and attributes each
one to a cause with a lever, so the report can say not just "here is
what you spent" but "here is what you could get back, and how."

`parse.py`/`events.py` do the actual detection; `waste.py` is purely a
*reader* of their already-parsed state, the same "detects nothing new"
convention `limits.py` follows. A turn's own tool_result blocks
carrying `is_error: true` become `Turn.tool_error_count`/`Turn.
tool_error_chars` (the v4-wasted-turns parser addition — length only,
never the error text itself), and `Turn.tool_errors_by_kind` records why
each failed (`blocked`, `denied`, `failed` or `misfire`, read from the
start of the error text and then dropped). A turn whose only failed
calls are commands that ran and reported failure (a failing test or
build) isn't wasted: Claude used that output. It is counted in
`waste_summary`'s `failed_command_turns` and left out. Everything else is read off
`EventKind`/`Turn.preceding_primary`/`Turn.gap_cause`/`TranscriptMeta.
stopped_by_user`, all of which already existed.

## What `waste.py` provides

- `compute_waste(results, rates, thresholds=None, *, config_dir=None)
  -> WasteStats` — the one-shot entry point: prices every transcript in
  `results` and returns the accumulated stats.
- `WasteStats` — the accumulator, mirroring `LimitStats`'s own
  accumulate-then-render shape; `WasteStats.add(result, rates)` folds
  one transcript in at a time, for a caller (like `report.py`'s own
  per-session loop) that already iterates transcripts one by one rather
  than collecting a flat list first.
- `WasteThresholds` — `share_pct` (default 10.0), `min_sessions`
  (default 5), `min_turns` (default 200); `.from_config(config)` reads
  a nested `"waste"` sub-dict, `.describe()` renders the thresholds as
  report notes, mirroring `LimitThresholds`.
- `build_section(stats, thresholds=None) -> Section` — the corpus-wide
  `waste` report section (four tables, described below).
- `RULES` — a one-element tuple, `(_rule_wasted_turns,)`, of `(report,
  th) -> list[Recommendation]` callables in the same shape
  `recommend.py`'s own internal `_rule_*` functions already have.
  `recommend.recommend()` runs it as
  `recs.extend(waste.RULES[0](report, waste_th))`, the same way it runs
  `carry.RULES`, `compaction_sim.RULES` and `model_swap.RULES`.

## Cause detection

Every priced turn (`turn_index > 0`) is examined in one pass. A turn
whose gap to the previous turn was a usage-cap pause
(`Turn.gap_cause == "limit"`) is **excluded outright** — `limits.py`
already owns that attribution (pause count/duration, post-pause
re-cache cost); folding it in here would double-count it. Excluded
turns are still counted (`WasteStats.limit_pause_excluded_turns`) and
the exclusion is stated in the section's own notes.

Every other priced turn is checked against the causes below, in this
fixed priority order (a turn is assigned to exactly one cause, so
`waste_by_cause`'s turns/cost sum to `waste_summary`'s totals):

| Cause | Detected when | Lever |
|---|---|---|
| `max-turns` | The turn's own transcript has `TranscriptMeta.kind != "top-level"` and `TranscriptMeta.stopped_by_user` is true. A **transcript-level override**: every priced turn in a killed subagent transcript is wasted, since that transcript returns no report to its parent regardless of what any one turn did. | Raise the subagent's `maxTurns` budget, or narrow its brief so it finishes — and reports back — inside the turns it's given. |
| `tool-error` | The turn's own `Turn.tool_error_count > 0` — one of its own tool_use calls came back with a tool_result carrying `is_error: true` — and at least one of those calls couldn't run as written (`misfire` in `Turn.tool_errors_by_kind`: a wrong path, a malformed command, an edit whose text wasn't found). | Give exact paths and names in briefs, and have Claude check a path exists or read a file before it edits or runs against it. |
| `blocked` | The same, where a hook or a Claude Code guard blocked the call (`blocked`) and nothing misfired. | Put the rule the hook enforces into the instructions of the agent that keeps hitting it. |
| `interrupt` | The *next* priced turn's `preceding_primary == EventKind.INTERRUPT` — the turn under scrutiny is the one the user cut off. | Batch instructions and plan the whole step before running it. |
| `tool-denial` | The *next* priced turn's `preceding_primary == EventKind.TOOL_DENIAL`. | Add the repeatedly-denied tool/command to the permissions allowlist. |

`api-error-retry` is tracked separately and **counted only, never
priced**: a turn whose own `preceding_primary == EventKind.API_ERROR`
(a 529/retry gap immediately before it). The harness already retried
automatically, so this is a frequency signal, not spend — its cost and
tokens are always reported as zero and it never contributes to the
recoverable ceiling.

## Cost and tokens

A wasted turn's cost is its full priced cost — input, cache write,
cache read, output — via `pricing.price_turn`, the same function every
other analytics module in this codebase prices a turn with. Its token
total is the same four components summed.

Every `share_pct` column in this section, for both turns and cost, is
computed against the **whole corpus's** total priced turns/cost, not
just the wasted subset. This is a deliberate, simpler choice than
`recache.py`'s dual target/control weighting: the point of a
"recoverable spend ceiling" is to answer "how much of my total spend is
this", which only a corpus-wide denominator can answer directly.

## The `waste` report section

| Table | What it shows |
|---|---|
| `waste_summary` | One "all" row: total priced turns/cost, wasted turns/share of turns, wasted cost (labelled "Recoverable spend ceiling")/share of cost, wasted tokens, the limit-pause-excluded count, and the api-error-retry count. |
| `waste_by_cause` | One row per cause (`tool-error`, `blocked`, `interrupt`, `tool-denial`, `max-turns`, fixed order) plus an `api-error-retry` row: turns, share of all priced turns, cost, share of all priced cost, tokens, and that cause's own lever text. |
| `waste_by_agent_type` | Per-agent-type roll-up (`TranscriptMeta.agent_type`, or `"top-level"`): turns, share of turns, cost, share of cost, tokens. Sorted descending by cost. |
| `waste_top_sessions` | The 20 sessions with the highest wasted cost: a salted, non-reversible session hash, turns, cost, share of cost, and a `cause:count` cause-mix string (most frequent cause first). |

## Session hashing and privacy

`waste_top_sessions` needs a stable-but-non-reversible per-session key,
the same privacy posture as every other per-session table in this
codebase. Session ids are hashed inside `WasteStats.add` — never stored
raw — via the same salted-HMAC-SHA256 construction
`exports._hash_slug`/`team.machine_id` already use (a domain tag plus a
truncated hex digest), with this module's own `"session:"` tag so its
namespace can never collide with theirs even at the same 12-hex-char
truncation length.

`compute_waste`/`WasteStats` both take an optional `config_dir`
keyword, following `team.py`/`exports.py`'s own precedent (an explicit
parameter, `parse.load_or_create_salt(config_dir)` called directly)
rather than `parse.py`'s process-wide `set_salt` convention, which
exists for the parsing layer's own multiprocessing workers. Omitting it
uses `load_or_create_salt`'s own default resolution
(`$CLAUDE_CONFIG_DIR/token-lens`, else `~/.claude/token-lens`); a
caller that must keep this module from touching the real config
directory — such as a read-only verification run — should pass its own
scratch directory explicitly.

`tool_error_chars` (and every length this module reports) is a
character count, never the underlying error text — this module never
reads, stores, or prints message text, file paths, or commands beyond
what the rest of the codebase already permits.

## The `wasted-turns` recommendation rule

`_rule_wasted_turns(report, th)` fires when the corpus clears the
minimum-sample gate (`th.min_sessions` sessions or `th.min_turns`
priced turns — same gate `recommend.py`'s own rules use, reimplemented
locally to avoid a circular import) **and** `waste_summary`'s own
`wasted_cost_share_pct` exceeds `th.share_pct` (default 10.0). It names
the dominant cost-bearing cause (read off `waste_by_cause`) and its
lever, and cites the wasted cost share, the recoverable ceiling, the
wasted turn count, and the dominant cause's own cost as evidence — each
evidence tuple resolves to a real, already-rendered cell in the
`waste` section, the same evidence contract every other rule in this
codebase follows.

## Assumptions

- `max-turns` eligibility is subagent-only: `topology.py`'s own
  `_add_chains` only ever reads `stopped_by_user` off subagent
  transcripts, never the top-level one, so a top-level transcript's
  `stopped_by_user` is not treated as a meaningful signal here either.
- `max-turns` is a transcript-level override, taking priority over any
  other cause a turn in that transcript might otherwise match.
- Cause priority for a turn that could match more than one rule:
  limit-pause exclusion first, then `max-turns`, then `tool-error`
  (any misfire), then `blocked`, then `interrupt`/`tool-denial`.
- `api-error-retry` is counted, never priced.
- Every `share_pct` column is against the whole corpus's priced
  turns/cost, not just the wasted subset.

## Downstream attribution

`report.build_report` feeds every transcript through `WasteStats.add`
in its per-session loop, appends `waste.build_section(...)` after the
`model_swap` section, and adds `waste.ASSUMPTIONS` to the report's
assumptions. `recommend.recommend()` runs `waste.RULES[0]` with
`WasteThresholds.from_config(config.thresholds)`. The CLI's `waste`
subcommand prints this section plus `overview`. No scorecard dimension
reads wasted spend yet.
