"""What a recommendation asks you to change, said plainly, plus two ways
to make the change: a command and a prompt for Claude.

The dashboard never edits your Claude Code config itself. For each
:class:`~claude_token_lens.model.SettingChange` on a recommendation,
:func:`build_fix` returns:

- ``explainer``: six (heading, text) pairs -- what the setting controls,
  now and after, where it is set and who it affects, the expected
  effect, the trade-off, and how to undo it;
- ``command``: a ``claude-token-lens apply --set ... --dry-run`` line,
  or ``None`` when the right value needs your judgement;
- ``command_warning``: what the command does not do (see
  :data:`_PREPARE`), or ``""``;
- ``prompt``: a self-contained request to paste into Claude Code. It
  names the file, the key and the value, says why, warns that Claude
  Code will ask permission (``.claude`` is a protected directory) and
  asks Claude to show the change before saving it.

A recommendation with no setting change (workflow advice) gets a single
fix with a prompt only when one is useful (see :data:`_WORKFLOW_PROMPTS`).
"""

from __future__ import annotations

import json
import shlex

from .model import Recommendation, SettingChange

#: Shown after every fix and profile change, and printed after an apply:
#: Claude Code reads settings, agent files and CLAUDE.md when it starts,
#: so the session that made the change keeps the old ones.
RESTART_NOTE = (
    "Restart Claude Code to pick up the change. It reads settings and agent files when it starts, so a "
    "session that is already open keeps the old ones (claude --continue picks your last conversation back up)."
)

#: The last line of every prompt for Claude that changes Claude Code's
#: files, so the reminder comes at the moment the change is saved.
PROMPT_RESTART = "Once it's saved, remind me to restart Claude Code so it picks up the change."

#: key -> (what it controls, trade-off, extra caveat).
SETTING_TEXT: dict[str, tuple[str, str, str]] = {
    "omitClaudeMd": (
        "Whether Claude Code sends your CLAUDE.md files and auto memory to this agent when it starts.",
        "The agent no longer sees your project and personal rules. Only its own prompt (the body of its "
        "agent file) and the skills listed in its frontmatter reach it on every spawn, so the rules it needs "
        "must be moved there first, and the saving shrinks by their size.",
        "Needs Claude Code 2.1.271 or later; older versions ignore it.",
    ),
    "disallowedTools": (
        "Tools this agent may not call. They are removed from what it can use.",
        "The agent can't use the tools you list, even when a task would need them.",
        "",
    ),
    "mcpServers": (
        "The MCP servers this agent can use. Servers left out are not loaded for it.",
        "The agent can't call tools from servers you leave out.",
        "",
    ),
    "tools": (
        "The only tools this agent may call. Definitions of other tools are not sent to it.",
        "The agent can't use any tool missing from the list, for example to edit a file.",
        "",
    ),
    "model": (
        "Which Claude model does the work. Smaller models cost less per token.",
        "A smaller model may need more replies for the same task, or get some tasks wrong. Try it on a few "
        "tasks and compare the results before keeping it.",
        "",
    ),
    "autoCompactWindow": (
        "How large the conversation may grow, in tokens, before Claude Code replaces it with a summary. "
        "Every reply re-reads the whole conversation, so a smaller window means cheaper replies.",
        "A summary drops detail. After one, Claude may re-read files or lose track of earlier decisions.",
        "",
    ),
    "effortLevel": (
        "How hard Claude thinks before replying. Thinking is billed as output.",
        "Lower effort can miss things on hard problems. You can raise it for one task with /effort.",
        "",
    ),
    "promptCacheTtl": (
        "How long the main session's cache is kept between replies: 5 minutes or 1 hour.",
        "A 1-hour cache costs more to write, so it only pays off when you often pause for more than 5 minutes.",
        "A 1-hour lifetime is ignored while a Pro or Max plan is using extra usage credits.",
    ),
    "subagentPromptCacheTtl": (
        "How long every subagent's cache is kept between replies: 5 minutes or 1 hour.",
        "A 1-hour cache costs more to write, so it only pays off when subagents often wait more than 5 minutes.",
        "A 1-hour lifetime is ignored while a Pro or Max plan is using extra usage credits.",
    ),
    "experimental.cacheTtl": (
        "How long this agent's cache is kept between replies: 5 minutes or 1 hour.",
        "A 1-hour cache costs more to write, so it only pays off when this agent often waits more than 5 minutes.",
        "Needs Claude Code 2.1.248 or later. A 1-hour lifetime is ignored while a Pro or Max plan is using "
        "extra usage credits.",
    ),
}

