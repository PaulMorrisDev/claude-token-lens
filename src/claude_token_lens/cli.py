"""Command-line interface for claude-token-lens.

WP0 shipped the argparse surface and subcommand stubs. WP10c (this
module) wires every subcommand up to the engine WP1-WP9/WP10a already
built: ``report``/``sessions``/``recache``/``ttl``/``compactions`` all
go through :func:`~claude_token_lens.report.build_report`, filtered to
a single section (plus ``overview``) for the four focused subcommands;
``config-diff`` is its own consumer of
:mod:`~claude_token_lens.snapshots` (see :func:`_build_session_metrics`'s
docstring for why it can't just reuse ``build_report``'s "config"
section); ``log-usage``, ``probe``, ``statusline`` and ``scrub-fixture``
delegate to their own modules; ``pricing-check`` and ``snapshot-config``
are unchanged from WP2/WP7.

Exit codes throughout: 0 ok, 1 no data (an empty corpus, or no config
snapshots for ``config-diff`` -- always with a one-line reason on
stderr naming the projects root and window), 2 bad input (a
``ConfigError``/``PricingError``, a bad flag combination argparse
itself doesn't already catch, or an unimplemented subcommand).
"""

from __future__ import annotations

import argparse
import importlib.resources
import importlib.util
import json
import os
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import available_timezones

from . import __version__, baseline as baseline_mod, classify, discovery, installer as installer_mod, onboarding
from . import helptext, hook_health, probe as probe_mod, recache, snapshots
from . import statusline as statusline_mod
from .cache import DigestCache
from .config import Config, ConfigError, load_config, load_session_overrides
from .corpus import Corpus, load_corpus
from .parse import load_or_create_salt
from .model import Diagnostics, EventKind, PricingMeta, ReportMeta, ReportModel, Section, TranscriptResult
from .pricing import Pricing, PricingCoverage, PricingError, load_pricing, price_turn
from .render.csv_out import write_csv_dir
from .render.html import render_html
from .render.json_out import render_json
from .render.markdown import render_markdown
from .render.tables import format_cell
from .report import build_report
from .scorecard import ScorecardError
from .tools import log_usage as log_usage_mod
from .tools import scrub as scrub_mod

#: Every subcommand in the CLI surface, in the order they are
#: registered. "report" is also the default when no subcommand is given.
SUBCOMMANDS: tuple[str, ...] = (
    "report",
    "sessions",
    "recache",
    "ttl",
    "limits",
    "carry",
    "compaction-sim",
    "model-swap",
    "waste",
    "quality",
    "compactions",
    "config-diff",
    "snapshot-config",
    "probe-config",
    "log-usage",
    "pricing-check",
    "scrub-fixture",
    "probe",
    "statusline",
    "export",
    "monthly-report",
    "compare",
    "reconcile",
    "init",
    "baseline",
    "apply",
    "serve",
    "install-service",
    "uninstall-service",
    "update",
    "changes",
    "review",
    "check",
    "uninstall",
    "import",
    "team-report",
)

DEFAULT_SUBCOMMAND = "report"

# Tokens that must never trigger default-subcommand insertion because
# argparse needs to see them as the very first token.
_LEADING_PASSTHROUGH = ("-h", "--help", "--version")

#: Subcommands whose output is a filtered :func:`report.build_report`
#: section (plus "overview"), keyed by the section key they add. "report"
#: itself passes ``include=None`` (the whole report), so isn't listed
#: here.
_REPORT_LIKE_SECTIONS: dict[str, str] = {
    "sessions": "sessions",
    "recache": "recache",
    "ttl": "ttl",
    "limits": "limits",
    "carry": "carry",
    "compaction-sim": "compaction_sim",
    "model-swap": "model_swap",
    "waste": "waste",
    "quality": "quality",
    "compactions": "compactions",
}

#: Subcommands that render a report (directly or via a filtered
#: section) and so accept the shared output flags.
_REPORT_LIKE_COMMANDS: tuple[str, ...] = ("report", *_REPORT_LIKE_SECTIONS)


