"""Config-snapshot loading, joining and diffing (WP7; schema 2 additions
per the plan's "Configuration layers and per-project effective config"
section).

Reads the JSON files written by ``hooks/snapshot-config.py`` (see that
module's docstring and plan Appendix A6 for the on-disk shape) and answers
several questions a report needs:

- Which snapshot was current when a given session started
  (:func:`snapshot_for`)?
- Which config keys actually changed across a window, and — for one chosen
  key — how do sessions grouped by that key's value compare
  (:func:`diff_keys`, :func:`co_changed_keys`, :func:`build_config_diff_table`)?
- (schema 2) What is the *effective* merged config for a project right now,
  and which layer supplied each key (:func:`effective_config`,
  :func:`layers`, :func:`build_effective_config_table`)?
- (schema 2) Which projects share an identical effective config
  (:func:`build_config_groups_table`), and which sessions' *observed*
  behaviour (a caller-computed dominant model/TTL-mix/effort) disagrees
  with what their snapshot says should be in effect
  (:func:`detect_drift`, :func:`build_config_drift_table`) — a mismatch
  implies a shell-profile env var or a ``--settings`` overlay the hook
  cannot see.
- (schema 2) Does ``~/.claude.json``'s own per-project ``last*`` session
  total agree with this tool's own accounting for the same session
  (:func:`claude_json_cross_check`)?

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
here stays a pure function of its arguments. The schema-2 additions keep
the same convention: :func:`build_config_drift_table` and
:func:`claude_json_cross_check` take the caller's already-computed
"observed" values (from real ``Turn``/``SessionRecord`` data) as plain
dicts rather than reaching into the parser themselves.

Project identity for the schema-2 multi-project tables
(:func:`build_effective_config_table`, :func:`build_config_layers_table`,
:func:`build_config_groups_table`) comes from each snapshot's own
``project_slug`` field (the hook's cwd at capture time) rather than a
caller-supplied project argument — a single ``<config-dir>/snapshots/``
directory accumulates snapshots from every project the hook has ever run
in, exactly like ``~/.claude/projects/`` itself. A schema-1 snapshot (no
``project_slug`` field) collapses into one ``"(unknown project)"`` bucket
per :func:`_project_label`.

This module may import from the rest of the package (unlike the standalone
hook script) — it reuses :class:`~claude_token_lens.model.Table` and
:class:`~claude_token_lens.model.Column` so a config-diff table renders
through the same renderers as every other report table.
"""

from __future__ import annotations

import hashlib
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

#: Schema 2's settings layers, precedence high to low -- matches
#: ``hooks/snapshot-config.py``'s own ``SETTINGS_LAYER_ORDER`` (duplicated
#: rather than imported: that script stays standalone stdlib and this
#: module is the one importing side of that relationship, not the other
#: way around -- see the hook's module docstring).
SETTINGS_LAYER_NAMES: tuple[str, ...] = ("managed", "project_local", "project_shared", "user")


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


# -- schema 2: effective config / layers / project grouping -----------------


def effective_config(snapshot: Snapshot) -> dict:
    """Schema 2's ``effective`` field: every documented settings lever's
    value, merged across the settings layers in precedence order (see
    ``hooks/snapshot-config.py``'s ``build_effective_settings``). ``{}`` for
    a schema-1 snapshot, which predates the settings-layer merge — a fresh
    snapshot is needed to get effective config for that project.
    """
    value = snapshot.data.get("effective")
    return dict(value) if isinstance(value, dict) else {}


def effective_provenance(snapshot: Snapshot) -> dict:
    """Schema 2's ``effective_provenance`` field: ``{key: layer_name}`` for
    every key in :func:`effective_config`. ``{}`` for a schema-1 snapshot.
    """
    value = snapshot.data.get("effective_provenance")
    return dict(value) if isinstance(value, dict) else {}


def layers(snapshot: Snapshot) -> dict:
    """Schema 2's ``settings_layers`` field: ``{layer_name: {present,
    source_path_hash, content_hash, ...}}`` for each of ``managed``,
    ``project_local``, ``project_shared``, ``user`` (precedence high to
    low). ``{}`` for a schema-1 snapshot.
    """
    value = snapshot.data.get("settings_layers")
    return dict(value) if isinstance(value, dict) else {}


