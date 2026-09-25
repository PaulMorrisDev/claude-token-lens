"""Usage-window elasticity (v4-elasticity): how many percentage points of
a Claude subscription's ``five_hour``/``seven_day``/``spend_limit``
window one million tokens (or one list-price dollar) is actually worth,
measured empirically from this machine's own usage-log samples and its
own parsed transcript corpus.

The project's whole point is helping a subscription user stay inside
their 5-hour/7-day usage windows, not just their USD spend -- the
binding constraint most target users actually feel. Every other
analytics module in this codebase reports a saving in dollars;
:func:`express_in_window` is the one function that turns a dollar saving
back into "x% of your weekly window".

Where it is used: under subscription billing with a non-empty
``<config_dir>/usage-log.csv``, ``report._report_units`` runs
:func:`compute_elasticity` (thresholds from ``config.toml``'s
``[thresholds.elasticity]``) and hands the result to ``units.Units``,
whose ``money`` phrases every amount through :func:`express_in_window`.
``report.build_report`` also adds the ``elasticity`` section
(:func:`build_section`) from the same result, and ``recommend.recommend``
runs :data:`RULES` last, after the other recommendations are final.

Method (see the project brief this module was written against):

1. **Pairing.** ``tools.log_usage``'s CSV logs one row per
   ``(window, used_percentage, resets_at)`` sample every time the
   statusline (or a hand-pasted ``get_usage`` result) is logged. Two
   *consecutive* rows for the same window, sorted by ``logged_at``, form
   one observation: the increase in ``used_percentage`` between them was
   caused by whatever tokens this machine consumed (any session, any
   agent) in that interval. A pair whose two rows carry a different
   ``resets_at`` spans a window reset -- the window started over
   partway through, so the observed delta no longer means what it means
   within one reset period -- and is dropped outright, not adjusted for.
   A pair with a negative delta (a corrected/rolled-back sample) is
   dropped too.
2. **Volume.** For each surviving pair ``(t0, t1]``, every priced turn
   (``Turn.turn_index > 0``, the standing "only count what actually
   became a billable request" convention every analytics module in this
   codebase follows) across *every* parsed transcript handed to
   :func:`compute_elasticity` -- top-level and subagent alike, every
   project -- whose own ``Turn.ts`` falls in that interval is summed
   into three volumes: new tokens (``input_tokens + cache_creation_tokens
   + output_tokens``), cache-read tokens (separately), and list-price
   USD via :func:`pricing.price_turn` (the same turn-pricing function
   every other analytics module in this codebase uses).
3. **Fit.** Per window kind and per volume metric, this module fits
   ``used_percentage delta = slope * volume`` through the origin (a
   window's ``used_percentage`` is 0 at a fresh reset with 0 tokens
   consumed, so an intercept term would only fit noise) via
   :func:`_weighted_least_squares_through_origin`: each pair is weighted
   by ``1/volume`` (equivalently, the fit minimises squared *relative*
   error), which reduces the estimator to the interpretable ratio
   ``slope = sum(deltas) / sum(volumes)`` -- a single low-volume pair's
   own noise cannot dominate a fit built from pairs spanning very
   different volumes, which an unweighted (equal-weight) fit would be
   vulnerable to. A pair contributes to a given metric's fit only when
   its own volume for that metric is strictly positive (the weight is
   otherwise undefined); ``n_pairs`` is reported per (window, metric)
   for exactly this reason, since a pair can have zero cache-read volume
   while still having positive new-token volume.
4. **Acceptance gate.** A fit is reported only when it clears
   ``ElasticityThresholds.min_pairs`` (default 8) *and*
   ``ElasticityThresholds.min_r2`` (default 0.5); below either,
   :class:`FitResult.slope` is withheld (``None``) and ``.reason``
   states which bound failed and by how much. This is a hard refusal,
   not a low-confidence caveat -- the brief's own instruction -- because
   a slope fit from a handful of noisy pairs is worse than no number at
   all for something a user might act on.
5. **Budget.** ``100 / (percent per million new tokens)`` is the million
   new tokens a full window is worth, reported in the ``elasticity_budget``
   table for every window kind whose own new-tokens fit was accepted
   (and whose slope is positive -- a non-positive slope cannot be
   inverted into a budget). The last-``ElasticityThresholds.
   burn_window_hours`` (default 24) hours of new-token volume, expressed
   as a share of ``ElasticityThresholds.weekly_window`` (default
   ``"seven_day"``, "your weekly window"), is reported in
   ``elasticity_recent_burn``.

Privacy: every field this module reads or produces is a window name, a
count, a percentage, a token total, or a USD figure -- never message
text, tool output, or a file path. ``usage_rows`` (as produced by
``tools.log_usage.load_usage_log``) and ``TranscriptResult`` are both
already-sanitised inputs; this module adds no new string field capable
of carrying either.

``RULES`` -- a one-element tuple, ``(_rule_window_budget,)``, of
``(report, th) -> list[Recommendation]`` callables in the same shape
``waste.py``'s own ``RULES`` (and, before it, ``recommend.py``'s internal
``_rule_*`` functions) already have: it operates on the *rendered*
``ReportModel`` via self-contained ``_cell``/``_evidence`` helpers
(duplicated locally rather than imported from ``waste.py``/
``recommend.py``, matching both modules' own stated reason -- avoiding a
circular import), not on an ``ElasticityStats`` instance directly.
``_rule_window_budget`` additionally reads ``report.recommendations``
to name the single most material *other* recommendation, by its title
only -- deliberately never repeating a number that recommendation
already owns. ``recommend.recommend`` therefore runs it after every
other rule has been finished and ordered.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Sequence

from .model import Column, Recommendation, ReportModel, Section, Table, TranscriptResult, Turn
from .pricing import Pricing, price_turn
from .tools.log_usage import WINDOW_NAMES

#: This module's own judgement calls, printed verbatim in the report's
#: "## Assumptions" block (via ``ReportMeta.assumptions``) alongside
#: every other module's own tuple of the same name -- see ``waste.py``'s
#: identical convention.
ASSUMPTIONS: tuple[str, ...] = (
    "All machine consumption in a paired interval is attributed to this "
    "machine's own transcripts (top-level and subagent, every project) "
    "-- a second device, or a shared/team account logging into the same "
    "plan window, would need its own accounting; this module cannot see "
    "tokens it never parsed a transcript for.",
    "A pair spanning a reset -- the two usage log rows name different "
    "reset times -- is dropped outright, not adjusted for.",
    "Cache-read tokens may be weighted differently from new tokens by "
    "the plan's own internal usage-window accounting; this module fits "
    "and reports the percent-per-million-cache-read-token rate exactly "
    "as observed, rather than assuming it is near zero.",
    "The fit is observational, not causal: it describes the correlation "
    "between token volume and movement in the share of the limit used "
    "across this machine's own logged history, not a controlled experiment or a "
    "guaranteed future rate.",
)

#: Plain names for each window kind, for recommendation text.
WINDOW_LABELS: dict[str, str] = {"five_hour": "5-hour", "seven_day": "weekly", "spend_limit": "spend"}

#: The three volume metrics every window kind is fit against.
_METRICS: tuple[str, ...] = ("new_tokens", "cache_read", "usd")

_METRIC_UNIT_LABEL: dict[str, str] = {
    "new_tokens": "million new tokens",
    "cache_read": "million cache-read tokens",
    "usd": "USD (list price)",
}

#: The window kind :func:`express_in_window` and :class:`ElasticityThresholds`
#: default to when nothing more specific is asked for -- "your weekly
#: window" in the plan's own vocabulary.
DEFAULT_WEEKLY_WINDOW = "seven_day"


# -- thresholds ---------------------------------------------------------------


@dataclass(slots=True)
class ElasticityThresholds:
    """The tunable numbers this module's fit/refusal/budget logic depends
    on. Mirrors ``WasteThresholds``'s ``from_config``/``describe``
    convention.
    """

    #: A (window, metric) fit is reported only when it has at least this
    #: many usable pairs.
    min_pairs: int = 8
    #: ...and its R² is at least this.
    min_r2: float = 0.5
    #: The trailing window (hours) :func:`compute_elasticity`'s recent-burn
    #: figure is measured over.
    burn_window_hours: float = 24.0
    #: Which window kind counts as "your weekly window" for
    #: :func:`express_in_window` and the ``window-budget`` rule.
    weekly_window: str = DEFAULT_WEEKLY_WINDOW

    @classmethod
    def from_config(cls, config: dict | None) -> "ElasticityThresholds":
        data = config or {}
        if not isinstance(data, dict):
            data = {}
        nested = data.get("elasticity")
        if isinstance(nested, dict):
            data = nested
        kwargs: dict = {}
        if "min_pairs" in data:
            kwargs["min_pairs"] = int(data["min_pairs"])
        if "min_r2" in data:
            kwargs["min_r2"] = float(data["min_r2"])
        if "burn_window_hours" in data:
            kwargs["burn_window_hours"] = float(data["burn_window_hours"])
        if "weekly_window" in data:
            kwargs["weekly_window"] = str(data["weekly_window"])
        return cls(**kwargs)

    def describe(self) -> list[str]:
        weekly = WINDOW_LABELS.get(self.weekly_window, self.weekly_window)
        return [
            f"A fit is shown only with at least {self.min_pairs} readings and a fit "
            f"score of {self.min_r2:.2f} or more; below either, the slope is withheld "
            "and the fit table says why.",
            f"Recent use is measured over the last {self.burn_window_hours:.0f} hours.",
            f"The {weekly} limit is the one treated as your weekly limit.",
        ]


_DEFAULT_THRESHOLDS = ElasticityThresholds()


# -- fit / stats containers ----------------------------------------------------


@dataclass(slots=True)
class FitResult:
    """One (window, metric) fit: a slope through the origin plus its own
    fit-quality diagnostics, and whether it cleared the acceptance gate.
    """

    window: str = ""
    metric: str = ""
    #: Percent-of-window per unit of this metric (see
    #: ``_METRIC_UNIT_LABEL``). ``None`` when ``accepted`` is ``False`` --
    #: a refused fit reports no figure, per the brief.
    slope: float | None = None
    #: Always populated when at least one usable pair exists, even for a
    #: refused fit, so ``.reason`` can cite the actual number that fell
    #: short.
    r2: float | None = None
    n_pairs: int = 0
    residual_std: float | None = None
    accepted: bool = False
    reason: str | None = None


@dataclass(slots=True)
class ElasticityStats:
    """Everything :func:`build_section` needs to render the ``elasticity``
    report section, and everything :func:`express_in_window` needs to
    convert a USD saving into a share of a usage window.
    """

    thresholds: ElasticityThresholds = field(default_factory=ElasticityThresholds)
    #: window -> metric -> FitResult, one entry per (window, metric) in
    #: ``tools.log_usage.WINDOW_NAMES`` x ``_METRICS``.
    fits: dict[str, dict[str, FitResult]] = field(default_factory=dict)
    #: window -> total consecutive usage-log row pairs considered
    #: (kept + dropped), for diagnostics/notes.
    pairs_seen: dict[str, int] = field(default_factory=dict)
    pairs_dropped_reset: dict[str, int] = field(default_factory=dict)
    pairs_dropped_negative: dict[str, int] = field(default_factory=dict)
    #: window -> million new tokens per full window, only for windows
    #: whose own new-tokens fit was accepted with a positive slope.
    window_budget_million_tokens: dict[str, float] = field(default_factory=dict)
    recent_burn_window: str = DEFAULT_WEEKLY_WINDOW
    recent_burn_hours: float = 24.0
    recent_new_tokens: float = 0.0
    #: Share of ``recent_burn_window``'s full window burned in the last
    #: ``recent_burn_hours`` hours. ``None`` when that window's own
    #: new-tokens fit wasn't accepted -- see ``recent_burn_reason``.
    recent_burn_pct: float | None = None
    recent_burn_reason: str | None = None
    pricing_version: str | None = None

    def fit(self, window: str, metric: str) -> FitResult | None:
        return self.fits.get(window, {}).get(metric)


# -- shared small helpers (each analytics module keeps its own copy of
#    these -- see corpus.py/compaction.py/exports.py/waste.py etc. for
#    the same "own module-local _parse_ts" convention) ------------------


def _parse_ts_utc(ts: str | None) -> datetime | None:
    """Same parsing as every other module's own ``_parse_ts``, plus
    coercing a naive result to UTC so a usage-log ``logged_at`` and a
    ``Turn.ts`` can always be compared/sorted against each other without
    ever mixing naive and aware ``datetime`` values (see
    ``corpus.py``'s own docstring on why that mix is avoided
    elsewhere in this codebase).
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _priced_turns(result: TranscriptResult) -> list[Turn]:
    return [t for t in result.turns if t.turn_index > 0]


