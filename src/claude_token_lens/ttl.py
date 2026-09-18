"""TTL break-even simulation: plan Appendix A4's prompt-cache-TTL
counterfactual, replayed per transcript and rolled up per agent type.

This is WP4 of the project plan (see the plan's "TTL break-even" section
and Appendix A4 for the simulation pseudocode, and Appendix A5's
``ttl-switch`` row for the recommendation thresholds). It depends only on
``model.py`` (frozen contract) and ``pricing.py`` (WP2's ``price_turn``,
whose default and simulation paths this module relies on agreeing
exactly — see ``observed``).

Three layers:

- :func:`simulate` and :func:`observed` replay one transcript's priced
  turns under a single fixed TTL policy (or the real, as-billed split)
  and return a :class:`SimResult`.
- :func:`dominant_ttl` and :func:`fidelity` self-check how well a single
  fixed-policy simulation predicts a transcript's actual observed cost,
  so the report can flag agent types where the model's assumptions don't
  hold.
- :class:`TtlStats` accumulates all of the above across many transcripts,
  keyed by agent type ("top-level" for the main conversation, otherwise
  ``TranscriptMeta.agent_type``), and :func:`build_section` renders the
  roll-up as a report :class:`~claude_token_lens.model.Section`.

A second, follow-up layer answers "was the cache I paid for actually
used" rather than only "which fixed policy is cheaper": wasted-write
tracking, 1h-premium-vs-5m-expiry-loss bucketing, a break-even share
computed straight from the resolved rate card, a near-miss histogram at
the two TTL boundaries, a TTL-addressable-vs-content-addressable split
of re-cache turns, and a standalone :func:`cache_economy` summary. These
are additional ``TtlTypeStats`` fields and additional tables in the same
``ttl`` :class:`~claude_token_lens.model.Section` — the three-layer
shape above (simulate/observed, dominant_ttl/fidelity, TtlStats/
build_section) is unchanged.

"Priced turns" throughout this module means ``Turn.turn_index > 0``:
``parse.py``'s ``_finalize_turn`` only assigns a positive, 1-based
``turn_index`` to a turn that is both non-synthetic and carried a
``usage`` block, so a zero ``turn_index`` already encodes exactly
"synthetic or usage-less" (see ``model.py``'s ``Turn.turn_index``
docstring and ``test_synthetic.py``). Every function here filters on
that instead of re-deriving the same skip condition from token totals.

WP3 (RE-CACHE) is a parallel work package: it is what actually populates
``Turn.recache_signature``. This module only reads that field — it never
sets it, and every function here works correctly (degrading to the
plain gap-based branch of Appendix A4) on turns where it is still the
``model.py`` default of ``None``, i.e. before WP3 lands or for turns
WP3's detector didn't flag.

Privacy: every field this module produces (``SimResult``, ``TtlTypeStats``,
the report ``Table`` rows) is a number, a percentage, a short fixed
vocabulary string ("5m"/"1h"/"mixed"/"none", a recommendation sentence,
a lever name), or an agent-type string already carried on
``TranscriptMeta.agent_type`` elsewhere in the codebase — never message
text, a path, or a command.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .model import Column, CostBreakdown, Section, Table, TranscriptResult, Turn
from .pricing import ModelRates, ResolvedRates, price_turn

#: Plan Appendix A4's TTL simulation assumptions, printed verbatim in the
#: report's "## Assumptions" block (``ReportMeta.assumptions``), plus the
#: gap definition sentence (matches ``parse.py``'s own docstring: gap_s is
#: measured request-start to request-start).
ASSUMPTIONS: list[str] = [
    "content is TTL-invariant",
    "cacheable prefix C_i = cache_read_i + cache_creation_i",
    "a hit refreshes TTL so survival depends only on gap_s",
    "prefix-invalidated turns keep their observed split under every policy (no double counting)",
    "reads are priced at the flat cache_read rate",
    "compaction shrink clamps write at 0",
    "gap is measured from the start of one request to the start of the next",
]

#: TTL policy identifiers, in the seconds form ``price_turn``'s
#: ``write_split`` accepts directly (plan Appendix A4: T in {300, 3600}).
POLICY_5M = 300
POLICY_1H = 3600

#: A side's share of cache-write tokens must be at least this fraction of
#: the total for ``dominant_ttl`` to call it dominant (plan: "≥ 90% one
#: side = that side").
_DOMINANCE_THRESHOLD = 0.9

#: ``build_section`` flags an agent type whose fidelity exceeds this
#: percentage (plan: "flag agent types above 10%").
_FIDELITY_WARN_PCT = 10.0

#: A switch recommendation requires the candidate policy's cost to be
#: below this fraction of the observed cost (plan: "> 5%" cheaper) *and*
#: the absolute saving to exceed ``_SWITCH_USD_THRESHOLD`` (plan: "> 1.00
#: USD") — both conditions, each independently blocking.
_SWITCH_PCT_THRESHOLD = 0.95
_SWITCH_USD_THRESHOLD = 1.00

#: Gap buckets for the per-agent gap-distribution table: (key, label,
#: lower bound inclusive, upper bound exclusive) in seconds.
_GAP_BUCKETS: tuple[tuple[str, str, float, float], ...] = (
    ("lt_1m", "<1m", 0.0, 60.0),
    ("1_5m", "1-5m", 60.0, 300.0),
    ("5_15m", "5-15m", 300.0, 900.0),
    ("15_60m", "15-60m", 900.0, 3600.0),
    ("gt_60m", ">60m", 3600.0, float("inf")),
)

#: What ``price_turn``/rate resolution accepts: an already-resolved rate
#: (bare or wrapped), or ``None`` for an unresolved model (prices at
#: zero, ``model_known=False`` — see ``pricing.price_turn``).
RatesArg = ModelRates | ResolvedRates | None

#: Fix item 2: a turn-by-turn rate resolver, ``pricing.Pricing.resolve_model``'s
#: own signature — ``model id -> ResolvedRates | None``. Every function in
#: this module that prices more than one turn (``simulate``, ``observed``,
#: ``cache_economy``, ``TtlStats.add``) accepts either this or a single
#: already-resolved :data:`RatesArg` (see :func:`_as_lookup`): a mixed-model
#: transcript (subagents can run a different model from their parent, and a
#: user can switch models mid top-level session) must price each turn at
#: its own observed model's rate, not the first turn's.
RatesLookup = Callable[[str], RatesArg]


def _as_lookup(rates: "RatesArg | RatesLookup") -> RatesLookup:
    """Normalise a caller's ``rates`` argument to a per-turn lookup.

    A single already-resolved rate (bare ``ModelRates``, ``ResolvedRates``,
    or ``None``) is wrapped in a lookup that ignores the model id and
    always returns it — the pre-item-2 behaviour every existing call site
    (and most of this module's own tests, which build one Sonnet-5 rate
    and reuse it) still relies on. A callable is assumed to already be a
    per-model lookup (``Pricing.resolve_model``, or a test double with the
    same shape) and is returned unchanged.
    """
    if callable(rates):
        return rates
    return lambda _model_id: rates

#: Near-miss histogram boundaries (seconds), asymmetric on purpose: a
#: gap that lands at or just under the TTL boundary still hit the cache
#: (``[240, 300]``/``[3540, 3600]``, inclusive both ends — the boundary
#: value itself is still a hit, see ``simulate``'s ``gap_s <= policy_s``
#: branch), while a gap that lands just over it forced a full rewrite
#: (``(300, 360]``/``(3600, 3660]``, exclusive of the boundary).
_NEAR_5M_HIT = (240.0, 300.0)
_NEAR_5M_MISS = (300.0, 360.0)
_NEAR_1H_HIT = (3540.0, 3600.0)
_NEAR_1H_MISS = (3600.0, 3660.0)

#: WP3 (RE-CACHE)'s own minimal re-cache detection rule, mirrored here
#: only as a fallback for item 5 (TTL-addressable share) when
#: ``Turn.recache_signature`` is unset — see ``_recache_classification``.
#: These must stay in sync with WP3's real detector; this module never
#: sets ``recache_signature`` itself, only reads it when present.
_RECACHE_CTX_FLOOR = 20_000
_RECACHE_CR_RATIO = 0.2
_RECACHE_FULL_EXPIRY_CR = 2_000


def _priced_turns(turns: list[Turn]) -> list[Turn]:
    """The subset of ``turns`` that are actually priced: synthetic and
    usage-less turns (``turn_index == 0``) are skipped in every function
    in this module. See the module docstring."""
    return [t for t in turns if t.turn_index > 0]


def _bucket_for(gap_s: float) -> str:
    for key, _label, lo, hi in _GAP_BUCKETS:
        if lo <= gap_s < hi:
            return key
    return _GAP_BUCKETS[-1][0]


def _percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile over ``values`` (0 <= pct <= 100).
    ``None`` for an empty input."""
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[idx]


