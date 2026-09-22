"""Plain-English labels and "how to read this" help for the report.

Every report builder names its tables and columns for the code that reads
them (``recommend.py`` looks rows up by those raw keys). This module is
the display layer on top: :func:`annotate` walks an assembled
``ReportModel`` and fills in plain titles, column labels and help, value
labels, section intros, and where each table belongs on the dashboard.
It never changes a table's ``name``, a column's ``key`` or a row value,
so evidence lookups and the JSON/CSV keys stay exactly as they were.

House style: ``docs/writing-help.md``. Coverage and banned-word checks:
``tests/test_help_coverage.py``.

Dashboard placement (``Table.dashboard``) is the table audit: a table
stays on the dashboard ("keep") only if it answers a question you would
act on or explains a recommendation; supporting detail is shown collapsed
("advanced"); everything else is left to the CLI, JSON and CSV reports
("report").
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from .model import Column, Diagnostics, Help, ReportModel, Section, Table

# -- the table audit ------------------------------------------------------

#: Table name -> dashboard placement. A table not listed here defaults to
#: "keep"; ``tests/test_help_coverage.py`` checks every table the report
#: can emit is listed, so a new table is placed on purpose.
PLACEMENT: dict[str, str] = {
    # overview
    "totals": "keep",
    "by_model": "keep",
    # usage
    "by_day": "keep",
    "by_week": "advanced",
    "by_month": "advanced",
    "by_project": "keep",
    "by_entrypoint": "advanced",
    "five_hour_blocks": "keep",
    # sessions
    "sessions_by_mode": "keep",
    "sessions_by_purpose": "keep",
    "sessions_detail": "report",
    # cache rebuilds
    "recache_summary": "keep",
    "recache_signature_split": "keep",
    "recache_gap_buckets": "keep",
    "recache_preceding_tool": "advanced",
    "recache_top_command_prefixes": "advanced",
    "recache_primary_cause": "keep",
    "recache_primary_cause_prefix_invalidated": "advanced",
    "recache_event_cooccurrence": "report",
    "recache_attachment_subsplit": "advanced",
    "recache_by_agent_type": "keep",
    "recache_huge_context": "advanced",
    "recache_by_group": "advanced",
    "cache_ground_truth": "advanced",
    # cache lifetime
    "ttl_by_agent_type": "keep",
    "ttl_gap_distribution": "advanced",
    "ttl_wasted_writes": "advanced",
    "ttl_premium_waste": "advanced",
    "ttl_break_even_share": "keep",
    "ttl_near_miss": "advanced",
    "ttl_addressable_share": "advanced",
    "ttl_cache_economy": "keep",
    # usage limits
    "limits_summary": "keep",
    "limits_hits_by_kind": "advanced",
    "limits_agent_terminated": "advanced",
    "limits_pauses": "keep",
    "limits_reset_hour_histogram": "advanced",
    "limits_by_agent_type": "advanced",
    "limits_csv_cross_check": "advanced",
    # savings
    "carry_by_tool": "keep",
    "carry_by_agent_type": "advanced",
    "carry_top_results": "advanced",
    "carry_truncation_savings": "keep",
    "compaction_sim_by_window": "keep",
    "compaction_sim_by_agent_type": "keep",
    "compaction_sim_fidelity": "advanced",
    "model_swap_by_agent_type": "keep",
    "model_swap_summary": "keep",
    "waste_summary": "keep",
    "waste_by_cause": "keep",
    "waste_by_agent_type": "advanced",
    "waste_top_sessions": "advanced",
    # compactions
    "compactions_summary": "keep",
    "compactions_trigger_mix": "advanced",
    "compactions_per_session": "advanced",
    # subagent startup
    "agent_startup_breakdown": "keep",
    "agent_startup_unused": "keep",
    "agent_startup_shared": "keep",
    # agents
    "topology_spawn_write": "advanced",
    "topology_session_baseline": "advanced",
    "topology_upward_tool_result": "advanced",
    "topology_report_proxy": "advanced",
    "topology_skills_rollup": "keep",
    "topology_spawn_depth": "advanced",
    "topology_cost_per_spawn": "keep",
    "topology_chains_summary": "advanced",
    "topology_reminder_hook_pressure": "advanced",
    "topology_cache_signal_histogram": "report",
    "topology_mcp_cost": "keep",
    "topology_effort_tokens": "keep",
    "topology_per_turn_effort_tokens": "report",
    "topology_effort_by_agent_type": "advanced",
    "topology_context_composition": "keep",
    "topology_redundant_work": "advanced",
    "topology_redundant_reads": "advanced",
    # workstyle / workflows
    "workstyle_archetypes": "keep",
    "workflows_summary": "keep",
    "workflows_status_mix": "advanced",
    "workflows_detail": "advanced",
    # phases
    "phases_summary": "keep",
    "phases_by_transcript_kind": "advanced",
    "phases_by_agent_type": "advanced",
    # config
    "effective-config": "keep",
    "config-layers": "advanced",
    "config-groups": "advanced",
    "config-drift": "keep",
    # context budget
    "context_budget_baseline": "keep",
    "context_budget_autocompact": "advanced",
    "context_budget_statusline": "advanced",
    # scorecard
    "dimensions": "keep",
    "overall": "keep",
    # before and after
    "baseline_comparison_overview": "keep",
    "baseline_comparison_by_mode": "advanced",
    "data_quality": "keep",
    # Tables of other CLI commands (compare, elasticity, finance, pricing,
    # reconcile, savers, usage-windows, monthly). They never reach the
    # dashboard; "report" records that.
    "compare_overview": "report",
    "compare_by_stratum": "report",
    "compare_co_changed": "report",
    "cost_by_model": "report",
    "elasticity_budget": "report",
    "elasticity_fit": "report",
    "elasticity_recent_burn": "report",
    "finance_summary": "report",
    "pricing_rates": "report",
    "pricing_unknown_models": "report",
    "reconcile_by_period": "report",
    "savers_detected": "report",
    "savers_effect_by_stratum": "report",
    "savers_overhead": "report",
    "savers_search_substitution": "report",
    "savers_verdict": "report",
    "usage_windows_latest": "report",
    "usage_windows_regression": "report",
}

#: Table-name prefixes for tables whose names are built at run time (one
#: per changed config key).
PLACEMENT_PREFIXES: tuple[tuple[str, str], ...] = (("config-diff-", "advanced"), ("team_", "keep"))


def placement_for(table_name: str) -> str:
    """The dashboard placement for ``table_name`` (see :data:`PLACEMENT`)."""
    if table_name in PLACEMENT:
        return PLACEMENT[table_name]
    for prefix, placement in PLACEMENT_PREFIXES:
        if table_name.startswith(prefix):
            return placement
    return "keep"


# -- shared column help ---------------------------------------------------

#: Column key -> help, used for any column whose table doesn't give its
#: own. Covers the columns that mean the same thing everywhere.
COMMON_COLUMN_HELP: dict[str, str] = {
    "agent_type": "The subagent type, as named in its agent file or by Claude Code for built-in agents.",
    "spawns": "How many times this agent type was started in the window.",
    "turns": "How many model replies this covers.",
    "priced_turns": "Model replies with a known price.",
    "sessions": "How many sessions this covers.",
    "transcripts": "How many conversation logs this covers: one per main session and one per subagent run.",
    "cost": "Cost at list prices for the window.",
    "transcript_kind": "Main session or subagents.",
}


# -- per-section and per-table copy ---------------------------------------


@dataclass(frozen=True, slots=True)
class TableCopy:
    """Display copy for one table. ``columns`` maps a column key to
    ``(label, help)``; an empty label keeps the builder's own."""

    title: str = ""
    help: Help | None = None
    columns: dict[str, tuple[str, str]] = field(default_factory=dict)
    value_labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SectionCopy:
    title: str = ""
    intro: str = ""
    help: Help | None = None