def _build_common_parser() -> argparse.ArgumentParser:
    """Global options shared by every subcommand, per the plan's CLI
    surface. Returned as a parent parser so each subcommand inherits them.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--projects-root",
        action="append",
        default=None,
        help="folder of Claude Code project folders; repeatable. Default ~/.claude/projects, "
        "plus any extra_projects_roots in config.toml (such as a WSL distro's)",
    )
    common.add_argument(
        "--project",
        action="append",
        default=None,
        help="repeatable; default is the current directory's project slug",
    )
    common.add_argument("--all-projects", action="store_true")
    common.add_argument("--project-family", metavar="REGEX")

    window = common.add_mutually_exclusive_group()
    window.add_argument("--days", type=int)
    window.add_argument("--since")
    common.add_argument("--until")
    common.add_argument("--limit", type=int)

    common.add_argument(
        "--window-by", choices=("mtime", "timestamp"), default="mtime"
    )
    common.add_argument("--pricing", metavar="PATH")
    common.add_argument(
        "--config-dir", metavar="PATH", default=None, help="default: ~/.claude/token-lens"
    )
    common.add_argument(
        "--tz",
        metavar="ZONE",
        default=None,
        help="IANA zone name overriding config.toml's tz for this run only "
        "(e.g. America/New_York); default: config.toml's tz, or the "
        "machine's own local zone",
    )
    common.add_argument(
        "--group-by",
        # Fix R6: this tuple used to be hand-maintained and had drifted
        # from classify._GROUP_KEYS (it offered a non-existent
        # "profile" key -- which classify.group_sessions() would reject
        # with an uncaught ValueError deep inside build_report() rather
        # than a clean CLI error -- and omitted the real "entrypoint"
        # key entirely). Derive the choices so they can never drift
        # again; argparse itself exits 2 with a one-line message on an
        # invalid choice.
        choices=sorted(classify._GROUP_KEYS),
    )

    cache = common.add_mutually_exclusive_group()
    cache.add_argument("--no-cache", action="store_true")
    cache.add_argument("--rebuild-cache", action="store_true")

    common.add_argument(
        "--jobs", type=int, default=1, metavar="N", help="parallel parsing workers (default: 1)"
    )

    verbosity = common.add_mutually_exclusive_group()
    verbosity.add_argument("--quiet", action="store_true")
    verbosity.add_argument("--verbose", action="store_true")

    return common


def _add_report_output_args(sub: argparse.ArgumentParser, *, allow_patch_set: bool = False) -> None:
    """Flags shared by every report-like subcommand (``report`` and the
    focused views: ``sessions``, ``recache``, ``ttl``, ``compactions``,
    ``limits``, ``carry``, ``compaction-sim``, ``model-swap``, ``waste``).
    """
    sub.add_argument(
        "--json", action="store_true", help="print the whole report as JSON instead of Markdown"
    )
    sub.add_argument(
        "--html", metavar="PATH", help="also write a single-file HTML report to PATH"
    )
    sub.add_argument(
        "--explain",
        action="store_true",
        help="add what each table shows, how to read it and when to act (Markdown output)",
    )
    sub.add_argument(
        "--csv-dir", metavar="DIR", help="also write one CSV file per table (plus an index) to DIR"
    )
    sub.add_argument(
        "--phases",
        action="store_true",
        help="add the DISCOVERY/IMPLEMENTATION/VERIFICATION phase-split section",
    )
    # v0.3 Task 2: --baseline is deliberately on _add_report_output_args
    # itself (not gated behind a keyword-only flag the way --patch-set
    # is via allow_patch_set) so every report-like subcommand
    # (report/sessions/recache/ttl/compactions) accepts it uniformly --
    # see report.build_report's own docstring for why the resulting
    # baseline_comparison section bypasses --group-by-style include
    # filtering rather than silently vanishing on a focused subcommand.
    sub.add_argument(
        "--baseline",
        metavar="ID|latest",
        help="add a baseline_comparison section against a saved `baseline` record",
    )
    # Fix R17: --allow-titles was removed -- report.py's own module
    # docstring documents that its allow_titles parameter is a
    # currently-permanent no-op (nothing anywhere in this codebase
    # captures customTitle/ai-title text to gate in the first place), so
    # the flag implied a privacy control that did not actually exist.
    # build_report() still accepts the keyword (matching its required
    # signature; report.py is out of this fix's file scope), always
    # called with the default.
    if allow_patch_set:
        sub.add_argument(
            "--patch-set",
            action="store_true",
            help="print the recommendation patch set after the report",
        )


def _add_config_diff_args(sub: argparse.ArgumentParser) -> None:
    group = sub.add_mutually_exclusive_group(required=True)
    group.add_argument("--key", metavar="KEY", help="one flattened config key to diff sessions by")
    group.add_argument(
        "--auto-keys",
        action="store_true",
        help="diff every config key that changed across the available snapshots",
    )


def _add_compare_output_args(sub: argparse.ArgumentParser) -> None:
    """``--json``/``--html``/``--csv-dir`` only -- ``compare`` has no
    ``--phases``/``--patch-set`` concept, so it doesn't share
    ``_add_report_output_args`` wholesale.
    """
    sub.add_argument(
        "--json", action="store_true", help="print the comparison as JSON instead of Markdown"
    )
    sub.add_argument(
        "--html", metavar="PATH", help="also write a single-file HTML comparison to PATH"
    )
    sub.add_argument(
        "--csv-dir", metavar="DIR", help="also write one CSV file per table (plus an index) to DIR"
    )


def _add_compare_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--a",
        required=True,
        metavar="SPEC",
        dest="arm_a",
        help="arm A selector: window:<since>..<until>, key:<key>=<value>, profile:<id>, or project:<slug>[,<slug>...]",
    )
    sub.add_argument(
        "--b",
        required=True,
        metavar="SPEC",
        dest="arm_b",
        help="arm B selector, same grammar as --a",
    )
    sub.add_argument(
        "--stratify",
        default="purpose,mode",
        metavar="KEY,KEY",
        help="comma-separated stratification keys (purpose, mode; default: purpose,mode)",
    )
    sub.add_argument(
        "--min-sessions",
        type=int,
        default=None,
        metavar="N",
        help="minimum sessions required per arm (per stratum) before a row counts as sample_ok "
        "(default: config.toml's min_sessions, itself 5)",
    )
    _add_compare_output_args(sub)


def _add_reconcile_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--admin-csv", required=True, metavar="FILE", help="path to an Admin API usage/cost export CSV"
    )
    sub.add_argument(
        "--by",
        default="day",
        choices=("day", "model", "day,model"),
        help="grouping for the reconciliation table (default: day)",
    )
    _add_compare_output_args(sub)


def _add_scrub_fixture_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--session-dir", metavar="PATH", help="path to <project_dir>/<session_id> to scrub")
    sub.add_argument("--out", metavar="PATH", help="output directory to write the scrubbed session into")
    sub.add_argument("--verify", metavar="OUT_DIR", help="run the privacy scan over an already-scrubbed directory")
    sub.add_argument(
        "--key-seed",
        default=None,
        metavar="SEED",
        help="deterministic HMAC key seed (tests only) -- omit for a random key",
    )


def _add_probe_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--file", metavar="PATH", help="probe a single transcript file instead of a project"
    )


def _add_statusline_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--print-install-fragment",
        "--install",
        dest="print_install_fragment",
        action="store_true",
        help="print the statusLine settings.json fragment instead of reading stdin",
    )


def _add_export_args(sub: argparse.ArgumentParser) -> None:
    """Flags for the ``export`` subcommand (S1-exports, plan "Feeds
    existing tooling" / "Aggregation without surveillance"): aggregate-only
    and hashed by default (in every mode -- fix for review finding 2),
    per-session is opt-in and the hashing opt-out still redacts the
    OS-username segment rather than printing the slug fully raw (see
    ``exports._apply_slug_redaction``).
    """
    sub.add_argument(
        "--format",
        choices=("csv-flat", "json", "otel-jsonl"),
        default="csv-flat",
        help="export format (default: csv-flat)",
    )
    aggregate = sub.add_mutually_exclusive_group()
    aggregate.add_argument(
        "--aggregate-only",
        action="store_true",
        dest="aggregate_only",
        default=None,
        help="no session ids, no per-session rows (default)",
    )
    aggregate.add_argument(
        "--per-session",
        action="store_false",
        dest="aggregate_only",
        help="opt in to per-session rows (includes session ids)",
    )
    hash_slugs = sub.add_mutually_exclusive_group()
    hash_slugs.add_argument(
        "--hash-slugs",
        action="store_true",
        dest="hash_slugs",
        default=None,
        help="replace project slugs with a salted hash (default, in every mode)",
    )
    hash_slugs.add_argument(
        "--no-hash-slugs",
        action="store_false",
        dest="hash_slugs",
        help="don't hash project slugs -- an explicit, informed opt-out, not the "
        "default; the OS-username segment is still redacted to '<user>' "
        "rather than printed raw, and a one-line warning is printed to stderr",
    )
    sub.add_argument("--out", metavar="PATH", help="write to PATH instead of stdout")
    sub.add_argument(
        "--generated-at",
        metavar="ISO8601",
        default=None,
        dest="generated_at",
        help="override --format json's meta.generated_at (also honours the "
        "SOURCE_DATE_EPOCH env var) so the export is byte-reproducible "
        "(nit 21/19: it wasn't wired up to the CLI before)",
    )
    # v0.3 Task 1: a team-aggregate document is a different shape from
    # every other --format (per-group sums, never a session id or a
    # per-session row), so it's its own flag rather than a --format
    # choice -- --aggregate-only/--per-session/--hash-slugs (which
    # govern the *per-session* export shapes) have no effect on it, and
    # --format itself is ignored (a team document is always JSON) since
    # it has no CSV/OTel analogue.
    sub.add_argument(
        "--aggregate",
        action="store_true",
        help="write a team-aggregate JSON document (see `import`/`team-report`, "
        "docs/team.md) instead of a per-project export; aggregate-only and "
        "hashed by construction",
    )
    sub.add_argument(
        "--include-projects",
        action="store_true",
        dest="include_projects",
        help="with --aggregate, add a 'projects' list of hashed project slugs "
        "(default: omitted -- opt in per person)",
    )


def _add_monthly_report_args(sub: argparse.ArgumentParser) -> None:
    """Flags for the ``monthly-report`` subcommand (S1-exports, plan
    "Finance" / feature 10 promoted to v0.2 ``serve --monthly-report``).
    """
    sub.add_argument(
        "--out", metavar="DIR", required=True, help="directory to write the Markdown/HTML report into"
    )
    sub.add_argument(
        "--month",
        metavar="YYYY-MM",
        default=None,
        help="calendar month to report on (default: the previous calendar month)",
    )
    sub.add_argument(
        "--generated-at",
        metavar="ISO8601",
        default=None,
        dest="generated_at",
        help="override the report's trailing 'Generated at: ...' line/comment "
        "(also honours the SOURCE_DATE_EPOCH env var) so repeated runs are "
        "genuinely byte-identical (fix for review finding 11)",
    )


def _add_serve_args(sub: argparse.ArgumentParser) -> None:
    """Extra flags for the ``serve`` subcommand (v0.2's local JSON API +
    watcher service, ``service/serve.py``). ``--projects-root`` and
    ``--config-dir`` are already on the common parser; everything below
    is serve-only.
    """
    sub.add_argument("--port", type=int, default=8765, help="default: 8765")
    sub.add_argument(
        "--bind",
        default="127.0.0.1",
        metavar="ADDRESS",
        help="default: 127.0.0.1 (loopback only)",
    )
    sub.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow --bind to a non-loopback address (refused by default)",
    )
    sub.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        metavar="NAME",
        dest="allowed_host",
        help="repeatable; an extra host name the browser may use to reach the dashboard "
        "(loopback names and a specific --bind address are always allowed)",
    )
    sub.add_argument(
        "--poll-interval",
        type=float,
        default=30.0,
        metavar="SECONDS",
        dest="poll_interval",
        help="watcher poll interval in seconds (default: 30)",
    )
    sub.add_argument(
        "--retention-days",
        type=int,
        default=None,
        metavar="N",
        help="prune sessions older than N days on every poll tick (default: config.toml's retention_days, else keep forever)",
    )
    sub.add_argument(
        "--exclude-project",
        action="append",
        default=None,
        metavar="SLUG",
        dest="exclude_project",
        help="repeatable; project slug never scanned",
    )
    sub.add_argument(
        "--once",
        action="store_true",
        help="run a single watcher tick, print its WatcherStats, and exit instead of serving",
    )
    sub.add_argument(
        "--billing-mode",
        choices=("api", "subscription"),
        default=None,
        dest="billing_mode",
        metavar="{api,subscription}",
        help="stamped onto every session (default: 'billing' from "
        "<config-dir>/config.toml, else worked out from your transcripts)",
    )
    sub.add_argument(
        "--monthly-report",
        default=None,
        metavar="DIR",
        dest="monthly_report_dir",
        help="while serving, write the previous month's report (as 'monthly-report' does) into DIR "
        "when it is missing; checked at startup and hourly (default: none)",
    )
    sub.add_argument(
        "--purge",
        action="store_true",
        help="delete <config-dir>/service.db (and its WAL/SHM sidecars) and exit; "
        "requires --yes",
    )
    sub.add_argument(
        "--yes",
        action="store_true",
        help="confirm a destructive flag such as --purge (no interactive prompt)",
    )


def _add_install_service_args(sub: argparse.ArgumentParser) -> None:
    """Extra flags for ``install-service`` (v3): register ``serve`` to
    start at logon/boot. ``--projects-root``/``--config-dir`` are
    already on the common parser.
    """
    sub.add_argument("--port", type=int, default=8765, help="default: 8765 (must match how you run 'serve')")
    sub.add_argument(
        "--bind",
        default="127.0.0.1",
        metavar="ADDRESS",
        help="default: 127.0.0.1 (loopback only)",
    )
    sub.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="print exactly what would be written/run, without writing or running anything",
    )


def _add_update_args(sub: argparse.ArgumentParser) -> None:
    """Extra flags for ``update``: where to install from, and the same
    ``--port``/``--bind``/``--dry-run`` as ``install-service``, which it
    runs last."""
    _add_install_service_args(sub)
    sub.add_argument(
        "--from",
        dest="source",
        default=UPDATE_SOURCE,
        metavar="SOURCE",
        help="what pip installs from (default: the GitHub repository; a local folder also works)",
    )
    sub.add_argument(
        "--no-service",
        action="store_true",
        dest="no_service",
        help="install the new version but leave the running dashboard alone",
    )


def _add_uninstall_service_args(sub: argparse.ArgumentParser) -> None:
    """Extra flags for ``uninstall-service`` (v3): the inverse of
    ``install-service``. Takes no ``--port``/``--bind`` -- removing a
    registration never depends on them (see
    ``installer.InstallPlan.uninstall_commands``/``uninstall_files``).
    """
    sub.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="print exactly what would be run/removed, without running or removing anything",
    )


def _add_uninstall_args(sub: argparse.ArgumentParser) -> None:
    """Extra flags for ``uninstall``. Without ``--yes`` every step shows
    what it changes and asks first."""
    _add_claude_root_arg(sub)
    sub.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="show every step and change, without changing anything",
    )
    sub.add_argument(
        "--yes",
        action="store_true",
        help="make the settings.json and service changes without asking (they are still printed)",
    )
    sub.add_argument(
        "--revert-changes",
        action="store_true",
        dest="revert_changes",
        help="also undo every change 'apply' made that is still in place, newest first",
    )
    sub.add_argument(
        "--delete-data",
        action="store_true",
        dest="delete_data",
        help="also delete this tool's data folder (database, snapshots, usage log, profiles and backups)",
    )


def _add_snapshot_config_args(sub: argparse.ArgumentParser) -> None:
    """Extra flags for the ``snapshot-config`` subcommand only (WP7). Every
    other subcommand stays a bare stub, so this is added just for this one
    subparser rather than the shared common parser.
    """
    group = sub.add_mutually_exclusive_group()
    group.add_argument(
        "--print-hook",
        action="store_true",
        help="print the SessionStart settings.json fragment (Windows and POSIX)",
    )
    group.add_argument(
        "--install-hook",
        action="store_true",
        help="copy hooks/snapshot-config.py into <config-dir>/hooks/ (<config-dir> "
        "defaults to ~/.claude/token-lens)",
    )
    sub.add_argument(
        "--managed-path",
        metavar="PATH",
        default=None,
        help="override the platform managed-settings.json path (fix 7; "
        "default is the platform's own policy-file location)",
    )
    sub.add_argument(
        "--project-dir",
        metavar="PATH",
        default=None,
        help="run the hook for this project directory instead of the current one "
        "(schema 2, plan 'Configuration layers' section). Named --project-dir, "
        "not --project, because the common --project flag already means "
        "'a repeatable project slug to filter by'.",
    )
    sub.add_argument(
        "--min-interval",
        type=int,
        default=300,
        metavar="SECONDS",
        help="skip the write when the newest snapshot for this project is both "
        "younger than this and has identical content (default: 300; the hook "
        "script itself already accepts this flag -- fix #13 exposes it here too)",
    )


def _add_apply_args(sub: argparse.ArgumentParser) -> None:
    """Flags for the ``apply`` subcommand (v0.3 milestone, ``profiles/apply.py``):
    apply a profile's allowlisted settings/agent/env levers to a project
    or the current user, dry-run its diff first, or revert/list a
    previous apply. ``profile`` is a catalogue id (``profiles.catalogue``)
    or a path to a profile TOML file; it is optional only because
    ``--revert``/``--list-backups`` don't need one.

    Named ``--project-dir``, not ``--project``: the common ``--project``
    flag every subcommand already has means "a repeatable project slug
    to filter a report by" (same collision, same resolution, as
    ``snapshot-config``/``probe-config``'s own ``--project-dir``).
    """
    sub.add_argument(
        "profile", nargs="?", metavar="PROFILE", help="catalogue id or path to a profile TOML file"
    )
    sub.add_argument(
        "--set",
        metavar="KEY=VALUE",
        action="append",
        default=None,
        dest="set_values",
        help="change one allowlisted setting instead of applying a profile (repeatable); "
        "a list value is comma-separated, e.g. tools=Read,Grep",
    )
    sub.add_argument(
        "--agent",
        metavar="NAME",
        default=None,
        help="with --set: change this agent's frontmatter (.claude/agents/NAME.md) instead of settings.json",
    )
    sub.add_argument(
        "--scope",
        choices=("user", "project-local", "repo"),
        default=None,
        help="default: user, or project-local when --project-dir is given",
    )
    sub.add_argument(
        "--project-dir",
        metavar="PATH",
        default=None,
        dest="project_dir",
        help="project directory for a project-local/repo scope",
    )
    sub.add_argument(
        "--claude-root",
        metavar="PATH",
        default=None,
        dest="claude_root",
        help="the Claude Code directory holding settings.json/agents/ for user scope "
        "(default: $CLAUDE_CONFIG_DIR, else ~/.claude -- see cli._resolve_claude_root; "
        "deliberately independent of --config-dir, which is this tool's own directory "
        "and may be pointed anywhere)",
    )
    sub.add_argument(
        "--dry-run", action="store_true", help="print the diff and how to apply it, without writing anything"
    )
    sub.add_argument(
        "--launch",
        action="store_true",
        help="write a one-session settings-only overlay instead of a persisted apply",
    )
    sub.add_argument(
        "--allow-tracked",
        action="store_true",
        dest="allow_tracked",
        help="allow writing a target file that is already tracked by git",
    )
    sub.add_argument(
        "--revert", metavar="TS", default=None, help="undo a previous apply, named by its backup timestamp"
    )
    sub.add_argument(
        "--ignore-changes",
        action="store_true",
        dest="ignore_changes",
        help="with --revert: restore the backup even if the file was edited after the apply",
    )
    sub.add_argument(
        "--force",
        action="store_true",
        help="create a missing agent frontmatter file from scratch instead of refusing",
    )
    sub.add_argument(
        "--list-backups",
        action="store_true",
        dest="list_backups",
        help="list previous applies (timestamp, profile, scope) and exit",
    )


def _add_claude_root_arg(sub: argparse.ArgumentParser) -> None:
    """``--claude-root`` for the commands that read or change Claude
    Code's own ``settings.json`` (``init``, ``uninstall``, ``changes``);
    ``apply`` declares its own with the same meaning."""
    sub.add_argument(
        "--claude-root",
        metavar="PATH",
        default=None,
        dest="claude_root",
        help="the Claude Code folder holding settings.json (default: $CLAUDE_CONFIG_DIR, else ~/.claude; "
        "never worked out from --config-dir)",
    )


def _add_init_args(sub: argparse.ArgumentParser) -> None:
    _add_claude_root_arg(sub)
    sub.add_argument(
        "--answers",
        metavar="FILE",
        default=None,
        help="a JSON file answering some or all of init's questions (any key it "
        "omits falls back to interactive prompting, or a derived default under "
        "--non-interactive)",
    )
    sub.add_argument(
        "--non-interactive",
        action="store_true",
        help="never prompt on stdin; any question --answers doesn't cover uses a "
        "derived default, printed as '(derived) ...' so nothing is guessed silently",
    )
    sub.add_argument(
        "--no-install",
        action="store_true",
        help="skip connecting to Claude Code: no SessionStart hook / statusLine change and no fragments printed",
    )
    sub.add_argument(
        "--repair-hook",
        action="store_true",
        dest="repair_hook",
        help="fix a SessionStart hook command that cannot run (a path broken by JSON escaping, "
        "a missing interpreter or a %%VARIABLE%%), without asking (settings.json is backed up first)",
    )
    sub.add_argument(
        "--connect",
        action="store_true",
        help="add the config snapshot hook (and a statusline, if you have none) to "
        "Claude Code's settings.json without asking; the change is still printed and the file backed up first",
    )
    service_group = sub.add_mutually_exclusive_group()
    service_group.add_argument(
        "--install-service",
        action="store_true",
        dest="install_service",
        help="register 'serve' to start at logon/boot without asking (also the "
        "--non-interactive default, which is otherwise 'no')",
    )
    service_group.add_argument(
        "--no-service",
        action="store_true",
        dest="no_service",
        help="skip init's final 'run the service at logon?' step entirely -- no "
        "question asked, nothing installed",
    )
    sub.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="show the settings.json change and the service-install plan without "
        "making either (config.toml and the initial baseline are still written)",
    )


def _add_baseline_args(sub: argparse.ArgumentParser) -> None:
    # Note: --days is already provided by the common parent parser
    # (mutually exclusive with --since) -- every subcommand inherits it,
    # baseline included, so it is not redefined here.
    sub.add_argument(
        "--finalise",
        action="store_true",
        help="accept this baseline even if the onboarding capture window hasn't finished",
    )
    sub.add_argument("--list", action="store_true", dest="list_baselines", help="list every saved baseline")
    sub.add_argument("--show", metavar="ID", default=None, help="print one saved baseline's onboarding report")


def _add_probe_config_args(sub: argparse.ArgumentParser) -> None:
    """Flags for the ``probe-config`` subcommand (schema 2): the same scan
    ``snapshot-config`` does, for an arbitrary project directory, without a
    session and without writing anything.
    """
    sub.add_argument(
        "--project-dir",
        metavar="PATH",
        default=None,
        help="project directory to scan (default: the current directory). Named "
        "--project-dir, not --project, because the common --project flag "
        "already means 'a repeatable project slug to filter by'.",
    )
    sub.add_argument(
        "--managed-path",
        metavar="PATH",
        default=None,
        help="override the platform managed-settings.json path (default is the "
        "platform's own policy-file location)",
    )


def _add_import_args(sub: argparse.ArgumentParser) -> None:
    """Flags for the ``import`` subcommand (v0.3 Task 1): one or more
    already-built team-aggregate documents (``export --aggregate``'s own
    output) to validate and copy into ``<config_dir>/team/``.
    """
    sub.add_argument(
        "files", nargs="+", metavar="FILE", help="team-aggregate JSON document(s) to import"
    )


def _add_team_report_args(sub: argparse.ArgumentParser) -> None:
    """Flags for the ``team-report`` subcommand (v0.3 Task 1): reads
    only already-imported documents under ``<config_dir>/team/`` (see
    ``import``), so it needs no project/window selection of its own --
    just the shared output flags. Deviation (report, don't silently
    resolve): the plan's ``[--md|--json|--html]`` gets no explicit
    ``--md`` switch -- "no output flag" already means Markdown for
    every other report-like/compare-like subcommand in this CLI
    (``_add_compare_output_args``), so adding one here would be the one
    inconsistent flag in the whole surface.
    """
    _add_compare_output_args(sub)
    sub.add_argument(
        "--min-sessions",
        type=int,
        default=5,
        metavar="N",
        help="minimum sessions required in a machine's own group row before a "
        "team-report cell shows a number rather than 'n<N' (default: 5)",
    )


def _make_parser() -> argparse.ArgumentParser:
    common = _build_common_parser()
    parser = argparse.ArgumentParser(prog="claude-token-lens")
    parser.add_argument(
        "--version", action="version", version=f"claude-token-lens {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command")
    for name in SUBCOMMANDS:
        help_text = {
            "snapshot-config": "capture Claude Code config (see config-diff to compare)",
            "report": "full report (default)",
            "sessions": "sessions-only report view",
            "recache": "RE-CACHE-only report view",
            "ttl": "TTL break-even-only report view",
            "limits": "usage-limits-only report view",
            "carry": "context-carry-cost-only report view (cost of carrying tool results across later turns)",
            "compaction-sim": "autoCompactWindow-sweep-only report view (modelled cost under other window settings)",
            "model-swap": "model-swap-only report view (modelled saving from a cheaper model tier)",
            "waste": "wasted-turn-spend-only report view (turns whose output was never used)",
            "quality": "quality-signals-only report view (failed agent runs, failed tool calls, corrections, "
            "compared by model and effort)",
            "compactions": "compactions-only report view",
            "config-diff": "compare sessions grouped by a config key's value",
            "pricing-check": "print the resolved rate card's provenance and rate table",
            "probe-config": "scan a project's config layers without a session (schema 2)",
            "log-usage": "append a pasted get_usage JSON payload to the usage log",
            "probe": "content-free schema histogram of a project or file",
            "statusline": "Claude Code statusLine handler (reads stdin JSON)",
            "export": "export digests as csv-flat, json or otel-jsonl (aggregate-only by default)",
            "monthly-report": "write a monthly Markdown/HTML finance report",
            "compare": "A/B compare two arms of sessions (window/key/profile/project), stratified by purpose+mode",
            "reconcile": "compare local usage/cost accounting against an Admin API CSV export, offline",
            "scrub-fixture": "scrub a real session into a privacy-safe test fixture",
            "apply": "apply a profile's settings/agent/env levers to a project or your user config",
            "init": "detect + ask (or derive) config, write config.toml, run an initial baseline",
            "baseline": "capture/list/show an onboarding baseline (mode mix, suggested profile, projected saving)",
            "serve": "run the local JSON API + watcher service",
            "install-service": "register 'serve' to start at logon/boot (Scheduled Task / systemd user unit / LaunchAgent)",
            "uninstall-service": "remove a logon/boot registration made by install-service (or by init)",
            "update": "install the newest version and restart the dashboard on it",
            "changes": "list what this tool has installed and changed, and the command that undoes each",
            "review": "review your CLAUDE.md files or skills: size, how often each is sent, cost, and fixes",
            "check": "quick actions: answer one token question (or all of them) with evidence and fixes",
            "uninstall": "remove the hook, statusline and logon service, optionally undo applied changes and delete data",
            "import": "validate and copy team-aggregate document(s) into <config_dir>/team/",
            "team-report": "cross-machine comparison built from every imported team document",
        }.get(name, f"{name} (not implemented yet)")
        sub = subparsers.add_parser(name, parents=[common], help=help_text)
        if name == "pricing-check":
            sub.add_argument(
                "--models",
                metavar="ID,ID,...",
                help="comma-separated model ids to resolve and report",
            )
        if name == "snapshot-config":
            _add_snapshot_config_args(sub)
        if name == "probe-config":
            _add_probe_config_args(sub)
        if name == "apply":
            _add_apply_args(sub)
        if name == "review":
            sub.add_argument(
                "what",
                choices=("claude-md", "skills"),
                help="claude-md: every CLAUDE.md file and rule; skills: every skill Claude Code lists",
            )
        if name == "check":
            from .quick_actions import CHECK_IDS

            sub.add_argument(
                "id",
                nargs="?",
                choices=CHECK_IDS,
                help="the check to run in full; leave it out for every check's one-line answer",
            )
        if name in _REPORT_LIKE_COMMANDS:
            _add_report_output_args(sub, allow_patch_set=(name == "report"))
        if name == "config-diff":
            _add_config_diff_args(sub)
        if name == "scrub-fixture":
            _add_scrub_fixture_args(sub)
        if name == "probe":
            _add_probe_args(sub)
        if name == "statusline":
            _add_statusline_args(sub)
        if name == "export":
            _add_export_args(sub)
        if name == "monthly-report":
            _add_monthly_report_args(sub)
        if name == "compare":
            _add_compare_args(sub)
        if name == "reconcile":
            _add_reconcile_args(sub)
        if name == "serve":
            _add_serve_args(sub)
        if name == "install-service":
            _add_install_service_args(sub)
        if name == "uninstall-service":
            _add_uninstall_service_args(sub)
        if name == "update":
            _add_update_args(sub)
        if name == "uninstall":
            _add_uninstall_args(sub)
        if name == "changes":
            _add_claude_root_arg(sub)
        if name == "init":
            _add_init_args(sub)
        if name == "baseline":
            _add_baseline_args(sub)
        if name == "import":
            _add_import_args(sub)
        if name == "team-report":
            _add_team_report_args(sub)
    return parser


# -- shared config-dir / project / corpus plumbing --------------------------


def _resolve_config_dir(cli_arg: str | Path | None) -> Path:
    """``--config-dir`` wins -- and IS the token-lens directory itself
    everywhere (fix config-dir: one meaning across the hook, the CLI and
    ``snapshots.py`` -- see ``snapshots.load_snapshots``'s docstring)
    -- else ``$CLAUDE_CONFIG_DIR/token-lens``; else ``~/.claude/token-lens``.
    Mirrors ``config.py``'s own ``_default_config_dir``/
    ``_resolve_config_dir`` -- each module in this package keeps its own
    copy of this small lookup rather than sharing one (see e.g.
    ``tools/log_usage.py``'s module docstring), and the CLI is no
    exception.
    """
    if cli_arg:
        return Path(cli_arg)
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base) if base else (Path.home() / ".claude")
    return root / "token-lens"


def _resolve_claude_root(cli_arg: str | Path | None) -> Path:
    """The Claude Code root directory: the one that directly holds
    ``settings.json`` and ``agents/``. ``--claude-root`` wins; else
    ``$CLAUDE_CONFIG_DIR``; else ``~/.claude``.

    Delegates to :func:`discovery.claude_root`, the one rule every
    command uses to find ``settings.json`` (``init``, ``uninstall``,
    ``changes``, the dashboard's hook checks, the snapshot hook).

    Fix B3: deliberately NOT derived from ``_resolve_config_dir``'s
    result. ``apply`` used to compute ``home = config_dir.parent``,
    which happens to equal this exact directory only when ``config_dir``
    took its own untouched default (``<claude-root>/token-lens``) --
    ``--config-dir``/``config.toml`` can point this tool's own
    token-lens directory anywhere, at which point ``.parent`` is just
    some unrelated directory. With the (also then-wrong) default,
    ``apply``'s ``home`` ended up equal to the Claude root itself, and
    ``profiles.apply._resolve_settings_path`` appended another
    ``.claude/`` on top of it -- so a user-scope apply silently wrote
    ``<claude-root>/.claude/settings.json`` (``~/.claude/.claude/settings.json``
    in the default layout) while printing "Applied ..." and leaving the
    real ``~/.claude/settings.json`` untouched.
    """
    return discovery.claude_root(cli_arg)


def _config_dir_args(config_dir: Path) -> str:
    """`` --config-dir "<absolute path>"`` for a hook or statusline
    command when this tool's data folder is not the default
    ``<claude-root>/token-lens``, else an empty string. Without it the
    hook and statusline, which Claude Code starts on its own, would
    write snapshots and the usage log to the default folder while the
    CLI and dashboard read ``--config-dir``."""
    config_dir = Path(config_dir).resolve()
    if config_dir == (discovery.claude_root() / "token-lens").resolve():
        return ""
    return f' --config-dir "{config_dir}"'


def _priced_turns(result: TranscriptResult):
    """Turns that actually got a ``turn_index`` (excludes synthetic and
    missing-usage turns). Deliberately duplicated rather than imported
    from ``report.py`` -- the same one-line-helper convention
    ``workflows.py``/``phases.py``/``report.py`` itself document.
    """
    return [t for t in result.turns if t.turn_index > 0]


def _service_projects_roots(args: argparse.Namespace) -> list[Path]:
    """The ``--projects-root`` folders the logon service is registered
    with: the ones given, or this computer's default. ``serve`` adds
    ``config.toml``'s ``extra_projects_roots`` itself each time it
    starts, so they are never baked into the registration."""
    return [Path(p) for p in args.projects_root] if args.projects_root else [discovery.projects_root()]


def _resolve_project_dirs_for_args(args: argparse.Namespace, config: Config) -> tuple[str, list[Path]]:
    roots = discovery.projects_roots(args.projects_root, config.extra_projects_roots)
    root = ", ".join(str(r) for r in roots)
    slugs = list(args.project) if args.project else None
    if not args.all_projects and not args.project_family and not slugs:
        slugs = [discovery.slug_for(os.getcwd())]
    project_dirs = discovery.resolve_project_dirs(
        roots,
        slugs=slugs,
        all_projects=args.all_projects,
        family_regex=args.project_family,
        exclude_projects=config.exclude_projects,
    )
    return root, project_dirs


def _window_description(args: argparse.Namespace) -> str:
    if args.since or args.until:
        start = f"since {args.since}" if args.since else "since the beginning"
        end = f"until {args.until}" if args.until else "until now"
        return f"{start} {end}"
    if args.days:
        return f"last {args.days} days"
    return "all time"


def _load_config_and_pricing(args: argparse.Namespace) -> tuple[Config | None, Pricing | None, Path, int | None]:
    """Resolve the config dir, load ``config.toml`` and the pricing rate
    card. Returns ``(config, pricing, config_dir, exit_code)`` -- on
    failure the first two are ``None`` and ``exit_code`` (2, always a bad
    input) is set, with the reason already printed to stderr.
    """
    config_dir = _resolve_config_dir(args.config_dir)
    try:
        config = load_config(config_dir)
    except ConfigError as exc:
        print(f"claude-token-lens: {exc}", file=sys.stderr)
        return None, None, config_dir, 2

    # Fix R24: --tz overrides config.toml's tz for this run only, the
    # same "explicit flag wins over the file" convention --pricing/
    # --config-dir already follow. classify.classify_session/usage.py's
    # _to_local both already fall back to the machine's local zone when
    # a zone name can't be resolved -- deliberately, since a bare
    # Windows install with no tzdata package can't resolve *any* named
    # zone (see classify.py's module docstring) and that's a machine
    # limitation, not a bad value. So this only rejects a --tz value
    # outright when the machine actually has a populated tz database to
    # check it against and the name genuinely isn't in it (a real
    # command-line typo); otherwise it's passed through uncontested and
    # degrades the same way a config.toml value already does.
    tz_override = getattr(args, "tz", None)
    if tz_override is not None:
        known_zones = available_timezones()
        if known_zones and tz_override not in known_zones:
            print(f"claude-token-lens: --tz {tz_override!r} is not a known IANA zone", file=sys.stderr)
            return None, None, config_dir, 2
        config.tz = tz_override

    pricing_path = args.pricing or config.pricing_path
    try:
        rates = load_pricing(path=pricing_path, config_dir=config_dir)
    except PricingError as exc:
        print(f"claude-token-lens: {exc}", file=sys.stderr)
        return None, None, config_dir, 2

    return config, rates, config_dir, None


def _print_corpus_stats(corpus: Corpus) -> None:
    print(
        f"[corpus] files={corpus.total_files} bytes={corpus.total_bytes} "
        f"cache_hits={corpus.cache_hits} cache_misses={corpus.cache_misses} "
        f"elapsed_s={corpus.elapsed_s:.3f}",
        file=sys.stderr,
    )


def _load_corpus_for_args(
    args: argparse.Namespace, config: Config, config_dir: Path, project_dirs: list[Path]
) -> Corpus:
    cache = None
    if not args.no_cache:
        cache = DigestCache(config_dir)
        if args.rebuild_cache:
            cache.purge(all=True)
    # Fix #8: wire the A3 read-target-hash salt up to the actual corpus
    # load -- previously nothing in src/ ever called set_salt/
    # load_or_create_salt, so Turn.read_target_hashes was always empty in
    # every shipped code path. load_corpus threads this through to both
    # the in-process (jobs == 1) parse calls and, for jobs > 1, every
    # ProcessPoolExecutor worker's own initializer.
    salt = load_or_create_salt(config_dir)
    corpus = load_corpus(
        project_dirs,
        days=args.days,
        since=args.since,
        until=args.until,
        limit=args.limit,
        window_by=args.window_by,
        cache=cache,
        jobs=args.jobs,
        exclude_projects=config.exclude_projects,
        salt=salt,
    )
    # Fix R21: --quiet was accepted by argparse (mutually exclusive with
    # --verbose) but never actually consulted anywhere -- a silent no-op
    # flag. The CLI's argparse wiring already keeps a human from passing
    # both at once, but this function's own contract shouldn't depend on
    # that: guard explicitly so --quiet reliably suppresses this stderr
    # diagnostic even if a future caller builds/mutates the Namespace
    # itself (e.g. a script driving this function directly) rather than
    # going through argparse's mutual-exclusion check.
    if args.verbose and not args.quiet:
        _print_corpus_stats(corpus)
    return corpus


def _print_table(table, currency: str) -> None:
    """Print one :class:`~claude_token_lens.model.Table` as a plain-text,
    fixed-width table (the same rendering ``pricing-check`` has always
    used). Shared by ``pricing-check`` and ``config-diff``.
    """
    headers = [column.label for column in table.columns]
    formatted_rows = [
        [
            format_cell(value, column.kind, currency=currency)
            for value, column in zip(row, table.columns)
        ]
        for row in table.rows
    ]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in formatted_rows)) if formatted_rows else len(headers[i])
        for i in range(len(headers))
    ]
    print(table.title)
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
    print("  ".join("-" * width for width in widths))
    for row in formatted_rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)))
    for note in table.notes:
        print(f"note: {note}")


def _resolve_generated_at(args: argparse.Namespace) -> str | None:
    """``args.generated_at`` (``--generated-at``) if given; else
    ``SOURCE_DATE_EPOCH`` (the same reproducible-build env var convention
    other tooling already looks for), interpreted as an integer Unix
    timestamp; else ``None`` (the callee's own "current instant"
    default). Shared by ``export`` (nit 19) and ``monthly-report``
    (finding 11) -- both subcommands' output is otherwise only
    "identical apart from one wall-clock line" across repeated runs.
    """
    generated_at = args.generated_at
    if generated_at is None:
        source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
        if source_date_epoch:
            try:
                generated_at = (
                    datetime.fromtimestamp(int(source_date_epoch), tz=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            except (ValueError, OverflowError, OSError):
                generated_at = None
    return generated_at


# -- report-like subcommands (report/sessions/recache/ttl/compactions) -----


def _render_patch_set_text(model, args: argparse.Namespace) -> str | None:
    """The recommendation patch-set text for ``--patch-set``, or ``None``
    when the flag wasn't passed or ``recommend`` (an optional dependency,
    see the module docstring) isn't importable. Computed once and reused
    across whichever output modes are active, since where it lands
    differs per mode -- see ``_emit_report_outputs``.
    """
    if not getattr(args, "patch_set", False):
        return None
    if importlib.util.find_spec("claude_token_lens.recommend") is None:
        return None
    from . import recommend  # local import: optional dependency, see module docstring

    return recommend.render_patch_set(model.recommendations)


def _emit_report_outputs(model, args: argparse.Namespace) -> None:
    # Fix cli/patch-set-json: with --json, the patch set used to be
    # printed as trailing text *after* the JSON blob, which made
    # `--json --patch-set` together produce output no `json.loads`
    # could parse. Each output mode now gets the patch set through its
    # own channel: embedded as a JSON string key for --json, a sibling
    # `patch-set.txt` file for --html/--csv-dir (a file has no "after
    # the blob" to corrupt), and unchanged trailing stdout text for the
    # default Markdown mode.
    patch_text = _render_patch_set_text(model, args)

    if getattr(args, "json", False):
        print(render_json(model, patch_set=patch_text))
    else:
        text = render_markdown(model, explain=getattr(args, "explain", False))
        print(text, end="")
        if patch_text:
            print()
            print(patch_text)

    html_path = getattr(args, "html", None)
    if html_path:
        Path(html_path).write_text(render_html(model), encoding="utf-8")
        if patch_text:
            Path(html_path).parent.joinpath("patch-set.txt").write_text(patch_text, encoding="utf-8")

    csv_dir = getattr(args, "csv_dir", None)
    if csv_dir:
        write_csv_dir(model, csv_dir)
        if patch_text:
            Path(csv_dir).joinpath("patch-set.txt").write_text(patch_text, encoding="utf-8")


def _cmd_report_like(args: argparse.Namespace, include: set[str] | None, *, emit=None) -> int:
    command = args.command or DEFAULT_SUBCOMMAND

    config, rates, config_dir, err = _load_config_and_pricing(args)
    if err is not None:
        return err

    root, project_dirs = _resolve_project_dirs_for_args(args, config)
    window = _window_description(args)
    if not project_dirs:
        print(
            f"claude-token-lens {command}: no matching project directories under {root}",
            file=sys.stderr,
        )
        return 1

    corpus = _load_corpus_for_args(args, config, config_dir, project_dirs)
    if not corpus.sessions:
        print(
            f"claude-token-lens {command}: no sessions found under {root} for window {window!r}",
            file=sys.stderr,
        )
        return 1

    snaps = snapshots.load_snapshots(config_dir) or None
    projects = tuple(p.name for p in project_dirs)

    try:
        session_overrides = load_session_overrides(config_dir)
    except ConfigError as exc:
        print(f"claude-token-lens {command}: {exc}", file=sys.stderr)
        return 2

    # S1-exports: when the statusline has been logging ground truth
    # (context_window/cache) into <config_dir>/usage-log.csv, feed those
    # rows into build_report -- statusline.load_usage_log_ground_truth is
    # the tolerant reader that copes with both an old-format file (no
    # cache_* columns yet) and a new one, per its own docstring.
    #
    # Fix for review finding 8: this used to hand build_report every row
    # ever logged, from every project, ignoring this invocation's own
    # --days/--since/--until window and --project selection -- the only
    # table in the report that didn't respect either. Scoped now to
    # exactly the sessions this invocation actually loaded (project
    # selection falls out of that for free: project_dirs already
    # reflects --project/--all-projects/--project-family) and to the
    # resolved --days/--since/--until window via each row's own
    # logged_at (discovery._resolve_window is the same resolution
    # discovery.find_sessions itself uses -- see service/rebuild.py for
    # existing precedent importing this private helper cross-module).
    from .discovery import _resolve_window

    since_dt, until_dt = _resolve_window(args.days, args.since, args.until)
    usage_log_rows = statusline_mod.scoped_usage_log_rows(
        config_dir / "usage-log.csv", {b.session_id for b in corpus.sessions}, since_dt, until_dt
    )

    # v0.3 Task 2: --baseline <id|latest> resolves a saved baseline.py
    # record for build_report's own baseline_comparison section. This is
    # a plain-dict/no-Path lookup (baseline_mod.list_baselines/
    # load_baseline), so it's resolved here rather than inside
    # build_report itself -- this is a separate flag-specific lookup from
    # the v4-wiring-round ``config_dir`` build_report now does accept
    # (see report.py's own module docstring's deviation note): that one
    # is narrowly for waste.WasteStats's own salted session-id hash, not
    # a general "build_report may now load files itself" opening, so
    # --baseline's own file lookup still happens here rather than moving
    # inside build_report. When resolution fails, the section is simply
    # omitted and a note is threaded through as baseline_note instead of
    # erroring -- matches the plan's "when no baseline exists, the
    # section is omitted" wording.
    baseline_record = None
    baseline_note = None
    baseline_arg = getattr(args, "baseline", None)
    if baseline_arg:
        if baseline_arg == "latest":
            saved = baseline_mod.list_baselines(config_dir)
            baseline_record = saved[-1] if saved else None
            if baseline_record is None:
                baseline_note = (
                    "--baseline latest requested but no baseline has been saved yet -- run "
                    "`claude-token-lens baseline` first."
                )
        else:
            baseline_record = baseline_mod.load_baseline(config_dir, baseline_arg)
            if baseline_record is None:
                baseline_note = (
                    f"--baseline {baseline_arg!r} requested but no such baseline was found -- run "
                    "`claude-token-lens baseline --list` to see what's saved."
                )

    try:
        model = build_report(
            corpus,
            rates,
            config,
            projects=projects,
            window=window,
            group_by=args.group_by,
            phases=getattr(args, "phases", False),
            snapshots=snaps,
            include=include,
            session_overrides=session_overrides,
            usage_log_rows=usage_log_rows,
            baseline_record=baseline_record,
            baseline_note=baseline_note,
            config_dir=config_dir,
        )
    except ScorecardError as exc:
        # Fix R20: a misordered [thresholds.scorecard] override in
        # config.toml used to surface as a raw traceback out of
        # scorecard.build_section (called deep inside build_report);
        # give it the same clean one-line-and-exit-2 treatment as every
        # other user-facing config error in this function.
        print(f"claude-token-lens {command}: {exc}", file=sys.stderr)
        return 2
    if emit is not None:
        return emit(model, config_dir, window)
    _emit_report_outputs(model, args)
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    """``review claude-md|skills``: the dashboard's Context files review
    as Markdown. File text and skill descriptions are read now, never
    stored."""
    from . import claude_md_review, skills_review
    from .units import Units

    def emit(model, config_dir, window) -> int:
        units = model.units or Units()
        period = _period_phrase(window)
        context = model.context_files or {}
        if args.what == "skills":
            print(skills_review.render_markdown(skills_review.review(config_dir, context, units, period)))
        else:
            review = claude_md_review.build_review(config_dir, context)
            print(claude_md_review.render_markdown(review, units, period))
        return 0

    return _cmd_report_like(args, {"overview"}, emit=emit)


def _period_phrase(window: str) -> str:
    """"last 30 days" -> "over the last 30 days", for amounts."""
    if window.startswith("since"):
        return window
    return f"over the {window}" if window.startswith("last") else f"over {window}"


def _cmd_check(args: argparse.Namespace) -> int:
    """``check [ID]``: the dashboard's Quick actions as Markdown -- every
    check's one-line answer, or one check in full with its evidence,
    fixes and tips. Nothing is changed; fixes are prompts and dry-run
    commands."""
    from . import quick_actions
    from .units import Units

    def emit(model, config_dir, window) -> int:
        snapshot = next(
            (s for s in reversed(snapshots.load_snapshots(config_dir)) if isinstance(s.data.get("effective"), dict)),
            None,
        )
        agents = snapshot.data.get("effective_agents") if snapshot is not None else None
        ctx = quick_actions.Context(
            model=model,
            units=model.units or Units(),
            period=_period_phrase(window),
            config_dir=Path(config_dir),
            effective=snapshots.effective_config(snapshot) if snapshot is not None else {},
            effective_agents=agents if isinstance(agents, dict) else {},
        )
        if args.id:
            print(quick_actions.render_markdown(quick_actions.run(args.id, ctx)))
            return 0
        marks = {"act": "Act", "ok": "OK", "no_data": "No data"}
        print(f"# Quick actions ({ctx.period})\n")
        for row in quick_actions.run_all(ctx):
            print(f"- **{marks[row['status']]}** `{row['id']}`: {row['question']} {row['summary']}")
        print("\nRun `claude-token-lens check <id>` for the evidence and fixes.")
        return 0

    return _cmd_report_like(args, None, emit=emit)


# -- config-diff -------------------------------------------------------------


def _build_session_metrics(
    corpus: Corpus, rates: Pricing, recache_th, config: Config, session_overrides: dict
) -> list[dict]:
    """Per-session ``{session_id, first_ts, turns, cost, recache_cc,
    cc_total, compactions, span_s}`` dicts for
    :func:`~claude_token_lens.snapshots.build_config_diff_table`.

    ``report.py``'s own "config" section (used when a report is rendered
    with snapshots attached) builds the same shape internally, but only
    exposes it bundled into every changed key up to a fixed cap -- not
    as a reusable function, and not for a caller that wants exactly one
    named key regardless of whether it happens to have changed. This is
    a standalone recomputation from public APIs only (``classify``,
    ``recache.detect``, ``pricing.price_turn``), not a report.py
    private-function reuse.
    """
    metrics: list[dict] = []
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        # Fix R18: this used to hardcode {} here, so a manual
        # sessions.toml mode/purpose override -- honoured by every
        # other subcommand via _cmd_report_like's own
        # load_session_overrides(config_dir) -- was silently ignored
        # for config-diff alone.
        classification = classify.classify_session(bundle.top, bundle.subs, session_overrides, config.tz)
        record = classify.build_session_record(
            bundle.top, bundle.subs, bundle.workflows, classification, bundle.slug
        )

        cost = 0.0
        cc_total = 0
        turns = 0
        recache_cc = 0
        for tr in [bundle.top, *bundle.subs]:
            flagged = recache.detect(tr.turns, recache_th)
            recache_cc += sum(t.cache_creation_tokens for t in flagged)
            for turn in _priced_turns(tr):
                resolved = rates.resolve_model(turn.model)
                cost += price_turn(turn, resolved).total
                cc_total += turn.cache_creation_tokens
                turns += 1

        compactions = sum(1 for e in bundle.top.events if e.kind == EventKind.COMPACT_BOUNDARY)

        metrics.append(
            {
                "session_id": record.session_id,
                "first_ts": record.first_ts,
                "project_key": snapshots.snapshot_project_key(bundle.slug),
                "turns": turns,
                "cost": cost,
                "recache_cc": recache_cc,
                "cc_total": cc_total,
                "compactions": compactions,
                "span_s": record.span_s,
            }
        )
    return metrics


def _cmd_config_diff(args: argparse.Namespace) -> int:
    config, rates, config_dir, err = _load_config_and_pricing(args)
    if err is not None:
        return err

    root, project_dirs = _resolve_project_dirs_for_args(args, config)
    window = _window_description(args)
    if not project_dirs:
        print(
            f"claude-token-lens config-diff: no matching project directories under {root}",
            file=sys.stderr,
        )
        return 1

    corpus = _load_corpus_for_args(args, config, config_dir, project_dirs)
    if not corpus.sessions:
        print(
            f"claude-token-lens config-diff: no sessions found under {root} for window {window!r}",
            file=sys.stderr,
        )
        return 1

    snaps = snapshots.load_snapshots(config_dir)
    if not snaps:
        print(
            "claude-token-lens config-diff: no config snapshots found under "
            f"{config_dir / 'snapshots'}",
            file=sys.stderr,
        )
        return 1

    try:
        session_overrides = load_session_overrides(config_dir)
    except ConfigError as exc:
        print(f"claude-token-lens config-diff: {exc}", file=sys.stderr)
        return 2

    recache_th = recache.RecacheThresholds.from_config(config.thresholds)
    session_metrics = _build_session_metrics(corpus, rates, recache_th, config, session_overrides)

    if args.auto_keys:
        changed_keys = sorted(snapshots.diff_keys(snaps).keys())
        if not changed_keys:
            print("No config keys changed across the available snapshots.")
            return 0
        tables = [
            snapshots.build_config_diff_table(session_metrics, snaps, key) for key in changed_keys
        ]
    else:
        tables = [snapshots.build_config_diff_table(session_metrics, snaps, args.key)]

    helptext.annotate_section(Section(key="config", title="", tables=tables), "subscription" if config.billing == "subscription" else "api")
    for table in tables:
        _print_table(table, rates.currency)
        print()
    return 0


# -- compare / reconcile (V3-compare) ----------------------------------------


def _wrap_section_as_report(
    section, rates: Pricing, config: Config, projects: tuple[str, ...], window: str, coverage_pct: float
) -> ReportModel:
    """Wrap one already-built :class:`~claude_token_lens.model.Section`
    (``compare``'s or ``reconcile``'s) in a minimal
    :class:`~claude_token_lens.model.ReportModel` so it can go out
    through the same Markdown/JSON/CSV/HTML renderers ``report``/
    ``sessions``/``recache``/etc. use via :func:`_emit_report_outputs`,
    rather than each having its own bespoke renderer. ``report.py``'s own
    ``build_report`` is not reusable here (it always assembles the whole
    fixed section list from a live corpus scan; a single already-built
    ``Section`` has nowhere to plug in), so this mirrors its ``meta``
    construction by hand instead.
    """
    meta = ReportMeta(
        tool_version=__version__,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z",
        window=window,
        projects=tuple(sorted({discovery.redact_slug(p) for p in projects})),
        pricing=PricingMeta(
            path=rates.path,
            version=rates.version,
            sha8=rates.sha8,
            currency=rates.currency,
            coverage_pct=coverage_pct,
        ),
        thresholds={},
        billing_mode=config.billing,
        assumptions=[],
    )
    return ReportModel(meta=meta, sections=[section], recommendations=[], diagnostics=Diagnostics())


def _cmd_compare(args: argparse.Namespace) -> int:
    from . import compare as compare_mod

    config, rates, config_dir, err = _load_config_and_pricing(args)
    if err is not None:
        return err

    try:
        arm_a = compare_mod.parse_arm_spec(args.arm_a)
        arm_b = compare_mod.parse_arm_spec(args.arm_b)
    except ValueError as exc:
        print(f"claude-token-lens compare: {exc}", file=sys.stderr)
        return 2

    stratify_by = tuple(s.strip() for s in args.stratify.split(",") if s.strip())
    bad_keys = [k for k in stratify_by if k not in ("purpose", "mode")]
    if bad_keys:
        print(
            f"claude-token-lens compare: bad --stratify key(s) {bad_keys}: expected purpose and/or mode",
            file=sys.stderr,
        )
        return 2
    min_sessions = args.min_sessions if args.min_sessions is not None else config.min_sessions

    root, project_dirs = _resolve_project_dirs_for_args(args, config)
    window = _window_description(args)
    if not project_dirs:
        print(f"claude-token-lens compare: no matching project directories under {root}", file=sys.stderr)
        return 1

    corpus = _load_corpus_for_args(args, config, config_dir, project_dirs)
    if not corpus.sessions:
        print(f"claude-token-lens compare: no sessions found under {root} for window {window!r}", file=sys.stderr)
        return 1

    snaps = snapshots.load_snapshots(config_dir) or None

    try:
        session_overrides = load_session_overrides(config_dir)
    except ConfigError as exc:
        print(f"claude-token-lens compare: {exc}", file=sys.stderr)
        return 2

    # Fewer than min_sessions in either arm is not an error (plan's
    # minimum-sample gate is a caveat on the reading, not a reason to
    # refuse to show data): compare() always returns a full overview with
    # sample_ok="no" on every row instead, and the CLI still exits 0.
    section = compare_mod.compare(
        corpus,
        rates,
        config,
        arm_a=arm_a,
        arm_b=arm_b,
        stratify_by=stratify_by,
        min_sessions=min_sessions,
        snapshots=snaps,
        session_overrides=session_overrides,
        config_dir=config_dir,
    )

    coverage = PricingCoverage()
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        for tr in [bundle.top, *bundle.subs]:
            for turn in _priced_turns(tr):
                coverage.add(turn, price_turn(turn, rates.resolve_model(turn.model)))

    model = _wrap_section_as_report(
        section, rates, config, tuple(p.name for p in project_dirs), window, coverage.coverage_pct
    )
    _emit_report_outputs(model, args)
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    from . import reconcile as reconcile_mod
    from .discovery import _resolve_window

    config, rates, config_dir, err = _load_config_and_pricing(args)
    if err is not None:
        return err

    try:
        admin_result = reconcile_mod.parse_admin_csv(args.admin_csv)
    except reconcile_mod.ReconcileError as exc:
        print(f"claude-token-lens reconcile: {exc}", file=sys.stderr)
        return 2

    by = tuple(args.by.split(","))

    root, project_dirs = _resolve_project_dirs_for_args(args, config)
    window = _window_description(args)
    if not project_dirs:
        print(f"claude-token-lens reconcile: no matching project directories under {root}", file=sys.stderr)
        return 1

    corpus = _load_corpus_for_args(args, config, config_dir, project_dirs)
    if not corpus.sessions:
        print(f"claude-token-lens reconcile: no sessions found under {root} for window {window!r}", file=sys.stderr)
        return 1

    since_dt, until_dt = _resolve_window(args.days, args.since, args.until)
    since = since_dt.astimezone(timezone.utc).date().isoformat() if since_dt else None
    until = until_dt.astimezone(timezone.utc).date().isoformat() if until_dt else None

    section = reconcile_mod.reconcile(
        corpus,
        rates,
        config,
        admin_rows=admin_result.rows,
        unmapped_headers=admin_result.unmapped_headers,
        by=by,
        since=since,
        until=until,
    )

    coverage = PricingCoverage()
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        for tr in [bundle.top, *bundle.subs]:
            for turn in _priced_turns(tr):
                coverage.add(turn, price_turn(turn, rates.resolve_model(turn.model)))

    model = _wrap_section_as_report(
        section, rates, config, tuple(p.name for p in project_dirs), window, coverage.coverage_pct
    )
    _emit_report_outputs(model, args)
    # Exit 0 whenever the admin CSV parsed, regardless of how large the
    # reconciliation deltas turn out to be -- only a parse failure (caught
    # above) is a bad-input exit.
    return 0


# -- log-usage ---------------------------------------------------------------


def _cmd_log_usage(args: argparse.Namespace) -> int:
    config_dir = _resolve_config_dir(args.config_dir)
    try:
        raw = sys.stdin.read()
    except Exception as exc:  # pragma: no cover - stdin failures are rare
        print(f"claude-token-lens log-usage: cannot read stdin: {exc}", file=sys.stderr)
        return 2

    rows = log_usage_mod.parse_usage_json(raw)
    if not rows:
        print(
            "claude-token-lens log-usage: no usage rows found in the given JSON",
            file=sys.stderr,
        )
        return 2

    csv_path = log_usage_mod.default_usage_log_path(config_dir)
    written = log_usage_mod.append_rows(csv_path, rows, source="manual")
    print(f"Logged {written} row(s) to {csv_path}")
    return 0


# -- probe --------------------------------------------------------------------


def _discover_probe_paths(project_dirs: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for project_dir in project_dirs:
        paths.extend(sorted(project_dir.glob("*.jsonl")))
        for session_dir in sorted(p for p in project_dir.iterdir() if p.is_dir()):
            subagents_dir = session_dir / "subagents"
            if subagents_dir.is_dir():
                paths.extend(sorted(subagents_dir.glob("*.jsonl")))
    return paths


def _cmd_probe(args: argparse.Namespace) -> int:
    if args.file:
        target = Path(args.file)
        if not target.exists():
            print(f"claude-token-lens probe: file not found: {args.file}", file=sys.stderr)
            return 2
        paths = [target]
    else:
        config_dir = _resolve_config_dir(args.config_dir)
        try:
            config = load_config(config_dir)
        except ConfigError as exc:
            print(f"claude-token-lens probe: {exc}", file=sys.stderr)
            return 2

        root, project_dirs = _resolve_project_dirs_for_args(args, config)
        if not project_dirs:
            print(
                f"claude-token-lens probe: no matching project directories under {root}",
                file=sys.stderr,
            )
            return 1

        paths = _discover_probe_paths(project_dirs)
        if not paths:
            print(
                f"claude-token-lens probe: no transcript files found under {root}",
                file=sys.stderr,
            )
            return 1

    result = probe_mod.probe_paths(paths)
    print(probe_mod.render_probe(result))
    return 0


# -- statusline / scrub-fixture (thin delegates) -----------------------------


def _cmd_statusline(args: argparse.Namespace) -> int:
    forward = ["--print-install-fragment"] if getattr(args, "print_install_fragment", False) else []
    # --config-dir was accepted by argparse (it's a "common" flag added to
    # every subcommand, including statusline -- see --help) but silently
    # dropped on the floor here: statusline_mod.main() always resolved
    # against log_usage.resolve_config_dir(None), i.e. $CLAUDE_CONFIG_DIR
    # or ~/.claude/token-lens, never the value the caller actually passed.
    # Forward it so statusline honours the same --config-dir contract
    # every other subcommand does (fixed for v0.2 release verification).
    config_dir = getattr(args, "config_dir", None)
    if config_dir:
        forward += ["--config-dir", str(config_dir)]
    return statusline_mod.main(forward)


# -- export / monthly-report (S1-exports) ------------------------------------


def _cmd_export(args: argparse.Namespace) -> int:
    from . import exports as exports_mod

    command = "export"
    config, rates, config_dir, err = _load_config_and_pricing(args)
    if err is not None:
        return err

    root, project_dirs = _resolve_project_dirs_for_args(args, config)
    window = _window_description(args)
    if not project_dirs:
        print(
            f"claude-token-lens {command}: no matching project directories under {root}",
            file=sys.stderr,
        )
        return 1

    corpus = _load_corpus_for_args(args, config, config_dir, project_dirs)
    if not corpus.sessions:
        print(
            f"claude-token-lens {command}: no sessions found under {root} for window {window!r}",
            file=sys.stderr,
        )
        return 1

    if args.aggregate:
        from . import team as team_mod

        if args.aggregate_only is not None or args.hash_slugs is not None:
            print(
                f"claude-token-lens {command}: --aggregate-only/--per-session and "
                "--hash-slugs/--no-hash-slugs have no effect with --aggregate (a "
                "team document is aggregate-only and hashes project slugs by "
                "construction)",
                file=sys.stderr,
            )
        snaps = snapshots.load_snapshots(config_dir) or None
        document = team_mod.build_team_aggregate(
            corpus,
            rates,
            config,
            config_dir,
            window=window,
            projects=tuple(p.name for p in project_dirs),
            include_projects=args.include_projects,
            snapshots=snaps,
            generated_at=_resolve_generated_at(args),
        )
        text = json.dumps(document, indent=2, sort_keys=True) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0

    options = exports_mod.resolve_export_options(args.format, args.aggregate_only, args.hash_slugs)

    # Nit 18: --per-session/--hash-slugs are silently accepted but moot
    # for otel-jsonl (it carries no project/session dimension at all --
    # see exports.py's module docstring), so a caller who explicitly
    # asked for per-session data doesn't silently get an aggregate file.
    if options.fmt == "otel-jsonl" and (args.aggregate_only is not None or args.hash_slugs is not None):
        print(
            f"claude-token-lens {command}: --aggregate-only/--per-session and "
            "--hash-slugs/--no-hash-slugs have no effect on --format otel-jsonl "
            "(it carries no project/session attribute at all)",
            file=sys.stderr,
        )

    # Fix for review finding 2: the hashing opt-out still redacts the
    # OS-username segment (exports._apply_slug_redaction), but the rest
    # of the project slug -- directory shape, a client/codename -- is
    # still visible, so name that residual risk explicitly.
    if not options.hash_slugs and options.fmt != "otel-jsonl":
        print(
            f"claude-token-lens {command}: --no-hash-slugs is in effect -- project "
            "slugs will still have their OS-username segment redacted, but the "
            "rest of the path shape (which can itself name a client or a "
            "project) is exported as-is",
            file=sys.stderr,
        )

    # Nit 19: build_export_text already accepted a generated_at override
    # (for a reproducible --format json), but _cmd_export never passed
    # anything through, so it was unreachable from the CLI. --generated-at
    # wins; else SOURCE_DATE_EPOCH (the same env var reproducible-build
    # tooling already looks for), interpreted as an integer Unix
    # timestamp; else the default (the current instant) -- see
    # _resolve_generated_at, shared with _cmd_monthly_report (finding 11).
    generated_at = _resolve_generated_at(args)

    text = exports_mod.build_export_text(
        corpus, rates, config, config_dir, options, window=window, generated_at=generated_at
    )

    # Fix for review finding 1 (blocking): csv.DictWriter's default
    # dialect already terminates rows with "\r\n" (render_csv_flat builds
    # the text with an io.StringIO opened with newline="\n", so that
    # "\r\n" survives into the returned string literally). Handing that
    # text to a text-mode writer with the platform's own newline
    # translation switched on then rewrites every "\n" to os.linesep a
    # *second* time -- "\r\n" becomes "\r\r\n" on Windows, and Python's
    # own csv.reader then sees a blank record after every real one. Both
    # output paths below write with newline translation switched off
    # ("newline=''", the same convention render/csv_out.py already uses)
    # so the bytes generated are the bytes written, once.
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="")
    else:
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(newline="")
            except Exception:
                pass
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def _cmd_monthly_report(args: argparse.Namespace) -> int:
    from . import monthly as monthly_mod

    command = "monthly-report"
    config, rates, config_dir, err = _load_config_and_pricing(args)
    if err is not None:
        return err

    root, project_dirs = _resolve_project_dirs_for_args(args, config)
    if not project_dirs:
        print(f"claude-token-lens {command}: no matching project directories under {root}", file=sys.stderr)
        return 1

    try:
        # Fix for review finding 12: resolve_month's "previous calendar
        # month" default takes its reference date from config.tz (falling
        # back to the machine's own zone when tz is None/unresolvable).
        month = monthly_mod.resolve_month(args.month, config.tz)
    except ValueError as exc:
        print(f"claude-token-lens {command}: {exc}", file=sys.stderr)
        return 2

    # Shared with serve --monthly-report (service/monthly_job.py):
    # the no-projects/no-sessions checks, the usage log (review finding
    # 9) and the empty-month note (nit 17). A fixed generated_at (review
    # finding 11) makes repeated runs byte-identical -- see
    # _resolve_generated_at (shared with _cmd_export).
    try:
        paths = monthly_mod.run_monthly_report(
            config=config,
            pricing=rates,
            config_dir=config_dir,
            root=root,
            project_dirs=project_dirs,
            month=month,
            out_dir=Path(args.out),
            load_corpus=lambda dirs: _load_corpus_for_args(args, config, config_dir, dirs),
            generated_at=_resolve_generated_at(args),
            note=lambda text: print(f"claude-token-lens {command}: {text} (exit 0)", file=sys.stderr),
        )
    except monthly_mod.MonthlyReportError as exc:
        print(f"claude-token-lens {command}: {exc}", file=sys.stderr)
        return exc.exit_code
    for path in paths:
        print(str(path))
    return 0


def _cmd_scrub_fixture(args: argparse.Namespace) -> int:
    forward: list[str] = []
    if args.verify:
        forward += ["--verify", args.verify]
    else:
        if not args.session_dir or not args.out:
            print(
                "claude-token-lens scrub-fixture: --session-dir and --out are required "
                "unless --verify is given",
                file=sys.stderr,
            )
            return 2
        forward += ["--session-dir", args.session_dir, "--out", args.out]
    if args.key_seed:
        forward += ["--key-seed", args.key_seed]
    return scrub_mod.main(forward)


# -- snapshot-config (WP7, unchanged) ----------------------------------------


def _load_snapshot_hook_module():
    """Dynamically import the packaged ``hooks/snapshot-config.py`` module
    from its source text. That script is standalone stdlib and must never
    import from this package (see its own docstring), so the dependency
    runs the other way: this CLI command loads it, rather than it
    importing anything here.

    Reads the source via ``importlib.resources`` (``Traversable.
    read_text``) and ``exec``s it into a fresh module, rather than
    ``importlib.util.spec_from_file_location`` on the resource path
    directly -- fix (surfaced once ``init``, not just ``snapshot-
    config``/``probe-config``, started calling this at runtime): inside
    a zipapp build (``scripts/build-pyz.py``,
    ``tests/test_service_build_pyz.py``), ``importlib.resources.files``
    returns a ``zipfile.Path``, which satisfies the ``Traversable``
    protocol but not ``os.PathLike`` -- ``spec_from_file_location``
    rejects it outright (``TypeError: expected str, bytes or
    os.PathLike object, not Path``). Reading the text and ``exec``-ing
    it works identically on a normal filesystem install and inside a
    zip.
    """
    hook_resource = importlib.resources.files("claude_token_lens") / "hooks" / "snapshot-config.py"
    try:
        source = hook_resource.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read snapshot-config hook at {hook_resource}: {exc}") from exc
    module = types.ModuleType("_claude_token_lens_snapshot_hook")
    module.__file__ = str(hook_resource)
    code = compile(source, str(hook_resource), "exec")
    exec(code, module.__dict__)
    return module


def _cmd_snapshot_config(args: argparse.Namespace) -> int:
    hook = _load_snapshot_hook_module()
    config_dir = hook.resolve_config_dir(args.config_dir)

    if args.print_hook:
        print(hook.hook_fragment_text(script=config_dir / "hooks" / "snapshot-config.py"))
        return 0

    if args.install_hook:
        dest = hook.install_hook(config_dir)
        print(f"Installed snapshot-config hook to {dest}")
        print(
            "This does not edit settings.json - add the SessionStart fragment "
            "from --print-hook yourself."
        )
        return 0

    cwd = getattr(args, "project_dir", None) or os.getcwd()
    path, written = hook.snapshot_and_get_path(
        config_dir, cwd, min_interval=args.min_interval, managed_path=args.managed_path
    )
    if path is None:
        print("No snapshot written and none exists yet.", file=sys.stderr)
        return 1
    # Fix #13: previously discarded `written`, so running this command
    # twice inside --min-interval printed an old snapshot's path and
    # exited 0 -- indistinguishable from having actually captured.
    if written:
        print(path)
    else:
        print(f"{path} (unchanged, not rewritten)")
    return 0


# -- probe-config (schema 2) --------------------------------------------------


def _render_probe_config_markdown(snapshot: dict) -> str:
    """The layers + effective-config tables as Markdown -- no paths, only
    hashes and the project slug (schema 2's own privacy posture; see
    ``hooks/snapshot-config.py``'s module docstring).
    """
    snap = snapshots.Snapshot(path=Path("-"), ts=str(snapshot.get("ts", "")), data=snapshot)
    project_slug = snapshot.get("project_slug") or "(unknown project)"

    lines: list[str] = [f"# Config probe: {project_slug}", ""]

    lines += ["## Settings layers", "", "| Layer | Present | Content hash |", "| --- | --- | --- |"]
    layer_map = snapshots.layers(snap)
    for layer_name in snapshots.SETTINGS_LAYER_NAMES:
        layer_info = layer_map.get(layer_name) or {}
        present = "yes" if layer_info.get("present") else "no"
        content_hash = layer_info.get("content_hash") or "-"
        lines.append(f"| {layer_name} | {present} | {content_hash} |")
    lines.append("")

    lines += ["## Effective config", "", "| Key | Value | Source layer |", "| --- | --- | --- |"]
    effective = snapshots.effective_config(snap)
    provenance = snapshots.effective_provenance(snap)
    for key in sorted(effective):
        lines.append(f"| {key} | {effective[key]} | {provenance.get(key, '')} |")
    if not effective:
        lines.append("| *(no settings layer defines any allowlisted key)* | | |")
    lines.append("")

    return "\n".join(lines)


def _cmd_probe_config(args: argparse.Namespace) -> int:
    """Run the hook's own scan for a project directory without a session
    and without writing anything (schema 2, plan "Configuration layers"
    section): ``snapshot-config`` captures and persists; this only prints.
    """
    hook = _load_snapshot_hook_module()
    config_dir = hook.resolve_config_dir(args.config_dir)
    project_path = getattr(args, "project_dir", None) or os.getcwd()
    snapshot = hook.build_snapshot({}, project_path, config_dir, managed_path=args.managed_path)
    print(_render_probe_config_markdown(snapshot))
    return 0


# -- pricing-check (WP2, unchanged) ------------------------------------------


def _cmd_pricing_check(args: argparse.Namespace) -> int:
    """``pricing-check``: print the resolved rate card's provenance and
    rate table, and (with ``--models``) how each given model id resolves
    against it. Exit 2 on a malformed or unreadable pricing file.
    """
    try:
        rates = load_pricing(path=args.pricing, config_dir=args.config_dir)
    except PricingError as exc:
        print(f"claude-token-lens pricing-check: {exc}", file=sys.stderr)
        return 2

    print(f"Pricing file: {rates.path}")
    print(f"Version:      {rates.version}")
    print(f"SHA8:         {rates.sha8}")
    print(f"Currency:     {rates.currency}")
    if rates.source_url:
        print(f"Source:       {rates.source_url}")
    if rates.retrieved:
        print(f"Retrieved:    {rates.retrieved}")
    print()

    _print_table(rates.describe(), rates.currency)

    if args.models:
        print()
        print("Model resolution:")
        for model_id in (m.strip() for m in args.models.split(",")):
            if not model_id:
                continue
            resolved = rates.resolve_model(model_id)
            if resolved is None:
                print(f"  {model_id} -> UNKNOWN (no matching rate)")
            else:
                print(
                    f"  {model_id} -> {resolved.canonical_id} (matched via {resolved.matched_via})"
                )

    return 0


# -- init / baseline (v0.3) / serve (v0.2) -----------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    """``init``: detect what's on the machine, ask (or derive, under
    ``--non-interactive``) the "Asked, not guessed" questions, write
    ``config.toml``/``projects/<slug>.toml``, print the install-step
    fragments, and run an initial baseline. The two fragments (the
    SessionStart hook's and the statusLine's) are resolved here, via the
    same dynamic-import/``statusline`` module this CLI already uses for
    ``snapshot-config``/``statusline`` -- ``onboarding.py`` itself never
    imports either, so it stays plain and unit-testable (see its module
    docstring).
    """
    config_dir = _resolve_config_dir(args.config_dir)
    service_roots = _service_projects_roots(args)
    projects_root_path = service_roots[0]

    claude_root = _resolve_claude_root(args.claude_root)
    extra_args = _config_dir_args(config_dir)

    hook = _load_snapshot_hook_module()
    hook_fragment = hook.hook_fragment_text(
        script=Path(config_dir).resolve() / "hooks" / "snapshot-config.py", extra_args=extra_args
    )
    statusline_fragment = statusline_mod.print_install_fragment(extra_args=extra_args)

    try:
        rc = onboarding.run_init(
            config_dir=config_dir,
            projects_root_path=projects_root_path,
            extra_projects_roots=service_roots[1:],
            find_wsl_roots=discovery.find_wsl_projects_roots,
            answers_path=args.answers,
            non_interactive=args.non_interactive,
            no_install=args.no_install,
            repair_hook=args.repair_hook,
            connect_step=not args.no_install and (args.connect or not args.non_interactive),
            hook_fragment=hook_fragment,
            statusline_fragment=statusline_fragment,
            # Resolved here rather than relying on run_init's own
            # sys.stdin/sys.stdout default parameter values: a default
            # argument is bound once, at function-definition time, so it
            # would keep pointing at whatever sys.stdout/sys.stdin were
            # when onboarding.py was first imported -- not whatever a
            # caller (e.g. pytest's capsys, which monkeypatches
            # sys.stdout per test) has made current by the time this
            # actually runs. Referencing sys.stdin/sys.stdout here,
            # inside the function body, re-resolves them fresh on every
            # call, same as every other subcommand's own bare print().
            stdin=sys.stdin,
            stdout=sys.stdout,
            # Fix S6: honour the shared --all-projects/--project/
            # --project-family selection flags for the initial baseline
            # capture, the same fallback-to-cwd-slug rule
            # _resolve_project_dirs_for_args already uses for every
            # report-like subcommand.
            all_projects=args.all_projects,
            project=args.project,
            project_family=args.project_family,
            claude_root=claude_root,
        )
    except onboarding.OnboardingError as exc:
        print(f"claude-token-lens init: {exc}", file=sys.stderr)
        return 2

    if rc != 0:
        return rc

    # v3: init's final step -- offer to register `serve` at logon, kept
    # a separate function (rather than folded into onboarding.run_init's
    # own Q&A) so onboarding.py stays free of installer.py's real
    # subprocess/file-write side effects, the same "onboarding.py never
    # itself installs anything" boundary its module docstring already
    # draws for the hook/statusLine fragments above.
    if not args.no_install and (args.connect or not args.non_interactive):
        _cmd_init_connect_step(args, config_dir=config_dir, hook=hook, claude_root=claude_root)
    return _cmd_init_service_step(args, config_dir=config_dir, projects_root_path=service_roots)


def _cmd_init_connect_step(
    args: argparse.Namespace, *, config_dir: Path, hook, claude_root: Path | None = None, stdin=None, stdout=None
) -> None:
    """``init``'s "Connect to Claude Code" step: install the snapshot
    hook script into this tool's own folder, then show the exact
    ``settings.json`` change that runs it (and adds a statusline when
    you have none) and write it only after a yes, or with ``--connect``;
    ``--dry-run`` shows it and writes nothing. ``settings.json`` is
    backed up first. Commands name a Python and the script by full
    path, so they need neither the ``py`` launcher nor shell variables,
    and carry ``--config-dir`` when this tool's folder is not the
    default (:func:`_config_dir_args`). ``settings.json`` is the one in
    ``claude_root`` (:func:`_resolve_claude_root`), never next to
    ``--config-dir``."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    claude_root = claude_root if claude_root is not None else _resolve_claude_root(None)
    extra_args = _config_dir_args(config_dir)
    script = hook.install_hook(Path(config_dir).resolve())
    plan = hook_health.plan_connect(
        config_dir,
        hook_command=hook.hook_command(script=script, extra_args=extra_args),
        statusline_command=statusline_mod.install_command(extra_args=extra_args),
        claude_root=claude_root,
    )
    stdout.write("Connect to Claude Code\n")
    if plan.new_text is None:
        for line in plan.changes:
            stdout.write(f"- {line}\n")
        if not plan.changes:
            stdout.write("- Already connected: settings.json runs the snapshot hook.\n")
        stdout.write("\n")
        return
    stdout.write(f"This adds to {plan.settings_path}:\n")
    for line in plan.changes:
        stdout.write(f"- {line}\n")
    stdout.write("\n" + plan.diff + "\n")
    if getattr(args, "dry_run", False):
        stdout.write("Dry run: settings.json left unchanged. Run 'claude-token-lens init --connect' to make it.\n\n")
        return
    stdout.write("To undo it later: claude-token-lens uninstall (or restore the backup named below).\n")
    if not args.connect:
        stdout.write("Make this change? settings.json is backed up first. (y/n) [n]: ")
        stdout.flush()
        if (stdin.readline() or "").strip().lower() not in ("y", "yes"):
            stdout.write("Left unchanged. Run 'claude-token-lens init --connect' to make it later.\n\n")
            return
    try:
        backup = hook_health.connect(plan)
    except (OSError, ValueError) as exc:
        stdout.write(f"Could not change settings.json: {exc}\n\n")
        return
    stdout.write("Connected." + (f" The previous settings.json is at {backup}" if backup else "") + "\n\n")


def _cmd_init_service_step(
    args: argparse.Namespace,
    *,
    config_dir: Path,
    projects_root_path: list[Path],
    stdin=None,
    stdout=None,
) -> int:
    """``init``'s final step (v3): "Run the service at logon?" --
    default yes when asked interactively; under ``--non-interactive``
    the derived default is no, *unless* ``--install-service`` was
    given; ``--no-service`` skips the step entirely (no question, no
    install). ``stdin``/``stdout`` default to the live ``sys.stdin``/
    ``sys.stdout`` at call time, same reasoning as ``_cmd_init``'s own
    comment above -- resolved here (not as a bound default argument) so
    a test's ``capsys``/piped-stdin fixture is always the one actually
    read.
    """
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout

    if args.no_service:
        stdout.write("Service-at-logon step skipped (--no-service).\n")
        return 0

    if args.install_service:
        should_install = True
    elif args.non_interactive:
        should_install = False
        stdout.write(
            "(derived) run_service: not given on the command line; used default False "
            "(pass --install-service to install non-interactively)\n"
        )
    else:
        stdout.write("Run the service at logon? (y/n) [y]: ")
        stdout.flush()
        raw = (stdin.readline() or "").strip().lower()
        should_install = raw in ("", "y", "yes")

    if not should_install:
        stdout.write(
            "Service not installed. Run 'claude-token-lens install-service' any time to add it later.\n"
        )
        return 0

    plan = installer_mod.plan_service_install(sys.executable, projects_root_path, config_dir)
    try:
        installer_mod.install(plan, dry_run=args.dry_run)
    except installer_mod.InstallerError as exc:
        print(f"claude-token-lens init: {exc}", file=sys.stderr)
        return 2

    if not args.dry_run:
        _probe_service_after_install(plan.platform)

    return 0


#: Short pause (seconds) before probing a just-installed service, so a
#: platform that starts it immediately on install (systemd's
#: ``enable --now``, launchd's ``bootstrap`` with ``RunAtLoad``) has a
#: moment to actually come up before ``/api/health`` is hit. Windows'
#: Scheduled Task is logon-triggered, not started by registration
#: itself, so this probe is expected to (and does) report "not
#: responding yet" there -- see ``_probe_service_after_install``'s
#: printed message for that case.
_POST_INSTALL_PROBE_DELAY_S = 1.0


def _http_health_ok(url: str) -> bool:
    """Best-effort ``GET <url>/api/health``: ``True`` only on a real
    ``200`` with a JSON ``ok: true`` body, ``False`` for absolutely any
    failure (connection refused, timeout, non-200, malformed body) --
    never raises. A short timeout (this is a one-shot post-install
    courtesy check, not a readiness gate anything blocks on).
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=2) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError):
        return False
    return bool(body.get("ok"))


def _http_health_version(url: str) -> str | None:
    """The ``version`` the dashboard at ``url`` reports in
    ``/api/health``, or ``None`` when it can't be read (an old copy from
    before 0.4.1 reports none). Never raises."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=2) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError):
        return None
    data = body.get("data") if isinstance(body, dict) else None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) else "older than 0.4.1"


