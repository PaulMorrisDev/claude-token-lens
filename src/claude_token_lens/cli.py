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

from . import __version__, baseline as baseline_mod, classify, discovery, onboarding
from . import probe as probe_mod, recache, snapshots
from . import statusline as statusline_mod
from .cache import DigestCache
from .config import Config, ConfigError, load_config, load_session_overrides
from .corpus import Corpus, load_corpus
from .parse import load_or_create_salt
from .model import Diagnostics, EventKind, PricingMeta, ReportMeta, ReportModel, TranscriptResult
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
    common.add_argument("--projects-root")
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
    ``sessions``/``recache``/``ttl``/``compactions`` focused views).
    """
    sub.add_argument(
        "--json", action="store_true", help="print the whole report as JSON instead of Markdown"
    )
    sub.add_argument(
        "--html", metavar="PATH", help="also write a single-file HTML report to PATH"
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
        help="prune sessions older than N days on every poll tick (default: keep forever)",
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
        "<config-dir>/config.toml, else 'api')",
    )
    sub.add_argument(
        "--monthly-report",
        default=None,
        metavar="DIR",
        dest="monthly_report_dir",
        help="directory a monthly report is written into (default: none)",
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


def _add_init_args(sub: argparse.ArgumentParser) -> None:
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
        "derived default, printed as 'derived: ...' so nothing is guessed silently",
    )
    sub.add_argument(
        "--no-install",
        action="store_true",
        help="skip printing the SessionStart hook / statusLine install fragments",
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
            "snapshot-config": "capture and diff Claude Code config",
            "report": "full report (default)",
            "sessions": "sessions-only report view",
            "recache": "RE-CACHE-only report view",
            "ttl": "TTL break-even-only report view",
            "limits": "usage-limits-only report view",
            "compactions": "compactions-only report view",
            "config-diff": "compare sessions grouped by a config key's value",
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


def _priced_turns(result: TranscriptResult):
    """Turns that actually got a ``turn_index`` (excludes synthetic and
    missing-usage turns). Deliberately duplicated rather than imported
    from ``report.py`` -- the same one-line-helper convention
    ``workflows.py``/``phases.py``/``report.py`` itself document.
    """
    return [t for t in result.turns if t.turn_index > 0]


def _resolve_project_dirs_for_args(args: argparse.Namespace, config: Config) -> tuple[Path, list[Path]]:
    root = Path(args.projects_root) if args.projects_root else discovery.projects_root()
    slugs = list(args.project) if args.project else None
    if not args.all_projects and not args.project_family and not slugs:
        slugs = [discovery.slug_for(os.getcwd())]
    project_dirs = discovery.resolve_project_dirs(
        root,
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


def _usage_log_row_in_window(row: dict, since_dt, until_dt) -> bool:
    """``True`` when a usage-log ground-truth row's own ``logged_at``
    falls inside ``[since_dt, until_dt]`` (either bound ``None`` means
    unbounded on that side) -- part of the fix for review finding 8, see
    :func:`_cmd_report_like`. A row with no parseable ``logged_at`` is
    kept only when there is no window filter active at all (nothing to
    exclude it for), matching this project's usual "unparseable ->
    excluded only when it would otherwise matter" posture.
    """
    if since_dt is None and until_dt is None:
        return True
    logged_at_raw = row.get("logged_at")
    if not logged_at_raw:
        return False
    try:
        logged_at = datetime.fromisoformat(str(logged_at_raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if since_dt is not None and logged_at < since_dt:
        return False
    if until_dt is not None and logged_at > until_dt:
        return False
    return True


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
        text = render_markdown(model)
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


def _cmd_report_like(args: argparse.Namespace, include: set[str] | None) -> int:
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
    usage_log_csv_path = config_dir / "usage-log.csv"
    usage_log_rows = None
    if usage_log_csv_path.exists():
        from .discovery import _resolve_window

        raw_usage_log_rows = statusline_mod.load_usage_log_ground_truth(usage_log_csv_path)
        known_session_ids = {b.session_id for b in corpus.sessions}
        since_dt, until_dt = _resolve_window(args.days, args.since, args.until)
        usage_log_rows = [
            row
            for row in raw_usage_log_rows
            if row.get("session_id") in known_session_ids
            and _usage_log_row_in_window(row, since_dt, until_dt)
        ]

    # v0.3 Task 2: --baseline <id|latest> resolves a saved baseline.py
    # record for build_report's own baseline_comparison section. This is
    # a plain-dict/no-Path lookup (baseline_mod.list_baselines/
    # load_baseline), so it's resolved here rather than inside
    # build_report itself -- report.py must not gain a config_dir/file-IO
    # dependency just for this one flag (matches the module's own
    # documented "no config_dir parameter" deviation for session
    # overrides above). When resolution fails, the section is simply
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
        )
    except ScorecardError as exc:
        # Fix R20: a misordered [thresholds.scorecard] override in
        # config.toml used to surface as a raw traceback out of
        # scorecard.build_section (called deep inside build_report);
        # give it the same clean one-line-and-exit-2 treatment as every
        # other user-facing config error in this function.
        print(f"claude-token-lens {command}: {exc}", file=sys.stderr)
        return 2
    _emit_report_outputs(model, args)
    return 0


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
        print(
            f"claude-token-lens {command}: no matching project directories under {root}",
            file=sys.stderr,
        )
        return 1

    try:
        # Fix for review finding 12: resolve_month's "previous calendar
        # month" default now takes its reference date from config.tz
        # (falling back to the machine's own zone when tz is None/
        # unresolvable, unchanged from before) rather than unconditionally
        # from the machine's own zone -- see resolve_month's docstring.
        month = monthly_mod.resolve_month(args.month, config.tz)
    except ValueError as exc:
        print(f"claude-token-lens {command}: {exc}", file=sys.stderr)
        return 2

    # The monthly report always covers exactly the calendar month itself
    # (never --days/--since/--until, which are for the other report-like
    # subcommands): load the whole corpus for the project(s) and let
    # monthly.write_monthly_report do its own month-window filtering
    # against each turn's local timestamp, the same way build_report's
    # own --days/--since/--until filtering happens at discovery.load_corpus
    # time rather than post-hoc -- here there is no discovery-level
    # equivalent for "one specific calendar month", so the filtering
    # happens inside monthly.py itself instead.
    corpus = _load_corpus_for_args(args, config, config_dir, project_dirs)
    if not corpus.sessions:
        print(
            f"claude-token-lens {command}: no sessions found under {root}",
            file=sys.stderr,
        )
        return 1

    # Fix for review finding 9: docs/exports.md promises cache_ground_truth
    # in the monthly report's usage section "when a usage log is
    # available", but write_monthly_report never received usage_log_rows
    # at all -- load it the same tolerant way _cmd_report_like does (no
    # --days/--since/--until/--project scoping needed here beyond what
    # write_monthly_report's own month-session filter already applies).
    usage_log_csv_path = config_dir / "usage-log.csv"
    usage_log_rows = (
        statusline_mod.load_usage_log_ground_truth(usage_log_csv_path) if usage_log_csv_path.exists() else None
    )

    # Nit 17: an empty target month exits 0 and still writes files with
    # zeroed tables (a reasonable choice for a scheduled job -- it should
    # not fail a cron run just because nothing happened that month), but
    # that was undocumented and asymmetric with the *corpus*-empty case
    # just above, which exits 1 and writes nothing. Behaviour is
    # unchanged; this just names it on stderr instead of failing silent.
    if not monthly_mod.filter_corpus_to_month(corpus, month, config.tz).sessions:
        print(
            f"claude-token-lens {command}: no sessions found for {month} -- "
            "writing a report with zeroed tables (exit 0)",
            file=sys.stderr,
        )

    # Fix for review finding 11: a fixed generated_at makes repeated runs
    # genuinely byte-identical rather than "identical apart from one
    # line" -- see _resolve_generated_at (shared with _cmd_export).
    generated_at = _resolve_generated_at(args)

    out_dir = Path(args.out)
    paths = monthly_mod.write_monthly_report(
        corpus, rates, config, month, out_dir, usage_log_rows=usage_log_rows, generated_at=generated_at
    )
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
        print(hook.hook_fragment_text())
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
    projects_root_path = Path(args.projects_root) if args.projects_root else discovery.projects_root()

    hook = _load_snapshot_hook_module()
    hook_fragment = hook.hook_fragment_text()
    statusline_fragment = statusline_mod.print_install_fragment()

    try:
        return onboarding.run_init(
            config_dir=config_dir,
            projects_root_path=projects_root_path,
            answers_path=args.answers,
            non_interactive=args.non_interactive,
            no_install=args.no_install,
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
        )
    except onboarding.OnboardingError as exc:
        print(f"claude-token-lens init: {exc}", file=sys.stderr)
        return 2


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
        saved_path = team_mod.save_team_document(config_dir, doc)
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


def _cmd_apply(args: argparse.Namespace) -> int:
    """``apply``: apply a profile's allowlisted settings/agent/env
    levers to a project or the current user (plan Milestone v0.3's
    ``apply`` bullet -- see ``profiles/apply.py``'s module docstring for
    the full resolution/backup/revert contract this delegates to).

    Exit codes: 0 success (including ``--dry-run``/``--list-backups``/a
    successful ``--revert``), 1 a refused operation (a git-tracked
    target without ``--allow-tracked``, a missing agent file without
    ``--force``, or an existing file this command cannot parse), 2 bad
    input (an unrecognised profile, a scope/``--project`` mismatch, or
    an unknown ``--revert`` timestamp).
    """
    from .profiles import apply as apply_mod

    command = "apply"
    config_dir = _resolve_config_dir(args.config_dir)
    home = config_dir.parent

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
            result = apply_mod.revert(args.revert, config_dir=config_dir)
        except apply_mod.ApplyError as exc:
            print(f"claude-token-lens {command}: {exc}", file=sys.stderr)
            return 2
        print(
            f"Reverted {args.revert}: restored {len(result.restored)} file(s), "
            f"removed {len(result.deleted)} file(s)."
        )
        return 0

    if not args.profile:
        print(
            f"claude-token-lens {command}: a profile id or path is required "
            "(or use --revert/--list-backups)",
            file=sys.stderr,
        )
        return 2

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
            home=home,
            snapshot=latest_snapshot,
            allow_tracked=args.allow_tracked,
            force=args.force,
        )
    except ValueError as exc:
        print(f"claude-token-lens {command}: {exc}", file=sys.stderr)
        return 2
    except apply_mod.ApplyError as exc:
        print(f"claude-token-lens {command}: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        from .profiles.diff import apply_command

        print(plan.diff_text if plan.diff_text else "No changes to apply.")
        if plan.skipped_managed:
            for key in plan.skipped_managed:
                print(f"# {key}: managed by policy, raise with your administrator")
        if plan.env_lines:
            print("Environment variables (set these yourself; never written to any file):")
            for line in plan.env_lines:
                print(f"  export {line}")
        suggested = apply_command(
            plan.profile_id, scope, str(project_path) if project_path else None
        )
        print(suggested)
        return 0

    if plan.blocked:
        for reason in plan.blocked:
            print(f"claude-token-lens {command}: refused: {reason}", file=sys.stderr)
        return 1

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
    user only has to say so once, in one place.
    """
    config_dir = _resolve_config_dir(args.config_dir)

    if args.purge:
        return _cmd_serve_purge(config_dir, confirmed=args.yes)

    from .service.contracts import ServeOptions
    from .service.serve import run as run_serve

    projects_root = Path(args.projects_root) if args.projects_root else discovery.projects_root()
    if args.billing_mode is not None:
        billing_mode = args.billing_mode
    else:
        try:
            billing_mode = load_config(config_dir).billing
        except ConfigError as exc:
            print(f"claude-token-lens serve: {exc}", file=sys.stderr)
            return 2
    monthly_report_dir = Path(args.monthly_report_dir) if args.monthly_report_dir else None
    options = ServeOptions(
        projects_root=projects_root,
        config_dir=config_dir,
        port=args.port,
        bind=args.bind,
        poll_interval_s=args.poll_interval,
        retention_days=args.retention_days,
        exclude_projects=tuple(args.exclude_project or ()),
        billing_mode=billing_mode,
        monthly_report_dir=monthly_report_dir,
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
    if command == "import":
        return _cmd_import(args)
    if command == "team-report":
        return _cmd_team_report(args)

    print(f"claude-token-lens {command}: not implemented", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
