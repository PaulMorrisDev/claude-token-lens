"""End-to-end tests for ``service.api``'s ``make_handler()`` (S1-api):
spin up a real ``ThreadingHTTPServer`` against a temp SQLite ``Store``
seeded via ``Store``'s own writers (mirroring
``tests/test_service_store.py``'s ``_seed`` convention) and a real
:class:`~claude_token_lens.corpus.Corpus` built the same way
``tests/test_cli.py``'s ``_write_project``/``corpus.load_corpus`` do,
then exercise every ``/api/*`` route ``docs/api.md`` documents.

``service.rebuild`` (S1-watcher's concurrently-written module) does not
exist in every checkout this suite runs from -- ``service/api.py``'s
own module docstring documents importing it lazily, inside the
function that needs it, for exactly this reason. This file never
imports the real thing: :func:`_install_fake_rebuild` installs a
minimal stand-in ``claude_token_lens.service.rebuild`` module (both in
``sys.modules`` and as a ``claude_token_lens.service`` package
attribute, so ``api.py``'s ``from . import rebuild`` resolves it either
way) whose ``corpus_from_store`` simply returns the pre-built
``Corpus`` regardless of its ``days``/``since``/``until``/``window_by``
arguments -- sufficient for exercising every report-backed route and
proving the CLI-JSON byte-parity contract, without depending on
S1-watcher landing first.
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
import types
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from claude_token_lens import corpus as corpus_mod
from claude_token_lens.config import ConfigError, load_config, load_session_overrides
from claude_token_lens.pricing import load_pricing
from claude_token_lens.profiles import catalogue as profile_catalogue
from claude_token_lens.profiles import schema as profile_schema
from claude_token_lens.render.json_out import render_json
from claude_token_lens.report import build_report
from claude_token_lens.service import api as service_api
from claude_token_lens.service.contracts import ServeOptions
from claude_token_lens.service.store import Store
from claude_token_lens.snapshots import Snapshot

from helpers import assert_privacy, turn_line, write_jsonl

#: Distinctive fake local-only strings -- same convention
#: ``tests/test_service_store.py`` uses for its own path-leak guard. If
#: any of these ever surfaces in a response body, a route has forwarded
#: a store-local-only column (``transcripts.path``, ``projects.root_path``
#: or ``profiles.toml_path``) in violation of ``docs/api.md``'s privacy
#: section.
_FAKE_PATH = r"C:\Users\definitely-not-a-real-person\.claude\projects\proj-a\session-a.jsonl"
_FAKE_ROOT = r"C:\Users\definitely-not-a-real-person\.claude\projects\proj-a"
_FAKE_PROFILE_PATH = r"C:\Users\definitely-not-a-real-person\.claude\token-lens\profiles\p1.toml"
_LEAK_NEEDLES = (_FAKE_PATH, _FAKE_ROOT, _FAKE_PROFILE_PATH, "definitely-not-a-real-person")


def _build_corpus(tmp_path: Path) -> corpus_mod.Corpus:
    project_dir = tmp_path / "projects" / "proj-a"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "session-a.jsonl",
        [
            turn_line(input_tokens=100 + i, output_tokens=20 + i, cache_read_input_tokens=30)
            for i in range(3)
        ],
    )
    return corpus_mod.load_corpus([project_dir])


def _seed_store(store: Store, corpus: corpus_mod.Corpus) -> str:
    """Seed the store with rows for ``corpus``'s one session, following
    ``test_service_store.py``'s ``_seed`` shape. Returns the session id.
    """
    bundle = corpus.sessions[0]
    snapshot_id = store.upsert_snapshot(
        project_slug="proj-a",
        project_root_path=_FAKE_ROOT,
        ts="2026-09-18T12:00:00Z",
        schema_version=1,
        digest_json=json.dumps({"agents": {}}),
    )
    store.upsert_session(
        session_id=bundle.session_id,
        project_slug="proj-a",
        project_root_path=_FAKE_ROOT,
        slug="proj-a",
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
        total_tokens=450,
    )
    store.upsert_transcript(
        session_id=bundle.session_id,
        path=_FAKE_PATH,
        kind="top-level",
        mtime_ns=123,
        size_bytes=456,
        parser_version=3,
        digest_json=json.dumps({"turns": 3}),
        turns_agg=[
            {
                "day": "2026-09-18",
                "model": "claude-sonnet-5",
                "turns": 3,
                "input_tokens": 300,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 90,
                "output_tokens": 63,
                "thinking_tokens": 0,
                "cc_5m": 0,
                "cc_1h": 0,
                "cost": 1.23,
            }
        ],
        recache_turns=[
            {
                "turn_index": 1,
                "signature": "full-expiry",
                "cache_creation_tokens": 500,
                "preceding_primary": "HUMAN_TEXT",
                "gap_s": 400.0,
            }
        ],
        events=[{"kind": "compact_boundary", "subkind": None, "count": 1, "dropped_tokens_sum": 0, "duration_ms_sum": 0}],
        compactions=[
            {
                "ts": "2026-09-18T12:30:00Z",
                "pre_tokens": 1000,
                "post_tokens": 200,
                "dropped_tokens": 800,
                "trigger": "auto",
                "join_delta_s": 5.0,
            }
        ],
    )
    store.upsert_profile(profile_id="p1", name="implementation-heavy", toml_path=_FAKE_PROFILE_PATH)
    store.record_baseline(
        project_slug="proj-a",
        project_root_path=_FAKE_ROOT,
        window_start="2026-09-11T00:00:00Z",
        window_end="2026-09-18T00:00:00Z",
        archetype="plan-high-implement-low",
        digest_json=json.dumps({"sessions": 1, "suggested_profile": "implementation-heavy"}),
    )
    store.set_tag(bundle.session_id, "purpose", "refactor-override")
    return bundle.session_id


def _install_fake_rebuild(monkeypatch, corpus: corpus_mod.Corpus) -> None:
    import claude_token_lens.service as service_pkg

    fake = types.ModuleType("claude_token_lens.service.rebuild")

    def corpus_from_store(store, *, days=None, since=None, until=None, window_by="mtime"):
        return corpus

    fake.corpus_from_store = corpus_from_store
    # Both forms so `from . import rebuild` resolves it regardless of
    # whether Python's import machinery checks the package attribute or
    # sys.modules first -- see this module's own docstring.
    monkeypatch.setitem(sys.modules, "claude_token_lens.service.rebuild", fake)
    monkeypatch.setattr(service_pkg, "rebuild", fake, raising=False)


class _ServerHandle:
    """Thin wrapper around a running ``ThreadingHTTPServer`` plus the
    fixtures behind it, for tests to issue requests against."""

    def __init__(self, server: ThreadingHTTPServer, thread: threading.Thread, *, corpus, store, options):
        self.server = server
        self.thread = thread
        self.corpus = corpus
        self.store = store
        self.options = options

    @property
    def port(self) -> int:
        return self.server.server_port

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
    ):
        """``headers`` overrides/extends the default ``Content-Type``
        this method sends whenever ``body`` is given -- used by the
        review-S3 same-origin tests to send ``Origin``/``Sec-Fetch-Site``
        or a deliberately wrong ``Content-Type``. ``raw_body``, when
        given, is sent verbatim instead of JSON-encoding ``body`` (also
        S3: a non-JSON payload with a spoofed ``Content-Type``)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            sent_headers: dict[str, str] = {}
            payload = raw_body
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                sent_headers["Content-Type"] = "application/json"
            if headers:
                sent_headers.update(headers)
            conn.request(method, path, body=payload, headers=sent_headers)
            resp = conn.getresponse()
            raw = resp.read()
            return resp, raw
        finally:
            conn.close()

    def get_json(self, path: str):
        resp, raw = self.request("GET", path)
        return resp, json.loads(raw)

    def post_json(self, path: str, body: dict):
        resp, raw = self.request("POST", path, body=body)
        return resp, json.loads(raw)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _start_server(tmp_path, monkeypatch, **handler_kwargs) -> _ServerHandle:
    """Shared setup behind the ``server`` fixture below -- factored out
    so a test that needs a non-default ``make_handler`` keyword (e.g.
    v3's ``service_registered``) can build its own handle without
    duplicating this whole sequence.
    """
    corpus = _build_corpus(tmp_path)
    _install_fake_rebuild(monkeypatch, corpus)

    store = Store(tmp_path / "service.db")
    store.open()
    session_id = _seed_store(store, corpus)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    options = ServeOptions(projects_root=tmp_path / "projects", config_dir=config_dir)

    handler_cls = service_api.make_handler(store, options, **handler_kwargs)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    handle = _ServerHandle(httpd, thread, corpus=corpus, store=store, options=options)
    handle.session_id = session_id
    return handle


