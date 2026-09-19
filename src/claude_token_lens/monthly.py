"""Monthly finance report (S1-exports, plan "Finance" / feature 10
"Scheduled reports", promoted to v0.2 ``serve --monthly-report``):
``claude-token-lens monthly-report --out DIR [--month YYYY-MM]`` writes
``DIR/claude-token-lens-YYYY-MM.md`` and the matching ``.html`` for one
calendar month.

Scope: deliberately just a finance summary, not the full multi-section
report ``report`` prints -- this is the scheduled, habit-forming
artefact the plan's "Finance" section describes (cost/tokens by
model/project/entrypoint, five-hour blocks for subscription billing),
so the body is a short finance header table followed by the ``usage``
section alone (``build_report(..., include={"usage"})``), not
recache/ttl/compaction/topology/etc., which are optimisation-focused
rather than finance-focused.

Month attribution: a session is attributed to the calendar month of its
*first* top-level turn's local timestamp (``config.tz``, the same
fallback-to-machine-zone convention ``usage.py``'s/``classify.py``'s own
``_to_local`` use -- duplicated here per this project's small-helper
convention). A session whose turns straddle a month boundary is
therefore counted wholly in the month it started, not split across two
reports -- a documented approximation, the same kind ``usage.py``'s own
five-hour-block grid already accepts for a similar reason (no exact
per-turn slicing without touching every other section's own per-session
assumptions).

Idempotency: the same ``(corpus, pricing, config, month)`` must produce
byte-identical files across repeated runs (the acceptance criterion),
so the report body carries no wall-clock value anywhere -- unlike
``render/markdown.py``/``render/html.py``'s own ``render_markdown``/
``render_html``, which bake ``model.meta.generated_at`` into a bullet
near the top and are therefore *not* used here. This module renders its
own compact tables (via ``render.tables.format_cell``/``escape_md``,
the same formatting primitives every other renderer already shares) and
places the *only* wall-clock value, a "Generated at: ..." line, as the
last line of the Markdown file and inside an HTML comment immediately
before ``</body>`` of the HTML file -- a test asserting idempotency
strips that one line/comment before comparing.

Entry point for the service (S1-integration): ``write_monthly_report``
is the function the sibling package's ``service.serve`` wires up to
``serve --monthly-report DIR`` (documented in ``docs/exports.md``), so
it takes an already-loaded ``corpus``/``pricing``/``config`` rather than
loading them itself, matching ``report.build_report``'s own "caller
loads, this function only assembles" contract.
"""

from __future__ import annotations

import dataclasses
import html as _html_mod
import re
from calendar import monthrange
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Config
from .corpus import Corpus, SessionBundle
from .model import ReportModel, Table
from .pricing import Pricing
from .render.tables import escape_md, format_cell
from .report import build_report

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

_NUMERIC_KINDS = frozenset({"int", "float", "pct", "money", "tokens", "secs"})


def resolve_month(month_str: str | None) -> str:
    """``month_str`` validated as ``YYYY-MM``, or (when ``None``/empty)
    the previous calendar month relative to today, in ``YYYY-MM`` form.
    Raises ``ValueError`` with a one-line, CLI-printable reason on a
    malformed value.
    """
    if not month_str:
        today = date.today()
        year, month = today.year, today.month
        if month == 1:
            return f"{year - 1:04d}-12"
        return f"{year:04d}-{month - 1:02d}"
    if not _MONTH_RE.match(month_str):
        raise ValueError(f"--month must be YYYY-MM, got {month_str!r}")
    return month_str


# -- small helpers duplicated per this project's convention ------------------


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_local(dt: datetime, tz: str | None) -> datetime:
    if tz:
        try:
            return dt.astimezone(ZoneInfo(tz))
        except (ZoneInfoNotFoundError, ValueError):
            return dt.astimezone()
    return dt.astimezone()


# -- month-scoped corpus filtering -------------------------------------------


def _session_local_month(bundle: SessionBundle, tz: str | None) -> str | None:
    """The ``YYYY-MM`` of ``bundle``'s earliest top-level turn's local
    timestamp, or ``None`` when it has no top-level transcript or no
    turn with a parseable timestamp."""
    if bundle.top is None:
        return None
    for turn in bundle.top.turns:
        parsed = _parse_ts(turn.ts)
        if parsed is not None:
            return _to_local(parsed, tz).strftime("%Y-%m")
    return None


