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
    return str(value)


def _cli_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


def _where(change: SettingChange, scope: str) -> tuple[str, str]:
    if change.target == "agent":
        path, who = _SCOPE_WHERE.get(scope, _SCOPE_WHERE["user"])
        return path.format(agent=change.agent), who
    return _SETTINGS_WHERE.get(scope, _SETTINGS_WHERE["user"])


def command_for(change: SettingChange, scope: str) -> str | None:
    """The ``apply --set`` line for ``change`` (dry run first), or
    ``None`` when the value needs judgement or a new agent file."""
    if change.value is None or change.new_agent_file:
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


def explainer_for(rec: Recommendation, change: SettingChange) -> list[tuple[str, str]]:
    what, tradeoff, caveat = SETTING_TEXT.get(change.key, (f"The {change.key} setting.", "", ""))
    path, who = _where(change, rec.scope)
    if change.new_agent_file:
        where = (
            f"A new file, {path}. {change.agent} is built into Claude Code; an agent file with the same "
            "name replaces it, so the new file must still do the built-in agent's job."
        )
    else:
        where = f"{path}: {who}."
    effect = rec.estimated_saving or "Not estimated: this part isn't measured on its own."
    if rec.estimated_saving and rec.saving_basis:
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
        ("Now and after", f"Now: {_human(change.current)}. After: {_after(change)}."),
        ("Where and who it affects", where),
        ("Expected effect", effect),
        ("Trade-off", " ".join(t for t in (tradeoff, notes) if t) or "None known."),
        ("How to undo it", undo),
    ]


def prompt_for(rec: Recommendation, change: SettingChange) -> str:
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
        "my permission to edit files under .claude; that is expected. Change nothing else."
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
            "prompt": template.format(title_lower=rec.title[:1].lower() + rec.title[1:]),
        }
    ]


def attach_fixes(recommendations: list[Recommendation]) -> None:
    for rec in recommendations:
        rec.fixes = build_fixes(rec)


__all__ = ["SETTING_TEXT", "attach_fixes", "build_fix", "build_fixes", "command_for", "explainer_for", "prompt_for"]
