"""``autoCompactWindow`` sweep: replay every top-level transcript under a
range of candidate auto-compaction windows and estimate the total cost
under each, so a report can recommend the window that saves the most.

This module is new (not part of the frozen work-package plan ``ttl.py``/
``compaction.py``/``recache.py`` implement); it follows their shape and
conventions closely rather than inventing new ones. It depends only on
``model.py`` (frozen contract), ``pricing.py`` (``price_turn``),
``recache.py`` (``RecacheThresholds``, reused unmodified for the
corpus-wide rediscovery-allowance sample) and ``compaction.py``'s public
``compaction_records_for_transcript``/``CompactionRecord`` (never its
private ``_correlate_compactions_to_turn_index``/``_MAX_JOIN_DELTA_S`` --
this module's own :func:`_real_compaction_turn_indices` re-implements the
same event-to-turn forward-merge locally, at this module's own
``CompactionSimThresholds.max_join_delta_s`` gate, rather than reaching
into ``compaction.py``'s private symbols).

**Why a compaction costs and saves money** (the model this module
simulates): Claude Code auto-compacts a session once its context nears
``autoCompactWindow`` tokens (observed in config snapshots, e.g.
``300000``; the model's own context window is typically 1,000,000 for a
"[1m]"-aliased model or 200,000 otherwise -- see
``context_budget.py``'s own assumed-window constants). It keeps a
reserve below the window, so a 300,000 window summarises near 267,000;
this module measures that reserve from real auto compactions in sessions
whose configured window is known. Each compaction:

- **Costs** the summary request (not logged in the transcript: the
  context read once more, with the summary as its output) and the reply
  after it, which re-caches its whole context: the part of the session's
  starting context that stays cached (system prompt, tools) is read, the
  rest (CLAUDE.md, skills listing, the summary) is written again.
- **Saves** money on every later turn, which now carries the session's
  starting context plus the summary instead of the whole conversation --
  priced at ``cache_read`` (or ``cache_write`` on a turn that itself
  re-caches).

The session's starting context (its first priced turn's context, a
median of about 80,000 tokens on a real corpus) never goes away: a
summary replaces the conversation, not the system prompt, tools and
CLAUDE.md in front of it. Summary size barely tracks conversation size,
so a simulated summary is this corpus's own median ``postTokens``, not a
ratio of the context.

:func:`simulate_compaction_windows` replays every transcript's priced
turns, in order, against each of :data:`CANDIDATE_WINDOWS`: whenever the
running (possibly already-shrunk) context passes the candidate window
less the trigger reserve, a simulated compaction is inserted -- charging
the summary request and the re-cached reply above, and removing the
tokens the summary dropped from every later turn's context and cache
reads. A compaction that would not shrink the context (a starting
context plus summary already at least as large) is skipped. Content
added after the summary is kept whole: a later turn carries the new
baseline plus whatever the conversation grew by since. The files a
session re-reads after a summary are not visible in the transcript it
replays, so the sweep does not charge them; the ``compaction-window``
rule adds a rediscovery correction for that instead. A **real**
observed compaction already recorded in the transcript's own events is
kept as-is under every candidate window (its real cost, not a synthetic
one) -- a policy sweep asks "what would happen on top of what already
happened", not "pretend the real compaction never fired".

**Sign convention (a deliberate divergence from ``ttl.py``):**
``delta_usd = candidate_cost - observed_cost`` throughout this module --
**negative means cheaper** (a saving), positive means more expensive.
``ttl.py``'s own tables use the opposite sign (``cost_observed -
best_cost``, positive = saving); that convention is unchanged there. This
module picks the other one deliberately: "delta vs observed" reads most
naturally as "what changes if you adopt this candidate", and a sweep
walks *many* candidate windows per key (not one best-vs-observed pair), so
a uniform "candidate minus observed" avoids re-deriving the sign per row.
``saving_usd = max(0, -delta_usd)`` is always non-negative, exactly like
``ttl.py``'s own ``saving_usd``.

**The "no candidate window" identity**: ``window=None`` never triggers a
synthetic compaction (the ``window is not None`` guard never opens), so
the per-transcript dropped-token offset never leaves ``0`` and every turn is
priced via its own unmodified, real values -- ``simulate_compaction_windows``'s
``window=None`` row is therefore *exactly* the transcript's true observed
cost, including every real compaction that already happened in it. This
identity is asserted directly in this module's tests and is why no
separate "observed cost" code path exists here.

**Wiring instructions for the integration agent** (this module cannot
edit ``report.py``/``cli.py``/``recommend.py`` -- see the project's file
ownership rules):

1. ``report.py`` (wherever it assembles sections, alongside ``ttl.build_section``/
   ``compaction`` etc.): build a ``snapshot_windows: dict[str, int | None]``
   mapping each top-level session id to its project's configured
   ``autoCompactWindow`` -- the same value ``context_budget.py``'s
   ``_build_autocompact_table`` already reads via
   ``snapshots.effective_config(snapshot).get("autoCompactWindow")``,
   just keyed by session id instead of project (reuse
   ``context_budget.py``'s own ``session_to_project`` reverse-lookup
   pattern, built from ``ContextBudgetStats.projects[project].session_ids``,
   or an equivalent per-session snapshot lookup already available at that
   point in ``report.py``). Then call::

       stats = compaction_sim.simulate_compaction_windows(all_results, rates, snapshot_windows)
       section = compaction_sim.build_section(stats)

   and append ``section`` to the assembled report's ``sections`` list.
   ``all_results`` is every parsed ``TranscriptResult`` (top-level and
   subagent) already available at that point in ``report.py``; ``rates``
   is whatever ``ModelRates``/``ResolvedRates``/lookup callable the
   surrounding code already passes to ``ttl.TtlStats.add``/``compaction
   .compaction_records_for_transcript``.
2. ``recommend.py`` (in ``recommend()``, alongside the other
   ``recs.extend(_rule_xxx(...))`` calls): add
   ``recs.extend(compaction_sim.RULES[0](report, compaction_sim.CompactionSimThresholds(), snapshot))``
   (or thread a shared ``CompactionSimThresholds.from_config(config.thresholds)``
   through, same as every other rule's threshold object) -- this requires
   step 1 to have already added the ``compaction_sim`` section to
   ``report`` first, since the rule reads its evidence back out of
   ``report``'s own tables (same evidence contract as every existing
   rule -- see ``recommend.py``'s module docstring).
3. ``docs/sections-reference.md`` already carries this module's own
   paragraph (added alongside this file); no further doc wiring needed.

Public surface: :data:`ASSUMPTIONS`, :data:`CANDIDATE_WINDOWS`,
:class:`CompactionSimThresholds`, :class:`CompactionSimWindowStats`,
:class:`CompactionSimTypeStats`, :class:`CompactionSimFidelityRow`,
:class:`CompactionSimStats`, :func:`simulate_compaction_windows`,
:func:`build_section`, :data:`RULES`.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable

from .compaction import CompactionRecord, compaction_records_for_transcript
from .model import (
    Column,
    EventKind,
    Recommendation,
    ReportModel,
    Section,
    Table,
    TranscriptResult,
    Turn,
)
from .pricing import ModelRates, ResolvedRates, price_turn
from .recache import RecacheThresholds
from .snapshots import Snapshot, effective_provenance, managed_keys

#: Assumptions this module's simulation makes, printed verbatim in the
#: report section's notes (same convention as ``ttl.ASSUMPTIONS``).
ASSUMPTIONS: list[str] = [
    "a simulated compaction resets context to the session's own starting "
    "context (its first reply's context: system prompt, tools, CLAUDE.md) "
    "plus a summary of this corpus's own median post_tokens across real "
    "compact_boundary events (20,000 tokens when this corpus has none); a "
    "compaction that would not shrink the context is skipped",
    "a candidate window summarises at the window less this corpus's own "
    "median trigger reserve (window minus pre_tokens across real auto "
    "compactions in sessions whose configured window is known; 0 when "
    "none)",
    "a simulated compaction charges the summary request (the context read "
    "again, the summary as output, the triggering reply's own cache "
    "split) and the reply after it re-caching its whole context: this "
    "corpus's own median share of the starting context still cached after "
    "a real compaction is read, the rest written at the reply's own "
    "cache lifetime (all written when this corpus has none)",
    "files re-read after a summary are not charged by the sweep -- the "
    "compaction-window rule corrects for them before recommending a window",
    "every later turn's context and cache reads shrink by the tokens the "
    "simulated summary dropped (cache writes once reads are used up), "
    "until the next compaction, real or simulated; growth after the "
    "summary is kept whole",
    "a real, observed compaction already in a transcript is kept as-is "
    "under every candidate window -- never re-simulated, never removed",
    "delta_usd = candidate_cost - observed_cost throughout this module: "
    "negative means the candidate is cheaper (a saving) -- the opposite "
    "sign convention to ttl.py's own tables, see this module's docstring",
]

#: Candidate ``autoCompactWindow`` values swept per transcript, plus
#: ``None`` meaning "never auto-compact" (real, already-observed
#: compactions are still kept under ``None`` -- see the module
#: docstring's "no candidate window" identity).
CANDIDATE_WINDOWS: tuple[int | None, ...] = (
    100_000,
    150_000,
    200_000,
    250_000,
    300_000,
    400_000,
    500_000,
    None,
)

def _window_label(window: int | None) -> str:
    return "none" if window is None else f"{window:,}"


# -- thresholds ---------------------------------------------------------


@dataclass(slots=True)
class CompactionSimThresholds:
    """Every tunable number this module's sweep and recommendation
    depend on, consolidated into one config-driven object -- same
    pattern as :class:`~claude_token_lens.recache.RecacheThresholds` and
    :class:`~claude_token_lens.ttl.TtlThresholds`.
    """

    #: Summary size used for a corpus with no real ``compact_boundary``
    #: event to measure ``postTokens`` from.
    default_summary_tokens: int = 20_000
    #: Tokens below ``autoCompactWindow`` at which a compaction fires, used
    #: for a corpus with no real auto compaction in a session whose
    #: configured window is known.
    default_trigger_reserve_tokens: int = 0
    #: Share of a session's starting context still read from cache on the
    #: reply after a compaction, used for a corpus with no real one (0:
    #: the whole new context is written).
    default_cached_prefix_share: float = 0.0
    #: Used for a corpus with no real post-compaction re-cache turn to
    #: measure a rediscovery allowance from (the ``compaction-window``
    #: rule's correction, not the sweep).
    default_rediscovery_allowance_usd: float = 0.0
    #: The ``compaction-window`` rule and the profile goals never suggest a
    #: window that summarises more often than this per session.
    max_compactions_per_session: float = 2.0
    #: A window switch is recommended only when the best candidate
    #: window's cost is below this fraction of the observed cost AND
    #: saves more than ``switch_usd`` -- both conditions, independently
    #: blocking (mirrors ``TtlThresholds.switch_pct``/``switch_usd``,
    #: plan-analogous "> 5% and > $1.00").
    switch_pct: float = 0.95
    switch_usd: float = 1.00
    #: A top-level session's fidelity self-check (simulating at its own
    #: snapshot's configured ``autoCompactWindow``) is flagged in the
    #: section notes once its fidelity exceeds this.
    fidelity_warn_pct: float = 10.0
    #: A real ``compact_boundary`` event is only correlated to a
    #: following priced turn when that turn's own timestamp lands within
    #: this many seconds of the event; a looser join is treated as
    #: unmatched (mirrors ``compaction.py``'s own private
    #: ``_MAX_JOIN_DELTA_S`` join-tightness gate of 900s).
    max_join_delta_s: float = 900.0

    @classmethod
    def from_config(cls, config: dict | None) -> "CompactionSimThresholds":
        """Build thresholds from a config dict, keeping this class's
        defaults for any key that's absent or of the wrong shape.

        Reads either a flat dict of this class's own field names, or a
        full ``config.toml``-shaped dict with a nested ``thresholds``
        table -- same flexible-shape convention as
        ``RecacheThresholds.from_config``/``TtlThresholds.from_config``.
        """
        data = config or {}
        if not isinstance(data, dict):
            data = {}
        nested = data.get("thresholds")
        if isinstance(nested, dict):
            data = nested

        kwargs: dict = {}
        if "default_summary_tokens" in data:
            kwargs["default_summary_tokens"] = int(data["default_summary_tokens"])
        if "default_trigger_reserve_tokens" in data:
            kwargs["default_trigger_reserve_tokens"] = int(data["default_trigger_reserve_tokens"])
        if "default_cached_prefix_share" in data:
            kwargs["default_cached_prefix_share"] = float(data["default_cached_prefix_share"])
        if "max_compactions_per_session" in data:
            kwargs["max_compactions_per_session"] = float(data["max_compactions_per_session"])
        if "default_rediscovery_allowance_usd" in data:
            kwargs["default_rediscovery_allowance_usd"] = float(data["default_rediscovery_allowance_usd"])
        if "switch_pct" in data:
            kwargs["switch_pct"] = float(data["switch_pct"])
        if "switch_usd" in data:
            kwargs["switch_usd"] = float(data["switch_usd"])
        if "fidelity_warn_pct" in data:
            kwargs["fidelity_warn_pct"] = float(data["fidelity_warn_pct"])
        if "max_join_delta_s" in data:
            kwargs["max_join_delta_s"] = float(data["max_join_delta_s"])
        return cls(**kwargs)

    def describe(self) -> list[str]:
        """One sentence per threshold, for the section's own notes --
        same convention as ``RecacheThresholds.describe``/
        ``TtlThresholds.describe``."""
        return [
            f"default_summary_tokens = {self.default_summary_tokens:,}: used when this "
            "corpus has no real compact_boundary event to measure a summary size from.",
            f"default_trigger_reserve_tokens = {self.default_trigger_reserve_tokens:,}: used "
            "when this corpus has no real auto compaction under a known window to measure "
            "the reserve from.",
            f"default_cached_prefix_share = {self.default_cached_prefix_share:.2f}: used when "
            "this corpus has no real compaction to measure how much of the starting context "
            "stays cached after one.",
            f"default_rediscovery_allowance_usd = ${self.default_rediscovery_allowance_usd:.2f}: "
            "used when this corpus has no real post-compaction re-cache turn to measure "
            "a rediscovery allowance from.",
            f"max_compactions_per_session = {self.max_compactions_per_session:g}: no window "
            "that summarises more often than this per session is suggested.",
            f"switch_pct = {self.switch_pct:.2f} and switch_usd = ${self.switch_usd:.2f}: a "
            "window switch is recommended only when the best candidate window's cost is "
            "below switch_pct of the observed cost AND saves more than switch_usd -- both "
            "conditions, independently blocking.",
            f"fidelity_warn_pct = {self.fidelity_warn_pct:.1f}%: a top-level session's "
            "fidelity self-check is flagged in the section notes once it exceeds this.",
            f"max_join_delta_s = {self.max_join_delta_s:.0f}s: a real compact_boundary "
            "event is only correlated to a following priced turn when that turn's own "
            "timestamp lands within this many seconds of the event.",
        ]


_DEFAULT_THRESHOLDS = CompactionSimThresholds()

#: What ``price_turn``/rate resolution accepts, and a per-model lookup
#: for mixed-model transcripts/subagents -- same aliases as ``ttl.py``.
RatesArg = ModelRates | ResolvedRates | None
RatesLookup = Callable[[str], RatesArg]


def _as_lookup(rates: "RatesArg | RatesLookup") -> RatesLookup:
    """Normalise a caller's ``rates`` argument to a per-turn lookup --
    identical contract to ``ttl._as_lookup``."""
    if callable(rates):
        return rates
    return lambda _model_id: rates


def _parse_ts(ts_raw: str | None) -> datetime | None:
    if not ts_raw:
        return None
    try:
        return datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _priced_turns(turns: list[Turn]) -> list[Turn]:
    """``Turn.turn_index > 0`` -- same "priced turns" convention as
    ``ttl.py``/``compaction.py`` (see ``model.Turn.turn_index``)."""
    return [t for t in turns if t.turn_index > 0]


def _dominant_model(turns: list[Turn]) -> str | None:
    """The most-common ``Turn.model`` among ``turns`` -- an approximation
    used only to pick a single rate for corpus-wide compaction-record
    correlation (:func:`_corpus_rediscovery_allowance`); per-turn pricing
    in the replay itself always uses ``lookup(turn.model)``."""
    models = [t.model for t in turns if t.model]
    if not models:
        return None
    return Counter(models).most_common(1)[0][0]


# -- real-compaction correlation -----------------------------------------


def _real_compaction_turn_indices(
    tr: TranscriptResult, priced_turns: list[Turn], th: CompactionSimThresholds
) -> dict[int, int]:
    """``{turn_index_in_priced_turns: count}`` for every real
    ``COMPACT_BOUNDARY`` event in ``tr.events`` correlated to the next
    priced turn at or after it, gated at ``th.max_join_delta_s`` -- a
    small local re-implementation of ``compaction.py``'s private
    forward-merge (never imported: see the module docstring)."""
    marked: dict[int, int] = {}
    idx = 0
    n = len(priced_turns)
    for event in tr.events:
        if event.kind != EventKind.COMPACT_BOUNDARY:
            continue
        event_dt = _parse_ts(event.ts)
        if event_dt is None:
            continue
        while idx < n and (_parse_ts(priced_turns[idx].ts) is None or _parse_ts(priced_turns[idx].ts) < event_dt):
            idx += 1
        if idx >= n:
            continue
        turn_dt = _parse_ts(priced_turns[idx].ts)
        if turn_dt is None:
            continue
        join_delta = (turn_dt - event_dt).total_seconds()
        if join_delta > th.max_join_delta_s:
            continue
        marked[idx] = marked.get(idx, 0) + 1
    return marked


# -- corpus-wide defaults --------------------------------------------------


def _corpus_summary_tokens(results: list[TranscriptResult], th: CompactionSimThresholds) -> tuple[float, bool]:
    """``(tokens, is_default)`` -- the median ``post_tokens`` (the summary
    a compaction leaves behind) across every real ``COMPACT_BOUNDARY``
    event in ``results``, or ``th.default_summary_tokens`` (flagged) when
    there are none. A flat size, not a ratio of the context: on a real
    corpus a summary barely grows with the conversation it replaces."""
    sizes = [
        event.post_tokens
        for tr in results
        for event in tr.events
        if event.kind == EventKind.COMPACT_BOUNDARY and event.post_tokens
    ]
    if not sizes:
        return float(th.default_summary_tokens), True
    return float(statistics.median(sizes)), False


def _corpus_trigger_reserve(
    results: list[TranscriptResult], snapshot_windows: dict[str, int | None], th: CompactionSimThresholds
) -> tuple[float, bool]:
    """``(tokens, is_default)`` -- how far below ``autoCompactWindow`` a
    real auto compaction fired: the median ``window - pre_tokens`` across
    auto-triggered ``COMPACT_BOUNDARY`` events in top-level sessions whose
    configured window is known, or ``th.default_trigger_reserve_tokens``
    (flagged) when there are none."""
    reserves: list[int] = []
    for tr in results:
        window = snapshot_windows.get(tr.meta.session_id) if tr.meta.kind == "top-level" else None
        if not window:
            continue
        for event in tr.events:
            if event.kind != EventKind.COMPACT_BOUNDARY or event.trigger != "auto" or not event.pre_tokens:
                continue
            reserve = window - event.pre_tokens
            if 0 <= reserve < window:
                reserves.append(reserve)
    if not reserves:
        return float(th.default_trigger_reserve_tokens), True
    return float(statistics.median(reserves)), False


def _corpus_cached_prefix_share(
    results: list[TranscriptResult], th: CompactionSimThresholds
) -> tuple[float, bool]:
    """``(share, is_default)`` -- how much of a session's starting context
    the reply after a real compaction still read from cache (its
    ``cache_read`` over the transcript's first priced turn's context,
    capped at 1), median across every real compaction joined to a reply,
    or ``th.default_cached_prefix_share`` (flagged) when there are none."""
    shares: list[float] = []
    for tr in results:
        priced = _priced_turns(tr.turns)
        if not priced or priced[0].ctx <= 0:
            continue
        floor = priced[0].ctx
        for index in _real_compaction_turn_indices(tr, priced, th):
            shares.append(min(1.0, priced[index].cache_read_tokens / floor))
    if not shares:
        return th.default_cached_prefix_share, True
    return statistics.median(shares), False


def _corpus_rediscovery_allowance(
    results: list[TranscriptResult], lookup: RatesLookup, th: CompactionSimThresholds
) -> tuple[float, bool]:
    """``(allowance_usd, is_default)`` -- the median
    ``next_turn_write_cost`` across every real, join-tight,
    recache-flagged :class:`~claude_token_lens.compaction.CompactionRecord`
    in ``results``, or ``th.default_rediscovery_allowance_usd``
    (flagged) when there are none."""
    recache_th = RecacheThresholds()
    costs: list[float] = []
    for tr in results:
        priced = _priced_turns(tr.turns)
        if not priced:
            continue
        rates = lookup(_dominant_model(priced))
        records: list[CompactionRecord] = compaction_records_for_transcript(tr, rates, recache_th)
        for record in records:
            if not record.next_turn_is_recache or record.next_turn_write_cost is None:
                continue
            if record.join_delta_s is not None and record.join_delta_s > th.max_join_delta_s:
                continue
            costs.append(record.next_turn_write_cost)
    if not costs:
        return th.default_rediscovery_allowance_usd, True
    return statistics.median(costs), False


# -- replay -----------------------------------------------------------------


@dataclass(slots=True)
class _ReplayResult:
    cost: float = 0.0
    compactions: int = 0
    ctx_sum: float = 0.0
    turns: int = 0


@dataclass(slots=True, frozen=True)
class _Shape:
    """What a simulated compaction looks like, measured from this
    corpus's real ones (see ``ASSUMPTIONS``)."""

    summary_tokens: float = 20_000.0
    trigger_reserve: float = 0.0
    cached_prefix_share: float = 0.0


