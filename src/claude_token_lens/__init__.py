"""claude-token-lens: config-aware token and prompt-cache analytics for
Claude Code transcripts.
"""

from __future__ import annotations

__version__ = "0.1.0.dev0"

#: Bump when transcript-parsing logic changes in a way that could change
#: results computed from a previously cached file.
PARSER_VERSION = 1

#: Bump when the model.py contract changes in a way that invalidates the
#: on-disk digest cache (see model.py's module docstring for the contract
#: rules: fields may be added with defaults, never renamed or removed
#: without a SCHEMA_VERSION bump).
SCHEMA_VERSION = 1

__all__ = ["__version__", "PARSER_VERSION", "SCHEMA_VERSION"]