@pytest.fixture
def server(tmp_path, monkeypatch):
    handle = _start_server(tmp_path, monkeypatch)
    try:
        yield handle
    finally:
        handle.close()
        handle.store.close()


def _assert_no_leak(raw: bytes) -> None:
    text = raw.decode("utf-8")
    for needle in _LEAK_NEEDLES:
        assert needle not in text, f"{needle!r} leaked into response: {text[:500]}"


# -- envelope / headers ---------------------------------------------------


def test_every_response_has_security_headers_and_content_type(server):
    resp, _raw = server.request("GET", "/api/health")
    assert resp.getheader("Cache-Control") == "no-store"
    assert resp.getheader("X-Content-Type-Options") == "nosniff"
    assert resp.getheader("Content-Security-Policy")
    assert resp.getheader("Content-Type") == "application/json"


def test_unknown_route_is_404_not_found(server):
    resp, body = server.get_json("/api/does-not-exist")
    assert resp.status == 404
    assert body == {"ok": False, "error": {"code": "not_found", "message": body["error"]["message"]}}
    assert_privacy(body)


# -- finding 9: HEAD/PUT/DELETE/PATCH/OPTIONS --------------------------------


def test_head_health_matches_get_headers_with_no_body(server):
    resp, raw = server.request("HEAD", "/api/health")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "application/json"
    assert resp.getheader("Cache-Control") == "no-store"
    assert resp.getheader("X-Content-Type-Options") == "nosniff"
    assert raw == b""


def test_head_report_json_returns_the_unwrapped_routes_headers_with_no_body(server):
    # report.json is the one route with its own content type/envelope
    # rules (finding 1) -- confirm HEAD threads head_only through that
    # path too, not just the generic envelope one above.
    resp, raw = server.request("HEAD", "/api/report.json")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "application/json"
    assert raw == b""


def test_head_static_index_returns_no_body(server):
    resp, raw = server.request("HEAD", "/")
    assert resp.status == 200
    assert resp.getheader("Content-Type", "").startswith("text/html")
    assert raw == b""


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS"])
def test_unsupported_methods_return_405_with_the_usual_envelope(server, method):
    resp, raw = server.request(method, "/api/health")
    assert resp.status == 405
    assert resp.getheader("X-Content-Type-Options") == "nosniff"
    body = json.loads(raw)
    assert body == {
        "ok": False,
        "error": {"code": "method_not_allowed", "message": f"{method} is not supported on this route"},
    }
    assert_privacy(body)


# -- store-backed routes ----------------------------------------------------


def test_health(server):
    resp, body = server.get_json("/api/health")
    assert resp.status == 200
    assert body["ok"] is True
    assert body["data"]["status"] == "ok"
    assert body["data"]["schema_version"] >= 1
    assert "watcher" in body["data"]
    # v3: no `service_registered` probe was wired up (the `server`
    # fixture calls make_handler with no extra kwargs), so this must be
    # null ("unknown"), never folded into false.
    assert body["data"]["service_registered"] is None
    assert_privacy(body)
    _assert_no_leak(json.dumps(body).encode("utf-8"))


@pytest.mark.parametrize("registered_value", [True, False])
def test_health_reports_service_registered_when_a_probe_is_wired_up(tmp_path, monkeypatch, registered_value):
    handle = _start_server(tmp_path, monkeypatch, service_registered=lambda: registered_value)
    try:
        resp, body = handle.get_json("/api/health")
        assert resp.status == 200
        assert body["data"]["service_registered"] is registered_value
        # v3: the field is a bare boolean -- confirm a wired-up (non-null)
        # probe result still can't smuggle a path/command string into the
        # response (see installer.py's module docstring on why api.py
        # never imports it directly).
        assert_privacy(body)
    finally:
        handle.close()
        handle.store.close()


def test_health_caches_the_service_registered_probe_for_ten_minutes(tmp_path, monkeypatch):
    calls = []

    def probe():
        calls.append(1)
        return True

    fake_time = {"now": 1_000.0}
    monkeypatch.setattr(service_api.time, "monotonic", lambda: fake_time["now"])

    handle = _start_server(tmp_path, monkeypatch, service_registered=probe)
    try:
        handle.get_json("/api/health")
        handle.get_json("/api/health")
        assert len(calls) == 1  # second request within the TTL is served from cache

        fake_time["now"] += service_api._SERVICE_REGISTERED_CACHE_TTL_S + 1
        handle.get_json("/api/health")
        assert len(calls) == 2  # cache expired -- probed again
    finally:
        handle.close()
        handle.store.close()


def test_summary(server):
    resp, body = server.get_json("/api/summary")
    assert resp.status == 200
    assert body["data"]["sessions"] == 1
    assert body["data"]["transcripts"] == 1
    assert_privacy(body)


def test_summary_rejects_bad_window_days(server):
    resp, body = server.get_json("/api/summary?window_days=not-a-number")
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"


def test_sessions_listing_has_no_transcripts_key(server):
    resp, body = server.get_json("/api/sessions")
    assert resp.status == 200
    assert len(body["data"]) == 1
    assert "transcripts" not in body["data"][0]
    assert_privacy(body)


def test_sessions_rejects_bad_limit(server):
    resp, body = server.get_json("/api/sessions?limit=-1")
    assert resp.status == 400


def test_session_detail(server):
    resp, body = server.get_json(f"/api/session/{server.session_id}")
    assert resp.status == 200
    assert body["data"]["id"] == server.session_id
    assert len(body["data"]["transcripts"]) == 1
    assert body["data"]["transcripts"][0]["kind"] == "top-level"
    assert "path" not in body["data"]["transcripts"][0]
    assert body["data"]["tags"] == {"purpose": "refactor-override"}
    assert_privacy(body)
    _assert_no_leak(json.dumps(body).encode("utf-8"))