def _turn_new_tokens(turn: Turn) -> int:
    return turn.input_tokens + turn.cache_creation_tokens + turn.output_tokens


def _usage_pct(row: dict) -> float | None:
    value = row.get("used_percentage")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# -- pairing --------------------------------------------------------------


@dataclass(slots=True)
class _Pair:
    t0: datetime
    t1: datetime
    delta_pct: float


def _build_pairs(
    usage_rows: Sequence[dict],
) -> tuple[dict[str, list[_Pair]], dict[str, int], dict[str, int]]:
    """Group ``usage_rows`` by window, sort each group by ``logged_at``,
    and pair consecutive rows: a pair whose two rows carry a different
    ``resets_at`` is dropped (reset boundary); a pair whose delta is
    negative is dropped too. Rows with an unparseable/missing
    ``logged_at`` or ``used_percentage`` are skipped before pairing
    (they can't anchor either side of an interval).

    Returns ``(pairs_by_window, dropped_reset_by_window,
    dropped_negative_by_window)``, every dict keyed by every name in
    ``tools.log_usage.WINDOW_NAMES`` (present even at 0).
    """
    by_window: dict[str, list[dict]] = {name: [] for name in WINDOW_NAMES}
    for row in usage_rows:
        window = row.get("window")
        if window not in by_window:
            continue
        ts = _parse_ts_utc(row.get("logged_at"))
        pct = _usage_pct(row)
        if ts is None or pct is None:
            continue
        by_window[window].append({"ts": ts, "pct": pct, "resets_at": row.get("resets_at") or None})

    pairs: dict[str, list[_Pair]] = {name: [] for name in WINDOW_NAMES}
    dropped_reset: dict[str, int] = {name: 0 for name in WINDOW_NAMES}
    dropped_negative: dict[str, int] = {name: 0 for name in WINDOW_NAMES}

    for window, rows in by_window.items():
        rows_sorted = sorted(rows, key=lambda r: r["ts"])
        for a, b in zip(rows_sorted, rows_sorted[1:]):
            if a["resets_at"] != b["resets_at"]:
                dropped_reset[window] += 1
                continue
            delta = b["pct"] - a["pct"]
            if delta < 0:
                dropped_negative[window] += 1
                continue
            pairs[window].append(_Pair(t0=a["ts"], t1=b["ts"], delta_pct=delta))

    return pairs, dropped_reset, dropped_negative