def _probe_service_after_install(
    platform: str,
    *,
    bind: str = "127.0.0.1",
    port: int = 8765,
    is_registered_fn=None,
    health_check=None,
    sleep_fn=None,
    version_check=None,
) -> None:
    """Print a short "is it actually working" summary right after
    :func:`installer.install` returns: :func:`installer.is_registered`
    (per-platform probe) and one ``/api/health`` hit. Every dependency
    is an injectable keyword-only parameter (default: the real thing)
    purely so tests never have to monkeypatch ``time.sleep``/spawn a
    real HTTP server/shell out to ``schtasks``/``systemctl``/
    ``launchctl`` to exercise this function.
    """
    import time as time_mod

    is_registered_fn = is_registered_fn or (lambda: installer_mod.is_registered(platform))
    if version_check is None:
        # Only the real health check has a real dashboard to ask.
        version_check = _http_health_version if health_check is None else (lambda url: None)
    health_check = health_check or _http_health_ok
    sleep_fn = sleep_fn or time_mod.sleep

    sleep_fn(_POST_INSTALL_PROBE_DELAY_S)

    registered = is_registered_fn()
    if registered is True:
        print("claude-token-lens: confirmed -- the service is registered to start at logon.")
    elif registered is False:
        print("claude-token-lens: the service registration could not be confirmed -- check the output above.")
    else:
        print("claude-token-lens: service registration status could not be determined on this platform.")

    url = f"http://{bind}:{port}"
    if health_check(url):
        from . import __version__

        answering = version_check(url)
        if answering is not None and answering != __version__:
            print(
                f"claude-token-lens: {url} is answering with version {answering}, not {__version__}: "
                "an older copy still holds the port. See 'An old dashboard won't go away' in the README "
                "to find and stop it."
            )
        else:
            print(f"claude-token-lens: the service is already responding at {url}")
    else:
        print(
            f"claude-token-lens: not responding yet (the first start reads your whole history, "
            f"which can take a minute). Once it's running, open {url}"
        )


