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
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from claude_token_lens import monthly
from claude_token_lens import statusline
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


def test_write_monthly_report_finance_summary_has_no_bare_dollar_and_uses_units_under_a_subscription(tmp_path):
    """UX-1: the digest's money cells (module docstring's "uses Units"
    wiring item) route through ``model.units``, so a subscription's
    "Total cost" reads as a list-price-equivalent/weekly-usage-limit
    figure, never a bare "$", and the API report is unaffected."""
    corpus = _build_month_corpus(tmp_path / "projects")
    out_dir_api = tmp_path / "out-api"
    out_dir_sub = tmp_path / "out-sub"
    md_api, _ = monthly.write_monthly_report(corpus, PRICING, Config(billing="api"), "2026-09", out_dir_api)
    md_sub, _ = monthly.write_monthly_report(corpus, PRICING, Config(billing="subscription"), "2026-09", out_dir_sub)
    text_api = md_api.read_text(encoding="utf-8")
    text_sub = md_sub.read_text(encoding="utf-8")
    assert "$" not in text_api
    assert "$" not in text_sub
    total_cost_row_sub = next(line for line in text_sub.splitlines() if "| Total cost" in line)
    # No elasticity fit is logged for this synthetic corpus, so Units
    # falls back to its "list-price equivalent" phrasing (units.py's
    # NO_LIMIT_SHARE_HINT branch) -- still proof the subscription path
    # was actually taken, not silently skipped.
    assert "list-price equivalent" in total_cost_row_sub
    total_cost_row_api = next(line for line in text_api.splitlines() if "| Total cost" in line)
    assert "list-price equivalent" not in total_cost_row_api


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


# -- review finding 12: resolve_month uses config.tz, not the machine zone --


def _tzdata_has(name: str) -> bool:
    """Mirrors ``tests/test_classify.py``'s own helper: some machines
    (a bare Windows install without the ``tzdata`` package) have no
    source ``zoneinfo`` can resolve a named zone from at all."""
    try:
        ZoneInfo(name)
        return True
    except ZoneInfoNotFoundError:
        return False


def test_resolve_month_default_uses_given_tz_not_machine_zone():
    """A UTC 'now' of 2026-09-01T00:30 is still August in a tz well
    behind UTC (America/Los_Angeles, UTC-7 in September) -- resolve_month
    must use the *given* tz's previous month, not the machine's own
    zone's. Regression test for review finding 12.

    Both assertions below need real zoneinfo data to resolve the named
    zones; where it's unavailable (see ``_tzdata_has``), resolve_month's
    own documented fallback applies instead (same as an unresolvable tz
    string), so the assertion is against that fallback value rather than
    skipped outright.
    """
    now_utc = datetime(2026, 9, 1, 0, 30, tzinfo=timezone.utc)
    machine_zone_result = monthly.resolve_month(None, None, now=now_utc)

    # America/Los_Angeles is UTC-7 in September, so this instant is still
    # 2026-08-31 there: the *current* local month is August, and the
    # previous one -- what resolve_month must return -- is July.
    if _tzdata_has("America/Los_Angeles"):
        assert monthly.resolve_month(None, "America/Los_Angeles", now=now_utc) == "2026-07"
    else:
        assert monthly.resolve_month(None, "America/Los_Angeles", now=now_utc) == machine_zone_result

    # Pacific/Kiritimati (UTC+14) has already turned into September, so
    # its previous month is August.
    if _tzdata_has("Pacific/Kiritimati"):
        assert monthly.resolve_month(None, "Pacific/Kiritimati", now=now_utc) == "2026-08"
    else:
        assert monthly.resolve_month(None, "Pacific/Kiritimati", now=now_utc) == machine_zone_result


def test_resolve_month_default_with_no_tz_falls_back_to_machine_zone(monkeypatch):
    """Omitting tz entirely must behave exactly as before (machine's own
    zone via astimezone()) -- resolve_month's own _to_local fallback."""
    now_utc = datetime(2026, 9, 1, 0, 30, tzinfo=timezone.utc)
    local = monthly._to_local(now_utc, None)
    expected_year, expected_month = local.year, local.month
    if expected_month == 1:
        expected = f"{expected_year - 1:04d}-12"
    else:
        expected = f"{expected_year:04d}-{expected_month - 1:02d}"
    assert monthly.resolve_month(None, None, now=now_utc) == expected


