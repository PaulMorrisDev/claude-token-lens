# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`compaction_sim.py`**: the `autoCompactWindow` sweep — replays every
  top-level transcript's priced turns under each of a fixed set of
  candidate auto-compaction windows (100k/150k/200k/250k/300k/400k/500k/
  none), estimating total cost under each against this corpus's own
  observed compression ratio and post-compaction rediscovery cost, with
  a fidelity self-check against the session's actual configured window
  and a `compaction-window` recommendation naming the cheapest one and
  its projected saving. See [`docs/compaction-sim.md`](docs/compaction-sim.md)
  and the `compaction_sim` entry in
  [`docs/sections-reference.md`](docs/sections-reference.md). Not yet
  wired into `report.py`/`cli.py`/`recommend.py` — see the module's own
  docstring for the exact integration call sites.

## [0.3.0] - 2026-09-19

### Added

- **`docs/first-run.md`**: the numbered, Windows-first walkthrough for a
  first-time user on a locked-down work machine (no admin rights,
  possibly no `git`, possibly no `pip` network access) — install (all
  three routes: `.pyz`, `pip` from a local clone, `pip` from GitHub),
  `init`, the logon-service step and how to confirm it actually
  registered, opening the dashboard, the first `report`, a privacy
  self-check, and a complete uninstall, plus a troubleshooting table
  and a POSIX quick variant. Every command in it was rehearsed
  end-to-end against a synthetic project during this work.
- **Release CI (`.github/workflows/release.yml`)**: on every `v*` tag
  push, builds `dist/claude-token-lens.pyz` with `scripts/build-pyz.py`,
  smoke-tests it with `--version`, and attaches it to the GitHub
  Release via `softprops/action-gh-release`.
- **Cross-platform service installer (`install-service`/
  `uninstall-service`, and `init`'s new logon-service step)**
  (`src/claude_token_lens/installer.py`, work package v3): registers
  `claude-token-lens serve` to run continuously from logon/boot, so
  history isn't lost the first time Claude Code's own
  `cleanupPeriodDays` cleans up a transcript that no watcher was
  running to see. Windows registers a `-RunLevel Limited` Scheduled
  Task (no admin rights) via inline PowerShell cmdlets; Linux writes a
  hardened `~/.config/systemd/user/claude-token-lens.service` unit and
  runs `systemctl --user enable --now`; macOS writes a LaunchAgent
  plist and runs `launchctl bootstrap`. Building a plan
  (`plan_service_install`) never has a side effect, so `--dry-run`
  (on both the new subcommands and `init` itself) always prints
  exactly what would be written/run without touching the machine, and
  every real write/run goes through an injectable `runner` so the test
  suite never shells out to `schtasks`/`systemctl`/`launchctl`/
  `powershell.exe` for real. Running from a `.pyz` build registers an
  action that re-invokes that same archive. `claude-token-lens init`
  now finishes with a "Run the service at logon?" question (default
  yes; `--install-service`/`--no-service` to answer up front; derives
  to *not* installing under `--non-interactive` unless
  `--install-service` is also given), after which it probes
  `is_registered()` and `GET /api/health` once and prints the dashboard
  URL. `GET /api/health` gains a `service_registered: true|false|null`
  field (`null` when the probe can't run at all), cached for ten
  minutes per running `serve` process; the dashboard's Overview tab
  shows a banner when it comes back `false`. See
  [`docs/deploy.md`](docs/deploy.md).
- **Service: baseline and profile ingestion, and the real `/api/profiles*`
  routes** (`service/watcher.py`, `service/store.py`, `service/api.py`,
  work package V3-service): the watcher now ingests every
  `<config_dir>/baselines/*.json` baseline record and every
  `<config_dir>/profiles/*.toml` user profile into new `baselines`/
  `profiles` store tables on each tick, content-hash deduped so a
  repeat tick over an unchanged file is a no-op (`SCHEMA_VERSION` 4 to
  5, for the new `content_hash`/`record_id` columns). `GET
  /api/baseline` now returns the latest stored baseline, its full
  history, and a `capture_status` block (with a one-line human-readable
  `summary`) so the UI can mark recommendations provisional while a
  capture window is open. `GET /api/profiles` now returns the seven
  shipped catalogue profiles plus every stored user profile, each
  tagged `source: "catalogue"|"user"`, and the latest baseline's
  `suggested_profile_id` if any. `GET /api/profiles/<id>/diff` is a
  real route: it diffs the requested profile against the latest config
  snapshot's effective config (or an empty one, with a note, if no
  snapshot has been recorded yet) via `profiles/diff.py`, returning the
  settings/agent/env overlay rows, the unified diff text, and the
  `apply_command`/`launch_command` to run on the host — it never
  accepts a client-supplied project directory, so no filesystem path
  can round-trip through the API. `POST /api/profiles` is a real route:
  it validates the request body against `profiles/schema.py`, rejects
  an unknown key with `400` and the schema's own error text, refuses to
  overwrite a catalogue id (`409`), refuses to overwrite an existing
  user profile unless `?replace=1` is given (`409`), writes the new
  profile TOML file atomically, and re-ingests it immediately so the
  `201` response is consistent with a following `GET /api/profiles`.
- **Service UI: Profiles tab, and a baseline panel on Config** (`service/
  static/app.js`, `app.css`, work package V3-service): a new Profiles
  tab lists the catalogue and user profiles (marking the one suggested
  by the latest baseline), renders a selected profile's diff as
  settings/agent/environment tables plus the full unified diff text,
  and shows the apply/launch commands in a code block with a copy
  button. A minimal "save as a new user profile" form (id, name, a JSON
  settings-overlay textarea) posts to `POST /api/profiles` and surfaces
  the server's `400`/`409` validation message inline. The Config tab
  gained a "Latest baseline" panel (the stored baseline, its history,
  and the capture-window status line); the Recommendations tab shows
  the same "capture window open: provisional" notice while a capture
  window is in progress.
- **v0.3 team aggregate: `export --aggregate`, `import`, `team-report`**
  (`team.py`, v0.3 Task 1): `claude-token-lens export --aggregate
  [--include-projects]` writes one machine's own team document — tool
  version, generated-at, a stable-but-non-reversible `machine_id`
  (salted HMAC-SHA256 over the hostname, same construction/domain-tag
  separation convention as `exports._hash_slug`), the report window,
  and per-group aggregates only (sessions, priced turns, tokens by
  kind, cost, re-cache share, compaction rate, TTL mix, mean spawn
  write, mean report size) across five axes — archetype, mode,
  purpose, agent type, model — plus the corpus-wide scorecard levels.
  No session id ever; a project slug appears only as its hash, and
  only with `--include-projects`. `claude-token-lens import FILE...`
  schema-checks each document (`team.validate_team_document`: an
  explicit key allowlist, no string over 64 characters) before copying
  it into `<config_dir>/team/<machine_id>-<generated_at>.json`,
  exiting 2 with the reason on the first invalid file and writing
  nothing for the rest of the batch. `claude-token-lens team-report
  [--json|--html|--csv-dir]` keeps the latest document per machine and
  renders per-archetype and per-agent-type comparison tables across
  machines (a machine's short hashed id as the column key, never a
  hostname), gated by a minimum-sample rule (5 sessions per cell;
  below that a cell reads `n<5`), with an "observed, not controlled"
  note. See [docs/team.md](docs/team.md) and the README's "For team
  leads" section.
- **v0.3 baseline comparison in the report** (`report.py`/`baseline.py`,
  v0.3 Task 2): `report --baseline <id|latest>` (and every report-like
  subcommand — `sessions`/`recache`/`ttl`/`compactions` — that builds
  the same `ReportModel`) adds a `## Baseline comparison` section:
  cost per session, re-cache share, compactions per session, session
  baseline size, TTL mix (top-level and per agent type), mean spawn
  write per agent type, and scorecard level per dimension, each shown
  as baseline value / current value / delta / delta %, plus a
  per-mode breakdown table (cost/re-cache/compactions only) when the
  baseline recorded a mode mix, gated by the same 5-session minimum
  the rest of the codebase uses. Unresolvable (`--baseline
  does-not-exist`) or absent (`--baseline latest` with nothing saved)
  baselines omit the section and add a note to `## Assumptions`
  instead of failing the run. `baseline.build_baseline`'s own record
  gained the matching fields (`cost_per_session`,
  `recache_share_pct`, `compactions_per_session`, `ttl_mix_top_level`,
  `ttl_mix_by_agent_type`, `session_baseline_size`,
  `mean_spawn_write_by_agent_type`, `scorecard_dimensions`,
  `by_mode`), extracted from an already-built report's own tables via
  new shared functions in `report.py` (`overview_metric`,
  `recache_share_pct_metric`, `compactions_per_session_metric`,
  `ttl_mix_by_agent_type_metric`, `session_baseline_size_metric`,
  `mean_spawn_write_by_agent_type_metric`,
  `scorecard_dimensions_metric`) — never independently recomputed. See
  [docs/onboarding.md](docs/onboarding.md)'s baseline-record table.

### Fixed

- **`statusline.print_install_fragment()` emitted a `-m` command that
  cannot work from a `.pyz` build** (`statusline.py`) — the printed
  `statusLine` fragment (both from `claude-token-lens init` and
  `statusline --print-install-fragment`) always read `py -3 -m
  claude_token_lens.statusline`/`python3 -m claude_token_lens.statusline`,
  regardless of how the tool was installed. Run from inside a `.pyz`
  archive, `-m claude_token_lens.statusline` fails outright (`No module
  named claude_token_lens.statusline`) because the package lives inside
  the zip, not on `sys.path` — a pyz-only user who followed `init`'s own
  printed instructions ended up with a statusline that never worked.
  `print_install_fragment` now mirrors `installer.plan_service_install`'s
  existing pyz-awareness: it detects the running `.pyz` the same way
  (`installer.detect_pyz_path`, also newly hardened to always return an
  **absolute** path even when `sys.argv[0]` itself was relative — the
  same absolute-path requirement `_serve_argv`'s Scheduled-Task/systemd/
  launchd action already depended on) and, when running from one,
  emits `"<python>" "<abs path to .pyz>" statusline` instead. An ordinary
  installed package/checkout is unaffected — the fragment keeps the
  original `-m` form. New unit tests cover both modes (explicit
  `pyz_path=`, auto-detected via `sys.argv[0]`, and the no-pyz default).
- **`apply` user scope resolved the wrong Claude Code directory**
  (`profiles/apply.py`, `cli.py`) — user scope derived its target as
  `config_dir.parent`, so with `config_dir` defaulting to
  `<claude-root>/token-lens` this happened to work out, but the two are
  independent by design (`--config-dir` can point anywhere), and
  deriving one from the other meant a user-scope apply actually wrote
  `<config_dir_parent>/.claude/settings.json` (effectively
  `~/.claude/.claude/settings.json`) and could never find a user-scope
  agent file to patch at all. `plan_apply` now takes an explicit
  `claude_root` parameter, resolved by the new `cli._resolve_claude_root`
  (a new `--claude-root PATH` flag, else `$CLAUDE_CONFIG_DIR`, else
  `~/.claude`) — never derived from `--config-dir`. See
  [`docs/profiles.md`](docs/profiles.md).
- **`apply --dry-run` diffed against a stale snapshot instead of the
  real target files** (`profiles/apply.py`) — the preview text was
  built from the caller's *snapshot* of the effective config
  (`diff.diff_against_effective`), which can already be stale by apply
  time (an agent file hand-edited since the snapshot was taken, for
  example), so the diff could show a change against a value the file
  no longer has. `plan_apply` now renders the dry-run diff
  (`render_plan_diff`) from the exact same `actions` — real
  before/after file bytes — that a real apply writes from, so the
  preview and the real write are provably one computation.
- **`apply --dry-run` exited `0` even when the real apply would
  refuse** (`cli.py`) — a git-tracked target or a missing agent file
  now prints each `plan.blocked` reason to stderr and exits `2` from
  `--dry-run` too, instead of only surfacing the refusal once the user
  ran the apply for real.
- **`init` ignored `--all-projects`/`--project`/`--project-family` for
  its initial baseline capture** (`onboarding.py`) — the baseline was
  always hard-wired to the current directory's own project slug.
  `run_init` now honours all three selectors for the baseline the same
  way `report`'s own project selection does, falling back to the
  current project only when none of the three are given.
- **Git-tracked-file refusal only ever checked project scope**
  (`profiles/apply.py`) — a `~/.claude` kept under version control in a
  personal dotfiles repository could be silently overwritten by a
  user-scope apply, since `_is_git_tracked` was only consulted for
  `project-local`/`repo` targets. The check now applies at every scope;
  `--allow-tracked` still opts in.
- **Frontmatter parser refused any YAML block scalar** (`profiles/
  frontmatter.py`) — a `description: |` or `>` block (with its
  indented continuation lines) raised `FrontmatterError` outright
  instead of parsing. Block scalars are now recognised and kept as
  opaque, byte-preserved blocks: every line is left untouched, and only
  a top-level scalar key or the `experimental:` mapping can still be
  patched (attempting to patch the block-scalar key itself still
  raises, rather than guessing how to collapse it).
- **README's `report` row documented a removed `--allow-titles`
  flag** — the flag itself was removed as part of an earlier fix
  (R17); the CLI reference table's `report` row still listed it as a
  no-op option. Removed.
- **Settings JSON rewrite always reformatted to 2-space indent and
  `\n` line endings** (`profiles/apply.py`) — a merged `settings.json`
  is now written back with the existing file's own indent width (2 vs
  4 spaces) and line ending (`\r\n` vs `\n`) detected and preserved,
  matching how agent-frontmatter patching already only ever touches the
  lines it changes.