def _shrunk_cost(turn: Turn, rates: RatesArg, dropped: float) -> float:
    """Price ``turn`` with ``dropped`` tokens taken out of its context:
    out of its cache reads first (the dropped history is the old, cached
    part of the prefix), then out of its cache writes once the reads are
    used up (a turn that re-cached its whole prefix)."""
    if dropped <= 0:
        return price_turn(turn, rates).total
    ctx = max(0, int(round(turn.ctx - dropped)))
    read = max(0, int(round(turn.cache_read_tokens - dropped)))
    from_writes = max(0.0, dropped - turn.cache_read_tokens)
    write = turn.cache_creation_tokens
    keep = max(0.0, (write - from_writes) / write) if write else 1.0
    shrunk = replace(
        turn,
        ctx=ctx,
        cache_read_tokens=read,
        cache_creation_tokens=int(round(write * keep)),
        cc_5m=int(round(turn.cc_5m * keep)),
        cc_1h=int(round(turn.cc_1h * keep)),
    )
    return price_turn(shrunk, rates).total


def _summary_request_cost(turn: Turn, rates: RatesArg, dropped: float, summary_tokens: float) -> float:
    """The summary request a compaction sends and the transcript never
    logs: the triggering turn's own (already shrunk) context, read and
    written the way that turn's was, with the summary as its output."""
    request = replace(turn, output_tokens=int(round(summary_tokens)), thinking_tokens=0)
    return _shrunk_cost(request, rates, dropped)


