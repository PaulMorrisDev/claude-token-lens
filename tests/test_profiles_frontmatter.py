"""Tests for ``profiles/frontmatter.py``: the no-YAML-library parser and
patcher for a Claude Code ``<agent>.md`` frontmatter block. A round-trip
corpus of synthetic agent files, one per construct the module claims to
handle (plain scalar, quoted string, int, bool, flow list, block list,
dotted nested key, nested-mapping form, comments, a brand-new key), plus
the "patching twice equals patching once" idempotency guarantee and the
refuse-don't-guess error cases.
"""

from __future__ import annotations

import pytest

from claudeglass.profiles.frontmatter import (
    FrontmatterError,
    parse_frontmatter,
    patch_frontmatter,
)


# --------------------------------------------------------------------
# Construct corpus -- eight+ synthetic agent files
# --------------------------------------------------------------------

PLAIN_SCALAR = """---
name: claude-implementer
model: sonnet
---
Body text.
"""

QUOTED_STRING = """---
name: "claude implementer"
description: "Implements things: carefully."
---
Body.
"""

INT_VALUE = """---
name: claude-implementer
maxTurns: 40
---
Body.
"""

BOOL_VALUE = """---
name: claude-implementer
omitClaudeMd: false
---
Body.
"""

FLOW_LIST = """---
name: claude-implementer
tools: [Read, Edit, Bash]
---
Body.
"""

BLOCK_LIST = """---
name: claude-implementer
disallowedTools:
  - Bash
  - WebFetch
---
Body.
"""

DOTTED_NESTED = """---
name: verification-runner
experimental.cacheTtl: 1h
---
Body.
"""

NESTED_MAPPING = """---
name: verification-runner
experimental:
  cacheTtl: 5m
---
Body.
"""

WITH_COMMENTS = """---
# top-of-file comment, keep me
name: claude-implementer
model: sonnet  # pinned deliberately
# a lone comment line between keys
effort: medium
---
Body.
"""

NO_MATCHING_KEY = """---
name: claude-implementer
---
Body.
"""

# Fix S8: a YAML block scalar ("|" literal style) used for a multi-line
# description -- the review's exact repro shape, including a blank line
# and a line containing a colon inside the block (which must not be
# mistaken for a fresh top-level key).
BLOCK_SCALAR = """---
name: claude-implementer
description: |
  Line one of the description.

  Line two: contains a colon, which must not be mistaken for a key.
model: sonnet
---
Body.
"""

# The folded ("&gt;") style, plus a chomping indicator ("|-"), covering the
# rest of the shapes _BLOCK_SCALAR_RE matches.
BLOCK_SCALAR_FOLDED = """---
name: claude-implementer
description: >
  Folded line one.
  Folded line two.
model: sonnet
---
Body.
"""

BLOCK_SCALAR_STRIP_CHOMPED = """---
name: claude-implementer
description: |-
  Line one.
  Line two.
model: sonnet
---
Body.
"""


CONSTRUCT_FIXTURES = {
    "plain_scalar": PLAIN_SCALAR,
    "quoted_string": QUOTED_STRING,
    "int_value": INT_VALUE,
    "bool_value": BOOL_VALUE,
    "flow_list": FLOW_LIST,
    "block_list": BLOCK_LIST,
    "dotted_nested": DOTTED_NESTED,
    "nested_mapping": NESTED_MAPPING,
    "with_comments": WITH_COMMENTS,
    "no_matching_key": NO_MATCHING_KEY,
    "block_scalar": BLOCK_SCALAR,
    "block_scalar_folded": BLOCK_SCALAR_FOLDED,
    "block_scalar_strip_chomped": BLOCK_SCALAR_STRIP_CHOMPED,
}


@pytest.mark.parametrize("name", sorted(CONSTRUCT_FIXTURES))
def test_parse_frontmatter_does_not_raise_on_every_construct(name):
    parse_frontmatter(CONSTRUCT_FIXTURES[name])


def test_parse_plain_scalar():
    assert parse_frontmatter(PLAIN_SCALAR) == {"name": "claude-implementer", "model": "sonnet"}