_MAIN_OR_SUB = {"top-level": "Main session", "subagent": "Subagents", "workflow-agent": "Workflow agents"}

SECTION_COPY: dict[str, SectionCopy] = {
    "agent_startup": SectionCopy(
        title="What subagents are given at startup",
        intro=(
            "Every time a subagent starts, Claude Code sends it a set of instructions and lists before it "
            "does any work. You pay to write all of it into the cache, once per spawn."
        ),
        help=Help(
            shows="What each agent type receives before its first reply, what it received but never used, "
            "and what nearly every agent type receives alike.",
            read="Sizes are average tokens per spawn. Multiply by the number of spawns to see the total. "
            "\"Not recorded\" is the part Claude Code doesn't log in the transcript, mostly the tool "
            "definitions and system prompt.",
            act="Look for large parts that an agent never uses, such as a skills list no spawn called, "
            "or CLAUDE.md sent to agents that only search. The recommendations tab turns these into "
            "specific changes.",
        ),
    ),
    "agents": SectionCopy(
        title="Subagents: cost and flow",
        intro="How much your subagents cost, what they send back, and what fills their context.",
        help=Help(
            shows="Cost per subagent run, cost of skills and MCP servers, effort and thinking, and what "
            "fills the context window.",
            read="Compare agent types with each other. A type that costs much more per run, or carries "
            "far more context, is worth a closer look.",
            act="Start with the most expensive agent type per run, then check its startup context above.",
        ),
    ),
    "workstyle": SectionCopy(
        title="How you work",
        intro="Your sessions grouped by working pattern, such as a single operator or a fan-out of subagents.",
        help=Help(
            shows="How many sessions match each working pattern.",
            read="The largest group is your usual way of working. Profiles and recommendations are tuned to it.",
            act="",
        ),
    ),
    "workflows": SectionCopy(
        title="Workflows",
        intro="Multi-agent workflow runs: how many ran, how they ended, and what they cost.",
        help=Help(
            shows="Workflow runs in the window, their status, and the most expensive runs.",
            read="A high failure or cancel share means spend that produced nothing.",
            act="If one workflow dominates cost, check its agent count and phases.",
        ),
    ),
}