def _cmd_install_service(args: argparse.Namespace) -> int:
    """``install-service`` (v3): register ``serve`` to start at
    logon/boot, for anyone who skipped it during ``init`` (or ran
    ``init`` before this milestone existed).
    """
    config_dir = _resolve_config_dir(args.config_dir)
    projects_root_path = _service_projects_roots(args)

    try:
        plan = installer_mod.plan_service_install(
            sys.executable, projects_root_path, config_dir, port=args.port, bind=args.bind
        )
        installer_mod.install(plan, dry_run=args.dry_run)
    except installer_mod.InstallerError as exc:
        print(f"claude-token-lens install-service: {exc}", file=sys.stderr)
        return 2

    if not args.dry_run:
        _probe_service_after_install(plan.platform, bind=args.bind, port=args.port)

    return 0


#: What ``update`` installs from by default.
UPDATE_SOURCE = "git+https://github.com/PaulMorrisDev/claude-token-lens"

#: Where a ``.pyz`` user downloads the newest copy.
_RELEASES_URL = "https://github.com/PaulMorrisDev/claude-token-lens/releases/latest"


def _cmd_update(args: argparse.Namespace, *, runner=None, is_registered_fn=None) -> int:
    """``update``: the README's two update steps as one command. Installs
    the newest version with pip (``--force-reinstall``, because pip skips
    a copy whose version number hasn't changed), then, when the dashboard
    is registered to start at logon, runs the *new* copy's
    ``install-service``, which stops the old dashboard, starts the new one
    and checks the version that answers on the port."""
    import subprocess

    from . import __version__

    runner = runner or subprocess.run
    is_registered_fn = is_registered_fn or installer_mod.is_registered
    if installer_mod.detect_pyz_path() is not None:
        print(
            "claude-token-lens update: this copy runs from a .pyz file, which pip can't update. "
            f"Download the new claude-token-lens.pyz from {_RELEASES_URL}, put it in place of this one, "
            "then run it with install-service.",
            file=sys.stderr,
        )
        return 2

    config_dir = _resolve_config_dir(args.config_dir)
    install = [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", "--no-deps", args.source]
    restart = [
        sys.executable,
        "-m",
        "claude_token_lens",
        "install-service",
        *(arg for root in _service_projects_roots(args) for arg in ("--projects-root", str(root))),
        "--config-dir",
        str(config_dir),
        "--port",
        str(args.port),
        "--bind",
        args.bind,
    ]
    print(f"claude-token-lens update: this is version {__version__}.")
    print("1. Install the newest version:\n   " + " ".join(install))
    if args.dry_run:
        print("2. Restart the dashboard on it (only if it starts at logon):\n   " + " ".join(restart))
        print("Dry run: nothing installed or restarted.")
        return 0

    if runner(install).returncode != 0:
        print(
            "claude-token-lens update: pip could not install the new version (its message is above). "
            "Nothing else was changed.",
            file=sys.stderr,
        )
        return 1
    probe = runner(
        [sys.executable, "-c", "import claude_token_lens; print(claude_token_lens.__version__)"],
        capture_output=True,
        text=True,
    )
    new_version = (probe.stdout or "").strip() or "unknown"
    print(f"   Installed version {new_version}.")

    if args.no_service:
        print("Left the dashboard alone (--no-service). Restart it with: python -m claude_token_lens install-service")
        return 0
    if is_registered_fn() is False:
        print(
            "The dashboard isn't set to start at logon, so there is nothing to restart. "
            "Start it with 'python -m claude_token_lens serve', or have it start at logon with "
            "'python -m claude_token_lens install-service'."
        )
        return 0
    print("2. Restart the dashboard on the new version:\n   " + " ".join(restart))
    return runner(restart).returncode


def _cmd_uninstall_service(args: argparse.Namespace) -> int:
    """``uninstall-service`` (v3): remove a registration made by
    ``install-service`` or by ``init``'s own logon-service step.
    ``--port``/``--bind`` don't apply here -- removing a registration
    never depends on them (see
    ``installer.InstallPlan.uninstall_commands``/``uninstall_files``).
    """
    config_dir = _resolve_config_dir(args.config_dir)
    projects_root_path = _service_projects_roots(args)

    plan = installer_mod.plan_service_install(sys.executable, projects_root_path, config_dir)
    return installer_mod.uninstall(plan, dry_run=args.dry_run)


def _cmd_changes(args: argparse.Namespace) -> int:
    """``changes``: everything this tool has installed or changed on this
    machine, whether it costs tokens, and the command that undoes it
    (``footprint.inventory``)."""
    from . import footprint

    config_dir = _resolve_config_dir(args.config_dir)
    items = footprint.inventory(
        config_dir,
        service_registered=installer_mod.is_registered(),
        claude_root=_resolve_claude_root(getattr(args, "claude_root", None)),
    )
    print("What claude-token-lens has installed and changed\n")
    for item in items:
        print(f"{item.title}: {item.status}")
        print(f"  Where: {item.where}")
        print(f"  What it does: {item.what_it_does}")
        print(f"  Tokens: {item.token_cost}")
        if item.status in ("installed", "in place"):
            print(f"  To undo: {item.undo}")
        print()
    print("What to expect\n")
    for title, text in footprint.EXPECTATIONS:
        print(f"- {title}. {text}")
    print(f"\nTo remove everything: {footprint.UNINSTALL_COMMAND}")
    return 0


def _ask(question: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    sys.stdout.write(question + " (y/n) [n]: ")
    sys.stdout.flush()
    return (sys.stdin.readline() or "").strip().lower() in ("y", "yes")


def _cmd_uninstall(args: argparse.Namespace) -> int:
    """``uninstall``: take claude-token-lens back out, step by step.

    1. Remove its SessionStart hook and statusline from settings.json
       (the diff is shown, and the file backed up first).
    2. Remove the logon service, if registered.
    3. With ``--revert-changes``: undo every applied change still in
       place, newest first (``apply --revert``; a file edited since is
       skipped and reported, never overwritten).
    4. With ``--delete-data``: delete the data folder, including the
       backups, so it runs last and refuses while applied changes are
       still in place unless they were just reverted.

    The package itself is removed with pip (printed at the end)."""
    from . import footprint

    config_dir = _resolve_config_dir(args.config_dir)
    projects_root_path = _service_projects_roots(args)
    claude_root = _resolve_claude_root(args.claude_root)
    plan = footprint.plan_uninstall(config_dir, claude_root=claude_root)
    dry = args.dry_run
    problems = 0

    print("1. Claude Code settings")
    if plan.new_settings_text is None:
        print("   Nothing to remove: settings.json does not run this tool.\n")
    else:
        for line in plan.settings_changes:
            print(f"   - {line}")
        print("\n" + plan.settings_diff)
        if dry:
            print("   Dry run: settings.json left unchanged.\n")
        elif _ask("   Remove these entries? settings.json is backed up first.", assume_yes=args.yes):
            backup = footprint.remove_settings_entries(plan)
            print(f"   Removed. The previous settings.json is at {backup}\n")
        else:
            print("   Left unchanged.\n")

    print("2. Dashboard at logon")
    registered = installer_mod.is_registered()
    if registered is False:
        print("   Not registered.\n")
    else:
        service_plan = installer_mod.plan_service_install(sys.executable, projects_root_path, config_dir)
        if dry or _ask("   Remove the logon registration?", assume_yes=args.yes):
            problems += installer_mod.uninstall(service_plan, dry_run=dry) != 0
        print()

    print("3. Changes applied to your Claude Code settings and agent files")
    if not plan.applied:
        print("   None in place.\n")
    elif not args.revert_changes:
        print("   Still in place (kept; add --revert-changes to undo them all):")
        for backup in plan.applied:
            print(f"   - {backup.ts} ({backup.profile_id}): claude-token-lens apply --revert {backup.ts}")
        print()
    else:
        from .profiles import apply as apply_mod

        for backup in plan.applied:
            if dry:
                print(f"   Would undo {backup.ts} ({backup.profile_id}).")
                continue
            try:
                result = apply_mod.revert(backup.ts, config_dir=config_dir)
            except apply_mod.ApplyError as exc:
                problems += 1
                print(f"   Could not undo {backup.ts}:")
                for reason in exc.reasons:
                    print(f"     {reason}")
                continue
            print(f"   Undid {backup.ts}: {len(result.restored)} restored, {len(result.deleted)} removed.")
        print()

    print("4. This tool's data folder")
    if plan.data_dir is None:
        print("   Nothing to delete.\n")
    elif not args.delete_data:
        print(f"   Kept: {footprint.home_label(plan.data_dir)} (add --delete-data to delete it).\n")
    else:
        still_applied = [b for b in footprint.plan_uninstall(config_dir, claude_root=claude_root).applied] if not dry else (
            [] if args.revert_changes else plan.applied
        )
        if still_applied:
            problems += 1
            print(
                "   Not deleted: it holds the backups for changes still in place, and deleting them would leave "
                "you no way to undo those changes. Undo them first (--revert-changes).\n"
            )
        elif dry:
            print(f"   Would delete {footprint.home_label(plan.data_dir)}.\n")
        elif _ask(f"   Delete {footprint.home_label(plan.data_dir)}? This cannot be undone.", assume_yes=args.yes):
            failures = footprint.delete_data(plan.data_dir)
            if failures:
                problems += 1
                print("   Some files could not be deleted (stop a running dashboard first):")
                for failure in failures:
                    print(f"     {failure}")
            else:
                print("   Deleted.")
            print()

    print("Finally, remove the program itself with: pip uninstall claude-token-lens")
    return 1 if problems else 0


def _cmd_baseline(args: argparse.Namespace) -> int:
    """``baseline``: capture a new baseline (default), or ``--list``/
    ``--show ID`` an already-saved one. Shares its project/window
    resolution with every report-like subcommand
    (``_resolve_project_dirs_for_args``/``_load_config_and_pricing``).
    """
    config_dir = _resolve_config_dir(args.config_dir)

    if args.list_baselines:
        records = baseline_mod.list_baselines(config_dir)
        if not records:
            print(f"claude-token-lens baseline: no baselines saved yet under {config_dir}", file=sys.stderr)
            return 1
        for record in records:
            flag = " (provisional)" if record.get("provisional") else ""
            print(f"{record['id']}  {record.get('created_at', '')}  sessions={record.get('sessions_analysed', 0)}{flag}")
        return 0

    if args.show:
        record = baseline_mod.load_baseline(config_dir, args.show)
        if record is None:
            print(f"claude-token-lens baseline: no baseline {args.show!r} under {config_dir}", file=sys.stderr)
            return 1
        print(baseline_mod.render_onboarding_report(record))
        return 0

    config, rates, config_dir, err = _load_config_and_pricing(args)
    if err is not None:
        return err

    root, project_dirs = _resolve_project_dirs_for_args(args, config)
    if not project_dirs:
        print(f"claude-token-lens baseline: no matching project directories under {root}", file=sys.stderr)
        return 1

    record, _model = baseline_mod.build_baseline(
        config=config,
        pricing=rates,
        config_dir=config_dir,
        project_dirs=project_dirs,
        days=args.days,
        finalise=args.finalise,
    )
    report_markdown = baseline_mod.render_onboarding_report(record)
    path = baseline_mod.save_baseline(config_dir, record, report_markdown)
    print(f"Wrote {path}")
    print(report_markdown)

    status = baseline_mod.capture_status(config)
    print(baseline_mod.format_capture_status(status))
    return 0


# -- team aggregate import / team-report (v0.3 Task 1) -----------------------


def _cmd_import(args: argparse.Namespace) -> int:
    """``import FILE...``: validate every file first (schema/length
    allowlist -- :func:`team.validate_team_document`), then copy each
    into ``<config_dir>/team/<machine_id>-<generated_at>.json``. Exits 2
    on the first invalid file, naming it and the reason, per the plan's
    own "schema-checked ... exit 2 with the reason" contract; nothing is
    written for *any* file once one has failed, so a bad batch never
    partially imports.
    """
    from . import team as team_mod

    config_dir = _resolve_config_dir(args.config_dir)

    documents: list[tuple[str, dict]] = []
    for file_arg in args.files:
        path = Path(file_arg)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"claude-token-lens import: cannot read {file_arg}: {exc}", file=sys.stderr)
            return 2
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"claude-token-lens import: {file_arg} is not valid JSON: {exc}", file=sys.stderr)
            return 2
        reason = team_mod.validate_team_document(doc)
        if reason is not None:
            print(f"claude-token-lens import: {file_arg} rejected: {reason}", file=sys.stderr)
            return 2
        documents.append((file_arg, doc))

    for file_arg, doc in documents:
        try:
            saved_path = team_mod.save_team_document(config_dir, doc)
        except (OSError, ValueError) as exc:
            print(f"claude-token-lens import: cannot write {file_arg}: {exc}", file=sys.stderr)
            return 2
        print(f"Imported {file_arg} -> {saved_path}")
    return 0


