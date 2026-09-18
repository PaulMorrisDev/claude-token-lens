"""RE-CACHE detection and reporting: which turns paid to re-write a prompt
prefix that should have been a cheap cache hit, and why.

This is WP3 of the project plan (see plan "RE-CACHE" section and Appendix
A5's ``notification-invalidation``/``long-tool-waits`` recommendation
thresholds, which read the tables built here). It depends only on
``model.py`` and ``pricing.py``.

Three things live here:

- :class:`RecacheThresholds` — the four tunable numbers that decide
  whether a turn counts as a re-cache, plus its two signatures.
- :func:`detect` — the pure classifier: given a transcript's turns and a
  set of thresholds, which turns are re-caches, and which signature.
- :func:`gap_bucket` — buckets a turn's inter-turn gap for the gap-cause
  table.
- :class:`RecacheStats` / :func:`build_section` — a corpus-wide
  accumulator and the ``Section`` it renders into: summary, signature
  split, gap buckets, preceding tool, top command prefixes, primary
  cause, event co-occurrence, attachment sub-split, by-agent-type, and
  huge-context cache-read volume.

Definitions (plan "RE-CACHE" section):

A turn is a **re-cache** when it is not the transcript's first priced
turn, its context is above ``ctx_floor``, and the fraction of that
context actually served from cache read falls below ``cr_ratio`` — i.e.
the model had to pay to write most of its own context again instead of
reading it from a warm cache entry. Every re-cache turn gets one of two
**signatures**:

- ``full-expiry`` — ``cache_read_tokens`` is below ``full_expiry_cr``: the
  cache entry had essentially nothing left to hit, consistent with its
  TTL having fully expired since the previous turn.
- ``prefix-invalidated`` — ``cache_read_tokens`` is between
  ``full_expiry_cr`` and ``cr_ratio * ctx``: there was a partial hit, so
  the TTL had not expired but something upstream of the cached prefix
  changed (a notification, an attachment, a model switch, ...) and broke
  it anyway.

A turn's **avoidable cost** is what its own cache-creation tokens cost at
the write rate they were actually billed at, minus what those same
tokens would have cost at the flat cache-read rate had the cache not
been invalidated — computed with two ``pricing.price_turn`` calls on the
same turn so every other cost component (input, output, the turn's own
already-observed cache reads) cancels out of the subtraction exactly.

Privacy: every field this module reads off a ``Turn`` (tool names, event
kinds, a ``<=40``-char command prefix, token counts) is already
privacy-clean per ``model.py``'s contract; this module never adds a new
field that could hold message text, a full path, or a full command.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Callable, Sequence

from .model import Column, EventKind, Section, Table, TranscriptResult, Turn
from .pricing import ModelRates, Pricing, ResolvedRates, price_turn

#: Bucket labels, in report order. "unknown" covers a turn with no
#: measurable gap (the transcript's first priced turn, or a timestamp
#: parse failure — see parse.py's ``Diagnostics.timestamp_parse_failures``).
GAP_BUCKETS: tuple[str, ...] = ("<1m", "1-5m", "5-15m", "15-60m", ">60m", "unknown")

#: The two re-cache signatures, in report order.
SIGNATURES: tuple[str, ...] = ("full-expiry", "prefix-invalidated")


@dataclass(slots=True)
class RecacheThresholds:
    """The four tunable numbers that decide re-cache detection (plan
    Appendix A5's defaults). All overridable via ``config.toml``'s
    ``[thresholds]`` table or CLI flags — :meth:`from_config` reads
    either a flat dict of these four keys or a full config dict with a
    nested ``thresholds`` table, so a caller can pass either shape.
    """

    #: A turn's ``ctx`` must exceed this before it can ever be flagged,
    #: however low its cache-read ratio — a small early-conversation turn
    #: re-writing a small prefix isn't worth reporting on.
    ctx_floor: int = 20_000
    #: A turn is flagged when ``cache_read_tokens < cr_ratio * ctx``.
    cr_ratio: float = 0.2
    #: A flagged turn is signature "full-expiry" when
    #: ``cache_read_tokens < full_expiry_cr``, else "prefix-invalidated".
    full_expiry_cr: int = 2_000
    #: The context-size threshold for the huge-context cache-read-volume
    #: table (plan: "36.5% of recent top-level turns had ctx > 200k" —
    #: reported as a context-hygiene metric, not a pricing surcharge).
    huge_ctx: int = 200_000

    @classmethod
    def from_config(cls, config: dict | None) -> "RecacheThresholds":
        """Build thresholds from a config dict, keeping this class's
        defaults for any key that's absent or of the wrong shape.

        Accepts either a flat dict of the four field names directly, or
        a full ``config.toml``-shaped dict with a nested ``thresholds``
        table (``{"thresholds": {"ctx_floor": ...}}``) — whichever a
        caller happens to have loaded. Unknown keys are ignored.
        """
        data = config or {}
        if not isinstance(data, dict):
            data = {}
        nested = data.get("thresholds")
        if isinstance(nested, dict):
            data = nested

        kwargs: dict = {}
        if "ctx_floor" in data:
            kwargs["ctx_floor"] = int(data["ctx_floor"])
        if "cr_ratio" in data:
            kwargs["cr_ratio"] = float(data["cr_ratio"])
        if "full_expiry_cr" in data:
            kwargs["full_expiry_cr"] = int(data["full_expiry_cr"])
        if "huge_ctx" in data:
            kwargs["huge_ctx"] = int(data["huge_ctx"])
        return cls(**kwargs)

    def describe(self) -> list[str]:
        """One sentence per threshold, for the report's thresholds block
        (and this module's table notes)."""
        return [
            f"ctx_floor = {self.ctx_floor:,} tokens: a turn is never "
            "flagged as a re-cache unless its context exceeds this.",
            f"cr_ratio = {self.cr_ratio:.2f}: a turn is flagged when "
            "cache_read_tokens falls below this fraction of ctx.",
            f"full_expiry_cr = {self.full_expiry_cr:,} tokens: a flagged "
            "turn is signature full-expiry when cache_read_tokens falls "
            "below this, else prefix-invalidated.",
            f"huge_ctx = {self.huge_ctx:,} tokens: the threshold for the "
            "huge-context cache-read-volume table.",
        ]


def detect(turns: Sequence[Turn], th: RecacheThresholds) -> list[Turn]:
    """Return copies of every re-cache turn in ``turns``, with
    ``is_recache`` and ``recache_signature`` set.

    Pure: never mutates ``turns`` or any ``Turn`` in it. A turn qualifies
    when it is not the transcript's first priced turn (``turn_index >
    1``), is not synthetic, its ``ctx`` exceeds ``th.ctx_floor``, and its
    ``cache_read_tokens`` is below ``th.cr_ratio * ctx``. The returned
    list contains only the qualifying turns (as new objects, via
    ``dataclasses.replace``), in the same relative order as ``turns`` —
    callers that need the full turn list with these substituted in
    should index by ``message_id`` (unique within one transcript).
    """
    detected: list[Turn] = []
    for turn in turns:
        if turn.is_synthetic or turn.turn_index <= 1:
            continue
        if turn.ctx <= th.ctx_floor:
            continue
        if turn.cache_read_tokens >= th.cr_ratio * turn.ctx:
            continue
        signature = "full-expiry" if turn.cache_read_tokens < th.full_expiry_cr else "prefix-invalidated"
        detected.append(dataclasses.replace(turn, is_recache=True, recache_signature=signature))
    return detected


def gap_bucket(gap_s: float | None) -> str:
    """Bucket an inter-turn gap into one of :data:`GAP_BUCKETS`.

    ``None`` (no previous turn, or a timestamp parse failure) buckets as
    "unknown". The 5-minute boundary is inclusive on its lower side: a
    gap of exactly 300 seconds falls in "5-15m", not "1-5m" — each
    bucket's lower bound is inclusive, its upper bound exclusive.
    """
    if gap_s is None:
        return "unknown"
    if gap_s < 60:
        return "<1m"
    if gap_s < 300:
        return "1-5m"
    if gap_s < 900:
        return "5-15m"
    if gap_s < 3600:
        return "15-60m"
    return ">60m"


def _pct(part: float, whole: float) -> float:
    return 100.0 * part / whole if whole else 0.0


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _avoidable_cost(turn: Turn, rates: ResolvedRates | ModelRates | None) -> float:
    """The re-cache turn's avoidable cost: its own cache-creation tokens
    priced at the write rate they were actually billed at, minus those
    same tokens priced at the flat cache-read rate. Both calls hold
    every other component of the turn fixed, so input/output/already-
    observed cache-read cost cancel out of the subtraction exactly (see
    the module docstring's worked example).
    """
    if rates is None:
        return 0.0
    actual = price_turn(turn, rates)
    hypothetical_read = turn.cache_read_tokens + turn.cache_creation_tokens
    hypothetical = price_turn(turn, rates, write_split={"5m": 0, "1h": 0}, read_tokens=hypothetical_read)
    return actual.total - hypothetical.total


@dataclass(slots=True)
class _Record:
    """One priced turn folded into a :class:`RecacheStats` accumulator.
    ``turn`` is the re-cache-detected copy when it qualified, else the
    turn as parsed — so a single pass over ``RecacheStats.records``
    yields both the re-cache population (``turn.is_recache``) and the
    "all priced turns" control population in one list.
    """

    turn: Turn
    avoidable_cost: float
    agent_type: str
    group: str


class RecacheStats:
    """Accumulates re-cache facts across every transcript in a corpus (or
    a ``--group-by`` slice of one), for :func:`build_section` to render.

    ``group_key``, when given, is called once per :meth:`add` with the
    ``TranscriptResult`` and must return a short grouping label (plan:
    "so WP10 can pass session mode"). :func:`build_section` can then be
    called once per group by passing that same label as its ``group``
    argument, or once for the whole corpus by leaving it ``None``.
    """

    def __init__(
        self,
        th: RecacheThresholds,
        group_key: Callable[[TranscriptResult], str] | None = None,
    ) -> None:
        self.th = th
        self.group_key = group_key
        self.transcripts = 0
        #: group label -> transcripts folded into that group, so a
        #: group-filtered ``build_section`` reports the right transcript
        #: count instead of the corpus-wide total.
        self.transcripts_by_group: dict[str, int] = {}
        self.records: list[_Record] = []

    def add(
        self,
        result: TranscriptResult,
        rates_lookup: Callable[[str], ResolvedRates | ModelRates | None],
    ) -> None:
        """Fold one transcript's priced, non-synthetic turns into the
        accumulator, running :func:`detect` internally against
        ``self.th``. Every priced turn is recorded, not just the
        detected re-cache ones, so :func:`build_section` can compute
        both the re-cache tables and their all-priced-turns control
        columns from the same population.
        """
        self.transcripts += 1
        group = self.group_key(result) if self.group_key is not None else "all"
        self.transcripts_by_group[group] = self.transcripts_by_group.get(group, 0) + 1
        agent_type = result.meta.agent_type or "top-level"

        recache_by_id = {t.message_id: t for t in detect(result.turns, self.th)}
        for turn in result.turns:
            if turn.is_synthetic or turn.turn_index <= 0:
                continue
            effective = recache_by_id.get(turn.message_id, turn)
            avoidable = 0.0
            if effective.is_recache:
                rates = rates_lookup(effective.model)
                avoidable = _avoidable_cost(effective, rates)
            self.records.append(_Record(turn=effective, avoidable_cost=avoidable, agent_type=agent_type, group=group))

    def groups(self) -> tuple[str, ...]:
        """Every distinct group label seen so far, sorted."""
        return tuple(sorted({r.group for r in self.records}))


def build_section(stats: RecacheStats, pricing: Pricing, th: RecacheThresholds, group: str | None = None) -> Section:
    """Render ``stats`` into the ``recache`` report section: summary,
    signature split, gap buckets, preceding tool, top command prefixes,
    primary cause, event co-occurrence, attachment sub-split,
    by-agent-type, and huge-context cache-read volume.

    ``group``, when given, restricts the section to records whose
    :class:`RecacheStats` ``group_key`` returned that label (see
    :class:`RecacheStats`'s docstring); ``None`` (the default) covers
    every record regardless of group.
    """
    records = [r for r in stats.records if group is None or r.group == group]
    all_turns = [r.turn for r in records]
    recache_records = [r for r in records if r.turn.is_recache]
    recache_turns = [r.turn for r in recache_records]

    transcripts = stats.transcripts if group is None else stats.transcripts_by_group.get(group, 0)
    total_priced = len(all_turns)
    total_recache = len(recache_turns)
    total_cc_all = sum(t.cache_creation_tokens for t in all_turns)
    total_cc_recache = sum(t.cache_creation_tokens for t in recache_turns)
    total_avoidable = sum(r.avoidable_cost for r in recache_records)

    tables = [
        _summary_table(transcripts, total_priced, total_recache, total_cc_recache, total_cc_all, total_avoidable),
        _signature_table(recache_turns, recache_records),
        _gap_bucket_table(all_turns, recache_turns, total_cc_recache, total_priced),
        _preceding_tool_table(all_turns, recache_turns, total_cc_recache, total_priced),
        _top_command_prefix_table(recache_turns),
        _primary_cause_table(all_turns, recache_turns, recache_records, total_recache, total_priced),
        _cooccurrence_table(all_turns, recache_turns, total_recache, total_priced),
        _attachment_subsplit_table(recache_turns),
        _by_agent_type_table(records),
        _huge_context_table(all_turns, th),
    ]

    notes = [f"Thresholds: {' '.join(th.describe())}"]
    if group is not None:
        notes.append(f"Filtered to group: {group}.")
    notes.append(f"Avoidable cost priced against {pricing.version} ({pricing.currency}, sha8={pricing.sha8}).")

    return Section(key="recache", title="Re-cache events", tables=tables, notes=notes)


def _summary_table(
    transcripts: int,
    total_priced: int,
    total_recache: int,
    total_cc_recache: int,
    total_cc_all: int,
    total_avoidable: float,
) -> Table:
    return Table(
        name="recache_summary",
        title="Re-cache summary",
        columns=[
            Column(key="transcripts", label="Transcripts", kind="int"),
            Column(key="priced_turns", label="Priced turns", kind="int"),
            Column(key="recache_turns", label="Re-cache turns", kind="int"),
            Column(key="recache_turn_share_pct", label="Re-cache turn share", kind="pct"),
            Column(key="recache_cc_tokens", label="Cache-creation tokens (re-cache)", kind="tokens"),
            Column(key="total_cc_tokens", label="Cache-creation tokens (all)", kind="tokens"),
            Column(key="recache_cc_share_pct", label="Cache-creation share", kind="pct"),
            Column(key="avoidable_cost_usd", label="Avoidable cost", kind="money"),
        ],
        rows=[
            [
                transcripts,
                total_priced,
                total_recache,
                _pct(total_recache, total_priced),
                total_cc_recache,
                total_cc_all,
                _pct(total_cc_recache, total_cc_all),
                round(total_avoidable, 6),
            ]
        ],
        notes=[
            "A turn is a re-cache when it is not the transcript's first "
            "priced turn, ctx > ctx_floor, and cache_read_tokens < "
            "cr_ratio * ctx.",
        ],
    )


def _signature_table(recache_turns: list[Turn], recache_records: list[_Record]) -> Table:
    rows = []
    for sig in SIGNATURES:
        sub = [t for t in recache_turns if t.recache_signature == sig]
        cc = sum(t.cache_creation_tokens for t in sub)
        cost = sum(r.avoidable_cost for r in recache_records if r.turn.recache_signature == sig)
        ctx_med = _median([float(t.ctx) for t in sub])
        gap_med = _median([t.gap_s for t in sub if t.gap_s is not None])
        rows.append([sig, len(sub), cc, round(cost, 6), ctx_med, gap_med])
    return Table(
        name="recache_signature_split",
        title="Re-cache signature split",
        columns=[
            Column(key="signature", label="Signature", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="cc_tokens", label="Cache-creation tokens", kind="tokens"),
            Column(key="cost_delta_usd", label="Avoidable cost", kind="money"),
            Column(key="median_ctx", label="Median ctx", kind="tokens"),
            Column(key="median_gap_s", label="Median gap", kind="secs"),
        ],
        rows=rows,
        notes=[
            "full-expiry: cache_read_tokens < full_expiry_cr (the cache "
            "entry had essentially nothing left to hit). "
            "prefix-invalidated: cache_read_tokens is between "
            "full_expiry_cr and cr_ratio * ctx (a partial hit, so the TTL "
            "had not expired but something upstream of the cached prefix "
            "changed anyway).",
        ],
    )


def _gap_bucket_table(
    all_turns: list[Turn], recache_turns: list[Turn], total_cc_recache: int, total_priced: int
) -> Table:
    control_counts = {b: 0 for b in GAP_BUCKETS}
    for t in all_turns:
        control_counts[gap_bucket(t.gap_s)] += 1
    recache_by_bucket: dict[str, list[Turn]] = {b: [] for b in GAP_BUCKETS}
    for t in recache_turns:
        recache_by_bucket[gap_bucket(t.gap_s)].append(t)

    rows = []
    for bucket in GAP_BUCKETS:
        sub = recache_by_bucket[bucket]
        cc = sum(t.cache_creation_tokens for t in sub)
        rows.append(
            [
                bucket,
                len(sub),
                cc,
                _pct(cc, total_cc_recache),
                control_counts[bucket],
                _pct(control_counts[bucket], total_priced),
            ]
        )
    return Table(
        name="recache_gap_buckets",
        title="Re-cache by inter-turn gap",
        columns=[
            Column(key="bucket", label="Gap since previous turn", kind="str"),
            Column(key="turns", label="Re-cache turns", kind="int"),
            Column(key="cc_tokens", label="Cache-creation tokens", kind="tokens"),
            Column(key="cc_share_pct", label="Share of re-cache cc tokens", kind="pct"),
            Column(key="control_turns", label="All priced turns (control)", kind="int"),
            Column(key="control_share_pct", label="Control share", kind="pct"),
        ],
        rows=rows,
        notes=[
            "The 5-minute bucket boundary is inclusive on its lower side: "
            "a gap of exactly 300s falls in 5-15m, not 1-5m. The control "
            "columns show the same buckets over every priced turn, so a "
            "re-cache bucket's over-representation is visible against the "
            "baseline distribution of gaps, not just its raw count.",
        ],
    )


def _preceding_tool_table(
    all_turns: list[Turn], recache_turns: list[Turn], total_cc_recache: int, total_priced: int
) -> Table:
    control_counts: dict[str, int] = {}
    for t in all_turns:
        control_counts[t.preceding_tool] = control_counts.get(t.preceding_tool, 0) + 1
    recache_by_tool: dict[str, list[Turn]] = {}
    for t in recache_turns:
        recache_by_tool.setdefault(t.preceding_tool, []).append(t)

    tools = sorted(set(control_counts) | set(recache_by_tool))
    rows = []
    for tool in tools:
        sub = recache_by_tool.get(tool, [])
        cc = sum(t.cache_creation_tokens for t in sub)
        rows.append(
            [
                tool,
                len(sub),
                cc,
                _pct(cc, total_cc_recache),
                control_counts.get(tool, 0),
                _pct(control_counts.get(tool, 0), total_priced),
            ]
        )
    rows.sort(key=lambda row: row[2], reverse=True)
    return Table(
        name="recache_preceding_tool",
        title="Re-cache by preceding tool",
        columns=[
            Column(key="preceding_tool", label="Preceding tool", kind="str"),
            Column(key="turns", label="Re-cache turns", kind="int"),
            Column(key="cc_tokens", label="Cache-creation tokens", kind="tokens"),
            Column(key="cc_share_pct", label="Share of re-cache cc tokens", kind="pct"),
            Column(key="control_turns", label="All priced turns (control)", kind="int"),
            Column(key="control_share_pct", label="Control share", kind="pct"),
        ],
        rows=rows,
        notes=[
            "preceding_tool is Bash if the previous turn used it, else "
            "PowerShell, else the previous turn's first tool, else "
            "'none'/'n/a' (parse.py's priority scan).",
        ],
    )


def _top_command_prefix_table(recache_turns: list[Turn], limit: int = 12) -> Table:
    cc_by_prefix: dict[str, int] = {}
    turns_by_prefix: dict[str, int] = {}
    for t in recache_turns:
        prefix = t.preceding_cmd_prefix
        if not prefix:
            continue
        cc_by_prefix[prefix] = cc_by_prefix.get(prefix, 0) + t.cache_creation_tokens
        turns_by_prefix[prefix] = turns_by_prefix.get(prefix, 0) + 1
    top = sorted(cc_by_prefix.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    rows = [[prefix, turns_by_prefix[prefix], cc] for prefix, cc in top]
    return Table(
        name="recache_top_command_prefixes",
        title="Top preceding command prefixes (re-cache)",
        columns=[
            Column(key="preceding_cmd_prefix", label="Preceding command prefix", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="cc_tokens", label="Cache-creation tokens", kind="tokens"),
        ],
        rows=rows,
        notes=[
            f"Top {limit} by cache-creation tokens among re-cache turns "
            "whose previous turn's Bash/PowerShell command left a "
            "recorded (<=40-char, path-redacted) prefix.",
        ],
    )


def _primary_cause_table(
    all_turns: list[Turn],
    recache_turns: list[Turn],
    recache_records: list[_Record],
    total_recache: int,
    total_priced: int,
) -> Table:
    control_counts: dict[EventKind, int] = {}
    for t in all_turns:
        control_counts[t.preceding_primary] = control_counts.get(t.preceding_primary, 0) + 1
    recache_by_primary: dict[EventKind, list[Turn]] = {}
    for t in recache_turns:
        recache_by_primary.setdefault(t.preceding_primary, []).append(t)
    cost_by_primary: dict[EventKind, float] = {}
    for r in recache_records:
        cost_by_primary[r.turn.preceding_primary] = cost_by_primary.get(r.turn.preceding_primary, 0.0) + r.avoidable_cost

    rows = []
    for primary in set(control_counts) | set(recache_by_primary):
        sub = recache_by_primary.get(primary, [])
        cc = sum(t.cache_creation_tokens for t in sub)
        share = _pct(len(sub), total_recache)
        control_share = _pct(control_counts.get(primary, 0), total_priced)
        rows.append(
            [
                primary.value,
                len(sub),
                cc,
                share,
                round(cost_by_primary.get(primary, 0.0), 6),
                control_share,
                share - control_share,
            ]
        )
    rows.sort(key=lambda row: row[2], reverse=True)
    return Table(
        name="recache_primary_cause",
        title="Re-cache primary cause",
        columns=[
            Column(key="preceding_primary", label="Preceding primary", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="cc_tokens", label="Cache-creation tokens", kind="tokens"),
            Column(key="share_pct", label="Share of re-cache turns", kind="pct"),
            Column(key="avoidable_cost_usd", label="Avoidable cost", kind="money"),
            Column(key="control_share_pct", label="Control share (all priced turns)", kind="pct"),
            Column(key="over_representation_points", label="Over-representation", kind="float"),
        ],
        rows=rows,
        notes=[
            "preceding_primary is the single highest-precedence event "
            "kind observed since the previous turn (plan Appendix A2). "
            "over-representation is this row's share of re-cache turns "
            "minus its share of all priced turns, in percentage points, "
            "so a cause's prevalence in ordinary turns is never mistaken "
            "for evidence it drives re-caching.",
        ],
    )


def _cooccurrence_table(
    all_turns: list[Turn], recache_turns: list[Turn], total_recache: int, total_priced: int
) -> Table:
    kinds = sorted(
        {k for t in all_turns for k in t.preceding_event_kinds} | {k for t in recache_turns for k in t.preceding_event_kinds},
        key=lambda k: k.value,
    )
    rows = []
    for kind in kinds:
        recache_count = sum(1 for t in recache_turns if kind in t.preceding_event_kinds)
        control_count = sum(1 for t in all_turns if kind in t.preceding_event_kinds)
        rows.append(
            [
                kind.value,
                recache_count,
                _pct(recache_count, total_recache),
                control_count,
                _pct(control_count, total_priced),
            ]
        )
    rows.sort(key=lambda row: row[1], reverse=True)
    return Table(
        name="recache_event_cooccurrence",
        title="Re-cache event co-occurrence",
        columns=[
            Column(key="event_kind", label="Event kind", kind="str"),
            Column(key="recache_turns", label="Re-cache turns containing it", kind="int"),
            Column(key="recache_share_pct", label="Share of re-cache turns", kind="pct"),
            Column(key="control_turns", label="All priced turns containing it (control)", kind="int"),
            Column(key="control_share_pct", label="Control share", kind="pct"),
        ],
        rows=rows,
        notes=[
            "Counts every event kind observed since the previous turn "
            "(a turn can carry several), not just its highest-precedence "
            "preceding_primary — so a cause the precedence table hides "
            "because a higher-ranked kind also occurred that turn is "
            "still visible here.",
        ],
    )


def _attachment_subsplit_table(recache_turns: list[Turn]) -> Table:
    prefix_invalidated = [t for t in recache_turns if t.recache_signature == "prefix-invalidated"]
    turns_by_type: dict[str, int] = {}
    cc_by_type: dict[str, int] = {}
    for t in prefix_invalidated:
        for atype in set(t.preceding_attachment_types):
            turns_by_type[atype] = turns_by_type.get(atype, 0) + 1
            cc_by_type[atype] = cc_by_type.get(atype, 0) + t.cache_creation_tokens
    rows = sorted(([atype, turns_by_type[atype], cc_by_type[atype]] for atype in turns_by_type), key=lambda row: row[2], reverse=True)
    return Table(
        name="recache_attachment_subsplit",
        title="Prefix-invalidated attachment types",
        columns=[
            Column(key="attachment_type", label="Attachment type", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="cc_tokens", label="Cache-creation tokens", kind="tokens"),
        ],
        rows=rows,
        notes=[
            "Restricted to prefix-invalidated re-cache turns: full-expiry "
            "turns are excluded, since their cache had already fully "
            "expired regardless of any attachment observed alongside "
            "them. A turn carrying several attachment types in the same "
            "window counts once toward each.",
        ],
    )


def _by_agent_type_table(records: list[_Record]) -> Table:
    agent_types = sorted({r.agent_type for r in records})
    rows = []
    for agent_type in agent_types:
        agent_all = [r.turn for r in records if r.agent_type == agent_type]
        agent_recache = [t for t in agent_all if t.is_recache]
        cc = sum(t.cache_creation_tokens for t in agent_recache)
        cost = sum(r.avoidable_cost for r in records if r.agent_type == agent_type)
        rows.append(
            [
                agent_type,
                len(agent_all),
                len(agent_recache),
                _pct(len(agent_recache), len(agent_all)),
                cc,
                round(cost, 6),
            ]
        )
    rows.sort(key=lambda row: row[4], reverse=True)
    return Table(
        name="recache_by_agent_type",
        title="Re-cache by agent type",
        columns=[
            Column(key="agent_type", label="Agent type", kind="str"),
            Column(key="priced_turns", label="Priced turns", kind="int"),
            Column(key="recache_turns", label="Re-cache turns", kind="int"),
            Column(key="recache_share_pct", label="Re-cache share", kind="pct"),
            Column(key="cc_tokens", label="Cache-creation tokens (re-cache)", kind="tokens"),
            Column(key="avoidable_cost_usd", label="Avoidable cost", kind="money"),
        ],
        rows=rows,
        notes=[
            "agent_type is the transcript's TranscriptMeta.agent_type, or "
            "'top-level' for the main conversation.",
        ],
    )


def _huge_context_table(all_turns: list[Turn], th: RecacheThresholds) -> Table:
    huge_turns = [t for t in all_turns if t.ctx >= th.huge_ctx]
    total_cache_read = sum(t.cache_read_tokens for t in all_turns)
    huge_cache_read = sum(t.cache_read_tokens for t in huge_turns)
    return Table(
        name="recache_huge_context",
        title="Huge-context cache-read volume",
        columns=[
            Column(key="huge_ctx_turns", label="Turns with ctx >= huge_ctx", kind="int"),
            Column(key="total_priced_turns", label="Total priced turns", kind="int"),
            Column(key="huge_ctx_cache_read_tokens", label="Cache-read tokens from huge-ctx turns", kind="tokens"),
            Column(key="total_cache_read_tokens", label="Total cache-read tokens", kind="tokens"),
            Column(key="share_pct", label="Share of cache-read volume", kind="pct"),
        ],
        rows=[
            [
                len(huge_turns),
                len(all_turns),
                huge_cache_read,
                total_cache_read,
                _pct(huge_cache_read, total_cache_read),
            ]
        ],
        notes=[
            f"huge_ctx = {th.huge_ctx:,} tokens. This is a context-hygiene "
            "metric, not a pricing surcharge: the docs state 4.6+ models "
            "bill the full 1M-token context window at standard rates.",
        ],
    )


__all__ = [
    "GAP_BUCKETS",
    "SIGNATURES",
    "RecacheThresholds",
    "detect",
    "gap_bucket",
    "RecacheStats",
    "build_section",
]
