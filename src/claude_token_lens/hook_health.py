"""Is the SessionStart snapshot hook actually running?

``hooks/snapshot-config.py`` writes a config snapshot at the start of
every Claude Code session, but only if ``~/.claude/settings.json`` has a
SessionStart hook whose command points at it. A hand-edited command can
be quietly wrong: in a Windows path written into JSON with single
backslashes, ``.claude`` + backslash + ``token-lens`` decodes the
backslash-t as a tab, the path no longer exists, and Claude Code runs
the hook without complaint while no snapshot is ever written. The same
silence follows when the interpreter the command names is not installed
(``py -3`` with no Python launcher on PATH), or when the command relies on
a ``%VAR%`` that Claude Code's shell on Windows (Git Bash) never expands.

:func:`check` reports what it finds in plain words for the Data quality
tab (``GET /api/diagnostics``) and ``init``; :func:`repair` rewrites
only that one command string, after a backup, when ``init`` is told to.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sys
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
#: The command's first word: a quoted path or a bare name.
_FIRST_WORD_RE = re.compile(r'\s*(?:"([^"]+)"|(\S+))')
#: A cmd.exe-style variable, which Git Bash passes through unexpanded.
_PERCENT_VAR_RE = re.compile(r"%[A-Za-z_][A-Za-z0-9_]*%")


@dataclass(slots=True)
class HookHealth:
    settings_path: Path
    #: The SessionStart command that runs the snapshot hook, if any.
    command: str | None = None
    #: The script path that command runs, with variables expanded.
    script_path: Path | None = None
    script_exists: bool = False
    #: The program the command starts (its first word), as written.
    interpreter: str | None = None
    #: That program is an existing file or found on PATH.
    interpreter_found: bool = True
    #: The command uses a ``%VAR%`` that Git Bash does not expand.
    percent_vars: bool = False
    #: The command holds a control character: a mis-escaped path.
    mis_escaped: bool = False
    #: Days since the newest snapshot, or None when there is none.
    last_snapshot_days: float | None = None
    #: A corrected command, when one can be worked out and its script exists.
    fixed_command: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.command is not None
            and self.script_exists
            and not self.mis_escaped
            and self.interpreter_found
            and not self.percent_vars
        )

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
        if not self.interpreter_found:
            return (
                f"{age}. The SessionStart hook starts '{self.interpreter}', which is not installed or not on "
                "your PATH, so the hook never runs. Run 'claude-token-lens init --repair-hook' to point it "
                "at this Python."
            )
        if self.percent_vars:
            return (
                f"{age}. The SessionStart hook command uses a %VARIABLE%, which Claude Code's shell on Windows "
                "(Git Bash) does not expand, so the hook may never run. Run 'claude-token-lens init "
                "--repair-hook' to write the full path instead."
            )
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


def _interpreter(command: str) -> str | None:
    match = _FIRST_WORD_RE.match(command)
    if match is None:
        return None
    return match.group(1) or match.group(2)


def _interpreter_found(program: str) -> bool:
    expanded = os.path.expanduser(os.path.expandvars(program))
    if os.path.isabs(expanded) or os.sep in expanded or (os.altsep and os.altsep in expanded):
        return Path(expanded).is_file()
    return shutil.which(expanded) is not None


def stable_python() -> str:
    """The Python a hook command should name: the base interpreter when
    this one runs in a virtual environment, since the hook script uses
    only the standard library and a venv can be deleted or rebuilt."""
    base = getattr(sys, "_base_executable", "") or ""
    return base if base and Path(base).is_file() else sys.executable


def _python_command(script: Path, python: str | None = None) -> str:
    """A hook command that names a Python and the script by their full
    paths, so it depends on neither PATH nor shell variables."""
    return f'"{python or stable_python()}" "{script}"'


def _expand_percent_vars(command: str) -> str | None:
    """``command`` with each ``%VAR%`` replaced by its value here, or
    ``None`` when one isn't set."""
    missing = False

    def value(match: re.Match) -> str:
        nonlocal missing
        found = os.environ.get(match.group(0)[1:-1])
        if found is None:
            missing = True
            return match.group(0)
        return found

    expanded = _PERCENT_VAR_RE.sub(value, command)
    return None if missing else expanded


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


def check(config_dir: str | Path, *, now: datetime | None = None, python: str | None = None) -> HookHealth:
    """Inspect ``settings.json`` next to ``config_dir`` and the snapshot
    history in ``config_dir``. Never raises: an unreadable settings file
    reads as "no hook". ``python`` is the interpreter a fixed command
    names (default: the one running this code)."""
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
    health.interpreter = _interpreter(command)
    health.interpreter_found = health.interpreter is not None and _interpreter_found(health.interpreter)
    health.percent_vars = bool(_PERCENT_VAR_RE.search(command))

    if not health.ok:
        candidate = _unescape_decoded(command)
        candidate_path = _script_path(candidate)
        if candidate_path is None or not candidate_path.is_file():
            return health
        program = _interpreter(candidate)
        expanded = _expand_percent_vars(candidate) if _PERCENT_VAR_RE.search(candidate) else candidate
        if (
            program is not None
            and _interpreter_found(program)
            and expanded is not None
            and _script_path(expanded) is not None
            and _script_path(expanded).is_file()
        ):
            # Only the escaping or a %VAR% was wrong: keep the user's own
            # interpreter and arguments, with the variable written out.
            if expanded != command:
                health.fixed_command = expanded
        else:
            health.fixed_command = _python_command(candidate_path.resolve(), python)
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


#: Session entrypoints that run in a terminal, where Claude Code runs a
#: statusline. The desktop app and claude.ai/code never do.
TERMINAL_ENTRYPOINTS = frozenset({"cli"})


