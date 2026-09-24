"""Is the SessionStart snapshot hook actually running?

``hooks/snapshot-config.py`` writes a config snapshot at the start of
every Claude Code session, but only if Claude Code's ``settings.json``
(``~/.claude``, or ``$CLAUDE_CONFIG_DIR`` -- see :func:`settings_path`) has a
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

Metrics capture adds its own entries (``hooks/capture-hook.py`` on
SessionStart and SubagentStart for its note, PostToolUse for Deep, and
SessionEnd, Notification and PermissionRequest for the free signals),
described by :class:`HookSpec`. :func:`check_capture` checks them the same way;
:func:`plan_capture` works out the change that makes settings.json run
exactly the entries the chosen metrics need, and :func:`connect` writes
it. :func:`install_hook_files` copies the hook scripts out of the
package, which works inside the ``.pyz`` build too.
"""

from __future__ import annotations

import csv
import difflib
import importlib.resources
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import capture_catalogue, discovery, snapshots

HOOK_SCRIPT_NAME = "snapshot-config.py"

#: Hook scripts metrics capture installs; an entry whose command runs one
#: of them belongs to capture.
CAPTURE_SCRIPTS = (capture_catalogue.HOOK_SCRIPT,)

#: Files each capture script needs next to it under ``<config-dir>/hooks``.
CAPTURE_FILES = {capture_catalogue.HOOK_SCRIPT: (capture_catalogue.HOOK_SCRIPT, capture_catalogue.CATALOGUE_FILE)}

#: Seconds Claude Code waits for a capture hook before giving up on it.
CAPTURE_TIMEOUT_S = 5


@dataclass(frozen=True, slots=True)
class HookSpec:
    """One settings.json hook entry this tool wants: the script it runs,
    the event, the matcher (``""`` matches everything) and whether Claude
    Code runs it in the background."""

    script: str
    event: str
    matcher: str = ""
    async_: bool = False

    def describe(self) -> str:
        when = {
            "SessionStart": "when a session starts, is cleared or compacts",
            "SubagentStart": "when a subagent starts",
            "PostToolUse": "after "
            + ("web results" if set(self.matcher.split("|")) <= set(capture_catalogue.WEB_TOOLS) else "shell, read, search, web and MCP results"),
            "SessionEnd": "when a session ends",
            "Notification": "when Claude waits for you, in the background",
            "PermissionRequest": "when Claude asks for permission, in the background",
        }.get(self.event, f"on {self.event}")
        return f"{self.script} {when}"


def capture_specs(ids) -> tuple[HookSpec, ...]:
    """The hook entries the metrics in ``ids`` need
    (``capture_catalogue.hook_specs``)."""
    return tuple(HookSpec(*spec) for spec in capture_catalogue.hook_specs(ids))


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


def settings_path(claude_root: str | Path | None = None) -> Path:
    """Claude Code's user ``settings.json``: in ``claude_root`` when
    given (``--claude-root``), else where :func:`discovery.claude_root`
    finds it (``$CLAUDE_CONFIG_DIR``, else ``~/.claude``). Never next to
    ``--config-dir``, which can point this tool's folder anywhere."""
    return discovery.claude_root(claude_root) / "settings.json"


def _event_entries(settings: dict, event: str) -> list[tuple[str, dict]]:
    """``(matcher, entry)`` for every command entry under ``event``."""
    found = []
    hooks = settings.get("hooks")
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    for group in groups if isinstance(groups, list) else []:
        entries = group.get("hooks") if isinstance(group, dict) else None
        matcher = group.get("matcher") if isinstance(group, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("command"), str):
                found.append((matcher if isinstance(matcher, str) else "", entry))
    return found


def _event_commands(settings: dict, event: str) -> list[str]:
    return [entry["command"] for _matcher, entry in _event_entries(settings, event)]


def _expand(text: str) -> str:
    """``text`` with ``~``, ``$VAR`` and ``%VAR%`` expanded the same way
    on every platform (``os.path.expandvars`` only knows ``%VAR%`` on
    Windows). A variable that isn't set is left as written."""
    text = _PERCENT_VAR_RE.sub(lambda m: os.environ.get(m.group(0)[1:-1], m.group(0)), text)
    return os.path.expanduser(os.path.expandvars(text))


def _script_path(command: str) -> Path | None:
    match = _QUOTED_SCRIPT_RE.search(command) or _BARE_SCRIPT_RE.search(command)
    if match is None:
        return None
    return Path(_expand(match.group(1)))