def filter_corpus_to_month(corpus: Corpus, month: str, tz: str | None) -> Corpus:
    """A new :class:`Corpus` carrying only the sessions attributed to
    ``month`` (see the module docstring's month-attribution rule)."""
    sessions = [b for b in corpus.sessions if _session_local_month(b, tz) == month]
    return dataclasses.replace(corpus, sessions=sessions)


# -- finance header -----------------------------------------------------


def _find_table(model: ReportModel, section_key: str, table_name: str) -> Table | None:
    for section in model.sections:
        if section.key != section_key:
            continue
        for table in section.tables:
            if table.name == table_name:
                return table
    return None


def _regroup_by_model(by_month_table: Table | None) -> Table:
    """``by_month``'s ``(period, model, turns, tokens, cost)`` rows,
    regrouped to ``(model, turns, tokens, cost)`` -- ``period`` is
    dropped since every row already belongs to the single target month
    (the corpus was pre-filtered), and any rows that still share a model
    (a timezone edge landing two period buckets on the same calendar
    month) are summed together rather than left duplicated.
    """
    totals: dict[str, list] = {}
    for row in (by_month_table.rows if by_month_table else []):
        _period, model, turns, tokens, cost = row
        bucket = totals.setdefault(model, [0, 0, 0.0])
        bucket[0] += turns
        bucket[1] += tokens
        bucket[2] += cost
    rows = [[model, turns, tokens, cost] for model, (turns, tokens, cost) in sorted(totals.items(), key=lambda kv: -kv[1][2])]
    from .model import Column

    return Table(
        name="cost_by_model",
        title="Cost by model",
        columns=[
            Column(key="model", label="Model", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="tokens", label="Tokens", kind="tokens"),
            Column(key="cost", label="Cost", kind="money"),
        ],
        rows=rows,
    )


def _finance_summary_table(
    *, total_cost: float, total_tokens: int, sessions: int, five_hour_blocks_used: int | None, currency: str
) -> Table:
    from .model import Column

    rows = [
        ["Total cost", total_cost],
        ["Total tokens", total_tokens],
        ["Sessions", sessions],
    ]
    if five_hour_blocks_used is not None:
        rows.append(["Five-hour blocks used", five_hour_blocks_used])
    kinds = ["money", "tokens", "int", "int"]
    formatted_rows = [
        [label, format_cell(value, kinds[i], currency)] for i, (label, value) in enumerate(rows)
    ]
    return Table(
        name="finance_summary",
        title="Finance summary",
        columns=[Column(key="metric", label="Metric", kind="str"), Column(key="value", label="Value", kind="str")],
        rows=formatted_rows,
    )


# -- local, deterministic renderers (see module docstring) -------------------


def _md_table(table: Table, currency: str) -> list[str]:
    lines = [f"### {table.title}", ""]
    aligns = ["right" if c.kind in _NUMERIC_KINDS else "left" for c in table.columns]
    header = "| " + " | ".join(escape_md(c.label) for c in table.columns) + " |"
    divider = "| " + " | ".join(("---:" if a == "right" else "---") for a in aligns) + " |"
    lines.append(header)
    lines.append(divider)
    for row in table.rows:
        cells = []
        for value, column in zip(row, table.columns):
            # finance_summary's own "value" column is pre-formatted text
            # (mixed units row to row), everything else formats by kind.
            cell_text = value if (table.name == "finance_summary" and column.key == "value") else format_cell(value, column.kind, currency)
            cells.append(escape_md(cell_text))
        lines.append("| " + " | ".join(cells) + " |")
    if table.notes:
        lines.append("")
        lines.extend(f"- {note}" for note in table.notes)
    return lines


def _render_month_markdown(month: str, tables: list[Table], currency: str, generated_at: str) -> str:
    lines = [f"# Claude token lens — Monthly report — {month}", ""]
    for table in tables:
        lines.extend(_md_table(table, currency))
        lines.append("")
    lines.append(f"Generated at: {generated_at}")
    return "\n".join(lines).rstrip("\n") + "\n"


def _esc(value) -> str:
    return _html_mod.escape(str(value), quote=True)


