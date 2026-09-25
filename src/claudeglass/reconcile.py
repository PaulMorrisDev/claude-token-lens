"""Admin API usage/cost export reconciliation (plan "Enterprise use" /
"Finance": ``reconcile --admin-csv <file>`` "compares local estimates with
an Admin API usage/cost export by day and model, offline").

This module never makes a network call: it reads a CSV the user already
exported from the Anthropic Console/Admin API and compares it against
this tool's own per-turn accounting for the same sessions, entirely
offline, per the plan's own wording.

Column names in a real Admin usage/cost export are not fixed in this
codebase's plan or fixtures, so :func:`parse_admin_csv` uses a tolerant
header mapper (:data:`_SLOT_HEADERS`) that recognises several
plausible spellings per field rather than one fixed schema, and reports
every column it could not place (``AdminCsvResult.unmapped_headers``) so
a user with a differently-named export can see exactly what was ignored.
Every header name and token/cost column this module maps is an assumed
shape, not one confirmed against Anthropic's own published export schema
-- documented again in ``docs/compare.md``'s own admin-CSV mapping table,
per this project's "surface assumptions rather than silently guess"
convention. In particular, the ``_5m``/``_1h`` cache-creation TTL-split
column names (:data:`_SLOT_HEADERS`'s ``cache_creation_tokens_5m``/
``cache_creation_tokens_1h`` entries) are this module's own guess at a
plausible naming pattern, folded into one ``cache_creation_tokens``
total either way. The flat column is taken to be the split's own total,
so a row carrying both counts the larger of the two, never their sum.

Day bucketing: this module buckets its own local per-turn totals by the
turn's own timestamp's **UTC calendar day** (not ``config.tz``), on the
assumption that an Admin usage/cost export buckets by UTC day too. An
Admin date cell that is a full timestamp is reduced to its UTC day the
same way, and the CLI converts its ``--since``/``--until`` window to
UTC days before calling :func:`reconcile`. This
is itself an assumption (documented in ``docs/compare.md``), which is
also why "UTC day boundaries" is one of the fixed reasons every
reconciliation table's note lists for an expected local/Admin
difference: if the Admin export actually buckets by some other
boundary (e.g. workspace-local time), a session whose turns straddle
midnight will land in a different day on each side even though every
token was accounted for correctly.

Like ``compare.py``, the small per-transcript helpers (``_priced_turns``,
``_transcripts_of``, ``_parse_ts``) are deliberately duplicated rather
than imported from ``report.py`` -- see that module's own docstring, and
``report.py``/``usage.py``/``workflows.py``/``phases.py``/``cli.py``'s,
for the convention this follows. Every cost figure comes from
``pricing.price_turn`` -- never recomputed independently.

Parser-signals addition (SURV-5, ``PARSER_VERSION`` 19): :func:`claude_code_reported_costs`
reads a session's own ``cost-state`` line (``TranscriptMeta.cc_cost_usd``,
parsed by ``parse.py``) next to this tool's own per-turn pricing for the
same session, per plan P9's later "Q1 gap metric" wording -- "reconcile
shows Claude Code's own cost vs ClaudeGlass's cost for each session ...
from SIG-4 and cost-state".

Q1 gap metric (P9b): :func:`cost_ground_truth_gaps` builds on that --
one row per session with either kind of ground truth, ``cost-state``
preferred when a session has both (it is a validated per-transcript
running total; ``statusline.py``'s own SIG-4 line is a periodic,
possibly mid-session, last-seen snapshot -- see that module's own
docstring). :func:`build_cost_ground_truth_gap_table` turns those into
the ``cost_ground_truth_gap`` table :func:`reconcile` adds whenever a
``config_dir`` is given, with the plan's own threshold note (median
absolute gap over 5%, across at least 10 sessions with a computable
gap) phrased in the caller's billing-mode units (:mod:`units`). Reading
SIG-4 signals follows the same read-only-salt posture
``report._capture_signals`` established (duplicated here, not
imported -- see this module's own duplication convention above): never
creates the salt file, so a corpus with capture off, or on but no salt
yet, simply contributes no ``statusline`` rows. Numbers only, no
network call and no OTel, same as every other function in this module.
"""

