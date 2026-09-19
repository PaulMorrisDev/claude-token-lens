"""Benchmark harness for the S1-perf work package (not a pytest file --
its name doesn't match ``test_*.py``/``*_test.py`` and it defines no
``test_*`` functions, so ``pytest`` never collects it).

Measures one ``claude-token-lens serve`` watcher's first ("cold") and
second ("warm, nothing changed") tick against a real corpus, without
ever touching a real ``~/.claude`` installation -- always pass a
throwaway ``--config-dir``, never the default one ``config.py`` would
resolve.

Usage::

    python tests/bench_watcher.py \\
        --projects-root ~/.claude/projects \\
        --config-dir /tmp/perf/cfg

``--config-dir`` is reset before tick 1 on every run (``service.db`` +
its ``-wal``/``-shm`` sidecars deleted, ``cache/`` removed unless
``--keep-cache`` is given) so "tick 1" always means a genuinely cold
store and cache, matching how a brand-new install would see its very
first ``serve`` run -- the scenario S1-perf's target numbers (first
tick under 60s, store under 80MB) are about.

``--use-cache`` mirrors whatever ``service/serve.py`` itself does at
the point in the work package this script is run from: before the
S1-perf ``serve.py`` change (item 2) lands, ``serve.run()`` constructs
``FileWatcher(store, options)`` with no cache at all, so the baseline
measurement (S1-perf step 1) is taken *without* ``--use-cache`` to
match that exactly; after that change, real ``serve.py`` always passes
a ``DigestCache(config_dir)``, so every later step's measurement passes
``--use-cache`` too. This script does not try to detect which state
``serve.py`` is in -- get it wrong and the reported numbers simply
don't describe what production does, not a crash.

Every timing is wall-clock (``time.monotonic``), one process, one run:
this script does not average across repeated runs -- for a corpus this
large a single cold run already takes long enough that the caller
controls how many times to repeat it.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from claude_token_lens.cache import DigestCache  # noqa: E402
from claude_token_lens.service.contracts import ServeOptions, WatcherStats  # noqa: E402
from claude_token_lens.service.serve import STORE_FILENAME  # noqa: E402
from claude_token_lens.service.store import Store  # noqa: E402
from claude_token_lens.service.watcher import FileWatcher  # noqa: E402

#: WatcherStats fields added by S1-perf item 5 -- printed as "n/a" when
#: benchmarking a pre-S1-perf watcher build so this script works across
#: the whole work package rather than only after that step lands.
_PHASE_FIELDS = ("discovery_s", "parse_s", "store_s")


def _reset_config_dir(config_dir: Path, *, keep_cache: bool) -> None:
    db_path = config_dir / STORE_FILENAME
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}")
        if candidate.exists():
            candidate.unlink()
    if not keep_cache:
        cache_dir = config_dir / "cache"
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
    config_dir.mkdir(parents=True, exist_ok=True)


def _checkpoint_and_size(db_path: Path) -> int:
    """WAL-checkpoint ``db_path`` (folding the ``-wal`` file back into
    the main file and truncating it) before measuring its size, so the
    reported number reflects steady state rather than however much of
    the last tick's writes happen to still be sitting uncommitted-to-
    the-main-file in ``-wal`` at the moment this script happens to look.
    """
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return db_path.stat().st_size


def _table_counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(db_path))
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in sorted(tables)}
    finally:
        conn.close()


def _print_stats(label: str, stats: WatcherStats) -> None:
    print(
        f"[{label}] duration_s={stats.duration_s:.3f} files_scanned={stats.files_scanned} "
        f"files_parsed={stats.files_parsed} files_skipped_live={stats.files_skipped_live} "
        f"sessions_upserted={stats.sessions_upserted} errors={stats.errors}"
    )
    for name in _PHASE_FIELDS:
        value = getattr(stats, name, None)
        if isinstance(value, float):
            print(f"    {name} = {value:.3f}")
        else:
            print(f"    {name} = n/a (pre-S1-perf watcher build)")
    if stats.error_messages:
        print(f"    error_messages = {stats.error_messages!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--projects-root", required=True, type=Path)
    parser.add_argument("--config-dir", required=True, type=Path)
    parser.add_argument(
        "--keep-cache",
        action="store_true",
        help="don't wipe <config-dir>/cache before tick 1 (default: wiped, for a genuinely cold first tick)",
    )
    parser.add_argument(
        "--use-cache",
        action="store_true",
        help="construct the watcher with a DigestCache, mirroring service/serve.py after S1-perf item 2 "
        "(the pre-item-2 baseline never passes one -- see module docstring)",
    )
    args = parser.parse_args(argv)

    projects_root = args.projects_root.expanduser().resolve()
    config_dir = args.config_dir.expanduser().resolve()
    if not projects_root.is_dir():
        print(f"error: --projects-root {projects_root} is not a directory", file=sys.stderr)
        return 2

    print(f"projects_root = {projects_root}")
    print(f"config_dir    = {config_dir}")
    print(f"use_cache     = {args.use_cache}")

    _reset_config_dir(config_dir, keep_cache=args.keep_cache)

    options = ServeOptions(projects_root=projects_root, config_dir=config_dir)
    db_path = config_dir / STORE_FILENAME
    store = Store(db_path)
    store.open()

    cache = DigestCache(config_dir) if args.use_cache else None
    watcher = FileWatcher(store, options, cache=cache)

    stats1 = watcher.run_once()
    _print_stats("tick 1 (cold)", stats1)

    stats2 = watcher.run_once()
    _print_stats("tick 2 (warm, nothing changed)", stats2)

    store.close()

    db_size = _checkpoint_and_size(db_path)
    print(f"[store] service.db size = {db_size / 1_000_000:.1f} MB ({db_size} bytes)")

    print("[store] per-table row counts:")
    for table, count in _table_counts(db_path).items():
        print(f"    {table} = {count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