# -- review finding 9: usage_log_rows threaded through to cache_ground_truth --


def test_write_monthly_report_includes_cache_ground_truth_when_usage_log_rows_given(tmp_path):
    """Regression test for review finding 9 (should-fix): docs/exports.md
    promises a cache_ground_truth table in the monthly report's usage
    section "when a usage log is available", but write_monthly_report
    never accepted usage_log_rows at all, so the promise could never be
    kept. A row for a session inside the target month must now surface
    in the rendered Markdown."""
    corpus = _build_month_corpus(tmp_path / "projects")
    out_dir = tmp_path / "out"

    csv_path = tmp_path / "usage-log.csv"
    now = datetime(2026, 9, 5, 10, 5, tzinfo=timezone.utc)
    statusline._append_context_window_row(
        csv_path,
        {"session_id": "sep-session-1", "prompt_cache": {"warm": True, "misses": 1}},
        now,
    )
    usage_log_rows = statusline.load_usage_log_ground_truth(csv_path)
    assert len(usage_log_rows) == 1

    md_path, _html_path = monthly.write_monthly_report(
        corpus, PRICING, Config(), "2026-09", out_dir, usage_log_rows=usage_log_rows
    )
    text = md_path.read_text(encoding="utf-8")
    assert "Cache health per session, from your statusline" in text


def test_write_monthly_report_scopes_usage_log_rows_to_sessions_in_month(tmp_path):
    """A usage-log row for a session that is NOT part of the filtered
    month must not leak into that month's report (mirrors finding 8's
    window-scoping, applied here to the month scope instead)."""
    corpus = _build_month_corpus(tmp_path / "projects")
    out_dir = tmp_path / "out"

    csv_path = tmp_path / "usage-log.csv"
    now = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
    # aug-session is excluded from the "2026-09" report by month filtering.
    statusline._append_context_window_row(
        csv_path,
        {"session_id": "aug-session", "prompt_cache": {"warm": True, "misses": 1}},
        now,
    )
    usage_log_rows = statusline.load_usage_log_ground_truth(csv_path)

    md_path, _html_path = monthly.write_monthly_report(
        corpus, PRICING, Config(), "2026-09", out_dir, usage_log_rows=usage_log_rows
    )
    text = md_path.read_text(encoding="utf-8")
    assert "aug-session" not in text


# -- review finding 11: generated_at makes output genuinely byte-identical --


def test_write_monthly_report_generated_at_makes_output_genuinely_byte_identical(tmp_path):
    """Passing a fixed generated_at must make two separate calls produce
    byte-identical files with NO stripping needed at all -- stronger
    than test_write_monthly_report_is_idempotent's "apart from one line"
    check, closing review finding 11."""
    corpus = _build_month_corpus(tmp_path / "projects")
    out_dir_1 = tmp_path / "out1"
    out_dir_2 = tmp_path / "out2"

    fixed = "2026-01-01T00:00:00+00:00"
    md1, html1 = monthly.write_monthly_report(corpus, PRICING, Config(), "2026-09", out_dir_1, generated_at=fixed)
    md2, html2 = monthly.write_monthly_report(corpus, PRICING, Config(), "2026-09", out_dir_2, generated_at=fixed)

    assert md1.read_bytes() == md2.read_bytes()
    assert html1.read_bytes() == html2.read_bytes()
    assert fixed in md1.read_text(encoding="utf-8")
    assert fixed in html1.read_text(encoding="utf-8")


# -- nit 21(b): no CLI-level test coverage of monthly-report at all ---------


def test_cli_monthly_report_happy_path_writes_files(tmp_path):
    from claude_token_lens import cli as cli_mod

    projects_root = tmp_path / "projects"
    project_dir = projects_root / "proj-a"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "s1.jsonl",
        [turn_line(timestamp="2026-09-05T10:00:00.000Z", input_tokens=100, output_tokens=20)],
    )
    config_dir = tmp_path / "token-lens"
    out_dir = tmp_path / "out"

    rc = cli_mod.main(
        [
            "monthly-report",
            "--projects-root",
            str(projects_root),
            "--all-projects",
            "--config-dir",
            str(config_dir),
            "--out",
            str(out_dir),
            "--month",
            "2026-09",
        ]
    )
    assert rc == 0
    assert (out_dir / "claude-token-lens-2026-09.md").exists()
    assert (out_dir / "claude-token-lens-2026-09.html").exists()