def _cmd_team_report(args: argparse.Namespace) -> int:
    """``team-report``: the cross-machine comparison built from every
    document already imported into ``<config_dir>/team/`` (see
    ``import``). Reads no project/session data of its own at all --
    only the already-aggregated, already-privacy-checked documents on
    disk.
    """
    from . import team as team_mod

    config_dir = _resolve_config_dir(args.config_dir)
    documents = team_mod.load_latest_team_documents(config_dir)
    if not documents:
        print(
            f"claude-token-lens team-report: no team documents under {config_dir / 'team'} -- "
            "run `claude-token-lens import <file>...` first",
            file=sys.stderr,
        )
        return 1

    section = team_mod.build_team_report_section(documents, min_sessions=args.min_sessions)
    machine_ids = sorted({str(doc.get("machine_id", "?")) for doc in documents})

    meta = ReportMeta(
        tool_version=__version__,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z",
        window=f"{len(documents)} machine(s): {', '.join(machine_ids)}",
    )
    model = ReportModel(meta=meta, sections=[section], recommendations=[], diagnostics=Diagnostics())
    _emit_report_outputs(model, args)
    return 0


# -- apply (v0.3 milestone) ---------------------------------------------------


def _load_profile_arg(profile_arg: str):
    """``(Profile, None)`` on success, or ``(None, message)`` on
    failure -- ``profile_arg`` is tried as a catalogue id first
    (:func:`~claude_token_lens.profiles.catalogue.get`), then as a path
    to a profile TOML file (:func:`~claude_token_lens.profiles.schema.load_profile`).
    """
    from .profiles import catalogue as catalogue_mod
    from .profiles.schema import ProfileError, load_profile

    catalogue_profile = catalogue_mod.get(profile_arg)
    if catalogue_profile is not None:
        return catalogue_profile, None

    path = Path(profile_arg)
    if not path.is_file():
        return None, f"no such catalogue profile or profile file: {profile_arg}"
    try:
        return load_profile(path), None
    except ProfileError as exc:
        return None, str(exc)