SETTING_TEXT.update(
    {
        "effort": (
            "How hard this agent thinks before replying. Thinking is billed as output.",
            "Lower effort can miss things on hard problems.",
            "",
        ),
        "maxTurns": (
            "The most replies this agent may take before it has to stop and report back.",
            "An agent that hits the limit stops mid-task and returns what it has.",
            "",
        ),
        "memory": (
            "Which persistent memory this agent reads and writes between runs.",
            "Memory adds to what the agent is sent on every run.",
            "",
        ),
        "skills": (
            "The skills loaded into this agent when it starts, in full.",
            "Every listed skill is sent on every spawn, whether the task needs it or not.",
            "",
        ),
        "outputStyle": (
            "The output style Claude uses for replies, such as concise or explanatory.",
            "A terser style gives less explanation.",
            "",
        ),
        "enabledPlugins": (
            "The plugins turned on for Claude Code.",
            "Each plugin can add skills, agents, hooks and MCP servers, which are sent on every session.",
            "",
        ),
        "disabledMcpjsonServers": (
            "MCP servers from the project's .mcp.json that are turned off.",
            "Claude can't call tools from servers you turn off.",
            "",
        ),
        "skillOverrides": (
            "How each skill is shown to Claude. \"name-only\" lists the skill by name without its "
            "description; \"user-invocable-only\" hides it from Claude but keeps it in your / menu; \"off\" "
            "hides it everywhere. Skills not named stay as they are.",
            "Claude uses a skill on its own only when its description tells Claude what it is for. With the "
            "description gone Claude may not reach for it; hidden, only you can start it, by typing /name.",
            "",
        ),
        "enabledMcpjsonServers": (
            "MCP servers from the project's .mcp.json that are turned on.",
            "Every enabled server's tool list is sent with each session.",
            "",
        ),
        "alwaysThinkingEnabled": (
            "Whether Claude always thinks before replying.",
            "Thinking is billed as output, so replies cost more.",
            "",
        ),
        "autoCompactEnabled": (
            "Whether Claude Code summarises the conversation automatically when it grows too large.",
            "With it off, a long session keeps growing until you run /compact or start a new one.",
            "",
        ),
        "cleanupPeriodDays": (
            "How many days Claude Code keeps conversation logs before deleting them.",
            "Logs older than this are gone, including from this tool's reports.",
            "",
        ),
    }
)

#: Short names for every allowlisted key, for forms and tables.
LEVER_LABELS = {
    "model": "Model",
    "effortLevel": "Effort level",
    "effort": "Effort level",
    "autoCompactWindow": "Summarise the conversation at (tokens)",
    "autoCompactEnabled": "Summarise the conversation automatically",
    "outputStyle": "Output style",
    "promptCacheTtl": "Main session cache lifetime",
    "subagentPromptCacheTtl": "Subagent cache lifetime",
    "experimental.cacheTtl": "Cache lifetime",
    "enabledPlugins": "Plugins turned on",
    "skillOverrides": "How skills are shown to Claude",
    "disabledMcpjsonServers": "Project MCP servers turned off",
    "enabledMcpjsonServers": "Project MCP servers turned on",
    "alwaysThinkingEnabled": "Always think before replying",
    "cleanupPeriodDays": "Keep conversation logs for (days)",
    "maxTurns": "Most replies per run",
    "omitClaudeMd": "Leave out CLAUDE.md files",
    "memory": "Memory",
    "tools": "Tools it may use",
    "disallowedTools": "Tools it may not use",
    "skills": "Skills loaded at start",
    "mcpServers": "MCP servers it may use",
}

_SCOPE_WHERE = {
    "user": ("~/.claude/agents/{agent}.md", "your own agent file, used in every project"),
    "repo": (".claude/agents/{agent}.md", "this project's agent file, used by everyone who works in it"),
}
_SETTINGS_WHERE = {
    "user": ("~/.claude/settings.json", "your user settings, used in every project"),
    "repo": (".claude/settings.json", "this project's shared settings"),
}