# -- turn volume index (all transcripts, all projects) ------------------


@dataclass(slots=True)
class _TurnIndex:
    """Sorted-by-timestamp prefix sums of every priced turn's own new
    tokens / cache-read tokens / list-price USD, across every transcript
    handed to :func:`compute_elasticity` -- lets any ``(t0, t1]`` interval
    (a usage-log pair, or the recent-burn window) be summed in
    O(log n) via ``bisect`` rather than rescanning the whole corpus once
    per interval.
    """

    dts: list[datetime]
    cum_new_tokens: list[float]
    cum_cache_read_tokens: list[float]
    cum_usd: list[float]

    def sums_in(self, t0: datetime, t1: datetime) -> tuple[float, float, float]:
        """``(new_tokens, cache_read_tokens, usd)`` summed over every
        turn with ``t0 < ts <= t1`` (the brief's own half-open interval)."""
        lo = bisect.bisect_right(self.dts, t0)
        hi = bisect.bisect_right(self.dts, t1)
        return (
            self.cum_new_tokens[hi] - self.cum_new_tokens[lo],
            self.cum_cache_read_tokens[hi] - self.cum_cache_read_tokens[lo],
            self.cum_usd[hi] - self.cum_usd[lo],
        )


def _build_turn_index(results: Sequence[TranscriptResult], rates: Pricing) -> _TurnIndex:
    entries: list[tuple[datetime, int, int, float]] = []
    for result in results:
        for turn in _priced_turns(result):
            dt = _parse_ts_utc(turn.ts)
            if dt is None:
                continue
            resolved = rates.resolve_model(turn.model)
            breakdown = price_turn(turn, resolved)
            entries.append((dt, _turn_new_tokens(turn), turn.cache_read_tokens, breakdown.total))
    entries.sort(key=lambda e: e[0])

    dts: list[datetime] = []
    cum_new: list[float] = [0.0]
    cum_cr: list[float] = [0.0]
    cum_usd: list[float] = [0.0]
    for dt, new_tok, cr_tok, usd in entries:
        dts.append(dt)
        cum_new.append(cum_new[-1] + new_tok)
        cum_cr.append(cum_cr[-1] + cr_tok)
        cum_usd.append(cum_usd[-1] + usd)

    return _TurnIndex(dts=dts, cum_new_tokens=cum_new, cum_cache_read_tokens=cum_cr, cum_usd=cum_usd)


