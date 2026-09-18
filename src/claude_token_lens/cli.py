"""Command-line interface skeleton for claude-token-lens.

WP0 ships the argparse surface and subcommand stubs only; each work
package named in the project plan replaces its stub with a real
implementation. Nothing here reads a transcript yet.
"""

from __future__ import annotations

import argparse
import importlib.resources
import importlib.util
import os
import sys

from . import __version__
from .pricing import PricingError, load_pricing
from .render.tables import format_cell

#: Every subcommand in the WP0 CLI surface, in the order they are
#: registered. "report" is also the default when no subcommand is given.
SUBCOMMANDS: tuple[str, ...] = (
    "report",
    "sessions",
    "recache",
    "ttl",
    "compactions",
    "config-diff",
    "snapshot-config",
    "log-usage",
    "pricing-check",
    "scrub-fixture",
    "probe",
    "statusline",
    "init",
    "baseline",
    "serve",
)

DEFAULT_SUBCOMMAND = "report"

# Tokens that must never trigger default-subcommand insertion because
# argparse needs to see them as the very first token.
_LEADING_PASSTHROUGH = ("-h", "--help", "--version")


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
        "--group-by",
        choices=("mode", "purpose", "agent", "project", "model", "profile"),
    )

    cache = common.add_mutually_exclusive_group()
    cache.add_argument("--no-cache", action="store_true")
    cache.add_argument("--rebuild-cache", action="store_true")

    verbosity = common.add_mutually_exclusive_group()
    verbosity.add_argument("--quiet", action="store_true")
    verbosity.add_argument("--verbose", action="store_true")

    return common


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
        help="copy hooks/snapshot-config.py into <config-dir>/token-lens/hooks/",
    )


def _make_parser() -> argparse.ArgumentParser:
    common = _build_common_parser()
    parser = argparse.ArgumentParser(prog="claude-token-lens")
    parser.add_argument(
        "--version", action="version", version=f"claude-token-lens {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command")
    for name in SUBCOMMANDS:
        help_text = (
            "capture and diff Claude Code config (WP7)"
            if name == "snapshot-config"
            else f"{name} (not implemented yet)"
        )
        sub = subparsers.add_parser(name, parents=[common], help=help_text)
        if name == "pricing-check":
            sub.add_argument(
                "--models",
                metavar="ID,ID,...",
                help="comma-separated model ids to resolve and report",
            )
        if name == "snapshot-config":
            _add_snapshot_config_args(sub)
    return parser


def _load_snapshot_hook_module():
    """Dynamically import the packaged ``hooks/snapshot-config.py`` module
    by path. That script is standalone stdlib and must never import from
    this package (see its own docstring), so the dependency runs the other
    way: this CLI command loads it, rather than it importing anything here.
    """
    hook_path = (
        importlib.resources.files("claude_token_lens")
        / "hooks"
        / "snapshot-config.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_claude_token_lens_snapshot_hook", hook_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot locate snapshot-config hook at {hook_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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

    path, _written = hook.snapshot_and_get_path(config_dir, os.getcwd())
    if path is None:
        print("No snapshot written and none exists yet.", file=sys.stderr)
        return 1
    print(path)
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

    table = rates.describe()
    headers = [column.label for column in table.columns]
    formatted_rows = [
        [
            format_cell(value, column.kind, currency=rates.currency)
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

    print(f"claude-token-lens {command}: not implemented", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