def _write_cost(turn: Turn, rates: RatesArg, ttl_seconds: int, tokens: int) -> float:
    """The cache-write dollar cost of writing ``tokens`` tokens at a
    single TTL bucket (``POLICY_5M`` or ``POLICY_1H``) for ``turn``, via
    ``price_turn``'s simulation path (``write_split={ttl_seconds:
    tokens}``, ``read_tokens=0`` — this call prices only the write side).
    Used throughout the cache-utilisation metrics below wherever a
    hypothetical or bucketed write needs its own dollar figure, so every
    one of them is still "priced via price_turn" rather than a
    hand-rolled rate lookup."""
    if tokens <= 0:
        return 0.0
    return price_turn(turn, rates, write_split={ttl_seconds: tokens}, read_tokens=0).cache_write_cost


def _cache_tokens_at_input_rate(
    turn: Turn, rates: RatesArg, real: CostBreakdown | None = None
) -> float:
    """What ``turn``'s cache tokens (``cache_read_tokens +
    cache_creation_tokens``) would have cost if there were no caching at
    all and they were priced as plain input tokens instead — the
    "uncached-equivalent" figure ``cache_economy`` and the cache-economy
    table need.

    Computed via two ``price_turn`` calls rather than reading
    ``rates.input`` directly: a synthetic turn that folds the cache
    tokens into ``input_tokens`` (zeroing the cache fields) is priced,
    and the turn's own real ``input_cost`` is subtracted back out. Both
    calls see the same ``ctx`` (only the token *bucket* changes, not the
    prefix size) and the same geo, so any long-context or geo multiplier
    ``price_turn`` applies is identical on both sides and cancels
    exactly in the subtraction, leaving only the cache tokens' cost at
    the (possibly multiplied) input rate — still entirely price_turn's
    own rate resolution, never re-derived here.
    """
    if real is None:
        real = price_turn(turn, rates)
    synthetic = dataclasses.replace(
        turn,
        input_tokens=turn.input_tokens + turn.cache_read_tokens + turn.cache_creation_tokens,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        cc_5m=0,
        cc_1h=0,
    )
    inflated = price_turn(synthetic, rates)
    return inflated.input_cost - real.input_cost


def _recache_classification(t: Turn) -> str | None:
    """"full-expiry" | "prefix-invalidated" | ``None`` for one priced
    turn, for item 5 (TTL-addressable share).

    Prefers ``t.recache_signature`` (WP3's own classification) when set.
    Falls back, turn by turn, to WP3's documented minimal rule when it
    is ``None``: a re-cache turn is ``turn_index > 1`` (never the
    transcript's first write) with ``ctx > 20_000`` and ``cache_read <
    0.2 * ctx`` (most of a large prefix was NOT served from cache — the
    signature of *some* re-cache event, TTL or content), sub-classified
    as "full-expiry" when ``cache_read < 2_000`` (essentially nothing
    survived: a clean TTL expiry) or otherwise "prefix-invalidated" (a
    partial read: the cache content itself changed upstream of some
    point, which no TTL policy can prevent). Falling back per turn
    rather than only when every turn in a transcript is unsigned
    produces the same result in every case this worktree can observe
    (WP3 hasn't merged, so ``recache_signature`` is uniformly ``None``
    across the whole corpus today) while staying correct turn-by-turn
    once WP3 lands partially or its detector skips some turns.
    """
    if t.recache_signature is not None:
        return t.recache_signature
    if t.turn_index > 1 and t.ctx > _RECACHE_CTX_FLOOR and t.cache_read_tokens < _RECACHE_CR_RATIO * t.ctx:
        return "full-expiry" if t.cache_read_tokens < _RECACHE_FULL_EXPIRY_CR else "prefix-invalidated"
    return None


@dataclass(slots=True)
class SimResult:
    """One simulated (or observed) TTL-policy run over a transcript's
    priced turns: total cost and the write/read token totals that
    produced it."""

    cost: float = 0.0
    write_tokens: int = 0
    read_tokens: int = 0
    #: Turns whose branch had to fall back to the observed split because
    #: ``gap_s`` was unknown (plan A4: "unknown gap -> carry observed").
    unsimulatable: int = 0
    #: Count of priced turns this result was computed over.
    turns: int = 0
    #: Fix item 2: priced turns whose model the rates lookup could not
    #: resolve — priced at zero (``price_turn``'s ``model_known=False``
    #: path) and counted here rather than silently vanishing from cost.
    unpriced_turns: int = 0


def simulate(turns: list[Turn], rates: "RatesArg | RatesLookup", policy_s: int) -> SimResult:
    """Replay ``turns`` under a single fixed TTL policy (plan Appendix
    A4): ``policy_s`` is ``POLICY_5M`` (300) or ``POLICY_1H`` (3600)
    seconds, though any positive int is accepted as a hypothetical
    policy.

    ``rates`` (fix item 2) is either a single already-resolved rate
    (applied to every turn regardless of its own model — the pre-item-2
    behaviour) or a per-turn :data:`RatesLookup` callable, resolved via
    :func:`_as_lookup`; a mixed-model transcript is priced correctly
    either way turn by turn. A turn whose model the lookup cannot
    resolve prices at zero and is counted in ``unpriced_turns``.

    Per priced turn ``t`` (``C = t.cache_read_tokens +
    t.cache_creation_tokens``):

    - The first priced turn always writes the full prefix: ``read=0``,
      ``write=C``.
    - A turn classified "prefix-invalidated" (via
      ``_recache_classification``: WP3's own ``recache_signature`` when
      set, else its minimal fallback rule — see that function's
      docstring) keeps its *observed* split under every policy — a
      prefix invalidation is a cache-content event, not a TTL-expiry
      event, so simulating a different TTL cannot have prevented it (no
      double counting the two causes). The fallback means this branch is
      reachable even in a pipeline that never called
      ``recache.apply``/``recache.detect`` on the transcript first.
    - A turn with unknown ``gap_s`` also carries its observed split
      (nothing to simulate) and is counted in ``unsimulatable``.
    - Otherwise: ``gap_s <= policy_s`` means the previous write's TTL
      entry was still alive, so this turn reads whatever of the previous
      prefix survives (``read = min(C, prev_C)``) and only writes the
      remainder (``write = max(0, C - read)`` — clamped at 0 so a
      shrinking prefix, e.g. after compaction, is never priced as a
      negative write). ``gap_s > policy_s`` means the entry expired:
      full rewrite, ``read=0``, ``write=C``.

    Each turn is priced via ``price_turn(t, rates, write_split={policy_s:
    write}, read_tokens=read)`` — the simulation path documented in
    ``pricing.price_turn``, using the turn's own observed geo/model/ctx
    for everything else.
    """
    lookup = _as_lookup(rates)
    priced = _priced_turns(turns)
    prev_c = 0
    cost = 0.0
    write_tokens = 0
    read_tokens = 0
    unsimulatable = 0
    unpriced_turns = 0
    for i, t in enumerate(priced):
        c = t.cache_read_tokens + t.cache_creation_tokens
        if i == 0:
            read, write = 0, c
        elif _recache_classification(t) == "prefix-invalidated":
            read, write = t.cache_read_tokens, t.cache_creation_tokens
        elif t.gap_s is None:
            read, write = t.cache_read_tokens, t.cache_creation_tokens
            unsimulatable += 1
        elif t.gap_s <= policy_s:
            read = min(c, prev_c)
            write = max(0, c - read)
        else:
            read, write = 0, c

        turn_rates = lookup(t.model)
        if turn_rates is None:
            unpriced_turns += 1
        breakdown = price_turn(t, turn_rates, write_split={policy_s: write}, read_tokens=read)
        cost += breakdown.total
        write_tokens += write
        read_tokens += read
        prev_c = c

    return SimResult(
        cost=cost,
        write_tokens=write_tokens,
        read_tokens=read_tokens,
        unsimulatable=unsimulatable,
        turns=len(priced),
        unpriced_turns=unpriced_turns,
    )