def _metric_volume(metric: str, sums: tuple[float, float, float]) -> float:
    new_tokens, cache_read_tokens, usd = sums
    if metric == "new_tokens":
        return new_tokens / 1_000_000.0
    if metric == "cache_read":
        return cache_read_tokens / 1_000_000.0
    if metric == "usd":
        return usd
    raise ValueError(f"unknown elasticity metric: {metric!r}")


# -- weighted least squares through the origin ---------------------------


@dataclass(slots=True)
class _FitCore:
    slope: float
    r2: float
    residual_std: float
    n: int


def _weighted_least_squares_through_origin(points: Sequence[tuple[float, float]]) -> _FitCore | None:
    """Fit ``y = slope * x`` through the origin, weighting each point by
    ``1/x`` -- equivalent to minimising squared *relative* error, which
    reduces the closed-form estimate to ``slope = sum(y) / sum(x)``: a
    single low-volume, high-relative-noise pair cannot dominate a fit
    built from pairs spanning very different volumes, the way an
    unweighted (equal-weight) least-squares-through-origin fit would let
    it. Points with ``x <= 0`` must already be excluded by the caller
    (the weight is undefined there). Returns ``None`` when there are no
    usable points, or their total ``x`` is not positive.

    ``r2``/``residual_std`` are computed from the *unweighted* residuals
    against this slope (standard R², using the mean of ``y`` as the
    baseline) -- diagnostics describing how well the fitted line
    actually explains the observed deltas, independent of which
    weighting scheme produced the slope estimate itself.
    """
    n = len(points)
    if n == 0:
        return None
    sum_x = sum(x for x, _ in points)
    sum_y = sum(y for _, y in points)
    if sum_x <= 0:
        return None

    slope = sum_y / sum_x
    residuals = [y - slope * x for x, y in points]
    mean_y = sum_y / n
    ss_res = sum(r * r for r in residuals)
    ss_tot = sum((y - mean_y) ** 2 for _, y in points)
    if ss_tot > 0:
        r2 = 1.0 - ss_res / ss_tot
    else:
        # Every observed delta was identical: a degenerate baseline. A
        # perfect (zero-residual) fit still deserves r2=1.0; anything
        # else against a baseline with no variance to explain is
        # reported as 0.0 rather than an undefined/negative-infinity
        # value.
        r2 = 1.0 if ss_res == 0 else 0.0
    residual_std = math.sqrt(ss_res / n)
    return _FitCore(slope=slope, r2=r2, residual_std=residual_std, n=n)