def statusline_check(config_dir: str | Path, entrypoints: dict[str, dict]) -> tuple[bool, str]:
    """Is the statusline feeding the usage log? ``entrypoints`` is
    ``Store.entrypoint_counts()``. Returns (working, one plain sentence).
    Usage-limit amounts for Pro and Max plans come only from here."""
    from .footprint import is_own_statusline

    config_dir = Path(config_dir)
    try:
        settings = json.loads(_settings_path(config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = None
    last_logged = None
    try:
        with (config_dir / "usage-log.csv").open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("source") == "statusline" and row.get("logged_at"):
                    last_logged = max(last_logged or "", row["logged_at"])
    except (OSError, csv.Error):
        pass
    terminal = sum(v.get("count", 0) for k, v in entrypoints.items() if k in TERMINAL_ENTRYPOINTS)
    total = sum(v.get("count", 0) for v in entrypoints.values())
    desktop_note = (
        f" {total - terminal} of your {total} sessions ran outside a terminal (for example in the desktop app), "
        "where Claude Code does not run a statusline, so usage limits are only logged from terminal sessions."
        if total and terminal < total
        else ""
    )
    if not is_own_statusline(settings if isinstance(settings, dict) else None):
        return False, (
            "The statusline is not set up, so usage limits are not logged and Pro and Max amounts show as "
            "list-price equivalents. Run 'claude-token-lens init --connect' to add it." + desktop_note
        )
    if total and not terminal:
        return False, (
            f"All {total} of your sessions ran outside a terminal (for example in the desktop app), where Claude "
            "Code does not run a statusline, so no usage limits are logged. Pro and Max amounts show as "
            "list-price equivalents. Using Claude Code in a terminal now and then is enough to calibrate them."
        )
    if last_logged is None:
        return False, "The statusline is set up but has not logged anything yet." + desktop_note
    newest_terminal = max(
        (v.get("last_ts") or "" for k, v in entrypoints.items() if k in TERMINAL_ENTRYPOINTS), default=""
    )
    when = last_logged[:16].replace("T", " ")
    if newest_terminal[:16] > last_logged[:16] and newest_terminal[:10] > last_logged[:10]:
        return False, (
            f"The statusline last logged at {when} UTC, but terminal sessions ran later. Check that its "
            "command still runs: 'claude-token-lens changes' shows it." + desktop_note
        )
    return True, f"The statusline last logged at {when} UTC." + desktop_note


@dataclass(slots=True)
class ConnectPlan:
    """What ``init`` would add to ``settings.json`` so Claude Code feeds
    this tool: the snapshot hook, and a statusline when none is set."""

    settings_path: Path
    #: Plain sentences, one per change.
    changes: list[str]
    #: A unified diff of settings.json before and after.
    diff: str
    #: The whole file after the change; ``None`` when nothing changes.
    new_text: str | None


def plan_connect(config_dir: str | Path, *, hook_command: str, statusline_command: str | None) -> ConnectPlan:
    """Work out the ``settings.json`` change that connects this tool,
    without writing anything. The snapshot hook is added only when no
    SessionStart hook runs it yet (a broken one is :func:`repair`'s job);
    the statusline only when ``statusLine`` is unset, so a statusline of
    your own is never replaced."""
    import difflib

    settings_path = _settings_path(Path(config_dir))
    try:
        before = settings_path.read_text(encoding="utf-8")
        settings = json.loads(before)
    except FileNotFoundError:
        before, settings = "", {}
    except (OSError, ValueError):
        return ConnectPlan(settings_path, ["settings.json could not be read, so nothing will be changed."], "", None)
    if not isinstance(settings, dict):
        return ConnectPlan(settings_path, ["settings.json is not a JSON object, so nothing will be changed."], "", None)

    changes = []
    if not any(HOOK_SCRIPT_NAME in c for c in _session_start_commands(settings)):
        hooks = settings.setdefault("hooks", {})
        if isinstance(hooks, dict) and isinstance(hooks.setdefault("SessionStart", []), list):
            # async: Claude Code starts the hook and carries on without
            # waiting for it; it prints nothing, so it adds no tokens.
            hooks["SessionStart"].append({"hooks": [{"type": "command", "command": hook_command, "async": True}]})
            changes.append(
                "Add a SessionStart hook that records your settings (key names, a few safe values and file sizes, "
                "never contents) when a session starts. It runs in the background and prints nothing, so it adds "
                "no tokens."
            )
    if statusline_command and "statusLine" not in settings:
        settings["statusLine"] = {"type": "command", "command": statusline_command}
        changes.append(
            "Add a statusline that logs your usage limits and cache health. It runs in the terminal only, "
            "not in the desktop app."
        )
    if not changes:
        return ConnectPlan(settings_path, [], "", None)
    after = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="settings.json (now)",
            tofile="settings.json (after)",
        )
    )
    return ConnectPlan(settings_path, changes, diff, after)


def connect(plan: ConnectPlan, *, now: datetime | None = None) -> Path | None:
    """Write ``plan`` after backing up the current file to
    ``settings.json.bak-<UTC timestamp>``. Returns the backup path, or
    ``None`` when there was no file to back up."""
    if plan.new_text is None:
        raise ValueError("nothing to change")
    now = now or datetime.now(timezone.utc)
    backup = None
    if plan.settings_path.exists():
        backup = plan.settings_path.with_name(f"settings.json.bak-{now.strftime('%Y%m%dT%H%M%SZ')}")
        shutil.copy2(plan.settings_path, backup)
    plan.settings_path.parent.mkdir(parents=True, exist_ok=True)
    plan.settings_path.write_text(plan.new_text, encoding="utf-8")
    return backup


__all__ = ["HOOK_SCRIPT_NAME", "HookHealth", "check", "repair", "ConnectPlan", "plan_connect", "connect"]