#: Workflow recommendations (no setting to change) that come with a
#: prompt anyway, keyed by ``Recommendation.id``.
_WORKFLOW_PROMPTS = {
    "spawn-shared-claude-md": (
        "My CLAUDE.md files are sent to most of my subagents every time one starts: {title_lower}. "
        "Please read my CLAUDE.md files and my agent files in ~/.claude/agents and .claude/agents. "
        "Find sections that only some agents need, and propose moving each one into those agents' own "
        "files or into a skill they load on demand. Keep rules every agent needs where they are. "
        "Show me the proposed moves and the diff before changing anything. Claude Code will ask my "
        "permission before editing files under .claude."
    ),
    "baseline-bloat": (
        "Every Claude Code session I start loads a large context before my first message: {title_lower}. "
        "Please list the MCP servers and plugins I have enabled (in ~/.claude/settings.json, this project's "
        ".claude/settings.json and .mcp.json), say which ones this project doesn't seem to use, and propose "
        "turning those off for this project only. Show me the proposed change before making it. Claude Code "
        "will ask my permission before editing files under .claude."
    ),
    "agent-report-size": (
        "{agent} sends back long final reports, and each one stays in my main session's context. Please "
        "read {agent}'s agent file (~/.claude/agents/{agent}.md or .claude/agents/{agent}.md) and propose "
        "one or two lines for its prompt asking for a short report: findings, file paths and next steps, "
        "not the working. If {agent} has no agent file (it is built into Claude Code), propose a sentence I "
        "can add to the task prompts I send it instead. Show me the diff before saving. Claude Code will ask "
        "my permission before editing files under .claude."
    ),
}


#: Work that has to happen before a key is set, or the change loses
#: something the agent needs. The prompt asks Claude to do it first; the
#: command, which only sets the key, carries it as a warning.
_PREPARE = {
    "omitClaudeMd": (
        "First read the CLAUDE.md files {agent} receives today (~/.claude/CLAUDE.md, the project's CLAUDE.md "
        "and CLAUDE.local.md, and any files they import). List the rules {agent} needs to do its job, show me "
        "the list, and add them to the agent's own prompt (the body of {path}, below the frontmatter). Keep it "
        "short: every line is sent on every spawn."
    ),
}
_COMMAND_WARNINGS = {
    "omitClaudeMd": (
        "This only sets the flag. Move the rules the agent needs into its agent file first, or use the prompt "
        "above, which does both."
    ),
}


def _human(value) -> str:
    if value is None:
        return "not set"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "(empty list)"
    if isinstance(value, dict):
        return ", ".join(f"{k}: {_human(v)}" for k, v in value.items()) if value else "(none)"
    return str(value)


def _shown(value) -> str:
    """``_human`` for reading: whole numbers get thousands separators.
    Prompts keep ``_human`` so Claude copies the value as written."""
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return _human(value)


def _model_family(model_id: str) -> str:
    for family in ("haiku", "sonnet", "opus", "fable"):
        if family in model_id:
            return family
    return model_id


def already_set(key: str, value, now) -> bool:
    """Whether ``now`` already is ``value``, so offering the change would
    do nothing. A model id matches its family's alias ("claude-sonnet-5"
    is "sonnet"), text ignores case, and a map (skillOverrides) is set
    when every entry it names already has that value."""
    if value is None or now is None:
        return False
    if isinstance(value, dict):
        return isinstance(now, dict) and all(already_set(key, v, now.get(k)) for k, v in value.items())
    if isinstance(value, str) and isinstance(now, str):
        wanted, current = value.strip().lower(), now.strip().lower()
        if key == "model":
            wanted, current = _model_family(wanted), _model_family(current)
        return wanted == current
    if isinstance(value, bool) or isinstance(now, bool):
        return value is now
    return value == now


def _cli_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return ",".join(f"{k}:{_cli_value(v)}" for k, v in value.items())
    return str(value)


_MANAGED_WHERE = ("your organisation's managed settings", "set by policy; only your administrator can change it")


def _where(change: SettingChange, scope: str) -> tuple[str, str]:
    scope = change.scope or scope
    if scope == "managed":
        return _MANAGED_WHERE
    if change.target == "agent":
        path, who = _SCOPE_WHERE.get(scope, _SCOPE_WHERE["user"])
        return path.format(agent=change.agent), who
    return _SETTINGS_WHERE.get(scope, _SETTINGS_WHERE["user"])


def command_for(change: SettingChange, scope: str) -> str | None:
    """The ``apply --set`` line for ``change`` (dry run first), or
    ``None`` when the value needs judgement or a new agent file."""
    scope = change.scope or scope
    if change.value is None or change.new_agent_file or scope == "managed":
        return None
    parts = ["claude-token-lens", "apply", "--set", f"{change.key}={_cli_value(change.value)}"]
    if change.target == "agent" and change.agent:
        parts += ["--agent", change.agent]
    if scope == "repo":
        parts += ["--scope", "repo", "--project-dir", "."]
    else:
        parts += ["--scope", "user"]
    parts.append("--dry-run")
    return " ".join(shlex.quote(p) for p in parts)


