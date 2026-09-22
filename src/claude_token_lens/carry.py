"""Context carry cost per tool (v4-carry-cost): what a tool result left
sitting in context actually cost, turn after turn, until a compaction
finally drops it.

Every project analytic so far prices a tool result once, at the turn it
was produced (``topology.py``'s ``tool_result_chars``/
``tool_result_chars_by_tool``, ``context_budget.py``'s composition
tables). That undercounts its real cost: once a tool result lands in the
transcript at turn ``i``, the harness's prompt cache carries it forward
on every later turn as part of the cached prefix -- re-read (billed at
the flat ``cache_read`` rate) when the prefix survived, or re-written
(billed at the ``cache_write_5m``/``cache_write_1h`` rate) when a
TTL-expiry or content change forced a re-cache -- until a
``COMPACT_BOUNDARY`` finally drops it from context, or the transcript
simply ends first. A single 20k-token ``Read`` at turn 2 of a 40-turn
session is paid for, in the caching sense, up to 38 times over -- this
module is what puts a dollar figure on that.

Depends only on ``model.py`` (frozen contract) and ``pricing.py``'s
``price_turn`` -- the same two dependencies ``ttl.py`` declares, since
this module reuses ``price_turn`` exactly the way ``ttl.py`` does (see
:func:`_turn_read_write_rates`) rather than reading a rate card field
directly. Every project lever this module ends up recommending is a
*workflow* change ("truncate/summarise this before it enters context"),
never a settings key -- there's nothing here for ``render_patch_set`` to
patch.

The model, precisely
-------------------

For one tool result that entered context at priced turn ``i`` (an entry
in ``Turn.tool_result_chars_by_tool``, sized in chars and converted to
tokens via the project's standing chars/4 approximation -- see
:data:`_CHARS_PER_TOKEN_APPROX`):

- **Turns carried**: every priced turn strictly after ``i`` up to and
  including the last priced turn *before* the next ``COMPACT_BOUNDARY``
  that follows ``i`` (a boundary drops the whole context, this result
  included -- nothing past it is still being carried), or the
  transcript's own last priced turn when no such boundary exists. A
  result entering at turn 2 of a 10-turn transcript with no compaction
  carries through turn 10 (8 turns); the same result with a compaction
  landing at turn 6 carries only through turn 5 (3 turns).
- **Carry tokens**: the result's own token size, multiplied by the
  number of turns it carried (a turn re-reading it once and a turn
  re-reading it eight times both cost real money each time).
- **Carry cost**: summed turn by turn, not as a single flat rate.
  Each later turn's own observed ``cache_read_tokens``/
  ``cache_creation_tokens`` split tells us what fraction of *that
  turn's* whole cached prefix was read back unchanged versus rewritten
  -- this module applies that same fraction to the carried result's own
  token count (there is no field anywhere that attributes a turn's
  aggregate cache volume back to which earlier write produced which
  slice of it, so a proportional split is the closest approximation the
  data supports) and prices the two slices via ``price_turn`` at that
  turn's own resolved model rate: the read slice at the flat
  ``cache_read`` rate, the write slice at the turn's own blended
  ``cache_write_5m``/``cache_write_1h`` rate (derived from
  ``price_turn``'s own output, never a raw rate-card lookup -- see
  :func:`_turn_read_write_rates`, matching ``ttl.py``'s own
  ``_write_cost``/``_cache_tokens_at_input_rate`` convention of always
  pricing through ``price_turn``).
- **Avoidable-if-truncated**: because carry cost is exactly linear in
  the result's own token count (the per-turn rate mix doesn't depend on
  the result's size), the saving from having capped a result at ``T``
  tokens is exact, not simulated: ``carry_cost * (1 - T / tokens)`` for
  every result whose size exceeds ``T`` -- see
  :func:`_truncation_saving`.

Granularity note (privacy): the finest per-call size the frozen ``Turn``
contract exposes is ``tool_result_chars_by_tool`` -- tool name -> total
chars for *that turn's* calls to that tool, not one entry per individual
tool_use_id/call (see ``model.py``'s and ``parse.py``'s own docstrings on
that field). So one "result" in this module's tables is one
``(turn, tool name)`` entry: two ``Read`` calls answered within the same
turn are already summed together by the time this module ever sees them.
This is a coarsening, not a privacy exception -- no content, path, or
command is read here, only tool names (already privacy-cleared
elsewhere in this codebase, e.g. ``Turn.tool_names``) and integers.

Deviation from the brief, reported rather than made silently (project
convention, see ``model.py``'s module docstring): ``compute_carry``'s
required signature is ``(results, rates) -> CarryStats`` with no
thresholds parameter; this module adds an optional, defaulted
``thresholds: CarryThresholds | None = None`` third parameter (the same
"every existing/required call site keeps working unchanged" convention
``ttl.py``'s/``limits.py``'s own ``thresholds`` parameters follow) so a
caller can tune ``top_n``/``truncation_tokens`` without a second
function.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .model import Column, EventKind, Recommendation, ReportModel, Section, Table, TranscriptResult, Turn, agent_type_label
from .pricing import ModelRates, ResolvedRates, price_turn

#: No tokenizer is run over transcript content (privacy rule); tool-result
#: sizes are only ever known in characters. Duplicated from
#: ``topology._CHARS_PER_TOKEN_APPROX`` (also duplicated by
#: ``context_budget.py``) rather than imported -- this project's own
#: established convention for this one constant (see both modules' own
#: docstrings), so a reader of any one of these files sees the whole
#: approximation inline instead of chasing an import.
_CHARS_PER_TOKEN_APPROX = 4

#: This module's own modelling assumptions, printed verbatim in the
#: report's "## Assumptions" block (``ReportMeta.assumptions``) alongside
#: ``ttl.ASSUMPTIONS``/``recache.ASSUMPTIONS``/``limits.ASSUMPTIONS`` --
#: same convention, see those modules' own ``ASSUMPTIONS`` constants.
ASSUMPTIONS: tuple[str, ...] = (
    "turns carried is observed directly from Turn.turn_index (how many "
    "later priced turns actually exist), never estimated",
    "a COMPACT_BOUNDARY event preceding a later turn ends every "
    "in-flight result's carry at that point -- nothing at or past the "
    "boundary turn is priced as still-cached",
    "each later turn's carry cost is split between the model's flat "
    "cache_read rate and its blended cache_write (5m/1h) rate in "
    "proportion to that turn's own observed cache_read_tokens/"
    "cache_creation_tokens split -- an approximation, since no field "
    "attributes a turn's aggregate cache volume back to which earlier "
    "write produced which slice of it",
    "tokens are chars / 4 (this project's standing chars-to-tokens "
    "approximation, see topology._CHARS_PER_TOKEN_APPROX), not a real "
    "tokenizer",
    "one 'result' is one (turn, tool name) entry in "
    "Turn.tool_result_chars_by_tool -- multiple calls to the same tool "
    "within one turn are already summed together, the finest per-call "
    "grain the privacy-safe Turn contract exposes",
)

#: What ``price_turn``/rate resolution accepts: an already-resolved rate
#: (bare or wrapped), or ``None`` for an unresolved model (prices at
#: zero -- see ``pricing.price_turn``). Mirrors ``ttl.RatesArg``.
RatesArg = ModelRates | ResolvedRates | None

#: A turn-by-turn rate resolver, ``pricing.Pricing.resolve_model``'s own
#: signature. Mirrors ``ttl.RatesLookup`` -- a mixed-model transcript
#: (subagents can run a different model, a user can switch mid-session)
#: must price each turn at its own observed model's rate.
RatesLookup = Callable[[str], RatesArg]


def _as_lookup(rates: "RatesArg | RatesLookup") -> RatesLookup:
    """Normalise ``rates`` to a per-turn lookup. Duplicated from
    ``ttl._as_lookup`` (not imported -- this project's own convention of
    small per-module helpers, see e.g. ``cli._priced_turns``'s docstring
    for the same rationale) since a single already-resolved rate is
    still the common case in this module's own tests."""
    if callable(rates):
        return rates
    return lambda _model_id: rates