def observed(turns: list[Turn], rates: "RatesArg | RatesLookup") -> SimResult:
    """The real, as-billed cost: each priced turn through ``price_turn``'s
    default path (no ``write_split``/``read_tokens`` override, so it
    prices ``turn.cc_5m``/``turn.cc_1h``/``turn.cache_read_tokens`` as
    observed). ``pricing.price_turn``'s own contract guarantees this
    equals calling it with the observed split passed explicitly, so this
    is also "the simulation path with the observed split" the plan
    describes — there is only one code path either way.

    ``rates`` accepts the same single-rate-or-per-turn-lookup shape as
    :func:`simulate` (fix item 2, see :func:`_as_lookup`).
    """
    lookup = _as_lookup(rates)
    priced = _priced_turns(turns)
    cost = 0.0
    write_tokens = 0
    read_tokens = 0
    unpriced_turns = 0
    for t in priced:
        turn_rates = lookup(t.model)
        if turn_rates is None:
            unpriced_turns += 1
        breakdown = price_turn(t, turn_rates)
        cost += breakdown.total
        write_tokens += t.cc_5m + t.cc_1h
        read_tokens += t.cache_read_tokens
    return SimResult(
        cost=cost,
        write_tokens=write_tokens,
        read_tokens=read_tokens,
        turns=len(priced),
        unpriced_turns=unpriced_turns,
    )


def dominant_ttl(turns: list[Turn]) -> str:
    """Which TTL a transcript's priced turns actually observed, by
    cache-write token volume: "5m" or "1h" when one side is at least
    ``_DOMINANCE_THRESHOLD`` (90%) of ``cc_5m + cc_1h``, "mixed" when
    neither reaches that share, "none" when no cache-write tokens were
    observed at all (nothing to be dominant over)."""
    priced = _priced_turns(turns)
    total_5m = sum(t.cc_5m for t in priced)
    total_1h = sum(t.cc_1h for t in priced)
    total = total_5m + total_1h
    if total == 0:
        return "none"
    if total_5m / total >= _DOMINANCE_THRESHOLD:
        return "5m"
    if total_1h / total >= _DOMINANCE_THRESHOLD:
        return "1h"
    return "mixed"


def fidelity(turns: list[Turn], rates: "RatesArg | RatesLookup") -> float | None:
    """Self-check: simulate at the transcript's own ``dominant_ttl`` and
    compare to ``observed``. ``None`` when there is nothing meaningful to
    compare — ``dominant_ttl`` is "mixed"/"none", or observed cost is
    zero (would divide by zero, and a zero-cost transcript has nothing at
    stake either way).

    ``rates`` accepts the same single-rate-or-per-turn-lookup shape as
    :func:`simulate`/:func:`observed` (fix item 2) and is forwarded to
    both unchanged."""
    dominant = dominant_ttl(turns)
    if dominant in ("mixed", "none"):
        return None
    obs = observed(turns, rates)
    if obs.cost == 0:
        return None
    policy_s = POLICY_5M if dominant == "5m" else POLICY_1H
    sim = simulate(turns, rates, policy_s)
    return abs(sim.cost - obs.cost) / obs.cost


def cache_economy(turns: list[Turn], rates: "RatesArg | RatesLookup") -> dict:
    """Standalone cache-economy summary over an arbitrary turns list —
    item 6 of the cache-utilisation follow-up. Not tied to
    :class:`TtlStats`, so report assembly (WP10) can compute the same
    "overall" line independently of the per-agent-type roll-up;
    ``TtlStats.add`` also calls this and folds its numbers into each
    agent type's running totals, so the two are guaranteed to agree.

    Every priced turn's actual write/read cost comes from
    ``price_turn``'s default (observed) path; ``uncached_equivalent_usd``
    is what the same cache tokens would have cost priced as plain input
    (see ``_cache_tokens_at_input_rate``) — i.e. what the transcript
    would have cost with no caching at all. ``net_saving_usd`` is that
    minus what was actually paid for writes and reads, and ``cache_roi``
    is the saving as a multiple of what caching itself cost
    (``net_saving_usd / write_usd``, ``0.0`` when nothing was ever
    written).

    ``rates`` accepts the same single-rate-or-per-turn-lookup shape as
    :func:`simulate`/:func:`observed` (fix item 2, see :func:`_as_lookup`)
    — each turn is priced at its own resolved model's rate.

    Returns a dict with keys ``tokens_written``, ``tokens_read``,
    ``write_usd``, ``read_usd``, ``uncached_equivalent_usd``,
    ``net_saving_usd``, ``cache_roi``, ``unpriced_turns``.
    """
    lookup = _as_lookup(rates)
    priced = _priced_turns(turns)
    tokens_written = 0
    tokens_read = 0
    write_usd = 0.0
    read_usd = 0.0
    uncached_equivalent_usd = 0.0
    unpriced_turns = 0
    for t in priced:
        turn_rates = lookup(t.model)
        if turn_rates is None:
            unpriced_turns += 1
        breakdown = price_turn(t, turn_rates)
        tokens_written += t.cc_5m + t.cc_1h
        tokens_read += t.cache_read_tokens
        write_usd += breakdown.cache_write_cost
        read_usd += breakdown.cache_read_cost
        uncached_equivalent_usd += _cache_tokens_at_input_rate(t, turn_rates, breakdown)
    net_saving_usd = uncached_equivalent_usd - (write_usd + read_usd)
    cache_roi = net_saving_usd / write_usd if write_usd > 0 else 0.0
    return {
        "tokens_written": tokens_written,
        "tokens_read": tokens_read,
        "write_usd": write_usd,
        "read_usd": read_usd,
        "uncached_equivalent_usd": uncached_equivalent_usd,
        "net_saving_usd": net_saving_usd,
        "cache_roi": cache_roi,
        "unpriced_turns": unpriced_turns,
    }


@dataclass(slots=True)
class _RawAccumulator:
    """Mutable running totals for one agent-type key, across every
    transcript ``TtlStats.add`` has folded in. Not part of the public
    API — ``TtlStats.by_key`` derives the immutable, ready-to-render
    :class:`TtlTypeStats` from this."""

    key: str
    spawns: int = 0
    priced_turns: int = 0
    cc_5m_tokens: int = 0
    cc_1h_tokens: int = 0
    gaps_over_5m: int = 0
    gaps_over_1h: int = 0
    gap_values: list[float] = field(default_factory=list)
    gap_buckets: dict[str, int] = field(
        default_factory=lambda: {key: 0 for key, _label, _lo, _hi in _GAP_BUCKETS}
    )
    cost_observed: float = 0.0
    cost_all_5m: float = 0.0
    cost_all_1h: float = 0.0
    unsimulatable: int = 0
    #: Fix item 2: priced turns whose model the rates lookup could not
    #: resolve, taken from ``observed()``'s own count (see
    #: ``TtlStats.add``) rather than re-tallied by hand.
    unpriced_turns: int = 0
    #: Token-weighted running sum/weight for the mean fidelity fraction
    #: across every transcript of this agent type (see ``TtlStats.add``).
    fidelity_weighted_sum: float = 0.0
    fidelity_weight: int = 0
    #: Fix item 2: per-model total token volume (input + cache_creation +
    #: cache_read + output) seen for this agent type, so ``by_key`` can
    #: resolve ``premium_ratio`` against the *dominant* model's rates
    #: rather than merely "whichever turn happened to price last" (the
    #: pre-item-2 ``sample_rates`` behaviour).
    model_tokens: dict[str, int] = field(default_factory=dict)
    #: The rates-lookup callable last passed to ``TtlStats.add`` — reused
    #: by ``by_key`` to resolve the dominant model found above. A single
    #: ``TtlStats`` is expected to be fed from one consistent lookup
    #: (e.g. ``Pricing.resolve_model``) across every ``add`` call.
    rates_lookup: "RatesLookup | None" = None

    # -- item 1: wasted writes -----------------------------------------
    waste_writes: int = 0
    waste_wasted_writes: int = 0
    waste_tokens_written: int = 0
    waste_tokens_wasted: int = 0
    waste_usd_wasted: float = 0.0
    waste_terminal_writes: int = 0
    waste_terminal_tokens: int = 0
    waste_terminal_usd: float = 0.0

    # -- item 2: 1h premium waste vs 5m expiry loss ---------------------
    premium_1h_not_needed_tokens: int = 0
    premium_1h_not_needed_usd: float = 0.0
    premium_1h_earned_tokens: int = 0
    premium_1h_earned_usd: float = 0.0
    premium_1h_expired_tokens: int = 0
    premium_1h_expired_usd: float = 0.0
    premium_5m_fine_tokens: int = 0
    premium_5m_loss_tokens: int = 0
    premium_5m_loss_usd: float = 0.0
    premium_5m_would_expire_tokens: int = 0

    # -- item 3: break-even share ---------------------------------------
    inwindow_weight: float = 0.0
    inwindow_total_weight: float = 0.0
    #: Σ_i W_i × (write_1h - write_5m) over *every* priced write (not
    #: gated on a gap at all) — the dollar premium paid across the whole
    #: transcript if every write had used a 1h TTL instead of 5m.
    premium_all_1h_usd: float = 0.0
    #: Σ over gaps in (300s, 3600s] of C_j × write_5m, where C_j is the
    #: prefix (cache_read + cache_creation) of the turn that follows the
    #: gap — the dollar cost of re-writing that whole prefix because a 5m
    #: TTL expired where a 1h one would have survived.
    expiry_loss_all_5m_usd: float = 0.0

    # -- item 4: near-miss histogram -------------------------------------
    near_5m_hit: int = 0
    near_5m_miss: int = 0
    near_5m_miss_tokens: int = 0
    near_5m_miss_usd: float = 0.0
    near_1h_hit: int = 0
    near_1h_miss: int = 0
    near_1h_miss_tokens: int = 0
    near_1h_miss_usd: float = 0.0

    # -- item 5: TTL-addressable share -----------------------------------
    addressable_full_expiry_tokens: int = 0
    addressable_full_expiry_usd: float = 0.0
    addressable_prefix_invalidated_tokens: int = 0
    addressable_prefix_invalidated_usd: float = 0.0

    # -- item 6: cache economy -------------------------------------------
    economy_tokens_written: int = 0
    economy_tokens_read: int = 0
    economy_write_usd: float = 0.0
    economy_read_usd: float = 0.0
    economy_uncached_equivalent_usd: float = 0.0


