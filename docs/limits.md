# Usage-limit tracking (v3-limits)

When an account hits its usage cap, Claude Code's harness pauses; on
resume, the prompt cache has expired. Left unattributed, that pause looks
exactly like an ordinary long idle gap — inflating "gaps > 5 min" counts,
masquerading as a routine full-expiry re-cache, driving long-tool-wait
recommendations, and occasionally pushing a session into "overnight" mode
purely because a cap pause, not real overnight work, made the span/gap
long enough. v3-limits turns a usage-cap pause, a harness-forced subagent
termination, and the desktop app's automatic resume ping into first-class,
attributable facts, and threads that attribution through every downstream
table that would otherwise misread it.

`events.py`/`parse.py` do the actual detection; `limits.py` is purely a
*reader* of their already-parsed state — it detects nothing new. A
synthetic assistant line's text becomes `EventKind.LIMIT_HIT` (subkind
`session_limit`/`weekly_limit`), the desktop app's resume ping becomes
`EventKind.LIMIT_RESUME`, and a harness-killed subagent's task
notification becomes `EventKind.AGENT_TERMINATED` (subkind
`rate_limit`/`other`). Every turn whose gap to the previous one spanned
one of these events carries `Turn.gap_cause == "limit"`.

## What `limits.py` provides

- `limit_pause_intervals(top)` — every usage-cap pause in a transcript's
  own top-level turns, as `(start, end)` UTC-aware `datetime` pairs, read
  directly off `Turn.gap_cause`/`Turn.gap_s`/`Turn.ts` (not re-derived
  from raw `LIMIT_HIT`/`LIMIT_RESUME` events — see the deviation note
  below). Consumed by `classify.py` to discount pause time out of its
  gap/span statistics (see "Downstream attribution" below).
- `limit_markers(result)` — every `LIMIT_HIT`/`LIMIT_RESUME`/
  `AGENT_TERMINATED` event as a `(ts, kind, detail)` triple, sorted by
  `ts`, ready for a session-timeline API to render as markers (see
  "Session-timeline marker contract" below).
- `LimitStats`/`build_section` — the corpus-wide `limits` report section
  (six tables, described below).
- `csv_cross_check` — cross-checks the transcript-derived hit count
  against `usage-log.csv` (`tools/log_usage.py`'s own ground-truth log,
  when one exists) as a sanity check, not a second detector.

## The `limits` report section

| Table | What it shows |
|---|---|
| `limits_summary` | One "all" row: transcripts, sessions affected, limit hits (session + weekly split), resumes, agents terminated (and by rate limit specifically), pause count/total time, and the cache-creation tokens/write cost paid by the turn immediately following each pause. |
| `limits_hits_by_kind` | `session_limit`/`weekly_limit` hit counts and share. |
| `limits_agent_terminated` | `rate_limit`/`other` termination counts and share. |
| `limits_pauses` | Corpus-wide pause count/total/mean duration (a corpus-wide *median* can't be derived from already-aggregated per-agent-type medians, so it isn't reported here — see the by-agent-type table). |
| `limits_reset_hour_histogram` | Count and share of `LIMIT_HIT` resets by local hour of day (0-23). |
| `limits_by_agent_type` | Per-agent-type roll-up: hits, resumes, terminations, pause count/total/median/max, and the post-pause cache-creation tokens/cost. |
| `limits_csv_cross_check` (`csv_cross_check`, called separately) | Transcript-derived hit counts vs. `usage-log.csv`'s own exhaustion-row counts for `five_hour`/`seven_day`. |

### Reconciling the two "cost of a limit pause" figures (N2)

Two tables both put a dollar figure on usage-cap pauses, and they are
**related but not equal** — reading one as a check on the other will
look like a discrepancy unless the population difference is understood:

| Figure | Table | Population |
|---|---|---|
| `limit_turn_write_cost_usd` | `limits_summary` (this section) | Every turn with `Turn.gap_cause == "limit"` and `turn_index > 0` — i.e. every turn that immediately followed a usage-cap pause, full stop. |
| `unavoidable_limit_expiry_cost_usd` | `recache_summary` (`docs/sections-reference.md`'s "recache" section) | The subset of the above that *also* clears `recache.py`'s ordinary re-cache thresholds (`ctx > ctx_floor` and `cache_read_tokens < cr_ratio * ctx` — see `recache.detect`). A post-pause turn with a small context, or one whose cache happened to still hold enough to clear `cr_ratio`, is counted here but not there. |

Both are legitimate: `limits_summary`'s figure answers "what did every
post-pause turn cost to rewrite its cache", unconditionally, because the
assumption above ("`Turn.gap_cause == "limit"``' always did a full prefix
rewrite") holds regardless of `recache.py`'s thresholds. `recache_summary`'s
figure exists to make sure `avoidable_cost_usd` on that same table only
ever totals genuinely avoidable causes — a limit-expiry turn's cost is
reported there as `unavoidable_limit_expiry_cost_usd`, a separate column,
specifically so it is never summed into `avoidable_cost_usd` (review B5)
and never double-counted as caching behaviour to fix. Neither figure is
wrong; they simply answer different questions over overlapping but
distinct populations.

## Downstream attribution

- **`classify.py`**: `SessionFeatures.limit_pause_s` sums every pause
  interval's duration. `_median_and_max_gap` subtracts per-gap pause
  overlap (clamped at zero) so a usage-cap pause no longer counts as a
  behavioural gap. `classify_mode`'s overnight check uses
  `effective_span_s = max(0.0, span_s - limit_pause_s)` instead of the
  raw span, so a pause alone can no longer misclassify a session as
  overnight; the `multi_day` flag still uses the raw span (a pause
  spanning midnight is still, correctly, multi-day).
- **`recache.py`/`ttl.py`**: a pause-caused full-expiry re-cache is priced
  the same way any other full-expiry re-cache is (both already establish
  that a `gap_cause == "limit"` turn does a full prefix rewrite under
  every TTL policy); `limits.py` reads that same fact rather than
  re-deriving it, so the two can never drift apart.
- **`recommend.py`**: the `limit-pressure` rule (`_rule_limit_pressure`)
  fires when the `limits_summary` table reports at least
  `limit_pressure_min_hits` (default 3) limit hits, or at least
  `limit_pressure_min_terminated_rate_limit` (default 1) subagent
  terminated specifically by the rate limit — surfacing repeated
  usage-cap pressure as its own finding, since it is the root cause
  behind several other tables' downstream symptoms.
- **`scorecard.py`**: `ScorecardInputs.limit_recache_share_pct` (portion
  of `recache_share_pct` already known to be pause-forced) is subtracted
  from the raw re-cache share before `cache_efficiency` scores it — an
  account-level pause is not a workflow choice, and scoring it as one
  would be misleading — reported via a note rather than silently.
  `ScorecardInputs.limit_pause_sessions`, when non-zero, adds a
  non-scoring `data_quality` note pointing at this section.
- **`statusline.py`**: a `5h`/`7d` rate-limit segment gets a trailing
  `!` marker once its `used_percentage` clears 90%, warning live that a
  harness pause may follow shortly; a `usage-log.csv` row for
  `five_hour`/`seven_day` read back at or above 100% used is tagged
  `source=limit_hit` instead of the usual `source=statusline`.

## Session-timeline marker contract

`limit_markers(result) -> list[tuple[str, str, dict]]` is the basis for
`service/api.py`'s `GET /api/session/<id>` usage-limit markers: each
triple is `(ts, kind, detail)`, sorted by `ts` (ascending, ISO-8601
string comparison; an event with no timestamp sorts first as `""`).

- `ts`: the event's own `Event.ts` (an ISO-8601 string), or `""`.
- `kind`: the event kind's own string value — one of `"limit_hit"`,
  `"limit_resume"`, `"agent_terminated"`.
- `detail`: a shallow copy of the event's own `Event.detail` dict (already
  privacy-clean — counts, enum-like strings, an hour-of-day integer, no
  message text or paths), plus a `"subkind"` key when the event carries
  one (`session_limit`/`weekly_limit` for `limit_hit`;
  `rate_limit`/`other` for `agent_terminated`).

A consumer can render one marker per triple without importing `EventKind`
or reaching into `TranscriptResult.events` directly.

Wired: `service/store.py`'s `Store.turns_for_session` calls this
function against the session's stored top-level transcript digest and
reshapes each triple into a `{"ts", "kind", "detail"}` object;
`service/api.py`'s `route_session` forwards the result as
`GET /api/session/<id>`'s `limit_markers` field (see
[`docs/api.md`](api.md)). `service/static/app.js`'s session timeline
renders them as their own marker kind (distinct colour, legend entry,
tooltip naming `kind`/`detail.subkind`), positioned along the chart's
time axis by `ts` rather than by turn index — unlike the
compaction/spawn/human markers, a usage-limit event's timestamp falls
*inside* the pause gap between two turns, not at a turn index of its
own, so it cannot be pinned to one of `turn_series`'s existing points
(see `docs/ui.md`).

## Assumptions and what isn't attributable

- A pause's `(start, end)` interval is read directly off the turn that
  carries `Turn.gap_cause == "limit"` (`start = turn.ts - turn.gap_s`,
  `end = turn.ts`), not re-derived from raw `LIMIT_HIT`/`LIMIT_RESUME`
  events. The two are equivalent by construction (`parse.py`'s two-buffer
  scheme guarantees the limit event(s) precede exactly the turn that
  carries `gap_cause == "limit"`), and this is far simpler.
- A turn immediately following a usage-cap pause is priced via
  `pricing.price_turn`'s default observed-split path, not re-detected
  against `recache.py`'s `ctx_floor`/`cr_ratio` thresholds — it always did
  a full prefix rewrite regardless of those thresholds.
- Reset-hour-of-day prefers `LIMIT_HIT`'s own `reset_minutes_of_day` (the
  literal local hour named in the synthetic text, e.g. "resets 3:00pm")
  over converting `reset_ts` (always UTC) through the machine's own local
  zone, which is used only as a fallback when the text carried no
  parseable "resets ..." clause.
- `csv_cross_check` counts a `usage-log.csv` row as an exhaustion signal
  purely on `used_percentage >= csv_exhaustion_pct` (default 100%): a bare
  `resets_at` value is present on nearly every row regardless of
  exhaustion, so it is never treated as a signal on its own.
- Pause intervals are computed only from a transcript's own top-level
  turns, never a subagent's — a pause is an account-wide event, but the
  gap/span statistics it feeds (`classify.py`'s median/max gap, its
  overnight check) are themselves top-level-only.
- A `LIMIT_HIT` whose reset clause has neither `reset_minutes_of_day` nor
  a parseable `reset_ts` is excluded from the reset-hour histogram (but
  still counted in `limits_summary`).
- This module does not attempt to distinguish a usage-cap pause from an
  ordinary long idle gap that merely happens to end near a `LIMIT_HIT`
  event with no matching turn (e.g. the transcript ends mid-pause) — such
  a case simply produces no `gap_cause == "limit"` turn and is not
  double-counted or guessed at.