def test_session_detail_has_no_turn_series_without_a_stored_digest(server):
    # _seed_store's transcript digest_json is the synthetic {"turns": 3}
    # shape (not a real encode_result payload), so Store.turns_for_session
    # can't decode it -- route_session must degrade gracefully and simply
    # omit turn_series/markers rather than 500 or fabricate empty lists.
    resp, body = server.get_json(f"/api/session/{server.session_id}")
    assert resp.status == 200
    assert "turn_series" not in body["data"]
    assert "markers" not in body["data"]


def test_session_detail_turn_series_and_markers(tmp_path, monkeypatch):
    # Deliverable 1.g: GET /api/session/<id> exposes turn_series/markers
    # sourced from the top-level transcript's stored digest.
    from claude_token_lens.cache import encode_result
    from claude_token_lens.model import EventKind, Turn, TranscriptMeta, TranscriptResult

    corpus = _build_corpus(tmp_path)
    _install_fake_rebuild(monkeypatch, corpus)

    store = Store(tmp_path / "service.db")
    store.open()
    session_id = corpus.sessions[0].session_id
    store.upsert_session(session_id=session_id, project_slug="proj-a", slug="proj-a")

    result = TranscriptResult(
        meta=TranscriptMeta(path=_FAKE_PATH, kind="top-level", session_id=session_id),
        turns=[
            Turn(turn_index=1, ctx=1000, cache_creation_tokens=500, is_recache=False,
                 preceding_primary=EventKind.HUMAN_TEXT, human_prompt_chars=42),
            Turn(turn_index=2, ctx=1500, cache_creation_tokens=0, is_recache=True,
                 preceding_primary=EventKind.COMPACT_BOUNDARY),
        ],
    )
    store.upsert_transcript(
        session_id=session_id,
        path=_FAKE_PATH,
        kind="top-level",
        digest_json=json.dumps(encode_result(result)),
    )

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    options = ServeOptions(projects_root=tmp_path / "projects", config_dir=config_dir)
    handler_cls = service_api.make_handler(store, options)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    handle = _ServerHandle(httpd, thread, corpus=corpus, store=store, options=options)
    try:
        resp, body = handle.get_json(f"/api/session/{session_id}")
        assert resp.status == 200
        assert body["data"]["turn_series"] == [
            [1, 1000, 500, False, "human_text"],
            [2, 1500, 0, True, "compact_boundary"],
        ]
        assert body["data"]["markers"] == {"compactions": [2], "spawns": [], "human": [1]}
        assert body["data"]["truncated"] is False
        assert_privacy(body)
        _assert_no_leak(json.dumps(body).encode("utf-8"))
    finally:
        handle.close()
        store.close()


def test_session_detail_limit_markers(tmp_path, monkeypatch):
    # v3-limits wiring: GET /api/session/<id> exposes limit_markers
    # (limits.limit_markers) alongside turn_series/markers, sourced from
    # the same stored top-level transcript digest.
    from claude_token_lens.cache import encode_result
    from claude_token_lens.model import Event, EventKind, Turn, TranscriptMeta, TranscriptResult

    corpus = _build_corpus(tmp_path)
    _install_fake_rebuild(monkeypatch, corpus)

    store = Store(tmp_path / "service.db")
    store.open()
    session_id = corpus.sessions[0].session_id
    store.upsert_session(session_id=session_id, project_slug="proj-a", slug="proj-a")

    result = TranscriptResult(
        meta=TranscriptMeta(path=_FAKE_PATH, kind="top-level", session_id=session_id),
        turns=[
            Turn(turn_index=1, ctx=1000, cache_creation_tokens=500, is_recache=False,
                 preceding_primary=EventKind.HUMAN_TEXT, human_prompt_chars=42, ts="2026-09-18T12:00:05.000Z"),
            Turn(turn_index=2, ctx=1500, cache_creation_tokens=25_000, is_recache=True,
                 gap_cause="limit", gap_s=10_795.0, ts="2026-09-18T15:00:10.000Z"),
        ],
        events=[
            Event(kind=EventKind.LIMIT_HIT, subkind="session_limit", ts="2026-09-18T12:00:05.000Z",
                  detail={"reset_minutes_of_day": 15 * 60}),
            Event(kind=EventKind.LIMIT_RESUME, ts="2026-09-18T15:00:00.000Z"),
        ],
    )
    store.upsert_transcript(
        session_id=session_id,
        path=_FAKE_PATH,
        kind="top-level",
        digest_json=json.dumps(encode_result(result)),
    )

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    options = ServeOptions(projects_root=tmp_path / "projects", config_dir=config_dir)
    handler_cls = service_api.make_handler(store, options)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    handle = _ServerHandle(httpd, thread, corpus=corpus, store=store, options=options)
    try:
        resp, body = handle.get_json(f"/api/session/{session_id}")
        assert resp.status == 200
        assert body["data"]["limit_markers"] == [
            {"ts": "2026-09-18T12:00:05.000Z", "kind": "limit_hit", "detail": {"reset_minutes_of_day": 900, "subkind": "session_limit"}},
            {"ts": "2026-09-18T15:00:00.000Z", "kind": "limit_resume", "detail": {}},
        ]
        assert_privacy(body)
        _assert_no_leak(json.dumps(body).encode("utf-8"))
    finally:
        handle.close()
        store.close()


def test_session_detail_not_found(server):
    resp, body = server.get_json("/api/session/does-not-exist")
    assert resp.status == 404
    assert body["error"]["code"] == "not_found"


def test_recache(server):
    resp, body = server.get_json("/api/recache")
    assert resp.status == 200
    assert body["data"]["by_signature"]["full-expiry"]["turns"] == 1
    assert_privacy(body)


def test_compactions(server):
    resp, body = server.get_json("/api/compactions")
    assert resp.status == 200
    assert body["data"][0]["dropped_tokens"] == 800
    assert_privacy(body)


def test_profiles_listing_has_no_toml_path(server):
    resp, body = server.get_json("/api/profiles")
    assert resp.status == 200
    profiles = body["data"]["profiles"]
    user_entries = [p for p in profiles if p["source"] == "user"]
    assert user_entries == [
        {
            "id": "p1",
            "name": "implementation-heavy",
            "source": "user",
            "archetype": None,
            "for": [],
            "updated_at": user_entries[0]["updated_at"],
        }
    ]
    assert_privacy(body)
    _assert_no_leak(json.dumps(body).encode("utf-8"))


def test_profiles_listing_includes_the_catalogue(server):
    resp, body = server.get_json("/api/profiles")
    assert resp.status == 200
    catalogue_ids = {p["id"] for p in body["data"]["profiles"] if p["source"] == "catalogue"}
    assert catalogue_ids == set(profile_catalogue.CATALOGUE_IDS)
    assert_privacy(body)


def test_profiles_listing_reports_the_baseline_suggested_profile(server):
    resp, body = server.get_json("/api/profiles")
    assert resp.status == 200
    assert body["data"]["suggested_profile_id"] == "implementation-heavy"


def test_baseline_returns_the_latest_capture_and_capture_status(server):
    resp, body = server.get_json("/api/baseline")
    assert resp.status == 200
    data = body["data"]
    assert data["baseline"]["archetype"] == "plan-high-implement-low"
    assert data["baseline"]["record"] == {"sessions": 1, "suggested_profile": "implementation-heavy"}
    assert len(data["history"]) == 1
    assert data["capture_status"]["started"] is False
    assert "not started" in data["capture_status"]["summary"]
    assert_privacy(body)


