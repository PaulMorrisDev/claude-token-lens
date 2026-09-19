"""Egress test for the v0.2 service (S1-api): proves ``docs/api.md``'s
"Local only" guarantee -- the service opens exactly one listening
socket and never opens an outbound connection -- by monkeypatching
``socket.socket.connect`` for the lifetime of a real test server and
recording every address it is ever asked to connect to, while a test
client exercises every ``/api/*`` route (store-backed, report-backed,
the two mutating routes, the v0.3 stub routes, and static/traversal).

The test client's own connections to the server (``http.client``,
itself built on ``socket.socket.connect``) are the *only* expected
entries -- always the loopback address this test bound the server to.
Anything else recorded would mean a route reached outside the process
(a DNS lookup, a proxy, a "phone home" call), which ``docs/api.md``
promises never happens.

Builds its own minimal fixtures (synthetic corpus + seeded store + a
fake ``service.rebuild`` stand-in) rather than importing
``tests/test_service_api.py``'s -- test modules in this repo don't share
fixtures across files (each is self-contained against ``tests/helpers``
only), and this file's needs are a strict subset of that one's.
"""

from __future__ import annotations

import http.client
import json
import socket
import sys
import threading
import types
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from claude_token_lens import corpus as corpus_mod
from claude_token_lens.service import api as service_api
from claude_token_lens.service.contracts import ServeOptions
from claude_token_lens.service.store import Store

from helpers import turn_line, write_jsonl

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _build_corpus(tmp_path: Path) -> corpus_mod.Corpus:
    project_dir = tmp_path / "projects" / "proj-a"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "session-a.jsonl",
        [turn_line(input_tokens=100 + i, output_tokens=20 + i) for i in range(2)],
    )
    return corpus_mod.load_corpus([project_dir])


def _install_fake_rebuild(monkeypatch, corpus: corpus_mod.Corpus) -> None:
    import claude_token_lens.service as service_pkg

    fake = types.ModuleType("claude_token_lens.service.rebuild")

    def corpus_from_store(store, *, days=None, since=None, until=None, window_by="mtime"):
        return corpus

    fake.corpus_from_store = corpus_from_store
    monkeypatch.setitem(sys.modules, "claude_token_lens.service.rebuild", fake)
    monkeypatch.setattr(service_pkg, "rebuild", fake, raising=False)


def _seed_minimal(store: Store, corpus: corpus_mod.Corpus) -> str:
    bundle = corpus.sessions[0]
    store.upsert_session(
        session_id=bundle.session_id,
        project_slug="proj-a",
        slug="proj-a",
        first_ts="2026-09-18T12:00:00Z",
        last_ts="2026-09-18T13:00:00Z",
    )
    store.upsert_transcript(
        session_id=bundle.session_id,
        path=str(Path("proj-a") / "session-a.jsonl"),
        kind="top-level",
        digest_json=json.dumps({"turns": 2}),
    )
    store.upsert_profile(profile_id="p1", name="implementation-heavy", toml_path="profiles/p1.toml")
    store.record_baseline(
        project_slug="proj-a",
        window_start="2026-09-11T00:00:00Z",
        window_end="2026-09-18T00:00:00Z",
        archetype="plan-high-implement-low",
        digest_json=json.dumps({"sessions": 1}),
    )
    return bundle.session_id


def _request(port: int, method: str, path: str, body: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def test_no_connect_call_ever_targets_a_non_loopback_address(tmp_path, monkeypatch):
    corpus = _build_corpus(tmp_path)
    _install_fake_rebuild(monkeypatch, corpus)

    store = Store(tmp_path / "service.db")
    store.open()
    session_id = _seed_minimal(store, corpus)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    options = ServeOptions(projects_root=tmp_path / "projects", config_dir=config_dir)

    handler_cls = service_api.make_handler(store, options)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    connect_targets: list = []
    real_connect = socket.socket.connect

    def _recording_connect(self, address, *args, **kwargs):
        connect_targets.append(address)
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", _recording_connect)

    try:
        port = httpd.server_port

        # Every GET route named in docs/api.md.
        for path in (
            "/",
            "/static/does-not-exist.js",
            "/api/health",
            "/api/summary",
            "/api/summary?window_days=7",
            "/api/sessions",
            f"/api/session/{session_id}",
            "/api/session/does-not-exist",
            "/api/recache",
            "/api/compactions",
            "/api/profiles",
            "/api/profiles/p1/diff",
            "/api/baseline",
            "/api/ttl",
            "/api/config-diff?auto_keys=1",
            "/api/recommendations",
            "/api/report.json",
            "/api/report.md",
            "/api/report.html",
            "/api/does-not-exist",
        ):
            status = _request(port, "GET", path)
            assert status in (200, 400, 404, 501), f"{path} -> unexpected {status}"

        # The two mutating POST routes.
        status = _request(port, "POST", f"/api/sessions/{session_id}/tags", {"key": "mode", "value": "interactive"})
        assert status == 200
        status = _request(port, "POST", "/api/profiles", {"id": "p2", "name": "x"})
        assert status == 501
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        store.close()

    assert connect_targets, "expected at least the test client's own loopback connections"
    for address in connect_targets:
        host = address[0] if isinstance(address, tuple) else address
        assert host in _LOOPBACK_HOSTS, f"socket.connect reached a non-loopback address: {address!r}"


__all__: list[str] = []
