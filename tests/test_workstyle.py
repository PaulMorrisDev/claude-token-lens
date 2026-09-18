"""``workstyle.py``: model-tier resolution and the six workstyle
archetypes' detection conditions, on hand-built ``SessionFeatures`` --
this module never reads a transcript itself, so every test here builds
the evidence directly (per the plan's test list).
"""

from __future__ import annotations

from claude_token_lens.model import SessionRecord
from claude_token_lens.workstyle import (
    SessionFeatures,
    corpus_archetype,
    describe_archetype,
    detect_archetype,
    model_tier,
)


# -- model_tier ----------------------------------------------------------


def test_model_tier_orders_families_low_to_high():
    assert model_tier("claude-haiku-4-5-20251001") == 0
    assert model_tier("claude-sonnet-5") == 1
    assert model_tier("claude-opus-5") == 2
    assert model_tier("claude-fable-5-1") == 3


def test_model_tier_matches_short_alias_forms():
    assert model_tier(None, agent_model_alias="sonnet") == 1
    assert model_tier(None, agent_model_alias="fable[1m]") == 3


def test_model_tier_prefers_model_id_over_alias_when_both_given():
    # Ground truth (Turn.model) wins over the .meta.json alias.
    assert model_tier("claude-opus-5", agent_model_alias="haiku") == 2


def test_model_tier_falls_back_to_alias_when_model_id_unset():
    assert model_tier(None, agent_model_alias="opus") == 2
    assert model_tier("", agent_model_alias="opus") == 2


def test_model_tier_unknown_family_returns_minus_one():
    assert model_tier("some-other-vendor-model") == -1
    assert model_tier(None, None) == -1


# -- detect_archetype ------------------------------------------------------


def test_detect_overseer_fanout():
    features = SessionFeatures(
        top_level_models=("claude-opus-5",),
        subagent_models=(("claude-sonnet-5", None), ("claude-sonnet-5", None), ("claude-sonnet-5", None)),
        spawn_count=3,
    )
    archetype, evidence = detect_archetype(features)
    assert archetype == "overseer-fanout"
    assert evidence["top_level_tier"] == 2
    assert evidence["subagent_tier"] == 1
    assert evidence["spawn_count"] == 3


def test_overseer_fanout_requires_at_least_three_spawns():
    # Same tier gap, but only 2 spawns -> falls through past overseer-fanout.
    features = SessionFeatures(
        top_level_models=("claude-opus-5",),
        subagent_models=(("claude-sonnet-5", None), ("claude-sonnet-5", None)),
        spawn_count=2,
    )
    archetype, _ = detect_archetype(features)
    assert archetype != "overseer-fanout"


def test_detect_plan_high_implement_low():
    features = SessionFeatures(
        top_level_models=("claude-opus-5",),
        plan_mode_seen=True,
        post_plan_lower_tier=True,
    )
    archetype, evidence = detect_archetype(features)
    assert archetype == "plan-high-implement-low"
    assert evidence["plan_mode_seen"] is True
    assert evidence["post_plan_lower_tier"] is True


def test_plan_mode_seen_alone_is_not_enough():
    features = SessionFeatures(plan_mode_seen=True, post_plan_lower_tier=False)
    archetype, _ = detect_archetype(features)
    assert archetype != "plan-high-implement-low"


def test_detect_workflow_heavy():
    features = SessionFeatures(has_workflow=True)
    archetype, evidence = detect_archetype(features)
    assert archetype == "workflow-heavy"
    assert evidence["has_workflow"] is True


def test_detect_effort_varied():
    # Two effort levels each >= 20% of effort-tagged turns.
    features = SessionFeatures(effort_turn_counts={"high": 4, "low": 4, "medium": 1})
    archetype, evidence = detect_archetype(features)
    assert archetype == "effort-varied"
    assert evidence["effort_shares"]["high"] == 4 / 9
    assert evidence["effort_shares"]["low"] == 4 / 9


def test_effort_varied_requires_two_qualifying_levels():
    # Only "high" clears the 20% bar; "low" is below it.
    features = SessionFeatures(effort_turn_counts={"high": 19, "low": 1})
    archetype, _ = detect_archetype(features)
    assert archetype != "effort-varied"


def test_detect_single_model():
    features = SessionFeatures(
        top_level_models=("claude-sonnet-5",),
        subagent_models=(("claude-sonnet-5", None),),
        spawn_count=1,
    )
    archetype, evidence = detect_archetype(features)
    assert archetype == "single-model"
    assert evidence["known_model_families"] == 1