@dataclass(slots=True)
class CarryThresholds:
    """Every tunable number this module's own logic depends on, in one
    config-driven object (mirrors ``ttl.TtlThresholds``/
    ``limits.LimitThresholds``'s own ``from_config``/``describe``
    convention).
    """

    #: A carried result at or above this many tokens counts as "big" for
    #: the truncation-savings table's own header row and the
    #: ``tool-output-carry`` rule's evidence -- see :meth:`describe`.
    big_result_tokens: float = 8_000.0
    #: ``tool-output-carry`` fires for a tool whose carry cost exceeds
    #: this share (percentage points) of the corpus's total cache
    #: volume -- see :data:`RULES`.
    carry_share_pct: float = 25.0
    #: How many of the single largest carried results
    #: ``carry_top_results`` lists.
    top_n: int = 10
    #: The token caps "avoidable if truncated" savings are computed
    #: against -- one ``carry_truncation_savings`` row per value.
    truncation_tokens: tuple[int, ...] = (2_000, 8_000)
    #: ``tool-output-carry`` never fires for a tool with fewer carried
    #: results than this -- the per-rule sample floor (mirrors
    #: ``recommend.RecommendThresholds``'s own per-rule minimums; this
    #: module keeps its own copy rather than importing that class, since
    #: ``recommend.py`` is a downstream consumer of this module, not a
    #: dependency of it).
    min_sample_results: int = 5

    @classmethod
    def from_config(cls, config: dict | None) -> "CarryThresholds":
        """Build thresholds from a config dict, keeping this class's
        defaults for any key that's absent or of the wrong shape (same
        posture as ``ttl.TtlThresholds.from_config``/
        ``limits.LimitThresholds.from_config``)."""
        data = config or {}
        if not isinstance(data, dict):
            data = {}
        nested = data.get("thresholds")
        if isinstance(nested, dict):
            data = nested
        kwargs: dict = {}
        if "big_result_tokens" in data:
            try:
                kwargs["big_result_tokens"] = float(data["big_result_tokens"])
            except (TypeError, ValueError):
                pass
        if "carry_share_pct" in data:
            try:
                kwargs["carry_share_pct"] = float(data["carry_share_pct"])
            except (TypeError, ValueError):
                pass
        if "top_n" in data:
            try:
                kwargs["top_n"] = int(data["top_n"])
            except (TypeError, ValueError):
                pass
        if "truncation_tokens" in data:
            raw = data["truncation_tokens"]
            if isinstance(raw, (list, tuple)):
                try:
                    kwargs["truncation_tokens"] = tuple(int(x) for x in raw)
                except (TypeError, ValueError):
                    pass
        if "min_sample_results" in data:
            try:
                kwargs["min_sample_results"] = int(data["min_sample_results"])
            except (TypeError, ValueError):
                pass
        return cls(**kwargs)

    def describe(self) -> list[str]:
        """One sentence per threshold, for the report's thresholds block
        and this module's own table notes -- same convention as
        ``ttl.TtlThresholds.describe``/``limits.LimitThresholds.describe``."""
        return [
            f"big_result_tokens = {self.big_result_tokens:,.0f} tokens: the "
            "size a carried result is called 'big' at, for this module's own "
            "reporting.",
            f"carry_share_pct = {self.carry_share_pct:.1f}%: the "
            "tool-output-carry rule fires for a tool whose carry cost exceeds "
            "this share of the corpus's total cache volume.",
            f"top_n = {self.top_n}: how many of the single largest carried "
            "results carry_top_results lists.",
            f"truncation_tokens = {list(self.truncation_tokens)}: the token "
            "caps the avoidable-if-truncated savings table is computed "
            "against.",
            f"min_sample_results = {self.min_sample_results}: the "
            "tool-output-carry rule never fires for a tool with fewer "
            "carried results than this.",
        ]


