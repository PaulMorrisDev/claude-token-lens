"""Is the SessionStart snapshot hook actually running?

``hooks/snapshot-config.py`` writes a config snapshot at the start of
every Claude Code session, but only if ``~/.claude/settings.json`` has a
SessionStart hook whose command points at it. A hand-edited command can
be quietly wrong: in a Windows path written into JSON with single
backslashes, ``.claude`` + backslash + ``token-lens`` decodes the
backslash-t as a tab, the path no longer exists, and Claude Code runs
the hook without complaint while no snapshot is ever written.

:func:`check` reports what it finds in plain words for the Data quality
tab (``GET /api/diagnostics``) and ``init``; :func:`repair` rewrites
only that one command string, after a backup, when ``init`` is told to.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import snapshots

HOOK_SCRIPT_NAME = "snapshot-config.py"

#: A JSON string escape that silently turned part of a Windows path into
#: a control character, and the two characters it came from.
_DECODED_ESCAPES = {"\t": "\\t", "\n": "\\n", "\r": "\\r", "\b": "\\b", "\f": "\\f"}

_QUOTED_SCRIPT_RE = re.compile(r'"([^"]*' + re.escape(HOOK_SCRIPT_NAME) + r')"')
_BARE_SCRIPT_RE = re.compile(r"(\S*" + re.escape(HOOK_SCRIPT_NAME) + r")")


@dataclass(slots=True)
class HookHealth:
    settings_path: Path
    #: The SessionStart command that runs the snapshot hook, if any.
    command: str | None = None
    #: The script path that command runs, with variables expanded.
    script_path: Path | None = None
    script_exists: bool = False
    #: The command holds a control character: a mis-escaped path.
    mis_escaped: bool = False
    #: Days since the newest snapshot, or None when there is none.
    last_snapshot_days: float | None = None
    #: A corrected command, when one can be worked out and its script exists.
    fixed_command: str | None = None

    @property
    def ok(self) -> bool:
        return self.command is not None and self.script_exists and not self.mis_escaped

    def summary(self) -> str:
        """One plain sentence for the Data quality tab and ``init``."""
        if self.last_snapshot_days is None:
            age = "No automatic config snapshot has been taken yet"
        else:
            days = int(self.last_snapshot_days)
            age = "Last config snapshot: " + ("today" if days == 0 else f"{days} day{'s' if days != 1 else ''} ago")
        if self.command is None:
            return f"{age}. No SessionStart hook runs {HOOK_SCRIPT_NAME} (see 'claude-token-lens snapshot-config --print-hook')."
        if self.mis_escaped:
            return (
                f"{age}. The SessionStart hook command in settings.json has a broken path: a single backslash "
                "in JSON turned part of it into a tab or newline, so the hook never runs. "
                "Run 'claude-token-lens init --repair-hook' to fix it."
            )
        if not self.script_exists:
            return f"{age}. The SessionStart hook runs {self.script_path}, which does not exist."
        return f"{age}. The SessionStart hook is set up."


def _settings_path(config_dir: Path) -> Path:
    # config_dir is <claude dir>/token-lens (pricing.TOKEN_LENS_DIRNAME).
    return Path(config_dir).parent / "settings.json"


def _session_start_commands(settings: dict) -> list[str]:
    commands = []
    hooks = settings.get("hooks")
    groups = hooks.get("SessionStart") if isinstance(hooks, dict) else None
    for group in groups if isinstance(groups, list) else []:
        entries = group.get("hooks") if isinstance(group, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("command"), str):
                commands.append(entry["command"])
    return commands


def _script_path(command: str) -> Path | None:
    match = _QUOTED_SCRIPT_RE.search(command) or _BARE_SCRIPT_RE.search(command)
    if match is None:
        return None
    return Path(os.path.expanduser(os.path.expandvars(match.group(1))))


def _unescape_decoded(command: str) -> str:
    return "".join(_DECODED_ESCAPES.get(ch, ch) for ch in command)


def _newest_snapshot_days(config_dir: Path, now: datetime) -> float | None:
    newest = None
    for snap in snapshots.load_snapshots(config_dir):
        ts = snapshots._parse_ts(snap.ts)
        if ts is not None and (newest is None or ts > newest):
            newest = ts
    if newest is None:
        return None
    return max(0.0, (now - newest).total_seconds() / 86400)


def check(config_dir: str | Path, *, now: datetime | None = None) -> HookHealth:
    """Inspect ``settings.json`` next to ``config_dir`` and the snapshot
    history in ``config_dir``. Never raises: an unreadable settings file
    reads as "no hook"."""
    config_dir = Path(config_dir)
    now = now or datetime.now(timezone.utc)
    health = HookHealth(settings_path=_settings_path(config_dir))
    health.last_snapshot_days = _newest_snapshot_days(config_dir, now)
    try:
        settings = json.loads(health.settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return health
    if not isinstance(settings, dict):
        return health

    command = next((c for c in _session_start_commands(settings) if HOOK_SCRIPT_NAME in c), None)
    if command is None:
        return health
    health.command = command
    health.mis_escaped = any(ch in command for ch in _DECODED_ESCAPES)
    health.script_path = _script_path(command)
    health.script_exists = bool(health.script_path and health.script_path.is_file())

    if not health.ok:
        candidate = _unescape_decoded(command)
        candidate_path = _script_path(candidate)
        if candidate != command and candidate_path is not None and candidate_path.is_file():
            health.fixed_command = candidate
    return health


def repair(health: HookHealth, *, now: datetime | None = None) -> Path:
    """Replace the broken command with ``health.fixed_command`` in
    ``settings.json``, after copying it to ``settings.json.bak-<UTC
    timestamp>``. Only that one string changes; the file is re-written
    with two-space indentation. Returns the backup path."""
    if health.command is None or health.fixed_command is None:
        raise ValueError("nothing to repair")
    now = now or datetime.now(timezone.utc)
    settings = json.loads(health.settings_path.read_text(encoding="utf-8"))
    replaced = 0
    for group in settings["hooks"]["SessionStart"]:
        for entry in group.get("hooks", []):
            if isinstance(entry, dict) and entry.get("command") == health.command:
                entry["command"] = health.fixed_command
                replaced += 1
    if replaced == 0:
        raise ValueError("the hook command changed since it was checked")
    backup = health.settings_path.with_name(f"settings.json.bak-{now.strftime('%Y%m%dT%H%M%SZ')}")
    shutil.copy2(health.settings_path, backup)
    health.settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return backup


__all__ = ["HOOK_SCRIPT_NAME", "HookHealth", "check", "repair"]