@dataclass(slots=True)
class TtlTypeStats:
    """Rolled-up TTL stats for one agent type (or ``"top-level"``) — the
    values behind one row of ``build_section``'s table."""

    key: str
    spawns: int
    priced_turns: int
    observed_5m_pct: float
    observed_1h_pct: float
    gaps_over_5m: int
    gaps_over_1h: int
    gap_p50_s: float | None
    gap_p90_s: float | None
    cost_observed: float
    cost_all_5m: float
    cost_all_1h: float
    unsimulatable: int
    #: Token-weighted mean fidelity across this type's transcripts, as a
    #: percentage. ``None`` when no transcript of this type had a
    #: computable fidelity (see ``fidelity``).
    fidelity_pct: float | None
    #: bucket key (see ``_GAP_BUCKETS``) -> count.
    gap_buckets: dict[str, int]
    #: Fix item 2: priced turns whose model the rates lookup could not
    #: resolve, priced at zero rather than silently vanishing from cost.
    unpriced_turns: int = 0

    # -- item 1: wasted writes -----------------------------------------
    waste_writes: int = 0
    waste_wasted_writes: int = 0
    waste_tokens_written: int = 0
    waste_tokens_wasted: int = 0
    waste_usd_wasted: float = 0.0
    waste_terminal_writes: int = 0
    waste_terminal_tokens: int = 0
    waste_terminal_usd: float = 0.0

    # -- item 2: 1h premium waste vs 5m expiry loss ---------------------
    premium_1h_not_needed_tokens: int = 0
    premium_1h_not_needed_usd: float = 0.0
    premium_1h_earned_tokens: int = 0
    premium_1h_earned_usd: float = 0.0
    premium_1h_expired_tokens: int = 0
    premium_1h_expired_usd: float = 0.0
    premium_5m_fine_tokens: int = 0
    premium_5m_loss_tokens: int = 0
    premium_5m_loss_usd: float = 0.0
    premium_5m_would_expire_tokens: int = 0

    # -- item 3: break-even share ---------------------------------------
    premium_ratio: float = 0.0
    in_window_share: float = 0.0
    #: Σ_i W_i × (write_1h - write_5m) over every write, in USD: the
    #: total premium paid across the transcript if every write had used
    #: a 1h TTL instead of 5m.
    premium_all_1h: float = 0.0
    #: Σ over in-window gaps (300s, 3600s] of C_j × write_5m, in USD: the
    #: total cost of re-writing the prefix each time a 5m TTL expired
    #: where a 1h one would have survived.
    expiry_loss_all_5m: float = 0.0
    #: ``premium_ratio`` scaled by the incremental-to-prefix token ratio
    #: (Σ W_i / Σ_{all gaps} C_j) — the break-even point for
    #: ``in_window_share`` to clear before 1h pays for itself.
    break_even_share: float = 0.0

    # -- item 4: near-miss histogram -------------------------------------
    near_5m_hit: int = 0
    near_5m_miss: int = 0
    near_5m_miss_tokens: int = 0
    near_5m_miss_usd: float = 0.0
    near_1h_hit: int = 0
    near_1h_miss: int = 0
    near_1h_miss_tokens: int = 0
    near_1h_miss_usd: float = 0.0

    # -- item 5: TTL-addressable share -----------------------------------
    addressable_full_expiry_tokens: int = 0
    addressable_full_expiry_usd: float = 0.0
    addressable_prefix_invalidated_tokens: int = 0
    addressable_prefix_invalidated_usd: float = 0.0

    # -- item 6: cache economy -------------------------------------------
    economy_tokens_written: int = 0
    economy_tokens_read: int = 0
    economy_write_usd: float = 0.0
    economy_read_usd: float = 0.0
    economy_uncached_equivalent_usd: float = 0.0

    @property
    def waste_share_pct(self) -> float:
        """Wasted tokens as a percentage of tokens written — excludes
        every transcript's terminal write (see the class-level note on
        ``waste_terminal_*``): ``0.0`` when nothing non-terminal was ever
        written."""
        if self.waste_tokens_written <= 0:
            return 0.0
        return 100.0 * self.waste_tokens_wasted / self.waste_tokens_written

    @property
    def margin(self) -> float:
        """``expiry_loss_all_5m`` minus ``premium_all_1h``, in USD —
        positive means running under a 5m TTL would have cost more (in
        re-written prefixes) than paying the 1h premium on every write,
        i.e. 1h pays."""
        return self.expiry_loss_all_5m - self.premium_all_1h

    @property
    def verdict(self) -> str:
        """One-word break-even verdict: "marginal" when ``margin`` is
        small either in relative terms (within 5% of the larger side) or
        in absolute terms (within $1.00) — either condition alone is
        enough to call it marginal. Otherwise "1h pays" or "5m pays"."""
        margin = self.margin
        larger_side = max(self.expiry_loss_all_5m, self.premium_all_1h)
        if abs(margin) < 0.05 * larger_side or abs(margin) < 1.00:
            return "marginal"
        return "1h pays" if margin > 0 else "5m pays"

    @property
    def net_saving_usd(self) -> float:
        """Item 6: what caching actually saved vs. pricing every cache
        token as plain input — ``uncached_equivalent_usd`` minus what was
        actually paid for writes and reads."""
        return self.economy_uncached_equivalent_usd - (self.economy_write_usd + self.economy_read_usd)

    @property
    def cache_roi(self) -> float:
        """Item 6: net saving as a multiple of what caching itself cost.
        ``0.0`` when nothing was ever written (avoids a division by
        zero; there is no ROI on a write that never happened)."""
        if self.economy_write_usd <= 0:
            return 0.0
        return self.net_saving_usd / self.economy_write_usd

    @property
    def best_policy(self) -> str:
        """Which fixed policy is cheaper overall: "5m" or "1h". Ties go
        to "5m" (the harness default)."""
        return "5m" if self.cost_all_5m <= self.cost_all_1h else "1h"

    @property
    def best_cost(self) -> float:
        return min(self.cost_all_5m, self.cost_all_1h)

    @property
    def delta_usd(self) -> float:
        """Observed cost minus the cheaper fixed policy's cost — positive
        means the best available policy would have cost less than what
        was actually billed."""
        return self.cost_observed - self.best_cost

    @property
    def delta_pct(self) -> float:
        if self.cost_observed <= 0:
            return 0.0
        return 100.0 * self.delta_usd / self.cost_observed

    @property
    def recommendation(self) -> str:
        """Plan Appendix A4's recommendation rule: switch only when the
        candidate policy is both > 5% cheaper AND saves > $1.00 — each
        threshold independently blocking. 1h is checked first (matching
        the plan's own pseudocode order), then 5m; otherwise "no material
        difference"."""
        if self.cost_observed <= 0:
            return "no material difference"
        for policy_cost, label in ((self.cost_all_1h, "1h"), (self.cost_all_5m, "5m")):
            saving = self.cost_observed - policy_cost
            if policy_cost < self.cost_observed * _SWITCH_PCT_THRESHOLD and saving > _SWITCH_USD_THRESHOLD:
                return f"switch to {label}"
        return "no material difference"

    @property
    def lever(self) -> str:
        """The concrete setting/frontmatter change the recommendation
        names (plan "TTL break-even" section)."""
        if self.key == "top-level":
            return "promptCacheTtl"
        return f"experimental.cacheTtl in {self.key}.md (or subagentPromptCacheTtl for all subagents)"