_DEFAULT_THRESHOLDS = CarryThresholds()


def _priced_turns(turns: list[Turn]) -> list[Turn]:
    """The subset of ``turns`` that are actually priced -- see
    ``ttl._priced_turns``'s/``topology._priced_turns``'s identical
    docstring (duplicated rather than imported, same convention)."""
    return [t for t in turns if t.turn_index > 0]


def _turn_read_write_rates(turn: Turn, rates: RatesArg) -> tuple[float, float]:
    """Per-token cache_read and (blended 5m/1h) cache_write USD rates for
    one turn's own observed split, derived from ``price_turn``'s own
    output rather than a raw rate-card lookup -- so any geo or
    long-context multiplier the turn actually observed is folded in
    automatically. Mirrors ``ttl.py``'s own "always price through
    price_turn" convention (see e.g. ``ttl._write_cost``).

    Either rate is ``0.0`` when the turn carried no tokens of that kind
    (nothing to divide by) or the model couldn't be resolved
    (``price_turn`` prices an unresolved model at zero throughout).
    """
    breakdown = price_turn(turn, rates)
    read_rate = breakdown.cache_read_cost / turn.cache_read_tokens if turn.cache_read_tokens > 0 else 0.0
    write_rate = (
        breakdown.cache_write_cost / turn.cache_creation_tokens if turn.cache_creation_tokens > 0 else 0.0
    )
    return read_rate, write_rate


