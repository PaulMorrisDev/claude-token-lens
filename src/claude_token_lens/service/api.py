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
  ``transcripts.digest_json`` rows via ``service.rebuild.corpus_from_store``
  (a sibling package's module -- imported lazily, inside the function
  that needs it, per this work package's brief, so this module still
  imports cleanly before that module exists) and run it through the same
  :func:`~claude_token_lens.report.build_report`/
  :func:`~claude_token_lens.recommend.recommend` pipeline the CLI's
  ``report`` subcommand uses, then a renderer. Rebuilt once per
  ``(window_days, store-change-token)`` pair and cached in-process (see
  ``_ReportCache``) so switching UI tabs (``docs/ui.md``) never re-parses
  the whole store for the same window.

Contract notes / deviations (reported here rather than silently, per this
project's convention -- see e.g. ``report.py``'s own module docstring):

- ``GET /api/session/<id>`` returns ``Store.session()``'s dict verbatim,
  which includes ``mode_source``/``purpose_source`` alongside the fields
  ``docs/api.md`` lists for ``/api/sessions``, plus (S1-integration fix
  1.g) ``turn_series``/``markers`` from ``Store.turns_for_session``. This
  is a superset, not a contradiction -- ``docs/api.md`` describes it as
  "the session-summary fields above, plus transcripts ... and tags", not
  an exact field count, and dropping fields ``Store`` already computes
  for no privacy reason would only lose information a client might want.
- ``GET/POST /api/profiles/<id>/diff`` and ``POST /api/profiles`` always
  return ``501 not_implemented`` -- v0.3's ``profiles/schema.py`` (the
  profile-file validator both routes depend on) does not exist yet. The
  envelope/validation plumbing (body-shape checks) still runs before the
  501 is returned, so the route is easy to finish once that module
  lands: swap the final ``_not_implemented(...)`` for the real read/write.
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
import json
import mimetypes
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..config import ConfigError, load_config, load_session_overrides
from ..pricing import load_pricing
from ..render.html import render_html
from ..render.json_out import render_json, to_jsonable
from ..render.markdown import render_markdown
from ..report import build_report
from ..snapshots import Snapshot
from .contracts import ApiError, ServeOptions, WatcherStats

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

_PLACEHOLDER_INDEX_HTML = (
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>claude-token-lens</title>"
    "</head><body>UI not built yet.</body></html>"
)

_SESSION_ID_RE = re.compile(r"^/api/session/([^/]+)$")
_SESSION_TAGS_RE = re.compile(r"^/api/sessions/([^/]+)/tags$")
_PROFILE_DIFF_RE = re.compile(r"^/api/profiles/([^/]+)/diff$")


# -- envelope helpers ---------------------------------------------------


def _ok(data) -> tuple[int, dict]:
    return 200, {"ok": True, "data": data}


def _error(status: int, code: str, message: str) -> tuple[int, dict]:
    return status, ApiError(status=status, code=code, message=message).to_envelope()


def _not_found(message: str = "not found") -> tuple[int, dict]:
    return _error(404, "not_found", message)


def _bad_request(message: str) -> tuple[int, dict]:
    return _error(400, "bad_request", message)


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


def _find_section(model, key: str):
    for section in model.sections:
        if section.key == key:
            return section
    return None


# -- make_handler ---------------------------------------------------------


