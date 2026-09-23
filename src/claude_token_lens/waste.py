"""Wasted-turn spend (v4-wasted-turns): price the turns whose output the
user never actually benefited from, and attribute each to a cause with a
lever -- the project's whole point is "every number leads to a lever and
a projected saving", and a wasted turn is the most direct instance of
that: money spent on a turn the user got nothing usable back from.

Detection reads state ``parse.py``/``events.py`` already produce (this
module detects nothing new, mirroring ``limits.py``'s own "purely a
reader" convention -- see that module's docstring):

- ``tool-error`` -- the assistant turn whose own tool_use produced a
  ``tool_result`` block with ``is_error: true`` (``Turn.tool_error_count
  > 0``, the v4-wasted-turns parser addition -- see model.py's module
  docstring) because the call couldn't run as written: a wrong path, a
  malformed command, an edit whose text wasn't found (``misfire`` in
  ``Turn.tool_errors_by_kind``). This is the turn *itself*, not the turn
  before or after it: the assistant spent tokens producing a tool call
  that failed.
- ``blocked`` -- the same, where a hook or a Claude Code guard stopped
  the call (``blocked``) and nothing misfired.
- A turn whose only errors are commands that ran and reported failure
  (``failed``: a failing test or build, a timeout) is not wasted: Claude
  reads that output and acts on it. It is counted
  (``WasteStats.failed_command_turns``) and left out. One whose only
  errors are denials falls through to ``tool-denial``. A digest from
  before ``tool_errors_by_kind`` existed counts every error as
  ``tool-error``.
- ``interrupt`` -- a turn immediately followed by ``[Request
  interrupted``: i.e. the *next* priced turn's own
  ``preceding_primary == EventKind.INTERRUPT``. The turn under scrutiny
  is the one the user cut off before letting it finish, not the turn
  that reports the interruption.
- ``tool-denial`` -- same "followed by" framing as ``interrupt``, keyed
  off the next priced turn's ``preceding_primary == EventKind.TOOL_DENIAL``.
- ``max-turns`` -- a subagent transcript's own ``TranscriptMeta.
  stopped_by_user`` is the only truncation signal this codebase can
  observe (``topology.py``'s own module docstring: a true ``maxTurns``
  cutoff and a user-killed subagent are indistinguishable from the
  transcript alone -- only ``stoppedByUser`` is ever set). Since a
  killed subagent's transcript never returns any report to its parent,
  *every* priced turn in such a transcript is treated as wasted under
  this cause -- a transcript-level override that takes priority over any
  other cause detected for one of its own turns (see ``ASSUMPTIONS``).
  Only ``TranscriptMeta.kind != "top-level"`` transcripts are eligible
  (``topology.py``'s own ``_add_chains`` only ever reads
  ``stopped_by_user`` off ``subs``, never ``top`` -- the field is not a
  meaningful top-level signal in this codebase's existing convention).
- ``api-error-retry`` -- turns whose own ``preceding_primary ==
  EventKind.API_ERROR`` (a 529/retry gap immediately before this turn),
  counted only -- never priced or added to the recoverable ceiling, per
  the brief's explicit "count only" instruction. The harness already
  retried automatically; this is a frequency signal, not spend.
- ``limit-pause`` turns (``Turn.gap_cause == "limit"``) are excluded
  outright, not folded into any of the above -- ``limits.py`` already
  owns that attribution (pause count/duration, post-pause re-cache
  cost). Excluded turns are counted (``WasteStats.limit_pause_excluded_
  turns``) and the exclusion is stated in the report section's own
  notes, per the brief's "exclude them and say so".

Cost/tokens: a wasted turn's cost is its full priced cost (input, cache
write, cache read, output) via ``pricing.price_turn`` -- the same
turn-pricing function every other analytics module in this codebase
uses -- plus its token total (the same four components summed). Every
``share_pct`` column in this section (turns and cost alike) is computed
against the *whole corpus's* priced turns/cost, not just the wasted
subset: the point of a "recoverable spend ceiling" is to answer "how
much of my total spend is this", which only a corpus-wide denominator
can answer -- a share computed only within the wasted subset would
answer a narrower, less actionable question (see ``waste_summary``'s own
``wasted_cost_share_pct``, which is exactly this ratio and is what
:func:`_rule_wasted_turns` compares against ``WasteThresholds.share_pct``).

Cause priority when a turn could match more than one rule: limit-pause
exclusion first (outranks everything -- see above), then ``max-turns``
(a whole-transcript override), then ``tool-error``/``blocked`` (the
turn's own defect), then ``interrupt``/``tool-denial`` (what happened
right after it). ``api-error-retry`` is independent of this priority order -- it is
a separate, non-costed counter, not a cause a turn is exclusively
assigned to.

Session identity: ``waste_top_sessions`` needs a stable-but-non-reversible
per-session key, same privacy posture as every other per-session table
in this codebase. Session ids are hashed inside :meth:`WasteStats.add`
(never stored raw) via the same salted-HMAC-SHA256 construction
``exports._hash_slug``/``team.machine_id`` already use (domain-tag +
truncated hex digest), with this module's own ``"session:"`` tag so its
namespace can never collide with theirs even at the same truncation
length. Following ``team.py``/``exports.py``'s own precedent (an
explicit ``config_dir`` parameter, ``parse.load_or_create_salt(config_dir)``
called directly) rather than ``parse.py``'s process-wide ``set_salt``
convention (that mechanism exists for the *parsing* layer, threaded
through a multiprocessing initializer -- this is a report-layer module,
called once, for which an explicit parameter is both simpler and
consistent with the two nearest analogues). ``compute_waste``/
``WasteStats`` both accept an optional ``config_dir`` for this reason;
omitting it uses ``load_or_create_salt``'s own default resolution
(``$CLAUDE_CONFIG_DIR/token-lens``, else ``~/.claude/token-lens``) --
a caller that must keep this module from touching the real config
directory (e.g. a read-only run against a scratch cache) should pass its
own scratch directory explicitly.

This module deliberately mirrors ``limits.py``'s accumulate-then-render
shape (:class:`WasteStats` is an accumulator with a per-transcript
:meth:`WasteStats.add`, mirroring ``LimitStats.add``) while also
exposing the plain :func:`compute_waste` entry point the project brief
asked for -- ``compute_waste(results, rates, thresholds)`` is exactly
``stats = WasteStats(thresholds); [stats.add(r, rates) for r in
results]; return stats``, so a caller that already loops per-transcript
(``report.py``'s own session loop, which already calls ``ls.add(result,
rates_lookup)`` inline) can call ``WasteStats.add`` the same way instead
of collecting a flat list first.

``RULES`` -- a tuple of ``(report, th) -> list[Recommendation]``
functions, mirroring the *shape* ``recommend.py``'s internal ``_rule_*``
functions already have (same signature, same evidence-tuple contract:
every ``Recommendation.evidence`` entry cites a real, already-rendered
table cell -- see ``_cell``/``_evidence`` below, deliberately
self-contained rather than imported from ``recommend.py``, to avoid a
circular import whichever direction the wiring agent connects the two
modules) -- since no ``RULES`` constant exists anywhere in this codebase
yet (``recommend.py`` calls its own rule functions directly from its
``recommend()`` entry point), this is a new, minimal convention this
module establishes for a wiring agent to consume, e.g. ``recs.extend(
waste.RULES[0](report, waste_th))``.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .model import Column, EventKind, Recommendation, ReportModel, Section, Table, TranscriptResult, Turn, agent_type_label
from .parse import load_or_create_salt
from .pricing import Pricing, price_turn

#: This module's own judgement calls, printed verbatim in the report's
#: "## Assumptions" block alongside every other module's own
#: ``ASSUMPTIONS`` tuple.
ASSUMPTIONS: tuple[str, ...] = (
    "max-turns: only TranscriptMeta.kind != \"top-level\" transcripts are "
    "eligible for the max-turns cause -- topology.py's own _add_chains "
    "only ever reads stopped_by_user off subagent transcripts, never the "
    "top-level one, so a top-level transcript's stopped_by_user is not "
    "treated as a meaningful signal here either",
    "max-turns is a transcript-level override: every priced turn in a "
    "stopped_by_user subagent transcript is wasted under this cause, "
    "regardless of whether that specific turn also had a tool error or "
    "was itself interrupted -- a killed subagent's transcript returns no "
    "report to its parent regardless of what any one turn did",
    "cause priority when more than one rule could match the same turn: "
    "limit-pause exclusion first, then max-turns, then tool-error, then "
    "interrupt/tool-denial -- each wasted turn is assigned exactly one "
    "cause, so waste_by_cause's turns/cost sum to waste_summary's totals",
    "api-error-retry is counted, never priced -- it is a frequency "
    "signal (how often a 529/retry gap preceded a turn), not spend: the "
    "harness already retried automatically",
    "every share_pct column in this section is computed against the "
    "whole corpus's priced turns/cost, not just the wasted subset, so "
    "wasted_cost_share_pct answers \"how much of my total spend is this\" "
    "directly rather than only describing the wasted subset's own "
    "internal breakdown",
)

#: The cost-attributed causes, in the fixed display order every
#: by-cause table uses (not sorted by size -- see limits.py's HIT_KINDS
#: for the same fixed-order convention).
CAUSES: tuple[str, ...] = ("tool-error", "blocked", "interrupt", "tool-denial", "max-turns")

#: The count-only cause, reported alongside CAUSES but never priced.
API_ERROR_RETRY_CAUSE = "api-error-retry"

#: One lever per cost-attributed cause -- what to change to stop paying
#: for this kind of wasted turn again.
LEVERS: dict[str, str] = {
    "tool-error": (
        "Give exact paths and names in briefs, and have Claude check a path "
        "exists or read a file before it edits or runs against it, so a "
        "tool call works the first time."
    ),
    "blocked": (
        "Put the rule a hook enforces into the instructions of the agent "
        "that keeps hitting it (its prompt, or CLAUDE.md for the main "
        "session), so Claude doesn't try the blocked action first."
    ),
    "interrupt": (
        "Batch instructions and plan the whole step before running it, so "
        "there is less to interrupt mid-turn."
    ),
    "tool-denial": (
        "Add the repeatedly-denied tool/command to the permissions "
        "allowlist so it stops being denied mid-run."
    ),
    "max-turns": (
        "Raise the subagent's maxTurns budget or narrow its brief so it "
        "finishes -- and reports back -- inside the turns it's given."
    ),
}

#: How many rows :func:`_top_sessions_table` reports, highest wasted cost
#: first. Not itself in the brief; a documented default rather than an
#: unbounded table (a large corpus could otherwise produce thousands of
#: rows for a report meant to be read).
TOP_SESSIONS_LIMIT = 20

#: HMAC domain tag for session-id hashing (see module docstring) --
#: distinct from exports._hash_slug's "slug:" and team.machine_id's
#: "machine:" tags so the three namespaces can never collide even at the
#: same truncation length.
_SESSION_HASH_TAG = b"session:"
_SESSION_HASH_HEX_CHARS = 12


def _hash_session_id(session_id: str, salt: bytes) -> str:
    digest = hmac.new(salt, _SESSION_HASH_TAG + session_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:_SESSION_HASH_HEX_CHARS]


def _pct(part: float, whole: float) -> float:
    return 100.0 * part / whole if whole else 0.0


def _turn_tokens(turn: Turn) -> int:
    return turn.input_tokens + turn.cache_creation_tokens + turn.cache_read_tokens + turn.output_tokens


def _priced_turns(result: TranscriptResult) -> list[Turn]:
    return [t for t in result.turns if t.turn_index > 0]


@dataclass(slots=True)
class WasteThresholds:
    """The tunable numbers this module's own rule depends on. Mirrors
    ``LimitThresholds``'s ``from_config``/``describe`` convention.
    """

    #: :func:`_rule_wasted_turns` fires when wasted cost exceeds this
    #: share (percentage points) of the corpus's total priced cost.
    share_pct: float = 10.0
    #: Minimum-sample gate, same numbers/convention as
    #: ``recommend.RecommendThresholds`` (5 sessions OR 200 priced
    #: turns) -- reimplemented locally rather than imported, to avoid a
    #: circular import (see module docstring).
    min_sessions: int = 5
    min_turns: int = 200

    @classmethod
    def from_config(cls, config: dict | None) -> "WasteThresholds":
        data = config or {}
        if not isinstance(data, dict):
            data = {}
        nested = data.get("waste")
        if isinstance(nested, dict):
            data = nested
        kwargs: dict = {}
        if "share_pct" in data:
            kwargs["share_pct"] = float(data["share_pct"])
        if "min_sessions" in data:
            kwargs["min_sessions"] = int(data["min_sessions"])
        if "min_turns" in data:
            kwargs["min_turns"] = int(data["min_turns"])
        return cls(**kwargs)

    def describe(self) -> list[str]:
        return [
            f"share_pct = {self.share_pct:.1f}%: wasted-turns fires when "
            "wasted cost exceeds this share of the corpus's total priced "
            "cost.",
            f"min_sessions = {self.min_sessions}, min_turns = "
            f"{self.min_turns}: the corpus must clear one of these before "
            "wasted-turns fires (same minimum-sample gate "
            "recommend.py's own rules use).",
        ]


_DEFAULT_THRESHOLDS = WasteThresholds()


# -- accumulator --------------------------------------------------------------


@dataclass(slots=True)
class _CauseAcc:
    turns: int = 0
    tokens: int = 0
    cost_usd: float = 0.0


@dataclass(slots=True)
class _AgentTypeAcc:
    turns: int = 0
    tokens: int = 0
    cost_usd: float = 0.0


@dataclass(slots=True)
class _SessionAcc:
    turns: int = 0
    cost_usd: float = 0.0
    cause_counts: dict[str, int] = field(default_factory=dict)


class WasteStats:
    """Accumulates wasted-turn facts across every transcript in a corpus,
    for :func:`build_section` to render. Mirrors ``LimitStats``'s own
    accumulate-then-render shape.
    """

    def __init__(self, thresholds: WasteThresholds | None = None, config_dir: str | Path | None = None) -> None:
        self.thresholds = thresholds or _DEFAULT_THRESHOLDS
        self._salt = load_or_create_salt(config_dir)
        self.total_priced_turns = 0
        self.total_priced_cost_usd = 0.0
        self.total_priced_tokens = 0
        self.limit_pause_excluded_turns = 0
        self.api_error_retry_turns = 0
        #: Turns whose only failed tool calls were commands that ran and
        #: reported failure -- work, not waste (see the module docstring).
        self.failed_command_turns = 0
        self.pricing_version: str | None = None
        self.pricing_currency: str | None = None
        self.pricing_sha8: str | None = None
        self._by_cause: dict[str, _CauseAcc] = {cause: _CauseAcc() for cause in CAUSES}
        self._by_agent_type: dict[str, _AgentTypeAcc] = {}
        self._by_session: dict[str, _SessionAcc] = {}

    def add(self, result: TranscriptResult, rates: Pricing) -> None:
        """Fold one transcript (top-level or subagent) into the
        accumulator."""
        if self.pricing_version is None:
            self.pricing_version = rates.version
            self.pricing_currency = rates.currency
            self.pricing_sha8 = rates.sha8

        agent_type = agent_type_label(result)
        session_id = result.meta.session_id
        session_key = _hash_session_id(session_id, self._salt) if session_id else None
        transcript_truncated = result.meta.kind != "top-level" and bool(result.meta.stopped_by_user)

        priced = _priced_turns(result)
        n = len(priced)
        for i, turn in enumerate(priced):
            resolved = rates.resolve_model(turn.model)
            breakdown = price_turn(turn, resolved)
            tokens = _turn_tokens(turn)

            self.total_priced_turns += 1
            self.total_priced_cost_usd += breakdown.total
            self.total_priced_tokens += tokens

            if turn.preceding_primary == EventKind.API_ERROR:
                self.api_error_retry_turns += 1

            if turn.gap_cause == "limit":
                self.limit_pause_excluded_turns += 1
                continue

            cause: str | None = None
            if transcript_truncated:
                cause = "max-turns"
            elif turn.tool_error_count > 0:
                cause = _error_cause(turn)
                if cause is None and turn.tool_errors_by_kind.get("failed"):
                    self.failed_command_turns += 1
            if cause is None and not transcript_truncated:
                next_turn = priced[i + 1] if i + 1 < n else None
                if next_turn is not None and next_turn.preceding_primary == EventKind.INTERRUPT:
                    cause = "interrupt"
                elif next_turn is not None and next_turn.preceding_primary == EventKind.TOOL_DENIAL:
                    cause = "tool-denial"

            if cause is None:
                continue

            cause_acc = self._by_cause[cause]
            cause_acc.turns += 1
            cause_acc.tokens += tokens
            cause_acc.cost_usd += breakdown.total

            type_acc = self._by_agent_type.setdefault(agent_type, _AgentTypeAcc())
            type_acc.turns += 1
            type_acc.tokens += tokens
            type_acc.cost_usd += breakdown.total

            if session_key is not None:
                session_acc = self._by_session.setdefault(session_key, _SessionAcc())
                session_acc.turns += 1
                session_acc.cost_usd += breakdown.total
                session_acc.cause_counts[cause] = session_acc.cause_counts.get(cause, 0) + 1

    @property
    def wasted_turns(self) -> int:
        return sum(acc.turns for acc in self._by_cause.values())

    @property
    def wasted_cost_usd(self) -> float:
        return sum(acc.cost_usd for acc in self._by_cause.values())

    @property
    def wasted_tokens(self) -> int:
        return sum(acc.tokens for acc in self._by_cause.values())


def compute_waste(
    results: Sequence[TranscriptResult],
    rates: Pricing,
    thresholds: WasteThresholds | None = None,
    *,
    config_dir: str | Path | None = None,
) -> WasteStats:
    """Fold every transcript in ``results`` (top-level and subagent alike
    -- session grouping is read off each one's own ``TranscriptMeta.
    session_id``, so callers need not pre-sort them) into one
    :class:`WasteStats`. Equivalent to constructing ``WasteStats`` and
    calling :meth:`WasteStats.add` once per transcript -- see the module
    docstring for why both entry points exist.
    """
    stats = WasteStats(thresholds=thresholds, config_dir=config_dir)
    for result in results:
        stats.add(result, rates)
    return stats


def _error_cause(turn) -> str | None:
    """The cause a turn with failed tool calls counts under, or ``None``
    when none of them wasted the turn (see the module docstring)."""
    kinds = turn.tool_errors_by_kind
    if not kinds or kinds.get("misfire"):
        return "tool-error"
    if kinds.get("blocked"):
        return "blocked"
    return None


# -- report section -----------------------------------------------------------


def build_section(stats: WasteStats, thresholds: WasteThresholds | None = None) -> Section:
    """Render ``stats`` into the ``waste`` report section: summary,
    by-cause, by-agent-type, and top-sessions tables.
    """
    th = thresholds or stats.thresholds or _DEFAULT_THRESHOLDS

    tables = [
        _summary_table(stats),
        _by_cause_table(stats),
        _by_agent_type_table(stats),
        _top_sessions_table(stats),
    ]

    notes = [f"Thresholds: {' '.join(th.describe())}"]
    if stats.pricing_version:
        notes.append(
            f"Priced against {stats.pricing_version} ({stats.pricing_currency}, sha8={stats.pricing_sha8})."
        )
    notes.append(
        f"{stats.limit_pause_excluded_turns} turn(s) following a usage-cap pause "
        "(Turn.gap_cause == \"limit\") were excluded from this section entirely -- "
        "see the limits section for their own pause/cost accounting."
    )
    notes.append(
        f"{stats.failed_command_turns} turn(s) whose only failed tool calls were commands that "
        "ran and reported failure (a failing test or build, a timeout) are not counted as wasted: "
        "Claude used that output."
    )

    return Section(key="waste", title="Wasted-turn spend", tables=tables, notes=notes)


def _summary_table(stats: WasteStats) -> Table:
    wasted_turns = stats.wasted_turns
    wasted_cost = stats.wasted_cost_usd
    wasted_tokens = stats.wasted_tokens
    return Table(
        name="waste_summary",
        title="Wasted-turn spend summary",
        columns=[
            Column(key="metric", label="Metric", kind="str"),
            Column(key="total_priced_turns", label="Total priced turns", kind="int"),
            Column(key="total_priced_cost_usd", label="Total priced cost", kind="money"),
            Column(key="wasted_turns", label="Wasted turns", kind="int"),
            Column(key="wasted_turns_share_pct", label="Share of turns", kind="pct"),
            Column(key="wasted_cost_usd", label="Recoverable spend ceiling", kind="money"),
            Column(key="wasted_cost_share_pct", label="Share of cost", kind="pct"),
            Column(key="wasted_tokens", label="Wasted tokens", kind="tokens"),
            Column(key="limit_pause_excluded_turns", label="Excluded (limit pause)", kind="int"),
            Column(key="api_error_retry_turns", label="API-error-retry turns (count only)", kind="int"),
            Column(key="failed_command_turns", label="Not counted (a command ran and failed)", kind="int"),
        ],
        rows=[
            [
                "all",
                stats.total_priced_turns,
                round(stats.total_priced_cost_usd, 6),
                wasted_turns,
                _pct(wasted_turns, stats.total_priced_turns),
                round(wasted_cost, 6),
                _pct(wasted_cost, stats.total_priced_cost_usd),
                wasted_tokens,
                stats.limit_pause_excluded_turns,
                stats.api_error_retry_turns,
                stats.failed_command_turns,
            ]
        ],
        notes=[
            "wasted_cost_usd is the recoverable spend ceiling: the full "
            "priced cost (input, cache write, cache read, output) of every "
            "turn whose output the user never benefited from -- see "
            "waste_by_cause for the per-cause breakdown and lever.",
            "api_error_retry_turns is a frequency count only -- it is not "
            "priced and is not included in wasted_cost_usd (the harness "
            "already retried these automatically).",
        ],
    )


def _by_cause_table(stats: WasteStats) -> Table:
    rows = []
    for cause in CAUSES:
        acc = stats._by_cause[cause]
        rows.append(
            [
                cause,
                acc.turns,
                _pct(acc.turns, stats.total_priced_turns),
                round(acc.cost_usd, 6),
                _pct(acc.cost_usd, stats.total_priced_cost_usd),
                acc.tokens,
                LEVERS[cause],
            ]
        )
    rows.append(
        [
            API_ERROR_RETRY_CAUSE,
            stats.api_error_retry_turns,
            _pct(stats.api_error_retry_turns, stats.total_priced_turns),
            0.0,
            0.0,
            0,
            "None -- retried automatically by the harness; investigate only if persistently frequent.",
        ]
    )
    return Table(
        name="waste_by_cause",
        title="Wasted turns by cause",
        columns=[
            Column(key="cause", label="Cause", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="share_of_turns_pct", label="Share of all priced turns", kind="pct"),
            Column(key="cost_usd", label="Cost", kind="money"),
            Column(key="share_of_cost_pct", label="Share of all priced cost", kind="pct"),
            Column(key="tokens", label="Tokens", kind="tokens"),
            Column(key="lever", label="Lever", kind="str"),
        ],
        rows=rows,
        notes=[
            "share_of_turns_pct/share_of_cost_pct are both against the "
            "whole corpus's priced turns/cost, not just the wasted subset "
            "(see the module docstring) -- so they sum, across the four "
            "costed causes, to waste_summary's own wasted_turns_share_pct/"
            "wasted_cost_share_pct.",
            f"{API_ERROR_RETRY_CAUSE} is shown for its frequency only -- "
            "its cost/tokens are always 0 and it is never part of the "
            "recoverable ceiling.",
        ],
    )


def _by_agent_type_table(stats: WasteStats) -> Table:
    rows = [
        [
            agent_type,
            acc.turns,
            _pct(acc.turns, stats.total_priced_turns),
            round(acc.cost_usd, 6),
            _pct(acc.cost_usd, stats.total_priced_cost_usd),
            acc.tokens,
        ]
        for agent_type, acc in stats._by_agent_type.items()
    ]
    # Deterministic order: descending by cost, tie-broken by agent type.
    rows.sort(key=lambda row: (row[3], row[0]), reverse=True)
    return Table(
        name="waste_by_agent_type",
        title="Wasted-turn spend by agent type",
        columns=[
            Column(key="agent_type", label="Agent type", kind="str"),
            Column(key="turns", label="Wasted turns", kind="int"),
            Column(key="share_of_turns_pct", label="Share of all priced turns", kind="pct"),
            Column(key="cost_usd", label="Wasted cost", kind="money"),
            Column(key="share_of_cost_pct", label="Share of all priced cost", kind="pct"),
            Column(key="tokens", label="Wasted tokens", kind="tokens"),
        ],
        rows=rows,
        notes=[
            "agent_type is the transcript's TranscriptMeta.agent_type, or "
            "'top-level' for the main conversation.",
        ],
    )


def _top_sessions_table(stats: WasteStats) -> Table:
    rows = []
    for session_hash, acc in stats._by_session.items():
        cause_mix = ", ".join(f"{cause}:{count}" for cause, count in sorted(acc.cause_counts.items(), key=lambda kv: kv[1], reverse=True))
        rows.append(
            [
                session_hash,
                acc.turns,
                round(acc.cost_usd, 6),
                _pct(acc.cost_usd, stats.total_priced_cost_usd),
                cause_mix,
            ]
        )
    rows.sort(key=lambda row: (row[2], row[0]), reverse=True)
    rows = rows[:TOP_SESSIONS_LIMIT]
    return Table(
        name="waste_top_sessions",
        title=f"Top {TOP_SESSIONS_LIMIT} sessions by wasted cost",
        columns=[
            Column(key="session_hash", label="Session (hashed)", kind="str"),
            Column(key="turns", label="Wasted turns", kind="int"),
            Column(key="cost_usd", label="Wasted cost", kind="money"),
            Column(key="share_of_cost_pct", label="Share of all priced cost", kind="pct"),
            Column(key="cause_mix", label="Cause mix", kind="str"),
        ],
        rows=rows,
        notes=[
            "session_hash is a salted HMAC-SHA256 of the session id "
            "(12 hex chars, own \"session:\" domain tag) -- never the raw "
            "session id, matching every other per-session table in this "
            "codebase's privacy convention.",
            "cause_mix lists this session's own wasted-turn causes as "
            "cause:count pairs, most frequent first.",
        ],
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
    column doesn't exist -- mirrors ``recommend.py``'s own ``_cell``
    contract (reimplemented locally, see module docstring, to avoid a
    circular import)."""
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