def _carry_cost_for_turn(turn: Turn, result_tokens: float, rates: RatesArg) -> float:
    """The dollar cost of carrying ``result_tokens`` worth of an earlier
    result through this one later turn: ``result_tokens`` split between
    the read/write rate in proportion to the turn's own observed
    ``cache_read_tokens``/``cache_creation_tokens`` mix (see the module
    docstring's "carry cost" paragraph). ``0.0`` when the turn carried no
    cache volume at all (e.g. a turn that fell entirely outside any
    cached prefix) or ``result_tokens`` is non-positive.
    """
    cache_volume = turn.cache_read_tokens + turn.cache_creation_tokens
    if cache_volume <= 0 or result_tokens <= 0:
        return 0.0
    read_rate, write_rate = _turn_read_write_rates(turn, rates)
    read_share = turn.cache_read_tokens / cache_volume
    write_share = turn.cache_creation_tokens / cache_volume
    return result_tokens * (read_share * read_rate + write_share * write_rate)


@dataclass(slots=True)
class CarriedResult:
    """One tool result's own carry record within a single transcript --
    the row-level unit :func:`compute_carry` aggregates from and
    :func:`build_section`'s ``carry_top_results`` table lists directly.
    Holds no content, path, or command -- only a tool name, an agent
    type, and integers/floats.
    """

    tool: str
    agent_type: str
    entry_turn_index: int
    #: The result's own size, in tokens (chars / 4 -- see
    #: :data:`_CHARS_PER_TOKEN_APPROX`), rounded once here so every
    #: downstream sum is over whole numbers.
    tokens: int
    turns_carried: int
    #: ``tokens * turns_carried``.
    carry_tokens: int
    carry_cost_usd: float


def _boundary_turn_indices(priced: list[Turn]) -> list[int]:
    """Every priced turn index whose ``preceding_event_kinds`` carries a
    ``COMPACT_BOUNDARY`` -- the same detection ``topology.py``'s own
    rediscovery-window code uses (see ``topology._add_redundant_work``),
    duplicated here rather than imported."""
    return sorted(t.turn_index for t in priced if EventKind.COMPACT_BOUNDARY in t.preceding_event_kinds)


def _carry_end_index(entry_index: int, boundary_indices: list[int], last_index: int) -> int:
    """The last priced turn index a result entering at ``entry_index``
    is still carried through: the turn immediately before the first
    compaction boundary that follows ``entry_index``, or ``last_index``
    (the transcript's own last priced turn) when no such boundary
    exists."""
    for boundary in boundary_indices:
        if boundary > entry_index:
            return boundary - 1
    return last_index


def _extract_results(result: TranscriptResult, lookup: RatesLookup) -> list[CarriedResult]:
    """Every carried result in one transcript, per the module docstring's
    model. Returns ``[]`` for a transcript with no priced turns."""
    priced = _priced_turns(result.turns)
    if not priced:
        return []
    agent_type = agent_type_label(result)
    boundary_indices = _boundary_turn_indices(priced)
    last_index = priced[-1].turn_index
    by_index = {t.turn_index: t for t in priced}

    out: list[CarriedResult] = []
    for turn in priced:
        if not turn.tool_result_chars_by_tool:
            continue
        end_index = _carry_end_index(turn.turn_index, boundary_indices, last_index)
        turns_carried = max(0, end_index - turn.turn_index)
        for tool_name, chars in turn.tool_result_chars_by_tool.items():
            if chars <= 0:
                continue
            tokens = round(chars / _CHARS_PER_TOKEN_APPROX)
            if tokens <= 0:
                continue
            cost = 0.0
            for idx in range(turn.turn_index + 1, end_index + 1):
                later_turn = by_index.get(idx)
                if later_turn is None:
                    continue
                cost += _carry_cost_for_turn(later_turn, tokens, lookup(later_turn.model))
            out.append(
                CarriedResult(
                    tool=tool_name,
                    agent_type=agent_type,
                    entry_turn_index=turn.turn_index,
                    tokens=tokens,
                    turns_carried=turns_carried,
                    carry_tokens=tokens * turns_carried,
                    carry_cost_usd=cost,
                )
            )
    return out