- **`tests/test_onboarding.py` imported `assert_privacy_deep` but never
  called it** — the privacy assertion its import implied was never
  actually exercised against `init`'s own output. Now called against
  the written `config.toml`, the written `projects/<slug>.toml`, and
  `init`'s stdout; fixing this surfaced a real leak in the latter
  (`onboarding.py`'s "Wrote ..." confirmation lines printed the full
  absolute path), now printed relative to `config_dir` instead.
- **`/api/summary` windowing bug**: for a given `window_days`, this
  route counted sessions and transcripts by a session row's own stored
  timestamp instead of by the top-level transcript file's mtime — the
  same `window_by="mtime"` rule `discovery.find_sessions`/
  `corpus.load_corpus` already use, and that the CLI `report` overview
  has always honoured. The two could disagree by hundreds of
  transcripts on a real corpus (window_days=7 returned sessions=8/
  transcripts=1867 against the report's sessions=11/top-level 11/
  subagent 270 on the same corpus). `Store.summary()` now windows the
  same way the report does; a synthetic-corpus regression test proves
  the two stay in parity.
- **`profiles/diff.py`: `apply_command` printed the wrong CLI flag** —
  it hardcoded `--project`, but the real `apply` subcommand flag for a
  project directory is `--project-dir` (`--project`, singular, is
  already taken by every subcommand's own repeatable project-slug
  filter). Fixed in `apply_command` and its docstring, and in
  `docs/profiles.md`'s own description of the two-line invocation it
  returns, which had the same stale flag.
- **v3-limits usage-limit tracking** (`limits.py`, work package v3-limits):
  a 5-hour/weekly usage-cap pause, a harness-forced early subagent
  termination, and the desktop app's resume ping are now first-class,
  attributable facts instead of behavioural noise. New `limits` report
  section (`limits_summary`, `limits_hits_by_kind`,
  `limits_agent_terminated`, `limits_pauses`,
  `limits_reset_hour_histogram`, `limits_by_agent_type`,
  `limits_csv_cross_check`) plus `limit_pause_intervals`/`limit_markers`
  for other consumers. Attribution threaded through:
  `classify.py` (`SessionFeatures.limit_pause_s`; pause time discounted
  out of gap/span statistics and the overnight-mode check),
  `recommend.py` (new `limit-pressure` rule), `scorecard.py`
  (`ScorecardInputs.limit_recache_share_pct`/`limit_pause_sessions`
  exclude pause-forced re-cache from the `cache_efficiency` level and
  note affected sessions under `data_quality`), and `statusline.py` (a
  `5h`/`7d` segment gets a `!` warning marker at >=90% used, and a
  `usage-log.csv` row for an exhausted `five_hour`/`seven_day` window is
  tagged `source=limit_hit`). See [`docs/limits.md`](docs/limits.md).