def _corpus_sessions_and_priced_turns(report: ReportModel) -> tuple[int, int]:
    sessions = _cell(report, "overview", "totals", "sessions", "value")
    priced_turns = _cell(report, "overview", "totals", "priced_turns", "value")
    return (
        int(sessions) if isinstance(sessions, (int, float)) else 0,
        int(priced_turns) if isinstance(priced_turns, (int, float)) else 0,
    )


def _meets_min_sample(report: ReportModel, th: WasteThresholds) -> bool:
    sessions, priced_turns = _corpus_sessions_and_priced_turns(report)
    return sessions >= th.min_sessions or priced_turns >= th.min_turns


def _rule_wasted_turns(report: ReportModel, th: WasteThresholds) -> list[Recommendation]:
    """Fires when wasted cost exceeds ``th.share_pct`` of the corpus's
    total priced cost (and the corpus clears the minimum-sample gate).
    Names the dominant cost-bearing cause's own lever and cites the
    recoverable ceiling. Returns ``[]`` when the ``waste`` section isn't
    present (e.g. an older cached report, or a report assembled before
    this batch's wiring landed) -- matches ``_rule_limit_pressure``'s own
    "returns [] when its section is absent" convention.
    """
    if not _meets_min_sample(report, th):
        return []

    share = _cell(report, "waste", "waste_summary", "all", "wasted_cost_share_pct")
    if not isinstance(share, (int, float)) or share <= th.share_pct:
        return []

    wasted_cost = _cell(report, "waste", "waste_summary", "all", "wasted_cost_usd")
    wasted_turns = _cell(report, "waste", "waste_summary", "all", "wasted_turns")

    cause_table = _table(report, "waste", "waste_by_cause")
    dominant_cause: str | None = None
    dominant_cost = 0.0
    if cause_table is not None:
        cost_idx = _col_index(cause_table, "cost_usd")
        if cost_idx is not None:
            for row in cause_table.rows:
                if not row or row[0] not in CAUSES:
                    continue
                cost = row[cost_idx] if isinstance(row[cost_idx], (int, float)) else 0.0
                if dominant_cause is None or cost > dominant_cost:
                    dominant_cause = row[0]
                    dominant_cost = cost

    lever_text = LEVERS.get(dominant_cause) if dominant_cause else None
    if lever_text is None:
        lever_text = "review the waste_by_cause table to find the dominant cause."

    evidence = [
        _evidence("Wasted cost share", share, "waste", "waste_summary", "all"),
        _evidence("Recoverable spend ceiling", wasted_cost, "waste", "waste_summary", "all"),
        _evidence("Wasted turns", wasted_turns, "waste", "waste_summary", "all"),
    ]
    if dominant_cause is not None:
        evidence.append(_evidence(f"{dominant_cause} cost", dominant_cost, "waste", "waste_by_cause", dominant_cause))

    cause_clause = f"mostly '{dominant_cause}'" if dominant_cause else "see waste_by_cause for the breakdown"
    action = (
        f"{share:.1f}% of priced cost (a recoverable ceiling of ${wasted_cost:,.2f}) went to turns whose "
        f"output was never used, {cause_clause}. {lever_text}"
    )

    return [
        Recommendation(
            id="wasted-turns",
            severity="advice",
            category="workflow",
            archetypes=(),
            title="A material share of spend went to turns with no benefit",
            action=action,
            lever=None,
            evidence=evidence,
        )
    ]


#: One rule per this module's own convention (see module docstring): a
#: tuple of ``(report, th) -> list[Recommendation]`` callables for a
#: wiring agent to fold into ``recommend.recommend()``'s own output,
#: e.g. ``recs.extend(waste.RULES[0](report, waste_th))``.
RULES: tuple = (_rule_wasted_turns,)


__all__ = [
    "ASSUMPTIONS",
    "CAUSES",
    "API_ERROR_RETRY_CAUSE",
    "LEVERS",
    "TOP_SESSIONS_LIMIT",
    "WasteThresholds",
    "WasteStats",
    "compute_waste",
    "build_section",
    "RULES",
]