def _recached_reply_cost(turn: Turn, rates: RatesArg, new_ctx: float, cached_prefix: float) -> float:
    """The reply right after a simulated compaction: its context is now
    ``new_ctx`` (starting context plus summary), of which ``cached_prefix``
    is still read from cache and the rest is written, at this turn's own
    5-minute/1-hour mix (5 minutes when it wrote nothing)."""
    ctx = int(round(new_ctx))
    read = min(ctx, int(round(cached_prefix)))
    uncached = min(turn.input_tokens, ctx - read)
    write = ctx - read - uncached
    written = turn.cc_5m + turn.cc_1h
    write_1h = int(round(write * turn.cc_1h / written)) if written else 0
    reply = replace(
        turn,
        ctx=ctx,
        input_tokens=uncached,
        cache_read_tokens=read,
        cache_creation_tokens=write,
        cc_5m=write - write_1h,
        cc_1h=write_1h,
    )
    return price_turn(reply, rates).total


def _replay_transcript(
    priced_turns: list[Turn],
    lookup: RatesLookup,
    window: int | None,
    shape: _Shape,
    real_after: dict[int, int],
) -> _ReplayResult:
    """Walk ``priced_turns`` in order under candidate ``window``. See the
    module docstring's algorithm description and its "no candidate
    window" identity (``window=None`` reproduces the true observed cost
    exactly, since ``dropped`` then never leaves 0)."""
    if not priced_turns:
        return _ReplayResult()
    starting_ctx = float(priced_turns[0].ctx)
    new_ctx = starting_ctx + shape.summary_tokens
    cached_prefix = starting_ctx * shape.cached_prefix_share
    trigger = None if window is None else window - shape.trigger_reserve
    dropped = 0.0  # tokens simulated summaries have taken out of the context
    cost = 0.0
    compactions = 0
    ctx_sum = 0.0
    for i, turn in enumerate(priced_turns):
        rates = lookup(turn.model)
        real_count = real_after.get(i, 0)
        if real_count:
            # A real compact_boundary event already reset context here --
            # the turn's own observed values already reflect what
            # actually happened, so it is priced as-is (never re-scaled,
            # never double-charged) and any earlier synthetic summary
            # is superseded.
            dropped = 0.0
            compactions += real_count
            cost += price_turn(turn, rates).total
            sim_ctx = float(turn.ctx)
        else:
            sim_ctx = max(0.0, turn.ctx - dropped)
            if trigger is not None and sim_ctx > trigger and new_ctx < sim_ctx:
                cost += _summary_request_cost(turn, rates, dropped, shape.summary_tokens)
                cost += _recached_reply_cost(turn, rates, new_ctx, cached_prefix)
                compactions += 1
                dropped = turn.ctx - new_ctx
                sim_ctx = new_ctx
            else:
                cost += _shrunk_cost(turn, rates, dropped)
        ctx_sum += sim_ctx
    return _ReplayResult(cost=cost, compactions=compactions, ctx_sum=ctx_sum, turns=len(priced_turns))


