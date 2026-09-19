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


def test_resolve_export_options_per_session_defaults_hash_slugs_false():
    options = exports.resolve_export_options("csv-flat", False, None)
    assert options.aggregate_only is False
    assert options.hash_slugs is False


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


def test_export_per_session_opt_in_does_contain_session_id(tmp_path):
    """The flip side of the aggregate-only guarantee: --per-session is
    an explicit, informed opt-in, so the session id IS present then."""
    config_dir = tmp_path / "token-lens"
    config_dir.mkdir()
    corpus = _build_corpus(tmp_path / "projects")
    options = exports.resolve_export_options("csv-flat", False, False)
    text = exports.build_export_text(corpus, PRICING, Config(), config_dir, options)
    assert "session-super-secret" in text
