"""Tests for ``service.contracts``: the dataclasses construct with their
documented defaults, ``ApiError.to_envelope`` matches the documented
``{"ok": false, "error": {...}}`` shape, and a minimal duck-typed
implementation of each ``Protocol`` satisfies ``isinstance`` against it
(``runtime_checkable`` is not used here -- structural typing is checked
statically by mypy/pyright in CI, not at runtime -- so these tests
instead assert that a hand-written implementation can be called through
each Protocol's declared method signatures without error).
"""

from __future__ import annotations

from pathlib import Path

from claudeglass.service.contracts import ApiError, ServeOptions, WatcherStats


def test_serve_options_defaults():
    options = ServeOptions(projects_root=Path("/data/claude/projects"), config_dir=Path("/data/claudeglass"))
    assert options.port == 8765
    assert options.bind == "127.0.0.1"
    assert options.poll_interval_s == 30.0
    assert options.retention_days is None
    assert options.exclude_projects == ()


def test_serve_options_overrides():
    options = ServeOptions(
        projects_root=Path("/x"),
        config_dir=Path("/y"),
        port=9000,
        bind="0.0.0.0",
        poll_interval_s=10.0,
        retention_days=90,
        exclude_projects=("secret-repo",),
    )
    assert options.port == 9000
    assert options.exclude_projects == ("secret-repo",)


def test_serve_options_billing_mode_and_monthly_report_dir_defaults():
    options = ServeOptions(projects_root=Path("/x"), config_dir=Path("/y"))
    assert options.billing_mode == "api"
    assert options.monthly_report_dir is None


def test_serve_options_billing_mode_and_monthly_report_dir_overrides():
    options = ServeOptions(
        projects_root=Path("/x"),
        config_dir=Path("/y"),
        billing_mode="subscription",
        monthly_report_dir=Path("/reports"),
    )
    assert options.billing_mode == "subscription"
    assert options.monthly_report_dir == Path("/reports")


def test_watcher_stats_defaults_are_all_zero_or_empty():
    stats = WatcherStats()
    assert stats.files_scanned == 0
    assert stats.files_parsed == 0
    assert stats.files_skipped_live == 0
    assert stats.files_removed == 0
    assert stats.transcripts_missing == 0
    assert stats.sessions_upserted == 0
    assert stats.errors == 0
    assert stats.error_messages == ()


def test_api_error_to_envelope_shape():
    error = ApiError(status=404, code="not_found", message="session not found")
    envelope = error.to_envelope()
    assert envelope == {"ok": False, "error": {"code": "not_found", "message": "session not found"}}


class _FakeWatcher:
    """Minimal stand-in satisfying ``contracts.Watcher``'s three
    methods plus its ``last_stats`` attribute, proving the Protocol's
    shape is actually usable this way (a Protocol has no runtime
    enforcement on its own)."""

    def __init__(self) -> None:
        self.started = False
        self.last_stats: WatcherStats | None = None

    def run_once(self) -> WatcherStats:
        self.last_stats = WatcherStats(files_scanned=1)
        return self.last_stats

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False


def test_fake_watcher_satisfies_the_watcher_protocol_shape():
    from claudeglass.service.contracts import Watcher

    watcher: Watcher = _FakeWatcher()
    assert watcher.last_stats is None
    stats = watcher.run_once()
    assert stats.files_scanned == 1
    assert watcher.last_stats is stats
    watcher.start()
    watcher.stop()


def test_fake_api_handler_satisfies_the_api_handler_protocol_shape():
    from claudeglass.service.contracts import ApiHandler

    def handler(store, query, body):
        return 200, {"ok": True, "data": {"echo": query}}

    typed_handler: ApiHandler = handler
    status, payload = typed_handler(None, {"a": "1"}, None)
    assert status == 200
    assert payload == {"ok": True, "data": {"echo": {"a": "1"}}}


def test_fake_make_handler_satisfies_the_make_handler_protocol_shape():
    from claudeglass.service.contracts import MakeHandler

    class _DummyHandlerClass:
        pass

    def make_handler(store, options):
        return _DummyHandlerClass

    typed_factory: MakeHandler = make_handler
    handler_cls = typed_factory(None, ServeOptions(projects_root=Path("."), config_dir=Path(".")))
    assert handler_cls is _DummyHandlerClass