def _args_after_script(command: str) -> str:
    """Whatever follows the script path in ``command`` (leading space
    kept), or an empty string."""
    match = _QUOTED_SCRIPT_RE.search(command) or _BARE_SCRIPT_RE.search(command)
    return command[match.end():].rstrip() if match else ""


def _interpreter(command: str) -> str | None:
    match = _FIRST_WORD_RE.match(command)
    if match is None:
        return None
    return match.group(1) or match.group(2)


def _interpreter_found(program: str) -> bool:
    expanded = _expand(program)
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


def check(
    config_dir: str | Path,
    *,
    now: datetime | None = None,
    python: str | None = None,
    claude_root: str | Path | None = None,
) -> HookHealth:
    """Inspect Claude Code's ``settings.json`` (see :func:`settings_path`)
    and the snapshot history in ``config_dir``. Never raises: an
    unreadable settings file reads as "no hook". ``python`` is the
    interpreter a fixed command names (default: the one running this
    code)."""
    config_dir = Path(config_dir)
    now = now or datetime.now(timezone.utc)
    health = HookHealth(settings_path=settings_path(claude_root))
    health.last_snapshot_days = _newest_snapshot_days(config_dir, now)
    try:
        settings = json.loads(health.settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return health
    if not isinstance(settings, dict):
        return health

    command = next((c for c in _event_commands(settings, "SessionStart") if HOOK_SCRIPT_NAME in c), None)
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
            # Keep any arguments after the script (such as the
            # --config-dir init adds for a non-default data folder).
            health.fixed_command = _python_command(candidate_path.resolve(), python) + _args_after_script(candidate)
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
    for event in list(settings.get("hooks", {})):
        for _matcher, entry in _event_entries(settings, event):
            if entry.get("command") == health.command:
                entry["command"] = health.fixed_command
                replaced += 1
    if replaced == 0:
        raise ValueError("the hook command changed since it was checked")
    backup = backup_path(health.settings_path, now)
    shutil.copy2(health.settings_path, backup)
    health.settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return backup


def backup_path(settings_path: Path, now: datetime) -> Path:
    """``settings.json.bak-<UTC timestamp>`` next to ``settings_path``,
    with ``-2``, ``-3``... added when two changes land in one second, so
    one backup never overwrites another."""
    base = f"{settings_path.name}.bak-{now.strftime('%Y%m%dT%H%M%SZ')}"
    candidate = settings_path.with_name(base)
    n = 2
    while candidate.exists():
        candidate = settings_path.with_name(f"{base}-{n}")
        n += 1
    return candidate


#: Session entrypoints that run in a terminal, where Claude Code runs a
#: statusline. The desktop app and claude.ai/code never do.
TERMINAL_ENTRYPOINTS = frozenset({"cli"})


def statusline_check(
    config_dir: str | Path, entrypoints: dict[str, dict], *, claude_root: str | Path | None = None
) -> tuple[bool, str]:
    """Is the statusline feeding the usage log? ``entrypoints`` is
    ``Store.entrypoint_counts()``. Returns (working, one plain sentence).
    Usage-limit amounts for Pro and Max plans come only from here."""
    from .footprint import is_own_statusline

    config_dir = Path(config_dir)
    try:
        settings = json.loads(settings_path(claude_root).read_text(encoding="utf-8"))
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


def plan_connect(
    config_dir: str | Path,
    *,
    hook_command: str,
    statusline_command: str | None,
    claude_root: str | Path | None = None,
    capture_specs_wanted: tuple[HookSpec, ...] = (),
    capture_commands: dict[str, str] | None = None,
) -> ConnectPlan:
    """Work out the ``settings.json`` change that connects this tool,
    without writing anything. The snapshot hook is added only when no
    SessionStart hook runs it yet (a broken one is :func:`repair`'s job);
    the statusline only when ``statusLine`` is unset, so a statusline of
    your own is never replaced. With ``capture_commands``, the capture
    entries are made to match ``capture_specs_wanted`` as well
    (:func:`plan_capture`)."""
    path, before, settings, refusal = _read_settings(claude_root)
    if refusal is not None:
        return refusal

    changes = []
    if not any(HOOK_SCRIPT_NAME in c for c in _event_commands(settings, "SessionStart")):
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
    if capture_commands is not None:
        changes += _sync_capture_entries(settings, capture_specs_wanted, capture_commands)
    return _finish_plan(path, before, settings, changes)


def _read_settings(claude_root: str | Path | None) -> tuple[Path, str, dict, ConnectPlan | None]:
    """settings.json's path, text and parsed object, or a plan that
    refuses to change a file it can't read."""
    path = settings_path(claude_root)
    try:
        before = path.read_text(encoding="utf-8")
        settings = json.loads(before)
    except FileNotFoundError:
        return path, "", {}, None
    except (OSError, ValueError):
        return path, "", {}, ConnectPlan(path, ["settings.json could not be read, so nothing will be changed."], "", None)
    if not isinstance(settings, dict):
        return path, "", {}, ConnectPlan(path, ["settings.json is not a JSON object, so nothing will be changed."], "", None)
    return path, before, settings, None


def _finish_plan(path: Path, before: str, settings: dict, changes: list[str]) -> ConnectPlan:
    if not changes:
        return ConnectPlan(path, [], "", None)
    after = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="settings.json (now)",
            tofile="settings.json (after)",
        )
    )
    return ConnectPlan(path, changes, diff, after)


