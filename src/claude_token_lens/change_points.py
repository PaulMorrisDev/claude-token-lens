"""When your Claude Code setup changed: each ``apply`` (a profile or a
one-off ``--set``), each revert, and each change the config hook's
snapshots show between one session start and the next (a change you or
Claude made by hand, or with a prompt from the dashboard).

Each change to metrics capture (``capture-log.jsonl``, written by
``config.set_capture``) is one too: it changes what Claude writes and
what it costs.

EST-P9: when a corpus is on hand (``change_points(config_dir, corpus)``),
a session's transcript can show a change nothing else caught -- a
CLAUDE.md or memory size change of :data:`CLAUDE_MD_CHANGE_PCT` percent
or more, or the dominant model or effort level shifting -- between one
session and the next in the same project. These are ``source
"transcript"`` points, timestamped at the first session that shows the
new value.

Used for the "Since my last change" window and for the before-and-after
comparison in :mod:`impact`. Reads ``<config_dir>/backups/*/manifest.json``,
``<config_dir>/snapshots/`` and ``<config_dir>/capture-log.jsonl``;
writes nothing.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import capture_catalogue
from . import config as config_mod
from . import snapshots as snapshots_mod
from .model import EventKind
from .profiles import apply as apply_mod
from .profiles.frontmatter import parse_frontmatter

_TS_FORMAT = "%Y%m%dT%H%M%SZ"
#: A CLAUDE.md/memory size change at least this big (either way, from one
#: session to the next in the same project) is a change point (EST-P9).
CLAUDE_MD_CHANGE_PCT = 10.0

#: Snapshot sections whose change is a change to how Claude Code runs.
#: ``env_names`` (which variables are set, not their values) and
#: provenance fields are left out: they change without changing behaviour.
_BEHAVIOUR_PREFIXES = ("effective.", "agents.", "user_settings.", "project_settings.", "mcp_servers", "enabled_plugins")


@dataclass(slots=True)
class ChangePoint:
    ts: datetime
    #: "apply", "revert", "config", "capture" or "transcript".
    source: str
    label: str
    #: Settings keys that changed, as ``key`` or ``agent: key``, when known.
    keys: list[str] = field(default_factory=list)
    #: Each changed key's old and new value, when the apply manifest has them.
    changes: list[dict] = field(default_factory=list)
    #: Backup timestamp, for an apply (so the undo command can name it).
    backup_ts: str = ""
    reverted: bool = False

    def iso(self) -> str:
        return self.ts.strftime("%Y-%m-%dT%H:%M:%SZ")

    def to_dict(self) -> dict:
        return {
            "ts": self.iso(),
            "source": self.source,
            "label": self.label,
            "keys": list(self.keys),
            "changes": list(self.changes),
            "backup_ts": self.backup_ts,
            "reverted": self.reverted,
        }


def _parse_backup_ts(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts.split("-")[0], _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return _parse_backup_ts(ts)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _manifest_changes(config_dir: Path, backup_ts: str) -> list[dict]:
    folder = config_dir / "backups" / backup_ts
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for entry in manifest.get("entries") or ():
        if not isinstance(entry, dict):
            continue
        if "changes" not in entry:
            out.extend(_backup_diff(folder, entry))
        for change in entry.get("changes") or ():
            if isinstance(change, dict) and change.get("key"):
                out.append(change)
    return out


def _read_keys(path: Path, kind: str) -> dict | None:
    """A settings file's top-level keys or an agent file's frontmatter;
    ``{}`` for a missing file, ``None`` for one that can't be read."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        data = json.loads(text) if kind == "settings" else parse_frontmatter(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _backup_diff(folder: Path, entry: dict) -> list[dict]:
    """The keys an apply changed, for a manifest written before applies
    recorded them: the backed-up file against the file now. Later edits
    to the same keys show too, so this names keys but not values."""
    kind = entry.get("kind")
    if kind not in ("settings", "agent_frontmatter") or not entry.get("path"):
        return []
    old = _read_keys(folder / "files" / entry["backup"], kind) if entry.get("backup") else {}
    new = _read_keys(Path(entry["path"]), kind)
    if old is None or new is None:
        return []
    agent = entry.get("agent_name") if kind == "agent_frontmatter" else None
    return [
        {"key": key, "agent": agent}
        for key in sorted(set(old) | set(new))
        if json.dumps(old.get(key), sort_keys=True, default=str) != json.dumps(new.get(key), sort_keys=True, default=str)
    ]


def _key_label(change: dict) -> str:
    return f"{change['agent']}: {change['key']}" if change.get("agent") else str(change["key"])