@dataclass(slots=True)
class CarryByKeyStats:
    """Rolled-up carry stats for one tool name (or one agent type) -- one
    row of ``carry_by_tool``/``carry_by_agent_type``."""

    key: str
    result_count: int
    tokens_entered: int
    mean_turns_carried: float | None
    carry_tokens: int
    carry_cost_usd: float
    #: This key's carry_tokens as a percentage of the corpus's total
    #: cache volume (``CarryStats.total_cache_volume_tokens``) -- an
    #: attribution share, not a partition (carry_tokens double-counts by
    #: construction, see the module docstring), so shares across every
    #: tool/agent-type row do not sum to 100%.
    share_of_cache_volume_pct: float


@dataclass(slots=True)
class TruncationSaving:
    """One ``carry_truncation_savings`` row: what capping every result
    above ``truncate_to_tokens`` would have saved, corpus-wide."""

    truncate_to_tokens: int
    results_affected: int
    tokens_saved: int
    usd_saved: float


@dataclass(slots=True)
class CarryStats:
    """Everything :func:`compute_carry` computes over a corpus of
    transcripts, ready for :func:`build_section` to render."""

    transcripts: int
    total_results: int
    #: Sum of ``cache_read_tokens + cache_creation_tokens`` over every
    #: priced turn in every transcript given to ``compute_carry`` -- the
    #: denominator for ``share_of_cache_volume_pct``.
    total_cache_volume_tokens: float
    total_carry_cost_usd: float
    #: Priced turns whose model the rates lookup could not resolve
    #: (``price_turn``'s ``model_known=False`` path) -- counted rather
    #: than silently pricing that turn's carry contribution at zero
    #: without a trace (fix item 2's convention in ``ttl.py``).
    unpriced_turns: int
    by_tool: list[CarryByKeyStats]
    by_agent_type: list[CarryByKeyStats]
    top_results: list[CarriedResult]
    truncation_savings: list[TruncationSaving]


def _fold(acc: dict[str, list], key: str, item: CarriedResult) -> None:
    acc.setdefault(key, []).append(item)


def _finalize_by_key(grouped: dict[str, list[CarriedResult]], total_cache_volume: float) -> list[CarryByKeyStats]:
    rows: list[CarryByKeyStats] = []
    for key, items in grouped.items():
        turns_carried_values = [i.turns_carried for i in items]
        carry_tokens = sum(i.carry_tokens for i in items)
        rows.append(
            CarryByKeyStats(
                key=key,
                result_count=len(items),
                tokens_entered=sum(i.tokens for i in items),
                mean_turns_carried=(
                    sum(turns_carried_values) / len(turns_carried_values) if turns_carried_values else None
                ),
                carry_tokens=carry_tokens,
                carry_cost_usd=sum(i.carry_cost_usd for i in items),
                share_of_cache_volume_pct=(
                    100.0 * carry_tokens / total_cache_volume if total_cache_volume > 0 else 0.0
                ),
            )
        )
    rows.sort(key=lambda r: (-r.carry_cost_usd, r.key))
    return rows


def _truncation_saving(results: list[CarriedResult], truncate_to_tokens: int) -> TruncationSaving:
    """The exact saving from having capped every carried result above
    ``truncate_to_tokens`` at that limit -- exact, not simulated, because
    ``carry_cost_usd`` is linear in a result's own token count (the
    per-turn read/write rate mix doesn't depend on the result's size):
    a result of ``tokens`` costing ``carry_cost_usd`` to carry would have
    cost ``carry_cost_usd * (truncate_to_tokens / tokens)`` capped at
    ``truncate_to_tokens``, so the saving is
    ``carry_cost_usd * (1 - truncate_to_tokens / tokens)``. Likewise for
    tokens: ``(tokens - truncate_to_tokens) * turns_carried``.
    """
    affected = [r for r in results if r.tokens > truncate_to_tokens]
    tokens_saved = sum((r.tokens - truncate_to_tokens) * r.turns_carried for r in affected)
    usd_saved = sum(
        r.carry_cost_usd * (1.0 - truncate_to_tokens / r.tokens) for r in affected if r.tokens > 0
    )
    return TruncationSaving(
        truncate_to_tokens=truncate_to_tokens,
        results_affected=len(affected),
        tokens_saved=tokens_saved,
        usd_saved=usd_saved,
    )


