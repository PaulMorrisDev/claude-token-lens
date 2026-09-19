# claude-token-lens

Config-aware token and prompt-cache analytics for Claude Code transcripts:
where your tokens go, what your caching configuration costs or saves, and
which configuration changes to make. Stdlib-only, MIT-licensed, runs
entirely on your own machine.

**Status: pre-release, v0.1 in progress.** The parsing, pricing, RE-CACHE,
TTL, classification, compaction, config-snapshot, topology, workstyle,
workflow and phase-split engines are implemented and covered by tests
(868 passing at the time of writing). The command-line surface is not:
today only `claude-token-lens pricing-check` and `claude-token-lens
snapshot-config` are wired up; every other subcommand in `cli.py`
(`report`, `sessions`, `recache`, `ttl`, `compactions`, `config-diff`,
`log-usage`, `scrub-fixture`, `probe`, `statusline`, `init`, `baseline`,
`serve`) is a registered stub that prints "not implemented" and exits 2.
Report assembly (turning the section builders below into one `ReportModel`
and printing it), the recommendation engine, and the optimisation
scorecard described in the project plan do not exist yet in this
codebase. This README describes what the code actually does today, not
the full plan — see [`docs/sections-reference.md`](docs/sections-reference.md)
for section-by-section detail and this file's own notes on what's still
missing.

Three things are usable directly today, without the `report` command:

- `python -m claude_token_lens.tools.scrub` — turn a real transcript into
  a privacy-scrubbed fixture (see [Privacy and security](#11-privacy-and-security)).
- `python -m claude_token_lens.statusline` — a live Claude Code status
  line (see [Installing the hook and statusline](#8-installing-the-sessionstart-hook-and-the-statusline)).
- `python -m claude_token_lens.tools.log_usage` — append a `get_usage`
  paste to a local CSV log.
- Every analytics module (`recache`, `ttl`, `classify`, `compaction`,
  `snapshots`, `topology`, `workstyle`, `workflows`, `phases`,
  `pricing`) can be called directly from a Python shell or a short
  script against your own transcripts; that is how the worked examples
  in this README were produced.

## 1. What it is, what it measures, and what it cannot

claude-token-lens reads Claude Code's local JSONL transcripts
(`~/.claude/projects/<slug>/<session>.jsonl`, plus
`<session>/subagents/agent-*.jsonl` and `<session>/workflows/wf_*.json`),
groups assistant lines into priced turns, and turns them into token,
cache and cost analytics. It never sends anything anywhere: no network
calls, no telemetry, no update check.

What it cannot do:

- **There is no billing API.** Anthropic does not publish a way to read
  back what a Claude Code session actually cost. Every dollar figure
  this tool prints is computed from a rate card **you** supply and edit
  in [`pricing.toml`](src/claude_token_lens/pricing.toml) — it is never
  fetched, and the tool has no code path that could fetch it.
- **Subscription users are not billed in USD per token.** If you run
  Claude Code on a subscription within your plan's usage window, the
  binding constraint is the 5-hour/7-day usage window, not a dollar
  total — and a 1-hour prompt-cache TTL on a subagent is documented as
  being ignored while you're on usage credits. `pricing.toml`-derived
  money columns for a subscription account are a **list-price
  equivalent**: a useful way to compare two configurations against each
  other, not a real invoice. (`ReportMeta.billing_mode` and
  `Config.billing` already model `"api"` vs `"subscription"` in
  [`model.py`](src/claude_token_lens/model.py) and
  [`config.py`](src/claude_token_lens/config.py); nothing yet renders a
  billing-mode-aware report, since report assembly doesn't exist.)
- **The JSONL format is observed, not a published API.** Every field
  name this tool reads was found by inspecting real transcripts, not
  from documentation, and Claude Code's own docs describe the transcript
  format as internal and unstable. The parser tolerates unknown line
  types and fields rather than failing on them (see
  [Windows notes](#9-windows-notes) and `Diagnostics.ignored_line_types`),
  but a future Claude Code release can still change field names under
  it. Two integration points *are* documented and stable, and are the
  better choice if you need a durable contract instead of a
  best-effort parser: Claude Code's OpenTelemetry metrics
  (`CLAUDE_CODE_ENABLE_TELEMETRY=1`, metrics `claude_code.token.usage`
  and `claude_code.cost.usage`), and `claude -p --output-format json`
  for scripted single-shot invocations.

## 2. Quick start

```bash
pip install .
# or, isolated from your other Python environments:
pipx install .
```

Because the package has no third-party dependencies (`dependencies = []`
in `pyproject.toml`; `rich` is an optional extra), it can also be built
into a single-file, dependency-free `.pyz` with the standard library's
own `zipapp` module — useful on a locked-down machine that only has a
bare Python 3.11+ interpreter:

```bash
python -m zipapp src -m "claude_token_lens.cli:main" -o claude-token-lens.pyz
```

Windows:

```powershell
py -3 claude-token-lens.pyz report
```

This build step is not yet wired into CI or attached to GitHub releases
— run it yourself for now. `report` above will currently only print
"not implemented" (see the Status note); it's shown because it's the
default subcommand. One caveat verified while writing this README:
`zipapp`'s generated `__main__.py` for a `module:function` target calls
the function but never wraps it in `sys.exit(...)`, so a `.pyz` build
always exits 0 even when `main()` returns 1 or 2 — the installed
console script (`pip install .`) does not have this problem. Don't rely
on the `.pyz`'s exit code in a script until this is addressed.

<!-- CLI-USAGE: filled by WP10c -->

### Global flags (`cli.py`)

These are the flags every subcommand parses today, whether or not that
subcommand does anything with them yet:

| Flag | Meaning |
|---|---|
| `--projects-root PATH` | override the `<projects_root>` directory (default: `~/.claude/projects`, or `$CLAUDE_CONFIG_DIR/projects`) |
| `--project NAME` | repeatable; a project slug to include (default: the current directory's own slug) |
| `--all-projects` | include every project under the projects root |
| `--project-family REGEX` | group worktree slugs matching a regex as one project family |
| `--days N` / `--since DATE` | window start (mutually exclusive) |
| `--until DATE` | window end |
| `--limit N` | cap the number of sessions considered |
| `--window-by {mtime,timestamp}` | which timestamp windows and sorts by (default `mtime`) |
| `--pricing PATH` | use a rate card other than the packaged default / config-dir override |
| `--config-dir PATH` | override `~/.claude/token-lens` (or `$CLAUDE_CONFIG_DIR/token-lens`) |
| `--group-by {mode,purpose,agent,project,model,profile}` | grouping axis for tables that support it |
| `--no-cache` / `--rebuild-cache` | parsed on every subcommand (mutually exclusive), but not yet acted on by any subcommand handler — `corpus.load_corpus`, the function that actually consults the digest cache, isn't called from `cli.py` yet |
| `--quiet` / `--verbose` | verbosity (mutually exclusive) |
| `--version` | print the tool version and exit |

`pricing-check` additionally takes `--models ID,ID,...` to resolve
specific model ids against the loaded rate card. `snapshot-config`
additionally takes `--print-hook`, `--install-hook` and `--managed-path`
— see [section 8](#8-installing-the-sessionstart-hook-and-the-statusline).

## 3. The two token totals

Every priced `Turn` (see `model.py`) carries four raw token counts:
`input_tokens`, `cache_creation_tokens`, `cache_read_tokens` and
`output_tokens`. Two different totals matter, and confusing them is the
single easiest way to misread a Claude Code session's cost:

- **Usage tokens** — everything a turn actually processed:
  `input_tokens + cache_creation_tokens + cache_read_tokens +
  output_tokens`. This is what gets billed for that specific API call,
  and it repeats every time the same content is resent as part of a
  growing context.
- **New tokens** — `input_tokens + cache_creation_tokens`
  (`compaction.new_tokens(turn)`): tokens that entered the context for
  the *first* time on this turn, whether typed by you, generated by the
  model, or newly written into the prompt cache. Content already served
  from a warm cache entry (`cache_read_tokens`) is deliberately excluded,
  because it was not new — it was a cheap re-read of something already
  paid for.

**Worked example.** Say a tool call reads a 5,000-token file once, early
in a session, and the file's contents stay in context (via the prompt
cache) for the next 99 turns:

- **New tokens** for that file: 5,000 — charged once, as
  `cache_creation_tokens` on the turn that first read it.
- **Usage tokens** attributable to that file across the session: roughly
  5,000 × 100 = 500,000 — the same 5,000 tokens are re-processed as
  `cache_read_tokens` (much cheaper per token, but not free) on every one
  of the following 99 turns, because Claude Code resends the whole
  context on every turn.

A report that only prints usage tokens makes a session look far more
expensive to *write* than it actually was; a report that only prints new
tokens hides how much volume the cache is carrying. `compaction.py`'s
dropped-token accounting and `ttl.py`'s simulations both use *new*
tokens as the denominator for exactly this reason — see
[section 7](#7-ttl-simulation-assumptions).

## 4. How caching works in Claude Code

Claude Code's prompt cache works on **prefixes**: the system prompt,
`CLAUDE.md`, tool schemas, skill listings and the growing conversation
history form a layered prefix, and a cache write is billed once per
layer per TTL entry. A cache entry has a time-to-live: **5 minutes by
default**, with a documented **1-hour opt-in** available at three levels:

- `promptCacheTtl` in `settings.json` — the main conversation.
- `subagentPromptCacheTtl` in `settings.json` — the default for every
  spawned subagent that doesn't set its own.
- `experimental.cacheTtl` in an individual agent's frontmatter (e.g.
  `.claude/agents/verification-runner.md`) — overrides the subagent
  default for that one agent type.
- The equivalent environment variables `CLAUDE_CODE_PROMPT_CACHE_TTL`
  and `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL`.

A cached prefix survives as long as nothing upstream of it changes and
the entry hasn't expired. What invalidates it, as observed and encoded
in [`events.py`](src/claude_token_lens/events.py)'s `EventKind` table:
a compaction (`COMPACT_BOUNDARY`/`COMPACT_SUMMARY`), a model switch
(`MODEL_FALLBACK`), the `CACHE_SIGNAL` family (a model change, thinking
being stripped, entering/exiting ultra-effort, a change to the set of
deferred or prefix-loaded tool schemas, an MCP-instructions delta, an
agent-listing delta, entering/exiting plan mode or auto mode, an output
style change) — and, simply, the TTL running out between two turns.
`recache.py` (see [section 5](#5-re-cache-definitions-and-signatures))
is the module that detects when one of these actually cost you money.

Subagents each get **their own cache** — a subagent's first turn always
pays a fresh write for its own briefing and system prompt, independent
of whatever the parent conversation's cache state is; `topology.py`
measures this as each agent type's mean first-turn `cache_creation`.

Two practical consequences worth designing around:

- A 1-hour TTL is **ignored while the account is on usage credits**
  (subscription billing) rather than an API key — per Claude Code's own
  documentation. Recommending a 1h switch for a subscription account's
  subagents would recommend a lever that does nothing.
- TTL should be chosen **per spawned agent type**, not globally.
  `ttl.py`'s `TtlStats` is keyed by agent type for exactly this reason:
  a long-wait agent (one that sits idle while you read a build or test
  result — a verifier) tends to benefit from 1h, because its gaps
  regularly exceed 5 minutes; a short-gap agent (an implementer you're
  actively steering, turn after turn within seconds) rarely benefits,
  because its cache almost never has time to expire under 5m anyway.

## 5. RE-CACHE definitions and signatures

A turn is a **re-cache** ([`recache.py`](src/claude_token_lens/recache.py))
when all of the following hold:

- it is not the transcript's first priced turn,
- its context (`ctx`) exceeds `ctx_floor` (default 20,000 tokens), and
- the fraction of that context actually served from cache read falls
  below `cr_ratio` (default 0.2, i.e. less than 20% of context came from
  a cache hit) —

in other words: the model had to pay to write most of its own context
again, instead of reading a warm cache entry, for no correctness reason.
Every re-cache turn is assigned one of two **signatures**:

- **`full-expiry`** — `cache_read_tokens` is below `full_expiry_cr`
  (default 2,000 tokens): the cache entry had essentially nothing left
  to hit, consistent with its TTL having simply run out since the
  previous turn.
- **`prefix-invalidated`** — `cache_read_tokens` sits between
  `full_expiry_cr` and `cr_ratio × ctx`: there was a partial hit, so the
  TTL had *not* expired, but something upstream of the cached prefix
  changed anyway (a notification, an attachment, a model switch, a
  compaction, ...) and broke it regardless.

A re-cache turn's **avoidable cost** is what its own cache-creation
tokens cost at the write rate they were actually billed at, minus what
those same tokens would have cost at the flat cache-read rate had the
cache not been invalidated. Every threshold above is overridable via
`config.toml`'s `[thresholds]` table (`RecacheThresholds.from_config`).

## 6. Reading the report sections

There is no assembled `report` yet (see the Status note above), but
every section below is implemented as a `build_section(...)` function
returning a `Section` of `Table`s ([`model.py`](src/claude_token_lens/model.py)),
fully tested, and runnable today from a short Python script against your
own transcripts. The table below is what exists; the "not yet built"
list underneath it is honest about the gap against the full project
plan.

| Section key | Title | Module | What it answers |
|---|---|---|---|
| `sessions` | Sessions | `classify.py` | mode (interactive/long-agentic/overnight/mixed) and purpose (docs/refactor/test-triage/...) per session, with the evidence that produced each classification |
| `recache` | Re-cache events | `recache.py` | which turns paid to re-write a prefix that should have been a cache hit, why, and what it cost — see section 5 |
| `ttl` | Cache TTL break-even | `ttl.py` | per agent type: observed cost vs. simulated 5m-only/1h-only cost, plus the utilisation metrics below |
| `compactions` | Compactions | `compaction.py` | compaction count, trigger mix, pre/post/dropped tokens, and the re-cache cost of the turn right after each compaction |
| `agents` | Agents and information flow | `topology.py` | downward cost (briefing/system-prompt writes into each agent type), upward cost (`Agent`/`Workflow` tool-result sizes flowing back), skill roll-ups, spawn-depth chains |
| `workstyle` | Workstyle | `workstyle.py` | one archetype per session/corpus: `overseer-fanout`, `plan-high-implement-low`, `workflow-heavy`, `effort-varied`, `chat-only`, `single-model`, with the evidence features |
| `workflows` | Workflows | `workflows.py` | per-run agent count, phase count, duration and cost from `<session>/workflows/wf_*.json` |
| `phases` | Phases | `phases.py` | cost split across DISCOVERY (read/search only), IMPLEMENTATION (real edits or an ordinary shell command), VERIFICATION (a test/build tool, or a scratch-file edit), OTHER |
| `config_diff` | Config diff | `snapshots.py` | which config keys changed across a window, and how sessions grouped by one key's value compare, from `hooks/snapshot-config.py`'s captured snapshots |
| `usage_windows` | Usage windows | `tools/log_usage.py` | your 5-hour/7-day plan usage-window percentages over time, from a `log-usage` paste or the statusline logger, with a token-volume regression per window |

**Not yet built:** an `overview` section, a general day/week/month
`usage` section (the plan's Finance features), a `scorecard.py`
optimisation-level module, a `recommend.py` recommendation engine, and
the `report`/`sessions`/`recache`/`ttl`/`compactions`/`config-diff`
CLI subcommands that would assemble and print all of the above as one
document. `Diagnostics` (parse-quality counters: unparsable lines,
synthetic turns, TTL-sum mismatches, ...) exists as a dataclass on every
`TranscriptResult` but has no dedicated report section yet either.

### The TTL section's utilisation metrics

Beyond "which fixed policy would have been cheaper", `ttl.py` answers
"was the cache I paid for actually used": **wasted writes** (a
cache-creation write never read back before it expired), **1h-premium
waste vs. 5m-expiry loss** (the write premium a 1h TTL paid but never
earned back, and symmetrically what a 5m TTL lost to expiries a 1h TTL
would have survived), **break-even share** (the point, from the
resolved rate card, where the two policies cost the same), a
**near-miss histogram** (turns whose gap just missed or just made a TTL
boundary), and **cache ROI** (total saved by caching at all, as a ratio
to what was spent on writes — independent of the 5m-vs-1h question).
Only **prefix-invalidated** re-cache turns are addressable by a TTL
change; a `full-expiry` turn's cache had genuinely run out, which no
TTL choice below the gap length would have prevented. See
[`docs/sections-reference.md`](docs/sections-reference.md#ttl-utilisation-metrics)
for the full definitions and field names.

### Worked example (real, scrubbed session)

[`docs/sections-reference.md`](docs/sections-reference.md#worked-example) has
a real re-cache and compaction summary produced by running
`recache.build_section`/`compaction.build_section` against
[`tests/fixtures/real/session-a`](tests/fixtures/real/session-a) — a
genuine session put through `tools/scrub.py`'s whitelist rewrite, so
every id is HMAC-rehashed and every string field is either a
documented-safe value or an `x`-filled placeholder. Nothing in it is
invented. The same file also lists the remaining tables each section
produces (gap buckets, primary-cause attribution, per-agent-type
breakdowns, trigger mix, and so on) and how to read each column.

## 7. TTL simulation assumptions

Printed verbatim from `ttl.ASSUMPTIONS` — this is exactly what the 5m/1h
break-even simulation assumes, stated so a reader can judge for
themselves where the model might not hold for their own working style:

- content is TTL-invariant
- cacheable prefix `C_i = cache_read_i + cache_creation_i`
- a hit refreshes TTL so survival depends only on `gap_s`
- prefix-invalidated turns keep their observed split under every policy
  (no double counting)
- reads are priced at the flat cache_read rate
- compaction shrink clamps write at 0
- gap is measured from the start of one request to the start of the next

A per-transcript **fidelity self-check** replays the simulation at the
transcript's own dominant observed TTL and compares it to the actually
observed cost; `ttl.build_section` flags any agent type whose fidelity
error exceeds 10% (`TtlThresholds.fidelity_warn_pct`), so a report never
presents a simulated number as trustworthy when the model's own
assumptions demonstrably don't fit that agent type's transcripts.

## 8. Installing the SessionStart hook and the statusline

### SessionStart config-capture hook

`hooks/snapshot-config.py` is a standalone stdlib script — it
deliberately imports nothing from this package, so it keeps working if
copied on its own onto a machine that only has a bare Python
interpreter. Installed and run today via the working `snapshot-config`
CLI subcommand:

```bash
claude-token-lens snapshot-config --install-hook   # copies the script into <config-dir>/token-lens/hooks/
claude-token-lens snapshot-config --print-hook      # prints the settings.json fragment below
```

`--print-hook` prints both variants; merge the relevant one into the
`"hooks"` key of `~/.claude/settings.json` (`SessionStart` may already
have other entries — add to the list, don't replace it):

Windows:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "py -3 \"%USERPROFILE%\\.claude\\token-lens\\hooks\\snapshot-config.py\""
          }
        ]
      }
    ]
  }
}
```

POSIX (Linux/macOS):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/.claude/token-lens/hooks/snapshot-config.py\""
          }
        ]
      }
    ]
  }
}
```

The hook always exits 0 and prints nothing on success (or a single
stderr line on failure) so a broken Python can never block a session
start. It writes one snapshot JSON file per run (skipped when an
unchanged snapshot already exists within `--min-interval`, default 300s)
to `<config-dir>/token-lens/snapshots/<UTC compact timestamp>.json`,
capturing:

- **Environment variable names only, never values**, for every name
  matching `ANTHROPIC_*` / `CLAUDE_*`.
- **Settings values, but only for a small allowlist**: `model`,
  `effortLevel`, `outputStyle`, `autoCompactWindow`, `promptCacheTtl`,
  `subagentPromptCacheTtl`, `cleanupPeriodDays`,
  `desktopSessionCleanupPeriodDays`, `autoUpdatesChannel`,
  `alwaysThinkingEnabled`, plus any plain `bool`/`int` value (a toggle or
  a limit, never content). Every other settings key is replaced by a
  shape-only marker (`dict(n)`, `list(n)`, `str(len)`).
- The same redaction rule applied to every agent's frontmatter (model,
  effort, `maxTurns`, `omitClaudeMd`, nested `experimental.cacheTtl` kept
  verbatim; `description` always reduced to `str(len)`), MCP server
  names, and enabled plugin names (names only).
- **Managed (enterprise-policy) settings**, read the same way from the
  platform's system-wide policy file and redacted identically into
  `managed_settings`, with its raw top-level key names (never values)
  recorded verbatim into `managed_keys` — so a future recommendation can
  say "this lever is managed by policy" for any key that appears there.
  Platform paths: macOS
  `/Library/Application Support/ClaudeCode/managed-settings.json`, Linux
  `/etc/claude-code/managed-settings.json`, Windows
  `%ProgramData%\ClaudeCode\managed-settings.json` (override with
  `--managed-path`).

### Statusline

The statusline is a separate, self-contained entry point — it is *not*
one of `cli.py`'s stub subcommands (that `statusline` entry still prints
"not implemented"); invoke the module directly:

```bash
python -m claude_token_lens.statusline --print-install-fragment
```

Merge the result into `~/.claude/settings.json` (replaces any existing
`"statusLine"` key):

Windows:

```json
{ "statusLine": { "type": "command", "command": "py -3 -m claude_token_lens.statusline" } }
```

POSIX (Linux/macOS):

```json
{ "statusLine": { "type": "command", "command": "python3 -m claude_token_lens.statusline" } }
```

On every status-line refresh, Claude Code writes a JSON payload to this
script's stdin; the statusline reads `context_window.used_tokens` (a
compact `ctx NNk` figure), `prompt_cache` (a cache hit-ratio percentage),
`rate_limits.{five_hour,seven_day}.used_percentage` (plan usage-window
percentages — also appended to `~/.claude/token-lens/usage-log.csv`,
deduped by session/reset-time/percentage), and the last 64 KB of
`transcript_path` (never the whole file) to compute seconds remaining
before the current TTL entry expires. It never raises: any failure
anywhere in that path falls back to printing a minimal `token-lens` line
rather than blanking the status bar.

## 9. Windows notes

- **Slug case variants.** A project slug is derived from the working
  directory (`discovery.slug_for`); the projects directory is
  de-duplicated across sessions by
  `os.path.normcase(os.path.realpath(path))`, so `C--Dev-RevIXO` and
  `c--Dev-RevIXO` resolve to the same project on a case-insensitive
  filesystem. `--project-family REGEX` groups worktree slugs of the same
  underlying project together.
- **MSYS / Git Bash paths.** The privacy scan (`helpers.assert_privacy`,
  exercised by `tests/test_privacy.py`) explicitly checks for an
  MSYS-style drive path (`/c/Users/...`) alongside a Windows drive path
  (`C:\...`) and a `\Users\` segment, since a command run from Git Bash
  on Windows produces that third form.
- **Long paths.** Paths at or beyond 255 characters get the `\\?\`
  extended-length prefix (`\\?\UNC\` for a UNC path) before being opened,
  on Windows only (`jsonl._windows_long_path`).
- **Live files.** A transcript whose `mtime` is under 60 seconds old is
  treated as still being actively written and is read with
  `errors="replace"`, never cached (`cache.py`), and tolerated even if
  its final line is truncated mid-write.
- **`CLAUDE_CONFIG_DIR`** moves the whole config tree: both
  `<projects_root>` (`.../projects`) and claude-token-lens's own
  `<config-dir>` (`.../token-lens`) resolve underneath it when set,
  instead of `~/.claude`.
- **`CLAUDE_CODE_PROJECT_DIR_NAME`** overrides the computed slug for the
  *current* project directory only, independently of whether
  `CLAUDE_CONFIG_DIR` also moved the whole tree.

## 10. For team leads and enterprise

What's implemented today:

- **`exclude_projects`** (`config.toml`, a list of slug regexes) excludes
  matching projects from discovery entirely — for a confidential repo
  you never want scanned, not just hidden from output. A malformed regex
  is skipped, never fatal.
- **`retention_days`** (`config.toml`, an integer) is read and validated
  as a setting for the eventual local SQLite store's own retention
  policy; nothing enforces it yet, since that store (the v0.2 service)
  doesn't exist in this codebase.
- **Managed settings** are captured by the snapshot hook (see section 8)
  into `managed_settings` (redacted the same as user settings) and
  `managed_keys` (raw key names only) — the data a future "raise this
  with your administrator" recommendation would need is already
  recorded; the recommendation engine that would render it doesn't exist
  yet (no `recommend.py` in this codebase).
- **Provider detection.** `parse.detect_provider` classifies each turn's
  billing surface from the model id's own shape: an
  `anthropic.`/`us.anthropic.`-prefixed or `-v1:0`-suffixed id is
  Bedrock, an `@<date>`-suffixed id is Vertex, anything else is direct
  Anthropic API. This is recorded on `TranscriptMeta.provider` and
  `Config.provider`. **Not yet implemented:** Foundry detection (only
  `anthropic`/`bedrock`/`vertex` are recognised today), and any
  per-provider rate override in `pricing.toml` — every provider is
  currently priced against the same Anthropic-direct rate card.

What's planned but not yet built: an aggregate-only `export` command
(per-archetype/purpose/agent-type rollups with slugs hashed, no session
ids) — there is no `export` subcommand or module in this codebase yet.
Until it exists, the only "team" workflow available is running
claude-token-lens's own modules locally against each person's own
`~/.claude/projects`.

## 11. Privacy and security

See [SECURITY.md](SECURITY.md) for the full sign-off checklist. In
short: every dataclass field is length-capped and shape-checked so
message text, tool results, full paths and full commands never reach a
report — enforced today by:

```bash
python -m pytest tests/test_privacy.py tests/test_scrub.py -q
```

`tests/test_privacy.py` walks every field `parse_transcript` produces
across a battery of synthetic fixtures and asserts no `str` field
exceeds 64 characters outside a small, named allowlist (agent type,
model id, tool names, session id, slug, attachment subkind, the
transcript's own file path), that `cmd_prefix`/`preceding_cmd_prefix`
never exceed 40 characters, and — via `tests/helpers.assert_privacy` —
that no field matches a Windows drive path, a POSIX `/home/` path, a
`\Users\` path, an MSYS drive path, a bare `@`, or a URL.

If you generate your own JSON or HTML output by calling
`render.json_out`/`render.html` directly (there is no wired `report`
command to do this for you yet — see section 6), you can run the same
kind of audit by hand over the file:

```bash
grep -RnoE '[^"]{65,}|[A-Za-z]:\\\\|/home/|\\\\Users\\\\|/c/Users/|@' report.json report.html
```

Treat any hit as a bug and open an issue with the field name (never the
value).

## 12. Prior art and credits

- **[nateherkai/token-dashboard](https://github.com/nateherkai/token-dashboard)**
  — the closest prior art: stdlib Python + SQLite + a web UI, dedupes by
  `message.id`, per-prompt cost, a tips tab. claude-token-lens adds TTL
  break-even simulation, RE-CACHE cause attribution, per-subagent-type
  accounting, and config snapshot/diff — none of which token-dashboard
  covers.
- **[cebert/cache-ttl-analyzer](https://github.com/cebert/cache-ttl-analyzer)**
  — a 5m-vs-1h replay for the main conversation. claude-token-lens
  extends the same idea to every subagent type independently, adds the
  wasted-write/premium/near-miss utilisation metrics, and ports the
  simulation to Python against the real transcript format rather than a
  fixed-price TypeScript model.
- **[ryoppippi/ccusage](https://github.com/ryoppippi/ccusage)** — daily
  and monthly cost tables across multiple CLIs. claude-token-lens is
  Claude-Code-specific and goes deeper on one client: the 5m/1h split,
  RE-CACHE detection, per-subagent-type attribution and config-aware
  recommendations that ccusage's broader scope doesn't attempt.
- **[Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor)**
  and the wider `claude-usage` ecosystem — real-time quota/5-hour-window/
  burn-rate monitoring. claude-token-lens is a complementary, offline
  analytics tool: it explains *why* a session cost what it did (caching,
  config, workstyle) rather than watching the live burn rate.

## 13. Licence, contributing, roadmap

**Licence:** MIT — see [LICENSE](LICENSE).

**Contributing:**

```bash
pip install -e .[test]
pytest -q
```

Conventional commits (`feat(parse): ...`, `test(ttl): ...`,
`docs(readme): ...`), one focused commit per change. `tests/helpers.py`
has the fixture builders used throughout the test suite
(`turn_line`, `attachment_line`, `system_line`, `write_jsonl`,
`assert_privacy`, ...) — start there before writing a new test fixture
by hand.

**Roadmap** (see the project plan for full detail; nothing below exists
in this codebase yet):

- **v0.2** — report assembly, the recommendation engine, the
  optimisation scorecard, `claude-token-lens serve` (a local read-only
  service: watcher thread, SQLite store, `http.server` JSON API and a
  dependency-free static web UI), and Docker packaging.
- **v0.3** — a `baseline`/onboarding capture window, a profile schema
  and catalogue, and `apply`/`--revert` for writing a chosen profile
  into `settings.json`/agent frontmatter.
