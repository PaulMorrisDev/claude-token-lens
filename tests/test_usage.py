"""``usage.py``: period bucketing (day/week/month), by-project/by-entrypoint
breakdowns, and the subscription-only five-hour-block table. Built on
synthetic corpora via ``tests/helpers``/``corpus.load_corpus`` (per the
plan's test list), the same construction pattern ``test_corpus.py`` uses.
"""

from __future__ import annotations

from pathlib import Path

from claude_token_lens.config import Config
from claude_token_lens.corpus import load_corpus
from claude_token_lens.pricing import load_pricing
from claude_token_lens.usage import build_section

from helpers import assert_privacy, turn_line, write_jsonl

PRICING = load_pricing()


def _write_top(project_dir: Path, session_id: str, timestamps: list[str], **overrides) -> None:
    write_jsonl(
        project_dir / f"{session_id}.jsonl",
        [turn_line(timestamp=ts, **overrides) for ts in timestamps],
    )


def test_empty_corpus_renders_usage_section_without_exceptions(tmp_path):
    project_dir = tmp_path / "proj-empty"
    project_dir.mkdir()
    corpus = load_corpus([project_dir])
    section = build_section(corpus, PRICING, Config())
    assert section.key == "usage"
    table_names = {t.name for t in section.tables}
    assert table_names == {"by_day", "by_week", "by_month", "by_project", "by_entrypoint", "five_hour_blocks"}
    for table in section.tables:
        assert table.rows == []
    assert_privacy(section)


def test_period_bucketing_across_a_month_boundary(tmp_path):
    # Config.tz=None: usage.py's own _to_local falls back to the
    # machine's own local zone (dt.astimezone()) without ever
    # attempting a named zoneinfo lookup, so this test's expectations
    # (built the same way) hold regardless of whether an IANA tzdata
    # source is installed (e.g. a bare Windows install without the
    # tzdata package never resolves a named zone -- see test_classify.py's
    # _tzdata_has convention for the same issue).
    # Wide margins from midnight UTC (06:00/18:00) so the assigned
    # calendar day/month survives any realistic local UTC offset
    # (-12..+14) without actually crossing into the other bucket --
    # only the deliberate 31-Aug/1-Sep gap between them does that.
    ts_aug = "2026-08-31T06:00:00.000Z"
    ts_sep = "2026-09-01T18:00:00.000Z"
    project_dir = tmp_path / "proj-month"
    project_dir.mkdir()
    _write_top(project_dir, "session-boundary", [ts_aug, ts_sep], input_tokens=100, output_tokens=20)
    corpus = load_corpus([project_dir])
    section = build_section(corpus, PRICING, Config(tz=None))

    from datetime import datetime

    local_aug = datetime.fromisoformat(ts_aug.replace("Z", "+00:00")).astimezone()
    local_sep = datetime.fromisoformat(ts_sep.replace("Z", "+00:00")).astimezone()
    expected_months = {local_aug.strftime("%Y-%m"), local_sep.strftime("%Y-%m")}
    expected_days = {local_aug.strftime("%Y-%m-%d"), local_sep.strftime("%Y-%m-%d")}

    by_month = next(t for t in section.tables if t.name == "by_month")
    months = {row[0] for row in by_month.rows}
    assert months == expected_months

    by_day = next(t for t in section.tables if t.name == "by_day")
    days = {row[0] for row in by_day.rows}
    assert days == expected_days

    # Every period table's turns sum to the corpus's total priced turns (2).
    assert sum(row[2] for row in by_month.rows) == 2
    assert sum(row[2] for row in by_day.rows) == 2


def test_by_project_and_by_entrypoint_totals(tmp_path):
    project_dir = tmp_path / "proj-multi"
    project_dir.mkdir()
    _write_top(project_dir, "session-a", ["2026-09-18T10:00:00.000Z"], input_tokens=100, output_tokens=20)
    _write_top(project_dir, "session-b", ["2026-09-18T11:00:00.000Z"], input_tokens=200, output_tokens=40)

    corpus = load_corpus([project_dir])
    section = build_section(corpus, PRICING, Config())

    by_project = next(t for t in section.tables if t.name == "by_project")
    assert len(by_project.rows) == 1  # both sessions share one project dir/slug
    assert by_project.rows[0][1] == 2  # sessions count

    by_entrypoint = next(t for t in section.tables if t.name == "by_entrypoint")
    assert len(by_entrypoint.rows) == 1
    assert by_entrypoint.rows[0][0] == "unknown"
    assert by_entrypoint.rows[0][2] == 2  # turns


def test_api_billing_skips_five_hour_blocks_with_a_note(tmp_path):
    project_dir = tmp_path / "proj-api"
    project_dir.mkdir()
    _write_top(project_dir, "session-a", ["2026-09-18T10:00:00.000Z"])
    corpus = load_corpus([project_dir])

    section = build_section(corpus, PRICING, Config(billing="api"))
    blocks = next(t for t in section.tables if t.name == "five_hour_blocks")
    assert blocks.rows == []
    assert blocks.notes and "Skipped" in blocks.notes[0]

    for table in section.tables[:3]:
        for column in table.columns:
            if column.key == "cost":
                assert column.label == "Cost"


def test_subscription_billing_populates_five_hour_blocks_and_relabels_cost(tmp_path):
    project_dir = tmp_path / "proj-sub"
    project_dir.mkdir()
    _write_top(project_dir, "session-a", ["2026-09-18T10:00:00.000Z"], input_tokens=100, output_tokens=20)
    corpus = load_corpus([project_dir])

    section = build_section(corpus, PRICING, Config(billing="subscription"))
    blocks = next(t for t in section.tables if t.name == "five_hour_blocks")
    assert len(blocks.rows) == 1
    assert blocks.rows[0][1] == 1  # one session

    for table in section.tables:
        for column in table.columns:
            if column.key == "cost":
                assert column.label == "Cost (list-price equivalent USD)"
    assert any("subscription" in note for note in section.notes)
    assert_privacy(section)