# -- accumulation -----------------------------------------------------------


@dataclass(slots=True)
class _WindowAccumulator:
    compactions: int = 0
    ctx_sum: float = 0.0
    ctx_turns: int = 0
    cost: float = 0.0


@dataclass(slots=True)
class CompactionSimWindowStats:
    """One ``(agent-type key, candidate window)`` cell: the corpus-wide
    roll-up used to build ``compaction_sim_by_window``."""

    key: str = ""
    window: int | None = None
    sessions: int = 0
    compactions: int = 0
    ctx_sum: float = 0.0
    ctx_turns: int = 0
    cost: float = 0.0
    observed_cost: float = 0.0

    @property
    def compactions_per_session(self) -> float:
        return self.compactions / self.sessions if self.sessions else 0.0

    @property
    def mean_ctx(self) -> float:
        return self.ctx_sum / self.ctx_turns if self.ctx_turns else 0.0

    @property
    def delta_usd(self) -> float:
        """``candidate_cost - observed_cost``: negative = cheaper. See
        the module docstring's sign-convention note."""
        return self.cost - self.observed_cost

    @property
    def delta_pct(self) -> float | None:
        if self.observed_cost <= 0:
            return None
        return 100.0 * self.delta_usd / self.observed_cost

    @property
    def saving_usd(self) -> float:
        return max(0.0, -self.delta_usd)


