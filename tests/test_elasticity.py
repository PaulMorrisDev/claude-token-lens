"""Tests for ``elasticity.py``: usage-log pairing (reset-boundary and
negative-delta drops), the weighted-least-squares-through-origin fit
(recovering a known slope, and refusing below the pair/R² gates),
``express_in_window``'s conversion arithmetic, the ``elasticity`` report
section's own privacy posture, and the ``window-budget`` recommendation
rule's branches.

Fixtures build ``Turn``/``TranscriptResult`` directly (no message text or
tool content involved anywhere in this module, so there is nothing a
synthetic JSONL fixture would add — see ``test_compaction.py`` for the
same "construct the dataclass directly" convention for a pure-computation
module).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from claude_token_lens import elasticity
from claude_token_lens.model import (
    Recommendation,
    ReportMeta,
    ReportModel,
    TranscriptResult,
    Turn,
)
from claude_token_lens.pricing import ModelRates, Pricing

from helpers import assert_privacy

_MODEL = "claude-widget-9"


def _pricing() -> Pricing:
    """Every rate $1/million tokens, so a turn's USD cost equals its own
    token count in millions -- easy to hand-compute (same convention
    ``test_waste.py``'s own ``_pricing()`` fixture uses)."""
    rates = ModelRates(
        canonical_id=_MODEL,
        input=1.0,
        output=1.0,
        cache_write_5m=1.0,
        cache_write_1h=1.0,
        cache_read=1.0,
    )
    return Pricing(
        path="<test>",
        version="test-2026",
        currency="USD",
        source_url=None,
        retrieved=None,
        notes=None,
        sha256="0" * 64,
        models={_MODEL: rates},
    )


def _iso(hour: int, minute: int = 0) -> str:
    return f"2026-09-01T{hour:02d}:{minute:02d}:00.000Z"


def _row(window: str, hour: int, pct: float, resets_at: str = "2026-09-08T00:00:00Z") -> dict:
    return {
        "logged_at": _iso(hour),
        "session_id": "sess-1",
        "window": window,
        "used_percentage": pct,
        "resets_at": resets_at,
        "source": "statusline",
    }


def _turn(hour: int, minute: int, input_tokens: int, turn_index: int) -> Turn:
    return Turn(
        turn_index=turn_index,
        ts=_iso(hour, minute),
        model=_MODEL,
        input_tokens=input_tokens,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        output_tokens=0,
    )


# -- pairing: reset boundary dropped, negative delta dropped -----------------


def test_build_pairs_drops_reset_boundary_and_negative_delta():
    rows = [
        _row("five_hour", 0, 10, resets_at="R1"),
        _row("five_hour", 1, 20, resets_at="R1"),  # kept: delta=10, same reset
        _row("five_hour", 2, 5, resets_at="R2"),  # dropped: resets_at R1 -> R2
        _row("five_hour", 3, 15, resets_at="R2"),  # kept: delta=10, same reset
        _row("five_hour", 4, 12, resets_at="R2"),  # dropped: delta=-3 (negative)
    ]

    pairs, dropped_reset, dropped_negative = elasticity._build_pairs(rows)

    assert len(pairs["five_hour"]) == 2
    assert [p.delta_pct for p in pairs["five_hour"]] == [10, 10]
    assert dropped_reset["five_hour"] == 1
    assert dropped_negative["five_hour"] == 1

    # Untouched windows report zero, not a missing key.
    assert pairs["seven_day"] == []
    assert dropped_reset["seven_day"] == 0
    assert dropped_negative["spend_limit"] == 0


def test_build_pairs_skips_rows_with_unparseable_timestamp_or_percentage():
    rows = [
        _row("seven_day", 0, 10),
        {"logged_at": "not-a-timestamp", "window": "seven_day", "used_percentage": 20, "resets_at": "R"},
        {"logged_at": _iso(1), "window": "seven_day", "used_percentage": "n/a", "resets_at": "R"},
        _row("seven_day", 2, 30),
    ]

    pairs, dropped_reset, dropped_negative = elasticity._build_pairs(rows)

    # Only the two well-formed rows survive to be paired.
    assert len(pairs["seven_day"]) == 1
    assert pairs["seven_day"][0].delta_pct == 20


# -- regression recovers a known slope on synthetic data ---------------------


def _known_slope_fixture(slope: float, volumes_millions: list[int]):
    """Build ``(usage_rows, results)`` for the ``seven_day`` window whose
    pairs have exactly ``delta_pct = slope * volume`` for each of
    ``volumes_millions`` (in whole millions of input tokens), with zero
    noise, so the fitted slope/R² are exactly recoverable.
    """
    usage_rows = [_row("seven_day", 0, 0.0)]
    turns: list[Turn] = []
    running_pct = 0.0
    for i, volume in enumerate(volumes_millions):
        delta = slope * volume
        running_pct += delta
        usage_rows.append(_row("seven_day", i + 1, running_pct))
        turns.append(_turn(hour=i, minute=30, input_tokens=volume * 1_000_000, turn_index=i + 1))
    results = [TranscriptResult(turns=turns)]
    return usage_rows, results


def test_regression_recovers_known_slope_on_synthetic_data():
    volumes = [1, 2, 3, 1, 2, 3, 1, 2, 3, 4]  # 10 pairs, clears min_pairs=8
    usage_rows, results = _known_slope_fixture(slope=2.0, volumes_millions=volumes)

    stats = elasticity.compute_elasticity(usage_rows, results, _pricing())

    fit = stats.fit("seven_day", "new_tokens")
    assert fit is not None
    assert fit.accepted is True
    assert fit.n_pairs == len(volumes)
    assert fit.slope == pytest.approx(2.0, rel=1e-9)
    assert fit.r2 == pytest.approx(1.0, abs=1e-9)
    assert fit.residual_std == pytest.approx(0.0, abs=1e-9)

    # 100 / (percent per million new tokens) = million new tokens per window.
    assert stats.window_budget_million_tokens["seven_day"] == pytest.approx(50.0, rel=1e-9)

    # Every turn had zero cache-read tokens: that metric has no usable
    # (positive-volume) pairs at all and is refused, never asserted to
    # be "near zero" outright -- it is a genuine, reported refusal.
    cache_read_fit = stats.fit("seven_day", "cache_read")
    assert cache_read_fit.accepted is False
    assert cache_read_fit.n_pairs == 0
    assert "cache_read" in cache_read_fit.reason

    # $1/M-token rates make usd volume numerically equal to the
    # new-tokens volume in millions, so the same slope is recoverable
    # against USD too.
    usd_fit = stats.fit("seven_day", "usd")
    assert usd_fit.accepted is True
    assert usd_fit.slope == pytest.approx(2.0, rel=1e-9)

    # Windows with zero usage-log rows at all get no budget entry.
    assert "five_hour" not in stats.window_budget_million_tokens
    assert "spend_limit" not in stats.window_budget_million_tokens


# -- refusal below n/R2 thresholds --------------------------------------------


def test_refuses_when_fewer_than_min_pairs():
    volumes = [1, 2, 3]  # only 3 pairs, below the default min_pairs=8
    usage_rows, results = _known_slope_fixture(slope=2.0, volumes_millions=volumes)

    stats = elasticity.compute_elasticity(usage_rows, results, _pricing())

    fit = stats.fit("seven_day", "new_tokens")
    assert fit.accepted is False
    assert fit.slope is None
    assert fit.n_pairs == 3
    # Failed the pair-count gate specifically, not the R2 gate (this
    # fixture is noise-free, so R2 would otherwise be a perfect 1.0).
    assert "3 pair" in fit.reason
    assert "8" in fit.reason
    assert fit.r2 == pytest.approx(1.0, abs=1e-9)

    assert "seven_day" not in stats.window_budget_million_tokens


def test_refuses_when_r2_below_threshold():
    # 8 pairs, identical volume, but a delta pattern that doesn't scale
    # with volume at all (alternating high/low) -- a real slope estimate
    # exists (mean ratio), but it explains none of the swing: R2 == 0.
    usage_rows = [_row("seven_day", 0, 0.0)]
    turns: list[Turn] = []
    running_pct = 0.0
    deltas = [10, 0, 10, 0, 10, 0, 10, 0]
    for i, delta in enumerate(deltas):
        running_pct += delta
        usage_rows.append(_row("seven_day", i + 1, running_pct))
        turns.append(_turn(hour=i, minute=30, input_tokens=1_000_000, turn_index=i + 1))
    results = [TranscriptResult(turns=turns)]

    stats = elasticity.compute_elasticity(usage_rows, results, _pricing())

    fit = stats.fit("seven_day", "new_tokens")
    assert fit.n_pairs == 8  # clears min_pairs
    assert fit.r2 == pytest.approx(0.0, abs=1e-9)
    assert fit.accepted is False
    assert fit.slope is None
    assert "R²" in fit.reason or "R2" in fit.reason

    assert "seven_day" not in stats.window_budget_million_tokens


def test_thresholds_are_configurable_and_gate_acceptance():
    volumes = [1, 2, 3, 1, 2, 3, 1, 2, 3, 4]
    usage_rows, results = _known_slope_fixture(slope=2.0, volumes_millions=volumes)

    strict = elasticity.ElasticityThresholds(min_pairs=20, min_r2=0.5)
    stats = elasticity.compute_elasticity(usage_rows, results, _pricing(), thresholds=strict)

    fit = stats.fit("seven_day", "new_tokens")
    assert fit.accepted is False
    assert "10 pair" in fit.reason
    assert "20" in fit.reason


def test_from_config_reads_nested_elasticity_dict():
    th = elasticity.ElasticityThresholds.from_config(
        {"elasticity": {"min_pairs": 12, "min_r2": 0.6, "burn_window_hours": 48, "weekly_window": "spend_limit"}}
    )
    assert th.min_pairs == 12
    assert th.min_r2 == pytest.approx(0.6)
    assert th.burn_window_hours == pytest.approx(48.0)
    assert th.weekly_window == "spend_limit"


# -- recent burn --------------------------------------------------------------


def test_recent_burn_share_of_weekly_window():
    volumes = [1, 2, 3, 1, 2, 3, 1, 2, 3, 4]
    usage_rows, results = _known_slope_fixture(slope=2.0, volumes_millions=volumes)

    # The fixture's last turn lands at hour 9, minute 30. Anchor "now" a
    # couple of hours later so the whole fixture's volume falls inside
    # the default 24h burn window.
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    stats = elasticity.compute_elasticity(usage_rows, results, _pricing(), now=now)

    total_new_tokens = sum(volumes) * 1_000_000
    assert stats.recent_new_tokens == pytest.approx(total_new_tokens)
    # slope (2.0 %/M) * total_new_tokens_in_millions
    expected_pct = 2.0 * (total_new_tokens / 1_000_000.0)
    assert stats.recent_burn_pct == pytest.approx(expected_pct, rel=1e-9)
    assert stats.recent_burn_reason is None


def test_recent_burn_reports_reason_when_weekly_fit_not_accepted():
    volumes = [1, 2, 3]  # below min_pairs -> weekly fit refused
    usage_rows, results = _known_slope_fixture(slope=2.0, volumes_millions=volumes)

    stats = elasticity.compute_elasticity(usage_rows, results, _pricing())

    assert stats.recent_burn_pct is None
    assert stats.recent_burn_reason is not None
    assert "seven_day" in stats.recent_burn_reason


# -- express_in_window ---------------------------------------------------


def _stats_with_usd_fits() -> elasticity.ElasticityStats:
    return elasticity.ElasticityStats(
        thresholds=elasticity.ElasticityThresholds(weekly_window="seven_day"),
        fits={
            "seven_day": {
                "usd": elasticity.FitResult(
                    window="seven_day", metric="usd", slope=0.5, r2=0.9, n_pairs=10, residual_std=0.1, accepted=True
                )
            },
            "five_hour": {
                "usd": elasticity.FitResult(
                    window="five_hour",
                    metric="usd",
                    slope=None,
                    r2=0.2,
                    n_pairs=3,
                    residual_std=None,
                    accepted=False,
                    reason="only 3 pair(s)",
                )
            },
        },
    )


def test_express_in_window_converts_usd_saving_to_window_share():
    stats = _stats_with_usd_fits()
    assert elasticity.express_in_window(100.0, stats) == pytest.approx(50.0)


def test_express_in_window_none_for_non_positive_or_non_finite_saving():
    stats = _stats_with_usd_fits()
    assert elasticity.express_in_window(0.0, stats) is None
    assert elasticity.express_in_window(-5.0, stats) is None
    assert elasticity.express_in_window(math.inf, stats) is None
    assert elasticity.express_in_window(math.nan, stats) is None


def test_express_in_window_none_when_fit_refused_or_missing():
    stats = _stats_with_usd_fits()
    assert elasticity.express_in_window(100.0, stats, window="five_hour") is None  # refused fit
    assert elasticity.express_in_window(100.0, stats, window="spend_limit") is None  # no fit at all


# -- build_section / privacy --------------------------------------------------


def test_build_section_shape_and_privacy():
    volumes = [1, 2, 3, 1, 2, 3, 1, 2, 3, 4]
    usage_rows, results = _known_slope_fixture(slope=2.0, volumes_millions=volumes)
    stats = elasticity.compute_elasticity(usage_rows, results, _pricing())

    section = elasticity.build_section(stats)
    assert section.key == "elasticity"

    fit_table = next(t for t in section.tables if t.name == "elasticity_fit")
    # 3 window kinds x 3 metrics = 9 rows.
    assert len(fit_table.rows) == 9
    seven_day_new_tokens = next(r for r in fit_table.rows if r[0] == "seven_day" and r[1] == "new_tokens")
    assert seven_day_new_tokens[3] == pytest.approx(2.0)  # slope
    assert seven_day_new_tokens[7] == "yes"  # accepted

    budget_table = next(t for t in section.tables if t.name == "elasticity_budget")
    seven_day_budget = next(r for r in budget_table.rows if r[0] == "seven_day")
    assert seven_day_budget[1] == pytest.approx(50.0)

    burn_table = next(t for t in section.tables if t.name == "elasticity_recent_burn")
    assert burn_table.rows[0][0] == "seven_day"

    assert_privacy(section)


# -- rule: window-budget -------------------------------------------------


def _report_with_elasticity_section(billing_mode: str, recommendations=None) -> ReportModel:
    volumes = [1, 2, 3, 1, 2, 3, 1, 2, 3, 4]
    usage_rows, results = _known_slope_fixture(slope=2.0, volumes_millions=volumes)
    stats = elasticity.compute_elasticity(usage_rows, results, _pricing())
    section = elasticity.build_section(stats)
    return ReportModel(
        meta=ReportMeta(billing_mode=billing_mode),
        sections=[section],
        recommendations=list(recommendations or []),
    )


def test_rule_does_not_fire_outside_subscription_billing():
    report = _report_with_elasticity_section(billing_mode="api")
    th = elasticity.ElasticityThresholds()
    assert elasticity.RULES[0](report, th) == []


def test_rule_does_not_fire_when_elasticity_section_absent():
    report = ReportModel(meta=ReportMeta(billing_mode="subscription"), sections=[])
    th = elasticity.ElasticityThresholds()
    assert elasticity.RULES[0](report, th) == []


def test_rule_fires_with_budget_and_no_lever_clause_when_no_other_recommendations():
    report = _report_with_elasticity_section(billing_mode="subscription")
    th = elasticity.ElasticityThresholds()

    recs = elasticity.RULES[0](report, th)

    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == "window-budget"
    assert isinstance(rec, Recommendation)
    assert "50.0 million new tokens" in rec.action
    assert "recommendation" not in rec.action.lower()  # nothing else to point at yet


def test_rule_names_the_biggest_other_lever_by_id_without_repeating_its_number():
    other = Recommendation(
        id="wasted-turns",
        severity="advice",
        category="workflow",
        title="A material share of spend went to turns with no benefit",
        action="37.0% of priced cost went nowhere.",
        evidence=[("Recoverable spend ceiling", 942.17, "waste.waste_summary", "all")],
    )
    report = _report_with_elasticity_section(billing_mode="subscription", recommendations=[other])
    th = elasticity.ElasticityThresholds()

    recs = elasticity.RULES[0](report, th)

    assert len(recs) == 1
    action = recs[0].action
    assert "'wasted-turns'" in action
    assert "942.17" not in action  # names the lever, never repeats its own number


def test_rule_picks_action_severity_over_info_for_biggest_lever():
    info_rec = Recommendation(
        id="pricing-coverage",
        severity="info",
        category="data",
        title="Some turns priced against an unknown model",
        action="...",
        evidence=[("Unpriced cost", 5000.0, "overview", "totals")],
    )
    action_rec = Recommendation(
        id="model-tier",
        severity="action",
        category="settings",
        title="Move a subagent down a model tier",
        action="...",
        evidence=[("Ceiling saving", 12.0, "model_swap", "row")],
    )
    report = _report_with_elasticity_section(billing_mode="subscription", recommendations=[info_rec, action_rec])
    th = elasticity.ElasticityThresholds()

    recs = elasticity.RULES[0](report, th)

    assert "'model-tier'" in recs[0].action
    assert "'pricing-coverage'" not in recs[0].action


def test_rule_evidence_cites_real_table_cells():
    report = _report_with_elasticity_section(billing_mode="subscription")
    th = elasticity.ElasticityThresholds()

    recs = elasticity.RULES[0](report, th)
    assert len(recs) == 1

    for label, value, source_table, row_key in recs[0].evidence:
        section_key, table_name = source_table.split(".", 1)
        section = next(s for s in report.sections if s.key == section_key)
        table = next(t for t in section.tables if t.name == table_name)
        row = next(r for r in table.rows if r[0] == row_key)
        assert value in row, f"{label}: {value!r} not found in row {row!r} for {source_table}/{row_key}"