def test_single_model_requires_at_most_two_spawns():
    features = SessionFeatures(
        top_level_models=("claude-sonnet-5",),
        subagent_models=(
            ("claude-sonnet-5", None),
            ("claude-sonnet-5", None),
            ("claude-sonnet-5", None),
        ),
        spawn_count=3,
    )
    archetype, _ = detect_archetype(features)
    assert archetype != "single-model"


def test_detect_chat_only():
    features = SessionFeatures(spawn_count=0, top_level_tool_names=frozenset({"Read", "Grep"}))
    archetype, evidence = detect_archetype(features)
    assert archetype == "chat-only"
    assert evidence["top_level_tool_names"] == ["Grep", "Read"]


def test_chat_only_requires_no_mutating_tools():
    features = SessionFeatures(spawn_count=0, top_level_tool_names=frozenset({"Read", "Edit"}))
    archetype, _ = detect_archetype(features)
    assert archetype != "chat-only"


def test_detect_mixed_fallback():
    # No archetype's condition holds: multiple spawns but no tier gap,
    # no plan/workflow signal, uniform effort, more than one model family
    # and more than 2 spawns, and tools beyond chat-only's allowlist.
    features = SessionFeatures(
        top_level_models=("claude-sonnet-5",),
        subagent_models=(("claude-opus-5", None), ("claude-opus-5", None), ("claude-opus-5", None)),
        spawn_count=3,
        top_level_tool_names=frozenset({"Edit", "Bash"}),
    )
    archetype, _ = detect_archetype(features)
    assert archetype == "mixed"


def test_detect_archetype_evidence_always_carries_agent_settings():
    features = SessionFeatures(agent_settings={"careful": 3, "yolo": 1}, has_workflow=True)
    _, evidence = detect_archetype(features)
    assert evidence["agent_settings"] == {"careful": 3, "yolo": 1}


def test_first_match_wins_order_overseer_beats_workflow_heavy():
    # Both overseer-fanout's and workflow-heavy's conditions hold;
    # overseer-fanout is checked first and wins.
    features = SessionFeatures(
        top_level_models=("claude-opus-5",),
        subagent_models=(("claude-sonnet-5", None), ("claude-sonnet-5", None), ("claude-sonnet-5", None)),
        spawn_count=3,
        has_workflow=True,
    )
    archetype, _ = detect_archetype(features)
    assert archetype == "overseer-fanout"


# -- corpus_archetype --------------------------------------------------------


def test_corpus_archetype_majority_vote():
    records = [
        SessionRecord(session_id="s1", archetype="workflow-heavy"),
        SessionRecord(session_id="s2", archetype="workflow-heavy"),
        SessionRecord(session_id="s3", archetype="chat-only"),
    ]
    archetype, evidence = corpus_archetype(records)
    assert archetype == "workflow-heavy"
    assert evidence["sessions"] == 3
    assert evidence["counts"] == {"workflow-heavy": 2, "chat-only": 1}


def test_corpus_archetype_ties_break_alphabetically():
    # corpus_archetype breaks ties via max((count, name)) -- the
    # lexicographically *largest* name wins a tie, deterministically.
    records = [
        SessionRecord(session_id="s1", archetype="single-model"),
        SessionRecord(session_id="s2", archetype="chat-only"),
    ]
    archetype, _ = corpus_archetype(records)
    assert archetype == "single-model"  # "single-model" > "chat-only"


def test_corpus_archetype_unclassified_records_counted_but_not_voted():
    records = [
        SessionRecord(session_id="s1", archetype=None),
        SessionRecord(session_id="s2", archetype=None),
    ]
    archetype, evidence = corpus_archetype(records)
    assert archetype is None
    assert evidence["sessions"] == 2
    assert evidence["counts"] == {}


def test_corpus_archetype_empty_corpus():
    archetype, evidence = corpus_archetype([])
    assert archetype is None
    assert evidence["sessions"] == 0


# -- describe_archetype -------------------------------------------------------


def test_describe_archetype_covers_all_six_plus_mixed():
    for archetype in (
        "overseer-fanout",
        "plan-high-implement-low",
        "workflow-heavy",
        "effort-varied",
        "single-model",
        "chat-only",
        "mixed",
    ):
        text = describe_archetype(archetype)
        assert isinstance(text, str) and text


def test_describe_archetype_unrecognised_value_does_not_raise():
    text = describe_archetype("not-a-real-archetype")
    assert "not-a-real-archetype" in text