def _html_table(table: Table, currency: str) -> str:
    thead = "<tr>" + "".join(f"<th>{_esc(c.label)}</th>" for c in table.columns) + "</tr>"
    body_rows = []
    for row in table.rows:
        cells = []
        for value, column in zip(row, table.columns):
            cell_text = value if (table.name == "finance_summary" and column.key == "value") else format_cell(value, column.kind, currency)
            cells.append(f"<td>{_esc(cell_text)}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    notes_html = ""
    if table.notes:
        notes_html = "<ul>" + "".join(f"<li>{_esc(note)}</li>" for note in table.notes) + "</ul>"
    return f"<h2>{_esc(table.title)}</h2><table><thead>{thead}</thead><tbody>{''.join(body_rows)}</tbody></table>{notes_html}"


def _render_month_html(month: str, tables: list[Table], currency: str, generated_at: str) -> str:
    tables_html = "".join(_html_table(t, currency) for t in tables)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{_esc(f'Claude token lens — Monthly report — {month}')}</title>\n"
        "<style>table{border-collapse:collapse;margin-bottom:1.5em}"
        "th,td{border:1px solid #ccc;padding:4px 8px;text-align:left}</style>\n"
        "</head>\n"
        "<body>\n"
        f"<h1>{_esc(f'Claude token lens — Monthly report — {month}')}</h1>\n"
        f"{tables_html}\n"
        f"<!-- Generated at: {_esc(generated_at)} -->\n"
        "</body>\n"
        "</html>\n"
    )


# -- top-level entry point ------------------------------------------------


def write_monthly_report(corpus: Corpus, pricing: Pricing, config: Config, month: str, out_dir: str | Path) -> list[Path]:
    """Write ``claude-token-lens-<month>.md`` and ``.html`` into
    ``out_dir`` (created if absent) for the given ``month`` (``YYYY-MM``,
    see :func:`resolve_month`). Returns the two paths written, in that
    order.
    """
    filtered = filter_corpus_to_month(corpus, month, config.tz)
    projects = tuple(sorted({b.slug for b in filtered.sessions if b.slug}))
    year, month_num = (int(part) for part in month.split("-"))
    days_in_month = monthrange(year, month_num)[1]
    window = f"{month}-01 to {month}-{days_in_month:02d} (calendar month)"

    model = build_report(
        filtered,
        pricing,
        config,
        projects=projects,
        window=window,
        include={"usage"},
    )

    by_month_table = _find_table(model, "usage", "by_month")
    by_project_table = _find_table(model, "usage", "by_project")
    by_entrypoint_table = _find_table(model, "usage", "by_entrypoint")
    five_hour_table = _find_table(model, "usage", "five_hour_blocks")

    total_cost = sum(row[4] for row in (by_month_table.rows if by_month_table else []))
    total_tokens = sum(row[3] for row in (by_month_table.rows if by_month_table else []))
    five_hour_blocks_used = (
        len(five_hour_table.rows) if (config.billing == "subscription" and five_hour_table is not None) else None
    )

    currency = model.meta.pricing.currency
    finance_summary = _finance_summary_table(
        total_cost=total_cost,
        total_tokens=total_tokens,
        sessions=len(filtered.sessions),
        five_hour_blocks_used=five_hour_blocks_used,
        currency=currency,
    )
    cost_by_model = _regroup_by_model(by_month_table)
    cost_by_project = dataclasses.replace(by_project_table, title="Cost by project") if by_project_table else None
    cost_by_entrypoint = dataclasses.replace(by_entrypoint_table, title="Cost by entrypoint") if by_entrypoint_table else None

    header_tables = [finance_summary, cost_by_model]
    if cost_by_project is not None:
        header_tables.append(cost_by_project)
    if cost_by_entrypoint is not None:
        header_tables.append(cost_by_entrypoint)

    usage_section = next((s for s in model.sections if s.key == "usage"), None)
    body_tables = list(usage_section.tables) if usage_section else []

    all_tables = header_tables + body_tables

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().astimezone().isoformat()

    md_path = out_path / f"claude-token-lens-{month}.md"
    html_path = out_path / f"claude-token-lens-{month}.html"
    md_path.write_text(_render_month_markdown(month, all_tables, currency, generated_at), encoding="utf-8")
    html_path.write_text(_render_month_html(month, all_tables, currency, generated_at), encoding="utf-8")
    return [md_path, html_path]


__all__ = [
    "resolve_month",
    "filter_corpus_to_month",
    "write_monthly_report",
]
