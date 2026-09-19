# Security policy

claude-token-lens is a local, read-only analytics tool. This document is
a sign-off checklist for a corporate security review, written to be
verifiable against the code rather than taken on trust.

**Status note:** every guarantee below describes what the *current*
code does, verifiable against it rather than taken on trust. This
includes the `claude-token-lens serve` service (watcher, SQLite store,
JSON API, static web UI) and its deployment artefacts — see
[README.md's "Running the service"](README.md#14-running-the-service)
and [docs/deploy.md](docs/deploy.md).

## What is read

- Claude Code transcript JSONL files under `<projects-root>/<slug>/
  <session>.jsonl`, `<session>/subagents/agent-*.jsonl` (+ sibling
  `.meta.json`), and `<session>/workflows/wf_*.json`.
- Claude Code configuration, via the `SessionStart` snapshot hook
  (`hooks/snapshot-config.py`): user and project `settings*.json` files
  (including `.claude/settings.local.json`), the platform's system-wide
  `managed-settings.json` (if present), agent frontmatter
  (`.claude/agents/*.md`), MCP server names, and enabled plugin names.
- Environment variable **names** matching `ANTHROPIC_*` / `CLAUDE_*` /
  `OTEL_*` (plus a short fixed list of irregularly-named levers) —
  never their values, with one exception: `MAX_THINKING_TOKENS`,
  `MAX_MCP_OUTPUT_TOKENS`, `BASH_MAX_OUTPUT_LENGTH`, and
  `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` are numeric caps, not secrets, so
  their integer value is recorded alongside the name.
- (Schema 2 — see [`docs/config-layers.md`](docs/config-layers.md) for
  the full field list) `~/.claude.json`, the CLI's own per-machine state
  file: matched to the current project by
  `os.path.normcase(os.path.realpath(...))` — the raw matching key is
  never stored — yielding MCP server/plugin names, small counts, and
  numeric per-project session totals (a cross-check against this tool's
  own accounting for the same session, joined by session id, never
  message text); and byte counts, file counts, and names only (never
  content) for the CLAUDE.md family, `.claude/rules/`,
  `.claude/commands/`, skills, `.mcp.json`, output styles, auto-memory,
  and installed plugins.

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
  `autoCompactWindow`, `autoCompactEnabled`, `promptCacheTtl`,
  `subagentPromptCacheTtl`, `cleanupPeriodDays`,
  `desktopSessionCleanupPeriodDays`, `autoUpdatesChannel`,
  `alwaysThinkingEnabled`) or a plain `bool`/`int`. Two further keys get
  their own safe summary shape instead of a raw value: `statusLine`
  (a bare present/absent boolean, never the command it runs) and
  `modelPricing` (a present flag plus the model ids it overrides, never
  the overridden numbers).

