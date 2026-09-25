"""Usage-limit tracking (v3-limits): turn a 5-hour/weekly usage-cap pause,
the harness's forced early termination of a subagent, and the desktop
app's automatic resume ping into first-class, attributable facts instead
of behavioural noise.

Motivation (see ``model.py``'s "Usage-limits batch" module docstring
section for the full field-level contract this module reads): when the
account hits its usage cap, the harness pauses and, on resume, the
prompt cache has expired. Left unattributed, that pause looks exactly
like a very long idle gap, inflating "gaps > 5 min" counts in
``recache.py``/``ttl.py``, forcing a full-expiry-shaped re-cache that
looks like ordinary TTL churn, driving ``recommend.py``'s long-tool-wait
heuristic, and occasionally pushing a session into "overnight" mode in
``classify.py`` purely because the pause — not real overnight work —
made the span/gap long enough.

``events.py``/``parse.py`` already do the actual detection (see their
own module docstrings): a synthetic assistant line's text becomes
``EventKind.LIMIT_HIT`` (subkind ``session_limit``/``weekly_limit``), the
desktop app's resume ping becomes ``EventKind.LIMIT_RESUME``, and a
harness-killed subagent's task notification becomes
``EventKind.AGENT_TERMINATED`` (subkind ``rate_limit``/``other``). Every
turn whose gap to the previous one spanned one of these events carries
``Turn.gap_cause == "limit"``. This module is purely a *reader* of that
already-parsed state — it detects nothing new — and exists to:

- Give ``classify.py`` a per-session list of usage-cap pause intervals
  (:func:`limit_pause_intervals`) so its human-gap/span statistics can
  discount them (see classify.py's own module docstring for exactly
  which two places use it).
- Give a session-timeline API a flat, ready-to-render list of markers
  (:func:`limit_markers`).
- Accumulate corpus-wide facts (:class:`LimitStats`) and render them as
  the report's own ``limits`` :class:`~claudeglass.model.Section`
  (:func:`build_section`): hit/resume/termination counts, pause
  durations, a reset-hour-of-day histogram, a by-agent-type roll-up, and
  the unavoidable re-cache cost paid by the turn immediately following
  each pause.
- Cross-check the transcript-derived hit count against
  ``usage-log.csv`` (``tools/log_usage.py``'s own ground-truth log, when
  one exists) via :func:`csv_cross_check` — a sanity check, not a second
  detector.

Privacy: every field this module produces (counts, percentages, USD
totals, token counts, an hour-of-day integer, an enum-like subkind
string) is already privacy-clean per ``model.py``'s contract; this
module never reads or stores message text, a path, or a full command.

Deviation from the plan, reported rather than made silently (project
convention, see ``model.py``'s module docstring): the original brief's
"pauses (hit ts -> next priced turn or resume marker; duration)" is
computed here as ``(turn.ts - turn.gap_s, turn.ts)`` for every turn with
``gap_cause == "limit"`` -- i.e. read directly off ``parse.py``'s own
gap computation -- rather than re-deriving the interval by walking raw
``LIMIT_HIT``/``LIMIT_RESUME`` events and matching them up by hand. The
two are equivalent by construction (``parse.py``'s two-buffer scheme
guarantees the limit event(s) precede exactly the turn that carries
``gap_cause == "limit"``, and ``gap_s`` is that turn's own gap to the
previous finalised turn), and reading it off ``Turn`` is far simpler and
needs no event/turn correlation of its own.

Similarly, the "limit-induced re-cache tokens/cost" table does not run
its own re-cache *detection* (``recache.RecacheThresholds``'s
``ctx_floor``/``cr_ratio``): every turn with ``gap_cause == "limit"``
already necessarily did a full prefix rewrite regardless of those
thresholds (``ttl.py``'s ``simulate()`` "limit-expiry" branch: ``read=0,
write=cache_read+cache_creation`` under *every* TTL policy) — so this
module simply prices each such turn's observed split via
``pricing.price_turn``'s default (observed) path, the same way
``ttl.observed()`` does, without needing ``RecacheThresholds`` at all.
"""

