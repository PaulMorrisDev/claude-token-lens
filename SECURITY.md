# Security policy

claude-token-lens is a local, read-only analytics tool. This document is
a sign-off checklist for a corporate security review, written to be
verifiable against the code rather than taken on trust.

**Status note:** only the CLI's `pricing-check` and `snapshot-config`
subcommands are wired up today; report assembly and the
`serve`/Docker service described in the project plan do not exist in
this codebase yet (see [README.md](README.md)'s Status note). Every
guarantee below describes what the *current* code does — it will be
extended, never weakened, as those pieces land.

## What is read

- Claude Code transcript JSONL files under `<projects-root>/<slug>/
  <session>.jsonl`, `<session>/subagents/agent-*.jsonl` (+ sibling
  `.meta.json`), and `<session>/workflows/wf_*.json`.
- Claude Code configuration, via the `SessionStart` snapshot hook
  (`hooks/snapshot-config.py`): user and project `settings*.json` files,
  the platform's system-wide `managed-settings.json` (if present), agent
  frontmatter (`.claude/agents/*.md`), MCP server names, and enabled
  plugin names.
- Environment variable **names** matching `ANTHROPIC_*` / `CLAUDE_*` —
  never their values.

Nothing outside these locations is read, and nothing is ever written to
except claude-token-lens's own on-disk digest cache, config-snapshot
files, and usage log under `<config-dir>` (default
`~/.claude/token-lens`, or `$CLAUDE_CONFIG_DIR/token-lens`).

## What is stored

Only numeric digests and short, non-identifying labels:

- Token counts, timestamps, model ids, tool names, event kinds, and up
  to a **40-character** Bash/PowerShell command prefix
  (`Turn.cmd_prefix`/`preceding_cmd_prefix`).
- Structural shape markers for anything not on an explicit allowlist:
  `dict(n)`, `list(n)`, `str(len)` — used by the config-snapshot hook for
  every settings/frontmatter value that isn't one of the small set of
  named-safe keys (`model`, `effortLevel`, `outputStyle`,
  `autoCompactWindow`, `promptCacheTtl`, `subagentPromptCacheTtl`,
  `cleanupPeriodDays`, `desktopSessionCleanupPeriodDays`,
  `autoUpdatesChannel`, `alwaysThinkingEnabled`) or a plain `bool`/`int`.

Message text, tool-result content, file contents, full file paths and
full shell commands are never written to a dataclass field, the on-disk
cache, or any rendered output. This is enforced today by
`tests/test_privacy.py` (walks every field `parse_transcript` produces
and asserts no `str` field exceeds 64 characters outside a small named
allowlist, and that command-prefix fields never exceed 40) and
`tests/helpers.assert_privacy` (a second, shape-based scan every
privacy-relevant test fixture also runs, checking for a Windows drive
path, a POSIX `/home/` path, a `\Users\` path, an MSYS drive path, a
bare `@`, or a URL). Run it yourself:

```bash
python -m pytest tests/test_privacy.py tests/test_scrub.py -q
```

## No outbound network calls

No module in this codebase imports a networking library — no `socket`,
`urllib`, `http.client`, `requests` or equivalent anywhere in
`src/claude_token_lens/`. The package has zero third-party dependencies
(`pyproject.toml`'s `dependencies = []`); `rich` is an optional,
opt-in extra for nicer terminal output, not a networking dependency.
Pricing comes from a user-edited local `pricing.toml`, never a live
lookup — there is no code path that could fetch it.

This is currently a structural guarantee (nothing to call out to,
because there is no networking code), not yet an automated test — there
is no `serve`/service surface in this codebase to test the egress of.
**The planned v0.2 service** (`claude-token-lens serve`: a local
`http.server` API and static UI) will add an automated egress test
before it ships, asserting no outbound connection is ever opened and
that its HTTP bind is `127.0.0.1`-only.

## Digest cache

`cache.py`'s on-disk cache stores one JSON file per transcript under
`<config-dir>/cache/<sha256 of normcase(realpath)>.json` — a
provenance header (mtime, size, schema/parser version) plus the same
privacy-clean `TranscriptResult` fields described above, never raw
transcript content. A transcript modified in the last 60 seconds is
never read from or written to the cache (it's still being written by an
active session). To purge it:

```bash
# POSIX
rm -rf ~/.claude/token-lens/cache
# Windows
Remove-Item -Recurse -Force "$env:USERPROFILE\.claude\token-lens\cache"
```

`cli.py` parses `--rebuild-cache`/`--no-cache` on every subcommand
already, but as of this writing no subcommand handler acts on them yet
(`corpus.load_corpus`, the function that actually consults
`DigestCache`, isn't called from `cli.py` yet — only `pricing-check` and
`snapshot-config` are wired up; see [README.md](README.md)). Until that
lands, deleting the cache directory above is the only way to purge it.

## Excluding confidential projects

`exclude_projects` in `<config-dir>/config.toml` is a list of slug
regexes (matched with `re.search`, case-insensitive); any project whose
slug matches is excluded from discovery entirely — never scanned, never
parsed, never appearing in a cache file — not merely hidden from
output. A malformed regex in the list is skipped, never fatal.

```toml
exclude_projects = ["^confidential-", "client-acme$"]
```

## Reporting a vulnerability

Please open a private security advisory on the GitHub repository (or,
if that isn't available, open an issue asking for a private contact
channel) rather than a public issue, so a fix can land before the
details are public. Include the claude-token-lens version
(`claude-token-lens --version`), your OS, and — since transcripts are
never meant to leave your machine — a minimal *synthetic* reproduction
rather than a real transcript excerpt.
