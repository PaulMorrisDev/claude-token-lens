"""HTML renderer: ``ReportModel`` -> one self-contained HTML file.

"Self-contained" means no external reference of any kind: no stylesheet
or script ``src``/``href``, no ``@import``, no ``url(...)``, no bare
``http://``/``https://`` literal. Everything the page needs (CSS, the
optional click-to-sort script) is inlined. ``tests/test_render.py``
greps the rendered output for those substrings and fails on any hit, so
this module must never introduce one.

Note on the plan's pricing ``source_url``: that field lives in
``pricing.toml`` (parsed by WP2), not on ``model.PricingMeta`` in this
codebase (``path``, ``version``, ``sha8``, ``currency``,
``coverage_pct`` only — see ``model.py``'s deviation note). There is
therefore nothing to strip a scheme from today. If a future work
package adds it to ``PricingMeta``, render it as plain escaped text
(never as an ``<a href>``) to keep this module's no-external-reference
guarantee.

Every piece of report text is passed through ``html.escape`` before
being placed in the page, whether as element content or as an
attribute value.
"""

from __future__ import annotations

import dataclasses
import html as _html

from ..model import Diagnostics, ReportModel, Table
from .tables import format_cell, format_evidence_value

#: Column kinds that read as quantities: right-aligned, sortable numerically.
_NUMERIC_KINDS = frozenset({"int", "float", "pct", "money", "tokens", "secs"})

_STYLE = """
:root {
  --bg: #ffffff;
  --fg: #1a1a1a;
  --muted: #666666;
  --border: #d8d8d8;
  --header-bg: #f2f2f2;
  --zebra: #f8f8f8;
  --accent: #2563eb;
  --bar-bg: #cfe0fb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #121212;
    --fg: #e8e8e8;
    --muted: #a3a3a3;
    --border: #3a3a3a;
    --header-bg: #1e1e1e;
    --zebra: #191919;
    --accent: #7aa8f7;
    --bar-bg: #223a5e;
  }
}
* {
  box-sizing: border-box;
}
body {
  margin: 0;
  padding: 1.5rem;
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  line-height: 1.5;
}
h1 {
  font-size: 1.6rem;
}
h2 {
  margin-top: 2rem;
  padding-bottom: 0.25rem;
  border-bottom: 1px solid var(--border);
}
h3 {
  margin-top: 1.25rem;
}
table {
  width: 100%;
  border-collapse: collapse;
  margin: 0.5rem 0 1rem;
  font-size: 0.9rem;
}
th, td {
  padding: 0.35rem 0.6rem;
  border: 1px solid var(--border);
  text-align: left;
}
thead th {
  position: sticky;
  top: 0;
  background: var(--header-bg);
  cursor: pointer;
  user-select: none;
}
tbody tr:nth-child(even) {
  background: var(--zebra);
}
td.num, th.num {
  text-align: right;
}
.bar {
  display: inline-block;
  height: 0.5em;
  margin-right: 0.4em;
  vertical-align: middle;
  background: var(--bar-bg);
  border-radius: 2px;
}
.notes, .evidence-list {
  color: var(--muted);
  font-size: 0.85rem;
}
.rec {
  margin: 0.75rem 0;
  padding: 0.75rem 1rem;
  border: 1px solid var(--border);
  border-left: 4px solid var(--accent);
  border-radius: 4px;
}
footer {
  margin-top: 2rem;
  color: var(--muted);
  font-size: 0.8rem;
}
""".strip()

_SCRIPT = """
(function () {
  document.querySelectorAll("table.sortable").forEach(function (table) {
    var heads = table.querySelectorAll("th");
    heads.forEach(function (th, index) {
      th.addEventListener("click", function () {
        var tbody = table.querySelector("tbody");
        var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
        var ascending = th.getAttribute("data-dir") !== "asc";
        heads.forEach(function (h) { h.removeAttribute("data-dir"); });
        th.setAttribute("data-dir", ascending ? "asc" : "desc");
        rows.sort(function (a, b) {
          var av = a.children[index].getAttribute("data-sort") || "";
          var bv = b.children[index].getAttribute("data-sort") || "";
          var an = parseFloat(av);
          var bn = parseFloat(bv);
          var cmp;
          if (!isNaN(an) && !isNaN(bn)) {
            cmp = an - bn;
          } else {
            cmp = av.localeCompare(bv);
          }
          return ascending ? cmp : -cmp;
        });
        rows.forEach(function (row) { tbody.appendChild(row); });
      });
    });
  });
})();
""".strip()


def _esc(value) -> str:
    return _html.escape("" if value is None else str(value))


def _list_html(lines: list[str], css_class: str) -> str:
    items = "".join(f"<li>{_esc(line)}</li>" for line in lines)
    return f'<ul class="{css_class}">{items}</ul>'


def _meta_lines(model: ReportModel) -> list[str]:
    meta = model.meta
    pricing = meta.pricing
    lines = [
        f"Tool version: {meta.tool_version or '-'}",
        f"Generated at: {meta.generated_at or '-'}",
        f"Window: {meta.window or '-'}",
        f"Projects: {', '.join(meta.projects) if meta.projects else '-'}",
        "Pricing: "
        + ", ".join(
            [
                f"path={pricing.path or '-'}",
                f"version={pricing.version or '-'}",
                f"sha8={pricing.sha8 or '-'}",
                f"currency={pricing.currency}",
                f"coverage={pricing.coverage_pct:.1f}%",
            ]
        ),
        f"Billing mode: {meta.billing_mode}",
    ]
    thresholds = ", ".join(f"{k}={v}" for k, v in meta.thresholds.items()) or "-"
    lines.append(f"Thresholds: {thresholds}")
    return lines