@dataclass(slots=True)
class CompactionSimTypeStats:
    """One agent-type key's best candidate window, drawn from the same
    per-``(key, window)`` accumulator ``CompactionSimWindowStats`` reads
    -- used to build ``compaction_sim_by_agent_type``."""

    key: str = ""
    sessions: int = 0
    observed_cost: float = 0.0
    best_window: int | None = None
    best_cost: float = 0.0

    @property
    def delta_usd(self) -> float:
        return self.best_cost - self.observed_cost

    @property
    def delta_pct(self) -> float | None:
        if self.observed_cost <= 0:
            return None
        return 100.0 * self.delta_usd / self.observed_cost

    @property
    def saving_usd(self) -> float:
        return max(0.0, -self.delta_usd)

    def recommendation(self, th: CompactionSimThresholds) -> str:
        """Mirrors ``TtlTypeStats.recommendation``'s switch-gating
        shape: a switch is only worth stating when it clears both
        ``switch_pct`` and ``switch_usd``."""
        if self.observed_cost <= 0:
            return "no material difference"
        pct_ok = self.best_cost < th.switch_pct * self.observed_cost
        usd_ok = self.saving_usd > th.switch_usd
        if self.best_window is None:
            return "no material difference"
        if pct_ok and usd_ok:
            return f"switch to autoCompactWindow={self.best_window:,} (saves ${self.saving_usd:.2f})"
        return "no material difference"


@dataclass(slots=True)
class CompactionSimFidelityRow:
    """One top-level session's fidelity self-check: simulating at its
    own snapshot's configured ``autoCompactWindow`` should reproduce its
    true observed cost almost exactly."""

    session_id: str = ""
    window: int = 0
    simulated_cost: float = 0.0
    observed_cost: float = 0.0

    @property
    def fidelity_pct(self) -> float | None:
        if self.observed_cost <= 0:
            return None
        return 100.0 * abs(self.simulated_cost - self.observed_cost) / self.observed_cost


class CompactionSimStats:
    """Accumulates :func:`_replay_transcript` results across many
    transcripts, keyed by ``(agent-type key, candidate window)`` --
    ``"top-level"`` for the main session, otherwise
    ``TranscriptMeta.agent_type`` (``"unknown"`` fallback), same
    convention as ``ttl.TtlStats``/``compaction.CompactionStats``.
    """

    def __init__(
        self,
        shape: _Shape,
        defaults: frozenset[str],
        rediscovery_allowance_usd: float,
        rediscovery_allowance_is_default: bool,
    ) -> None:
        self.shape = shape
        #: The ``_Shape`` fields that fell back to a threshold default.
        self.defaults = defaults
        self.rediscovery_allowance_usd = rediscovery_allowance_usd
        self.rediscovery_allowance_is_default = rediscovery_allowance_is_default
        self._acc: dict[tuple[str, int | None], _WindowAccumulator] = {}
        self._sessions_by_key: dict[str, int] = {}
        self._fidelity_rows: list[CompactionSimFidelityRow] = []

    def add_transcript(
        self,
        tr: TranscriptResult,
        lookup: RatesLookup,
        snapshot_window: int | None,
        th: CompactionSimThresholds,
    ) -> None:
        priced_turns = _priced_turns(tr.turns)
        if not priced_turns:
            return
        key = "top-level" if tr.meta.kind == "top-level" else (tr.meta.agent_type or "unknown")
        real_after = _real_compaction_turn_indices(tr, priced_turns, th)
        self._sessions_by_key[key] = self._sessions_by_key.get(key, 0) + 1

        results_by_window: dict[int | None, _ReplayResult] = {}
        for window in CANDIDATE_WINDOWS:
            result = _replay_transcript(priced_turns, lookup, window, self.shape, real_after)
            results_by_window[window] = result
            acc = self._acc.setdefault((key, window), _WindowAccumulator())
            acc.compactions += result.compactions
            acc.ctx_sum += result.ctx_sum
            acc.ctx_turns += result.turns
            acc.cost += result.cost

        if tr.meta.kind == "top-level" and snapshot_window is not None:
            observed_cost = results_by_window[None].cost
            sim_result = results_by_window.get(snapshot_window)
            if sim_result is None:
                sim_result = _replay_transcript(priced_turns, lookup, snapshot_window, self.shape, real_after)
            self._fidelity_rows.append(
                CompactionSimFidelityRow(
                    session_id=tr.meta.session_id,
                    window=snapshot_window,
                    simulated_cost=sim_result.cost,
                    observed_cost=observed_cost,
                )
            )

    def by_window(self, key: str) -> list[CompactionSimWindowStats]:
        """Every candidate window's roll-up for ``key`` (in
        ``CANDIDATE_WINDOWS`` order), each carrying that key's observed
        (``window=None``) cost for the delta columns."""
        sessions = self._sessions_by_key.get(key, 0)
        observed_acc = self._acc.get((key, None))
        observed_cost = observed_acc.cost if observed_acc else 0.0
        rows: list[CompactionSimWindowStats] = []
        for window in CANDIDATE_WINDOWS:
            acc = self._acc.get((key, window))
            if acc is None:
                continue
            rows.append(
                CompactionSimWindowStats(
                    key=key,
                    window=window,
                    sessions=sessions,
                    compactions=acc.compactions,
                    ctx_sum=acc.ctx_sum,
                    ctx_turns=acc.ctx_turns,
                    cost=acc.cost,
                    observed_cost=observed_cost,
                )
            )
        return rows

    def by_key(self) -> dict[str, CompactionSimTypeStats]:
        """Every agent-type key's best candidate window (lowest cost;
        ``None`` -- never auto-compact -- included as a candidate like
        any other), keyed by ``key``."""
        out: dict[str, CompactionSimTypeStats] = {}
        for key in sorted(self._sessions_by_key):
            sessions = self._sessions_by_key[key]
            observed_acc = self._acc.get((key, None))
            observed_cost = observed_acc.cost if observed_acc else 0.0
            best_window: int | None = None
            best_cost: float | None = None
            for window in CANDIDATE_WINDOWS:
                acc = self._acc.get((key, window))
                if acc is None:
                    continue
                if best_cost is None or acc.cost < best_cost:
                    best_cost = acc.cost
                    best_window = window
            out[key] = CompactionSimTypeStats(
                key=key,
                sessions=sessions,
                observed_cost=observed_cost,
                best_window=best_window,
                best_cost=best_cost if best_cost is not None else observed_cost,
            )
        return out

    @property
    def fidelity_rows(self) -> list[CompactionSimFidelityRow]:
        return list(self._fidelity_rows)

    def keys(self) -> list[str]:
        return sorted(self._sessions_by_key)