def _accumulate_waste(priced: list[Turn], lookup: RatesLookup, acc: _RawAccumulator) -> None:
    """Item 1: for every priced turn's cache-write portion(s), was the
    prefix it wrote ever actually read back before its TTL expired?

    A turn with both ``cc_5m`` and ``cc_1h`` nonzero (a mixed write) is
    two separate writes here, one per TTL bucket, each checked
    independently against the same prefix size ``C_i`` (see the module
    docstring's item 1). The transcript's last priced turn is always a
    "terminal write" — nothing can come after it to prove the prefix was
    reused, which says nothing about whether it *would* have been reused
    in a later session — so it is tallied separately and never counted
    towards ``waste_writes``/``waste_wasted_writes``.

    ``lookup`` (fix item 2) resolves each turn's own model, so a
    mixed-model transcript's write cost is never mispriced at another
    turn's rate.
    """
    n = len(priced)
    for i, t in enumerate(priced):
        c_i = t.cache_read_tokens + t.cache_creation_tokens
        is_terminal = i == n - 1
        turn_rates = lookup(t.model)
        for ttl_seconds, tokens in ((POLICY_5M, t.cc_5m), (POLICY_1H, t.cc_1h)):
            if tokens <= 0:
                continue
            cost = _write_cost(t, turn_rates, ttl_seconds, tokens)
            if is_terminal:
                acc.waste_terminal_writes += 1
                acc.waste_terminal_tokens += tokens
                acc.waste_terminal_usd += cost
                continue
            acc.waste_writes += 1
            acc.waste_tokens_written += tokens
            used = False
            for j in range(i + 1, n):
                gap = priced[j].gap_s
                if gap is None or gap > ttl_seconds:
                    # The chain from i to j is broken (or unknown, which
                    # is treated the same as broken — see the module
                    # docstring on unknown gaps): nothing past this point
                    # can still be the same cache entry written at i.
                    break
                if priced[j].cache_read_tokens >= c_i:
                    used = True
                    break
            if not used:
                acc.waste_wasted_writes += 1
                acc.waste_tokens_wasted += tokens
                acc.waste_usd_wasted += cost