def _assumptions_lines(model: ReportModel) -> list[str]:
    return list(model.meta.assumptions) or ["None recorded."]


def _diagnostics_lines(model: ReportModel) -> list[str]:
    lines = []
    diagnostics = model.diagnostics
    for field_def in dataclasses.fields(Diagnostics):
        value = getattr(diagnostics, field_def.name)
        if isinstance(value, dict):
            value = ", ".join(f"{k}={v}" for k, v in value.items()) if value else "-"
        lines.append(f"{field_def.name}: {value}")
    return lines


def _table_html(table: Table, currency: str, table_id: str) -> str:
    head_cells = []
    for column in table.columns:
        cls_attr = ' class="num"' if column.kind in _NUMERIC_KINDS else ""
        head_cells.append(
            f'<th{cls_attr} data-key="{_esc(column.key)}">{_esc(column.label)}</th>'
        )
    thead = f"<thead><tr>{''.join(head_cells)}</tr></thead>"

    body_rows = []
    for row in table.rows:
        cells = []
        for value, column in zip(row, table.columns):
            display = _esc(format_cell(value, column.kind, currency))
            cls_attr = ' class="num"' if column.kind in _NUMERIC_KINDS else ""
            sort_value = "" if value is None else str(value)
            cell_html = display
            if column.kind == "pct" and value is not None:
                try:
                    pct = max(0.0, min(100.0, float(value)))
                except (TypeError, ValueError):
                    pct = 0.0
                cell_html = (
                    f'<span class="bar" style="width:{pct:.1f}%"></span>'
                    f'<span class="bar-text">{display}</span>'
                )
            cells.append(
                f'<td{cls_attr} data-sort="{_esc(sort_value)}">{cell_html}</td>'
            )
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    tbody = f"<tbody>{''.join(body_rows)}</tbody>"

    notes_html = ""
    if table.notes:
        notes_html = (
            '<ul class="notes">'
            + "".join(f"<li>{_esc(note)}</li>" for note in table.notes)
            + "</ul>"
        )

    return (
        f"<h3>{_esc(table.title)}</h3>"
        f'<table class="sortable" id="{_esc(table_id)}">{thead}{tbody}</table>'
        f"{notes_html}"
    )


def _sections_html(model: ReportModel) -> str:
    currency = model.meta.pricing.currency
    parts = []
    for section_index, section in enumerate(model.sections):
        parts.append(f"<section><h2>{_esc(section.title)}</h2>")
        for table_index, table in enumerate(section.tables):
            table_id = f"table-{section_index}-{table_index}"
            parts.append(_table_html(table, currency, table_id))
        if section.notes:
            parts.append(
                '<ul class="notes">'
                + "".join(f"<li>{_esc(note)}</li>" for note in section.notes)
                + "</ul>"
            )
        parts.append("</section>")
    return "".join(parts)


def _recommendations_html(model: ReportModel) -> str:
    if not model.recommendations:
        return "<p>None.</p>"
    currency = model.meta.pricing.currency
    parts = []
    for rec in model.recommendations:
        parts.append('<article class="rec">')
        parts.append(f"<h3>[{_esc(rec.severity)}] {_esc(rec.title)}</h3>")
        parts.append(f"<p>Action: {_esc(rec.action)}</p>")
        if rec.lever:
            parts.append(f"<p>Lever: {_esc(rec.lever)} (scope: {_esc(rec.scope)})</p>")
        if rec.evidence:
            parts.append('<p>Evidence:</p><ul class="evidence-list">')
            for label, value, source_table, row_key in rec.evidence:
                # Fix A3: format the cited value using its home table
                # column's kind, same as the Markdown renderer -- see
                # render/tables.py's module docstring.
                formatted = format_evidence_value(model, value, source_table, row_key, currency)
                text = f"{label}: {formatted} (table {source_table}, row {row_key})"
                parts.append(f"<li>{_esc(text)}</li>")
            parts.append("</ul>")
        parts.append("</article>")
    return "".join(parts)


def render_html(model: ReportModel) -> str:
    """Render ``model`` as a single, self-contained HTML document with
    inline CSS and an optional inline click-to-sort script.
    """
    meta_html = _list_html(_meta_lines(model), "meta-list")
    assumptions_html = _list_html(_assumptions_lines(model), "assumptions-list")
    sections_html = _sections_html(model)
    recommendations_html = _recommendations_html(model)
    diagnostics_html = _list_html(_diagnostics_lines(model), "diagnostics-list")

    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{_esc('Claude token lens report')}</title>\n"
        f"<style>{_STYLE}</style>\n"
        "</head>\n"
        "<body>\n"
        "<h1>Claude token lens report</h1>\n"
        f"<section><h2>Overview</h2>{meta_html}</section>\n"
        f"<section><h2>Assumptions</h2>{assumptions_html}</section>\n"
        f"{sections_html}\n"
        f"<section><h2>Recommendations</h2>{recommendations_html}</section>\n"
        f"<section><h2>Diagnostics</h2>{diagnostics_html}</section>\n"
        f"<script>{_SCRIPT}</script>\n"
        "</body>\n"
        "</html>\n"
    )


__all__ = ["render_html"]