#: Profile id recorded in the backup manifest for an ``apply --set``.
ONE_OFF_PROFILE_ID = "one-off"


def _parse_set_value(raw: str, spec) -> object:
    """``--set``'s VALUE as the allowlisted key's type: true/false for a
    bool, a number for an int, a comma-separated list for a list (empty
    means an empty list), NAME:VALUE pairs for a map, else the text
    itself."""
    if spec is None:
        return raw
    if spec.kind == "bool":
        lowered = raw.strip().lower()
        if lowered in ("true", "yes", "1", "on"):
            return True
        if lowered in ("false", "no", "0", "off"):
            return False
        return raw
    if spec.kind == "int":
        try:
            return int(raw)
        except ValueError:
            return raw
    if spec.kind == "list[str]":
        return [item.strip() for item in raw.split(",") if item.strip()]
    if spec.kind.startswith("map["):
        # NAME:VALUE pairs, comma-separated: skillOverrides=pdf:off,xlsx:name-only
        out: dict = {}
        for pair in raw.split(","):
            name, sep, value = pair.strip().rpartition(":")
            if not sep or not name:
                return raw
            value = value.strip()
            if spec.kind == "map[str,bool]":
                lowered = value.lower()
                if lowered not in ("true", "false"):
                    return raw
                out[name.strip()] = lowered == "true"
            else:
                out[name.strip()] = value
        return out
    return raw