def _entry_spec(event: str, matcher: str, entry: dict) -> HookSpec | None:
    command = entry.get("command", "")
    script = next((name for name in CAPTURE_SCRIPTS if name in command), None)
    if script is None:
        return None
    return HookSpec(script, event, matcher or "", bool(entry.get("async")))


def _capture_entries(settings: dict) -> list[tuple[HookSpec, dict]]:
    hooks = settings.get("hooks")
    found = []
    for event in list(hooks) if isinstance(hooks, dict) else []:
        for matcher, entry in _event_entries(settings, event):
            spec = _entry_spec(event, matcher, entry)
            if spec is not None:
                found.append((spec, entry))
    return found


def _sync_capture_entries(settings: dict, wanted: tuple[HookSpec, ...], commands: dict[str, str]) -> list[str]:
    """Make ``settings`` run exactly the capture entries in ``wanted``,
    each with the command ``commands`` gives its script: entries no
    longer wanted, or with the wrong command, matcher or background
    setting, are replaced. Other hooks are never touched. Returns one
    plain sentence per change."""
    present = _capture_entries(settings)
    keep = {
        spec
        for spec, entry in present
        if spec in wanted and entry.get("command") == commands.get(spec.script) and entry.get("timeout") == CAPTURE_TIMEOUT_S
    }
    changes = []
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks):
            groups = hooks[event]
            if not isinstance(groups, list):
                continue
            kept_groups = []
            for group in groups:
                entries = group.get("hooks") if isinstance(group, dict) else None
                if isinstance(entries, list):
                    matcher = group.get("matcher") if isinstance(group.get("matcher"), str) else ""
                    kept = []
                    for entry in entries:
                        spec = _entry_spec(event, matcher, entry) if isinstance(entry, dict) and isinstance(entry.get("command"), str) else None
                        if spec is None or spec in keep:
                            kept.append(entry)
                        elif spec not in wanted:
                            changes.append(f"Remove the capture hook that runs {spec.describe()}.")
                    if not kept:
                        continue
                    if len(kept) != len(entries):
                        group = {**group, "hooks": kept}
                kept_groups.append(group)
            if kept_groups:
                hooks[event] = kept_groups
            else:
                del hooks[event]
        if not hooks:
            settings.pop("hooks", None)
    for spec in wanted:
        if spec in keep:
            continue
        hooks = settings.setdefault("hooks", {})
        if not isinstance(hooks, dict) or not isinstance(hooks.setdefault(spec.event, []), list):
            continue  # a hooks section of an unexpected shape is left alone; check_capture reports the gap
        entry = {"type": "command", "command": commands[spec.script], "timeout": CAPTURE_TIMEOUT_S}
        if spec.async_:
            entry["async"] = True
        group = {"matcher": spec.matcher, "hooks": [entry]} if spec.matcher else {"hooks": [entry]}
        hooks[spec.event].append(group)
        replacing = any(s.event == spec.event and s.script == spec.script for s, _entry in present)
        changes.append(("Update" if replacing else "Add") + f" the capture hook that runs {spec.describe()}.")
    return changes


def remove_capture_entries(settings: dict) -> list[str]:
    """Take every capture entry out of ``settings``; one plain sentence
    per entry removed."""
    return _sync_capture_entries(settings, (), {})


def plan_capture(
    wanted: tuple[HookSpec, ...],
    commands: dict[str, str],
    *,
    claude_root: str | Path | None = None,
) -> ConnectPlan:
    """The settings.json change that makes it run exactly the capture
    entries in ``wanted`` (none, to take capture back out), without
    writing anything. ``commands`` maps each script to its command."""
    path, before, settings, refusal = _read_settings(claude_root)
    if refusal is not None:
        return refusal
    return _finish_plan(path, before, settings, _sync_capture_entries(settings, tuple(wanted), commands))


