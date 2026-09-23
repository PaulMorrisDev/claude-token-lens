# Security policy

claude-token-lens is a local analytics tool. It reads the files Claude
Code writes; it never calls Claude or any other remote service, and
uses none of your tokens. This document is a sign-off checklist for a
corporate security review, written to be verifiable against the code
rather than taken on trust.

**Status note:** every guarantee below describes what the *current*
code does. This includes the `claude-token-lens serve` service
(watcher, SQLite store, JSON API, static web UI) and its deployment
artefacts — see
[README.md's "Running the service"](README.md#10-running-the-service)
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

- **On request only**, when you open the Context files tab or run
  `claude-token-lens review claude-md|skills` or `check claude-md|skills`:
  the text of your CLAUDE.md-family files (user, project, local, rule
  files, nested CLAUDE.md files, auto memory `MEMORY.md` and one level of
  `@` imports), and the skill descriptions in the newest transcripts'
  skill listing. They are read to measure sections, find duplicates and
  stale references and show each skill's description, then discarded:
  never written to the database, a snapshot, a log or a cache. The
  prompts built from them name headings and line numbers, not whole
  passages.

- `settings.json` in the folder above `<config-dir>` (normally
  `~/.claude/settings.json`), read directly by `init`, `changes`,
  `uninstall` and the dashboard's Data quality tab, to see whether the
  SessionStart hook and statusline are set up. Only the hook command and
  whether `statusLine` runs this tool are used; nothing from it is
  stored.
- The JSON payload Claude Code sends the statusline command on stdin at
  each refresh (see "Statusline" below).

Nothing outside these locations is read.

## What is written, and where

Everything this tool writes by itself lives under `<config-dir>`
(default `~/.claude/token-lens`, or `$CLAUDE_CONFIG_DIR/token-lens`):
`config.toml`, `projects/`, `baselines/`, `snapshots/`, `cache/`,
`usage-log.csv`, `statusline-keys.json`, `salt`, `service.db`,
`hooks/snapshot-config.py`, `profiles/`, `backups/` and
`active-profile`. The only other files it writes are output files you
name on the command line: for example `--out` (`export`,
`monthly-report`, `scrub-fixture`), `report --html PATH` or `serve
--monthly-report DIR`.

Four commands change something outside `<config-dir>`. Each prints the
change before making it:

- **`init`** (connect step): adds a SessionStart hook and, if you have
  none, a statusline to `settings.json` in the folder above
  `<config-dir>`. It asks first (default no), or does it without asking
  with `--connect`. `init --repair-hook` rewrites only a broken hook
  command. Both copy the file to `settings.json.bak-<UTC time>` beside
  it before writing.
- **`install-service`** (and `init`'s last step, when you say yes):
  registers `serve` to start at logon. On Windows that is a Scheduled
  Task (`-RunLevel Limited`, no file written); on Linux it writes
  `~/.config/systemd/user/claude-token-lens.service`; on macOS
  `~/Library/LaunchAgents/com.claude-token-lens.plist`. `--dry-run`
  prints the plan and does nothing. `uninstall-service` removes it. See
  [docs/deploy.md](docs/deploy.md).
- **`uninstall`**: removes this tool's hook and statusline from
  `settings.json` (after the same `.bak-` copy), removes the logon
  service, and, only with the matching flags, reverts applied changes
  (`--revert-changes`) and deletes `<config-dir>` (`--delete-data`).
  It asks before each step unless you pass `--yes`.
- **`apply`**: see "Applying a profile" below.

Running processes: `install-service`, `uninstall-service`,
`uninstall`, `changes` and the service's `/api/health` run the
platform's own task tool (`schtasks`, `powershell.exe`, `systemctl` or
`launchctl`) to register, remove or check the logon service. `apply`
runs `git ls-files` to check whether a target file is tracked. Nothing
else starts a process.

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
  `alwaysThinkingEnabled`) or a plain `bool`/`int`/`float`/`None`. Two further keys get
  their own safe summary shape instead of a raw value: `statusLine`
  (a bare present/absent boolean, never the command it runs) and
  `modelPricing` (a present flag plus the model ids it overrides, never
  the overridden numbers).

