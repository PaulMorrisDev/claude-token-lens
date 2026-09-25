# Usage-window elasticity (v4-elasticity)

The project's whole point is helping a subscription user stay inside
their Claude usage windows, not just their USD spend — the binding
constraint most target users actually feel, since they're on a
subscription and the 5-hour/7-day windows run out long before the
dollar figure means anything to them. Every other analytics module in
this codebase reports a saving in dollars. `elasticity.py` measures,
empirically, how many percentage points of a `five_hour`/`seven_day`/
`spend_limit` window one million tokens (or one list-price dollar) is
actually worth on *this* machine's own observed history — so a dollar
saving elsewhere in the report can be re-expressed as "≈ x% of your
weekly window" instead.

## What `elasticity.py` provides

- `compute_elasticity(usage_rows, results, rates, thresholds=None, *,
  now=None) -> ElasticityStats` — the one-shot entry point: pairs
  `usage_rows` (as `tools.log_usage.load_usage_log` returns), sums token
  volume from `results` (every parsed transcript — top-level and
  subagent, every project) between each pair, fits and gates each
  (window, metric) combination, and derives the window budget and
  recent-burn figures.
- `ElasticityThresholds` — `min_pairs` (default 8), `min_r2` (default
  0.5), `burn_window_hours` (default 24), `weekly_window` (default
  `"seven_day"`); `.from_config(config)` reads a nested `"elasticity"`
  sub-dict, `.describe()` renders the thresholds as report notes —
  mirrors `WasteThresholds`.
- `build_section(stats, thresholds=None) -> Section` — the `elasticity`
  report section (three tables, described below).
- `express_in_window(usd_saving, elasticity_stats, window=None) -> float
  | None` — converts a list-price USD saving into a share (percentage
  points) of a usage window via that window's own USD fit. Defaults to
  `elasticity_stats.thresholds.weekly_window` ("your weekly window").
  Returns `None` — never a misleading number — for a non-positive/
  non-finite saving, an unknown window, or a refused/non-positive fit.
- `RULES` — a one-element tuple, `(_rule_window_budget,)`, of `(report,
  th) -> list[Recommendation]` callables in the same shape `waste.py`'s
  own `RULES` already establishes.

## Method

1. **Pairing.** Within each window kind, consecutive usage-log rows
   (sorted by `logged_at`) form one observation. A pair whose two rows
   carry a different `resets_at` spans a reset and is dropped outright
   — the window started over partway through, so the delta no longer
   means what it means within one reset period. A pair with a negative
   delta (a corrected/rolled-back sample) is dropped too. Both drop
   counts are tracked per window and stated in the section's own notes.
2. **Volume.** For each surviving pair `(t0, t1]`, every priced turn
   (`Turn.turn_index > 0`) across *every* transcript handed to
   `compute_elasticity` — top-level and subagent alike, every project —
   whose own `Turn.ts` falls in that interval is summed into three
   volumes: new tokens (`input_tokens + cache_creation_tokens +
   output_tokens`), cache-read tokens (separately), and list-price USD
   via `pricing.price_turn` (the same turn-pricing function every other
   analytics module in this codebase uses).
3. **Fit.** Per window kind and per volume metric, `used_percentage
   delta = slope * volume` is fit through the origin (a window's
   `used_percentage` is 0 at a fresh reset with 0 tokens consumed, so an
   intercept would only fit noise), weighting each pair by `1/volume` —
   equivalently minimising squared *relative* error — which collapses
   the closed-form estimate to `slope = sum(deltas) / sum(volumes)`. A
   single low-volume, high-relative-noise pair cannot dominate a fit
   built from pairs spanning very different volumes, the way an
   unweighted least-squares-through-origin fit would let it. A pair
   contributes to a given metric's fit only when its own volume for
   that metric is strictly positive (the weight is undefined at zero);
   `n_pairs` is therefore reported per (window, metric), since a pair
   can have zero cache-read volume while still having positive
   new-token volume.
4. **Acceptance gate.** A fit is reported only when it clears
   `ElasticityThresholds.min_pairs` (default 8) *and*
   `ElasticityThresholds.min_r2` (default 0.5); below either, the slope
   is withheld (`None`) and the `elasticity_fit` table's own `reason`
   column states which bound failed. This is a hard refusal, not a
   low-confidence caveat: a slope fit from a handful of noisy pairs is
   worse than no number at all for something a user might act on.
