"""What if? The estimated effect of a set of settings and agent changes
on the report's own window, looked up in tables the report already
computes -- no new simulation:

- a model for the main session or an agent: ``model_swap_by_agent_type``
  (the same tokens repriced);
- ``autoCompactWindow``: ``compaction_sim_by_window`` (sessions replayed
  with that window);
- a cache lifetime (``promptCacheTtl``, ``subagentPromptCacheTtl``, an
  agent's ``experimental.cacheTtl``): ``ttl_by_agent_type`` (every cache
  write replayed at 5 minutes or 1 hour);
- an agent's ``omitClaudeMd``: ``agent_startup_breakdown`` (the CLAUDE.md
  tokens each spawn writes);
- ``skillOverrides`` and ``enabledPlugins``: each skill's listing cost
  from ``ReportModel.context_files``;
- effort: no estimate, only how much of that agent's output was
  thinking (``topology_effort_by_agent_type``), since how much less a
  lower effort thinks isn't measured.

Each row says how it was worked out (``fidelity``): "ceiling",
"simulated", "measured per spawn", "estimated" or "not estimated". A
negative ``saving_usd`` means the change costs more.

EST-P6: given a ``calibration`` lookup (``backtest.calibration_multipliers``,
built from judged predictions -- ``estimate``'s own caller passes it in,
since this module stays "look it up in tables already computed", never
touching the store itself), a row whose ``(agent, key)`` has learned a
multiplier is scaled by it and its fidelity becomes "calibrated": this
kind of change's estimate, adjusted by how it actually turned out for
you before, not just repriced or replayed.
"""

from __future__ import annotations

from dataclasses import dataclass

from .units import Units

TOP = "top-level"

FIDELITY_TEXT = {
    # E3/EST-P1: a model reprice is a *ceiling* on the saving, not a
    # simulation -- it assumes the same tokens at the new model's rate,
    # but a different model may need more (or fewer) replies for the
    # same work, which this doesn't capture. Kept distinct from
    # "simulated" (autoCompactWindow, cache TTL) below, which replays
    # real sessions rather than just repricing their tokens.
    "ceiling": "Ceiling: the same tokens repriced at the new model's rate. The real "
    "saving could be smaller (or the change could cost more) if that model needs "
    "more replies for the same work.",
    "simulated": "Simulated: your own sessions replayed with the new value.",
    "measured": "Measured per spawn, then multiplied by the spawns in this window.",
    "estimated": "Estimated from the size of what stops being sent.",
    "none": "Not estimated.",
    # EST-P6: applied only once at least 3 of your own past predictions
    # for this exact kind of change have been judged against what
    # actually happened (backtest.calibration_multipliers) -- before
    # that, an estimate keeps its own ceiling/simulated/measured/
    # estimated fidelity above unchanged.
    "calibrated": "Adjusted by how this kind of change has actually turned out for you before (at least 3 "
    "judged predictions), not just its own ceiling, simulation or estimate.",
}


@dataclass(slots=True)
class _Tables:
    model: object

    def rows(self, section_key: str, table_name: str) -> list[dict]:
        for section in getattr(self.model, "sections", ()) or ():
            if section.key != section_key:
                continue
            for table in section.tables:
                if table.name == table_name:
                    keys = [column.key for column in table.columns]
                    return [dict(zip(keys, row)) for row in table.rows]
        return []

    def row(self, section_key: str, table_name: str, agent: str) -> dict | None:
        return next((r for r in self.rows(section_key, table_name) if r.get("agent_type") == agent), None)


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _model_column(row: dict, value: str) -> str | None:
    """The ``cost_<model id>`` column a model setting ("sonnet",
    "claude-haiku-4-5", "opus[1m]") prices at: the newest id containing
    the alias, or an exact id."""
    wanted = str(value).lower().replace("[1m]", "")
    columns = [k for k in row if k.startswith("cost_claude")]
    exact = f"cost_{wanted}"
    if exact in columns:
        return exact
    matches = [k for k in columns if wanted in k[len("cost_") :]]
    if not matches:
        return None
    # Newest model family first: ids sort by version within a family.
    return sorted(matches)[-1]


def _row(key: str, agent: str | None, value, saving: float | None, fidelity: str, basis: str) -> dict:
    return {
        "key": key,
        "agent": agent,
        "value": value,
        "saving_usd": round(saving, 6) if saving is not None else None,
        "fidelity": fidelity,
        "basis": basis,
    }


def _model(tables: _Tables, agent: str, value, key: str, label: str | None) -> dict:
    row = tables.row("model_swap", "model_swap_by_agent_type", agent)
    if row is None:
        return _row(key, label, value, None, "none", f"No {agent} runs in this window.")
    column = _model_column(row, value)
    observed = _num(row.get("observed_cost"))
    new = _num(row.get(column)) if column else None
    if column is None or observed is None or new is None:
        return _row(key, label, value, None, "none", f"No price for {value} in the rate card.")
    who = "the main session" if agent == TOP else f"{agent}"
    return _row(
        key,
        label,
        value,
        observed - new,
        "ceiling",
        f"Worked out by repricing {who}'s replies in this window at {column[len('cost_'):]}. "
        "A different model may need more or fewer replies for the same work, which this doesn't capture.",
    )