def _one_off_profile(set_values: list[str], agent: str | None):
    """An ad-hoc profile holding just the ``--set`` changes, validated
    through the same allowlist and range checks as a real profile.
    Returns ``(profile, None)`` or ``(None, error text)``."""
    from .profiles import schema

    allowlist = schema.AGENT_ALLOWLIST if agent else schema.SETTINGS_ALLOWLIST
    values: dict = {}
    for item in set_values:
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or not key:
            return None, f"--set {item!r}: expected KEY=VALUE"
        values[key] = _parse_set_value(raw, allowlist.get(key))
    doc: dict = {"id": ONE_OFF_PROFILE_ID, "name": "One-off change"}
    if agent:
        doc["agents"] = {agent: values}
    else:
        doc["settings"] = values
    try:
        return schema.load_dict(doc), None
    except schema.ProfileError as exc:
        return None, str(exc)


def _cmd_apply(args: argparse.Namespace) -> int:
    """``apply``: apply a profile's allowlisted settings/agent/env
    levers to a project or the current user (plan Milestone v0.3's
    ``apply`` bullet -- see ``profiles/apply.py``'s module docstring for
    the full resolution/backup/revert contract this delegates to).

    Exit codes: 0 success (``--list-backups``, a successful ``--revert``,
    or a ``--dry-run`` whose plan is not blocked), 1 a refused *real*
    apply (a git-tracked target without ``--allow-tracked``, a missing
    agent file without ``--force``, or an existing file this command
    cannot parse), 2 bad input (an unrecognised profile, a scope/
    ``--project`` mismatch, an unknown ``--revert`` timestamp) -- fix
    S1: also a ``--dry-run`` whose plan *would* be refused, so the dry
    run a user runs specifically to find out whether an apply will work
    doesn't print a clean diff and exit 0 for one that wouldn't.
    """
    from .profiles import apply as apply_mod

    command = "apply"
    config_dir = _resolve_config_dir(args.config_dir)
    claude_root = _resolve_claude_root(args.claude_root)

    if args.list_backups:
        backups = apply_mod.list_backups(config_dir)
        if not backups:
            print("No backups found.")
            return 0
        for backup in backups:
            print(f"{backup.ts}  profile={backup.profile_id}  scope={backup.scope}  files={backup.file_count}")
        return 0

    if args.revert:
        try:
            result = apply_mod.revert(
                args.revert, config_dir=config_dir, ignore_changes=getattr(args, "ignore_changes", False)
            )
        except apply_mod.ApplyError as exc:
            print(f"claude-token-lens {command}: {exc}", file=sys.stderr)
            return 2
        print(
            f"Reverted {args.revert}: restored {len(result.restored)} file(s), "
            f"removed {len(result.deleted)} file(s)."
        )
        return 0

    if args.set_values and args.profile:
        print(f"claude-token-lens {command}: give a profile or --set, not both", file=sys.stderr)
        return 2
    if args.agent and not args.set_values:
        print(f"claude-token-lens {command}: --agent only goes with --set", file=sys.stderr)
        return 2
    if not args.profile and not args.set_values:
        print(
            f"claude-token-lens {command}: a profile id or path is required "
            "(or use --set/--revert/--list-backups)",
            file=sys.stderr,
        )
        return 2

    if args.set_values:
        profile, err = _one_off_profile(args.set_values, args.agent)
    else:
        profile, err = _load_profile_arg(args.profile)
    if err is not None:
        print(f"claude-token-lens {command}: {err}", file=sys.stderr)
        return 2

    project_path = Path(args.project_dir) if args.project_dir else None
    scope = args.scope or ("project-local" if project_path else "user")
    if scope != "user" and project_path is None:
        print(f"claude-token-lens {command}: --scope {scope} requires --project-dir", file=sys.stderr)
        return 2

    snaps = snapshots.load_snapshots(config_dir)
    latest_snapshot = snaps[-1] if snaps else None

    if args.launch:
        managed = set(snapshots.managed_keys(latest_snapshot)) if latest_snapshot else set()
        path = apply_mod.write_launch_overlay(profile, config_dir=config_dir, managed_keys=managed)
        print(f"Wrote {path}")
        print(f"claude --settings {path}")
        return 0

    try:
        plan = apply_mod.plan_apply(
            profile,
            scope=scope,
            project_path=project_path,
            config_dir=config_dir,
            claude_root=claude_root,
            snapshot=latest_snapshot,
            allow_tracked=args.allow_tracked,
            force=args.force,
            mark_active=not args.set_values,
        )
    except ValueError as exc:
        print(f"claude-token-lens {command}: {exc}", file=sys.stderr)
        return 2
    except apply_mod.ApplyError as exc:
        print(f"claude-token-lens {command}: {exc}", file=sys.stderr)
        return 1

    explanation = apply_mod.explain_plan(plan)
    if args.dry_run:
        from .profiles.diff import apply_command

        for line in explanation:
            print(line)
        if explanation:
            print()
        print(plan.diff_text if plan.diff_text else "No changes to apply.")
        if plan.skipped_managed:
            for key in plan.skipped_managed:
                print(f"# {key}: managed by policy, raise with your administrator")
        if plan.env_lines:
            print("Environment variables (set these yourself; never written to any file):")
            for line in plan.env_lines:
                print(f"  export {line}")
        if plan.blocked:
            # Fix S1: a real apply of this plan would refuse -- say so
            # here too, rather than printing a clean diff and exiting 0
            # as if the apply would succeed.
            for reason in plan.blocked:
                print(f"claude-token-lens {command}: would be refused: {reason}", file=sys.stderr)
            return 2
        if args.set_values:
            print("To make this change, run the same command without --dry-run.")
        else:
            suggested = apply_command(
                plan.profile_id, scope, str(project_path) if project_path else None
            )
            print(suggested)
        return 0

    if plan.blocked:
        for reason in plan.blocked:
            print(f"claude-token-lens {command}: refused: {reason}", file=sys.stderr)
        return 1

    for line in explanation:
        print(line)
    result = apply_mod.execute(plan, config_dir=config_dir)
    print(f"Applied {plan.profile_id} ({scope}).")
    for path in result.written:
        print(f"  wrote {path}")
    if plan.env_lines:
        print("Environment variables (set these yourself; never written to any file):")
        for line in plan.env_lines:
            print(f"  export {line}")
    print(f"To revert: claude-token-lens apply --revert {result.ts}")
    return 0


