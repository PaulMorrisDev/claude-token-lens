"""Markdown renderer: ``ReportModel`` -> a single GitHub-flavoured
Markdown document.

Layout (fixed, see the project plan's "Renderers and CLI" section):

1. ``# Claude token lens report``
2. A meta block (tool version, generated at, window, projects, pricing
   provenance, billing mode, thresholds).
3. ``## Assumptions`` (from ``model.meta.assumptions``).
4. ``## <Section.title>`` per section, each ``Table`` as a GitHub pipe
   table plus its notes.
5. ``## Recommendations``.
6. ``## Diagnostics``.

No emoji. British English in every fixed string this module writes.
"""

from __future__ import annotations

import dataclasses

from ..model import Diagnostics, ReportModel, Table
from .tables import escape_md, format_cell

#: Column kinds that read as quantities and so are right-aligned by
#: default in a pipe table, unless the column overrides ``align``.
_NUMERIC_KINDS = frozenset({"int", "float", "pct", "money", "tokens", "secs"})


def _align_for(kind: str, align: str | None) -> str:
    if align in ("left", "right", "center"):
        return align
    return "right" if kind in _NUMERIC_KINDS else "left"


def _alignment_marker(align: str) -> str:
    if align == "right":
        return "---:"
    if align == "center":
        return ":---:"
    return "---"


def _render_meta(model: ReportModel) -> list[str]:
    meta = model.meta
    pricing = meta.pricing
    lines = [
        f"- Tool version: {meta.tool_version or '-'}",
        f"- Generated at: {meta.generated_at or '-'}",
        f"- Window: {meta.window or '-'}",
        f"- Projects: {', '.join(meta.projects) if meta.projects else '-'}",
        "- Pricing: "
        + ", ".join(
            [
                f"path={pricing.path or '-'}",
                f"version={pricing.version or '-'}",
                f"sha8={pricing.sha8 or '-'}",
                f"currency={pricing.currency}",
                f"coverage={pricing.coverage_pct:.1f}%",
            ]
        ),
        f"- Billing mode: {meta.billing_mode}",
    ]
    if meta.thresholds:
        thresholds = ", ".join(f"{k}={v}" for k, v in meta.thresholds.items())
    else:
        thresholds = "-"
    lines.append(f"- Thresholds: {thresholds}")
    return lines


def _render_assumptions(model: ReportModel) -> list[str]:
    lines = ["## Assumptions", ""]
    if model.meta.assumptions:
        lines.extend(f"- {text}" for text in model.meta.assumptions)
    else:
        lines.append("- None recorded.")
    return lines


def _render_table(table: Table, currency: str) -> list[str]:
    lines = [f"### {table.title}", ""]
    aligns = [_align_for(column.kind, column.align) for column in table.columns]
    header = "| " + " | ".join(escape_md(column.label) for column in table.columns) + " |"
    divider = "| " + " | ".join(_alignment_marker(a) for a in aligns) + " |"
    lines.append(header)
    lines.append(divider)
    for row in table.rows:
        cells = [
            escape_md(format_cell(value, column.kind, currency))
            for value, column in zip(row, table.columns)
        ]
        lines.append("| " + " | ".join(cells) + " |")
    if table.notes:
        lines.append("")
        lines.extend(f"- {note}" for note in table.notes)
    return lines


def _render_sections(model: ReportModel) -> list[str]:
    currency = model.meta.pricing.currency
    lines: list[str] = []
    for section in model.sections:
        lines.append(f"## {section.title}")
        lines.append("")
        for table in section.tables:
            lines.extend(_render_table(table, currency))
            lines.append("")
        if section.notes:
            lines.extend(f"- {note}" for note in section.notes)
            lines.append("")
    return lines


def _render_recommendations(model: ReportModel) -> list[str]:
    lines = ["## Recommendations", ""]
    if not model.recommendations:
        lines.append("None.")
        return lines
    for rec in model.recommendations:
        lines.append(f"### [{rec.severity}] {rec.title}")
        lines.append("")
        lines.append(f"Action: {rec.action}")
        if rec.lever:
            lines.append("")
            lines.append(f"Lever: {rec.lever} (scope: {rec.scope})")
        if rec.evidence:
            lines.append("")
            lines.append("Evidence:")
            for label, value, source_table, row_key in rec.evidence:
                lines.append(f"- {label}: {value} (table {source_table}, row {row_key})")
        lines.append("")
    return lines


def _render_diagnostics(model: ReportModel) -> list[str]:
    lines = ["## Diagnostics", ""]
    diagnostics = model.diagnostics
    for field_def in dataclasses.fields(Diagnostics):
        value = getattr(diagnostics, field_def.name)
        if isinstance(value, dict):
            if value:
                value = ", ".join(f"{k}={v}" for k, v in value.items())
            else:
                value = "-"
        lines.append(f"- {field_def.name}: {value}")
    return lines


def render_markdown(model: ReportModel) -> str:
    """Render ``model`` as a single Markdown document (see module
    docstring for the fixed section order).
    """
    lines: list[str] = ["# Claude token lens report", ""]
    lines.extend(_render_meta(model))
    lines.append("")
    lines.extend(_render_assumptions(model))
    lines.append("")
    lines.extend(_render_sections(model))
    lines.extend(_render_recommendations(model))
    lines.append("")
    lines.extend(_render_diagnostics(model))
    return "\n".join(lines).rstrip("\n") + "\n"


__all__ = ["render_markdown"]
