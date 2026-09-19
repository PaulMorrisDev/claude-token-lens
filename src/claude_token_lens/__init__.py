"""claude-token-lens: config-aware token and prompt-cache analytics for
Claude Code transcripts.
"""

from __future__ import annotations

__version__ = "0.2.0"

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
PARSER_VERSION = 5

#: Bump when the model.py contract changes in a way that invalidates the
#: on-disk digest cache (see model.py's module docstring for the contract
#: rules: fields may be added with defaults, never renamed or removed
#: without a SCHEMA_VERSION bump).
SCHEMA_VERSION = 1

__all__ = ["__version__", "PARSER_VERSION", "SCHEMA_VERSION"]