def _project_label(snapshot: Snapshot) -> str:
    """The project identity a schema-2 snapshot's own ``project_slug``
    names, or a fixed placeholder for a schema-1 snapshot (which predates
    that field) — see the module docstring's "Project identity" note.
    """
    slug = snapshot.data.get("project_slug")
    return str(slug) if isinstance(slug, str) and slug else "(unknown project)"


def latest_snapshot_per_project(snapshots: list[Snapshot]) -> dict[str, Snapshot]:
    """The most recent snapshot for each project represented in
    ``snapshots`` (grouped by :func:`_project_label`). Relies on
    ``snapshots`` already being ascending by ``ts`` — :func:`load_snapshots`'
    own contract — so simply keeping the last one seen per project is
    correct without a separate sort/max step.
    """
    latest: dict[str, Snapshot] = {}
    for snap in snapshots:
        latest[_project_label(snap)] = snap
    return latest


def _hash_effective_config(effective: dict) -> str:
    encoded = json.dumps(effective, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_effective_config_table(snapshots: list[Snapshot]) -> Table:
    """One row per (project, key) for every project's *latest* effective
    config: the value currently in effect and which settings layer
    supplied it. Empty (no rows) if no snapshot carries schema 2's
    ``effective`` field yet.
    """
    latest = latest_snapshot_per_project(snapshots)
    rows: list[list] = []
    for project in sorted(latest):
        snap = latest[project]
        eff = effective_config(snap)
        prov = effective_provenance(snap)
        for key in sorted(eff):
            rows.append([project, key, _stringify_config_value(eff[key]), prov.get(key, "")])

    notes: list[str] = []
    if snapshots and not rows:
        notes.append(
            "No schema-2 effective config found in any supplied snapshot "
            "(capture a fresh snapshot with the current hook to populate this table)."
        )
    return Table(
        name="effective-config",
        title="Effective config",
        columns=[
            Column(key="project", label="Project", kind="str"),
            Column(key="key", label="Key", kind="str"),
            Column(key="value", label="Value", kind="str"),
            Column(key="provenance", label="Source layer", kind="str"),
        ],
        rows=rows,
        notes=notes,
    )


def build_config_layers_table(snapshots: list[Snapshot]) -> Table:
    """One row per (project, settings layer): whether that layer file is
    present, plus a repeated per-project content-layer summary (agent
    count, skill count, rules count, total CLAUDE.md bytes, command count,
    MCP server count) so a reader sees a project's whole config footprint
    without cross-referencing a second table.
    """
    latest = latest_snapshot_per_project(snapshots)
    rows: list[list] = []
    for project in sorted(latest):
        snap = latest[project]
        layer_map = layers(snap)
        content = snap.data.get("content_layers")
        content = content if isinstance(content, dict) else {}

        agents_summary = content.get("agents_summary") or {}
        skills = content.get("skills") or {}
        project_skills = (skills.get("project") or {}).get("names") or []
        user_skills = (skills.get("user") or {}).get("names") or []
        rules = content.get("rules") or {}
        commands = content.get("commands") or {}
        claude_md = content.get("claude_md") or {}
        claude_md_bytes = sum(
            value
            for value in (
                claude_md.get("user_bytes"),
                claude_md.get("project_root_bytes"),
                claude_md.get("project_local_bytes"),
                claude_md.get("nested_bytes"),
            )
            if isinstance(value, (int, float))
        )
        mcp_servers = snap.data.get("mcp_servers")
        mcp_names = (mcp_servers or {}).get("names") or []

        for layer_name in SETTINGS_LAYER_NAMES:
            layer_info = layer_map.get(layer_name) or {}
            rows.append(
                [
                    project,
                    layer_name,
                    bool(layer_info.get("present")),
                    agents_summary.get("count", 0),
                    len(project_skills) + len(user_skills),
                    rules.get("count", 0),
                    claude_md_bytes,
                    commands.get("count", 0),
                    len(mcp_names),
                ]
            )

    return Table(
        name="config-layers",
        title="Config layers",
        columns=[
            Column(key="project", label="Project", kind="str"),
            Column(key="layer", label="Layer", kind="str"),
            Column(key="present", label="Present", kind="str"),
            Column(key="agents", label="Agents", kind="int"),
            Column(key="skills", label="Skills", kind="int"),
            Column(key="rules", label="Rules", kind="int"),
            Column(key="claude_md_bytes", label="CLAUDE.md bytes", kind="int"),
            Column(key="commands", label="Commands", kind="int"),
            Column(key="mcp_servers", label="MCP servers", kind="int"),
        ],
        rows=rows,
        notes=[],
    )


def build_config_groups_table(
    snapshots: list[Snapshot], sessions_with_metrics: list[dict] | None = None
) -> Table:
    """Projects grouped by an identical *current* effective config (each
    project's latest snapshot), with an optional session count per group
    when ``sessions_with_metrics`` (the same ``{session_id, first_ts, ...}``
    shape :func:`build_config_diff_table` takes) is supplied — a session is
    attributed to whichever project its own :func:`snapshot_for` join
    resolves to, independent of which exact snapshot it joined (only that
    snapshot's project matters for this count).
    """
    latest = latest_snapshot_per_project(snapshots)

    session_counts: dict[str, int] = {}
    if sessions_with_metrics:
        for session in sessions_with_metrics:
            snap = snapshot_for(session.get("first_ts"), snapshots)
            if snap is None:
                continue
            project = _project_label(snap)
            session_counts[project] = session_counts.get(project, 0) + 1

    groups: dict[str, dict] = {}
    for project in sorted(latest):
        snap = latest[project]
        config_hash = _hash_effective_config(effective_config(snap))
        bucket = groups.setdefault(config_hash, {"projects": [], "sessions": 0})
        bucket["projects"].append(project)
        bucket["sessions"] += session_counts.get(project, 0)

    rows: list[list] = []
    for config_hash in sorted(groups, key=lambda h: (-len(groups[h]["projects"]), h)):
        bucket = groups[config_hash]
        rows.append(
            [config_hash[:12], len(bucket["projects"]), ", ".join(sorted(bucket["projects"])), bucket["sessions"]]
        )

    return Table(
        name="config-groups",
        title="Config groups",
        columns=[
            Column(key="config_hash", label="Effective-config hash", kind="str"),
            Column(key="project_count", label="Projects", kind="int"),
            Column(key="projects", label="Project list", kind="str"),
            Column(key="sessions", label="Sessions", kind="int"),
        ],
        rows=rows,
        notes=[],
    )


# -- schema 2: drift detection ------------------------------------------------


def _model_family(model_id: object, resolve_model) -> object:
    """Fix #15: reduce ``model_id`` to the canonical id ``resolve_model``
    (a ``Pricing.resolve_model``-shaped callable) resolves it to, so a
    settings *alias* (``"sonnet"``, ``"fable[1m]"``) compares equal to an
    observed full API id (``claude-opus-4-5-20260101``) when they name the
    same model. Falls back to the raw value unchanged when it isn't a
    non-empty string or ``resolve_model`` can't resolve it at all (an
    unrecognised id is still compared as itself, so a genuine drift to an
    unknown model is still reported).
    """
    if resolve_model is None or not isinstance(model_id, str) or not model_id:
        return model_id
    resolved = resolve_model(model_id)
    return resolved.canonical_id if resolved is not None else model_id


def detect_drift(snapshot: Snapshot, observed: dict, resolve_model=None) -> list[tuple[str, object, object]]:
    """Every key present in both ``snapshot``'s merged
    :func:`effective_config` and the caller's ``observed`` dict (its own
    computed dominant model / observed TTL mix / effort mode from real
    ``Turn``/``SessionRecord`` data — this module never touches turn data
    itself) whose values disagree, as ``(key, snapshot_value,
    observed_value)`` triples. A mismatch implies a shell-profile env var
    or a ``--settings`` one-launch overlay the hook cannot see (plan
    "Configuration layers" section) — evidence, not proof. A key in
    ``observed`` that the snapshot doesn't have an effective value for is
    silently skipped: there's nothing to compare it against.

    Fix #15: raw equality used to compare a settings *alias* (what
    ``effective_config``'s ``model`` key holds -- ``"sonnet"``, ``"opus"``,
    ``"fable[1m]"``) against a full API model id (what any caller derives
    ``observed["model"]`` from), which can never compare equal -- every
    session would report 100% drift on ``model`` the moment this table is
    wired up (see #14). ``resolve_model`` (a ``Pricing.resolve_model``-
    shaped callable, typically ``pricing.resolve_model``) resolves both
    sides to the same canonical id before comparing when the drifting key
    is ``"model"``; every other key keeps the previous exact-value
    comparison. ``promptCacheTtl``'s own false-positive case (a session
    that legitimately writes cache in both the 5m default and an 1h
    override tier within the same window) is not resolved here -- doing
    so needs the caller to say whether the session wrote in both tiers,
    which is outside this function's plain (key -> value) ``observed``
    contract; flagged rather than silently "fixed" by guessing.
    """
    eff = effective_config(snapshot)
    mismatches: list[tuple[str, object, object]] = []
    for key, observed_value in observed.items():
        if key not in eff:
            continue
        snap_value = eff[key]
        if key == "model":
            if _model_family(snap_value, resolve_model) == _model_family(observed_value, resolve_model):
                continue
        elif _hashable(snap_value) == _hashable(observed_value):
            continue
        mismatches.append((key, snap_value, observed_value))
    return mismatches


def build_config_drift_table(
    sessions_with_observed: list[dict], snapshots: list[Snapshot], resolve_model=None
) -> Table:
    """``sessions_with_observed`` entries: ``{"session_id", "first_ts",
    "observed": {key: value, ...}}``. One row per (session, key) where the
    session's joined snapshot's effective value disagrees with what was
    observed; a session predating every snapshot is skipped and counted in
    a note (same convention as :func:`build_config_diff_table``).
    ``resolve_model`` is forwarded to :func:`detect_drift` (fix #15).
    """
    rows: list[list] = []
    excluded = 0
    for session in sessions_with_observed:
        observed = session.get("observed") or {}
        snap = snapshot_for(session.get("first_ts"), snapshots)
        if snap is None:
            excluded += 1
            continue
        for key, snap_value, observed_value in detect_drift(snap, observed, resolve_model=resolve_model):
            rows.append(
                [
                    str(session.get("session_id", "")),
                    key,
                    _stringify_config_value(snap_value),
                    _stringify_config_value(observed_value),
                ]
            )

    notes: list[str] = []
    if excluded:
        plural = "s" if excluded != 1 else ""
        notes.append(
            f"{excluded} session{plural} predate the earliest config snapshot and were skipped."
        )
    if not rows:
        notes.append("No drift detected between snapshot effective config and observed session values.")

    return Table(
        name="config-drift",
        title="Config drift",
        columns=[
            Column(key="session_id", label="Session", kind="str"),
            Column(key="key", label="Key", kind="str"),
            Column(key="snapshot_value", label="Snapshot value", kind="str"),
            Column(key="observed_value", label="Observed value", kind="str"),
        ],
        rows=rows,
        notes=notes,
    )


# -- schema 2: ~/.claude.json cross-check ------------------------------------

#: ``claude_json.last_session`` key -> the matching observed-totals key the
#: caller's ``observed_session_totals`` dict is expected to use (see
#: :func:`claude_json_cross_check`).
_CLAUDE_JSON_CROSS_CHECK_FIELDS: tuple[tuple[str, str], ...] = (
    ("lastTotalInputTokens", "input_tokens"),
    ("lastTotalOutputTokens", "output_tokens"),
    ("lastTotalCacheCreationInputTokens", "cache_creation_tokens"),
    ("lastTotalCacheReadInputTokens", "cache_read_tokens"),
    ("lastCost", "cost"),
)


def claude_json_cross_check(snapshot: Snapshot, observed_session_totals: dict) -> dict:
    """Compare ``~/.claude.json``'s per-project ``last_session`` numbers
    (schema 2's ``claude_json`` field) against this tool's own totals for
    the same session, joined by ``lastSessionId`` -- comparison only
    happens when ``observed_session_totals["session_id"]`` matches, since
    the two sides otherwise describe different sessions entirely.

    ``observed_session_totals``: ``{"session_id", "input_tokens",
    "output_tokens", "cache_creation_tokens", "cache_read_tokens", "cost"}``
    -- whatever subset the caller has; a field missing on either side is
    skipped rather than reported as a difference.

    Returns ``{"matched": bool, "differences": {field: (claude_json_value,
    observed_value)}}`` -- ``matched`` is ``False`` (and ``differences``
    empty) whenever the snapshot has no ``claude_json.last_session`` at
    all, or its ``lastSessionId`` doesn't equal the observed session id.
    """
    claude_json = snapshot.data.get("claude_json")
    claude_json = claude_json if isinstance(claude_json, dict) else {}
    last_session = claude_json.get("last_session")
    last_session = last_session if isinstance(last_session, dict) else {}

    last_session_id = last_session.get("lastSessionId")
    observed_session_id = observed_session_totals.get("session_id")
    if not last_session_id or last_session_id != observed_session_id:
        return {"matched": False, "differences": {}}

    differences: dict[str, tuple] = {}
    for claude_json_key, observed_key in _CLAUDE_JSON_CROSS_CHECK_FIELDS:
        if claude_json_key not in last_session or observed_key not in observed_session_totals:
            continue
        claude_json_value = last_session[claude_json_key]
        observed_value = observed_session_totals[observed_key]
        if observed_key == "cost":
            # Fix #16: lastCost and the observed cost are two
            # independently computed floats (Claude Code's own rate card
            # vs pricing.price_turn against pricing.toml) -- exact `!=`
            # reports a "difference" for any discrepancy as small as the
            # 15th decimal place. Compared with a tolerance instead; every
            # other field here is an integer token count, which stays
            # exact.
            if not _floats_effectively_equal(claude_json_value, observed_value):
                differences[observed_key] = (claude_json_value, observed_value)
        elif claude_json_value != observed_value:
            differences[observed_key] = (claude_json_value, observed_value)

    return {"matched": True, "differences": differences}


#: Fix #16: absolute cost tolerance (half a cent) plus a relative 0.1%
#: tolerance for larger totals -- either satisfied is enough to call two
#: independently computed costs "the same".
_COST_ABS_TOLERANCE = 0.005
_COST_REL_TOLERANCE = 0.001


def _floats_effectively_equal(a: object, b: object) -> bool:
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return a == b
    return abs(a - b) <= max(_COST_ABS_TOLERANCE, _COST_REL_TOLERANCE * max(abs(a), abs(b)))


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
    *,
    include_effective: bool = False,
    sessions_with_observed: list[dict] | None = None,
    resolve_model=None,
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

    Schema 2 additions (both optional and off by default, so an existing
    caller passing only the three positional arguments gets exactly the
    one table it always has):

    - ``include_effective=True`` appends :func:`build_effective_config_table`,
      :func:`build_config_layers_table` and :func:`build_config_groups_table`
      (the last also folded ``sessions_with_metrics`` in for its session
      counts).
    - ``sessions_with_observed`` (the ``{"session_id", "first_ts",
      "observed": {...}}`` shape :func:`build_config_drift_table` takes),
      when given, appends a config-drift table.
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
    tables = [section_table]
    if include_effective:
        tables.append(build_effective_config_table(snapshots))
        tables.append(build_config_layers_table(snapshots))
        tables.append(build_config_groups_table(snapshots, sessions_with_metrics))
    if sessions_with_observed:
        tables.append(build_config_drift_table(sessions_with_observed, snapshots, resolve_model=resolve_model))
    return Section(key="config_diff", title="Config diff", tables=tables)


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
    # schema 2
    "SETTINGS_LAYER_NAMES",
    "effective_config",
    "effective_provenance",
    "layers",
    "latest_snapshot_per_project",
    "build_effective_config_table",
    "build_config_layers_table",
    "build_config_groups_table",
    "detect_drift",
    "build_config_drift_table",
    "claude_json_cross_check",
]