TABLE_COPY: dict[str, TableCopy] = {
    # -- subagent startup ---------------------------------------------------
    "agent_startup_breakdown": TableCopy(
        title="What each agent type is given at startup",
        help=Help(
            shows="One row per agent type. Each column is the average size of one part of the startup "
            "context, in tokens per spawn.",
            read="Startup size is everything the first reply had to read. The parts to its right add up "
            "to \"Share explained\"; the rest is \"Not recorded\".",
            act="A part that is large and repeated on every spawn is the cheapest thing to cut. CLAUDE.md "
            "can be switched off per agent; skills and tools can be narrowed in the agent file.",
        ),
        columns={
            "spawns": ("", "How many times this agent type started."),
            "fork_spawns": ("", "Spawns that copied the main conversation instead of starting fresh. Left out of the averages."),
            "startup_tokens": ("", "Everything the first reply read: task, instructions, tools and system prompt."),
            "task_prompt": ("", "The instructions the parent wrote when it started this agent."),
            "claude_md": ("", "Your CLAUDE.md files and auto memory, as sent to this agent."),
            "skills_listing": ("", "The list of skills the agent could call."),
            "tool_lists": ("", "Lists of deferred tools, MCP server instructions and other agent types it could start."),
            "hook_context": ("", "Text your hooks added before the first reply."),
            "other_attachments": ("", "Claude Code's own notes: environment, model, date and settings."),
            "system_prompt": ("", "Claude Code's system prompt and the agent's own prompt, when recorded."),
            "tool_definitions": ("", "The definitions of every tool the agent could call, when recorded."),
            "not_recorded": ("", "Startup size minus every part above. Mostly tool definitions and system prompt."),
            "measured_pct": ("", "How much of the startup size the parts to the left account for."),
            "write_price": (
                "",
                "List price of writing a million tokens into the cache for this agent's model. "
                "Used to put a price on each part.",
            ),
        },
    ),
    "agent_startup_unused": TableCopy(
        title="Loaded at startup but never used",
        help=Help(
            shows="For each agent type: how often it was given skills, MCP tools and CLAUDE.md, and how "
            "often it actually used them.",
            read="Compare \"given\" with \"used\". A skills list given on 500 spawns and used on 2 is "
            "paid for 498 times for nothing.",
            act="When an agent almost never uses something it is given, narrow it in that agent's file: "
            "set its tools, MCP servers or skills, or turn off CLAUDE.md for agents that only search or read.",
        ),
        columns={
            "spawns": ("", "Spawns with a measured startup."),
            "skills_listing_tokens": ("", "Average size of the skills list per spawn, in tokens."),
            "skills_listed_spawns": ("", "Spawns that were given the skills list."),
            "skills_used_spawns": ("", "Spawns that called at least one skill."),
            "mcp_offered_spawns": ("", "Spawns that were offered at least one MCP tool."),
            "mcp_used_spawns": ("", "Spawns that called at least one MCP tool."),
            "claude_md_tokens": ("", "Average size of CLAUDE.md and memory per spawn, in tokens."),
            "read_only_spawns": ("", "Spawns given CLAUDE.md that only searched or read files."),
        },
    ),
    "agent_startup_shared": TableCopy(
        title="Sent to most agent types",
        help=Help(
            shows="Startup parts that most agent types receive at about the same size, and where each "
            "one comes from.",
            read="\"Total across spawns\" is what that one source cost you in cache writes over the window.",
            act="A big shared source is worth trimming at the source: move agent-specific sections out of "
            "CLAUDE.md into the agent files or into skills loaded on demand.",
        ),
        columns={
            "part": ("", "The startup part."),
            "source": ("", "The file or feature that adds it."),
            "agent_types": ("", "How many agent types receive it, out of all measured types."),
            "mean_tokens": ("", "Average size per spawn, in tokens."),
            "total_tokens": ("", "Size per spawn times every spawn that received it."),
        },
        value_labels={
            "claude_md:User": "Global CLAUDE.md",
            "claude_md:Project": "Project CLAUDE.md",
            "claude_md:Local": "Project CLAUDE.local.md",
            "claude_md:AutoMem": "Auto memory",
            "claude_md:Managed": "Managed policy CLAUDE.md",
            "claude_md:Nested": "Subfolder CLAUDE.md and rules",
            "claude_md:Other": "Other instruction files",
            "skills_listing": "Skills list",
            "tool_lists": "Tool and agent lists",
            "hook_context": "Hook output",
            "other_attachments": "Environment and settings notes",
            "system_prompt": "System prompt",
            "tool_definitions": "Tool definitions",
        },
    ),
    # -- agents ------------------------------------------------------------
    "topology_spawn_write": TableCopy(
        title="Cache written when each subagent starts",
        help=Help(
            shows="The tokens each agent type writes to the cache on its first reply.",
            read="This is the startup cost you pay once per spawn, before any work.",
            act="If one type is much higher than the rest, see its breakdown in \"What each agent type is given at startup\".",
        ),
        columns={
            "mean_write": ("Average startup write", "Average tokens written to the cache on the first reply."),
            "median_write": ("Typical startup write", "The middle value, less affected by a few very large spawns."),
            "mean_briefing_chars": ("Average task prompt (characters)", "Length of the task the parent wrote when starting the agent."),
        },
    ),
    "topology_session_baseline": TableCopy(
        title="Main session startup write",
        help=Help(
            shows="The tokens your main session writes to the cache on its first reply.",
            read="This is the fixed cost of opening a session: system prompt, tools, CLAUDE.md and your first message.",
            act="",
        ),
        columns={
            "metric": ("", "Which sessions this row covers."),
            "mean_baseline": ("Average startup write", "Average tokens written on the first reply."),
            "median_baseline": ("Typical startup write", "The middle value."),
        },
        value_labels={"all": "All sessions"},
    ),
    "topology_upward_tool_result": TableCopy(
        title="Reports returned to the main session",
        help=Help(
            shows="How much text subagents and workflows handed back to the main session.",
            read="Every character returned stays in the main session's context for the rest of the session.",
            act="If reports are long, ask agents for a short summary in their instructions.",
        ),
        columns={
            "tool": ("Returned by", "Subagent (Agent) or workflow runs."),
            "calls": ("Runs", "How many reports came back."),
            "total_chars": ("Total characters", "All report text added to the main session."),
            "mean_chars_per_call": ("Average characters per report", "Average report length."),
        },
        value_labels={"Agent": "Subagents", "Workflow": "Workflows"},
    ),
    "topology_report_proxy": TableCopy(
        title="Length of each agent type's final reply",
        help=Help(
            shows="The output tokens of each agent type's last reply, which is roughly the report it hands back.",
            read="Higher means longer reports carried in the main session afterwards.",
            act="For a type with long reports, add a length limit to its instructions.",
        ),
        columns={
            "mean_proxy": ("Average final reply (tokens)", "Average output tokens of the last reply."),
            "median_proxy": ("Typical final reply (tokens)", "The middle value."),
        },
    ),
    "topology_skills_rollup": TableCopy(
        title="Cost of each skill, including the subagents it starts",
        help=Help(
            shows="For each skill: its own replies plus every subagent it started, directly or through other agents.",
            read="\"Spawned cost\" is usually most of it for skills that fan out.",
            act="For an expensive skill, check how many agents it starts per use and whether they all need to.",
        ),
        columns={
            "skill": ("", "The skill name."),
            "invocations": ("Uses", "How many times the skill was called."),
            "direct_cost": ("Own cost", "The skill's own replies."),
            "spawned_cost": ("Subagent cost", "Every subagent the skill started, and the ones they started."),
            "total_cost": ("", "Own cost plus subagent cost."),
            "mean_spawns": ("Subagents per use", "Average subagents started per use."),
            "mean_report_proxy": ("Average report (tokens)", "Average length of those subagents' final replies."),
        },
    ),
    "topology_spawn_depth": TableCopy(
        title="How deep subagents start other subagents",
        help=Help(
            shows="How many subagents were started by the main session (depth 1) or by another subagent (depth 2 or more).",
            read="Deeper chains multiply startup costs.",
            act="",
        ),
        columns={"depth": ("Depth", "1 means started by the main session."), "count": ("Subagents", "How many subagents at this depth.")},
    ),
    "topology_cost_per_spawn": TableCopy(
        title="Cost per subagent run",
        help=Help(
            shows="What one run of each agent type costs, and how long its tool calls took.",
            read="Compare types doing similar work. The typical run is a fairer guide than the average when a few runs are huge.",
            act="For the most expensive type, check its startup context and whether a cheaper model fits.",
        ),
        columns={
            "mean_cost": ("Average cost per run", "Total cost of a run, averaged."),
            "median_cost": ("Typical cost per run", "The middle value."),
            "mean_tool_wait": ("Average tool wait", "How long each of its tool calls took to answer."),
        },
    ),
    "topology_chains_summary": TableCopy(
        title="Subagent runs stopped early",
        help=Help(
            shows="How many subagent runs there were, and how many you stopped by hand.",
            read="Stopped runs are spend that produced no report.",
            act="",
        ),
        columns={"metric": ("", "What is counted."), "value": ("", "The count.")},
    ),
    "topology_reminder_hook_pressure": TableCopy(
        title="Claude Code notes and hook output per reply",
        help=Help(
            shows="How often Claude Code's own notes and your hooks' output were added, per reply.",
            read="Each addition grows the context and can end cache reuse early.",
            act="If hook output is frequent, check whether your hooks need to print on every call.",
        ),
        columns={
            "mean_reminders_per_turn": ("Notes per reply", "Claude Code's own reminders added per reply."),
            "mean_hook_output_per_turn": ("Hook outputs per reply", "Hook results added per reply."),
        },
        value_labels=_MAIN_OR_SUB,
    ),
    "topology_mcp_cost": TableCopy(
        title="Cost by MCP server",
        help=Help(
            shows="The replies that called each MCP server's tools, and what those replies cost.",
            read="This is the cost of the replies that used the server, not of loading its tool definitions.",
            act="A server that costs a lot for little use is worth disabling where you don't need it.",
        ),
        columns={"mcp_server": ("MCP server", "The server whose tool was called.")},
    ),
    "topology_effort_tokens": TableCopy(
        title="Output and thinking by effort level",
        help=Help(
            shows="For each effort setting: how many replies used it, and how much of their output was thinking.",
            read="Higher effort means more thinking per reply. Thinking is billed as output.",
            act="If high effort is common for routine work, a lower default effort can cut output cost.",
        ),
        columns={
            "effort": ("Effort", "The effort level the reply ran at."),
            "output_tokens": ("Output", "All output tokens, including thinking."),
            "thinking_tokens": ("Thinking", "Output tokens spent thinking."),
            "thinking_share": ("Thinking share", "Thinking as a share of output."),
        },
    ),
    "topology_per_turn_effort_tokens": TableCopy(
        title="Output and thinking by per-reply effort",
        help=Help(
            shows="The same split as by effort level, using the effort recorded on each reply.",
            read="Differs from the table above only when effort changed mid-session.",
            act="",
        ),
        columns={
            "effort": ("Effort", "The effort recorded on the reply."),
            "output_tokens": ("Output", "All output tokens, including thinking."),
            "thinking_tokens": ("Thinking", "Output tokens spent thinking."),
            "thinking_share": ("Thinking share", "Thinking as a share of output."),
        },
    ),
    "topology_effort_by_agent_type": TableCopy(
        title="Output and thinking by agent type",
        help=Help(
            shows="Output and thinking for the main session and each agent type.",
            read="A type with a high thinking share is running at high effort.",
            act="Lower the effort in an agent's file if its work doesn't need deep reasoning.",
        ),
        columns={
            "output_tokens": ("Output", "All output tokens, including thinking."),
            "thinking_tokens": ("Thinking", "Output tokens spent thinking."),
            "thinking_share": ("Thinking share", "Thinking as a share of output."),
        },
        value_labels={"top-level": "Main session"},
    ),
    "topology_context_composition": TableCopy(
        title="What fills the context window: main session vs subagents",
        help=Help(
            shows="For the main session and for subagents: the average size of each thing that fills the context, per conversation.",
            read="Startup is the first reply's cache write. Tool results and Claude Code's notes pile up as the conversation runs.",
            act="If tool results dominate, large outputs are being carried turn after turn; see the Savings tab.",
        ),
        columns={
            "mean_baseline": ("Startup", "Tokens written to the cache on the first reply."),
            "mean_tool_result_tokens": ("Tool results", "Tool output added over the conversation (estimated)."),
            "mean_output_tokens": ("Claude's replies", "Tokens Claude wrote over the conversation."),
            "mean_attachment_tokens": ("Claude Code's notes", "Reminders, file notes and settings added (estimated)."),
            "mean_compactions": ("Summaries", "Conversation summaries per conversation."),
        },
        value_labels=_MAIN_OR_SUB,
    ),
    "topology_redundant_work": TableCopy(
        title="Repeated work",
        help=Help(
            shows="Commands run again with the same start, and files re-read soon after a conversation summary.",
            read="Repeats after a summary mean the summary dropped something that had to be found again.",
            act="",
        ),
        columns={
            "metric": ("", "What is counted."),
            "mean_per_session": ("Per session", "Average per session."),
            "total": ("Total", "All sessions together."),
        },
    ),
    "topology_redundant_reads": TableCopy(
        title="Files read more than once",
        help=Help(
            shows="Files read more than once in the same session, and how many of those came right after a summary.",
            read="Some re-reads are normal after edits. Many re-reads of the same files add context for no new information.",
            act="",
        ),
        columns={
            "metric": ("", "What is counted."),
            "mean_per_session": ("Per session", "Average per session."),
            "total": ("Total", "All sessions together."),
        },
    ),
    # -- workstyle / workflows ---------------------------------------------
    "workstyle_archetypes": TableCopy(
        title="Your working patterns",
        help=Help(
            shows="Each working pattern, how many sessions match it, and what it means.",
            read="The biggest share is how you usually work.",
            act="",
        ),
        columns={
            "archetype": ("Pattern", "The working pattern."),
            "pct": ("Share", "Share of sessions."),
            "description": ("What it means", "How the pattern is recognised."),
        },
    ),
    "workflows_summary": TableCopy(
        title="Workflow runs",
        help=Help(shows="Totals for workflow runs in the window.", read="", act=""),
        columns={"metric": ("", "What is counted."), "value": ("", "The total.")},
    ),
    "workflows_status_mix": TableCopy(
        title="How workflow runs ended",
        help=Help(
            shows="How many runs completed, failed or were cancelled.",
            read="Failed and cancelled runs are spend that produced no result.",
            act="",
        ),
        columns={"status": ("", "How the run ended."), "count": ("Runs", "How many runs."), "pct": ("Share", "Share of runs.")},
    ),
    "workflows_detail": TableCopy(
        title="Most expensive workflow runs",
        help=Help(shows="The 20 workflow runs that cost the most.", read="", act=""),
        columns={
            "run_id": ("", "The run's id."),
            "session_id": ("", "The session it ran in."),
            "status": ("", "How it ended."),
            "agent_count": ("", "Agents it started."),
            "phases": ("", "How many phases it had."),
            "started": ("", "When it started."),
            "finished": ("", "When it finished."),
        },
    ),
}


