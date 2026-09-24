"""``report.py``: ``build_report``'s corpus-wide integration of every
WP3-WP9 accumulator into one :class:`~claude_token_lens.model.ReportModel`.
Synthetic corpora are built with ``tests/helpers``/``corpus.load_corpus``
(the same JSONL-fixture pattern ``test_corpus.py`` uses), per the plan's
test list: an empty corpus, a two-session corpus with one subagent,
group-sum invariants, a smoke render through all four renderers, and a
real-fixture check against ``tests/fixtures/real/session-a`` (skipped
when absent).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_token_lens.config import Config
from claude_token_lens.corpus import load_corpus
from claude_token_lens.pricing import load_pricing
from claude_token_lens.report import _SECTION_ORDER, build_report
from claude_token_lens.render.csv_out import write_csv_dir
from claude_token_lens.render.html import render_html
from claude_token_lens.render.json_out import render_json
from claude_token_lens.render.markdown import render_markdown
from claude_token_lens.snapshots import Snapshot

from helpers import assert_privacy, turn_line, user_str_line, write_jsonl

PRICING = load_pricing()


def _write_top(project_dir: Path, session_id: str, n_turns: int = 2, **overrides) -> Path:
    """Write ``n_turns`` assistant lines, applying ``overrides`` (e.g.
    cache_creation_input_tokens/cache_read_input_tokens) to every turn
    uniformly -- simpler than varying it per-turn, and sufficient for
    this file's assertions, which only check totals/sums, not per-turn
    cache shape."""
    path = project_dir / f"{session_id}.jsonl"
    write_jsonl(
        path,
        [turn_line(input_tokens=100 + i, output_tokens=20 + i, **overrides) for i in range(n_turns)],
    )
    return path


def _write_subagent(project_dir: Path, session_id: str, agent_id: str, n_turns: int = 1, meta: dict | None = None) -> Path:
    agent_dir = project_dir / session_id / "subagents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = agent_dir / f"{agent_id}.jsonl"
    write_jsonl(jsonl_path, [turn_line(input_tokens=50 + i, output_tokens=10 + i) for i in range(n_turns)])
    meta_path = agent_dir / f"{agent_id}.meta.json"
    payload = {"agentType": "claude-implementer", "model": "claude-sonnet-5"}
    if meta:
        payload.update(meta)
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    return jsonl_path


def _all_sections_row_keys_are_valid(sections) -> None:
    for section in sections:
        for table in section.tables:
            for row in table.rows:
                assert row, f"{section.key}/{table.name}: empty row"
                key = row[0]
                assert isinstance(key, (str, int)) and not isinstance(key, bool), (
                    f"{section.key}/{table.name}: row[0]={key!r} is not a str/int key"
                )
                if isinstance(key, str):
                    assert key != "", f"{section.key}/{table.name}: row[0] is empty string"


# -- empty corpus ----------------------------------------------------------


def test_empty_corpus_renders_without_exceptions(tmp_path):
    project_dir = tmp_path / "proj-empty"
    project_dir.mkdir()
    corpus = load_corpus([project_dir])
    report = build_report(corpus, PRICING, Config(), projects=("proj-empty",), window="last 7 days")

    assert [s.key for s in report.sections] == [k for k in _SECTION_ORDER if k not in ("phases", "config", "elasticity")]
    overview = next(s for s in report.sections if s.key == "overview")
    totals = {row[0]: row[1] for row in overview.tables[0].rows}
    assert totals["sessions"] == 0
    assert totals["total_cost_usd"] == 0.0
    assert overview.notes  # "no sessions" note present

    for section in report.sections:
        assert_privacy(section)
    _all_sections_row_keys_are_valid(report.sections)

    # Every renderer must still succeed on an empty report.
    assert render_markdown(report)
    assert render_json(report)
    assert render_html(report)


# -- two sessions, one subagent ---------------------------------------------


def _two_session_corpus(tmp_path: Path):
    project_dir = tmp_path / "proj-two"
    project_dir.mkdir()
    _write_top(project_dir, "session-001", n_turns=2, cache_creation_input_tokens=1000, cache_read_input_tokens=100)
    _write_top(project_dir, "session-002", n_turns=3, cache_creation_input_tokens=500, cache_read_input_tokens=50)
    _write_subagent(project_dir, "session-002", "agent-aaa111", n_turns=2)
    return load_corpus([project_dir])


def test_two_session_corpus_with_one_subagent_builds_every_section(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="last 7 days")

    overview = next(s for s in report.sections if s.key == "overview")
    totals = {row[0]: row[1] for row in overview.tables[0].rows}
    assert totals["sessions"] == 2
    assert totals["top_level_transcripts"] == 2
    assert totals["subagent_transcripts"] == 1
    # 2 + 3 top-level turns + 2 subagent turns = 7 priced turns.
    assert totals["priced_turns"] == 7
    assert totals["total_cost_usd"] > 0.0

    _all_sections_row_keys_are_valid(report.sections)
    for section in report.sections:
        assert_privacy(section)


def test_subagents_are_never_double_counted(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="last 7 days")
    overview = next(s for s in report.sections if s.key == "overview")
    totals = {row[0]: row[1] for row in overview.tables[0].rows}
    # 5 top-level + 2 subagent = 7, not e.g. double-counted to 9 or 14.
    assert totals["priced_turns"] == 7


# -- group-sum invariant -----------------------------------------------------


def test_by_model_group_sums_equal_overview_totals(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="last 7 days")
    overview = next(s for s in report.sections if s.key == "overview")
    totals = {row[0]: row[1] for row in overview.tables[0].rows}
    by_model = overview.tables[1]

    assert sum(row[1] for row in by_model.rows) == totals["priced_turns"]
    assert sum(row[2] for row in by_model.rows) == totals["input_tokens"]
    assert sum(row[3] for row in by_model.rows) == totals["cache_creation_tokens"]
    assert sum(row[4] for row in by_model.rows) == totals["cache_read_tokens"]
    assert sum(row[5] for row in by_model.rows) == totals["output_tokens"]
    assert sum(row[6] for row in by_model.rows) == pytest.approx(totals["total_cost_usd"])


def _write_session_with_ctx_values(
    project_dir: Path, session_id: str, top_ctx_values: list[int], sub_ctx_values: list[int]
) -> None:
    """Write one session whose top-level turns' ``ctx`` (== ``input_tokens``
    here -- no cache tokens involved) are exactly ``top_ctx_values`` and
    whose single subagent's turns' ``ctx`` are exactly ``sub_ctx_values``,
    so a test can assert precisely which set a stat was computed from.
    """
    write_jsonl(
        project_dir / f"{session_id}.jsonl",
        [turn_line(input_tokens=v, output_tokens=20) for v in top_ctx_values],
    )
    if sub_ctx_values:
        agent_dir = project_dir / session_id / "subagents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        write_jsonl(
            agent_dir / "agent-ctx.jsonl",
            [turn_line(input_tokens=v, output_tokens=20) for v in sub_ctx_values],
        )
        (agent_dir / "agent-ctx.meta.json").write_text(
            json.dumps({"agentType": "claude-implementer", "model": "claude-sonnet-5"}), encoding="utf-8"
        )


def test_scorecard_ctx_stats_use_top_level_transcripts_only(tmp_path, monkeypatch):
    """Regression test for review finding R7: the scorecard's
    context-hygiene ctx values were built from every transcript's turns,
    not top-level only, so a subagent with a much bigger ctx (subagents
    typically start from a large system-prompt/task payload) skewed both
    the median and the p90 upward.
    """
    project_dir = tmp_path / "proj-ctx"
    project_dir.mkdir()
    _write_session_with_ctx_values(project_dir, "session-ctx", top_ctx_values=[100, 200], sub_ctx_values=[500_000])

    corpus = load_corpus([project_dir])

    from claude_token_lens import report as report_mod

    captured = {}
    original_build_section = report_mod.scorecard.build_section

    def _capture(inputs, th):
        captured["inputs"] = inputs
        return original_build_section(inputs, th)

    monkeypatch.setattr(report_mod.scorecard, "build_section", _capture)

    build_report(corpus, PRICING, Config(), projects=("proj-ctx",), window="w")

    inputs = captured["inputs"]
    # Top-level turns only: ctx values [100, 200]. If the subagent's
    # 500_000-token turn leaked in, both stats would be orders of
    # magnitude bigger.
    assert inputs.median_top_level_ctx == pytest.approx(150.0)
    assert inputs.p90_top_level_ctx == pytest.approx(200.0)


def test_scorecard_context_hygiene_threshold_scales_for_1m_window_model(tmp_path):
    """D2/COV-12: ``ScorecardThresholds.context_p90_ctx``'s defaults
    (50k/100k/150k/200k) were sized for the 200k-window assumption. A
    corpus run entirely on ``claude-sonnet-5`` (natively 1M, V24) with a
    p90 top-level ctx of 300_000 scored "very poor" (level 1) against
    the flat default even though 300k tokens is a small fraction of that
    model's actual window; report assembly now scales the thresholds by
    the corpus's own resolved model window (5x here), landing 300_000 at
    level 4 instead.
    """
    project_dir = tmp_path / "proj-1m"
    project_dir.mkdir()
    # turn_line's default model is claude-sonnet-5 (1M context in the
    # packaged pricing.toml), so no override is needed here.
    _write_session_with_ctx_values(
        project_dir, "session-1m", top_ctx_values=[50_000, 100_000, 250_000, 300_000], sub_ctx_values=[]
    )

    corpus = load_corpus([project_dir])
    report = build_report(corpus, PRICING, Config(), projects=("proj-1m",), window="w")
    scorecard_section = next(s for s in report.sections if s.key == "scorecard")
    dim_table = next(t for t in scorecard_section.tables if t.name == "dimensions")
    row = next(r for r in dim_table.rows if r[0] == "context_hygiene")

    assert row[4] == pytest.approx(300_000.0)  # value: p90_top_level_ctx
    assert row[1] == 4  # level: level 1 under the unscaled 200k-tuned default


def test_overview_long_context_share_is_top_level_turn_count_basis(tmp_path):
    """Coordinator follow-up to R7: the overview's "long-context share of
    recent top-level turns" verification anchor needs a turn-count-basis,
    top-level-only stat to check against. Top-level ctx values
    [50_000, 100_000, 250_000, 300_000] -> median 175_000.0, and 2 of 4
    (50%) are >= huge_ctx (200_000). A subagent turn with an even bigger
    ctx must not shift either figure.
    """
    project_dir = tmp_path / "proj-ctx2"
    project_dir.mkdir()
    _write_session_with_ctx_values(
        project_dir,
        "session-ctx2",
        top_ctx_values=[50_000, 100_000, 250_000, 300_000],
        sub_ctx_values=[900_000],
    )

    corpus = load_corpus([project_dir])
    report = build_report(corpus, PRICING, Config(), projects=("proj-ctx2",), window="w")
    overview = next(s for s in report.sections if s.key == "overview")
    totals = {row[0]: row[1] for row in overview.tables[0].rows}

    assert totals["top_level_median_ctx"] == pytest.approx(175_000.0)
    assert totals["top_level_turns_ctx_ge_200k_pct"] == pytest.approx(50.0)


def test_recache_by_group_table_rows_sum_to_the_ungrouped_summary(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(
        corpus, PRICING, Config(), projects=("proj-two",), window="last 7 days", group_by="mode"
    )
    recache_section = next(s for s in report.sections if s.key == "recache")
    summary_row = recache_section.tables[0].rows[0]  # recache_summary: metric, transcripts, priced_turns, ...
    group_table = next(t for t in recache_section.tables if t.name == "recache_by_group")
    assert group_table.rows
    # group column prepended, so index 1 onward mirrors recache_summary's columns (metric first).
    assert sum(row[2] for row in group_table.rows) == summary_row[1]  # transcripts
    assert sum(row[3] for row in group_table.rows) == summary_row[2]  # priced_turns


# -- v3-limits wiring --------------------------------------------------------


def _corpus_with_one_limit_hit(tmp_path: Path):
    # Mirrors tests/test_limits.py's own _session_limit_fixture: a
    # synthetic session-limit LIMIT_HIT, a human resume message, then a
    # post-pause turn that necessarily did a full-expiry re-cache.
    project_dir = tmp_path / "proj-limit"
    project_dir.mkdir()
    lines = [
        turn_line(message_id="msg_1", input_tokens=30_000, timestamp="2026-09-18T12:00:00.000Z"),
        turn_line(
            message_id="msg_synth",
            model="<synthetic>",
            isApiErrorMessage=True,
            input_tokens=0,
            output_tokens=0,
            content=[{"type": "text", "text": "You've hit your session limit · resets 3pm (Europe/London)"}],
            timestamp="2026-09-18T12:00:05.000Z",
        ),
        user_str_line(
            "I hit my usage limit while you were working, but it has reset now.",
            promptSource="sdk",
            origin={"kind": "human"},
            timestamp="2026-09-18T15:00:00.000Z",
        ),
        turn_line(
            message_id="msg_2",
            input_tokens=30_000,
            cache_creation_input_tokens=25_000,
            cache_read_input_tokens=0,
            timestamp="2026-09-18T15:00:10.000Z",
        ),
    ]
    write_jsonl(project_dir / "session-limit.jsonl", lines)
    return load_corpus([project_dir])


def test_limits_section_and_scorecard_receive_the_limit_hit(tmp_path):
    corpus = _corpus_with_one_limit_hit(tmp_path)
    report = build_report(corpus, PRICING, Config(), projects=("proj-limit",), window="w")

    limits_section = next(s for s in report.sections if s.key == "limits")
    summary_row = {c.key: v for c, v in zip(limits_section.tables[0].columns, limits_section.tables[0].rows[0])}
    assert summary_row["limit_hits"] == 1
    assert summary_row["session_limit_hits"] == 1
    assert summary_row["sessions_affected"] == 1
    assert summary_row["pause_count"] == 1
    assert summary_row["limit_turn_cc_tokens"] == 25_000

    assert any(a.startswith("a usage-cap pause's") for a in report.meta.assumptions)

    scorecard_section = next(s for s in report.sections if s.key == "scorecard")
    dimensions_table = next(t for t in scorecard_section.tables if t.name == "dimensions")
    # data_quality's note fires whenever ScorecardInputs.limit_pause_sessions
    # > 0 (scorecard.py's _data_quality) -- proves report.py actually
    # threaded LimitStats.sessions_affected through, not just built the
    # section.
    assert any("usage-limit pause" in note for note in dimensions_table.notes if note)

    for section in report.sections:
        assert_privacy(section)
    _all_sections_row_keys_are_valid(report.sections)


# -- subscription vs api labelling (delegated to usage.py, exercised here) --


def test_usage_section_cost_label_reflects_billing_mode(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    api_report = build_report(corpus, PRICING, Config(billing="api"), projects=("proj-two",), window="w")
    sub_report = build_report(corpus, PRICING, Config(billing="subscription"), projects=("proj-two",), window="w")

    api_usage = next(s for s in api_report.sections if s.key == "usage")
    sub_usage = next(s for s in sub_report.sections if s.key == "usage")

    api_project_table = next(t for t in api_usage.tables if t.name == "by_project")
    sub_project_table = next(t for t in sub_usage.tables if t.name == "by_project")
    assert api_project_table.columns[-1].label == "Cost"
    assert sub_project_table.columns[-1].label == "Cost (list-price equivalent USD)"


# -- phases / snapshots / include -------------------------------------------


def test_phases_flag_adds_phases_section(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="w", phases=True)
    assert "phases" in [s.key for s in report.sections]

    report_off = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="w", phases=False)
    assert "phases" not in [s.key for s in report_off.sections]


def test_snapshots_add_config_section(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    snaps = [
        Snapshot(path="cfg1", ts="2026-09-01T00:00:00.000Z", data={"billing": "api"}),
        Snapshot(path="cfg2", ts="2026-09-19T00:00:00.000Z", data={"billing": "subscription"}),
    ]
    report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="w", snapshots=snaps)
    assert "config" in [s.key for s in report.sections]

    report_none = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="w", snapshots=None)
    assert "config" not in [s.key for s in report_none.sections]


def test_include_restricts_to_named_sections(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(
        corpus, PRICING, Config(), projects=("proj-two",), window="w", include={"overview", "recache"}
    )
    assert [s.key for s in report.sections] == ["overview", "recache"]


# -- v0.3 Task 2: baseline_comparison ---------------------------------------


def _minimal_baseline_record(**overrides) -> dict:
    record = {
        "id": "abc123",
        "created_at": "2026-09-01T00:00:00+00:00",
        "window_days": 7,
        "sessions_analysed": 6,
        "mode_mix": {},
        "cost_per_session": 0.01,
        "recache_share_pct": 5.0,
        "compactions_per_session": 0.5,
        "ttl_mix_top_level": {"5m_pct": 80.0, "1h_pct": 20.0},
        "ttl_mix_by_agent_type": {"claude-implementer": {"5m_pct": 90.0, "1h_pct": 10.0}},
        "session_baseline_size": 1234.0,
        "mean_spawn_write_by_agent_type": {"claude-implementer": 500.0},
        "scorecard_dimensions": {"cache_efficiency": 3, "context_hygiene": 4},
        "by_mode": {},
    }
    record.update(overrides)
    return record


def test_baseline_record_adds_baseline_comparison_section(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    baseline_record = _minimal_baseline_record()
    report = build_report(
        corpus, PRICING, Config(), projects=("proj-two",), window="w", baseline_record=baseline_record
    )
    assert "baseline_comparison" in [s.key for s in report.sections]
    section = next(s for s in report.sections if s.key == "baseline_comparison")
    overview_table = next(t for t in section.tables if t.name == "baseline_comparison_overview")
    metrics = {row[0] for row in overview_table.rows}
    assert "Cost per session" in metrics
    assert "Re-cache share of cache-creation" in metrics
    assert "Compactions per session" in metrics
    assert any(m.startswith("TTL mix - top-level") for m in metrics)
    assert any(m.startswith("TTL mix - claude-implementer") for m in metrics)
    assert any(m.startswith("Mean spawn write - claude-implementer") for m in metrics)
    assert any(m.startswith("Scorecard level - ") for m in metrics)
    # baseline_comparison is not part of _SECTION_ORDER's include-filtering
    # contract -- it bypasses `include` deliberately (see build_report's
    # own docstring), so a focused-view call still gets it.
    focused = build_report(
        corpus,
        PRICING,
        Config(),
        projects=("proj-two",),
        window="w",
        include={"overview"},
        baseline_record=baseline_record,
    )
    assert [s.key for s in focused.sections] == ["overview", "baseline_comparison"]
    for section in report.sections:
        assert_privacy(section)


def test_baseline_comparison_by_mode_table_only_when_mode_mix_recorded(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    no_mode_record = _minimal_baseline_record(mode_mix={})
    report = build_report(
        corpus, PRICING, Config(), projects=("proj-two",), window="w", baseline_record=no_mode_record
    )
    section = next(s for s in report.sections if s.key == "baseline_comparison")
    assert [t.name for t in section.tables] == ["baseline_comparison_overview"]

    with_mode_record = _minimal_baseline_record(
        mode_mix={"interactive": 6}, by_mode={"interactive": {"sessions": 6, "cost_per_session": 0.01}}
    )
    report2 = build_report(
        corpus, PRICING, Config(), projects=("proj-two",), window="w", baseline_record=with_mode_record
    )
    section2 = next(s for s in report2.sections if s.key == "baseline_comparison")
    assert "baseline_comparison_by_mode" in [t.name for t in section2.tables]
    by_mode_table = next(t for t in section2.tables if t.name == "baseline_comparison_by_mode")
    # The table's row set is the union of the baseline's recorded modes and
    # whatever mode(s) the current window's own sessions classified as --
    # "interactive" (baseline-only, 0 current sessions) is always present;
    # any modes the current corpus itself produced are additional rows,
    # not a mismatch.
    assert "interactive" in {row[0] for row in by_mode_table.rows}


def test_baseline_comparison_by_mode_suppresses_below_min_sample(tmp_path):
    corpus = _two_session_corpus(tmp_path)  # only 2 sessions, below the 5-session gate
    record = _minimal_baseline_record(mode_mix={"interactive": 6}, by_mode={"interactive": {"sessions": 6}})
    report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="w", baseline_record=record)
    section = next(s for s in report.sections if s.key == "baseline_comparison")
    by_mode_table = next(t for t in section.tables if t.name == "baseline_comparison_by_mode")
    row = next(r for r in by_mode_table.rows if r[0] == "interactive")
    sample_ok_index = [c.key for c in by_mode_table.columns].index("sample_ok")
    assert row[sample_ok_index] == "no"


def test_no_baseline_record_omits_section_and_no_note_by_default(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="w")
    assert "baseline_comparison" not in [s.key for s in report.sections]
    assert not any("baseline" in a.lower() for a in report.meta.assumptions)


def test_baseline_note_is_recorded_in_assumptions_when_no_record(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(
        corpus,
        PRICING,
        Config(),
        projects=("proj-two",),
        window="w",
        baseline_record=None,
        baseline_note="run `claude-token-lens baseline` first",
    )
    assert "baseline_comparison" not in [s.key for s in report.sections]
    assert "run `claude-token-lens baseline` first" in report.meta.assumptions


# -- meta ---------------------------------------------------------------


def test_report_meta_is_fully_populated(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="last 7 days")
    meta = report.meta
    assert meta.window == "last 7 days"
    assert meta.projects == ("proj-two",)
    assert meta.pricing.version == PRICING.version
    assert meta.pricing.sha8 == PRICING.sha8
    assert meta.pricing.currency == PRICING.currency
    assert 0.0 <= meta.pricing.coverage_pct <= 100.0
    assert meta.billing_mode == "api"
    assert meta.thresholds["recache"]["ctx_floor"] == 20_000
    assert meta.assumptions  # ttl + recache assumptions merged in
    assert meta.tool_version
    assert meta.generated_at.endswith("Z")
    # UX-1: meta.units {mode, share_per_usd, period_label, basis} -- the
    # JS mirror's (app.js money()) only source of billing-mode facts.
    assert meta.units["mode"] == "api"
    assert meta.units["share_per_usd"] is None  # API billing has no window share
    assert meta.units["period_label"] == "weekly usage limit"
    assert meta.units["basis"] == meta.amounts_basis


def test_report_meta_units_reflects_subscription_billing_with_no_elasticity_fit(tmp_path):
    """UX-1: under a subscription with no elasticity fit yet (this
    corpus logs no statusline usage-limit samples), ``meta.units``
    still reports ``mode == "subscription"`` and a ``None`` share
    rather than crashing or silently defaulting to API's shape."""
    corpus = _two_session_corpus(tmp_path)
    report = build_report(
        corpus, PRICING, Config(billing="subscription"), projects=("proj-two",), window="last 7 days"
    )
    assert report.meta.units["mode"] == "subscription"
    assert report.meta.units["share_per_usd"] is None
    assert report.meta.units["period_label"] == "weekly usage limit"
    assert report.meta.units["basis"] == report.meta.amounts_basis