from __future__ import annotations

import csv as csv_mod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence

from .model import Column, Event, EventKind, Section, Table, TranscriptResult, Turn, agent_type_label
from .pricing import ModelRates, Pricing, ResolvedRates, price_turn

#: This module's own reporting/cross-check assumptions, printed verbatim
#: in the report's "## Assumptions" block (``ReportMeta.assumptions``)
#: alongside ``recache.ASSUMPTIONS``/``ttl.ASSUMPTIONS``.
ASSUMPTIONS: tuple[str, ...] = (
    "a usage-cap pause's (start, end) interval is read directly off the "
    "turn that carries Turn.gap_cause == \"limit\" -- start = turn.ts - "
    "turn.gap_s, end = turn.ts -- not re-derived from raw LIMIT_HIT/"
    "LIMIT_RESUME events",
    "a turn immediately following a usage-cap pause always did a full "
    "prefix rewrite (ttl.py's simulate() 'limit-expiry' branch), so its "
    "cost is priced via price_turn's default observed-split path, not "
    "re-detected against recache.py's ctx_floor/cr_ratio thresholds",
    "reset-hour-of-day prefers LIMIT_HIT's own reset_minutes_of_day "
    "(the literal local hour named in the synthetic text) over "
    "converting reset_ts (UTC) through the machine's local zone, which "
    "is used only as a fallback when the text carried no parseable "
    "clause",
    "the usage-log.csv cross-check counts a row as an exhaustion signal "
    "purely on used_percentage >= csv_exhaustion_pct: a bare resets_at "
    "value is present on nearly every row regardless of exhaustion, so "
    "it is not treated as a signal on its own",
)

#: The two ``EventKind.LIMIT_HIT`` subkinds this module ever sees (the
#: other four ``classify_synthetic_text`` outcomes -- overloaded,
#: unsupported_model, autocompact_thrash, other_api_error -- never
#: synthesise a LIMIT_HIT event; see parse.py's module docstring).
HIT_KINDS: tuple[str, ...] = ("session_limit", "weekly_limit")

#: The two ``EventKind.AGENT_TERMINATED`` subkinds (events.py's
#: ``_agent_terminated_subkind``).
TERMINATED_KINDS: tuple[str, ...] = ("rate_limit", "other")

#: The two usage-log.csv windows this module cross-checks (the third,
#: spend_limit, has no transcript-derived analogue).
CSV_WINDOWS: tuple[str, ...] = ("five_hour", "seven_day")


@dataclass(slots=True)
class LimitThresholds:
    """The one tunable number this module's own logic depends on (its
    detection is entirely upstream, in events.py/parse.py). Mirrors
    ``RecacheThresholds``/``TtlThresholds``'s ``from_config``/``describe``
    convention for consistency, even though there is only one field.
    """

    #: A usage-log.csv five_hour/seven_day row counts as an exhaustion
    #: signal when its ``used_percentage`` is at or above this.
    csv_exhaustion_pct: float = 100.0

    @classmethod
    def from_config(cls, config: dict | None) -> "LimitThresholds":
        data = config or {}
        if not isinstance(data, dict):
            data = {}
        nested = data.get("thresholds")
        if isinstance(nested, dict):
            data = nested
        kwargs: dict = {}
        if "csv_exhaustion_pct" in data:
            kwargs["csv_exhaustion_pct"] = float(data["csv_exhaustion_pct"])
        return cls(**kwargs)

    def describe(self) -> list[str]:
        return [
            f"A usage log row counts as at the limit when it shows "
            f"{self.csv_exhaustion_pct:.1f}% or more of the 5-hour or weekly "
            "limit used.",
        ]


_DEFAULT_THRESHOLDS = LimitThresholds()


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _pct(part: float, whole: float) -> float:
    return 100.0 * part / whole if whole else 0.0


# -- classify.py's own two consumers -----------------------------------------


