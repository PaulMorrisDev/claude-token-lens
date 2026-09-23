# v0.3 onboarding: `init` and `baseline`

Never run `init` on this machine before? [`docs/first-run.md`](first-run.md)
is the short walkthrough, including what each question below actually
means in one line. This document is the full reference.

`claude-token-lens init` (`src/claude_token_lens/onboarding.py`) and
`claude-token-lens baseline` (`src/claude_token_lens/baseline.py`) are
the plan's "Milestone v0.3" onboarding pair: `init` asks (or derives) a
handful of questions this codebase genuinely cannot infer on its own,
writes `config.toml`, and starts an "onboarding capture window";
`baseline` turns whatever corpus has accumulated since then into a
JSON record plus a four-section Markdown report, citing only numbers
the report itself already computed.

Neither `onboarding.py` nor `baseline.py` touches `profiles/apply.py`,
the `service/` package, or SQLite — a baseline is one JSON file under
`<config_dir>/baselines/<id>.json` (plus a sibling `<id>.md`), the same
"plain files under the config dir" posture `config.py` already uses for
`config.toml`/`sessions.toml`. `onboarding.run_init` never writes
`settings.json` except to repair a broken hook command you agreed to
fix (step 2). The two steps that change things outside `<config_dir>`
— connecting to Claude Code (step 7) and the logon service (step 8) —
live in `cli.py` (`_cmd_init_connect_step`, `_cmd_init_service_step`),
so `onboarding.py` stays free of installer side effects.

## `init`

```
claude-token-lens init [--answers FILE] [--non-interactive] [--no-install]
                        [--repair-hook] [--connect]
                        [--install-service | --no-service] [--dry-run]
```

1. **Detect** what's already on the machine (`onboarding.detect` ->
   `Detection`): whether `<config_dir>` exists yet, how many config
   snapshots and whether a usage-log CSV are already on file, the
   current project's (redacted) slug, how many projects are
   discoverable at all under the projects root, and whether the
   SessionStart hook is set up (`hook_health.check`). Printed so the
   next step's questions have visible context. Only a redacted slug
   (`discovery.redact_slug`) and counts are printed, never session
   content.
2. **Offer a hook repair**, only when `settings.json` has a SessionStart
   hook command for `snapshot-config.py` that can't run (a path broken
   by JSON escaping, a missing interpreter, or a `%VARIABLE%` that Git
   Bash won't expand) and its script exists. `init` prints the current
   and fixed commands. `--repair-hook` makes the fix without asking;
   otherwise it asks (`[n]`), and under `--non-interactive` it only
   prints the command to run. The fix keeps your interpreter when it's
   found and writes each `%VARIABLE%` out in full; otherwise it names
   the base Python install by full path. Only that one command string
   changes, after `settings.json` is copied to
   `settings.json.bak-<UTC timestamp>`.
3. **Ask** (`onboarding.gather_answers` -> `Answers`) the "Asked, not
   guessed" question set below. Resolution order per question:
   - a value named in `--answers FILE` (a flat JSON object) wins;
   - otherwise, interactive stdin prompting (default shown in
     brackets, blank input accepts it);
   - otherwise, under `--non-interactive`, a derived default is used
     and the derivation is printed as `(derived) <key>: ...` — nothing
     is guessed silently.
