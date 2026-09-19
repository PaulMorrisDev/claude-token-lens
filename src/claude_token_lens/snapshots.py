"""Config-snapshot loading, joining and diffing (WP7).

Reads the JSON files written by ``hooks/snapshot-config.py`` (see that
module's docstring and plan Appendix A6 for the on-disk shape) and answers
two questions a report needs:

- Which snapshot was current when a given session started
  (:func:`snapshot_for`)?
- Which config keys actually changed across a window, and — for one chosen
  key — how do sessions grouped by that key's value compare
  (:func:`diff_keys`, :func:`co_changed_keys`, :func:`build_config_diff_table`)?

Deviation from the plan, reported rather than made silently (see
``model.py``'s module docstring for the project's convention on this): the
plan's Appendix A6/prose lists ``build_config_diff_table(sessions_with_metrics,
key) -> Table`` with two parameters, but grouping sessions by a config key's
*value* is impossible without the snapshots to resolve that value against
each session's start time — the function cannot do the join described
("sessions predating all snapshots excluded") without them. This module
takes ``snapshots`` as an explicit third parameter throughout (matching
``diff_keys`` and ``co_changed_keys``, which already take snapshots
directly) rather than reaching out to load them itself, so every function
here stays a pure function of its arguments.

This module may import from the rest of the package (unlike the standalone
hook script) — it reuses :class:`~claude_token_lens.model.Table` and
:class:`~claude_token_lens.model.Column` so a config-diff table renders
through the same renderers as every other report table.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .model import Column, Section, Table

#: Compact UTC hook timestamp, e.g. "20260918T191200Z".
_HOOK_TS_FORMAT = "%Y%m%dT%H%M%SZ"

#: ISO-ish transcript timestamps this module accepts for a session's
#: ``first_ts`` (with or without fractional seconds).
_ISO_TS_FORMATS = ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ")

#: Top-level snapshot keys that are actually config (as opposed to
#: identity/provenance fields like ``ts``/``session_id``), flattened for
#: diffing and grouping.
_CONFIG_SECTIONS = (
    "user_settings",
    "managed_settings",
    "project_settings",
    "mcp_servers",
    "enabled_plugins",
    "agents",
    "env_names",
)


@dataclass(slots=True)
class Snapshot:
    """One parsed ``<ts>.json`` snapshot file."""

    path: Path
    ts: str
    data: dict = field(default_factory=dict)


# -- loading ----------------------------------------------------------------


def load_snapshots(config_dir: Path | str) -> list[Snapshot]:
    """Every ``*.json`` file under ``<config_dir>/snapshots/``, parsed and
    sorted ascending by ``ts``. Unreadable or malformed files are skipped
    rather than raising — a report must degrade gracefully around one
    corrupt snapshot, the way the transcript parser tolerates bad lines.

    Fix config-dir: ``config_dir`` is the token-lens directory itself
    (matching every other module's convention — ``config.py``'s
    ``config.toml``, ``cache.py``'s ``cache/``, ``tools/log_usage.py``'s
    ``usage-log.csv`` — and ``hooks/snapshot-config.py``'s own
    ``--config-dir``), not the ``~/.claude`` root one level up. This
    used to disagree with the hook, which wrote snapshots under
    ``<config_dir>/token-lens/snapshots/`` for an *explicit*
    ``--config-dir`` — see ``cli.py``'s old ``_load_snapshots_for_config_dir``
    R16 dual-fallback, no longer needed now both sides agree.
    """
    snapshots_dir = Path(config_dir) / "snapshots"
    if not snapshots_dir.is_dir():
        return []

    result: list[Snapshot] = []
    for path in sorted(snapshots_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        ts = data.get("ts") or path.stem
        result.append(Snapshot(path=path, ts=str(ts), data=data))

    result.sort(key=lambda snap: snap.ts)
    return result


# -- timestamp parsing / join -------------------------------------------


def _parse_ts(ts: str | None) -> datetime | None:
    """Parse either a hook snapshot ``ts`` or a transcript ``first_ts``
    into a UTC-aware ``datetime``. Returns ``None`` for anything that
    doesn't match a known format rather than raising, so a malformed
    timestamp degrades to "no snapshot found" instead of crashing a report.
    """
    if not ts:
        return None
    try:
        return datetime.strptime(ts, _HOOK_TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in _ISO_TS_FORMATS:
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def snapshot_for(session_first_ts: str, snapshots: list[Snapshot]) -> Snapshot | None:
    """The latest snapshot with ``ts <= session_first_ts``, or ``None`` if
    the session predates every snapshot (or its timestamp is unparsable).
    """
    target = _parse_ts(session_first_ts)
    if target is None:
        return None

    best: Snapshot | None = None
    best_dt: datetime | None = None
    for snap in snapshots:
        dt = _parse_ts(snap.ts)
        if dt is None or dt > target:
            continue
        if best_dt is None or dt > best_dt:
            best, best_dt = snap, dt
    return best


# -- flattening / diffing -------------------------------------------------


def _flatten(prefix: str, value, out: dict) -> None:
    if isinstance(value, dict) and value:
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            _flatten(child_prefix, child, out)
        return
    out[prefix] = value


def flatten_snapshot(snapshot: Snapshot) -> dict:
    """Flatten a snapshot's config sections into ``key.path -> value``
    pairs, e.g. ``user_settings.autoCompactWindow`` or
    ``agents.code-reviewer.experimental.cacheTtl`` (the agents dict's own
    values are already flattened by the hook, so nesting one more level
    here reproduces exactly that example path).
    """
    out: dict = {}
    for section in _CONFIG_SECTIONS:
        if section in snapshot.data:
            _flatten(section, snapshot.data[section], out)
    return out


def managed_keys(snapshot: Snapshot) -> list[str]:
    """The top-level ``managed-settings.json`` key names recorded on
    ``snapshot`` (fix 7) — e.g. ``["model", "permissions"]`` — or ``[]`` if
    the snapshot predates this field or the machine has no managed-settings
    file. A report uses this to mark any recommendation whose lever is one
    of these keys as "managed by policy, raise with your administrator"
    instead of something the user can change themselves (plan "Enterprise
    use" section).
    """
    keys = snapshot.data.get("managed_keys")
    if not isinstance(keys, list):
        return []
    return [str(k) for k in keys]


def _hashable(value):
    """A value that compares/hashes consistently across dict/list/scalar
    shapes, for equality checks between flattened snapshot values.
    """
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    return value


def diff_keys(snapshots: list[Snapshot]) -> dict[str, list]:
    """Every flattened config key whose value differs somewhere across
    ``snapshots``, mapped to its value at each snapshot in order (missing
    keys as ``None``). Keys whose value never changes are omitted — this is
    the auto-detection ``config-diff --auto-keys`` uses to find candidate
    keys without the caller naming one.
    """
    flattened = [flatten_snapshot(snap) for snap in snapshots]
    all_keys: set[str] = set()
    for flat in flattened:
        all_keys.update(flat.keys())

    result: dict[str, list] = {}
    for key in sorted(all_keys):
        values = [flat.get(key) for flat in flattened]
        if len({_hashable(v) for v in values}) > 1:
            result[key] = values
    return result


def co_changed_keys(snapshot_a: Snapshot, snapshot_b: Snapshot) -> list[str]:
    """Every flattened key whose value differs between two snapshots,
    sorted. Used as the config-diff caveat: "these other keys also changed
    in the same snapshot", so a value change never gets credited (or
    blamed) alone for a cost difference.
    """
    flat_a = flatten_snapshot(snapshot_a)
    flat_b = flatten_snapshot(snapshot_b)
    changed = []
    for key in sorted(set(flat_a) | set(flat_b)):
        if _hashable(flat_a.get(key)) != _hashable(flat_b.get(key)):
            changed.append(key)
    return changed


def _keys_co_changed_with(snapshots: list[Snapshot], key: str) -> list[str]:
    """Across consecutive snapshots in ``snapshots``, every other key that
    changed at the same time ``key`` changed. Feeds the note on a
    :func:`build_config_diff_table` result.
    """
    co_changed: set[str] = set()
    for earlier, later in zip(snapshots, snapshots[1:]):
        flat_earlier = flatten_snapshot(earlier)
        flat_later = flatten_snapshot(later)
        if _hashable(flat_earlier.get(key)) == _hashable(flat_later.get(key)):
            continue
        for other in co_changed_keys(earlier, later):
            if other != key:
                co_changed.add(other)
    return sorted(co_changed)


# -- config-diff table ------------------------------------------------------


def build_config_diff_table(
    sessions_with_metrics: list[dict],
    snapshots: list[Snapshot],
    key: str,
) -> Table:
    """Group ``sessions_with_metrics`` by the value of flattened config
    ``key`` in effect at each session's start (via :func:`snapshot_for` +
    :func:`flatten_snapshot`), and summarise each group.

    Each session dict has ``session_id``, ``first_ts``, ``turns``, ``cost``,
    ``recache_cc``, ``cc_total``, ``compactions``, ``span_s`` (this module
    never depends on the parser, so callers build these from whatever
    ``SessionRecord``/``Turn`` data they have).

    A session whose start predates every snapshot is excluded from every
    group (there's no config value to attribute it to) and counted in a
    note instead. A second note lists every other key that changed
    alongside ``key`` in the same window, so a reader doesn't credit one
    key alone for a cost difference two keys might explain.
    """
    groups: dict[object, dict] = {}
    order: list[object] = []
    excluded = 0

    for session in sessions_with_metrics:
        snap = snapshot_for(session.get("first_ts"), snapshots)
        if snap is None:
            excluded += 1
            continue
        value = flatten_snapshot(snap).get(key)
        bucket_key = _hashable(value)
        if bucket_key not in groups:
            groups[bucket_key] = {
                "value": value,
                "sessions": 0,
                "turns": 0,
                "cost": 0.0,
                "recache_cc": 0.0,
                "cc_total": 0.0,
                "compactions": 0,
                "spans": [],
            }
            order.append(bucket_key)
        bucket = groups[bucket_key]
        bucket["sessions"] += 1
        bucket["turns"] += session.get("turns") or 0
        bucket["cost"] += session.get("cost") or 0.0
        bucket["recache_cc"] += session.get("recache_cc") or 0.0
        bucket["cc_total"] += session.get("cc_total") or 0.0
        bucket["compactions"] += session.get("compactions") or 0
        span = session.get("span_s")
        if span is not None:
            bucket["spans"].append(span)

    # Most-represented value first; ties broken by display text so output
    # order is deterministic.
    order.sort(key=lambda bk: (-groups[bk]["sessions"], str(groups[bk]["value"])))

    rows: list[list] = []
    for bucket_key in order:
        bucket = groups[bucket_key]
        sessions = bucket["sessions"]
        cost = bucket["cost"]
        cc_total = bucket["cc_total"]
        recache_share = (bucket["recache_cc"] / cc_total * 100.0) if cc_total else None
        rows.append(
            [
                bucket["value"],
                sessions,
                bucket["turns"],
                cost,
                cost / sessions if sessions else None,
                recache_share,
                bucket["compactions"] / sessions if sessions else None,
                statistics.median(bucket["spans"]) if bucket["spans"] else None,
            ]
        )

    notes: list[str] = []
    if excluded:
        plural = "s" if excluded != 1 else ""
        notes.append(
            f"{excluded} session{plural} predate the earliest config snapshot "
            "and are excluded from every group above."
        )
    co_changed = _keys_co_changed_with(snapshots, key)
    if co_changed:
        notes.append(
            "Keys that changed alongside "
            f"{key!r} in the same window: {', '.join(co_changed)}."
        )
    else:
        notes.append(f"No other key changed alongside {key!r} in the same window.")

    return Table(
        name=f"config-diff-{key}",
        title=f"Config diff: {key}",
        columns=[
            Column(key="value", label="Value", kind="str"),
            Column(key="sessions", label="Sessions", kind="int"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="cost", label="Cost", kind="money"),
            Column(key="cost_per_session", label="Cost/session", kind="money"),
            Column(key="recache_share", label="Re-cache share", kind="pct"),
            Column(
                key="compactions_per_session",
                label="Compactions/session",
                kind="float",
            ),
            Column(key="median_span", label="Median span", kind="secs"),
        ],
        rows=rows,
        notes=notes,
    )


def _stringify_config_value(value: object) -> str:
    """Render a flattened config value for use as a table row key (fix
    item 10). A config value can be a bool, number, string, list, dict,
    or ``None`` (unset) — never guaranteed str/int the way most other
    tables' row keys are — so this always returns a non-empty string,
    rather than passing the raw value through.
    """
    if value is None:
        return "(unset)"
    return str(value)


def build_config_section(
    sessions_with_metrics: list[dict],
    snapshots: list[Snapshot],
    key: str,
) -> Section:
    """Wrap :func:`build_config_diff_table` in a "Config diff" report
    ``Section`` (fix item 10), so a CLI report can list a config-diff
    table alongside every other section's the same way.

    :func:`build_config_diff_table` itself is unchanged and keeps
    returning the value column verbatim (whatever type the config
    literally holds) for callers that already depend on that. This
    function's own table stringifies that first column instead (see
    :func:`_stringify_config_value`), so every ``Section``'s ``Table``
    has a first column usable as a row key regardless of the underlying
    config value's type.
    """
    diff_table = build_config_diff_table(sessions_with_metrics, snapshots, key)
    rows = [[_stringify_config_value(row[0]), *row[1:]] for row in diff_table.rows]
    section_table = Table(
        name=diff_table.name,
        title=diff_table.title,
        columns=diff_table.columns,
        rows=rows,
        notes=diff_table.notes,
    )
    return Section(key="config_diff", title="Config diff", tables=[section_table])


__all__ = [
    "Snapshot",
    "load_snapshots",
    "snapshot_for",
    "flatten_snapshot",
    "managed_keys",
    "diff_keys",
    "co_changed_keys",
    "build_config_diff_table",
    "build_config_section",
]