def _build_fit(
    window: str,
    metric: str,
    pairs: Sequence[_Pair],
    turn_index: _TurnIndex,
    th: ElasticityThresholds,
) -> FitResult:
    points: list[tuple[float, float]] = []
    for pair in pairs:
        volume = _metric_volume(metric, turn_index.sums_in(pair.t0, pair.t1))
        if volume > 0:
            points.append((volume, pair.delta_pct))

    core = _weighted_least_squares_through_origin(points)
    n = core.n if core else 0
    r2 = core.r2 if core else None
    residual_std = core.residual_std if core else None
    raw_slope = core.slope if core else None

    accepted = True
    reason: str | None = None
    if n < th.min_pairs:
        accepted = False
        reason = f"only {n} pair(s) with usable {metric} volume (need at least {th.min_pairs})"
    elif r2 is None or r2 < th.min_r2:
        accepted = False
        r2_display = f"{r2:.2f}" if r2 is not None else "undefined"
        reason = f"R²={r2_display} is below the {th.min_r2:.2f} acceptance threshold"

    return FitResult(
        window=window,
        metric=metric,
        slope=raw_slope if accepted else None,
        r2=r2,
        n_pairs=n,
        residual_std=residual_std,
        accepted=accepted,
        reason=reason,
    )


# -- compute_elasticity ---------------------------------------------------