def compute_carry(
    results: list[TranscriptResult],
    rates: "RatesArg | RatesLookup",
    thresholds: CarryThresholds | None = None,
) -> CarryStats:
    """Compute every carry metric the module docstring describes over
    ``results`` (a corpus's worth of top-level and subagent
    ``TranscriptResult``\\ s -- the caller decides the window, same as
    ``topology.TopologyStats``/``ttl.TtlStats``).

    ``rates`` accepts a single already-resolved rate or a per-model
    :data:`RatesLookup` callable (e.g. ``pricing.Pricing.resolve_model``),
    normalised via :func:`_as_lookup` -- the same shape ``ttl.py``'s
    entry points accept, so a mixed-model corpus prices each turn at its
    own observed model's rate. ``thresholds`` (see the module docstring's
    deviation note) defaults to :data:`_DEFAULT_THRESHOLDS`.

    Unlike ``TopologyStats``/``TtlStats``/``LimitStats``, this is a plain
    function rather than an incremental accumulator: ``carry_top_results``
    needs a single global sort over every carried result once every
    transcript has been processed, so there is no benefit to an
    ``add()``-per-transcript shape here.
    """
    th = thresholds or _DEFAULT_THRESHOLDS
    lookup = _as_lookup(rates)

    all_results: list[CarriedResult] = []
    total_cache_volume = 0.0
    unpriced_turns = 0
    transcripts = 0

    for result in results:
        transcripts += 1
        priced = _priced_turns(result.turns)
        for t in priced:
            total_cache_volume += t.cache_read_tokens + t.cache_creation_tokens
            if lookup(t.model) is None:
                unpriced_turns += 1
        all_results.extend(_extract_results(result, lookup))

    by_tool_groups: dict[str, list[CarriedResult]] = {}
    by_agent_groups: dict[str, list[CarriedResult]] = {}
    for item in all_results:
        _fold(by_tool_groups, item.tool, item)
        _fold(by_agent_groups, item.agent_type, item)

    top_results = sorted(all_results, key=lambda r: r.carry_cost_usd, reverse=True)[: th.top_n]

    return CarryStats(
        transcripts=transcripts,
        total_results=len(all_results),
        total_cache_volume_tokens=total_cache_volume,
        total_carry_cost_usd=sum(r.carry_cost_usd for r in all_results),
        unpriced_turns=unpriced_turns,
        by_tool=_finalize_by_key(by_tool_groups, total_cache_volume),
        by_agent_type=_finalize_by_key(by_agent_groups, total_cache_volume),
        top_results=top_results,
        truncation_savings=[_truncation_saving(all_results, t) for t in th.truncation_tokens],
    )


# -- report section -----------------------------------------------------------


def _by_key_table(name: str, title: str, key_label: str, rows: list[CarryByKeyStats]) -> Table:
    columns = [
        Column(key="key", label=key_label, kind="str"),
        Column(key="result_count", label="Carried results", kind="int"),
        Column(key="tokens_entered", label="Tokens entered", kind="tokens"),
        Column(key="mean_turns_carried", label="Mean turns carried", kind="float"),
        Column(key="carry_tokens", label="Carry tokens (size x turns)", kind="tokens"),
        Column(key="carry_cost_usd", label="Carry cost", kind="money"),
        Column(key="share_of_cache_volume_pct", label="Share of cache volume", kind="pct"),
    ]
    table_rows = [
        [
            r.key,
            r.result_count,
            r.tokens_entered,
            r.mean_turns_carried,
            r.carry_tokens,
            r.carry_cost_usd,
            r.share_of_cache_volume_pct,
        ]
        for r in rows
    ]
    return Table(
        name=name,
        title=title,
        columns=columns,
        rows=table_rows,
        notes=[
            "share_of_cache_volume_pct is an attribution share, not a "
            "partition: carry_tokens double-counts by construction (the "
            "same physical cache read on a given turn also carries every "
            "other still-live result), so rows do not sum to 100%.",
        ],
    )


