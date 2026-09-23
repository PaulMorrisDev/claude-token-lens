"""Create a profile from a goal: each goal names the settings that move
it, and :func:`draft` proposes values for them from the report's own
tables and recommendations, each with the value in effect now, the
evidence, the trade-off and a what-if estimate (:mod:`whatif`).

A change is ticked only when the data supports it; the rest are offered
unticked, so a goal never quietly makes a quality trade for you (the
main session's model, for one, is never pre-ticked).
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import quality, whatif
from ..compaction_sim import CompactionSimThresholds
from ..fixes import LEVER_LABELS, SETTING_TEXT, already_set
from ..recommend import _NOT_OVERRIDABLE, _SKIPS_CLAUDE_MD
from ..units import Units
from .diff import _EFFECTIVE_AGENT_FIELD

TOP = whatif.TOP

#: A saving smaller than this share of what the change touches isn't
#: worth pre-ticking.
MIN_SHARE_PCT = 5.0
#: Thinking share (of output) above which a lower effort is offered.
THINKING_PCT = 20.0


@dataclass(frozen=True, slots=True)
class Goal:
    id: str
    title: str
    what: str


GOALS: tuple[Goal, ...] = (
    Goal(
        "recommendations",
        "Start from my recommendations",
        "Every setting change your current recommendations suggest, in one profile.",
    ),
    Goal(
        "subagents",
        "Spend less on subagents",
        "A cheaper model, less thinking, no CLAUDE.md or a different cache lifetime for the agents that cost most.",
    ),
    Goal(
        "models",
        "Cheaper models where it's safe",
        "The cheapest model each agent could move to, from its replies repriced at other models.",
    ),
    Goal(
        "cache",
        "Cheaper cache",
        "The cache lifetime (5 minutes or 1 hour) that would have cost least, for the main session and subagents.",
    ),
    Goal(
        "compaction",
        "Shorter conversations",
        "Summarise the conversation at the point that would have cost least, from your sessions replayed.",
    ),
    Goal(
        "thinking",
        "Less thinking where it isn't needed",
        "A lower effort for the main session or agents that spend a large share of their output thinking.",
    ),
    Goal(
        "current",
        "Start from my current settings",
        "Save the settings in effect now as a profile, to switch back to later or edit.",
    ),
)
GOAL_IDS = tuple(goal.id for goal in GOALS)


def _alias(model_id: str) -> str:
    """"claude-haiku-4-5-20251001" -> "haiku": the alias follows the
    newest model of that family."""
    for family in ("haiku", "sonnet", "opus", "fable"):
        if family in str(model_id):
            return family
    return str(model_id)


@dataclass(slots=True)
class _Draft:
    goal: Goal
    effective: dict
    effective_agents: dict
    candidates: list

    def now(self, key: str, agent: str | None):
        if agent is None:
            return self.effective.get(key)
        fields = self.effective_agents.get(agent)
        # effective_agents names its fields its own way (experimental_cache_ttl).
        return fields.get(_EFFECTIVE_AGENT_FIELD.get(key, key)) if isinstance(fields, dict) else None

    def add(self, key: str, agent: str | None, value, *, ticked: bool, evidence: str) -> None:
        if value is None or any(c["key"] == key and c["agent"] == agent for c in self.candidates):
            return
        # Started by Claude Code itself: no agent file can change them.
        if agent in _NOT_OVERRIDABLE or (key == "omitClaudeMd" and agent in _SKIPS_CLAUDE_MD):
            return
        now = self.now(key, agent)
        if now == value or already_set(key, value, now):
            return
        what, tradeoff, note = SETTING_TEXT.get(key, ("", "", ""))
        self.candidates.append(
            {
                "key": key,
                "agent": agent,
                "label": LEVER_LABELS.get(key, key),
                "now": now,
                "value": value,
                "ticked": ticked,
                "evidence": evidence,
                "what": what,
                "tradeoff": tradeoff,
                "note": note,
            }
        )


def _share(saving, base) -> float:
    saving = whatif._num(saving) or 0.0
    base = whatif._num(base) or 0.0
    return 100.0 * saving / base if base > 0 else 0.0


def _from_recommendations(draft: _Draft, recommendations, keys: set[str] | None = None, *, subagents_only=False):
    for rec in recommendations or ():
        for change in getattr(rec, "changes", ()) or ():
            if change.value is None or (keys is not None and change.key not in keys):
                continue
            if subagents_only and change.agent is None:
                continue
            draft.add(
                change.key,
                change.agent,
                change.value,
                # The main session's model is a quality trade that's yours to make.
                ticked=not (change.key == "model" and change.agent is None),
                evidence=f"Recommended: {rec.title}",
            )


def _models(draft: _Draft, tables, *, subagents_only: bool) -> None:
    worse = quality.worse_models(tables.rows("quality", "quality_by_setup"))
    for row in tables.rows("model_swap", "model_swap_by_agent_type"):
        agent = row.get("agent_type")
        best = row.get("best_cheaper_alternative_model")
        pct = whatif._num(row.get("saving_pct")) or 0.0
        if not best or pct < MIN_SHARE_PCT or (subagents_only and agent == TOP):
            continue
        if (agent, _alias(best)) in worse:
            # The quality check found this agent did worse on that model.
            continue
        who = "the main session" if agent == TOP else agent
        draft.add(
            "model",
            None if agent == TOP else agent,
            _alias(best),
            ticked=agent != TOP and pct >= 20.0,
            evidence=f"{who}'s replies in this window would have cost {pct:.0f}% less on {best}.",
        )


def _cache(draft: _Draft, tables, *, subagents_only: bool) -> None:
    rows = tables.rows("ttl", "ttl_by_agent_type")
    top = next((r for r in rows if r.get("agent_type") == TOP), None)
    if top is not None and not subagents_only:
        _cheapest_ttl(draft, "promptCacheTtl", None, [top], "the main session")
    subs = [r for r in rows if r.get("agent_type") != TOP]
    if subs and not subagents_only:
        _cheapest_ttl(draft, "subagentPromptCacheTtl", None, subs, "your subagents together")
    if subagents_only:
        for row in subs:
            _cheapest_ttl(draft, "experimental.cacheTtl", row.get("agent_type"), [row], row.get("agent_type"))


def _cheapest_ttl(draft: _Draft, key: str, agent: str | None, rows: list[dict], who: str) -> None:
    observed = sum(whatif._num(r.get("cost_observed")) or 0.0 for r in rows)
    costs = {ttl: sum(whatif._num(r.get(f"cost_all_{ttl}")) or 0.0 for r in rows) for ttl in ("5m", "1h")}
    best = min(costs, key=costs.get)
    pct = _share(observed - costs[best], observed)
    if pct < MIN_SHARE_PCT:
        return
    draft.add(
        key,
        agent,
        best,
        ticked=True,
        evidence=f"Every cache write of {who} replayed at {best} would have cost {pct:.0f}% less.",
    )


def _compaction(draft: _Draft, tables) -> None:
    rows = [r for r in tables.rows("compaction_sim", "compaction_sim_by_window") if whatif._num(r.get("cost"))]
    if not rows:
        return
    current = draft.now("autoCompactWindow", None)
    base = next((r for r in rows if str(r.get("window")).replace(",", "") == str(current)), None)
    base = base or next((r for r in rows if str(r.get("window")) == "none"), None)
    # Same limit as the compaction-window rule: a window that summarises
    # more often loses too much detail to suggest, however cheap.
    limit = CompactionSimThresholds().max_compactions_per_session
    best = min(
        (
            r
            for r in rows
            if str(r.get("window")) != "none" and (whatif._num(r.get("compactions_per_session")) or 0.0) <= limit
        ),
        key=lambda r: r["cost"],
        default=None,
    )
    if best is None or base is None:
        return
    pct = _share(base["cost"] - best["cost"], base["cost"])
    if pct < MIN_SHARE_PCT:
        return
    draft.add(
        "autoCompactWindow",
        None,
        int(str(best["window"]).replace(",", "")),
        ticked=True,
        evidence=(
            f"Your sessions replayed with summaries at {best['window']} tokens cost {pct:.0f}% less, with about "
            f"{whatif._num(best.get('compactions_per_session')) or 0:.1f} summaries per session."
        ),
    )


def _thinking(draft: _Draft, tables, *, subagents_only: bool) -> None:
    for row in tables.rows("agents", "topology_effort_by_agent_type"):
        agent = row.get("agent_type")
        share = whatif._num(row.get("thinking_share")) or 0.0
        if share < THINKING_PCT or (subagents_only and agent == TOP):
            continue
        is_top = agent == TOP
        draft.add(
            "effortLevel" if is_top else "effort",
            None if is_top else agent,
            "medium",
            ticked=False,
            evidence=(
                f"Thinking was {share:.0f}% of {'the main session' if is_top else agent}'s output. How much less "
                "a lower effort thinks isn't measured, so this isn't ticked for you."
            ),
        )


def _omit_claude_md(draft: _Draft, tables) -> None:
    for row in tables.rows("agent_startup", "agent_startup_breakdown"):
        tokens = whatif._num(row.get("claude_md")) or 0.0
        if tokens < 1000 or row.get("agent_type") == TOP:
            continue
        draft.add(
            "omitClaudeMd",
            row.get("agent_type"),
            True,
            ticked=False,
            evidence=(
                f"About {round(tokens):,} CLAUDE.md tokens at each of {int(whatif._num(row.get('spawns')) or 0)} "
                "spawns. Not ticked: move the rules it needs into its agent file first."
            ),
        )


def draft(
    goal_id: str,
    model,
    units: Units,
    *,
    effective: dict | None = None,
    effective_agents: dict | None = None,
    period: str = "",
) -> dict:
    """The candidate changes for ``goal_id``, each with its what-if row.
    Raises ``KeyError`` for an unknown goal."""
    goal = next(g for g in GOALS if g.id == goal_id) if goal_id in GOAL_IDS else None
    if goal is None:
        raise KeyError(goal_id)
    tables = whatif._Tables(model)
    d = _Draft(goal, dict(effective or {}), dict(effective_agents or {}), [])
    recommendations = getattr(model, "recommendations", ()) or ()
    if goal.id == "recommendations":
        _from_recommendations(d, recommendations)
    elif goal.id == "subagents":
        _from_recommendations(d, recommendations, {"model", "omitClaudeMd", "effort", "experimental.cacheTtl"},
                              subagents_only=True)
        _models(d, tables, subagents_only=True)
        _thinking(d, tables, subagents_only=True)
        _cache(d, tables, subagents_only=True)
        _omit_claude_md(d, tables)
    elif goal.id == "models":
        _from_recommendations(d, recommendations, {"model"})
        _models(d, tables, subagents_only=False)
    elif goal.id == "cache":
        _cache(d, tables, subagents_only=False)
    elif goal.id == "compaction":
        _compaction(d, tables)
    elif goal.id == "thinking":
        _thinking(d, tables, subagents_only=False)
    for candidate in d.candidates:
        settings, agents = _as_profile([candidate])
        candidate["estimate"] = whatif.estimate(settings, agents, model, units, period=period,
                                                current=d.effective)["rows"][0]
    ticked = [c for c in d.candidates if c["ticked"]]
    settings, agents = _as_profile(ticked)
    return {
        "goal": {"id": goal.id, "title": goal.title, "what": goal.what},
        "period": period,
        "from_current": goal.id == "current",
        "candidates": d.candidates,
        "profile": {"settings": settings, "agents": agents},
        "whatif": whatif.estimate(settings, agents, model, units, period=period, current=d.effective),
    }


def _as_profile(candidates: list[dict]) -> tuple[dict, dict]:
    settings: dict = {}
    agents: dict = {}
    for c in candidates:
        if c["agent"] is None:
            settings[c["key"]] = c["value"]
        else:
            agents.setdefault(c["agent"], {})[c["key"]] = c["value"]
    return settings, agents


def goals_list() -> list[dict]:
    return [{"id": g.id, "title": g.title, "what": g.what} for g in GOALS]


__all__ = ["GOALS", "GOAL_IDS", "draft", "goals_list"]