def _apply_points(config_dir: Path) -> list[ChangePoint]:
    points: list[ChangePoint] = []
    for backup in apply_mod.list_backups(config_dir):
        when = _parse_backup_ts(backup.ts)
        if when is None:
            continue
        changes = _manifest_changes(config_dir, backup.ts)
        profile = backup.profile_id
        label = "Applied a one-off change" if profile in ("one-off", "") else f"Applied profile {profile}"
        points.append(
            ChangePoint(
                ts=when,
                source="apply",
                label=label,
                keys=[_key_label(c) for c in changes],
                changes=changes,
                backup_ts=backup.ts,
                reverted=backup.reverted_at is not None,
            )
        )
        reverted = _parse_iso(backup.reverted_at)
        if reverted is not None:
            points.append(
                ChangePoint(
                    ts=reverted,
                    source="revert",
                    label=f"Undid {label[0].lower()}{label[1:]}",
                    keys=[_key_label(c) for c in changes],
                    backup_ts=backup.ts,
                )
            )
    return points


def _flat(snap: snapshots_mod.Snapshot) -> dict:
    flat = {k: v for k, v in snapshots_mod.flatten_snapshot(snap).items() if k.startswith(_BEHAVIOUR_PREFIXES)}
    for key, value in snapshots_mod.effective_config(snap).items():
        flat[f"effective.{key}"] = value
    return flat


def _changed_keys(before: snapshots_mod.Snapshot, after: snapshots_mod.Snapshot) -> list[str]:
    """Keys whose value differs. When both snapshots carry the merged
    ``effective`` settings, those stand for the per-layer settings keys
    (the same change would otherwise be listed twice)."""
    old, new = _flat(before), _flat(after)
    changed = sorted(
        k for k in set(old) | set(new)
        if json.dumps(old.get(k), sort_keys=True, default=str) != json.dumps(new.get(k), sort_keys=True, default=str)
    )
    if any(k.startswith("effective.") for k in changed):
        changed = [k for k in changed if not k.startswith(("user_settings.", "project_settings."))]
    return changed


def _config_points(config_dir: Path) -> list[tuple[datetime | None, ChangePoint]]:
    """A change point wherever one project's snapshot differs from its
    previous one in a key that changes how Claude Code runs, with the
    previous snapshot's time."""
    points: list[tuple[datetime | None, ChangePoint]] = []
    previous: dict[str, snapshots_mod.Snapshot] = {}
    for snap in snapshots_mod.load_snapshots(config_dir):
        project = str(snap.data.get("project_slug") or "")
        before = previous.get(project)
        previous[project] = snap
        if before is None:
            continue
        keys = _changed_keys(before, snap)
        when = _parse_backup_ts(snap.ts) or _parse_iso(snap.ts)
        if not keys or when is None:
            continue
        since = _parse_backup_ts(before.ts) or _parse_iso(before.ts)
        points.append((since, ChangePoint(ts=when, source="config", label="Your settings changed", keys=keys)))
    return points


def _capture_label(record: dict) -> str:
    changed = record.get("changed") if isinstance(record.get("changed"), dict) else {}
    level = str(record.get("level") or "off")
    title = capture_catalogue.LEVEL_TITLES.get(level, level)
    old = changed.get("level", {}).get("from") if isinstance(changed.get("level"), dict) else None
    if level == "off":
        return "Turned metrics capture off"
    if old == "off":
        return f"Turned metrics capture on: {title}"
    if "level" in changed:
        return f"Metrics capture level: {title}"
    return "Changed metrics capture"


def _capture_points(config_dir: Path) -> list[ChangePoint]:
    """A change point for each ``[capture]`` change in ``capture-log.jsonl``."""
    points = []
    for record in config_mod.load_capture_log(config_dir):
        when = _parse_iso(record.get("ts"))
        changed = record.get("changed")
        if when is None or not isinstance(changed, dict) or not changed:
            continue
        changes = [
            {"key": f"capture.{key}", "agent": None, "old": value.get("from"), "new": value.get("to")}
            for key, value in sorted(changed.items())
            if isinstance(value, dict)
        ]
        points.append(
            ChangePoint(
                ts=when,
                source="capture",
                label=_capture_label(record),
                keys=[c["key"] for c in changes],
                changes=changes,
            )
        )
    return points


# -- EST-P9: change points a transcript itself shows -----------------------


def _dominant(values) -> str:
    """The most common non-empty value (mirrors quality.py's own
    ``_dominant``, duplicated here since a session's dominant model/effort
    isn't otherwise available without a full ``quality.Run``)."""
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else ""


def _priced_turns(top) -> list:
    return [turn for turn in top.turns if turn.turn_index > 0]


