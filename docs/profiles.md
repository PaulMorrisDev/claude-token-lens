# v0.3 profiles

`claude_token_lens.profiles` (plan Milestone v0.3: "baseline capture,
archetype, profiles, apply") is the schema, shipped catalogue, and diff
renderer for a **profile**: a small, allowlisted bundle of Claude Code
settings/agent-frontmatter/environment-variable levers that a user can
apply to a project. This document is the frozen contract for the
package — a later work package wires `cli.py`'s `apply`/`init`/
`baseline` subcommands and the two `/api/profiles*` routes
(`docs/api.md`) against it; nothing in `profiles/` touches those files
itself.

A profile only ever names a lever this project can already trace back
to a real, observable effect in a report — never an invented setting —
so every allowlisted key below carries a doc reference back to the
config layer or report table that justifies it.

## What a profile is

A profile is a TOML document with up to eight top-level keys: `id`,
`name`, `for`, `archetype`, `settings`, `agents`, `env`, `notes`. See
Appendix A7 of the v0.3 plan for the illustrative shape; `schema.py`'s
`Profile` frozen dataclass is the in-memory form (`for` is renamed
`for_`, since `for` is a Python keyword, and a loaded-from-file
profile also carries a `source_path` that is never written back out —
see "What a profile cannot do" below).

| Field | Type | Meaning |
|---|---|---|
| `id` | string, `^[a-z0-9-]{1,40}$` | Stable identifier; also the catalogue filename and the `apply`/API argument. |
| `name` | string | Human-readable display name. |
| `for` | list of strings | Free-text purpose tags (not validated against the purpose list below — a hint for a human browsing the catalogue). |
| `archetype` | one of the seven workstyle archetypes, or absent | The `workstyle.detect_archetype` corpus shape this profile targets. |
| `settings` | table | `settings.json`-layer overrides — see the settings table below. |
| `agents` | table of `agents.<name>` sub-tables | Per-agent frontmatter overrides — see the agent table below. |
| `env` | table | Environment variable *names* this profile mentions (values are always supplied by the user at apply time, never stored in the profile — see "What a profile cannot do"). |
| `notes` | string | Free text citing the real report table/column that justifies this profile's settings. Never fabricated numbers. |

`schema.validate(d) -> list[str]` returns every problem with a
parsed-TOML-or-JSON dict `d` (empty list means valid); `load_dict`/
`loads_profile`/`load_profile` raise `ProfileError(problems)` on any
problem. `dump_profile(profile) -> str` is a hand-rolled, deterministic
TOML emitter (the standard library has no TOML writer) — the same
profile always serialises to byte-identical text regardless of the
input dict's key order, and a `load_profile → dump_profile →
loads_profile` round trip returns an equal `Profile` (`source_path`
excluded, since it is excluded from equality too).

## The allowlist: what a profile may contain

`SETTINGS_ALLOWLIST`, `AGENT_ALLOWLIST` and `ENV_ALLOWLIST` in
`schema.py` are the single source of truth — `validate()`'s rejection
messages and this document's tables are both generated from the exact
same three dicts, so they cannot drift apart silently. A profile
naming any other key is rejected outright.

### `settings` (top-level `settings.json` overlay keys)

| Key | Type | Allowed values | Doc reference |
|---|---|---|---|
| `model` | string | any | `docs/config-layers.md#the-layer-model` |
| `effortLevel` | enum | `low`, `medium`, `high`, `max` | `docs/config-layers.md#what-each-layer-records-settings_layerslayer` |
| `autoCompactWindow` | int | 0–1,000,000 | `docs/config-layers.md#what-each-layer-records-settings_layerslayer` |
| `outputStyle` | string | any | `docs/config-layers.md#what-each-layer-records-settings_layerslayer` |
| `promptCacheTtl` | enum | `5m`, `1h` | `docs/api.md#get-apittl` |
| `subagentPromptCacheTtl` | enum | `5m`, `1h` | `docs/api.md#get-apittl` |
| `enabledPlugins` | list of strings | any plugin names | `docs/config-layers.md#content_layers` |
| `disabledMcpjsonServers` | list of strings | any server names | `docs/config-layers.md#claude_json----the-claudejson-cross-check` |
| `enabledMcpjsonServers` | list of strings | any server names | `docs/config-layers.md#claude_json----the-claudejson-cross-check` |
| `alwaysThinkingEnabled` | bool | — | `docs/config-layers.md#redaction-rule-settings-and-agent-frontmatter-alike` |
| `autoCompactEnabled` | bool | — | `docs/config-layers.md#redaction-rule-settings-and-agent-frontmatter-alike` |
| `cleanupPeriodDays` | int | 0–3,650 | `docs/config-layers.md#redaction-rule-settings-and-agent-frontmatter-alike` |

### `agents.<name>` (per-agent frontmatter overrides)

| Key | Type | Allowed values | Doc reference |
|---|---|---|---|
| `model` | string | any | `docs/config-layers.md#effective_agents` |
| `effort` | enum | `low`, `medium`, `high`, `max` | `docs/config-layers.md#effective_agents` |
| `maxTurns` | int | 1–1,000,000 | `docs/config-layers.md#effective_agents` |
| `omitClaudeMd` | bool | — | `docs/sections-reference.md` |
| `memory` | string | any | `docs/config-layers.md#content_layers` |
| `tools` | list of strings | any | `docs/config-layers.md#content_layers` |
| `disallowedTools` | list of strings | any | `docs/config-layers.md#content_layers` |
| `skills` | list of strings | any | `docs/config-layers.md#content_layers` |
| `"experimental.cacheTtl"` | enum | `5m`, `1h` | `docs/config-layers.md#effective_agents` |

`"experimental.cacheTtl"` may be written either as that dotted key, or
as a nested `[agents.<name>.experimental]` table with a `cacheTtl` key
— both forms normalise to the dotted key in the loaded `Profile`.
Giving both with different values is rejected; a nested `experimental`
table holding any key other than `cacheTtl` is rejected.

### `env` (environment variable names)

Only the variable *name* is allowlisted; the value is always a
free-form string the user supplies when the profile is applied (see
"What a profile cannot do"). Allowed names:

`CLAUDE_CODE_PROMPT_CACHE_TTL`, `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL`,
`CLAUDE_CODE_SUBAGENT_MODEL`, `MAX_THINKING_TOKENS`,
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, `MAX_MCP_OUTPUT_TOKENS`,
`BASH_MAX_OUTPUT_LENGTH`, `DISABLE_NON_ESSENTIAL_MODEL_CALLS`,
`ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`,
`ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_DEFAULT_FABLE_MODEL`
(doc reference for all: `docs/config-layers.md#environment-variables`).

### Archetypes

`archetype`, if given, must be one of the seven values
`workstyle.detect_archetype`/`corpus_archetype` can return:
`overseer-fanout`, `plan-high-implement-low`, `workflow-heavy`,
`effort-varied`, `single-model`, `chat-only`, `mixed`.

## The catalogue: seven shipped starting points

`profiles/catalogue/*.toml` ships exactly seven profiles
(`catalogue.CATALOGUE_IDS`), each a normal profile document loaded and
validated the same way any other profile is, with `notes` citing a
real report table/column rather than an invented number.

| id | `for` | archetype | key settings | justification (report table/column) |
|---|---|---|---|---|
| `interactive-chat` | chat, quick-question, pairing | `chat-only` | `effortLevel=medium`, `promptCacheTtl=5m` | `ttl.ttl_by_agent_type`'s top-level `gap_p50_s`/`gap_p90_s`/`recommendation`/`lever` row (chat-only turn gaps rarely clear the 1h TTL break-even); `scorecard.dimensions` (no `cache_efficiency`/`context_hygiene`/`agent_efficiency` evidence to justify a higher tier). |
| `discovery-scrape` | data-exploration, web-research, database-exploration | `single-model` | `effortLevel=low`, `subagentPromptCacheTtl=5m`, `autoCompactWindow=100000`; `agents.Explore.effort=low`, `agents.Explore."experimental.cacheTtl"=5m` | `classify.classify_purpose`'s `local-llm-pipeline` signature and `phases.phases_summary`'s "discovery" row `cost_share_pct`; `recache.recache_huge_context.share_pct` / `scorecard.dimensions`' context-hygiene p90 proxy for the tightened `autoCompactWindow`. |
| `planning-requirements` | planning, requirements, architecture | `single-model` | `effortLevel=high`, `promptCacheTtl=1h` | `agents.topology_effort_tokens`'s `thinking_share` column (the deliberate opposite case to the effort-mismatch rule: a planning session's high thinking-token share is doing real work); `sessions.sessions_by_purpose`'s "planning" row for the longer per-turn gap justifying 1h. |
| `implementation-heavy` | implementation, refactor, test-triage, review | `plan-high-implement-low` | `effortLevel=medium`, `subagentPromptCacheTtl=5m`; `agents.claude-implementer.model=sonnet`, `.effort=medium`, `.maxTurns=60`, `.omitClaudeMd=false`, `."experimental.cacheTtl"=5m` | `agents.topology_spawn_write`'s `mean_write` column (recommend.py's spawn-cost rule threshold — `omitClaudeMd` is left `false` deliberately, since the rule only recommends flipping it once a specific corpus clears the threshold); `ttl.ttl_by_agent_type`'s per-agent-type lever text. |
| `overseer-fanout` | fanout, multi-agent-coordination | `overseer-fanout` | `effortLevel=high`, `subagentPromptCacheTtl=5m`; `agents.claude-implementer.effort=medium`, `.maxTurns=60` | `agents.topology_report_proxy`'s `mean_proxy` column (agent-report-size rule); `agents.topology_spawn_write`'s `mean_write` column (spawn-cost rule) for the top/implementer effort split. |
| `overnight-batch` | overnight-run, unattended-batch | `overseer-fanout` | `subagentPromptCacheTtl=1h`, `autoCompactWindow=300000`, `cleanupPeriodDays=30`; `agents.verification-runner."experimental.cacheTtl"=1h` | `classify.classify_mode`'s "overnight" mode (span > 4h, max human gap > 60min); `ttl.ttl_by_agent_type`'s `gaps_over_1h`/`gap_p90_s` columns; `compactions.compactions_summary`'s "Compactions per session (mean)" / dropped-token-share rows for the raised `autoCompactWindow`. |
| `workflow-ultracode` | workflow-run, scripted-multi-phase | `workflow-heavy` | `subagentPromptCacheTtl=5m`; `agents.claude-implementer.maxTurns=40`, `."experimental.cacheTtl"=5m` | `workflows.workflows_summary`'s "Total workflow runs" row and `workflows.workflows_detail`'s per-run `agent_count`/`phases` columns; `ttl.ttl_by_agent_type`'s per-agent-type lever (short-gap scripted phases). |