from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .config import Config
from .corpus import Corpus, SessionBundle
from .model import Column, Section, Table, TranscriptResult, Turn
from .pricing import Pricing, price_turn

if TYPE_CHECKING:
    from .units import Units

#: The three ``--by`` groupings the CLI understands.
BY_CHOICES: tuple[tuple[str, ...], ...] = (("day",), ("model",), ("day", "model"))


class ReconcileError(Exception):
    """The Admin CSV could not be parsed. The message is always a single,
    user-facing line naming the problem and (for a bad data row) the
    1-based line number only -- never the row's own content, so a
    reconcile failure can be printed to a shared terminal/log without
    risk of echoing whatever the export actually contained.
    """


# -- tolerant header mapping --------------------------------------------------

#: canonical field -> the header spellings (already lower-cased, non-
#: alphanumeric runs collapsed to "_") this module recognises for it. See
#: the module docstring: these are an assumed shape, not a confirmed
#: Admin API export schema.
_SLOT_HEADERS: dict[str, set[str]] = {
    "date": {"date", "day", "usage_date", "bucket_start"},
    "model": {"model", "model_name"},
    "input_tokens": {"input_tokens", "uncached_input_tokens"},
    "output_tokens": {"output_tokens"},
    "cache_read_tokens": {"cache_read_input_tokens", "cache_read_tokens"},
    "cache_creation_tokens": {"cache_creation_input_tokens", "cache_creation_tokens"},
    "cache_creation_tokens_5m": {
        "cache_creation_input_tokens_5m",
        "cache_creation_5m_input_tokens",
        "cache_creation_5m_tokens",
        "ephemeral_5m_input_tokens",
        "cache_creation_ephemeral_5m_input_tokens",
    },
    "cache_creation_tokens_1h": {
        "cache_creation_input_tokens_1h",
        "cache_creation_1h_input_tokens",
        "cache_creation_1h_tokens",
        "ephemeral_1h_input_tokens",
        "cache_creation_ephemeral_1h_input_tokens",
    },
    "cost": {"cost", "cost_usd", "total_cost"},
    "cost_cents": {"cost_cents", "total_cost_cents"},
}

_HEADER_TO_SLOT: dict[str, str] = {
    variant: slot for slot, variants in _SLOT_HEADERS.items() for variant in variants
}


def _normalize_header(raw: str) -> str:
    text = raw.strip().lower()
    out_chars = []
    prev_underscore = False
    for ch in text:
        if ch.isalnum():
            out_chars.append(ch)
            prev_underscore = False
        elif not prev_underscore:
            out_chars.append("_")
            prev_underscore = True
    return "".join(out_chars).strip("_")


def _map_headers(fieldnames: list[str]) -> tuple[dict[int, str], list[str]]:
    """``(index -> canonical slot, list of original headers left
    unmapped)`` for one Admin CSV header row.
    """
    index_to_slot: dict[int, str] = {}
    unmapped: list[str] = []
    for i, raw in enumerate(fieldnames):
        slot = _HEADER_TO_SLOT.get(_normalize_header(raw))
        if slot is None:
            unmapped.append(raw)
        else:
            index_to_slot[i] = slot
    return index_to_slot, unmapped


def _parse_int(raw: str) -> int:
    text = raw.strip().replace(",", "")
    if text == "":
        return 0
    try:
        return int(text)
    except ValueError:
        return int(round(float(text)))


def _parse_money(raw: str) -> float:
    text = raw.strip().replace(",", "").replace("$", "")
    if text == "":
        return 0.0
    return float(text)


