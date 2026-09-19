"""Tests for S1-exports' ``monthly.py`` (``claude-token-lens
monthly-report``): ``resolve_month`` default/validation, calendar-month
session attribution (including the documented first-turn-decides
approximation for a session straddling a month boundary), the finance
header tables, and -- the module's own acceptance criterion -- that two
runs over the same inputs produce byte-identical ``.md``/``.html``
files once the single wall-clock "Generated at" line/comment is
stripped, mirroring ``tests/test_cli.py``'s own
``_strip_generated_at``/``_GENERATED_AT_RE`` idempotency convention.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from claude_token_lens import monthly
from claude_token_lens.config import Config
from claude_token_lens.corpus import load_corpus
from claude_token_lens.pricing import load_pricing

from helpers import turn_line, write_jsonl

PRICING = load_pricing()

_GENERATED_AT_MD_RE = re.compile(r"Generated at:.*")
_GENERATED_AT_HTML_RE = re.compile(r"<!-- Generated at:.*-->")


def _strip_generated_at(text: str) -> str:
    text = _GENERATED_AT_MD_RE.sub("Generated at: STRIPPED", text)
    return _GENERATED_AT_HTML_RE.sub("<!-- Generated at: STRIPPED -->", text)


def _write_session(project_dir, session_id: str, timestamps: list[str], **overrides):
    path = project_dir / f"{session_id}.jsonl"
    write_jsonl(path, [turn_line(timestamp=ts, **overrides) for ts in timestamps])
    return path


# -- resolve_month ---------------------------------------------------------


def test_resolve_month_explicit_valid():
    assert monthly.resolve_month("2026-03") == "2026-03"


@pytest.mark.parametrize("bad", ["2026-13", "2026-00", "26-03", "2026/03", "not-a-month", ""])
def test_resolve_month_rejects_malformed(bad):
    if bad == "":
        # Empty string is falsy, so it takes the "default to previous
        # month" branch rather than the validation branch -- covered by
        # test_resolve_month_default_is_previous_calendar_month.
        return
    with pytest.raises(ValueError):
        monthly.resolve_month(bad)


def test_resolve_month_default_is_previous_calendar_month():
    today = date.today()
    if today.month == 1:
        expected = f"{today.year - 1:04d}-12"
    else:
        expected = f"{today.year:04d}-{today.month - 1:02d}"
    assert monthly.resolve_month(None) == expected
    assert monthly.resolve_month("") == expected


# -- filter_corpus_to_month -------------------------------------------------


def test_filter_corpus_to_month_keeps_only_matching_sessions(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    _write_session(project_dir, "aug-session", ["2026-08-15T10:00:00.000Z"])
    _write_session(project_dir, "sep-session", ["2026-09-05T10:00:00.000Z"])
    corpus = load_corpus([project_dir])

    filtered = monthly.filter_corpus_to_month(corpus, "2026-09", None)
    ids = {b.session_id for b in filtered.sessions}
    assert ids == {"sep-session"}


def test_filter_corpus_to_month_attributes_by_first_turn(tmp_path):
    """A session whose first turn is in August but whose later turns
    spill into September is counted wholly in August (the documented
    approximation) -- it must NOT show up under the September filter."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    _write_session(
        project_dir,
        "straddling-session",
        ["2026-08-31T10:00:00.000Z", "2026-09-05T10:00:00.000Z"],
    )
    corpus = load_corpus([project_dir])

    august = monthly.filter_corpus_to_month(corpus, "2026-08", None)
    september = monthly.filter_corpus_to_month(corpus, "2026-09", None)
    assert {b.session_id for b in august.sessions} == {"straddling-session"}
    assert {b.session_id for b in september.sessions} == set()


