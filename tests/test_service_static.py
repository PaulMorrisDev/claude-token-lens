"""Static web UI asset tests (work package S1-ui).

Three things are checked here:

1. Egress/safety of the three ``service/static/`` files: they exist,
   ``index.html`` references only its own two sibling assets, and none
   of the three contains a bare ``http(s)://`` literal, an inline
   ``<script>`` body, an ``on<event>=`` handler attribute, a dynamic
   ``import(``/``eval(`` call, an ``@import url(http...)``, or an emoji
   code point -- the same "no external reference, no inline execution"
   posture ``SECURITY.md`` and ``docs/ui.md`` require of this UI.
2. ``app.js`` actually calls every documented ``GET`` route in
   ``docs/api.md`` (minus the two routes this test deliberately
   excludes -- see ``_EXCLUDED_ROUTE_PREFIXES``), and ``service/static/*``
   is registered as package data in ``pyproject.toml``.
3. A tiny, test-only fixture HTTP server -- there is no ``api.py`` yet
   for a real end-to-end run against -- serves the static directory
   plus canned JSON for every ``/api/*`` route, built from
   ``tests/helpers`` builders through ``report.build_report`` and
   ``render/json_out``, plus a seeded ``Store``. ``urllib.request``
   fetches ``/`` and each static file and asserts a 200 status and the
   expected content type.
"""

from __future__ import annotations

import http.server
import json
import re
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from claude_token_lens.config import Config
from claude_token_lens.corpus import load_corpus
from claude_token_lens.pricing import load_pricing
from claude_token_lens.report import build_report
from claude_token_lens.render.json_out import render_json, to_jsonable
from claude_token_lens.service.store import Store
from claude_token_lens.snapshots import Snapshot

from helpers import turn_line, write_jsonl

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / "src" / "claude_token_lens" / "service" / "static"
API_MD = REPO_ROOT / "docs" / "api.md"
PYPROJECT_TOML = REPO_ROOT / "pyproject.toml"

STATIC_FILES = ("index.html", "app.js", "app.css")

#: Documented GET routes this test does not require app.js to fetch:
#: the profile-diff route is only ever reached from a click handler
#: (its id is dynamic, so there is no static literal to grep for), and
#: report.md/report.html are alternative renderings of report.json the
#: UI has no reason to also fetch.
_EXCLUDED_ROUTE_PREFIXES = (
    "/api/profiles/<id>/diff",
    "/api/report.md",
    "/api/report.html",
)

_FORBIDDEN_SUBSTRING_PATTERNS = {
    "bare http(s):// literal": re.compile(r"https?://"),
    "on<event>= handler attribute": re.compile(r"\bon[a-z]+\s*=", re.IGNORECASE),
    "dynamic import(...)": re.compile(r"\bimport\s*\("),
    "eval(...)": re.compile(r"\beval\s*\("),
    "@import url(http...)": re.compile(r"@import\s+url\(\s*['\"]?https?:", re.IGNORECASE),
}

#: Emoji/decorative-symbol code point ranges. Deliberately excludes the
#: em/en dash, ellipsis and similar typographic punctuation the UI does
#: use -- those are not emoji.
_EMOJI_RANGES = (
    (0x1F1E6, 0x1FAFF),  # regional indicators through symbols/pictographs
    (0x2600, 0x27BF),  # misc symbols and dingbats
    (0x2B00, 0x2BFF),  # misc symbols and arrows
    (0x2190, 0x21FF),  # arrows
    (0x200D, 0x200D),  # zero-width joiner (emoji sequences)
    (0xFE0F, 0xFE0F),  # variation selector-16 (emoji presentation)
)


def _is_emoji_code_point(code_point: int) -> bool:
    return any(low <= code_point <= high for low, high in _EMOJI_RANGES)