def _after(change: SettingChange) -> str:
    if change.value is not None:
        return _human(change.value)
    return change.suggested or "your choice"


#: Model families, cheapest first.
_MODEL_ORDER = ("haiku", "sonnet", "opus", "fable")


def _to_larger_model(change: SettingChange) -> bool:
    """Whether ``change`` moves a model setting up to a pricier family."""
    if change.key != "model" or not isinstance(change.value, str) or not isinstance(change.current, str):
        return False
    after, before = _model_family(change.value.lower()), _model_family(change.current.lower())
    return after in _MODEL_ORDER and before in _MODEL_ORDER and _MODEL_ORDER.index(after) > _MODEL_ORDER.index(before)


def explainer_for(rec: Recommendation, change: SettingChange) -> list[tuple[str, str]]:
    what, tradeoff, caveat = SETTING_TEXT.get(change.key, (f"The {change.key} setting.", "", ""))
    if _to_larger_model(change):
        tradeoff = (
            "A larger model costs more per token, so every reply this agent sends costs more. The models check "
            "shows how much."
        )
    path, who = _where(change, rec.scope)
    if change.new_agent_file:
        where = (
            f"A new file, {path}. {change.agent} is built into Claude Code; an agent file with the same "
            "name replaces it, so the new file must still do the built-in agent's job."
        )
    else:
        where = f"{path}: {who}."
    saving = change.saving or rec.estimated_saving
    effect = saving or "Not estimated: this part isn't measured on its own."
    if saving and rec.saving_basis:
        effect += " " + rec.saving_basis
    if change.unconfirmed:
        effect += " Claude Code's docs don't confirm this effect, so check the numbers after the change."
    notes = " ".join(n for n in (caveat, change.note) if n)
    undo = (
        "Run the apply --revert command that apply prints, or set the value back by hand."
        if command_for(change, rec.scope)
        else "Put the file back as it was (Claude Code shows the change before saving it)."
    )
    return [
        ("What this setting controls", what),
        (
            "Now and after",
            f"Now: {_shown(change.current)}. "
            f"After: {_shown(change.value) if change.value is not None else _after(change)}.",
        ),
        ("Where and who it affects", where),
        ("Expected effect", effect),
        ("Trade-off", " ".join(t for t in (tradeoff, notes) if t) or "None known."),
        ("How to undo it", undo),
    ]


def prompt_for(rec: Recommendation, change: SettingChange) -> str:
    if (change.scope or rec.scope) == "managed":
        return (
            f"My organisation's managed settings lock {change.key}, so I can't change it myself. Draft a short "
            f"request to my administrator to set {change.key} to {_after(change)}, saying why: "
            f"{rec.why or rec.title} Keep it under 120 words and don't change any files."
        )
    path, _ = _where(change, rec.scope)
    subject = f"the {change.agent} agent" if change.target == "agent" else "my Claude Code settings"
    prepare = _PREPARE.get(change.key, "").format(agent=change.agent, path=path)
    if change.new_agent_file:
        ask = (
            f"{change.agent} is a built-in Claude Code agent. Create {path}, a custom agent with the same "
            f"name, that does the same job as the built-in one and sets {change.key}: {_after(change)} in "
            "its frontmatter. Write its prompt from what you know of the built-in agent, and keep its "
            "tools the same."
        )
        if prepare:
            ask += " " + prepare
    elif isinstance(change.value, dict):
        # Objects keyed by name (skillOverrides, enabledPlugins): add or
        # update the named entries, as ``apply`` does, never replace.
        entries = ", ".join(f"{json.dumps(k)}: {json.dumps(v)}" for k, v in change.value.items())
        ask = f"In {path}, add {entries} to {change.key}, keeping every entry already there."
        if prepare:
            ask = f"{prepare} Then, {ask[0].lower()}{ask[1:]}"
    elif change.value is not None:
        where = " in the frontmatter" if change.target == "agent" else ""
        ask = f"In {path}, set {change.key} to {json.dumps(change.value)}{where}."
        if prepare:
            ask = f"{prepare} Then, in {path}, set {change.key} to {json.dumps(change.value)}{where}."
    else:
        ask = f"In {path}, change {change.key}: {change.suggested}."
    why = rec.why or rec.title
    lines = [
        f"I want to change a setting for {subject}. {ask}",
        f"Why: {why}",
    ]
    caveat = SETTING_TEXT.get(change.key, ("", "", ""))[2]
    lines += [text for text in (caveat, change.note) if text]
    if change.unconfirmed:
        lines.append("Its effect on startup size isn't documented, so it's an experiment.")
    lines.append(
        "Before saving, restate the change in one sentence and show me the diff. Claude Code will ask "
        "my permission to edit files under .claude; that is expected. Change nothing else. " + PROMPT_RESTART
    )
    return "\n".join(lines)


