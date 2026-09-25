"""The dashboard's page registry, and the ``{{page:...}}`` tokens that
link to it from server help, notes and action text.

The sidebar's shape lives twice: as data here, and as ``PAGES`` in
``service/static/links.js`` (``tests/test_pages.py`` keeps the two in
sync). A page id is a lower-case word, a segment id may join words with
a hyphen (``how-costs-work``), matching the router's own id shape
(``docs/ui.md``).

A server string names a page or segment with a token: ``{{page:cache}}``
or ``{{page:cache/rebuilds}}``. The dashboard's own ``links.js`` renders
each token as a link. Every other reader of that text -- the CLI, the
Markdown and HTML reports, ``docs/capture.md`` -- can't follow a link, so
each of those renderers calls :func:`plain` first, which turns every
token into the same plain label the dashboard shows ("Spend ›
Usage"), joined with the same separator ``links.js``'s ``viewLabel``
uses. JSON output (``render/json_out.py``, and every ``/api/*`` route)
keeps tokens as-is: the dashboard reads them from there and turns them
into links itself, and ``docs/api.md``'s byte-equivalence contract ties
``/api/report.json`` to the CLI's ``report --json`` output, which calls
the very same renderer -- so keeping the token in both is what makes
them byte-equivalent, not a bug.

A token must never reach a recommendation's ``why`` or ``title``, or a
``fixes[].prompt``/``fixes[].command``: ``fixes.py``'s ``prompt_for`` and
``build_fix`` build those from ``why``/``title`` verbatim into a prompt
or a standalone command, and neither should ever carry dashboard markup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The separator ``links.js``'s ``viewLabel`` uses between a page and a
#: segment label ("Spend › Usage"). The rest of this package's
#: server text is plain ASCII, but that's incidental -- several modules
#: already use non-ASCII punctuation (en/em dashes, ellipses) in text a
#: person reads, so there's no encoding reason to pick a different,
#: ASCII-only separator here; using the same one keeps a page's name
#: identical on the dashboard and in plain text, as ``docs/writing-help.md``
#: requires.
SEPARATOR = " › "


@dataclass(frozen=True, slots=True)
class Segment:
    id: str
    label: str


@dataclass(frozen=True, slots=True)
class Page:
    id: str
    label: str
    segments: tuple[Segment, ...] = field(default_factory=tuple)


#: The sidebar, in order -- mirrors ``PAGES`` in
#: ``service/static/links.js`` (``tests/test_pages.py`` checks both
#: name the same pages and segments, with the same labels).
PAGES: tuple[Page, ...] = (
    Page("overview", "Overview"),
    Page(
        "actions",
        "Actions",
        (
            Segment("recommendations", "Recommendations"),
            Segment("checks", "Checks"),
        ),
    ),
    Page(
        "spend",
        "Spend",
        (
            Segment("usage", "Usage"),
            Segment("savings", "Savings"),
            Segment("sessions", "Sessions"),
        ),
    ),
    Page(
        "cache",
        "Cache",
        (
            Segment("rebuilds", "Rebuilds"),
            Segment("lifetime", "Lifetime (TTL)"),
        ),
    ),
    Page(
        "agents",
        "Agents & context",
        (
            Segment("subagents", "Subagents"),
            Segment("quality", "Quality"),
            Segment("context", "Context"),
            Segment("hooks", "Hooks"),
        ),
    ),
    Page("habits", "Work habits"),
    Page(
        "setup",
        "Setup",
        (
            Segment("settings", "Settings"),
            Segment("profiles", "Profiles"),
            Segment("capture", "Capture"),
        ),
    ),
    Page("data", "Data quality"),
    Page(
        "glossary",
        "Glossary",
        (
            Segment("terms", "Terms"),
            Segment("how-costs-work", "How costs work"),
        ),
    ),
)

_PAGES_BY_ID: dict[str, Page] = {page.id: page for page in PAGES}


def find_page(page_id: str) -> Page | None:
    """The :class:`Page` named ``page_id``, or ``None``."""
    return _PAGES_BY_ID.get(page_id)


def find_segment(page: Page, segment_id: str) -> Segment | None:
    """The :class:`Segment` of ``page`` named ``segment_id``, or ``None``."""
    for segment in page.segments:
        if segment.id == segment_id:
            return segment
    return None


def label_for(token: str) -> str:
    """The plain label a ``page`` or ``page/segment`` id names, e.g.
    ``"cache/rebuilds"`` -> ``"Cache › Rebuilds"``.

    Raises :class:`ValueError` for an id this registry doesn't know, so a
    typo in a ``{{page:...}}`` token fails a test instead of silently
    reading back as its own raw text.
    """
    page_id, _, segment_id = token.partition("/")
    page = find_page(page_id)
    if page is None:
        raise ValueError(f"unknown page {page_id!r} (in {token!r})")
    if not segment_id:
        return page.label
    segment = find_segment(page, segment_id)
    if segment is None:
        raise ValueError(f"unknown segment {segment_id!r} of page {page_id!r} (in {token!r})")
    return page.label + SEPARATOR + segment.label


#: ``{{page:<page>}}`` or ``{{page:<page>/<segment>}}``.
_TOKEN_RE = re.compile(r"\{\{page:([a-z]+(?:-[a-z]+)*(?:/[a-z]+(?:-[a-z]+)*)?)\}\}")


def tokens_in(text: str) -> list[str]:
    """Every ``page`` or ``page/segment`` id a ``{{page:...}}`` token in
    ``text`` names, in order, duplicates included. Empty when ``text``
    carries none.
    """
    return _TOKEN_RE.findall(text or "")


def plain(text: str) -> str:
    """``text`` with every ``{{page:...}}`` token replaced by its plain
    label (``"{{page:cache/rebuilds}}"`` -> ``"Cache › Rebuilds"``).

    Every output path that isn't the dashboard itself -- the CLI, the
    Markdown and HTML reports, ``docs/capture.md`` -- calls this so a
    token never reaches a person reading plain text. JSON output does
    not: the dashboard's own ``links.js`` renders the token as a link.
    """
    if not text or "{{page:" not in text:
        return text
    return _TOKEN_RE.sub(lambda m: label_for(m.group(1)), text)


__all__ = [
    "Page",
    "Segment",
    "PAGES",
    "SEPARATOR",
    "find_page",
    "find_segment",
    "label_for",
    "tokens_in",
    "plain",
]