# -- data quality ----------------------------------------------------------

#: ``Diagnostics`` field -> (label, what it means). Every field is listed;
#: ``tests/test_help_coverage.py`` checks.
DIAGNOSTIC_LABELS: dict[str, tuple[str, str]] = {
    "lines": ("Lines read", "Lines read from every conversation log in the window."),
    "unparsable_lines": ("Lines that could not be read", "Lines that were not valid JSON. A few are normal while a session is still writing."),
    "truncated_final_line": ("Last line cut off", "A log's last line was incomplete, usually because the session was still running."),
    "assistant_lines": ("Claude reply lines", "Lines holding part of one of Claude's replies."),
    "distinct_turns": ("Replies", "Separate replies from Claude, after joining the lines of each one."),
    "synthetic_turns": ("Placeholder replies", "Replies Claude Code wrote itself, such as an error notice. Not billed."),
    "turns_missing_usage": ("Replies without token counts", "Replies with no token counts. They are left out of costs."),
    "ttl_sum_mismatch": ("Cache write split does not add up", "Replies whose five-minute and one-hour cache writes don't sum to the total. Costs use the total."),
    "late_duplicate_ids": ("Repeated reply ids", "The same reply seen again later in a log. Counted once."),
    "ignored_line_types": ("Line types skipped", "Kinds of line this tool does not use, with counts."),
    "oversized_lines": ("Lines too large to read", "Lines over the size limit, skipped without reading."),
    "trailing_events": ("Events after the last reply", "Events logged after a session's last reply, so attached to none."),
    "replayed_lines": ("Lines copied from an earlier session", "Lines a resumed session repeated. Counted once."),
    "timestamp_parse_failures": ("Unreadable timestamps", "Replies whose time could not be read. They are left out of time-based tables."),
    "agent_settings": ("Agent settings seen", "Agent settings recorded in the logs, with counts."),
    "modes": ("Permission modes seen", "Permission modes recorded in the logs, with counts."),
    "attachment_catch_all": ("Unrecognised note types", "Kinds of Claude Code note this tool does not recognise yet, with counts. Worth reporting if large."),
    "pre_split_turns": ("Replies from older Claude Code", "Replies logged before Claude Code split cache writes by lifetime."),
    "limit_hits": ("Usage-limit stops", "Times a session stopped at a usage limit."),
    "limit_resumes": ("Resumes after a limit", "Times a session carried on after a usage-limit stop."),
    "agents_terminated": ("Subagents stopped by a limit", "Subagents that ended because a usage limit was reached."),
}