- **v3-limits wired into `report.py`, the `limits` CLI subcommand, and
  the service/UI**: `report.build_report` now folds every transcript
  into a `limits.LimitStats` accumulator and appends the `limits`
  section (gated by `include`, like every other section), with
  `limits.csv_cross_check` bolted on as an extra table whenever
  `usage_log_rows` is supplied; `limits.ASSUMPTIONS` joins the
  report's assumptions list, and the scorecard's `cache_efficiency`/
  `data_quality` dimensions now actually receive
  `limit_recache_share_pct`/`limit_pause_sessions` (previously computed
  fields on `ScorecardInputs` that nothing ever populated).
  `claude-token-lens limits` is a new focused-view subcommand (`overview`
  + `limits`), alongside `recache`/`ttl`/`compactions`. `GET
  /api/session/<id>` gains `limit_markers` (`Store.turns_for_session`,
  reshaping `limits.limit_markers`'s triples into `{"ts", "kind",
  "detail"}` objects); the service UI's session timeline draws them as
  their own marker kind, positioned by timestamp interpolation between
  the session's `first_ts`/`last_ts` rather than by turn index (a
  usage-limit event's `ts` falls inside the gap between two turns, with
  no `turn_series` point of its own), and the `limits` report section
  itself renders on the Cache tab alongside `recache`/`ttl`. See
  [`docs/limits.md`](docs/limits.md), [`docs/sections-reference.md`](docs/sections-reference.md),
  [`docs/api.md`](docs/api.md) and [`docs/ui.md`](docs/ui.md).
- **`cli.py`: removed the dead `apply --dry-run` `--project` ->
  `--project-dir` substitution workaround** — it patched the suggested
  invocation text for a bug in `profiles/diff.py`'s `apply_command`
  that was already fixed (the "printed the wrong CLI flag" entry
  above), so the `.replace(...)` call had matched nothing for a while;
  `apply_command`'s own output now prints through unchanged. README's
  CLI reference table also had three stale "Planned for v0.2/v0.3" stub
  rows for `init`/`baseline`/`serve` left over from before those
  subcommands were implemented, contradicting the real rows already
  above them; removed, and `serve`'s row now documents its real flags
  (`--port`, `--bind`, `--allow-remote`, `--poll-interval`,
  `--retention-days`, `--exclude-project`, `--billing-mode`,
  `--monthly-report`, `--once`, `--purge --yes`) instead of the old
  "prints which milestone it's planned for and exits 2" stub text.
- **`limits.py`: `limits_reset_hour_histogram` used a bare `int` local
  hour (0-23) as its row key** — every other table's first column is a
  non-empty `str` label (the cross-module row-key contract in
  `tests/test_recommend_contract.py`), a mismatch this table was never
  caught on until `limits` was actually wired into `report.py` and
  exercised by that contract test for the first time. Row key is now a
  zero-padded `"00"`-`"23"` string; `Column.kind` updated from `"int"`
  to `"str"` to match.
- **`scripts/windows/Register-TokenLensTask.ps1`: registering the
  Scheduled Task failed with "Access is denied" for a non-admin
  user** — `New-ScheduledTaskTrigger -AtLogOn` with no `-User` creates
  an *any-user* logon trigger, which Task Scheduler treats as
  machine-wide and refuses to register without admin rights, even
  though the task's own `-Principal` was already scoped to the current
  account with `-RunLevel Limited`. The trigger now also carries
  `-User "$env:USERDOMAIN\$env:USERNAME"`, scoping it to this one
  account's logons; the `schtasks /create` fallback mirrors this with
  `/RU "$env:USERDOMAIN\$env:USERNAME" /IT`. Also added
  `-ExecutionTimeLimit ([TimeSpan]::Zero)` to the task settings, since
  `serve` is meant to run indefinitely and Task Scheduler's own default
  72-hour limit would otherwise kill it after three days. See
  [`docs/deploy.md`](docs/deploy.md).
- Overview tab: summary cards failed to render because the render
  callback's parameter order was reversed.
- Test suite: three tests only passed on Windows by coincidence and
  failed on Linux CI — a redacted-slug assertion that depended on the
  shape of the platform's own temp directory, a resolve-month timezone
  test with an arithmetically wrong expected value (masked on Windows by
  a missing-tzdata fallback), and a `~/.claude.json` path-matching test
  that assumed case-insensitive filesystems everywhere. No production
  behaviour changed.
- **`import`: path traversal via an untrusted document's `machine_id`**
  (review finding B1, `team.py`): a team document's `machine_id` was
  written verbatim into `<config_dir>/team/<machine_id>-<generated_at>.json`
  with no shape check, so a crafted `machine_id` (e.g. containing `../`)
  could write outside the team directory. `machine_id`/`generated_at`
  are now validated against the exact shapes this tool's own exporter
  produces (12 lowercase hex characters; an ISO-8601 UTC timestamp)
  both in `validate_team_document` (rejects with exit 2 before any file
  I/O) and again, defence-in-depth, in `save_team_document` itself,
  which also asserts the resolved output path stays inside
  `<config_dir>/team` before writing. Review finding N4: every other
  required string field (`window`, `tool_version`) is now type-checked
  as a string too, not just present.
- **`store.migrate()` dropped every table on any `SCHEMA_VERSION`
  mismatch** (review finding B2, `service/store.py`): an older store
  (e.g. one built by v0.2.0) hit the same code path as a newer,
  unreadable one, silently losing every row on the next `serve` run
  instead of being upgraded in place. `migrate()` now walks an additive
  migration ladder (currently one step, 4→5: `ALTER TABLE ADD COLUMN`
  for `profiles.content_hash`/`baselines.record_id`/
  `baselines.content_hash`, plus the `CREATE UNIQUE INDEX` SQLite
  requires in place of an `ALTER`-added `UNIQUE`, all in one
  transaction that stamps the new version last) and only falls back to
  the old drop-and-rebuild behaviour for a genuinely newer-than-code
  store or a version with no ladder step — and even then takes a
  `service.db.bak-<version>` backup first.
- **`recache_summary.avoidable_cost_usd` double-counted limit-pause
  cost** (review findings B5/N2, `recache.py`/`limits.py`): a
  `limit-expiry` re-cache (forced by a usage-limit pause, not by
  anything the agent could have avoided) was summed into
  `avoidable_cost_usd` alongside genuinely avoidable re-cache, and the
  same cost was *also* reported by the `limits` section — so the two
  sections' cost figures overlapped without saying so. `limit-expiry`
  turns are now excluded from `avoidable_cost_usd`, and a new
  `unavoidable_limit_expiry_cost_usd` row reports them separately; both
  `recache.py` and `limits.py` now carry a note cross-referencing the
  other section's own cost-of-a-limit-pause figure, and
  [`docs/limits.md`](docs/limits.md) documents precisely how the two
  numbers relate.
- **`import`: `FileNotFoundError` when the team directory doesn't
  exist yet** (review finding S2, `cli.py`): the first `import` run on
  a fresh `--config-dir` crashed instead of creating
  `<config_dir>/team/`. `_cmd_import` now wraps the save call and
  turns an `OSError`/`ValueError` into a single-line stderr message and
  exit 2, on top of `save_team_document`'s existing `mkdir(parents=True)`.
- **Service API: mutating routes accepted cross-origin POSTs**
  (review finding S3, `service/api.py`): `POST /api/profiles` and
  `POST /api/sessions/<id>/tags` had no origin check of any kind, so a
  malicious page open in the same browser could POST to the local
  service. `do_POST` now rejects a request whose `Origin` header is
  present and doesn't match the server's own origin, or whose
  `Sec-Fetch-Site` header is present and isn't `same-origin`/`none`,
  with `403 {"error": {"code": "forbidden"}}`, and separately requires
  `Content-Type: application/json` (`400` otherwise). See
  [`docs/api.md`](docs/api.md)'s new "Cross-site protection" section.
- **`compare`: overview headline metrics were arm totals, not
  per-session means** (review finding S4, `compare.py`): `cost`,
  `new_tokens` and `priced_turns` were summed across every session in
  an arm, so an arm with more sessions than the other always showed a
  large headline delta driven by arm size rather than by any real
  difference between the two arms' work. `compare_overview` now leads
  with per-session means (`cost_per_session`, per-session new tokens,
  per-session priced turns); the raw totals are kept as separate rows
  labelled "... (informational)". `compare_by_stratum`'s `cost_a`/
  `cost_b` (and its new-tokens columns) are normalised the same way.
  See [`docs/compare.md`](docs/compare.md)'s new "Overview metrics"
  section.
- **Team documents' `by_agent_type` axis carried raw custom agent
  names** (review finding S10, `team.py`): a project- or user-defined
  custom subagent's name (as opposed to one of Claude Code's own
  bundled agent types) is frequently product- or project-named, which
  is exactly the kind of detail this module otherwise never exports.
  Built-in agent types (and the synthetic `top-level`/`unknown`
  labels) are kept verbatim; any other agent type is now hashed to
  `custom:<8 hex chars>` with the same salted-HMAC construction as
  `machine_id`/project slugs before a team document is ever written.
  See [`docs/team.md`](docs/team.md)'s privacy-guarantees list.
- **Version stayed `0.2.0` throughout the v0.3 release** (review
  finding S5, `pyproject.toml`, `src/claude_token_lens/__init__.py`,
  `service/api.py`): it reached user-visible output — the report's
  "Tool version" line and every team document's `tool_version` field
  (`exports.py`/`team.py`, both already derived from `__version__` and
  so needed no code change) — and the HTTP `Server` response header,
  which was a hardcoded literal. Bumped to `0.3.0`; the `Server` header
  is now built from `__version__` (`claude-token-lens/{major}.{minor}`)
  so it can't go stale on a future release again.

### Planned

A v0.4 backlog, kept here until scheduled into a milestone:

- **Budget check.** `check --weekly-tokens N --daily-usd N` exits non-zero
  when exceeded; UI banner; uses `log-usage` window data when present.
  Guardrail for overnight runs.
- **Anomaly outliers.** Sessions or spawns whose cost is more than 3 median
  absolute deviations from their mode/purpose group, with the composition
  table attached. Catches runaway agents.
- **Scheduled reports.** `serve --monthly-report DIR` exists and is
  threaded onto `ServeOptions.monthly_report_dir`, but nothing consumes
  it yet — no watcher tick actually renders a report on that schedule.
  Wire a month-boundary check into the watcher's poll loop that calls
  `monthly.write_monthly_report` when the directory is set. Habit-forming
  review.
- **Opt-in local path view.** `--show-paths` (local only, never in exports)
  lists the top files by Read tokens, as token-dashboard does.

## [0.2.0] - 2026-09-19

### Added

- **v0.3 `init`/`baseline` onboarding pair** (`onboarding.py`,
  `baseline.py`, work package V3-init): `claude-token-lens init
  [--answers FILE] [--non-interactive] [--no-install]` detects what's
  already on the machine (config-dir/snapshot/usage-log/project-count
  facts), asks — or, non-interactively, derives and reports — a short
  question set (billing mode, excluded projects, settings-overlay
  usage, shared-project-config, timezone, default apply scope, and the
  onboarding capture-window length), writes `config.toml` and this
  project's own `projects/<slug>.toml`, prints the SessionStart
  hook/statusline install fragments (unless `--no-install`), and runs
  an initial baseline capture. `claude-token-lens baseline [--days N]
  [--finalise] [--list] [--show ID]` extracts a JSON baseline record —
  mode mix, dominant purposes, workstyle archetype, scorecard, a
  projected caching saving, a suggested profile, and an optional
  billing-mismatch warning — entirely from an already-built report's
  own tables (never recomputed independently), stored as plain JSON
  files under `<config_dir>/baselines/` (no SQLite), plus a
  four-section Markdown report. `_suggested_profile` applies a
  majority-overnight override that `profiles.catalogue.suggest()`
  can never reach on its own, and `_billing_mismatch_warning` flags a
  subscription-billing config showing an observed 1h TTL on a
  non-top-level agent type. See
  [docs/onboarding.md](docs/onboarding.md) for the full contract,
  including a noted scope gap against `docs/config-layers.md`'s
  richer "what `init` will ask" preview.
- **`config.py`: per-project TOML config and a generic config writer**
  (work package V3-init): a new `ProjectConfig` dataclass and
  `<config_dir>/projects/<slug>.toml` loading
  (`load_project_configs`)/writing (`save_project_config`), five new
  top-level `Config` fields (`capture_window`, `capture_started`,
  `launch_overlays`, `shared_project_config`, `apply_scope`,
  `projects`), and `write_config_values`/`_dump_toml_table` — a
  generic, validate-before-write `config.toml` merger built on the
  existing hand-rolled TOML value formatter, extended to one level of
  nested `[section]` tables, falling back to a `config.toml.new`
  sibling file (leaving the real file untouched) for a shape it can't
  safely round-trip.

- **v0.3 profile schema, catalogue and diff renderer** (`profiles/`,
  work package V3-profiles): a new `claude_token_lens.profiles`
  package with an allowlist-driven `Profile` schema (`schema.py`) —
  every settings/agent-frontmatter/environment-variable key a profile
  may set, with its type, permitted values, and a doc reference back
  to `docs/config-layers.md`/`docs/api.md`, so `validate()`'s
  rejections and [docs/profiles.md](docs/profiles.md)'s tables come
  from the same source of truth. A hand-rolled deterministic TOML
  emitter (`dump_profile`) round-trips every allowlisted value, since
  the standard library has no TOML writer. Seven shipped catalogue
  profiles (`profiles/catalogue/*.toml`, `catalogue.py`'s
  `list_profiles`/`get`/`suggest`) each cite a real report table/column
  as justification, never an invented number. `diff.py`'s
  `diff_against_effective`/`render_unified_diff`/`apply_command` are
  pure functions (no filesystem access) that render a profile's
  proposed changes against a project's effective config for the
  `"user"`/`"project-local"`/`"repo"` scopes, excluding managed-policy
  keys from the diff body in favour of a "managed by policy" note. See
  [docs/profiles.md](docs/profiles.md) for the full schema/catalogue/
  `suggest()`/diff contract, including the one `recommend.py` lever
  (`"mcpServers"`) that has no exact-name allowlist counterpart. This
  package does not wire `cli.py`'s `apply`/`init`/`baseline` or the
  `/api/profiles*` routes — those remain a later work package's scope.
- **`claude-token-lens apply`** (`profiles/apply.py`, `profiles/
  frontmatter.py`, work package V3-apply): applies a catalogue profile
  (or your own profile TOML file) to a project or your user config —
  the host-side write path the V3-profiles entry above deliberately
  left out of scope. `--dry-run` prints `diff.py`'s own unified-diff
  text before anything is written; a real apply backs up every touched
  file byte for byte under `<config-dir>/backups/<ts>/` before writing,
  so `apply --revert <ts>` always restores the exact prior state.
  `frontmatter.py` is a new, from-scratch parser/patcher for a `.claude/
  agents/<name>.md` file's `---`-delimited frontmatter block: it updates
  an allowlisted key in place while preserving every other character
  (comments, unrelated keys, formatting) verbatim, and refuses outright
  — rather than guessing — on any frontmatter shape it cannot safely
  round-trip (tab indentation, more than one level of nested mapping, a
  duplicate key, an unterminated fence, and a handful of other
  ambiguous shapes; see the module's own docstring for the full list).
  A project-scoped write to a file already tracked by git is refused
  unless `--allow-tracked` is given; a profile agent key with no
  existing `<name>.md` file is refused unless `--force` is given (it
  then creates one from scratch). A managed-settings key is never
  written regardless of scope or flags, and an `env` value is only ever
  printed as `export NAME=value` guidance, never written to any file.
  `--launch` writes a one-session `<config-dir>/profiles/<id>.settings
  .json` overlay instead of a persisted apply. `--project-dir` (not
  `--project`, already taken by the global project-slug filter) selects
  the target directory for `project-local`/`repo` scope, matching the
  identical collision `snapshot-config`/`probe-config` resolve the same
  way. See [docs/profiles.md#applying-a-profile](docs/profiles.md#applying-a-profile),
  [README.md's "Applying a profile"](README.md#15-applying-a-profile),
  and [SECURITY.md](SECURITY.md#applying-a-profile-the-one-command-that-writes-outside-config-dir)
  for full detail.
- **`compare` subcommand** (`compare.py`, work package V3-compare, plan
  "Feature expansion" item 6): A/B compare two arms of sessions, each
  independently selected by a `window:<since>..<until>`,
  `key:<key>=<value>` (a flattened config-snapshot key), `profile:<id>`,
  or `project:<slug>[,<slug>...]` spec, stratified by purpose/mode with
  a minimum-sample gate (`--min-sessions`, default 5). Every table
  carries an "observed, not controlled" note plus each arm's exact
  selection rule (plan "Risks and gaps" item 2: correlation is not
  causation), and a dedicated `compare_co_changed` table surfaces other
  config keys that changed alongside a `key:`-selected pair of arms. See
  [`docs/compare.md`](docs/compare.md).
- **`reconcile` subcommand** (`reconcile.py`, work package V3-compare,
  plan "Enterprise use"/"Finance"): offline-only comparison of this
  tool's own per-turn accounting against an Admin API usage/cost export
  CSV (`--admin-csv FILE`, grouped `--by day|model|day,model`), via a
  tolerant header mapper that recognises several plausible Admin export
  column spellings (including `_5m`/`_1h` cache-creation splits and
  `cost_cents`) and reports any column it couldn't place. A parse
  failure names only the 1-based bad-line number, never the row's own
  content. See [`docs/compare.md`](docs/compare.md).
- **Service web UI** (`service/static/index.html`/`app.js`/`app.css`,
  work package S1-ui): a CSP-compliant, framework-free, no-build-step
  UI with ten keyboard-navigable tabs (Overview, Sessions, Cache, TTL,
  Agents, Config, Profiles, Recommendations, Usage, Diagnostics)
  covering every documented `/api/*` route, `prefers-color-scheme`
  dark/light theming reused from the CLI's standalone HTML report, and
  inline-SVG scorecard/table bar charts. The session timeline now
  renders a real per-turn context/cache-creation series with
  compaction/spawn/human markers (see the S1-integration entry below
  for `turn_series`/`markers`), replacing the placeholder this shipped
  with initially.
- **Capture-improvements batch** (`PARSER_VERSION` 3 -> 4): seven new
  additive `Turn` fields, all derived from data the transcript already
  carries -- no new raw content is ever retained:
  - `tool_wait_s`/`model_latency_s`: timing either side of a turn's own
    tool calls, from the timestamps of the tool_result line(s) answering
    that turn's `tool_use_id`s -- `tool_wait_s` is the harness/tool
    round-trip, `model_latency_s` is the model's own think time before
    its next turn. Both `None` when the turn made no tool calls or a
    timestamp is missing.
  - `tool_result_chars_by_tool: dict[str, int]`: per-turn breakdown (by
    tool name) of the same lengths `TranscriptResult.tool_result_chars`
    already totals for the whole transcript, enabling per-turn context
    composition.
  - `agent_brief_chars: int | None` / `tool_input_chars_by_tool: dict[str,
    int]`: the total length of an `Agent`/`Task` tool_use's own `prompt`
    input string(s) (never the text itself), and a per-tool total of
    every tool_use's JSON-encoded input size for the turn.
  - `read_target_hashes: tuple[str, ...]`: salted HMAC-SHA256 (16 hex
    chars) of each `Read`/`Edit`/`Write`/`NotebookEdit` tool_use's own
    target path in the turn -- never the path itself. The salt is a
    random 32-byte file at `<config_dir>/salt`, created on first use via
    `parse.load_or_create_salt` (0600 where the OS supports it) and wired
    into the parser via the new module-level `parse.set_salt`, which
    keeps `parse_transcript`'s own signature unchanged so a
    `ProcessPoolExecutor` worker can still initialise it once per
    process. With no salt set, `read_target_hashes` is always empty.
  - `human_prompt_chars: int | None` / `human_prompt_has_paste: bool`: on
    the turn following a HUMAN_TEXT event, the summed length of that
    event's own text content and whether any of it looks pasted (over
    2,000 chars, or a `[Pasted text` marker) -- `events.classify_line`
    now also populates `Event.size_chars`/`detail["has_paste"]` for every
    HUMAN_TEXT event it emits, which this reads from.
  - Every new field is covered by `tests/test_capture_improvements.py`
    (including a same-salt/different-salt stability check for
    `read_target_hashes` and a scan confirming the hash never contains a
    path segment) and passes the existing `assert_privacy` scan.
  - `probe.compare_with_parser(result) -> list[str]`: lists every
    `<line_type>.<key>` a probed transcript actually carries that
    `parse.READ_KEYS` (a new, hand-maintained map of the top-level keys
    `parse_transcript` reads per raw line type) says the parser never
    looks up -- printed by `probe`'s CLI output under a new "unread keys
    (vs parse.py)" section. Run once over
    `tests/fixtures/real/session-a`: the unread keys are almost entirely
    session/process bookkeeping already captured elsewhere by
    `discovery.py` (`sessionId`, `parentUuid`, `cwd`, `gitBranch`,
    `agentId`, `slug`, `userType`) plus a handful of narrower items worth
    a look for a future batch -- `user.toolUseResult` (a possible
    alternate/duplicate tool-result representation), `queue-operation.
    content`/`reason`, and `system.stopReason` (present but empty in
    every observed line in this fixture, consistent with this batch's
    decision not to implement the related `TranscriptMeta` fields below).
  - Investigated but deliberately **not implemented**: the task's
    suggested `TranscriptMeta.endedReason`/`stopReason`/`maxTurnsReached`
    additive fields. None of the three keys exist meaningfully in
    `tests/fixtures/real/session-a` -- every subagent `.meta.json` in
    that fixture carries only `{agentType, description, model,
    spawnDepth, toolUseId}`, and the transcript's own `stopReason` field
    (on `system`/`stop_hook_summary` lines) is present but an empty
    string across all 104 occurrences, unrelated to subagent completion.
    Left for a future batch once a fixture that actually populates one of
    these keys is available.
  - `topology.py`'s spawn-write table (`topology_spawn_write`) gains a
    "Mean briefing chars" column (from the new `agent_brief_chars`,
    joined back to each direct spawn via the same tool_use_id join the
    skill roll-up uses); the cost-per-spawn table (`topology_cost_per_spawn`)
    gains a "Mean tool wait" column (mean `tool_wait_s` across that agent
    type's own priced turns). Existing columns on both tables are
    unchanged.
- **Config snapshot schema 2** (additive over schema 1 — a schema-1
  snapshot still loads unchanged): `hooks/snapshot-config.py` now
  resolves and records every settings layer (`managed` >
  `.claude/settings.local.json` > `.claude/settings.json` >
  `~/.claude/settings.json`) individually as `settings_layers`, plus the
  merged `effective`/`effective_provenance` result across them; a
  redacted read of `~/.claude.json` (`claude_json` — MCP server/plugin
  names, trust-dialog/allowed-tools counts, and per-project `last*`
  session totals, matched to the current project by
  `normcase(realpath(...))`, never the raw matching key); and
  `content_layers` (sizes/counts/names only, never content, for the
  CLAUDE.md family including a bounded nested walk, `.claude/rules/`,
  `.claude/commands/`, skills, agents source/shadow rollup, `.mcp.json`,
  output styles, auto-memory footprint, and installed plugins).
  `agents` entries are now tagged `source` (`user`/`project`) and, on a
  name clash, `shadowed_by_project`. See
  [`docs/config-layers.md`](docs/config-layers.md).
- Widened the settings allowlist (`autoCompactEnabled`, `modelPricing` —
  present flag + overridden model ids, never the numbers — plus a
  dedicated `statusLine` present-flag shape) and the environment-name
  allowlist (`OTEL_*`, plus enough irregular names —
  `MAX_THINKING_TOKENS`, `MAX_MCP_OUTPUT_TOKENS`,
  `BASH_MAX_OUTPUT_LENGTH`, `DISABLE_NON_ESSENTIAL_MODEL_CALLS` — that
  the existing `ANTHROPIC_*`/`CLAUDE_*` prefixes now cover every
  documented Claude Code environment lever by name). Four of those
  names are numeric caps rather than secrets, so their integer value is
  recorded too (`env_numeric_caps`): `MAX_THINKING_TOKENS`,
  `MAX_MCP_OUTPUT_TOKENS`, `BASH_MAX_OUTPUT_LENGTH`,
  `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`.
- `snapshots.py`: `effective_config`, `effective_provenance`, `layers`,
  `latest_snapshot_per_project`, `build_effective_config_table`,
  `build_config_layers_table`, `build_config_groups_table`,
  `detect_drift`, `build_config_drift_table`, `claude_json_cross_check`.
  `build_config_section` gains optional `include_effective=True` and
  `sessions_with_observed=` keyword arguments (both off by default, so
  an existing caller's output is unchanged).
- CLI: `claude-token-lens probe-config [--project-dir PATH]` scans a
  project's config layers without a session and prints the layers +
  effective-config tables as Markdown, never a raw path. `snapshot-config`
  gains `--project-dir PATH` to run the hook for an explicit project
  directory instead of the current one (named `--project-dir` rather
  than `--project`, which every subcommand already uses for "a
  repeatable project slug to filter a report by").
- **`claude-token-lens serve`'s JSON API** (S1-api, part of the v0.2
  milestone below): every `/api/*` route in `docs/api.md`
  (`service/api.py`'s `make_handler`), the `serve.py` runtime that opens
  the store, runs one watcher tick, and serves until interrupted, and
  the `serve` CLI subcommand (`--port`, `--bind`, `--allow-remote`,
  `--poll-interval`, `--retention-days`, `--exclude-project`, `--once`).
  Ships alongside an egress test proving no route ever opens an
  outbound connection. The watcher thread and the static web UI (also
  part of the v0.2 milestone) ship from concurrent sibling work
  packages.
- **Service watcher and store rebuild (v0.2)** — `FileWatcher`
  (`service/watcher.py`) polls `--projects-root` on a background thread,
  parsing new or changed transcripts (top-level, subagent and
  workflow-nested subagent) and config snapshots into the SQLite store,
  skipping files still inside the 60-second live-file window until they
  stabilise. `service/rebuild.py`'s `corpus_from_store` reconstructs a
  full `Corpus` from the store's `digest_json` columns alone, with no
  transcript files on disk, so a report can still be built for a session
  after Claude Code's own `cleanupPeriodDays` retention has removed its
  transcript.
- **Service integration (S1-integration)** — reconciled the seams the
  S0/S1-watcher/S1-api/S1-ui work packages documented against each
  other, and shipped the v0.2 deployment artefacts:
  - **Store schema v2**: `Store.upsert_snapshot` now dedupes by its
    natural key (`project_id`, `ts`, `schema_version`) via `ON CONFLICT
    DO UPDATE`, backed by a new unique index; `Store.migrate()`
    drop-and-rebuilds the store whenever a stored schema version is
    older than the code's, since the store is always a derived cache.
    Snapshots without a project slug keep their `__global__`
    attribution but `Store.snapshots()` now reports `project_slug` as
    `null` for them, and `/api/config-diff` treats those rows as
    user-level layers rather than a fabricated project.
  - **`workflow_runs` table**: the watcher now persists
    `<session>/workflows/*.json` via a new `workflows.py`, and
    `rebuild.corpus_from_store` reads them back into
    `SessionBundle.workflows`, closing the round-trip loss S1-watcher
    flagged (covered by a new `workflow-session` fixture).
  - **`ServeOptions.billing_mode`/`monthly_report_dir`**: `serve` gains
    `--billing-mode {api,subscription}` (defaulted from
    `<config-dir>/config.toml` when present) and `--monthly-report
    DIR`; the watcher stamps `sessions.billing_mode` from it.
  - **`Watcher.last_stats`**: the `contracts.Watcher` protocol now
    exposes the watcher's own last-tick stats directly, so `serve.run`
    no longer has to guess at them.
  - **`Store.change_token()`**: `api.py`'s report-model memo now
    invalidates on this instead of reaching into `store._connection()`.
  - **Session timeline data**: `GET /api/session/<id>` gains
    `turn_series` (per-turn context size, cache-creation tokens, RE-CACHE
    flag, preceding-primary flag) and `markers` (compaction/spawn/human
    turn indices), documented in `docs/api.md`; `static/app.js`'s
    `buildSessionTimeline` consumes this directly, replacing the
    placeholder chart S1-ui shipped with. `docs/ui.md`'s tab list is
    reconciled with the ten tabs `index.html` actually ships.
  - **CLI**: `serve --purge` (with `--yes`) deletes
    `<config-dir>/service.db` and its `-wal`/`-shm` sidecars after
    printing what it will delete.
  - **Deployment**: a hardened `Dockerfile`
    (non-root, `HEALTHCHECK` via stdlib `urllib`, no curl/wget) and
    `docker-compose.yml` (read-only config bind, named data volume,
    loopback-only port, `read_only` root filesystem, `cap_drop: [ALL]`,
    `no-new-privileges`); a Windows Scheduled Task pair
    (`scripts/windows/Register-TokenLensTask.ps1`/
    `Unregister-TokenLensTask.ps1`, `-RunLevel Limited`, PowerShell
    5.1-compatible); a systemd user unit
    (`scripts/systemd/claude-token-lens.service`,
    `ProtectHome=read-only` plus a carved-out `ReadWritePaths`); and
    `scripts/build-pyz.py`, a dependency-free `.pyz` build targeting
    `claude_token_lens.__main__:main` so real exit codes propagate. See
    [docs/deploy.md](docs/deploy.md).

- **S1-context-budget**: new `context_budget.py` section (`Section(key="context_budget")`)
  answering "do we track preloaded skills, the system prompt, and the
  autocompact buffer?" -- `context_budget_baseline` (per-project measured
  mean/median first-turn `cache_creation` next to labelled `(est)`
  buckets for human prompt, skills listing, memory files, custom agents,
  MCP tools and a residual system-prompt-and-tools share, from
  characters/bytes divided by 4), `context_budget_autocompact` (the
  configured `autoCompactWindow` vs. the observed effective threshold --
  median `compactMetadata.preTokens` over `trigger == "auto"`
  compactions, a new `compaction.effective_autocompact_threshold` helper
  -- the implied buffer, and a >10% drift flag), and
  `context_budget_statusline` (real, non-estimated ground truth once a
  usage-log row carries `context_window` fields). `recommend.py`'s
  `baseline-bloat` rule now cites this section's sized buckets as its
  evidence and names the largest one in its action text when the section
  is present. `statusline.py` appends three new trailing columns
  (`context_window_used_tokens`, `context_window_size`,
  `context_window_autocompact_threshold`) to the usage-log CSV whenever
  the payload's `context_window` carries numeric fields -- old-format
  (six-column) rows are still read without error.
- **S1-exports**: real `prompt_cache` cache ground truth in the
  statusline, an aggregate-only `export` command, and a scheduled
  `monthly-report`.
  - **Statusline cache segment** (`statusline.py`) now renders real
    ground truth instead of a guessed hit ratio: `cache warm 5m 03:12`
    (a `MM:SS` countdown to `prompt_cache.expires_at`) while warm, or
    `cache cold` with an optional `recache ~12k tokens` hint once
    expired; falls back to an estimate (`cache est ...`) only when the
    payload carries no usable `prompt_cache`, now driven by a
    transcript-derived TTL hint
    (`message.usage.cache_creation.ephemeral_1h_input_tokens`) rather
    than the caller-supplied TTL value. The numeric `prompt_cache`
    fields are appended to the usage-log CSV as six further trailing
    columns (15 columns total), feeding a new `cache_ground_truth` table
    in the `usage` section (`statusline.build_cache_ground_truth_table`,
    wired in by `report.build_report` via `usage_log_rows`).
  - **`report`/`sessions`/`recache`/`ttl`/`compactions`** now load
    `<config_dir>/usage-log.csv`, when present, with a tolerant reader
    and pass the rows into `build_report` as `usage_log_rows` — so
    `context_budget_statusline` and `cache_ground_truth` populate for
    the ordinary CLI report, not only for a caller that builds
    `usage_log_rows` itself.
  - **`claude-token-lens export --format csv-flat|json|otel-jsonl`**
    (`exports.py`): a privacy-safe, aggregate-only-by-default export for
    BI/observability tooling — one row per
    `day`/`project`/`model`/`entrypoint`/`agent_type` (`--per-session`
    opts into a `session_id` column), project slugs hashed by default
    whenever aggregate-only is in effect (`--no-hash-slugs` to opt out),
    reusing the existing salted-hash construction and salt file. The
    `otel-jsonl` format mirrors Claude Code's own OpenTelemetry metric
    names (`claude_code.token.usage`, `claude_code.cost.usage`) as an
    offline approximation. See [`docs/exports.md`](docs/exports.md).
  - **`claude-token-lens monthly-report --out DIR [--month YYYY-MM]`**
    (`monthly.py`): writes `claude-token-lens-YYYY-MM.md`/`.html` for one
    calendar month (default: the previous month) — a finance header
    (cost/tokens by model/project/entrypoint, five-hour blocks under
    subscription billing) plus the `usage` section. The same inputs
    always produce byte-identical files (idempotent), and
    `monthly.write_monthly_report` is the entry point the v0.2 service
    will wire up to `serve --monthly-report DIR`.

### Fixed

- **`export --format csv-flat` doubled every CRLF line ending on
  Windows**, corrupting the file for BI/pandas import; `--out` and
  stdout are now opened with `newline=""`.
- **`--per-session` used to default to raw (unhashed) project slugs.**
  `hash_slugs` now defaults to `True` unconditionally; `--no-hash-slugs`
  still opts out but now redacts just the OS-username segment and warns
  on stderr; `assert_privacy` gained a matching slug-shaped-username
  check.
- **Statusline hardening.** Output is now bounded to 120 characters
  (truncating or dropping the cache segment first) and never wraps to a
  second line; echoed string fields are sanitised and length-capped; a
  non-numeric or millisecond-scale `expires_at` is handled correctly;
  the warm countdown shows `expiring` instead of a clock-skew-stuck
  `00:00`.
- **`top_miss_causes` no longer double-counts a sticky field** — it now
  reads the wire's own cumulative `prompt_cache.miss_causes` counts (a
  new `cache_miss_causes` usage-log column) instead of re-counting one
  real miss on every quiet refresh.
- **The usage-log CSV header is now upgraded in place, once,** when a
  legacy file has fewer columns than the current writer expects, rather
  than silently misaligning columns forever.
- **csv-flat/otel-jsonl cache-creation totals now agree**, including for
  pre-TTL-split transcripts, by keying off the same
  `cache_creation_tokens` total rather than the 5m/1h split; a new
  `cache_write_tokens` csv-flat column carries this total explicitly.
- **`cache_ground_truth` now respects the report's own window/project
  scope** instead of including every ground-truth row ever logged; the
  monthly report applies the equivalent month-scoping.
- **`monthly-report` can now actually produce the `cache_ground_truth`
  table `docs/exports.md` already promised** — `write_monthly_report`
  gained the `usage_log_rows` parameter it was missing.
- **`context_window` field-name fallbacks widened** for used tokens,
  window size and percentage; every statusline invocation now records
  the payload's own key names to `statusline-keys.json` when they
  differ from what is stored.
- **`monthly-report`'s "byte-identical" idempotency claim is now
  actually true** — `write_monthly_report` gained a `generated_at`
  parameter (`--generated-at`/`SOURCE_DATE_EPOCH`) for a genuinely
  byte-identical run.
- **`resolve_month`'s "previous calendar month" default now uses
  `config.tz`**, not the machine's own local zone.
- Hash construction and "byte-identical" documentation corrections in
  `docs/exports.md`, `SECURITY.md`, and `README.md` — the project-slug
  hash now genuinely shares `parse.py`'s read-target-path hash
  construction (HMAC-SHA256, distinct domain tag and truncation length
  so the two can never collide).
- **A1 — `recommend.py`'s spawn-cost rule** now only offers the
  `omitClaudeMd` frontmatter lever for agent types that actually have a
  frontmatter file to trim; built-in agent types get `category="workflow"`
  advice instead.
- **A2 — no table row key may be a bare `int`.** `recache.py`'s and
  `topology.py`'s session/turn/depth-keyed tables now carry a real
  string row key with the count in its own typed column.
- **A3 — recommendation evidence values are now formatted by their
  cited column's kind** (e.g. `63.7%`, `47,345`) instead of printed
  raw; the JSON renderer is unaffected by design.
- **`statusline --config-dir` is now honoured.** `cli.py`'s
  `_cmd_statusline` never forwarded `args.config_dir` to
  `statusline.main()`, so an explicit `--config-dir` was silently
  ignored and the real `~/.claude/token-lens` was written to instead;
  found and fixed during v0.2 release verification against a real
  corpus.
- **`serve --once` now prints its `WatcherStats` line** (duration,
  discovery/parse/store timings, file and session counts) instead of
  discarding them silently, matching what `docs/api.md` already
  documented as a diagnostic.
- **Report-backed API routes now accept `since`/`until`.**
  `/api/report.json`, `/api/ttl`, `/api/config-diff` and
  `/api/recommendations` previously read only `window_days` and
  silently ignored `since`/`until`; they now resolve the window the
  same way the CLI does and cache the result under a `(window_days,
  since, until)` key. See `docs/api.md`.
- **`load_or_create_salt` now opens the salt file in binary mode on
  Windows.** The previous text-mode `os.open()` call silently turned a
  `\n` (`0x0a`) byte in the random salt into `\r\n`, corrupting the
  stored salt whenever one was drawn — the root cause of the
  intermittently flaky `test_load_or_create_salt_persists_across_calls`.

### Security
**`.pyz` zipapp: `_load_snapshot_hook_module` crashed under zipimport**
(`cli.py`, work package V3-init, found while wiring `init` to the same
hook loader) — unrelated to the v0.2-exports batch above:

- `importlib.resources.files(...)` returns a `zipfile.Path` inside a
  built `.pyz`, which `importlib.util.spec_from_file_location` rejects
  (`TypeError: expected str, bytes or os.PathLike object, not Path`) —
  this pre-existing bug affected `snapshot-config` and `probe-config`
  too, but no test exercised either via a built `.pyz` fixture before
  now. Fixed by reading the hook script's source text and `exec`-ing it
  into a fresh `types.ModuleType`, which works identically on a normal
  filesystem install and inside a zip.

### Planned

- Confirmed no route may return `transcripts.path`/`projects.root_path`
  during v0.2 release verification: a full privacy audit of every saved
  API response body, the generated report, and a full-text dump of
  `service.db` (all tables, decompressed `digest_blob`) found no path,
  username, or over-length string leak.

## [0.1.0] - 2026-09-19

### Added

- Repository scaffold: licence, changelog, security policy, `pyproject.toml`,
  the frozen `model.py` data contract, `render/tables.py` cell-formatting
  primitives, and the `cli.py` argparse skeleton with subcommand stubs.
- `workstyle.build_section`, `workflows.build_section`, and
  `snapshots.build_config_section`: report `Section`/`Table` wrappers for
  archetype counts, workflow-run summaries, and the config diff table,
  matching the pattern already used by `classify`/`compaction`/`recache`/
  `ttl`.
- `recommend.py` (WP10b): `recommend()` turns an assembled `ReportModel`
  into evidence-backed `Recommendation`s, implementing every Appendix A5
  rule (`ttl-switch`, `long-tool-waits`, `notification-invalidation`,
  `batch-instructions`, `subagent-volume`, `compaction-churn`,
  `long-context-share`, `cache-read-dominance`, `baseline-bloat`,
  `agent-report-size`, `spawn-cost`, `effort-mismatch`,
  `discovery-share`, `pricing-coverage`, `data-quality`) with archetype
  gating, a minimum-sample gate, and managed-settings-aware scope
  encoding; `render_patch_set()` renders the settings/frontmatter changes
  a recommendation set implies as unified-diff-style text.
- `report.build_report` now accepts `session_overrides` and populates
  `ReportModel.recommendations` via `recommend.recommend()`, using the
  corpus's own archetype and latest config snapshot.
- CLI wiring (WP10c): `report`/`sessions`/`recache`/`ttl`/`compactions` are
  now real, backed by `report.build_report` (the four focused subcommands
  via `include={"overview", <section>}`), with shared `--json`/`--html`/
  `--csv-dir`/`--phases`/`--allow-titles` output flags and `--patch-set`
  on `report` (a no-op until `recommend.py` lands, guarded by
  `importlib.util.find_spec`). `config-diff --key K|--auto-keys` is its
  own standalone consumer of `snapshots.build_config_diff_table`.
  `log-usage`, `scrub-fixture` and `statusline` delegate to their
  existing modules; `probe` (new `probe.py`) is a content-free schema
  histogram (line types, key names, attachment types, system subtypes,
  `version` field values -- every recorded string capped at 64 chars) of
  a project or a single transcript file, for pasting into a bug report
  without leaking transcript content. `init`/`baseline`/`serve` now
  print which future milestone they're planned for. Added `--jobs` to
  the global flag set. `__main__.py` makes `python -m claude_token_lens`
  (and a `python -m zipapp`-built `.pyz`) propagate the real exit code.
- `Recommendation.scope: str = "user"` (`"user"` | `"repo"` | `"managed"`,
  plan "Enterprise use" section) lands as a real `model.py` field on the
  wp10b/wp10c merge, replacing the `"[managed] "` string prefix on
  `Recommendation.lever` that `recommend.py` used as a workaround while
  `model.py` was outside its work package's file list. `render/
  markdown.py` and `render/html.py` show it alongside `Lever:`;
  `render/json_out.py` emits it automatically (generic dataclass-field
  serialisation). `cli.py`'s report-like subcommands now also load
  `<config_dir>/sessions.toml` via `config.load_session_overrides` and
  pass it to `report.build_report(session_overrides=...)`, closing the
  gap `report.py`'s module docstring flagged (WP10a had no `config_dir`
  parameter to load it from).

### Fixed

Independent review of the WP4/WP6/WP8 report-assembly modules found 15
defects (12 from the numbered review pass, 3 added by the coordinator
alongside it), each fixed and landed as its own commit, same
one-commit-per-fix / green-tests discipline throughout:

- **RE-CACHE detection and attribution (`recache.py`, `compaction.py`)**
  — cache-hit signatures are now applied to transcript turns before
  they're scored, instead of leaving every turn unsignatured; added a
  token-weighted control group and a prefix-invalidated primary-cause
  table (invalidation cause was previously undercounted for
  prefix-broken turns); `compaction.py` now marks turns as re-cache via
  the shared `recache.apply()` detector instead of a separate internal
  `is_recache_turn()` heuristic, so its RE-CACHE-flagged write-cost
  figures use the same signature logic as every other module.
- **TTL simulation (`ttl.py`, `compaction.py`)** — simulated turns are
  now priced per-turn via the rates lookup rather than a single blended
  rate; added unconditional counterfactual buckets and a single,
  consistent expiry-loss basis across the 5m/1h comparison; renamed
  `in_window_share`/`break_even_share` to `in_window_pct`/`break_even_pct`
  (stored as percents, turn 0's own prefix included in the shared
  denominator); `delta_usd`/`delta_pct` keep their sign and gained a
  `saving_usd = max(0, delta_usd)` companion; added a `TtlThresholds`
  dataclass (`from_config`/`describe()`, mirroring `RecacheThresholds`)
  threaded through the simulation, fidelity, and report-building
  functions; `TtlTypeStats.recommendation` changed from a property to a
  method taking `th`; `compaction.py`'s RE-CACHE trio now also reads
  from `RecacheThresholds` instead of hardcoding it a second time.
- **Compaction accounting (`compaction.py`)** — added
  `CompactionRecord.join_delta_s` and a module-level `new_tokens()`
  helper (`input_tokens + cache_creation_tokens`); post-compaction
  write/recache cost aggregates and the per-session cost column now
  exclude records whose join to their next turn took longer than 15
  minutes, and the dropped-token share is reported against both the
  `cache_creation` and `new_tokens` denominators.
- **Event and purpose classification (`events.py`, `classify.py`,
  `parse.py`)** — `classify_line` now tests
  `TOOL_DENIAL`/`TOOL_RESULT`/`TASK_NOTIFICATION`/`PEER_MESSAGE` before
  the generic `isMeta` check, so a line carrying both markers gets the
  more specific kind; `META` events gained a `subkind` (the line's
  `origin.kind`, else its leading XML-ish tag name, else `"plain"`).
  `classify_purpose` now checks intent signatures (local-llm-pipeline,
  workflow-run, review, test-triage, planning, docs-or-light-edit,
  refactor) before falling through to the generic
  `agent-fanout`/`general-dev` buckets; `review` relaxed to tolerate up
  to 2 edit turns, `test-triage` relaxed to drop its
  hits-vs-edit-turns comparison once hits reach 5; `build_section` now
  reports its active overnight local-time window as a note.
  `detect_archetype` (`workstyle.py`) now tests `chat-only` before
  `single-model` so a chat-only session with a single resolvable model
  family is no longer misclassified. Pre-split `cache_creation` reads
  (`parse.py`) are now treated as a format difference
  (`Diagnostics.pre_split_turns`) and normalized via
  `ttl.normalize_ttl_split`, rather than silently mis-parsed.
- **Report sections (`workstyle.py`, `workflows.py`, `snapshots.py`)**
  — added `build_section` to all three, matching the pattern already
  used by `classify`/`compaction`/`recache`/`ttl` (see Added above).
- **Statusline robustness (`statusline.py`)** — `_last_assistant_ts`
  now scans the transcript tail with `text.split("\n")` instead of
  `str.splitlines()` (the latter also breaks on `\r`/`\v`/U+2028/U+2029,
  which can legally appear inside a JSON string value and would shear a
  JSONL line into unparsable fragments); `main()` guards
  `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` in a
  `try`/`except`, and every print now goes through a `_safe_print`
  helper that never lets a print failure escape.
- **Test helpers (`tests/helpers.py`)** — `assert_privacy` now inspects
  `Table` cells and `Section` notes, not just top-level dataclass
  fields.

Two deviations from the original plan text, found and reported rather
than silently reconciled:

- **WP4 fidelity bar.** The plan's TTL break-even section (Appendix A4)
  is referenced elsewhere as flagging simulation fidelity above 5%, but
  Appendix A4 itself specifies "flag agent types above 10%" —
  `TtlThresholds.fidelity_warn_pct` implements the 10% figure that
  Appendix A4 actually states.
- **A2 META description.** Plan Appendix A2's detection table describes
  `META` lines without a `subkind`, but observed transcripts carry
  enough structure (an `origin.kind`, or a leading XML-ish tag in the
  content) to sub-classify them usefully — the events.py fix above adds
  `subkind` as an extension of A2's table rather than a literal
  implementation of it.

Further independent review (findings R4/R5/R7/R8/R15/R19/R23), each fixed
and landed as its own commit:

- **R4** — `parse.py`'s `_redact_paths` now also redacts relative
  Windows paths (no drive letter) and `<user@host>`-shaped `@`-tokens,
  not just absolute `C:\Users\<name>\...` paths.
- **R5** — `report.py`'s `--group-by agent`/`model`/`entrypoint` re-fold
  now keys `RecacheStats` by transcript instead of by session, so a
  subagent transcript is grouped under its own agent/model/entrypoint
  rather than inheriting its parent session's.
- **R7** — `report.py`'s scorecard context-hygiene stats
  (`median_ctx`/`median_top_level_ctx`) are now computed from top-level
  transcripts only, not every transcript (subagent ctx values, which run
  much larger, were skewing them).
- **R8** — `compaction.py`'s `compactions_per_session_mean` now divides
  by every session, not just the sessions that compacted; the old value
  is kept as `compactions_per_compacting_session_mean`.
- **R15** — `usage.py`'s five-hour usage blocks now assign each priced
  turn to the block containing that turn's own local timestamp, instead
  of stamping a whole session's turns onto the block its first turn
  landed in (a session spanning several blocks, e.g. 12+ hours, now
  splits across all of them).
- **R19** — stale "WP10 will..." notes in `compaction.py` and
  `topology.py` rewritten to describe current behaviour: the RE-CACHE
  join to the shared `recache.py` detector already landed (WP10b); the
  topology spawn-write/session-baseline tables were never joined against
  a session's snapshot MCP/plugin counts and no such join is planned.
- **R23** — `usage.py` now skips bundles with no top-level transcript
  (an orphaned subagent whose parent session was never discovered) the
  same way `report.py`'s `build_report` always has, so the two sections'
  session/turn counts no longer disagree on a corpus containing one.

Also landed alongside the above, same discipline:

- `report.py` — the "min sample" values printed alongside recommendation
  thresholds are now the ones `recommend()` actually applies
  (`RecommendThresholds`'s own `min_sessions`/`min_turns`, which can be
  overridden independently of `Config.min_sessions`/`min_turns`), not
  `config.min_sessions`/`min_turns` directly.
- `report.py` — the overview `totals` table gained two rows,
  `top_level_median_ctx` and `top_level_turns_ctx_ge_200k_pct`, computed
  from the same top-level-only record set as the R7 fix, giving the
  "long-context share of recent top-level turns" plan anchor a
  turn-count-basis, top-level-only figure to check against.

**Note from the round-3 review, resolved below:** `parse.py`'s redaction
behaviour changed under R4 above in a way that changes parsed output for
previously-cached transcripts (a path that previously leaked through
`_redact_paths` is now redacted) — `PARSER_VERSION` in `__init__.py`
needed bumping to invalidate stale cache entries, per that constant's
own doc comment. Not done in round 3: `__init__.py` was outside that
change's file scope. Done as part of the `0.1.0` release prep (see
"Release prep" below).

Independent review (round 3) fixes, `cli.py`/`recommend.py`/`ttl.py`:

- **Breaking:** removed the `--allow-titles` CLI flag. It implied a
  privacy control that never existed — `report.py`'s own module
  docstring documents `build_report`'s `allow_titles` parameter as a
  permanent no-op, since nothing anywhere in this codebase captures
  `customTitle`/`ai-title` text to gate in the first place.
  `build_report` still accepts the keyword (unused) for signature
  compatibility.
- New `--tz ZONE` flag overrides `config.toml`'s `tz` for a single run,
  threaded through every report-like subcommand and `config-diff`.
- `--quiet` now actually does something: it used to be accepted by
  argparse (mutually exclusive with `--verbose`) but never once
  consulted, so passing it silently changed nothing.
- `scorecard.py`'s `[thresholds.scorecard]` overrides are now validated
  for correct ascending/descending ordering; a misordered tuple used to
  silently score a corpus at the wrong level and now raises a clean,
  named `claude-token-lens report: ...` error (exit 2) instead.
- `init`/`baseline`/`serve`'s `--help` listing now leads with the same
  `(planned)` marker every other not-yet-implemented subcommand uses.

Independent review (round 4) fixes, merged from `fix-review3-a` into
`main` for this release, plus release-prep work:

- **Release prep.** `PARSER_VERSION` bumped `2` -> `3` in `__init__.py`
  (parse-time redaction changed under R4 above, invalidating
  previously-cached digests) and `pyproject.toml`'s version bumped to
  `0.1.0`, resolving the round-3 follow-up note above.
- **`cli.py`** — `report --json --patch-set` used to append the
  patch-set text after the JSON blob, producing invalid JSON on
  stdout. `--json` now embeds the patch set under a top-level
  `patch_set` string key instead of printing anything else to stdout;
  `--html`/`--csv-dir` write a sibling `patch-set.txt` file next to
  their output; Markdown mode is unchanged (still appends the patch
  set after the report text).
- **`recache.py`** — the two "Re-cache primary cause" tables had
  unstable row order for tied all-zero rows, caused by Python's
  randomized `StrEnum`/set-iteration hashing. Every sort in
  `build_section` now ties-break on the row key string, so output is
  deterministic across runs regardless of `PYTHONHASHSEED`; covered by
  a determinism test that builds the section twice from shuffled
  input.
- **Config-dir semantics** — `hooks/snapshot-config.py`'s
  `resolve_config_dir` treated an explicit `--config-dir` as the
  `~/.claude` root and appended `token-lens/snapshots`, while
  `cli.py`/`snapshots.py` already treated an explicit value as the
  token-lens directory itself. Reconciled on the majority convention:
  an explicit `--config-dir X` is the token-lens directory everywhere
  (`X/snapshots`, `X/cache`, `X/config.toml`, `X/usage-log.csv`);
  the default remains `~/.claude/token-lens`, honouring
  `CLAUDE_CONFIG_DIR`. `snapshots.load_snapshots` and the hook script
  (still standalone, no package import) were both updated, with a new
  round-trip test: the hook writes a snapshot with `--config-dir tmp`,
  then `config-diff --auto-keys --config-dir tmp` finds it.

### Documentation

- Retired stale forward-looking "WP8"/"WP10"/"WP12"/"WP3" notes in
  `classify.py`, `corpus.py`, and `ttl.py` now that those work
  packages have landed, replacing them with statements of current
  fact about what populates each field and why `recache_signature` is
  still unset in `report.py`'s TTL path today.
- README's Performance section now carries real timings (`22.1s` cold
  with `--jobs 1`, `7.6s` warm, `12.6s` warm with `--jobs 4`, measured
  2026-09-19 on the owner's own live 1.6 GB corpus: 120 sessions,
  1,644 subagent transcripts, 29 workflow runs, 30-day window)
  replacing the `<cold>`/`<warm>`/`<jobs4>` placeholders; the TTL
  section now also documents that `ttl.py` prints per-agent-type
  simulation fidelity and suppresses TTL-switch advice when the
  projected saving doesn't clear the simulation's own error margin or
  fidelity exceeds the configured bound.
- README rewritten against the code as it actually stands today (WP12b):
  what it measures and cannot (no billing API, user-supplied prices,
  subscription usage-window billing, the JSONL format's observed-not-
  published status and its two stable alternatives), the two token
  totals with a worked example, how Claude Code's prompt cache and its
  5m/1h TTL levers work, the RE-CACHE definitions and signatures, a
  section-by-section report reading guide (moved into
  `docs/sections-reference.md` once it grew past README-length) with a
  real worked example generated from `tests/fixtures/real/session-a`,
  the TTL simulation assumptions verbatim from `ttl.ASSUMPTIONS`,
  SessionStart-hook and statusline installation fragments generated
  from the code (not hand-typed), Windows-specific notes, an honest
  team/enterprise section naming what's implemented (`exclude_projects`,
  `retention_days`, managed-settings capture, Bedrock/Vertex provider
  detection) versus only planned (Foundry detection, per-provider
  pricing, an `export` command), prior-art credits, and licence/
  contributing/roadmap. `SECURITY.md` rewritten as a corporate-review
  sign-off checklist, correcting an earlier claim that an automated
  egress test already exists (it doesn't — there is no `serve` surface
  yet to test; the guarantee today is structural: no networking library
  is imported anywhere in the codebase) and clarifying that
  `--no-cache`/`--rebuild-cache` are parsed but not yet acted on by any
  CLI subcommand. Both files are written to match the CLI's actual
  current state: only `pricing-check` and `snapshot-config` are wired
  up; every other subcommand is a stub.
- README and `docs/sections-reference.md` brought up to date against
  the WP10c CLI wiring and WP10-merge `Recommendation.scope` (WP12c):
  the `<!-- CLI-USAGE -->` placeholder replaced with a subcommand table
  and exit-code reference generated from each subcommand's own
  `--help`; every "not yet built" callout that WP10a/WP10b/WP10c closed
  (`overview`, `usage`, `scorecard`, the `report` command, recommendations
  with `scope`, `--patch-set`, `--no-cache`/`--rebuild-cache` actually
  being wired, the `statusline` and `config-diff`/report `config`
  section distinction) rewritten or removed, verified by running the
  CLI against `tests/fixtures/real/session-a` and reading
  `report.py`/`recommend.py`/`scorecard.py`/`usage.py`/`probe.py`
  directly; the zipapp caveat fixed at the doc level (build against
  `claude_token_lens.__main__:main` instead of `cli:main`, verified with
  a fresh `.pyz` build whose `--version` exits 0 and `serve` exits 2);
  a new Performance subsection recording where the cold/warm/`--jobs`
  timings and the digest-cache-location note live; `docs/sections-reference.md`
  gained `overview`/`usage`/`scorecard` tables and Recommendations/
  Diagnostics blocks. The remaining genuine gaps (Foundry detection,
  per-provider pricing tables, `--allow-titles` being a no-op, and
  `usage_windows` not being wired into the assembled report) are kept,
  stated honestly rather than papered over.