def test_cli_monthly_report_bad_month_exits_2(tmp_path):
    from claude_token_lens import cli as cli_mod

    projects_root = tmp_path / "projects"
    project_dir = projects_root / "proj-a"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "s1.jsonl",
        [turn_line(timestamp="2026-09-05T10:00:00.000Z", input_tokens=100)],
    )
    config_dir = tmp_path / "token-lens"
    out_dir = tmp_path / "out"

    rc = cli_mod.main(
        [
            "monthly-report",
            "--projects-root",
            str(projects_root),
            "--all-projects",
            "--config-dir",
            str(config_dir),
            "--out",
            str(out_dir),
            "--month",
            "not-a-month",
        ]
    )
    assert rc == 2


def test_cli_monthly_report_no_sessions_in_corpus_exits_1(tmp_path):
    from claude_token_lens import cli as cli_mod

    projects_root = tmp_path / "projects"
    project_dir = projects_root / "empty-proj"
    project_dir.mkdir(parents=True)
    config_dir = tmp_path / "token-lens"
    out_dir = tmp_path / "out"

    rc = cli_mod.main(
        [
            "monthly-report",
            "--projects-root",
            str(projects_root),
            "--all-projects",
            "--config-dir",
            str(config_dir),
            "--out",
            str(out_dir),
        ]
    )
    assert rc == 1


def test_cli_monthly_report_empty_target_month_exits_0_with_stderr_note(tmp_path, capsys):
    """Nit 17: an empty target month still exits 0 and writes zeroed
    tables, but must now say so on stderr rather than failing silently
    -- verified here at the CLI level since tests/test_cli.py is not
    writable for this work package."""
    from claude_token_lens import cli as cli_mod

    projects_root = tmp_path / "projects"
    project_dir = projects_root / "proj-a"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "s1.jsonl",
        [turn_line(timestamp="2026-08-01T09:00:00.000Z", input_tokens=100)],
    )
    config_dir = tmp_path / "token-lens"
    out_dir = tmp_path / "out"

    rc = cli_mod.main(
        [
            "monthly-report",
            "--projects-root",
            str(projects_root),
            "--all-projects",
            "--config-dir",
            str(config_dir),
            "--out",
            str(out_dir),
            "--month",
            "2020-01",
        ]
    )
    assert rc == 0
    assert (out_dir / "claude-token-lens-2020-01.md").exists()
    err = capsys.readouterr().err
    assert "2020-01" in err
    assert "zeroed tables" in err



def test_the_monthly_report_carries_the_work_habits_digest_when_there_is_one(tmp_path):
    from helpers import user_str_line

    corpus = _build_month_corpus(tmp_path / "projects")
    md_path, _ = monthly.write_monthly_report(corpus, PRICING, Config(), "2026-09", tmp_path / "out")
    assert "Work habits" not in md_path.read_text(encoding="utf-8")

    project_dir = tmp_path / "rated" / "acme"
    project_dir.mkdir(parents=True)
    write_jsonl(project_dir / "sep-rated.jsonl", [
        user_str_line("fix it", origin={"kind": "human"}, timestamp="2026-09-10T09:00:00.000Z", sessionId="sep-rated"),
        turn_line(timestamp="2026-09-10T09:00:05.000Z", model="claude-sonnet-5", input_tokens=500, output_tokens=100,
                  sessionId="sep-rated"),
    ])
    rated = load_corpus([project_dir])
    ratings = {"sep-rated": {"outcome": "met", "slow": [], "worth": "yes", "helped": []}}
    md_path, _ = monthly.write_monthly_report(rated, PRICING, Config(), "2026-09", tmp_path / "out2", ratings=ratings)
    text = md_path.read_text(encoding="utf-8")
    assert "Work habits" in text and "Cost per goal met" in text
    assert "1 of 1 pieces you gave feedback on" in text
