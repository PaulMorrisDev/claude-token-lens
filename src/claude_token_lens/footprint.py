"""What claude-token-lens has installed and changed on this machine, and
how to take each part back out.

One list, used by ``claude-token-lens changes``, ``claude-token-lens
uninstall`` and the Data quality tab (``GET /api/setup``):

- the SessionStart snapshot hook and the statusline in
  ``~/.claude/settings.json``;
- the logon service (``install-service`` or ``init``);
- each change ``apply`` made to your Claude Code settings or agent files
  that has not been reverted;
- the active-profile marker and any one-session profile files;
- this tool's own data folder.

Every item says what it does, whether it costs tokens, and the command
that undoes it. :func:`plan_uninstall` and :func:`remove_settings_entries`
back ``uninstall``: they show the exact ``settings.json`` change first and
back the file up before writing, like ``init``'s connect step.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import hook_health
from .profiles import apply as apply_mod

#: What a statusLine command of this tool's looks like.
_STATUSLINE_MARKERS = ("claude_token_lens.statusline", "claude-token-lens", "claude_token_lens")


@dataclass(slots=True)
class FootprintItem:
    key: str
    title: str
    #: "installed", "not installed", "in place", "undone" or "unknown".
    status: str
    where: str
    what_it_does: str
    #: Whether it adds tokens to Claude's context, in plain words.
    token_cost: str
    #: The command (or plain instruction) that removes or undoes it.
    undo: str

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "where": self.where,
            "what_it_does": self.what_it_does,
            "token_cost": self.token_cost,
            "undo": self.undo,
        }


def home_label(path: Path | str) -> str:
    """``path`` with the home folder written as ``~``, so output names
    the file without your user name."""
    text = str(path)
    home = str(Path.home())
    if text.lower().startswith(home.lower()):
        return "~" + text[len(home):]
    return text


def _settings(config_dir: Path) -> tuple[Path, dict | None]:
    path = Path(config_dir).parent / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return path, None
    return path, data if isinstance(data, dict) else None


def is_own_statusline(settings: dict | None) -> bool:
    status_line = (settings or {}).get("statusLine")
    command = status_line.get("command") if isinstance(status_line, dict) else None
    return isinstance(command, str) and any(marker in command for marker in _STATUSLINE_MARKERS)


def _folder_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _size_label(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "bytes" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} GB"


#: What to expect from installing and using this tool, in plain words:
#: (title, text). Shown by ``changes``, the Data quality tab (``GET
#: /api/setup``) and docs/first-run.md.
EXPECTATIONS: tuple[tuple[str, str], ...] = (
    (
        "It never uses your Claude tokens",
        "This tool reads files Claude Code already writes. It never calls Claude, so it adds nothing to your "
        "usage, on the first run or after.",
    ),
    (
        "The hook and statusline add nothing to Claude's context",
        "The snapshot hook prints nothing and the statusline is shown only to you, so neither is sent to Claude. "
        "The hook starts a short Python process at each session start, which takes well under a second.",
    ),
    (
        "The first scan takes a while",
        "The first report or dashboard start reads every transcript under ~/.claude/projects, which can take a few "
        "minutes and use a CPU core. Later runs read only new or changed files.",
    ),
    (
        "It reads, it doesn't change",
        "The dashboard never changes Claude Code. Every fix is a prompt for Claude, which shows you the diff and "
        "asks before editing, or an apply command you run yourself with --dry-run first.",
    ),
    (
        "A change takes effect in new sessions",
        "Claude Code reads settings when a session starts, and every session builds its cache from scratch anyway, "
        "so a change costs nothing extra to switch on. Switching model inside a running session (/model) does "
        "rebuild that session's cache once.",
    ),
    (
        "Cheaper isn't free",
        "A cheaper model, lower effort or earlier summaries can mean more replies or missed details on hard work. "
        "Check \"Your changes and what they did\" on Profiles after a few sessions, and undo with the apply "
        "--revert command if it's worse.",
    ),
    (
        "Amounts are list-price equivalents",
        "Costs are worked out from each model's list price. On a Pro or Max plan you don't pay per token, so they "
        "show as a share of your weekly limit when the statusline has recorded limit readings.",
    ),
    (
        "Your data stays on this machine",
        "Transcripts and settings are read locally. CLAUDE.md text and skill descriptions are read when you ask "
        "for a review and never stored. Nothing is uploaded.",
    ),
)

#: The one command that takes everything back out, shown dry-run first.
UNINSTALL_COMMAND = "claude-token-lens uninstall --revert-changes --delete-data --dry-run"


def inventory(config_dir: str | Path, *, service_registered: bool | None = None) -> list[FootprintItem]:
    """Everything this tool has put on the machine, in the order
    ``uninstall`` removes it. ``service_registered`` comes from
    ``installer.is_registered`` (``None`` means it could not be checked)."""
    config_dir = Path(config_dir)
    settings_path, settings = _settings(config_dir)
    items: list[FootprintItem] = []

    health = hook_health.check(config_dir)
    items.append(
        FootprintItem(
            key="snapshot_hook",
            title="Config snapshot hook",
            status="installed" if health.command else "not installed",
            where=home_label(settings_path),
            what_it_does=(
                "When a Claude Code session starts, runs a small Python script that records your settings: "
                "key names, a few safe values and file sizes, never file contents. It runs in the background "
                "and prints nothing."
            ),
            token_cost="None. The hook prints nothing, so nothing is added to Claude's context.",
            undo="claude-token-lens uninstall",
        )
    )
    items.append(
        FootprintItem(
            key="statusline",
            title="Statusline",
            status="installed" if is_own_statusline(settings) else "not installed",
            where=home_label(settings_path),
            what_it_does=(
                "Shows context, usage limits and cache health under the prompt in the terminal, and logs them "
                "for the Usage and Cache tabs. Claude Code runs it in the terminal only, not in the desktop app."
            ),
            token_cost="None. The statusline is shown to you; Claude never reads it.",
            undo="claude-token-lens uninstall",
        )
    )
    items.append(
        FootprintItem(
            key="service",
            title="Dashboard at logon",
            status={True: "installed", False: "not installed"}.get(service_registered, "unknown"),
            where="Scheduled task, launch agent or systemd user unit",
            what_it_does=(
                "Starts the dashboard when you log on. It reads your transcripts on this machine and makes no "
                "network calls."
            ),
            token_cost="None. It reads files; it never calls Claude.",
            undo="claude-token-lens uninstall-service",
        )
    )

    for backup in apply_mod.list_backups(config_dir):
        # "one-off" is cli.ONE_OFF_PROFILE_ID, recorded for apply --set.
        label = "one-off change" if backup.profile_id in ("one-off", "") else f"profile {backup.profile_id}"
        items.append(
            FootprintItem(
                key=f"apply:{backup.ts}",
                title=f"Applied {label} ({backup.file_count} file{'s' if backup.file_count != 1 else ''})",
                status="undone" if backup.reverted_at else "in place",
                where=f"{_scope_label(backup.scope)}; backup in {home_label(config_dir / 'backups' / backup.ts)}",
                what_it_does="Changed Claude Code settings or agent files. This changes how Claude works from the next session.",
                token_cost=(
                    "None by itself: Claude Code reads settings when a session starts, and every session builds "
                    "its cache from scratch anyway. After that it saves or costs what its estimate said."
                ),
                undo=f"claude-token-lens apply --revert {backup.ts}",
            )
        )

    active = config_dir / "active-profile"
    if active.is_file():
        try:
            profile_id = active.read_text(encoding="utf-8").strip()
        except OSError:
            profile_id = ""
        items.append(
            FootprintItem(
                key="active_profile",
                title=f"Active profile marker ({profile_id or 'unknown'})",
                status="installed",
                where=home_label(active),
                what_it_does="Remembers which profile you applied last, so snapshots can be tagged with it.",
                token_cost="None.",
                undo="Deleted with this tool's data folder.",
            )
        )

    data_size = _folder_size(config_dir) if config_dir.is_dir() else 0
    items.append(
        FootprintItem(
            key="data",
            title="This tool's data folder",
            status="installed" if config_dir.is_dir() else "not installed",
            where=home_label(config_dir),
            what_it_does=(
                f"Holds the dashboard database, parsed-transcript cache, settings snapshots, usage log, your "
                f"profiles and the backups that let you undo applied changes ({_size_label(data_size)})."
            ),
            token_cost="None.",
            undo="claude-token-lens uninstall --delete-data",
        )
    )
    return items


def _scope_label(scope: str) -> str:
    return {
        "user": "your user settings (every project)",
        "project-local": "this project, on your machine only",
        "repo": "this project, for everyone who uses the repository",
    }.get(scope, scope or "unknown scope")


@dataclass(slots=True)
class UninstallPlan:
    settings_path: Path
    #: Plain sentences, one per settings.json entry removed.
    settings_changes: list[str] = field(default_factory=list)
    settings_diff: str = ""
    new_settings_text: str | None = None
    #: Applied changes still in place, newest first.
    applied: list[apply_mod.BackupInfo] = field(default_factory=list)
    data_dir: Path | None = None


def plan_uninstall(config_dir: str | Path) -> UninstallPlan:
    """What ``uninstall`` would remove from ``settings.json``, and which
    applied changes are still in place. Writes nothing."""
    config_dir = Path(config_dir)
    settings_path, settings = _settings(config_dir)
    plan = UninstallPlan(settings_path=settings_path, data_dir=config_dir if config_dir.is_dir() else None)
    plan.applied = [b for b in reversed(apply_mod.list_backups(config_dir)) if not b.reverted_at]
    if settings is None:
        return plan

    before = settings_path.read_text(encoding="utf-8")
    after_settings = json.loads(before)
    hooks = after_settings.get("hooks")
    groups = hooks.get("SessionStart") if isinstance(hooks, dict) else None
    if isinstance(groups, list):
        kept_groups = []
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if isinstance(entries, list):
                kept = [
                    e
                    for e in entries
                    if not (isinstance(e, dict) and hook_health.HOOK_SCRIPT_NAME in str(e.get("command", "")))
                ]
                if len(kept) != len(entries):
                    plan.settings_changes.append("Remove the SessionStart hook that runs the config snapshot script.")
                if not kept:
                    continue
                group = {**group, "hooks": kept}
            kept_groups.append(group)
        if kept_groups:
            hooks["SessionStart"] = kept_groups
        else:
            hooks.pop("SessionStart", None)
            if not hooks:
                after_settings.pop("hooks", None)
    if is_own_statusline(after_settings):
        after_settings.pop("statusLine", None)
        plan.settings_changes.append("Remove the claude-token-lens statusline.")

    if plan.settings_changes:
        after = json.dumps(after_settings, indent=2, ensure_ascii=False) + "\n"
        plan.new_settings_text = after
        plan.settings_diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="settings.json (now)",
                tofile="settings.json (after)",
            )
        )
    return plan


def remove_settings_entries(plan: UninstallPlan, *, now: datetime | None = None) -> Path:
    """Write ``plan``'s ``settings.json`` after backing the file up to
    ``settings.json.bak-<UTC timestamp>``. Returns the backup path."""
    if plan.new_settings_text is None:
        raise ValueError("nothing to remove")
    now = now or datetime.now(timezone.utc)
    backup = plan.settings_path.with_name(f"settings.json.bak-{now.strftime('%Y%m%dT%H%M%SZ')}")
    shutil.copy2(plan.settings_path, backup)
    plan.settings_path.write_text(plan.new_settings_text, encoding="utf-8")
    return backup


def delete_data(config_dir: str | Path) -> list[str]:
    """Delete this tool's data folder. Returns the paths that could not
    be removed (a database still open by a running dashboard, say)."""
    failures: list[str] = []

    def _onerror(_func, path, exc_info):
        failures.append(f"{path}: {exc_info[1]}")

    shutil.rmtree(Path(config_dir), onerror=_onerror)
    return failures


__all__ = [
    "EXPECTATIONS",
    "FootprintItem",
    "UNINSTALL_COMMAND",
    "UninstallPlan",
    "delete_data",
    "home_label",
    "inventory",
    "is_own_statusline",
    "plan_uninstall",
    "remove_settings_entries",
]
