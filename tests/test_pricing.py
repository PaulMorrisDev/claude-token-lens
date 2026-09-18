"""Tests for WP2: rate-card loading, model-id resolution and per-turn
pricing (``src/claude_token_lens/pricing.py``).

Turns are built directly as ``model.Turn`` instances (there is no parser
yet — that is WP1) via the small ``_turn`` helper below rather than
``tests/helpers.py``'s JSONL builders, which build raw transcript lines.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_token_lens import model
from claude_token_lens.pricing import (
    ModelRates,
    Pricing,
    PricingCoverage,
    PricingError,
    ResolvedRates,
    load_pricing,
    price_turn,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: Every model id the WP2 brief requires in the packaged pricing.toml.
REQUIRED_PACKAGED_MODEL_IDS = {
    "claude-fable-5-1",
    "claude-fable-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-opus-4-1",
    "claude-opus-4",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-sonnet-4",
    "claude-haiku-4-5-20251001",
    "claude-3-5-haiku-20241022",
    "claude-3-5-sonnet-20241022",
    "claude-3-7-sonnet-20250219",
    "claude-3-haiku-20240307",
    "claude-3-opus-20240229",
}


def _turn(**overrides) -> model.Turn:
    """Build a ``Turn`` with sane zero defaults, overridable per field."""
    fields = dict(
        message_id="msg_1",
        request_id="req_1",
        turn_index=1,
        ts="2026-09-18T12:00:00.000Z",
        model="claude-sonnet-5",
        input_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        output_tokens=0,
        cc_5m=0,
        cc_1h=0,
        ctx=0,
    )
    fields.update(overrides)
    return model.Turn(**fields)


# --------------------------------------------------------------------
# Packaged default
# --------------------------------------------------------------------


def test_packaged_default_loads():
    pricing = load_pricing()
    assert pricing.version == "2026-09-18"
    assert pricing.currency == "USD"
    assert pricing.sha256
    assert len(pricing.sha8) == 8


def test_packaged_default_covers_every_required_model_id():
    pricing = load_pricing()
    assert REQUIRED_PACKAGED_MODEL_IDS <= set(pricing.models)
    for model_id in REQUIRED_PACKAGED_MODEL_IDS:
        resolved = pricing.resolve_model(model_id)
        assert resolved is not None, model_id
        assert resolved.canonical_id == model_id
        assert resolved.matched_via == "exact"


def test_packaged_default_currency_and_version_surfaced():
    pricing = load_pricing()
    assert pricing.currency == "USD"
    assert pricing.version == "2026-09-18"
    assert pricing.source_url


# --------------------------------------------------------------------
# Path resolution order
# --------------------------------------------------------------------


def test_explicit_path_overrides_everything(tmp_path):
    explicit = tmp_path / "explicit.toml"
    explicit.write_text((FIXTURES / "pricing_min.toml").read_text(encoding="utf-8"), encoding="utf-8")
    pricing = load_pricing(path=explicit, config_dir=tmp_path / "unused")
    assert pricing.path == str(explicit)
    assert "claude-widget-9" in pricing.models


def test_config_dir_used_when_present(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    token_lens_dir.mkdir()
    (token_lens_dir / "pricing.toml").write_text(
        (FIXTURES / "pricing_min.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    pricing = load_pricing(config_dir=token_lens_dir)
    assert "claude-widget-9" in pricing.models
    assert pricing.path == str(token_lens_dir / "pricing.toml")


def test_falls_back_to_packaged_default_when_config_dir_empty(tmp_path):
    pricing = load_pricing(config_dir=tmp_path / "token-lens")
    assert "claude-sonnet-5" in pricing.models
    assert "packaged default" in pricing.path


def test_claude_config_dir_env_var_moves_the_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    token_lens_dir = tmp_path / "token-lens"
    token_lens_dir.mkdir()
    (token_lens_dir / "pricing.toml").write_text(
        (FIXTURES / "pricing_min.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    pricing = load_pricing()
    assert "claude-widget-9" in pricing.models


# --------------------------------------------------------------------
# Malformed files -> PricingError
# --------------------------------------------------------------------


def test_malformed_toml_syntax_raises_pricing_error():
    with pytest.raises(PricingError):
        load_pricing(path=FIXTURES / "pricing_bad.toml")


def test_missing_required_field_raises_pricing_error():
    with pytest.raises(PricingError, match="cache_read"):
        load_pricing(path=FIXTURES / "pricing_missing_field.toml")


def test_missing_file_raises_pricing_error(tmp_path):
    with pytest.raises(PricingError):
        load_pricing(path=tmp_path / "does-not-exist.toml")


# --------------------------------------------------------------------
# Model resolution
# --------------------------------------------------------------------


@pytest.fixture()
def min_pricing() -> Pricing:
    return load_pricing(path=FIXTURES / "pricing_min.toml")


def test_alias_resolution(min_pricing):
    resolved = min_pricing.resolve_model("widget")
    assert resolved is not None
    assert resolved.canonical_id == "claude-widget-9"
    assert resolved.matched_via == "alias"


def test_alias_resolution_with_1m_suffix_in_alias_table(min_pricing):
    # "widget[1m]" is registered directly as an alias, so it should
    # resolve as an ordinary alias hit, not via the [1m]-stripping step.
    resolved = min_pricing.resolve_model("widget[1m]")
    assert resolved is not None
    assert resolved.canonical_id == "claude-widget-9"
    assert resolved.matched_via == "alias"


def test_packaged_fable_1m_alias():
    pricing = load_pricing()
    resolved = pricing.resolve_model("fable[1m]")
    assert resolved is not None
    assert resolved.canonical_id == "claude-fable-5-1"
    assert resolved.matched_via == "alias"


def test_strip_1m_suffix_for_an_unregistered_alias(min_pricing):
    # "claude-gadget-2" has no "[1m]" alias registered, so resolution
    # must fall through to the strip-1m step against its exact id.
    resolved = min_pricing.resolve_model("claude-gadget-2[1m]")
    assert resolved is not None
    assert resolved.canonical_id == "claude-gadget-2"
    assert resolved.matched_via == "strip_1m"


def test_bedrock_prefix_and_suffix_strip():
    pricing = load_pricing()
    resolved = pricing.resolve_model("us.anthropic.claude-opus-4-8")
    assert resolved is not None
    assert resolved.canonical_id == "claude-opus-4-8"
    assert resolved.matched_via == "cloud_strip"


def test_bedrock_versioned_suffix_strip(min_pricing):
    resolved = min_pricing.resolve_model("anthropic.claude-widget-9-v1:0")
    assert resolved is not None
    assert resolved.canonical_id == "claude-widget-9"
    assert resolved.matched_via == "cloud_strip"


def test_vertex_date_suffix_strip():
    pricing = load_pricing()
    resolved = pricing.resolve_model("claude-sonnet-4-5@20250929")
    assert resolved is not None
    assert resolved.canonical_id == "claude-sonnet-4-5"
    assert resolved.matched_via == "cloud_strip"


def test_longest_prefix_match(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9-preview-2026")
    assert resolved is not None
    assert resolved.canonical_id == "claude-widget-9"
    assert resolved.matched_via == "prefix"


#: Two models sharing a numeric prefix, used to isolate the token-
#: boundary rule from the packaged pricing.toml's own model set (which
#: happens to give "claude-opus-4" and "claude-opus-4-1" identical
#: rates, making a wrong match invisible in the numbers).
_BOUNDARY_FIXTURE_TOML = """
version = "test"
currency = "USD"