def simulate_compaction_windows(
    results: list[TranscriptResult],
    rates: "RatesArg | RatesLookup",
    snapshot_windows: dict[str, int | None],
    thresholds: CompactionSimThresholds | None = None,
) -> CompactionSimStats:
    """Sweep :data:`CANDIDATE_WINDOWS` across every transcript in
    ``results`` and return the accumulated :class:`CompactionSimStats`.

    ``rates`` is either a single already-resolved rate or a per-model
    lookup (``pricing.Pricing.resolve_model``) -- see :func:`_as_lookup`.
    ``snapshot_windows`` maps a top-level session's ``session_id`` (the
    same id a subagent transcript's own ``TranscriptMeta.session_id``
    shares with its parent top-level session, per ``discovery.py``) to
    that session's own configured ``autoCompactWindow``, or ``None`` when
    unknown -- used for the fidelity self-check, which is restricted to
    top-level transcripts (see ``build_section``'s
    ``compaction_sim_fidelity`` table), and to measure the trigger reserve.
    """
    th = thresholds or _DEFAULT_THRESHOLDS
    lookup = _as_lookup(rates)
    summary, summary_is_default = _corpus_summary_tokens(results, th)
    reserve, reserve_is_default = _corpus_trigger_reserve(results, snapshot_windows, th)
    share, share_is_default = _corpus_cached_prefix_share(results, th)
    defaults = frozenset(
        name
        for name, is_default in (
            ("summary_tokens", summary_is_default),
            ("trigger_reserve", reserve_is_default),
            ("cached_prefix_share", share_is_default),
        )
        if is_default
    )
    allowance, allowance_is_default = _corpus_rediscovery_allowance(results, lookup, th)
    stats = CompactionSimStats(_Shape(summary, reserve, share), defaults, allowance, allowance_is_default)
    for tr in results:
        snapshot_window = snapshot_windows.get(tr.meta.session_id)
        stats.add_transcript(tr, lookup, snapshot_window, th)
    return stats


# -- report section -----------------------------------------------------


def build_section(stats: CompactionSimStats, thresholds: CompactionSimThresholds | None = None) -> Section:
    """Render a :class:`CompactionSimStats` roll-up as the report's
    "Compaction-window sweep" section: ``compaction_sim_by_window``
    (top-level sessions only), ``compaction_sim_by_agent_type`` (every
    key's best window, top-level and subagent), and
    ``compaction_sim_fidelity`` (top-level sessions with a known
    configured window). Notes print :data:`ASSUMPTIONS` verbatim, the
    summary size, trigger reserve, cached share and rediscovery allowance
    actually used (flagging a default), ``thresholds.describe()``, and a
    fidelity warning
    for any session above ``thresholds.fidelity_warn_pct``.
    """
    th = thresholds or _DEFAULT_THRESHOLDS

    by_window_columns = [
        Column(key="window", label="Candidate autoCompactWindow", kind="str"),
        Column(key="compactions_per_session", label="Simulated compactions / session", kind="float"),
        Column(key="mean_ctx", label="Mean ctx", kind="tokens"),
        Column(key="cost", label="Total cost", kind="money"),
        Column(key="delta_usd", label="Delta vs observed (USD, negative = cheaper)", kind="money"),
        Column(key="delta_pct", label="Delta vs observed (%, negative = cheaper)", kind="pct"),
    ]
    window_rows = stats.by_window("top-level")
    by_window_table = Table(
        name="compaction_sim_by_window",
        title="Compaction-window sweep: top-level sessions",
        columns=by_window_columns,
        rows=[
            [
                _window_label(r.window),
                r.compactions_per_session,
                r.mean_ctx,
                r.cost,
                r.delta_usd,
                r.delta_pct,
            ]
            for r in window_rows
        ],
        notes=(
            ["No top-level transcripts with priced turns in this corpus."]
            if not window_rows
            else []
        ),
    )

    by_type_columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="sessions", label="Sessions", kind="int"),
        Column(key="observed_cost", label="Observed cost", kind="money"),
        Column(key="best_window", label="Best window", kind="str"),
        Column(key="best_cost", label="Best cost", kind="money"),
        Column(key="saving_usd", label="Saving if switched (USD, 0 floor)", kind="money"),
        Column(key="delta_pct", label="Delta at best window (%, negative = cheaper)", kind="pct"),
        Column(key="recommendation", label="Recommendation", kind="str"),
    ]
    by_key = stats.by_key()
    by_type_table = Table(
        name="compaction_sim_by_agent_type",
        title="Compaction-window sweep: best window by agent type",
        columns=by_type_columns,
        rows=[
            [
                key,
                row.sessions,
                row.observed_cost,
                _window_label(row.best_window),
                row.best_cost,
                row.saving_usd,
                row.delta_pct,
                row.recommendation(th),
            ]
            for key, row in sorted(by_key.items())
        ],
        notes=(["No transcripts with priced turns in this corpus."] if not by_key else []),
    )

    fidelity_columns = [
        Column(key="session", label="Session", kind="str"),
        Column(key="configured_window", label="Configured autoCompactWindow", kind="tokens"),
        Column(key="simulated_cost", label="Simulated cost", kind="money"),
        Column(key="observed_cost", label="Observed cost", kind="money"),
        Column(key="fidelity_pct", label="Fidelity", kind="pct"),
    ]
    fidelity_rows = stats.fidelity_rows
    fidelity_table = Table(
        name="compaction_sim_fidelity",
        title="Compaction-window sweep: fidelity self-check",
        columns=fidelity_columns,
        rows=[
            [row.session_id, row.window, row.simulated_cost, row.observed_cost, row.fidelity_pct]
            for row in fidelity_rows
        ],
        notes=(
            ["No top-level session with a known configured autoCompactWindow in this corpus."]
            if not fidelity_rows
            else []
        ),
    )

    notes: list[str] = list(ASSUMPTIONS)
    shape = stats.shape
    notes.append(
        f"Summary size used: {shape.summary_tokens:,.0f} tokens"
        + (
            " (default -- no real compact_boundary event found)."
            if "summary_tokens" in stats.defaults
            else " (this corpus's own median post_tokens across real compact_boundary events)."
        )
    )
    notes.append(
        f"Trigger reserve used: {shape.trigger_reserve:,.0f} tokens below the window"
        + (
            " (default -- no real auto compaction under a known window found)."
            if "trigger_reserve" in stats.defaults
            else " (this corpus's own median window minus pre_tokens across real auto compactions)."
        )
    )
    notes.append(
        f"Starting context still cached after a summary: {100.0 * shape.cached_prefix_share:.0f}%"
        + (
            " (default -- no real compaction joined to a reply found)."
            if "cached_prefix_share" in stats.defaults
            else " (this corpus's own median across real compactions)."
        )
    )
    allowance_note = f"Rediscovery allowance used: ${stats.rediscovery_allowance_usd:.4f}"
    if stats.rediscovery_allowance_is_default:
        allowance_note += " (default -- no real post-compaction re-cache turn found)."
    else:
        allowance_note += " (this corpus's own median post-compaction re-cache write cost)."
    allowance_note += " Used by the compaction-window rule's correction, not charged by the sweep."
    notes.append(allowance_note)
    notes.extend(th.describe())

    flagged = [row for row in fidelity_rows if (row.fidelity_pct or 0.0) > th.fidelity_warn_pct]
    for row in sorted(flagged, key=lambda r: r.session_id):
        notes.append(
            f"Fidelity warning: session {row.session_id} simulated at its own configured "
            f"autoCompactWindow={row.window:,} differs from its observed cost by "
            f"{row.fidelity_pct:.1f}% (> {th.fidelity_warn_pct:.1f}%)."
        )

    return Section(
        key="compaction_sim",
        title="Compaction-window sweep",
        tables=[by_window_table, by_type_table, fidelity_table],
        notes=notes,
    )