def test_baseline_is_null_when_none_recorded(tmp_path, monkeypatch):
    corpus = _build_corpus(tmp_path)
    _install_fake_rebuild(monkeypatch, corpus)
    store = Store(tmp_path / "empty.db")
    store.open()
    config_dir = tmp_path / "empty-config"
    config_dir.mkdir()
    options = ServeOptions(projects_root=tmp_path / "projects", config_dir=config_dir)
    handler_cls = service_api.make_handler(store, options)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    handle = _ServerHandle(httpd, thread, corpus=corpus, store=store, options=options)
    try:
        resp, body = handle.get_json("/api/baseline")
        assert resp.status == 200
        assert body["data"]["baseline"] is None
        assert body["data"]["history"] == []
        assert_privacy(body)
    finally:
        handle.close()
        store.close()


def test_set_tag_round_trips(server):
    resp, body = server.post_json(
        f"/api/sessions/{server.session_id}/tags", {"key": "mode", "value": "interactive"}
    )
    assert resp.status == 200
    assert body["data"]["tags"]["mode"] == "interactive"
    assert body["data"]["session_id"] == server.session_id


def test_set_tag_bad_key_is_bad_request(server):
    resp, body = server.post_json(f"/api/sessions/{server.session_id}/tags", {"key": "bogus", "value": "x"})
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"


def test_set_tag_bad_value_type_is_bad_request(server):
    resp, body = server.post_json(f"/api/sessions/{server.session_id}/tags", {"key": "mode", "value": 5})
    assert resp.status == 400


def test_set_tag_unknown_session_is_not_found(server):
    resp, body = server.post_json("/api/sessions/does-not-exist/tags", {"key": "mode", "value": "x"})
    assert resp.status == 404


def test_set_tag_bad_json_body_is_bad_request(server):
    resp, raw = server.request(
        "POST", f"/api/sessions/{server.session_id}/tags", body=None
    )
    # No body at all still dispatches with body=None -> handler must
    # reject it as bad_request (key/value both missing).
    body = json.loads(raw)
    assert resp.status == 400


# -- v0.3 profile routes -----------------------------------------------------


def test_profile_diff_for_catalogue_profile_against_latest_snapshot(server):
    # The "server" fixture seeds one (schema-1, no "effective" field)
    # snapshot -- exercises the "a snapshot exists but the diff still
    # has to degrade gracefully" path; the "no snapshot at all" path is
    # covered separately below with a snapshot-free store.
    resp, body = server.get_json("/api/profiles/interactive-chat/diff")
    assert resp.status == 200
    data = body["data"]
    assert data["profile_id"] == "interactive-chat"
    assert data["scope"] == "user"
    assert data["notes"] == []
    assert isinstance(data["settings"], list) and data["settings"]
    assert "claude-token-lens apply interactive-chat" in data["apply_command"]
    assert data["launch_command"].startswith("claude --settings")
    assert_privacy(body)
    _assert_no_leak(json.dumps(body).encode("utf-8"))


def test_profile_diff_rows_say_where_each_change_lands_and_come_with_a_prompt(server):
    resp, body = server.get_json("/api/profiles/interactive-chat/diff?scope=project-local")
    assert resp.status == 200
    data = body["data"]
    row = data["settings"][0]
    assert row["label"] and row["setting"] and row["agent"] is None
    assert row["where"] == ".claude/settings.local.json"
    assert data["dry_run_command"] == data["apply_command"] + " --dry-run"
    assert "settings profile" in data["prompt"] and "show me the diff" in data["prompt"]


def test_profile_diff_notes_missing_snapshot_when_store_has_none(tmp_path, monkeypatch):
    corpus = _build_corpus(tmp_path)
    _install_fake_rebuild(monkeypatch, corpus)
    store = Store(tmp_path / "no-snapshot.db")
    store.open()
    config_dir = tmp_path / "no-snapshot-config"
    config_dir.mkdir()
    options = ServeOptions(projects_root=tmp_path / "projects", config_dir=config_dir)
    handler_cls = service_api.make_handler(store, options)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    handle = _ServerHandle(httpd, thread, corpus=corpus, store=store, options=options)
    try:
        resp, body = handle.get_json("/api/profiles/interactive-chat/diff")
        assert resp.status == 200
        assert any("no config snapshot" in note for note in body["data"]["notes"])
        assert_privacy(body)
    finally:
        handle.close()
        store.close()


def test_profile_diff_unknown_id_is_not_found(server):
    resp, body = server.get_json("/api/profiles/does-not-exist/diff")
    assert resp.status == 404


def test_profile_diff_rejects_a_user_profile_row_with_no_backing_file(server):
    # "p1" is a store-only fixture row (test_service_store.py's own
    # convention) with no real <config_dir>/profiles/p1.toml on disk --
    # the diff route must treat it the same as an unknown id, never
    # crash trying to read a file that was never written.
    resp, body = server.get_json("/api/profiles/p1/diff")
    assert resp.status == 404


def test_profile_diff_bad_scope_is_bad_request(server):
    resp, body = server.get_json("/api/profiles/interactive-chat/diff?scope=bogus")
    assert resp.status == 400


def test_profile_diff_never_includes_a_project_path(server):
    # The route never accepts a client-supplied project directory (see
    # route_profile_diff's own docstring note) -- a project-scoped scope
    # simply renders its apply command without --project-dir, so the
    # user fills in their own path when they actually run it.
    resp, body = server.get_json("/api/profiles/interactive-chat/diff?scope=repo")
    assert resp.status == 200
    assert "--project-dir" not in body["data"]["apply_command"]


def test_create_profile_writes_a_real_toml_file_and_is_listed(server):
    payload = {"id": "my-new-profile", "name": "My New Profile", "settings": {"promptCacheTtl": "1h"}}
    resp, body = server.post_json("/api/profiles", payload)
    assert resp.status == 201
    assert body["data"] == {"id": "my-new-profile", "name": "My New Profile", "source": "user", "updated_at": body["data"]["updated_at"]}

    written = server.options.config_dir / "profiles" / "my-new-profile.toml"
    assert written.is_file()
    loaded = profile_schema.load_profile(written)
    assert loaded.settings["promptCacheTtl"] == "1h"

    resp, body = server.get_json("/api/profiles")
    ids = {p["id"] for p in body["data"]["profiles"]}
    assert "my-new-profile" in ids


def test_create_profile_rejects_unknown_key(server):
    resp, body = server.post_json("/api/profiles", {"id": "bad-profile", "bogus_key": 1})
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"
    assert "bogus_key" in body["error"]["message"]


def test_create_profile_rejects_catalogue_id(server):
    resp, body = server.post_json("/api/profiles", {"id": "interactive-chat", "name": "shadow"})
    assert resp.status == 409


def test_create_profile_conflict_without_replace_then_succeeds_with_it(server):
    resp, body = server.post_json("/api/profiles", {"id": "dup-profile", "name": "first"})
    assert resp.status == 201

    resp, body = server.post_json("/api/profiles", {"id": "dup-profile", "name": "second"})
    assert resp.status == 409
    assert body["error"]["code"] == "conflict"

    resp, body = server.post_json("/api/profiles?replace=1", {"id": "dup-profile", "name": "second"})
    assert resp.status == 201
    written = server.options.config_dir / "profiles" / "dup-profile.toml"
    assert profile_schema.load_profile(written).name == "second"


