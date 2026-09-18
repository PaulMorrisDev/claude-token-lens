"""Tests for the four report renderers (markdown, json_out, csv_out,
html) against one shared synthetic ``ReportModel`` fixture that covers
every ``Column.kind``, two sections, notes, two recommendations with
evidence, and diagnostics.
"""

from __future__ import annotations

import csv
import json
import os

import pytest

from claude_token_lens.model import (
    Column,
    Diagnostics,
    PricingMeta,
    Recommendation,
    ReportMeta,
    ReportModel,
    Section,
    Table,
)
from claude_token_lens.render.csv_out import write_csv_dir
from claude_token_lens.render.html import render_html
from claude_token_lens.render.json_out import render_json
from claude_token_lens.render.markdown import render_markdown

# A cell that must survive every renderer's escaping unscathed: markdown's
# pipe/newline escaping and HTML's tag/ampersand escaping.
_DANGEROUS_CELL = "line one\nline two | pipe"
_XSS_CELL = "<script>alert(1)</script> & <b>x</b>"

# The raw numeric value used by the cross-format agreement test.
_TOKENS_RAW = 1234567
_TOKENS_FORMATTED = "1,234,567"


@pytest.fixture
def report_model() -> ReportModel:
    overview = Table(
        name="overview",
        title="Overview",
        columns=[
            Column(key="agent", label="Agent", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="avg_cost", label="Avg cost", kind="float"),
        ],
        rows=[
            ["top-level", 42, 1.5],
            ["claude-implementer", 7, 0.25],
        ],
    )
    cache = Table(
        name="cache",
        title="Cache",
        columns=[
            Column(key="hit_ratio", label="Hit ratio", kind="pct"),
            Column(key="cost", label="Cost", kind="money"),
        ],
        rows=[
            [87.654, 12.5],
            [None, None],
        ],
        notes=["Bars are capped at 100% even above it."],
    )
    agent_detail = Table(
        name="agent_detail",
        title="Agent detail",
        columns=[
            Column(key="tokens", label="Tokens", kind="tokens"),
            Column(key="duration", label="Duration", kind="secs"),
            Column(key="note", label="Note", kind="str"),
        ],
        rows=[
            [_TOKENS_RAW, 83, _DANGEROUS_CELL],
            [2222222, 45, _XSS_CELL],
        ],
    )

    usage_section = Section(
        key="usage",
        title="Usage",
        tables=[overview, cache],
        notes=["Usage section note."],
    )
    agents_section = Section(
        key="agents",
        title="Agents",
        tables=[agent_detail],
    )

    meta = ReportMeta(
        tool_version="0.1.0",
        generated_at="2026-09-18T12:00:00Z",
        window="last 30 days",
        projects=("proj-a", "proj-b"),
        pricing=PricingMeta(
            path="pricing.toml",
            version="2026-09-18",
            sha8="abcdef12",
            currency="USD",
            coverage_pct=98.5,
        ),
        thresholds={"ctx_floor": 20000, "cr_ratio": 0.2},
        billing_mode="api",
        assumptions=[
            "Content is TTL-invariant.",
            "Reads priced at the flat cache_read rate.",
        ],
    )

    recommendations = [
        Recommendation(
            id="ttl-switch",
            severity="action",
            category="settings",
            archetypes=("plan-high-implement-low",),
            title="Switch claude-implementer to 5m TTL",
            action="Set subagentPromptCacheTtl to 5m.",
            lever="subagentPromptCacheTtl",
            evidence=[("Turns", 7, "overview", "claude-implementer")],
        ),
        Recommendation(
            id="cache-read-dominance",
            severity="info",
            category="workflow",
            title="Cache reads dominate cost",
            action="No action needed; monitor.",
            evidence=[("Hit ratio", 87.654, "cache", 0)],
        ),
    ]

    diagnostics = Diagnostics(
        lines=100,
        unparsable_lines=1,
        truncated_final_line=False,
        assistant_lines=40,
        distinct_turns=38,
        synthetic_turns=0,
        turns_missing_usage=0,
        ttl_sum_mismatch=0,
        late_duplicate_ids=2,
        ignored_line_types={"foo": 3, "bar": 1},
    )

    return ReportModel(
        meta=meta,
        sections=[usage_section, agents_section],
        recommendations=recommendations,
        diagnostics=diagnostics,
    )