# -- recommendation rule --------------------------------------------------


def _cell(report: ReportModel, section_key: str, table_name: str, row_key: str, column_key: str):
    """Same lookup contract as ``recommend._cell``: the value at
    ``row_key``/``column_key`` in ``section_key``.``table_name``, or
    ``None`` when any of those don't exist -- kept local to this module
    since ``recommend.py``'s own helper is private."""
    for section in report.sections:
        if section.key != section_key:
            continue
        for table in section.tables:
            if table.name != table_name:
                continue
            col_index = None
            for i, col in enumerate(table.columns):
                if col.key == column_key:
                    col_index = i
                    break
            if col_index is None:
                return None
            for row in table.rows:
                if row and row[0] == row_key:
                    return row[col_index]
    return None


def _evidence(label: str, value, section_key: str, table_name: str, row_key) -> tuple:
    """One ``Recommendation.evidence`` tuple -- same contract as
    ``recommend._evidence`` (``source_table`` is
    ``"<section_key>.<table_name>"``)."""
    return (label, value, f"{section_key}.{table_name}", row_key)


def _scope_and_lever_note(snapshot: Snapshot | None) -> tuple[str, str]:
    """``(scope, file_note)`` for the ``autoCompactWindow`` lever, read
    from ``snapshots.effective_provenance`` -- deliberately more precise
    than ``recommend._lever_scope``'s generic repo/user/managed 3-way
    (which only ever distinguishes per-agent frontmatter from a plain
    settings key, so a plain settings key always reads "user" there).
    Since ``autoCompactWindow`` is always a plain settings key, this
    module instead reads which of the four real settings layers
    (``snapshots.SETTINGS_LAYER_NAMES``) actually set the effective
    value, and reports "user" or "project" per the brief's convention
    (with "managed" doing what it always does everywhere else: named,
    not offered as user-actionable)."""
    if snapshot is None:
        return "user", "~/.claude/settings.json"
    keys = set(managed_keys(snapshot))
    if "autoCompactWindow" in keys:
        return "managed", "the org's managed-settings.json (raise with your administrator)"
    layer = effective_provenance(snapshot).get("autoCompactWindow")
    if layer == "project_shared":
        return "project", "<project>/.claude/settings.json"
    if layer == "project_local":
        return "project", "<project>/.claude/settings.local.json"
    return "user", "~/.claude/settings.json"


def _table(report: ReportModel, section_key: str, table_name: str):
    """The raw :class:`Table` for ``section_key``.``table_name``, or
    ``None`` when either doesn't exist. Added alongside ``_cell`` for the
    conservative rewrite of :func:`_rule_compaction_window` below (v4
    wiring round): that rewrite needs whole rows (every candidate
    window's own compaction count and delta), not one cell at a time, so
    ``_cell``'s single-value contract doesn't fit -- this is the "small
    helper" the wiring brief allows adding to this module."""
    for section in report.sections:
        if section.key != section_key:
            continue
        for table in section.tables:
            if table.name == table_name:
                return table
    return None


def _rediscovery_allowance_usd_used(report: ReportModel, thresholds: CompactionSimThresholds) -> float:
    """The rediscovery allowance :func:`simulate_compaction_windows`
    actually charged per simulated compaction in this report, read back
    out of ``build_section``'s own ``"Rediscovery allowance used: $X"``
    note (the only place that number is rendered -- see
    ``build_section``'s notes list above). Falls back to
    ``thresholds.default_rediscovery_allowance_usd`` when the
    ``compaction_sim`` section or that note isn't present (e.g. a
    report filtered down to a single other section)."""
    prefix = "Rediscovery allowance used: $"
    for section in report.sections:
        if section.key != "compaction_sim":
            continue
        for note in section.notes:
            if note.startswith(prefix):
                value_str = note[len(prefix):].split(" ", 1)[0].rstrip(".")
                try:
                    return float(value_str)
                except ValueError:
                    continue
    return thresholds.default_rediscovery_allowance_usd


def _post_compaction_redundant_reads_mean(report: ReportModel) -> float | None:
    """This corpus's own mean count of redundant (repeated) reads landing
    within topology's rediscovery window of a real compaction, per
    session -- ``topology.py``'s ``topology_redundant_reads`` table's
    second row (``"...within N turns of a compaction"``), read
    positionally rather than by its exact wording (which embeds
    ``topology.py``'s own ``_REDISCOVERY_WINDOW_TURNS`` constant).
    ``None`` when the ``agents`` section (topology's own) isn't part of
    this report, or that table has no rows/mean -- the caller then falls
    back to one allowance per simulated compaction instead (see
    :func:`_rule_compaction_window`)."""
    table = _table(report, "agents", "topology_redundant_reads")
    if table is None or len(table.rows) < 2:
        return None
    col = {c.key: i for i, c in enumerate(table.columns)}
    if "mean_per_session" not in col:
        return None
    return table.rows[1][col["mean_per_session"]]


