"""Frozen v0.2 contracts for the watcher thread and the JSON API, so
``watcher.py``, ``api.py`` and their tests can be built in parallel
against this one file rather than against each other.

Three dataclasses describe data that crosses the watcher/API/CLI
boundary:

- :class:`ServeOptions` — everything ``claude-token-lens serve`` parses
  from its CLI flags, passed to both the watcher and the API factory.
- :class:`WatcherStats` — what one watcher poll tick did, for the
  ``/api/health``/diagnostics surface and for tests.
- :class:`ApiError` — the ``{"ok": false, "error": {...}}`` envelope
  shape every failed ``/api/*`` response uses (see ``docs/api.md``).

Two ``Protocol``s describe behaviour without importing an implementation:

- :class:`Watcher` — the polling loop ``watcher.py`` implements.
- :class:`ApiHandler` — the callable shape one ``/api/*`` route
  implements; :data:`MakeHandler` is the factory-function shape
  ``api.py``'s own ``make_handler`` follows to bind a set of routes to
  one :class:`~claude_token_lens.service.store.Store` and
  :class:`ServeOptions` pair as an ``http.server.BaseHTTPRequestHandler``
  subclass.

Nothing here imports ``store.py``, ``watcher.py`` or ``api.py`` at
runtime (only under ``TYPE_CHECKING``, for the ``Store`` type hints)
precisely so this module has no implementation to keep in sync — it is
the thing implementations are checked against, not the other way round.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

if typing.TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler

    from .store import Store


@dataclass(slots=True)
class ServeOptions:
    """Everything ``claude-token-lens serve --projects-root ... --config-dir
    ... --port ... --bind ...`` resolves from its CLI flags (plan
    Milestone v0.2's first bullet). Passed to both the watcher and the
    API handler factory so the two never have to agree on flag parsing
    independently.
    """

    projects_root: Path
    config_dir: Path
    port: int = 8765
    #: Localhost by default (plan: "port bound to localhost only").
    #: ``serve`` may accept a different bind for an explicit opt-in
    #: (e.g. a container's own loopback), never a wildcard by default.
    bind: str = "127.0.0.1"
    #: Watcher poll interval in seconds (plan: "interval 30 s").
    poll_interval_s: float = 30.0
    #: ``None`` means "keep forever"; otherwise ``Store.retention_prune``
    #: is run once per poll tick.
    retention_days: int | None = None
    #: Directories under ``projects_root`` to never scan (plan
    #: "Enterprise use": confidential repositories).
    exclude_projects: tuple[str, ...] = ()
    #: ``"api"`` (pay-per-token) or ``"subscription"`` (flat-rate plan) --
    #: stamped onto every ``sessions.billing_mode`` row the watcher
    #: upserts (S1-integration fix 1.a). ``cli.py``'s ``serve`` subcommand
    #: defaults this from ``<config_dir>/config.toml``'s own
    #: ``Config.billing`` when no explicit ``--billing-mode`` flag is
    #: given, falling back to this field's own default otherwise.
    billing_mode: str = "api"
    #: Directory a future monthly-report job should write its rendered
    #: reports to under a ``"subscription"`` billing mode (plan
    #: "Milestone v0.2"'s billing-mode note) -- ``None`` means no such
    #: job is configured. Not yet consumed by ``serve``/``watcher.py``
    #: themselves; carried here so ``cli.py``'s ``--monthly-report DIR``
    #: flag has somewhere to put the value once a caller needs it.
    monthly_report_dir: Path | None = None


@dataclass(slots=True)
class WatcherStats:
    """What one :meth:`Watcher.run_once` tick did — returned to the
    caller and folded into the ``/api/health`` response so a stuck or
    erroring watcher is externally visible rather than silent.
    """

    files_scanned: int = 0
    files_parsed: int = 0
    files_skipped_live: int = 0
    files_removed: int = 0
    sessions_upserted: int = 0
    errors: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float = 0.0
    #: Short, non-path error summaries (e.g. ``"json decode error"``),
    #: never a traceback or a file path — see ``service/__init__.py``'s
    #: privacy-rule docstring.
    error_messages: tuple[str, ...] = ()


@dataclass(slots=True)
class ApiError:
    """The shape of a failed ``/api/*`` response's ``error`` object
    (plan/``docs/api.md``: every response is either ``{"ok": true,
    "data": ...}`` or ``{"ok": false, "error": {...}}``).
    """

    status: int
    code: str
    message: str

    def to_envelope(self) -> dict:
        """The full JSON-ready ``{"ok": false, "error": {...}}`` body a
        handler writes for this error (``status`` is the HTTP status
        code, sent separately as the response header)."""
        return {"ok": False, "error": {"code": self.code, "message": self.message}}


class Watcher(Protocol):
    """The polling loop a ``watcher.py`` implementation must provide.
    ``serve`` constructs one ``Watcher``, calls :meth:`start` once, and
    :meth:`stop` on shutdown; tests call :meth:`run_once` directly
    without a background thread.
    """

    #: The most recent :meth:`run_once` tick's stats, or ``None`` before
    #: the first tick has ever run. A concrete ``Watcher`` must keep this
    #: up to date so ``/api/health`` (via ``serve.run``'s
    #: ``watcher_stats`` callable) always has a real answer once the
    #: background poll thread is running, without ``serve.py`` having to
    #: guess at an attribute a ``Watcher`` implementation might or might
    #: not happen to expose (S1-integration fix 1.e).
    last_stats: "WatcherStats | None"

    def run_once(self) -> "WatcherStats":
        """Scan ``ServeOptions.projects_root`` once: find new/changed
        transcript files since the last tick (via ``Store.known_files``),
        re-parse each from its last byte offset, fold the result into
        the store (``Store.upsert_transcript``/``upsert_session``),
        remove rows for files no longer present (``Store.remove_missing``),
        and run ``Store.retention_prune`` if configured. Returns stats
        for this one tick. Must never raise for a single bad file —
        record it in ``WatcherStats.error_messages`` and continue.
        """
        ...

    def start(self) -> None:
        """Begin polling every ``ServeOptions.poll_interval_s`` seconds
        on a background thread. Idempotent — calling it twice must not
        start a second thread."""
        ...

    def stop(self) -> None:
        """Stop the background thread and wait for the in-flight tick
        (if any) to finish. Idempotent."""
        ...


class ApiHandler(Protocol):
    """The callable shape one ``/api/*`` route implements: given the
    open :class:`~claude_token_lens.service.store.Store` and the
    request's parsed query-string mapping, return ``(http_status,
    json_ready_body)``. ``json_ready_body`` is always either
    ``{"ok": True, "data": ...}`` or an :class:`ApiError`'s
    :meth:`~ApiError.to_envelope`.

    A ``POST`` route (``/api/sessions/<id>/tags``, ``/api/profiles``)
    additionally receives the parsed JSON request body as ``body``;
    a ``GET`` route ignores it (``body`` is ``None``).
    """

    def __call__(
        self, store: "Store", query: dict[str, str], body: dict | None
    ) -> tuple[int, dict]:
        ...


class MakeHandler(Protocol):
    """The shape of ``api.py``'s own ``make_handler`` factory: given the
    open store and the resolved serve options, build an
    ``http.server.BaseHTTPRequestHandler`` subclass with every
    ``/api/*`` route (each implementing :class:`ApiHandler`) bound to
    them via closure, ready to pass to
    ``http.server.HTTPServer((options.bind, options.port), make_handler(store, options))``.
    """

    def __call__(self, store: "Store", options: "ServeOptions") -> type["BaseHTTPRequestHandler"]:
        ...


__all__ = [
    "ServeOptions",
    "WatcherStats",
    "ApiError",
    "Watcher",
    "ApiHandler",
    "MakeHandler",
]