def test_parse_quoted_string_unescapes():
    parsed = parse_frontmatter(QUOTED_STRING)
    assert parsed["name"] == "claude implementer"
    assert parsed["description"] == "Implements things: carefully."


def test_parse_int_value():
    assert parse_frontmatter(INT_VALUE)["maxTurns"] == 40


def test_parse_bool_value():
    assert parse_frontmatter(BOOL_VALUE)["omitClaudeMd"] is False


def test_parse_flow_list():
    assert parse_frontmatter(FLOW_LIST)["tools"] == ["Read", "Edit", "Bash"]


def test_parse_block_list():
    assert parse_frontmatter(BLOCK_LIST)["disallowedTools"] == ["Bash", "WebFetch"]


def test_parse_dotted_nested():
    assert parse_frontmatter(DOTTED_NESTED)["experimental.cacheTtl"] == "1h"


def test_parse_nested_mapping_flattens_to_dotted():
    assert parse_frontmatter(NESTED_MAPPING)["experimental.cacheTtl"] == "5m"


def test_parse_with_comments_ignores_comment_lines():
    parsed = parse_frontmatter(WITH_COMMENTS)
    assert parsed["model"] == "sonnet"
    assert parsed["effort"] == "medium"


def test_parse_no_frontmatter_block_returns_empty_dict():
    assert parse_frontmatter("no frontmatter here\n") == {}


# --------------------------------------------------------------------
# Fix S8: YAML block scalars ("|"/">" ) are opaque preserved blocks --
# the parser must not refuse the file outright, and must still see the
# top-level scalar keys before and after the block.
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", [BLOCK_SCALAR, BLOCK_SCALAR_FOLDED, BLOCK_SCALAR_STRIP_CHOMPED]
)
def test_parse_block_scalar_is_skipped_but_surrounding_keys_still_parse(text):
    parsed = parse_frontmatter(text)
    assert parsed["name"] == "claude-implementer"
    assert parsed["model"] == "sonnet"
    assert "description" not in parsed


def test_patch_around_block_scalar_preserves_it_byte_identically():
    out = patch_frontmatter(BLOCK_SCALAR, {"model": "opus"})
    # Every line of the block scalar -- including the blank line and the
    # line containing a colon -- must survive completely unchanged.
    assert "description: |\n" in out
    assert "  Line one of the description.\n" in out
    assert "\n\n" in out
    assert "  Line two: contains a colon, which must not be mistaken for a key.\n" in out
    # Byte-identical apart from the patched "model" line.
    assert out == BLOCK_SCALAR.replace("model: sonnet\n", "model: opus\n")
    assert parse_frontmatter(out)["model"] == "opus"


def test_patch_raises_on_block_scalar_as_patch_target():
    # description's existing value is a block scalar; patch_frontmatter
    # must refuse rather than guess how to collapse it to a plain scalar.
    with pytest.raises(FrontmatterError):
        patch_frontmatter(BLOCK_SCALAR, {"description": "One line now."})


# --------------------------------------------------------------------
# patch_frontmatter: update-in-place preserves everything unrelated
# --------------------------------------------------------------------


def test_patch_updates_plain_scalar_in_place():
    out = patch_frontmatter(PLAIN_SCALAR, {"model": "opus"})
    assert "model: opus\n" in out
    assert "name: claude-implementer\n" in out
    assert out.endswith("Body text.\n")
    assert parse_frontmatter(out)["model"] == "opus"


def test_patch_preserves_trailing_comment_on_updated_line():
    out = patch_frontmatter(WITH_COMMENTS, {"model": "opus"})
    assert "model: opus  # pinned deliberately\n" in out
    # every unrelated line is untouched, including comments and ordering
    assert "# top-of-file comment, keep me\n" in out
    assert "# a lone comment line between keys\n" in out
    assert "effort: medium\n" in out
    lines = out.splitlines()
    assert lines.index("model: opus  # pinned deliberately") < lines.index("effort: medium")