`Turn.read_target_hashes` is the one exception to "no path fragment is
ever stored", and it is deliberately a one-way hash rather than a
shortened/redacted string: for every `Read`/`Edit`/`Write`/
`NotebookEdit` tool call in a turn, `parse.py` stores
`hmac.new(salt, path.replace("\\", "/").casefold(), sha256).hexdigest()[:16]`
(a case- and slash-normalised path, not `os.path.normcase` — that call is a
no-op on POSIX and would leave two spellings of the same Windows path
unmerged) — a 16-character
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

**The `claude-token-lens serve` service's SQLite store**
(`<config-dir>/service.db`) is a narrow, documented exception to "no
path fragment is ever stored": `transcripts.path`, `projects.root_path`
and `profiles.toml_path` hold real local filesystem paths, including
the machine's username where it appears in a Windows/POSIX home
directory (`service/schema.py`'s module docstring). They exist purely
for the watcher's own bookkeeping — deciding what to re-parse and
where a project's scan root is — and every one of `Store`'s read
queries (`summary`, `sessions`, `session`, `daily_usage`, `recache`,
`compactions`, `snapshots`, `profiles`, `baselines`, `tags` and the
rest) is written to leave them out of its result dict entirely, so the
`/api/*` routes and the UI never see them; `tests/test_service_store.py`
asserts this with a distinctive fake path round-tripped through the
read queries.
`sessions.slug` (Claude Code's own project-slug encoding of the
project's absolute path, so it also embeds the username) is read out —
the UI needs some label for "which project" — but every read query
that selects it passes it through `discovery.redact_slug()` first,
which replaces the username segment with the literal `<user>` before
it ever reaches `/api/sessions`, `/api/session/<id>`,
`/api/report.{json,md,html}`, or the rendered UI.

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

<a id="applying-a-profile-the-one-command-that-writes-outside-config-dir"></a>

## Applying a profile

`claude-token-lens apply` (`profiles/apply.py`) is the one command that
changes how Claude Code behaves: it writes the settings and agent files
a profile or `--set` names. It does so only when you run it yourself —
never as a side effect of `report`, `snapshot-config`, the dashboard or
any other subcommand, and never on `--dry-run` (which only prints
text). `init` and `uninstall` also edit `settings.json`, but only to add
or remove this tool's own hook and statusline (see "What is written,
and where" above).

A real apply (not `--dry-run`, not `--launch`) writes exactly these
files, depending on `--scope`:

- One of `~/.claude/settings.json` (`user`), `<project>/.claude/
  settings.local.json` (`project-local`), or `<project>/.claude/
  settings.json` (`repo`) — a JSON read-merge-write of the profile's
  allowlisted settings keys only.
- `<project or ~>/.claude/agents/<name>.md` for each agent the profile
  configures — only the allowlisted frontmatter keys are patched in
  place; every surrounding character (comments, unrelated keys,
  formatting) is preserved verbatim.
- `<config-dir>/backups/<ts>/...` — a byte-for-byte pre-image of every
  file above, written *before* the new content, plus a `manifest.json`
  recording which backup corresponds to which target.
- `<config-dir>/snapshots/<ts>.json` and `<config-dir>/active-profile`
  — claude-token-lens's own bookkeeping, not a Claude Code config file.

`--launch` writes only `<config-dir>/profiles/<id>.settings.json` (a
one-session overlay) and nothing else. `apply` never writes an
environment-variable *value* to any file — a profile's `env` names are
printed as `export NAME=value` guidance only (see `docs/profiles.md`'s
"What a profile cannot do") — and never writes a key a managed-settings
layer currently governs, regardless of scope or flags.

Two refusals are on by default, both requiring an explicit flag to
override: writing to a file already tracked by git, at any scope
(`--allow-tracked`; checked with `git ls-files`), and creating an agent
frontmatter file that doesn't exist yet (`--force`). Every write is
preceded by a byte-for-byte backup, so any apply can be undone exactly
with `claude-token-lens apply --revert <ts>`: each file is restored from
its backup, and a file the apply created is deleted. A revert checks
each file's hash against the one `apply` recorded and refuses,
restoring nothing, if a file was edited after the apply, so it never
silently discards later changes; `--ignore-changes` overrides that. The
backups stay under `<config-dir>/backups/` until you delete that folder
(`uninstall --delete-data` refuses while an applied change is still in
place). Full detail:
[docs/profiles.md#applying-a-profile](docs/profiles.md#applying-a-profile).

## No outbound network calls

The tool never calls Claude, Anthropic or any other remote service,
and uses none of your tokens. Outside `src/claude_token_lens/service/`,
one function imports a networking library: after `install-service` (or
`init`'s service step) registers the service, `cli.py` makes one `GET
/api/health` request to the address and port it just registered
(`127.0.0.1:8765` by default), to report whether the service is
already answering. It never contacts any other address. No other module
imports `socket`, `urllib`, `http.client`, `requests` or equivalent. The package
has zero third-party dependencies (`pyproject.toml`'s
`dependencies = []`); `rich` is an optional, opt-in extra for nicer
terminal output, not a networking dependency. Pricing comes from a
user-edited local `pricing.toml`, never a live lookup — there is no
code path that could fetch it.

For the CLI's analytics/report subcommands this is a structural
guarantee: nothing to call out to, because they contain no networking
code at all. **The `claude-token-lens serve` service** (a local
`http.server` API and static UI, `src/claude_token_lens/service/`) is
the main exception: `service/api.py` and `service/serve.py` do import
`http.server` (to listen on its own local socket) and `urllib.parse`
(to parse request query strings — it never builds or fetches a URL).
The service opens one socket — its own local HTTP bind,
`127.0.0.1`-only unless you pass `--bind <address> --allow-remote` — but never
initiates a connection of its own. This is an automated, always-on
guarantee, not just documentation:
`tests/test_service_egress.py` monkeypatches every socket-level call
that could originate an outbound connection or a DNS lookup —
`socket.socket.connect`, `socket.socket.connect_ex`,
`socket.create_connection` and `socket.getaddrinfo` — for the lifetime
of a real running server, and asserts every recorded target is the
test client's own loopback address, so any future change that adds an
outbound call or even resolves a remote hostname fails the suite. It has also
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

## What a web page can and can't do to the service

The service has no login. Any program or user on this machine can call
it, and so can other machines if you bind it to a non-loopback address
(`--bind <address> --allow-remote`; `serve` refuses a non-loopback bind
without that flag). These guards stop a web page you visit from using
it through your browser:

- **DNS rebinding.** A page can't read the API by pointing its own
  domain name at `127.0.0.1`: every request whose `Host` header isn't a
  loopback name (`127.0.0.1`, `localhost`, `::1`), the specific
  `--bind` address, or a name you added with `serve --allowed-host`
  gets `403` before any route runs. A request with no `Host` header
  (not a browser) is allowed. See
  [docs/api.md](docs/api.md#host-allowlist-dns-rebinding).
- **Cross-site writes.** Every `POST` is refused with `403` when its
  `Origin` header isn't exactly `http://<Host>`, or its
  `Sec-Fetch-Site` is anything but `same-origin` or `none`. A request
  with neither header (a local script) is allowed. A `POST` must also
  be `Content-Type: application/json`, which a plain HTML form can't
  send. See [docs/api.md](docs/api.md#cross-site-protection-review-s3).
- **No CORS.** The service never sends `Access-Control-Allow-*`
  headers, so another site's script can't read a response.
- **Response headers.** Every response carries
  `Content-Security-Policy: default-src 'self'` (scripts only from the
  service itself), `X-Content-Type-Options: nosniff` and
  `Cache-Control: no-store`.
- **Static files.** `/static/*` serves only files inside the packaged
  UI folder; a path that escapes it gets `404`.
- **No request log.** Request paths are never written to stdout or a
  log.

## What the dashboard can change

The dashboard never changes your Claude Code configuration. A
recommendation or profile gives you a prompt to paste into Claude Code
(which asks your permission before editing anything under `.claude`)
and a `claude-token-lens apply ... --dry-run` command to run yourself.
The service's few write routes touch only its own files: session tags
(`mode`/`purpose`) in the store, and user profiles under
`<config-dir>/profiles/` (`POST /api/profiles`, and `POST
/api/profiles/from-current`, which saves the allowlisted keys of the
latest config snapshot there). Both refuse to overwrite an existing
profile unless asked to with `?replace=1`, and neither can create or
change a shipped catalogue profile. `POST /api/whatif` only works out
an estimate and writes nothing.

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
[README.md](README.md#global-flags-clipy).

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

## Statusline

Claude Code runs the statusline command only in a terminal session,
not in the desktop app. Its output is shown to you under the prompt and
is never sent to Claude. From each payload it appends a row to
`<config-dir>/usage-log.csv`: the time, the session id, usage-limit
percentages and reset times, context-window token counts and cache
state. No prompt text, file paths or command text.

### Payload key recording (`statusline-keys.json`)

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
