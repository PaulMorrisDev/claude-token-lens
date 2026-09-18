"""JSON renderer: ``ReportModel`` -> a pinned-shape JSON document.

``to_jsonable`` is a small, self-contained converter (no ``dataclasses.
asdict`` — that would silently follow every nested dataclass without a
chance to special-case enums, tuples and floats) used by both
``render_json`` and, transitively, nothing else: it exists so the JSON
output is byte-for-byte predictable across renderer changes and across
the digest cache.
"""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime, timezone
from enum import Enum

from ..model import ReportModel

#: Bumped only if the top-level JSON envelope shape changes; independent
#: of ``model.SCHEMA_VERSION`` (the on-disk digest cache key).
SCHEMA_VERSION = 1


def to_jsonable(value):
    """Convert ``value`` into something ``json.dumps`` can serialise
    directly: dataclasses become dicts (recursively, via
    ``dataclasses.fields`` so this also works on ``slots=True``
    dataclasses), enums become their ``.value``, tuples become lists,
    ``datetime`` becomes an ISO 8601 string with a ``Z`` suffix (UTC),
    and floats are rounded to 6 decimal places to keep output stable.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: to_jsonable(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (tuple, list)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    return value


def render_json(model: ReportModel) -> str:
    """Render ``model`` as a JSON string: ``{"schema_version": 1,
    "tool_version": ..., "report": {...}}``, keys sorted, 2-space
    indent.
    """
    payload = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": model.meta.tool_version,
        "report": to_jsonable(model),
    }
    return json.dumps(payload, sort_keys=True, indent=2)


def write_json(model: ReportModel, path: str | os.PathLike) -> None:
    """Render ``model`` to JSON and write it to ``path`` (utf-8)."""
    text = render_json(model)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


__all__ = ["to_jsonable", "render_json", "write_json", "SCHEMA_VERSION"]