def _static_text(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


# -- deliverable 1: file existence / index.html reference restriction ----


def test_all_three_static_files_exist() -> None:
    for name in STATIC_FILES:
        path = STATIC_DIR / name
        assert path.is_file(), f"missing {path}"


def test_index_html_references_only_its_own_static_assets() -> None:
    html = _static_text("index.html")
    hrefs = re.findall(r'href="([^"]+)"', html)
    srcs = re.findall(r'src="([^"]+)"', html)
    referenced = set(hrefs) | set(srcs)
    assert referenced == {"/static/app.css", "/static/app.js"}, referenced


# -- deliverable 1: forbidden-substring / no-emoji scans -----------------


@pytest.mark.parametrize("name", STATIC_FILES)
def test_no_forbidden_substrings(name: str) -> None:
    text = _static_text(name)
    for label, pattern in _FORBIDDEN_SUBSTRING_PATTERNS.items():
        matches = pattern.findall(text)
        assert not matches, f"{name} contains forbidden pattern ({label}): {matches!r}"


@pytest.mark.parametrize("name", STATIC_FILES)
def test_no_inline_script_bodies(name: str) -> None:
    """Every ``<script ...>`` tag in the three files must carry a
    ``src=`` attribute -- i.e. it loads an external (same-origin) file
    rather than running an inline body, per the CSP's ``script-src
    'self'`` and the brief's "no inline <script>" constraint."""
    text = _static_text(name)
    for tag in re.findall(r"<script\b[^>]*>", text, flags=re.IGNORECASE):
        assert "src=" in tag.lower(), f"{name} has a <script> tag without src=: {tag!r}"


@pytest.mark.parametrize("name", STATIC_FILES)
def test_no_emoji_code_points(name: str) -> None:
    text = _static_text(name)
    offenders = [ch for ch in text if _is_emoji_code_point(ord(ch))]
    assert not offenders, f"{name} contains emoji-range code point(s): {[hex(ord(c)) for c in offenders]}"


# -- deliverable 1: app.js sanity + package-data registration -----------


def test_app_js_has_balanced_braces() -> None:
    text = _static_text("app.js")
    # Braces inside string/regex literals or comments could in principle
    # throw this simple counter off, but a genuinely broken brace count
    # is exactly the failure mode this smoke check exists to catch, and
    # `node --check` (run manually during development, not a repo
    # dependency here) already validates full syntax.
    assert text.count("{") == text.count("}"), "app.js has unbalanced { }"
    assert text.count("(") == text.count(")"), "app.js has unbalanced ( )"
    assert text.count("[") == text.count("]"), "app.js has unbalanced [ ]"


def test_pyproject_declares_static_as_package_data() -> None:
    data = tomllib.loads(PYPROJECT_TOML.read_text(encoding="utf-8"))
    package_data = data["tool"]["setuptools"]["package-data"]["claude_token_lens"]
    assert any("service/static" in entry for entry in package_data), package_data


def _documented_get_routes() -> list[str]:
    text = API_MD.read_text(encoding="utf-8")
    # Only the route headings are the contract; prose may mention a
    # route family in shorthand (e.g. "`GET /api/report.*`").
    routes = re.findall(r"^### `GET (/api/[^`]+)`", text, flags=re.M)
    assert routes, "no GET routes found in docs/api.md -- has its format changed?"
    return [r for r in routes if r not in _EXCLUDED_ROUTE_PREFIXES]


def test_every_documented_get_route_is_fetched_by_app_js() -> None:
    app_js = _static_text("app.js")
    routes = _documented_get_routes()
    assert routes  # sanity: the exclusion list didn't eat everything
    for route in routes:
        # Routes with a path parameter (e.g. "/api/session/<id>") are
        # built up via string concatenation in app.js, not present
        # verbatim -- match on the literal prefix before "<" instead.
        prefix = route.split("<")[0]
        assert prefix in app_js, f"app.js never fetches documented route {route!r} (looked for prefix {prefix!r})"


# -- deliverable 3: fixture HTTP server -----------------------------------

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    """Serves the static directory plus canned ``/api/*`` JSON built by
    ``_build_fixture_data`` -- everything precomputed on the main thread
    before the server starts, so request handling never touches the
    ``Store`` (and its per-thread ``:memory:`` connections) at all."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass  # keep test output quiet

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/":
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
            return
        if path.startswith("/api/session/"):
            session_id = path[len("/api/session/") :]
            data = self.server.fixture_sessions.get(session_id)  # type: ignore[attr-defined]
            if data is None:
                self._send_json(404, {"ok": False, "error": {"code": "not_found", "message": "session not found"}})
            else:
                self._send_json(200, {"ok": True, "data": data})
            return
        if path.startswith("/api/profiles/") and path.endswith("/diff"):
            self._send_json(501, {"ok": False, "error": {"code": "not_implemented", "message": "profile diff ships in v0.3"}})
            return
        canned = self.server.fixture_canned.get(path)  # type: ignore[attr-defined]
        if canned is None:
            self._send_json(404, {"ok": False, "error": {"code": "not_found", "message": "not found"}})
            return
        if path == "/api/report.json":
            # Review finding 1 (blocking): docs/api.md deliberately keeps
            # report.json unwrapped ({"schema_version": ..., "report":
            # {...}}, no {"ok": ..., "data": ...} envelope) for CLI byte
            # parity -- serve it raw here too, matching api.py's real
            # route, instead of wrapping it like every other canned route.
            self._send_json(200, canned)
            return
        self._send_json(200, {"ok": True, "data": canned})

    def _serve_static(self, name: str) -> None:
        file_path = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in file_path.parents or not file_path.is_file():
            self._send_json(404, {"ok": False, "error": {"code": "not_found", "message": "no such file"}})
            return
        content_type = _CONTENT_TYPES.get(file_path.suffix, "application/octet-stream")
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _seed_store() -> Store:
    store = Store(":memory:")
    store.open()
    snapshot_id = store.upsert_snapshot(
        project_slug="proj-ui",
        project_root_path="proj-ui",
        ts="2026-09-18T12:00:00Z",
        schema_version=store.schema_version() or 1,
        digest_json=json.dumps({"billing": "api"}),
    )
    store.upsert_session(
        session_id="session-ui-1",
        project_slug="proj-ui",
        project_root_path="proj-ui",
        slug="proj-ui",
        first_ts="2026-09-18T12:00:00Z",
        last_ts="2026-09-18T13:00:00Z",
        span_s=3600.0,
        archetype="plan-high-implement-low",
        mode="agentic",
        mode_source="tool-signature",
        purpose="refactor",
        purpose_source="intent-signature",
        entrypoint="cli",
        billing_mode="subscription",
        snapshot_id=snapshot_id,
        profile_id="p1",
        total_cost=1.23,
        total_tokens=45000,
    )
    store.upsert_transcript(
        session_id="session-ui-1",
        path="fixture-top.jsonl",
        kind="top-level",
        agent_id=None,
        agent_type=None,
        spawn_depth=0,
        parent_agent_id=None,
        mtime_ns=1,
        size_bytes=2,
        parser_version=4,
        digest_json=json.dumps({"turns": 4}),
        turns_agg=[
            {
                "day": "2026-09-18",
                "model": "claude-sonnet-5",
                "turns": 4,
                "input_tokens": 400,
                "cache_creation_tokens": 4000,
                "cache_read_tokens": 2000,
                "output_tokens": 120,
                "thinking_tokens": 20,
                "cc_5m": 0,
                "cc_1h": 4000,
                "cost": 1.23,
            }
        ],
        recache_turns=[
            {
                "turn_index": 2,
                "signature": "full-expiry",
                "cache_creation_tokens": 4000,
                "preceding_primary": "HUMAN_TEXT",
                "gap_s": 400.0,
            }
        ],
        events=[{"kind": "compact_boundary", "subkind": None, "count": 1, "dropped_tokens_sum": 0, "duration_ms_sum": 0}],
        compactions=[
            {
                "ts": "2026-09-18T12:30:00Z",
                "pre_tokens": 150000,
                "post_tokens": 30000,
                "dropped_tokens": 120000,
                "trigger": "auto",
                "join_delta_s": 4.0,
            }
        ],
    )
    store.upsert_profile(profile_id="p1", name="ui-fixture-profile", toml_path="p1.toml")
    store.record_baseline(
        project_slug="proj-ui",
        project_root_path="proj-ui",
        window_start="2026-09-11T00:00:00Z",
        window_end="2026-09-18T00:00:00Z",
        archetype="plan-high-implement-low",
        digest_json=json.dumps({"sessions": 1}),
    )
    store.set_tag("session-ui-1", "purpose", "refactor-override")
    return store


def _build_fixture_data(tmp_path: Path) -> tuple[dict, dict]:
    """Build the canned ``/api/*`` payloads: store-backed routes from a
    seeded ``Store``, report-backed routes from a synthetic JSONL corpus
    run through ``report.build_report`` and ``render/json_out``."""
    store = _seed_store()
    sessions_map = {"session-ui-1": store.session("session-ui-1")}
    canned: dict = {
        "/api/health": {
            "status": "ok",
            "schema_version": store.schema_version(),
            "watcher": {"finished_at": "2026-09-19T00:00:00Z", "files_parsed": 1, "errors": 0},
        },
        "/api/summary": store.summary(),
        "/api/sessions": store.sessions(),
        "/api/recache": store.recache(),
        "/api/compactions": store.compactions(),
        "/api/profiles": store.profiles(),
        "/api/baseline": store.baselines(),
    }
    store.close()

    project_dir = tmp_path / "proj-ui"
    project_dir.mkdir()
    write_jsonl(
        project_dir / "session-ui-1.jsonl",
        [
            turn_line(
                input_tokens=100 + i,
                output_tokens=20 + i,
                cache_creation_input_tokens=1000,
                cache_read_input_tokens=500,
            )
            for i in range(4)
        ],
    )
    corpus = load_corpus([project_dir])
    pricing = load_pricing()
    config = Config()
    snapshots = [Snapshot(path="fixture-snapshot.json", ts="2026-09-18T12:00:00.000Z", data={"billing": "api"})]
    report = build_report(
        corpus,
        pricing,
        config,
        projects=("proj-ui",),
        window="last 7 days",
        phases=True,
        snapshots=snapshots,
    )

    canned["/api/report.json"] = json.loads(render_json(report))

    ttl_section = next((s for s in report.sections if s.key == "ttl"), None)
    canned["/api/ttl"] = to_jsonable(ttl_section) if ttl_section is not None else {"tables": []}

    config_section = next((s for s in report.sections if s.key == "config"), None)
    canned["/api/config-diff"] = to_jsonable(config_section) if config_section is not None else {"tables": []}

    canned["/api/recommendations"] = [to_jsonable(rec) for rec in report.recommendations]

    return canned, sessions_map


@pytest.fixture
def fixture_server(tmp_path: Path):
    canned, sessions_map = _build_fixture_data(tmp_path)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    server.fixture_canned = canned  # type: ignore[attr-defined]
    server.fixture_sessions = sessions_map  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(base_url: str, path: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(base_url + path, timeout=5) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def test_fixture_server_serves_index_at_root(fixture_server: str) -> None:
    status, content_type, body = _get(fixture_server, "/")
    assert status == 200
    assert content_type.startswith("text/html")
    assert b"claude-token-lens" in body


@pytest.mark.parametrize(
    ("name", "expected_type"),
    [
        ("app.js", "application/javascript"),
        ("app.css", "text/css"),
    ],
)
def test_fixture_server_serves_static_files(fixture_server: str, name: str, expected_type: str) -> None:
    status, content_type, body = _get(fixture_server, "/static/" + name)
    assert status == 200
    assert content_type.startswith(expected_type)
    assert len(body) > 0


def test_fixture_server_serves_every_canned_api_route(fixture_server: str) -> None:
    for route in _documented_get_routes():
        if "<id>" in route:
            continue  # exercised separately below with a real id
        status, content_type, body = _get(fixture_server, route)
        assert status == 200, f"{route} -> {status}"
        assert content_type.startswith("application/json")
        envelope = json.loads(body)
        if route == "/api/report.json":
            # Finding 1: report.json is the one route that is never
            # {"ok": ..., "data": ...} -- see
            # test_fixture_server_serves_report_json_unwrapped below.
            continue
        assert envelope["ok"] is True, f"{route} -> {envelope}"


def test_fixture_server_serves_report_json_unwrapped(fixture_server: str) -> None:
    """Regression test for review finding 1 (blocking): docs/api.md
    documents ``/api/report.json`` as the raw rendered document, kept
    unwrapped for CLI byte parity -- it must never gain an ``{"ok": ...,
    "data": ...}`` envelope the way every other ``/api/*`` route does.
    """
    status, content_type, body = _get(fixture_server, "/api/report.json")
    assert status == 200
    assert content_type.startswith("application/json")
    payload = json.loads(body)
    assert "schema_version" in payload
    assert "report" in payload
    assert "ok" not in payload
    assert "data" not in payload


def test_app_js_load_report_accepts_the_unwrapped_report_json_shape() -> None:
    """Regression test for review finding 1 (blocking): app.js's
    ``loadReport()`` used to gate success on ``body.ok !== true`` and
    only ever read the report out of ``body.data.report`` -- since the
    real ``/api/report.json`` response never sets ``body.ok`` (see
    ``test_fixture_server_serves_report_json_unwrapped`` above), every
    tab that calls ``loadReport()`` (Overview/Cache/TTL/Agents/Config/
    Usage/Diagnostics/Recommendations) treated a successful 200 response
    as a hard failure. This fails against the pre-fix source (which
    contains neither ``body.report`` nor an ``ok === false`` failure
    check) and passes once ``loadReport()`` accepts the unwrapped shape.
    """
    app_js = _static_text("app.js")
    start = app_js.index("function loadReport(")
    # Slice to the next top-level function declaration so the assertions
    # below are scoped to loadReport()'s own body, not a coincidental
    # match elsewhere in the file.
    end = app_js.index("\n  function ", start + 1)
    load_report_src = app_js[start:end]
    assert "body.report" in load_report_src, (
        "loadReport() must read the unwrapped report.json shape's `body.report` directly"
    )
    assert "body.ok !== true" not in load_report_src, (
        "loadReport() must not treat report.json's lack of `ok: true` as a failure"
    )
    assert "body.ok === false" in load_report_src, (
        "loadReport() should still treat an explicit `ok: false` body as a failure"
    )


def test_load_report_cache_is_keyed_by_the_selected_window() -> None:
    """Regression test for review finding 21 (should-fix): ``loadReport()``
    used to memoize a single ``state.reportPromise`` shared by every
    caller, regardless of which window was requested -- once Overview's
    window selector triggered one fetch, every tab (including Overview's
    own Scorecard/Totals) kept reading that same cached promise forever,
    silently showing one window's data no matter what the selector said.
    ``/api/report.json`` accepts a ``window_days`` query parameter
    (``docs/api.md``) and the fix caches per requested window instead of
    once globally. Fails against the pre-fix source (a single
    ``state.reportPromise`` field, and ``loadReport()`` taking no
    parameter and always fetching the bare ``/api/report.json`` URL).
    """
    app_js = _static_text("app.js")
    assert "reportPromise:" not in app_js, "the report cache must not be a single unkeyed promise"
    assert "reportPromises" in app_js, "the report cache should be keyed (e.g. by window_days)"

    start = app_js.index("function loadReport(")
    end = app_js.index("\n  function ", start + 1)
    load_report_src = app_js[start:end]
    assert "windowDays" in load_report_src, "loadReport() must accept the selected window"
    assert "window_days=" in load_report_src, "loadReport() must forward the window to /api/report.json"

    # renderOverview must actually pass the selected window through when
    # it (re)loads the report, and refetch it on a window change rather
    # than only refreshing the plain /api/summary cards.
    overview_start = app_js.index("function renderOverview(")
    overview_end = app_js.index("\n  function ", overview_start + 1)
    overview_src = app_js[overview_start:overview_end]
    assert "loadReport(select.value)" in overview_src or "loadReport(windowDays)" in overview_src
    change_listener_start = overview_src.index("addEventListener(\"change\"")
    change_listener_src = overview_src[change_listener_start:]
    assert "renderOverviewSummary" in change_listener_src
    assert "loadReport" in change_listener_src or "renderOverviewReportSections" in change_listener_src, (
        "the window-change handler must also refresh the report-backed Scorecard/Totals, not just the summary cards"
    )


def test_section_tab_map_includes_recache_by_group() -> None:
    """Regression test for review finding 20 (should-fix): docs/ui.md
    documents ``recache_by_group`` as mapping to the Cache tab alongside
    ``recache`` itself, but ``app.js``'s ``SECTION_TAB_MAP`` only listed
    ``recache`` -- a docs/code mismatch. Fails against the pre-fix
    source (no ``recache_by_group`` key in the map) and passes once it
    is added, mapped to the same ``"cache"`` tab.
    """
    app_js = _static_text("app.js")
    start = app_js.index("var SECTION_TAB_MAP")
    end = app_js.index("};", start) + 2
    section_tab_map_src = app_js[start:end]
    assert "recache_by_group" in section_tab_map_src
    assert re.search(r'recache_by_group\s*:\s*"cache"', section_tab_map_src), (
        "recache_by_group should map to the same Cache tab as recache"
    )


def test_render_baseline_shows_the_project_slug_not_the_raw_row_id() -> None:
    """Regression test for review nit 27: ``Store.baselines()`` was
    fixed to join in the owning project's redacted ``slug`` so a caller
    doesn't have to show the meaningless ``projects.id`` primary key --
    but ``renderBaseline`` in ``app.js`` kept reading ``row.project_id``
    for the "Project" column, so the store-side fix never reached the
    screen: the Baseline table still showed an opaque integer under a
    "Project" heading. Fails against the pre-fix source (``row.project_id``
    with no ``row.project_slug`` anywhere in the function) and passes
    once the column reads ``row.project_slug`` instead.
    """
    app_js = _static_text("app.js")
    start = app_js.index("function renderBaseline(")
    end = app_js.index("\n  function ", start + 1)
    render_baseline_src = app_js[start:end]
    assert "row.project_slug" in render_baseline_src, (
        "the Project column must render the joined, redacted project_slug"
    )
    assert "row.project_id" not in render_baseline_src, (
        "the Project column must not fall back to the opaque projects.id primary key"
    )


def test_app_js_timeline_never_uses_math_max_apply() -> None:
    """Regression test for review finding 10 (should-fix):
    ``Math.max.apply(null, array)`` spreads ``array`` as individual call
    arguments -- a session with tens of thousands of turns can exceed the
    engine's call-stack/argument-count limit. Fails against the pre-fix
    source (which used exactly this pattern in
    ``buildSessionTimeline``) and passes once it's replaced with a plain
    loop.
    """
    app_js = _static_text("app.js")
    assert "Math.max.apply" not in app_js
    assert ".apply(" not in app_js


def test_app_js_timeline_draws_a_circle_for_a_single_turn_session() -> None:
    """Regression test for review finding 11 (should-fix): a session with
    exactly one priced turn produces a single point, and an SVG
    ``<polyline>`` needs at least two points to render anything -- a
    single-turn session's chart silently rendered nothing at all. Fails
    against the pre-fix source (a single unconditional ``<polyline>``
    push, no ``points.length`` branch) and passes once
    ``buildSessionTimeline`` draws a ``<circle>`` for the one-point case.
    """
    app_js = _static_text("app.js")
    start = app_js.index("function buildSessionTimeline(")
    end = app_js.index("\n  function ", start + 1)
    timeline_src = app_js[start:end]
    assert "points.length === 1" in timeline_src or "points.length == 1" in timeline_src, (
        "buildSessionTimeline must special-case a single-point series"
    )
    assert "<circle" in timeline_src


def _split_top_level_args(args_str: str) -> list[str]:
    """Split a `loadInto(...)` argument string on top-level commas only,
    so a nested call like `encodeURIComponent(profile.id)` inside one
    argument doesn't get mistaken for an argument boundary."""
    parts = []
    depth = 0
    current = ""
    for ch in args_str:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return parts


def _loadinto_named_render_callbacks(app_js: str) -> list[str]:
    """Every bare identifier passed as `loadInto(container, url, <name>)`'s
    third argument -- i.e. render callbacks referenced by name, not the
    inline `function (data, container) {...}` literals `loadInto` is also
    called with."""
    names = []
    for match in re.finditer(r"loadInto\(", app_js):
        # Skip `function loadInto(container, url, render, options) {...}`
        # itself -- its own parameter list isn't a call site.
        if app_js[: match.start()].rstrip().endswith("function"):
            continue
        open_paren = match.end() - 1
        depth = 0
        i = open_paren
        while i < len(app_js):
            if app_js[i] == "(":
                depth += 1
            elif app_js[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        args = _split_top_level_args(app_js[open_paren + 1 : i])
        if len(args) >= 3:
            third = args[2].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", third):
                names.append(third)
    return names


def test_loadinto_render_callbacks_take_data_first_container_second() -> None:
    """Regression test: `loadInto(container, url, render)` (app.js's
    fetch-and-render helper) always invokes its third argument as
    `render(data, container)` -- data first, container second. Every
    named render callback it's called with must declare its parameters
    in that same order.

    This catches the bug where `renderSummaryCards` declared
    `(container, summary)` -- the reverse of what `loadInto` actually
    passes -- so the Overview tab's summary cards received the summary
    object where `container` was expected and blew up with
    `container.appendChild is not a function`. Fails against the
    pre-fix source (`renderSummaryCards(container, summary)`) and
    passes once the parameter order matches every other callback.
    """
    app_js = _static_text("app.js")
    names = _loadinto_named_render_callbacks(app_js)
    assert names, "no named render callbacks found -- has loadInto's call pattern changed?"

    bad_first_params = {"container", "target", "panel", "el"}
    good_second_params = {"container", "target"}
    for name in names:
        match = re.search(r"function\s+" + re.escape(name) + r"\s*\(([^)]*)\)", app_js)
        assert match, f"no declaration found for render callback {name!r}"
        params = [p.strip() for p in match.group(1).split(",")]
        assert len(params) >= 2, f"{name}({', '.join(params)}) declares fewer than 2 parameters"
        assert params[0] not in bad_first_params, (
            f"{name}'s first parameter is {params[0]!r} -- loadInto calls render(data, container), "
            f"so the first parameter must be the data argument, not the container"
        )
        assert params[1] in good_second_params, (
            f"{name}'s second parameter is {params[1]!r}, expected one of {sorted(good_second_params)}"
        )


def test_render_health_shows_a_logon_banner_when_service_not_registered() -> None:
    """v3: ``/api/health``'s ``service_registered`` field (see
    ``docs/api.md``) drives a warning banner in the Overview tab's
    "Service health" panel -- someone who skipped ``install-service``
    (or whose registration was later removed) needs to see this in the
    UI, not just find it by reading a JSON field. Regression-style
    source check rather than a DOM test, matching this file's other
    ``renderHealth``/``renderBaseline``-style assertions -- there is no
    browser in this test process.
    """
    app_js = _static_text("app.js")
    start = app_js.index("function renderHealth(")
    end = app_js.index("\n  function ", start + 1)
    render_health_src = app_js[start:end]

    assert "service_registered" in render_health_src, "renderHealth never reads health.service_registered"
    assert "=== false" in render_health_src, "the banner must be conditional on service_registered === false"
    assert "install-service" in render_health_src, "the banner text must tell the operator what command to run"
    assert "cleanupPeriodDays" in render_health_src, "the banner must explain why registration matters (retention)"


def test_fixture_server_serves_session_detail(fixture_server: str) -> None:
    status, content_type, body = _get(fixture_server, "/api/session/session-ui-1")
    assert status == 200
    assert content_type.startswith("application/json")
    envelope = json.loads(body)
    assert envelope["ok"] is True
    assert envelope["data"]["id"] == "session-ui-1"
    assert envelope["data"]["tags"] == {"purpose": "refactor-override"}


def test_fixture_server_404s_unknown_session(fixture_server: str) -> None:
    status, _content_type, body = _get(fixture_server, "/api/session/does-not-exist")
    assert status == 404
    envelope = json.loads(body)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "not_found"