def _claude_md_chars(top) -> int:
    """CLAUDE.md/memory characters injected before this session's first
    priced turn (CONTEXT_INJECT events, subkind "instructions" or
    "nested_memory") -- the same events context_budget.py's startup
    accounting counts, duplicated minimally here since EST-P9 only needs
    the total, not the per-source breakdown."""
    turns = _priced_turns(top)
    first_ts = _parse_iso(turns[0].ts) if turns else None
    total = 0
    for event in top.events:
        if event.kind != EventKind.CONTEXT_INJECT or event.subkind not in ("instructions", "nested_memory"):
            continue
        if first_ts is not None:
            event_ts = _parse_iso(event.ts)
            if event_ts is not None and event_ts >= first_ts:
                continue
        total += event.size_chars or 0
    return total


@dataclass(slots=True)
class _SessionSignature:
    start: datetime
    project: str
    claude_md_chars: int
    model: str
    effort: str


def _session_signature(bundle) -> _SessionSignature | None:
    top = bundle.top
    if top is None:
        return None
    turns = _priced_turns(top)
    start = next((t for t in (_parse_iso(turn.ts) for turn in turns) if t is not None), None)
    if start is None:
        return None
    return _SessionSignature(
        start=start,
        project=bundle.project_dir,
        claude_md_chars=_claude_md_chars(top),
        model=_dominant(turn.model for turn in turns),
        effort=_dominant(turn.effort or "" for turn in turns) or "default",
    )


_TRANSCRIPT_KEY_LABELS = {"model": "Model", "effortLevel": "Effort level", "claude_md_chars": "CLAUDE.md size"}


def _join(bits: list[str]) -> str:
    if len(bits) <= 1:
        return bits[0] if bits else ""
    if len(bits) == 2:
        return f"{bits[0]} and {bits[1]}"
    return f"{', '.join(bits[:-1])} and {bits[-1]}"


def _transcript_label(keys: list[str]) -> str:
    bits = [_TRANSCRIPT_KEY_LABELS[k] for k in ("model", "effortLevel", "claude_md_chars") if k in keys]
    return f"{_join(bits)} changed" if bits else "Your setup changed"  # pragma: no cover


def _transcript_points(corpus) -> list[ChangePoint]:
    """A change point wherever one project's sessions show a CLAUDE.md or
    memory size change of :data:`CLAUDE_MD_CHANGE_PCT` percent or more,
    or the dominant model or effort level used differs from the previous
    session in the same project (EST-P9). Consecutive sessions only, so a
    slow drift across many small sessions doesn't fire repeatedly."""
    signatures = sorted(
        (sig for sig in (_session_signature(bundle) for bundle in corpus.sessions) if sig is not None),
        key=lambda s: s.start,
    )
    points: list[ChangePoint] = []
    previous: dict[str, _SessionSignature] = {}
    for sig in signatures:
        before = previous.get(sig.project)
        previous[sig.project] = sig
        if before is None:
            continue
        keys: list[str] = []
        changes: list[dict] = []
        if before.model and sig.model and before.model != sig.model:
            keys.append("model")
            changes.append({"key": "model", "agent": None, "old": before.model, "new": sig.model})
        if before.effort and sig.effort and before.effort != sig.effort:
            keys.append("effortLevel")
            changes.append({"key": "effortLevel", "agent": None, "old": before.effort, "new": sig.effort})
        base = before.claude_md_chars
        change_pct = (abs(sig.claude_md_chars - base) / base * 100.0) if base > 0 else (
            100.0 if sig.claude_md_chars > 0 else 0.0
        )
        if change_pct >= CLAUDE_MD_CHANGE_PCT:
            keys.append("claude_md_chars")
            changes.append({"key": "claude_md_chars", "agent": None, "old": base, "new": sig.claude_md_chars})
        if not keys:
            continue
        points.append(
            ChangePoint(ts=sig.start, source="transcript", label=_transcript_label(keys), keys=keys, changes=changes)
        )
    return points


def change_points(config_dir: Path | str, corpus=None) -> list[ChangePoint]:
    """Every change point, oldest first. A snapshot difference that spans
    an apply or revert is that change seen again, not a second one.
    ``corpus``, when given, adds transcript-derived points too (EST-P9,
    see the module docstring) -- opt-in, since building a corpus is more
    than ``config_dir`` alone can do, and most callers (the "since my
    last change" window) don't have one on hand yet."""
    config_dir = Path(config_dir)
    applied = _apply_points(config_dir)
    points = list(applied)
    for since, point in _config_points(config_dir):
        if any((since is None or since <= other.ts) and other.ts <= point.ts for other in applied):
            continue
        points.append(point)
    points.extend(_capture_points(config_dir))
    if corpus is not None:
        points.extend(_transcript_points(corpus))
    points.sort(key=lambda p: p.ts)
    return points


def latest(config_dir: Path | str) -> ChangePoint | None:
    points = change_points(config_dir)
    return points[-1] if points else None


__all__ = ["ChangePoint", "change_points", "latest"]