4. **Write** `config.toml` (`config.write_config_values` — merges into
   an existing file key-by-key, `.toml.new` fallback if the merged
   shape can't be round-tripped) and this project's own
   `<config_dir>/projects/<slug>.toml` (`config.save_project_config`).
5. **Print the install fragments**, only under `--non-interactive`
   without `--connect`: the SessionStart hook fragment
   (`hooks/snapshot-config.py`'s own `hook_fragment_text()`) and the
   statusLine fragment (`statusline.print_install_fragment()`), for you
   to merge in yourself. `--no-install` skips this and step 7.
6. **Run an initial baseline** for the current project (unless no
   project directory has ever been recorded for it yet) and print the
   capture-window status (`baseline.format_capture_status`).
7. **Connect to Claude Code** (`cli.py`'s `_cmd_init_connect_step`),
   when asked interactively or with `--connect`, and not with
   `--no-install`. It copies the hook script to
   `<config_dir>/hooks/snapshot-config.py`, then shows the exact change
   to Claude Code's `settings.json` (`--claude-root`, else
   `$CLAUDE_CONFIG_DIR`, else `~/.claude` -- never the folder above
   `<config_dir>`)
   (`hook_health.plan_connect`) as a diff:
   - a SessionStart hook (`"async": true`) running that script, added
     only when no SessionStart hook runs `snapshot-config.py` yet (a
     broken one is step 2's job);
   - a `statusLine`, added only when none is set, so yours is never
     replaced.

   The hook command names the base Python install (not a virtual
   environment's, since the script is stdlib-only) and the script by
   full path. The statusline command names the running Python with
   `-m claude_token_lens.statusline`, or the `.pyz` by full path.
   When `<config_dir>` is not the default `<Claude folder>/token-lens`,
   both commands end with `--config-dir "<config_dir>"`, so the hook
   and the statusline write where the CLI and dashboard read.
   It writes only after a `y` (default `n`), or at once with
   `--connect`, after copying `settings.json` to
   `settings.json.bak-<UTC timestamp>`. Declining prints how to do it
   later (`init --connect`).
8. **Offer to register the service at logon** (v3, `cli.py`'s
   `_cmd_init_service_step` — see [`docs/deploy.md`](deploy.md)).
   `--no-service` skips this step entirely (prints "Service-at-logon
   step skipped (--no-service)."), with no question and no install.
   Otherwise: `--install-service` installs without asking; under
   `--non-interactive` (and without `--install-service`) the derived
   default is *not* to install (`(derived) run_service: not given on
   the command line; used default False (pass --install-service to
   install non-interactively)` — installing a background service is
   never assumed on someone's behalf); otherwise it's an interactive
   prompt (`Run the service at logon? (y/n) [y]:`, blank/`y`/`yes`
   accepts). Declining prints "Service not installed. Run
   'claude-token-lens install-service' any time to add it later."
   Accepting calls `installer.plan_service_install`/`installer.install`
   exactly like the standalone `install-service` subcommand, honours
   `init`'s own `--dry-run` (which also stops step 7 at showing its
   diff; `config.toml` and the baseline are still written), and, once installed for real, probes `is_registered()`
   and one `GET http://127.0.0.1:8765/api/health` after a short delay
   to report whether the service is already up. `init` always registers
   port 8765 on `127.0.0.1`; use `install-service --port/--bind` for
   anything else. The first start reads your whole history, so the
   health check can report "not responding yet" for a minute.

### The question set

| Key | Asked as | Feeds |
|---|---|---|
| `billing` | How do you pay for Claude Code? (`subscription`, `api` or `auto`, the default when unset; `pro`, `max`, `team`, `enterprise` and `plan` mean `subscription`, and anything else is rejected — see the README's billing note) | `config.billing` |
| `exclude_projects` | Projects to always leave out (folder names under `~/.claude/projects`, comma-separated) | `config.exclude_projects` |
| `launch_overlays` | Do you start Claude Code with `--settings` or `CLAUDE_CONFIG_DIR` pointing at extra settings? | `config.launch_overlays` and this project's `projects/<slug>.toml` |
| `shared_project_config` | Is this project's `.claude` folder (agents, skills) committed to a repo colleagues use? | `config.shared_project_config` and this project's `projects/<slug>.toml` |
| `tz` | Time zone, such as Europe/London (blank for this computer's) | `config.tz` |
| `apply_scope` | Where should changes you apply go by default (`user`/`project-local`/`repo`) | `config.apply_scope` and this project's `projects/<slug>.toml` |
| `capture_window` | How many days to collect data before the first baseline | `config.capture_window` (default 7) |

`config.capture_started` is set to the current UTC timestamp by the
first `init` — it isn't a question. Running `init` again (for example
`init --connect` or `init --repair-hook`) keeps it, so the capture
window you are part-way through carries on. To start a new window on
purpose, delete the `capture_started` line from `config.toml` and run
`init` again.

**Scope note** (docs vs. code): `docs/config-layers.md`'s "What `init`
(v0.3) will ask" section previews a richer detection step (per-key
settings-layer provenance, agent-inheritance/`shadowed_by_project`
status, `.mcp.json` presence, CLAUDE.md/rules footprint,
`~/.claude.json` trust-dialog state). This work package's `Detection`
covers the config-dir/snapshot/usage-log/project-count facts above only
— the deeper per-project settings-layer scan that section previews
would need a snapshot (or a fresh `probe-config`-style scan) folded
into `detect()`, which is a reasonable follow-on but not implemented
here. Flagged rather than silently narrowed, per this project's
"report a deviation, don't paper over it" convention (see
`profiles/catalogue.py`'s own such note).

### `--answers FILE`

A flat JSON object naming any subset of the keys above (any key it
omits falls back to interactive prompting, or a derived default under
`--non-interactive`):

```json
{
  "billing": "subscription",
  "exclude_projects": ["work-thing"],
  "launch_overlays": false,
  "shared_project_config": true,
  "tz": "Europe/London",
  "apply_scope": "repo",
  "capture_window": 14
}
```

## `baseline`

```
claude-token-lens baseline [--days N] [--finalise] [--list] [--show ID]
```

With no flags: builds a report over the given window (or all time),
extracts a baseline record from it, saves `<config_dir>/baselines/
<id>.json` (+ `<id>.md`), and prints the Markdown report.
`--list`/`--show ID` read back what's already saved instead of
capturing anything new.

A baseline built before the `init`-started capture window has finished
is **provisional** (`record.provisional == true`) unless `--finalise`
is given, or the window has already elapsed on its own
(`baseline.capture_status`).

### What's in a baseline record

Every field is read straight out of the already-built `ReportModel`'s
own tables — never recomputed independently (the same "never fabricate,
only cite the report's own tables" convention `recommend.py`'s
`Recommendation.evidence` contract already enforces):

| Field | Source |
|---|---|
| `mode_mix` | `sessions` section's `sessions_by_mode` table |
| `dominant_purposes` | `sessions` section's `sessions_by_purpose` table (top 3) |
| `archetype` | `workstyle` section's `workstyle_archetypes` table (top row) |
| `scorecard_overall` / `scorecard_label` | `scorecard` section's `overall` table |
| `projected_saving_usd` | sum of the `ttl` section's `ttl_by_agent_type` table's `saving_usd` column |
| `suggested_profile` / `suggested_profile_reason` | see below |
| `billing_mismatch_warning` | see below |
| `projects` | project directory names, redacted (`discovery.redact_slug`) |
| `cost_per_session` | `overview` section's `totals` table, `total_cost_usd` ÷ `sessions_analysed` |
| `recache_share_pct` | `recache` section's `recache_summary` table (`report.recache_share_pct_metric`) |
| `compactions_per_session` | `compactions` section's `compactions_summary` table (`report.compactions_per_session_metric`) |
| `ttl_mix_top_level` / `ttl_mix_by_agent_type` | `ttl` section's `ttl_by_agent_type` table (`report.ttl_mix_by_agent_type_metric`) |
| `session_baseline_size` | `agents` section's `topology_session_baseline` table (`report.session_baseline_size_metric`) |
| `mean_spawn_write_by_agent_type` | `agents` section's `topology_spawn_write` table (`report.mean_spawn_write_by_agent_type_metric`) |
| `scorecard_dimensions` | `scorecard` section's `dimensions` table (`report.scorecard_dimensions_metric`) |
| `by_mode` | cost/re-cache/compactions per session, recomputed once per distinct mode over a session-filtered sub-corpus (see below) |

These nine fields (v0.3 Task 2) feed `report --baseline <id|latest>`'s
`## Baseline comparison` section (see the main README and
[docs/exports.md](exports.md) for the wider export surface).
`report.py` and `baseline.py` share the same extraction functions
(defined once in `report.py`, imported by `baseline.py`) rather than
duplicating them, since both sides need identical logic — one for a
saved baseline, one for the current window — and `baseline.py` already
imports one-way from `report.py`.

`by_mode` is deliberately narrower than the other eight fields: it
covers only cost per session, re-cache share and compactions per
session, computed by re-running `build_report` once per distinct
`mode` value present in the corpus (`baseline._by_mode_metrics`), over
a session-filtered sub-corpus for that mode. TTL mix, session baseline
size, mean spawn write and scorecard levels are **not** broken out by
mode, because `topology.TopologyStats`/`ttl.TtlStats` accumulate flat
lists/dicts with no per-session id retained — stratifying those would
need changes to those modules, which is out of this work package's
writable surface.

### Suggested profile, and the overnight-batch override

`profiles.catalogue.suggest(archetype, purposes)` can never return
`"overnight-batch"` by construction — its signature has no session
*mode* parameter (see `profiles/catalogue.py`'s
`UNREACHABLE_BY_SUGGEST`). Since a majority-overnight corpus is exactly
the case that profile exists for, `baseline._suggested_profile` applies
the override itself, directly from the corpus's own `sessions_by_mode`
table: when at least half of the corpus's sessions are classified
`mode=overnight`, the suggestion is `"overnight-batch"` regardless of
what `suggest()` would otherwise say, with the reason string citing the
exact count. Below that share, `catalogue.suggest()` is called
normally.

The baseline never applies the suggested profile. The report names the
profile id and reason only; preview it with `claude-token-lens apply
<id> --dry-run` (see [docs/profiles.md](profiles.md#applying-a-profile)).

### Billing-mismatch warning

A subagent's 1-hour cache TTL is documented as not actually taking
effect under subscription billing (only the top-level conversation
gets 1h there). So when `config.billing == "subscription"` and any
non-top-level row in `ttl_by_agent_type` shows `observed_1h_pct` above
`baseline.BILLING_MISMATCH_THRESHOLD_PCT` (5.0), the record's
`billing_mismatch_warning` names the agent type and percentage —
observing a 1h TTL happening somewhere it's supposed to be impossible
is evidence the stated `billing` value may actually be `"api"`. This is
a heuristic (documented as such in `baseline.py`'s module docstring),
not a locked plan rule — the plan describes the suppression behaviour
but not an exact cross-check threshold.

### The four-section report

`baseline.render_onboarding_report` always produces exactly these
headings, in order: **Summary**, **Suggested profile**, **Projected
saving**, **Next steps**. "Projected saving" is always the
`projected_saving_usd` figure above with a one-line citation back to
the `ttl_by_agent_type` table it was summed from — never a number
computed any other way.

## Privacy

Every baseline field that could carry a filesystem path is redacted
before it is ever written to disk or printed
(`discovery.redact_slug`); nothing here reads message text, tool output,
or raw session content. `tests/test_baseline.py` and
`tests/test_onboarding.py` run every constructed record and rendered
report through `tests/helpers.assert_privacy_deep`.

`init`'s own CLI feedback is the one exception. `Wrote <path>` lines
name `config.toml`/`projects/<slug>.toml`/a baseline relative to
`--config-dir`. The hook-repair, connect and service steps print the
full `settings.json`, backup and service paths. These are the tool's
own operational file locations on the user's own machine, the same class of message
`snapshot-config --install-hook`/`scrub-fixture --out` already print —
not project- or session-derived content, so it is outside the privacy
scan's scope (see `tests/test_cli.py`'s `test_init_writes_config_and_
runs_an_initial_baseline` for the line this distinction is pinned on).
