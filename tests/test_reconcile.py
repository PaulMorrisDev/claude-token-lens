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


# -- Q1 gap metric: cost_ground_truth_gaps / build_cost_ground_truth_gap_table --


def _sig4_capture_on(config_dir: Path, level: str = "free") -> None:
    """A minimal ``config.toml`` with capture switched on -- required
    before ``statusline._write_ground_truth_signal`` will write anything
    at all (see its own ``_capture_table`` gate), mirroring
    ``test_statusline.py``'s own ``_sig4_config`` fixture."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(f'[capture]\nlevel = "{level}"\n', encoding="utf-8")


def _sig4_signal(config_dir: Path, session_id: str, cost_usd: float, now) -> None:
    from claude_token_lens import statusline

    statusline._write_ground_truth_signal(config_dir, {"session_id": session_id, "cost": {"total_cost_usd": cost_usd}}, now)


def test_cost_ground_truth_gaps_prefers_cost_state_over_statusline(tmp_path):
    from datetime import datetime, timezone

    from claude_token_lens.parse import load_or_create_salt

    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "s1.jsonl",
        [
            turn_line(timestamp="2026-08-05T09:00:00.000Z", input_tokens=500, output_tokens=80),
            ignorable_line("cost-state", totalCostUSD=1.23, hasUnknownModelCost=False),
        ],
    )
    corpus = load_corpus([project_dir])
    config_dir = tmp_path / "config"
    load_or_create_salt(config_dir)
    _sig4_capture_on(config_dir)
    now = datetime(2026, 8, 5, 10, 0, 0, tzinfo=timezone.utc)
    # A statusline SIG-4 line also exists for this session, at a
    # different figure -- cost-state must still win.
    _sig4_signal(config_dir, "s1", 9.99, now)

    gaps = reconcile_mod.cost_ground_truth_gaps(corpus, PRICING, config_dir)
    assert len(gaps) == 1
    assert gaps[0].source == "cost_state"
    assert gaps[0].cc_cost_usd == 1.23


def test_cost_ground_truth_gaps_falls_back_to_statusline_without_cost_state(tmp_path):
    from datetime import datetime, timezone

    from claude_token_lens.parse import load_or_create_salt

    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "s1", "2026-08-05T09:00:00.000Z", input_tokens=500, output_tokens=80)
    corpus = load_corpus([project_dir])
    config_dir = tmp_path / "config"
    load_or_create_salt(config_dir)
    _sig4_capture_on(config_dir)
    now = datetime(2026, 8, 5, 10, 0, 0, tzinfo=timezone.utc)
    _sig4_signal(config_dir, "s1", 0.42, now)

    gaps = reconcile_mod.cost_ground_truth_gaps(corpus, PRICING, config_dir)
    assert len(gaps) == 1
    assert gaps[0].source == "statusline"
    assert gaps[0].cc_cost_usd == 0.42
    turn = corpus.sessions[0].top.turns[0]
    expected_local = price_turn(turn, PRICING.resolve_model(turn.model)).total
    assert gaps[0].local_cost_usd == pytest.approx(expected_local)
    assert gaps[0].gap_usd == pytest.approx(expected_local - 0.42)


def test_cost_ground_truth_gaps_empty_without_any_ground_truth(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "s1", "2026-08-05T09:00:00.000Z")
    corpus = load_corpus([project_dir])
    assert reconcile_mod.cost_ground_truth_gaps(corpus, PRICING, None) == []
    assert reconcile_mod.cost_ground_truth_gaps(corpus, PRICING, tmp_path / "no-such-config") == []


def test_cost_ground_truth_gaps_never_creates_the_salt(tmp_path):
    """SIG-4's read-only-salt posture (see ``statusline.py``'s and
    ``report._capture_signals``'s docstrings) applies to this reader too:
    a config dir with a signals folder but no salt file yet must not get
    one created just by reading gaps."""
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "s1", "2026-08-05T09:00:00.000Z")
    corpus = load_corpus([project_dir])
    config_dir = tmp_path / "config"
    (config_dir / "signals").mkdir(parents=True)

    gaps = reconcile_mod.cost_ground_truth_gaps(corpus, PRICING, config_dir)

    assert gaps == []
    assert not (config_dir / "salt").exists()


def test_cost_ground_truth_gaps_passes_privacy_scan(tmp_path):
    from datetime import datetime, timezone

    from claude_token_lens.parse import load_or_create_salt

    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "s1", "2026-08-05T09:00:00.000Z")
    corpus = load_corpus([project_dir])
    config_dir = tmp_path / "config"
    load_or_create_salt(config_dir)
    _sig4_capture_on(config_dir)
    _sig4_signal(config_dir, "s1", 0.42, datetime(2026, 8, 5, 10, 0, 0, tzinfo=timezone.utc))

    gaps = reconcile_mod.cost_ground_truth_gaps(corpus, PRICING, config_dir)
    assert_privacy(gaps)


def test_build_cost_ground_truth_gap_table_none_when_no_gaps():
    assert reconcile_mod.build_cost_ground_truth_gap_table([]) is None


def test_build_cost_ground_truth_gap_table_basic_shape():
    gaps = [
        reconcile_mod.CostGroundTruthGap(
            session_id="s1", source="cost_state", cc_cost_usd=1.0, local_cost_usd=1.1, gap_usd=0.1, gap_pct=10.0
        )
    ]
    table = reconcile_mod.build_cost_ground_truth_gap_table(gaps)
    assert table.name == "cost_ground_truth_gap"
    col_index = {c.key: i for i, c in enumerate(table.columns)}
    row = table.rows[0]
    assert row[col_index["session_id"]] == "s1"
    assert row[col_index["source"]] == "cost_state"
    assert row[col_index["gap_usd"]] == pytest.approx(0.1)
    assert row[col_index["gap_pct"]] == pytest.approx(10.0)
    assert any("Gap = " in n for n in table.notes)


def _gap(i: int, cc: float, local: float) -> "reconcile_mod.CostGroundTruthGap":
    return reconcile_mod.CostGroundTruthGap(
        session_id=f"s{i}",
        source="cost_state",
        cc_cost_usd=cc,
        local_cost_usd=local,
        gap_usd=local - cc,
        gap_pct=reconcile_mod._pct_of(cc, local),
    )


def test_build_cost_ground_truth_gap_table_notes_the_median_gap_once_threshold_and_sample_size_met():
    # 12 sessions, each 20% higher locally than Claude Code's own figure
    # -- comfortably past both the 5% and >=10-session thresholds.
    gaps = [_gap(i, 1.0, 1.2) for i in range(12)]
    table = reconcile_mod.build_cost_ground_truth_gap_table(gaps)
    assert any("median" in n and "20%" in n for n in table.notes)


def test_build_cost_ground_truth_gap_table_says_nothing_below_ten_sessions():
    # Same 20% gap, but only 9 sessions -- below the plan's own
    # ">= 10 sessions" floor, so no note even though the gap is large.
    gaps = [_gap(i, 1.0, 1.2) for i in range(9)]
    table = reconcile_mod.build_cost_ground_truth_gap_table(gaps)
    assert not any("median" in n for n in table.notes)


def test_build_cost_ground_truth_gap_table_says_nothing_below_five_percent():
    # 12 sessions, comfortably past the sample-size floor, but only a 1%
    # gap -- below the plan's own "> 5%" threshold.
    gaps = [_gap(i, 1.0, 1.01) for i in range(12)]
    table = reconcile_mod.build_cost_ground_truth_gap_table(gaps)
    assert not any("median" in n for n in table.notes)


def test_build_cost_ground_truth_gap_table_median_note_uses_billing_mode_units(tmp_path):
    from claude_token_lens.units import Units

    # Each session's gap is $0.50 (cc=$1.00, local=$1.50); the note phrases
    # the *median* per-session gap, not a sum across sessions.
    gaps = [_gap(i, 1.0, 1.5) for i in range(12)]
    table = reconcile_mod.build_cost_ground_truth_gap_table(gaps, units=Units(billing_mode="api", currency="USD"))
    median_note = next(n for n in table.notes if "median" in n)
    assert "0.50 USD" in median_note


def test_reconcile_adds_the_gap_table_only_when_config_dir_has_ground_truth(tmp_path):
    """A session with no ``cost-state`` line contributes nothing to the
    gap metric on its own -- the table only appears once ``config_dir``
    is given *and* actually carries SIG-4 ground truth for it. (A
    ``cost-state`` line, by contrast, needs no ``config_dir`` at all --
    see ``test_cost_ground_truth_gaps_prefers_cost_state_over_statusline``
    -- so it is deliberately left out of this fixture.)"""
    from datetime import datetime, timezone

    from claude_token_lens.parse import load_or_create_salt

    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "s1", "2026-08-05T09:00:00.000Z")
    corpus = load_corpus([project_dir])

    without_config_dir = reconcile_mod.reconcile(corpus, PRICING, CONFIG, admin_rows=[])
    assert len(without_config_dir.tables) == 1

    config_dir = tmp_path / "config"
    load_or_create_salt(config_dir)
    _sig4_capture_on(config_dir)
    _sig4_signal(config_dir, "s1", 0.01, datetime(2026, 8, 5, 10, 0, 0, tzinfo=timezone.utc))

    with_config_dir = reconcile_mod.reconcile(corpus, PRICING, CONFIG, admin_rows=[], config_dir=config_dir)
    assert len(with_config_dir.tables) == 2
    assert with_config_dir.tables[1].name == "cost_ground_truth_gap"


def test_reconcile_gap_table_section_passes_privacy_scan(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "s1.jsonl",
        [
            turn_line(timestamp="2026-08-05T09:00:00.000Z"),
            ignorable_line("cost-state", totalCostUSD=0.01, hasUnknownModelCost=False),
        ],
    )
    corpus = load_corpus([project_dir])
    section = reconcile_mod.reconcile(corpus, PRICING, CONFIG, admin_rows=[], config_dir=tmp_path / "config")
    assert_privacy(section)


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
