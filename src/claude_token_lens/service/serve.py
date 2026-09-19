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
- ``contracts.Watcher`` exposes no getter for the *latest* background-
  thread tick's :class:`~.contracts.WatcherStats` (only ``run_once``,
  ``start``, ``stop``). ``/api/health`` still needs *some* answer once
  the watcher is polling on its own thread, so :func:`run` passes
  ``make_handler`` a ``watcher_stats`` callable that prefers a
  ``watcher.last_stats`` attribute when the concrete ``FileWatcher``
  happens to expose one (a common pattern for this kind of poll loop),
  and otherwise falls back to the stats captured from the synchronous
  first tick this function always runs before serving. This is an
  integration assumption for the S1-watcher package to confirm/adjust,
  not a requirement ``contracts.Watcher`` enforces.
"""

from __future__ import annotations

import sys
import threading
from http.server import ThreadingHTTPServer

from .contracts import ServeOptions, WatcherStats
from .store import Store

#: See module docstring's first contract note.
_STORE_FILENAME = "service.db"

_LOOPBACK_ADDRESSES = {"127.0.0.1", "::1", "localhost"}


def _is_loopback(bind: str) -> bool:
    return bind in _LOOPBACK_ADDRESSES


def run(options: ServeOptions, *, once: bool = False, allow_remote: bool = False) -> int:
    """Run the v0.2 service until ``SIGINT``/``KeyboardInterrupt``
    (or, with ``once=True``, run a single watcher tick and return).
    Returns the process exit code (``0`` on a clean stop, ``2`` if
    binding a non-loopback ``options.bind`` was refused).
    """
    options.config_dir.mkdir(parents=True, exist_ok=True)
    store = Store(options.config_dir / _STORE_FILENAME)
    store.open()

    # Local import: service.watcher is a sibling work package's module
    # (S1-watcher) -- see api.py's module docstring for the identical
    # constraint applied to service.rebuild. Importing at call time
    # (rather than at this module's top level) means this module still
    # imports cleanly in a checkout where watcher.py hasn't landed yet.
    from .watcher import FileWatcher

    watcher = FileWatcher(store, options)

    stats_lock = threading.Lock()
    first_tick_stats: WatcherStats = WatcherStats()

    def _current_stats() -> WatcherStats:
        live = getattr(watcher, "last_stats", None)
        if isinstance(live, WatcherStats):
            return live
        with stats_lock:
            return first_tick_stats

    with stats_lock:
        first_tick_stats = watcher.run_once()

    if once:
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

    watcher.start()
    try:
        handler_cls = make_handler(store, options, watcher_stats=_current_stats)
        server = ThreadingHTTPServer((options.bind, options.port), handler_cls)
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


__all__ = ["run"]
