"""CSV renderer: ``ReportModel`` -> one CSV file per ``Table``, plus a
directory index and a meta dump, all under one output directory.

Unlike Markdown/HTML, values are written raw (unformatted numbers, ``""``
for ``None``, ``;``-joined tuples) so the CSV files are fit for
spreadsheet import and further analysis, not just display.
"""

from __future__ import annotations

import csv
import os
import re
from pathlib import Path

from ..model import ReportModel

_SANITIZE_RE = re.compile(r"[^a-z0-9_]+")


def _sanitize(name: str) -> str:
    """Reduce ``name`` to the ``[a-z0-9_]`` charset a filename component
    must stay within: lower-case, run of any other character collapsed
    to a single underscore, leading/trailing underscores trimmed.
    """
    cleaned = _SANITIZE_RE.sub("_", name.lower()).strip("_")
    return cleaned or "_"


def _csv_value(value):
    """Raw (unformatted) cell value for CSV: ``None`` -> ``""``,
    tuple/list -> ``;``-joined, everything else passed through so
    ``csv.writer`` stringifies numbers itself without thousands
    separators or unit suffixes.
    """
    if value is None:
        return ""
    if isinstance(value, (tuple, list)):
        return ";".join(str(v) for v in value)
    return value


def _meta_rows(model: ReportModel):
    meta = model.meta
    pricing = meta.pricing
    rows = [
        ("tool_version", meta.tool_version),
        ("generated_at", meta.generated_at),
        ("window", meta.window),
        ("projects", ";".join(meta.projects)),
        ("pricing.path", pricing.path or ""),
        ("pricing.version", pricing.version or ""),
        ("pricing.sha8", pricing.sha8 or ""),
        ("pricing.currency", pricing.currency),
        ("pricing.coverage_pct", pricing.coverage_pct),
        ("billing_mode", meta.billing_mode),
    ]
    for key, value in meta.thresholds.items():
        rows.append((f"threshold.{key}", value))
    for index, text in enumerate(meta.assumptions):
        rows.append((f"assumption.{index}", text))
    for rec in model.recommendations:
        rows.append((f"recommendation.{rec.id}", rec.title))
    return rows


def write_csv_dir(model: ReportModel, out_dir: str | os.PathLike) -> None:
    """Write one ``<section.key>__<table.name>.csv`` per table in
    ``model``, plus ``_index.csv`` (section, table, file, rows) and
    ``_meta.csv`` (key, value), into ``out_dir`` (created if absent).
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    index_rows: list[tuple[str, str, str, int]] = []
    for section in model.sections:
        section_key = _sanitize(section.key)
        for table in section.tables:
            filename = f"{section_key}__{_sanitize(table.name)}.csv"
            with open(out_path / filename, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow([column.key for column in table.columns])
                for row in table.rows:
                    writer.writerow([_csv_value(value) for value in row])
            index_rows.append((section.key, table.name, filename, len(table.rows)))

    with open(out_path / "_index.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["section", "table", "file", "rows"])
        writer.writerows(index_rows)

    with open(out_path / "_meta.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["key", "value"])
        writer.writerows(_meta_rows(model))


__all__ = ["write_csv_dir"]