def _build_top_results_table(stats: CarryStats) -> Table:
    columns = [
        Column(key="tool", label="Tool", kind="str"),
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="tokens", label="Tokens", kind="tokens"),
        Column(key="turns_carried", label="Turns carried", kind="int"),
        Column(key="carry_cost_usd", label="Carry cost", kind="money"),
    ]
    rows = [
        [r.tool, r.agent_type, r.tokens, r.turns_carried, r.carry_cost_usd]
        for r in stats.top_results
    ]
    return Table(
        name="carry_top_results",
        title="Single most expensive carried results",
        columns=columns,
        rows=rows,
        notes=[
            "The single largest individual carried results by carry cost, "
            "corpus-wide -- no content, no path, no command: only the tool "
            "name, the agent type, its size, how many turns it carried, "
            "and what that cost.",
        ],
    )


def _build_truncation_table(stats: CarryStats, th: CarryThresholds) -> Table:
    columns = [
        Column(key="truncate_to_tokens", label="Truncate to (tokens)", kind="int"),
        Column(key="results_affected", label="Results affected", kind="int"),
        Column(key="tokens_saved", label="Tokens saved", kind="tokens"),
        Column(key="usd_saved", label="Saving", kind="money"),
    ]
    rows = [
        [str(s.truncate_to_tokens), s.results_affected, s.tokens_saved, s.usd_saved]
        for s in stats.truncation_savings
    ]
    return Table(
        name="carry_truncation_savings",
        title="Avoidable carry cost if large results were truncated",
        columns=columns,
        rows=rows,
        notes=[
            "Exact, not simulated: carry cost is linear in a result's own "
            "token count (the per-turn read/write rate mix doesn't depend "
            "on the result's size), so the saving from capping every "
            "result above the threshold at that limit is computed "
            "directly rather than re-run through a hypothetical.",
            f"big_result_tokens (this module's own 'big' threshold) = "
            f"{th.big_result_tokens:,.0f} tokens -- see the by-tool/"
            "by-agent-type tables' own result counts for how many results "
            "clear it.",
        ],
    )


def build_section(stats: CarryStats, thresholds: CarryThresholds | None = None) -> Section:
    """Render ``stats`` into the ``carry`` report section: per-tool and
    per-agent-type roll-ups, the single most expensive carried results,
    and the avoidable-if-truncated savings table.
    """
    th = thresholds or _DEFAULT_THRESHOLDS
    tables = [
        _by_key_table("carry_by_tool", "Context carry cost by tool", "Tool", stats.by_tool),
        _by_key_table(
            "carry_by_agent_type", "Context carry cost by agent type", "Agent type", stats.by_agent_type
        ),
        _build_top_results_table(stats),
        _build_truncation_table(stats, th),
    ]
    notes = list(ASSUMPTIONS) + [f"Thresholds: {' '.join(th.describe())}"]
    if stats.unpriced_turns:
        notes.append(
            f"{stats.unpriced_turns} priced turn(s) had a model this report's rate card couldn't "
            "resolve -- their own carry contribution prices at zero rather than vanishing silently."
        )
    return Section(key="carry", title="Context carry cost per tool", tables=tables, notes=notes)


# -- recommendation rule -------------------------------------------------


def _section(report: ReportModel, key: str) -> Section | None:
    """Duplicated from ``recommend._section`` (not imported -- see the
    module docstring's convention note; ``recommend.py`` is this
    module's own downstream consumer, not something this module should
    depend on)."""
    for section in report.sections:
        if section.key == key:
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


def _evidence(label: str, value, section_key: str, table_name: str, row_key) -> tuple:
    """Duplicated from ``recommend._evidence`` -- same ``(label, value,
    "<section_key>.<table_name>", row_key)`` evidence contract every
    other rule in ``recommend.py`` honours, so a caller's evidence-exists
    walk (``tests/test_recommend.py``'s own) resolves these tuples
    exactly the same way it resolves that module's own."""
    return (label, value, f"{section_key}.{table_name}", row_key)


