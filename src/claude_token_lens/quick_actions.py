"""Quick actions: one question per token lever, each answered from the
report's own tables for the window, with the evidence as a small table
and fixes in the :mod:`fixes` shape (explainer, prompt, and a dry-run
``apply`` command where one applies).

Unlike a recommendation, a check always answers, including "nothing to
do here" (``status`` "ok") and "not enough data" ("no_data"). Checks
reuse the recommendations, the goal drafts (:mod:`profiles.goals`) and
the CLAUDE.md and skills reviews rather than adding rules of their own,
so a check and a recommendation never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import whatif
from .fixes import build_fix, build_fixes
from .model import Recommendation, SettingChange
from .profiles import goals
from .recommend import _BUILTIN_AGENT_TYPES
from .units import Units

TOP = whatif.TOP


@dataclass(slots=True)
class Context:
    model: object
    units: Units
    period: str
    config_dir: Path
    effective: dict
    effective_agents: dict


@dataclass(frozen=True, slots=True)
class Check:
    id: str
    question: str
    why: str
    run: Callable[[Context], dict]


# -- helpers ---------------------------------------------------------------


def _money(ctx: Context, usd, *, period: bool = False) -> str:
    value = whatif._num(usd)
    amount = ctx.units.money(value, period=ctx.period if period else "") if value else None
    return amount.text() if amount is not None else "none"


def _who(agent) -> str:
    return "Main session" if agent in (None, TOP) else str(agent)


def _pct(value) -> str:
    number = whatif._num(value)
    return f"{number:.0f}%" if number is not None else ""


def _table(columns: list[tuple[str, str]], rows: list[list]) -> dict | None:
    if not rows:
        return None
    return {"columns": [{"key": k, "label": label} for k, label in columns], "rows": rows}


def _result(status: str, summary: str, *, table=None, fixes=None, tips=None) -> dict:
    return {"status": status, "summary": summary, "table": table, "fixes": fixes or [], "tips": tips or []}


def _recommendations(ctx: Context, ids: set[str]) -> list:
    return [rec for rec in getattr(ctx.model, "recommendations", ()) or () if rec.id in ids]


def _rec_fixes(recs) -> list[dict]:
    out = []
    for rec in recs:
        for fix in (getattr(rec, "fixes", None) or build_fixes(rec)):
            out.append({**fix, "title": fix.get("title") or rec.title})
    return out


def _candidate_fix(ctx: Context, candidate: dict, title: str) -> dict:
    """A goal candidate as a fix: the same explainer, prompt and command
    a recommendation's change gets."""
    agent = candidate["agent"]
    estimate = candidate.get("estimate") or {}
    change = SettingChange(
        target="agent" if agent else "settings",
        key=candidate["key"],
        agent=agent,
        value=candidate["value"],
        current=candidate["now"],
        new_agent_file=bool(agent) and agent in _BUILTIN_AGENT_TYPES and agent not in ctx.effective_agents,
    )
    effect = estimate.get("effect_text") or ""
    rec = Recommendation(
        id="quick-action",
        title=title,
        scope="user",
        why=candidate["evidence"],
        estimated_saving=f"{effect}." if effect and estimate.get("saving_usd") is not None else "",
        saving_basis=estimate.get("basis") or "",
        changes=[change],
    )
    return {**build_fix(rec, change), "title": title}


def _merge_fixes(*groups: list[dict]) -> list[dict]:
    """Every fix once: the first for a (key, agent) wins, and fixes with
    no key (prompt-only advice) are kept by their prompt."""
    seen: set = set()
    out = []
    for group in groups:
        for fix in group:
            marker = (fix.get("key"), fix.get("agent")) if fix.get("key") else ("prompt", fix.get("prompt"))
            if marker in seen:
                continue
            seen.add(marker)
            out.append(fix)
    return out


def _goal(ctx: Context, goal_id: str) -> dict:
    return goals.draft(
        goal_id, ctx.model, ctx.units, effective=ctx.effective, effective_agents=ctx.effective_agents, period=ctx.period
    )


def _goal_fixes(ctx: Context, draft: dict, title: Callable[[dict], str]) -> list[dict]:
    return [_candidate_fix(ctx, c, title(c)) for c in draft["candidates"]]


# -- checks ------------------------------------------------------------------