def _rule_compaction_window(
    report: ReportModel, thresholds: CompactionSimThresholds, snapshot: Snapshot | None
) -> list[Recommendation]:
    """"compaction-window": recommends a *range floor* for
    ``autoCompactWindow`` -- the smallest candidate window whose modelled
    per-session compaction count stays at or below
    ``thresholds.max_compactions_per_session`` and whose modelled saving
    clears ``thresholds.switch_pct``/``switch_usd`` over the observed
    cost, once a rediscovery correction has been taken off.

    The sweep (:func:`simulate_compaction_windows`) prices the summary
    and the re-cached reply after it, but cannot see the files a session
    re-reads once a summary has dropped them, so a small window's saving
    is an upper bound. This rule takes a rediscovery estimate off each
    candidate window's saving before deciding whether to recommend it,
    priced with the rediscovery allowance (this corpus's own median
    post-compaction re-cache write cost, from the section's notes):

    - When this report also carries topology's ``agents`` section, one
      allowance per redundant read landing shortly after a real
      compaction (``topology_redundant_reads``' second row, a mean per
      session) per simulated compaction.
    - Otherwise (no ``topology_redundant_reads`` data available -- e.g.
      a report filtered down to only the ``compaction_sim`` section),
      one allowance per simulated compaction -- noted as a fallback
      rather than silently applied.

    A recommendation, when one fires, is phrased as a floor ("at least
    W"), not a single optimal point -- the correction above is itself a
    modelled estimate, so naming one exact "best" window the way the
    previous point-recommendation did would overstate this rule's own
    precision. Evidence and rule id/category/lever match every other
    rule in this module/``recommend.py`` (see that module's docstring).
    """
    by_window_table = _table(report, "compaction_sim", "compaction_sim_by_window")
    if by_window_table is None or not by_window_table.rows:
        return []
    col = {c.key: i for i, c in enumerate(by_window_table.columns)}
    rows_by_label = {row[col["window"]]: row for row in by_window_table.rows}
    observed_row = rows_by_label.get("none")
    if observed_row is None:
        return []
    observed_cost = observed_row[col["cost"]]
    if not observed_cost:
        return []

    allowance_usd = _rediscovery_allowance_usd_used(report, thresholds)
    redundant_reads_mean = _post_compaction_redundant_reads_mean(report)
    if redundant_reads_mean is not None:
        extra_per_compaction_usd = allowance_usd * redundant_reads_mean
        allowance_source = (
            f"this corpus's own post-compaction redundant-read rate "
            f"({redundant_reads_mean:.2f} redundant reads/session, from topology_redundant_reads), "
            f"each priced at ${allowance_usd:.4f}, the median re-cache after a real summary"
        )
    else:
        extra_per_compaction_usd = allowance_usd
        allowance_source = (
            f"topology_redundant_reads unavailable in this report, so one ${allowance_usd:.4f} "
            "re-cache (the median after a real summary) per simulated summary as a fallback"
        )

    chosen: tuple[str, float, float, float] | None = None  # (label, compactions_per_session, raw_saving, adjusted_saving)
    for window in CANDIDATE_WINDOWS:
        if window is None:
            continue
        label = _window_label(window)
        row = rows_by_label.get(label)
        if row is None:
            continue
        compactions_per_session = row[col["compactions_per_session"]]
        delta_usd = row[col["delta_usd"]]
        if compactions_per_session is None or delta_usd is None:
            continue
        if compactions_per_session > thresholds.max_compactions_per_session:
            continue
        raw_saving_usd = max(0.0, -delta_usd)
        adjusted_saving_usd = raw_saving_usd - extra_per_compaction_usd * compactions_per_session
        pct_ok = adjusted_saving_usd > (1.0 - thresholds.switch_pct) * observed_cost
        usd_ok = adjusted_saving_usd > thresholds.switch_usd
        if pct_ok and usd_ok:
            chosen = (label, compactions_per_session, raw_saving_usd, adjusted_saving_usd)
            break  # CANDIDATE_WINDOWS (minus None) is ascending -- first hit is the smallest.

    if chosen is None:
        return []
    label, compactions_per_session, raw_saving_usd, adjusted_saving_usd = chosen

    fidelity_table = _table(report, "compaction_sim", "compaction_sim_fidelity")
    has_fidelity_rows = bool(fidelity_table and fidelity_table.rows)

    scope, file_note = _scope_and_lever_note(snapshot)
    action = (
        f"Set autoCompactWindow to at least {label} in {file_note}. This is a modelled, not "
        f"observed, range floor: smaller windows compact more often, and the replay can't see "
        f"the files a session re-reads after a summary, so only the smallest window clearing the "
        f"threshold after a rediscovery correction ({allowance_source}) is named, rather than a "
        f"single 'best' point. Projected saving at {label}: "
        f"${adjusted_saving_usd:.2f} vs the observed cost of ${observed_cost:.2f} "
        f"(raw modelled saving before this correction: ${raw_saving_usd:.2f})."
    )
    if has_fidelity_rows:
        action += (
            " See compaction_sim_fidelity for how well this sweep's modelled costs track this "
            "corpus's own real observed costs."
        )
    if scope == "managed":
        action += " This key is managed by policy -- raise with your administrator."

    evidence = [
        _evidence("Observed cost (top-level, window=none)", observed_cost, "compaction_sim", "compaction_sim_by_window", "none"),
        _evidence(f"Candidate {label}: modelled compactions/session", compactions_per_session, "compaction_sim", "compaction_sim_by_window", label),
        _evidence(f"Candidate {label}: raw modelled saving (pre-correction)", raw_saving_usd, "compaction_sim", "compaction_sim_by_window", label),
        _evidence(f"Candidate {label}: modelled saving after rediscovery correction", adjusted_saving_usd, "compaction_sim", "compaction_sim_by_window", label),
    ]

    return [
        Recommendation(
            id="compaction-window",
            severity="advice",
            category="settings",
            archetypes=(),
            title=f"Set autoCompactWindow to at least {label}",
            action=action,
            lever="autoCompactWindow",
            evidence=evidence,
            scope=scope,
            agent_type="top-level",
        )
    ]


#: Every recommendation-rule function this module exports, in the same
#: shape ``recommend.py`` would register them (one rule id per entry) --
#: see the module docstring's wiring instructions for how the
#: integration agent calls this from ``recommend.recommend()``.
RULES: list[Callable[[ReportModel, CompactionSimThresholds, Snapshot | None], list[Recommendation]]] = [
    _rule_compaction_window
]


__all__ = [
    "ASSUMPTIONS",
    "CANDIDATE_WINDOWS",
    "CompactionSimThresholds",
    "CompactionSimWindowStats",
    "CompactionSimTypeStats",
    "CompactionSimFidelityRow",
    "CompactionSimStats",
    "simulate_compaction_windows",
    "build_section",
    "RULES",
]