def test_patch_quoted_string_value():
    out = patch_frontmatter(QUOTED_STRING, {"description": "Implements: fast."})
    assert parse_frontmatter(out)["description"] == "Implements: fast."
    assert parse_frontmatter(out)["name"] == "claude implementer"


def test_patch_int_value():
    out = patch_frontmatter(INT_VALUE, {"maxTurns": 80})
    assert "maxTurns: 80\n" in out


def test_patch_bool_value():
    out = patch_frontmatter(BOOL_VALUE, {"omitClaudeMd": True})
    assert "omitClaudeMd: true\n" in out


def test_patch_flow_list_value():
    out = patch_frontmatter(FLOW_LIST, {"tools": ["Read", "Grep"]})
    assert parse_frontmatter(out)["tools"] == ["Read", "Grep"]
    assert "name: claude-implementer\n" in out


def test_patch_flow_list_to_empty_list_round_trips():
    out = patch_frontmatter(FLOW_LIST, {"tools": []})
    assert parse_frontmatter(out)["tools"] == []


def test_patch_block_list_value_preserves_indent_style():
    out = patch_frontmatter(BLOCK_LIST, {"disallowedTools": ["Bash", "WebFetch", "WebSearch"]})
    assert parse_frontmatter(out)["disallowedTools"] == ["Bash", "WebFetch", "WebSearch"]
    assert "  - Bash\n" in out
    assert "  - WebSearch\n" in out


def test_patch_block_list_to_empty_collapses_to_flow_list():
    out = patch_frontmatter(BLOCK_LIST, {"disallowedTools": []})
    assert parse_frontmatter(out)["disallowedTools"] == []
    assert "disallowedTools: []\n" in out


def test_patch_dotted_key_updates_existing_dotted_form():
    out = patch_frontmatter(DOTTED_NESTED, {"experimental.cacheTtl": "5m"})
    assert "experimental.cacheTtl: 5m\n" in out
    assert parse_frontmatter(out)["experimental.cacheTtl"] == "5m"


def test_patch_dotted_key_updates_existing_nested_form():
    out = patch_frontmatter(NESTED_MAPPING, {"experimental.cacheTtl": "1h"})
    assert "  cacheTtl: 1h\n" in out
    assert "experimental.cacheTtl:" not in out  # stayed in nested form, not dotted
    assert parse_frontmatter(out)["experimental.cacheTtl"] == "1h"


def test_patch_dotted_key_defaults_to_nested_form_when_absent():
    out = patch_frontmatter(NO_MATCHING_KEY, {"experimental.cacheTtl": "1h"})
    assert "experimental:\n" in out
    assert "  cacheTtl: 1h\n" in out
    assert parse_frontmatter(out)["experimental.cacheTtl"] == "1h"


def test_patch_appends_missing_top_level_key_before_closing_fence():
    out = patch_frontmatter(PLAIN_SCALAR, {"effort": "high"})
    assert "effort: high\n" in out
    lines = out.splitlines()
    fence_indices = [i for i, line in enumerate(lines) if line.strip() == "---"]
    assert len(fence_indices) == 2
    assert lines.index("effort: high") < fence_indices[1]
    assert parse_frontmatter(out)["effort"] == "high"


def test_patch_multiple_keys_at_once():
    out = patch_frontmatter(PLAIN_SCALAR, {"model": "opus", "effort": "high", "maxTurns": 30})
    parsed = parse_frontmatter(out)
    assert parsed["model"] == "opus"
    assert parsed["effort"] == "high"
    assert parsed["maxTurns"] == 30


def test_patch_never_touches_the_body_after_the_closing_fence():
    body_heavy = PLAIN_SCALAR + "\nMore body.\n\n- a bullet\n"
    out = patch_frontmatter(body_heavy, {"model": "opus"})
    assert out.endswith("\nMore body.\n\n- a bullet\n")


def test_patch_preserves_crlf_line_endings():
    crlf_text = PLAIN_SCALAR.replace("\n", "\r\n")
    out = patch_frontmatter(crlf_text, {"model": "opus"})
    assert "model: opus\r\n" in out
    assert "\r\n" in out
    assert parse_frontmatter(out)["model"] == "opus"


