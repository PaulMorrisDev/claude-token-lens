"""claude-token-lens: config-aware token and prompt-cache analytics for
Claude Code transcripts.
"""

from __future__ import annotations

__version__ = "0.5.2"

#: Bump when transcript-parsing logic changes in a way that could change
#: results computed from a previously cached file.
#:
#: Bumped to 2 by the wp11-cache/wp12a-fixtures merge: parse.py's
#: ttl_split_unknown/pre_split_turns fix (main's 709239a) and
#: workstyle.py's chat-only-before-single-model reorder (main's 0f892e3)
#: both landed on main after wp11-cache branched from 25c68c1 (before
#: PARSER_VERSION existed), so a cache entry written under the old
#: parsing behaviour must not be treated as still valid.
#:
#: Bumped to 3 by f99901f (fix(privacy): redact relative Windows paths
#: and @-tokens at parse time): ``_redact_paths`` in parse.py now catches
#: relative ``Users\``/``home/`` paths and any ``@``-bearing token that
#: previously survived redaction into ``cmd_prefix``, so a digest cached
#: from before that fix reflects a leakier ``cmd_prefix`` and must be
#: treated as stale.
#:
#: Bumped to 4 by the capture-improvements batch: parse.py now derives
#: seven new additive ``Turn`` fields from a transcript's raw lines
#: (``tool_wait_s``/``model_latency_s``/``tool_result_chars_by_tool``,
#: ``agent_brief_chars``/``tool_input_chars_by_tool``,
#: ``read_target_hashes``, ``human_prompt_chars``/``human_prompt_has_paste``
#: -- see model.py's module docstring), none of which a pre-batch digest
#: cache entry ever computed, so it must not be treated as still valid.
#:
#: Bumped to 5 by the v3-limits batch: parse.py now detects usage-limit
#: pauses (LIMIT_HIT/LIMIT_RESUME/AGENT_TERMINATED events,
#: Turn.synthetic_kind, Turn.gap_cause -- see model.py's module
#: docstring), none of which a pre-batch digest cache entry ever
#: computed, so it must not be treated as still valid.
#:
#: Bumped to 6 by the v4-wasted-turns batch: parse.py now derives two new
#: additive ``Turn`` fields, ``tool_error_count``/``tool_error_chars``,
#: from each turn's own tool_result blocks that carry ``is_error: true``
#: (see model.py's module docstring), which no pre-batch digest cache
#: entry ever computed, so it must not be treated as still valid.
#:
#: Bumped to 7 by the readability batch: events.py now measures an
#: attachment's ``Event.size_chars`` from the real ``rendered`` shape (a
#: list of ``{"content": str}`` blocks) or its content fields, where it
#: previously read only a bare string and so recorded ``None``; it also
#: adds per-source ``instructions`` sizes and ``prompt_snapshot``
#: system/tool sizes to ``Event.detail``.
#:
#: Bumped to 8 by the context-files batch: events.py now keeps one record
#: per instruction file (salted path hash, type, path-scoped, size) for
#: ``instructions`` and ``nested_memory``, and one per skill (name, size)
#: for ``skill_listing`` and ``invoked_skills``; parse.py adds
#: ``Turn.skills_invoked``.
#:
#: Bumped to 9 by the quality-signals batch: parse.py adds
#: ``Turn.stop_reason``, ``tool_calls_by_tool``, ``tool_errors_by_tool``, ``edit_target_hashes``
#: and ``human_correction``; events.py records task-notification and
#: agent-result outcomes in ``Event.detail``.
#:
#: Bumped to 10 by the same batch: discovery.py records a workflow
#: agent's end state on ``TranscriptMeta.workflow_agent_state``, which a
#: version-9 digest (already written by a development build) lacks.
#:
#: Bumped to 11: parse.py took a turn's usage from the first line of a
#: streamed reply, whose output_tokens is a partial count and which has no
#: thinking_tokens; it now keeps the most complete snapshot among the
#: reply's lines, so older digests undercount output and thinking tokens.
#:
#: Bumped to 12: parse.py records why each failed tool call failed
#: (``Turn.tool_errors_by_kind``), which waste.py needs to tell a failing
#: test or a hook block from a call that couldn't run.
#:
#: Bumped to 13 by the three-pricing-fixes batch: parse.py now records
#: ``Turn.speed`` from each turn's own ``usage.speed`` (see model.py's
#: module docstring), which pricing.py needs to apply a model's fast-mode
#: rate multiplier. A pre-13 digest has no ``speed`` recorded, so every
#: transcript is re-parsed once to pick it up.
#:
#: Bumped to 14 by the edit-capture batch: ``Turn.edit_target_hashes`` now
#: covers MultiEdit and the files a Bash or PowerShell command writes
#: (``shell_writes.py``), drops edits whose tool call failed, and paths
#: are normalised further before hashing (Git Bash's ``/c/`` form, ``.``
#: and ``..``), so older digests undercount edits and can't match a file
#: written by a shell command to the same file edited with Edit. The same
#: bump adds ``Turn.retry_marker``/``result_marker`` (the quality markers
#: Claude can be asked to write; see ``quality.MARKER_LINES``).
#:
#: Bumped to 15 by the metrics-capture batch: parse.py reads capture tags
#: (``Turn.cap``, ``spawn_marker``, ``retry_marker`` gains ``scope``) and
#: capture notes (``Turn.cap_note_chars``, ``TranscriptMeta.cap_*``), and
#: records ``agent_result_chars``, ``prompt_flags``, ``plan_stats`` and
#: ``commands_run``. Three miscounts are fixed at the same time:
#: ``read_target_hashes`` covered edits as well as reads, so every edited
#: file looked re-read; ``hook_system_message`` lines (shown to you, never
#: to the model) were counted as hook context; and task notifications
#: weren't sized, so a background agent's report had no size.
#:
#: Bumped to 16 by the feedback batch: parse.py reads your /tl-feedback
#: answers (``Turn.feedback``) from the skill's ``[tl-fb: ...]`` line or,
#: failing that, from the AskUserQuestion result itself. ``commands_run``
#: now names skills you ran with a slash too: they are written
#: ``<command-message>`` first, as your message, and were missed.
PARSER_VERSION = 16

#: Bump when the model.py contract changes in a way that invalidates the
#: on-disk digest cache (see model.py's module docstring for the contract
#: rules: fields may be added with defaults, never renamed or removed
#: without a SCHEMA_VERSION bump).
SCHEMA_VERSION = 1

__all__ = ["__version__", "PARSER_VERSION", "SCHEMA_VERSION"]
