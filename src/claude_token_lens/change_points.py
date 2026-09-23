"""When your Claude Code setup changed: each ``apply`` (a profile or a
one-off ``--set``), each revert, and each change the config hook's
snapshots show between one session start and the next (a change you or
Claude made by hand, or with a prompt from the dashboard).

Used for the "Since my last change" window and for the before-and-after
comparison in :mod:`impact`. Reads ``<config_dir>/backups/*/manifest.json``
and ``<config_dir>/snapshots/``; writes nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import snapshots as snapshots_mod
from .profiles import apply as apply_mod

_TS_FORMAT = "%Y%m%dT%H%M%SZ"

#: Snapshot sections whose change is a change to how Claude Code runs.
#: ``env_names`` (which variables are set, not their values) and
#: provenance fields are left out: they change without changing behaviour.
_BEHAVIOUR_PREFIXES = ("effective.", "agents.", "user_settings.", "project_settings.", "mcp_servers", "enabled_plugins")


@dataclass(slots=True)
class ChangePoint:
    ts: datetime
    #: "apply", "revert" or "config".
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
    try:
        manifest = json.loads((config_dir / "backups" / backup_ts / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for entry in manifest.get("entries") or ():
        for change in entry.get("changes") or () if isinstance(entry, dict) else ():
            if isinstance(change, dict) and change.get("key"):
                out.append(change)
    return out


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


def change_points(config_dir: Path | str) -> list[ChangePoint]:
    """Every change point, oldest first. A snapshot difference that spans
    an apply or revert is that change seen again, not a second one."""
    config_dir = Path(config_dir)
    applied = _apply_points(config_dir)
    points = list(applied)
    for since, point in _config_points(config_dir):
        if any((since is None or since <= other.ts) and other.ts <= point.ts for other in applied):
            continue
        points.append(point)
    points.sort(key=lambda p: p.ts)
    return points


def latest(config_dir: Path | str) -> ChangePoint | None:
    points = change_points(config_dir)
    return points[-1] if points else None


__all__ = ["ChangePoint", "change_points", "latest"]
