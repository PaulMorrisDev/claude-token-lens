# Model-swap counterfactual (v4-model-swap)

A subagent type's ``.claude/agents/<type>.md`` frontmatter, and the
top-level conversation's ``settings.json``, both carry a ``model``
lever. This feature turns that lever into a number: for each agent
type, and for the top-level conversation, what would today's
already-observed token volumes have cost at every other model this
rate card knows — and, the number that actually leads somewhere, what
is the ceiling saving from moving one tier down (fable -> opus ->
sonnet -> haiku)?

`model_swap.py` does this. It never proposes a jump of more than one
tier, and it never claims the saving is a prediction.

## What this is, precisely

For every priced turn already in the corpus, `compute_model_swap`
reprices it at every model `pricing.toml` carries, using the exact same
`pricing.price_turn` the rest of the engine uses to price the turn's
observed model — same input/output/cache-read tokens, and the same
observed 5-minute/1-hour cache-write split (`write_split=None`, so
`price_turn` falls back to the turn's own `cc_5m`/`cc_1h`). Nothing
about the turn's shape changes; only the per-token rate does.

That is a **price ceiling at today's usage shape**, not a forecast. A
smaller model may need more turns to reach the same result, or fail
the task outright — this module has no way to represent either
possibility, and says so in every note and every recommendation's
action text.

## Tier order

"One tier down" reuses `workstyle.model_tier`'s existing ranking
(fable=3, opus=2, sonnet=1, haiku=0) rather than inventing a
cost-derived ordering here. The next cheaper family's *current*
representative model is resolved through `Pricing.aliases[family]` —
the public alias table `pricing.py` already exposes — never a
hardcoded model id. If a future rate card drops a family's bare alias
entirely, the affected row reports "unknown tier" instead of guessing
at a stale id.

## The `model_swap` report section

| Table | What it shows |
|---|---|
| `model_swap_by_agent_type` | One row per agent type (`"top-level"` for the main conversation, else `TranscriptMeta.agent_type` or `"unknown"`): spawns, priced turns, unpriced turns (unknown model), observed model, observed cost, one `Cost at <model-id>` column per model in `pricing.toml`, the best cheaper alternative (model id and human label), the ceiling saving in USD and %, and the lever text (`settings.json` for top-level, `<type>.md`'s frontmatter for a subagent). |
| `model_swap_summary` | One row: the corpus-wide ceiling if every subagent type currently on Fable or Opus moved one tier down. Excludes the top-level row and any Fable/Opus agent type that's already at or below its next tier's cost at today's volumes. |

Every row's "best cheaper alternative" state is one of:

- **cheaper_available** — a real one-tier-down saving exists; the
  model id, USD and % figures are populated.
- **already_cheapest** — the observed model is already Haiku, or its
  own volumes are already cheaper than the next tier down at today's
  rates. The best-cheaper-alternative column is empty and the saving
  is `0.0` in both dollars and percent, never a stale positive number.
- **unknown_tier** — the observed model's family isn't recognised, or
  the rate card has no alias for the next family down.
- **no_data** — no priced turns for this agent type.

Only `cheaper_available` ever carries a non-zero saving, so the table
never implies a saving where none exists.

## The `model-tier` recommendation

`RULES["model-tier"]` reads the *rendered* `model_swap_by_agent_type`
table (never the raw stats — same "rules only read the finished
report" convention `recommend.py`'s own rules follow) and fires one
`Recommendation` per qualifying row: a real cheaper alternative exists,
the row's sample clears `ModelSwapThresholds.min_sessions`/
`min_turns`, and the ceiling saving clears both
`saving_pct_min` (default 10%) and `saving_usd_min` (default $1.00).

The action text names the exact file and line to change
(`settings.json`'s `"model"` key for the top-level conversation, or
`.claude/agents/<type>.md`'s `model:` frontmatter line for a subagent),
cites the saving as a ceiling, and repeats the "held constant / may
need more turns or fail outright / verify quality before committing"
caveat every time. Per-agent-type advice is suppressed under an
archetype that never spawns subagents of its own (`chat-only`) — the
top-level row's own model is a real lever regardless of archetype, so
it is never suppressed.

## API and wiring

```python
from claude_token_lens import model_swap

stats = model_swap.compute_model_swap(results, pricing)          # pure; hand in every transcript at once
section = model_swap.build_section(stats)                        # -> Section(key="model_swap", ...)
recs = model_swap.RULES["model-tier"](report, thresholds, archetype, snapshot)
```

- `model_swap.compute_model_swap(results, pricing, thresholds=None) -> ModelSwapStats`
- `model_swap.build_section(stats, thresholds=None) -> Section`
- `model_swap.RULES["model-tier"](report, thresholds, archetype=None, snapshot=None) -> list[Recommendation]`
- `model_swap.ModelSwapThresholds` — `saving_pct_min` (10.0), `saving_usd_min` (1.00), `min_sessions` (5), `min_turns` (200); `.from_config(dict)` and `.describe()` follow the same convention as `RecacheThresholds`/`TtlThresholds`.
- `model_swap.ASSUMPTIONS` — the four caveats this module states about every number it produces; fold into any parent "assumptions" listing.

`report.build_report` appends `model_swap.build_section(...)` after the
`compaction_sim` section and adds `model_swap.ASSUMPTIONS` to the
report's assumptions. `recommend.recommend()` runs
`RULES["model-tier"]` with
`ModelSwapThresholds.from_config(config.thresholds)`, the report's
archetype and the latest snapshot. The CLI's `model-swap` subcommand
prints this section plus `overview`.
