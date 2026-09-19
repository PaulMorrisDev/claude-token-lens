"""``scorecard.py``: the five-dimension optimisation scorecard, on
hand-built ``ScorecardInputs`` (this module never reads a transcript
itself — see its own module docstring). Pins the level boundaries per
the plan's test list ("scorecard boundaries") and covers the
agent-efficiency-skipped-for-non-spawning-corpora and
no-snapshot-scores-5 special cases.
"""

from __future__ import annotations

from claude_token_lens.model import Section
from claude_token_lens.scorecard import (
    ALL_DIMENSIONS,
    LEVEL_LABELS,
    ScorecardInputs,
    ScorecardThresholds,
    build_section,
)

from helpers import assert_privacy


def _assert_row_keys_are_valid(section: Section) -> None:
    for table in section.tables:
        for row in table.rows:
            assert row
            key = row[0]
            assert isinstance(key, (str, int)) and not isinstance(key, bool)
            if isinstance(key, str):
                assert key != ""


# -- level boundaries (pinned) --------------------------------------------


def test_cache_efficiency_level_boundaries_are_pinned():
    th = ScorecardThresholds()
    cases = [
        (0.0, 5),
        (5.0, 5),
        (5.1, 4),
        (15.0, 4),
        (15.1, 3),
        (30.0, 3),
        (30.1, 2),
        (50.0, 2),
        (50.1, 1),
        (100.0, 1),
    ]
    for value, expected_level in cases:
        inputs = ScorecardInputs(recache_share_pct=value)
        section = build_section(inputs, th)
        dims = {row[0]: row for row in section.tables[0].rows}
        assert dims["cache_efficiency"][1] == expected_level, (value, expected_level)
        assert dims["cache_efficiency"][2] == LEVEL_LABELS[expected_level]


def test_context_hygiene_level_boundaries_are_pinned():
    th = ScorecardThresholds()
    cases = [
        (10_000, 5),
        (50_000, 5),
        (50_001, 4),
        (100_000, 4),
        (100_001, 3),
        (150_000, 3),
        (150_001, 2),
        (200_000, 2),
        (200_001, 1),
    ]
    for value, expected_level in cases:
        inputs = ScorecardInputs(p90_top_level_ctx=value)
        section = build_section(inputs, th)
        dims = {row[0]: row for row in section.tables[0].rows}
        assert dims["context_hygiene"][1] == expected_level, (value, expected_level)


def test_agent_efficiency_level_boundaries_are_pinned():
    th = ScorecardThresholds()
    cases = [(1.0, 5), (1.2, 5), (1.3, 4), (1.5, 4), (1.6, 3), (2.0, 3), (2.1, 2), (3.0, 2), (3.1, 1)]
    for value, expected_level in cases:
        inputs = ScorecardInputs(has_spawns=True, agent_cost_variance_ratio=value)
        section = build_section(inputs, th)
        dims = {row[0]: row for row in section.tables[0].rows}
        assert dims["agent_efficiency"][1] == expected_level, (value, expected_level)


def test_config_fit_level_boundaries_are_pinned():
    th = ScorecardThresholds()
    cases = [(0, 5), (3, 4), (6, 3), (10, 2), (11, 1)]
    for value, expected_level in cases:
        inputs = ScorecardInputs(has_snapshot=True, changed_config_keys=value)
        section = build_section(inputs, th)
        dims = {row[0]: row for row in section.tables[0].rows}
        assert dims["config_fit"][1] == expected_level, (value, expected_level)


def test_data_quality_level_boundaries_are_pinned():
    th = ScorecardThresholds()
    cases = [(100.0, 5), (99.5, 5), (99.4, 4), (97.0, 4), (96.9, 3), (90.0, 3), (89.9, 2), (75.0, 2), (74.9, 1)]
    for value, expected_level in cases:
        inputs = ScorecardInputs(pricing_coverage_pct=value)
        section = build_section(inputs, th)
        dims = {row[0]: row for row in section.tables[0].rows}
        assert dims["data_quality"][1] == expected_level, (value, expected_level)


# -- skip / special-case semantics ---------------------------------------