`Turn.read_target_hashes` is the one exception to "no path fragment is
ever stored", and it is deliberately a one-way hash rather than a
shortened/redacted string: for every `Read`/`Edit`/`Write`/
`NotebookEdit` tool call in a turn, `parse.py` stores
`hmac.new(salt, normcase(path), sha256).hexdigest()[:16]` — a 16-character
hex digest that lets the *same* file be recognised as re-read across
turns and sessions (for "which files does this session keep
re-opening" analytics) without the path itself, or any substring of it,
ever appearing in a dataclass field, the digest cache, or a rendered
report. The salt is a random 32-byte value generated once with
`secrets.token_bytes(32)` and stored at `<config-dir>/salt` (`0600`
permissions where the OS supports it); without a salt in effect
(`parse.set_salt` never called), `read_target_hashes` is always empty
rather than falling back to an unsalted, offline-crackable hash. Because
the hash is keyed to a salt private to one machine's `<config-dir>`, it
cannot be correlated against a hash produced on a different machine or
after the salt file is rotated/deleted.

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

For the CLI's analytics/report subcommands this is a structural
guarantee: nothing to call out to, because there is no networking code
at all. **The `claude-token-lens serve` service** (a local
`http.server` API and static UI, `src/claude_token_lens/service/`) does
open one socket — its own local HTTP bind, `127.0.0.1`-only unless you
pass `--allow-remote` — but never initiates a connection of its own.
This is an automated, always-on guarantee, not just documentation:
`tests/test_service_egress.py` monkeypatches `socket.socket.connect`
for the lifetime of a real running server and asserts every recorded
connection target is the test client's own loopback address, so any
future change that adds an outbound call fails the suite. It has also
been verified against the built Docker image directly, independent of
the Python-level test: run with `--network none` (no network namespace
connectivity beyond loopback at all), the service still answers
`/api/health` correctly from inside the container — see
[docs/deploy.md](docs/deploy.md#verifying-no-egress) for the exact
commands. The Docker image adds defence in depth on top of that
guarantee (non-root user, read-only root filesystem, `cap_drop:
[ALL]`, `no-new-privileges`); the native Windows Scheduled Task and
systemd hosting paths are scoped instead by OS-level permissions
(`-RunLevel Limited`, `ProtectHome=read-only` plus a carved-out
`ReadWritePaths`) rather than a container boundary — detail on all
three in [docs/deploy.md](docs/deploy.md).

The service's on-disk SQLite store (`<config-dir>/service.db`) is
always a derived cache rebuilt from the same transcripts the CLI
already reads, never a second source of truth — `claude-token-lens
serve --purge` deletes it safely at any time (it prints exactly which
files it will delete and requires `--yes` before doing so).

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

`--rebuild-cache` does this for you (purges the cache, then repopulates
it as it parses); `--no-cache` skips the cache entirely for that one run
without deleting anything already on disk. Both are wired through to
`corpus.load_corpus` for every subcommand that loads a corpus — see
[README.md](README.md#2-quick-start).

## Excluding confidential projects

`exclude_projects` in `<config-dir>/config.toml` is a list of slug
regexes (matched with `re.search`, case-insensitive); any project whose
slug matches is excluded from discovery entirely — never scanned, never
parsed, never appearing in a cache file — not merely hidden from
output. A malformed regex in the list is skipped, never fatal.

```toml
exclude_projects = ["^confidential-", "client-acme$"]
```

## Aggregate exports (`export`, `monthly-report`)

`claude-token-lens export` (`src/claude_token_lens/exports.py`) and
`claude-token-lens monthly-report` (`src/claude_token_lens/monthly.py`)
read the same in-memory corpus every other subcommand does — no
additional file access, no network access — and write only counts, token
totals, and costs; never a prompt, a tool result, or a file path.

- **Aggregate-only by default.** `export` defaults to one row per
  `(day, project, model, entrypoint, agent_type)`; `session_id` is only
  present when the caller explicitly passes `--per-session`
  (`exports.resolve_export_options`).
- **Project slugs are hashed by default in every mode, including
  `--per-session`** — `--per-session` alone no longer implies raw
  slugs. Hashing uses a salted HMAC-SHA256 over the slug
  (`hmac.new(salt, b"slug:" + slug, sha256).hexdigest()[:12]`), the same
  HMAC-SHA256 construction (just a different truncation length and
  domain-separation tag) and the same `<config-dir>/salt` file described
  under "What is stored" above (`parse.load_or_create_salt`, reused
  rather than a second salt) — never an unsalted hash, and never the
  fully raw slug unless `--no-hash-slugs` is passed explicitly.
  `--no-hash-slugs` does not print the fully raw slug either: it
  replaces just the OS-username segment (`Users-<name>-`/`home-<name>-`)
  with `<user>` and prints a one-line warning to stderr naming the risk
  — a caller who genuinely needs raw slugs for correlation still cannot
  leak the machine's own username by accident.
- **`otel-jsonl` carries no project/session attribute at all** — the
  OpenTelemetry metric names it mirrors (`claude_code.token.usage`,
  `claude_code.cost.usage`) don't have one, so `--aggregate-only`/
  `--hash-slugs` are moot for that format.
- **`monthly-report` never lowers the bar.** It calls `report.build_report`
  over a month-filtered corpus and inherits that function's own privacy
  properties; it adds no session ids, slugs, or text of its own — only
  numeric finance totals.

`tests/test_exports.py` exercises this with `tests/helpers.assert_privacy`
against the fully rendered export text (not just the in-memory rows) in
every format, plus explicit checks that no real session id string is
present in an aggregate-only export and that no raw slug string is
present when `--hash-slugs` is in effect (`tests/helpers.assert_privacy`
also fails on any slug-shaped `Users-`/`home-`-anchored username segment
anywhere in a scanned string, not just in export output). Full
column-by-column detail: [`docs/exports.md`](docs/exports.md).

## Statusline payload key recording (`statusline-keys.json`)

`claude-token-lens`'s statusline integration
(`src/claude_token_lens/statusline.py`) records the *key names* of the
JSON payload Claude Code writes to it on every refresh —
recursively, dotted (e.g. `context_window.used_tokens`), capped at 200
names — to `<config-dir>/statusline-keys.json`, and only when that set
differs from what is already stored. **Names only, never values**: no
prompt text, no token counts, no file paths, no usernames, and no
session ids are ever written to this file, regardless of what the live
payload actually contains. See [`docs/exports.md`](docs/exports.md) for
why this exists (reconciling undocumented, partially-overlapping
field-name lists against the real payload shape).

## Reporting a vulnerability

Please open a private security advisory on the GitHub repository (or,
if that isn't available, open an issue asking for a private contact
channel) rather than a public issue, so a fix can land before the
details are public. Include the claude-token-lens version
(`claude-token-lens --version`), your OS, and — since transcripts are
never meant to leave your machine — a minimal *synthetic* reproduction
rather than a real transcript excerpt.
