"""``claude-token-lens serve``'s runtime entry point: open the SQLite
store, run one watcher tick synchronously so ``/api/*`` has data before
the first request, start the watcher's background poll thread, then
serve the JSON API (``service/api.py``'s ``make_handler``) until
interrupted.

:func:`run` is the whole module's surface -- ``cli.py``'s ``serve``
subcommand calls it directly with the ``ServeOptions`` it builds from
argv.

Contract notes:

- ``contracts.ServeOptions`` has no field for the SQLite file's name --
  only ``config_dir``. This module picks ``<config_dir>/service.db``
  (a sibling of ``config.toml``/``profiles/``/``snapshots/`` already
  living under the same directory per ``config.py``/``snapshots.py``),
  not a frozen contract choice, so a future work package is free to
  change it as long as ``serve`` and any migration path agree.
- ``allow_remote`` is a keyword-only addition beyond ``ServeOptions``'s
  own fields (mirroring ``api.py``'s ``watcher_stats`` addition to
  ``MakeHandler``) -- the plan's "port bound to localhost only" default
  posture needs an explicit opt-in for anything else, and
  ``ServeOptions`` itself carries no such flag.
- ``contracts.Watcher.last_stats`` (S1-integration fix 1.e) is now a
  documented Protocol attribute a concrete ``Watcher`` must keep current,
  so ``/api/health`` always has a real answer once the watcher is polling
  on its own thread: :func:`run` passes ``make_handler`` a
  ``watcher_stats`` callable that simply reads ``watcher.last_stats``.
  ``run`` always calls ``watcher.run_once()`` synchronously before
  serving starts, so by the time any request thread can call
  ``watcher_stats``, ``last_stats`` is always already populated.
"""

from __future__ import annotations

import sys
from http.server import ThreadingHTTPServer

from ..cache import DigestCache
from .contracts import ServeOptions
from .store import Store

#: See module docstring's first contract note. Public (S1-integration
#: fix 2.e) so ``cli.py``'s ``serve --purge`` can name the exact file
#: (and its WAL/SHM sidecars) it is about to delete without duplicating
#: the filename as a second literal.
STORE_FILENAME = "service.db"

_LOOPBACK_ADDRESSES = {"127.0.0.1", "::1", "localhost"}


def _is_loopback(bind: str) -> bool:
    return bind in _LOOPBACK_ADDRESSES


def _format_stats_line(stats) -> str:
    """Render a ``WatcherStats`` as the single line ``serve --once``
    prints on exit -- same fields ``/api/health`` exposes (``docs/api.md``),
    formatted for a terminal/cron log rather than as JSON, matching this
    module's other ``print()`` lines (``"claude-token-lens serve: ..."``).
    """
    line = (
        "claude-token-lens serve --once: "
        f"duration_s={stats.duration_s:.3f} "
        f"(discovery_s={stats.discovery_s:.3f} parse_s={stats.parse_s:.3f} store_s={stats.store_s:.3f}) "
        f"files_scanned={stats.files_scanned} files_parsed={stats.files_parsed} "
        f"files_skipped_live={stats.files_skipped_live} files_removed={stats.files_removed} "
        f"transcripts_missing={stats.transcripts_missing} sessions_upserted={stats.sessions_upserted} "
        f"errors={stats.errors}"
    )
    if stats.error_messages:
        line += f" error_messages={list(stats.error_messages)!r}"
    return line


def run(options: ServeOptions, *, once: bool = False, allow_remote: bool = False) -> int:
    """Run the v0.2 service until ``SIGINT``/``KeyboardInterrupt``
    (or, with ``once=True``, run a single watcher tick and return).
    Returns the process exit code (``0`` on a clean stop, ``2`` if
    binding a non-loopback ``options.bind`` was refused).
    """
    options.config_dir.mkdir(parents=True, exist_ok=True)
    store = Store(options.config_dir / STORE_FILENAME)
    store.open()

    # Local import: service.watcher is a sibling work package's module
    # (S1-watcher) -- see api.py's module docstring for the identical
    # constraint applied to service.rebuild. Importing at call time
    # (rather than at this module's top level) means this module still
    # imports cleanly in a checkout where watcher.py hasn't landed yet.
    from .watcher import FileWatcher

    # S1-perf item 2: share the on-disk digest cache with the CLI's own
    # ``load_corpus``/``--jobs`` path (``<config_dir>/cache``, the same
    # directory ``cache.DigestCache`` always resolves to for a given
    # ``config_dir``) so a transcript parsed by one is a cache hit for
    # the other, and so the watcher's own bulk-prewarm pool
    # (``FileWatcher._prewarm_cache``) has somewhere to persist parsed
    # results across ticks.
    cache = DigestCache(options.config_dir)
    watcher = FileWatcher(store, options, cache=cache)
    stats = watcher.run_once()

    if once:
        # Print the tick's WatcherStats before exiting -- --once is the
        # one-shot/cron/verification entry point, and until this was
        # added it discarded every number docs/api.md documents as a
        # first-class diagnostic (discovery_s/parse_s/store_s, errors,
        # ...), leaving an operator running --once with no way to see
        # what the tick actually did short of reading the store or
        # starting the full server just to hit /api/health once.
        print(_format_stats_line(stats))
        store.close()
        return 0

    if not allow_remote and not _is_loopback(options.bind):
        print(
            f"claude-token-lens serve: refusing to bind non-loopback address "
            f"{options.bind!r} without --allow-remote",
            file=sys.stderr,
        )
        store.close()
        return 2

    # Local import: service.api depends on this work package's own
    # api.py, which in turn lazily imports service.rebuild -- kept as a
    # call-time import here purely for symmetry with the watcher import
    # above, not out of necessity (api.py is this package's own module).
    from .api import make_handler

    # v3: wire the real logon/boot registration probe into /api/health
    # here (not inside api.py itself, per that module's own docstring on
    # why it never imports installer.py) -- installer.is_registered's
    # own default runner is subprocess.run, and api.py's make_handler
    # caches whatever this callable returns for
    # api._SERVICE_REGISTERED_CACHE_TTL_S, so this only actually shells
    # out to schtasks/systemctl/launchctl at most once every 10 minutes
    # of live traffic, not on every request.
    from ..installer import is_registered as _probe_service_registered

    watcher.start()
    try:
        handler_cls = make_handler(
            store,
            options,
            watcher_stats=lambda: watcher.last_stats,
            service_registered=_probe_service_registered,
        )
        server = ThreadingHTTPServer((options.bind, options.port), handler_cls)
        # nit 30: ThreadingHTTPServer defaults daemon_threads to True, so
        # a KeyboardInterrupt/shutdown() can tear the process down mid-
        # request, abandoning a request thread (and its still-open
        # per-thread Store connection, see api.py's Handler._dispatch)
        # without ever running its own cleanup. Non-daemon request
        # threads are joined properly on interpreter/thread-pool
        # teardown instead.
        server.daemon_threads = False
        host = options.bind if ":" not in options.bind else f"[{options.bind}]"
        print(f"claude-token-lens serve: listening on http://{host}:{server.server_port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
    finally:
        watcher.stop()
        store.close()

    return 0


__all__ = ["run", "STORE_FILENAME"]