def make_handler(
    store: "Store",
    options: ServeOptions,
    *,
    watcher_stats: Callable[[], WatcherStats] | None = None,
    static_dir: Path | None = None,
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
    reports an all-zero :class:`WatcherStats`.

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
    report_cache: dict = {"token": None, "models": {}}

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
            data["project_slug"] = row.get("project_slug")
            out.append(Snapshot(path=Path(""), ts=row["ts"], data=data))
        out.sort(key=lambda s: s.ts)
        return out

    def _build_report_model(window_days: int | None):
        # Local import: service.rebuild is a sibling work package's
        # module (S1-watcher), not yet present in every checkout this
        # module is imported from -- see this module's docstring.
        from . import rebuild

        config = load_config(options.config_dir)
        rates = load_pricing(path=config.pricing_path, config_dir=options.config_dir)
        corpus = rebuild.corpus_from_store(store, days=window_days)
        snaps = _snapshots_from_store()
        projects = tuple(sorted({bundle.slug for bundle in corpus.sessions if bundle.slug}))
        window = f"last {window_days} days" if window_days else "all time"
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
        return build_report(
            corpus,
            rates,
            config,
            projects=projects,
            window=window,
            snapshots=snaps or None,
            session_overrides=overrides,
        )

    def _get_report_model(window_days: int | None):
        token = store.change_token()
        with report_lock:
            if report_cache["token"] != token:
                report_cache["token"] = token
                report_cache["models"] = {}
            cached = report_cache["models"].get(window_days)
        if cached is not None:
            return cached
        model = _build_report_model(window_days)
        with report_lock:
            if report_cache["token"] == token:
                report_cache["models"][window_days] = model
        return model

    # -- store-backed routes ---------------------------------------------

    def route_health(store, query, body):
        stats = (watcher_stats() if watcher_stats is not None else None) or WatcherStats()
        data = {
            "status": "ok",
            "schema_version": store.schema_version() or 0,
            "watcher": to_jsonable(stats),
            # Finding 3: a transcript whose file has gone missing (past
            # Claude Code's own cleanupPeriodDays, or simply deleted) is
            # marked rather than removed -- surfacing the running total
            # here lets an operator notice a projects-root misconfiguration
            # (everything suddenly "missing") without it being silent.
            "transcripts_missing": store.count_missing_transcripts(),
        }
        return _ok(data)

    def route_summary(store, query, body):
        window_days, err = _int_query(query, "window_days", None, minimum=0)
        if err is not None:
            return err
        return _ok(store.summary(window_days=window_days))

    def route_sessions(store, query, body):
        limit, err = _int_query(query, "limit", 50, minimum=0)
        if err is not None:
            return err
        offset, err = _int_query(query, "offset", 0, minimum=0)
        if err is not None:
            return err
        return _ok(store.sessions(limit=limit, offset=offset))

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
        return _ok(result)

    def route_recache(store, query, body):
        return _ok(store.recache())

    def route_daily_usage(store, query, body):
        days, err = _int_query(query, "days", 30, minimum=1)
        if err is not None:
            return err
        return _ok(store.daily_usage(days=days))

    def route_compactions(store, query, body):
        return _ok(store.compactions())

    def route_profiles(store, query, body):
        return _ok(store.profiles())

    def route_baseline(store, query, body):
        latest: dict[object, dict] = {}
        for row in store.baselines():
            project_id = row["project_id"]
            current = latest.get(project_id)
            if current is None or row["created_at"] > current["created_at"]:
                latest[project_id] = row
        return _ok(list(latest.values()))

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

    # -- v0.3-dependent routes (plumbing now, 501 until profiles/schema.py) --

    def route_profile_diff(store, query, body):
        return _not_implemented(
            "profile diff rendering needs v0.3's profiles/schema.py, not yet available"
        )

    def route_profiles_post(store, query, body):
        if not isinstance(body, dict):
            return _bad_request("request body must be a JSON object")
        return _not_implemented(
            "profile creation needs v0.3's profiles/schema.py, not yet available"
        )

    # -- report-backed routes ---------------------------------------------

    def route_ttl(store, query, body):
        window_days, err = _int_query(query, "window_days", _DEFAULT_WINDOW_DAYS, minimum=1)
        if err is not None:
            return err
        model = _get_report_model(window_days)
        section = _find_section(model, "ttl")
        return _ok(to_jsonable(section) if section is not None else None)

    def route_config_diff(store, query, body):
        window_days, err = _int_query(query, "window_days", _DEFAULT_WINDOW_DAYS, minimum=1)
        if err is not None:
            return err
        key = query.get("key")
        auto_keys = query.get("auto_keys") == "1"
        if not key and not auto_keys:
            return _bad_request("provide 'key' or 'auto_keys=1'")
        model = _get_report_model(window_days)
        section = _find_section(model, "config")
        tables = section.tables if section is not None else []
        if auto_keys:
            return _ok([to_jsonable(table) for table in tables])
        table = next((t for t in tables if t.name == f"config-diff-{key}"), None)
        return _ok(to_jsonable(table) if table is not None else [])

    def route_recommendations(store, query, body):
        window_days, err = _int_query(query, "window_days", _DEFAULT_WINDOW_DAYS, minimum=1)
        if err is not None:
            return err
        model = _get_report_model(window_days)
        return _ok([to_jsonable(rec) for rec in model.recommendations])

    def _render_report(content_type: str, render: Callable[[object], str]):
        def _route(store, query, body):
            window_days, err = _int_query(query, "window_days", _DEFAULT_WINDOW_DAYS, minimum=1)
            if err is not None:
                return err
            model = _get_report_model(window_days)
            return ("raw", content_type, render(model))

        return _route

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
        "/api/config-diff": route_config_diff,
        "/api/recommendations": route_recommendations,
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
        (_PROFILE_DIFF_RE, route_profile_diff),
    )
    post_routes: dict[str, Callable] = {
        "/api/profiles": route_profiles_post,
    }
    post_patterns: tuple[tuple[re.Pattern, Callable], ...] = (
        (_SESSION_TAGS_RE, route_set_tag),
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "claude-token-lens/0.2"
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

        def _dispatch(self, body: dict | None, *, head_only: bool = False) -> None:
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

        def do_POST(self) -> None:  # noqa: N802 - stdlib method name
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
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