def _admin_utc_day(raw: str) -> str:
    """The UTC ``YYYY-MM-DD`` day an Admin row's date cell names. A
    plain date passes through unchanged; a full timestamp (e.g. a
    ``bucket_start`` of ``2026-08-05T00:00:00Z``) is reduced to its UTC
    day, so it groups and window-filters against the local side's own
    UTC-day keys rather than never matching them. Raises ``ValueError``
    for an empty cell.
    """
    text = raw.strip()
    if not text:
        raise ValueError("empty date")
    if len(text) <= 10:
        return text
    dt = _parse_ts(text)
    if dt is None:
        return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date().isoformat()


def _build_row(raw_row: list[str], index_to_slot: dict[int, str], line_num: int) -> dict:
    values: dict[str, str] = {}
    for i, cell in enumerate(raw_row):
        slot = index_to_slot.get(i)
        if slot is not None:
            values[slot] = cell
    try:
        date = _admin_utc_day(values.get("date", ""))
        model = values.get("model", "").strip() or None
        input_tokens = _parse_int(values.get("input_tokens", "0"))
        output_tokens = _parse_int(values.get("output_tokens", "0"))
        cache_read_tokens = _parse_int(values.get("cache_read_tokens", "0"))
        # The flat column is already the 5m + 1h total, so the two are
        # never added together. Take the larger of the flat figure and the
        # split's own sum -- the same rule ``parse.py`` applies to a
        # turn's ``usage`` -- so a file carrying either shape, or both,
        # counts each cache-creation token exactly once.
        cache_creation_tokens = max(
            _parse_int(values.get("cache_creation_tokens", "0")),
            _parse_int(values.get("cache_creation_tokens_5m", "0"))
            + _parse_int(values.get("cache_creation_tokens_1h", "0")),
        )
        cost = _parse_money(values.get("cost", "0"))
        if "cost_cents" in values:
            cost += _parse_money(values["cost_cents"]) / 100.0
    except ValueError as exc:
        raise ReconcileError(f"cannot parse admin CSV at line {line_num}") from exc
    return {
        "date": date,
        "model": model,
        "input_tokens": input_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "output_tokens": output_tokens,
        "cost": cost,
    }


@dataclass(slots=True)
class AdminCsvResult:
    """One parsed Admin usage/cost export: per-row canonical dicts
    (``date``, ``model`` (``None`` if the export has no model column),
    ``input_tokens``, ``cache_creation_tokens``, ``cache_read_tokens``,
    ``output_tokens``, ``cost``) plus the original header text of every
    column the mapper could not place.
    """

    rows: list[dict] = field(default_factory=list)
    unmapped_headers: list[str] = field(default_factory=list)


