# Configuration layers and per-project effective config

This is the field-by-field companion to the plan's "Configuration layers
and per-project effective config" section and Appendix A6, for schema 2
of the config snapshot (`hooks/snapshot-config.py`, `snapshots.py`).
Schema 2 is additive over schema 1 — every schema-1 field keeps the same
name and shape, and `snapshots.load_snapshots` still loads a schema-1
file unchanged.

## The layer model

Claude Code resolves settings from several files, highest precedence
first. Schema 2 records each one separately (`settings_layers`) as well
as the merged result (`effective`), so a report can say not just *what*
a lever is set to but *which file* set it:

| Layer            | File                                | Notes |
|------------------|--------------------------------------|-------|
| `managed`        | the platform's `managed-settings.json` | enterprise policy, outside any user's control (macOS `/Library/Application Support/ClaudeCode/…`, Linux `/etc/claude-code/…`, Windows `%ProgramData%\ClaudeCode\…`) |
| `project_local`  | `<project>/.claude/settings.local.json` | per-machine, not checked in |
| `project_shared` | `<project>/.claude/settings.json`   | checked in, shared with a team |
| `user`           | `~/.claude/settings.json`            | this machine's default |

`SETTINGS_LAYER_ORDER` in the hook (`SETTINGS_LAYER_NAMES` in
`snapshots.py`) fixes this precedence; `effective`/`effective_provenance`
walk it in this order and the first layer that defines a key wins.
`claude --settings` one-launch overlays and shell-profile environment
variables sit outside this file-based model entirely — the hook can't
see them, which is exactly what `detect_drift`/`build_config_drift_table`
exist to surface (a session whose *observed* behaviour disagrees with
its snapshot's `effective` config implies one of these).

## What each layer records (`settings_layers[<layer>]`)

- `present` — whether the file exists at all.
- `source_path_hash` — `sha256:<hex>` of the file's own path. The raw
  path is never recorded.
- `content_hash` — `sha256:<hex>` of the raw JSON, so two layers (or the
  same layer across two machines) can be compared for equality without
  diffing the file itself. `null` when `present` is `false`.
- `redacted` — the whole file, redacted key-by-key with the existing
  allowlist rule (see "Redaction rule" below).
- `env_names` — the settings file's own `env` block, **names only**.
- `permissions` — `{allow_count, deny_count, ask_count, default_mode}`.
  Never the allow/deny/ask rules themselves — a Bash allowlist entry is
  exactly the kind of content this hook must not record.
- `hooks` — `{event_name: entry_count}`. Never the commands a hook runs.
- `enabled_plugins` — plugin names from this layer's `enabledPlugins`.
- The named safe scalars, verbatim: `model`, `effort_level`,
  `always_thinking_enabled`, `auto_compact_window`, `prompt_cache_ttl`,
  `subagent_prompt_cache_ttl`, `cleanup_period_days`, `output_style`.
- `statusline_present` — a boolean, never the statusline command itself.

## Redaction rule (settings and agent frontmatter alike)

A value is kept as-is only if its key is on the safe allowlist, or the
value itself is a `bool`/`int`/`float`/`None` (a toggle or a small limit,
never content). Everything else becomes a shape-only marker: `dict(n)`,
`list(n)`, or `str(len)`. This applies uniformly, so an unknown key in a
future Claude Code version degrades safely instead of leaking its value.

**Safe settings allowlist** (kept verbatim): `model`, `effortLevel`,
`outputStyle`, `autoCompactWindow`, `autoCompactEnabled`,
`promptCacheTtl`, `subagentPromptCacheTtl`, `cleanupPeriodDays`,
`desktopSessionCleanupPeriodDays`, `autoUpdatesChannel`,
`alwaysThinkingEnabled`.

Two keys get their own summary shape instead of either "kept verbatim"
or the generic marker:

- `statusLine` → a bare `true`/`false` (a report only ever needs "is a
  statusline configured", never the command it runs).
- `modelPricing` → `{"present": bool, "model_ids": [...]}` — **the
  model ids it overrides, never the overridden numbers**. Those numbers
  are exactly the kind of "silently adopt whatever the file says" figure
  this project's own `pricing.toml` exists to keep user-editable and out
  of code.

`effective`/`effective_provenance` merge exactly this same key set
(`SETTINGS_SUMMARY_KEYS`) across the four layers — a value there has
already gone through the same redaction, so `effective["statusLine"]`
is a bool and `effective["modelPricing"]` is the present/model-ids shape,
never a raw value.

## Environment variables

Only variable **names** are ever recorded (`env_names`), never values,
with one deliberate exception below. Captured names are anything
matching a prefix in `ANTHROPIC_*` / `CLAUDE_*` / `OTEL_*`, plus a fixed
list of irregularly-named levers: `MAX_THINKING_TOKENS`,
`DISABLE_NON_ESSENTIAL_MODEL_CALLS`, `MAX_MCP_OUTPUT_TOKENS`,
`BASH_MAX_OUTPUT_LENGTH`. The `CLAUDE_*`/`ANTHROPIC_*` prefixes alone
already cover most of the documented Claude Code levers by name —
`CLAUDE_CODE_SUBAGENT_MODEL(_FORCE)`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`,
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, `CLAUDE_CODE_USE_BEDROCK`/`_VERTEX`/`_FOUNDRY`,
`ANTHROPIC_DEFAULT_OPUS_MODEL`/`_SONNET_MODEL`/`_HAIKU_MODEL`/`_FABLE_MODEL` —
without needing an individual name added; `OTEL_*` covers OpenTelemetry
export configuration the same way.

**Exception — numeric caps** (`env_numeric_caps`): `MAX_THINKING_TOKENS`,
`MAX_MCP_OUTPUT_TOKENS`, `BASH_MAX_OUTPUT_LENGTH`, and
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` are limits, not secrets or content —
the integer value itself is recorded alongside the name. A value that
doesn't parse as an integer is silently skipped from
`env_numeric_caps` (the name still appears in `env_names`). Every other
captured environment variable stays names-only.

## `effective_agents`

`agents` (schema 1) is extended with `source` (`"user"` or `"project"`)
and, on a name clash, `shadowed_by_project: true` on the project entry
that won the merge (project agents win, matching Claude Code's own
resolution order). `effective_agents[name]` reduces this to exactly the
fields a TTL/model/effort recommendation keys off:
`{source, experimental_cache_ttl, model, effort, max_turns}`.

## `claude_json` — the `~/.claude.json` cross-check

`~/.claude.json` is the CLI's own per-machine state file — distinct from
any Claude Code *settings* file. Schema 2 reads the entry for the
current project, matched by comparing
`os.path.normcase(os.path.realpath(...))` on every project key against
the current directory: on a real machine this file has been observed to
hold the *same* directory under several spellings at once (forward
slashes, backslashes, drive-letter case), so an exact string match would
silently miss it. **The raw matching key is never stored** — only the
matched entry's own values.

When matched, `claude_json` records:

- `mcp_servers` — MCP server names from the project's own `mcpServers`.
- `enabled_mcpjson_servers` / `disabled_mcpjson_servers` — names.
- `allowed_tools_count` — a count, never the tool list.
- `has_trust_dialog_accepted` — a boolean.
- `last_session` — the project's own `last*` session-statistics fields,
  if present: `lastCost`, `lastDuration`, `lastAPIDuration`,
  `lastTotalInputTokens`, `lastTotalOutputTokens`,
  `lastTotalCacheCreationInputTokens`, `lastTotalCacheReadInputTokens`,
  `lastSessionId` (an opaque id, never message text), `lastLinesAdded`,
  `lastLinesRemoved`. `snapshots.claude_json_cross_check` joins this
  against this tool's own per-session totals by `lastSessionId`, and
  reports any numeric disagreement — a sanity check on both sides'
  accounting for the same session, never anything content-bearing.
- `top_level` — `num_projects` (the size of the `projects` dict) plus
  `scalars`: every top-level `~/.claude.json` field that is itself a
  `bool`/`int`/`float` kept verbatim, or a `str` reduced to `str(len)`
  (e.g. `numStartups`, `autoUpdates`). Dict/list top-level fields
  (`oauthAccount`, `tipsHistory`, …) are skipped entirely — their shape
  is unpredictable and could hold identity data, unlike the small fixed
  set of per-project fields handled above.

A missing, unreadable, or malformed `~/.claude.json` degrades to the bare
`{"matched": false}` — never raises. A `~/.claude.json` that reads fine
but has no entry for the current project still reports `"matched":
false`, but keeps the `top_level` block described above (there is
nothing project-specific to redact, but the file's own top-level scalars
are still safe to report): `{"matched": false, "top_level": {...}}`.

## `content_layers`

Sizes, counts and names only — **never content** — for every content
layer the plan's "Configuration layers" section lists:

- `claude_md` — byte counts for the user (`~/.claude/CLAUDE.md`),
  project root (`CLAUDE.md`), and project-local (`CLAUDE.local.md`)
  files, plus a bounded walk's count/total-bytes for nested `CLAUDE.md`
  files below the project root. The walk is depth-limited (≤6) and
  visits at most 5,000 directories, skipping `.git`, `node_modules`,
  `.venv`, `bin`, `obj` — so a huge or symlink-cyclic tree can't make a
  session start hang.
- `rules` / `commands` — count and total bytes of
  `.claude/rules/*.md` / `.claude/commands/**/*.md`.
- `skills` — `{project, user}`, each `{names, total_bytes}` from
  `.claude/skills/*/SKILL.md` (a skill directory without a `SKILL.md`
  isn't a skill Claude Code will load, so it's excluded).
- `agents_summary` — `{count, user_count, project_count,
  shadowed_count}`, rolled up from `agents`'s own source/shadow flags.
- `mcp_json` — `{present, names}` for the project's own `.mcp.json`.
- `managed_mcp_present` — whether `<claude root>/managed-mcp.json`
  exists.
- `output_styles` — names from `<claude root>/output-styles/*.md`.
- `memory` — `{present, files, bytes}` for this project's auto-memory
  directory (`~/.claude/projects/<slug>/memory/`) — never its content.
- `plugins` — `{names, marketplaces}` — installed plugin directory
  names and a marketplace count, never plugin content.
- `claude_config_dir_set` — whether `CLAUDE_CONFIG_DIR` is set at all
  (never its value, which is a path).

## `project_slug`

The same algorithm `discovery.slug_for` uses (duplicated in the
standalone hook rather than imported — see that file's module
docstring): non-alphanumeric characters become `-`, truncated to 200
characters plus an 8-hex-char hash when longer. Honours
`CLAUDE_CODE_PROJECT_DIR_NAME` the same way `slug_for` does. The slug is
deliberately *not* treated as a raw path needing a hash — it's already
the on-disk directory name every transcript under
`~/.claude/projects/<slug>/` uses, and the non-alnum substitution means
it no longer contains a drive-letter colon or path separator.

## Multi-project tables (`snapshots.py`)

A single `<config-dir>/snapshots/` directory accumulates snapshots from
every project the hook has ever run in (exactly like
`~/.claude/projects/` itself), identified by each snapshot's own
`project_slug`. A schema-1 snapshot (no `project_slug`) collapses into
one `"(unknown project)"` bucket.

- `build_effective_config_table` — one row per (project, key) for each
  project's *latest* snapshot: the value in effect and which layer
  supplied it.
- `build_config_layers_table` — one row per (project, layer): whether
  that layer is present, plus a per-project content-layer summary
  (agent/skill/rule/command counts, total CLAUDE.md bytes, MCP server
  count) so a reader sees a project's whole config footprint in one
  table.
- `build_config_groups_table` — projects grouped by an identical
  *current* effective-config hash, with an optional session count per
  group when the caller supplies session data.
- `build_config_drift_table` / `detect_drift` — sessions whose
  *observed* behaviour (a caller-computed dominant model, TTL mix, or
  effort mode — this module never touches turn data itself) disagrees
  with what their joined snapshot's `effective` config says should be
  in effect. A mismatch implies a shell-profile environment variable or
  a `--settings` one-launch overlay the hook cannot see — evidence, not
  proof.

`build_config_section(..., include_effective=True, sessions_with_observed=...)`
appends these tables to the existing config-diff section; both keyword
arguments are optional and off by default, so an existing caller keeps
getting exactly the one table it always has.

## CLI

- `claude-token-lens snapshot-config --project-dir PATH` — runs the same
  hook logic for an explicit project directory instead of the current
  one. (Named `--project-dir`, not `--project`: the common `--project`
  flag every subcommand already has means "a repeatable project slug to
  filter a report by" — reusing it here for a single directory path
  would collide with that meaning.)
- `claude-token-lens probe-config [--project-dir PATH]` — runs the same
  scan **without** a session and without writing a snapshot file; prints
  the settings-layers and effective-config tables as Markdown. Defaults
  to the current directory. Its output never contains a raw path —
  only the project slug and content hashes, matching the hook's own
  privacy posture.

## What `init` (v0.3) will ask

The planned `init` command (v0.3 milestone) uses this same schema-2 scan
to open with a project-specific summary instead of a blank slate:

- Which settings layers are present for this project, and which one
  currently wins each effective-config key — so a suggested change can
  say *"set in `.claude/settings.json`, currently overridden by your
  user settings"* instead of guessing.
- Whether this project's agents are entirely inherited from `~/.claude/
  agents/`, or already has project-level overrides (`shadowed_by_project`)
  worth confirming before `init` proposes new ones.
- Whether a `.mcp.json`/managed MCP config already exists, so `init`
  doesn't propose a redundant server list.
- The project's current CLAUDE.md/rules/commands footprint (byte counts
  only), to calibrate whether `init` should propose splitting a large
  `CLAUDE.md` into `.claude/rules/*.md` files.
- Whether `~/.claude.json` already has a trust-dialog-accepted entry and
  prior session totals for this project, so `init` can skip
  onboarding questions Claude Code itself has already answered.

None of this reads message text, file contents, or raw paths — the same
guarantee schema 2 already provides for reporting.