def compute_elasticity(
    usage_rows: Sequence[dict],
    results: Sequence[TranscriptResult],
    rates: Pricing,
    thresholds: ElasticityThresholds | None = None,
    *,
    now: datetime | None = None,
) -> ElasticityStats:
    """Fit usage-window elasticity from ``usage_rows`` (as
    ``tools.log_usage.load_usage_log`` returns) against every transcript
    in ``results`` (top-level and subagent alike, every project -- see
    the module docstring's step 2), for each window kind and each of the
    three volume metrics, and derive the window-budget and recent-burn
    figures from the accepted fits.

    ``now`` defaults to the real current UTC time; a caller (a test, or
    a read-only verification run) that needs a deterministic
    recent-burn figure should pass its own fixed value.
    """
    th = thresholds or _DEFAULT_THRESHOLDS

    pairs_by_window, dropped_reset, dropped_negative = _build_pairs(usage_rows)
    turn_index = _build_turn_index(results, rates)

    fits: dict[str, dict[str, FitResult]] = {}
    window_budget: dict[str, float] = {}
    for window in WINDOW_NAMES:
        pairs = pairs_by_window.get(window, [])
        window_fits: dict[str, FitResult] = {}
        for metric in _METRICS:
            window_fits[metric] = _build_fit(window, metric, pairs, turn_index, th)
        fits[window] = window_fits

        new_tokens_fit = window_fits["new_tokens"]
        if new_tokens_fit.accepted and new_tokens_fit.slope and new_tokens_fit.slope > 0:
            window_budget[window] = 100.0 / new_tokens_fit.slope

    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    burn_start = now_dt - timedelta(hours=th.burn_window_hours)
    recent_new_tokens, _recent_cache_read, _recent_usd = turn_index.sums_in(burn_start, now_dt)

    weekly_window = th.weekly_window
    weekly_fit = fits.get(weekly_window, {}).get("new_tokens")
    recent_burn_pct: float | None = None
    recent_burn_reason: str | None = None
    if weekly_fit is None:
        recent_burn_reason = f"unknown window kind {weekly_window!r} (not in {WINDOW_NAMES})"
    elif not weekly_fit.accepted or not weekly_fit.slope or weekly_fit.slope <= 0:
        recent_burn_reason = (
            f"{weekly_window}'s new-tokens fit was not accepted "
            f"({weekly_fit.reason or 'no usable slope'})"
        )
    else:
        recent_burn_pct = (recent_new_tokens / 1_000_000.0) * weekly_fit.slope

    return ElasticityStats(
        thresholds=th,
        fits=fits,
        pairs_seen={
            w: len(pairs_by_window.get(w, [])) + dropped_reset.get(w, 0) + dropped_negative.get(w, 0)
            for w in WINDOW_NAMES
        },
        pairs_dropped_reset=dict(dropped_reset),
        pairs_dropped_negative=dict(dropped_negative),
        window_budget_million_tokens=window_budget,
        recent_burn_window=weekly_window,
        recent_burn_hours=th.burn_window_hours,
        recent_new_tokens=recent_new_tokens,
        recent_burn_pct=recent_burn_pct,
        recent_burn_reason=recent_burn_reason,
        pricing_version=rates.version,
    )


# -- express_in_window ------------------------------------------------------


def express_in_window(
    usd_saving: float,
    elasticity: ElasticityStats,
    window: str | None = None,
) -> float | None:
    """Convert a list-price USD saving into a share (percentage points)
    of a usage window, using that window's own fitted percent-per-USD
    elasticity -- the function a wiring step calls to attach an
    "≈ x% of your weekly window" suffix to any recommendation's
    saving line once ``config.billing == "subscription"``.

    ``window`` defaults to ``elasticity.thresholds.weekly_window`` ("your
    weekly window" -- ``"seven_day"`` unless configured otherwise); pass
    an explicit window name (one of ``tools.log_usage.WINDOW_NAMES``) to
    express a saving against a different window instead.

    Returns ``None`` -- never a number that would be misleading -- when:

    - ``usd_saving`` isn't a positive, finite number (a saving of zero
      or less has no window share worth stating);
    - ``window`` isn't a window kind this module fit at all;
    - that window's own USD fit was refused by the acceptance gate (see
      :class:`ElasticityThresholds`), or its slope isn't positive.
    """
    if not isinstance(usd_saving, (int, float)) or not math.isfinite(usd_saving) or usd_saving <= 0:
        return None
    window_name = window or elasticity.thresholds.weekly_window
    fit = elasticity.fits.get(window_name, {}).get("usd")
    if fit is None or not fit.accepted or not fit.slope or fit.slope <= 0:
        return None
    return usd_saving * fit.slope


# -- report section -----------------------------------------------------------