def limit_pause_intervals(top: TranscriptResult) -> list[tuple[datetime, datetime]]:
    """Every usage-cap pause in ``top``'s own turns, as ``(start, end)``
    UTC-aware ``datetime`` pairs.

    ``end`` is the turn's own parsed timestamp; ``start`` is ``end``
    minus that turn's ``gap_s`` (``parse.py``'s own gap computation, run
    backwards — see the module docstring's deviation note). Only turns
    with ``Turn.gap_cause == "limit"`` and a resolvable ``gap_s``/``ts``
    contribute. Only ``top``'s turns are considered, never any
    subagent's — a pause is an account-wide event, but the gap/span
    statistics this feeds (``classify._median_and_max_gap``,
    ``classify.classify_mode``'s overnight check) are themselves
    top-level-only (see ``classify.py``'s module docstring on which
    signals are ``top``-only vs. summed across ``subs``).
    """
    intervals: list[tuple[datetime, datetime]] = []
    for turn in top.turns:
        if turn.gap_cause != "limit" or turn.gap_s is None:
            continue
        end = _parse_ts(turn.ts)
        if end is None:
            continue
        intervals.append((end - timedelta(seconds=turn.gap_s), end))
    return intervals


def limit_markers(result: TranscriptResult) -> list[tuple[str, str, dict]]:
    """Every ``LIMIT_HIT``/``LIMIT_RESUME``/``AGENT_TERMINATED`` event in
    ``result``, as ``(ts, kind, detail)`` triples, sorted by ``ts``
    (events with no timestamp sort first, as ``""``).

    ``kind`` is the event kind's own string value
    (``"limit_hit"``/``"limit_resume"``/``"agent_terminated"``);
    ``detail`` is a shallow copy of the event's own ``detail`` dict, plus
    ``"subkind"`` when the event carries one -- ready for a session
    timeline API to render as markers without importing ``EventKind``
    itself. See the module docstring's marker-contract note.
    """
    markers: list[tuple[str, str, dict]] = []
    for event in result.events:
        if event.kind not in (EventKind.LIMIT_HIT, EventKind.LIMIT_RESUME, EventKind.AGENT_TERMINATED):
            continue
        detail = dict(event.detail)
        if event.subkind is not None:
            detail["subkind"] = event.subkind
        markers.append((event.ts or "", event.kind.value, detail))
    markers.sort(key=lambda m: m[0])
    return markers