def diagnostics_table(diagnostics: Diagnostics, hook=None) -> Table:
    """The parse-quality counters as a plain-English table (the Data
    quality tab, ``GET /api/diagnostics``). Rows keep the raw field name
    as their key, shown through ``value_labels``. ``hook``, a
    ``hook_health.HookHealth``, adds a first row saying whether the
    config snapshot hook is running."""
    rows = []
    if hook is not None:
        rows.append(["snapshot_hook", "working" if hook.ok else "needs attention", hook.summary()])
    for field_def in dataclasses.fields(Diagnostics):
        value = getattr(diagnostics, field_def.name)
        if isinstance(value, dict):
            value = ", ".join(f"{k}: {v}" for k, v in sorted(value.items())) if value else "none"
        elif isinstance(value, bool):
            value = "yes" if value else "no"
        _, meaning = DIAGNOSTIC_LABELS.get(field_def.name, ("", ""))
        rows.append([field_def.name, value, meaning])
    return Table(
        name="data_quality",
        title="What could be read",
        columns=[
            Column(key="check", label="Check", kind="str", help="What was counted."),
            Column(key="value", label="Count", kind="str", help="The count for the window."),
            Column(key="meaning", label="What it means", kind="str", help="What the count tells you."),
        ],
        rows=rows,
        help=Help(
            shows="Counters from reading your conversation logs.",
            read="Most should be zero or small. Large skipped or unreadable counts mean some usage is missing from the other tabs.",
            act="If unrecognised note types or unreadable lines are large, your Claude Code version may be newer than this tool.",
        ),
        value_labels={"snapshot_hook": "Config snapshot hook"}
        | {key: label for key, (label, _) in DIAGNOSTIC_LABELS.items()},
    )