def build_section(stats: ElasticityStats, thresholds: ElasticityThresholds | None = None) -> Section:
    """Render ``stats`` into the ``elasticity`` report section: the fit
    table, the derived window budget, and the recent-burn share.
    """
    th = thresholds or stats.thresholds or _DEFAULT_THRESHOLDS

    fit_columns = [
        Column(key="window", label="Window", kind="str"),
        Column(key="metric", label="Metric", kind="str"),
        Column(key="unit", label="Per unit", kind="str"),
        Column(key="slope", label="Window % per unit", kind="float"),
        Column(key="r2", label="R²", kind="float"),
        Column(key="n_pairs", label="Pairs used", kind="int"),
        Column(key="residual_std", label="Residual spread (pct pts)", kind="float"),
        Column(key="accepted", label="Accepted", kind="str"),
        Column(key="reason", label="Reason", kind="str"),
    ]
    fit_rows: list[list] = []
    for window in WINDOW_NAMES:
        window_fits = stats.fits.get(window, {})
        for metric in _METRICS:
            fit = window_fits.get(metric)
            if fit is None:
                continue
            fit_rows.append(
                [
                    window,
                    metric,
                    _METRIC_UNIT_LABEL[metric],
                    fit.slope,
                    fit.r2,
                    fit.n_pairs,
                    fit.residual_std,
                    "yes" if fit.accepted else "no",
                    fit.reason or "",
                ]
            )

    budget_columns = [
        Column(key="window", label="Window", kind="str"),
        Column(key="million_new_tokens_per_window", label="Million new tokens per full window", kind="float"),
        Column(key="note", label="Note", kind="str"),
    ]
    budget_rows: list[list] = []
    for window in WINDOW_NAMES:
        value = stats.window_budget_million_tokens.get(window)
        new_tokens_fit = stats.fits.get(window, {}).get("new_tokens")
        if value is not None and new_tokens_fit is not None:
            note = f"from a {new_tokens_fit.n_pairs}-pair fit, R²={new_tokens_fit.r2:.2f}"
        elif new_tokens_fit is not None and new_tokens_fit.reason:
            note = new_tokens_fit.reason
        else:
            note = "new-tokens fit not accepted"
        budget_rows.append([window, value, note])

    burn_columns = [
        Column(key="window", label="Window", kind="str"),
        Column(key="hours", label="Burn window (hours)", kind="float"),
        Column(key="new_tokens", label="New tokens in window", kind="tokens"),
        Column(key="pct_of_window", label="Share of full window", kind="pct"),
        Column(key="note", label="Note", kind="str"),
    ]
    burn_rows = [
        [
            stats.recent_burn_window,
            stats.recent_burn_hours,
            stats.recent_new_tokens,
            stats.recent_burn_pct,
            stats.recent_burn_reason or "",
        ]
    ]

    notes: list[str] = list(ASSUMPTIONS)
    notes.extend(th.describe())
    for window in WINDOW_NAMES:
        dropped_r = stats.pairs_dropped_reset.get(window, 0)
        dropped_n = stats.pairs_dropped_negative.get(window, 0)
        if dropped_r or dropped_n:
            label = WINDOW_LABELS.get(window, window)
            notes.append(
                f"{label[:1].upper()}{label[1:]} limit: {dropped_r} pair(s) of readings dropped "
                f"for spanning a reset, {dropped_n} for a fall in the share used."
            )
    if stats.pricing_version:
        notes.append(f"Costs use prices from pricing.toml, version {stats.pricing_version}.")

    return Section(
        key="elasticity",
        title="Usage-window elasticity",
        tables=[
            Table(
                name="elasticity_fit",
                title="Elasticity fit by window and metric",
                columns=fit_columns,
                rows=fit_rows,
            ),
            Table(
                name="elasticity_budget",
                title="Derived window budget",
                columns=budget_columns,
                rows=budget_rows,
            ),
            Table(
                name="elasticity_recent_burn",
                title="Recent burn rate",
                columns=burn_columns,
                rows=burn_rows,
            ),
        ],
        notes=notes,
    )


# -- rules ----------------------------------------------------------------


def _section(report: ReportModel, section_key: str) -> Section | None:
    for section in report.sections:
        if section.key == section_key:
            return section
    return None


def _table(report: ReportModel, section_key: str, table_name: str) -> Table | None:
    section = _section(report, section_key)
    if section is None:
        return None
    for table in section.tables:
        if table.name == table_name:
            return table
    return None


def _col_index(table: Table, column_key: str) -> int | None:
    for idx, column in enumerate(table.columns):
        if column.key == column_key:
            return idx
    return None


def _row(table: Table, row_key) -> list | None:
    for row in table.rows:
        if row and row[0] == row_key:
            return row
    return None


def _cell(report: ReportModel, section_key: str, table_name: str, row_key, column_key: str):
    """Look up one cell, returning ``None`` when the section/table/row/
    column doesn't exist -- mirrors ``waste.py``'s own ``_cell`` contract
    (reimplemented locally here too, see the module docstring, to avoid
    a circular import)."""
    table = _table(report, section_key, table_name)
    if table is None:
        return None
    row = _row(table, row_key)
    if row is None:
        return None
    idx = _col_index(table, column_key)
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _evidence(label: str, value, section_key: str, table_name: str, row_key) -> tuple:
    return (label, value, f"{section_key}.{table_name}", row_key)


