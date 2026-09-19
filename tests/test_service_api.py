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
        digest_json=json.dumps({"sessions": 1}),
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

    def request(self, method: str, path: str, *, body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            headers = {}
            payload = None
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=payload, headers=headers)
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


@pytest.fixture
def server(tmp_path, monkeypatch):
    corpus = _build_corpus(tmp_path)
    _install_fake_rebuild(monkeypatch, corpus)

    store = Store(tmp_path / "service.db")
    store.open()
    session_id = _seed_store(store, corpus)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    options = ServeOptions(projects_root=tmp_path / "projects", config_dir=config_dir)

    handler_cls = service_api.make_handler(store, options)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    handle = _ServerHandle(httpd, thread, corpus=corpus, store=store, options=options)
    handle.session_id = session_id
    try:
        yield handle
    finally:
        handle.close()
        store.close()


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
    assert_privacy(body)
    _assert_no_leak(json.dumps(body).encode("utf-8"))


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
    assert body["data"] == [{"id": "p1", "name": "implementation-heavy", "updated_at": body["data"][0]["updated_at"]}]
    assert_privacy(body)
    _assert_no_leak(json.dumps(body).encode("utf-8"))


def test_baseline_latest_per_project(server):
    resp, body = server.get_json("/api/baseline")
    assert resp.status == 200
    assert len(body["data"]) == 1
    assert body["data"][0]["archetype"] == "plan-high-implement-low"
    assert_privacy(body)


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


# -- v0.3-dependent stub routes ----------------------------------------------


def test_profile_diff_is_not_implemented(server):
    resp, body = server.get_json("/api/profiles/p1/diff")
    assert resp.status == 501
    assert body["error"]["code"] == "not_implemented"


def test_create_profile_is_not_implemented(server):
    resp, body = server.post_json("/api/profiles", {"id": "p2", "name": "new-profile"})
    assert resp.status == 501
    assert body["error"]["code"] == "not_implemented"


def test_create_profile_bad_body_is_bad_request(server):
    resp, raw = server.request("POST", "/api/profiles", body=None)
    body = json.loads(raw)
    assert resp.status == 400


# -- report-backed routes -----------------------------------------------------


def test_ttl_route(server):
    resp, body = server.get_json("/api/ttl")
    assert resp.status == 200
    assert body["ok"] is True
    assert_privacy(body)


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
