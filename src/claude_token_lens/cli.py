"""Command-line interface skeleton for claude-token-lens.

WP0 ships the argparse surface and subcommand stubs only; each work
package named in the project plan replaces its stub with a real
implementation. Nothing here reads a transcript yet.
"""

from __future__ import annotations

import argparse
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


def _make_parser() -> argparse.ArgumentParser:
    common = _build_common_parser()
    parser = argparse.ArgumentParser(prog="claude-token-lens")
    parser.add_argument(
        "--version", action="version", version=f"claude-token-lens {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command")
    for name in SUBCOMMANDS:
        sub = subparsers.add_parser(
            name, parents=[common], help=f"{name} (not implemented yet)"
        )
        if name == "pricing-check":
            sub.add_argument(
                "--models",
                metavar="ID,ID,...",
                help="comma-separated model ids to resolve and report",
            )
    return parser


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

    print(f"claude-token-lens {command}: not implemented", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
