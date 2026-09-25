"""Glossary > How costs work and its Terms cross-links (Phase 8).

Three things are checked here, on top of what test_pricing.py already
holds `Pricing.rates_meta()` to (``report.json``'s ``meta.rates``, the
one place a ratio comes from):

1. Every ratio the packaged `pricing.toml` yields is worded sensibly by
   `format.js`'s `fraction()` -- read out of its own source, the same
   way `test_service_static.py` reads `GLOSSARY`/`PAGES`, and applied in
   Python (there is no JS runtime in this suite; `node --check` is a
   manual, not automated, check per that file's own comment).
2. `costs.js` and `page-glossary.js` never spell out a multiplier
   themselves -- every price ratio in their copy is produced by a call
   to `fraction()`/`priced()`, never a table word or a raw number typed
   into a sentence.
3. `links.js`'s `COST_CARDS` is internally consistent (unique slugs,
   every `terms` entry a real `GLOSSARY` term), every slug has a rule
   function in `costs.js` and is actually drawn by `page-glossary.js`,
   and both Glossary segments handle their deep-link parameter
   (`?card=`/`?term=`).

`tests/test_ui_copy.py` already holds `costs.js`/`page-glossary.js` to
the house style (banned words, no snake_case, short sentences); this
file only adds the multiplier-specific checks its general word scan
can't.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest

from claudeglass.pricing import load_pricing

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / "src" / "claudeglass" / "service" / "static"


def _static_text(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def _js_modules() -> list[Path]:
    return sorted(p for p in STATIC_DIR.iterdir() if p.suffix == ".js")


def _app_js() -> str:
    """Every first-party JS module, concatenated (mirrors
    test_service_static.py's helper of the same name), so a helper
    moved between page-actions.js/costs.js/page-glossary.js is found
    wherever it now lives."""
    return "\n".join(p.read_text(encoding="utf-8") for p in _js_modules())


# -- tiny JS source readers (test_service_static.py's helpers, kept
# local per this suite's convention of not importing one test module
# from another -- see test_ui_copy.py's own _string_literals/_modules) --


def _skip_js_string_or_comment(src: str, i: int) -> int:
    ch = src[i]
    if ch in "\"'`":
        j = i + 1
        while j < len(src) and src[j] != ch:
            j += 2 if src[j] == "\\" else 1
        return j + 1
    if src.startswith("//", i):
        end = src.find("\n", i)
        return len(src) if end == -1 else end
    if src.startswith("/*", i):
        end = src.find("*/", i + 2)
        return len(src) if end == -1 else end + 2
    return i


def _balanced_end(src: str, open_index: int) -> int:
    pairs = {"{": "}", "(": ")", "[": "]"}
    stack: list[str] = []
    i = open_index
    while i < len(src):
        skipped = _skip_js_string_or_comment(src, i)
        if skipped != i:
            i = skipped
            continue
        ch = src[i]
        if ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return i + 1
        i += 1
    raise AssertionError(f"unbalanced bracket opened at {open_index}")


def _declaration_source(src: str, name: str) -> str:
    match = re.search(r"(?:export\s+)?(?:var|let|const)\s+" + re.escape(name) + r"\s*=\s*", src)
    assert match, f"no declaration of {name} in the dashboard's modules"
    return src[match.start() : _balanced_end(src, match.end())]


def _function_source(src: str, name: str) -> str:
    match = re.search(r"(?<![\w$.])function\s+" + re.escape(name) + r"\s*\(", src)
    assert match, f"no function {name}() in the dashboard's modules"
    params_end = _balanced_end(src, match.end() - 1)
    body_start = src.index("{", params_end)
    return src[match.start() : _balanced_end(src, body_start)]


def _js_literal_to_json(src: str) -> object:
    """A JS object/array literal (bare or quoted keys, trailing commas)
    as Python data -- test_service_static.py's helper of the same name."""

    def quote_key(match: re.Match) -> str:
        return match.group(0) if match.group(1) is None else '"' + match.group(1) + '":'

    text = re.sub(r'"(?:[^"\\]|\\.)*"|([A-Za-z_]\w*)\s*:', quote_key, src)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return json.loads(text)


def _string_literals(src: str) -> list[str]:
    """Every quoted string's text, comments and regex literals skipped
    (test_ui_copy.py's scanner, minus the line numbers this file doesn't
    need)."""
    found = []
    i, n = 0, len(src)
    preceders = set("=(,:;!&|?{}[\n")
    while i < n:
        ch = src[i]
        if src.startswith("//", i):
            end = src.find("\n", i)
            i = n if end == -1 else end
            continue
        if src.startswith("/*", i):
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if ch in "\"'`":
            j = i + 1
            while j < n and src[j] != ch:
                j += 2 if src[j] == "\\" else 1
            found.append(src[i + 1 : j])
            i = j + 1
            continue
        if ch == "/":
            before = src[:i].rstrip()
            if not before or before[-1] in preceders:
                j = i + 1
                while j < n and src[j] not in "/\n":
                    if src[j] == "\\":
                        j += 1
                    elif src[j] == "[":
                        while j < n and src[j] != "]":
                            j += 2 if src[j] == "\\" else 1
                    j += 1
                i = j + 1
                continue
        i += 1
    return found


def _cost_cards() -> list[dict]:
    source = _declaration_source(_app_js(), "COST_CARDS")
    return _js_literal_to_json(source[source.index("[") :].rstrip().rstrip(";"))


def _glossary_terms() -> set[str]:
    pairs = re.findall(r'\["([^"]+)", "([^"]+)"\]', _declaration_source(_app_js(), "GLOSSARY"))
    assert pairs, "GLOSSARY has no entries"
    return {name for name, _ in pairs}


# --------------------------------------------------------------------
# (a)/(b): every ratio pricing.toml yields is worded correctly by
# fraction(), read out of format.js rather than re-typed here.
# --------------------------------------------------------------------


def _num(expr: str) -> float:
    expr = expr.strip()
    if "/" in expr:
        num, den = expr.split("/")
        return float(num) / float(den)
    return float(expr)


def _fraction_table() -> list[tuple[float, str]]:
    decl = _declaration_source(_static_text("format.js"), "FRACTION_WORDS")
    pairs = [(_num(value), phrase) for value, phrase in re.findall(r"\[\s*([0-9./ ]+?)\s*,\s*\"([^\"]+)\"\s*\]", decl)]
    assert len(pairs) >= 9, pairs
    return pairs


def _fraction(ratio: float, table: list[tuple[float, str]]) -> str:
    """format.js's fraction(), reimplemented from its own parsed table
    and rounding rule (Math.round ~= Python round for the positive,
    non-half values real pricing ratios produce)."""
    if not math.isfinite(ratio) or ratio <= 0:
        return ""
    for value, phrase in table:
        if abs(ratio - value) < 0.005:
            return phrase
    if ratio < 1:
        return f"{round(ratio * 100)}% of"
    scaled = round(ratio * 100) / 100
    text = f"{scaled:.2f}".rstrip("0").rstrip(".")
    return f"{text} times"


def _every_ratio_pricing_yields() -> list[float]:
    meta = load_pricing().rates_meta()
    ratios: list[float] = []
    for entry in meta.values():
        for key in ("cache_read_ratio", "cache_write_5m_ratio", "cache_write_1h_ratio"):
            if entry[key] is not None:
                ratios.append(entry[key])
        ratios.extend(entry["input_ratio_to"].values())
    assert ratios, "pricing.toml yielded no ratios to check"
    return ratios


def test_report_meta_rates_ratios_come_straight_from_pricing_toml():
    """(a) report.json's meta.rates (Pricing.rates_meta()) never
    re-derives a ratio independently of pricing.toml's own per-model
    rates -- ties this file's fraction() checks to the same rate card
    test_pricing.py holds meta.rates to."""
    pricing = load_pricing()
    meta = pricing.rates_meta()
    for model_id, rates in pricing.models.items():
        entry = meta[model_id]
        if rates.input:
            assert entry["cache_read_ratio"] == pytest.approx(rates.cache_read / rates.input)
            assert entry["cache_write_5m_ratio"] == pytest.approx(rates.cache_write_5m / rates.input)
            assert entry["cache_write_1h_ratio"] == pytest.approx(rates.cache_write_1h / rates.input)


def test_every_pricing_ratio_is_worded_by_fractions_own_table():
    """(b) Every ratio the packaged pricing.toml yields -- every model's
    cache_read_ratio, cache_write_5m_ratio, cache_write_1h_ratio and
    every pairwise input_ratio_to -- gets a non-empty, well-formed
    phrase from fraction()'s own table and rounding rule. A ratio within
    0.005 of a table entry must read exactly that entry's words; every
    other ratio must read the generic "N% of"/"N times" form."""
    table = _fraction_table()
    generic = re.compile(r"^\d+(\.\d+)?% of$|^\d+(\.\d+)? times$")
    for ratio in _every_ratio_pricing_yields():
        words = _fraction(ratio, table)
        assert words, ratio
        table_hit = next((phrase for value, phrase in table if abs(ratio - value) < 0.005), None)
        if table_hit is not None:
            assert words == table_hit, (ratio, words, table_hit)
        else:
            assert generic.match(words), (ratio, words)


# --------------------------------------------------------------------
# (c): costs.js/page-glossary.js never spell out a multiplier
# themselves -- every ratio in their copy goes through fraction().
# --------------------------------------------------------------------


def test_costs_and_glossary_never_hard_code_a_multiplier():
    table = _fraction_table()
    phrases = [phrase for _, phrase in table]
    banned = re.compile(
        "|".join(r"\b" + re.escape(phrase) + r"\b" for phrase in phrases)
        + r"|\b\d+(?:\.\d+)?\s*times\b"
        + r"|\b\d+(?:\.\d+)?\s*%\s*of\b",
        re.IGNORECASE,
    )
    for name in ("costs.js", "page-glossary.js"):
        for text in _string_literals(_static_text(name)):
            assert not banned.search(text), (name, text)


# --------------------------------------------------------------------
# (d): COST_CARDS is internally consistent, every card is priced and
# drawn.
# --------------------------------------------------------------------


def test_cost_card_slugs_are_unique_and_every_term_is_a_real_glossary_entry():
    cards = _cost_cards()
    assert cards, "COST_CARDS has no entries"
    slugs = [card["slug"] for card in cards]
    assert len(slugs) == len(set(slugs)), slugs
    glossary_terms = _glossary_terms()
    for card in cards:
        for term in card.get("terms", []):
            assert term in glossary_terms, (card["slug"], term)


def _object_string_keys(declaration_src: str) -> set[str]:
    return set(re.findall(r'"([a-z][a-z-]*)"\s*:', declaration_src))


def test_every_cost_card_has_a_rule_a_numbers_paragraph_and_a_link():
    """Every links.js COST_CARDS slug has a rule function in costs.js
    (CARD_RULES) and a numbers paragraph and an action link in
    page-glossary.js (CARD_NUMBERS/CARD_LINKS) -- a card added to
    COST_CARDS without wiring it up elsewhere would otherwise draw
    silently blank."""
    slugs = {card["slug"] for card in _cost_cards()}
    costs_js = _static_text("costs.js")
    glossary_js = _static_text("page-glossary.js")
    assert _object_string_keys(_declaration_source(costs_js, "CARD_RULES")) == slugs
    assert _object_string_keys(_declaration_source(glossary_js, "CARD_NUMBERS")) == slugs
    assert _object_string_keys(_declaration_source(glossary_js, "CARD_LINKS")) == slugs


def test_render_cost_cards_draws_every_card_in_order():
    body = _function_source(_static_text("page-glossary.js"), "renderCostCards")
    assert "COST_CARDS.forEach" in body
    assert "renderCostCard(card" in body


# --------------------------------------------------------------------
# (e): both Glossary segments handle their deep-link parameter.
# --------------------------------------------------------------------


def test_how_costs_work_handles_the_card_param():
    app_js = _app_js()
    body = _function_source(app_js, "renderCostCards")
    assert 'onParams("glossary/how-costs-work"' in body
    assert "params.card" in body
    assert "pulseNode(" in body


def test_terms_handles_the_term_param():
    app_js = _app_js()
    body = _function_source(app_js, "renderGlossary")
    assert 'onParams("glossary/terms"' in body
    assert "params.term" in body
    assert "pulseNode(" in body