def _ttl(tables: _Tables, agents: list[str], value, key: str, label: str | None) -> dict:
    column = {"5m": "cost_all_5m", "1h": "cost_all_1h"}.get(str(value))
    if column is None:
        return _row(key, label, value, None, "none", "Only 5m and 1h are simulated.")
    saving = 0.0
    found = False
    for agent in agents:
        row = tables.row("ttl", "ttl_by_agent_type", agent)
        observed, new = (_num(row.get("cost_observed")), _num(row.get(column))) if row else (None, None)
        if observed is not None and new is not None:
            saving += observed - new
            found = True
    if not found:
        return _row(key, label, value, None, "none", "No runs in this window to replay.")
    return _row(
        key, label, value, saving, "simulated",
        f"Every cache write in this window replayed with a {value} lifetime: a longer lifetime costs more per "
        "write but rebuilds less after pauses.",
    )


def _compact(tables: _Tables, value, current) -> dict:
    rows = {str(r.get("window")).replace(",", ""): r for r in tables.rows("compaction_sim", "compaction_sim_by_window")}
    new = rows.get(str(value))
    base = rows.get(str(current)) if current is not None else rows.get("none")
    if new is None or base is None:
        options = ", ".join(k for k in rows if k != "none")
        return _row(
            "autoCompactWindow", None, value, None, "none",
            f"Only these windows are simulated: {options}." if options else "No sessions to replay.",
        )
    saving = (_num(base.get("cost")) or 0.0) - (_num(new.get("cost")) or 0.0)
    return _row(
        "autoCompactWindow", None, value, saving, "simulated",
        f"Your sessions replayed with summaries at {int(value):,} tokens: about "
        f"{_num(new.get('compactions_per_session')) or 0:.1f} summaries per session. Earlier summaries carry "
        "less context each reply but lose detail.",
    )


def _omit_claude_md(tables: _Tables, agent: str, label: str) -> dict:
    row = tables.row("agent_startup", "agent_startup_breakdown", agent)
    tokens, spawns, price = (
        (_num(row.get("claude_md")), _num(row.get("spawns")), _num(row.get("write_price"))) if row else (None, None, None)
    )
    if not tokens or not spawns or not price:
        return _row("omitClaudeMd", label, True, None, "none", f"No CLAUDE.md measured at {agent}'s start.")
    return _row(
        "omitClaudeMd", label, True, tokens * spawns * price / 1_000_000, "measured",
        f"About {round(tokens):,} CLAUDE.md tokens written at each of {int(spawns)} spawns. The agent then "
        "works without your project's rules.",
    )


def _skills(context_files: dict, overrides: dict) -> dict:
    skills = {row.get("name"): row for row in (context_files or {}).get("skills") or () if isinstance(row, dict)}
    saving = 0.0
    counted = []
    for name, visibility in overrides.items():
        row = skills.get(name)
        if row is None or visibility == "on":
            continue
        cost = _num(row.get("listing_cost_usd")) or 0.0
        # "name-only" keeps the name: roughly the first few tokens of a line.
        share = 1.0 if visibility in ("off", "user-invocable-only") else 0.8
        saving += cost * share
        counted.append(name)
    if not counted:
        return _row("skillOverrides", None, overrides, None, "none", "None of these skills was listed in this window.")
    return _row(
        "skillOverrides", None, overrides, saving, "estimated",
        f"The listing lines of {len(counted)} skill{'s' if len(counted) != 1 else ''}, sent at the start of "
        "every session and subagent, stop being sent (only the name stays with name-only).",
    )


def _plugins(context_files: dict, plugins: dict) -> dict:
    skills = (context_files or {}).get("skills") or ()
    saving = 0.0
    off = [name.split("@")[0] for name, enabled in plugins.items() if enabled is False]
    for row in skills:
        name = str(row.get("name") or "")
        if ":" in name and name.split(":")[0] in off:
            saving += _num(row.get("listing_cost_usd")) or 0.0
    if not off:
        return _row("enabledPlugins", None, plugins, 0.0, "estimated", "Turns plugins on: adds their skills and tools.")
    return _row(
        "enabledPlugins", None, plugins, saving or None, "estimated" if saving else "none",
        "The listing lines of those plugins' skills stop being sent. Their tools and MCP servers go too; those "
        "aren't counted here.",
    )