def _reset_local_hour(detail: dict) -> int | None:
    """Local hour of day (0-23) a ``LIMIT_HIT`` event's reset falls at.

    Prefers ``reset_minutes_of_day`` (the literal local hour the
    synthetic text itself named, e.g. "resets 3:00pm") over converting
    ``reset_ts`` (always UTC) through the machine's own local zone --
    the latter is only a fallback for the minority of hits whose text
    carried no parseable "resets ..." clause at all (see the module
    docstring's assumptions).
    """
    minutes = detail.get("reset_minutes_of_day")
    if isinstance(minutes, int):
        return (minutes // 60) % 24
    reset_ts = detail.get("reset_ts")
    if not isinstance(reset_ts, str):
        return None
    dt = _parse_ts(reset_ts)
    if dt is None:
        return None
    return dt.astimezone().hour


# -- accumulator --------------------------------------------------------------


@dataclass(slots=True)
class _RawAccumulator:
    """Mutable running totals for one agent-type key (mirrors
    ``ttl._RawAccumulator``'s own pattern)."""

    key: str
    transcripts: int = 0
    session_limit_hits: int = 0
    weekly_limit_hits: int = 0
    resumes: int = 0
    terminated_rate_limit: int = 0
    terminated_other: int = 0
    pause_seconds: list[float] = field(default_factory=list)
    reset_hours: list[int] = field(default_factory=list)
    limit_turn_cc_tokens: int = 0
    limit_turn_write_cost_usd: float = 0.0
    limit_turn_total_cost_usd: float = 0.0


@dataclass(slots=True)
class LimitTypeStats:
    """Rolled-up usage-limit stats for one agent type (or
    ``"top-level"``) -- the values behind one row of ``build_section``'s
    by-agent-type table."""

    key: str
    transcripts: int
    session_limit_hits: int
    weekly_limit_hits: int
    resumes: int
    terminated_rate_limit: int
    terminated_other: int
    pause_count: int
    pause_total_s: float
    pause_median_s: float | None
    pause_max_s: float | None
    limit_turn_cc_tokens: int
    limit_turn_write_cost_usd: float
    limit_turn_total_cost_usd: float

    @property
    def hits(self) -> int:
        return self.session_limit_hits + self.weekly_limit_hits

    @property
    def terminated(self) -> int:
        return self.terminated_rate_limit + self.terminated_other


class LimitStats:
    """Accumulates usage-limit facts across every transcript in a corpus,
    for :func:`build_section` to render. Mirrors ``RecacheStats``'s/
    ``TtlStats``'s own accumulate-then-render shape.
    """

    def __init__(self) -> None:
        self.transcripts = 0
        self.sessions_affected: set[str] = set()
        self._by_key: dict[str, _RawAccumulator] = {}

    def _acc(self, key: str) -> _RawAccumulator:
        acc = self._by_key.get(key)
        if acc is None:
            acc = _RawAccumulator(key=key)
            self._by_key[key] = acc
        return acc

    def add(
        self,
        result: TranscriptResult,
        rates_lookup: Callable[[str], "ResolvedRates | ModelRates | None"] | None = None,
    ) -> None:
        """Fold one transcript (top-level or subagent) into the
        accumulator. ``rates_lookup`` (a per-model rate resolver, e.g.
        ``pricing.Pricing.resolve_model``) is optional -- omitting it
        still accumulates every count/duration table, only the
        limit-turn cost columns stay at zero.
        """
        self.transcripts += 1
        agent_type = agent_type_label(result)
        acc = self._acc(agent_type)
        acc.transcripts += 1

        session_affected = False
        for event in result.events:
            if event.kind == EventKind.LIMIT_HIT:
                session_affected = True
                if event.subkind == "weekly_limit":
                    acc.weekly_limit_hits += 1
                else:
                    acc.session_limit_hits += 1
                hour = _reset_local_hour(event.detail)
                if hour is not None:
                    acc.reset_hours.append(hour)
            elif event.kind == EventKind.LIMIT_RESUME:
                session_affected = True
                acc.resumes += 1
            elif event.kind == EventKind.AGENT_TERMINATED:
                session_affected = True
                if event.subkind == "rate_limit":
                    acc.terminated_rate_limit += 1
                else:
                    acc.terminated_other += 1

        for turn in result.turns:
            if turn.gap_cause != "limit":
                continue
            if turn.gap_s is not None:
                acc.pause_seconds.append(turn.gap_s)
            if turn.turn_index <= 0:
                continue
            acc.limit_turn_cc_tokens += turn.cache_creation_tokens
            if rates_lookup is not None:
                rates = rates_lookup(turn.model)
                breakdown = price_turn(turn, rates)
                acc.limit_turn_write_cost_usd += breakdown.cache_write_cost
                acc.limit_turn_total_cost_usd += breakdown.total

        if session_affected and result.meta.session_id:
            self.sessions_affected.add(result.meta.session_id)

    def by_key(self) -> list[LimitTypeStats]:
        """One :class:`LimitTypeStats` per agent type seen so far,
        sorted by key."""
        rows = []
        for key in sorted(self._by_key):
            acc = self._by_key[key]
            rows.append(
                LimitTypeStats(
                    key=key,
                    transcripts=acc.transcripts,
                    session_limit_hits=acc.session_limit_hits,
                    weekly_limit_hits=acc.weekly_limit_hits,
                    resumes=acc.resumes,
                    terminated_rate_limit=acc.terminated_rate_limit,
                    terminated_other=acc.terminated_other,
                    pause_count=len(acc.pause_seconds),
                    pause_total_s=sum(acc.pause_seconds),
                    pause_median_s=_median(acc.pause_seconds),
                    pause_max_s=max(acc.pause_seconds) if acc.pause_seconds else None,
                    limit_turn_cc_tokens=acc.limit_turn_cc_tokens,
                    limit_turn_write_cost_usd=acc.limit_turn_write_cost_usd,
                    limit_turn_total_cost_usd=acc.limit_turn_total_cost_usd,
                )
            )
        return rows

    def reset_hour_counts(self) -> dict[int, int]:
        """Hour (0-23) -> number of ``LIMIT_HIT`` events whose reset
        resolved to that local hour, across every agent type."""
        counts: dict[int, int] = {h: 0 for h in range(24)}
        for acc in self._by_key.values():
            for hour in acc.reset_hours:
                counts[hour] = counts.get(hour, 0) + 1
        return counts


# -- report section -----------------------------------------------------------


def build_section(stats: LimitStats, pricing: Pricing | None = None, th: LimitThresholds | None = None) -> Section:
    """Render ``stats`` into the ``limits`` report section: summary,
    hit-kind split, terminated split, pause distribution, reset-hour
    histogram, and by-agent-type roll-up.
    """
    th = th or _DEFAULT_THRESHOLDS
    rows = stats.by_key()

    tables = [
        _summary_table(stats, rows),
        _hit_kind_table(rows),
        _terminated_table(rows),
        _pause_table(rows),
        _reset_hour_table(stats.reset_hour_counts()),
        _by_agent_type_table(rows),
    ]

    notes = [f"Thresholds: {' '.join(th.describe())}"]
    if pricing is not None:
        notes.append(f"The cost of cache writes after a pause uses prices from pricing.toml, version {pricing.version}.")

    return Section(key="limits", title="Usage limits", tables=tables, notes=notes)


def _summary_table(stats: LimitStats, rows: list[LimitTypeStats]) -> Table:
    total_hits = sum(r.hits for r in rows)
    total_session = sum(r.session_limit_hits for r in rows)
    total_weekly = sum(r.weekly_limit_hits for r in rows)
    total_resumes = sum(r.resumes for r in rows)
    total_terminated = sum(r.terminated for r in rows)
    total_terminated_rate_limit = sum(r.terminated_rate_limit for r in rows)
    total_pause_count = sum(r.pause_count for r in rows)
    total_pause_s = sum(r.pause_total_s for r in rows)
    total_cc_tokens = sum(r.limit_turn_cc_tokens for r in rows)
    total_write_cost = sum(r.limit_turn_write_cost_usd for r in rows)
    return Table(
        name="limits_summary",
        title="Usage-limits summary",
        columns=[
            Column(key="metric", label="Metric", kind="str"),
            Column(key="transcripts", label="Transcripts", kind="int"),
            Column(key="sessions_affected", label="Sessions affected", kind="int"),
            Column(key="limit_hits", label="Limit hits", kind="int"),
            Column(key="session_limit_hits", label="Session-limit hits", kind="int"),
            Column(key="weekly_limit_hits", label="Weekly-limit hits", kind="int"),
            Column(key="limit_resumes", label="Limit resumes", kind="int"),
            Column(key="agents_terminated", label="Agents terminated", kind="int"),
            Column(key="agents_terminated_rate_limit", label="...by rate limit", kind="int"),
            Column(key="pause_count", label="Pauses", kind="int"),
            Column(key="pause_total_s", label="Total pause time", kind="secs"),
            Column(key="limit_turn_cc_tokens", label="Cache-creation tokens (post-pause turns)", kind="tokens"),
            Column(key="limit_turn_write_cost_usd", label="Cache-write cost (post-pause turns)", kind="money"),
        ],
        rows=[
            [
                "all",
                stats.transcripts,
                len(stats.sessions_affected),
                total_hits,
                total_session,
                total_weekly,
                total_resumes,
                total_terminated,
                total_terminated_rate_limit,
                total_pause_count,
                round(total_pause_s, 3),
                total_cc_tokens,
                round(total_write_cost, 6),
            ]
        ],
        notes=[
            "A reply after a pause is the first reply after a usage-limit "
            "pause. It always rewrites the whole cache, so its cache writes "
            "and their cost can't be avoided, and aren't an ordinary cache "
            "rebuild.",
            "\"Cost of cache writes after a pause\" counts every reply after "
            "a pause. The rebuild cost after a pause in the cache rebuild "
            "tables counts only replies that also pass the rebuild check. "
            "So the two are related but not equal: "
            "docs/limits.md explains the difference.",
        ],
    )


def _hit_kind_table(rows: list[LimitTypeStats]) -> Table:
    total = sum(r.hits for r in rows)
    counts = {
        "session_limit": sum(r.session_limit_hits for r in rows),
        "weekly_limit": sum(r.weekly_limit_hits for r in rows),
    }
    table_rows = [[kind, counts[kind], _pct(counts[kind], total)] for kind in HIT_KINDS]
    return Table(
        name="limits_hits_by_kind",
        title="Limit hits by kind",
        columns=[
            Column(key="kind", label="Kind", kind="str"),
            Column(key="hits", label="Hits", kind="int"),
            Column(key="share_pct", label="Share", kind="pct"),
        ],
        rows=table_rows,
        notes=[
            "5-hour session limit: \"You've hit your session limit\" (the "
            "rolling 5-hour window). Weekly limit: \"You've hit your "
            "weekly limit\".",
        ],
    )


def _terminated_table(rows: list[LimitTypeStats]) -> Table:
    total = sum(r.terminated for r in rows)
    counts = {
        "rate_limit": sum(r.terminated_rate_limit for r in rows),
        "other": sum(r.terminated_other for r in rows),
    }
    table_rows = [[kind, counts[kind], _pct(counts[kind], total)] for kind in TERMINATED_KINDS]
    return Table(
        name="limits_agent_terminated",
        title="Agents terminated early",
        columns=[
            Column(key="kind", label="Reason", kind="str"),
            Column(key="terminated", label="Terminated", kind="int"),
            Column(key="share_pct", label="Share", kind="pct"),
        ],
        rows=table_rows,
        notes=[
            "A subagent Claude Code stopped mid-task, from its task "
            "notification's own \"Agent terminated early due to ...\" "
            "text. Usage limit: the notification named a rate limit as "
            "the error type. Other: any other reason, or none stated.",
        ],
    )


def _pause_table(rows: list[LimitTypeStats]) -> Table:
    # A corpus-wide median can't be exactly derived from already-aggregated
    # per-agent-type medians, so this table reports count/total/mean only;
    # per-agent-type median/max live on the by-agent-type table instead.
    total_count = sum(r.pause_count for r in rows)
    total_s = sum(r.pause_total_s for r in rows)
    mean_s = (total_s / total_count) if total_count else None
    return Table(
        name="limits_pauses",
        title="Usage-cap pause durations",
        columns=[
            Column(key="metric", label="Metric", kind="str"),
            Column(key="pause_count", label="Pauses", kind="int"),
            Column(key="total_s", label="Total pause time", kind="secs"),
            Column(key="mean_s", label="Mean pause", kind="secs"),
        ],
        rows=[["all", total_count, round(total_s, 3), round(mean_s, 3) if mean_s is not None else None]],
        notes=[
            "One pause per reply whose wait since the previous reply "
            "spanned a usage-limit pause. Its length is that wait. The "
            "typical and longest pause per agent type are in the "
            "by-agent-type table. An overall typical pause can't be "
            "worked out from the per-type ones.",
        ],
    )


def _reset_hour_table(counts: dict[int, int]) -> Table:
    total = sum(counts.values())
    rows = [[f"{hour:02d}", counts.get(hour, 0), _pct(counts.get(hour, 0), total)] for hour in range(24)]
    return Table(
        name="limits_reset_hour_histogram",
        title="Limit resets by local hour of day",
        columns=[
            Column(key="local_hour", label="Local hour", kind="str"),
            Column(key="resets", label="Resets", kind="int"),
            Column(key="share_pct", label="Share", kind="pct"),
        ],
        rows=rows,
        notes=[
            "The local hour comes from the limit message's own \"resets "
            "H:MMam/pm\" text when present, else from its reset time in "
            "this machine's time zone. A stop with neither is left out of "
            "this chart but still counted in the summary.",
        ],
    )


def _by_agent_type_table(rows: list[LimitTypeStats]) -> Table:
    table_rows = [
        [
            r.key,
            r.transcripts,
            r.hits,
            r.resumes,
            r.terminated,
            r.pause_count,
            round(r.pause_total_s, 3),
            r.pause_median_s,
            r.pause_max_s,
            r.limit_turn_cc_tokens,
            round(r.limit_turn_write_cost_usd, 6),
        ]
        for r in rows
    ]
    # Deterministic order: descending by hits, tie-broken by the row key.
    table_rows.sort(key=lambda row: (row[2], row[0]), reverse=True)
    return Table(
        name="limits_by_agent_type",
        title="Usage limits by agent type",
        columns=[
            Column(key="agent_type", label="Agent type", kind="str"),
            Column(key="transcripts", label="Transcripts", kind="int"),
            Column(key="limit_hits", label="Limit hits", kind="int"),
            Column(key="limit_resumes", label="Limit resumes", kind="int"),
            Column(key="agents_terminated", label="Agents terminated", kind="int"),
            Column(key="pause_count", label="Pauses", kind="int"),
            Column(key="pause_total_s", label="Total pause time", kind="secs"),
            Column(key="pause_median_s", label="Median pause", kind="secs"),
            Column(key="pause_max_s", label="Max pause", kind="secs"),
            Column(key="limit_turn_cc_tokens", label="Cache-creation tokens (post-pause turns)", kind="tokens"),
            Column(key="limit_turn_write_cost_usd", label="Cache-write cost (post-pause turns)", kind="money"),
        ],
        rows=table_rows,
        notes=[
            "Each subagent type as Claude Code recorded it, plus one row "
            "for the main session.",
        ],
    )


# -- usage-log.csv cross-check -------------------------------------------------


def read_usage_log_rows(csv_path: str | Path) -> list[dict]:
    """Load ``csv_path`` (``tools/log_usage.py``'s own CSV shape:
    ``logged_at, session_id, window, used_percentage, resets_at,
    source``) for :func:`csv_cross_check`. Returns ``[]`` when the file
    doesn't exist -- the log is optional, never required.

    A thin, independent re-implementation of
    ``tools.log_usage.load_usage_log`` (not a call to it): that module
    is one of two other concurrent writers' surface for this batch, and
    this module must not depend on its internals changing shape under
    it. The two are expected to agree on ``CSV_FIELDS``' meaning by
    convention, not by sharing code.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        return []
    rows: list[dict] = []
    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv_mod.DictReader(fh, restkey="_extra")
        for raw_row in reader:
            row = dict(raw_row)
            used = row.get("used_percentage")
            if used not in (None, ""):
                try:
                    row["used_percentage"] = float(used)
                except ValueError:
                    pass
            rows.append(row)
    return rows


def csv_cross_check(rows: Sequence[dict], stats: LimitStats, th: LimitThresholds | None = None) -> Table:
    """Cross-check ``usage-log.csv`` rows (see :func:`read_usage_log_rows`)
    against the transcript-derived hit counts in ``stats``: for each of
    ``CSV_WINDOWS`` (``five_hour``/``seven_day``), how many CSV rows
    reported ``used_percentage >= th.csv_exhaustion_pct`` versus how many
    transcript-derived hits ``stats`` recorded for the matching kind
    (``five_hour`` -> ``session_limit``, ``seven_day`` -> ``weekly_limit``).

    A sanity check, not a second detector: the two sources sample the
    account's usage independently (the statusline polls
    ``rate_limits`` continuously; a transcript only records a hit at the
    moment a request was actually blocked), so exact agreement is not
    expected -- a large, persistent gap either way is what is worth a
    human's attention, not the raw numbers alone.
    """
    th = th or _DEFAULT_THRESHOLDS
    type_stats = stats.by_key()
    transcript_hits = {
        "five_hour": sum(r.session_limit_hits for r in type_stats),
        "seven_day": sum(r.weekly_limit_hits for r in type_stats),
    }
    csv_counts: dict[str, int] = {window: 0 for window in CSV_WINDOWS}
    for row in rows:
        window = row.get("window")
        if window not in CSV_WINDOWS:
            continue
        used = row.get("used_percentage")
        if not isinstance(used, (int, float)):
            continue
        if used >= th.csv_exhaustion_pct:
            csv_counts[window] += 1

    table_rows = []
    for window in CSV_WINDOWS:
        csv_n = csv_counts[window]
        transcript_n = transcript_hits[window]
        table_rows.append([window, csv_n, transcript_n, transcript_n - csv_n])

    return Table(
        name="limits_csv_cross_check",
        title="Usage-log.csv cross-check",
        columns=[
            Column(key="window", label="Window", kind="str"),
            Column(key="csv_exhaustion_rows", label="usage-log.csv exhaustion rows", kind="int"),
            Column(key="transcript_hits", label="Transcript-derived hits", kind="int"),
            Column(key="delta", label="Transcript minus CSV", kind="int"),
        ],
        rows=table_rows,
        notes=[
            f"A usage log row counts as at the limit at "
            f"{th.csv_exhaustion_pct:.1f}% used or more. 5-hour rows are "
            "compared with 5-hour session limit stops, weekly rows with "
            "weekly limit stops. The two sides are counted separately, so "
            "they needn't match exactly.",
        ],
    )


def signals_cross_check(session_signals, stats: LimitStats) -> Table:
    """Cross-check the free ``waits``/``turn_signals`` capture signals
    (SIG-2, SIG-3) against the transcript-derived limit-hit count in
    ``stats``: how many sessions logged a ``quota`` wait (a claude.ai
    usage-limit auto-resume notification) or a ``rate_limit``/
    ``overloaded`` ``StopFailure``, against how many transcript-derived
    ``session_limit``/``weekly_limit`` hits ``stats`` recorded in total.

    ``session_signals`` is ``signals.by_session(...)``'s own return
    shape: ``{session_id: signals.SessionSignals}``. Unlike
    :func:`csv_cross_check`, neither signal names its window
    (``five_hour``/``seven_day``), so this compares one combined figure
    per side, not a per-window breakdown -- a sanity check, not a second
    detector, same posture as :func:`csv_cross_check` (see its own
    docstring for why exact agreement isn't expected between two
    independently-sampled sources).
    """
    type_stats = stats.by_key()
    transcript_hits = sum(r.session_limit_hits + r.weekly_limit_hits for r in type_stats)
    quota_waits = sum(seen.waits.get("quota", 0) for seen in session_signals.values())
    limit_failures = sum(
        seen.failures.get("rate_limit", 0) + seen.failures.get("overloaded", 0) for seen in session_signals.values()
    )
    rows = [
        ["quota wait signals (Notification)", quota_waits, transcript_hits, transcript_hits - quota_waits],
        ["rate_limit/overloaded turn failures (StopFailure)", limit_failures, transcript_hits, transcript_hits - limit_failures],
    ]
    return Table(
        name="limits_signals_cross_check",
        title="Free-signal cross-check",
        columns=[
            Column(key="signal", label="Signal", kind="str"),
            Column(key="signal_count", label="Logged", kind="int"),
            Column(key="transcript_hits", label="Transcript-derived hits (both windows)", kind="int"),
            Column(key="delta", label="Transcript minus signal", kind="int"),
        ],
        rows=rows,
        notes=[
            "Neither signal names its window, so both rows compare with the 5-hour and weekly limit stops "
            "together. The two sides are counted separately, so they needn't match exactly.",
        ],
    )


__all__ = [
    "ASSUMPTIONS",
    "HIT_KINDS",
    "TERMINATED_KINDS",
    "CSV_WINDOWS",
    "LimitThresholds",
    "limit_pause_intervals",
    "limit_markers",
    "LimitTypeStats",
    "LimitStats",
    "build_section",
    "read_usage_log_rows",
    "csv_cross_check",
    "signals_cross_check",
]