def test_thresholds_min_sample_reflects_recommend_overrides_not_config_defaults(tmp_path):
    """Regression test for the min-sample header fix: the thresholds
    header used to print ``config.min_sessions``/``config.min_turns``
    directly, but ``recommend.RecommendThresholds`` has its own
    independently overridable ``min_sessions``/``min_turns`` (via
    ``[thresholds.recommend]``), which is what ``recommend()`` actually
    gates on. A config that leaves the top-level fields at their defaults
    but overrides ``[thresholds.recommend]`` must show the *override* in
    the header, not the stale top-level default.
    """
    corpus = _two_session_corpus(tmp_path)
    config = Config(thresholds={"recommend": {"min_sessions": 42, "min_turns": 4242}})
    assert config.min_sessions == 5  # top-level default, deliberately left untouched
    assert config.min_turns == 200

    report = build_report(corpus, PRICING, config, projects=("proj-two",), window="w")
    assert report.meta.thresholds["min_sessions"] == 42
    assert report.meta.thresholds["min_turns"] == 4242


# -- smoke: full render through every renderer -------------------------


def test_full_report_renders_through_every_renderer(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(
        corpus,
        PRICING,
        Config(),
        projects=("proj-two",),
        window="last 7 days",
        phases=True,
        snapshots=[Snapshot(path="cfg1", ts="2026-09-01T00:00:00.000Z", data={"billing": "api"})],
    )

    md = render_markdown(report)
    assert isinstance(md, str) and md

    js = render_json(report)
    parsed = json.loads(js)
    assert "report" in parsed
    assert "sections" in parsed["report"]

    html = render_html(report)
    assert isinstance(html, str) and html

    out_dir = tmp_path / "csv-out"
    write_csv_dir(report, out_dir)
    assert list(out_dir.rglob("*.csv"))


# -- real fixture (skipped when absent) --------------------------------

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "real" / "session-a"

pytestmark_real = pytest.mark.skipif(
    not FIXTURE_DIR.exists() or not any(FIXTURE_DIR.glob("*.jsonl")),
    reason="tests/fixtures/real/session-a/ not present (real fixture not checked out)",
)


@pytestmark_real
def test_build_report_against_real_fixture():
    corpus = load_corpus([FIXTURE_DIR])
    report = build_report(corpus, PRICING, Config(), projects=("session-a",), window="real fixture")

    assert [s.key for s in report.sections] == [k for k in _SECTION_ORDER if k not in ("phases", "config", "elasticity")]
    overview = next(s for s in report.sections if s.key == "overview")
    totals = {row[0]: row[1] for row in overview.tables[0].rows}
    assert totals["sessions"] > 0
    assert totals["priced_turns"] > 0
    assert totals["total_cost_usd"] > 0.0

    for section in report.sections:
        assert_privacy(section)
    _all_sections_row_keys_are_valid(report.sections)


# -- R5: group_by="agent" must key by transcript, not session ---------------


@pytestmark_real
def test_recache_by_group_agent_matches_recache_by_agent_type_on_real_fixture():
    """Regression test for review finding R5: the group-by re-fold used to
    key every transcript by its *session's* one dominant group, so every
    subagent in a session inherited that session's single label even when
    the session spawned several different agent types. The real fixture's
    one session spawns claude-implementer/general-purpose/revixo-researcher/
    verification-runner subagents (plus the top-level transcript), so a
    correct per-transcript ``group_by="agent"`` re-fold must produce one
    ``recache_by_group`` row per agent_type -- matching
    ``recache_by_agent_type`` exactly -- rather than collapsing them all
    into whichever single agent type the session-keyed lookup used to pick.
    """
    corpus = load_corpus([FIXTURE_DIR])
    report = build_report(
        corpus, PRICING, Config(), projects=("session-a",), window="real fixture", group_by="agent"
    )
    recache_section = next(s for s in report.sections if s.key == "recache")
    group_table = next(t for t in recache_section.tables if t.name == "recache_by_group")
    by_agent_type_table = next(t for t in recache_section.tables if t.name == "recache_by_agent_type")

    assert len(by_agent_type_table.rows) > 1  # the fixture spawns several distinct agent types
    assert len(group_table.rows) == len(by_agent_type_table.rows)

    priced_turns_by_agent_type = {row[0]: row[1] for row in by_agent_type_table.rows}
    recache_turns_by_agent_type = {row[0]: row[2] for row in by_agent_type_table.rows}
    for row in group_table.rows:
        label = row[0]
        assert label in priced_turns_by_agent_type, f"unexpected group label {label!r}"
        # group column prepended: row[1]=metric, row[2]=transcripts, row[3]=priced_turns, row[4]=recache_turns.
        assert row[3] == priced_turns_by_agent_type[label]
        assert row[4] == recache_turns_by_agent_type[label]


def test_limit_recache_share_counts_only_limit_expiry_rebuilds():
    from claude_token_lens.model import Turn
    from claude_token_lens.report import _recache_shares

    turns = [
        Turn(cache_creation_tokens=100, is_recache=True, recache_signature="limit-expiry", gap_cause="limit"),
        # After a limit pause, but the cache survived: not a rebuild.
        Turn(cache_creation_tokens=300, gap_cause="limit"),
        Turn(cache_creation_tokens=600),
    ]
    recache_share, limit_share = _recache_shares(turns)
    assert recache_share == 10.0
    assert limit_share == 10.0
    assert _recache_shares([]) == (None, None)


def test_session_records_carry_the_profile_active_at_their_start(tmp_path, monkeypatch):
    # The apply stamp for "lean" predates the session's first turn, so
    # its SessionRecord is filled in with that profile.
    from claude_token_lens import report as report_mod

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    write_jsonl(project_dir / "s1.jsonl", [turn_line(timestamp="2026-09-18T12:00:00.000Z")])
    config_dir = tmp_path / "config"
    (config_dir / "snapshots").mkdir(parents=True)
    (config_dir / "snapshots" / "20260918T110000Z.json").write_text(
        json.dumps({"ts": "20260918T110000Z", "schema_version": 2, "profile_id": "lean"}), encoding="utf-8"
    )
    seen: list = []
    real = report_mod.workstyle.build_section
    monkeypatch.setattr(report_mod.workstyle, "build_section", lambda records: seen.extend(records) or real(records))

    build_report(load_corpus([project_dir]), PRICING, Config(), projects=("proj",), window="w", config_dir=config_dir)

    assert [r.profile_id for r in seen] == ["lean"]