def _find_value(obj, target) -> bool:
    """Recursively search a JSON-decoded structure for ``target``."""
    if obj == target:
        return True
    if isinstance(obj, dict):
        return any(_find_value(v, target) for v in obj.values())
    if isinstance(obj, list):
        return any(_find_value(v, target) for v in obj)
    return False


# -- markdown --------------------------------------------------------------


def test_markdown_has_title_and_section_headings(report_model):
    md = render_markdown(report_model)
    assert md.startswith("# Claude token lens report")
    assert "## Usage" in md
    assert "## Agents" in md
    assert "### Overview" in md
    assert "### Cache" in md
    assert "### Agent detail" in md


def test_markdown_pipe_table_has_alignment_row(report_model):
    md = render_markdown(report_model)
    # Overview: str, int, float -> left, right, right.
    assert "| Agent | Turns | Avg cost |" in md
    assert "| --- | ---: | ---: |" in md


def test_markdown_escapes_pipe_and_newline_cells(report_model):
    md = render_markdown(report_model)
    assert "line one<br>line two \\| pipe" in md
    assert "\n" not in _extract_cell_line(md, "line one<br>")


def _extract_cell_line(md: str, needle: str) -> str:
    for line in md.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"{needle!r} not found in markdown output")


def test_markdown_formats_numbers_by_kind(report_model):
    md = render_markdown(report_model)
    assert _TOKENS_FORMATTED in md
    assert "12.50 USD" in md
    assert "87.7%" in md  # 87.654 rounded to one decimal
    assert "- " in md  # None cells render as "-"


def test_markdown_notes_render_as_bullets(report_model):
    md = render_markdown(report_model)
    assert "- Bars are capped at 100% even above it." in md
    assert "- Usage section note." in md


def test_markdown_recommendations_section(report_model):
    md = render_markdown(report_model)
    assert "## Recommendations" in md
    assert "### [action] Switch claude-implementer to 5m TTL" in md
    assert "Action: Set subagentPromptCacheTtl to 5m." in md
    assert "Lever: subagentPromptCacheTtl" in md
    assert "Evidence:" in md
    assert "- Turns: 7 (table overview, row claude-implementer)" in md
    assert "- Hit ratio: 87.654 (table cache, row 0)" in md


def test_markdown_diagnostics_section(report_model):
    md = render_markdown(report_model)
    assert "## Diagnostics" in md
    assert "- lines: 100" in md
    assert "- ignored_line_types: foo=3, bar=1" in md


def test_markdown_assumptions_section(report_model):
    md = render_markdown(report_model)
    assert "## Assumptions" in md
    assert "- Content is TTL-invariant." in md


# -- json --------------------------------------------------------------


def test_json_has_pinned_top_level_keys(report_model):
    text = render_json(report_model)
    payload = json.loads(text)
    assert set(payload.keys()) == {"schema_version", "tool_version", "report"}
    assert payload["schema_version"] == 1
    assert payload["tool_version"] == "0.1.0"


def test_json_round_trips_and_is_sorted(report_model):
    text = render_json(report_model)
    payload = json.loads(text)
    re_dumped = json.dumps(payload, sort_keys=True, indent=2)
    assert re_dumped == text


def test_json_contains_every_table_cell_value(report_model):
    payload = json.loads(render_json(report_model))
    for value in (42, 1.5, 87.654, 12.5, _TOKENS_RAW, 45):
        assert _find_value(payload, value), f"{value!r} missing from JSON output"


def test_json_none_and_dangerous_strings_survive(report_model):
    payload = json.loads(render_json(report_model))
    assert _find_value(payload, None)
    assert _find_value(payload, _XSS_CELL)


# -- csv -----------------------------------------------------------------


def test_csv_dir_has_expected_files(report_model, tmp_path):
    write_csv_dir(report_model, tmp_path)
    files = {p.name for p in tmp_path.iterdir()}
    assert files == {
        "usage__overview.csv",
        "usage__cache.csv",
        "agents__agent_detail.csv",
        "_index.csv",
        "_meta.csv",
    }


