"""Rendering primitives for claude-token-lens report output.

Later work packages add the actual per-format renderers (markdown.py,
json_out.py, csv_out.py, html.py); this package currently exposes only
the shared cell-formatting primitives they will all depend on.
"""

from __future__ import annotations

from .tables import escape_md, format_cell

__all__ = ["escape_md", "format_cell"]
