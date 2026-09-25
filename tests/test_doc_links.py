"""Links between the Markdown docs resolve: every relative link in the
README, ``docs/``, ``SECURITY.md`` and ``CHANGELOG.md`` names a file that
exists, and every ``#anchor`` names a heading (or an ``<a id>``) in its
target file, using GitHub's own heading-to-anchor rules.

The README once carried eleven numbered reference sections that the docs
linked into by anchor; moving them out would otherwise break those links
silently, since GitHub renders a dead anchor as a plain jump to the top.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_FILES = sorted(
    [REPO_ROOT / "README.md", REPO_ROOT / "SECURITY.md", REPO_ROOT / "CHANGELOG.md", *(REPO_ROOT / "docs").glob("*.md")]
)

FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
HTML_ANCHOR = re.compile(r"<a\s+(?:id|name)=\"([^\"]+)\"")
#: ``[text](target)`` or ``![alt](target)``, target without spaces; an
#: optional ``"title"`` after it is ignored.
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_LINK = re.compile(r"(?:href|src|srcset)=\"([^\"]+)\"")
CODE_SPAN = re.compile(r"`[^`]*`")


def _prose_lines(text: str) -> list[str]:
    """The file's lines outside fenced code blocks."""
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            out.append(line)
    return out


def github_slug(heading: str) -> str:
    """GitHub's anchor for a heading: its rendered text, lower-cased,
    with punctuation other than ``-`` and ``_`` removed and each space
    turned into a hyphen (so "Spend › Usage" becomes ``spend--usage``)."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("`", "").replace("*", "")
    text = re.sub(r"[^\w\- ]", "", text.lower())
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    """Every anchor a file offers, with GitHub's ``-1``, ``-2`` suffixes
    for repeated headings."""
    seen: dict[str, int] = {}
    found: set[str] = set()
    for line in _prose_lines(path.read_text(encoding="utf-8")):
        found.update(HTML_ANCHOR.findall(line))
        match = HEADING.match(line)
        if not match:
            continue
        slug = github_slug(match.group(2))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def relative_links(path: Path) -> list[str]:
    """Relative link targets in the file's prose (not in code)."""
    targets: list[str] = []
    for line in _prose_lines(path.read_text(encoding="utf-8")):
        line = CODE_SPAN.sub("", line)
        targets += LINK.findall(line)
        targets += HTML_LINK.findall(line)
    return [t for t in targets if not re.match(r"[a-z][a-z0-9+.-]*:", t, re.I)]


def test_github_slug_matches_githubs_rules() -> None:
    assert github_slug("An old dashboard won't go away") == "an-old-dashboard-wont-go-away"
    assert github_slug("An update doesn't take (more than one Python)") == "an-update-doesnt-take-more-than-one-python"
    assert github_slug("`baseline_comparison` (`report.py`)") == "baseline_comparison-reportpy"
    assert github_slug("`--aggregate` (team documents)") == "--aggregate-team-documents"
    assert github_slug("Glossary › Terms") == "glossary--terms"
    assert github_slug("What it reads, and what it can't") == "what-it-reads-and-what-it-cant"


@pytest.mark.parametrize("path", DOC_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)).replace("\\", "/"))
def test_relative_links_resolve(path: Path) -> None:
    broken: list[str] = []
    for target in relative_links(path):
        file_part, _, anchor = target.partition("#")
        dest = (path.parent / file_part).resolve() if file_part else path
        if not dest.exists():
            broken.append(f"{target} (no such file)")
            continue
        if anchor and dest.suffix == ".md" and anchor not in anchors(dest):
            broken.append(f"{target} (no such heading)")
    assert broken == [], broken