def test_agent_efficiency_is_omitted_when_corpus_never_spawned():
    inputs = ScorecardInputs(recache_share_pct=1.0, has_spawns=False)
    section = build_section(inputs)
    dims = {row[0] for row in section.tables[0].rows}
    assert "agent_efficiency" not in dims
    assert any("never spawned" in note for note in section.notes)


def test_config_fit_scores_five_when_no_snapshot_available():
    inputs = ScorecardInputs(has_snapshot=False)
    section = build_section(inputs)
    dims = {row[0]: row for row in section.tables[0].rows}
    assert dims["config_fit"][1] == 5
    dimensions_table = section.tables[0]
    assert any("No config snapshot" in (note or "") for note in dimensions_table.notes)


def test_dimensions_with_none_metric_are_skipped_entirely():
    inputs = ScorecardInputs()  # every optional metric left at None
    section = build_section(inputs)
    dims = {row[0] for row in section.tables[0].rows}
    assert "cache_efficiency" not in dims
    assert "context_hygiene" not in dims
    assert "agent_efficiency" not in dims
    # config_fit and data_quality always compute (no None-skip semantics).
    assert "config_fit" in dims
    assert "data_quality" in dims


def test_overall_is_the_minimum_of_the_four_non_data_dimensions_never_an_average():
    inputs = ScorecardInputs(
        recache_share_pct=1.0,  # -> 5
        p90_top_level_ctx=10_000,  # -> 5
        has_spawns=True,
        agent_cost_variance_ratio=5.0,  # -> 1
        has_snapshot=True,
        changed_config_keys=0,  # -> 5
        pricing_coverage_pct=1.0,  # -> 1 (data_quality, excluded from overall)
    )
    section = build_section(inputs)
    overall_row = section.tables[1].rows[0]
    assert overall_row == ["overall", 1, "very poor"]


def test_overall_falls_back_to_config_fit_alone_when_nothing_else_has_data():
    # cache_efficiency/context_hygiene/agent_efficiency all skip (None
    # metrics, no spawns); config_fit always computes (see its own
    # module docstring: "no observed instability" scores 5 rather than
    # being treated as missing), so overall can never actually be
    # "unmeasured" in practice -- it reflects config_fit alone here.
    inputs = ScorecardInputs(has_spawns=False)
    section = build_section(inputs)
    overall_row = section.tables[1].rows[0]
    assert overall_row == ["overall", 5, "excellent"]


def test_all_dimensions_constant_matches_the_five_named_dimensions():
    assert set(ALL_DIMENSIONS) == {
        "cache_efficiency",
        "context_hygiene",
        "agent_efficiency",
        "config_fit",
        "data_quality",
    }


def test_thresholds_from_config_reads_flat_dict_and_ignores_unknown_keys():
    th = ScorecardThresholds.from_config(
        {
            "cache_recache_share_pct": [1.0, 2.0, 3.0, 4.0],
            "unknown_key": [9, 9, 9, 9],
        }
    )
    assert th.cache_recache_share_pct == (1.0, 2.0, 3.0, 4.0)
    assert th.context_p90_ctx == ScorecardThresholds().context_p90_ctx  # default kept


def test_thresholds_from_config_keeps_defaults_for_malformed_input():
    assert ScorecardThresholds.from_config(None) == ScorecardThresholds()
    assert ScorecardThresholds.from_config("not a dict") == ScorecardThresholds()
    assert ScorecardThresholds.from_config({"cache_recache_share_pct": [1, 2]}) == ScorecardThresholds()


# -- contract / privacy ---------------------------------------------------


def test_scorecard_build_section_row_keys_are_all_str_or_int():
    inputs = ScorecardInputs(
        recache_share_pct=10.0,
        p90_top_level_ctx=60_000,
        has_spawns=True,
        agent_cost_variance_ratio=1.4,
        has_snapshot=True,
        changed_config_keys=2,
        pricing_coverage_pct=98.0,
    )
    section = build_section(inputs)
    assert section.key == "scorecard"
    _assert_row_keys_are_valid(section)
    assert_privacy(section)