@dataclass(slots=True)
class CaptureHookHealth:
    """Whether settings.json runs the capture entries the chosen metrics
    need."""

    settings_path: Path
    needed: tuple[HookSpec, ...] = ()
    #: Needed entries settings.json lacks.
    missing: tuple[HookSpec, ...] = ()
    #: Capture entries no chosen metric needs. Harmless: the hook adds
    #: nothing for a metric that is off.
    extra: tuple[HookSpec, ...] = ()
    #: Plain sentences, one per problem with an entry that is there.
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.problems

    def summary(self) -> str:
        if not self.needed and not self.extra:
            return "No capture hooks are needed or installed."
        if self.ok:
            return "The capture hooks are set up." + (
                " settings.json also runs capture hooks no chosen metric needs; they add nothing."
                if self.extra
                else ""
            )
        parts = [f"settings.json does not run {spec.describe()}." for spec in self.missing] + self.problems
        return " ".join(parts) + " Run 'claude-token-lens capture connect' to fix it."


def check_capture(
    wanted: tuple[HookSpec, ...], *, claude_root: str | Path | None = None
) -> CaptureHookHealth:
    """Compare settings.json's capture entries with ``wanted``. Never
    raises: an unreadable settings file reads as no entries."""
    health = CaptureHookHealth(settings_path=settings_path(claude_root), needed=tuple(wanted))
    try:
        settings = json.loads(health.settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    present = _capture_entries(settings if isinstance(settings, dict) else {})
    specs = [spec for spec, _entry in present]
    health.missing = tuple(spec for spec in wanted if spec not in specs)
    health.extra = tuple(spec for spec in specs if spec not in wanted)
    for spec, entry in present:
        if spec not in wanted:
            continue
        command = entry["command"]
        if any(ch in command for ch in _DECODED_ESCAPES):
            health.problems.append(f"The command for {spec.describe()} has a broken path (a single backslash in JSON).")
            continue
        script = _script_path_for(command, spec.script)
        if script is None or not script.is_file():
            health.problems.append(f"The command for {spec.describe()} runs {script or spec.script}, which does not exist.")
        program = _interpreter(command)
        if program is None or not _interpreter_found(program):
            health.problems.append(f"The command for {spec.describe()} starts '{program}', which is not installed or not on your PATH.")
        if _PERCENT_VAR_RE.search(command):
            health.problems.append(f"The command for {spec.describe()} uses a %VARIABLE%, which Git Bash does not expand.")
    return health


def _script_path_for(command: str, script_name: str) -> Path | None:
    match = re.search(r'"([^"]*' + re.escape(script_name) + r')"', command) or re.search(
        r"(\S*" + re.escape(script_name) + r")", command
    )
    return Path(_expand(match.group(1))) if match else None


def hook_command(script: Path, extra_args: str = "", python: str | None = None) -> str:
    """The command a hook entry runs: a Python and ``script`` by their
    full paths, then ``extra_args`` as written."""
    return _python_command(script, python) + extra_args


def install_hook_files(config_dir: str | Path, names) -> list[Path]:
    """Copy the packaged ``hooks/<name>`` files into
    ``<config_dir>/hooks/``, replacing older copies. Reads them through
    ``importlib.resources``, so it works from a ``.pyz`` too. Returns the
    paths written."""
    dest_dir = Path(config_dir) / "hooks"
    dest_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in names:
        data = (importlib.resources.files("claude_token_lens") / "hooks" / name).read_bytes()
        dest = dest_dir / name
        tmp = dest.with_name(f"{name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        written.append(dest)
    return written


def connect(plan: ConnectPlan, *, now: datetime | None = None) -> Path | None:
    """Write ``plan`` after backing up the current file to
    ``settings.json.bak-<UTC timestamp>``. Returns the backup path, or
    ``None`` when there was no file to back up."""
    if plan.new_text is None:
        raise ValueError("nothing to change")
    now = now or datetime.now(timezone.utc)
    backup = None
    if plan.settings_path.exists():
        backup = backup_path(plan.settings_path, now)
        shutil.copy2(plan.settings_path, backup)
    plan.settings_path.parent.mkdir(parents=True, exist_ok=True)
    plan.settings_path.write_text(plan.new_text, encoding="utf-8")
    return backup


__all__ = [
    "CAPTURE_SCRIPTS",
    "CaptureHookHealth",
    "ConnectPlan",
    "HOOK_SCRIPT_NAME",
    "HookHealth",
    "HookSpec",
    "backup_path",
    "capture_specs",
    "check",
    "check_capture",
    "connect",
    "hook_command",
    "install_hook_files",
    "plan_capture",
    "plan_connect",
    "remove_capture_entries",
    "repair",
    "settings_path",
]
