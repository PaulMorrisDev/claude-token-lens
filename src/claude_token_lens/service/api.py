"""``http.server`` JSON API (v0.2), built against ``service/contracts.py``'s
``ApiHandler``/``MakeHandler`` shapes and the frozen contract in
``docs/api.md``.

:func:`make_handler` is the one entry point this module exports: given an
open :class:`~claude_token_lens.service.store.Store` and the resolved
:class:`~claude_token_lens.service.contracts.ServeOptions`, it returns an
``http.server.BaseHTTPRequestHandler`` subclass with every ``/api/*``
route (plus the static-file routes ``docs/ui.md`` describes) bound to
them via closure. Every route function has the exact ``(store, query,
body) -> (status, json_ready_body)`` shape ``contracts.ApiHandler``
declares -- report-backed routes additionally close over ``options`` and
a small in-process report cache rather than taking them as parameters,
since ``ApiHandler`` itself only threads ``store``/``query``/``body``
through.

Two kinds of route:

- **Store-backed** (``/api/health``, ``/api/summary``, ``/api/sessions``,
  ``/api/session/<id>``, ``/api/recache``, ``/api/compactions``,
  ``/api/profiles``, ``/api/baseline``, ``/api/daily-usage``, the two
  ``POST`` routes): read straight from ``store``'s own read queries --
  cheap, always fresh.
- **Report-backed** (``/api/ttl``, ``/api/config-diff``,
  ``/api/recommendations``, ``/api/report.{md,html,json}``): rebuild a
  :class:`~claude_token_lens.corpus.Corpus` from the store's own
  ``transcripts.digest_blob`` rows via ``service.rebuild.corpus_from_store``
  (a sibling package's module -- imported lazily, inside the function
  that needs it, per this work package's brief, so this module still
  imports cleanly before that module exists) and run it through the same
  :func:`~claude_token_lens.report.build_report`/
  :func:`~claude_token_lens.recommend.recommend` pipeline the CLI's
  ``report`` subcommand uses, then a renderer. Rebuilt once per
  ``(window_days, since, until, store-change-token)`` key and cached in-process (see
  ``_ReportCache``) so switching UI tabs (``docs/ui.md``) never re-parses
  the whole store for the same window.

Contract notes / deviations (reported here rather than silently, per this
project's convention -- see e.g. ``report.py``'s own module docstring):

- ``GET /api/session/<id>`` returns ``Store.session()``'s dict verbatim,
  which includes ``mode_source``/``purpose_source`` alongside the fields
  ``docs/api.md`` lists for ``/api/sessions``, plus (S1-integration fix
  1.g) ``turn_series``/``markers``/``truncated``, and (v3-limits wiring)
  ``limit_markers``, all from ``Store.turns_for_session``. This is a
  superset, not a contradiction -- ``docs/api.md`` describes it as "the
  session-summary fields above, plus transcripts ... and tags", not an
  exact field count, and dropping fields ``Store`` already computes for
  no privacy reason would only lose information a client might want.
- ``GET /api/profiles/<id>/diff`` renders the real
  ``profiles/diff.py`` computation (v0.3) against the store's own
  *latest* recorded config snapshot (``snapshots.effective_config`` and
  friends) -- not a per-project selection, since config-diff/ttl/
  recommendations are already computed the same window-wide,
  not-per-project way elsewhere in this module. A store with no
  snapshot at all diffs against an empty effective config (nothing
  currently set, nothing managed) and adds a note saying so, rather
  than erroring.
- ``POST /api/profiles`` validates the body via
  ``profiles.schema.load_dict`` (v0.3), writes
  ``<config_dir>/profiles/<id>.toml`` atomically (temp file +
  ``os.replace``, this module's own convention -- see ``cache.py``'s
  ``DigestCache.put``), and re-ingests it into the store immediately
  (rather than waiting for the watcher's next tick) so the response's
  own ``GET /api/profiles`` reflects the write straight away. A
  catalogue id can never be created or overwritten this way -- ``409``
  regardless of ``?replace=1``.
- ``GET /api/report.json``/``.md``/``.html`` are **not** wrapped in the
  ``{"ok": ..., "data": ...}`` envelope on success -- their body is the
  renderer's own native output (``render_json``/``render_markdown``/
  ``render_html``), so ``/api/report.json`` is byte-equivalent to the
  CLI's ``report --json`` for the same window (the parity ``docs/api.md``
  requires). A request error on one of these three routes (a bad
  ``window_days``, or an unexpected exception) still falls back to the
  normal JSON error envelope -- only the success path is raw.
- ``GET /api/config-diff`` reuses the already-assembled report's
  ``"config"`` section (``report.py``'s own ``_build_config_section``,
  capped at 20 changed keys) rather than recomputing
  ``snapshots.build_config_diff_table`` a second time with a
  service-specific session-metrics rebuild -- the CLI's own
  ``config-diff`` subcommand computes session metrics itself only
  because it has no ``build_report`` call to reuse in that code path;
  the service always builds a full report for the same window anyway
  (``/api/ttl``/``/api/recommendations`` need to), so reusing that
  report's own "config" section tables is strictly less duplicated work
  for the identical numbers. A ``key`` naming a config key that did not
  change in this window returns ``{"ok": true, "data": []}`` (an empty,
  valid diff), not an error.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import mimetypes
import os
import re
import tempfile
import threading
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from zoneinfo import ZoneInfo

from .. import __version__ as _TOOL_VERSION
from .. import baseline as baseline_mod
from .. import helptext, hook_health
from .. import snapshots as snapshots_mod
from ..config import ConfigError, load_config, load_session_overrides
from ..pricing import load_pricing
from ..profiles import catalogue as profile_catalogue
from ..profiles import diff as profile_diff_mod
from ..profiles import schema as profile_schema
from ..render.html import render_html
from ..render.json_out import render_json, to_jsonable
from ..render.markdown import render_markdown
from ..report import build_report
from ..snapshots import Snapshot
from .contracts import ApiError, ServeOptions, WatcherState, WatcherStats

if TYPE_CHECKING:
    from .store import Store

#: Every security header ``docs/api.md`` requires on every response,
#: regardless of route or outcome.
_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    (
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'",
    ),
)

#: Default report window (days) for the report-backed routes when
#: ``window_days`` isn't given -- matches ``docs/api.md``'s "Accept
#: window_days (default 30) on these routes".
_DEFAULT_WINDOW_DAYS = 30

#: v3: how long a ``service_registered`` probe result is reused before
#: ``/api/health`` runs the platform's own query command again --
#: registration status essentially never changes between requests, and
#: the probe itself shells out to ``schtasks``/``systemctl``/
#: ``launchctl`` (``installer.is_registered``), so this keeps a busy UI
#: polling ``/api/health`` from spawning that process on every refresh.
_SERVICE_REGISTERED_CACHE_TTL_S = 600.0

#: ``/api/health`` calls the scanner stale once no scan has finished for
#: this long (or ten poll intervals, if longer).
_STALE_AFTER_S = 600.0

#: ... and calls one scan stuck once it has run this long (a first read
#: of a large history takes minutes, not hours).
_STUCK_SCAN_S = 3600.0

#: How many built reports (one per window) the service keeps.
_REPORT_CACHE_SIZE = 8

#: A kept report older than this is rebuilt before it is served, rather
#: than served while a rebuild runs (a tab reopened after a long idle
#: shouldn't show figures from hours ago, even briefly).
_STALE_REPORT_MAX_AGE_S = 600.0

_RESTART_ADVICE = "Restart the dashboard: claude-token-lens install-service, or stop and start serve."


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _clock(ts: str | None) -> str:
    parsed = _parse_utc(ts)
    return parsed.strftime("%H:%M UTC") if parsed is not None else "an unknown time"


def _scan_progress_message(state: WatcherState) -> str:
    if state.phase == "finding" and state.done:
        return (
            f"Scanning your history: found {state.done:,} transcript files so far. "
            "Figures may be incomplete until it finishes."
        )
    if state.phase == "reading" and state.total:
        return (
            f"Scanning your history: read {state.done:,} of {state.total:,} changed files. "
            "Figures may be incomplete until it finishes."
        )
    if state.phase == "storing" and state.total:
        return (
            f"Scanning your history: stored {state.done:,} of {state.total:,} sessions. "
            "Figures may be incomplete until it finishes."
        )
    return "Scanning your history. Figures may be incomplete until it finishes."


def _health_status(
    stats: WatcherStats | None,
    state: WatcherState | None,
    *,
    poll_interval_s: float,
    now: datetime | None = None,
) -> tuple[str, str | None]:
    """``/api/health``'s ``(status, message)``: ``"ok"`` (message
    ``None``); ``"starting"`` while the scanner has yet to finish its
    first scan; ``"degraded"`` when its last scan failed outright;
    ``"stale"`` when it has stopped, or has not finished a scan for a
    long while. With no ``state`` (no watcher wired in) it is always
    ``"ok"``."""
    if state is None:
        return "ok", None
    now = now or datetime.now(timezone.utc)
    failure = None
    if stats is not None:
        failure = next((m for m in reversed(stats.error_messages) if m.startswith("tick failed: ")), None)
    reason = failure[len("tick failed: "):] if failure else "an error"
    every = f"{poll_interval_s:g} s"

    if not state.running:
        if state.last_success_at is None:
            return "stale", f"The background scanner is not running, so nothing has been read yet. {_RESTART_ADVICE}"
        return (
            "stale",
            f"The background scanner has stopped; figures are as of {_clock(state.last_success_at)}. "
            f"{_RESTART_ADVICE}",
        )
    if state.last_success_at is None:
        if state.last_tick_failed and not state.scanning:
            return "degraded", f"The first scan failed ({reason}). Retrying every {every}."
        return "starting", _scan_progress_message(state)
    if state.last_tick_failed:
        return (
            "degraded",
            f"The last scan failed ({reason}); figures are as of {_clock(state.last_success_at)}. "
            f"Retrying every {every}.",
        )
    stale_after = max(_STALE_AFTER_S, 10 * poll_interval_s)
    last_success = _parse_utc(state.last_success_at)
    if state.scanning:
        started = _parse_utc(state.scan_started_at)
        if started is not None and (now - started).total_seconds() > max(_STUCK_SCAN_S, stale_after):
            return (
                "stale",
                f"A scan has been running since {_clock(state.scan_started_at)} without finishing; "
                f"figures are as of {_clock(state.last_success_at)}. {_RESTART_ADVICE}",
            )
        return "ok", None
    if last_success is not None and (now - last_success).total_seconds() > stale_after:
        return (
            "stale",
            f"No scan has finished since {_clock(state.last_success_at)}, so figures may be out of date. "
            f"{_RESTART_ADVICE}",
        )
    return "ok", None

_PLACEHOLDER_INDEX_HTML = (
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>claude-token-lens</title>"
    "</head><body>UI not built yet.</body></html>"
)

_SESSION_ID_RE = re.compile(r"^/api/session/([^/]+)$")
_SESSION_TAGS_RE = re.compile(r"^/api/sessions/([^/]+)/tags$")
_PROFILE_DIFF_RE = re.compile(r"^/api/profiles/([^/]+)/diff$")
_PROFILE_RE = re.compile(r"^/api/profiles/([^/]+)$")
_SESSION_EXPLAIN_RE = re.compile(r"^/api/session/([^/]+)/explain$")
_CLAUDE_MD_RE = re.compile(r"^/api/claude-md/([0-9a-f]{16})$")
_QUICK_ACTION_RE = re.compile(r"^/api/quick-actions/([a-z0-9-]+)$")

#: ``profiles.diff``'s own ``_VALID_SCOPES`` -- duplicated rather than
#: imported (that name is private) so a scope query param can be
#: validated with a clear 400 before ever reaching ``diff.py``/
#: ``apply_command``, which both raise ``ValueError`` on an unknown one.
_VALID_PROFILE_SCOPES = ("user", "project-local", "repo")


# -- envelope helpers ---------------------------------------------------


def _ok(data) -> tuple[int, dict]:
    return 200, {"ok": True, "data": data}


def _error(status: int, code: str, message: str) -> tuple[int, dict]:
    return status, ApiError(status=status, code=code, message=message).to_envelope()


def _not_found(message: str = "not found") -> tuple[int, dict]:
    return _error(404, "not_found", message)


def _bad_request(message: str) -> tuple[int, dict]:
    return _error(400, "bad_request", message)


def _forbidden(message: str) -> tuple[int, dict]:
    return _error(403, "forbidden", message)


def _internal_error(message: str) -> tuple[int, dict]:
    return _error(500, "internal_error", message)


def _not_implemented(message: str) -> tuple[int, dict]:
    return _error(501, "not_implemented", message)


def _int_query(
    query: dict[str, str], key: str, default: int | None, *, minimum: int | None = None
) -> tuple[int | None, tuple[int, dict] | None]:
    """Parse ``query[key]`` as an int, or return ``default`` when absent
    (or an empty string -- an HTML form/query-string convention for
    "unset"). Returns ``(value, None)`` on success, ``(None, error)`` --
    an already-built ``400 bad_request`` response -- otherwise.
    """
    raw = query.get(key)
    if raw is None or raw == "":
        return default, None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, _bad_request(f"{key!r} must be an integer")
    if minimum is not None and value < minimum:
        return None, _bad_request(f"{key!r} must be >= {minimum}")
    return value, None


def _str_query(query: dict[str, str], key: str) -> str | None:
    """``query[key]`` as a string, or ``None`` when absent/empty -- the
    same "empty string means unset" convention ``_int_query`` uses.
    """
    raw = query.get(key)
    return raw if raw else None


def _parse_iso8601(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


#: Short windows the dashboard offers by name (``?window=``), each as the
#: phrase that follows an amount.
WINDOW_NAMES = {
    "1h": "in the last hour",
    "today": "today",
    "24h": "in the last 24 hours",
    "change": "since your last change",
    "all": "over all time",
}


def _named_window_since(name: str, config_dir: Path | None, now: datetime | None = None) -> tuple[str | None, str]:
    """``(since, "")`` for a named window as an ISO timestamp, rounded
    down to the minute so repeat requests share one cached report, or
    ``(None, reason)`` when it can't be worked out."""
    now = now or datetime.now(timezone.utc)
    if name == "1h":
        start = now - timedelta(hours=1)
    elif name == "24h":
        start = now - timedelta(hours=24)
    elif name == "today":
        tz = None
        if config_dir is not None:
            try:
                tz_name = load_config(config_dir).tz
                tz = ZoneInfo(tz_name) if tz_name else None
            except Exception:  # noqa: BLE001 -- a bad tz falls back to the machine's own
                tz = None
        local = now.astimezone(tz) if tz is not None else now.astimezone()
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    elif name == "change":
        from .. import change_points

        point = change_points.latest(config_dir) if config_dir is not None else None
        if point is None:
            return None, (
                "No change recorded yet. This window starts at your latest `apply` (a profile or a "
                "one-off change), its undo, or a settings change the config hook saw."
            )
        start = point.ts
    else:
        return None, f"'window' must be one of {', '.join(WINDOW_NAMES)}"
    return start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z"), ""


