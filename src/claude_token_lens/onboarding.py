"""``claude-token-lens init`` (v0.3 milestone, plan "Configuration
layers" section's "Asked, not guessed (v0.3 init)"): detect what's
already on the machine, ask (or derive, under ``--non-interactive``) a
handful of questions this codebase genuinely cannot infer on its own,
write ``config.toml``/``<config_dir>/projects/<slug>.toml``, print the
install-step fragments (never edit ``settings.json`` directly), and
finish by running an initial :mod:`~claude_token_lens.baseline` capture
and printing its capture-window status.

This module never performs the dynamic import of the packaged
``hooks/snapshot-config.py`` hook script itself -- that stays
``cli.py``'s job (see its own ``_load_snapshot_hook_module``), so this
module only ever receives the hook's fragment text and the statusline's
install fragment as plain strings (``hook_fragment``/
``statusline_fragment`` below). This keeps ``onboarding.py`` importable
and unit-testable without touching ``importlib``/packaged-resource
plumbing at all.

Nothing here edits ``settings.json``: the plan is explicit that a
managed/user/project settings file is the user's own to hand-edit (or
apply a profile to, once ``profiles/apply.py`` -- out of this package's
current scope -- exists); this command only prints the fragments to add
and the commands that already exist for doing so safely
(``snapshot-config --install-hook``, ``statusline --print-install-
fragment``).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from . import baseline as baseline_mod
from . import discovery, hook_health, snapshots
from .config import (
    Config,
    ConfigError,
    ProjectConfig,
    load_config,
    save_project_config,
    write_config_values,
)
from .pricing import PricingError, load_pricing

__all__ = [
    "OnboardingError",
    "Detection",
    "detect",
    "Answers",
    "load_answers_file",
    "gather_answers",
    "run_init",
]

#: Default onboarding capture window length in days, when neither the
#: answers file nor an existing config.toml names one.
DEFAULT_CAPTURE_WINDOW_DAYS = 7

_TRUE_STRINGS = frozenset({"y", "yes", "true", "1", "on"})


def _relative_label(path: Path, base: Path) -> str:
    """``path`` rendered relative to ``base`` when possible, matching
    ``profiles.apply._relative_label``'s convention: ``base`` (here,
    always ``config_dir``, a path the caller explicitly supplied via
    ``--config-dir`` or its documented default) is not itself a privacy
    leak, so printing paths *relative to it* keeps the "Wrote ..."
    confirmation messages useful without ever putting a raw absolute
    filesystem path (home directory, username, drive letter) on stdout.
    Falls back to ``path``'s own name when it isn't under ``base``.
    """
    try:
        return str(path.relative_to(base))
    except ValueError:
        return path.name


class OnboardingError(Exception):
    """An ``--answers`` file could not be read or is not valid JSON.
    Same "stand-alone, user-facing" convention as
    :class:`~claude_token_lens.config.ConfigError`.
    """


@dataclass(slots=True)
class Detection:
    """What ``init`` found already on the machine before asking
    anything -- printed verbatim so the user can see what's being
    derived from, and never itself containing a raw filesystem path
    (only a redacted slug -- see ``discovery.redact_slug``) or session
    content, per this project's privacy invariant.
    """

    config_dir: Path
    config_dir_exists: bool
    existing_config: Config
    snapshot_count: int
    usage_log_present: bool
    #: The current directory's project slug, already redacted.
    project_slug: str
    #: How many project directories are discoverable under
    #: ``projects_root`` at all (not filtered to the current one).
    project_count: int


def detect(config_dir: str | Path, projects_root_path: str | Path) -> Detection:
    """Gather :class:`Detection` for ``config_dir``/``projects_root_path``.
    Tolerant throughout: a missing config dir, no snapshots, and no
    discoverable projects are all just facts to report, not errors.
    """
    config_dir = Path(config_dir)
    projects_root_path = Path(projects_root_path)

    try:
        existing_config = load_config(config_dir)
    except ConfigError:
        # A malformed config.toml is surfaced later, when init tries to
        # write it back (write_config_values re-validates) -- detection
        # itself never fails, it just reports the defaults for now.
        existing_config = Config()

    try:
        snapshot_count = len(snapshots.load_snapshots(config_dir))
    except Exception:
        snapshot_count = 0

    usage_log_present = (config_dir / "usage-log.csv").exists()
    project_slug = discovery.redact_slug(discovery.slug_for(os.getcwd()))

    project_count = 0
    if projects_root_path.is_dir():
        project_count = len(
            discovery.resolve_project_dirs(
                projects_root_path, all_projects=True, exclude_projects=existing_config.exclude_projects
            )
        )

    return Detection(
        config_dir=config_dir,
        config_dir_exists=config_dir.exists(),
        existing_config=existing_config,
        snapshot_count=snapshot_count,
        usage_log_present=usage_log_present,
        project_slug=project_slug,
        project_count=project_count,
    )


@dataclass(slots=True)
class Answers:
    """The "Asked, not guessed (v0.3 init)" question set, resolved --
    from an ``--answers`` file, interactively, or (``--non-interactive``)
    derived from the current :class:`Detection`/:class:`Config`. Every
    value here ends up in either ``config.toml`` or this project's own
    ``projects/<slug>.toml``.
    """

    billing: str = "api"
    exclude_projects: list[str] = field(default_factory=list)
    launch_overlays: bool = False
    shared_project_config: bool = False
    tz: str | None = None
    apply_scope: str = "user"
    capture_window: int = DEFAULT_CAPTURE_WINDOW_DAYS
    #: One line per question answered by derivation rather than by the
    #: user -- printed so a ``--non-interactive`` run never silently
    #: guesses without saying so (this task's own "derive instead of
    #: ask, and say how" acceptance rule).
    notes: list[str] = field(default_factory=list)


def load_answers_file(path: str | Path) -> dict:
    """Parse an ``--answers`` file: a flat JSON object whose keys are
    any of :class:`Answers`' field names (any subset; omitted keys fall
    back to derivation the same as if no file were given at all).
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OnboardingError(f"cannot read answers file: {path} ({exc})") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OnboardingError(f"malformed answers file ({path}): not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise OnboardingError(f"malformed answers file ({path}): expected a JSON object")
    return data


def _ask(
    key: str,
    prompt: str,
    default: str,
    *,
    answers_data: dict | None,
    non_interactive: bool,
    stdin: IO[str],
    stdout: IO[str],
    notes: list[str],
) -> str:
    if answers_data is not None and key in answers_data:
        return str(answers_data[key])
    if non_interactive:
        notes.append(f"{key}: not given in --answers; used default {default!r}")
        return default
    stdout.write(f"{prompt} [{default}]: ")
    stdout.flush()
    raw = stdin.readline()
    raw = (raw or "").strip()
    return raw if raw else default


def _ask_bool(
    key: str,
    prompt: str,
    default: bool,
    *,
    answers_data: dict | None,
    non_interactive: bool,
    stdin: IO[str],
    stdout: IO[str],
    notes: list[str],
) -> bool:
    if answers_data is not None and key in answers_data:
        raw = answers_data[key]
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in _TRUE_STRINGS
    raw = _ask(
        key,
        f"{prompt} (y/n)",
        "y" if default else "n",
        answers_data=answers_data,
        non_interactive=non_interactive,
        stdin=stdin,
        stdout=stdout,
        notes=notes,
    )
    return raw.strip().lower() in _TRUE_STRINGS


def gather_answers(
    *,
    detection: Detection,
    answers_path: str | Path | None = None,
    non_interactive: bool = False,
    stdin: IO[str] = sys.stdin,
    stdout: IO[str] = sys.stdout,
) -> Answers:
    """Resolve every :class:`Answers` field: an ``--answers`` file wins
    for any key it names; otherwise interactive prompting (default:
    derived from ``detection.existing_config``); under
    ``--non-interactive`` with no matching answers-file key, the derived
    default is used and recorded in ``Answers.notes``.
    """
    answers_data = load_answers_file(answers_path) if answers_path is not None else None
    notes: list[str] = []
    existing = detection.existing_config

    billing = _ask(
        "billing",
        "Billing mode (api/subscription)",
        existing.billing,
        answers_data=answers_data,
        non_interactive=non_interactive,
        stdin=stdin,
        stdout=stdout,
        notes=notes,
    )

    exclude_raw = answers_data.get("exclude_projects") if answers_data is not None else None
    if exclude_raw is None:
        exclude_str = _ask(
            "exclude_projects",
            "Comma-separated project slugs to always exclude (blank for none)",
            ",".join(existing.exclude_projects),
            answers_data=None,
            non_interactive=non_interactive,
            stdin=stdin,
            stdout=stdout,
            notes=notes,
        )
        exclude_projects = [s.strip() for s in exclude_str.split(",") if s.strip()]
    elif isinstance(exclude_raw, list):
        exclude_projects = [str(item) for item in exclude_raw]
    else:
        exclude_projects = [s.strip() for s in str(exclude_raw).split(",") if s.strip()]

    launch_overlays = _ask_bool(
        "launch_overlays",
        "Do you launch Claude Code with --settings/CLAUDE_CONFIG_DIR overlays "
        "rather than each project's own settings files",
        existing.launch_overlays,
        answers_data=answers_data,
        non_interactive=non_interactive,
        stdin=stdin,
        stdout=stdout,
        notes=notes,
    )

    shared_project_config = _ask_bool(
        "shared_project_config",
        "Are this project's agents/skills shared with colleagues (e.g. committed "
        "to a shared repo)",
        existing.shared_project_config,
        answers_data=answers_data,
        non_interactive=non_interactive,
        stdin=stdin,
        stdout=stdout,
        notes=notes,
    )

    tz_raw = _ask(
        "tz",
        "Timezone (IANA name, blank for the machine's own local zone)",
        existing.tz or "",
        answers_data=answers_data,
        non_interactive=non_interactive,
        stdin=stdin,
        stdout=stdout,
        notes=notes,
    )
    tz = tz_raw or None

    apply_scope = _ask(
        "apply_scope",
        "Default scope for applying a profile (user/project-local/repo)",
        existing.apply_scope,
        answers_data=answers_data,
        non_interactive=non_interactive,
        stdin=stdin,
        stdout=stdout,
        notes=notes,
    )

    capture_window_raw = _ask(
        "capture_window",
        "Onboarding capture window length in days",
        str(existing.capture_window or DEFAULT_CAPTURE_WINDOW_DAYS),
        answers_data=answers_data,
        non_interactive=non_interactive,
        stdin=stdin,
        stdout=stdout,
        notes=notes,
    )
    try:
        capture_window = int(capture_window_raw)
    except (TypeError, ValueError):
        capture_window = DEFAULT_CAPTURE_WINDOW_DAYS
        notes.append(
            f"capture_window: {capture_window_raw!r} is not an integer; used default "
            f"{capture_window!r}"
        )

    return Answers(
        billing=billing,
        exclude_projects=exclude_projects,
        launch_overlays=launch_overlays,
        shared_project_config=shared_project_config,
        tz=tz,
        apply_scope=apply_scope,
        capture_window=capture_window,
        notes=notes,
    )


def _offer_hook_repair(health, *, repair_hook: bool, non_interactive: bool, stdin, stdout, now) -> None:
    """A broken SessionStart hook command that can be fixed (see
    ``hook_health``): repair it with ``--repair-hook`` or a yes at the
    prompt, never silently. The fix changes only that command string,
    and settings.json is backed up first."""
    if health.fixed_command is None:
        return
    stdout.write(f"The hook command in {health.settings_path} is:\n  {health.command!r}\n")
    stdout.write(f"It should be:\n  {health.fixed_command!r}\n")
    if not repair_hook:
        if non_interactive:
            stdout.write("Run 'claude-token-lens init --repair-hook' to fix it (settings.json is backed up first).\n\n")
            return
        stdout.write("Fix it now? settings.json is backed up first. (y/n) [n]: ")
        stdout.flush()
        if (stdin.readline() or "").strip().lower() not in _TRUE_STRINGS:
            stdout.write("Left unchanged.\n\n")
            return
    try:
        backup = hook_health.repair(health, now=now)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        stdout.write(f"Could not fix the hook command: {exc}\n\n")
        return
    stdout.write(f"Fixed. The previous settings.json is at {backup}\n\n")


def run_init(
    *,
    config_dir: str | Path,
    projects_root_path: str | Path,
    answers_path: str | Path | None = None,
    non_interactive: bool = False,
    no_install: bool = False,
    hook_fragment: str,
    statusline_fragment: str,
    stdin: IO[str] = sys.stdin,
    stdout: IO[str] = sys.stdout,
    now: datetime | None = None,
    all_projects: bool = False,
    project: list[str] | None = None,
    project_family: str | None = None,
    repair_hook: bool = False,
) -> int:
    """Run the whole ``init`` flow: detect, ask/derive, write
    ``config.toml``/``projects/<slug>.toml``, print the install step,
    then run an initial baseline capture and print its capture-window
    status. Returns the process exit code (0 ok, 2 bad input -- a
    malformed ``--answers`` file or a ``config.toml`` shape
    :func:`~claude_token_lens.config.write_config_values` can't
    validate).

    ``all_projects``/``project``/``project_family`` mirror
    ``cli._resolve_project_dirs_for_args``'s own selection flags and
    fallback rule (fix S6): the baseline capture used to always scan
    only the current directory's own slug regardless of these -- a user
    running ``init --all-projects`` from a fresh directory silently got
    no baseline at all, even though the CLI's own subparser already
    accepted (and printed as ``detection.project_count``) the wider
    selection. ``config.toml``/``projects/<slug>.toml`` are still always
    written for the *current* project -- these flags affect only which
    project(s) the initial baseline is built from.
    """
    now = now or datetime.now(timezone.utc)
    config_dir = Path(config_dir)
    projects_root_path = Path(projects_root_path)

    detection = detect(config_dir, projects_root_path)

    stdout.write("claude-token-lens init\n")
    stdout.write(f"- config directory: {'exists' if detection.config_dir_exists else 'will be created'}\n")
    stdout.write(f"- current project: {detection.project_slug}\n")
    stdout.write(f"- projects discovered under projects root: {detection.project_count}\n")
    stdout.write(f"- config snapshots on file: {detection.snapshot_count}\n")
    stdout.write(f"- usage log present: {'yes' if detection.usage_log_present else 'no'}\n")
    health = hook_health.check(config_dir, now=now)
    stdout.write(f"- config snapshot hook: {health.summary()}\n")
    stdout.write("\n")
    _offer_hook_repair(health, repair_hook=repair_hook, non_interactive=non_interactive, stdin=stdin, stdout=stdout, now=now)

    try:
        answers = gather_answers(
            detection=detection,
            answers_path=answers_path,
            non_interactive=non_interactive,
            stdin=stdin,
            stdout=stdout,
        )
    except OnboardingError as exc:
        stdout.write(f"claude-token-lens init: {exc}\n")
        return 2

    for note in answers.notes:
        stdout.write(f"(derived) {note}\n")
    if answers.notes:
        stdout.write("\n")

    updates: dict = {
        "billing": answers.billing,
        "exclude_projects": answers.exclude_projects,
        "launch_overlays": answers.launch_overlays,
        "shared_project_config": answers.shared_project_config,
        "apply_scope": answers.apply_scope,
        "capture_window": answers.capture_window,
        "capture_started": now.isoformat(),
    }
    if answers.tz is not None:
        updates["tz"] = answers.tz

    try:
        written_path = write_config_values(config_dir, updates)
    except ConfigError as exc:
        stdout.write(f"claude-token-lens init: {exc}\n")
        return 2

    stdout.write(f"Wrote {_relative_label(written_path, config_dir)}\n")
    if written_path.name == "config.toml.new":
        stdout.write(
            "(the existing config.toml had a shape init could not merge automatically -- "
            "reconcile config.toml.new by hand and rename it into place)\n"
        )

    project_config = ProjectConfig(
        kind=None,
        shared_project_config=answers.shared_project_config,
        launch_overlays=answers.launch_overlays,
        apply_scope=answers.apply_scope,
    )
    project_slug = discovery.slug_for(os.getcwd())
    project_path = save_project_config(config_dir, project_slug, project_config)
    stdout.write(f"Wrote {_relative_label(project_path, config_dir)}\n\n")

    if no_install:
        stdout.write("Install step skipped (--no-install).\n\n")
    else:
        stdout.write(
            "Install step -- these are not written to settings.json automatically; "
            "merge them in yourself (or run 'claude-token-lens snapshot-config "
            "--install-hook' for the hook script itself):\n\n"
        )
        stdout.write(hook_fragment.rstrip("\n") + "\n\n")
        stdout.write(statusline_fragment.rstrip("\n") + "\n\n")

    config = load_config(config_dir)
    baseline_slugs = list(project) if project else None
    if not all_projects and not project_family and not baseline_slugs:
        baseline_slugs = [project_slug]
    project_dirs = discovery.resolve_project_dirs(
        projects_root_path,
        slugs=baseline_slugs,
        all_projects=all_projects,
        family_regex=project_family,
        exclude_projects=config.exclude_projects,
    )
    if not project_dirs:
        stdout.write(
            f"No recorded Claude Code sessions found yet for {detection.project_slug} -- "
            "skipping the initial baseline capture.\n"
        )
    else:
        try:
            pricing = load_pricing(path=config.pricing_path, config_dir=config_dir)
        except PricingError as exc:
            stdout.write(f"claude-token-lens init: {exc}\n")
            pricing = None
        if pricing is not None:
            record, _model = baseline_mod.build_baseline(
                config=config,
                pricing=pricing,
                config_dir=config_dir,
                project_dirs=project_dirs,
                now=now,
            )
            report_markdown = baseline_mod.render_onboarding_report(record)
            baseline_path = baseline_mod.save_baseline(config_dir, record, report_markdown)
            stdout.write(f"Wrote initial baseline {_relative_label(baseline_path, config_dir)}\n")
            stdout.write(f"Sessions analysed: {record['sessions_analysed']}\n")

    status = baseline_mod.capture_status(config, now=now)
    stdout.write(baseline_mod.format_capture_status(status) + "\n")

    return 0
