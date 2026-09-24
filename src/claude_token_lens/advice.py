"""The plain-language layer over :func:`recommend.recommend`'s rules.

The rules decide *whether* something is worth saying and cite the table
cells that prove it. This module decides *how it is said* and what to
change, in one place, so every card reads the same way:

- **Consolidation.** Rules that look at the same setting from different
  angles can disagree. ``compaction-window`` (a modelled sweep that
  weighs both sides) replaces ``compaction-churn`` ("summaries happen too
  often") and takes the setting away from ``long-context-share`` ("the
  context is too large"), which then keeps only its workflow advice.
  ``model-tier`` fires once per agent type; its cards are merged into one
  with a change per agent type, largest saving first.
- **Plain words.** Each recommendation gets a plain title, an action, a
  ``why`` sentence and, where a setting is involved, ``changes``
  (:class:`~claude_token_lens.model.SettingChange`) with the current
  value from the latest config snapshot.
- **Already applied.** The rules measure the whole period, so a change
  made part-way through it would still be offered at its full saving. A
  change the current config already makes is left out, and a card with
  nothing left to change is dropped.
- **Amounts** follow the billing mode (:class:`~claude_token_lens.units.Units`).

Titles and actions are rewritten here, not in the rules, so the rules'
own modules (and their unit tests) keep their wording and evidence.
House style: ``docs/writing-help.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from . import model_gate, whatif
from .fixes import already_set
from .model import Recommendation, ReportModel, SettingChange
from .snapshots import Snapshot, effective_config
from .units import NO_LIMIT_SHARE_HINT, Units

#: Agent types Claude Code starts itself (workflow scripts, forks): no
#: agent file can change them, so no per-agent change is offered.
NOT_OVERRIDABLE = frozenset({"workflow-subagent", "fork", "unknown", "(unknown)"})

_PERIOD = "over the period in this report"
_UNKNOWN = "unknown (no config snapshot yet)"


@dataclass(slots=True)
class _Context:
    report: ReportModel
    snapshot: Snapshot | None
    units: Units | None

    def cell(self, section_key: str, table_name: str, row_key, column_key: str):
        for section in self.report.sections:
            if section.key != section_key:
                continue
            for table in section.tables:
                if table.name != table_name:
                    continue
                keys = [c.key for c in table.columns]
                if column_key not in keys:
                    return None
                idx = keys.index(column_key)
                for row in table.rows:
                    if row and row[0] == row_key and idx < len(row):
                        return row[idx]
        return None

    def setting_now(self, key: str):
        if self.snapshot is None:
            return _UNKNOWN
        return effective_config(self.snapshot).get(key)

    def agent_entry(self, agent_type: str) -> dict | None:
        if self.snapshot is None:
            return None
        agents = self.snapshot.data.get("agents")
        entry = agents.get(agent_type) if isinstance(agents, dict) else None
        return entry if isinstance(entry, dict) else None

    def agent_now(self, agent_type: str, key: str):
        if self.snapshot is None:
            return _UNKNOWN
        entry = self.agent_entry(agent_type)
        return entry.get(key) if entry is not None else None

    def agent_scope(self, agent_type: str) -> tuple[str, bool]:
        """``(scope, has_file)`` for an agent-file change."""
        entry = self.agent_entry(agent_type)
        if entry is not None:
            return ("repo" if entry.get("source") == "project" else "user"), True
        from .recommend import _agent_has_frontmatter

        return "user", _agent_has_frontmatter(agent_type, self.snapshot)

    def money(self, usd, *, prefix: str = "") -> str:
        if self.units is None or not isinstance(usd, (int, float)) or usd <= 0:
            return ""
        amount = self.units.money(float(usd), period=_PERIOD)
        if amount is None:
            return ""
        # UX-2: Amount.phrase avoids "About about X% of your weekly
        # usage limit" -- a subscription's own share text already opens
        # with "about" (units.Units.money), so a plain f"{prefix}{...}"
        # concatenation here used to double it (finding F3).
        text = f"{amount.phrase(prefix)}."
        return text[:1].upper() + text[1:]

    def basis(self, text: str) -> str:
        if self.units is None:
            return text
        probe = self.units.money(1.0)
        if probe is not None and probe.basis == NO_LIMIT_SHARE_HINT:
            return f"{text} {probe.basis}"
        return text


def _evidence_value(rec: Recommendation, label_part: str):
    for label, value, _table, _row in rec.evidence:
        if label_part in label:
            return value
    return None


def _tokens(value) -> str:
    return f"{value:,.0f}" if isinstance(value, (int, float)) else str(value)


def _family_alias(model_id: str) -> str:
    """``sonnet``/``haiku``/``opus``/``fable`` for a model id of that
    family, so an agent follows the family's current model; any other id
    as is."""
    for family in ("haiku", "sonnet", "opus", "fable"):
        if family in model_id:
            return family
    return model_id


def _who(agent_type: str) -> str:
    return "your main session" if agent_type == "top-level" else agent_type


def _percent(_saving, rec: Recommendation) -> str:
    pct = _evidence_value(rec, "Ceiling saving (%)")
    return f"up to {pct:.0f}% less" if isinstance(pct, (int, float)) else "less"


# -- consolidation -------------------------------------------------------------


def _merge_model_tier(recs: list[Recommendation], ctx: _Context) -> list[Recommendation]:
    tier = [r for r in recs if r.id == "model-tier"]
    if not tier:
        return recs
    rest = [r for r in recs if r.id != "model-tier"]
    tables = whatif._Tables(ctx.report)
    # Metrics capture: agents whose runs said a larger model would suit,
    # whose work was mostly reported hard, or that were retried for the
    # model. A veto only: a "smaller would do" never adds a suggestion.
    worse, retried, unfit = model_gate.raw(tables)
    left_out: list[str] = []
    rows = []
    for rec in tier:
        agent = rec.agent_type or "top-level"
        if agent in NOT_OVERRIDABLE:
            continue
        alt = ctx.cell("model_swap", "model_swap_by_agent_type", agent, "best_cheaper_alternative_model")
        saving = ctx.cell("model_swap", "model_swap_by_agent_type", agent, "saving_usd")
        observed = ctx.cell("model_swap", "model_swap_by_agent_type", agent, "observed_model")
        if not alt:
            continue
        now = ctx.setting_now("model") if agent == "top-level" else ctx.agent_now(agent, "model")
        if already_set("model", _family_alias(alt), now):
            # Already on the cheaper model; the saving is from before the change.
            continue
        if agent in unfit:
            left_out.append(f"{_who(agent)} ({unfit[agent]})")
            continue
        if (agent, _family_alias(alt)) in worse:
            # The quality section found this agent did worse on that model.
            left_out.append(f"{_who(agent)} (did worse on {_family_alias(alt)})")
            continue
        if (agent, _family_alias(alt)) in retried:
            # Its runs on that model were often retried on a larger one.
            left_out.append(f"{_who(agent)} ({retried[(agent, _family_alias(alt))]['reason']})")
            continue
        rows.append((rec, agent, alt, saving if isinstance(saving, (int, float)) else 0.0, observed))
    if not rows:
        return rest
    rows.sort(key=lambda r: -r[3])
    changes = []
    evidence = []
    for rec, agent, alt, saving, observed in rows:
        evidence.extend(rec.evidence)
        now = observed or "unknown"
        if agent == "top-level":
            current = ctx.setting_now("model")
            changes.append(
                SettingChange(
                    target="settings",
                    key="model",
                    value=_family_alias(alt),
                    current=current if current not in (None, _UNKNOWN) else f"not set (used {now})",
                    note="This changes the model for your main session in every project.",
                    scope=_advice_scope(rec.scope),
                    saving=ctx.money(saving, prefix="At most "),
                )
            )
            continue
        scope, has_file = ctx.agent_scope(agent)
        current = ctx.agent_now(agent, "model")
        changes.append(
            SettingChange(
                target="agent",
                key="model",
                agent=agent,
                value=_family_alias(alt),
                current=current if current not in (None, _UNKNOWN) else f"not set (used {now})",
                new_agent_file=not has_file,
                scope="managed" if rec.scope == "managed" else scope,
                # PROF-02: unlike a settings change (which `apply --launch`
                # can scope to one session via a --settings overlay), an
                # agent frontmatter edit has no session-only path -- Claude
                # Code's --agents flag would need the agent's full prompt
                # body inline, which this dashboard never reads or copies
                # (Assumption: nobody wants their agent prompts round-
                # tripped through a savings estimate). So it's written
                # once and then applies to every run of that agent from
                # then on: labelled as such, and given the plain saving
                # figure rather than the "At most" session ceiling used
                # for a change that might only be tried for a session.
                note="Persistent: affects every task this agent runs, not just one session.",
                saving=ctx.money(saving),
            )
        )
    total = sum(r[3] for r in rows)
    top = rows[0]
    merged = Recommendation(
        id="model-tier",
        severity="advice",
        category="settings",
        title="A cheaper model could do some of this work",
        why=(
            f"{_who(top[1]).capitalize()} ran on {top[4] or 'a larger model'}, and the same work "
            f"priced at {_family_alias(top[2])} would cost {_percent(top[3], top[0])}."
            if len(rows) == 1
            else f"{len(rows)} of your agent types ran on a larger model than their work may need. "
            f"The biggest saving is {_who(top[1])}."
        )
        + (
            f" Left out: {', '.join(left_out)}."
            if left_out
            else ""
        ),
        action=(
            "Try the cheaper model on a few tasks and compare the results before keeping it."
            if len(rows) == 1
            else "Move one agent type to the cheaper model first, compare its results for a few days, "
            "then decide about the rest."
        ),
        lever="model",
        scope=tier[0].scope,
        evidence=evidence,
        changes=changes,
        estimated_saving=ctx.money(total, prefix="At most "),
        saving_basis=ctx.basis(
            "Worked out by pricing the same tokens at the cheaper model's list price. A smaller model may "
            "need more replies or fail some tasks, so this is a ceiling, not a forecast."
        ),
        saving_usd=total,
    )
    return rest + [merged]


def _consolidate_compaction(recs: list[Recommendation], ctx: _Context) -> list[Recommendation]:
    window = next((r for r in recs if r.id == "compaction-window"), None)
    if window is not None:
        floor = window.title.rsplit(" ", 1)[-1].replace(",", "")
        current = ctx.setting_now("autoCompactWindow")
        if floor.isdigit() and isinstance(current, int) and current <= int(floor):
            # Already summarising at or below the modelled window.
            recs = [r for r in recs if r is not window]
            window = None
    churn = next((r for r in recs if r.id == "compaction-churn"), None)
    if window is not None:
        recs = [r for r in recs if r.id != "compaction-churn"]
        churn = None
    for rec in recs:
        if rec.id == "long-context-share" and (window is not None or churn is not None):
            # The setting belongs to the rule that weighs both sides.
            rec.lever = None
            rec.category = "workflow"
    return recs


def _drop_applied(recs: list[Recommendation]) -> list[Recommendation]:
    """Leave out changes the current config already makes. The rules
    measure the whole period, so a change made part-way through it still
    shows its full saving; a card whose every change is already in effect
    is dropped."""
    out = []
    for rec in recs:
        if rec.changes:
            pending = [c for c in rec.changes if not already_set(c.key, c.value, c.current)]
            if not pending:
                continue
            rec.changes = pending
        out.append(rec)
    return out


# -- per-rule wording ------------------------------------------------------------

#: ``compaction_sim._scope_and_lever_note`` (compaction_sim.py is outside
#: this work package's file list -- P5 owns it) still returns ``"project"``
#: for a value set at either the project_shared or the project_local
#: layer, one token short of this module's/``fixes.py``'s ``"repo"``/
#: ``"project-local"`` split. Folded to ``"repo"`` here so every
#: ``SettingChange`` this module builds carries a scope ``fixes.py``'s
#: ``_SETTINGS_WHERE``/``command_for`` actually recognise -- COV-01's
#: project-local precision is still lost for ``compaction-window``
#: specifically (it can name the wrong of the two project-scoped files
#: when the value is actually project-local); every other rule's scope
#: (``recommend.py``'s own ``_lever_scope``, fixed for COV-01) already
#: distinguishes the two correctly.
_SCOPE_ALIASES = {"project": "repo"}


def _advice_scope(rec_scope: str) -> str:
    """COV-01: the scope a ``SettingChange`` this module builds should
    carry, translated from whichever vocabulary the rule that produced
    ``rec`` uses into ``fixes.py``'s own user/project-local/repo/managed
    four-way split. Every call site below used to hardcode
    ``"managed" if rec.scope == "managed" else "user"``, silently
    discarding a rule's own already-correct "a higher layer overrides
    this" finding (recommend.py's ``_lever_scope``) whenever it wasn't
    exactly "managed" -- the flattening finding D3/D5 describe."""
    return _SCOPE_ALIASES.get(rec_scope, rec_scope)


def _explain_compaction_window(rec: Recommendation, ctx: _Context) -> None:
    label = rec.title.rsplit(" ", 1)[-1]
    value = int(label.replace(",", "")) if label.replace(",", "").isdigit() else None
    saving = _evidence_value(rec, "after rediscovery correction")
    rec.title = f"Summarising the main session at {label} tokens would cost less"
    rec.why = (
        "Every reply re-reads the whole conversation, so a long main session gets more expensive with each "
        f"reply. A summary at {label} tokens resets that."
    )
    rec.action = (
        f"Set autoCompactWindow to {label}. Claude Code then summarises the main session a little before its "
        "context reaches that size."
    )
    rec.changes = [
        SettingChange(
            target="settings",
            key="autoCompactWindow",
            value=value,
            current=ctx.setting_now("autoCompactWindow"),
            suggested=f"{label} tokens",
            scope=_advice_scope(rec.scope),
        )
    ]
    rec.estimated_saving = ctx.money(saving, prefix="About ")
    rec.saving_usd = saving if isinstance(saving, (int, float)) else None
    rec.saving_basis = ctx.basis(
        "Modelled by replaying your main sessions with summaries at this size, shaped like your past ones: "
        "the summary itself, re-caching the reply after it, and an allowance for re-reading files. Not measured."
    )


def _explain_compaction_churn(rec: Recommendation, ctx: _Context) -> None:
    mean = _evidence_value(rec, "Compactions per session")
    rec.title = "Your main sessions are summarised often"
    rec.why = (
        f"Main sessions were summarised {mean:.1f} times each on average. " if isinstance(mean, (int, float)) else ""
    ) + "Each summary rewrites the cache and can drop detail Claude then has to find again."
    rec.action = "Raise autoCompactWindow so summaries happen less often, or start a fresh session between tasks."
    rec.changes = [
        SettingChange(
            target="settings",
            key="autoCompactWindow",
            current=ctx.setting_now("autoCompactWindow"),
            suggested="a larger window than now, so summaries happen less often",
            scope=_advice_scope(rec.scope),
        )
    ]


def _explain_long_context_share(rec: Recommendation, ctx: _Context) -> None:
    share = _evidence_value(rec, "huge-context")
    p90 = _evidence_value(rec, "p90")
    rec.title = "Your main session's context is running large"
    parts = []
    if isinstance(share, (int, float)):
        parts.append(f"{share:.0f}% of what replies read back from the cache came from very large contexts.")
    if isinstance(p90, (int, float)):
        parts.append(f"One session in ten grew past {p90:,.0f} tokens.")
    parts.append("Every reply re-reads all of it.")
    rec.why = " ".join(parts)
    if rec.lever is None:
        rec.action = (
            "Send searches and exploration to a subagent, whose context is thrown away when it finishes, and "
            "start a fresh session when you switch tasks."
        )
        rec.changes = []
        return
    rec.action = (
        "Summarise the main session sooner (a smaller autoCompactWindow), and send exploration to subagents."
    )
    rec.changes = [
        SettingChange(
            target="settings",
            key="autoCompactWindow",
            current=ctx.setting_now("autoCompactWindow"),
            suggested="a smaller window than now, so the main session is summarised sooner",
            scope=_advice_scope(rec.scope),
        )
    ]


def _explain_ttl_switch(rec: Recommendation, ctx: _Context) -> None:
    agent = rec.agent_type or "top-level"
    verdict = _evidence_value(rec, "TTL recommendation")
    target = "1h" if isinstance(verdict, str) and verdict.endswith("1h") else "5m"
    saving = ctx.cell("ttl", "ttl_by_agent_type", agent, "saving_usd")
    who = "your main session" if agent == "top-level" else agent
    lifetime = "1 hour" if target == "1h" else "5 minutes"
    rec.title = f"A {'1-hour' if target == '1h' else '5-minute'} cache lifetime would suit {who} better"
    rec.why = (
        f"At the pauses {who} actually takes between replies, a {lifetime} cache would have cost less than "
        "the one it used."
    )
    rec.action = f"Set {who}'s cache lifetime to {lifetime} ({target})."
    if agent == "top-level":
        change = SettingChange(target="settings", key="promptCacheTtl", value=target, current=ctx.setting_now("promptCacheTtl"))
    else:
        scope, has_file = ctx.agent_scope(agent)
        change = SettingChange(
            target="agent",
            key="experimental.cacheTtl",
            agent=agent,
            value=target,
            current=ctx.agent_now(agent, "experimental.cacheTtl"),
            new_agent_file=not has_file,
            scope=scope,
        )
    if rec.scope == "managed":
        change.scope = "managed"
    rec.changes = [change]
    rec.estimated_saving = ctx.money(saving, prefix="About ")
    rec.saving_usd = saving if isinstance(saving, (int, float)) else None
    rec.saving_basis = ctx.basis(
        "Replays every reply's cache writes and reads under the other lifetime at list price."
    )


def _explain_effort_mismatch(rec: Recommendation, ctx: _Context) -> None:
    if any(source == "habits.habits_effort_fit" for _label, _value, source, _row in rec.evidence):
        _explain_effort_mismatch_reported(rec, ctx)
        return
    share = _evidence_value(rec, "thinking share")
    rec.title = "High effort is being spent on light work"
    rec.why = (
        (f"At high effort, {share:.0f}% of Claude's output was thinking, " if isinstance(share, (int, float)) else "")
        + "yet many of your sessions are docs or light edits that rarely need it."
    )
    rec.why = rec.why[:1].upper() + rec.why[1:]
    rec.action = "Make medium your default effort, and raise it with /effort for the tasks that need it."
    rec.changes = [
        SettingChange(
            target="settings",
            key="effortLevel",
            value="medium",
            current=ctx.setting_now("effortLevel"),
            note="The high-effort thinking share is measured across all sessions, not only the light ones.",
            scope=_advice_scope(rec.scope),
        )
    ]


def _explain_effort_mismatch_reported(rec: Recommendation, ctx: _Context) -> None:
    """The direct path: messages Claude reported as easy that ran at high
    effort or above (``recommend._effort_mismatch_reported``)."""
    easy = sum(v for label, v, _s, _r in rec.evidence if label.startswith("Easy messages") and isinstance(v, int))
    shares = [v for label, v, _s, _r in rec.evidence if "thinking share" in label and isinstance(v, (int, float))]
    rec.title = "High effort is being spent on easy work"
    rec.why = (
        f"Claude reported {easy} of your messages as easy work, yet they ran at high effort or above"
        + (f", and up to {max(shares):.0f}% of their output was thinking." if shares else ".")
    )
    rec.action = "Make medium your default effort, and raise it with /effort for the tasks that need it."
    rec.changes = [
        SettingChange(
            target="settings",
            key="effortLevel",
            value="medium",
            current=ctx.setting_now("effortLevel"),
            note="Measured on the messages Claude reported as easy (metrics capture).",
            scope=_advice_scope(rec.scope),
        )
    ]
    rec.estimated_saving = ctx.money(rec.saving_usd, prefix="About ")
    rec.saving_basis = ctx.basis("Half the thinking on those messages, at list price. Not measured.")


def _explain_baseline_bloat(rec: Recommendation, ctx: _Context) -> None:
    table = next((t for s in ctx.report.sections if s.key == "agents" for t in s.tables
                  if t.name == "topology_session_baseline"), None)
    row_key = table.rows[0][0] if table is not None and table.rows else None
    baseline = ctx.cell("agents", "topology_session_baseline", row_key, "mean_baseline")
    rec.title = "Every session starts with a large context"
    rec.why = (
        f"Each main session writes about {_tokens(baseline)} tokens to the cache before your first message, "
        "and MCP servers and plugins you load everywhere add to it."
        if isinstance(baseline, (int, float))
        else "MCP servers and plugins you load everywhere add to every session's startup context."
    )
    rec.action = "Turn off MCP servers and plugins in the projects that don't use them."


def _explain_spawn_cost(rec: Recommendation, ctx: _Context) -> None:
    write = _evidence_value(rec, "first-turn write")
    agent = rec.agent_type or "this agent"
    rec.title = f"Starting {agent} is expensive before it does any work"
    rec.action = (
        f"Check what {agent} is given when it starts: its agent file, the CLAUDE.md files it receives and "
        "the task prompt you send it. Trim what it doesn't need."
    )
    rec.why = (
        f"Each {agent} spawn writes about {_tokens(write)} tokens to the cache on its first reply."
        if isinstance(write, (int, float))
        else f"Each {agent} spawn writes a lot to the cache on its first reply."
    )


def _explain_agent_report_size(rec: Recommendation, ctx: _Context) -> None:
    size = _evidence_value(rec, "report proxy")
    agent = rec.agent_type or "this agent"
    rec.title = f"{agent} sends back long reports"
    rec.why = (
        f"{agent}'s final reply averages {_tokens(size)} tokens, and it stays in the main session's context "
        "for the rest of the session."
        if isinstance(size, (int, float))
        else f"{agent}'s final reply stays in the main session's context for the rest of the session."
    )
    rec.action = f"Ask {agent} for a short report: the findings and file paths, not the working."


def _explain_limit_pressure(rec: Recommendation, ctx: _Context) -> None:
    hits = _evidence_value(rec, "Usage-cap hits")
    killed = _evidence_value(rec, "terminated")
    rec.title = "You keep hitting your usage limit"
    parts = []
    if isinstance(hits, (int, float)) and hits:
        parts.append(f"Sessions stopped at a usage limit {hits:,.0f} times")
    if isinstance(killed, (int, float)) and killed:
        parts.append(f"{killed:,.0f} subagents were cut off by it")
    rec.why = (" and ".join(parts) + ".") if parts else "Sessions keep stopping at a usage limit."
    rec.action = (
        "Run fewer agents at once when a limit is close, and check the Usage limits tab for when yours resets."
    )


def _explain_cache_read_dominance(rec: Recommendation, ctx: _Context) -> None:
    share = _evidence_value(rec, "Cache-read share")
    rec.title = "Most of your cost is re-reading the conversation"
    rec.why = (
        (f"Cache reads are {share:.0f}% of your cost. " if isinstance(share, (int, float)) else "")
        + "Each reply reads the whole conversation back from the cache. That is already the cheapest way to "
        "send it, so the saving comes from sending less."
    )
    rec.action = "Keep contexts small: start fresh sessions between tasks, and send exploration to subagents."


def _explain_data_quality(rec: Recommendation, ctx: _Context) -> None:
    rec.title = "A few numbers may be slightly off"
    rec.why = rec.action.split(":", 1)[-1].strip() if ":" in rec.action else rec.action
    rec.action = "See the Data quality tab for what could not be read."


def _explain_pricing_coverage(rec: Recommendation, ctx: _Context) -> None:
    # Fix 2: coverage_pct alone can't tell "no price at all" (cost is
    # left out entirely, totals read too low) from "priced by closest
    # match" (cost is counted, but only an estimate) apart -- read the
    # two usage tables the rule cites back off the report itself, the
    # same way _explain_baseline_bloat above reads a table it needs
    # rows from rather than a single cell.
    unknown_table = next(
        (t for s in ctx.report.sections if s.key == "usage" for t in s.tables if t.name == "pricing_unknown_models"),
        None,
    )
    closest_table = next(
        (t for s in ctx.report.sections if s.key == "usage" for t in s.tables if t.name == "pricing_closest_match"),
        None,
    )
    has_unknown = bool(unknown_table is not None and unknown_table.rows)
    has_closest_match = bool(closest_table is not None and closest_table.rows)

    if has_unknown and has_closest_match:
        rec.title = "Some usage has no price, some is only an estimate"
        rec.why = (
            "Replies from models missing from pricing.toml are left out of every cost, so "
            "totals are too low; others were priced at a different model's rate, so their "
            "cost may be off."
        )
    elif has_closest_match:
        rec.title = "Some usage is priced by closest match, not its own rate"
        rec.why = (
            "These replies' model has no pricing.toml row of its own, so their cost is "
            "estimated from the closest registered model's rate instead, and may be off."
        )
    else:
        rec.title = "Some usage has no price"
        rec.why = "Replies from models missing from pricing.toml are left out of every cost, so totals are too low."


def _explain_discovery_share(rec: Recommendation, ctx: _Context) -> None:
    share = _evidence_value(rec, "DISCOVERY")
    rec.title = "Much of the work is finding your way around"
    rec.why = (
        f"Searching and reading the code took {share:.0f}% of the cost." if isinstance(share, (int, float)) else ""
    )
    rec.action = (
        "Write down what gets rediscovered each time (where things live, how to run things) in CLAUDE.md or a "
        "short reference file, so sessions start from it."
    )


def _explain_subagent_volume(rec: Recommendation, ctx: _Context) -> None:
    agent = rec.agent_type or "One agent type"
    share = re.search(r"([\d.]+)%", rec.action)
    rec.title = f"{agent} is most of your subagent cost"
    rec.why = (
        f"{agent} is {share.group(1)}% of what your subagents cost, so it is the best place to look first."
        if share
        else "One agent type drives most subagent spend, so it is the best place to look first."
    )
    rec.action = (
        f"Check whether every {agent} run is needed, whether a cheaper model fits, and how long its task "
        "prompts are."
    )


def _why_only(text: str, title: str = "") -> Callable[[Recommendation, _Context], None]:
    def explain(rec: Recommendation, ctx: _Context) -> None:
        if title:
            rec.title = title
        if not rec.why:
            rec.why = text

    return explain


_EXPLAIN: dict[str, Callable[[Recommendation, _Context], None]] = {
    "compaction-window": _explain_compaction_window,
    "compaction-churn": _explain_compaction_churn,
    "long-context-share": _explain_long_context_share,
    "ttl-switch": _explain_ttl_switch,
    "effort-mismatch": _explain_effort_mismatch,
    "baseline-bloat": _explain_baseline_bloat,
    "spawn-cost": _explain_spawn_cost,
    "agent-report-size": _explain_agent_report_size,
    "limit-pressure": _explain_limit_pressure,
    "cache-read-dominance": _explain_cache_read_dominance,
    "data-quality": _explain_data_quality,
    "pricing-coverage": _explain_pricing_coverage,
    "discovery-share": _explain_discovery_share,
    "subagent-volume": _explain_subagent_volume,
    "long-tool-waits": _why_only(
        "When a command runs for more than 5 minutes, the cache expires and the next reply pays to rebuild it.",
        "Long waits for commands let the cache expire",
    ),
    "notification-invalidation": _why_only(
        "A notice that lands mid-conversation changes what the cache holds, so the next reply rebuilds it.",
        "Subagent notices keep breaking the cache",
    ),
    "batch-instructions": _why_only(
        "Each message you queue while Claude works is written to the cache on its own.",
        "Messages sent one at a time rebuild the cache",
    ),
    "tool-output-carry": _why_only(
        "A tool's output stays in the conversation, so every later reply pays to read it again."
    ),
    "wasted-turns": _why_only("These replies cost money but produced nothing you kept."),
}

_SEVERITY_ORDER = {"action": 0, "advice": 1, "info": 2}


def finish(
    recs: list[Recommendation], report: ReportModel, snapshot: Snapshot | None, units: Units | None
) -> list[Recommendation]:
    """Consolidate, reword and order ``recs`` (see the module docstring).
    Returns a new list; the recommendations in it are changed in place."""
    ctx = _Context(report=report, snapshot=snapshot, units=units)
    recs = _merge_model_tier(list(recs), ctx)
    recs = _consolidate_compaction(recs, ctx)
    # Nothing in an agent file can change how Claude Code starts these.
    recs = [r for r in recs if not (r.id == "spawn-cost" and r.agent_type in NOT_OVERRIDABLE)]
    for rec in recs:
        explain = _EXPLAIN.get(rec.id)
        if explain is not None:
            explain(rec, ctx)
        if rec.scope == "managed" and rec.changes and "administrator" not in rec.action:
            rec.action += " Your organisation's managed settings set this, so only your administrator can change it."
    recs = _drop_applied(recs)
    # Most important first: severity, then the largest estimated saving.
    recs.sort(key=lambda r: (_SEVERITY_ORDER.get(r.severity, 3), -(r.saving_usd or 0.0)))
    return recs


__all__ = ["NOT_OVERRIDABLE", "finish"]
