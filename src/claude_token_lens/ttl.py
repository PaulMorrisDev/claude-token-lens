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

from dataclasses import dataclass, field

from .model import Column, Section, Table, TranscriptResult, Turn
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


def simulate(turns: list[Turn], rates: RatesArg, policy_s: int) -> SimResult:
    """Replay ``turns`` under a single fixed TTL policy (plan Appendix
    A4): ``policy_s`` is ``POLICY_5M`` (300) or ``POLICY_1H`` (3600)
    seconds, though any positive int is accepted as a hypothetical
    policy.

    Per priced turn ``t`` (``C = t.cache_read_tokens +
    t.cache_creation_tokens``):

    - The first priced turn always writes the full prefix: ``read=0``,
      ``write=C``.
    - A turn WP3 flagged ``recache_signature == "prefix-invalidated"``
      keeps its *observed* split under every policy — a prefix
      invalidation is a cache-content event, not a TTL-expiry event, so
      simulating a different TTL cannot have prevented it (no double
      counting the two causes).
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
    priced = _priced_turns(turns)
    prev_c = 0
    cost = 0.0
    write_tokens = 0
    read_tokens = 0
    unsimulatable = 0
    for i, t in enumerate(priced):
        c = t.cache_read_tokens + t.cache_creation_tokens
        if i == 0:
            read, write = 0, c
        elif t.recache_signature == "prefix-invalidated":
            read, write = t.cache_read_tokens, t.cache_creation_tokens
        elif t.gap_s is None:
            read, write = t.cache_read_tokens, t.cache_creation_tokens
            unsimulatable += 1
        elif t.gap_s <= policy_s:
            read = min(c, prev_c)
            write = max(0, c - read)
        else:
            read, write = 0, c

        breakdown = price_turn(t, rates, write_split={policy_s: write}, read_tokens=read)
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
    )


def observed(turns: list[Turn], rates: RatesArg) -> SimResult:
    """The real, as-billed cost: each priced turn through ``price_turn``'s
    default path (no ``write_split``/``read_tokens`` override, so it
    prices ``turn.cc_5m``/``turn.cc_1h``/``turn.cache_read_tokens`` as
    observed). ``pricing.price_turn``'s own contract guarantees this
    equals calling it with the observed split passed explicitly, so this
    is also "the simulation path with the observed split" the plan
    describes — there is only one code path either way.
    """
    priced = _priced_turns(turns)
    cost = 0.0
    write_tokens = 0
    read_tokens = 0
    for t in priced:
        breakdown = price_turn(t, rates)
        cost += breakdown.total
        write_tokens += t.cc_5m + t.cc_1h
        read_tokens += t.cache_read_tokens
    return SimResult(cost=cost, write_tokens=write_tokens, read_tokens=read_tokens, turns=len(priced))


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


def fidelity(turns: list[Turn], rates: RatesArg) -> float | None:
    """Self-check: simulate at the transcript's own ``dominant_ttl`` and
    compare to ``observed``. ``None`` when there is nothing meaningful to
    compare — ``dominant_ttl`` is "mixed"/"none", or observed cost is
    zero (would divide by zero, and a zero-cost transcript has nothing at
    stake either way)."""
    dominant = dominant_ttl(turns)
    if dominant in ("mixed", "none"):
        return None
    obs = observed(turns, rates)
    if obs.cost == 0:
        return None
    policy_s = POLICY_5M if dominant == "5m" else POLICY_1H
    sim = simulate(turns, rates, policy_s)
    return abs(sim.cost - obs.cost) / obs.cost


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
    #: Token-weighted running sum/weight for the mean fidelity fraction
    #: across every transcript of this agent type (see ``TtlStats.add``).
    fidelity_weighted_sum: float = 0.0
    fidelity_weight: int = 0


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


class TtlStats:
    """Accumulates TTL break-even stats per agent type across many
    transcripts. Feed it with ``add(result, rates)`` for every top-level
    and subagent transcript in a corpus, session, or report window; read
    the roll-up via ``by_key()``.
    """

    def __init__(self) -> None:
        self._raw: dict[str, _RawAccumulator] = {}

    def add(self, result: TranscriptResult, rates: RatesArg) -> None:
        """Fold one transcript's turns into its agent type's running
        totals: "top-level" for a ``kind="top-level"`` transcript,
        otherwise ``result.meta.agent_type`` (falling back to "unknown"
        for a subagent/workflow-agent transcript with no recorded
        type).

        ``rates`` is the single resolved rate used for every turn in
        this transcript (the same argument ``simulate``/``observed``
        take) — callers with a mixed-model transcript should resolve per
        the transcript's dominant model before calling this.
        """
        key = "top-level" if result.meta.kind == "top-level" else (result.meta.agent_type or "unknown")
        acc = self._raw.setdefault(key, _RawAccumulator(key=key))
        acc.spawns += 1

        priced = _priced_turns(result.turns)
        acc.priced_turns += len(priced)
        for i, t in enumerate(priced):
            acc.cc_5m_tokens += t.cc_5m
            acc.cc_1h_tokens += t.cc_1h
            # i == 0 is the transcript's first priced turn: it has no
            # previous priced turn, so gap_s is always None there and
            # carries no gap-distribution information (see simulate's
            # i == 0 branch) — never counted as an "unknown gap" turn.
            if i > 0 and t.gap_s is not None:
                acc.gap_values.append(t.gap_s)
                acc.gap_buckets[_bucket_for(t.gap_s)] += 1
                if t.gap_s > POLICY_5M:
                    acc.gaps_over_5m += 1
                if t.gap_s > POLICY_1H:
                    acc.gaps_over_1h += 1

        obs = observed(result.turns, rates)
        sim_5m = simulate(result.turns, rates, POLICY_5M)
        sim_1h = simulate(result.turns, rates, POLICY_1H)
        acc.cost_observed += obs.cost
        acc.cost_all_5m += sim_5m.cost
        acc.cost_all_1h += sim_1h.cost
        # The gap_s is None branch fires identically regardless of which
        # policy_s is being simulated (it's checked before the gap
        # comparison), so sim_5m and sim_1h always agree on this count;
        # either would do.
        acc.unsimulatable += sim_5m.unsimulatable

        fid = fidelity(result.turns, rates)
        if fid is not None:
            weight = obs.write_tokens + obs.read_tokens
            if weight > 0:
                acc.fidelity_weighted_sum += fid * weight
                acc.fidelity_weight += weight

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
                fidelity_pct=fidelity_pct,
                gap_buckets=dict(acc.gap_buckets),
            )
        return out


#: Quoted verbatim from the plan's Risk 1 (subscription users are not
#: billed in USD): the documented reason a 1h subagent recommendation is
#: moot in subscription mode.
_SUBSCRIPTION_1H_IGNORED_NOTE = (
    "a 1h cacheTtl is ignored while on usage credits"
)


def build_section(stats: TtlStats, billing_mode: str = "api") -> Section:
    """Render a :class:`TtlStats` roll-up as the report's "Cache TTL
    break-even" section: the per-agent-type table, a per-agent gap
    distribution table, and notes (a fidelity warning for any agent type
    above 10%, plus — in ``billing_mode="subscription"`` — the
    subscription-suppression note).

    ``billing_mode="subscription"`` suppresses every subagent (non
    "top-level") row's switch recommendation, per plan Appendix A5's
    ``ttl-switch`` row ("suppressed for subagents in subscription mode"):
    the harness ignores a 1h ``cacheTtl`` while on usage credits, so
    recommending a subagent TTL change there would promise an effect the
    harness cannot deliver. The top-level row is never suppressed — the
    main conversation's TTL is a real, user-set lever
    (``promptCacheTtl``) in both billing modes.
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

    return Section(key="ttl", title="Cache TTL break-even", tables=[table, gap_table], notes=notes)


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
    "build_section",
]