def _models(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    rows = [r for r in tables.rows("model_swap", "model_swap_by_agent_type") if whatif._num(r.get("observed_cost"))]
    if not rows:
        return _result("no_data", "No priced replies in this window.")
    rows.sort(key=lambda r: -(whatif._num(r.get("observed_cost")) or 0))
    table = _table(
        [("agent", "Agent"), ("model", "Model used"), ("cost", "Cost"), ("cheaper", "Cheapest alternative"),
         ("saving", "Would save")],
        [
            [_who(r.get("agent_type")), r.get("observed_model") or "", _money(ctx, r.get("observed_cost")),
             r.get("best_cheaper_alternative_model") or "none cheaper",
             f"{_money(ctx, r.get('saving_usd'))} ({_pct(r.get('saving_pct'))})" if whatif._num(r.get("saving_usd"))
             else ""]
            for r in rows
        ],
    )
    draft = _goal(ctx, "models")
    fixes = _goal_fixes(ctx, draft, lambda c: f"{_who(c['agent'])}: use {c['value']}")
    if not fixes:
        return _result("ok", "Every agent is already on the cheapest model that priced lower by a useful margin.",
                       table=table)
    top = max(draft["candidates"], key=lambda c: (c["estimate"] or {}).get("saving_usd") or 0)
    return _result(
        "act",
        f"{len(fixes)} model change{'s' if len(fixes) != 1 else ''} would have cost less. The largest: {_who(top['agent'])} on "
        f"{top['value']}, {top['estimate']['effect_text'][:1].lower()}{top['estimate']['effect_text'][1:]}. A "
        "cheaper model may need more replies for hard work, so try it on one agent first.",
        table=table,
        fixes=fixes,
    )


def _effort(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    rows = [r for r in tables.rows("agents", "topology_effort_by_agent_type") if whatif._num(r.get("output_tokens"))]
    if not rows:
        return _result("no_data", "No thinking recorded in this window.")
    rows.sort(key=lambda r: -(whatif._num(r.get("thinking_share")) or 0))
    table = _table(
        [("agent", "Agent"), ("output", "Output tokens"), ("thinking", "Of which thinking")],
        [[_who(r.get("agent_type")), f"{int(whatif._num(r.get('output_tokens')) or 0):,}",
          _pct(r.get("thinking_share"))] for r in rows],
    )
    draft = _goal(ctx, "thinking")
    fixes = _merge_fixes(
        _rec_fixes(_recommendations(ctx, {"effort-mismatch"})),
        _goal_fixes(ctx, draft, lambda c: f"{_who(c['agent'])}: effort {c['value']}"),
    )
    if not fixes:
        return _result(
            "ok", f"No agent spends more than {goals.THINKING_PCT:.0f}% of its output thinking.", table=table
        )
    return _result(
        "act",
        f"{len(fixes)} of your agents or sessions spent more than {goals.THINKING_PCT:.0f}% of their output "
        "thinking. Thinking is billed as output; a lower effort thinks less, but how much less isn't measured, so "
        "check \"Your changes and what they did\" on Profiles after a few sessions.",
        table=table,
        fixes=fixes,
    )


def _compaction(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    rows = tables.rows("compaction_sim", "compaction_sim_by_window")
    if not rows:
        return _result("no_data", "No sessions long enough to replay in this window.")
    table = _table(
        [("window", "Summarise at (tokens)"), ("summaries", "Summaries per session"), ("cost", "Cost"),
         ("change", "Against your sessions as they ran")],
        [[r.get("window"), f"{whatif._num(r.get('compactions_per_session')) or 0:.1f}", _money(ctx, r.get("cost")),
          _pct(r.get("delta_pct"))] for r in rows],
    )
    draft = _goal(ctx, "compaction")
    fixes = _goal_fixes(ctx, draft, lambda c: f"Summarise at {c['value']:,} tokens")
    if not fixes:
        return _result("ok", "Your current summary point is within a few percent of the cheapest one replayed.",
                       table=table)
    [candidate] = draft["candidates"]
    return _result(
        "act",
        f"Summarising at {candidate['value']:,} tokens {candidate['estimate']['effect_text'][:1].lower()}"
        f"{candidate['estimate']['effect_text'][1:]}. Earlier summaries make each reply cheaper but drop detail.",
        table=table,
        fixes=fixes,
    )


def _cache(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    rows = [r for r in tables.rows("ttl", "ttl_by_agent_type") if whatif._num(r.get("cost_observed"))]
    if not rows:
        return _result("no_data", "No cache writes in this window.")
    table = _table(
        [("agent", "Agent"), ("gaps", "Pauses over 5 minutes"), ("now", "Cost now"), ("m5", "All 5 minutes"),
         ("h1", "All 1 hour"), ("best", "Cheaper")],
        [[_who(r.get("agent_type")), r.get("gaps_over_5m"), _money(ctx, r.get("cost_observed")),
          _money(ctx, r.get("cost_all_5m")), _money(ctx, r.get("cost_all_1h")), r.get("best_policy") or ""]
         for r in rows],
    )
    draft = _goal(ctx, "cache")
    subagents = _goal(ctx, "subagents")
    per_agent = [c for c in subagents["candidates"] if c["key"] == "experimental.cacheTtl"]
    fixes = _merge_fixes(
        _goal_fixes(ctx, draft, lambda c: f"{goals.LEVER_LABELS.get(c['key'], c['key'])}: {c['value']}"),
        [_candidate_fix(ctx, c, f"{c['agent']}: cache lifetime {c['value']}") for c in per_agent],
        _rec_fixes(_recommendations(ctx, {"ttl-switch"})),
    )
    if not fixes:
        return _result("ok", "Your cache lifetimes are within a few percent of the cheapest replayed.", table=table)
    return _result(
        "act",
        f"{len(fixes)} cache lifetime change{'s' if len(fixes) != 1 else ''} would have cost less. A 1-hour cache "
        "costs more to write but survives longer pauses; a 5-minute one is cheaper when you reply quickly.",
        table=table,
        fixes=fixes,
    )


def _tools(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    rows = tables.rows("agent_startup", "agent_startup_unused")
    if not rows:
        return _result("no_data", "No subagents started in this window.")
    table = _table(
        [("agent", "Agent"), ("spawns", "Starts"), ("mcp", "Offered MCP / used it"),
         ("skills", "Listed skills / used one")],
        [[_who(r.get("agent_type")), r.get("spawns"),
          f"{r.get('mcp_offered_spawns') or 0} / {r.get('mcp_used_spawns') or 0}",
          f"{r.get('skills_listed_spawns') or 0} / {r.get('skills_used_spawns') or 0}"] for r in rows],
    )
    ids = {"spawn-unused-mcp", "spawn-unused-skills", "spawn-read-only-tools", "baseline-bloat"}
    fixes = _rec_fixes(_recommendations(ctx, ids))
    if not fixes:
        return _result("ok", "Every agent uses the tools, MCP servers and skills it's given, or they cost little.",
                       table=table)
    return _result(
        "act",
        "Some agents start with MCP servers, skills or tools they never use, and each is sent at every start.",
        table=table,
        fixes=fixes,
    )


def _skills(ctx: Context) -> dict:
    from . import skills_review

    data = skills_review.review(
        ctx.config_dir, getattr(ctx.model, "context_files", None) or {}, ctx.units, ctx.period,
    )
    rows = data["skills"]
    if not any(r["status"] != "not listed" for r in rows):
        return _result("no_data", f"No skill listing was recorded {ctx.period}.")
    unused = sorted((r for r in rows if r["status"] == "unused"), key=lambda r: -r["listing_cost_usd"])
    table = _table(
        [("name", "Skill"), ("source", "From"), ("description", "What it is"), ("cost", "Listing cost")],
        [[r["name"], r["source_label"], r["description"], r["listing_cost_text"]] for r in unused[:20]],
    )
    if not unused:
        return _result("ok", f"Claude used every listed skill {ctx.period}.")
    return _result(
        "act",
        f"{len(unused)} skills were listed to Claude at every session and subagent start but never used "
        f"{ctx.period}. Hiding them from Claude keeps them available to you as /name.",
        table=table,
        fixes=data["fixes"] + [fix for r in unused[:5] for fix in r["fixes"]],
    )


def _claude_md(ctx: Context) -> dict:
    from . import claude_md_review

    review = claude_md_review.build_review(
        ctx.config_dir, getattr(ctx.model, "context_files", None) or {},
    )
    pairs = sorted(
        ((item, claude_md_review.file_summary(item, ctx.units, ctx.period)) for item in review.files),
        key=lambda pair: -pair[1]["cost_usd"],
    )
    if not pairs:
        return _result("no_data", "No CLAUDE.md files found.")
    summaries = [summary for _item, summary in pairs]
    table = _table(
        [("file", "File"), ("tokens", "Tokens"), ("sent", "Sent to"), ("cost", "Cost"), ("findings", "Findings")],
        [[s["path"], f"{s['tokens']:,}", s["reach_text"], s["cost_text"] or "none", len(s["findings"])]
         for s in summaries[:10]],
    )
    if not any(summary["seen"] for summary in summaries):
        return _result(
            "no_data",
            f"None of your CLAUDE.md files was seen in a session {ctx.period}, so how often each is sent isn't known.",
            table=table,
        )
    detail = claude_md_review.file_detail(pairs[0][0], ctx.units, ctx.period)
    fixes = _merge_fixes(
        detail["fixes"], _rec_fixes(_recommendations(ctx, {"spawn-claude-md", "spawn-shared-claude-md"}))
    )
    if not fixes or not summaries[0]["cost_usd"]:
        return _result("ok", "Your CLAUDE.md files are small or rarely sent.", table=table)
    return _result(
        "act",
        f"{summaries[0]['path']} costs most: {summaries[0]['tokens']:,} tokens sent to {summaries[0]['reach_text']}, "
        f"{summaries[0]['cost_text']}. Context files (or claude-token-lens review claude-md) shows every file's "
        "sections.",
        table=table,
        fixes=fixes,
    )


#: Environment levers for tool output: (tool test, env name, suggested
#: value, Claude Code's default, what it is).
_OUTPUT_LEVERS = (
    (lambda tool: tool in ("Bash", "PowerShell"), "BASH_MAX_OUTPUT_LENGTH", "15000", "30,000 characters",
     "The most characters of a shell command's output Claude Code keeps; the middle of longer output is cut."),
    (lambda tool: tool.startswith("mcp__"), "MAX_MCP_OUTPUT_TOKENS", "10000", "25,000 tokens",
     "The most tokens of one MCP tool result Claude Code keeps."),
)


def _env_fix(name: str, value: str, default: str, what: str, carry_text: str) -> dict:
    return {
        "key": name,
        "agent": None,
        "title": f"Cap {name} at {value}",
        "explainer": [
            ["What this setting controls", what],
            ["Now and after", f"Now: Claude Code's default ({default}) unless you set it. After: {value}."],
            ["Where and who it affects", "The env block of ~/.claude/settings.json: every session, in every project."],
            ["Expected effect", f"Results from these tools stayed in context and cost {carry_text}. Capping "
             "them cuts that for the largest results; the saving isn't estimated on its own."],
            ["Trade-off", "Long output is cut, which can hide an error at the end. Claude then re-runs a narrower "
             "command, which costs a reply."],
            ["How to undo it", f"Remove {name} from the env block (Claude Code shows the change before saving)."],
        ],
        "command": None,
        "command_warning": "",
        "prompt": (
            f"In ~/.claude/settings.json, add \"{name}\": \"{value}\" to the \"env\" object (create it if it's "
            "missing), keeping every other entry. It takes effect in new sessions. Show me the diff before saving. "
            "Claude Code will ask my permission to edit files under .claude; that is expected."
        ),
    }


def _tool_output(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    rows = sorted(tables.rows("carry", "carry_by_tool"), key=lambda r: -(whatif._num(r.get("carry_cost_usd")) or 0))
    if not rows:
        return _result("no_data", "No tool results in this window.")
    total = sum(whatif._num(r.get("carry_cost_usd")) or 0 for r in rows)
    table = _table(
        [("tool", "Tool"), ("results", "Results"), ("tokens", "Tokens returned"), ("turns", "Replies each stays for"),
         ("cost", "Cost of carrying them")],
        [[r.get("key"), f"{int(whatif._num(r.get('result_count')) or 0):,}",
          f"{int(whatif._num(r.get('tokens_entered')) or 0):,}", f"{whatif._num(r.get('mean_turns_carried')) or 0:.0f}",
          _money(ctx, r.get("carry_cost_usd"))] for r in rows[:10]],
    )
    fixes = []
    for matches, name, value, default, what in _OUTPUT_LEVERS:
        cost = sum(whatif._num(r.get("carry_cost_usd")) or 0 for r in rows if matches(str(r.get("key") or "")))
        if total and cost / total >= 0.05:
            fixes.append(_env_fix(name, value, default, what, _money(ctx, cost, period=True)))
    tips = []
    read = next((r for r in rows if r.get("key") == "Read"), None)
    if read and total and (whatif._num(read.get("carry_cost_usd")) or 0) / total >= 0.1:
        tips.append({
            "title": "Point Claude at the part of a file you mean",
            "text": "File reads are the largest thing carried in your context. Naming the function or line range "
                    "(\"read handle_request in api.py\") keeps whole files out of every later reply.",
        })
    if not fixes and not tips:
        return _result("ok", "No one tool's output dominates your context.", table=table)
    return _result(
        "act",
        f"Carrying tool results in context cost {_money(ctx, total, period=True)}. "
        "Every result is re-read on every later reply until a summary drops it.",
        table=table,
        fixes=fixes,
        tips=tips,
    )


_HABIT_RECS = {
    "batch-instructions", "long-tool-waits", "notification-invalidation", "agent-report-size", "spawn-task-prompt",
    "cache-read-dominance", "limit-pressure", "long-context-share", "subagent-volume", "discovery-share",
}


def _habits(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    causes = [r for r in tables.rows("waste", "waste_by_cause") if whatif._num(r.get("turns"))]
    table = _table(
        [("cause", "Replies that went nowhere"), ("turns", "Replies"), ("cost", "Cost"), ("lever", "What helps")],
        [[r.get("cause"), r.get("turns"), _money(ctx, r.get("cost_usd")), r.get("lever") or ""] for r in causes],
    )
    recs = _recommendations(ctx, _HABIT_RECS)
    tips = [{"title": rec.title, "text": rec.action or rec.why} for rec in recs]
    fixes = _rec_fixes(recs)
    if not recs and not causes:
        return _result("no_data", "Not enough sessions in this window.")
    if not recs:
        return _result("ok", "No habit stands out as costing tokens.", table=table)
    return _result(
        "act",
        f"{len(recs)} way{'s' if len(recs) != 1 else ''} of working cost tokens {ctx.period}. These are habits, not "
        "settings: nothing changes unless you change how you work.",
        table=table,
        fixes=fixes,
        tips=tips,
    )


CHECKS: tuple[Check, ...] = (
    Check("models", "Is each agent on the cheapest model that does the job?",
          "Every reply is priced by its model; a cheaper model for routine agents is usually the largest saving.",
          _models),
    Check("effort", "Is anything thinking more than the work needs?",
          "Thinking is billed as output, the most expensive kind of token.", _effort),
    Check("compaction", "When should conversations be summarised?",
          "Every reply re-reads the whole conversation, so the point it's summarised at sets the cost of each reply.",
          _compaction),
    Check("cache", "Which cache lifetime is cheaper for you?",
          "A 5-minute cache is cheaper to write; a 1-hour one survives longer pauses without rebuilding.", _cache),
    Check("tools", "Do agents carry tools, MCP servers or skills they never use?",
          "Everything an agent is offered is sent each time it starts, used or not.", _tools),
    Check("skills", "Which skills are listed to Claude but never used?",
          "Each skill's name and description is sent at every session and subagent start.", _skills),
    Check("claude-md", "Which CLAUDE.md files cost most?",
          "CLAUDE.md files are sent at the start of every session and most subagents.", _claude_md),
    Check("tool-output", "Do tool results fill your context?",
          "A tool's output stays in the conversation and is re-read on every later reply.", _tool_output),
    Check("habits", "Do any habits cost tokens?",
          "Pauses, retries and long reports cost tokens that no setting can save.", _habits),
)
CHECK_IDS = tuple(check.id for check in CHECKS)


def run(check_id: str, ctx: Context) -> dict:
    """One check's full answer. Raises ``KeyError`` for an unknown id."""
    check = next((c for c in CHECKS if c.id == check_id), None)
    if check is None:
        raise KeyError(check_id)
    return {"id": check.id, "question": check.question, "why": check.why, "period": ctx.period, **check.run(ctx)}


def run_all(ctx: Context) -> list[dict]:
    """Every check's status and summary (the list view)."""
    out = []
    for check in CHECKS:
        result = run(check.id, ctx)
        out.append({key: result[key] for key in ("id", "question", "why", "status", "summary")}
                   | {"fix_count": len(result["fixes"]), "tip_count": len(result["tips"])})
    return out


def render_markdown(result: dict) -> str:
    lines = [f"## {result['question']}", "", result["summary"], ""]
    table = result.get("table")
    if table:
        lines.append("| " + " | ".join(c["label"] for c in table["columns"]) + " |")
        lines.append("|" + "---|" * len(table["columns"]))
        for row in table["rows"]:
            lines.append("| " + " | ".join(str(cell).replace("|", "/") for cell in row) + " |")
        lines.append("")
    for tip in result.get("tips") or ():
        lines += [f"- **{tip['title']}**: {tip['text']}"]
    if result.get("tips"):
        lines.append("")
    for fix in result.get("fixes") or ():
        lines += [f"### {fix.get('title') or fix.get('key')}", ""]
        for heading, text in fix.get("explainer") or ():
            lines.append(f"- **{heading}**: {text}")
        if fix.get("prompt"):
            lines += ["", "Prompt for Claude:", "", "```text", fix["prompt"], "```"]
        if fix.get("command"):
            lines += ["", "Command:", "", "```bash", fix["command"], "```"]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["CHECKS", "CHECK_IDS", "Context", "render_markdown", "run", "run_all"]
