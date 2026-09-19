"""Tests for the v0.3 ``init``/onboarding milestone's
``src/claude_token_lens/baseline.py``: :class:`~claude_token_lens.
baseline.CaptureStatus`, the report-table extraction helpers (hand-built
:class:`~claude_token_lens.model.Section`/``Table`` objects, the same
"pin the consumer against a fixture, not a real transcript" approach
``test_scorecard.py`` already uses for a report-table consumer), the
overnight-majority profile override, the billing-mismatch heuristic, and
the end-to-end :func:`~claude_token_lens.baseline.build_baseline` +
JSON persistence + Markdown report round trip against synthetic corpora
built with ``tests/helpers``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_token_lens import baseline
from claude_token_lens.config import Config
from claude_token_lens.corpus import load_corpus
from claude_token_lens.model import Column, ReportModel, Section, Table
from claude_token_lens.pricing import load_pricing

from helpers import assert_privacy_deep, turn_line, write_jsonl

PRICING = load_pricing()


def _write_session(project_dir: Path, session_id: str, n_turns: int = 3) -> None:
    write_jsonl(
        project_dir / f"{session_id}.jsonl",
        [turn_line(input_tokens=100 + i, output_tokens=20 + i) for i in range(n_turns)],
    )


# --------------------------------------------------------------------
# CaptureStatus / format_capture_status
# --------------------------------------------------------------------


def test_capture_status_not_started():
    status = baseline.capture_status(Config())
    assert status.started is False
    assert status.complete is False
    assert "not started" in baseline.format_capture_status(status)


def test_capture_status_in_progress():
    now = datetime(2026, 9, 19, tzinfo=timezone.utc)
    started = (now - timedelta(days=2)).isoformat()
    config = Config(capture_window=7, capture_started=started)
    status = baseline.capture_status(config, now=now)
    assert status.started is True
    assert status.complete is False
    assert status.elapsed_days == pytest.approx(2.0)
    assert status.remaining_days == pytest.approx(5.0)
    assert "in progress" in baseline.format_capture_status(status)


def test_capture_status_complete():
    now = datetime(2026, 9, 19, tzinfo=timezone.utc)
    started = (now - timedelta(days=10)).isoformat()
    config = Config(capture_window=7, capture_started=started)
    status = baseline.capture_status(config, now=now)
    assert status.complete is True
    assert status.remaining_days == 0.0
    assert "complete" in baseline.format_capture_status(status)


def test_capture_status_malformed_started_value_never_raises():
    config = Config(capture_window=7, capture_started="not-a-timestamp")
    status = baseline.capture_status(config)
    assert status.started is True
    assert status.elapsed_days is None
    assert status.complete is False
    assert "could not be read" in baseline.format_capture_status(status)


# --------------------------------------------------------------------
# Report-table extraction helpers (hand-built ReportModel fixtures)
# --------------------------------------------------------------------


def _model_with_sections(*sections: Section) -> ReportModel:
    return ReportModel(sections=list(sections))


def test_table_returns_none_when_section_or_table_missing():
    model = _model_with_sections(Section(key="sessions", title="Sessions", tables=[]))
    assert baseline._table(model, "sessions", "sessions_by_mode") is None
    assert baseline._table(model, "nope", "nope") is None


def test_mode_mix_and_dominant_purposes_from_sessions_section():
    mode_table = Table(
        name="sessions_by_mode",
        columns=[Column(key="value"), Column(key="sessions", kind="int")],
        rows=[["mixed", 6], ["overnight", 4]],
    )
    purpose_table = Table(
        name="sessions_by_purpose",
        columns=[Column(key="value"), Column(key="sessions", kind="int")],
        rows=[["general-dev", 5], ["refactor", 3], ["review", 2]],
    )
    model = _model_with_sections(Section(key="sessions", tables=[mode_table, purpose_table]))

    assert baseline._mode_mix(model) == {"mixed": 6, "overnight": 4}
    assert baseline._dominant_purposes(model) == ["general-dev", "refactor", "review"]
    assert baseline._dominant_purposes(model, limit=1) == ["general-dev"]


def test_corpus_archetype_reads_top_row_of_workstyle_table():
    table = Table(
        name="workstyle_archetypes",
        columns=[Column(key="archetype"), Column(key="sessions", kind="int")],
        rows=[["chat-only", 8], ["mixed", 2]],
    )
    model = _model_with_sections(Section(key="workstyle", tables=[table]))
    assert baseline._corpus_archetype(model) == "chat-only"


def test_corpus_archetype_none_when_no_rows():
    table = Table(name="workstyle_archetypes", columns=[Column(key="archetype")], rows=[])
    model = _model_with_sections(Section(key="workstyle", tables=[table]))
    assert baseline._corpus_archetype(model) is None


def test_scorecard_overall_reads_row():
    table = Table(
        name="overall",
        columns=[Column(key="metric"), Column(key="level", kind="int"), Column(key="label")],
        rows=[["overall", 4, "good"]],
    )
    model = _model_with_sections(Section(key="scorecard", tables=[table]))
    assert baseline._scorecard_overall(model) == (4, "good")


def test_projected_saving_sums_saving_usd_column_by_key_not_index():
    # Deliberately out of the "usual" column order, to prove the column
    # is located by its key, never a hardcoded index.
    table = Table(
        name="ttl_by_agent_type",
        columns=[Column(key="agent_type"), Column(key="lever"), Column(key="saving_usd", kind="money")],
        rows=[["top-level", "ttl-1h", 1.5], ["claude-implementer", "ttl-5m", 2.25]],
    )
    model = _model_with_sections(Section(key="ttl", tables=[table]))
    assert baseline._projected_saving_usd(model) == pytest.approx(3.75)


def test_projected_saving_zero_when_ttl_section_absent():
    model = _model_with_sections(Section(key="sessions", tables=[]))
    assert baseline._projected_saving_usd(model) == 0.0


# --------------------------------------------------------------------
# _suggested_profile: overnight-majority override + catalogue.suggest()
# --------------------------------------------------------------------


def test_suggested_profile_overrides_to_overnight_batch_on_majority():
    profile_id, reason = baseline._suggested_profile(
        {"overnight": 6, "mixed": 4}, archetype="chat-only", purposes=["general-dev"]
    )
    assert profile_id == "overnight-batch"
    assert "overnight" in reason
    assert "sessions_by_mode" in reason


def test_suggested_profile_no_override_below_majority_share():
    profile_id, _reason = baseline._suggested_profile(
        {"overnight": 4, "mixed": 6}, archetype="chat-only", purposes=[]
    )
    assert profile_id != "overnight-batch"


def test_suggested_profile_delegates_to_catalogue_suggest_otherwise():
    from claude_token_lens.profiles import catalogue

    profile_id, reason = baseline._suggested_profile({"mixed": 10}, archetype="workflow-heavy", purposes=[])
    assert profile_id == catalogue.suggest("workflow-heavy", [])
    assert "catalogue.suggest" in reason


def test_suggested_profile_empty_mode_mix_never_overrides():
    profile_id, _reason = baseline._suggested_profile({}, archetype="chat-only", purposes=[])
    assert profile_id != "overnight-batch"


# --------------------------------------------------------------------
# _billing_mismatch_warning
# --------------------------------------------------------------------


def _ttl_model(rows: list[list]) -> ReportModel:
    table = Table(
        name="ttl_by_agent_type",
        columns=[Column(key="agent_type"), Column(key="observed_1h_pct", kind="pct")],
        rows=rows,
    )
    return _model_with_sections(Section(key="ttl", tables=[table]))


def test_billing_mismatch_warns_on_subagent_1h_under_subscription():
    model = _ttl_model([["top-level", 90.0], ["claude-implementer", 12.0]])
    config = Config(billing="subscription")
    warning = baseline._billing_mismatch_warning(config, model)
    assert warning is not None
    assert "claude-implementer" in warning
    assert "subscription" in warning


def test_billing_mismatch_silent_under_api_billing():
    model = _ttl_model([["claude-implementer", 90.0]])
    config = Config(billing="api")
    assert baseline._billing_mismatch_warning(config, model) is None


def test_billing_mismatch_silent_when_only_top_level_shows_1h():
    model = _ttl_model([["top-level", 90.0]])
    config = Config(billing="subscription")
    assert baseline._billing_mismatch_warning(config, model) is None


def test_billing_mismatch_silent_below_threshold():
    model = _ttl_model([["claude-implementer", baseline.BILLING_MISMATCH_THRESHOLD_PCT]])
    config = Config(billing="subscription")
    assert baseline._billing_mismatch_warning(config, model) is None


# --------------------------------------------------------------------
# build_baseline: end to end against synthetic corpora
# --------------------------------------------------------------------


def test_build_baseline_with_no_sessions_is_a_minimal_provisional_record(tmp_path):
    project_dir = tmp_path / "projects" / "empty-proj"
    project_dir.mkdir(parents=True)
    config_dir = tmp_path / "config"

    record, model = baseline.build_baseline(
        config=Config(),
        pricing=PRICING,
        config_dir=config_dir,
        project_dirs=[project_dir],
    )
    assert model is None
    assert record["sessions_analysed"] == 0
    assert record["provisional"] is True
    assert record["mode_mix"] == {}
    assert record["projected_saving_usd"] == 0.0
    assert record["billing_mismatch_warning"] is None


def test_build_baseline_with_sessions_extracts_from_the_real_report(tmp_path):
    project_dir = tmp_path / "projects" / "real-proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "session-1", n_turns=4)
    config_dir = tmp_path / "config"

    record, model = baseline.build_baseline(
        config=Config(),
        pricing=PRICING,
        config_dir=config_dir,
        project_dirs=[project_dir],
    )
    assert model is not None
    assert record["sessions_analysed"] == 1
    assert record["suggested_profile"]
    assert isinstance(record["projected_saving_usd"], float)
    assert record["projects"] == ["real-proj"]


def test_build_baseline_provisional_by_default_before_window_elapses(tmp_path):
    project_dir = tmp_path / "projects" / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "session-1")
    config_dir = tmp_path / "config"
    now = datetime.now(timezone.utc)
    config = Config(capture_window=7, capture_started=now.isoformat())

    record, _model = baseline.build_baseline(
        config=config, pricing=PRICING, config_dir=config_dir, project_dirs=[project_dir], now=now
    )
    assert record["provisional"] is True


def test_build_baseline_finalise_forces_non_provisional(tmp_path):
    project_dir = tmp_path / "projects" / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "session-1")
    config_dir = tmp_path / "config"
    now = datetime.now(timezone.utc)
    config = Config(capture_window=7, capture_started=now.isoformat())

    record, _model = baseline.build_baseline(
        config=config,
        pricing=PRICING,
        config_dir=config_dir,
        project_dirs=[project_dir],
        finalise=True,
        now=now,
    )
    assert record["provisional"] is False


def test_build_baseline_not_provisional_once_window_has_elapsed(tmp_path):
    project_dir = tmp_path / "projects" / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "session-1")
    config_dir = tmp_path / "config"
    now = datetime.now(timezone.utc)
    config = Config(capture_window=7, capture_started=(now - timedelta(days=8)).isoformat())

    record, _model = baseline.build_baseline(
        config=config, pricing=PRICING, config_dir=config_dir, project_dirs=[project_dir], now=now
    )
    assert record["provisional"] is False


def test_build_baseline_projects_are_redacted(tmp_path):
    # Fix 6-style privacy convention: project directory names are run
    # through discovery.redact_slug before ever landing in a baseline
    # record.
    project_dir = tmp_path / "projects" / "Users-alice-work-thing"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "session-1")
    config_dir = tmp_path / "config"

    record, _model = baseline.build_baseline(
        config=Config(), pricing=PRICING, config_dir=config_dir, project_dirs=[project_dir]
    )
    assert record["projects"] == ["Users-<user>-work-thing"]


def test_build_baseline_record_and_markdown_pass_the_privacy_scan(tmp_path):
    project_dir = tmp_path / "projects" / "Users-alice-work-thing"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "session-1", n_turns=5)
    config_dir = tmp_path / "config"

    record, _model = baseline.build_baseline(
        config=Config(), pricing=PRICING, config_dir=config_dir, project_dirs=[project_dir]
    )
    assert_privacy_deep(record)
    markdown = baseline.render_onboarding_report(record)
    assert_privacy_deep({"markdown": markdown})


# --------------------------------------------------------------------
# render_onboarding_report: four named sections
# --------------------------------------------------------------------


def test_render_onboarding_report_has_four_named_sections(tmp_path):
    project_dir = tmp_path / "projects" / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "session-1")
    config_dir = tmp_path / "config"

    record, _model = baseline.build_baseline(
        config=Config(), pricing=PRICING, config_dir=config_dir, project_dirs=[project_dir]
    )
    markdown = baseline.render_onboarding_report(record)
    for heading in ("## Summary", "## Suggested profile", "## Projected saving", "## Next steps"):
        assert heading in markdown


def test_render_onboarding_report_mentions_provisional_next_step_only_when_provisional():
    base_record = {
        "sessions_analysed": 1,
        "provisional": True,
        "archetype": None,
        "dominant_purposes": [],
        "mode_mix": {},
        "scorecard_overall": None,
        "scorecard_label": None,
        "billing_mismatch_warning": None,
        "suggested_profile": "interactive-chat",
        "suggested_profile_reason": "test",
        "projected_saving_usd": 0.0,
    }
    provisional_markdown = baseline.render_onboarding_report(base_record)
    assert "provisional" in provisional_markdown

    final_record = dict(base_record, provisional=False)
    final_markdown = baseline.render_onboarding_report(final_record)
    assert "capture window has not finished" not in final_markdown


# --------------------------------------------------------------------
# Persistence: baselines_dir / save_baseline / list_baselines / load_baseline
# --------------------------------------------------------------------


def test_save_list_load_baseline_round_trip(tmp_path):
    config_dir = tmp_path / "config"
    record = {"id": "abc123", "created_at": "2026-09-19T00:00:00+00:00", "sessions_analysed": 3}

    path = baseline.save_baseline(config_dir, record, report_markdown="# report\n")
    assert path == baseline.baselines_dir(config_dir) / "abc123.json"
    assert path.is_file()
    assert (baseline.baselines_dir(config_dir) / "abc123.md").read_text(encoding="utf-8") == "# report\n"

    loaded = baseline.load_baseline(config_dir, "abc123")
    assert loaded == record

    assert baseline.load_baseline(config_dir, "does-not-exist") is None


def test_list_baselines_empty_directory_returns_empty_list(tmp_path):
    assert baseline.list_baselines(tmp_path / "config") == []


def test_list_baselines_sorted_oldest_first_and_skips_malformed(tmp_path):
    config_dir = tmp_path / "config"
    baseline.save_baseline(config_dir, {"id": "b", "created_at": "2026-09-19T00:00:00+00:00"})
    baseline.save_baseline(config_dir, {"id": "a", "created_at": "2026-09-18T00:00:00+00:00"})
    (baseline.baselines_dir(config_dir) / "corrupt.json").write_text("{not json", encoding="utf-8")

    records = baseline.list_baselines(config_dir)
    assert [r["id"] for r in records] == ["a", "b"]
