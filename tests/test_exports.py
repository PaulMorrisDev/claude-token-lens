"""Tests for S1-exports' ``exports.py`` (``claude-token-lens export``):
row-grain correctness for ``csv-flat``/``json``, the otel-jsonl shape,
``--aggregate-only``/``--hash-slugs`` resolution, and the privacy
guarantees the plan's "Aggregation without surveillance" section
promises (no session ids when aggregate-only, no raw slugs when
hashed) -- verified both generically (``tests.helpers.assert_privacy``,
which walks any string it is handed) and with an explicit substring
check for the exact session id / slug this file's fixtures use, since
the generic scan alone can't know what a "leak" would look like for
values it has never seen.
"""

from __future__ import annotations

import json

from claude_token_lens import exports
from claude_token_lens.config import Config
from claude_token_lens.corpus import load_corpus
from claude_token_lens.pricing import load_pricing

from helpers import assert_privacy, turn_line, write_jsonl

PRICING = load_pricing()


def _write_top(project_dir, session_id: str, n_turns: int = 2, **overrides):
    path = project_dir / f"{session_id}.jsonl"
    write_jsonl(
        path,
        [turn_line(input_tokens=100 + i, output_tokens=20 + i, **overrides) for i in range(n_turns)],
    )
    return path


def _build_corpus(tmp_path, slug: str = "acme-secret-project", session_id: str = "session-super-secret"):
    project_dir = tmp_path / slug
    project_dir.mkdir(parents=True)
    _write_top(project_dir, session_id, n_turns=3, cache_read_input_tokens=10, cache_creation_input_tokens=5)
    corpus = load_corpus([project_dir])
    return corpus


# -- build_export_rows --------------------------------------------------


def test_build_export_rows_aggregate_grain(tmp_path):
    corpus = _build_corpus(tmp_path)
    rows = exports.build_export_rows(corpus, PRICING, Config(), per_session=False)
    assert len(rows) == 1
    row = rows[0]
    assert row["day"] == "2026-09-18"
    assert row["project"] == "acme-secret-project"
    assert row["model"] == "claude-sonnet-5"
    assert row["turns"] == 3
    assert "session_id" not in row


def test_build_export_rows_per_session_adds_session_id(tmp_path):
    corpus = _build_corpus(tmp_path)
    rows = exports.build_export_rows(corpus, PRICING, Config(), per_session=True)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "session-super-secret"