def parse_admin_csv(path: str | Path) -> AdminCsvResult:
    """Parse an Admin usage/cost export CSV at ``path``. Raises
    :class:`ReconcileError` (never any other exception) when the file
    cannot be opened, has no header row, has no recognisable date
    column, or a data row fails to parse -- always a single line naming
    the problem, with a 1-based line number for a bad row and never that
    row's own content (see :class:`ReconcileError`'s docstring).
    """
    path = Path(path)
    try:
        fh = open(path, "r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise ReconcileError(f"cannot open admin CSV {path}: {exc.strerror or exc}") from exc

    try:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ReconcileError("admin CSV is empty (no header row)") from exc
        except csv.Error as exc:
            raise ReconcileError(f"cannot parse admin CSV at line {reader.line_num}") from exc

        index_to_slot, unmapped = _map_headers(header)
        if "date" not in index_to_slot.values():
            raise ReconcileError(
                "admin CSV has no recognisable date column "
                "(expected one of: date, day, usage_date, bucket_start)"
            )

        rows: list[dict] = []
        try:
            for raw_row in reader:
                if not raw_row or all(not cell.strip() for cell in raw_row):
                    continue  # blank line: skipped, not an error
                rows.append(_build_row(raw_row, index_to_slot, reader.line_num))
        except csv.Error as exc:
            raise ReconcileError(f"cannot parse admin CSV at line {reader.line_num}") from exc
    finally:
        fh.close()

    return AdminCsvResult(rows=rows, unmapped_headers=unmapped)


# -- local accounting ----------------------------------------------------


def _priced_turns(result: TranscriptResult) -> list[Turn]:
    """Duplicated one-line helper -- see module docstring."""
    return [t for t in result.turns if t.turn_index > 0]


def _transcripts_of(bundle: SessionBundle) -> list[TranscriptResult]:
    """Duplicated one-line helper -- see module docstring."""
    transcripts: list[TranscriptResult] = []
    if bundle.top is not None:
        transcripts.append(bundle.top)
    transcripts.extend(bundle.subs)
    return transcripts


def _parse_ts(ts: str | None) -> datetime | None:
    """Duplicated one-line helper -- see module docstring."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _utc_day(ts: str | None) -> str | None:
    dt = _parse_ts(ts)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date().isoformat()


@dataclass(slots=True)
class ClaudeCodeCost:
    """One session's self-reported cost next to this tool's own pricing
    for the same session (SURV-5, the ``cost-state`` half of plan P9's
    "Q1 gap metric" -- see :func:`cost_ground_truth_gaps` for the half
    that also folds in SIG-4's statusline ground truth and the module
    docstring for the metric as a whole).
    """

    session_id: str = ""
    #: Claude Code's own reported total (``TranscriptMeta.cc_cost_usd``,
    #: from a ``cost-state`` line's own ``totalCostUSD`` -- the last one
    #: seen in the session's top-level transcript, since it's a running
    #: total).
    cc_cost_usd: float = 0.0
    #: Whether Claude Code itself flagged an unpriced/unknown model
    #: anywhere in this session's own usage
    #: (``TranscriptMeta.cc_cost_has_unknown_model``).
    cc_has_unknown_model: bool = False
    #: This tool's own per-turn pricing (``pricing.price_turn``) summed
    #: across the session's top-level transcript and every subagent
    #: transcript under it -- the same total the rest of this module
    #: calls "local" cost.
    local_cost_usd: float = 0.0


def _local_cost_for_bundle(bundle: SessionBundle, pricing: Pricing) -> float:
    """This tool's own per-turn pricing, summed across a session's
    top-level transcript and every subagent transcript under it -- the
    same total :func:`claude_code_reported_costs` and
    :func:`cost_ground_truth_gaps` both call "local" cost."""
    total = 0.0
    for tr in _transcripts_of(bundle):
        for turn in _priced_turns(tr):
            resolved = pricing.resolve_model(turn.model)
            total += price_turn(turn, resolved).total
    return total


def claude_code_reported_costs(corpus: Corpus, pricing: Pricing) -> list[ClaudeCodeCost]:
    """One :class:`ClaudeCodeCost` per session whose top-level transcript
    carried at least one ``cost-state`` line -- most sessions carry none
    (an infrequent, apparently version-gated line: 25 occurrences across
    a 2,617-file real-corpus survey), so this list is typically much
    shorter than ``corpus.sessions``. Read-only, numbers only, no OTel --
    see this module's own docstring's rule that it never makes a network
    call, which extends to never building any telemetry pipeline either.
    """
    out: list[ClaudeCodeCost] = []
    for bundle in corpus.sessions:
        top = bundle.top
        if top is None or top.meta.cc_cost_usd is None:
            continue
        out.append(
            ClaudeCodeCost(
                session_id=top.meta.session_id,
                cc_cost_usd=top.meta.cc_cost_usd,
                cc_has_unknown_model=top.meta.cc_cost_has_unknown_model,
                local_cost_usd=_local_cost_for_bundle(bundle, pricing),
            )
        )
    return out


def _session_signals_by_id(corpus: Corpus, config_dir: str | Path | None):
    """The free signals metrics capture logged under ``config_dir``, by
    session id -- duplicated from ``report._capture_signals`` rather than
    imported (see this module's own docstring on that convention).
    ``None`` without a config directory, a signals folder or the salt the
    hook hashed session ids with (never created here -- SIG-4, like
    SIG-2/3 before it, only ever reads an existing salt)."""
    if config_dir is None:
        return None
    from . import parse, signals

    try:
        if not signals.signals_dir(config_dir).is_dir() or not (Path(config_dir) / "salt").is_file():
            return None
        salt = parse.load_or_create_salt(config_dir)
        return signals.by_session(signals.load(config_dir), [b.session_id for b in corpus.sessions], salt)
    except (OSError, ValueError):
        return None


@dataclass(slots=True)
class CostGroundTruthGap:
    """One session's local-vs-ground-truth cost gap for the Q1 gap
    metric (see the module docstring). ``source`` is ``"cost_state"`` or
    ``"statusline"`` -- which ground-truth channel supplied
    ``cc_cost_usd`` for this row; ``cost_state`` wins when a session
    carries both (see :func:`cost_ground_truth_gaps`). ``gap_usd`` is
    local minus the ground truth, matching this module's existing
    "delta = local minus X" convention; ``gap_pct`` is that gap as a
    percentage of the ground truth, ``None`` when the ground truth is
    ``0.0`` (nothing to take a percentage of)."""

    session_id: str
    source: str
    cc_cost_usd: float
    local_cost_usd: float
    gap_usd: float
    gap_pct: float | None


def cost_ground_truth_gaps(corpus: Corpus, pricing: Pricing, config_dir: str | Path | None) -> list[CostGroundTruthGap]:
    """One :class:`CostGroundTruthGap` per session that carries *either*
    ground-truth signal: a ``cost-state`` line (preferred -- a validated
    running total logged straight from the transcript) or, when a
    session has no ``cost-state`` line at all, SIG-4's statusline
    ``cost`` signal (a periodic, possibly mid-session, last-seen
    snapshot -- see ``statusline.py``'s own docstring). A session with
    neither contributes nothing here; that is the ordinary case (see
    :func:`claude_code_reported_costs`'s own docstring on how rare
    ``cost-state`` is), which is exactly why SIG-4 exists as a second,
    much more common source of the same kind of ground truth.
    """
    out: list[CostGroundTruthGap] = []
    covered: set[str] = set()
    for cc in claude_code_reported_costs(corpus, pricing):
        out.append(
            CostGroundTruthGap(
                session_id=cc.session_id,
                source="cost_state",
                cc_cost_usd=cc.cc_cost_usd,
                local_cost_usd=cc.local_cost_usd,
                gap_usd=cc.local_cost_usd - cc.cc_cost_usd,
                gap_pct=_pct_of(cc.cc_cost_usd, cc.local_cost_usd),
            )
        )
        covered.add(cc.session_id)

    by_session = _session_signals_by_id(corpus, config_dir)
    if not by_session:
        return out
    for bundle in corpus.sessions:
        session_id = bundle.session_id
        if session_id in covered:
            continue
        seen = by_session.get(session_id)
        if seen is None or seen.statusline_cost_usd is None:
            continue
        local_cost = _local_cost_for_bundle(bundle, pricing)
        out.append(
            CostGroundTruthGap(
                session_id=session_id,
                source="statusline",
                cc_cost_usd=seen.statusline_cost_usd,
                local_cost_usd=local_cost,
                gap_usd=local_cost - seen.statusline_cost_usd,
                gap_pct=_pct_of(seen.statusline_cost_usd, local_cost),
            )
        )
    return out


#: Q1 gap metric: the median absolute gap, as a percentage of the ground
#: truth, above which :func:`build_cost_ground_truth_gap_table` adds a
#: note -- the plan's own "> 5%" wording.
_GAP_NOTE_THRESHOLD_PCT = 5.0
#: ... and only once there are at least this many sessions with a
#: computable gap -- the plan's own "across >= 10 sessions" wording; a
#: median of fewer than that is too noisy to call out.
_GAP_NOTE_MIN_SESSIONS = 10


def build_cost_ground_truth_gap_table(gaps: list[CostGroundTruthGap], units: "Units | None" = None) -> Table | None:
    """The ``cost_ground_truth_gap`` table: one row per
    :class:`CostGroundTruthGap`, sorted by session id for a stable
    order, plus the plan's own median-gap note when the threshold is
    crossed. ``None`` when ``gaps`` is empty -- nothing to show, and
    :func:`reconcile` leaves the table out entirely rather than adding
    an empty one.
    """
    if not gaps:
        return None
    rows = sorted(gaps, key=lambda g: g.session_id)
    pct_values = [abs(g.gap_pct) for g in gaps if g.gap_pct is not None]

    notes = [
        "Gap = ClaudeGlass's own local pricing minus Claude Code's own reported cost (cost-state when a "
        "session has it, else the statusline's own SIG-4 ground truth); gap % is relative to Claude Code's "
        "own figure.",
        "Known reasons a correct local figure and Claude Code's own figure can still differ: an unknown "
        "model is priced at zero locally; cost-state is a running total as of when the transcript last "
        "wrote it, which can be mid-session; the statusline's own figure resets on /clear and may be "
        "logged mid-session too.",
    ]
    if len(pct_values) >= _GAP_NOTE_MIN_SESSIONS:
        median_pct = statistics.median(pct_values)
        if median_pct > _GAP_NOTE_THRESHOLD_PCT:
            median_usd = statistics.median([g.gap_usd for g in gaps if g.gap_pct is not None])
            amount = units.money(abs(median_usd)) if units is not None else None
            amount_text = amount.phrase() if amount is not None else f"${abs(median_usd):,.2f}"
            direction = "higher" if median_usd >= 0 else "lower"
            notes.append(
                f"Local pricing runs a median {median_pct:.0f}% {direction} than Claude Code's own reported "
                f"cost across {len(pct_values)} sessions (about {amount_text}) -- above the 5% threshold "
                "worth a closer look."
            )

    return Table(
        name="cost_ground_truth_gap",
        title="Local cost vs Claude Code's own reported cost",
        columns=[
            Column(key="session_id", label="Session", kind="str"),
            Column(key="source", label="Ground truth", kind="str"),
            Column(key="cc_cost_usd", label="Claude Code's own cost", kind="money"),
            Column(key="local_cost_usd", label="Local cost", kind="money"),
            Column(key="gap_usd", label="Gap", kind="money"),
            Column(key="gap_pct", label="Gap (% of Claude Code's own cost)", kind="pct"),
        ],
        rows=[[g.session_id, g.source, g.cc_cost_usd, g.local_cost_usd, g.gap_usd, g.gap_pct] for g in rows],
        notes=notes,
    )


def _collect_local_rows(corpus: Corpus, pricing: Pricing) -> list[dict]:
    """One row per priced turn in ``corpus``, same canonical shape
    :func:`parse_admin_csv` produces, so both sides can be grouped and
    summed identically.
    """
    rows: list[dict] = []
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        for tr in _transcripts_of(bundle):
            for turn in _priced_turns(tr):
                resolved = pricing.resolve_model(turn.model)
                breakdown = price_turn(turn, resolved)
                rows.append(
                    {
                        "date": _utc_day(turn.ts),
                        "model": turn.model or None,
                        "input_tokens": turn.input_tokens,
                        "cache_creation_tokens": turn.cache_creation_tokens,
                        "cache_read_tokens": turn.cache_read_tokens,
                        "output_tokens": turn.output_tokens,
                        "cost": breakdown.total,
                    }
                )
    return rows


def _filter_by_window(rows: list[dict], since: str | None, until: str | None) -> list[dict]:
    """Keep only rows whose ``date`` (an ISO ``YYYY-MM-DD`` string, so
    lexicographic and calendar order agree) falls inside
    ``[since, until]``. A row with no ``date`` is dropped whenever a
    window is active (nothing to compare it against); with no window at
    all, every row (including a dateless one) passes through unchanged.
    """
    if since is None and until is None:
        return rows
    kept = []
    for row in rows:
        date = row.get("date")
        if not date:
            continue
        if since is not None and date < since:
            continue
        if until is not None and date > until:
            continue
        kept.append(row)
    return kept


_ZERO_TOTALS: dict[str, float] = {
    "input_tokens": 0,
    "cache_creation_tokens": 0,
    "cache_read_tokens": 0,
    "output_tokens": 0,
    "cost": 0.0,
}


def _group_key(row: dict, by: tuple[str, ...]) -> tuple:
    parts = []
    if "day" in by:
        parts.append(row.get("date") or "<unknown day>")
    if "model" in by:
        parts.append(row.get("model") or "<unknown model>")
    return tuple(parts)


def _sum_rows(rows: list[dict], by: tuple[str, ...]) -> dict[tuple, dict]:
    groups: dict[tuple, dict] = {}
    for row in rows:
        acc = groups.setdefault(_group_key(row, by), dict(_ZERO_TOTALS))
        for field_name in _ZERO_TOTALS:
            acc[field_name] += row.get(field_name, 0)
    return groups


def _pct_of(base: float, actual: float) -> float | None:
    if base == 0:
        return None
    return (actual - base) / base * 100.0


_TOKEN_METRIC_SPECS: tuple[tuple[str, str], ...] = (
    ("input_tokens", "Input tokens"),
    ("cache_creation_tokens", "Cache-creation tokens"),
    ("cache_read_tokens", "Cache-read tokens"),
    ("output_tokens", "Output tokens"),
)

#: The fixed set of reasons a correct local figure and a correct Admin
#: figure can still legitimately differ (plan "Enterprise use"/"Finance").
KNOWN_DIFFERENCE_REASONS: tuple[str, ...] = (
    "subscription usage has no Admin cost",
    "other tools may use the same API key",
    "workspace filters on the Admin export",
    "UTC day boundaries (a session's turns are bucketed by UTC day here, the Admin export's own day boundary "
    "may differ)",
    "an unknown model is priced at zero locally",
)


def reconcile(
    corpus: Corpus,
    pricing: Pricing,
    config: Config,
    *,
    admin_rows: list[dict],
    unmapped_headers: list[str] | None = None,
    by: tuple[str, ...] = ("day",),
    since: str | None = None,
    until: str | None = None,
    config_dir: str | Path | None = None,
    units: "Units | None" = None,
) -> Section:
    """Build the ``reconcile`` :class:`Section`: one ``reconcile_by_period``
    table comparing this tool's own local per-turn accounting against
    ``admin_rows`` (:attr:`AdminCsvResult.rows`), grouped by ``by``
    (one of :data:`BY_CHOICES`), plus the Q1 gap metric's own
    ``cost_ground_truth_gap`` table (see the module docstring) whenever
    there's ground truth to show it against -- unlike the Admin-CSV
    table, that one needs no export at all: a ``cost-state`` line needs
    nothing further, and SIG-4's statusline ground truth needs only
    ``config_dir`` (to read, never write, the signals it already logged
    on this machine). So the table appears whenever either source has
    something for at least one session, rather than being gated on this
    command's own required ``--admin-csv``.

    ``since``/``until`` (ISO ``YYYY-MM-DD`` date strings, either or both
    ``None``) restrict both sides to the same window before grouping, so
    a partial admin export is compared against the matching local slice
    rather than the whole corpus. Delta is always local minus Admin;
    delta-% is that delta as a percentage of the Admin figure (Admin
    being the export a caller is reconciling *against*), never a
    significance claim. The gap table isn't windowed by ``since``/
    ``until`` itself -- it's already scoped to whatever ``corpus`` the
    caller assembled (the CLI's own ``--since``/``--until``/project
    selection), one row per session rather than per day.
    """
    if by not in BY_CHOICES:
        raise ValueError(f"reconcile: bad by={by!r}, expected one of {BY_CHOICES}")
    unmapped_headers = unmapped_headers or []

    local_rows = _filter_by_window(_collect_local_rows(corpus, pricing), since, until)
    admin_rows = _filter_by_window(admin_rows, since, until)

    local_groups = _sum_rows(local_rows, by)
    admin_groups = _sum_rows(admin_rows, by)
    all_keys = sorted(set(local_groups) | set(admin_groups))

    columns: list[Column] = []
    if "day" in by:
        columns.append(Column(key="day", label="Day", kind="str"))
    if "model" in by:
        columns.append(Column(key="model", label="Model", kind="str"))
    for field_name, label in _TOKEN_METRIC_SPECS:
        columns.append(Column(key=f"{field_name}_local", label=f"{label} (local)", kind="tokens"))
        columns.append(Column(key=f"{field_name}_admin", label=f"{label} (Admin)", kind="tokens"))
        columns.append(Column(key=f"{field_name}_delta", label=f"{label} delta", kind="tokens"))
        columns.append(Column(key=f"{field_name}_delta_pct", label=f"{label} delta (% of Admin)", kind="pct"))
    columns.append(Column(key="cost_local", label="Cost (local)", kind="money"))
    columns.append(Column(key="cost_admin", label="Cost (Admin)", kind="money"))
    columns.append(Column(key="cost_delta", label="Cost delta", kind="money"))
    columns.append(Column(key="cost_delta_pct", label="Cost delta (% of Admin)", kind="pct"))

    period_col_count = (1 if "day" in by else 0) + (1 if "model" in by else 0)

    def _row_for(key: tuple, local: dict, admin: dict) -> list:
        row = list(key)
        for field_name, _label in _TOKEN_METRIC_SPECS:
            local_v = local.get(field_name, 0)
            admin_v = admin.get(field_name, 0)
            row.extend([local_v, admin_v, local_v - admin_v, _pct_of(admin_v, local_v)])
        cost_local = local.get("cost", 0.0)
        cost_admin = admin.get("cost", 0.0)
        row.extend([cost_local, cost_admin, cost_local - cost_admin, _pct_of(cost_admin, cost_local)])
        return row

    rows = [_row_for(key, local_groups.get(key, dict(_ZERO_TOTALS)), admin_groups.get(key, dict(_ZERO_TOTALS))) for key in all_keys]

    totals_local = dict(_ZERO_TOTALS)
    totals_admin = dict(_ZERO_TOTALS)
    for key in all_keys:
        for field_name in _ZERO_TOTALS:
            totals_local[field_name] += local_groups.get(key, dict(_ZERO_TOTALS)).get(field_name, 0)
            totals_admin[field_name] += admin_groups.get(key, dict(_ZERO_TOTALS)).get(field_name, 0)
    total_key = ("TOTAL",) + ("-",) * (period_col_count - 1) if period_col_count > 1 else ("TOTAL",)
    rows.append(_row_for(total_key, totals_local, totals_admin))

    notes = [
        "Delta = local minus Admin; delta % is relative to the Admin figure.",
        "Known reasons a correct local figure and a correct Admin figure can still differ: "
        + "; ".join(KNOWN_DIFFERENCE_REASONS) + ".",
    ]
    if unmapped_headers:
        notes.append(f"Admin CSV column(s) not recognised and ignored: {', '.join(unmapped_headers)}.")

    table = Table(
        name="reconcile_by_period",
        title=f"Local vs Admin usage/cost (by {', '.join(by)})",
        columns=columns,
        rows=rows,
        notes=notes,
    )
    tables = [table]
    # Q1 gap metric: its own table, own notes (kept separate from the
    # Admin-CSV table's notes above -- two different comparisons, each
    # with its own caveats) -- added only when there's something to show.
    gap_table = build_cost_ground_truth_gap_table(cost_ground_truth_gaps(corpus, pricing, config_dir), units)
    if gap_table is not None:
        tables.append(gap_table)
    return Section(key="reconcile", title="Admin CSV reconciliation", tables=tables, notes=notes)


__all__ = [
    "ReconcileError",
    "AdminCsvResult",
    "parse_admin_csv",
    "reconcile",
    "BY_CHOICES",
    "KNOWN_DIFFERENCE_REASONS",
    "ClaudeCodeCost",
    "claude_code_reported_costs",
    "CostGroundTruthGap",
    "cost_ground_truth_gaps",
    "build_cost_ground_truth_gap_table",
]
