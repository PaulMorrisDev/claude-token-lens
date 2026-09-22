"""Cell formatting primitives shared by every renderer.

``format_cell`` is the single place a raw value becomes display text, so
Markdown, JSON, CSV and HTML renderers all agree on numeric formatting
(thousands separators, decimal places, unit suffixes). Renderers must not
format numbers themselves — they call ``format_cell`` per ``Column.kind``.

v0.2.0 fix A3: a ``Recommendation.evidence`` tuple (``label, value,
source_table, row_key``) carries no column reference of its own, so the
Markdown/HTML renderers used to print its raw ``value`` unformatted --
``63.749066571507974`` rather than ``63.7%``, ``47345.372881355936``
rather than ``47,345`` (a "tokens"-kind cell). :func:`resolve_evidence_column_kind` looks
the cited ``source_table``/``row_key`` up in the ``ReportModel`` the
evidence came from and finds which of that row's columns actually holds
``value`` (the same value-to-cell match ``tests/test_recommend_contract.
py``'s own evidence check already relies on), so
:func:`format_evidence_value` can format it exactly as it appears in its
home table. The JSON renderer is deliberately untouched -- it serialises
``Recommendation.evidence`` as raw dataclass data via ``to_jsonable``
(rounded floats only), never through ``format_cell``, so a JSON consumer
still gets the exact number rather than a display string.
"""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    from ..model import ReportModel

_KINDS = ("str", "int", "float", "pct", "money", "tokens", "secs")


def _format_secs(value: float) -> str:
    """Render a duration in seconds as compact ``1h 2m 3s`` style,
    dropping leading zero units (``83`` -> ``1m 23s``, ``45`` -> ``45s``).
    """
    total = int(round(value))
    sign = "-" if total < 0 else ""
    total = abs(total)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if hours or minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return sign + " ".join(parts)


def format_cell(value, kind: str, currency: str = "USD") -> str:
    """Render ``value`` as display text for a table column of the given
    ``kind``.

    kind is one of "str", "int", "float", "pct", "money", "tokens", "secs".
    ``None`` always renders as ``"-"``, regardless of kind.

    - str: as-is via ``str()``.
    - int: thousands separators, e.g. ``1,234,567``.
    - float: thousands separators, 2 decimal places.
    - pct: 1 decimal place with a trailing ``%``, e.g. ``12.3%``.
    - money: 2 decimal places with a trailing currency code,
      e.g. ``12.34 USD``.
    - tokens: integers with thousands separators, e.g. ``12,345``.
    - secs: compact duration, e.g. ``1m 23s``.
    """
    if kind not in _KINDS:
        raise ValueError(f"unknown column kind: {kind!r}")
    if value is None:
        return "-"
    if kind == "str":
        return str(value)
    if kind == "int":
        return f"{int(value):,}"
    if kind == "float":
        return f"{float(value):,.2f}"
    if kind == "pct":
        return f"{float(value):.1f}%"
    if kind == "money":
        return f"{float(value):,.2f} {currency}"
    if kind == "tokens":
        return f"{int(round(float(value))):,}"
    if kind == "secs":
        return _format_secs(float(value))
    raise AssertionError("unreachable")  # pragma: no cover


def _evidence_cell_matches(cell, value) -> bool:
    """Whether a table cell and an evidence tuple's cited value are "the
    same" number/string, tolerating the usual int/float and str/number
    crossings a value can pick up on its way from analytics code into a
    ``Recommendation.evidence`` tuple. Mirrors ``tests/
    test_recommend_contract.py``'s own ``_value_matches_some_cell``."""
    if isinstance(cell, bool) or isinstance(value, bool):
        return cell is value
    if isinstance(cell, (int, float)) and isinstance(value, (int, float)):
        try:
            return abs(float(cell) - float(value)) < 1e-9
        except (TypeError, ValueError):
            return False
    return cell == value or str(cell) == str(value)


def resolve_evidence_column_kind(model: "ReportModel", source_table: str, row_key, value) -> str:
    """The ``Column.kind`` of whichever column in ``model``'s
    ``<section_key>.<table_name>`` (``source_table``) row ``row_key``
    actually holds ``value`` — the column-formatting information a
    ``Recommendation.evidence`` tuple doesn't carry directly (see this
    module's docstring). Falls back to ``"str"`` (format_cell's plain
    ``str()`` path — safe for any value, including one that fails to
    resolve) whenever the section, table, row or a matching cell can't be
    found, so a stale or hand-built evidence tuple degrades to an
    unformatted-but-correct string rather than raising.
    """
    section_key, _, table_name = source_table.partition(".")
    for section in model.sections:
        if section.key != section_key:
            continue
        for table in section.tables:
            if table.name != table_name:
                continue
            for row in table.rows:
                if not row or row[0] != row_key:
                    continue
                for cell, column in zip(row, table.columns):
                    if _evidence_cell_matches(cell, value):
                        return column.kind
    return "str"


def format_evidence_value(model: "ReportModel", value, source_table: str, row_key, currency: str = "USD") -> str:
    """Render one ``Recommendation.evidence`` value exactly as it reads in
    its home table: resolve the cited column's ``kind`` (see
    :func:`resolve_evidence_column_kind`) and format ``value`` through
    :func:`format_cell` with it.
    """
    kind = resolve_evidence_column_kind(model, source_table, row_key, value)
    return format_cell(value, kind, currency)


def display_cell(value, column, table, currency: str = "USD") -> str:
    """``format_cell`` plus the table's display labels
    (``Table.value_labels``, e.g. "top-level" -> "Main session"). Only
    the Markdown and HTML renderers use this: JSON and CSV keep the raw
    value, which is what scripts and ``recommend.py`` match on."""
    labels = getattr(table, "value_labels", None) or {}
    if isinstance(value, str) and value in labels:
        return labels[value]
    return format_cell(value, column.kind, currency)


def help_parts(help_) -> list[tuple[str, str]]:
    """A ``model.Help`` as ``(heading, text)`` pairs, empty parts left out."""
    if help_ is None:
        return []
    parts = [("What it shows", help_.shows), ("How to read it", help_.read), ("When to act", help_.act)]
    return [(heading, text) for heading, text in parts if text]


def escape_md(cell) -> str:
    """Escape a table cell for embedding in a Markdown table.

    Only pipes and newlines are escaped: pipes because they are the
    Markdown table column delimiter, newlines because a raw newline
    breaks a table row. ``None`` renders as an empty string.
    """
    if cell is None:
        return ""
    text = str(cell)
    text = text.replace("|", "\\|")
    text = text.replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")
    return text


__all__ = [
    "format_cell",
    "escape_md",
    "resolve_evidence_column_kind",
    "format_evidence_value",
]