def _window_query(
    query: dict[str, str],
    *,
    config_dir: Path | None = None,
) -> tuple[tuple[int | None, str | None, str | None], tuple[int, dict] | None]:
    """Parse the report-backed routes' windowing query params: ``window``
    (a named short window, :data:`WINDOW_NAMES`, resolved to ``since``), or
    ``since``/``until`` (ISO 8601, matching the CLI's own ``report
    --since``/``--until``, ``discovery._resolve_window``'s resolution)
    or ``window_days`` -- never both defaulted at once, mirroring the
    CLI's ``--days``/``--since`` mutually-exclusive argparse group so a
    ``since``/``until`` request isn't silently also clamped to the
    routes' usual 30-day default (docs/api.md's "byte-equivalent to the
    CLI" parity requirement for ``/api/report.*``).

    Returns ``((window_days, since, until), None)`` on success, or
    ``(None, error)`` -- an already-built ``400 bad_request`` response.
    """
    name = _str_query(query, "window")
    if name == "all":
        return (None, None, None), None
    if name is not None:
        since, reason = _named_window_since(name, config_dir)
        if since is None:
            return None, _bad_request(reason)
        return (None, since, None), None
    since = _str_query(query, "since")
    until = _str_query(query, "until")
    for label, value in (("since", since), ("until", until)):
        if value is not None and not _parse_iso8601(value):
            return None, _bad_request(f"{label!r} must be an ISO 8601 timestamp")
    has_since_until = since is not None or until is not None
    default_days = None if has_since_until else _DEFAULT_WINDOW_DAYS
    window_days, err = _int_query(query, "window_days", default_days, minimum=1)
    if err is not None:
        return None, err
    return (window_days, since, until), None


def _period_text(window_days: int | None, since: str | None, until: str | None, *, name: str | None = None) -> str:
    """The window as a phrase that follows an amount: "over the last 30
    days", "in the last hour", "since 2026-09-20T10:00:00Z", "over all
    time"."""
    if name in WINDOW_NAMES:
        return WINDOW_NAMES[name]
    if since or until:
        return _window_label(window_days, since, until)
    if window_days:
        return f"over the last {window_days} days"
    return "over all time"


def _window_label(window_days: int | None, since: str | None, until: str | None) -> str:
    """Matches ``cli.py``'s own ``_window_description`` exactly, so
    ``report.meta.window`` in an API-served report is byte-identical to
    the CLI's for the same window (see this module's docstring).
    """
    if since or until:
        start = f"since {since}" if since else "since the beginning"
        end = f"until {until}" if until else "until now"
        return f"{start} {end}"
    if window_days:
        return f"last {window_days} days"
    return "all time"


def _find_section(model, key: str):
    for section in model.sections:
        if section.key == key:
            return section
    return None


# -- make_handler ---------------------------------------------------------