def _apply_table_copy(table: Table, copy: TableCopy | None, billing_mode: str) -> None:
    table.dashboard = placement_for(table.name)
    if copy is not None:
        if copy.title:
            table.title = copy.title
        if copy.help is not None:
            table.help = copy.help
        if copy.value_labels:
            table.value_labels = dict(copy.value_labels)
    for column in table.columns:
        label, help_text = ("", "")
        if copy is not None:
            label, help_text = copy.columns.get(column.key, ("", ""))
        if label:
            column.label = label
        column.help = help_text or column.help or COMMON_COLUMN_HELP.get(column.key, "")
        if column.kind == "money" and billing_mode == "subscription" and "list price" not in column.help:
            # A subscription isn't billed per token: say what the figure is.
            column.help = (column.help + " " if column.help else "") + "Shown at list price; your plan is not billed this way."


def annotate_section(section: Section, billing_mode: str = "api") -> None:
    """Apply :data:`SECTION_COPY`/:data:`TABLE_COPY`/:data:`PLACEMENT` to
    one section, in place."""
    copy = SECTION_COPY.get(section.key)
    if copy is not None:
        if copy.title:
            section.title = copy.title
        if copy.intro:
            section.intro = copy.intro
        if copy.help is not None:
            section.help = copy.help
    for table in section.tables:
        _apply_table_copy(table, TABLE_COPY.get(table.name), billing_mode)


def annotate(model: ReportModel) -> ReportModel:
    """Apply display copy to every section of ``model``, in place, and
    return it. Never changes a table name, a column key or a row value."""
    billing_mode = getattr(model.meta, "billing_mode", "api") or "api"
    for section in model.sections:
        annotate_section(section, billing_mode)
    return model


__all__ = [
    "COMMON_COLUMN_HELP",
    "DIAGNOSTIC_LABELS",
    "PLACEMENT",
    "PLACEMENT_PREFIXES",
    "SECTION_COPY",
    "TABLE_COPY",
    "SectionCopy",
    "TableCopy",
    "annotate",
    "annotate_section",
    "diagnostics_table",
    "placement_for",
]
