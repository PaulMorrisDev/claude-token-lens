"""Tests for V3-compare's ``reconcile.py`` (``claude-token-lens
reconcile``): the tolerant Admin-CSV header mapper (standard headers,
alternate spellings, ``_5m``/``_1h`` cache-creation splits (never added on
top of the flat total), timestamp dates, ``cost_cents``,
unmapped-column reporting), ``ReconcileError``'s "line number only, never
row content" contract, the ``reconcile()`` entry point's local-vs-Admin
delta table (including an exact round-trip where the admin export is
built from the local corpus's own numbers), and the CLI wiring
(``cli.main(["reconcile", ...])`` exit codes and output).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_token_lens import cli
from claude_token_lens import reconcile as reconcile_mod
from claude_token_lens.config import Config
from claude_token_lens.corpus import load_corpus
from claude_token_lens.pricing import load_pricing, price_turn

from helpers import assert_privacy, ignorable_line, turn_line, write_jsonl

PRICING = load_pricing()
CONFIG = Config()


def _write_csv(path: Path, lines: list[str]) -> None:
    # newline="" avoids Path.write_text's universal-newline translation
    # doubling up "\r\n" into "\r\r\n" on Windows, which would otherwise
    # throw csv.reader's line_num off (it counts embedded record
    # terminators, not source lines).
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")


def _table(section):
    assert len(section.tables) == 1
    return section.tables[0]


# -- parse_admin_csv: tolerant header mapping --------------------------------


def test_parse_admin_csv_maps_standard_headers(tmp_path):
    csv_path = tmp_path / "admin.csv"
    _write_csv(
        csv_path,
        [
            "date,model,input_tokens,output_tokens,cache_read_input_tokens,cache_creation_input_tokens,cost",
            "2026-08-05,claude-sonnet-5,500,80,40,120,1.2345",
        ],
    )
    result = reconcile_mod.parse_admin_csv(csv_path)
    assert result.unmapped_headers == []
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row == {
        "date": "2026-08-05",
        "model": "claude-sonnet-5",
        "input_tokens": 500,
        "cache_creation_tokens": 120,
        "cache_read_tokens": 40,
        "output_tokens": 80,
        "cost": 1.2345,
    }


def test_parse_admin_csv_maps_alternate_header_variants(tmp_path):
    csv_path = tmp_path / "admin.csv"
    _write_csv(
        csv_path,
        [
            "usage_date,model_name,uncached_input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,cost_usd",
            "2026-08-06,claude-fable,10,20,30,40,0.5",
        ],
    )
    result = reconcile_mod.parse_admin_csv(csv_path)
    assert result.unmapped_headers == []
    assert result.rows[0]["date"] == "2026-08-06"
    assert result.rows[0]["model"] == "claude-fable"
    assert result.rows[0]["input_tokens"] == 10
    assert result.rows[0]["cache_read_tokens"] == 30
    assert result.rows[0]["cache_creation_tokens"] == 40
    assert result.rows[0]["cost"] == 0.5


def test_parse_admin_csv_sums_5m_and_1h_cache_creation_split(tmp_path):
    csv_path = tmp_path / "admin.csv"
    _write_csv(
        csv_path,
        [
            "date,ephemeral_5m_input_tokens,ephemeral_1h_input_tokens",
            "2026-08-07,100,50",
        ],
    )
    result = reconcile_mod.parse_admin_csv(csv_path)
    assert result.rows[0]["cache_creation_tokens"] == 150


def test_parse_admin_csv_flat_and_split_cache_creation_not_double_counted(tmp_path):
    # Regression: a row carrying the flat total (150) *and* its own 5m/1h
    # split (100 + 50) used to sum all three to 300. The flat column is
    # the split's total, so the row counts 150 cache-creation tokens once.
    csv_path = tmp_path / "admin.csv"
    _write_csv(
        csv_path,
        [
            "date,input_tokens,cache_creation_input_tokens,"
            "cache_creation_ephemeral_5m_input_tokens,cache_creation_ephemeral_1h_input_tokens,output_tokens",
            "2026-08-07,500,150,100,50,80",
            "2026-08-07,10,0,30,20,5",
            "2026-08-07,10,70,,,5",
        ],
    )
    result = reconcile_mod.parse_admin_csv(csv_path)
    assert [r["cache_creation_tokens"] for r in result.rows] == [150, 50, 70]
    # Input tokens stay uncached input only -- cache creation is never
    # folded into them as well.
    assert [r["input_tokens"] for r in result.rows] == [500, 10, 10]


def test_parse_admin_csv_timestamp_date_reduced_to_utc_day(tmp_path):
    csv_path = tmp_path / "admin.csv"
    _write_csv(
        csv_path,
        [
            "bucket_start,input_tokens",
            "2026-08-05T00:00:00Z,1",
            "2026-08-05T22:00:00-05:00,2",
            "2026-08-06,3",
        ],
    )
    result = reconcile_mod.parse_admin_csv(csv_path)
    assert [r["date"] for r in result.rows] == ["2026-08-05", "2026-08-06", "2026-08-06"]


def test_parse_admin_csv_cost_cents_divided_by_100(tmp_path):
    csv_path = tmp_path / "admin.csv"
    _write_csv(csv_path, ["date,cost_cents", "2026-08-08,250"])
    result = reconcile_mod.parse_admin_csv(csv_path)
    assert result.rows[0]["cost"] == pytest.approx(2.5)


def test_parse_admin_csv_reports_unmapped_headers(tmp_path):
    csv_path = tmp_path / "admin.csv"
    _write_csv(csv_path, ["date,some_unknown_column,cost", "2026-08-09,xyz,1.0"])
    result = reconcile_mod.parse_admin_csv(csv_path)
    assert result.unmapped_headers == ["some_unknown_column"]
    assert result.rows[0]["cost"] == 1.0


def test_parse_admin_csv_skips_blank_lines(tmp_path):
    csv_path = tmp_path / "admin.csv"
    _write_csv(csv_path, ["date,cost", "2026-08-10,1.0", "", "2026-08-11,2.0"])
    result = reconcile_mod.parse_admin_csv(csv_path)
    assert len(result.rows) == 2


def test_parse_admin_csv_empty_file_raises(tmp_path):
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("", encoding="utf-8")
    with pytest.raises(reconcile_mod.ReconcileError, match="empty"):
        reconcile_mod.parse_admin_csv(csv_path)


def test_parse_admin_csv_missing_file_raises(tmp_path):
    with pytest.raises(reconcile_mod.ReconcileError, match="cannot open"):
        reconcile_mod.parse_admin_csv(tmp_path / "does-not-exist.csv")


def test_parse_admin_csv_missing_date_column_raises(tmp_path):
    csv_path = tmp_path / "admin.csv"
    _write_csv(csv_path, ["model,cost", "claude-sonnet-5,1.0"])
    with pytest.raises(reconcile_mod.ReconcileError, match="no recognisable date column"):
        reconcile_mod.parse_admin_csv(csv_path)


def test_parse_admin_csv_bad_row_error_names_only_the_line_number(tmp_path):
    csv_path = tmp_path / "admin.csv"
    _write_csv(
        csv_path,
        [
            "date,input_tokens",
            "2026-08-01,10",
            "2026-08-02,not-a-real-number",
        ],
    )
    with pytest.raises(reconcile_mod.ReconcileError) as excinfo:
        reconcile_mod.parse_admin_csv(csv_path)
    message = str(excinfo.value)
    assert message == "cannot parse admin CSV at line 3"
    assert "not-a-real-number" not in message
    assert "2026-08-02" not in message


# -- reconcile(): local vs admin ----------------------------------------


def _write_session(project_dir: Path, session_id: str, timestamp: str, **overrides) -> Path:
    path = project_dir / f"{session_id}.jsonl"
    write_jsonl(path, [turn_line(timestamp=timestamp, **overrides)])
    return path


def test_reconcile_totals_reconcile_exactly_when_admin_built_from_local(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(
        project_dir,
        "s1",
        "2026-08-05T09:00:00.000Z",
        input_tokens=500,
        output_tokens=80,
        cache_read_input_tokens=40,
        cache_creation_input_tokens=120,
    )
    corpus = load_corpus([project_dir])
    turn = corpus.sessions[0].top.turns[0]
    local_cost = price_turn(turn, PRICING.resolve_model(turn.model)).total

    admin_rows = [
        {
            "date": "2026-08-05",
            "model": "claude-sonnet-5",
            "input_tokens": 500,
            "cache_creation_tokens": 120,
            "cache_read_tokens": 40,
            "output_tokens": 80,
            "cost": local_cost,
        }
    ]
    section = reconcile_mod.reconcile(corpus, PRICING, CONFIG, admin_rows=admin_rows, by=("day", "model"))
    table = _table(section)
    assert table.name == "reconcile_by_period"
    # One data row (2026-08-05 / claude-sonnet-5) plus a TOTAL row.
    assert len(table.rows) == 2

    col_index = {c.key: i for i, c in enumerate(table.columns)}
    for row in table.rows:
        assert row[col_index["input_tokens_delta"]] == 0
        assert row[col_index["cache_creation_tokens_delta"]] == 0
        assert row[col_index["cache_read_tokens_delta"]] == 0
        assert row[col_index["output_tokens_delta"]] == 0
        assert row[col_index["cost_delta"]] == pytest.approx(0.0, abs=1e-9)
        pct = row[col_index["cost_delta_pct"]]
        assert pct is None or pct == pytest.approx(0.0, abs=1e-6)

    total_row = table.rows[-1]
    assert total_row[0] == "TOTAL"
    assert total_row[1] == "-"


def test_reconcile_split_admin_export_matches_local_cache_creation(tmp_path):
    # End to end: one local turn wrote 150 cache tokens (100 at 5m, 50 at
    # 1h); the Admin export for that day carries the flat total and the
    # split. The cache-creation delta must be zero, not -150.
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(
        project_dir,
        "s1",
        "2026-08-05T09:00:00.000Z",
        input_tokens=500,
        output_tokens=80,
        cache_creation_input_tokens=150,
        ephemeral_5m_input_tokens=100,
        ephemeral_1h_input_tokens=50,
    )
    corpus = load_corpus([project_dir])
    csv_path = tmp_path / "admin.csv"
    _write_csv(
        csv_path,
        [
            "date,input_tokens,output_tokens,cache_creation_input_tokens,"
            "cache_creation_input_tokens_5m,cache_creation_input_tokens_1h",
            "2026-08-05,500,80,150,100,50",
        ],
    )
    admin = reconcile_mod.parse_admin_csv(csv_path)
    section = reconcile_mod.reconcile(corpus, PRICING, CONFIG, admin_rows=admin.rows, by=("day",))
    table = _table(section)
    col_index = {c.key: i for i, c in enumerate(table.columns)}
    for row in table.rows:
        assert row[col_index["cache_creation_tokens_local"]] == 150
        assert row[col_index["cache_creation_tokens_admin"]] == 150
        assert row[col_index["cache_creation_tokens_delta"]] == 0
        assert row[col_index["input_tokens_delta"]] == 0


def test_reconcile_buckets_local_turns_by_utc_day(tmp_path):
    # 22:30 at UTC-05:00 is 03:30 UTC the next day; the Admin export
    # buckets by UTC day, so the turn must land on 2026-08-06.
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "s1", "2026-08-05T22:30:00.000-05:00")
    corpus = load_corpus([project_dir])

    section = reconcile_mod.reconcile(corpus, PRICING, CONFIG, admin_rows=[], by=("day",))
    table = _table(section)
    assert [row[0] for row in table.rows[:-1]] == ["2026-08-06"]
    joined_notes = " ".join(section.notes)
    assert "bucketed by UTC day" in joined_notes
    assert "local time" not in joined_notes


def test_reconcile_reports_nonzero_delta(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(
        project_dir,
        "s1",
        "2026-08-05T09:00:00.000Z",
        input_tokens=500,
        output_tokens=80,
    )
    corpus = load_corpus([project_dir])

    admin_rows = [{"date": "2026-08-05", "input_tokens": 400, "output_tokens": 80, "cost": 0.0}]
    section = reconcile_mod.reconcile(corpus, PRICING, CONFIG, admin_rows=admin_rows, by=("day",))
    table = _table(section)
    col_index = {c.key: i for i, c in enumerate(table.columns)}
    data_row = table.rows[0]
    # Local has 500 input tokens, Admin has 400 -> delta = local - admin = +100.
    assert data_row[col_index["input_tokens_local"]] == 500
    assert data_row[col_index["input_tokens_admin"]] == 400
    assert data_row[col_index["input_tokens_delta"]] == 100
    assert data_row[col_index["input_tokens_delta_pct"]] == pytest.approx(25.0)


def test_reconcile_by_day_and_model_grouping(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "s1", "2026-08-05T09:00:00.000Z", model="claude-sonnet-5")
    _write_session(project_dir, "s2", "2026-08-05T10:00:00.000Z", model="claude-fable-5")
    corpus = load_corpus([project_dir])

    section = reconcile_mod.reconcile(corpus, PRICING, CONFIG, admin_rows=[], by=("day", "model"))
    table = _table(section)
    # Two distinct (day, model) groups plus TOTAL.
    period_keys = {(row[0], row[1]) for row in table.rows[:-1]}
    assert period_keys == {("2026-08-05", "claude-sonnet-5"), ("2026-08-05", "claude-fable-5")}
    assert table.rows[-1][0] == "TOTAL"


def test_reconcile_window_filters_both_local_and_admin_rows(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "aug", "2026-08-05T09:00:00.000Z")
    _write_session(project_dir, "sep", "2026-09-05T09:00:00.000Z")
    corpus = load_corpus([project_dir])

    admin_rows = [
        {"date": "2026-08-05", "cost": 1.0},
        {"date": "2026-09-05", "cost": 2.0},
    ]
    section = reconcile_mod.reconcile(
        corpus, PRICING, CONFIG, admin_rows=admin_rows, by=("day",), since="2026-08-01", until="2026-08-31"
    )
    table = _table(section)
    day_keys = [row[0] for row in table.rows[:-1]]
    assert day_keys == ["2026-08-05"]


def test_reconcile_unmapped_headers_note(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "s1", "2026-08-05T09:00:00.000Z")
    corpus = load_corpus([project_dir])

    section = reconcile_mod.reconcile(
        corpus, PRICING, CONFIG, admin_rows=[], unmapped_headers=["weird_col"], by=("day",)
    )
    assert any("weird_col" in n for n in section.notes)


def test_reconcile_notes_list_known_difference_reasons(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "s1", "2026-08-05T09:00:00.000Z")
    corpus = load_corpus([project_dir])

    section = reconcile_mod.reconcile(corpus, PRICING, CONFIG, admin_rows=[], by=("day",))
    joined_notes = " ".join(section.notes)
    for reason in reconcile_mod.KNOWN_DIFFERENCE_REASONS:
        assert reason in joined_notes


def test_reconcile_rejects_bad_by_tuple(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "s1", "2026-08-05T09:00:00.000Z")
    corpus = load_corpus([project_dir])

    with pytest.raises(ValueError):
        reconcile_mod.reconcile(corpus, PRICING, CONFIG, admin_rows=[], by=("model", "day"))


def test_reconcile_section_passes_privacy_scan(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "s1", "2026-08-05T09:00:00.000Z")
    corpus = load_corpus([project_dir])

    section = reconcile_mod.reconcile(corpus, PRICING, CONFIG, admin_rows=[], by=("day", "model"))
    assert_privacy(section)


# -- claude_code_reported_costs (SURV-5, PARSER_VERSION 19) ------------------
#
# The ``cost-state`` half of plan P9's later "Q1 gap metric": pairs a
# session's self-reported ``TranscriptMeta.cc_cost_usd`` against this
# tool's own locally-priced total for that same session. No admin CSV, no
# network call -- see reconcile.py's own module docstring.


def test_claude_code_reported_costs_pairs_self_report_with_local_total(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "s1.jsonl",
        [
            turn_line(
                timestamp="2026-08-05T09:00:00.000Z",
                input_tokens=500,
                output_tokens=80,
            ),
            ignorable_line("cost-state", totalCostUSD=1.23, hasUnknownModelCost=False),
        ],
    )
    corpus = load_corpus([project_dir])

    costs = reconcile_mod.claude_code_reported_costs(corpus, PRICING)
    assert len(costs) == 1
    entry = costs[0]
    assert entry.session_id == "s1"
    assert entry.cc_cost_usd == 1.23
    assert entry.cc_has_unknown_model is False
    turn = corpus.sessions[0].top.turns[0]
    expected_local = price_turn(turn, PRICING.resolve_model(turn.model)).total
    assert entry.local_cost_usd == pytest.approx(expected_local)


def test_claude_code_reported_costs_uses_last_cost_state_line(tmp_path):
    # A running total: the last line in file order is the most complete.
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "s1.jsonl",
        [
            turn_line(timestamp="2026-08-05T09:00:00.000Z"),
            ignorable_line("cost-state", totalCostUSD=0.10, hasUnknownModelCost=False),
            ignorable_line("cost-state", totalCostUSD=0.55, hasUnknownModelCost=True),
        ],
    )
    corpus = load_corpus([project_dir])

    costs = reconcile_mod.claude_code_reported_costs(corpus, PRICING)
    assert len(costs) == 1
    assert costs[0].cc_cost_usd == 0.55
    assert costs[0].cc_has_unknown_model is True


def test_claude_code_reported_costs_skips_sessions_without_cost_state(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "s1", "2026-08-05T09:00:00.000Z")
    corpus = load_corpus([project_dir])

    assert reconcile_mod.claude_code_reported_costs(corpus, PRICING) == []


def test_claude_code_reported_costs_passes_privacy_scan(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "s1.jsonl",
        [
            turn_line(timestamp="2026-08-05T09:00:00.000Z"),
            ignorable_line("cost-state", totalCostUSD=2.5, hasUnknownModelCost=False),
        ],
    )
    corpus = load_corpus([project_dir])
    costs = reconcile_mod.claude_code_reported_costs(corpus, PRICING)
    assert_privacy(costs)


# -- CLI wiring --------------------------------------------------------------


def _write_cli_project(root: Path, slug: str, session_id: str, timestamp: str, **overrides) -> Path:
    project_dir = root / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(project_dir / f"{session_id}.jsonl", [turn_line(timestamp=timestamp, **overrides)])
    return project_dir


def test_cli_reconcile_malformed_csv_exits_2_without_echoing_row(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_cli_project(root, "proj", "s1", "2026-08-05T09:00:00.000Z")
    csv_path = tmp_path / "admin.csv"
    _write_csv(csv_path, ["date,input_tokens", "2026-08-01,10", "2026-08-02,not-a-number"])

    exit_code = cli.main(
        [
            "reconcile",
            "--projects-root",
            str(root),
            "--project",
            "proj",
            "--admin-csv",
            str(csv_path),
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "claude-token-lens reconcile:" in err
    assert "cannot parse admin CSV at line 3" in err
    assert "not-a-number" not in err


def test_cli_reconcile_missing_csv_exits_2(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_cli_project(root, "proj", "s1", "2026-08-05T09:00:00.000Z")
    exit_code = cli.main(
        [
            "reconcile",
            "--projects-root",
            str(root),
            "--project",
            "proj",
            "--admin-csv",
            str(tmp_path / "does-not-exist.csv"),
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "claude-token-lens reconcile:" in err


def test_cli_reconcile_end_to_end_json(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_cli_project(root, "proj", "s1", "2026-08-05T09:00:00.000Z")
    csv_path = tmp_path / "admin.csv"
    _write_csv(csv_path, ["date,cost", "2026-08-05,0.0"])

    exit_code = cli.main(
        [
            "reconcile",
            "--projects-root",
            str(root),
            "--project",
            "proj",
            "--admin-csv",
            str(csv_path),
            "--by",
            "day,model",
            "--json",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    section_keys = {s["key"] for s in payload["report"]["sections"]}
    assert "reconcile" in section_keys
    assert_privacy(payload)


def test_cli_reconcile_large_delta_still_exits_0(tmp_path, capsys):
    """Exit 0 whenever the CSV parses, regardless of delta size -- only a
    parse failure is a bad-input exit (see cli.py's _cmd_reconcile
    docstring comment)."""
    root = tmp_path / "projects"
    _write_cli_project(
        root, "proj", "s1", "2026-08-05T09:00:00.000Z", input_tokens=100000, output_tokens=100000
    )
    csv_path = tmp_path / "admin.csv"
    _write_csv(csv_path, ["date,input_tokens", "2026-08-05,1"])

    exit_code = cli.main(
        [
            "reconcile",
            "--projects-root",
            str(root),
            "--project",
            "proj",
            "--admin-csv",
            str(csv_path),
        ]
    )
    assert exit_code == 0
