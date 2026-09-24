"""Read the metrics-capture tags Claude writes (see ``capture_catalogue``).

Two places carry them:

- **The end of a reply.** ``[tl: task=bugfix brief=partial ...]`` ends the
  final reply to each of your messages in the main session, and a
  subagent's final report ends ``[result: done fit=right rules=used]``.
  Only the reply's last :data:`TAIL_SCAN_CHARS` characters are read, and
  the tags must be the very last thing in it (trailing markdown aside), so
  a tag quoted further up a reply -- when Claude explains the format, say
  -- is never counted.
- **The start of a brief.** ``[retry: brief]`` and ``[spawn: isolate]``
  open the brief handed to an agent, in either order.

Every value is checked against the closed vocabularies in
``capture_catalogue``; unknown keys and words are dropped, so nothing
Claude wrote in its own words is kept. The one name that can survive is a
skill name in ``skill=would-help:<name>``, and only when it matches a
skill this transcript listed or used.
"""

from __future__ import annotations

import re
from collections.abc import Collection

from .capture_catalogue import (
    LIST_KEYS,
    RESULT_WORDS,
    RETRY_REASONS,
    SKILL_NAME_PATTERN,
    SPAWN_REASONS,
    TAG_VOCAB,
)
from .model import CaptureTag

#: How much of a reply's end is searched for tags. A full Deep ``[tl:]`` tag
#: is about 220 characters; a ``[result:]`` tag can sit next to it.
TAIL_SCAN_CHARS = 480

#: One or more tags ending the text, each on one line, with only
#: whitespace or markdown (backticks, emphasis, a full stop) between and
#: after them.
_TRAILING_TAGS_RE = re.compile(
    r"(?:\[(?:tl|result):[^\[\]\n]{0,300}\][`*_.\s]*){1,3}$",
    re.IGNORECASE,
)
_ONE_TAG_RE = re.compile(r"\[(tl|result):([^\[\]\n]{0,300})\]", re.IGNORECASE)
_SKILL_NAME_RE = re.compile(rf"^{SKILL_NAME_PATTERN}$")

#: Brief-start markers: up to two ``[retry: x]``/``[spawn: x]`` tags before
#: anything else, optionally in backticks.
_BRIEF_PREFIX_RE = re.compile(r"^\s*(?:`?\[(?:retry|spawn):\s*[A-Za-z-]+\s*\]`?\s*){1,2}", re.IGNORECASE)
_BRIEF_MARKER_RE = re.compile(r"\[(retry|spawn):\s*([A-Za-z-]+)\s*\]", re.IGNORECASE)

#: A capture note's marker: ``tl-cap v1`` then the metric codes, comma
#: separated (see ``capture_catalogue.NOTE_MARKER``).
_NOTE_RE = re.compile(r"tl-cap v(\d{1,3})(?: ([a-z_,]{0,400}))?")
_CODE_RE = re.compile(r"^[a-z_]{1,24}$")

_VOCAB_SETS = {key: frozenset(words) for key, words in TAG_VOCAB.items()}


def _apply_word(values: dict, key: str, value: str, skill_names: Collection[str]) -> None:
    """Fold one ``key=value`` word into ``values`` if both are known."""
    vocab = _VOCAB_SETS.get(key)
    if vocab is None:
        return
    if key in LIST_KEYS:
        words = tuple(dict.fromkeys(w for w in value.lower().split(",") if w in vocab))
        if words:
            values[key] = words
        return
    if key == "skill" and ":" in value:
        word, _, name = value.partition(":")
        word = word.lower()
        if word != "would-help":
            return
        values["skill"] = word
        values["skill_name"] = name if _SKILL_NAME_RE.match(name) and name in skill_names else None
        return
    word = value.lower()
    if word in vocab:
        values[key] = word
        if key == "skill":
            values["skill_name"] = None


def parse_reply_tags(text: str, skill_names: Collection[str] = ()) -> tuple[CaptureTag | None, str | None]:
    """The capture tag and the ``[result: ...]`` word ending ``text``.

    Returns ``(tag, result_word)``: ``tag`` is ``None`` when the reply ends
    in no tag at all, and ``result_word`` is ``None`` without a valid
    ``[result: ...]``. ``skill_names`` are the skills this transcript has
    listed or used so far; a ``would-help:<name>`` naming any other skill
    keeps the ``would-help`` and drops the name.
    """
    if not text or "[" not in text:
        return None, None
    tail = text[-TAIL_SCAN_CHARS:]
    match = _TRAILING_TAGS_RE.search(tail)
    if match is None:
        return None, None
    values: dict = {}
    result_word: str | None = None
    has_tl = False
    for kind, body in _ONE_TAG_RE.findall(match.group(0)):
        words = body.split()
        if kind.lower() == "result":
            if not words or words[0].lower() not in RESULT_WORDS:
                continue
            result_word = words[0].lower()
            words = words[1:]
        else:
            has_tl = True
        for word in words:
            key, sep, value = word.partition("=")
            if sep and value:
                _apply_word(values, key.lower(), value.strip("`*_.,;"), skill_names)
    if result_word is None and not has_tl:
        return None, None
    tag = CaptureTag(has_tl=has_tl, chars=len(match.group(0).rstrip()), **values)
    return tag, result_word


def parse_brief_markers(text: str) -> tuple[str | None, str | None]:
    """``(retry_reason, spawn_reason)`` from the start of a brief; either
    is ``None`` when absent or not a known word."""
    if not text or "[" not in text[:40]:
        return None, None
    prefix = _BRIEF_PREFIX_RE.match(text)
    if prefix is None:
        return None, None
    retry: str | None = None
    spawn: str | None = None
    for kind, word in _BRIEF_MARKER_RE.findall(prefix.group(0)):
        kind, word = kind.lower(), word.lower()
        if kind == "retry" and retry is None and word in RETRY_REASONS:
            retry = word
        elif kind == "spawn" and spawn is None and word in SPAWN_REASONS:
            spawn = word
    return retry, spawn


def parse_note_codes(text: str) -> tuple[int | None, tuple[str, ...]]:
    """The format version and metric codes of a capture note's
    ``tl-cap v1 task,brief`` marker; ``(None, ())`` without one."""
    match = _NOTE_RE.search(text)
    if match is None:
        return None, ()
    codes = tuple(c for c in (match.group(2) or "").split(",") if _CODE_RE.match(c))
    return int(match.group(1)), codes
