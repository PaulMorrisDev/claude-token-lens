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
import os
import sys
from pathlib import Path

from . import __version__, classify, discovery, probe as probe_mod, recache, snapshots
from . import statusline as statusline_mod
from .cache import DigestCache
from .config import Config, ConfigError, load_config, load_session_overrides
from .corpus import Corpus, load_corpus
from .model import EventKind, TranscriptResult
from .pricing import Pricing, PricingError, load_pricing, price_turn
from .render.csv_out import write_csv_dir
from .render.html import render_html
from .render.json_out import render_json
from .render.markdown import render_markdown
from .render.tables import format_cell
from .report import build_report
from .tools import log_usage as log_usage_mod
from .tools import scrub as scrub_mod

#: Every subcommand in the CLI surface, in the order they are
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

#: Subcommands whose output is a filtered :func:`report.build_report`
#: section (plus "overview"), keyed by the section key they add. "report"
#: itself passes ``include=None`` (the whole report), so isn't listed
#: here.
_REPORT_LIKE_SECTIONS: dict[str, str] = {
    "sessions": "sessions",
    "recache": "recache",
    "ttl": "ttl",
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
    sub.add_argument(
        "--allow-titles",
        action="store_true",
        help="include customTitle/ai-title text (off by default for privacy)",
    )
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
    sub.add_argument(
        "--managed-path",
        metavar="PATH",
        default=None,
        help="override the platform managed-settings.json path (fix 7; "
        "default is the platform's own policy-file location)",
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
            "compactions": "compactions-only report view",
            "config-diff": "compare sessions grouped by a config key's value",
            "log-usage": "append a pasted get_usage JSON payload to the usage log",
            "probe": "content-free schema histogram of a project or file",
            "statusline": "Claude Code statusLine handler (reads stdin JSON)",
            "scrub-fixture": "scrub a real session into a privacy-safe test fixture",
            "init": "planned for v0.3",
            "baseline": "planned for v0.3",
            "serve": "planned for v0.2",
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
    return parser


# -- shared config-dir / project / corpus plumbing --------------------------


def _resolve_config_dir(cli_arg: str | Path | None) -> Path:
    """``--config-dir`` wins; else ``$CLAUDE_CONFIG_DIR/token-lens``; else
    ``~/.claude/token-lens``. Mirrors ``config.py``'s own
    ``_default_config_dir``/``_resolve_config_dir`` -- each module in this
    package keeps its own copy of this small lookup rather than sharing
    one (see e.g. ``tools/log_usage.py``'s module docstring), and the CLI
    is no exception.
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
    )
    if args.verbose:
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


# -- report-like subcommands (report/sessions/recache/ttl/compactions) -----


def _emit_report_outputs(model, args: argparse.Namespace) -> None:
    if getattr(args, "json", False):
        print(render_json(model))
    else:
        text = render_markdown(model)
        print(text, end="")

    html_path = getattr(args, "html", None)
    if html_path:
        Path(html_path).write_text(render_html(model), encoding="utf-8")

    csv_dir = getattr(args, "csv_dir", None)
    if csv_dir:
        write_csv_dir(model, csv_dir)

    if getattr(args, "patch_set", False):
        if importlib.util.find_spec("claude_token_lens.recommend") is not None:
            from . import recommend  # local import: optional dependency, see module docstring

            patch_text = recommend.render_patch_set(model.recommendations)
            if patch_text:
                print()
                print(patch_text)


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

    snaps = snapshots.load_snapshots(config_dir.parent) or None
    projects = tuple(p.name for p in project_dirs)

    try:
        session_overrides = load_session_overrides(config_dir)
    except ConfigError as exc:
        print(f"claude-token-lens {command}: {exc}", file=sys.stderr)
        return 2

    model = build_report(
        corpus,
        rates,
        config,
        projects=projects,
        window=window,
        group_by=args.group_by,
        phases=getattr(args, "phases", False),
        snapshots=snaps,
        allow_titles=getattr(args, "allow_titles", False),
        include=include,
        session_overrides=session_overrides,
    )
    _emit_report_outputs(model, args)
    return 0


# -- config-diff -------------------------------------------------------------


def _build_session_metrics(corpus: Corpus, rates: Pricing, recache_th, config: Config) -> list[dict]:
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
        classification = classify.classify_session(bundle.top, bundle.subs, {}, config.tz)
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

    snaps = snapshots.load_snapshots(config_dir.parent)
    if not snaps:
        print(
            "claude-token-lens config-diff: no config snapshots found under "
            f"{config_dir.parent / 'token-lens' / 'snapshots'}",
            file=sys.stderr,
        )
        return 1

    recache_th = recache.RecacheThresholds.from_config(config.thresholds)
    session_metrics = _build_session_metrics(corpus, rates, recache_th, config)

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
    return statusline_mod.main(forward)


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

    path, _written = hook.snapshot_and_get_path(
        config_dir, os.getcwd(), managed_path=args.managed_path
    )
    if path is None:
        print("No snapshot written and none exists yet.", file=sys.stderr)
        return 1
    print(path)
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


# -- init / baseline / serve (v0.2/v0.3 stubs) -------------------------------


def _cmd_planned_stub(command: str, milestone: str) -> int:
    print(f"claude-token-lens {command}: planned for {milestone}", file=sys.stderr)
    return 2


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
    if command == "scrub-fixture":
        return _cmd_scrub_fixture(args)
    if command in ("init", "baseline"):
        return _cmd_planned_stub(command, "v0.3")
    if command == "serve":
        return _cmd_planned_stub(command, "v0.2")

    print(f"claude-token-lens {command}: not implemented", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
