# v0.3 profiles

`claude_token_lens.profiles` (plan Milestone v0.3: "baseline capture,
archetype, profiles, apply") is the schema, shipped catalogue, diff
renderer and apply/revert code for a **profile**: a small, allowlisted
bundle of Claude Code settings/agent-frontmatter/environment-variable
levers that a user can apply to a project. This document is the
contract for the package. `cli.py`'s `apply`/`init`/`baseline`
subcommands and the `/api/profiles*`, `/api/profile-schema` and
`/api/profile-goals` routes (`docs/api.md`) are built on it. Only
`profiles/apply.py` writes Claude Code files, and only when you run
`apply` yourself.

## On the dashboard

**Create a profile** starts from a goal instead of a blank form:

1. Pick a goal: start from my recommendations, spend less on subagents,
   cheaper models where it's safe, cheaper cache, shorter conversations,
   less thinking where it isn't needed, or start from my current
   settings (`profiles/goals.py`).
2. Tick the changes you want. Each row shows the setting, its value now
   and after, the estimated effect over the window, the evidence and the
   trade-off. A change is ticked for you only when your own sessions
   support it. A model, cache or summary-point change that would save
   less than 5% of what it touches is not offered at all; an agent's
   model change is ticked from a 20% saving. The main session's model
   is never ticked for you. A lower effort or skipping CLAUDE.md found
   from your tables is offered unticked, for you to decide; the same
   change from a recommendation is ticked. The total at the top is a what-if estimate (`whatif.py`),
   updated as you tick.
3. Name it and save it. It then works like any other profile below.

The estimate reads the report's own tables and runs no new simulation:

| Change | Read from | How it is worked out |
|---|---|---|
| `model` (main session or an agent) | model-swap table | Simulated: the same tokens repriced |
| `autoCompactWindow` | summary-point sweep | Simulated: your sessions replayed |
| `promptCacheTtl`, `subagentPromptCacheTtl`, an agent's `experimental.cacheTtl` | cache-lifetime simulation | Simulated: every cache write replayed at 5 minutes or 1 hour |
| an agent's `omitClaudeMd = true` | CLAUDE.md tokens per spawn | Measured per spawn, times the spawns in the window |
| `skillOverrides`, `enabledPlugins` (turning one off) | each skill's listing cost (Context files) | Estimated from what stops being sent |
| `effortLevel`, an agent's `effort` | thinking share of output | Not estimated: shows the thinking share only |

Any other key says "not estimated" rather than guessing. Changes
overlap, so a total of several rows is rough. Each profile's detail
shows the same estimate.

**Your changes and what they did** lists every `apply`, its undo, and
any settings change the snapshot hook saw between one session start
and the next (`change_points.py`). A snapshot change that spans an
apply or undo is that same change, not a second one. For each, newest
first (at most 10), it compares the sessions started before it with
those started after it (`impact.py`):

- **Before** is the 14 days before the change, cut short by an earlier
  change. **After** runs from the change to the next one, or now.
- Changes within 10 minutes of each other (one apply writing several
  files, say) share their before and after.
- It needs at least 3 sessions on each side. With fewer, it says how
  many it has and gives no verdict.
- The measures follow the keys that changed: cost per reply for a
  model or effort change, summaries per session and largest context
  for `autoCompactWindow`, the share of cache writes that rebuilt
  expired context for a cache lifetime, context at session start for
  skills, plugins and MCP servers, and cost and start-up context per
  spawn for a change to one agent. Cost per session always comes last.
  A change under 5% reads as "about the same".
- An apply names its keys from its backup manifest. An apply made
  before manifests recorded keys names them from the difference
  between the backup and the file as it is now. Later edits to the
  same file show up too, so it names keys but not values.

Each change also gets a quality check: the runs of each agent it
changed (or the main session's, for a setting that isn't per agent)
before against after, on the [quality signals](concepts.md#7-quality-signals)
such as failed tool calls and runs that didn't finish. A cheaper model
that cost less but failed more shows up here. It needs 5 runs on each
side.

Sessions differ in size and kind of work, so a difference is a signal,
not proof.

The Profiles tab also shows one card per profile with the settings it
changes by their plain labels. Opening one shows a table of Setting /
Now / After / Set in for the scope you pick, then three ways to use it:
a prompt that asks Claude to make the changes and show you the diff
first (`fixes.profile_prompt`), the `claude-token-lens apply <id>
--dry-run` command, and a one-session `--launch` trial that changes no
Claude Code settings file (it writes only its own overlay under
`<config-dir>/profiles/`). "Save my current settings as a profile"
saves your latest
config snapshot's allowlisted, non-managed values as a user profile
(`POST /api/profiles/from-current`), and "Edit settings directly" is a
form built from `GET /api/profile-schema`. None of these change your
Claude Code config; only running the command, or Claude acting on the
prompt with your permission, does.

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
`schema.py` are the single source of truth. `validate()`'s rejection
messages are built from these three dicts; the tables below are copied
from them by hand, so update both together. A profile naming any
other key is rejected outright.

### `settings` (top-level `settings.json` overlay keys)

`enabledPlugins` and `skillOverrides` are objects keyed by name. `apply`
merges them into the object already there, so a profile that turns one
plugin off, or hides one skill, leaves every other entry alone.
`skillOverrides` sets how Claude sees a skill: `on` (listed with its
description), `name-only` (listed by name, which costs fewer tokens),
`user-invocable-only` (only you can start it, with `/name`) or `off`.

| Key | Type | Allowed values | Doc reference |
|---|---|---|---|
| `model` | string | any | `docs/config-layers.md#the-layer-model` |
| `effortLevel` | enum | `low`, `medium`, `high`, `max` | `docs/config-layers.md#what-each-layer-records-settings_layerslayer` |
| `autoCompactWindow` | int | 0–1,000,000 | `docs/config-layers.md#what-each-layer-records-settings_layerslayer` |
| `outputStyle` | string | any | `docs/config-layers.md#what-each-layer-records-settings_layerslayer` |
| `promptCacheTtl` | enum | `5m`, `1h` | `docs/api.md#get-apittl` |
| `subagentPromptCacheTtl` | enum | `5m`, `1h` | `docs/api.md#get-apittl` |
| `enabledPlugins` | map of plugin name to on/off | `true`, `false` | `docs/config-layers.md#content_layers` |
| `skillOverrides` | map of skill name to visibility | `on`, `name-only`, `user-invocable-only`, `off` | `docs/profiles.md#settings-top-level-settingsjson-overlay-keys` |
| `disabledMcpjsonServers` | list of strings | any server names | `docs/config-layers.md#claude_json--the-claudejson-cross-check` |
| `enabledMcpjsonServers` | list of strings | any server names | `docs/config-layers.md#claude_json--the-claudejson-cross-check` |
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
| `mcpServers` | list of strings | any | `docs/config-layers.md#content_layers` |
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

`suggest(archetype, purposes) -> str` is the deterministic mapping
`baseline.py` (behind `init` and `baseline`) calls once it has detected
a corpus's archetype and dominant purposes
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
still returns it directly, and `baseline._suggested_profile` suggests
it before calling `suggest()` when at least half the window's sessions
are overnight (`sessions_by_mode`).

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
`project-local` by an `apply --project-dir`). Neither the plan text nor
the brief fully specified this split; it is recorded here rather than
left implicit, per this project's "report deviations" convention.

### `apply_command`

```python
apply_command(profile_id: str, scope: "user" | "project-local" | "repo", project_path: str | None = None) -> str
```

Two lines: the exact `claude-token-lens apply <id> [--scope <scope>]
[--project-dir <path>] [--allow-tracked]` invocation (`--scope` appears
for every scope but `user`, since `apply` otherwise falls back to user
scope; `--project-dir` only appears when
`project_path` is given; `--allow-tracked` is only added for
`scope="repo"`, matching that scope writing a version-controlled
`.claude/settings.json`), and the `--launch` one-session-overlay
alternative, `claude --settings <config-dir>/profiles/<id>.settings.json`.
Raises `ValueError` for an unrecognised `scope`.

The flag is `--project-dir`, not `--project`: every subcommand already
has its own `--project` flag (repeatable, filters a report by project
slug), so `apply`'s project-*directory* argument is deliberately named
`--project-dir` instead (`cli.py`'s `_add_apply_args` docstring) — a
v0.3 fix; an earlier version of both this function and this document
printed the wrong flag.

**Privacy:** when `project_path` is omitted, nothing in the returned
text is an absolute path — a `project-local`/`repo` scope with no
`project_path` simply omits `--project-dir` (matching `apply`'s own
documented default of the current working directory). When
`project_path` is given, it is printed exactly as given, and nowhere
else in the output. `<config-dir>` in the `--launch` line is always a
literal placeholder, never a real path. `service/api.py`'s
`GET /api/profiles/<id>/diff` route never passes a `project_path` at
all — see `docs/api.md`'s note on that route for why.

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
- **A path to a real file.** A profile names keys, never files. Where
  a change lands is decided by `apply`'s `--scope` and `--project-dir`
  (see "Applying a profile"). `diff.py` is pure, and `schema.py`'s
  `load_profile`/`dump_profile` only ever touch the one profile file
  they are explicitly given; `apply.py` is the only module here that
  writes Claude Code files.
- **An override of a managed-settings key.** `diff_against_effective`/
  `render_unified_diff` know about `managed_keys` precisely so a
  profile's proposed change to a managed key is surfaced as "managed
  by policy" rather than silently presented as applicable. `apply`
  drops a managed key from its write plan (see "Managed keys" below).
  `POST /api/profiles` only saves a profile file and never writes
  Claude Code settings at all.
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
undoes it. `cli.py`'s `apply` and `uninstall --revert-changes` are the
only callers that write; the dashboard only reads the backups (to list
your changes) and never calls `execute` or `revert`.

```
claude-token-lens apply <profile> [--scope user|project-local|repo]
claude-token-lens apply --set KEY=VALUE [--set ...] [--agent NAME]
                                   [--scope user|project-local|repo]
                                   [--project-dir PATH]
                                   [--dry-run] [--launch]
                                   [--allow-tracked] [--force]
                                   [--revert TS [--ignore-changes]] [--list-backups]
```

`<profile>` is a catalogue id or a path to a profile TOML file.
`--set` instead builds a one-off profile (id `one-off`) from the given
keys (under `agents.NAME` with `--agent`) and validates it through
`schema.load_dict`, so the allowlist and ranges are the same. It calls
`plan_apply(..., mark_active=False)`: the active-profile marker is left
alone.

### Scopes: what gets touched

| `--scope` | Settings file written | Agent files written |
|---|---|---|
| `user` (default with no `--project-dir`) | `<claude-root>/settings.json` | `<claude-root>/agents/<name>.md` |
| `project-local` (default once `--project-dir` is given) | `<project>/.claude/settings.local.json` | `<project>/.claude/agents/<name>.md` |
| `repo` | `<project>/.claude/settings.json` | `<project>/.claude/agents/<name>.md` |

### `<claude-root>`: the real Claude Code directory, resolved independently of `--config-dir`

`user` scope targets `<claude-root>`, resolved by `cli._resolve_claude_root`
in this order: the explicit `--claude-root PATH` flag, else
`$CLAUDE_CONFIG_DIR`, else `~/.claude`. This is *not* derived from
`--config-dir`/`config_dir` (this tool's own `token-lens` subdirectory,
which may be pointed anywhere) — the two started out coincidentally
related (`config_dir` used to default to `<claude-root>/token-lens`,
so `config_dir.parent` happened to equal `<claude-root>`), but deriving
one from the other broke the moment `--config-dir` pointed somewhere
else, silently targeting `<config-dir-parent>/.claude/settings.json`
instead of the real Claude Code config and leaving user-scope agent
patches unable to find any agent file at all. `plan_apply`'s
`claude_root` parameter and `--claude-root` exist precisely so the two
directories are never conflated again.

`project-local` and `repo` patch the same
`<project>/.claude/agents/<name>.md` — an agent file has no local
variant, so only the settings file differs between those two scopes.
`user` scope patches `<claude-root>/agents/<name>.md` instead. Existing keys
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

`--dry-run` prints a real unified diff of every file `plan_apply`
would write, followed by any managed-key notes, the env-var export
lines, and the exact command to run for real. When `plan.blocked` is
non-empty (see "Git-tracked files" and "Missing agent files" below),
`--dry-run` still prints the diff, but then prints each blocked reason
to stderr instead of the command, and exits `2` — the same refusal the
real apply would hit (which exits `1`), surfaced before you run it for
real rather than a clean-looking `0` exit for a preview whose apply
would refuse. Nothing is written to disk either way.

This diff text (`apply.render_plan_diff`) is built from the exact same
`actions` list `execute()` writes from — each action already carries
the target file's *real current bytes* (`old_bytes`, read from disk
when `plan_apply` was called) and the *real merged bytes* it would
write (`new_bytes`) — so the preview is provably a diff of the real
before/after files, not a second, independent computation that could
drift from what a real apply does. An earlier version instead built
the dry-run text from `diff.diff_against_effective`/
`render_unified_diff` against the caller's *snapshot* of the effective
config — a value that can already be stale by the time `apply` runs
(an agent file edited by hand since the snapshot was taken, for
example), so the preview could show a change against a value the file
no longer has, or hide a change the file already needs. `diff.py`'s
functions are unaffected by this fix and remain in use elsewhere (the
service's `GET /api/profiles/<id>/diff` route, which has no local
files to read from and genuinely needs the snapshot-based diff).

### Settings file formatting: indentation and line endings preserved

Rewriting a settings JSON file detects the existing file's indent
width (2 vs 4 spaces, sniffed from its first indented line) and line
ending (`\r\n` vs `\n`, sniffed from whether `\r\n` appears at all) and
reproduces both in the merged output, rather than always emitting
`json.dumps`'s own 2-space/`\n` default. A file that doesn't exist yet
falls back to 2 spaces and `\n`. Agent frontmatter files always
preserved their own indentation/line endings already
(`frontmatter.patch_frontmatter` only ever rewrites the specific lines
it patches); this fix brings the settings-file writer in line with
that same "never reformat what you didn't touch" convention.

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
a file the apply had created rather than emptying it. The manifest
also records, per file, the keys changed (old and new values) and the
SHA-256 of the content written. If a file no longer matches that hash
(you or Claude Code edited it since), `--revert` refuses and restores
nothing, because restoring would discard those edits; `--ignore-changes`
restores the backup anyway. Manifests from before hashes were recorded
revert without the check. A revert keeps the backup and the snapshot
stamp, and writes a `reverted.json` marker beside the manifest, so the
dashboard and `uninstall` stop listing that apply as still in place.
`--revert` exits `2` when it refuses or cannot find the backup.

Both `--dry-run` and a real apply first print each change in words:
what the setting controls, its value now and after, which file and who
it affects, the trade-off, and how to undo it. `--list-backups`
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

A write whose target file — a settings file or an agent's frontmatter
file — is already tracked by git is refused by default (an apply
changing a file colleagues share through version control, or that the
user themselves keeps under version control, should be a deliberate,
reviewed choice, not a side effect of running a profile). `--allow-tracked`
opts in. This check applies at every scope, including `user`: a
`~/.claude` kept in a personal dotfiles repository is just as much a
tracked target as a project's `.claude/settings.json`, and is refused
the same way — an earlier version only ever consulted git status for
project scope, silently overwriting a tracked user-scope file. The
check never applies to the config directory's own files (backups, the
active-profile marker, snapshot stamps), none of which are ever
expected to live in a project's or the user's repository.

### Missing agent files: refused unless `--force`

Applying an agent-frontmatter change to an agent that has no
`<name>.md` file yet at the resolved scope is refused by default —
there is nothing to patch, and creating one from a profile's partial
key set would be a guess about the rest of that agent's configuration.
`--force` is the explicit escape hatch: it creates a new frontmatter
file holding exactly the keys the profile sets. `--force` controls only
this behaviour — it has no effect on the git-tracked-file refusal
above, which is `--allow-tracked`'s job specifically.