def _cmd_serve_purge(config_dir: Path, *, confirmed: bool) -> int:
    """``serve --purge`` (S1-integration fix 2.e): delete
    ``<config-dir>/service.db`` and its WAL/SHM sidecars. The store is
    always a derived cache (never source of truth -- see
    ``service/store.py``'s module docstring), so this is safe: the next
    ``serve`` run simply rebuilds it from the transcripts on disk.
    Always prints exactly what it would delete; only actually deletes
    when ``confirmed`` (``--yes``) is set.
    """
    from .service.serve import STORE_FILENAME

    db_path = config_dir / STORE_FILENAME
    candidates = [db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")]
    existing = [p for p in candidates if p.exists()]

    if not existing:
        print(f"claude-token-lens serve --purge: nothing to delete ({db_path} does not exist)")
        return 0

    print("claude-token-lens serve --purge: will delete:")
    for path in existing:
        print(f"  {path}")

    if not confirmed:
        print("Re-run with --yes to actually delete these files.", file=sys.stderr)
        return 2

    # Review finding 15: don't let one un-removable sidecar (e.g. a WAL
    # file still open in another process, or a permissions problem) abort
    # the whole purge with an unhandled OSError -- delete what can be
    # deleted and report the rest, the same "always tell you exactly what
    # happened" posture as the rest of this command.
    deleted = 0
    failures: list[str] = []
    for path in existing:
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            failures.append(f"{path}: {exc}")

    if failures:
        print(f"Deleted {deleted} file(s); {len(failures)} failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(f"Deleted {deleted} file(s).")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """Build ``ServeOptions`` from argv and hand off to
    ``service.serve.run`` (S1-api). Both ``service.serve`` and
    ``service.contracts`` are imported here rather than at module load
    time only to keep this module's own top-level import list free of
    the ``service`` package for readers who never touch ``serve`` --
    neither import risks the sibling S1-watcher package: ``serve.py``
    itself only reaches into ``service.watcher``/``service.rebuild``
    lazily, inside the functions that need them (see its module
    docstring).

    ``--billing-mode`` (S1-integration fix 1.a) defaults to
    ``config.toml``'s own ``billing`` setting (itself defaulting to
    ``"api"``) when not given on the command line, so a subscription
    user only has to say so once, in one place. ``--retention-days``
    falls back to ``config.toml``'s ``retention_days`` the same way.
    """
    config_dir = _resolve_config_dir(args.config_dir)

    if args.purge:
        return _cmd_serve_purge(config_dir, confirmed=args.yes)

    from .service.contracts import ServeOptions
    from .service.serve import run as run_serve

    try:
        config = load_config(config_dir)
    except ConfigError as exc:
        print(f"claude-token-lens serve: {exc}", file=sys.stderr)
        return 2
    projects_root, *extra_projects_roots = discovery.projects_roots(args.projects_root, config.extra_projects_roots)
    billing_mode = args.billing_mode if args.billing_mode is not None else config.billing
    retention_days = args.retention_days if args.retention_days is not None else config.retention_days
    monthly_report_dir = Path(args.monthly_report_dir) if args.monthly_report_dir else None
    options = ServeOptions(
        projects_root=projects_root,
        extra_projects_roots=tuple(extra_projects_roots),
        config_dir=config_dir,
        port=args.port,
        bind=args.bind,
        poll_interval_s=args.poll_interval,
        retention_days=retention_days,
        exclude_projects=tuple(args.exclude_project or ()),
        billing_mode=billing_mode,
        monthly_report_dir=monthly_report_dir,
        allowed_hosts=tuple(args.allowed_host or ()),
    )
    try:
        return run_serve(options, once=args.once, allow_remote=args.allow_remote)
    except KeyboardInterrupt:
        return 0


def _insert_default_subcommand(argv: list[str]) -> list[str]:
    """If the first token isn't a known subcommand (and isn't a top-level
    flag argparse must see first, like --version), insert the default
    subcommand ahead of it.
    """
    if not argv:
        return [DEFAULT_SUBCOMMAND]
    first = argv[0]
    if first in SUBCOMMANDS or first in _LEADING_PASSTHROUGH:
        return argv
    return [DEFAULT_SUBCOMMAND, *argv]


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    raw_argv = list(sys.argv[1:]) if argv is None else list(argv)
    raw_argv = _insert_default_subcommand(raw_argv)

    parser = _make_parser()
    args = parser.parse_args(raw_argv)  # may raise SystemExit (--version, --help, errors)

    command = args.command or DEFAULT_SUBCOMMAND

    if command == "pricing-check":
        return _cmd_pricing_check(args)
    if command == "snapshot-config":
        return _cmd_snapshot_config(args)
    if command == "probe-config":
        return _cmd_probe_config(args)
    if command == "report":
        return _cmd_report_like(args, include=None)
    if command in _REPORT_LIKE_SECTIONS:
        return _cmd_report_like(args, include={"overview", _REPORT_LIKE_SECTIONS[command]})
    if command == "config-diff":
        return _cmd_config_diff(args)
    if command == "log-usage":
        return _cmd_log_usage(args)
    if command == "probe":
        return _cmd_probe(args)
    if command == "statusline":
        return _cmd_statusline(args)
    if command == "export":
        return _cmd_export(args)
    if command == "monthly-report":
        return _cmd_monthly_report(args)
    if command == "compare":
        return _cmd_compare(args)
    if command == "reconcile":
        return _cmd_reconcile(args)
    if command == "scrub-fixture":
        return _cmd_scrub_fixture(args)
    if command == "apply":
        return _cmd_apply(args)
    if command == "init":
        return _cmd_init(args)
    if command == "baseline":
        return _cmd_baseline(args)
    if command == "serve":
        return _cmd_serve(args)
    if command == "install-service":
        return _cmd_install_service(args)
    if command == "uninstall-service":
        return _cmd_uninstall_service(args)
    if command == "update":
        return _cmd_update(args)
    if command == "import":
        return _cmd_import(args)
    if command == "team-report":
        return _cmd_team_report(args)
    if command == "changes":
        return _cmd_changes(args)
    if command == "review":
        return _cmd_review(args)
    if command == "check":
        return _cmd_check(args)
    if command == "uninstall":
        return _cmd_uninstall(args)

    print(f"claude-token-lens {command}: not implemented", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