def test_create_profile_bad_body_is_bad_request(server):
    resp, raw = server.request("POST", "/api/profiles", body=None)
    body = json.loads(raw)
    assert resp.status == 400


# -- S3: same-origin / Content-Type guard on mutating routes -----------------


def test_post_wrong_content_type_is_bad_request(server):
    resp, raw = server.request(
        "POST",
        f"/api/sessions/{server.session_id}/tags",
        raw_body=json.dumps({"key": "mode", "value": "agentic"}).encode("utf-8"),
        headers={"Content-Type": "text/plain"},
    )
    body = json.loads(raw)
    assert resp.status == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "bad_request"
    # The tag was never set.
    resp2, tags_body = server.get_json(f"/api/session/{server.session_id}")
    assert tags_body["data"]["tags"].get("mode") != "agentic"


def test_post_content_type_with_charset_parameter_is_accepted(server):
    resp, raw = server.request(
        "POST",
        f"/api/sessions/{server.session_id}/tags",
        raw_body=json.dumps({"key": "mode", "value": "agentic"}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    body = json.loads(raw)
    assert resp.status == 200
    assert body["ok"] is True


def test_post_cross_origin_is_forbidden(server):
    resp, raw = server.request(
        "POST",
        f"/api/sessions/{server.session_id}/tags",
        body={"key": "mode", "value": "agentic"},
        headers={"Origin": "https://evil.example"},
    )
    body = json.loads(raw)
    assert resp.status == 403
    assert body["ok"] is False
    assert body["error"]["code"] == "forbidden"
    resp2, session_body = server.get_json(f"/api/session/{server.session_id}")
    assert session_body["data"]["tags"].get("mode") != "agentic"


def test_post_cross_site_sec_fetch_site_is_forbidden(server):
    resp, raw = server.request(
        "POST",
        "/api/profiles",
        body={"id": "csrf-test", "name": "CSRF test"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    body = json.loads(raw)
    assert resp.status == 403
    assert body["ok"] is False
    # Nothing was written.
    assert not (server.options.config_dir / "profiles" / "csrf-test.toml").exists()


def test_post_same_origin_is_allowed(server):
    resp, body = server.post_json(
        f"/api/sessions/{server.session_id}/tags",
        {"key": "mode", "value": "agentic"},
    )
    # post_json sends no Origin/Sec-Fetch-Site at all (plain
    # http.client), which must still be accepted -- but confirm the
    # explicit same-origin/same-origin-site case works too.
    assert resp.status == 200
    resp2, raw2 = server.request(
        "POST",
        f"/api/sessions/{server.session_id}/tags",
        body={"key": "mode", "value": "agentic"},
        headers={"Origin": f"http://127.0.0.1:{server.port}", "Sec-Fetch-Site": "same-origin"},
    )
    body2 = json.loads(raw2)
    assert resp2.status == 200
    assert body2["ok"] is True


def test_post_with_no_content_type_and_no_body_is_bad_request(server):
    """A request with Content-Length: 0 and no Content-Type header must
    be rejected as bad_request (never dispatched with an empty/None
    body) -- this is the same code path review S2's sibling finding
    (a missing header) used to reach the handler directly."""
    resp, raw = server.request("POST", f"/api/sessions/{server.session_id}/tags")
    body = json.loads(raw)
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"


# -- report-backed routes -----------------------------------------------------


def test_ttl_route(server):
    resp, body = server.get_json("/api/ttl")
    assert resp.status == 200
    assert body["ok"] is True
    assert_privacy(body)


@pytest.mark.parametrize("route", ["/api/carry", "/api/compaction-sim", "/api/model-swap", "/api/waste"])
def test_v4_report_backed_routes_return_ok(server, route):
    """Mirrors test_ttl_route for the v4 wiring round's four new
    report-backed routes -- each just reads its own like-named section
    back out of the same assembled report `/api/ttl` already builds."""
    resp, body = server.get_json(route)
    assert resp.status == 200
    assert body["ok"] is True
    assert_privacy(body)


def test_v4_report_backed_routes_accept_since_until(server):
    for route in ("/api/carry", "/api/compaction-sim", "/api/model-swap", "/api/waste"):
        resp, body = server.get_json(f"{route}?since=2026-08-01T00:00:00%2B00:00&until=2026-08-31T00:00:00%2B00:00")
        assert resp.status == 200
        assert body["ok"] is True

        resp, body = server.get_json(f"{route}?since=not-a-date")
        assert resp.status == 400
        assert body["error"]["code"] == "bad_request"


def test_recommendations_route(server):
    resp, body = server.get_json("/api/recommendations")
    assert resp.status == 200
    assert isinstance(body["data"], list)
    assert_privacy(body)


def test_ttl_and_recommendations_accept_since_until(server):
    # Shares _window_query with /api/report.json (already tested for
    # forwarding/cache-key behaviour above) -- confirm the other
    # report-backed routes also accept since/until rather than rejecting
    # them as unknown query params.
    resp, body = server.get_json("/api/ttl?since=2026-08-01T00:00:00%2B00:00&until=2026-08-31T00:00:00%2B00:00")
    assert resp.status == 200
    assert body["ok"] is True

    resp, body = server.get_json("/api/recommendations?since=2026-08-01T00:00:00%2B00:00")
    assert resp.status == 200
    assert isinstance(body["data"], list)

    resp, body = server.get_json("/api/ttl?since=not-a-date")
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"


def test_config_diff_requires_key_or_auto_keys(server):
    resp, body = server.get_json("/api/config-diff")
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"


def test_config_diff_auto_keys(server):
    resp, body = server.get_json("/api/config-diff?auto_keys=1")
    assert resp.status == 200
    assert isinstance(body["data"], list)


def test_config_diff_unknown_key_is_empty_not_error(server):
    resp, body = server.get_json("/api/config-diff?key=does-not-exist")
    assert resp.status == 200
    assert body["data"] == []


def _reconstruct_snapshots(store: Store) -> list[Snapshot] | None:
    """Mirror ``service/api.py``'s private ``_snapshots_from_store()``
    closure (not reachable from outside ``make_handler``) so this test
    can build the exact same ``snapshots`` argument the route's own
    ``build_report`` call used -- ``_seed_store`` above upserts one
    snapshot row, so the route never actually calls ``build_report``
    with ``snapshots=None``.
    """
    snaps: list[Snapshot] = []
    for row in store.snapshots():
        try:
            data = json.loads(row["digest_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        # Mirror the real closure's S1-integration fix 1.c: it injects
        # the store's own project attribution over whatever (if
        # anything) the digest blob itself carries under this key, so
        # snapshots.py's _project_label() sees an honest per-project
        # slug rather than the "(unknown project)" fallback.
        data["project_slug"] = row.get("project_slug")
        snaps.append(Snapshot(path=Path(""), ts=row["ts"], data=data))
    snaps.sort(key=lambda s: s.ts)
    return snaps or None


def test_report_json_matches_cli_json_for_same_corpus(server):
    """The core CLI-JSON byte-parity contract: ``/api/report.json``
    must equal ``render_json(build_report(same corpus, ...))``, modulo
    ``ReportMeta.generated_at`` (wall-clock; may differ by a second
    between the two ``build_report`` calls -- the same posture
    ``tests/test_cli.py``'s own byte-identical-output tests take for
    the Markdown "Generated at" line).
    """
    resp, raw = server.request("GET", "/api/report.json")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "application/json"

    config = load_config(server.options.config_dir)
    rates = load_pricing(path=config.pricing_path, config_dir=server.options.config_dir)
    projects = tuple(sorted({b.slug for b in server.corpus.sessions if b.slug}))
    # Mirrors api.py's own _build_report_model: store-set session tags
    # (review finding 7) must be folded into the overrides the report is
    # built with, the same way the real route does, or this "expected"
    # build drifts from `server.store`'s seeded `purpose` tag.
    try:
        overrides = load_session_overrides(server.options.config_dir)
    except ConfigError:
        overrides = {}
    overrides = {sid: dict(entry) for sid, entry in overrides.items()}
    for session_id, tags in server.store.all_tags().items():
        merged = overrides.get(session_id, {})
        merged.update(tags)
        overrides[session_id] = merged
    model = build_report(
        server.corpus,
        rates,
        config,
        projects=projects,
        window="last 30 days",
        snapshots=_reconstruct_snapshots(server.store),
        session_overrides=overrides,
    )
    expected = json.loads(render_json(model))
    actual = json.loads(raw)

    expected["report"]["meta"]["generated_at"] = "STRIPPED"
    actual["report"]["meta"]["generated_at"] = "STRIPPED"
    assert actual == expected


def test_report_json_is_not_enveloped(server):
    # docs/api.md: report.json/.md/.html are the raw rendered document,
    # not {"ok": ..., "data": ...} -- confirm the top-level shape is the
    # renderer's own, not the envelope's.
    resp, raw = server.request("GET", "/api/report.json")
    body = json.loads(raw)
    assert "schema_version" in body and "report" in body
    assert "ok" not in body


def test_report_md_route(server):
    resp, raw = server.request("GET", "/api/report.md")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "text/markdown; charset=utf-8"
    assert len(raw) > 0
    _assert_no_leak(raw)


def test_report_html_route(server):
    resp, raw = server.request("GET", "/api/report.html")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "text/html; charset=utf-8"
    assert len(raw) > 0
    _assert_no_leak(raw)


def test_report_routes_reject_bad_window_days(server):
    resp, body = server.get_json("/api/report.json?window_days=nope")
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"


def test_report_routes_reject_bad_since_and_until(server):
    resp, body = server.get_json("/api/report.json?since=not-a-date")
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"

    resp, body = server.get_json("/api/report.json?until=also-not-a-date")
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"


def test_report_json_forwards_since_until_to_rebuild_and_ignores_default_window(server, monkeypatch):
    """Release-verification finding: the report-backed routes only ever
    accepted ``window_days`` and silently ignored ``since``/``until``,
    so ``/api/report.json?since=...&until=...`` was byte-identical to a
    plain ``/api/report.json`` (always the last-30-days window) instead
    of the CLI's ``report --since ... --until ...`` for the same span --
    breaking the parity ``docs/api.md`` promises. Confirms ``since``/
    ``until`` reach ``corpus_from_store`` and that ``window_days`` is
    *not* defaulted to 30 alongside them (mirrors the CLI's own
    ``--days``/``--since`` mutually-exclusive argparse group).
    """
    calls = []
    real_corpus = server.corpus

    def recording_corpus_from_store(store, *, days=None, since=None, until=None, window_by="mtime"):
        calls.append({"days": days, "since": since, "until": until, "window_by": window_by})
        return real_corpus

    import claude_token_lens.service as service_pkg

    fake = types.ModuleType("claude_token_lens.service.rebuild")
    fake.corpus_from_store = recording_corpus_from_store
    monkeypatch.setitem(sys.modules, "claude_token_lens.service.rebuild", fake)
    monkeypatch.setattr(service_pkg, "rebuild", fake, raising=False)

    resp, raw = server.request(
        "GET", "/api/report.json?since=2026-08-01T00:00:00%2B00:00&until=2026-08-31T00:00:00%2B00:00"
    )
    assert resp.status == 200
    assert calls == [
        {
            "days": None,
            "since": "2026-08-01T00:00:00+00:00",
            "until": "2026-08-31T00:00:00+00:00",
            "window_by": "mtime",
        }
    ]
    body = json.loads(raw)
    assert body["report"]["meta"]["window"] == "since 2026-08-01T00:00:00+00:00 until 2026-08-31T00:00:00+00:00"


def test_report_json_since_until_is_a_separate_cache_key_from_window_days(server, monkeypatch):
    calls = {"n": 0}
    real_corpus = server.corpus

    def counting_corpus_from_store(store, *, days=None, since=None, until=None, window_by="mtime"):
        calls["n"] += 1
        return real_corpus

    import claude_token_lens.service as service_pkg

    fake = types.ModuleType("claude_token_lens.service.rebuild")
    fake.corpus_from_store = counting_corpus_from_store
    monkeypatch.setitem(sys.modules, "claude_token_lens.service.rebuild", fake)
    monkeypatch.setattr(service_pkg, "rebuild", fake, raising=False)

    resp1, _ = server.request("GET", "/api/report.json")  # default window_days=30
    resp2, _ = server.request("GET", "/api/report.json?since=2026-08-01T00:00:00%2B00:00")
    resp3, _ = server.request("GET", "/api/report.json?since=2026-08-01T00:00:00%2B00:00")  # cache hit
    assert resp1.status == resp2.status == resp3.status == 200
    assert calls["n"] == 2


def test_report_json_is_memoized_per_window(server, monkeypatch):
    calls = {"n": 0}
    real_corpus = server.corpus

    def counting_corpus_from_store(store, *, days=None, since=None, until=None, window_by="mtime"):
        calls["n"] += 1
        return real_corpus

    import claude_token_lens.service as service_pkg

    fake = types.ModuleType("claude_token_lens.service.rebuild")
    fake.corpus_from_store = counting_corpus_from_store
    monkeypatch.setitem(sys.modules, "claude_token_lens.service.rebuild", fake)
    monkeypatch.setattr(service_pkg, "rebuild", fake, raising=False)

    resp1, _ = server.request("GET", "/api/report.json")
    resp2, _ = server.request("GET", "/api/report.json")
    assert resp1.status == 200
    assert resp2.status == 200
    assert calls["n"] == 1, "second request for the same window should hit the cache, not rebuild"

    # A different window_days is a different cache key -> rebuilds.
    resp3, _ = server.request("GET", "/api/report.json?window_days=7")
    assert resp3.status == 200
    assert calls["n"] == 2


def test_report_json_cache_invalidates_when_store_change_token_changes(server, monkeypatch):
    # Deliverable 1.f: the memo key is Store.change_token(), not the raw
    # connection object -- mutating the store (even without touching the
    # window_days cache key) must force a rebuild on the next request.
    calls = {"n": 0}
    real_corpus = server.corpus

    def counting_corpus_from_store(store, *, days=None, since=None, until=None, window_by="mtime"):
        calls["n"] += 1
        return real_corpus

    import claude_token_lens.service as service_pkg

    fake = types.ModuleType("claude_token_lens.service.rebuild")
    fake.corpus_from_store = counting_corpus_from_store
    monkeypatch.setitem(sys.modules, "claude_token_lens.service.rebuild", fake)
    monkeypatch.setattr(service_pkg, "rebuild", fake, raising=False)

    resp1, _ = server.request("GET", "/api/report.json")
    assert resp1.status == 200
    assert calls["n"] == 1

    resp2, _ = server.request("GET", "/api/report.json")
    assert resp2.status == 200
    assert calls["n"] == 1  # unchanged store -> cache hit

    server.store.upsert_transcript(
        session_id=server.session_id,
        path=_FAKE_PATH + ".new",
        kind="subagent",
        digest_json=json.dumps({"turns": 1}),
    )

    resp3, _ = server.request("GET", "/api/report.json")
    assert resp3.status == 200
    assert calls["n"] == 2  # change_token moved -> rebuilt


# -- static file serving ------------------------------------------------------


def test_root_serves_placeholder_index_when_static_dir_is_empty(server):
    resp, raw = server.request("GET", "/")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "text/html"
    assert len(raw) > 0


def test_static_traversal_is_rejected(server):
    resp, raw = server.request("GET", "/static/..%2f..%2fsecrets.txt")
    body = json.loads(raw)
    assert resp.status == 404
    assert body["error"]["code"] == "not_found"


def test_static_missing_file_is_404(server):
    resp, body = server.get_json("/static/does-not-exist.js")
    assert resp.status == 404


def test_static_file_is_served_from_a_real_static_dir(tmp_path, monkeypatch):
    # The package's own static/ is empty at S1-api's own delivery time
    # (a sibling package ships its contents) -- make_handler's
    # static_dir keyword override (an S1-api addition beyond
    # contracts.MakeHandler's bare (store, options), documented on
    # make_handler itself) lets this test point at a tmp_path directory
    # with a real file instead, without writing anything into the
    # source tree.
    corpus = _build_corpus(tmp_path)
    _install_fake_rebuild(monkeypatch, corpus)

    store = Store(tmp_path / "service.db")
    store.open()

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>hello</html>", encoding="utf-8")
    (static_dir / "app.js").write_text("console.log('hi');", encoding="utf-8")

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    options = ServeOptions(projects_root=tmp_path / "projects", config_dir=config_dir)

    handler_cls = service_api.make_handler(store, options, static_dir=static_dir)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    handle = _ServerHandle(httpd, thread, corpus=corpus, store=store, options=options)
    try:
        resp, raw = handle.request("GET", "/")
        assert resp.status == 200
        assert b"hello" in raw

        resp2, raw2 = handle.request("GET", "/static/app.js")
        assert resp2.status == 200
        assert resp2.getheader("Content-Type") in ("text/javascript", "application/javascript")
        assert b"console.log" in raw2
    finally:
        handle.close()
        store.close()


# -- 500 on unexpected exceptions --------------------------------------------


def test_unexpected_exception_becomes_500_without_traceback(server, monkeypatch):
    # The routing tables are closures inside make_handler, not reachable
    # by name from outside -- exercise the real failure path via a store
    # method monkeypatched to raise, which the health route calls, and
    # confirm the dispatcher's last-resort except clause turns it into a
    # one-line 500 body, never a traceback or the exception's own
    # message text (which could, in principle, embed something private).
    monkeypatch.setattr(server.store, "schema_version", _raise_runtime_error)
    resp, body = server.get_json("/api/health")
    assert resp.status == 500
    assert body["error"]["code"] == "internal_error"
    assert "boom" not in body["error"]["message"]
    assert "Traceback" not in body["error"]["message"]
    assert "\n" not in body["error"]["message"]


def _raise_runtime_error(*args, **kwargs):
    raise RuntimeError("boom: something unexpected happened")


__all__: list[str] = []


def test_diagnostics_route_returns_the_labelled_table(server):
    resp, body = server.get_json("/api/diagnostics")
    assert resp.status == 200
    table = body["data"]
    assert table["name"] == "data_quality"
    assert [c["key"] for c in table["columns"]] == ["check", "value", "meaning"]
    assert table["value_labels"]["lines"] == "Lines read"
    assert_privacy(body)


# -- Host allowlist (DNS rebinding) -------------------------------------------


@pytest.mark.parametrize("path", ["/api/summary", "/api/report.json", "/", "/static/app.js"])
def test_forged_host_is_refused_on_get(server, path):
    resp, raw = server.request("GET", path, headers={"Host": "attacker.example:8765"})
    assert resp.status == 403
    assert json.loads(raw)["error"]["code"] == "forbidden"


def test_forged_host_is_refused_on_post(server):
    resp, raw = server.request(
        "POST", "/api/profiles", body={"id": "x"}, headers={"Host": "attacker.example"}
    )
    assert resp.status == 403
    assert json.loads(raw)["error"]["code"] == "forbidden"


@pytest.mark.parametrize("host", ["127.0.0.1:1234", "localhost", "LOCALHOST:8765", "[::1]:8765"])
def test_loopback_hosts_are_allowed(server, host):
    resp, _ = server.request("GET", "/api/health", headers={"Host": host})
    assert resp.status == 200


def test_allowed_host_names_add_specific_binds_and_extra_names():
    from claude_token_lens.service.api import allowed_host_names

    base = ServeOptions(projects_root=Path("p"), config_dir=Path("c"))
    assert "0.0.0.0" not in allowed_host_names(ServeOptions(projects_root=Path("p"), config_dir=Path("c"), bind="0.0.0.0"))
    assert "192.168.1.5" in allowed_host_names(ServeOptions(projects_root=Path("p"), config_dir=Path("c"), bind="192.168.1.5"))
    extra = ServeOptions(projects_root=Path("p"), config_dir=Path("c"), allowed_hosts=("Lens.Local",))
    assert "lens.local" in allowed_host_names(extra)
    assert "lens.local" not in allowed_host_names(base)


# -- readability P6: profile schema, one profile, session explain, from-current --


def test_profile_schema_lists_every_allowlisted_key_in_plain_words(server):
    resp, payload = server.get_json("/api/profile-schema")
    assert resp.status == 200
    data = payload["data"]
    assert {lever["key"] for lever in data["settings"]} == set(profile_schema.SETTINGS_ALLOWLIST)
    assert {lever["key"] for lever in data["agents"]} == set(profile_schema.AGENT_ALLOWLIST)
    effort = next(lever for lever in data["settings"] if lever["key"] == "effortLevel")
    assert effort["label"] == "Effort level"
    assert effort["values"] == ["low", "medium", "high", "max"]
    assert effort["description"]
    assert all(lever["description"] for lever in data["settings"] + data["agents"])
    assert [scope["key"] for scope in data["scopes"]] == ["user", "project-local", "repo"]


def test_profile_route_returns_a_catalogue_profile_and_404s_unknown(server):
    profile_id = profile_catalogue.list_profiles()[0].id
    resp, payload = server.get_json(f"/api/profiles/{profile_id}")
    assert resp.status == 200
    assert payload["data"]["id"] == profile_id
    assert payload["data"]["source"] == "catalogue"
    assert payload["data"]["setting_count"] >= 1
    resp, _payload = server.get_json("/api/profiles/no-such-profile")
    assert resp.status == 404


def test_session_explain_gives_template_sentences(server):
    resp, raw = server.request("GET", f"/api/session/{server.session_id}/explain")
    assert resp.status == 200
    _assert_no_leak(raw)
    data = json.loads(raw)["data"]
    assert data["headline"].startswith("This session cost ")
    assert "3 replies" in data["headline"]
    text = " ".join(data["sentences"])
    assert "No subagents ran" in text
    assert "The cache was rebuilt once" in text
    assert "summarised the conversation once" in text
    parts = {row["part"]: row for row in data["cost_split"]}
    assert set(parts) == {"cache_read", "cache_write", "output", "input"}
    assert round(sum(row["share_pct"] for row in data["cost_split"])) == 100
    resp, _raw = server.request("GET", "/api/session/unknown/explain")
    assert resp.status == 404


def test_profiles_from_current_saves_allowlisted_non_managed_keys(server):
    server.store.upsert_snapshot(
        project_slug="proj-a",
        project_root_path=_FAKE_ROOT,
        ts="2026-09-19T12:00:00Z",
        schema_version=2,
        digest_json=json.dumps(
            {
                "effective": {"effortLevel": "high", "model": "opus", "permissions": {"allow": []}},
                "managed_keys": ["model"],
                "effective_agents": {"reviewer": {"effort": "low", "color": "blue"}},
            }
        ),
    )
    # A later apply stamp (active-profile marker) records no settings and
    # must not hide the real snapshot before it.
    server.store.upsert_snapshot(
        project_slug="proj-a",
        project_root_path=_FAKE_ROOT,
        ts="2026-09-19T13:00:00Z",
        schema_version=2,
        digest_json=json.dumps({"ts": "2026-09-19T13:00:00Z", "schema_version": 2, "profile_id": "x"}),
    )
    resp, payload = server.post_json("/api/profiles/from-current", {"name": "Mine"})
    assert resp.status == 201, payload
    assert payload["data"]["id"] == "my-current-settings"
    assert payload["data"]["skipped_managed"] == ["model"]
    resp, payload = server.get_json("/api/profiles/my-current-settings")
    assert payload["data"]["settings"] == {"effortLevel": "high"}
    assert payload["data"]["agents"] == {"reviewer": {"effort": "low"}}
    # A second save without replace=1 is a conflict, not an overwrite.
    resp, _payload = server.post_json("/api/profiles/from-current", {})
    assert resp.status == 409


# -- context files, named windows ----------------------------------------


def test_claude_md_route_lists_files_and_404s_an_unknown_id(server):
    (server.options.config_dir.parent / "CLAUDE.md").write_text("# Rules\n\nBe brief.\n", encoding="utf-8")
    resp, payload = server.get_json("/api/claude-md")
    assert resp.status == 200
    data = payload["data"]
    assert data["period"] == "over the last 30 days"
    user = next(item for item in data["files"] if item["level"] == "User")
    resp, payload = server.get_json(f"/api/claude-md/{user['id']}")
    assert resp.status == 200
    assert payload["data"]["section_rows"][0]["heading"] == "Rules"
    resp, _ = server.get_json("/api/claude-md/0123456789abcdef")
    assert resp.status == 404


def test_skills_route_returns_rows_and_fixes(server):
    resp, payload = server.get_json("/api/skills?window=24h")
    assert resp.status == 200
    data = payload["data"]
    assert data["period"] == "in the last 24 hours"
    assert isinstance(data["skills"], list) and "fixes" in data


@pytest.mark.parametrize("name", ["1h", "today", "24h"])
def test_named_windows_resolve_to_since(server, name):
    resp, payload = server.get_json(f"/api/summary?window={name}")
    assert resp.status == 200
    assert "sessions" in payload["data"]
    resp, _raw = server.request("GET", f"/api/report.json?window={name}")
    assert resp.status == 200


def test_since_last_change_window_needs_a_change(server):
    resp, payload = server.get_json("/api/recommendations?window=change")
    assert resp.status == 400
    assert "No change recorded yet" in payload["error"]["message"]
    resp, payload = server.get_json("/api/summary?window=fortnight")
    assert resp.status == 400


def test_named_window_since_is_rounded_to_the_minute():
    from datetime import datetime, timezone

    now = datetime(2026, 9, 23, 10, 17, 42, tzinfo=timezone.utc)
    since, reason = service_api._named_window_since("1h", None, now)
    assert since == "2026-09-23T09:17:00Z" and reason == ""
    since, _ = service_api._named_window_since("24h", None, now)
    assert since == "2026-09-22T10:17:00Z"


def test_impact_is_empty_without_changes_and_lists_an_apply(server):
    resp, payload = server.get_json("/api/impact")
    assert resp.status == 200
    assert payload["data"]["changes"] == []
    assert payload["data"]["min_sessions"] >= 1

    from claude_token_lens.profiles import apply as apply_mod
    from claude_token_lens.profiles.schema import load_dict

    config_dir = server.options.config_dir
    claude_root = config_dir.parent / "fake-claude"
    claude_root.mkdir()
    plan = apply_mod.plan_apply(
        load_dict({"id": "one-off", "settings": {"effortLevel": "medium"}}),
        scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root,
    )
    apply_mod.execute(plan, config_dir=config_dir)
    resp, payload = server.get_json("/api/impact")
    [change] = payload["data"]["changes"]
    assert change["change"]["keys"] == ["effortLevel"]
    assert change["enough"] is False and "so far" in change["verdict"]
    resp, payload = server.get_json("/api/summary?window=change")
    assert resp.status == 200


def test_profile_goals_lists_goals_and_drafts_one(server):
    resp, payload = server.get_json("/api/profile-goals")
    assert resp.status == 200
    ids = [goal["id"] for goal in payload["data"]["goals"]]
    assert "recommendations" in ids and "current" in ids
    resp, payload = server.get_json("/api/profile-goals?goal=cache&window=24h")
    assert resp.status == 200
    data = payload["data"]
    assert data["goal"]["id"] == "cache" and data["period"] == "in the last 24 hours"
    assert set(data) >= {"candidates", "profile", "whatif"}
    resp, payload = server.get_json("/api/profile-goals?goal=nope")
    assert resp.status == 400


def test_whatif_estimates_and_validates(server):
    resp, payload = server.post_json("/api/whatif", {"settings": {"model": "sonnet"}, "agents": {}})
    assert resp.status == 200
    [row] = payload["data"]["rows"]
    assert row["key"] == "model" and row["effect_text"]
    resp, payload = server.post_json("/api/whatif", {"settings": {"effortLevel": "enormous"}})
    assert resp.status == 400


def test_whatif_rejects_cross_site_posts(server):
    resp, _raw = server.request(
        "POST", "/api/whatif", body={"settings": {"model": "sonnet"}}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert resp.status == 403


def test_quick_actions_list_and_detail(server):
    resp, payload = server.get_json("/api/quick-actions?window=24h")
    assert resp.status == 200
    checks = payload["data"]["checks"]
    assert [c["id"] for c in checks][:2] == ["models", "effort"]
    assert all(c["status"] in ("act", "ok", "no_data") and c["summary"] for c in checks)
    for check in checks:
        resp, payload = server.get_json(f"/api/quick-actions/{check['id']}")
        assert resp.status == 200
        assert set(payload["data"]) >= {"question", "table", "fixes", "tips"}
    resp, _payload = server.get_json("/api/quick-actions/nope")
    assert resp.status == 404


def test_setup_lists_the_footprint_expectations_and_uninstall(server):
    resp, payload = server.get_json("/api/setup")
    assert resp.status == 200
    data = payload["data"]
    assert {item["key"] for item in data["items"]} >= {"snapshot_hook"}
    assert all(set(item) >= {"title", "status", "token_cost", "undo"} for item in data["items"])
    assert data["expectations"][0]["title"] == "It never uses your Claude tokens"
    assert data["uninstall_command"].endswith("--dry-run")