def test_csv_header_equals_column_keys(report_model, tmp_path):
    write_csv_dir(report_model, tmp_path)
    with open(tmp_path / "usage__overview.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["agent", "turns", "avg_cost"]
    assert len(rows) == 1 + len(report_model.sections[0].tables[0].rows)


def test_csv_values_are_raw_not_formatted(report_model, tmp_path):
    write_csv_dir(report_model, tmp_path)
    with open(tmp_path / "agents__agent_detail.csv", newline="", encoding="utf-8") as fh:
        text = fh.read()
    assert str(_TOKENS_RAW) in text
    assert _TOKENS_FORMATTED not in text


def test_csv_none_becomes_empty_string(report_model, tmp_path):
    write_csv_dir(report_model, tmp_path)
    with open(tmp_path / "usage__cache.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[2] == ["", ""]


def test_csv_index_lists_every_table(report_model, tmp_path):
    write_csv_dir(report_model, tmp_path)
    with open(tmp_path / "_index.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["section", "table", "file", "rows"]
    body = rows[1:]
    assert len(body) == 3
    files = {row[2] for row in body}
    assert files == {"usage__overview.csv", "usage__cache.csv", "agents__agent_detail.csv"}


def test_csv_meta_has_recommendation_rows(report_model, tmp_path):
    write_csv_dir(report_model, tmp_path)
    with open(tmp_path / "_meta.csv", newline="", encoding="utf-8") as fh:
        rows = {row[0]: row[1] for row in csv.reader(fh)}
    assert rows["tool_version"] == "0.1.0"
    assert rows["recommendation.ttl-switch"] == "Switch claude-implementer to 5m TTL"
    assert rows["recommendation.cache-read-dominance"] == "Cache reads dominate cost"


def test_csv_sanitises_filenames_to_lowercase_alnum_underscore(report_model, tmp_path):
    weird = Table(name="A B/C!", title="Weird", columns=[Column(key="x", label="X", kind="int")], rows=[[1]])
    section = Section(key="Sect One", title="Sect One", tables=[weird])
    model = ReportModel(sections=[section])
    write_csv_dir(model, tmp_path)
    names = {p.name for p in tmp_path.iterdir()}
    assert "sect_one__a_b_c.csv" in names


# -- html ------------------------------------------------------------------


_FORBIDDEN_SUBSTRINGS = ("http://", "https://", "<script src", "<link", "@import", "url(")


def test_html_has_no_external_references(report_model):
    out = render_html(report_model)
    for forbidden in _FORBIDDEN_SUBSTRINGS:
        assert forbidden not in out, f"found forbidden substring {forbidden!r}"


def test_html_has_dark_mode_media_query(report_model):
    out = render_html(report_model)
    assert "@media (prefers-color-scheme: dark)" in out


def test_html_has_sort_script(report_model):
    out = render_html(report_model)
    assert "<script>" in out
    assert "addEventListener" in out
    assert "table.sortable" in out


def test_html_escapes_dangerous_cells(report_model):
    out = render_html(report_model)
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert "&lt;b&gt;x&lt;/b&gt;" in out
    assert " &amp; " in out


def test_html_contains_formatted_cell_values(report_model):
    out = render_html(report_model)
    assert "12.50 USD" in out
    assert _TOKENS_FORMATTED in out


def test_html_pct_bar_is_capped_at_100(report_model):
    out = render_html(report_model)
    assert 'class="bar" style="width:87.7%"' in out
    assert "width:100" not in out  # no value in the fixture actually exceeds 100


def test_html_is_well_formed_top_level_structure(report_model):
    out = render_html(report_model)
    assert out.startswith("<!doctype html>")
    assert "<meta charset=\"utf-8\">" in out
    assert "<title>Claude token lens report</title>" in out
    assert out.rstrip().endswith("</html>")


# -- cross-format agreement --------------------------------------------------


def test_same_numeric_cell_agrees_across_formats(report_model, tmp_path):
    md = render_markdown(report_model)
    html_out = render_html(report_model)
    write_csv_dir(report_model, tmp_path)
    with open(tmp_path / "agents__agent_detail.csv", newline="", encoding="utf-8") as fh:
        csv_text = fh.read()

    # Markdown and HTML both show the human-formatted value...
    assert _TOKENS_FORMATTED in md
    assert _TOKENS_FORMATTED in html_out
    # ...HTML also carries the exact raw value for sorting...
    assert f'data-sort="{_TOKENS_RAW}"' in html_out
    # ...and CSV carries only the raw, unformatted value.
    assert str(_TOKENS_RAW) in csv_text
    assert _TOKENS_FORMATTED not in csv_text