def _biggest_lever(report: ReportModel, exclude_id: str) -> Recommendation | None:
    """Pick whichever *other* already-computed recommendation on this
    report looks like the biggest lever, so ``_rule_window_budget`` can
    point at it by id without repeating a number that recommendation
    already owns. Ranked by severity (``action`` > ``advice`` > ``info``)
    then, as a tie-break within the same severity, by the largest single
    numeric value in its own evidence tuples -- a simple, order-
    independent proxy for "which lever moves the most spend" that
    doesn't require this module to understand every other rule's own
    units. Returns ``None`` when ``report.recommendations`` is empty (or
    holds only this rule's own prior output).
    """
    severity_rank = {"action": 2, "advice": 1, "info": 0}
    best: Recommendation | None = None
    best_key: tuple[int, float] | None = None
    for rec in report.recommendations:
        if rec.id == exclude_id:
            continue
        top_value = 0.0
        for entry in rec.evidence:
            if len(entry) >= 2 and isinstance(entry[1], (int, float)):
                top_value = max(top_value, float(entry[1]))
        key = (severity_rank.get(rec.severity, 0), top_value)
        if best_key is None or key > best_key:
            best_key = key
            best = rec
    return best


def _rule_window_budget(report: ReportModel, th: ElasticityThresholds) -> list[Recommendation]:
    """Fires only when ``report.meta.billing_mode == "subscription"``
    (a USD-billed account has no usage window to budget against) and
    ``th.weekly_window``'s own new-tokens fit was accepted (read back
    off the ``elasticity`` section's own ``elasticity_budget`` table,
    the same evidence contract every rule in this codebase follows).
    States the derived tokens-per-window budget and the last-24h burn
    share, and names whichever other recommendation already on the
    report looks like the biggest lever (see :func:`_biggest_lever`), by
    its title only -- never repeating a number that recommendation
    already cites.

    Assumes ``th.weekly_window`` is the same window kind
    :func:`compute_elasticity` was run with when it produced the
    ``elasticity`` section this rule reads back -- the same "same
    thresholds object flows to both build_section and the rule" contract
    ``waste.py``'s own rule/thresholds pairing already relies on.
    """
    if report.meta.billing_mode != "subscription":
        return []

    window = th.weekly_window
    budget = _cell(report, "elasticity", "elasticity_budget", window, "million_new_tokens_per_window")
    if not isinstance(budget, (int, float)):
        return []

    burn_table = _table(report, "elasticity", "elasticity_recent_burn")
    burn_pct = None
    burn_hours = None
    if burn_table is not None:
        row = _row(burn_table, window)
        if row is not None:
            pct_idx = _col_index(burn_table, "pct_of_window")
            hours_idx = _col_index(burn_table, "hours")
            if pct_idx is not None and pct_idx < len(row) and isinstance(row[pct_idx], (int, float)):
                burn_pct = row[pct_idx]
            if hours_idx is not None and hours_idx < len(row) and isinstance(row[hours_idx], (int, float)):
                burn_hours = row[hours_idx]

    evidence = [
        _evidence("Million new tokens per full window", budget, "elasticity", "elasticity_budget", window),
    ]
    if burn_pct is not None:
        evidence.append(
            _evidence("Share of window burned in the last 24h", burn_pct, "elasticity", "elasticity_recent_burn", window)
        )

    limit = WINDOW_LABELS.get(window, window)
    burn_clause = ""
    if burn_pct is not None and burn_hours is not None:
        burn_clause = f" The last {burn_hours:.0f} hours used {burn_pct:.1f}% of it."

    lever = _biggest_lever(report, exclude_id="window-budget")
    lever_clause = ""
    if lever is not None:
        lever_clause = f' To make it last longer, start with "{lever.title}" in your recommendations.'

    action = (
        f"A full {limit} limit is worth about {budget:,.1f} million new tokens (input, cache writes and "
        f"output).{burn_clause}{lever_clause}"
    )

    return [
        Recommendation(
            id="window-budget",
            severity="info",
            category="data",
            archetypes=(),
            title=f"How many tokens your {limit} limit holds",
            action=action,
            lever=None,
            evidence=evidence,
            why=(
                "Worked out from your logged usage-limit readings and the tokens your sessions used between "
                "them, so it is specific to how you work."
            ),
        )
    ]


#: One rule per this module's own convention (see the module docstring
#: and ``waste.py``'s identical one): a tuple of ``(report, th) ->
#: list[Recommendation]`` callables; ``recommend.recommend()`` runs it
#: last, against the report with the other recommendations filled in.
RULES: tuple = (_rule_window_budget,)


__all__ = [
    "ASSUMPTIONS",
    "DEFAULT_WEEKLY_WINDOW",
    "ElasticityThresholds",
    "FitResult",
    "ElasticityStats",
    "compute_elasticity",
    "express_in_window",
    "build_section",
    "RULES",
]