def test_filter_corpus_to_month_no_matches_returns_empty_corpus(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    _write_session(project_dir, "aug-session", ["2026-08-15T10:00:00.000Z"])
    corpus = load_corpus([project_dir])

    filtered = monthly.filter_corpus_to_month(corpus, "2026-01", None)
    assert filtered.sessions == []
    # Non-session fields are preserved from the original corpus.
    assert filtered.total_files == corpus.total_files


# -- write_monthly_report ----------------------------------------------------


def _build_month_corpus(tmp_path):
    project_dir = tmp_path / "acme"
    project_dir.mkdir(parents=True)
    _write_session(
        project_dir,
        "sep-session-1",
        ["2026-09-05T10:00:00.000Z", "2026-09-05T10:05:00.000Z"],
        model="claude-sonnet-5",
        input_tokens=500,
        output_tokens=100,
    )
    _write_session(
        project_dir,
        "sep-session-2",
        ["2026-09-20T09:00:00.000Z"],
        model="claude-opus-5",
        input_tokens=200,
        output_tokens=50,
    )
    # A session outside the target month, to prove it is excluded from
    # the written report's totals.
    _write_session(project_dir, "aug-session", ["2026-08-01T09:00:00.000Z"])
    return load_corpus([project_dir])


def test_write_monthly_report_writes_md_and_html(tmp_path):
    corpus = _build_month_corpus(tmp_path / "projects")
    out_dir = tmp_path / "out"
    paths = monthly.write_monthly_report(corpus, PRICING, Config(), "2026-09", out_dir)
    assert len(paths) == 2
    md_path, html_path = paths
    assert md_path.name == "claude-token-lens-2026-09.md"
    assert html_path.name == "claude-token-lens-2026-09.html"
    assert md_path.exists()
    assert html_path.exists()


def test_write_monthly_report_md_contains_finance_header(tmp_path):
    corpus = _build_month_corpus(tmp_path / "projects")
    out_dir = tmp_path / "out"
    md_path, _html_path = monthly.write_monthly_report(corpus, PRICING, Config(), "2026-09", out_dir)
    text = md_path.read_text(encoding="utf-8")
    assert "Monthly report — 2026-09" in text
    assert "Finance summary" in text
    assert "Cost by model" in text
    assert "Total cost" in text
    assert "Sessions" in text
    # August-only session must not leak into a September report.
    assert "aug-session" not in text


def test_write_monthly_report_excludes_sessions_outside_month(tmp_path):
    corpus = _build_month_corpus(tmp_path / "projects")
    out_dir = tmp_path / "out"
    md_path, _html_path = monthly.write_monthly_report(corpus, PRICING, Config(), "2026-09", out_dir)
    text = md_path.read_text(encoding="utf-8")
    # 2 September sessions -> "Sessions" row should read 2.
    row_line = next(line for line in text.splitlines() if "| Sessions" in line)
    assert "| 2 |" in row_line


def test_write_monthly_report_five_hour_blocks_only_for_subscription_billing(tmp_path):
    corpus = _build_month_corpus(tmp_path / "projects")
    out_dir_api = tmp_path / "out-api"
    out_dir_sub = tmp_path / "out-sub"
    md_api, _ = monthly.write_monthly_report(corpus, PRICING, Config(billing="api"), "2026-09", out_dir_api)
    md_sub, _ = monthly.write_monthly_report(corpus, PRICING, Config(billing="subscription"), "2026-09", out_dir_sub)
    text_api = md_api.read_text(encoding="utf-8")
    text_sub = md_sub.read_text(encoding="utf-8")
    assert "Five-hour blocks used" not in text_api
    assert "Five-hour blocks used" in text_sub


def test_write_monthly_report_html_has_table_markup(tmp_path):
    corpus = _build_month_corpus(tmp_path / "projects")
    out_dir = tmp_path / "out"
    _md_path, html_path = monthly.write_monthly_report(corpus, PRICING, Config(), "2026-09", out_dir)
    text = html_path.read_text(encoding="utf-8")
    assert "<table>" in text
    assert "Finance summary" in text
    assert "<!-- Generated at:" in text


def test_write_monthly_report_is_idempotent(tmp_path):
    """The acceptance criterion: same inputs -> byte-identical files
    across two separate calls, once the single wall-clock line/comment
    is stripped from each."""
    corpus = _build_month_corpus(tmp_path / "projects")
    out_dir_1 = tmp_path / "out1"
    out_dir_2 = tmp_path / "out2"

    md1, html1 = monthly.write_monthly_report(corpus, PRICING, Config(), "2026-09", out_dir_1)
    md2, html2 = monthly.write_monthly_report(corpus, PRICING, Config(), "2026-09", out_dir_2)

    assert _strip_generated_at(md1.read_text(encoding="utf-8")) == _strip_generated_at(md2.read_text(encoding="utf-8"))
    assert _strip_generated_at(html1.read_text(encoding="utf-8")) == _strip_generated_at(
        html2.read_text(encoding="utf-8")
    )


def test_write_monthly_report_no_sessions_in_month_still_writes_files(tmp_path):
    corpus = _build_month_corpus(tmp_path / "projects")
    out_dir = tmp_path / "out"
    md_path, html_path = monthly.write_monthly_report(corpus, PRICING, Config(), "2020-01", out_dir)
    assert md_path.exists()
    assert html_path.exists()
    text = md_path.read_text(encoding="utf-8")
    assert "Monthly report — 2020-01" in text
