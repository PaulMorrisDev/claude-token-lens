"""Create a profile from a goal: each goal names the settings that move
it, and :func:`draft` proposes values for them from the report's own
tables and recommendations, each with the value in effect now, the
evidence, the trade-off and a what-if estimate (:mod:`whatif`).

A change is ticked only when the data supports it; the rest are offered
unticked, so a goal never quietly makes a quality trade for you (the
main session's model, for one, is never pre-ticked).

The ``tasks`` goal needs metrics capture: it reads the Work habits
section's ``habits_setups`` table (the model and effort each kind of
task Claude reported ran on, and how often it went well) and drafts the
cheapest setup that went about as well as your usual one.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import habits, quality, whatif
from ..compaction_sim import CompactionSimThresholds
from ..fixes import LEVER_LABELS, SETTING_TEXT, already_set
from ..recommend import _NOT_OVERRIDABLE, _SKIPS_CLAUDE_MD
from ..units import Units
from . import catalogue
from .diff import _EFFECTIVE_AGENT_FIELD
from .schema import _EFFORT_LEVELS

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
        "tasks",
        "A profile for one kind of task",
        "The cheapest model and effort that went about as well as your usual setup, for one kind of task "
        "Claude reported. Needs metrics capture.",
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
    worse.update({key: row for key, row in quality.retried_models(tables.rows("quality", "quality_retried")).items()
                  if key not in worse})
    # Metrics capture: agents whose runs said they needed a larger model,
    # or whose work was mostly reported hard (advice._merge_model_tier).
    unfit = habits.unfit_agents(tables.rows("habits", "habits_agents"))
    for row in tables.rows("model_swap", "model_swap_by_agent_type"):
        agent = row.get("agent_type")
        best = row.get("best_cheaper_alternative_model")
        pct = whatif._num(row.get("saving_pct")) or 0.0
        if not best or pct < MIN_SHARE_PCT or (subagents_only and agent == TOP):
            continue
        if (agent, _alias(best)) in worse or agent in unfit:
            # The quality check found this agent did worse on that model,
            # or its runs on it were often retried on a larger one; or
            # Claude reported its work needed a larger model.
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
    # Metrics capture: what each agent type's runs said about CLAUDE.md.
    said = {row.get("agent_type"): row for row in tables.rows("habits", "habits_agents")}
    for row in tables.rows("agent_startup", "agent_startup_breakdown"):
        tokens = whatif._num(row.get("claude_md")) or 0.0
        agent = row.get("agent_type")
        if tokens < 1000 or agent == TOP:
            continue
        told = said.get(agent) or {}
        used = int(whatif._num(told.get("rules_used")) or 0)
        unused = int(whatif._num(told.get("rules_unused")) or 0)
        if used > unused:
            # Most of its runs that said, said they used it.
            continue
        evidence = f"About {round(tokens):,} CLAUDE.md tokens at each of {int(whatif._num(row.get('spawns')) or 0)} spawns."
        if unused:
            evidence += f" {unused} of the {used + unused} runs that said, said they didn't use it."
        else:
            evidence += " Not ticked: move the rules it needs into its agent file first."
        draft.add("omitClaudeMd", agent, True, ticked=unused > used, evidence=evidence)


def _setups_by_task(tables) -> dict[str, list[dict]]:
    """``habits_setups``' all-levels rows per kind of task, the most-used
    task first (the table's own order)."""
    by_task: dict[str, list[dict]] = {}
    for row in tables.rows("habits", "habits_setups"):
        if row.get("level") == "all" and row.get("task"):
            by_task.setdefault(str(row["task"]), []).append(row)
    return by_task


def _setup_text(row: dict) -> str:
    effort = row.get("effort")
    return f"{row.get('model')}" + (f" at {effort} effort" if effort and effort != "default" else "")


def _tasks(draft: _Draft, tables, task: str | None) -> tuple[list[str], str | None, str]:
    """The main session's model and effort for ``task`` (or, without
    one, the first kind of task that has a cheaper setup): the kinds of
    task there are, the one drafted, and a note."""
    by_task = _setups_by_task(tables)
    tasks = list(by_task)
    if not tasks:
        return [], None, (
            "No kind of task has been reported yet. Turn on metrics capture at Essentials or above on the "
            "Capture tab, then come back after a week or so of work."
        )
    if task not in by_task:
        task = next((t for t in tasks if any(r.get("verdict") == "cheaper" for r in by_task[t])), tasks[0])
    rows = by_task[task]
    usual = next((r for r in rows if r.get("verdict") == "usual"), rows[0])
    cheaper = next((r for r in rows if r.get("verdict") == "cheaper"), None)
    if cheaper is None:
        note = (
            f"Your usual setup for {task} work is {_setup_text(usual)}. No cheaper setup went as well over at "
            f"least {habits.MIN_GROUP} messages yet."
        )
    else:
        evidence = (
            f"For {task} work, {_setup_text(cheaper)} cost {whatif._num(cheaper.get('saving_pct')) or 0:.0f}% less "
            f"a message than your usual {_setup_text(usual)}, and went well "
            f"{whatif._num(cheaper.get('ok_pct')) or 0:.0f}% of the time against "
            f"{whatif._num(usual.get('ok_pct')) or 0:.0f}% ({int(whatif._num(cheaper.get('cycles')) or 0)} and "
            f"{int(whatif._num(usual.get('cycles')) or 0)} messages), compared level for level. They still ran on "
            "different work, so it's a lead, not proof."
        )
        if cheaper.get("model") != usual.get("model") and cheaper.get("model") in habits._FAMILIES:
            draft.add("model", None, cheaper["model"], ticked=False, evidence=evidence)
        if cheaper.get("effort") != usual.get("effort") and cheaper.get("effort") in _EFFORT_LEVELS:
            draft.add("effortLevel", None, cheaper["effort"], ticked=True, evidence=evidence)
        note = f"Save it, then launch Claude with it when you start {task} work."
    profile_id = catalogue.task_profile(task)
    if profile_id is not None:
        note += f" The catalogue profile {profile_id} is also a starting point for this kind of task."
    return tasks, task, note


def _task_agents(draft: _Draft, tables, task: str) -> None:
    """Cheaper-model candidates for the agent types that most often
    answered ``task``'s work (metrics capture's ``habits_agents_by_task``),
    vetoed exactly as ``_models`` vetoes its corpus-wide draft: a setup
    the quality check found worse, one often retried for the model, or
    an agent whose runs said, or were mostly, hard work
    (``habits.unfit_agents``)."""
    worse = quality.worse_models(tables.rows("quality", "quality_by_setup"))
    worse.update({key: row for key, row in quality.retried_models(tables.rows("quality", "quality_retried")).items()
                  if key not in worse})
    unfit = habits.unfit_agents(tables.rows("habits", "habits_agents"))
    rows = [r for r in tables.rows("habits", "habits_agents_by_task") if r.get("task") == task]
    for row in sorted(rows, key=lambda r: -(whatif._num(r.get("runs")) or 0.0)):
        agent = row.get("agent_type")
        best = row.get("cheaper_model")
        pct = whatif._num(row.get("cheaper_saving_pct")) or 0.0
        if not agent or not best or pct < MIN_SHARE_PCT:
            continue
        if (agent, best) in worse or agent in unfit:
            continue
        draft.add(
            "model",
            agent,
            best,
            ticked=pct >= 20.0,
            evidence=f"{agent}'s {task} runs in this window would have cost {pct:.0f}% less on {best}.",
        )


def _task_share(tables, task: str) -> float | None:
    """This task's share (%) of everything ``habits_by_task`` covers in
    the window, from its own ``cost`` column against the ``all`` row's.
    Used to scale down a main-session estimate that reprices the whole
    window (``whatif`` has no notion of a task). ``None`` without the
    data to compare."""
    by_task = {r.get("task"): r for r in tables.rows("habits", "habits_by_task")}
    task_row, all_row = by_task.get(task), by_task.get("all")
    total = whatif._num(all_row.get("cost")) if all_row else None
    if task_row is None or not total:
        return None
    return 100.0 * (whatif._num(task_row.get("cost")) or 0.0) / total


def _task_agent_share(tables, task: str, agent: str) -> float | None:
    """This task's share (%) of ``agent``'s total cost, from
    ``habits_agents_by_task`` (runs at this task, times its cost per
    run) against ``habits_agents``' own total. ``None`` without the data
    to compare."""
    row = next(
        (r for r in tables.rows("habits", "habits_agents_by_task") if r.get("task") == task and r.get("agent_type") == agent),
        None,
    )
    total_row = tables.row("habits", "habits_agents", agent)
    total = whatif._num(total_row.get("cost")) if total_row else None
    if row is None or not total:
        return None
    task_cost = (whatif._num(row.get("runs")) or 0.0) * (whatif._num(row.get("avg_cost")) or 0.0)
    return 100.0 * task_cost / total


def _scale_estimate(row: dict, pct: float | None, units: Units, period: str) -> dict:
    """A ``whatif`` row reprices *all* of a setup's observed work in the
    window, but a task's draft is for that task's share of it alone.
    Scale the saving down to ``pct``; without a clean share to scale by,
    drop the number rather than leave the unscaled (too large) one."""
    row = dict(row)
    if row.get("saving_usd") is None:
        return row
    if pct is None:
        row["saving_usd"] = None
        row["fidelity"] = "none"
        row["effect_text"] = "Not estimated"
        row["basis"] = "Not estimated: no per-task cost to scale this window's reprice by."
        return row
    row["saving_usd"] = round(row["saving_usd"] * pct / 100.0, 6)
    row["effect_text"] = whatif._effect_text(row["saving_usd"], units, period)
    row["basis"] = row.get("basis", "") + f" Scaled to this task's {pct:.0f}% share of what was repriced above."
    return row


def _task_share_for(tables, task: str, agent: str | None) -> float | None:
    return _task_share(tables, task) if agent is None else _task_agent_share(tables, task, agent)


def _scale_whatif(result: dict, tables, task: str, units: Units, period: str) -> dict:
    """Scale every row of a combined ``whatif.estimate`` result to
    ``task``'s share, and recompute the total from the scaled rows."""
    rows = [_scale_estimate(row, _task_share_for(tables, task, row.get("agent")), units, period) for row in result["rows"]]
    estimated = [row for row in rows if row["saving_usd"] is not None]
    total = sum(row["saving_usd"] for row in estimated)
    out = dict(result)
    out["rows"] = rows
    out["total_usd"] = round(total, 6)
    out["total_text"] = whatif._effect_text(total, units, period) if estimated else ""
    out["estimated"] = len(estimated)
    out["not_estimated"] = len(rows) - len(estimated)
    return out


def draft(
    goal_id: str,
    model,
    units: Units,
    *,
    effective: dict | None = None,
    effective_agents: dict | None = None,
    period: str = "",
    task: str | None = None,
) -> dict:
    """The candidate changes for ``goal_id``, each with its what-if row.
    Raises ``KeyError`` for an unknown goal. ``task``: for the ``tasks``
    goal, the kind of task to draft for (the first with a cheaper setup
    when it's missing or not in the data)."""
    goal = next(g for g in GOALS if g.id == goal_id) if goal_id in GOAL_IDS else None
    if goal is None:
        raise KeyError(goal_id)
    tables = whatif._Tables(model)
    d = _Draft(goal, dict(effective or {}), dict(effective_agents or {}), [])
    recommendations = getattr(model, "recommendations", ()) or ()
    tasks: list[str] = []
    note = None
    if goal.id == "tasks":
        tasks, task, note = _tasks(d, tables, task)
        if task is not None:
            _task_agents(d, tables, task)
    else:
        task = None
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
        estimate = whatif.estimate(settings, agents, model, units, period=period, current=d.effective)["rows"][0]
        if goal.id == "tasks" and task is not None:
            # whatif reprices all of that setup's work in the window;
            # a task's own draft only covers its share of it.
            estimate = _scale_estimate(estimate, _task_share_for(tables, task, candidate["agent"]), units, period)
        candidate["estimate"] = estimate
    ticked = [c for c in d.candidates if c["ticked"]]
    settings, agents = _as_profile(ticked)
    combined = whatif.estimate(settings, agents, model, units, period=period, current=d.effective)
    if goal.id == "tasks" and task is not None:
        combined = _scale_whatif(combined, tables, task, units, period)
    return {
        "goal": {"id": goal.id, "title": goal.title, "what": goal.what},
        "period": period,
        "from_current": goal.id == "current",
        "tasks": tasks,
        "task": task,
        "note": note,
        "candidates": d.candidates,
        "profile": {"settings": settings, "agents": agents},
        "whatif": combined,
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