def _rule_tool_output_carry(report: ReportModel, th: CarryThresholds) -> list[Recommendation]:
    """``tool-output-carry``: a tool whose carried results make up more
    than ``th.carry_share_pct`` of the corpus's total cache volume is
    worth truncating at the source, not just noting -- every later turn
    re-pays for that same output being in context. Fires per tool (a
    corpus can have more than one offender), gated on that tool's own
    ``result_count`` clearing ``th.min_sample_results`` so a single huge
    one-off result doesn't read as a systemic pattern.

    Not part of ``recommend.RULES`` (this module doesn't import or
    modify ``recommend.py`` -- see the module docstring); a caller wires
    ``carry.RULES`` into ``recommend.recommend``'s own rule list
    alongside its existing rules.
    """
    table = _table(report, "carry", "carry_by_tool")
    if table is None:
        return []
    count_idx = _col_index(table, "result_count")
    cost_idx = _col_index(table, "carry_cost_usd")
    share_idx = _col_index(table, "share_of_cache_volume_pct")
    if count_idx is None or cost_idx is None or share_idx is None:
        return []

    truncation_table = _table(report, "carry", "carry_truncation_savings")
    target_t = int(th.big_result_tokens)
    truncation_row_key = str(target_t) if truncation_table and _row(truncation_table, str(target_t)) else None
    if truncation_row_key is None and th.truncation_tokens:
        truncation_row_key = str(th.truncation_tokens[-1])

    out: list[Recommendation] = []
    for row in table.rows:
        tool = row[0]
        share = row[share_idx]
        count = row[count_idx]
        if not isinstance(share, (int, float)) or share <= th.carry_share_pct:
            continue
        if not isinstance(count, (int, float)) or count < th.min_sample_results:
            continue
        cost = row[cost_idx]

        evidence = [
            _evidence("Carry cost share of cache volume", share, "carry", "carry_by_tool", tool),
            _evidence("Carry cost", cost, "carry", "carry_by_tool", tool),
            _evidence("Carried results", count, "carry", "carry_by_tool", tool),
        ]
        saving_clause = ""
        if truncation_row_key is not None:
            saving_row = _row(truncation_table, truncation_row_key)
            if saving_row is not None:
                usd_idx = _col_index(truncation_table, "usd_saved")
                if usd_idx is not None:
                    saving = saving_row[usd_idx]
                    evidence.append(
                        _evidence(
                            f"Saving if truncated to {truncation_row_key} tokens",
                            saving,
                            "carry",
                            "carry_truncation_savings",
                            truncation_row_key,
                        )
                    )
                    if isinstance(saving, (int, float)):
                        saving_clause = (
                            f" Capping {tool}'s output at {truncation_row_key} tokens would have "
                            f"saved approximately ${saving:.2f} in carry cost alone."
                        )

        out.append(
            Recommendation(
                id="tool-output-carry",
                severity="advice",
                category="workflow",
                archetypes=(),
                title=f"{tool}'s output dominates context carried across turns",
                action=(
                    f"{tool} results made up {share:.1f}% of this corpus's cache volume once carry "
                    f"cost is counted (${cost:.2f} paid to keep them cached turn after turn). Pipe "
                    "long Bash/PowerShell output through head/tail or a digest script, prefer Grep "
                    "over Read for large files, and cap agent report length before it enters "
                    f"context.{saving_clause}"
                ),
                lever=None,
                evidence=evidence,
            )
        )
    return out


#: One rule function, ``(report, thresholds) -> list[Recommendation]`` --
#: the same baseline signature ``recommend.py``'s own sample-gated rules
#: use (e.g. ``_rule_cache_read_dominance``, ``_rule_limit_pressure``). A
#: caller (``recommend.recommend``, or a test) evaluates every entry
#: here the same way it evaluates its own ``_rule_*`` functions.
RULES: list[Callable[[ReportModel, CarryThresholds], list[Recommendation]]] = [_rule_tool_output_carry]


__all__ = [
    "ASSUMPTIONS",
    "CarryThresholds",
    "CarriedResult",
    "CarryByKeyStats",
    "TruncationSaving",
    "CarryStats",
    "compute_carry",
    "build_section",
    "RULES",
]
