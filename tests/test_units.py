"""Tests for :mod:`claude_token_lens.units`: :class:`Units`/:class:`Amount`
phrase amounts for the billing mode, and :meth:`Amount.phrase` must never
double "about" when a subscription's own weekly-limit-share text already
opens with it (plan finding F3, proposal UX-2)."""

from __future__ import annotations

from claude_token_lens.units import NO_LIMIT_SHARE_HINT, Amount, Units

from helpers import elasticity_with_slope as _elasticity_with_slope


# -- Units.money ------------------------------------------------------------


def test_money_is_none_for_non_positive_or_non_finite():
    units = Units()
    assert units.money(0.0) is None
    assert units.money(-1.0) is None
    assert units.money(float("nan")) is None
    assert units.money(float("inf")) is None


def test_api_mode_is_plain_dollars_with_no_dollar_sign_character():
    units = Units(billing_mode="api", currency="USD")
    amount = units.money(12.345, period="over 30 days")
    assert amount is not None
    assert "$" not in amount.text()
    assert amount.primary == "12.35 USD over 30 days"
    assert amount.secondary == ""
    assert amount.basis == "at list price"


def test_subscription_without_elasticity_falls_back_to_list_price_equivalent():
    units = Units(billing_mode="subscription", currency="USD")
    amount = units.money(5.0)
    assert amount is not None
    assert "$" not in amount.text()
    assert amount.primary == "5.00 USD list-price equivalent"
    assert amount.basis == NO_LIMIT_SHARE_HINT


def test_subscription_with_elasticity_is_a_weekly_limit_share():
    units = Units(billing_mode="subscription", currency="USD", elasticity=_elasticity_with_slope(0.5))
    amount = units.money(2.0)
    assert amount is not None
    assert "$" not in amount.text()
    assert amount.primary.startswith("about 1.0% of your weekly usage limit")
    assert amount.secondary == "2.00 USD list-price equivalent"


def test_subscription_small_share_keeps_two_decimals():
    units = Units(billing_mode="subscription", currency="USD", elasticity=_elasticity_with_slope(0.01))
    amount = units.money(2.0)
    assert amount is not None
    # 0.01 * 2.0 = 0.02%, under 1 -- two decimals so it doesn't read "0.0%".
    assert "0.02%" in amount.primary


# -- Amount.phrase: no doubled "about" ---------------------------------------


def test_phrase_with_no_prefix_is_just_text():
    amount = Amount(primary="12.34 USD")
    assert amount.phrase() == "12.34 USD"
    assert amount.phrase("") == "12.34 USD"


def test_phrase_joins_a_non_about_prefix_normally():
    amount = Amount(primary="12.34 USD")
    assert amount.phrase("At most ") == "At most 12.34 USD"


def test_phrase_does_not_double_about_when_primary_already_says_it():
    amount = Amount(primary="about 0.42% of your weekly usage limit", secondary="12.34 USD list-price equivalent")
    assert amount.phrase("About ") == "about 0.42% of your weekly usage limit (12.34 USD list-price equivalent)"
    assert "about about" not in amount.phrase("About ").lower()


def test_phrase_about_prefix_still_applies_when_primary_does_not_say_it():
    amount = Amount(primary="12.34 USD")
    assert amount.phrase("About ") == "About 12.34 USD"


def test_phrase_inserts_a_space_for_a_bare_word_prefix():
    amount = Amount(primary="12.34 USD")
    assert amount.phrase("About") == "About 12.34 USD"


# -- Units.money_text: always non-empty, never a bare "$" ---------------------


def test_money_text_falls_back_to_plain_zero_for_non_positive_amounts():
    units = Units(billing_mode="api", currency="USD")
    assert units.money_text(0.0) == "0.00 USD"
    assert units.money_text(-3.0) == "-3.00 USD"
    assert "$" not in units.money_text(0.0)


def test_money_text_uses_the_full_phrased_amount_when_positive():
    units = Units(billing_mode="subscription", currency="USD", elasticity=_elasticity_with_slope(0.5))
    text = units.money_text(2.0)
    assert text.startswith("about 1.0% of your weekly usage limit")
    assert "$" not in text
