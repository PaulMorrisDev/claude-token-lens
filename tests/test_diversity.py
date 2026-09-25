"""WP12a deliverable 5: nine hand-written synthetic session directories
under ``tests/fixtures/diversity/`` covering shapes the real corpus this
project was built from may not exercise -- cloud-provider model ids,
alternate entrypoints, tool-free chat, teammate messages, non-English
prompts, MCP-tool-heavy sessions, a plan-then-implement workflow, and a
pre-cache-split ("old version") transcript.

Every case is asserted to parse cleanly (zero unparsable lines) and pass
``assert_privacy``; most also carry one case-specific assertion. Two
cases (``chat-only``, ``plan-then-implement``) need a
``workstyle.SessionFeatures`` to exercise ``detect_archetype`` -- no
function in this worktree derives one from a parsed ``TranscriptResult``
yet (a gap noted for whoever owns WP8/workstyle.py), so ``_session_features``
below hand-builds one directly from the fixture's own turns/events. It is
a test-local stand-in, not a claim that this is the eventual real
derivation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claudeglass import classify, discovery, workstyle
from claudeglass.model import EventKind, TranscriptMeta, TranscriptResult
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing

from helpers import assert_privacy

FIXTURES = Path(__file__).parent / "fixtures" / "diversity"

_CASES = (
    "bedrock",
    "vertex",
    "sdk-headless",
    "chat-only",
    "peer-team",
    "non-english",
    "mcp-heavy",
    "plan-then-implement",
    "old-version",
)


def _load(case: str) -> tuple[TranscriptResult, list[TranscriptResult]]:
    case_dir = FIXTURES / case
    top_paths = list(case_dir.glob("*.jsonl"))
    assert len(top_paths) == 1, f"{case}: expected exactly one top-level jsonl, found {top_paths}"
    top_path = top_paths[0]
    session_id = top_path.stem

    meta = TranscriptMeta(path=str(top_path), kind="top-level", session_id=session_id)
    top_result = parse_transcript(top_path, meta)

    sub_results = []
    for jsonl_path, _meta_dict in discovery.find_subagents(case_dir, session_id):
        sub_meta = discovery.load_meta(jsonl_path.with_name(jsonl_path.stem + ".meta.json"))
        sub_results.append(parse_transcript(jsonl_path, sub_meta))
    return top_result, sub_results


def _session_features(top: TranscriptResult, **overrides) -> workstyle.SessionFeatures:
    """Hand-built ``workstyle.SessionFeatures`` for a top-level-only
    fixture (no subagents in any of the diversity cases that need this):
    model tuple and tool-name set read straight off the parsed turns,
    everything else defaulted unless overridden by the caller for the
    signals this simple parser can't derive on its own (``plan_mode_seen``/
    ``post_plan_lower_tier`` -- see module docstring).
    """
    priced = [t for t in top.turns if t.turn_index > 0]
    defaults = dict(
        top_level_models=tuple(t.model for t in priced),
        spawn_count=0,
        top_level_tool_names=frozenset(name for t in priced for name in t.tool_names),
    )
    defaults.update(overrides)
    return workstyle.SessionFeatures(**defaults)


# -- universal: every case parses cleanly and passes the privacy scan -------


@pytest.mark.parametrize("case", _CASES)
def test_every_diversity_case_parses_with_zero_unparsable_lines(case):
    top, subs = _load(case)
    assert top.diagnostics.unparsable_lines == 0
    for sub in subs:
        assert sub.diagnostics.unparsable_lines == 0


@pytest.mark.parametrize("case", _CASES)
def test_every_diversity_case_passes_the_dataclass_privacy_scan(case):
    top, subs = _load(case)
    for result in [top] + subs:
        assert_privacy(result)


# -- (a) bedrock: model id form us.anthropic.<id>-v1:0 -----------------------


def test_bedrock_model_id_resolves_via_cloud_strip():
    top, _subs = _load("bedrock")
    pricing = load_pricing()
    priced = [t for t in top.turns if t.turn_index > 0]
    assert priced, "fixture must carry at least one priced turn"
    for turn in priced:
        resolved = pricing.resolve_model(turn.model)
        assert resolved is not None, turn.model
        assert resolved.matched_via == "cloud_strip"
        assert resolved.canonical_id == "claude-sonnet-5"


def test_bedrock_provider_detected_from_model_id_form():
    top, _subs = _load("bedrock")
    assert top.meta.provider == "bedrock"


# -- (b) vertex: model id form <id>@<date> -----------------------------------


def test_vertex_model_id_resolves_via_cloud_strip():
    top, _subs = _load("vertex")
    pricing = load_pricing()
    priced = [t for t in top.turns if t.turn_index > 0]
    assert priced
    for turn in priced:
        resolved = pricing.resolve_model(turn.model)
        assert resolved is not None, turn.model
        assert resolved.matched_via == "cloud_strip"
        assert resolved.canonical_id == "claude-sonnet-5"


def test_vertex_provider_detected_from_model_id_form():
    top, _subs = _load("vertex")
    assert top.meta.provider == "vertex"


# -- (c) sdk-headless: entrypoint carried on every raw line ------------------


def test_sdk_headless_entrypoint_is_captured():
    top, _subs = _load("sdk-headless")
    assert top.meta.entrypoint == "sdk"


# -- (d) chat-only: no tool use anywhere -------------------------------------
#
# workstyle.detect_archetype used to check "single-model" (>=1 known model
# tier, <=2 spawns) BEFORE "chat-only" (0 spawns, tools within the
# discovery set). A real chat-only session almost always has a resolvable
# model tier (its turns carry a real model id), so it always landed on
# "single-model" -- "chat-only" was only reachable when no turn's model
# resolved to a known tier at all. Fixed on main (0f892e3) by testing
# chat-only first: it's a strictly narrower condition than single-model's
# "at most 2 spawns", so nothing that used to correctly match single-model
# or mixed changes.


def test_chat_only_has_no_tool_use_at_all():
    top, _subs = _load("chat-only")
    priced = [t for t in top.turns if t.turn_index > 0]
    assert priced
    for turn in priced:
        assert turn.tool_names == ()


def test_chat_only_archetype_is_chat_only():
    top, _subs = _load("chat-only")
    features = _session_features(top)
    archetype, evidence = workstyle.detect_archetype(features)
    assert archetype == "chat-only", evidence
    assert evidence["top_level_tool_names"] == sorted(features.top_level_tool_names)


# -- (e) peer-team: origin.kind == "peer" messages ---------------------------


def test_peer_team_messages_classify_as_peer_message_events():
    top, _subs = _load("peer-team")
    peer_events = [e for e in top.events if e.kind == EventKind.PEER_MESSAGE]
    assert len(peer_events) == 2


# -- (f) non-english: French prompts degrade to general-dev, not a crash ----


def test_non_english_prompts_classify_as_general_dev():
    top, _subs = _load("non-english")
    features = classify.extract_features(top, [])
    purpose, _evidence = classify.classify_purpose(features)
    assert purpose == "general-dev"


# -- (g) mcp-heavy: MCP tool names and attribution fields --------------------


def test_mcp_heavy_tool_names_and_attribution_are_captured():
    top, _subs = _load("mcp-heavy")
    priced = [t for t in top.turns if t.turn_index > 0]
    assert len(priced) == 3
    for turn in priced:
        assert len(turn.tool_names) == 1
        assert turn.tool_names[0].startswith("mcp__")
        assert turn.attribution_mcp_server is not None
        assert turn.attribution_mcp_tool is not None


# -- (h) plan-then-implement: plan_mode signal, then a lower-tier turn ------


def test_plan_then_implement_has_one_plan_mode_signal():
    top, _subs = _load("plan-then-implement")
    plan_events = [e for e in top.events if e.kind == EventKind.CACHE_SIGNAL and e.subkind == "plan_mode"]
    assert len(plan_events) == 1


def test_plan_then_implement_archetype_is_plan_high_implement_low():
    top, _subs = _load("plan-then-implement")
    priced = [t for t in top.turns if t.turn_index > 0]
    tiers = [workstyle.model_tier(t.model) for t in priced]
    plan_events = [e for e in top.events if e.kind == EventKind.CACHE_SIGNAL and e.subkind == "plan_mode"]

    # The fixture's own shape (see module docstring): the plan turn is
    # first, so any later turn with a strictly lower tier than the first
    # is "a lower-tier model turn observed after plan mode exited".
    post_plan_lower_tier = any(tier < tiers[0] for tier in tiers[1:])

    features = _session_features(
        top,
        plan_mode_seen=bool(plan_events),
        post_plan_lower_tier=post_plan_lower_tier,
    )
    archetype, evidence = workstyle.detect_archetype(features)
    assert archetype == "plan-high-implement-low", evidence


# -- (i) old-version: flat cache_creation_input_tokens, no ephemeral split --
#
# When a turn's usage payload has no nested "cache_creation" object at all
# (an older Claude Code JSONL shape, before the ephemeral_5m/1h split
# existed), cc_5m and cc_1h both default to 0. parse.py used to compare
# that 0 sum against the flat cache_creation_input_tokens field and flag
# a Diagnostics.ttl_sum_mismatch whenever the flat field was non-zero --
# but that's not an invariant breach, just a transcript older than the
# field split. Fixed on main (709239a): a missing nested object now sets
# Turn.ttl_split_unknown and is counted under the new, additive
# Diagnostics.pre_split_turns instead of ttl_sum_mismatch. The flat value
# itself is preserved (never zeroed or dropped): turn.cache_creation_tokens
# still reads the real flat number.


def test_old_version_flat_cache_creation_field_is_honoured():
    top, _subs = _load("old-version")
    priced = [t for t in top.turns if t.turn_index > 0]
    assert len(priced) == 2
    assert priced[0].cache_creation_tokens == 5000
    assert priced[0].cc_5m == 0
    assert priced[0].cc_1h == 0
    assert priced[1].cache_creation_tokens == 0


def test_old_version_flags_pre_split_turns_not_ttl_sum_mismatch():
    top, _subs = _load("old-version")
    # See the module-level note above (fixed on main, 709239a): a missing
    # nested cache_creation object is a format difference, not a sum
    # invariant breach, so it must never set ttl_sum_mismatch.
    priced = [t for t in top.turns if t.turn_index > 0]
    assert priced[0].ttl_split_unknown is True
    assert priced[1].ttl_split_unknown is True
    assert top.diagnostics.ttl_sum_mismatch == 0
    assert top.diagnostics.pre_split_turns == 2