# --------------------------------------------------------------------
# Idempotency: patching twice equals patching once
# --------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CONSTRUCT_FIXTURES))
def test_patching_twice_equals_patching_once(name):
    text = CONSTRUCT_FIXTURES[name]
    changes = {
        "model": "opus",
        "effort": "high",
        "maxTurns": 50,
        "omitClaudeMd": True,
        "tools": ["Read", "Edit"],
        "disallowedTools": ["Bash"],
        "experimental.cacheTtl": "1h",
    }
    once = patch_frontmatter(text, changes)
    twice = patch_frontmatter(once, changes)
    assert once == twice


def test_idempotent_on_a_file_that_already_has_every_construct():
    text = """---
name: claude-implementer
model: sonnet
maxTurns: 40
omitClaudeMd: false
tools: [Read, Edit]
disallowedTools:
  - Bash
experimental:
  cacheTtl: 5m
---
Body.
"""
    changes = {"model": "opus", "maxTurns": 60, "experimental.cacheTtl": "1h"}
    once = patch_frontmatter(text, changes)
    twice = patch_frontmatter(once, changes)
    assert once == twice


# --------------------------------------------------------------------
# Refuse-don't-guess error cases
# --------------------------------------------------------------------


def test_patch_raises_on_missing_frontmatter_block():
    with pytest.raises(FrontmatterError):
        patch_frontmatter("no frontmatter here\n", {"model": "opus"})


def test_patch_raises_on_unterminated_fence():
    with pytest.raises(FrontmatterError):
        patch_frontmatter("---\nname: x\nBody with no closing fence\n", {"model": "opus"})


def test_scan_raises_on_mixed_list_and_mapping():
    text = """---
name: claude-implementer
weird:
  - one
  key: value
---
Body.
"""
    with pytest.raises(FrontmatterError):
        patch_frontmatter(text, {"model": "opus"})


def test_scan_raises_on_nested_mapping_deeper_than_one_level():
    text = """---
name: claude-implementer
experimental:
  cacheTtl:
    inner: value
---
Body.
"""
    with pytest.raises(FrontmatterError):
        patch_frontmatter(text, {"experimental.cacheTtl": "1h"})


def test_scan_raises_on_tab_indentation():
    text = "---\nname: claude-implementer\ntools:\n\t- Read\n---\nBody.\n"
    with pytest.raises(FrontmatterError):
        patch_frontmatter(text, {"model": "opus"})


def test_scan_raises_on_duplicate_top_level_key():
    text = """---
name: claude-implementer
model: sonnet
model: opus
---
Body.
"""
    with pytest.raises(FrontmatterError):
        patch_frontmatter(text, {"effort": "high"})


def test_patch_raises_on_type_conflict_block_list_to_scalar():
    # A block list ("- item" lines) is a genuine multi-line structure; the
    # module refuses to collapse it down to a single scalar line rather
    # than guess how to restructure it.
    with pytest.raises(FrontmatterError):
        patch_frontmatter(BLOCK_LIST, {"disallowedTools": "Bash"})


def test_patch_raises_on_type_conflict_nested_mapping_to_scalar():
    # A nested mapping ("experimental:" then indented children) is also a
    # genuine multi-line structure; it cannot be replaced by a bare
    # top-level scalar/list value without a dotted or nested-form target.
    with pytest.raises(FrontmatterError):
        patch_frontmatter(NESTED_MAPPING, {"experimental": "5m"})


def test_patch_rewrites_single_line_flow_list_as_scalar():
    # A flow list ("tools: [a, b]") lives on a single physical line, just
    # like a plain scalar -- there is no structural reason to refuse
    # rewriting that one line with a plain scalar value.
    out = patch_frontmatter(FLOW_LIST, {"tools": "Read"})
    assert parse_frontmatter(out)["tools"] == "Read"


def test_patch_rewrites_single_line_scalar_as_flow_list():
    out = patch_frontmatter(PLAIN_SCALAR, {"model": ["a", "b"]})
    assert parse_frontmatter(out)["model"] == ["a", "b"]
