"""A narrow, no-YAML-library parser/patcher for a Claude Code agent
markdown file's ``---``-delimited frontmatter block (V3-apply).

``apply.py`` needs to change a handful of allowlisted frontmatter keys
(``model``, ``effort``, ``maxTurns``, ``experimental.cacheTtl``, ...) on
a real ``<agent>.md`` file without disturbing anything else a human
wrote in it -- unrelated keys, their comments, and their ordering must
come back byte-for-byte identical. ``tomllib``/PyYAML are of no help
here (the file is Markdown with a YAML-*ish* header, and even a real
YAML round-trip library would reformat everything, not just the lines
that changed), so this module hand-rolls the narrow subset of that
frontmatter shape Claude Code agents actually use -- the same subset
``hooks/snapshot-config.py``'s own ``parse_frontmatter`` reads (a
``---``-fenced block of scalar ``key: value`` lines, with one level of
nested mapping for ``experimental: / cacheTtl: ...``) -- extended here
with everything a *patcher* additionally needs to preserve: quoted
strings, ints, bools, flow lists (``[a, b]``), block lists (``- a``),
and comments (both whole-line and trailing).

This module is deliberately independent of ``hooks/snapshot-config.py``
(that script stays standalone stdlib with no import into the package,
and its own ``parse_frontmatter`` is a one-way, redacting read -- it
never needs to reproduce a byte-identical file) and of ``schema.py``
(this module knows nothing about ``AGENT_ALLOWLIST``; the caller in
``apply.py`` is responsible for only ever passing allowlisted keys to
:func:`patch_frontmatter`). Duplicating the small scalar-parsing rules
between the two is the same "each module keeps its own copy of a small
lookup" convention this project already follows elsewhere (see e.g.
``config.py``'s and ``cli.py``'s own ``_resolve_config_dir``).

Design, refuse-don't-guess: every function here either returns a
faithful answer or raises :class:`FrontmatterError` -- it never falls
back to a best-effort guess for a shape it does not fully understand
(tab indentation, more than one level of nested mapping, a block mixing
list items and mapping children under the same key, inconsistent
indentation among a block's children, a duplicate key, or a file with
no ``---``-fenced block at all).

Round-trip guarantee: :func:`patch_frontmatter` only ever rewrites the
exact line(s) that hold a changed key's value (or, for a full list
replacement, the key's own item-line range), reusing the original
line's indentation, inter-token whitespace and trailing comment
verbatim. Every other line -- including every comment, every blank
line, and every key not named in ``changes`` -- is copied through
character-for-character. Patching the same ``changes`` dict a second
time against the already-patched text is a no-op (the same value
serialises to the same text, so the "changed" line comes out
byte-identical the second time).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["FrontmatterError", "parse_frontmatter", "patch_frontmatter"]


class FrontmatterError(ValueError):
    """Raised by :func:`parse_frontmatter`/:func:`patch_frontmatter` when
    the frontmatter block is missing, unterminated, or shaped in a way
    this narrow parser does not understand -- refusing rather than
    guessing (see module docstring)."""


#: A top-level (indent 0) or nested-child (indent > 0) ``key: rest`` line.
#: Keys are restricted to the plain-identifier-plus-dot shape every real
#: Claude Code frontmatter key (and the dotted ``"experimental.cacheTtl"``
#: form) actually uses -- a quoted or otherwise unusual key is out of
#: scope (refused via "no match" the same as any other unparsable line).
_TOP_LINE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*):(?P<rest>.*)$")

#: A block-list item line: ``- value`` (optionally indented), the ``-``
#: and value separated by at most one space (Claude Code frontmatter, and
#: every YAML emitter this project has observed, writes exactly one).
_LIST_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)-\s?(?P<value>.*)$")

_RESERVED_BARE = {"true", "false", "null", "~", ""}
_SPECIAL_CHARS_RE = re.compile(r"""[:#\[\]{},&*!|>'"%@`]""")


# -- document splitting ------------------------------------------------


def _split_document(text: str) -> tuple[list[str], list[str], int, str] | None:
    """Split ``text`` into ``(lines, fm_lines, close_idx, line_ending)``:
    ``lines`` is every line of ``text`` (``str.splitlines(keepends=True)``,
    so each entry carries its own original ``\\n``/``\\r\\n``/none),
    ``fm_lines`` is the mutable slice strictly between the opening and
    closing ``---`` fences, ``close_idx`` is the closing fence's index
    into ``lines``, and ``line_ending`` is the line ending to use for any
    brand-new line this module writes (the first ending found anywhere in
    ``text``, or ``"\\n"`` if the file has none at all -- a single-line
    file). Returns ``None`` (never raises) if there is no opening fence
    at all, so callers can give ``parse_frontmatter``'s documented "no
    frontmatter block" behaviour and :func:`patch_frontmatter`'s "refuse"
    behaviour their own separate treatment of that same case.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n").strip() != "---":
        return None
    close_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].rstrip("\r\n").strip() == "---":
            close_idx = idx
            break
    if close_idx is None:
        raise FrontmatterError("frontmatter opening '---' fence is never closed")

    line_ending = "\n"
    for line in lines:
        if line.endswith("\r\n"):
            line_ending = "\r\n"
            break
        if line.endswith("\n"):
            line_ending = "\n"
            break

    return lines, list(lines[1:close_idx]), close_idx, line_ending


def _find_comment_split(s: str) -> tuple[str, str]:
    """Split ``s`` at the first ``#`` that is not inside a quoted string
    into ``(value_area, comment_area)`` -- ``comment_area`` starts at and
    includes the ``#`` and everything after it, verbatim; ``""`` if there
    is no comment. Never raises: an unterminated quote just means no
    ``#`` inside it is ever seen as "inside a quote" past end of string,
    which is harmless here (the value itself is re-parsed/validated by
    :func:`_parse_scalar_value` where that matters).
    """
    in_quote: str | None = None
    for idx, ch in enumerate(s):
        if in_quote:
            if ch == in_quote:
                in_quote = None
            continue
        if ch in "\"'":
            in_quote = ch
            continue
        if ch == "#":
            return s[:idx], s[idx:]
    return s, ""


# -- reading scalar values -----------------------------------------------


def _parse_scalar_value(raw: str):
    """Parse one already comment-stripped YAML-ish scalar: a quoted
    string (with ``\\\\``/``\\"`` unescaped for a double-quoted string), a
    ``[a, b]`` inline list, a bool, ``null``/``~``, an int, a float, or
    else the bare string -- mirrors ``hooks/snapshot-config.py``'s
    ``_parse_scalar`` (duplicated rather than imported, see module
    docstring), extended with quote-unescaping since this module also
    has to *write* a quoted value back out.
    """
    raw = raw.strip()
    if raw == "":
        return None
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar_value(item.strip()) for item in inner.split(",")]
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        inner = raw[1:-1]
        if raw[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


# -- writing scalar values ------------------------------------------------


def _needs_quotes(s: str) -> bool:
    if s == "" or s != s.strip():
        return True
    if s.lower() in _RESERVED_BARE:
        return True
    if _SPECIAL_CHARS_RE.search(s):
        return True
    try:
        float(s)
        return True
    except ValueError:
        return False


def _quote(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _scalar_repr(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _quote(value) if _needs_quotes(value) else value
    raise FrontmatterError(f"cannot serialise value of type {type(value).__name__}: {value!r}")


def _flow_list_repr(items) -> str:
    return "[" + ", ".join(_scalar_repr(item) for item in items) + "]"


# -- block model -----------------------------------------------------------


@dataclass
class _ScalarBlock:
    line: int  # index into fm_lines


@dataclass
class _ListBlock:
    key_line: int
    item_lines: list[int]
    end: int  # exclusive index of the first line after this block


@dataclass
class _MapBlock:
    key_line: int
    children: dict[str, int] = field(default_factory=dict)
    end: int = 0  # exclusive index of the first line after this block


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _scan(fm_lines: list[str]) -> dict[str, object]:
    """Every top-level key in ``fm_lines`` (the frontmatter's content,
    excluding the ``---`` fences), mapped to a :class:`_ScalarBlock`,
    :class:`_ListBlock` or :class:`_MapBlock`. Raises
    :class:`FrontmatterError` on anything this parser does not
    understand -- see the module docstring's "refuse-don't-guess" note.
    """
    blocks: dict[str, object] = {}
    n = len(fm_lines)
    i = 0
    while i < n:
        raw = fm_lines[i].rstrip("\r\n")
        if raw.strip() == "" or raw.lstrip().startswith("#"):
            i += 1
            continue
        if raw[:1] in (" ", "\t"):
            raise FrontmatterError(f"line {i + 1}: unexpected indentation outside any block: {raw!r}")
        m = _TOP_LINE_RE.match(raw)
        if not m:
            raise FrontmatterError(f"line {i + 1}: cannot parse frontmatter line: {raw!r}")
        key = m.group("key")
        if key in blocks:
            raise FrontmatterError(f"line {i + 1}: duplicate top-level key {key!r}")
        value_area, _comment = _find_comment_split(m.group("rest"))
        if value_area.strip() != "":
            blocks[key] = _ScalarBlock(line=i)
            i += 1
            continue

        # No inline value -- look ahead for indented children. A blank
        # line always ends the lookahead (see module docstring's
        # documented convention): a nested block never contains one.
        j = i + 1
        child_indices: list[int] = []
        while j < n:
            child_raw = fm_lines[j].rstrip("\r\n")
            if child_raw.strip() == "" or child_raw[:1] not in (" ", "\t"):
                break
            child_indices.append(j)
            j += 1

        if not child_indices:
            blocks[key] = _ScalarBlock(line=i)  # an explicitly null/empty value
            i = j
            continue

        real_children = [idx for idx in child_indices if not fm_lines[idx].rstrip("\r\n").lstrip().startswith("#")]
        if not real_children:
            raise FrontmatterError(
                f"line {i + 1}: key {key!r} has only comment lines under it -- "
                "cannot tell whether it is a list or a mapping"
            )
        for idx in real_children:
            if "\t" in _leading_ws(fm_lines[idx]):
                raise FrontmatterError(f"line {idx + 1}: tab indentation is not supported")
        indents = {len(_leading_ws(fm_lines[idx])) for idx in real_children}
        if len(indents) != 1:
            raise FrontmatterError(f"line {i + 1}: inconsistent indentation under key {key!r}")

        first_line = fm_lines[real_children[0]].rstrip("\r\n")
        is_list = _LIST_ITEM_RE.match(first_line) is not None

        if is_list:
            item_lines: list[int] = []
            for idx in real_children:
                line = fm_lines[idx].rstrip("\r\n")
                if not _LIST_ITEM_RE.match(line):
                    raise FrontmatterError(f"line {idx + 1}: mixed list and mapping under key {key!r}")
                item_lines.append(idx)
            blocks[key] = _ListBlock(key_line=i, item_lines=item_lines, end=j)
        else:
            children: dict[str, int] = {}
            for idx in real_children:
                line = fm_lines[idx].rstrip("\r\n")
                cm = _TOP_LINE_RE.match(line)
                if cm is None or cm.group("indent") == "":
                    raise FrontmatterError(f"line {idx + 1}: mixed list and mapping under key {key!r}")
                child_key = cm.group("key")
                child_value_area, _c = _find_comment_split(cm.group("rest"))
                if child_value_area.strip() == "":
                    raise FrontmatterError(
                        f"line {idx + 1}: nested mapping deeper than one level under {key!r}.{child_key}"
                    )
                if child_key in children:
                    raise FrontmatterError(f"line {idx + 1}: duplicate key {child_key!r} under {key!r}")
                children[child_key] = idx
            blocks[key] = _MapBlock(key_line=i, children=children, end=j)
        i = j
    return blocks


# -- public: parse ----------------------------------------------------------


def parse_frontmatter(text: str) -> dict:
    """Parse the ``---``-delimited frontmatter block at the top of
    ``text`` into a flat dict: top-level scalars/flow-lists/block-lists
    keep their bare key, and a one-level nested mapping's children are
    flattened to ``"parent.child"`` (matching
    ``hooks/snapshot-config.py``'s own flattening convention for
    ``experimental.cacheTtl``). Returns ``{}`` if ``text`` has no
    ``---``-fenced block at all. Raises :class:`FrontmatterError` for a
    block this parser cannot understand (an unterminated fence, or any
    of the shapes :func:`_scan` refuses).
    """
    doc = _split_document(text)
    if doc is None:
        return {}
    _lines, fm_lines, _close_idx, _ending = doc
    blocks = _scan(fm_lines)

    result: dict = {}
    for key, block in blocks.items():
        if isinstance(block, _ScalarBlock):
            line = fm_lines[block.line].rstrip("\r\n")
            m = _TOP_LINE_RE.match(line)
            value_area, _c = _find_comment_split(m.group("rest"))
            result[key] = _parse_scalar_value(value_area)
        elif isinstance(block, _ListBlock):
            items = []
            for idx in block.item_lines:
                line = fm_lines[idx].rstrip("\r\n")
                lm = _LIST_ITEM_RE.match(line)
                value_area, _c = _find_comment_split(lm.group("value"))
                items.append(_parse_scalar_value(value_area))
            result[key] = items
        elif isinstance(block, _MapBlock):
            for child_key, idx in block.children.items():
                line = fm_lines[idx].rstrip("\r\n")
                cm = _TOP_LINE_RE.match(line)
                value_area, _c = _find_comment_split(cm.group("rest"))
                result[f"{key}.{child_key}"] = _parse_scalar_value(value_area)
    return result


# -- public: patch ----------------------------------------------------------


def _shift_blocks_after(blocks: dict[str, object], from_idx: int, delta: int) -> None:
    """After inserting/removing ``delta`` lines at ``from_idx`` in the
    working ``fm_lines`` list, adjust every recorded line index (in every
    block, including the one just edited) so later lookups still point
    at the right line. A no-op for ``delta == 0``.
    """
    if delta == 0:
        return
    for block in blocks.values():
        if isinstance(block, _ScalarBlock):
            if block.line >= from_idx:
                block.line += delta
        elif isinstance(block, _ListBlock):
            if block.key_line >= from_idx:
                block.key_line += delta
            block.item_lines = [(idx + delta if idx >= from_idx else idx) for idx in block.item_lines]
            if block.end >= from_idx:
                block.end += delta
        elif isinstance(block, _MapBlock):
            if block.key_line >= from_idx:
                block.key_line += delta
            block.children = {k: (idx + delta if idx >= from_idx else idx) for k, idx in block.children.items()}
            if block.end >= from_idx:
                block.end += delta


def _render_updated_line(existing_line: str, new_value_text: str, line_ending: str) -> str:
    """Rewrite ``existing_line`` (a ``key: value  # comment`` line, at
    any indent) with ``new_value_text`` substituted for its value,
    reusing the original indent, key spelling, inter-token whitespace
    and trailing comment byte-for-byte. A previously null-valued key
    (``"key:"`` with nothing after the colon) gets a single space
    inserted ahead of the new value -- there is no original spacing to
    reuse.
    """
    stripped = existing_line.rstrip("\r\n")
    ending_here = existing_line[len(stripped) :] or line_ending
    m = _TOP_LINE_RE.match(stripped)
    assert m is not None, f"not a key: value line: {existing_line!r}"
    value_area, comment_area = _find_comment_split(m.group("rest"))
    core = value_area.strip()
    lead_ws = value_area[: len(value_area) - len(value_area.lstrip())]
    trail_ws = value_area[len(lead_ws) + len(core) :]
    if lead_ws == "":
        lead_ws = " "
    new_rest = f"{lead_ws}{new_value_text}{trail_ws}{comment_area}"
    return f"{m.group('indent')}{m.group('key')}:{new_rest}{ending_here}"


def _child_indent(block: _MapBlock, fm_lines: list[str]) -> str:
    for idx in block.children.values():
        line = fm_lines[idx].rstrip("\r\n")
        m = _TOP_LINE_RE.match(line)
        if m:
            return m.group("indent")
    return "  "


def patch_frontmatter(text: str, changes: dict) -> str:
    """Apply ``changes`` (``{"model": "opus", "experimental.cacheTtl":
    "1h", ...}`` -- a dotted key targets a one-level-nested child) to
    ``text``'s frontmatter block, returning the whole file's new text.

    - An existing key (top-level scalar, flow list, block list, or a
      nested child under a one-level mapping) is updated **in place**:
      only its value substring changes, every surrounding character
      (indent, spacing, trailing comment) is preserved verbatim.
    - A missing top-level key is appended just before the closing
      ``---`` fence (a list value is appended as a flow list).
    - A missing dotted key is written as the nested-mapping form
      (``parent:`` / ``  child: value``) if neither the dotted form nor
      an existing ``parent:`` mapping is present yet (the plan's
      "defaulting to nested"); if the file already has a top-level
      ``"parent.child"`` line, that line is updated instead; if the file
      already has a ``parent:`` mapping (with or without this child
      yet), the child is set/added inside that existing mapping.
    - Every line not touched by ``changes`` -- including every comment,
      blank line, and untouched key -- is copied through unchanged.

    Raises :class:`FrontmatterError` if ``text`` has no ``---``-fenced
    frontmatter block, or the existing shape of a key ``changes`` names
    conflicts with the type of value being set for it (e.g. a list value
    for a key that is currently a scalar) -- refusing rather than
    guessing, per the module's convention.
    """
    doc = _split_document(text)
    if doc is None:
        raise FrontmatterError("no '---'-delimited frontmatter block found")
    lines, fm_lines, close_idx, ending = doc
    blocks = _scan(fm_lines)

    top_changes: dict[str, object] = {}
    nested_changes: dict[str, dict[str, object]] = {}
    for key, value in changes.items():
        if "." in key:
            parent, _, child = key.partition(".")
            nested_changes.setdefault(parent, {})[child] = value
        else:
            top_changes[key] = value

    appended: list[str] = []

    # -- top-level scalar / list keys --
    for key, value in top_changes.items():
        block = blocks.get(key)
        if block is None:
            text_value = _flow_list_repr(value) if isinstance(value, list) else _scalar_repr(value)
            appended.append(f"{key}: {text_value}{ending}")
            continue
        if isinstance(block, _ScalarBlock):
            # A _ScalarBlock is any key confined to a single physical
            # line -- that covers both a plain scalar ("model: sonnet")
            # and a flow list on one line ("tools: [Read, Edit]"), so
            # either a scalar or a list value renders onto it just fine;
            # there is no structural conflict to refuse here.
            new_text = _flow_list_repr(value) if isinstance(value, list) else _scalar_repr(value)
            fm_lines[block.line] = _render_updated_line(fm_lines[block.line], new_text, ending)
        elif isinstance(block, _ListBlock):
            if not isinstance(value, list):
                raise FrontmatterError(f"{key}: existing value is a list, cannot patch with a scalar")
            if not value:
                fm_lines[block.key_line] = _render_updated_line(fm_lines[block.key_line], "[]", ending)
                start, end = block.key_line + 1, block.item_lines[-1] + 1
                del fm_lines[start:end]
                _shift_blocks_after(blocks, end, -(end - start))
            else:
                first_item = fm_lines[block.item_lines[0]].rstrip("\r\n")
                im = _LIST_ITEM_RE.match(first_item)
                indent = im.group("indent") if im else "  "
                new_items = [f"{indent}- {_scalar_repr(item)}{ending}" for item in value]
                start, end = block.item_lines[0], block.item_lines[-1] + 1
                delta = len(new_items) - (end - start)
                fm_lines[start:end] = new_items
                _shift_blocks_after(blocks, end, delta)
        elif isinstance(block, _MapBlock):
            raise FrontmatterError(f"{key}: existing value is a nested mapping, cannot patch as a scalar/list")

    # -- dotted (one-level nested) keys, e.g. "experimental.cacheTtl" --
    for parent, child_values in nested_changes.items():
        handled: set[str] = set()
        for child, value in child_values.items():
            dotted_block = blocks.get(f"{parent}.{child}")
            if dotted_block is not None:
                if not isinstance(dotted_block, _ScalarBlock):
                    raise FrontmatterError(f"{parent}.{child}: unexpected existing shape")
                fm_lines[dotted_block.line] = _render_updated_line(
                    fm_lines[dotted_block.line], _scalar_repr(value), ending
                )
                handled.add(child)

        remaining = {c: v for c, v in child_values.items() if c not in handled}
        if not remaining:
            continue

        parent_block = blocks.get(parent)
        if parent_block is None:
            appended.append(f"{parent}:{ending}")
            for child, value in remaining.items():
                appended.append(f"  {child}: {_scalar_repr(value)}{ending}")
            continue
        if not isinstance(parent_block, _MapBlock):
            raise FrontmatterError(
                f"{parent}: existing value is not a nested mapping, cannot set {sorted(remaining)}"
            )
        for child, value in remaining.items():
            if child in parent_block.children:
                idx = parent_block.children[child]
                fm_lines[idx] = _render_updated_line(fm_lines[idx], _scalar_repr(value), ending)
            else:
                indent = _child_indent(parent_block, fm_lines)
                insert_at = parent_block.end
                fm_lines.insert(insert_at, f"{indent}{child}: {_scalar_repr(value)}{ending}")
                _shift_blocks_after(blocks, insert_at, 1)
                parent_block.children[child] = insert_at

    fm_lines.extend(appended)
    return lines[0] + "".join(fm_lines) + "".join(lines[close_idx:])