#: Host names every request may carry in its ``Host`` header.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
#: Wildcard binds: never a name a browser should be sending as ``Host``.
_WILDCARD_BINDS = frozenset({"0.0.0.0", "::", ""})


def allowed_host_names(options: ServeOptions) -> frozenset[str]:
    """The ``Host`` names this server answers to: loopback, the bind
    address when it's a specific one, and ``options.allowed_hosts``.
    Lower-cased, without port or IPv6 brackets."""
    names = set(_LOOPBACK_HOSTS)
    if options.bind not in _WILDCARD_BINDS:
        names.add(options.bind.lower())
    names.update(h.strip().lower().strip("[]") for h in options.allowed_hosts if h.strip())
    return frozenset(names)


def _host_name(header: str) -> str:
    """``Host`` header value -> host name, without port or brackets."""
    value = header.strip().lower()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value[1:]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def make_handler(
    store: "Store",
    options: ServeOptions,
    *,
    watcher_stats: Callable[[], WatcherStats] | None = None,
    static_dir: Path | None = None,
    service_registered: Callable[[], bool | None] | None = None,
    watcher_state: Callable[[], WatcherState] | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Build an ``http.server.BaseHTTPRequestHandler`` subclass with every
    ``/api/*`` route from ``docs/api.md`` bound to ``store``/``options``,
    ready for ``http.server.HTTPServer((options.bind, options.port),
    make_handler(store, options))`` (``contracts.MakeHandler``'s shape).

    ``watcher_stats``, when given, is called on every ``/api/health``
    request for the current :class:`WatcherStats` snapshot -- this is an
    additional keyword-only parameter beyond ``contracts.MakeHandler``'s
    bare ``(store, options)`` signature (a Protocol callable is satisfied
    by an implementation that accepts *extra* optional parameters, so
    this remains a valid ``MakeHandler``); omitted, ``/api/health``
    reports an all-zero :class:`WatcherStats`. ``watcher_state``, when
    given, is called likewise for the watcher's current
    :class:`WatcherState`, from which ``/api/health`` works out its
    ``status``/``message`` (see :func:`_health_status`); omitted, the
    status is always ``"ok"`` and ``scan`` is ``null``.

    ``service_registered``, when given, is called (at most once every
    ``_SERVICE_REGISTERED_CACHE_TTL_S``, per module docstring above) for
    ``/api/health``'s own ``service_registered`` field -- ``True``/
    ``False``/``None`` exactly as it returns them. Omitted (the default,
    and always the case in this module's own tests -- see
    ``installer.py``'s "never touch the machine from a test" posture),
    ``/api/health`` reports ``service_registered: null``, the same
    "unknown, not false" meaning ``installer.is_registered`` itself
    documents. ``service.serve.run`` wires the real
    ``installer.is_registered`` probe in here for an actual ``serve``
    process; this module never imports ``installer.py`` itself, so a
    checkout with only this module's own tests never shells out to
    ``schtasks``/``systemctl``/``launchctl``.

    ``static_dir``, when given, overrides the directory the ``/`` and
    ``/static/*`` routes serve from (default: this package's own
    ``service/static/`` -- the UI package's build output, per
    ``docs/ui.md``). This is a second additional keyword-only parameter,
    added purely so tests can point it at a ``tmp_path`` fixture with a
    real ``index.html``/asset without writing anything into the source
    tree -- the package's own ``static/`` is empty at S1-api's own
    delivery time (a sibling work package ships its contents), so this
    module's own tests exercise only the placeholder-index and
    traversal-protection paths against the real default directory.
    """

    static_dir = (static_dir if static_dir is not None else Path(__file__).resolve().parent / "static")

    report_lock = threading.Lock()
    #: slot -> the latest report built for it: {"key", "token", "model",
    #: "started" (monotonic), "as_of" (ISO)}, least recently used first.
    #: A slot is the window as asked for: a named window by its name (its
    #: resolved start moves every minute), anything else by its key.
    report_cache: OrderedDict = OrderedDict()
    #: cache key -> the build in progress for it, which later requests
    #: for the same window wait on (see _get_report_model).
    report_building: dict = {}
    #: window name -> the start it last resolved to, so a named window's
    #: key maps back to its slot.
    named_window_starts: dict[str, str] = {}
    #: One background rebuild at a time: each is a whole report build,
    #: and they would only slow each other (and requests) down.
    background_builds = threading.Semaphore(1)
    #: What this request's figures are as of, for the X-Figures-As-Of
    #: header (see Handler._write_headers).
    request_ctx = threading.local()

    def _window_query(query, _parse=globals()["_window_query"]):
        # Named windows ("since your last change", "today") need this
        # service's config dir: its apply backups, snapshots and tz.
        window, err = _parse(query, config_dir=options.config_dir)
        name = _str_query(query, "window")
        if err is None and name in WINDOW_NAMES and window[1] is not None:
            with report_lock:
                named_window_starts[name] = window[1]
        return window, err

    service_registered_lock = threading.Lock()
    service_registered_cache: dict = {"checked_at": None, "value": None}

    def _cached_service_registered() -> bool | None:
        if service_registered is None:
            return None
        now = time.monotonic()
        with service_registered_lock:
            checked_at = service_registered_cache["checked_at"]
            if checked_at is not None and (now - checked_at) < _SERVICE_REGISTERED_CACHE_TTL_S:
                return service_registered_cache["value"]
        # Deliberately called outside the lock: the probe itself may
        # spawn a process (installer.is_registered's own subprocess
        # call) and take real wall-clock time -- holding the lock across
        # it would serialise every concurrent /api/health request behind
        # one slow probe instead of just letting a rare double-probe
        # happen right at cache expiry.
        value = service_registered()
        with service_registered_lock:
            service_registered_cache["checked_at"] = time.monotonic()
            service_registered_cache["value"] = value
        return value

    # -- report building / caching --------------------------------------

    def _snapshots_from_store() -> list[Snapshot]:
        out: list[Snapshot] = []
        for row in store.snapshots():
            try:
                data = json.loads(row["digest_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            # S1-integration fix 1.c: Store.snapshots() now reports the
            # snapshot's real project attribution (None for a machine-
            # wide capture, never a project-specific one). Injecting it
            # here lets snapshots.py's own _project_label() (which reads
            # data["project_slug"]) tell a genuinely project-scoped
            # snapshot apart from one that only ever applies at the
            # user/global layer, without this work package touching that
            # frozen module.
            if not snapshots_mod.records_config(data):
                continue  # apply's active-profile stamp: no config in it
            data["project_slug"] = row.get("project_slug")
            out.append(Snapshot(path=Path(""), ts=row["ts"], data=data))
        out.sort(key=lambda s: s.ts)
        return out

    def _latest_config_snapshot() -> Snapshot | None:
        """The newest snapshot that records settings. ``apply`` also
        writes a ``{ts, schema_version, profile_id}`` stamp into the
        snapshots folder to mark the active profile; that stamp carries
        no config, so reading "now" from it would show every key unset."""
        snaps = _snapshots_from_store()
        for snapshot in reversed(snaps):
            if isinstance(snapshot.data.get("effective"), dict):
                return snapshot
        return snaps[-1] if snaps else None

    def _config_snapshot_with_every_project_agents() -> Snapshot | None:
        """:func:`_latest_config_snapshot` with its agents widened to
        every project's latest snapshot (see
        ``snapshots.with_every_project_agents``), for the "now" value of
        an agent recorded in another project."""
        return snapshots_mod.with_every_project_agents(_snapshots_from_store())

    def _build_report_model(window_days: int | None, since: str | None = None, until: str | None = None):
        # Local import: service.rebuild is a sibling work package's
        # module (S1-watcher), not yet present in every checkout this
        # module is imported from -- see this module's docstring.
        from . import rebuild

        config = load_config(options.config_dir)
        rates = load_pricing(path=config.pricing_path, config_dir=options.config_dir)
        corpus = rebuild.corpus_from_store(store, days=window_days, since=since, until=until)
        snaps = _snapshots_from_store()
        projects = tuple(sorted({bundle.slug for bundle in corpus.sessions if bundle.slug}))
        window = _window_label(window_days, since, until)
        try:
            overrides = load_session_overrides(options.config_dir)
        except ConfigError:
            overrides = {}
        # Finding 7: POST /api/sessions/<id>/tags writes to the store's
        # own session_tags table, but classify.classify_session only ever
        # reads session_overrides (config.toml's sessions.toml). Without
        # this merge a tag write was accepted and stored, yet never
        # changed a single report/UI figure -- merge it into the same
        # overrides dict classify_session already consumes, with a
        # store-set tag (the more recently made edit) taking precedence
        # over a config-file override for the same key.
        overrides = {sid: dict(entry) for sid, entry in overrides.items()}
        for session_id, tags in store.all_tags().items():
            merged = overrides.get(session_id, {})
            merged.update(tags)
            overrides[session_id] = merged
        # The statusline's usage log (usage limits, Claude Code's own
        # cache-miss causes), scoped to this window's sessions exactly as
        # the CLI's report does.
        from .. import statusline as statusline_mod
        from ..discovery import _resolve_window

        since_dt, until_dt = _resolve_window(window_days, since, until)
        usage_log_rows = statusline_mod.scoped_usage_log_rows(
            Path(options.config_dir) / "usage-log.csv",
            {bundle.session_id for bundle in corpus.sessions},
            since_dt,
            until_dt,
        )
        return build_report(
            corpus,
            rates,
            config,
            projects=projects,
            window=window,
            snapshots=snaps or None,
            session_overrides=overrides,
            usage_log_rows=usage_log_rows,
            # v4 wiring round: without this, waste.WasteStats's salted
            # session-id hash would fall back to report.py's own
            # temp-directory default (see _default_waste_config_dir) --
            # harmless, but this service already has a real, legitimate
            # config_dir of its own, so its salt should live there
            # alongside its other state rather than in the OS temp dir.
            config_dir=options.config_dir,
        )

    def _slot_for(cache_key):
        """The report cache slot for a key (see ``report_cache``). Call
        with ``report_lock`` held."""
        window_days, since, until = cache_key
        if window_days is None and until is None and since is not None:
            for name, start in named_window_starts.items():
                if start == since:
                    return ("named", name)
        return cache_key

    def _keep_report(cache_key, token, model, started: float, as_of: str) -> None:
        """Keep a finished build, unless the slot already holds one that
        started later. Call with ``report_lock`` held."""
        slot = _slot_for(cache_key)
        current = report_cache.get(slot)
        if current is not None and current["started"] > started:
            return
        report_cache[slot] = {"key": cache_key, "token": token, "model": model, "started": started, "as_of": as_of}
        report_cache.move_to_end(slot)
        while len(report_cache) > _REPORT_CACHE_SIZE:
            report_cache.popitem(last=False)

    def _note_as_of(as_of: str, refreshing: bool) -> None:
        """Record this request's figures' age for its response headers
        (the oldest, when one request reads several reports)."""
        current = getattr(request_ctx, "as_of", None)
        if current is None or as_of < current[0]:
            request_ctx.as_of = (as_of, refreshing)
        elif refreshing:
            request_ctx.as_of = (current[0], True)

    def _build_and_keep(cache_key, building: Future):
        token = store.change_token()
        started = time.monotonic()
        as_of = _now_utc_iso()
        try:
            model = _build_report_model(*cache_key)
        except BaseException as exc:
            with report_lock:
                report_building.pop(cache_key, None)
            building.set_exception(exc)
            raise
        with report_lock:
            report_building.pop(cache_key, None)
            _keep_report(cache_key, token, model, started, as_of)
        building.set_result(model)
        return model, as_of

    def _rebuild_in_background(cache_key, building: Future) -> None:
        def run():
            try:
                with background_builds:
                    _build_and_keep(cache_key, building)
            except BaseException:  # noqa: BLE001 -- the next request retries; waiters see it via the Future
                pass
            finally:
                store.close()  # this thread's own connection

        threading.Thread(target=run, name="claude-token-lens-report", daemon=True).start()

    def _get_report_model(window_days: int | None, since: str | None = None, until: str | None = None):
        """The report for a window, built at most once per store change.

        Stale-while-revalidate: when the store has changed since the
        window's report was built (a live session writes every few
        seconds), the kept report is served at once and a rebuild starts
        in the background, so a tab never waits on a whole report build
        just because a transcript grew. Only a window with nothing kept
        (or a report older than ``_STALE_REPORT_MAX_AGE_S``) is built
        while the request waits, and requests for a window already being
        built wait on that one build rather than starting their own.
        """
        # Cache key widened from a bare window_days to the full
        # (window_days, since, until) triple so a since/until request
        # never collides with (or is served from) a plain window_days
        # entry for the same store change_token.
        cache_key = (window_days, since, until)
        token = store.change_token()
        now = time.monotonic()
        with report_lock:
            slot = _slot_for(cache_key)
            kept = report_cache.get(slot)
            if kept is not None:
                report_cache.move_to_end(slot)
                if kept["key"] == cache_key and kept["token"] == token:
                    _note_as_of(kept["as_of"], False)
                    return kept["model"]
                if now - kept["started"] > _STALE_REPORT_MAX_AGE_S:
                    kept = None
            building = report_building.get(cache_key)
            owner = building is None
            if owner:
                building = report_building[cache_key] = Future()
        if kept is not None:
            # Serve what is kept; refresh it behind the scenes.
            if owner:
                _rebuild_in_background(cache_key, building)
            _note_as_of(kept["as_of"], True)
            return kept["model"]
        if not owner:
            model = building.result()
            with report_lock:
                kept = report_cache.get(_slot_for(cache_key))
            _note_as_of(kept["as_of"] if kept is not None else _now_utc_iso(), False)
            return model
        model, as_of = _build_and_keep(cache_key, building)
        _note_as_of(as_of, False)
        return model

    # -- store-backed routes ---------------------------------------------

    def route_health(store, query, body):
        last_stats = watcher_stats() if watcher_stats is not None else None
        state = watcher_state() if watcher_state is not None else None
        stats = last_stats or WatcherStats()
        status, message = _health_status(last_stats, state, poll_interval_s=options.poll_interval_s)
        data = {
            # "ok", "starting" (first scan still running), "degraded"
            # (the last scan failed) or "stale" (the scanner stopped, or
            # nothing has finished for a long while); ``message`` says
            # what that means in plain words, null when ok.
            "status": status,
            "message": message,
            # Where the scanner is right now (its progress through a
            # scan, when the last one finished); ``watcher`` below is
            # what its last finished scan did.
            "scan": to_jsonable(state) if state is not None else None,
            # The running code's version, so "is the dashboard still on
            # the old version after an update?" has a one-look answer.
            "version": _TOOL_VERSION,
            "schema_version": store.schema_version() or 0,
            "watcher": to_jsonable(stats),
            # Finding 3: a transcript whose file has gone missing (past
            # Claude Code's own cleanupPeriodDays, or simply deleted) is
            # marked rather than removed -- surfacing the running total
            # here lets an operator notice a projects-root misconfiguration
            # (everything suddenly "missing") without it being silent.
            "transcripts_missing": store.count_missing_transcripts(),
            # v3: whether `serve` is registered to start at logon/boot
            # (installer.py) -- true/false when the platform's own query
            # command gave a clear answer, null when it couldn't be run
            # at all (no probe wired up, an unsupported platform, or the
            # query tool itself missing). Never a raw path -- a plain
            # boolean, per this route's existing privacy posture.
            "service_registered": _cached_service_registered(),
        }
        return _ok(data)

    def route_summary(store, query, body):
        if _str_query(query, "window") is not None:
            window, err = _window_query(query)
            if err is not None:
                return err
            return _ok(store.summary(since=window[1]))
        window_days, err = _int_query(query, "window_days", None, minimum=0)
        if err is not None:
            return err
        return _ok(store.summary(window_days=window_days))

    def _listing_window(query):
        """The window a store listing is limited to: none unless the
        request names one (``window``, ``window_days``, ``since`` or
        ``until``), then the same one the report uses."""
        if not any(key in query for key in ("window", "window_days", "since", "until")):
            return (None, None, None), None
        return _window_query(query)

    def route_sessions(store, query, body):
        limit, err = _int_query(query, "limit", 50, minimum=0)
        if err is not None:
            return err
        offset, err = _int_query(query, "offset", 0, minimum=0)
        if err is not None:
            return err
        window, err = _listing_window(query)
        if err is not None:
            return err
        window_days, since, until = window
        return _ok(store.sessions(limit=limit, offset=offset, window_days=window_days, since=since, until=until))

    def route_session(store, query, body):
        session_id = query.get("id", "")
        result = store.session(session_id)
        if result is None:
            return _not_found("session not found")
        # S1-integration fix 1.g: per-turn context/cache series for the
        # session-timeline chart, sourced from the top-level transcript's
        # stored digest -- see Store.turns_for_session's own docstring
        # for the exact shape. None (no stored top-level transcript --
        # shouldn't normally happen for a session store.session() found)
        # simply omits both keys rather than sending an empty shape.
        turns = store.turns_for_session(session_id)
        if turns is not None:
            result["turn_series"] = turns["turn_series"]
            result["markers"] = turns["markers"]
            # Finding 11: a very long session's turn_series is
            # downsampled server-side; tell the UI so it can say so
            # rather than silently rendering a thinned-out chart.
            result["truncated"] = turns["truncated"]
            # v3-limits wiring: usage-cap pause/resume/agent-terminated
            # markers for the session-timeline chart, alongside the
            # existing compactions/spawns/human markers above.
            result["limit_markers"] = turns["limit_markers"]
        return _ok(result)

    def route_recache(store, query, body):
        return _ok(store.recache())

    def route_daily_usage(store, query, body):
        days, err = _int_query(query, "days", 30, minimum=1)
        if err is not None:
            return err
        return _ok(store.daily_usage(days=days))

    def route_compactions(store, query, body):
        window, err = _listing_window(query)
        if err is not None:
            return err
        window_days, since, until = window
        return _ok(store.compactions(window_days=window_days, since=since, until=until))

    def _latest_baseline_row(store) -> dict | None:
        rows = store.baselines()
        if not rows:
            return None
        # Store.baselines() is already ordered by created_at ascending
        # (its own ``ORDER BY b.created_at``) -- the last row is the
        # most recent capture across every project, matching "the latest
        # stored baseline digest" (singular) this route now returns.
        return rows[-1]

    def route_profiles(store, query, body):
        # v0.3: catalogue profiles are shipped package data, never rows
        # in the store (service/watcher.py's _scan_profiles never
        # ingests a catalogue id) -- merged in here at query time instead,
        # each tagged with which of the two it came from.
        catalogue_entries = [
            {
                "id": p.id,
                "name": p.name or p.id,
                "source": "catalogue",
                "archetype": p.archetype,
                "for": list(p.for_),
                "updated_at": None,
            }
            for p in profile_catalogue.list_profiles()
        ]
        user_entries = [
            {
                "id": row["id"],
                "name": row["name"],
                "source": "user",
                "archetype": None,
                "for": [],
                "updated_at": row["updated_at"],
            }
            for row in store.profiles()
        ]

        suggested_profile_id = None
        latest_baseline = _latest_baseline_row(store)
        if latest_baseline is not None:
            try:
                record = json.loads(latest_baseline["digest_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                record = {}
            if isinstance(record, dict):
                suggested_profile_id = record.get("suggested_profile")

        return _ok(
            {
                "profiles": catalogue_entries + user_entries,
                "suggested_profile_id": suggested_profile_id,
            }
        )

    def route_baseline(store, query, body):
        # v0.3: pair the latest capture with the onboarding capture
        # window's own status (baseline.capture_status) so the UI can
        # mark a recommendation/diff built from it as provisional --
        # config.toml is read the same way _build_report_model already
        # does for every report-backed route (never guarded there
        # either: an unreadable config.toml is a genuine 500, not
        # something this route should mask).
        config = load_config(options.config_dir)
        status = baseline_mod.capture_status(config)

        latest_row = _latest_baseline_row(store)
        latest: dict | None = None
        if latest_row is not None:
            try:
                record = json.loads(latest_row["digest_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                record = None
            latest = {
                "id": latest_row["id"],
                "project_slug": latest_row["project_slug"],
                "window_start": latest_row["window_start"],
                "window_end": latest_row["window_end"],
                "archetype": latest_row["archetype"],
                "created_at": latest_row["created_at"],
                "record": record if isinstance(record, dict) else None,
            }

        history = [
            {
                "id": row["id"],
                "project_slug": row["project_slug"],
                "window_start": row["window_start"],
                "window_end": row["window_end"],
                "archetype": row["archetype"],
                "created_at": row["created_at"],
            }
            for row in reversed(store.baselines())
        ]

        return _ok(
            {
                "baseline": latest,
                "history": history,
                "capture_status": {
                    "started": status.started,
                    "window_days": status.window_days,
                    "elapsed_days": status.elapsed_days,
                    "remaining_days": status.remaining_days,
                    "complete": status.complete,
                    "summary": baseline_mod.format_capture_status(status),
                },
            }
        )

    def route_set_tag(store, query, body):
        session_id = query.get("id", "")
        if store.session(session_id) is None:
            return _not_found("session not found")
        if not isinstance(body, dict):
            return _bad_request("request body must be a JSON object")
        key = body.get("key")
        value = body.get("value")
        if key not in ("mode", "purpose"):
            return _bad_request("'key' must be 'mode' or 'purpose'")
        if not isinstance(value, str):
            return _bad_request("'value' must be a string")
        store.set_tag(session_id, key, value)
        return _ok({"session_id": session_id, "tags": store.tags(session_id)})

    # -- v0.3 profile routes -----------------------------------------------

    def _load_profile_by_id(profile_id: str):
        """``Profile`` for ``profile_id`` -- a catalogue id first (shipped
        package data, cheap to check), then a user profile written under
        ``<config_dir>/profiles/<id>.toml`` (the one path
        ``route_profiles_post``/``_scan_profiles`` ever write a user
        profile to -- see that route's own docstring). ``None`` if
        neither exists, or the on-disk file no longer parses (never lets
        a malformed file 500 the route -- this project's usual "skip,
        don't crash" posture for a foreign/edited-by-hand file)."""
        if profile_id in profile_catalogue.CATALOGUE_IDS:
            return profile_catalogue.get(profile_id)
        path = Path(options.config_dir) / "profiles" / f"{profile_id}.toml"
        if not path.is_file():
            return None
        try:
            return profile_schema.load_profile(path)
        except (OSError, profile_schema.ProfileError, ValueError):
            return None

    def route_profile_diff(store, query, body):
        profile_id = query.get("id", "")
        profile = _load_profile_by_id(profile_id)
        if profile is None:
            return _not_found(f"unknown profile: {profile_id!r}")

        scope = query.get("scope") or "user"
        if scope not in _VALID_PROFILE_SCOPES:
            return _bad_request(f"'scope' must be one of {_VALID_PROFILE_SCOPES}")

        notes: list[str] = []
        snapshot = _config_snapshot_with_every_project_agents()
        if snapshot is not None:
            effective = snapshots_mod.effective_config(snapshot)
            provenance = snapshots_mod.effective_provenance(snapshot)
            managed_keys = set(snapshots_mod.managed_keys(snapshot))
            # Deviation (mirrors profiles/apply.py's own, documented
            # deviation note): snapshots.py has no effective_agents()
            # accessor, so the schema-2 field is read straight off the
            # snapshot's own data dict.
            raw_effective_agents = snapshot.data.get("effective_agents")
            effective_agents = dict(raw_effective_agents) if isinstance(raw_effective_agents, dict) else {}
        else:
            effective, provenance, managed_keys, effective_agents = {}, {}, set(), {}
            notes.append("no config snapshot recorded yet; diff computed against an empty effective config")

        profile_diff = profile_diff_mod.diff_against_effective(
            profile, effective, effective_agents, provenance, managed_keys
        )
        diff_text = profile_diff_mod.render_unified_diff(profile_diff, scope=scope)
        # project_path is deliberately never accepted from the client here
        # (unlike diff.py's own apply_command signature) -- this route's
        # response is API/UI output, and this project's privacy rule
        # forbids a raw filesystem path in any of it; a project-scoped
        # apply command is rendered without --project-dir, exactly as
        # apply_command's own docstring describes for "project_path
        # omitted" (the user fills it in themselves when they run it).
        apply_cmd, launch_cmd = profile_diff_mod.apply_command(profile.id, scope).split("\n", 1)

        from ..fixes import LEVER_LABELS, SETTING_TEXT, profile_change_where, profile_prompt

        def _row(row) -> dict:
            parts = row.key.split(".")
            # agents.<agent>.<key>; the key itself may be dotted (experimental.cacheTtl).
            name = ".".join(parts[2:]) if row.key.startswith("agents.") else ".".join(parts[1:])
            return {
                "key": row.key,
                "setting": name,
                "agent": parts[1] if row.key.startswith("agents.") else None,
                "label": LEVER_LABELS.get(name, name),
                "description": SETTING_TEXT.get(name, ("", "", ""))[0],
                "where": profile_change_where(row.key, scope),
                "current_value": row.current_value,
                "current_provenance": row.current_provenance,
                "proposed_value": row.proposed_value,
                "target_file": row.target_file,
                "managed": row.managed,
            }

        settings_rows = [_row(r) for r in profile_diff.rows if r.key.startswith("settings.")]
        agent_rows = [_row(r) for r in profile_diff.rows if r.key.startswith("agents.")]
        env_rows = [_row(r) for r in profile_diff.rows if r.key.startswith("env.")]

        return _ok(
            {
                "profile_id": profile.id,
                "scope": scope,
                "diff": diff_text,
                "settings": settings_rows,
                "agents": agent_rows,
                "env": env_rows,
                "apply_command": apply_cmd,
                "dry_run_command": f"{apply_cmd} --dry-run",
                "launch_command": launch_cmd,
                "prompt": profile_prompt(profile.name or profile.id, settings_rows + agent_rows + env_rows, scope),
                "notes": notes,
            }
        )

    def route_profile_schema(store, query, body):
        """Every key a profile may set, with its type, allowed values and
        plain-English text, for the dashboard's profile form."""
        from ..fixes import LEVER_LABELS, SETTING_TEXT

        def _lever(key, spec) -> dict:
            what, tradeoff, caveat = SETTING_TEXT.get(key, ("", "", ""))
            return {
                "key": key,
                "label": LEVER_LABELS.get(key, key),
                "kind": spec.kind,
                "values": list(spec.values) if spec.values else None,
                "min": spec.min,
                "max": spec.max,
                "description": what,
                "tradeoff": " ".join(t for t in (tradeoff, caveat) if t),
            }

        return _ok(
            {
                "settings": [_lever(k, v) for k, v in profile_schema.SETTINGS_ALLOWLIST.items()],
                "agents": [_lever(k, v) for k, v in profile_schema.AGENT_ALLOWLIST.items()],
                "env": sorted(profile_schema.ENV_ALLOWLIST),
                "archetypes": list(profile_schema.ARCHETYPES),
                "scopes": [
                    {"key": "user", "label": "Your user settings, every project"},
                    {"key": "project-local", "label": "This project, on your machine only"},
                    {"key": "repo", "label": "This project, shared with everyone who works in it"},
                ],
            }
        )

    def route_profile(store, query, body):
        profile_id = query.get("id", "")
        profile = _load_profile_by_id(profile_id)
        if profile is None:
            return _not_found(f"unknown profile: {profile_id!r}")
        setting_count = len(profile.settings) + sum(len(v) for v in profile.agents.values()) + len(profile.env)
        return _ok(
            {
                "id": profile.id,
                "name": profile.name or profile.id,
                "source": "catalogue" if profile_id in profile_catalogue.CATALOGUE_IDS else "user",
                "archetype": profile.archetype,
                "for": list(profile.for_),
                "notes": profile.notes,
                "settings": dict(profile.settings),
                "agents": {name: dict(keys) for name, keys in profile.agents.items()},
                "env": dict(profile.env),
                "setting_count": setting_count,
            }
        )

    def route_session_explain(store, query, body):
        from .explain import explain_session
        from ..units import Units

        session_id = query.get("id", "")
        detail = store.session(session_id)
        if detail is None:
            return _not_found("session not found")
        config = load_config(options.config_dir)
        rates = load_pricing(path=config.pricing_path, config_dir=options.config_dir)
        units = Units(billing_mode=config.billing, currency=rates.currency)
        explained = explain_session(
            detail, store.session_parts(session_id), rates, units, store.median_session_cost()
        )
        return _ok({"session_id": session_id, **explained})

    def route_profiles_from_current(store, query, body):
        """Save the latest snapshot's effective config as a user
        profile: allowlisted keys only, managed keys left out and listed
        so the UI can say so. Writes only this tool's own profile store,
        never Claude Code's config."""
        body = body if isinstance(body, dict) else {}
        snapshot = _latest_config_snapshot()
        if snapshot is None or not isinstance(snapshot.data.get("effective"), dict):
            return _error(409, "conflict", "no config snapshot recorded yet; run claude-token-lens snapshot-config")
        effective = snapshots_mod.effective_config(snapshot)
        managed = set(snapshots_mod.managed_keys(snapshot))
        raw_agents = snapshot.data.get("effective_agents")
        effective_agents = raw_agents if isinstance(raw_agents, dict) else {}

        def _valid(doc: dict) -> bool:
            # One key at a time, so one out-of-range value drops only itself.
            return profile_schema.validate({"id": "x", **doc}) == []

        settings = {
            key: effective[key]
            for key in profile_schema.SETTINGS_ALLOWLIST
            if key in effective and key not in managed and effective[key] is not None
            and _valid({"settings": {key: effective[key]}})
        }
        agents: dict = {}
        for name, fields in effective_agents.items():
            if not isinstance(fields, dict):
                continue
            kept = {
                key: fields[key]
                for key in profile_schema.AGENT_ALLOWLIST
                if fields.get(key) is not None and _valid({"agents": {str(name): {key: fields[key]}}})
            }
            if kept:
                agents[str(name)] = kept
        doc = {
            "id": body.get("id") or "my-current-settings",
            "name": body.get("name") or "My current settings",
            "settings": settings,
            "agents": agents,
            "notes": f"Saved from the config snapshot taken {snapshot.ts}.",
        }
        status, payload = _save_user_profile(store, doc, replace=query.get("replace") == "1")
        if status == 201:
            payload["data"]["skipped_managed"] = sorted(k for k in profile_schema.SETTINGS_ALLOWLIST if k in managed)
        return status, payload

    def route_profiles_post(store, query, body):
        if not isinstance(body, dict):
            return _bad_request("request body must be a JSON object")
        return _save_user_profile(store, body, replace=query.get("replace") == "1")

    def _save_user_profile(store, body: dict, *, replace: bool):
        try:
            profile = profile_schema.load_dict(body)
        except profile_schema.ProfileError as exc:
            return _bad_request("; ".join(exc.problems))

        if profile.id in profile_catalogue.CATALOGUE_IDS:
            return _error(409, "conflict", f"{profile.id!r} is a reserved catalogue profile id")

        profiles_dir = Path(options.config_dir) / "profiles"
        target_path = profiles_dir / f"{profile.id}.toml"
        if target_path.is_file() and not replace:
            return _error(
                409, "conflict", f"profile {profile.id!r} already exists (pass ?replace=1 to overwrite)"
            )

        profiles_dir.mkdir(parents=True, exist_ok=True)
        text = profile_schema.dump_profile(profile)
        # Atomic write: temp file in the same directory + os.replace,
        # this module's own convention for "never leave a half-written
        # file behind" (mirrors cache.py's DigestCache.put).
        fd, tmp_name = tempfile.mkstemp(dir=str(profiles_dir), prefix=f".{profile.id}-", suffix=".toml.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp_name, target_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        store.upsert_profile(
            profile_id=profile.id,
            name=profile.name or profile.id,
            toml_path=str(target_path),
            content_hash=content_hash,
        )

        stored = next((row for row in store.profiles() if row["id"] == profile.id), None)
        data = {
            "id": profile.id,
            "name": profile.name or profile.id,
            "source": "user",
            "updated_at": stored["updated_at"] if stored is not None else None,
        }
        return 201, {"ok": True, "data": data}

    # -- report-backed routes ---------------------------------------------

    def route_ttl(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window)
        section = _find_section(model, "ttl")
        return _ok(to_jsonable(section) if section is not None else None)

    def route_carry(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window)
        section = _find_section(model, "carry")
        return _ok(to_jsonable(section) if section is not None else None)

    def route_compaction_sim(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window)
        section = _find_section(model, "compaction_sim")
        return _ok(to_jsonable(section) if section is not None else None)

    def route_model_swap(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window)
        section = _find_section(model, "model_swap")
        return _ok(to_jsonable(section) if section is not None else None)

    def route_waste(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window)
        section = _find_section(model, "waste")
        return _ok(to_jsonable(section) if section is not None else None)

    def route_config_diff(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        key = query.get("key")
        auto_keys = query.get("auto_keys") == "1"
        if not key and not auto_keys:
            return _bad_request("provide 'key' or 'auto_keys=1'")
        model = _get_report_model(*window)
        section = _find_section(model, "config")
        tables = section.tables if section is not None else []
        if auto_keys:
            return _ok([to_jsonable(table) for table in tables])
        table = next((t for t in tables if t.name == f"config-diff-{key}"), None)
        return _ok(to_jsonable(table) if table is not None else [])

    def route_diagnostics(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window)
        hook = hook_health.check(options.config_dir)
        statusline = hook_health.statusline_check(options.config_dir, store.entrypoint_counts())
        return _ok(to_jsonable(helptext.diagnostics_table(model.diagnostics, hook=hook, statusline=statusline)))

    def _report_units(model):
        from ..units import Units

        if model.units is not None:
            return model.units
        config = load_config(options.config_dir)
        return Units(billing_mode=config.billing, currency=model.meta.pricing.currency)

    def _claude_md_review(window, query):
        from .. import claude_md_review

        model = _get_report_model(*window)
        review = claude_md_review.build_review(options.config_dir, model.context_files or {})
        return claude_md_review, review, _report_units(model), _period_text(*window, name=query.get("window"))

    def route_claude_md(store, query, body):
        """Every CLAUDE.md-family file on disk, with how often it was sent
        in the window and what it cost. File text is read now and never
        stored."""
        window, err = _window_query(query)
        if err is not None:
            return err
        module, review, units, period = _claude_md_review(window, query)
        return _ok(
            {
                "period": period,
                "transcripts": review.transcripts,
                "files": [module.file_summary(item, units, period) for item in review.files],
            }
        )

    def route_claude_md_file(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        module, review, units, period = _claude_md_review(window, query)
        file_id = query.get("id", "")
        item = next((entry for entry in review.files if entry.id == file_id), None)
        if item is None:
            return _not_found("unknown CLAUDE.md file")
        return _ok({"period": period, **module.file_detail(item, units, period)})

    def route_skills(store, query, body):
        """Every skill Claude Code listed in the window: what it is (its
        description, read now from the newest listing and never stored),
        where it comes from, how often it was listed and used, and how
        to hide the ones Claude never uses."""
        from .. import skills_review

        window, err = _window_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window)
        return _ok(
            skills_review.review(
                options.config_dir,
                model.context_files or {},
                _report_units(model),
                _period_text(*window, name=query.get("window")),
            )
        )

    def _current_settings() -> tuple[dict, dict]:
        """The latest snapshot's effective settings, and every project's
        agent fields, or empty when no snapshot is recorded yet."""
        snapshot = _config_snapshot_with_every_project_agents()
        if snapshot is None:
            return {}, {}
        agents = snapshot.data.get("effective_agents")
        return snapshots_mod.effective_config(snapshot), agents if isinstance(agents, dict) else {}

    def route_profile_goals(store, query, body):
        """Without ``goal``: the goals a profile can start from. With it:
        that goal's candidate changes, each with the value in effect now,
        the evidence, the trade-off and a what-if estimate."""
        from ..profiles import goals

        goal = query.get("goal")
        if not goal:
            return _ok({"goals": goals.goals_list()})
        if goal not in goals.GOAL_IDS:
            return _bad_request(f"unknown goal {goal!r}; expected one of: {', '.join(goals.GOAL_IDS)}")
        window, err = _window_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window)
        effective, effective_agents = _current_settings()
        return _ok(
            goals.draft(
                goal,
                model,
                _report_units(model),
                effective=effective,
                effective_agents=effective_agents,
                period=_period_text(*window, name=query.get("window")),
            )
        )

    def route_whatif(store, query, body):
        """The estimated effect of ``{settings, agents}`` on the window,
        looked up in the report's own tables. Reads only; nothing is
        saved or applied."""
        from .. import whatif

        if not isinstance(body, dict):
            return _bad_request("request body must be a JSON object")
        settings = body.get("settings") or {}
        agents = body.get("agents") or {}
        if not isinstance(settings, dict) or not isinstance(agents, dict):
            return _bad_request("settings and agents must be JSON objects")
        problems = profile_schema.validate({"id": "whatif", "settings": settings, "agents": agents})
        if problems:
            return _bad_request("; ".join(problems))
        window, err = _window_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window)
        effective, _agents = _current_settings()
        return _ok(
            whatif.estimate(
                settings,
                agents,
                model,
                _report_units(model),
                period=_period_text(*window, name=query.get("window")),
                current=effective,
            )
        )

    def _quick_context(window, query):
        from .. import quick_actions

        model = _get_report_model(*window)
        effective, effective_agents = _current_settings()
        return quick_actions, quick_actions.Context(
            model=model,
            units=_report_units(model),
            period=_period_text(*window, name=query.get("window")),
            config_dir=Path(options.config_dir),
            effective=effective,
            effective_agents=effective_agents,
        )

    def route_quick_actions(store, query, body):
        """Every quick-action check's status and one-line answer for the
        window."""
        window, err = _window_query(query)
        if err is not None:
            return err
        module, ctx = _quick_context(window, query)
        return _ok({"period": ctx.period, "checks": module.run_all(ctx)})

    def route_quick_action(store, query, body):
        """One check in full: its evidence table, fixes and tips."""
        window, err = _window_query(query)
        if err is not None:
            return err
        module, ctx = _quick_context(window, query)
        if query.get("id") not in module.CHECK_IDS:
            return _not_found("unknown quick action")
        return _ok(module.run(query["id"], ctx))

    def route_setup(store, query, body):
        """What this tool has installed and changed on this machine, what
        each costs in tokens and how to undo it, plus what to expect."""
        from .. import footprint

        items = footprint.inventory(options.config_dir, service_registered=_cached_service_registered())
        return _ok(
            {
                "items": [item.as_dict() for item in items],
                "expectations": [{"title": title, "text": text} for title, text in footprint.EXPECTATIONS],
                "uninstall_command": footprint.UNINSTALL_COMMAND,
            }
        )

    impact_cache: dict = {"key": None, "data": None, "started": 0.0, "as_of": None, "building": False}

    def route_impact(store, query, body):
        """Each change you made (an apply, its undo, or a settings change
        the config hook saw) with the sessions before it against those
        after it, on the measures that change should move. Cached like
        the report (see _get_report_model): a store change serves the
        kept answer and refreshes it in the background, while a new
        change point (the list itself changing) is worked out at once."""
        from .. import change_points

        points = change_points.change_points(options.config_dir)
        point_key = tuple((p.iso(), p.source, p.backup_ts) for p in points)
        key = (store.change_token(), point_key)
        now = time.monotonic()
        refresh = False
        with report_lock:
            kept = impact_cache["data"]
            kept_key = impact_cache["key"]
            if kept is not None and kept_key == key:
                _note_as_of(impact_cache["as_of"], False)
                return _ok(kept)
            if (
                kept is not None
                and kept_key[1] == point_key
                and now - impact_cache["started"] <= _STALE_REPORT_MAX_AGE_S
            ):
                refresh = not impact_cache["building"]
                if refresh:
                    impact_cache["building"] = True
                _note_as_of(impact_cache["as_of"], True)
            else:
                kept = None
        if kept is not None:
            if refresh:

                def run():
                    try:
                        with background_builds:
                            _compute_impact(points, key)
                    except BaseException:  # noqa: BLE001 -- the next request retries
                        pass
                    finally:
                        with report_lock:
                            impact_cache["building"] = False
                        store.close()

                threading.Thread(target=run, name="claude-token-lens-impact", daemon=True).start()
            return _ok(kept)
        data = _compute_impact(points, key)
        _note_as_of(impact_cache["as_of"] or _now_utc_iso(), False)
        return _ok(data)

    def _compute_impact(points, key):
        from .. import impact as impact_mod
        from . import rebuild

        started = time.monotonic()
        as_of = _now_utc_iso()
        changes: list = []
        if points:
            config = load_config(options.config_dir)
            rates = load_pricing(path=config.pricing_path, config_dir=options.config_dir)
            earliest = points[0].ts - timedelta(days=impact_mod.LOOKBACK_DAYS)
            corpus = rebuild.corpus_from_store(store, since=earliest.strftime("%Y-%m-%dT%H:%M:%SZ"))
            sessions = impact_mod.session_facts(corpus, rates)
            units = _report_units(_get_report_model(_DEFAULT_WINDOW_DAYS))
            changes = impact_mod.impact(points, sessions, units)
        data = {
            "changes": changes,
            "caveat": impact_mod.CAVEAT,
            "min_sessions": impact_mod.MIN_SESSIONS,
            "lookback_days": impact_mod.LOOKBACK_DAYS,
        }
        with report_lock:
            if started >= impact_cache["started"]:
                impact_cache.update(key=key, data=data, started=started, as_of=as_of)
        return data

    def route_recommendations(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window)
        return _ok([to_jsonable(rec) for rec in model.recommendations])

    def _render_report(content_type: str, render: Callable[[object], str]):
        def _route(store, query, body):
            window, err = _window_query(query)
            if err is not None:
                return err
            model = _get_report_model(*window)
            return ("raw", content_type, render(model))

        return _route

    host_names = allowed_host_names(options)

    # -- routing tables -----------------------------------------------------

    get_routes: dict[str, Callable] = {
        "/api/health": route_health,
        "/api/summary": route_summary,
        "/api/sessions": route_sessions,
        "/api/recache": route_recache,
        "/api/compactions": route_compactions,
        "/api/profiles": route_profiles,
        "/api/baseline": route_baseline,
        "/api/daily-usage": route_daily_usage,
        "/api/ttl": route_ttl,
        "/api/carry": route_carry,
        "/api/compaction-sim": route_compaction_sim,
        "/api/model-swap": route_model_swap,
        "/api/waste": route_waste,
        "/api/config-diff": route_config_diff,
        "/api/recommendations": route_recommendations,
        "/api/diagnostics": route_diagnostics,
        "/api/profile-schema": route_profile_schema,
        "/api/claude-md": route_claude_md,
        "/api/skills": route_skills,
        "/api/impact": route_impact,
        "/api/profile-goals": route_profile_goals,
        "/api/quick-actions": route_quick_actions,
        "/api/setup": route_setup,
        "/api/report.json": _render_report("application/json", lambda model: render_json(model)),
        # Finding 22: charset was missing on the two text-ish renderers
        # (application/json has no encoding ambiguity, but text/markdown
        # and text/html do -- a client/browser guessing the wrong one on
        # a non-ASCII report is exactly the failure mode this closes).
        "/api/report.md": _render_report("text/markdown; charset=utf-8", render_markdown),
        "/api/report.html": _render_report("text/html; charset=utf-8", render_html),
    }
    get_patterns: tuple[tuple[re.Pattern, Callable], ...] = (
        (_SESSION_ID_RE, route_session),
        (_SESSION_EXPLAIN_RE, route_session_explain),
        (_PROFILE_DIFF_RE, route_profile_diff),
        (_PROFILE_RE, route_profile),
        (_CLAUDE_MD_RE, route_claude_md_file),
        (_QUICK_ACTION_RE, route_quick_action),
    )
    post_routes: dict[str, Callable] = {
        "/api/profiles": route_profiles_post,
        "/api/profiles/from-current": route_profiles_from_current,
        "/api/whatif": route_whatif,
    }
    post_patterns: tuple[tuple[re.Pattern, Callable], ...] = (
        (_SESSION_TAGS_RE, route_set_tag),
    )

    class Handler(BaseHTTPRequestHandler):
        # Review S5: derived from __version__ (major.minor, matching the
        # stdlib http.server convention of a two-part version on this
        # header) rather than a literal that goes stale on every release.
        server_version = f"claude-token-lens/{'.'.join(_TOOL_VERSION.split('.')[:2])}"
        protocol_version = "HTTP/1.1"

        # -- quiet by default: never log a request path to stdout/stderr
        # (docs/api.md's privacy rule is about response bodies, but a
        # request path can carry a session/profile id too -- keep both
        # off any shared log by default).
        def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
            pass

        # -- low-level writers ------------------------------------------

        def _write_headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            for name, value in _SECURITY_HEADERS:
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            # When this request read a built report: when that report's
            # figures were read from the store, and whether a newer one
            # is being built (see _get_report_model).
            as_of = getattr(request_ctx, "as_of", None)
            if as_of is not None:
                self.send_header("X-Figures-As-Of", as_of[0])
                if as_of[1]:
                    self.send_header("X-Figures-Refreshing", "1")
            self.end_headers()

        def _write_json(self, status: int, payload: dict, *, head_only: bool = False) -> None:
            body = json.dumps(payload).encode("utf-8")
            self._write_headers(status, "application/json", len(body))
            if not head_only:
                self.wfile.write(body)

        def _write_text(self, status: int, content_type: str, text: str, *, head_only: bool = False) -> None:
            body = text.encode("utf-8")
            self._write_headers(status, content_type, len(body))
            if not head_only:
                self.wfile.write(body)

        def _write_bytes(self, status: int, content_type: str, data: bytes, *, head_only: bool = False) -> None:
            self._write_headers(status, content_type, len(data))
            if not head_only:
                self.wfile.write(data)

        # -- static files --------------------------------------------------

        def _serve_index(self, *, head_only: bool = False) -> None:
            index_path = static_dir / "index.html"
            if static_dir.is_dir() and index_path.is_file():
                try:
                    self._write_bytes(200, "text/html", index_path.read_bytes(), head_only=head_only)
                    return
                except OSError:
                    pass
            self._write_text(200, "text/html", _PLACEHOLDER_INDEX_HTML, head_only=head_only)

        def _serve_static(self, raw_name: str, *, head_only: bool = False) -> None:
            name = urllib.parse.unquote(raw_name)
            if not name or ".." in Path(name).parts:
                self._write_json(*_not_found(), head_only=head_only)
                return
            try:
                base = static_dir.resolve()
                candidate = (static_dir / name).resolve()
            except (OSError, ValueError, RuntimeError):
                self._write_json(*_not_found(), head_only=head_only)
                return
            if candidate != base and base not in candidate.parents:
                self._write_json(*_not_found(), head_only=head_only)
                return
            if not candidate.is_file():
                self._write_json(*_not_found(), head_only=head_only)
                return
            content_type, _encoding = mimetypes.guess_type(str(candidate))
            self._write_bytes(
                200, content_type or "application/octet-stream", candidate.read_bytes(), head_only=head_only
            )

        # -- dispatch --------------------------------------------------------

        def _host_allowed(self) -> bool:
            """DNS-rebinding guard: a page on attacker.example that
            re-resolves its own name to 127.0.0.1 reaches this server as
            same-origin, but its requests still carry ``Host:
            attacker.example``. Only names in :func:`allowed_host_names`
            are answered. A request without ``Host`` (HTTP/1.0, not a
            browser) is allowed."""
            host = self.headers.get("Host")
            return host is None or _host_name(host) in host_names

        def _dispatch(self, body: dict | None, *, head_only: bool = False) -> None:
            request_ctx.as_of = None
            if not self._host_allowed():
                self._write_json(
                    *_forbidden("this Host is not allowed; start serve with --allowed-host NAME to add it"),
                    head_only=head_only,
                )
                store.close()
                return
            try:
                split = urllib.parse.urlsplit(self.path)
                path = split.path
                query = {
                    k: v[0]
                    for k, v in urllib.parse.parse_qs(split.query, keep_blank_values=True).items()
                }

                is_get_like = self.command in ("GET", "HEAD")

                if is_get_like and path == "/":
                    self._serve_index(head_only=head_only)
                    return
                if is_get_like and path.startswith("/static/"):
                    self._serve_static(path[len("/static/") :], head_only=head_only)
                    return

                handler = None
                if is_get_like:
                    handler = get_routes.get(path)
                    patterns = get_patterns
                elif self.command == "POST":
                    handler = post_routes.get(path)
                    patterns = post_patterns
                else:
                    patterns = ()

                if handler is None:
                    for pattern, candidate in patterns:
                        match = pattern.match(path)
                        if match:
                            handler = candidate
                            query = {**query, "id": urllib.parse.unquote(match.group(1))}
                            break

                if handler is None:
                    self._write_json(*_not_found("route not found"), head_only=head_only)
                    return

                result = handler(store, query, body)
                if isinstance(result, tuple) and len(result) == 3 and result[0] == "raw":
                    _tag, content_type, text = result
                    self._write_text(200, content_type, text, head_only=head_only)
                    return
                status, payload = result
                self._write_json(status, payload, head_only=head_only)
            except Exception as exc:  # noqa: BLE001 - last-resort 500, see docs/api.md
                self._write_json(
                    *_internal_error(f"unexpected error ({type(exc).__name__})"), head_only=head_only
                )
            finally:
                # nit 30: ThreadingHTTPServer hands each request its own
                # thread, and Store keeps one sqlite3 connection per
                # thread (threading.local) -- without this, that
                # connection is only ever reclaimed when the thread
                # object itself is garbage collected, letting open
                # connections/file descriptors pile up under sustained
                # traffic instead of being released as soon as the
                # request that opened them finishes.
                store.close()

        def _method_not_allowed(self) -> None:
            # Finding 9: PUT/DELETE/PATCH/OPTIONS previously fell through
            # to BaseHTTPRequestHandler's own default 501 handler, which
            # never runs through _write_json -- so it carried none of
            # this API's security headers or {"ok": false, ...} envelope.
            self._write_json(*_error(405, "method_not_allowed", f"{self.command} is not supported on this route"))

        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            self._dispatch(None)

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib method name
            self._dispatch(None, head_only=True)

        def do_PUT(self) -> None:  # noqa: N802 - stdlib method name
            self._method_not_allowed()

        def do_DELETE(self) -> None:  # noqa: N802 - stdlib method name
            self._method_not_allowed()

        def do_PATCH(self) -> None:  # noqa: N802 - stdlib method name
            self._method_not_allowed()

        def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib method name
            self._method_not_allowed()

        def _reject_cross_site_post(self) -> str | None:
            """Same-origin guard for every mutating route (review S3):
            both POST routes this server exposes (``/api/profiles``,
            ``/api/sessions/<id>/tags``) are, without this check, a
            preflight-free "simple" request a cross-site page can issue
            blind -- the response is opaque to it (no CORS headers are
            ever sent), but a written profile is exactly what ``apply``
            later reads back. Returns ``None`` to allow the request, or a
            human-readable reason for the 403 otherwise.

            ``Origin`` is present on every fetch/XHR POST a browser
            issues (cross-site or not) and is compared against this
            server's own ``Host`` header -- itself always
            ``options.bind:options.port`` since nothing here handles TLS,
            so a straight ``http://`` comparison is exact. Older browsers
            that omit ``Origin`` on a same-origin POST are still covered
            by the ``Sec-Fetch-Site`` check below (sent by every current
            browser); a request with neither header (e.g. a same-machine
            CLI tool) is allowed, matching this API's existing no-auth,
            localhost-only posture (``docs/api.md``).
            """
            origin = self.headers.get("Origin")
            if origin is not None:
                host = self.headers.get("Host")
                if host is None or origin != f"http://{host}":
                    return "cross-origin requests are not allowed on this route"
            sec_fetch_site = self.headers.get("Sec-Fetch-Site")
            if sec_fetch_site is not None and sec_fetch_site not in ("same-origin", "none"):
                return "cross-site requests are not allowed on this route"
            return None

        def do_POST(self) -> None:  # noqa: N802 - stdlib method name
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            # Read (and discard, on rejection) the body unconditionally,
            # before any check that might return early -- this is an
            # HTTP/1.1 keep-alive connection, and leaving unread bytes in
            # the socket would corrupt the next request on the same
            # connection.
            raw = self.rfile.read(length) if length > 0 else b""

            reason = self._reject_cross_site_post()
            if reason is None and not self._host_allowed():
                reason = "this Host is not allowed; start serve with --allowed-host NAME to add it"
            if reason is not None:
                self._write_json(*_forbidden(reason))
                return

            content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self._write_json(*_bad_request("Content-Type must be application/json"))
                return

            body: dict | None = None
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._write_json(*_bad_request("request body must be valid JSON"))
                    return
            self._dispatch(body)

    return Handler


__all__ = ["make_handler"]