def _effort(tables: _Tables, agent: str | None, value, key: str) -> dict:
    row = tables.row("agents", "topology_effort_by_agent_type", agent or TOP)
    share = _num(row.get("thinking_share")) if row else None
    basis = (
        f"Thinking was {share:.0f}% of {'the main session' if not agent else agent}'s output. A lower effort thinks "
        "less, but by how much isn't measured."
        if share is not None
        else "How much less a lower effort thinks isn't measured."
    )
    return _row(key, agent, value, None, "none", basis)


def estimate(
    settings: dict,
    agents: dict,
    model,
    units: Units,
    *,
    period: str = "",
    current: dict | None = None,
    calibration: dict[tuple[str | None, str], float] | None = None,
) -> dict:
    """One row per change in ``settings`` and ``agents`` (``{agent:
    {key: value}}``), plus a total of the rows that could be estimated.
    ``current`` is the settings in effect now, where known. ``calibration``
    (EST-P6, see the module docstring) is a ``(agent, key) -> multiplier``
    lookup; a row whose pair is in it has its ``saving_usd`` scaled by
    that multiplier and its fidelity set to "calibrated" -- the value
    and fidelity it would otherwise have had move to
    ``uncalibrated_usd``/``uncalibrated_fidelity`` (``None`` on every
    other row), so a caller logging a prediction to check later
    (``route_whatif``'s ``"log": true``) can still log the raw estimate
    rather than one already adjusted by a past prediction's own outcome
    -- calibrating a calibrated number would compound, not correct."""
    tables = _Tables(model)
    current = current or {}
    context_files = getattr(model, "context_files", None) or {}
    subagents = [r.get("agent_type") for r in tables.rows("ttl", "ttl_by_agent_type") if r.get("agent_type") != TOP]
    rows: list[dict] = []
    for key, value in (settings or {}).items():
        if key == "model":
            rows.append(_model(tables, TOP, value, key, None))
        elif key == "promptCacheTtl":
            rows.append(_ttl(tables, [TOP], value, key, None))
        elif key == "subagentPromptCacheTtl":
            rows.append(_ttl(tables, subagents, value, key, None))
        elif key == "autoCompactWindow":
            rows.append(_compact(tables, value, current.get("autoCompactWindow")))
        elif key == "skillOverrides" and isinstance(value, dict):
            rows.append(_skills(context_files, value))
        elif key == "enabledPlugins" and isinstance(value, dict):
            rows.append(_plugins(context_files, value))
        elif key == "effortLevel":
            rows.append(_effort(tables, None, value, key))
        else:
            rows.append(_row(key, None, value, None, "none", "This change isn't simulated."))
    for agent, levers in (agents or {}).items():
        for key, value in (levers or {}).items():
            if key == "model":
                rows.append(_model(tables, agent, value, key, agent))
            elif key in ("experimental.cacheTtl", "cacheTtl"):
                rows.append(_ttl(tables, [agent], value, "experimental.cacheTtl", agent))
            elif key == "omitClaudeMd" and value is True:
                rows.append(_omit_claude_md(tables, agent, agent))
            elif key == "effort":
                rows.append(_effort(tables, agent, value, key))
            else:
                rows.append(_row(key, agent, value, None, "none", "This change isn't simulated."))
    for row in rows:
        row["uncalibrated_usd"] = None
        row["uncalibrated_fidelity"] = None
    if calibration:
        for row in rows:
            if row["saving_usd"] is None:
                continue
            multiplier = calibration.get((row["agent"], row["key"]))
            if multiplier is not None:
                row["uncalibrated_usd"] = row["saving_usd"]
                row["uncalibrated_fidelity"] = row["fidelity"]
                row["saving_usd"] = round(row["saving_usd"] * multiplier, 6)
                row["fidelity"] = "calibrated"
    total = sum(row["saving_usd"] for row in rows if row["saving_usd"] is not None)
    for row in rows:
        row["effect_text"] = _effect_text(row["saving_usd"], units, period)
        row["fidelity_text"] = FIDELITY_TEXT.get(row["fidelity"], "")
    estimated = [row for row in rows if row["saving_usd"] is not None]
    return {
        "period": period,
        "rows": rows,
        "total_usd": round(total, 6),
        "total_text": _effect_text(total, units, period) if estimated else "",
        "estimated": len(estimated),
        "not_estimated": len(rows) - len(estimated),
        "total_note": (
            "Changes overlap (a cheaper model also makes each summary and cache write cheaper), so the total "
            "is rough; each row on its own is the better guide."
            if len(estimated) > 1
            else ""
        ),
    }


def _effect_text(saving: float | None, units: Units, period: str) -> str:
    if saving is None:
        return "Not estimated"
    amount = units.money(abs(saving))
    if amount is None:
        return "No measurable change"
    suffix = f" {period}" if period else ""
    return f"Saves {amount.text()}{suffix}" if saving > 0 else f"Costs {amount.text()} more{suffix}"


__all__ = ["FIDELITY_TEXT", "estimate"]
