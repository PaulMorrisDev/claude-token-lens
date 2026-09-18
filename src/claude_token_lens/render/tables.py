"""Cell formatting primitives shared by every renderer.

``format_cell`` is the single place a raw value becomes display text, so
Markdown, JSON, CSV and HTML renderers all agree on numeric formatting
(thousands separators, decimal places, unit suffixes). Renderers must not
format numbers themselves — they call ``format_cell`` per ``Column.kind``.
"""

from __future__ import annotations

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


__all__ = ["format_cell", "escape_md"]