class TtlStats:
    """Accumulates TTL break-even stats per agent type across many
    transcripts. Feed it with ``add(result, rates)`` for every top-level
    and subagent transcript in a corpus, session, or report window; read
    the roll-up via ``by_key()``.
    """

    def __init__(self) -> None:
        self._raw: dict[str, _RawAccumulator] = {}
        #: Every subagent-kind transcript's ``TranscriptMeta.mtime_ns``
        #: seen by ``add`` — ``build_section``'s ``window_start`` caveat
        #: note compares against ``min()`` of this list. Not keyed by
        #: agent type: the caveat is about a *window*, not a specific
        #: type.
        self._subagent_mtimes_ns: list[int] = []

    def add(self, result: TranscriptResult, rates_lookup: "RatesArg | RatesLookup") -> None:
        """Fold one transcript's turns into its agent type's running
        totals: "top-level" for a ``kind="top-level"`` transcript,
        otherwise ``result.meta.agent_type`` (falling back to "unknown"
        for a subagent/workflow-agent transcript with no recorded
        type).

        ``rates_lookup`` (fix item 2) is either a single already-resolved
        rate (the pre-item-2 behaviour, applied to every turn regardless
        of its own model) or a per-turn :data:`RatesLookup` callable —
        normalised via :func:`_as_lookup`. A mixed-model transcript
        (a model switch mid-session, or a subagent pinned to a different
        model from its parent) is priced turn by turn at its own
        resolved model's rate either way; a turn whose model the lookup
        can't resolve prices at zero and is counted in
        ``unpriced_turns``.
        """
        lookup = _as_lookup(rates_lookup)
        key = "top-level" if result.meta.kind == "top-level" else (result.meta.agent_type or "unknown")
        acc = self._raw.setdefault(key, _RawAccumulator(key=key))
        acc.spawns += 1
        acc.rates_lookup = lookup

        if result.meta.kind != "top-level":
            self._subagent_mtimes_ns.append(result.meta.mtime_ns)

        priced = _priced_turns(result.turns)
        acc.priced_turns += len(priced)
        n = len(priced)
        for i, t in enumerate(priced):
            turn_rates = lookup(t.model)
            if turn_rates is None:
                acc.unpriced_turns += 1
            acc.model_tokens[t.model] = acc.model_tokens.get(t.model, 0) + (
                t.input_tokens + t.cache_creation_tokens + t.cache_read_tokens + t.output_tokens
            )

            acc.cc_5m_tokens += t.cc_5m
            acc.cc_1h_tokens += t.cc_1h
            c_i = t.cache_read_tokens + t.cache_creation_tokens

            # Item 3: premium_all_1h, over *every* write regardless of
            # gap — W_i is this turn's own incremental write, priced at
            # both TTLs via _write_cost so geo/long-context multipliers
            # are honoured per turn rather than hand-rolled.
            w_i = t.cache_creation_tokens
            if w_i > 0:
                acc.premium_all_1h_usd += _write_cost(t, turn_rates, POLICY_1H, w_i) - _write_cost(
                    t, turn_rates, POLICY_5M, w_i
                )

            # i == 0 is the transcript's first priced turn: it has no
            # previous priced turn, so gap_s is always None there and
            # carries no gap-distribution information (see simulate's
            # i == 0 branch) — never counted as an "unknown gap" turn.
            if i > 0 and t.gap_s is not None:
                gap = t.gap_s
                acc.gap_values.append(gap)
                acc.gap_buckets[_bucket_for(gap)] += 1
                if gap > POLICY_5M:
                    acc.gaps_over_5m += 1
                if gap > POLICY_1H:
                    acc.gaps_over_1h += 1

                # Item 3: break-even in-window share, weighted by the
                # prefix size C of the turn that *follows* this gap.
                acc.inwindow_total_weight += c_i
                if POLICY_5M < gap <= POLICY_1H:
                    acc.inwindow_weight += c_i
                    # expiry_loss_all_5m: the dollar cost of re-writing
                    # this turn's whole prefix (C_j) at the 5m write
                    # rate, since a 5m TTL would have expired across
                    # this gap where a 1h one would have survived.
                    acc.expiry_loss_all_5m_usd += _write_cost(t, turn_rates, POLICY_5M, c_i)

                # Item 4: near-miss histogram at both TTL boundaries.
                if _NEAR_5M_HIT[0] <= gap <= _NEAR_5M_HIT[1]:
                    acc.near_5m_hit += 1
                elif _NEAR_5M_MISS[0] < gap <= _NEAR_5M_MISS[1]:
                    acc.near_5m_miss += 1
                    acc.near_5m_miss_tokens += t.cache_creation_tokens
                    acc.near_5m_miss_usd += price_turn(t, turn_rates).cache_write_cost
                if _NEAR_1H_HIT[0] <= gap <= _NEAR_1H_HIT[1]:
                    acc.near_1h_hit += 1
                elif _NEAR_1H_MISS[0] < gap <= _NEAR_1H_MISS[1]:
                    acc.near_1h_miss += 1
                    acc.near_1h_miss_tokens += t.cache_creation_tokens
                    acc.near_1h_miss_usd += price_turn(t, turn_rates).cache_write_cost

            # Item 2: 1h premium waste vs 5m expiry loss, bucketed by the
            # gap to the *next* priced turn (None when t is the last one
            # — treated the same as "gap > 3600", i.e. the entry expired
            # either way with nothing to show for the premium/the loss).
            # The *next* turn may run a different model in a mixed
            # transcript, so its own share of a cost is priced at its
            # own resolved rate, not this turn's.
            next_turn = priced[i + 1] if i + 1 < n else None
            next_gap = next_turn.gap_s if next_turn is not None else None
            next_rates = lookup(next_turn.model) if next_turn is not None else None
            if t.cc_1h > 0:
                if next_gap is not None and next_gap <= POLICY_5M:
                    premium = _write_cost(t, turn_rates, POLICY_1H, t.cc_1h) - _write_cost(
                        t, turn_rates, POLICY_5M, t.cc_1h
                    )
                    acc.premium_1h_not_needed_tokens += t.cc_1h
                    acc.premium_1h_not_needed_usd += premium
                elif next_gap is not None and next_gap <= POLICY_1H:
                    acc.premium_1h_earned_tokens += t.cc_1h
                    acc.premium_1h_earned_usd += _write_cost(t, turn_rates, POLICY_5M, t.cc_1h)
                else:
                    premium = _write_cost(t, turn_rates, POLICY_1H, t.cc_1h) - _write_cost(
                        t, turn_rates, POLICY_5M, t.cc_1h
                    )
                    acc.premium_1h_expired_tokens += t.cc_1h
                    acc.premium_1h_expired_usd += premium
            if t.cc_5m > 0:
                if next_gap is not None and next_gap <= POLICY_5M:
                    acc.premium_5m_fine_tokens += t.cc_5m
                elif next_gap is not None and next_gap <= POLICY_1H:
                    acc.premium_5m_loss_tokens += t.cc_5m
                    acc.premium_5m_loss_usd += price_turn(next_turn, next_rates).cache_write_cost
                else:
                    acc.premium_5m_would_expire_tokens += t.cc_5m

            # Item 5: TTL-addressable (full-expiry) vs content-addressable
            # (prefix-invalidated) re-cache tokens/USD.
            classification = _recache_classification(t)
            if classification in ("full-expiry", "prefix-invalidated"):
                write_cost = price_turn(t, turn_rates).cache_write_cost
                if classification == "full-expiry":
                    acc.addressable_full_expiry_tokens += t.cache_creation_tokens
                    acc.addressable_full_expiry_usd += write_cost
                else:
                    acc.addressable_prefix_invalidated_tokens += t.cache_creation_tokens
                    acc.addressable_prefix_invalidated_usd += write_cost

        # Item 1: wasted writes (needs the whole priced list at once, to
        # look ahead across possibly many turns — see _accumulate_waste).
        _accumulate_waste(priced, lookup, acc)

        obs = observed(result.turns, lookup)
        sim_5m = simulate(result.turns, lookup, POLICY_5M)
        sim_1h = simulate(result.turns, lookup, POLICY_1H)
        acc.cost_observed += obs.cost
        acc.cost_all_5m += sim_5m.cost
        acc.cost_all_1h += sim_1h.cost
        # The gap_s is None branch fires identically regardless of which
        # policy_s is being simulated (it's checked before the gap
        # comparison), so sim_5m and sim_1h always agree on this count;
        # either would do.
        acc.unsimulatable += sim_5m.unsimulatable

        fid = fidelity(result.turns, lookup)
        if fid is not None:
            weight = obs.write_tokens + obs.read_tokens
            if weight > 0:
                acc.fidelity_weighted_sum += fid * weight
                acc.fidelity_weight += weight

        # Item 6: cache economy, delegated to the standalone function so
        # the two are guaranteed to agree (see cache_economy's docstring).
        economy = cache_economy(result.turns, lookup)
        acc.economy_tokens_written += economy["tokens_written"]
        acc.economy_tokens_read += economy["tokens_read"]
        acc.economy_write_usd += economy["write_usd"]
        acc.economy_read_usd += economy["read_usd"]
        acc.economy_uncached_equivalent_usd += economy["uncached_equivalent_usd"]

    def by_key(self) -> dict[str, TtlTypeStats]:
        """The current roll-up, one :class:`TtlTypeStats` per agent type
        key that has had at least one transcript ``add``-ed."""
        out: dict[str, TtlTypeStats] = {}
        for key, acc in self._raw.items():
            total_cc = acc.cc_5m_tokens + acc.cc_1h_tokens
            observed_5m_pct = 100.0 * acc.cc_5m_tokens / total_cc if total_cc else 0.0
            observed_1h_pct = 100.0 * acc.cc_1h_tokens / total_cc if total_cc else 0.0
            fidelity_pct = (
                100.0 * acc.fidelity_weighted_sum / acc.fidelity_weight
                if acc.fidelity_weight > 0
                else None
            )

            # Item 3 (refined by fix item 2): premium_ratio from the
            # resolved rate card, never hard-coded — resolved against
            # this agent type's *dominant* model (the model with the
            # most total token volume, not merely whichever turn's rate
            # happened to be stored last), so a mixed-model agent type's
            # premium_ratio reflects the model that actually dominates
            # its cost rather than an arbitrary sample.
            rates: ModelRates | None = None
            if acc.model_tokens and acc.rates_lookup is not None:
                dominant_model = max(acc.model_tokens, key=acc.model_tokens.get)
                resolved = acc.rates_lookup(dominant_model)
                rates = resolved.rates if isinstance(resolved, ResolvedRates) else resolved
            premium_ratio = (
                (rates.cache_write_1h - rates.cache_write_5m) / rates.cache_write_5m
                if rates is not None and rates.cache_write_5m
                else 0.0
            )
            in_window_share = (
                acc.inwindow_weight / acc.inwindow_total_weight
                if acc.inwindow_total_weight > 0
                else 0.0
            )
            total_w = acc.cc_5m_tokens + acc.cc_1h_tokens  # Σ W_i, every write
            break_even_share = (
                premium_ratio * (total_w / acc.inwindow_total_weight)
                if acc.inwindow_total_weight > 0
                else 0.0
            )

            out[key] = TtlTypeStats(
                key=key,
                spawns=acc.spawns,
                priced_turns=acc.priced_turns,
                observed_5m_pct=observed_5m_pct,
                observed_1h_pct=observed_1h_pct,
                gaps_over_5m=acc.gaps_over_5m,
                gaps_over_1h=acc.gaps_over_1h,
                gap_p50_s=_percentile(acc.gap_values, 50),
                gap_p90_s=_percentile(acc.gap_values, 90),
                cost_observed=acc.cost_observed,
                cost_all_5m=acc.cost_all_5m,
                cost_all_1h=acc.cost_all_1h,
                unsimulatable=acc.unsimulatable,
                unpriced_turns=acc.unpriced_turns,
                fidelity_pct=fidelity_pct,
                gap_buckets=dict(acc.gap_buckets),
                waste_writes=acc.waste_writes,
                waste_wasted_writes=acc.waste_wasted_writes,
                waste_tokens_written=acc.waste_tokens_written,
                waste_tokens_wasted=acc.waste_tokens_wasted,
                waste_usd_wasted=acc.waste_usd_wasted,
                waste_terminal_writes=acc.waste_terminal_writes,
                waste_terminal_tokens=acc.waste_terminal_tokens,
                waste_terminal_usd=acc.waste_terminal_usd,
                premium_1h_not_needed_tokens=acc.premium_1h_not_needed_tokens,
                premium_1h_not_needed_usd=acc.premium_1h_not_needed_usd,
                premium_1h_earned_tokens=acc.premium_1h_earned_tokens,
                premium_1h_earned_usd=acc.premium_1h_earned_usd,
                premium_1h_expired_tokens=acc.premium_1h_expired_tokens,
                premium_1h_expired_usd=acc.premium_1h_expired_usd,
                premium_5m_fine_tokens=acc.premium_5m_fine_tokens,
                premium_5m_loss_tokens=acc.premium_5m_loss_tokens,
                premium_5m_loss_usd=acc.premium_5m_loss_usd,
                premium_5m_would_expire_tokens=acc.premium_5m_would_expire_tokens,
                premium_ratio=premium_ratio,
                in_window_share=in_window_share,
                premium_all_1h=acc.premium_all_1h_usd,
                expiry_loss_all_5m=acc.expiry_loss_all_5m_usd,
                break_even_share=break_even_share,
                near_5m_hit=acc.near_5m_hit,
                near_5m_miss=acc.near_5m_miss,
                near_5m_miss_tokens=acc.near_5m_miss_tokens,
                near_5m_miss_usd=acc.near_5m_miss_usd,
                near_1h_hit=acc.near_1h_hit,
                near_1h_miss=acc.near_1h_miss,
                near_1h_miss_tokens=acc.near_1h_miss_tokens,
                near_1h_miss_usd=acc.near_1h_miss_usd,
                addressable_full_expiry_tokens=acc.addressable_full_expiry_tokens,
                addressable_full_expiry_usd=acc.addressable_full_expiry_usd,
                addressable_prefix_invalidated_tokens=acc.addressable_prefix_invalidated_tokens,
                addressable_prefix_invalidated_usd=acc.addressable_prefix_invalidated_usd,
                economy_tokens_written=acc.economy_tokens_written,
                economy_tokens_read=acc.economy_tokens_read,
                economy_write_usd=acc.economy_write_usd,
                economy_read_usd=acc.economy_read_usd,
                economy_uncached_equivalent_usd=acc.economy_uncached_equivalent_usd,
            )
        return out

    @property
    def subagent_mtimes_ns(self) -> list[int]:
        """Every subagent-kind transcript's ``TranscriptMeta.mtime_ns``
        seen so far, for ``build_section``'s ``window_start`` discovery
        caveat."""
        return list(self._subagent_mtimes_ns)


#: Quoted verbatim from the plan's Risk 1 (subscription users are not
#: billed in USD): the documented reason a 1h subagent recommendation is
#: moot in subscription mode.
_SUBSCRIPTION_1H_IGNORED_NOTE = (
    "a 1h cacheTtl is ignored while on usage credits"
)