def test_build_export_rows_splits_by_model_and_entrypoint(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    write_jsonl(
        project_dir / "s1.jsonl",
        [
            turn_line(model="claude-sonnet-5", input_tokens=100),
            turn_line(model="claude-opus-5", input_tokens=200),
        ],
    )
    corpus = load_corpus([project_dir])
    rows = exports.build_export_rows(corpus, PRICING, Config(), per_session=False)
    models = {row["model"] for row in rows}
    assert models == {"claude-sonnet-5", "claude-opus-5"}


# -- resolve_export_options ----------------------------------------------


def test_resolve_export_options_defaults():
    options = exports.resolve_export_options("csv-flat", None, None)
    assert options.aggregate_only is True
    assert options.hash_slugs is True


def test_resolve_export_options_per_session_still_defaults_hash_slugs_true():
    """Regression test for review finding 2 (blocking): hash_slugs used to
    default to whatever aggregate_only resolved to, so --per-session (the
    *more* identifying mode, since it also adds session_id) ended up with
    *less* protection by default -- raw, un-hashed project slugs, which
    ``discovery.slug_for`` builds by dash-mangling the absolute working
    directory (OS username, directory layout, and any client/project name
    it contains). hash_slugs must now default to True unconditionally,
    independent of aggregate_only; the raw slug is opt-in only, via the
    explicit --no-hash-slugs flag (see test_resolve_export_options_explicit_choices_are_honoured
    just below for that opt-out path)."""
    options = exports.resolve_export_options("csv-flat", False, None)
    assert options.aggregate_only is False
    assert options.hash_slugs is True


def test_resolve_export_options_explicit_choices_are_honoured():
    options = exports.resolve_export_options("csv-flat", True, False)
    assert options.aggregate_only is True
    assert options.hash_slugs is False  # explicit opt-out, even with aggregate-only

    options2 = exports.resolve_export_options("csv-flat", False, True)
    assert options2.aggregate_only is False
    assert options2.hash_slugs is True


# -- renderers -------------------------------------------------------------


def test_render_csv_flat_header_and_row_count(tmp_path):
    corpus = _build_corpus(tmp_path)
    rows = exports.build_export_rows(corpus, PRICING, Config(), per_session=False)
    text = exports.render_csv_flat(rows, per_session=False)
    # csv.writer's default dialect uses "\r\n" line endings (matching
    # render/csv_out.py's own convention), so split on that rather than
    # bare "\n" to avoid a stray trailing "\r" on every line.
    lines = text.strip("\r\n").split("\r\n")
    assert lines[0].split(",") == list(exports._ROW_FIELDS)
    assert len(lines) == 2  # header + 1 row


def test_render_csv_flat_per_session_includes_session_id_column(tmp_path):
    corpus = _build_corpus(tmp_path)
    rows = exports.build_export_rows(corpus, PRICING, Config(), per_session=True)
    text = exports.render_csv_flat(rows, per_session=True)
    header = text.strip("\r\n").split("\r\n")[0].split(",")
    assert header[-1] == "session_id"


def test_render_json_has_meta_and_rows(tmp_path):
    corpus = _build_corpus(tmp_path)
    rows = exports.build_export_rows(corpus, PRICING, Config(), per_session=False)
    meta = {"tool_version": "0.1.0", "window": "w", "pricing_version": "v1", "generated_at": "x", "hash_slugs": True}
    text = exports.render_json(rows, meta)
    parsed = json.loads(text)
    assert parsed["meta"] == meta
    assert parsed["rows"] == rows


def test_render_otel_jsonl_shape(tmp_path):
    corpus = _build_corpus(tmp_path)
    text = exports.render_otel_jsonl(corpus, PRICING, Config())
    lines = [json.loads(line) for line in text.strip("\n").split("\n")]
    # 4 token-type points + 1 cost point, for the single (day, model) cell.
    assert len(lines) == 5
    token_points = [p for p in lines if p["name"] == "claude_code.token.usage"]
    cost_points = [p for p in lines if p["name"] == "claude_code.cost.usage"]
    assert len(cost_points) == 1
    types = {p["attributes"]["type"] for p in token_points}
    assert types == {"input", "output", "cacheRead", "cacheCreation"}
    for point in lines:
        assert point["attributes"]["model"] == "claude-sonnet-5"
        assert isinstance(point["time_unix_nano"], int)
    # No project/session attribute anywhere in this format.
    for point in lines:
        assert "project" not in point["attributes"]
        assert "session" not in point["attributes"]


def test_build_export_text_hashes_slugs_with_salt(tmp_path):
    config_dir = tmp_path / "token-lens"
    config_dir.mkdir()
    corpus = _build_corpus(tmp_path / "projects")
    options = exports.resolve_export_options("csv-flat", True, True)
    text = exports.build_export_text(corpus, PRICING, Config(), config_dir, options)
    assert "acme-secret-project" not in text
    assert (config_dir / "salt").exists()


def test_build_export_text_unknown_format_raises(tmp_path):
    corpus = _build_corpus(tmp_path)
    options = exports.ExportOptions(fmt="bogus", aggregate_only=True, hash_slugs=True)
    try:
        exports.build_export_text(corpus, PRICING, Config(), tmp_path, options)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unknown export format")


# -- privacy ---------------------------------------------------------------


def test_export_aggregate_only_never_contains_session_id(tmp_path):
    config_dir = tmp_path / "token-lens"
    config_dir.mkdir()
    corpus = _build_corpus(tmp_path / "projects")
    for fmt in ("csv-flat", "json"):
        options = exports.resolve_export_options(fmt, True, True)
        text = exports.build_export_text(corpus, PRICING, Config(), config_dir, options)
        assert "session-super-secret" not in text


def test_export_hashed_slugs_never_contains_raw_slug(tmp_path):
    config_dir = tmp_path / "token-lens"
    config_dir.mkdir()
    corpus = _build_corpus(tmp_path / "projects")
    for fmt in ("csv-flat", "json"):
        options = exports.resolve_export_options(fmt, True, True)
        text = exports.build_export_text(corpus, PRICING, Config(), config_dir, options)
        assert "acme-secret-project" not in text


def test_export_passes_generic_privacy_scan(tmp_path):
    config_dir = tmp_path / "token-lens"
    config_dir.mkdir()
    corpus = _build_corpus(tmp_path / "projects")
    for fmt in ("csv-flat", "json", "otel-jsonl"):
        options = exports.resolve_export_options(fmt, True, True)
        text = exports.build_export_text(corpus, PRICING, Config(), config_dir, options)
        assert_privacy(text)


def test_no_hash_slugs_redacts_the_username_segment_not_fully_raw(tmp_path):
    """Regression test for review finding 2's third fix component:
    --no-hash-slugs is an explicit opt-out of hashing, but it must still
    not print the fully raw slug -- exports._redact_slug (a local
    fallback for the sibling discovery.redact_slug fix, see the module
    docstring) replaces just the OS-username segment with '<user>'. The
    directory name mirrors discovery.slug_for's real output shape for
    C:\\Users\\paulm\\Dev\\acme-client-secret (corpus.py's own
    project_dir.name convention means this test can build that shape
    directly as a fixture directory name)."""
    config_dir = tmp_path / "token-lens"
    config_dir.mkdir()
    projects_root = tmp_path / "projects"
    slug_dir_name = "C--Users-paulm-Dev-acme-client-secret"
    project_dir = projects_root / slug_dir_name
    project_dir.mkdir(parents=True)
    _write_top(project_dir, "sess-1")
    corpus = load_corpus([project_dir])

    options = exports.resolve_export_options("csv-flat", True, False)  # --no-hash-slugs
    text = exports.build_export_text(corpus, PRICING, Config(), config_dir, options)

    assert "paulm" not in text
    assert "<user>" in text
    # The rest of the path shape is preserved -- this is the *documented*
    # residual risk of --no-hash-slugs, not a bug.
    assert "acme-client-secret" in text
    assert_privacy(text)


def test_apply_slug_redaction_leaves_a_slug_with_no_user_segment_untouched():
    rows = [{"project": "plain-project-name"}]
    exports._apply_slug_redaction(rows)
    assert rows[0]["project"] == "plain-project-name"


# -- review finding 7: cache-creation column / cross-format reconciliation --


def test_build_export_rows_cache_write_tokens_column_present_and_correct(tmp_path):
    corpus = _build_corpus(tmp_path)
    rows = exports.build_export_rows(corpus, PRICING, Config(), per_session=False)
    assert len(rows) == 1
    # Fixture uses cache_creation_input_tokens=5 per turn, 3 turns, with
    # a nested cache_creation object (5m/1h both 0 by default in
    # helpers.turn_line) -- so cache_write_tokens (the unsplit total)
    # must equal 15 even though the 5m/1h split columns are both 0.
    assert rows[0]["cache_write_tokens"] == 15
    assert rows[0]["cache_write_5m_tokens"] == 0
    assert rows[0]["cache_write_1h_tokens"] == 0


def test_cache_creation_totals_reconcile_across_csv_flat_otel_and_report(tmp_path):
    """Regression/reconciliation test for review finding 7 (should-fix)
    and nit 21(a): a pre-TTL-split transcript (usage carries
    cache_creation_input_tokens but no nested "cache_creation" object at
    all, so Turn.ttl_split_unknown is True and cc_5m/cc_1h both stay 0)
    must still contribute its full cache-creation total to csv-flat's
    cache_write_tokens column, otel-jsonl's cacheCreation data point,
    and report.build_report's own overview cache_creation_tokens total
    -- all three must agree, closing the "5000 tokens lost" gap the
    review's own repro demonstrated.
    """
    from claude_token_lens.report import build_report

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    line = turn_line(input_tokens=100, output_tokens=20)
    del line["message"]["usage"]["cache_creation"]
    line["message"]["usage"]["cache_creation_input_tokens"] = 5000
    write_jsonl(project_dir / "s1.jsonl", [line])

    corpus = load_corpus([project_dir])
    config = Config()

    rows = exports.build_export_rows(corpus, PRICING, config, per_session=False)
    assert len(rows) == 1
    assert rows[0]["cache_write_tokens"] == 5000
    assert rows[0]["cache_write_5m_tokens"] == 0
    assert rows[0]["cache_write_1h_tokens"] == 0

    otel_text = exports.render_otel_jsonl(corpus, PRICING, config)
    otel_lines = [json.loads(l) for l in otel_text.strip("\n").split("\n")]
    cache_creation_points = [
        p for p in otel_lines if p["name"] == "claude_code.token.usage" and p["attributes"]["type"] == "cacheCreation"
    ]
    assert len(cache_creation_points) == 1
    assert cache_creation_points[0]["value"] == 5000

    model = build_report(corpus, PRICING, config, projects=(), window="all time")
    overview_section = next(s for s in model.sections if s.key == "overview")
    totals_table = next(t for t in overview_section.tables if t.name == "totals")
    totals = dict(totals_table.rows)
    assert totals["cache_creation_tokens"] == 5000

    # All three agree.
    assert (
        rows[0]["cache_write_tokens"]
        == cache_creation_points[0]["value"]
        == totals["cache_creation_tokens"]
        == 5000
    )


# -- review finding 1: csv-flat CRLF doubling (blocking) ---------------------


def test_cli_export_csv_flat_out_file_has_no_doubled_cr(tmp_path):
    """Regression test for review finding 1 (blocking): csv.DictWriter's
    default dialect already terminates rows with \\r\\n; writing that
    text through a text-mode handle with default newline translation
    doubled every CR, corrupting the file for any BI/pandas import (a
    50% blank-row rate). --out must now be opened with newline=""."""
    from claude_token_lens import cli as cli_mod

    projects_root = tmp_path / "projects"
    project_dir = projects_root / "proj"
    project_dir.mkdir(parents=True)
    write_jsonl(project_dir / "s1.jsonl", [turn_line(input_tokens=100, output_tokens=20)])

    out_path = tmp_path / "team-usage.csv"
    config_dir = tmp_path / "token-lens"
    rc = cli_mod.main(
        [
            "export",
            "--projects-root",
            str(projects_root),
            "--all-projects",
            "--config-dir",
            str(config_dir),
            "--format",
            "csv-flat",
            "--out",
            str(out_path),
        ]
    )
    assert rc == 0
    raw = out_path.read_bytes()
    assert b"\r\r" not in raw

    import csv as _csv

    with open(out_path, newline="") as fh:
        parsed_rows = list(_csv.reader(fh))
    assert all(row for row in parsed_rows)  # no blank records
    assert len(parsed_rows) == 2  # header + 1 aggregate row


def test_cli_export_csv_flat_stdout_has_no_doubled_cr(tmp_path, capsys):
    """The stdout path (no --out) must be equally free of doubled CRs --
    the review reproduced the bug on both output paths."""
    from claude_token_lens import cli as cli_mod

    projects_root = tmp_path / "projects"
    project_dir = projects_root / "proj"
    project_dir.mkdir(parents=True)
    write_jsonl(project_dir / "s1.jsonl", [turn_line(input_tokens=100, output_tokens=20)])

    config_dir = tmp_path / "token-lens"
    rc = cli_mod.main(
        [
            "export",
            "--projects-root",
            str(projects_root),
            "--all-projects",
            "--config-dir",
            str(config_dir),
            "--format",
            "csv-flat",
        ]
    )
    assert rc == 0
    out_bytes = capsys.readouterr().out.encode("utf-8")
    assert b"\r\r" not in out_bytes


def test_export_per_session_opt_in_does_contain_session_id(tmp_path):
    """The flip side of the aggregate-only guarantee: --per-session is
    an explicit, informed opt-in, so the session id IS present then."""
    config_dir = tmp_path / "token-lens"
    config_dir.mkdir()
    corpus = _build_corpus(tmp_path / "projects")
    options = exports.resolve_export_options("csv-flat", False, False)
    text = exports.build_export_text(corpus, PRICING, Config(), config_dir, options)
    assert "session-super-secret" in text


def test_hashed_export_hashes_custom_agent_names_but_keeps_stock_ones(tmp_path):
    rows = [
        {"project": "p", "agent_type": "acme-secret-reviewer"},
        {"project": "p", "agent_type": "Explore"},
        {"project": "p", "agent_type": "top-level"},
        {"project": "p", "agent_type": "subagent"},
    ]
    exports._apply_hash_slugs(rows, tmp_path)
    assert rows[0]["agent_type"].startswith("custom:") and "acme" not in rows[0]["agent_type"]
    assert [r["agent_type"] for r in rows[1:]] == ["Explore", "top-level", "subagent"]