#: Where a profile's change lands, per ``apply`` scope: settings file,
#: agent file (``{agent}`` filled in), and who it affects.
PROFILE_SCOPE_WHERE = {
    "user": ("~/.claude/settings.json", "~/.claude/agents/{agent}.md", "you, in every project"),
    "project-local": (
        ".claude/settings.local.json",
        ".claude/agents/{agent}.md",
        "this project, on your machine only",
    ),
    "repo": (".claude/settings.json", ".claude/agents/{agent}.md", "everyone who works in this project"),
}


def profile_change_where(key: str, scope: str) -> str:
    """The file a profile diff row's dotted ``key`` (``settings.<name>``,
    ``agents.<agent>.<name>`` or ``env.<NAME>``) is written to under
    ``scope``."""
    settings_path, agent_path, _ = PROFILE_SCOPE_WHERE.get(scope, PROFILE_SCOPE_WHERE["user"])
    if key.startswith("agents."):
        return agent_path.format(agent=key.split(".")[1])
    if key.startswith("env."):
        return f"{settings_path} (env block)"
    return settings_path


def profile_prompt(name: str, rows: list[dict], scope: str) -> str:
    """A self-contained prompt asking Claude to make a profile's changes
    by hand: one line per changed, unmanaged key, naming the file, the
    old and the new value."""
    _, _, who = PROFILE_SCOPE_WHERE.get(scope, PROFILE_SCOPE_WHERE["user"])
    lines = [f'I want to apply the settings profile "{name}" to Claude Code. It affects {who}. Make these changes:']
    for row in rows:
        if row.get("managed") or row.get("current_value") == row.get("proposed_value"):
            continue
        key = row["key"]
        parts = key.split(".")
        where = profile_change_where(key, scope)
        if key.startswith("agents."):
            field = f"{'.'.join(parts[2:])} in the frontmatter"
        elif key.startswith("env."):
            field = f"the environment variable {parts[1]}"
        else:
            field = ".".join(parts[1:])
        lines.append(
            f"- In {where}, set {field} to {json.dumps(row.get('proposed_value'))} "
            f"(now: {_human(row.get('current_value'))})."
        )
    if len(lines) == 1:
        return f'The settings profile "{name}" matches your current settings; there is nothing to change.'
    lines.append(
        "If an agent file doesn't exist, the agent is built into Claude Code: say so and don't create one. "
        "Before saving, restate the changes and show me the diff. Claude Code will ask my permission to "
        "edit files under .claude; that is expected. Change nothing else. " + PROMPT_RESTART
    )
    return "\n".join(lines)


def build_fix(rec: Recommendation, change: SettingChange) -> dict:
    return {
        "key": change.key,
        "agent": change.agent,
        "explainer": [list(pair) for pair in explainer_for(rec, change)],
        "command": command_for(change, rec.scope),
        "command_warning": _COMMAND_WARNINGS.get(change.key, "") if command_for(change, rec.scope) else "",
        "prompt": prompt_for(rec, change),
    }


def build_fixes(rec: Recommendation) -> list[dict]:
    """One fix per :class:`SettingChange` on ``rec``; for workflow
    advice with a known prompt, one prompt-only fix."""
    if rec.changes:
        return [build_fix(rec, change) for change in rec.changes]
    template = _WORKFLOW_PROMPTS.get(rec.id)
    if template is None:
        return []
    return [
        {
            "key": None,
            "agent": None,
            "explainer": [],
            "command": None,
            "command_warning": "",
            "prompt": template.format(
                title_lower=rec.title[:1].lower() + rec.title[1:], agent=rec.agent_type or "this agent"
            ),
        }
    ]


def attach_fixes(recommendations: list[Recommendation]) -> None:
    for rec in recommendations:
        rec.fixes = build_fixes(rec)


__all__ = [
    "LEVER_LABELS",
    "PROFILE_SCOPE_WHERE",
    "SETTING_TEXT",
    "already_set",
    "attach_fixes",
    "build_fix",
    "build_fixes",
    "command_for",
    "explainer_for",
    "profile_change_where",
    "profile_prompt",
    "prompt_for",
]
