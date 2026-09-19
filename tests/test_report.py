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

from helpers import assert_privacy, turn_line, write_jsonl

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

    assert [s.key for s in report.sections] == [k for k in _SECTION_ORDER if k not in ("phases", "config")]
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


def test_recache_by_group_table_rows_sum_to_the_ungrouped_summary(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(
        corpus, PRICING, Config(), projects=("proj-two",), window="last 7 days", group_by="mode"
    )
    recache_section = next(s for s in report.sections if s.key == "recache")
    summary_row = recache_section.tables[0].rows[0]  # recache_summary: transcripts, priced_turns, ...
    group_table = next(t for t in recache_section.tables if t.name == "recache_by_group")
    assert group_table.rows
    # group column prepended, so index 1 onward mirrors recache_summary's columns.
    assert sum(row[1] for row in group_table.rows) == summary_row[0]  # transcripts
    assert sum(row[2] for row in group_table.rows) == summary_row[1]  # priced_turns


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

    assert [s.key for s in report.sections] == [k for k in _SECTION_ORDER if k not in ("phases", "config")]
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
        # group column prepended: row[1]=transcripts, row[2]=priced_turns, row[3]=recache_turns.
        assert row[2] == priced_turns_by_agent_type[label]
        assert row[3] == recache_turns_by_agent_type[label]
