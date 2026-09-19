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
total either way.

Day bucketing: this module buckets its own local per-turn totals by the
turn's own timestamp's **UTC calendar day** (not ``config.tz``), on the
assumption that an Admin usage/cost export buckets by UTC day too. This
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
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .corpus import Corpus, SessionBundle
from .model import Column, Section, Table, TranscriptResult, Turn
from .pricing import Pricing, price_turn

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


def _build_row(raw_row: list[str], index_to_slot: dict[int, str], line_num: int) -> dict:
    values: dict[str, str] = {}
    for i, cell in enumerate(raw_row):
        slot = index_to_slot.get(i)
        if slot is not None:
            values[slot] = cell
    try:
        date = values.get("date", "").strip()
        if not date:
            raise ValueError("empty date")
        model = values.get("model", "").strip() or None
        input_tokens = _parse_int(values.get("input_tokens", "0"))
        output_tokens = _parse_int(values.get("output_tokens", "0"))
        cache_read_tokens = _parse_int(values.get("cache_read_tokens", "0"))
        cache_creation_tokens = (
            _parse_int(values.get("cache_creation_tokens", "0"))
            + _parse_int(values.get("cache_creation_tokens_5m", "0"))
            + _parse_int(values.get("cache_creation_tokens_1h", "0"))
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
    "UTC day boundaries (a session's turns are bucketed by local time here, the Admin export's own day boundary "
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
) -> Section:
    """Build the ``reconcile`` :class:`Section`: one ``reconcile_by_period``
    table comparing this tool's own local per-turn accounting against
    ``admin_rows`` (:attr:`AdminCsvResult.rows`), grouped by ``by``
    (one of :data:`BY_CHOICES`).

    ``since``/``until`` (ISO ``YYYY-MM-DD`` date strings, either or both
    ``None``) restrict both sides to the same window before grouping, so
    a partial admin export is compared against the matching local slice
    rather than the whole corpus. Delta is always local minus Admin;
    delta-% is that delta as a percentage of the Admin figure (Admin
    being the export a caller is reconciling *against*), never a
    significance claim.
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
    return Section(key="reconcile", title="Admin CSV reconciliation", tables=[table], notes=notes)


__all__ = [
    "ReconcileError",
    "AdminCsvResult",
    "parse_admin_csv",
    "reconcile",
    "BY_CHOICES",
    "KNOWN_DIFFERENCE_REASONS",
]
