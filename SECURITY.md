# Security policy

claude-token-lens is a local, read-only analytics tool. This document is a
promise the code is expected to keep. It is enforced by `tests/test_privacy.py`
(added when the parser lands) and by the `probe` command's content-free
schema output.

## What is read

- Claude Code transcript JSONL files under `<projects-root>/<slug>/*.jsonl`
  and `<session>/subagents/agent-*.jsonl`.
- Claude Code configuration: user and project `settings*.json` files, agent
  frontmatter, MCP server names, and enabled plugin names.
- Environment variable *names* matching `ANTHROPIC_*` / `CLAUDE_*` — never
  their values.

Nothing outside these locations is read, and no file is ever written to
except the tool's own cache, database and report output.

## What is stored

Only numeric digests and short, non-identifying labels: token counts,
timestamps, model ids, tool names, event kinds, up to a 40-character
Bash/PowerShell command prefix, and structural counts (`dict(n)`, `list(n)`,
`str(len)` for anything not on the explicit config allowlist). Message text,
tool result content, file contents, full file paths and full shell commands
are never written to a dataclass field, the on-disk cache, the SQLite store,
or any report output (Markdown, JSON, CSV or HTML).

## No outbound network calls

claude-token-lens makes no network requests of any kind: no telemetry, no
update check, no phone-home. Pricing comes from a user-editable local
`pricing.toml`, never a live lookup. The service surface (`claude-token-lens
serve`) binds to `127.0.0.1` only; a test asserts no outbound connection is
ever opened.

## Container posture

The Docker image runs as a non-root user, mounts `~/.claude` read-only,
keeps the tool's own state in a separate writable volume, and binds its
HTTP API to `127.0.0.1` only — it is never exposed beyond the host.

## How to run the privacy audit

The privacy test suite greps every rendered report format for strings
longer than 64 characters, drive letters, `/home/`, `@`, and other
identifying patterns, and fails the build on any hit:

```
python -m pytest tests/test_privacy.py -q
```

This file, and the audit it describes, will grow with the codebase; no
guarantee above will be weakened without a corresponding test change.