[models."claude-foo-4-1"]
aliases = []
input = 1.0
output = 2.0
cache_write_5m = 0.5
cache_write_1h = 0.8
cache_read = 0.1
"""

_BOUNDARY_FIXTURE_WITH_SHORTER_TOML = (
    _BOUNDARY_FIXTURE_TOML
    + """
[models."claude-foo-4"]
aliases = []
input = 9.0
output = 9.0
cache_write_5m = 9.0
cache_write_1h = 9.0
cache_read = 9.0
"""
)


def test_prefix_match_requires_token_boundary_no_shorter_fallback(tmp_path):
    path = tmp_path / "boundary.toml"
    path.write_text(_BOUNDARY_FIXTURE_TOML, encoding="utf-8")
    pricing = load_pricing(path=path)
    # "claude-foo-4-10-x" merely shares the leading digits of the
    # registered "claude-foo-4-1" id — the character right after the
    # would-be match is "0", not "-"/"@"/end — and there's no shorter
    # registered id to fall back to, so it must resolve to None.
    assert pricing.resolve_model("claude-foo-4-10-x") is None


def test_prefix_match_boundary_rejects_longer_falls_back_to_shorter(tmp_path):
    path = tmp_path / "boundary2.toml"
    path.write_text(_BOUNDARY_FIXTURE_WITH_SHORTER_TOML, encoding="utf-8")
    pricing = load_pricing(path=path)
    resolved = pricing.resolve_model("claude-foo-4-10-x")
    assert resolved is not None
    # Must NOT resolve to "claude-foo-4-1" (fails the boundary check) —
    # the shorter "claude-foo-4" is a genuinely valid boundary match.
    assert resolved.canonical_id == "claude-foo-4"
    assert resolved.matched_via == "prefix"


def test_prefix_match_boundary_dash_still_resolves():
    pricing = load_pricing()
    resolved = pricing.resolve_model("claude-opus-4-1-preview")
    assert resolved is not None
    assert resolved.canonical_id == "claude-opus-4-1"
    assert resolved.matched_via == "prefix"


def test_cloud_strip_matched_via_survives_a_subsequent_prefix_step(min_pricing):
    # "claude-widget-9" has no exact/alias hit for the dated variant
    # below, so after the Bedrock prefix is stripped, resolution only
    # succeeds via the prefix-match step — matched_via must still report
    # "cloud_strip", not "prefix", since the cloud wrapping is the
    # substantive fact.
    resolved = min_pricing.resolve_model("us.anthropic.claude-widget-9-20260101")
    assert resolved is not None
    assert resolved.canonical_id == "claude-widget-9"
    assert resolved.matched_via == "cloud_strip"


def test_unknown_model_resolves_to_none(min_pricing):
    assert min_pricing.resolve_model("claude-totally-unheard-of") is None


# --------------------------------------------------------------------
# Legacy dated ids (independent-review fix 5)
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("claude-3-5-haiku-20241022", (0.8, 4.0, 1.0, 1.6, 0.08)),
        ("claude-3-5-sonnet-20241022", (3.0, 15.0, 3.75, 6.0, 0.3)),
        ("claude-3-7-sonnet-20250219", (3.0, 15.0, 3.75, 6.0, 0.3)),
        ("claude-3-haiku-20240307", (0.25, 1.25, 0.3, 0.5, 0.03)),
        ("claude-3-opus-20240229", (15.0, 75.0, 18.75, 30.0, 1.5)),
    ],
)
def test_packaged_legacy_ids_resolve_exactly_with_documented_rates(model_id, expected):
    pricing = load_pricing()
    resolved = pricing.resolve_model(model_id)
    assert resolved is not None
    assert resolved.canonical_id == model_id
    assert resolved.matched_via == "exact"
    rates = resolved.rates
    assert (
        rates.input,
        rates.output,
        rates.cache_write_5m,
        rates.cache_write_1h,
        rates.cache_read,
    ) == expected


def test_packaged_legacy_ids_have_no_geo_multiplier():
    # The documented "us" uplift only applies to 4.6-generation and
    # later models; every legacy dated id must carry none.
    pricing = load_pricing()
    for model_id in [
        "claude-3-5-haiku-20241022",
        "claude-3-5-sonnet-20241022",
        "claude-3-7-sonnet-20250219",
        "claude-3-haiku-20240307",
        "claude-3-opus-20240229",
    ]:
        assert pricing.models[model_id].geo_multipliers == {}


def test_packaged_web_search_counts_only_by_default():
    pricing = load_pricing()
    assert pricing.server_tools["web_search_per_1000"] == 0.0


@pytest.mark.parametrize("model_id", [None, "", "<synthetic>"])
def test_synthetic_and_empty_resolve_to_none_without_error(min_pricing, model_id):
    assert min_pricing.resolve_model(model_id) is None


# --------------------------------------------------------------------
# price_turn
# --------------------------------------------------------------------


def test_hand_computed_money_sonnet_5():
    pricing = load_pricing()
    resolved = pricing.resolve_model("claude-sonnet-5")
    turn = _turn(model="claude-sonnet-5", input_tokens=1_000_000, cache_read_tokens=2_000_000)
    breakdown = price_turn(turn, resolved)
    assert breakdown.model_known is True
    assert breakdown.input_cost == pytest.approx(2.00)
    assert breakdown.cache_read_cost == pytest.approx(0.40)
    assert breakdown.output_cost == pytest.approx(0.0)
    assert breakdown.cache_write_cost == pytest.approx(0.0)
    assert breakdown.total == pytest.approx(2.40)


def test_price_turn_accepts_bare_model_rates_too():
    pricing = load_pricing()
    resolved = pricing.resolve_model("claude-sonnet-5")
    turn = _turn(input_tokens=1_000_000)
    via_resolved = price_turn(turn, resolved)
    via_bare_rates = price_turn(turn, resolved.rates)
    assert via_resolved == via_bare_rates


def test_unknown_model_prices_at_zero(min_pricing):
    turn = _turn(model="claude-totally-unheard-of", input_tokens=1_000_000)
    resolved = min_pricing.resolve_model(turn.model)
    breakdown = price_turn(turn, resolved)
    assert breakdown.model_known is False
    assert breakdown.total == 0.0
    assert breakdown.input_cost == 0.0


def test_default_write_split_uses_turn_cc_5m_and_cc_1h(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9", cc_5m=1_000_000, cc_1h=500_000)
    breakdown = price_turn(turn, resolved)
    # 1,000,000 * 0.5 (cache_write_5m) + 500,000 * 0.8 (cache_write_1h), /1e6
    assert breakdown.cache_write_cost == pytest.approx(1_000_000 / 1e6 * 0.5 + 500_000 / 1e6 * 0.8)


def test_simulation_path_equals_default_path_for_observed_split(min_pricing):
    """The invariant the TTL package (WP4) relies on: passing a turn's own
    observed split explicitly through the simulation arguments must equal
    the default (implicit) path exactly.
    """
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(
        model="claude-widget-9",
        input_tokens=12_345,
        output_tokens=6_789,
        cc_5m=222_000,
        cc_1h=111_000,
        cache_read_tokens=444_000,
    )
    default_breakdown = price_turn(turn, resolved)
    simulated_breakdown = price_turn(
        turn,
        resolved,
        write_split={"5m": turn.cc_5m, "1h": turn.cc_1h},
        read_tokens=turn.cache_read_tokens,
    )
    assert simulated_breakdown == default_breakdown


def test_write_split_simulation_overrides_observed_values(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    # Turn observed an all-5m split; simulate what an all-1h policy
    # would have cost for the same cacheable volume instead.
    turn = _turn(model="claude-widget-9", cc_5m=1_000_000, cc_1h=0, cache_read_tokens=0)
    simulated_1h = price_turn(turn, resolved, write_split={"1h": 1_000_000}, read_tokens=0)
    assert simulated_1h.cache_write_cost == pytest.approx(1_000_000 / 1e6 * 0.8)
    assert simulated_1h.cache_write_cost != price_turn(turn, resolved).cache_write_cost


# --------------------------------------------------------------------
# write_split key normalisation (independent-review fix 4; plan
# Appendix A4's TTL simulation calls price_turn with write_split={T:
# write} where T is 300/3600, not "5m"/"1h").
# --------------------------------------------------------------------


def test_write_split_accepts_seconds_int_key_for_5m(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9")
    breakdown = price_turn(turn, resolved, write_split={300: 1_000_000}, read_tokens=0)
    assert breakdown.cache_write_cost == pytest.approx(1_000_000 / 1e6 * 0.5)  # cache_write_5m rate


def test_write_split_accepts_seconds_int_key_for_1h(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9")
    breakdown = price_turn(turn, resolved, write_split={3600: 1_000_000}, read_tokens=0)
    assert breakdown.cache_write_cost == pytest.approx(1_000_000 / 1e6 * 0.8)  # cache_write_1h rate


def test_write_split_accepts_stringified_seconds_key(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9")
    via_int = price_turn(turn, resolved, write_split={300: 1_000_000}, read_tokens=0)
    via_str = price_turn(turn, resolved, write_split={"300": 1_000_000}, read_tokens=0)
    assert via_int == via_str


def test_write_split_unknown_key_raises_value_error(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9")
    with pytest.raises(ValueError):
        price_turn(turn, resolved, write_split={"90m": 1_000_000}, read_tokens=0)


def test_geo_multiplier_applied_to_all_four_components(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(
        model="claude-widget-9",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cc_5m=1_000_000,
        cache_read_tokens=1_000_000,
    )
    base = price_turn(turn, resolved)
    with_geo = price_turn(turn, resolved, geo="us")
    assert with_geo.input_cost == pytest.approx(base.input_cost * 1.2)
    assert with_geo.output_cost == pytest.approx(base.output_cost * 1.2)
    assert with_geo.cache_write_cost == pytest.approx(base.cache_write_cost * 1.2)
    assert with_geo.cache_read_cost == pytest.approx(base.cache_read_cost * 1.2)
    assert with_geo.total == pytest.approx(base.total * 1.2)


def test_geo_multiplier_no_effect_when_geo_unmatched(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9", input_tokens=1_000_000)
    base = price_turn(turn, resolved)
    other_geo = price_turn(turn, resolved, geo="eu")
    assert other_geo == base


# --------------------------------------------------------------------
# geo sentinel default (independent-review fix 4): omitting `geo` means
# "use turn.inference_geo"; an explicit geo=None disables it even when
# the turn observed one.
# --------------------------------------------------------------------


def test_geo_omitted_uses_turns_own_inference_geo(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9", input_tokens=1_000_000, inference_geo="us")
    breakdown = price_turn(turn, resolved)  # geo not passed at all
    assert breakdown.input_cost == pytest.approx(1_000_000 / 1e6 * 1.0 * 1.2)


def test_geo_explicit_none_overrides_turns_inference_geo(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9", input_tokens=1_000_000, inference_geo="us")
    breakdown = price_turn(turn, resolved, geo=None)  # explicit override disables it
    assert breakdown.input_cost == pytest.approx(1_000_000 / 1e6 * 1.0)


def test_geo_not_available_string_applies_no_multiplier(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9", input_tokens=1_000_000, inference_geo="not_available")
    breakdown = price_turn(turn, resolved)
    assert breakdown.input_cost == pytest.approx(1_000_000 / 1e6 * 1.0)


def test_long_context_multiplier_applies_above_threshold(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")  # multiplier=2.0 at >=100000
    below = _turn(model="claude-widget-9", input_tokens=1_000_000, ctx=99_999)
    at_threshold = _turn(model="claude-widget-9", input_tokens=1_000_000, ctx=100_000)

    below_breakdown = price_turn(below, resolved)
    at_breakdown = price_turn(at_threshold, resolved)

    assert below_breakdown.long_context_applied is False
    assert at_breakdown.long_context_applied is True
    assert at_breakdown.input_cost == pytest.approx(below_breakdown.input_cost * 2.0)


def test_long_context_explicit_overrides_win_over_multiplier(min_pricing):
    resolved = min_pricing.resolve_model("claude-gadget-2")
    # long_context on claude-gadget-2 carries BOTH multiplier=3.0 and
    # explicit input/cache_read overrides; overrides must win for the
    # fields they cover.
    turn = _turn(
        model="claude-gadget-2",
        input_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        ctx=50_000,
    )
    breakdown = price_turn(turn, resolved)
    assert breakdown.long_context_applied is True
    # explicit override: input rate becomes 9.0/million, not 4.0*3=12.0
    assert breakdown.input_cost == pytest.approx(9.0)
    # explicit override: cache_read rate becomes 0.9/million, not 0.4*3=1.2
    assert breakdown.cache_read_cost == pytest.approx(0.9)


# --------------------------------------------------------------------
# PricingCoverage
# --------------------------------------------------------------------


def test_coverage_tracks_unknown_models_and_computes_percentage(min_pricing):
    coverage = PricingCoverage()

    known_turn = _turn(model="claude-widget-9", input_tokens=1_000_000)
    known_resolved = min_pricing.resolve_model(known_turn.model)
    coverage.add(known_turn, price_turn(known_turn, known_resolved))

    unknown_turn = _turn(model="claude-mystery-model", input_tokens=1_000_000)
    unknown_resolved = min_pricing.resolve_model(unknown_turn.model)
    coverage.add(unknown_turn, price_turn(unknown_turn, unknown_resolved))

    assert coverage.total_turns == 2
    assert coverage.priced_turns == 1
    assert "claude-mystery-model" in coverage.unknown
    assert coverage.unknown["claude-mystery-model"]["turns"] == 1
    assert coverage.unknown["claude-mystery-model"]["tokens"] == 1_000_000
    assert coverage.coverage_pct == pytest.approx(50.0)

    table = coverage.as_table()
    assert table.rows == [["claude-mystery-model", 1, 1_000_000]]


def test_coverage_pct_is_100_when_nothing_recorded():
    coverage = PricingCoverage()
    assert coverage.coverage_pct == 100.0


# --------------------------------------------------------------------
# Pricing.describe()
# --------------------------------------------------------------------


def test_describe_lists_every_model_with_its_aliases(min_pricing):
    table = min_pricing.describe()
    ids = [row[0] for row in table.rows]
    assert ids == sorted(ids)
    assert "claude-widget-9" in ids
    widget_row = table.rows[ids.index("claude-widget-9")]
    aliases_cell = widget_row[1]
    assert "widget" in aliases_cell
    assert "widget[1m]" in aliases_cell


def test_describe_packaged_default_lists_every_required_model():
    pricing = load_pricing()
    table = pricing.describe()
    ids = {row[0] for row in table.rows}
    assert REQUIRED_PACKAGED_MODEL_IDS <= ids


# --------------------------------------------------------------------
# Dataclass sanity (matches the module's own contract, not model.py's)
# --------------------------------------------------------------------


def test_resolved_rates_wraps_model_rates(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    assert isinstance(resolved, ResolvedRates)
    assert isinstance(resolved.rates, ModelRates)
    assert resolved.rates.canonical_id == "claude-widget-9"
