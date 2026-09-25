"""The dashboard's design tokens (``service/static/app.css``).

Every colour the dashboard draws text or controls with comes from a
token on ``:root``, declared once for light and once for dark (twice for
dark: under the system media query and under ``data-theme="dark"``).
These tests read those blocks and check what a screenshot can't prove:

- every text/surface pair reaches WCAG AA (4.5:1), and focus rings and
  control outlines reach 3:1 (WCAG 1.4.11), in both themes;
- the two dark blocks say the same thing;
- motion has a reduced-motion alternative;
- ``url()`` only ever names a vendored font or an in-page ``#`` fill;
- there are no side-stripe accents (a thick left/right border as a
  colour cue), and focus has one visible, global ring.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_CSS = REPO_ROOT / "src" / "claude_token_lens" / "service" / "static" / "app.css"

_HEX_TOKEN = re.compile(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;")
_ANY_TOKEN = re.compile(r"--([\w-]+):\s*([^;]+?)\s*;")


def _css() -> str:
    return APP_CSS.read_text(encoding="utf-8")


def _body(pattern: str) -> str:
    match = re.search(pattern + r"\s*\{([^{}]*)\}", _css(), re.MULTILINE)
    assert match, f"app.css has no block matching {pattern!r}"
    return match.group(1)


def _light() -> dict[str, str]:
    return dict(_HEX_TOKEN.findall(_body(r"^:root")))


def _dark_system_block() -> str:
    media = re.search(
        r'@media \(prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-theme="light"\]\)\s*\{([^{}]*)\}\s*\}', _css()
    )
    assert media, "app.css has no system dark block guarded by :root:not([data-theme=light])"
    return media.group(1)


def _dark_system() -> dict[str, str]:
    return dict(_HEX_TOKEN.findall(_dark_system_block()))


def _dark_picked() -> dict[str, str]:
    return dict(_HEX_TOKEN.findall(_body(r':root\[data-theme="dark"\]')))


def _themes() -> dict[str, dict[str, str]]:
    light = _light()
    dark = dict(light)
    dark.update(_dark_picked())
    return {"light": light, "dark": dark}


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


_SURFACES = ("surface-canvas", "surface-panel", "surface-raised", "surface-sunken", "surface-sidebar", "surface-hover")
_STATUS = ("good", "warn", "serious", "critical")


def _pairs() -> list[tuple[str, str, float]]:
    pairs = []
    for ink in ("ink-1", "ink-2", "ink-3", "accent-ink"):
        for surface in (*_SURFACES, "accent-soft"):
            pairs.append((ink, surface, 4.5))
    pairs.append(("on-accent", "accent", 4.5))
    for status in _STATUS:
        for surface in ("surface-panel", "surface-canvas", f"{status}-soft"):
            pairs.append((status, surface, 4.5))
        pairs.append(("ink-1", f"{status}-soft", 4.5))
    for surface in (*_SURFACES, "accent-soft"):
        pairs.append(("focus", surface, 3.0))
    for surface in ("surface-panel", "surface-canvas", "surface-raised"):
        pairs.append(("border-control", surface, 3.0))
    return pairs


def test_both_dark_blocks_declare_the_same_values() -> None:
    """Every declaration, not only the colours: a shadow or backdrop
    that drifted in one block would differ between "follow the system"
    and "dark" picked by hand."""
    system = dict(_ANY_TOKEN.findall(_dark_system_block()))
    picked = dict(_ANY_TOKEN.findall(_body(r':root\[data-theme="dark"\]')))
    assert system == picked
    assert _dark_system() == _dark_picked()


def test_every_light_colour_token_has_a_dark_value() -> None:
    light, dark = _light(), _dark_picked()
    missing = sorted(name for name in light if name not in dark and not name.startswith(("font", "text")))
    assert not missing, f"no dark value for: {missing}"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("fg,bg,minimum", _pairs())
def test_declared_pairs_meet_wcag_contrast(theme: str, fg: str, bg: str, minimum: float) -> None:
    tokens = _themes()[theme]
    ratio = _contrast(tokens[fg], tokens[bg])
    assert ratio >= minimum, f"{theme}: --{fg} on --{bg} is {ratio:.2f}:1, needs {minimum}:1"


def test_native_controls_follow_the_theme() -> None:
    assert "color-scheme: light dark" in _body(r"^:root")
    assert "color-scheme: light" in _body(r':root\[data-theme="light"\]')
    css = _css()
    assert re.search(r':root\[data-theme="dark"\]\s*\{\s*color-scheme: dark;\s*\}', css)


def test_reduced_motion_stills_every_animation_and_transition() -> None:
    match = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", _css(), re.DOTALL)
    assert match, "app.css has no prefers-reduced-motion: reduce block"
    block = match.group(1)
    for rule in ("animation-duration", "transition-duration", "scroll-behavior: auto"):
        assert rule in block, rule


def test_smooth_scrolling_only_when_motion_is_welcome() -> None:
    css = _css()
    welcome = "".join(re.findall(r"@media \(prefers-reduced-motion: no-preference\)\s*\{(.*?)\n\}", css, re.DOTALL))
    assert css.count("scroll-behavior: smooth") == welcome.count("scroll-behavior: smooth")


def test_url_only_names_a_vendored_font_or_an_in_page_fill() -> None:
    code = re.sub(r"/\*.*?\*/", "", _css(), flags=re.DOTALL)
    targets = re.findall(r"url\(\s*([^)]*)\)", code)
    assert targets, "app.css names no font at all"
    for target in targets:
        target = target.strip("'\" ")
        assert target.startswith("/static/fonts/") or target.startswith("#"), target


def test_fonts_swap_in_and_have_metric_matched_fallbacks() -> None:
    css = _css()
    faces = re.findall(r"@font-face\s*\{([^}]*)\}", css)
    web = [face for face in faces if "url(" in face]
    assert len(web) == 3
    for face in web:
        assert "font-display: swap" in face
    fallbacks = [face for face in faces if "local(" in face]
    assert len(fallbacks) == 2
    for face in fallbacks:
        for descriptor in ("size-adjust", "ascent-override", "descent-override"):
            assert descriptor in face


# A CSS length in px, rem or em, or a bare 0.
_LENGTH = r"-?\d*\.?\d+(?:px|rem|em)|(?<![\w.-])0(?![\w.%])"


def _px(length: str) -> float:
    number = float(re.match(r"-?\d*\.?\d+", length).group(0))
    return number * 16 if length.endswith(("rem", "em")) else number


def test_no_side_stripe_accents() -> None:
    """A thick coloured left/right border on a card, callout or row is
    the one accent this design never uses: status gets a chip with an
    icon and a label instead. Caught in every spelling: the physical and
    logical side properties, a border-width with a thicker side, and an
    inset box-shadow offset sideways."""
    css = re.sub(r"/\*.*?\*/", "", _css(), flags=re.DOTALL)
    side = r"border-(?:left|right|inline-start|inline-end|inline)(?:-width)?\s*:\s*([^;]+);"
    for match in re.finditer(side, css):
        widths = [_px(w) for w in re.findall(_LENGTH, match.group(1))]
        assert all(w <= 1 for w in widths), f"side stripe: {match.group(0)}"
    for match in re.finditer(r"border-width\s*:\s*([^;]+);", css):
        widths = [_px(w) for w in re.findall(_LENGTH, match.group(1))]
        # Four values run top, right, bottom, left: a side thicker than
        # 1px is a stripe unless every side matches.
        if len(widths) == 4 and max(widths[1], widths[3]) > 1:
            assert len(set(widths)) == 1, f"side stripe: {match.group(0)}"
    for match in re.finditer(r"box-shadow\s*:\s*([^;]+);", css):
        # Split the shadow list on commas outside colour functions.
        for shadow in re.split(r",(?![^()]*\))", match.group(1)):
            if "inset" not in shadow:
                continue
            offsets = [_px(w) for w in re.findall(_LENGTH, shadow)[:2]]
            if len(offsets) == 2:
                x, y = offsets
                assert abs(x) <= 1 or y != 0, f"side stripe as an inset shadow: {shadow.strip()}"


def test_one_visible_focus_ring_everywhere() -> None:
    """Every focusable element gets the same ring (WCAG 2.4.7): the
    global :focus-visible rule, drawn in the focus token."""
    css = _css()
    match = re.search(r"(?m)^:focus-visible\s*\{([^}]*)\}", css)
    assert match, "app.css has no global :focus-visible rule"
    assert "outline: 2px solid var(--focus)" in match.group(1)
    for rule in re.finditer(r"([^{}]*):focus-visible[^{]*\{([^}]*)\}", css):
        assert "outline: none" not in rule.group(2), rule.group(0)
        assert "outline: 0" not in rule.group(2), rule.group(0)


def test_every_token_used_is_declared() -> None:
    """A misspelt or retired token fails silently (the property just
    drops), so every var(--name) in app.css and the modules must be
    declared on :root."""
    css = _css()
    declared = set(re.findall(r"(--[\w-]+)\s*:", css))
    static = APP_CSS.parent
    sources = [css] + [p.read_text(encoding="utf-8") for p in sorted(static.glob("*.js"))]
    used = {name for text in sources for name in re.findall(r"var\(\s*(--[\w-]+)", text)}
    assert used, "found no var(--...) uses at all"
    assert not sorted(used - declared), f"used but never declared: {sorted(used - declared)}"


def test_the_hidden_attribute_beats_a_component_display() -> None:
    """The browser's own ``[hidden] { display: none }`` loses to any
    author rule that sets display, so a hidden button styled as the
    plain button would show again (Profiles' "Replace the saved copy"
    did). app.css restates the rule with !important."""
    assert re.search(r"(?m)^\[hidden\]\s*\{\s*display: none !important;\s*\}", _css())