def build_section(
    stats: TtlStats, billing_mode: str = "api", window_start: datetime | None = None
) -> Section:
    """Render a :class:`TtlStats` roll-up as the report's "Cache TTL
    break-even" section: the per-agent-type table, a per-agent gap
    distribution table, the six cache-utilisation-monitoring tables
    (wasted writes, 1h premium waste vs 5m expiry loss, break-even
    share, near-miss histogram, TTL-addressable share, cache economy),
    and notes (a fidelity warning for any agent type above 10%, plus —
    in ``billing_mode="subscription"`` — the subscription-suppression
    note, plus — when ``window_start`` is given and a subagent
    transcript predates it — the subagent-window discovery caveat).

    ``billing_mode="subscription"`` suppresses every subagent (non
    "top-level") row's switch recommendation, per plan Appendix A5's
    ``ttl-switch`` row ("suppressed for subagents in subscription mode"):
    the harness ignores a 1h ``cacheTtl`` while on usage credits, so
    recommending a subagent TTL change there would promise an effect the
    harness cannot deliver. The top-level row is never suppressed — the
    main conversation's TTL is a real, user-set lever
    (``promptCacheTtl``) in both billing modes.

    ``window_start`` is the report window's start timestamp (a caller
    building a date-bounded corpus, e.g. "last 30 days", knows this;
    ``TtlStats`` itself doesn't). ``discovery.find_subagents`` has no
    independent date filter of its own — it returns every subagent
    transcript under a session, however old, once that session's own
    (possibly much more recent) mtime lets the session itself into the
    window. When any subagent transcript's own ``TranscriptMeta.mtime_ns``
    predates ``window_start``, a note flags that a long-lived or resumed
    top-level session can drag arbitrarily old subagent spawns into an
    otherwise-recent window — the per-agent-type numbers above may mix
    regimes as a result. Silent (no note at all) when ``window_start`` is
    ``None``, so a caller that doesn't track a window pays nothing for
    this check.
    """
    by_key = stats.by_key()

    columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="spawns", label="Spawns", kind="int"),
        Column(key="priced_turns", label="Priced turns", kind="int"),
        Column(key="observed_5m_pct", label="Observed 5m share", kind="pct"),
        Column(key="observed_1h_pct", label="Observed 1h share", kind="pct"),
        Column(key="gaps_over_5m", label="Gaps > 5 min", kind="int"),
        Column(key="gaps_over_1h", label="Gaps > 60 min", kind="int"),
        Column(key="gap_p50_s", label="Gap p50", kind="secs"),
        Column(key="gap_p90_s", label="Gap p90", kind="secs"),
        Column(key="cost_observed", label="Cost (observed)", kind="money"),
        Column(key="cost_all_5m", label="Cost (all-5m)", kind="money"),
        Column(key="cost_all_1h", label="Cost (all-1h)", kind="money"),
        Column(key="best_policy", label="Best policy", kind="str"),
        Column(key="delta_usd", label="Delta vs observed", kind="money"),
        Column(key="delta_pct", label="Delta vs observed", kind="pct"),
        Column(key="fidelity_pct", label="Fidelity", kind="pct"),
        Column(key="unsimulatable", label="Unsimulatable turns", kind="int"),
        Column(key="unpriced_turns", label="Unpriced turns (unknown model)", kind="int"),
        Column(key="recommendation", label="Recommendation", kind="str"),
        Column(key="lever", label="Lever", kind="str"),
    ]

    rows: list[list] = []
    fidelity_warnings: list[str] = []
    for key in sorted(by_key):
        row_stats = by_key[key]
        recommendation = row_stats.recommendation
        if (
            billing_mode == "subscription"
            and key != "top-level"
            and recommendation != "no material difference"
        ):
            recommendation = "no material difference (suppressed: subscription billing)"
        rows.append(
            [
                row_stats.key,
                row_stats.spawns,
                row_stats.priced_turns,
                row_stats.observed_5m_pct,
                row_stats.observed_1h_pct,
                row_stats.gaps_over_5m,
                row_stats.gaps_over_1h,
                row_stats.gap_p50_s,
                row_stats.gap_p90_s,
                row_stats.cost_observed,
                row_stats.cost_all_5m,
                row_stats.cost_all_1h,
                row_stats.best_policy,
                row_stats.delta_usd,
                row_stats.delta_pct,
                row_stats.fidelity_pct,
                row_stats.unsimulatable,
                row_stats.unpriced_turns,
                recommendation,
                row_stats.lever,
            ]
        )
        if row_stats.fidelity_pct is not None and row_stats.fidelity_pct > _FIDELITY_WARN_PCT:
            fidelity_warnings.append(key)

    table = Table(
        name="ttl_by_agent_type",
        title="TTL break-even by agent type",
        columns=columns,
        rows=rows,
    )

    gap_columns = [Column(key="agent_type", label="Agent type", kind="str")] + [
        Column(key=bkey, label=label, kind="int") for bkey, label, _lo, _hi in _GAP_BUCKETS
    ]
    gap_rows = []
    for key in sorted(by_key):
        buckets = by_key[key].gap_buckets
        gap_rows.append([key] + [buckets.get(bkey, 0) for bkey, _label, _lo, _hi in _GAP_BUCKETS])
    gap_table = Table(
        name="ttl_gap_distribution",
        title="Inter-turn gap distribution by agent type",
        columns=gap_columns,
        rows=gap_rows,
    )

    # -- Item 1: wasted writes -------------------------------------------
    waste_columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="writes", label="Writes", kind="int"),
        Column(key="wasted_writes", label="Wasted writes", kind="int"),
        Column(key="tokens_written", label="Tokens written", kind="tokens"),
        Column(key="tokens_wasted", label="Tokens wasted", kind="tokens"),
        Column(key="share", label="Waste share", kind="pct"),
        Column(key="usd_wasted", label="USD wasted", kind="money"),
        Column(key="terminal_writes", label="Terminal writes", kind="int"),
    ]
    waste_rows = [
        [
            key,
            by_key[key].waste_writes,
            by_key[key].waste_wasted_writes,
            by_key[key].waste_tokens_written,
            by_key[key].waste_tokens_wasted,
            by_key[key].waste_share_pct,
            by_key[key].waste_usd_wasted,
            by_key[key].waste_terminal_writes,
        ]
        for key in sorted(by_key)
    ]
    waste_table = Table(
        name="ttl_wasted_writes",
        title="Cache write utilisation: wasted writes",
        columns=waste_columns,
        rows=waste_rows,
        notes=[
            "A write is \"used\" when a later turn in the same transcript reads back at"
            " least as much as the prefix it wrote, before that write's TTL (5m or 1h;"
            " a mixed write counts as two) ever lapsed. Terminal writes (a transcript's"
            " last turn) are unavoidable and excluded from the waste share."
        ],
    )

    # -- Item 2: 1h premium waste vs 5m expiry loss ----------------------
    premium_columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="h1_not_needed_tokens", label="1h: premium paid, not needed (tokens)", kind="tokens"),
        Column(key="h1_not_needed_usd", label="1h: premium paid, not needed (USD)", kind="money"),
        Column(key="h1_earned_tokens", label="1h: premium earned (tokens)", kind="tokens"),
        Column(key="h1_earned_usd", label="1h: premium earned (USD saved)", kind="money"),
        Column(key="h1_expired_tokens", label="1h: expired anyway (tokens)", kind="tokens"),
        Column(key="h1_expired_usd", label="1h: expired anyway (USD)", kind="money"),
        Column(key="m5_fine_tokens", label="5m: fine (tokens)", kind="tokens"),
        Column(key="m5_loss_tokens", label="5m: expiry loss (tokens)", kind="tokens"),
        Column(key="m5_loss_usd", label="5m: expiry loss (USD)", kind="money"),
        Column(key="m5_would_expire_tokens", label="5m: would expire under 1h too (tokens)", kind="tokens"),
    ]
    premium_rows = [
        [
            key,
            by_key[key].premium_1h_not_needed_tokens,
            by_key[key].premium_1h_not_needed_usd,
            by_key[key].premium_1h_earned_tokens,
            by_key[key].premium_1h_earned_usd,
            by_key[key].premium_1h_expired_tokens,
            by_key[key].premium_1h_expired_usd,
            by_key[key].premium_5m_fine_tokens,
            by_key[key].premium_5m_loss_tokens,
            by_key[key].premium_5m_loss_usd,
            by_key[key].premium_5m_would_expire_tokens,
        ]
        for key in sorted(by_key)
    ]
    premium_table = Table(
        name="ttl_premium_waste",
        title="1h premium waste vs 5m expiry loss",
        columns=premium_columns,
        rows=premium_rows,
    )

    # -- Item 3: break-even share -----------------------------------------
    break_even_columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="premium_all_1h", label="Premium if all 1h", kind="money"),
        Column(key="expiry_loss_all_5m", label="Expiry loss if all 5m", kind="money"),
        Column(key="margin", label="Margin", kind="money"),
        Column(key="in_window_share", label="Prefix-weighted 5-60min share", kind="pct"),
        Column(key="break_even_share", label="Break-even share", kind="pct"),
        Column(key="verdict", label="Verdict", kind="str"),
    ]
    break_even_rows = [
        [
            key,
            by_key[key].premium_all_1h,
            by_key[key].expiry_loss_all_5m,
            by_key[key].margin,
            by_key[key].in_window_share * 100.0,
            by_key[key].break_even_share * 100.0,
            by_key[key].verdict,
        ]
        for key in sorted(by_key)
    ]
    break_even_table = Table(
        name="ttl_break_even_share",
        title="Break-even share: 1h vs 5m",
        columns=break_even_columns,
        rows=break_even_rows,
        notes=[
            "the 1h premium is paid on incremental writes; an expiry re-writes the whole"
            " prefix, so the break-even share is the premium ratio scaled by the"
            " incremental-to-prefix ratio",
            "fix item 2: premium_ratio is resolved against this agent type's dominant"
            " model (the model with the most total token volume observed for it), not"
            " an arbitrary sample turn — a mixed-model agent type's break-even share"
            " reflects the model that actually dominates its cost.",
        ],
    )

    # -- Item 4: near-miss histogram ---------------------------------------
    near_miss_columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="near_5m_hit", label="5m near-miss, hit (240-300s)", kind="int"),
        Column(key="near_5m_miss", label="5m near-miss, missed (300-360s)", kind="int"),
        Column(key="near_5m_miss_tokens", label="5m just-missed rewrite tokens", kind="tokens"),
        Column(key="near_5m_miss_usd", label="5m just-missed rewrite USD", kind="money"),
        Column(key="near_1h_hit", label="1h near-miss, hit (3540-3600s)", kind="int"),
        Column(key="near_1h_miss", label="1h near-miss, missed (3600-3660s)", kind="int"),
        Column(key="near_1h_miss_tokens", label="1h just-missed rewrite tokens", kind="tokens"),
        Column(key="near_1h_miss_usd", label="1h just-missed rewrite USD", kind="money"),
    ]
    near_miss_rows = [
        [
            key,
            by_key[key].near_5m_hit,
            by_key[key].near_5m_miss,
            by_key[key].near_5m_miss_tokens,
            by_key[key].near_5m_miss_usd,
            by_key[key].near_1h_hit,
            by_key[key].near_1h_miss,
            by_key[key].near_1h_miss_tokens,
            by_key[key].near_1h_miss_usd,
        ]
        for key in sorted(by_key)
    ]
    near_miss_table = Table(
        name="ttl_near_miss",
        title="Near-miss histogram at the TTL boundaries",
        columns=near_miss_columns,
        rows=near_miss_rows,
        notes=["The statusline countdown targets these."],
    )

    # -- Item 5: TTL-addressable share -------------------------------------
    addressable_columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="full_expiry_tokens", label="Full-expiry tokens (TTL-addressable)", kind="tokens"),
        Column(key="full_expiry_usd", label="Full-expiry USD", kind="money"),
        Column(key="full_expiry_share", label="Full-expiry share", kind="pct"),
        Column(
            key="prefix_invalidated_tokens",
            label="Prefix-invalidated tokens (content-addressable)",
            kind="tokens",
        ),
        Column(key="prefix_invalidated_usd", label="Prefix-invalidated USD", kind="money"),
        Column(key="prefix_invalidated_share", label="Prefix-invalidated share", kind="pct"),
    ]
    addressable_rows = []
    for key in sorted(by_key):
        row_stats = by_key[key]
        total_tokens = (
            row_stats.addressable_full_expiry_tokens + row_stats.addressable_prefix_invalidated_tokens
        )
        full_share = 100.0 * row_stats.addressable_full_expiry_tokens / total_tokens if total_tokens else 0.0
        prefix_share = (
            100.0 * row_stats.addressable_prefix_invalidated_tokens / total_tokens if total_tokens else 0.0
        )
        addressable_rows.append(
            [
                key,
                row_stats.addressable_full_expiry_tokens,
                row_stats.addressable_full_expiry_usd,
                full_share,
                row_stats.addressable_prefix_invalidated_tokens,
                row_stats.addressable_prefix_invalidated_usd,
                prefix_share,
            ]
        )
    addressable_table = Table(
        name="ttl_addressable_share",
        title="TTL-addressable vs content-addressable re-cache",
        columns=addressable_columns,
        rows=addressable_rows,
        notes=[
            "Full-expiry re-cache is TTL-addressable: a longer TTL can prevent it."
            " Prefix-invalidated re-cache is content-addressable: the cached content"
            " itself changed, so no TTL policy can help. Uses Turn.recache_signature"
            " when WP3 has set it, falling back turn-by-turn to WP3's own minimal rule"
            " (turn_index > 1, ctx > 20k, cache_read < 0.2*ctx, full-expiry when"
            " cache_read < 2,000) when it hasn't."
        ],
    )

    # -- Item 6: cache economy ----------------------------------------------
    economy_columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="tokens_written", label="Tokens written", kind="tokens"),
        Column(key="tokens_read", label="Tokens read", kind="tokens"),
        Column(key="write_usd", label="Write USD", kind="money"),
        Column(key="read_usd", label="Read USD", kind="money"),
        Column(key="uncached_equivalent_usd", label="Uncached-equivalent USD", kind="money"),
        Column(key="net_saving_usd", label="Net saving USD", kind="money"),
        Column(key="cache_roi", label="Cache ROI", kind="float"),
    ]
    economy_rows = [
        [
            key,
            by_key[key].economy_tokens_written,
            by_key[key].economy_tokens_read,
            by_key[key].economy_write_usd,
            by_key[key].economy_read_usd,
            by_key[key].economy_uncached_equivalent_usd,
            by_key[key].net_saving_usd,
            by_key[key].cache_roi,
        ]
        for key in sorted(by_key)
    ]
    if by_key:
        overall_tokens_written = sum(s.economy_tokens_written for s in by_key.values())
        overall_tokens_read = sum(s.economy_tokens_read for s in by_key.values())
        overall_write_usd = sum(s.economy_write_usd for s in by_key.values())
        overall_read_usd = sum(s.economy_read_usd for s in by_key.values())
        overall_uncached_equivalent_usd = sum(s.economy_uncached_equivalent_usd for s in by_key.values())
        overall_net_saving_usd = overall_uncached_equivalent_usd - (overall_write_usd + overall_read_usd)
        overall_cache_roi = overall_net_saving_usd / overall_write_usd if overall_write_usd > 0 else 0.0
        economy_rows.append(
            [
                "overall",
                overall_tokens_written,
                overall_tokens_read,
                overall_write_usd,
                overall_read_usd,
                overall_uncached_equivalent_usd,
                overall_net_saving_usd,
                overall_cache_roi,
            ]
        )
    economy_table = Table(
        name="ttl_cache_economy",
        title="Cache economy per agent type",
        columns=economy_columns,
        rows=economy_rows,
        notes=[
            "Uncached-equivalent USD prices every cache_read and cache_creation token"
            " at the model's plain input rate — what the transcript would have cost"
            " with no caching at all. Cache ROI = net saving USD / write USD."
        ],
    )

    notes: list[str] = []
    if fidelity_warnings:
        notes.append(
            "Simulation fidelity exceeds 10% for: " + ", ".join(fidelity_warnings) + "."
        )
    if billing_mode == "subscription":
        notes.append(
            "Subagent TTL switch recommendations are suppressed in subscription mode: "
            + _SUBSCRIPTION_1H_IGNORED_NOTE
            + "."
        )
    if window_start is not None:
        stale = False
        for mtime_ns in stats.subagent_mtimes_ns:
            mtime = datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=timezone.utc)
            if mtime < window_start:
                stale = True
                break
        if stale:
            notes.append(
                "Subagent transcripts are pulled into a window by their parent"
                " top-level session's mtime, not their own: find_subagents applies no"
                " independent date filter, so a long-lived or resumed session can drag"
                " much older subagent spawns into an otherwise-recent window. At least"
                " one subagent transcript folded into this report predates the report"
                " window's start."
            )

    return Section(
        key="ttl",
        title="Cache TTL break-even",
        tables=[
            table,
            gap_table,
            waste_table,
            premium_table,
            break_even_table,
            near_miss_table,
            addressable_table,
            economy_table,
        ],
        notes=notes,
    )


__all__ = [
    "ASSUMPTIONS",
    "POLICY_5M",
    "POLICY_1H",
    "SimResult",
    "TtlTypeStats",
    "TtlStats",
    "simulate",
    "observed",
    "dominant_ttl",
    "fidelity",
    "cache_economy",
    "build_section",
]