5. **Budget and burn.** `100 / (percent per million new tokens)` is the
   million new tokens a full window is worth, reported for every window
   kind whose own new-tokens fit was accepted with a positive slope.
   The last `burn_window_hours` (default 24) hours of new-token volume,
   expressed as a share of `weekly_window` (default `seven_day`), is
   the recent-burn figure.

## The `elasticity` report section

| Table | What it shows |
|---|---|
| `elasticity_fit` | One row per window kind × metric (`new_tokens`, `cache_read`, `usd`): the unit, the fitted window-percent-per-unit slope (blank when refused), R², pairs used, residual spread (percentage points), whether the fit was accepted, and the reason when it wasn't. |
| `elasticity_budget` | One row per window kind: the derived million new tokens a full window is worth (blank unless that window's new-tokens fit was accepted with a positive slope), and a note (the fit's own pair count/R² when available, else the refusal reason). |
| `elasticity_recent_burn` | One row, for `weekly_window`: the burn-window length in hours, new tokens consumed in it, the resulting share of a full window, and a note (populated only when the fit backing it was refused). |

## Weighting choice

An **unweighted** least-squares-through-origin fit (`slope = sum(x*y) /
sum(x**2)`) would let the highest-volume pairs dominate the estimate
outright — exactly the wrong property when usage-log samples arrive at
uneven intervals and volumes can differ by orders of magnitude between
them. Weighting each pair by `1/volume` instead makes every pair
contribute to the estimate in proportion to its own measured percentage
delta rather than its raw size, and reduces to the simple, auditable
ratio `sum(deltas) / sum(volumes)` — total percentage-point movement
over total token volume, blended across every pair. R²/residual spread
are still computed from the *unweighted* residuals against that slope
(standard R², mean-of-`y` baseline) — a fit-quality diagnostic
independent of which estimator produced the slope.

## `express_in_window` for other sections

Every other analytics module in this codebase reports a ceiling saving
in USD. Under `config.billing == "subscription"`, that number alone
understates what actually matters to you: your plan's usage limits.
So `report.build_report` (via `_report_units`) calls
`compute_elasticity` whenever billing is `subscription` and
`<config-dir>/usage-log.csv` has rows, with thresholds from
`config.toml`'s `[thresholds.elasticity]`, and hands the result to
`units.Units`. `Units.money` then calls `express_in_window` for every
amount it phrases:

- With an accepted weekly-window USD fit, the amount reads "about x% of
  your weekly usage limit", with the list-price equivalent second.
- Otherwise (`express_in_window` returns `None`), it reads "$X
  list-price equivalent" plus a hint to log statusline usage-limit
  readings.

## The `window-budget` recommendation rule

`_rule_window_budget(report, th)` fires only when `report.meta.
billing_mode == "subscription"` *and* `th.weekly_window`'s own
new-tokens fit was accepted (read back off the already-rendered
`elasticity_budget` table — the same evidence contract every rule in
this codebase follows). It states the derived tokens-per-window budget
and the last-24h burn share, then names whichever *other*
already-computed recommendation on the report (`report.recommendations`
at the time this rule runs) looks like the biggest lever — ranked by
severity (`action` > `advice` > `info`), tie-broken by the largest
single numeric value in its own evidence — by that recommendation's
title only, deliberately never repeating a number it already owns.
`recommend.recommend()` runs it last, after every other rule's output
has been finished and ordered, so it sees the final titles.

## Assumptions

- All machine consumption in a paired interval is attributed to this
  machine's own transcripts (top-level and subagent, every project) —
  a second device, or a shared/team account logging into the same plan
  window, would need its own accounting.
- A pair spanning a reset is dropped outright, not adjusted for.
- Cache-read tokens may be weighted differently from new tokens by the
  plan's own internal usage-window accounting; this module fits and
  reports the observed rate exactly, rather than assuming it is near
  zero.
- The fit is observational, not causal: it describes the correlation
  seen in this machine's own logged history, not a controlled
  experiment or a guaranteed future rate.

## Where it appears

Under subscription billing with usage-log rows, `report.build_report`
adds the `elasticity` section right after `usage`, from the same fit
`units.Units` uses, and adds `elasticity.ASSUMPTIONS` to the report's
assumptions. `recommend.recommend()` runs `elasticity.RULES` last. The
dashboard shows the section on Spend › Usage. Under API billing, or with
no usage-log rows, none of this appears. No separate CLI subcommand
prints the section; `claudeglass report` includes it.
