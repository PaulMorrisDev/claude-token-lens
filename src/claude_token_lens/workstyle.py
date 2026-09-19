"""Workstyle archetype detection (WP8): what a session's (or a corpus's)
working pattern is, so the recommendation engine (WP10) never tells an
overseer user to "stop spawning agents" or a chat-only user about
subagent TTLs.

:class:`SessionFeatures` is the evidence a caller (a later report-
assembly package, once ``SessionRecord``s exist end-to-end) extracts from
one session; this module never reads a ``TranscriptResult`` or
``WorkflowRun`` directly, so :func:`detect_archetype` stays a pure
function testable from hand-built feature values, per the plan's test
list. Raw model ids/aliases, not pre-resolved tiers, are what
``SessionFeatures`` carries — :func:`model_tier` is the one place that
resolves a tier, so a test (or a future caller) can hand it either
observed shape (a full id like ``claude-fable-5-1``, or a short alias
like ``sonnet``) and get the same answer ``detect_archetype`` would.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence, Union

from .model import Column, Section, SessionRecord, Table

#: Tier order, lowest rank first, per the plan: "fable > opus > sonnet >
#: haiku, resolved from model id substrings and agent_model_alias."
_TIER_FAMILIES: tuple[str, ...] = ("haiku", "sonnet", "opus", "fable")


def model_tier(model_id: str | None, agent_model_alias: str | None = None) -> int:
    """Resolve a model tier rank (fable=3, opus=2, sonnet=1, haiku=0) from
    a raw model id and/or an agent's declared model alias, by case-
    insensitive substring match against the family name — the observed
    id/alias shapes (``claude-fable-5-1``, ``fable[1m]``, ``sonnet``,
    ``claude-haiku-4-5-20251001``) all embed the family name literally,
    so a substring match is sufficient and doesn't need a pricing-style
    alias table.

    ``model_id`` is tried first, then ``agent_model_alias`` — a
    subagent's own turns (``Turn.model``) carry the ground truth actually
    billed, so that takes priority over the ``.meta.json`` alias when
    both are given and happen to disagree.

    Returns ``-1`` when neither string names a recognised family, so
    "unknown" always sorts below every real tier without a separate
    ``None``-handling branch at every call site.
    """
    for candidate in (model_id, agent_model_alias):
        if not candidate:
            continue
        lowered = candidate.lower()
        for rank, family in enumerate(_TIER_FAMILIES):
            if family in lowered:
                return rank
    return -1


@dataclass(slots=True)
class SessionFeatures:
    """Evidence extracted from one session, used to detect its workstyle
    archetype. See module docstring for why this holds raw model
    ids/aliases rather than pre-resolved tiers.
    """

    #: Turn.model values observed on the top-level transcript's own
    #: priced turns.
    top_level_models: tuple[str, ...] = ()
    #: (Turn.model, TranscriptMeta.agent_model_alias) pairs, one per
    #: spawned subagent transcript — either element may be ``None``.
    subagent_models: tuple[tuple[str | None, str | None], ...] = ()
    #: Direct subagent spawns in the session (``len(subs)``).
    spawn_count: int = 0
    #: Whether the session ran at least one Workflow (ultracode) run.
    has_workflow: bool = False
    #: effort value -> turn count, over every priced turn (top-level and
    #: subagent) that carried a non-None ``Turn.effort``.
    effort_turn_counts: dict[str, int] = field(default_factory=dict)
    #: Whether a ``CACHE_SIGNAL`` ``plan_mode`` attachment was observed on
    #: the top-level transcript.
    plan_mode_seen: bool = False
    #: Whether a lower-tier model turn (top-level) or a lower-tier
    #: implementer agent was observed after plan mode exited.
    post_plan_lower_tier: bool = False
    #: Distinct tool names used on the top-level transcript's priced turns.
    top_level_tool_names: frozenset[str] = frozenset()
    #: Diagnostics.agent_settings from the top-level transcript (persona
    #: evidence), carried through as evidence only — never a deciding
    #: input to any archetype's condition.
    agent_settings: dict[str, int] = field(default_factory=dict)


#: Tool names a chat-only session is allowed to have used, beyond none at
#: all — read-only inspection, never a mutation or a spawn.
_CHAT_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob"})

#: Minimum share (of effort-tagged turns) an effort value needs to count
#: as one of "effort-varied"'s two-or-more qualifying levels.
_EFFORT_VARIED_MIN_SHARE = 0.20

#: Minimum direct spawns for "overseer-fanout".
_OVERSEER_MIN_SPAWNS = 3

#: Maximum direct spawns still compatible with "single-model" (a solo
#: operator who occasionally delegates a small task is still solo).
_SINGLE_MODEL_MAX_SPAWNS = 2


def detect_archetype(features: SessionFeatures) -> tuple[str, dict]:
    """Classify one session's workstyle archetype from its
    :class:`SessionFeatures`, first match wins:

    - ``overseer-fanout``: top-level model tier is strictly higher than
      every subagent's tier, AND at least 3 spawns.
    - ``plan-high-implement-low``: a plan-mode signal was seen, followed
      by a lower-tier model turn or implementer agent.
    - ``workflow-heavy``: at least one Workflow run.
    - ``effort-varied``: at least 2 distinct effort values each carrying
      at least 20% of effort-tagged turns.
    - ``single-model``: exactly one model family observed (top-level and
      subagent combined) and at most 2 spawns.
    - ``chat-only``: no spawns and no tool beyond Read/Grep/Glob.
    - ``mixed``: none of the above — a documented fallback, not itself
      one of the plan's six archetypes.

    Returns ``(archetype, evidence)``; ``evidence`` always carries
    ``features.agent_settings`` (persona evidence, per the plan) plus
    whatever specific values decided the match, so a report can cite
    exactly why a session landed where it did.
    """
    evidence: dict = {"agent_settings": dict(features.agent_settings)}

    top_tier = max((model_tier(m) for m in features.top_level_models), default=-1)
    subagent_tiers = [model_tier(model, alias) for model, alias in features.subagent_models]
    subagent_tier = max(subagent_tiers, default=-1)
    evidence["top_level_tier"] = top_tier
    evidence["subagent_tier"] = subagent_tier
    evidence["spawn_count"] = features.spawn_count

    if features.spawn_count >= _OVERSEER_MIN_SPAWNS and top_tier > subagent_tier:
        return "overseer-fanout", evidence

    if features.plan_mode_seen and features.post_plan_lower_tier:
        evidence["plan_mode_seen"] = True
        evidence["post_plan_lower_tier"] = True
        return "plan-high-implement-low", evidence

    if features.has_workflow:
        evidence["has_workflow"] = True
        return "workflow-heavy", evidence

    total_effort_turns = sum(features.effort_turn_counts.values())
    effort_shares = (
        {effort: count / total_effort_turns for effort, count in features.effort_turn_counts.items()}
        if total_effort_turns
        else {}
    )
    evidence["effort_shares"] = effort_shares
    qualifying_efforts = [
        effort for effort, share in effort_shares.items() if share >= _EFFORT_VARIED_MIN_SHARE
    ]
    if len(qualifying_efforts) >= 2:
        return "effort-varied", evidence

    # Fix (coordinator follow-up, WP12a diversity fixtures): chat-only
    # must be tested before single-model. A genuine chat-only session
    # (zero spawns, zero workflows, no plan-mode signal, and no tool
    # beyond Read/Grep/Glob) still resolves a real, single model family
    # from its own turns - so testing single-model first made chat-only
    # unreachable except when the model failed to resolve at all.
    if features.spawn_count == 0 and features.top_level_tool_names <= _CHAT_ONLY_TOOLS:
        evidence["top_level_tool_names"] = sorted(features.top_level_tool_names)
        return "chat-only", evidence

    known_families = {tier for tier in (top_tier, *subagent_tiers) if tier != -1}
    evidence["known_model_families"] = len(known_families)
    if len(known_families) == 1 and features.spawn_count <= _SINGLE_MODEL_MAX_SPAWNS:
        return "single-model", evidence

    return "mixed", evidence


def corpus_archetype(records: Sequence[SessionRecord]) -> tuple[str | None, dict]:
    """The majority archetype across a corpus of already-classified
    ``SessionRecord``s (``record.archetype``, set per session by a
    caller via :func:`detect_archetype`), with the vote counts as
    evidence.

    Records with no archetype set (``None``) are counted in ``sessions``
    but not in the vote. Returns ``(None, evidence)`` when nothing has an
    archetype yet, rather than raising, since a partially-classified
    corpus is a normal intermediate state.
    """
    counts: dict[str, int] = {}
    for record in records:
        if record.archetype:
            counts[record.archetype] = counts.get(record.archetype, 0) + 1
    evidence = {"sessions": len(records), "counts": counts}
    if not counts:
        return None, evidence
    # Ties break alphabetically so the result is deterministic.
    majority = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return majority, evidence


_ARCHETYPE_DESCRIPTIONS: dict[str, str] = {
    "overseer-fanout": (
        "The top-level session runs a higher-tier model than the agents it"
        " spawns, and spawns three or more of them — an overseer directing"
        " implementation work out to cheaper agents."
    ),
    "plan-high-implement-low": (
        "The session plans at a higher tier, then hands implementation to"
        " lower-tier model turns or agents once plan mode exits."
    ),
    "workflow-heavy": (
        "The session runs at least one Workflow (ultracode) run, coordinating"
        " many agents through a scripted multi-phase run rather than ad-hoc"
        " spawns."
    ),
    "effort-varied": (
        "The session mixes at least two effort levels, each accounting for a"
        " fifth or more of its turns, rather than running at one fixed effort"
        " throughout."
    ),
    "single-model": (
        "The session stays on one model family throughout and spawns at most"
        " two agents — a single operator doing the work directly."
    ),
    "chat-only": (
        "The session never spawns an agent and uses nothing beyond read-only"
        " inspection tools (Read/Grep/Glob) — a conversation, not an agentic"
        " run."
    ),
    "mixed": (
        "The session doesn't cleanly match any single archetype's evidence"
        " thresholds."
    ),
}


def describe_archetype(archetype: str) -> str:
    """One sentence describing ``archetype``, for the report."""
    return _ARCHETYPE_DESCRIPTIONS.get(archetype, f"Unrecognised archetype: {archetype!r}.")


# -- report section -----------------------------------------------------


def build_section(
    records_or_features: Sequence[Union[SessionRecord, SessionFeatures]],
) -> Section:
    """Build the "Workstyle" report section (fix item 10): one row per
    archetype, with its session count, corpus share, and description.

    Accepts either already-classified ``SessionRecord``s (archetype read
    straight from ``record.archetype``) or raw ``SessionFeatures``
    (classified here via :func:`detect_archetype`) — a caller upstream of
    full ``SessionRecord`` assembly can still get a workstyle table
    straight from extracted evidence, and one that already has
    classified records doesn't pay to re-run detection.

    A ``SessionRecord`` with no archetype set (``None`` — not yet
    classified) is counted in a note rather than a row, the same
    "don't drop it, don't guess it" posture :func:`corpus_archetype`
    already takes.
    """
    archetypes: list[str | None] = []
    for item in records_or_features:
        if isinstance(item, SessionFeatures):
            archetype, _ = detect_archetype(item)
        else:
            archetype = item.archetype
        archetypes.append(archetype)

    counts: dict[str, int] = {}
    unclassified = 0
    for archetype in archetypes:
        if archetype is None:
            unclassified += 1
        else:
            counts[archetype] = counts.get(archetype, 0) + 1

    total = len(archetypes)
    rows = [
        [archetype, count, 100.0 * count / total if total else None, describe_archetype(archetype)]
        for archetype, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    notes: list[str] = []
    if unclassified:
        plural = "s" if unclassified != 1 else ""
        notes.append(
            f"{unclassified} session{plural} had no archetype set and are excluded "
            "from the table above."
        )
    if not archetypes:
        notes.insert(0, "No sessions to classify in this window.")

    table = Table(
        name="workstyle_archetypes",
        title="Workstyle archetypes",
        columns=[
            Column(key="archetype", label="Archetype", kind="str"),
            Column(key="sessions", label="Sessions", kind="int"),
            Column(key="pct", label="Share", kind="pct"),
            Column(key="description", label="Description", kind="str"),
        ],
        rows=rows,
    )
    return Section(key="workstyle", title="Workstyle", tables=[table], notes=notes)


__all__ = [
    "SessionFeatures",
    "model_tier",
    "detect_archetype",
    "corpus_archetype",
    "describe_archetype",
    "build_section",
]