`list_profiles() -> list[Profile]` returns all seven, in the order
above; `get(profile_id) -> Profile | None` returns one by id or `None`
for an unrecognised id.

## `suggest()`: archetype/purpose → catalogue id

`suggest(archetype, purposes) -> str` is the deterministic mapping a
future `init`/`baseline` (out of this work package's scope) calls once
it has detected a corpus's archetype and dominant purposes
(`classify.classify_purpose`'s values, most-dominant first). A purpose
is checked first, in the caller's own list order, since it is a more
specific signal than the bare archetype; the archetype is only a
fallback.

**Purpose overrides (checked first, in list order):**

| Purpose | Catalogue id |
|---|---|
| `local-llm-pipeline` | `discovery-scrape` |
| `workflow-run` | `workflow-ultracode` |
| `agent-fanout` | `overseer-fanout` |
| `refactor` | `implementation-heavy` |
| `test-triage` | `implementation-heavy` |
| `review` | `implementation-heavy` |
| `planning` | `planning-requirements` |
| `docs-or-light-edit` | `interactive-chat` |
| `general-dev` | `planning-requirements` |

**Archetype fallback (used when no purpose above matched):**

| Archetype | Catalogue id |
|---|---|
| `chat-only` | `interactive-chat` |
| `single-model` | `planning-requirements` |
| `effort-varied` | `planning-requirements` |
| `plan-high-implement-low` | `implementation-heavy` |
| `overseer-fanout` | `overseer-fanout` |
| `workflow-heavy` | `workflow-ultracode` |
| `mixed` | `interactive-chat` |

An unrecognised or absent archetype with no matching purpose falls
back to `interactive-chat`.

**Known limitation, disclosed rather than papered over:** `suggest()`
can never return `overnight-batch` (`catalogue.UNREACHABLE_BY_SUGGEST`).
That profile is justified entirely by session *mode* evidence
(`classify.classify_mode`'s overnight rule: span > 4 hours and maximum
human gap > 60 minutes), which `suggest`'s plan-specified signature
(archetype and purpose only, no mode) has no way to receive — an
overnight session can be any archetype. `catalogue.get("overnight-batch")`
still returns it directly; reaching it automatically would require a
future, explicitly reviewed `suggest` signature change that also takes
the corpus's mode mix, not a guess baked into this mapping.

## Diffing a profile against a project's effective config

`diff.py` is a pure, filesystem-free module: it never reads a project,
a settings file, or `~/.claude` itself — every input is a value the
caller (a later `apply`/API route) already computed via
`snapshots.py`'s schema-2 `effective`/`effective_provenance`/
`effective_agents`.

### `diff_against_effective`

```python
diff_against_effective(
    profile: Profile,
    effective: dict,             # snapshots.py's flat effective settings dict
    effective_agents: dict,      # {agent_name: {source, experimental_cache_ttl, model, effort, max_turns}}
    provenance: dict,            # snapshots.py's effective_provenance: {key: layer_name}
    managed_keys: set[str],      # keys the managed-settings layer currently governs
) -> ProfileDiff
```

Returns one `DiffRow` per settings key, per agent key, and per env name
the profile sets — never fewer, even when the current value already
matches (a diff renderer decides what to display; the full row set is
always available). Each `DiffRow` carries `key` (a namespaced string
such as `settings.effortLevel`, `agents.claude-implementer.effort`, or
`env.MAX_THINKING_TOKENS`), `current_value`/`current_provenance` (from
`effective`/`provenance`, or the matching field of
`effective_agents[name]` for the four traceable agent keys — `model`,
`effort`, `maxTurns` → `max_turns`, `"experimental.cacheTtl"` →
`experimental_cache_ttl` — `None` for the other five agent keys, which
have no "current value" a snapshot can trace), `proposed_value` (the
profile's own value), `target_file` (where the value currently lives,
derived from `provenance`/`effective_agents[name].source` — see the
design note below), and `managed` (whether `managed_keys` currently
governs this key).

### `render_unified_diff`

```python
render_unified_diff(profile_diff: ProfileDiff, *, scope: "user" | "project-local" | "repo") -> str
```

Renders a unified-diff-style block per settings-file target (grouping
settings rows together, then each agent's frontmatter rows, then env
rows), skipping any row whose current and proposed values already
match. `scope` selects *where the settings-kind changes would be
written* if applied — it only affects the settings group's
`---`/`+++` header, never the agent-frontmatter group (a `.claude/
agents/<name>.md` file's path does not depend on scope) or the env
group:

| `scope` | Settings target file |
|---|---|
| `"user"` | `user settings` |
| `"project-local"` | `.claude/settings.local.json` |
| `"repo"` | `.claude/settings.json` |

Env rows render as a single `+NAME=value` addition line with no
removal line (a profile only ever proposes setting an env var; it
cannot know or print whatever value is currently exported in the
user's shell). Raises `ValueError` for any `scope` other than the
three above. Output is byte-stable: the same `ProfileDiff` always
renders to the same text, with settings keys in `SETTINGS_ALLOWLIST`
order, agents in sorted-name order, and env names in `ENV_ALLOWLIST`
order.

**Managed-key exclusion.** A row with `managed=True` is never rendered
inside a `---`/`+++` diff hunk — it is instead collected and rendered
as a trailing note, `<key>: managed by policy, raise with your
administrator`, matching `model.py`'s `Recommendation.scope="managed"`
convention and `docs/api.md`'s "Managed-settings routes" contract
(`POST /api/profiles` never writes a managed key regardless of what a
profile requests). A profile whose only changed rows are all managed
therefore renders no diff hunk at all, only the note block.

**Design note — `target_file` vs `scope`.** These are deliberately two
different things: `target_file` on a `DiffRow` answers "where does
this value currently live" (derived from the caller-supplied
`provenance`, so it can be `managed-settings.json`, `.claude/
settings.local.json`, `.claude/settings.json`, or `user settings`, or
an agent's `.claude/agents/<name>.md`, or the env-var note), while
`scope` on `render_unified_diff` answers "where does the caller want
to *write* the proposed change" — a project's current provenance for
a key and the scope the caller is applying it at are independent
(a value currently set in user settings can still be targeted at
`project-local` by an `apply --project`). Neither the plan text nor
the brief fully specified this split; it is recorded here rather than
left implicit, per this project's "report deviations" convention.

### `apply_command`

```python
apply_command(profile_id: str, scope: "user" | "project-local" | "repo", project_path: str | None = None) -> str
```

Two lines: the exact `claude-token-lens apply <id> [--project <path>]
[--allow-tracked]` invocation (`--project` only appears when
`project_path` is given; `--allow-tracked` is only added for
`scope="repo"`, matching that scope writing a version-controlled
`.claude/settings.json`), and the `--launch` one-session-overlay
alternative, `claude --settings <config-dir>/profiles/<id>.settings.json`.
Raises `ValueError` for an unrecognised `scope`.

**Privacy:** when `project_path` is omitted, nothing in the returned
text is an absolute path — a `project-local`/`repo` scope with no
`project_path` simply omits `--project` (matching `apply`'s own
documented default of the current working directory). When
`project_path` is given, it is printed exactly as given, and nowhere
else in the output. `<config-dir>` in the `--launch` line is always a
literal placeholder, never a real path.

## What a profile cannot do

A profile is a bundle of **settings, agent-frontmatter, and
environment-variable-name levers only** — the exact set `settings.json`,
a `.claude/agents/<name>.md` frontmatter block, and Claude Code's own
environment-variable surface expose. It is not, and cannot become:

- **A behavioural or workflow instruction.** Advice like "keep spawned
  agent reports short" or "coordinate through a scripted workflow"
  (`recommend.py`'s own category split between a settings-level lever
  and workflow advice) is not representable — a profile can only ever
  nudge a *setting*, never a prompting style or a run's shape.
- **An environment variable *value*.** `env` in a profile is a
  *name allowlist* — the schema never stores or transmits an actual
  secret or value; the user supplies that at apply time, in their own
  shell.
- **A path to a real file.** No function in this package reads or
  writes a project's filesystem — `diff.py` is pure, and `schema.py`'s
  `load_profile`/`dump_profile` only ever touch the one profile file
  they are explicitly given.
- **An override of a managed-settings key.** `diff_against_effective`/
  `render_unified_diff` know about `managed_keys` precisely so a
  profile's proposed change to a managed key is surfaced as "managed
  by policy" rather than silently presented as applicable — the actual
  refusal to *write* a managed key is enforced by `apply`/the
  `POST /api/profiles` route (both out of this work package), not by
  anything here.
- **A key this project has no way to honour.** The allowlist is the
  single source of truth; `validate()` rejects any key not in it, so a
  profile can never promise an effect the harness cannot deliver
  (`docs/api.md`'s own `POST /api/profiles` note, quoted from the
  plan).

## The one `recommend.py` lever not representable under its own name

`recommend.py`'s `_rule_baseline_bloat` rule emits the literal
`lever="mcpServers"`. There is no settings key spelled `mcpServers` —
the allowlist's two MCP-server keys are `enabledMcpjsonServers` and
`disabledMcpjsonServers` (the `~/.claude.json` project-level
enable/disable lists — `docs/config-layers.md`'s `claude_json`
section). `schema.RECOMMEND_LEVER_MAP` resolves the bare `"mcpServers"`
lever to `("settings", "disabledMcpjsonServers")`: the rule's own
action text is "review which MCP servers … disabling unused ones
shrinks every session's first-turn cache write", so the concrete,
representable action is populating `disabledMcpjsonServers` — a
profile has no way to discover *which* servers to newly enable, so
`enabledMcpjsonServers` is never the target of this mapping. Every
other `lever` literal `recommend.py`/`ttl.py` can emit
(`"promptCacheTtl"`, `"autoCompactWindow"`, `"effortLevel"`,
`"omitClaudeMd"`, and the per-agent-type `"experimental.cacheTtl in
<agent>.md"` sentence form) resolves directly to an allowlisted key —
see `tests/test_profiles_schema.py`'s `recommend.py` lever-coverage
tests for the regression check that keeps this true as `recommend.py`
evolves.

## Applying a profile

`profiles/apply.py` is the one module in this package that actually
writes to a project's or a user's real files — `plan_apply` resolves
every write without touching disk, `execute` performs it, and `revert`
undoes it. `cli.py`'s `apply` subcommand is the only caller; the
functions themselves take no CLI dependency (a future `POST
/api/profiles` route can call them the same way).

```
claude-token-lens apply <profile> [--scope user|project-local|repo]
                                   [--project-dir PATH]
                                   [--dry-run] [--launch]
                                   [--allow-tracked] [--force]
                                   [--revert TS] [--list-backups]
```

`<profile>` is a catalogue id or a path to a profile TOML file.

### Scopes: what gets touched

| `--scope` | Settings file written | Agent files written |
|---|---|---|
| `user` (default with no `--project-dir`) | `~/.claude/settings.json` | `~/.claude/agents/<name>.md` |
| `project-local` (default once `--project-dir` is given) | `<project>/.claude/settings.local.json` | `<project>/.claude/agents/<name>.md` |
| `repo` | `<project>/.claude/settings.json` | `<project>/.claude/agents/<name>.md` |

An agent's frontmatter file is not itself scope-specific — the same
`.claude/agents/<name>.md` is patched regardless of which settings
scope is chosen (this mirrors `diff.py`'s own `target_file` note above:
a per-agent row always renders against that one path). Existing keys
and surrounding text (comments, unrelated keys, formatting) in an agent
file are preserved exactly — only the allowlisted keys a profile sets
are patched in place (`frontmatter.patch_frontmatter`).

### The `--project-dir` flag, not `--project`

Every subcommand already has a `--project` flag (repeatable, filters a
report by project slug). `apply` needs an unrelated "which directory is
this project" argument, so — matching the identical collision already
resolved for `snapshot-config`/`probe-config` — it is spelled
`--project-dir` instead. `--scope` defaults to `user` when
`--project-dir` is omitted, and to `project-local` when it is given.

### `--dry-run`: the diff, never a write

`--dry-run` prints exactly the text `diff.render_unified_diff` would
render for this profile/scope/effective-config combination (see
"Diffing a profile against a project's effective config" above —
`plan_apply` calls the same `diff_against_effective`/
`render_unified_diff` functions to build this text, so the preview and
the real write plan are provably one computation, not two that could
drift apart), followed by any managed-key notes, the env-var export
lines, and the exact command to run for real. Nothing is written to
disk.

### Backups and `--revert`

A real apply first backs up every file it is about to overwrite, byte
for byte, under `<config-dir>/backups/<ts>/` (a file that didn't exist
yet backs up as "absent" rather than empty), writes a `manifest.json`
recording which backup corresponds to which target, then writes the
new content atomically (temp file + rename, so a crash mid-apply never
leaves a half-written target). It also writes a small stamp snapshot to
`<config-dir>/snapshots/<ts>.json` and updates
`<config-dir>/active-profile` to the applied profile's id (the same
file `hooks/snapshot-config.py`'s `_read_active_profile` already reads
on its next run).

`claude-token-lens apply --revert <ts>` restores every file from that
apply's manifest to its exact pre-apply state — byte for byte, deleting
a file the apply had created rather than emptying it. `--list-backups`
prints every previous apply's timestamp, profile id, scope, and file
count, oldest first.

Two applies landing within the same wall-clock second (both `execute()`
would otherwise stamp with an identical timestamp) get distinct backup
directories — a `-2`, `-3`, ... suffix disambiguates rather than
letting the second apply silently overwrite the first's backup.

### `--launch`: a one-session overlay, not a persisted apply

`--launch` writes only `<config-dir>/profiles/<id>.settings.json` — a
plain `settings.json`-shaped JSON object holding the profile's
non-managed settings keys — and prints the matching `claude --settings
<path>` command. No backup, no manifest, no `active-profile` update, no
existing file read or merged: this is a one-off overlay for a single
session, not a change to any of the layered settings files.

### Environment variables: printed, never written

A profile's `env` names are printed as `export NAME=value` lines (both
in `--dry-run` and after a real apply) — never written to any file.
This matches `env` being a name allowlist in the first place (see "What
a profile cannot do" above): the value the profile carries is applied
by the user exporting it in their own shell.

### Managed keys

Any settings/agent/env key the caller's snapshot reports as governed by
a managed-settings layer is dropped from the write plan entirely (never
attempted, never blocked-and-retryable) and named instead in a "managed
by policy, raise with your administrator" note — the same exclusion
`diff.py`'s own unified-diff rendering already applies.

### Git-tracked files: refused unless `--allow-tracked`

A project-scoped write (`project-local` or `repo`) whose target file —
a settings file or an agent's frontmatter file — is already tracked by
git is refused by default (an apply changing a file colleagues share
through version control should be a deliberate, reviewed choice, not a
side effect of running a profile). `--allow-tracked` opts in. This
check never applies to `user` scope or to the config directory's own
files (backups, the active-profile marker, snapshot stamps), none of
which are ever expected to live in a project's repository.

### Missing agent files: refused unless `--force`

Applying an agent-frontmatter change to an agent that has no
`<name>.md` file yet at the resolved scope is refused by default —
there is nothing to patch, and creating one from a profile's partial
key set would be a guess about the rest of that agent's configuration.
`--force` is the explicit escape hatch: it creates a new frontmatter
file holding exactly the keys the profile sets. `--force` controls only
this behaviour — it has no effect on the git-tracked-file refusal
above, which is `--allow-tracked`'s job specifically.
