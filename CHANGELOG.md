# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

After updating, the first dashboard start re-reads every transcript (a
few minutes): `PARSER_VERSION` bumped to 18 (from 14) to pick up each
reply's fast-mode flag, the fuller edit records, the quality markers,
the metrics-capture tags and notes below, the feedback tag, the
capture-integrity fixes below (tag/reminder splitting, forged-tag and
self-authorisation rejection, the coverage-denominator and per-call
sizing corrections), and each hook call's event name, real duration and
whether it was Token Lens's own (below).

### Added

- **Metrics capture (opt-in, off by default, and it uses tokens while
  it's on).** Turn it on and Claude ends each reply — and a subagent's
  final report — with a one-line, closed-vocabulary tag such as
  `[tl: task=bugfix brief=clear]`, at one of four levels (Free,
  Essentials, Standard, Deep) that each add more of it; nothing outside
  the fixed word lists is ever kept. `init`'s last-but-one question
  offers it, after a warning that it costs tokens and a table of what
  each level would have cost over your own last 14 days; turning a
  level on asks one more question, a 14-day time-box that switches
  capture back off by itself unless you turn the limit off or set a
  different length (`capture on --for`). `claude-token-lens capture`
  (`status`/`on`/`off`/`level`/`enable`/`disable`/`connect`/`remove`)
  changes it at any other time, always showing the `settings.json` diff
  first and asking before writing it. See
  [`docs/capture.md`](docs/capture.md).
- **A capture banner and Capture tab on the dashboard.** A banner under
  the health banner, on every tab, shows the running token cost,
  coverage, and a note when hooks are missing, no notes have been seen,
  the time-box has passed, or there isn't enough data yet. The Capture
  tab adds level cards, a row per metric (what it captures, the exact
  tag, why, what it feeds, estimate against actual cost, how much has
  been collected), and sampling and time-box controls, each repeating
  the cost warning before anything that spends more tokens. Turning
  something on writes only `[capture]` in Token Lens's own
  `config.toml`, from a loopback request; it never touches Claude
  Code's `settings.json` itself — a missing hook entry shows the
  `capture connect` command to run instead.
- **Free capture signals, and none of them need a level.** How a
  session ended, how long you waited on a notification or a permission
  prompt, and (already in every transcript, so no hook is needed) which
  instruction files, commands, skills, task lists and API errors came
  up are logged to `<config-dir>/signals/`, keyed by a salted hash of
  the session id rather than the id itself.
- **The `/tl-feedback` skill and a second status line.** `capture
  feedback on` (or saying yes to `init`'s last question) adds an
  optional skill you run after a piece of work to rate whether it
  delivered, what slowed it, whether it was worth the tokens and what
  would have helped — shown in full and written only after a yes, and
  it works at any capture level, even off. The statusline can now show
  a second line: a live coaching hint (a large context building up,
  a large last tool result, many reads so far) or a reminder to run
  `/tl-feedback`; the first line is unchanged.
- **Work habits tab and habit playbook.** A new section, built per
  message and per agent run, turns everything metrics capture and your
  own feedback have reported into a weekly digest and a playbook of
  habits worth trying, each with its evidence, a rough saving, and
  where the evidence came from (what Claude reported, what the
  transcript shows, or your own feedback, in that order of trust).
  Brief templates — checklists per kind of task, built from what your
  own requests tend to lack — and an optional `/tl-brief` skill that
  checks a new request against its checklist are part of the same tab.
  The model-tier, effort-fit, spawn-CLAUDE.md and wasted-turns checks,
  and session purpose, now also read what capture reported, and
  `report`, `compare` and `config-diff` read the dashboard's own tags
  and ratings.
- **Profiles tuned per kind of task.** Once enough sessions carry a
  reported task, the Work habits tab breaks cost and how often the
  work went well down by task, model and effort, and picks the
  cheapest setup that did as well as your usual one; a new "A profile
  for one kind of task" goal on the Profiles tab drafts from it, and
  `compare` can stratify by task the same way. A `capture` change (a
  level, enabling a metric) now counts as a change point the same way
  an `apply` does, measured by capture's own token cost and the share
  of messages tagged.
- **Claude can say why it re-ran an agent and whether one finished.**
  "Is any agent struggling?" offers two lines for `~/.claude/CLAUDE.md`
  (about 100 tokens, read from the prompt cache after each session's
  first reply) that ask Claude to start a re-run agent's brief with
  `[retry: model|brief|tools|other]` and a subagent to end its last
  reply with `[result: done|partial|blocked]`, about six output tokens
  each. Only the word is kept. A retry that blames the brief, tools or
  something else no longer counts against the cheaper model (two or more
  for one agent become a tip to fix its task prompt or tools); one that
  blames the model counts even for a different agent type. Partial or
  blocked counts as didn't finish. The Agents tab adds **Why agents were
  run again** and, under advanced, **Markers Claude wrote** with how
  often each was written and what it cost. The fix explains how to
  remove the lines again.
- **Spots when a cheaper model wasn't enough.** When an agent's run on
  a cheaper model is followed, in the same session, by the same agent
  started again on a larger model that edits the same files within two
  hours, the run counts as retried on a larger model. The Agents tab
  lists each agent and model this happened to ("Agent runs retried on a
  larger model"), and the retried share joins the quality signals and
  Profiles' before-and-after. Once a tenth of an agent's runs on a model
  were retried, that model is no longer suggested for it: the models
  recommendation, the Models quick action and the Profiles models goal
  skip it and say why. When the agent file is on that model and it
  happened twice or more, "Is any agent struggling?" offers to move it
  back up; when the agent file names another model, it says the cheaper
  model was picked by whatever started the agent. On real history this
  flagged claude-implementer on Haiku (4 of 31 runs retried on Sonnet,
  against 2 of 350 Sonnet runs retried on Opus), which the models check
  had been recommending.
- **The dashboard opens straight away.** `serve` now binds its port
  before reading your history, instead of refusing connections until a
  first scan of the whole history finished (a minute or more on a large
  one). A banner shows the scan's progress (files found, read and
  stored), and once it finishes offers **Redraw figures**.
- **Tabs no longer wait on a report rebuild while you work.** Every
  reply in a live session changed the store and threw away every built
  report, so each tab opened afterwards rebuilt the whole report first,
  and the "last hour" and "last 24 hours" windows rebuilt every minute.
  The dashboard now answers from the report it has and rebuilds it in
  the background, and its footer says what time the figures are from.
  A report is only rebuilt while you wait when there is none for that
  window yet or it is over ten minutes old. Responses built from a
  report carry `X-Figures-As-Of` (and `X-Figures-Refreshing: 1` while a
  newer one is built).
- **Every change now ends by telling you to restart Claude Code.**
  Claude Code reads settings and agent files when it starts, so a
  session already open kept the old ones with nothing saying so. Every
  fix on the dashboard and in `report`/`check` output, the Profiles
  tab's apply command, and `apply`, `apply --revert`, `init`'s connect
  and hook repair and `uninstall`'s settings removal now say to restart
  it, and every prompt for Claude asks it to remind you once it has
  saved.
- `serve --store PATH` puts the dashboard's database somewhere other
  than `<config-dir>/service.db`, so a second copy (a dev checkout) can
  run beside the logon service without sharing it.
- **Edits made through a shell command now count.** The quality
  signals (edits, files edited, edited again, retried on a larger model)
  saw only Edit, Write and NotebookEdit. They now also see MultiEdit and
  the files a Bash or PowerShell command writes with content it
  authored: `sed -i`, `perl -i`, `Set-Content`/`Add-Content`, a heredoc
  or `echo` redirected to a file. A program's output captured to a log
  (`npm test > test.log`) isn't an edit. Relative paths resolve against
  the directory the command ran in, and Git Bash's `/c/` form matches
  `C:\`, so the same file changed both ways counts once. Only a salted
  hash of each path is kept, as before. An edit whose tool call failed
  (the text to replace wasn't found, you declined it) no longer counts.
- **A hook that fails on most of its calls is now flagged.** Every hook
  attachment Claude Code writes to a transcript (`PreToolUse`,
  `PostToolUse`, and so on — the event name only, never the
  matcher/tool-name suffix, so an MCP server or tool name can never
  surface) is tallied by outcome; `capture status` now prints one plain
  prompt when a hook's non-blocking-error rate crosses 50% over at
  least 20 calls, naming it, its failure share, where to find it in
  `settings.json`, the latency/noise trade-off, and the undo. This only
  ever prints — nothing here changes `settings.json`.
- **`capture status` now shows Deep's actual measured wait**, replacing
  the old, unsourced "a fraction of a second" guess: the big_output/web
  PostToolUse hook's real `durationMs` (Claude Code records one on every
  hook call; this parser used to drop it) is now kept, and while either
  metric is on, `capture status` prints the median and p90 wait over
  Token Lens's own calls in the last 7 days ("Deep's large-output/web
  hook waited ≈Ns (median, p90 ≈Ns) over N calls this week").
- **The digest cache now carries a salt fingerprint.** A cache entry's
  path/skill-name hashes are salted; without recording which salt wrote
  them, a cache hit after the salt rotated (e.g. a fresh `~/.claude`) would
  keep serving hashes salted under the old one. `DigestCache` now hashes
  the salt itself (never the raw salt) into each entry's header and
  misses when it doesn't match a reader that was itself given a salt; a
  reader given no salt is unaffected.
- **Signal files and the capture-change log now prune themselves by
  default.** `serve`'s watcher already pruned report data
  (`retention_days`) only when you set it; it now also prunes
  `<config-dir>/signals/` and `capture-log.jsonl` on every tick
  regardless, at `retention_days` when set or a new 180-day default
  (`config.SIGNAL_RETENTION_DEFAULT_DAYS`) otherwise — this is Token
  Lens's own background telemetry, not visible report data, so it was
  never meant to accumulate forever. `capture prune` runs the same
  housekeeping by hand (`--dry-run` to preview) for anyone not running
  the service.
- **Did your estimate come true? (P8: evidence, back-test, prediction
  log.)** "Your changes and what they did" now backs its before/after
  verdict with a real statistical test — a ratio-of-sums estimate with
  delta-method variance, Holm-corrected across the measures compared in
  one change, giving each a `lower`/`possibly_lower`/`higher`/
  `possibly_higher`/`no_clear_change`/`too_little_data` verdict instead
  of only "about the same" or a raw percentage — and the "after" side is
  now reweighted to match "before"'s mix of task/purpose first, so a
  change in the kind of work people did after a settings change doesn't
  read as the change's own effect. A session's own transcript can now
  surface a change point nothing else caught: a CLAUDE.md or memory size
  change of 10% or more, or the dominant model or effort level shifting,
  from one session to the next in the same project. New: whenever you
  tick a change to track (not while just exploring "what if?"), the
  dashboard logs its estimate and later checks it against what actually
  happened once a matching real change and enough sessions have come in
  — a new "Did your estimates come true?" table on the Profiles tab
  (`GET /api/backtest`) and a read-only `claude-token-lens backtest` CLI
  command show a verdict (`as_estimated`, `smaller`, `larger`,
  `opposite`, or `too_little_data` while the window is still open) for
  each one, and once at least 3 of your own past estimates for the same
  kind of change have been judged, later "what if?" estimates of that
  kind are calibrated by how it actually turned out for you before
  (shown as fidelity `calibrated`) instead of guessed cold every time.
  See [`docs/backtest.md`](docs/backtest.md).

#### P7a: config coverage (COV-02/03/05/09/10, PROF-09)

- **The effective-settings view only ever showed the highest-priority
  layer's own `env`/permissions/hooks/plugins/MCP-server lists, hiding
  whatever a lower layer added underneath.** These now deep-merge across
  every settings layer with the rule each actually has: `env` and
  `enabledPlugins` per-name (highest layer wins per name, not per file),
  permission and hook counts additively (a lower layer's rule or hook
  still applies), and `enabledMcpjsonServers`/`disabledMcpjsonServers`
  as a union where a rejection at any layer wins. A stray `mcpServers`
  settings key was also being merged even though Claude Code never
  writes settings there (only `managedMcpServers`, managed-layer-only,
  is real) — that dead-code path is removed.
- **On Windows, the system managed-settings scan looked in
  `%ProgramData%\ClaudeCode`, and `~/.claude.json` was always read from
  the home directory.** Both were doc/code conflicts against Claude
  Code's own docs: the managed directory is `%ProgramFiles%\ClaudeCode`,
  and `managed-mcp.json` lives there too, not under the project's own
  `claude_root`; `.claude.json` now honours `CLAUDE_CONFIG_DIR` the same
  way `settings.json` does.
- **Five new recommendations for easy-to-miss environment-variable and
  deprecated-setting levers**: any `DISABLE_PROMPT_CACHING*` variant set
  (high severity — this quietly turns off prompt caching entirely);
  `ANTHROPIC_BASE_URL` set without `ENABLE_TOOL_SEARCH` on a config with
  several MCP servers or plugins; `CLAUDE_CODE_MAX_OUTPUT_TOKENS` set
  (shrinks the effective context window ahead of auto-compaction);
  `CLAUDE_CODE_SUBAGENT_MODEL` set on an archetype that spawns
  subagents (names the exact model-resolution order, and that it never
  reaches the built-in Explore/Plan subagents); and the deprecated
  `includeCoAuthoredBy` set without the `attribution` setting that
  replaces it. Each recommendation explains the trade-off and how to
  undo it in place, since an environment variable has no single
  settings file to write a fix into yet. `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`
  now also scales the simulated auto-compact window in the compaction
  simulation, so that simulation matches what a session with the
  override actually ran under instead of the unscaled default.
- **Config scanning now covers more of what a project or skill
  actually configures.** Agent directories are scanned recursively
  (a nested agent directory previously went uncounted); each CLAUDE.md
  file's own `@import` count is recorded; each skill's `model`/
  `effort`/`context`/`paths` frontmatter is summarised (never its body
  or description); and an installed plugin's own skill names and agent
  count are recorded (best-effort, its default `skills/`/`agents/`
  layout).
- **A session's own observed model or effort could silently diverge
  from what its settings snapshot says is configured** (a shell-profile
  env var or a `--settings`/`--model`/`--effort` CLI override the
  config hook can't see) with only the model half ever surfacing in the
  config-drift table. The report now also feeds each session's own
  dominant observed effort in alongside its dominant observed model, so
  a settings/effort mismatch shows up the same way a settings/model
  mismatch already did.
- **Money and advice presentation now follow your billing mode
  everywhere, and every card says where, what it costs, and how to undo
  it.** Under a subscription, the dashboard, the report and every
  recommendation phrase an amount as a share of your weekly usage limit
  (falling back to a labelled list-price equivalent without an accepted
  elasticity fit), instead of a bare dollar figure that means little
  when you're not billed per token; "about" no longer doubles into
  "about about" when a share and a caveat combine. `meta.units`
  (`{mode, share_per_usd, period_label, basis}`) carries the same facts
  to a JSON API consumer. Every recommendation and every Work habits
  playbook item now has a "where and who it affects", a trade-off and
  how to undo it — including the 23 workflow-only recommendation rules
  that propose no setting change (a purely informational one, like
  cache-read-dominance, keeps its explainer but drops the "ask Claude to
  do it" prompt it never had), and every one of the 21 playbook habits,
  with `allow_routine` stating its security trade-off and the
  `/permissions` command that undoes it. A habit already covered by a
  recommendation that fired this report (`effort_fit` by
  `effort-mismatch`, `short_reports` by `agent-report-size`,
  `quiet_output` by `tool-output-carry`) shows no saving of its own and
  links to the recommendation instead of reporting the same figure
  twice; `effort_fit` and `effort-mismatch` now also agree on the exact
  message-count and thinking-share gate that decides whether there's
  enough evidence to say something, instead of two independent numbers
  that could disagree; the dashboard's Quick actions tips pick at most
  one habit per theme and skip one already covered by a recommendation,
  instead of listing near-duplicates. A saving spread over "a week" no
  longer divides by a fraction of a week for a corpus under 7 days old
  (which used to multiply a single day's total by about 7x to fake a
  weekly rate) — under 7 days it's the raw total so far, and the Work
  habits digest is titled "Weekly pace (last N days)" rather than a
  fixed "This week" that implied a calendar week regardless of span.
  The playbook shows the 5 habits worth the most as cards up front; the
  rest collapse into a "more habits worth trying" section instead of a
  long, uncapped wall of cards.

### Fixed

- **Repeated reads were miscounted.** An edit counted as a read of the
  same file, and a read straight after an edit to it counted again too
  (870 -> 138 repeated reads on a real corpus once fixed). Agent report
  size also used the agent's own last output tokens instead of the
  result its parent actually received (or the task notification for a
  background agent), and `hook_system_message` lines — shown to you
  only, never sent to Claude — were counted as context.
- **Fast mode was ignored when pricing a quality marker's cost** (the
  `[tl: ...]`/`[result: ...]` tags and capture notes): every marker
  priced at a turn's standard rate even when that turn ran at 2x fast
  mode, long-context or data-residency rates. Marker cost is now priced
  at the turn that actually wrote it, using the same effective rate the
  context-carry figures already use.
- **A `.pyz` or wheel install could ship or run without the capture
  hook script.** `hooks/capture-hook.py` is now installed from package
  resources the same way the config-snapshot hook already was (fixing
  `install_hook` inside a `.pyz`), and was missing from package data
  entirely for a wheel build; a test now checks every hook file is
  actually shipped.
- **A live coaching hint could claim more cache life than a lifetime
  allows.** When a reply's own timestamp was stamped ahead of the
  clock (clock skew, a resumed session), the cache-freshness estimate
  behind the statusline's coaching line could read past a full TTL. It
  is now capped at the cache lifetime in effect.
- A skill run with a slash (`/tl-feedback`, `/tl-brief`) wasn't
  recorded in `commands_run`, because Claude Code writes it inside a
  `<command-message>` block first; it is now.
- **The dashboard could stop updating for good and still report
  healthy.** When the database was busy at the moment the background
  scanner started a scan (a second `serve` on the same database, say),
  the scanner thread died. The page kept serving the figures it had,
  frozen, while `/api/health` still said `ok`. A failed scan now fails
  only that scan, the scanner retries at the next poll, and a busy
  database is waited on for up to 30 seconds instead of 5.
- `/api/health`'s `status` is no longer always `ok`: it is `starting`
  during the first scan, `degraded` when the last scan failed and
  `stale` when the scanner has stopped or nothing has finished for ten
  minutes, with a plain-words `message` and the scan's progress
  (`scan`). The dashboard shows these in a banner on every tab and in
  its footer, with the command to restart it.
- A failed scan's error now keeps SQLite's own reason ("database is
  locked") rather than only "OperationalError".
- Two `serve`s on one database are refused. `serve` locks its database
  for as long as it runs; a second one exits naming the process that
  holds it and its address, and `serve --purge` refuses to delete a
  database a running `serve` has open.
- A port already in use is reported in a sentence instead of a
  traceback.
- The Profiles tab's "Or try it for one session" command was
  `claude --settings <config-dir>/profiles/<id>.settings.json`: a
  placeholder, pointing at a file that only `apply <id> --launch`
  writes. It is now `claude-token-lens apply <id> --launch`, which
  writes the file and prints the command with its real path, and the
  tab says when the profile's agent or environment changes can't come
  along for a one-session trial.
- `--since` and `--until` given a bare date (`--since 2026-09-01`), as
  the README documents, or a time with no offset, crashed comparing it
  with the transcripts' own times. Both are now read as UTC.
- **Claude Opus 5.5 had no rate card entry, so it silently priced at
  Opus 5's rate: a quarter too much on input and output and two and a
  half times too much on cache reads.** `pricing.toml` now carries its own row (input $4,
  output $20, cache writes $5/$8 for a five-minute/one-hour TTL, cache
  reads $0.20, all per million tokens, plus the documented 1.1x "us"
  data-residency uplift) instead of falling back to a prefix match on
  the shorter "claude-opus-5" id. Every other rate in the file was
  checked against the current pricing page while this was open; none
  needed a correction.
- **A reply priced against another, similar model's rate — because its
  own model id only prefix-matched, not because it had its own
  pricing.toml row — counted as "100% priced," so the report read as if
  every model had an exact price.** The Usage tab now gets a "Priced by
  closest match" table (model, priced as, replies, tokens) whenever this
  happens, the Data quality tab's counters and the `pricing-coverage`
  recommendation name it too, and `pricing-check --models` marks a
  closest-match resolution `(closest match, not this model's own rate)`.
  The coverage percentage itself is unchanged — a closest-match reply
  still counts as priced, since its cost isn't zero — only the wording
  now says so plainly instead of implying an exact price.
- **Fast mode (`usage.speed == "fast"`, currently 2x standard rates on
  Claude Opus 5.5, Opus 5 and Opus 4.8) was ignored and every reply was
  billed at its standard rate regardless.** `pricing.toml` now carries a
  `[models."<id>".fast]` multiplier for those three models, and a fast
  reply on a model with no such table still prices at standard (as
  before) but now says so: a new "Fast turns priced at standard rate"
  table on the Usage tab, and matching Data quality tab counters, name
  which models and how many replies. Recording each reply's own speed
  needed a new `Turn.speed` field, hence the `PARSER_VERSION` bump above.
- **The `opus`/`opus[1m]` aliases resolved to Claude Opus 5 instead of
  Opus 5.5**, so a session or agent config that named the family alias
  priced (and reported) as the older model. `pricing.toml` now carries
  those aliases on `claude-opus-5-5`; `claude-opus-5` resolves only by
  its own id.
- **Web search requests were tracked but never priced.** `[server_tools]
  .web_search_per_1000` was `0.0` and nothing read it. It's now $10 per
  1,000 requests (the documented rate), included in every turn's total
  as its own `server_tool_cost` line; `web_fetch` requests are still
  counted but have no documented per-request rate, so they remain
  unpriced.
- `xhigh` was a real effort level Claude Code accepts for
  `effortLevel`/an agent's `effort`, but the profile schema's closed
  vocabulary didn't include it, so a profile or observed session using
  it failed validation. `_EFFORT_LEVELS` now lists it between `high`
  and `max`.
- **Every "near the context limit" table (autocompaction, huge-context
  cache reads, the optimisation scorecard, the compaction-window sweep)
  assumed a flat 200,000-token window regardless of which model was
  actually running**, so a session on a natively 1M-token model (Fable
  5.1, Fable 5, Sonnet 5, Opus 4.7 and later) was flagged as constantly
  near its limit when it had 5x the room. `pricing.toml` now carries
  each model's real `context_window_tokens` (1,000,000 for the models
  above, 200,000 elsewhere by default), and `context_budget.py`,
  `recache.py`'s huge-context table, the scorecard's context-hygiene
  threshold and `compaction_sim.py`'s candidate-window sweep (widened
  past 500,000, up to the ~967,000-token point Claude Code itself
  compacts a 1M window at) all resolve it per model instead of assuming
  200,000. Expect fewer "near the limit" warnings on 1M-context models —
  that's the correct behavior, not a regression.
- **A what-if model change was labelled "Simulated" like a real replay,
  when it's actually a ceiling** on the saving: the same tokens
  repriced at the new model's rate, which can't capture that model
  needing more or fewer replies for the same work. It now gets its own
  `fidelity: "ceiling"` (`fidelity_text` explains the difference),
  matching the wording `/api/model-swap` already used for the same
  number.
- **The dashboard still said "Apply" in two places, and kept a latent
  fallback that would have shown an apply-directly command if the
  dry-run one were ever missing** — this tool never changes your Claude
  Code config itself (see "What the dashboard can change" in
  `SECURITY.md`). "Apply it to:" is now "Target file:" (it picks which
  settings file a profile's diff targets), "Apply tags" is now "Save
  tags" (it writes to this tool's own tag overrides, not Claude Code),
  and the dry-run command box no longer falls back to
  `data.apply_command`. A new static test fails if any button label or
  click-handler name says "Apply" again.
- **A shell command with one huge, unbroken run of characters (a base64
  heredoc body, say) could take minutes to parse.** Redacting a
  command's paths/URLs/user@host targets ran its regexes over the
  *whole* command before only ever keeping the first 40 characters of
  the result; one of those regexes backtracks quadratically over a long
  run with no `@` and no whitespace, so a 1 MB such command could take
  tens of minutes. The command is now capped to 512 characters (at a
  whitespace boundary, so a token straddling the cut is dropped whole
  rather than left as a raw, un-redacted fragment) before redaction
  runs at all — 1,000x the kept length, so this changes nothing for any
  realistic command, only the pathological ones.
- **The comment explaining why the metrics-capture hook that adds a
  large-tool-result note runs in the foreground was wrong.** It said
  Claude Code ignores what a background hook prints; the docs actually
  say an async hook's `additionalContext` does reach Claude, just on
  the next conversation turn — a full reply late for a note about the
  result Claude just saw, which is the real reason this stays
  synchronous. No behavior changed, only the comment (`capture-hook.py`,
  `capture_catalogue.py`), re-verified against the current hooks
  reference.
- **`GET /api/profiles/<id>` and `.../diff` could be made to read a file
  outside your profiles folder.** The route only ever sees one raw path
  segment (`..`/`/` would already fail to match), but a percent-encoded
  separator (`..%2F..%2Fetc%2Fpasswd`, `C:%5CWindows%5C...`) hid it from
  that check and was then decoded back into a real separator before the
  id reached the filesystem. `_load_profile_by_id` now checks the
  decoded id against the same shape a profile's own id must already
  satisfy to be saved (`^[a-z0-9-]{1,40}$`) before touching disk;
  neither route ever served anything outside `<config-dir>/profiles/`
  under a valid id, but this closes the traversal for good.
- **No `POST` route capped how large a request body could be.** This
  API has no authentication (local-only, by design), so any local
  process could force an arbitrarily large body to be read into memory
  and JSON-parsed on every `POST` route. Every route now caps the body
  at 64 KB — checked against `Content-Length` before a byte is read off
  the socket, `413 payload_too_large` otherwise. Every route's actual
  body (a profile, a tag, a feedback payload) is small hand-typed or
  hand-picked JSON, well under the cap. See "Body size limit (G5)" in
  `docs/api.md`.
- **A capture tag glued straight onto the reminder sentence could eat
  part of it, or leave part of the tag behind as if it were prose.**
  `capture_tags.py` now strips the exact reminder sentence out of a
  reply before it looks for the tag at the tail, whichever order Claude
  wrote them in; the catalogue's own reminder text is placed before the
  tag it explains, not after, so the two are never adjacent to begin
  with either.
- **A forged `[tl-fb: ...]` line — typed into a reply by hand, or
  copied from an earlier one — could count as real `/tl-feedback`
  answers.** It now counts only when the cycle's first turn actually
  ran `/tl-feedback`; work-habit calibration already only reads real
  answers and dashboard ratings, never the tag itself.
- **A captured tag key could be kept even when nothing asked Claude for
  it** — a model volunteering a field the current level never
  requested, or a stale key surviving a level change mid-session. A
  tag key is now kept only when the session's own note asked for it
  (there was at least one capture note) and the key names a metric that
  note actually requested. `reported_task` is now counted once per
  cycle instead of once per line that mentions it, so a reply that
  repeats its own tag doesn't inflate the count.
- **A skill could claim a capture note was meant for it by naming
  itself in a reply, and a name that didn't match Claude Code's own
  skill-id shape was recorded as if it were real.** `Turn.skills_invoked`
  now validates every name against the same pattern Claude Code itself
  uses for a skill id, and drops a skill whose `Skill` tool call
  actually errored — a skill can no longer self-authorise its own
  capture note by name alone.
- **Coverage undercounted or overcounted depending on what a cycle
  actually was.** Feedback cycles (`/tl-feedback` itself), interrupted
  cycles and cycles that hit `max_tokens` are no longer counted in the
  coverage denominator — none of them could ever carry a normal tag, so
  they only ever diluted the percentage. A cycle's tags are now merged
  key by key as later lines arrive instead of one line's tags replacing
  the whole set, `_big_output` is sized per tool call instead of once
  per turn, and `_WRAP`'s per-hook accounting carries the `:Tool` suffix
  it was missing.
- **The 14-day capture time-box (`capture on`'s default `--for`) only
  applied from the interactive `init` flow.** Non-interactive `init`,
  `capture on`/`capture level`, `config.set_capture` and
  `POST /api/capture` now all default a fresh switch-on to the same 14
  days unless `--capture-no-limit`/`--for`/`--until` says otherwise or
  an `until` is already set. `init --capture-for DAYS` sets a different
  default non-interactively; the help text for the existing flags now
  says what happens when none of them are given.
- **A downgrade to an older schema version, followed by an upgrade back,
  silently dropped your `/tl-feedback` answers and any capture tags the
  older schema doesn't know about.** `session_feedback` and every
  captured tag are now exported before a downgrade drops them and
  re-imported after an upgrade brings the columns back, verified with a
  v6-to-v5-to-v6 round trip. Each schema step's backup file is now
  timestamped (`.bak-<version>-<timestamp>`) so a second downgrade in
  the same run never overwrites the first one's backup.
- **`retention_days` and `exclude_projects` in `config.toml` were
  trusted without checking.** `retention_days` is now rejected outside
  1–36500; each `exclude_projects` pattern is compiled once at load
  (a bad regex fails fast, naming the pattern, instead of failing later
  inside the hook), and the hook itself now skips one bad pattern at a
  time instead of a bad pattern anywhere in the list silently
  disabling every exclusion. The `config.toml` writer now escapes
  control characters instead of writing them raw, and re-parses the
  file it's about to write before replacing the real one, so a bug in
  the writer can never leave `config.toml` unreadable.
- **The on-disk digest cache had no way to notice a vocabulary or
  scoring change that didn't come with a `PARSER_VERSION` bump.** Cache
  entries now live under a `cache/p<version>/` folder per
  `PARSER_VERSION`, and each entry also carries a fingerprint hash of
  the closed vocabularies, labels and prompt flags it was written
  against — a mismatch on either is a cache miss, same as before. Old
  version folders past 14 days old are pruned automatically.
- **A subagent running inside a workflow (`subagents/workflows/<run
  id>/agent-*.jsonl`, one directory deeper than an ordinary
  `subagents/agent-*.jsonl`) wasn't recognised as a subagent transcript
  by the capture hook**, so it never got the subagent capture note
  after a compaction. Subagent detection now also matches on the
  transcript's own filename shape, not only its parent directory name.

### P9a — Parser signals: task/structured-output events, a new cache
    signal, cost-state reconciliation, image/document sizing

`PARSER_VERSION` bumped to 18 (from 17): every transcript is re-parsed
once to pick up the new detection rules and fields below.

- **`task_status` and `structured_output` attachment lines now get their
  own event kinds** instead of falling into the generic attachment
  catch-all: `task_status` keeps only a closed status word
  (`running`/`completed`/`failed`/`stopped`/`cancelled`, else `other`)
  and task type (`local_bash`/`local_agent`, else `other`) — never the
  description, delta summary, output file path or shell command these
  lines also carry in the real corpus; `structured_output` keeps only
  the size of its payload, never the payload itself.
- **A model dropping its own prior extended-thinking blocks
  (`thinking_drop`, a prefix mismatch) now joins the `CACHE_SIGNAL`
  family** as a likely cache-bust, alongside thinking being stripped —
  only the closed drop reason and block/turn counts are kept.
- **`cost-state` lines (Claude Code's own running cost total for the
  session) are now read**, numbers only: `totalCostUSD`/
  `hasUnknownModelCost` land on the session's own metadata as a check on
  this tool's own pricing. `reconcile.claude_code_reported_costs(corpus,
  pricing)` pairs each session's self-reported total against this
  tool's own locally-priced total for the same session — the
  `cost-state` half of a later cost-gap metric; no Admin CSV, no network
  call, same as the rest of `reconcile.py`.
- **A tool_result's or a human prompt's own image/document content
  blocks are now sized** by Anthropic's documented Standard-tier
  image-token rule (`tokens = ceil(width/28) * ceil(height/28)`, itself
  capped at 1568 tokens) instead of silently counting as zero characters
  — PNG, GIF, JPEG and WebP headers are read just far enough to get
  their pixel dimensions, never decoded further. A block this parser
  can't size confidently (a document, an oversized or high-resolution-
  tier image, a malformed payload) is now counted as **unsized** rather
  than guessed at (`unsized_blocks`, by block type).
- **A line type no detection rule recognises at all is now counted
  separately** from one this parser knows about and deliberately ignores
  (`unknown_line_types`, apart from the existing `ignored_line_types`) —
  the type name itself is sanitised to a closed, safe token shape (or
  counted as `other`) before it ever reaches a diagnostic counter's key,
  since it comes straight off the wire. Both new counters live on a new
  `parser_notes` side channel next to `Diagnostics` (present only when
  non-empty) and are rendered alongside it by every renderer.

### P10b — Docs sweep, SECURITY.md corrections, Diagnostics privacy fix

- **`SECURITY.md` corrected against the current code.** It claimed
  `/tl-feedback`'s answer was checked for a question mark or negation
  before being kept — there is no free-text answer at all; all four
  questions are checkbox-only, and the section now says so and points at
  `POST /api/sessions/<id>/feedback`. It also claimed a wait signal
  records how long you waited before answering a prompt — only the
  categorical kind (permission/idle/question/agent/quota/other) is ever
  logged, never a duration, and the text is corrected to say so. The
  "what the dashboard can change" section now documents the feedback
  POST and P8's `POST /api/predictions/seen` alongside the existing
  tags/profiles/capture routes. The network section now documents that
  `update` is the one command that reaches the real internet — a pip
  install from the unpinned GitHub source, plus two loopback
  `GET /api/health` probes — instead of undercounting it as one function.
- **The README's tab table and glossary now match the dashboard**, with
  a regression test for each (`test_service_static.py`): the "What each
  tab answers" table was missing its Work habits and Capture rows (14 of
  16), and the glossary was missing the nine metrics-capture terms
  app.js's own `GLOSSARY` never got (Metrics capture, Capture level,
  Tag, Prompt cycle, Work habits, Feedback skill, Brief templates,
  Sampling, Time-box) — added to both README.md and `app.js` so they
  read the same. `docs/ui.md`'s "fifteen tabs" and "fourteen-tab"
  summaries are corrected to sixteen. `docs/api.md`'s quick-actions
  summary now lists all ten `quick_actions.CHECK_IDS` (it was missing
  "quality"), with its own sync test in `test_quick_actions.py`.
- **`Diagnostics.ignored_line_types` now sanitises its key the same way
  its sibling `unknown_line_types` already did.** Both are populated
  from a line's own top-level `type` — wire input, not a trusted enum —
  but only `unknown_line_types` ran it through `sanitize_line_type`
  first; `ignored_line_types` (a type the parser recognises and
  deliberately drops, including any `file-history-`/`artifact-`-prefixed
  or unclassified type) stored it verbatim. Neither the privacy suite's
  generic field walk nor `assert_privacy` opens a `dict`-typed field
  key-by-key, so this had no fixture catching it; both now do the same
  sanitize-or-`"other"` before the key is ever used.
- **New privacy fixtures** (`tests/test_privacy.py`) put a `[tl: ...]`
  tag, a `[tl-fb: ...]` tag, `/tl-feedback`'s AskUserQuestion answers,
  and a capture note through `parse_transcript` for the first time in
  this file, proving unknown keys, free-text "Other" answers, and a
  note's own surrounding text never reach a `Turn`/`Event` field; plus a
  fixture locking in the `ignored_line_types` fix above. Skill names
  already had a dedicated fixture (SEC-P3); not duplicated.
### P10a — Report performance: Habits built once, cache-carry costing off the linear path

No output change: every figure below comes out identical to before
(tests hand-compute the equivalence to 1e-12; a live smoke report's
JSON diffs at zero, generated_at aside, against a frozen transcript
snapshot run on the pre-change code).

- **`habits.collect()` walked the whole corpus twice per report** — once
  for the "Work habits" section, again for "Metrics capture"'s
  `capture_dependent_value`. `report.build_report` now runs it once and
  passes the result to both (`habits.section_from`, and a new optional
  `habits.capture_section(..., h=...)`), falling back to its own
  `collect()` only on the rare config that resolves a non-default
  effort-mismatch share threshold, so the two sections can't disagree
  on it.
- **Context-carry costing (`carry.py:_extract_results`) re-summed every
  later turn's cache rate for every carried tool result — O(turns ×
  tool results) per transcript**, the report's single largest post-parse
  cost. It now precomputes each turn's rate once, prefix-sums them, and
  reads off an O(log turns) range sum per tool result instead (`bisect`
  over turn index, since indices can skip). The now-unused per-pair
  `_carry_cost_for_turn` is removed.
- **`ttl.cache_economy`'s `_cache_tokens_at_input_rate` priced every
  cached turn twice** (once for real, once more with its cache emptied,
  via `dataclasses.replace`, just to isolate the input-rate cost) —
  `pricing.effective_rates` already folds in fast-mode, long-context and
  geo the same way, so it's called once and multiplied directly.
- **`habits._CarryCost` re-resolved the same turn's effective rates
  twice**, once each for `.read()` and `.write()`. A new
  `_Rates.read_write()` resolves once and returns both.
- Measured on the smoke corpus (`--all-projects --since <30d> --jobs
  4`), the post-parse stage (everything after transcript parsing) drops
  from a ~25.0s to a ~16.0s median of 3 runs, about 36% -- short of
  halving it. The remaining top hot spots are the same shape of problem
  in `context_files.py`'s `_Carry` (linear `index_at`, an uncached
  `resolve_model` per turn) and `compaction_sim.py` (an uncached
  `lookup(turn.model)` per replay window; its own heavy
  `dataclasses.replace` use doesn't share carry.py's fix, since it
  changes `ctx` itself, which can cross the long-context threshold) —
  both out of this phase's file scope, left for a follow-up.

## [0.5.2] - 2026-09-23

### Fixed

- **Short windows counted sessions with no replies in them.** A session
  counted in a window when its transcript file last changed there, and
  Claude Code (the desktop app especially) appends titles and other
  notes to old transcripts. "Last 24 hours" could show a week-old
  session's full cost with nothing run that day. A session now counts
  when its last reply falls in the window, in the dashboard, `report`
  and every other command (`--window-by last-reply`, the new default;
  `mtime` keeps the old rule).
- The dashboard's Sessions list and the Usage tab's conversation
  summaries ignored the window picker and always listed everything; both
  now show only what falls in the window. `GET /api/sessions` and
  `GET /api/compactions` accept the same window parameters as the
  report.

## [0.5.1] - 2026-09-23

After updating, the first dashboard start re-reads every transcript
(a few minutes), and output and thinking tokens, and the costs built on
them, come out noticeably higher: they were undercounted before.

### Fixed

- **Output and thinking tokens were undercounted by more than half.**
  Claude Code writes a streamed reply as one line per content block, and
  only the last line carries the full output count and the thinking
  count; the parser took the first line's. Cached digests are re-parsed
  (`PARSER_VERSION` 12).
- **The auto-compact window advice overstated savings several times
  over.** The simulation dropped a session's context to about 15% after
  each summary, but the system prompt, tools, CLAUDE.md and skills
  listing (about 80,000 tokens on a typical corpus) stay. A simulated
  summary now leaves the session's own starting context plus a summary
  of your usual size, fires the same distance below the window as your
  real ones (a 300,000 window fires near 267,000), and is charged for
  the summary request and the re-cached reply after it. The rule's
  "at most 2 summaries a session" limit now also applies to the
  compaction profile goal. When `autoCompactWindow` is already the best
  point, the compaction check now says so, and says that its last
  column compares each point with your sessions as they ran.
- **The model and quality checks gave opposite advice for the same
  agent.** A cheaper model the quality section found an agent did worse
  on is no longer suggested for it by the models check, the report's
  model-tier card or the models profile goal; each says it was left
  out. A setup clearly worse on some signals and clearly better on
  others is now "Mixed" rather than "Worse", with no switch offered, and
  going back to a larger model says it costs more rather than warning
  about extra replies.
- **The skills review offered to hide skills Claude Code's own tools
  need.** The Artifact tool tells Claude to load `artifact-design`,
  `artifact-capabilities`, `artifact-diagramming` and `workshop`, and
  the Workflow tool `workflow-authoring`; hidden, those tools'
  instructions break. They are left out of the hide-all fix, marked
  "Needed by a Claude Code tool", and offered `name-only` instead.
- **The skills review offered to hide skills you had deleted.** A skill
  with no file left on disk was labelled "Built into Claude Code" and
  offered for hiding, though hiding it saves nothing once it's gone. One
  that no listing has named for 14 days before the newest one is now
  "Removed", marked "No longer listed", and gets no fixes.
- **The CLAUDE.md review called sections agent-only when they weren't.**
  An agent with a one-word name (`claude`, `Explore`, `Plan`) matched
  every "Claude Code", `.claude/` path or "plan", so whole files were
  offered for moving into that agent. A one-word name now counts only
  in backticks, before "agent" or "subagent", or as a `subagent_type`.
- **The shell output cap quoted every shell result's cost as its
  saving.** The tool-output check now prices each cap on the results it
  would cut (the new `carry_output_cap_savings` table) and offers it
  only when it saves at least 10% of what those results cost; otherwise
  it says why not. The `tool-output-carry` recommendation now quotes
  that tool's own saving (`carry_by_tool`'s new `saving_if_capped_usd`)
  rather than every tool's.
- **Failing tests and hook blocks were counted as wasted tool errors.**
  The parser now records why each tool call failed (the kind only, never
  the text). A command that ran and reported failure, such as a failing
  test or build, is no longer wasted (it is counted in `waste_summary`'s
  new `failed_command_turns`); a hook or Claude Code guard block is its
  own `blocked` cause with its own lever; a denial counts as
  `tool-denial`. `tool-error` now means a call that couldn't run as
  written.
- **Advice about another project's agents.** Recommendations, `check`,
  and the dashboard's quick actions and profile goals read agent
  settings from the newest config snapshot only, which records just the
  agents of the project it was taken in. Another project's agents showed
  as "not set", custom agents could be called built into Claude Code,
  and fixes pointed at `~/.claude/agents` with `--scope user` instead of
  the project's own agent file. Agents now come from every project's
  latest snapshot.
- **Changes already made were still recommended.** Advice is measured
  over the whole period, so a change made part-way through it kept being
  offered at its full saving. A change the current config already makes
  (a model alias such as `sonnet` matches `claude-sonnet-5`) is now left
  out, and a card with nothing left to change is dropped. Quick actions
  and profile goals also read an agent's cache lifetime and max turns
  under the wrong names, so they always showed as "not set".
- The skills review no longer offers to hide a skill that
  `~/.claude/settings.json` already hides, or one whose plugin is turned
  off; offering `user-invocable-only` for a skill set to `off` would have
  shown it again. Such skills are marked "Already hidden".
- The dashboard asked for each report once per tab: opening several tabs
  built the same slow report several times over. Requests for a report
  that is already being built now wait for that build.
- **Dashboard layout and wording.** Overview's two "start here" links ran
  together into one line ("…and howOr check…"). Long prompts on Context
  files and the sessions table widened the whole page past the window;
  code blocks now wrap and wide tables scroll in their own box. Money
  reads `1,234.56 USD` as in the report; times read `2026-09-23 13:26 UTC`
  rather than raw ISO; session ids show 8 characters with the full id on
  hover; skill descriptions in Quick actions are shortened; setting values
  and counts get thousands separators. The Usage tab's "Recent
  conversation summaries" showed the oldest 50, not the newest, and
  formatted its transcript number as a quantity ("1,038"). Quick
  actions' evidence buttons now say what they show and hide.

## [0.5.0] - 2026-09-23

### Added

- **Sessions from WSL.** `init` finds Claude Code sessions inside WSL
  distros (`\\wsl.localhost\<distro>\home\<user>\.claude\projects`) and asks
  whether to include them. They go in `config.toml`'s new
  `extra_projects_roots`, which the dashboard, the logon service and
  every command read alongside this computer's own folder.
  `--projects-root` can now be given more than once.
- The Sessions tab shows a **Where** column ("This computer" or
  "WSL: Ubuntu") when any session ran outside this computer, and the
  session detail says where it ran. `GET /api/sessions` and
  `GET /api/session/<id>` carry it as `source`, never the path.
- **`update`**: installs the newest version and restarts the dashboard
  on it in one command (`python -m claude_token_lens update`).
- `install-service` says when the dashboard answering on the port is a
  different version from the one just installed: an older copy is still
  holding the port.

### Changed

- The dashboard no longer marks a folder's transcripts missing while
  that folder is out of reach (a WSL distro that was shut down).

## [0.4.1] - 2026-09-23

### Changed

- **Updating is two commands.** On Windows, `install-service` now stops
  a dashboard the logon task already started, re-registers the task for
  the Python you ran it with, and starts it straight away (a first
  install no longer waits for the next logon). On Linux it also restarts
  the service. So after `pip install --force-reinstall`, running
  `python -m claude_token_lens install-service` switches the dashboard
  to the new version.
- The dashboard's footer and `GET /api/health` (`version`) show the
  running version, so a dashboard still on an old copy is easy to spot.
- `init` asks its questions in plain words ("How do you pay for Claude
  Code?"), and the billing question accepts `pro`, `max`, `team`,
  `enterprise` and `plan` as `subscription`.
- The README's quick start covers checking Python, installing,
  updating, uninstalling and what to do when `claude-token-lens` isn't
  found.

### Fixed

- The hook health check expands `%VAR%` on every platform, not only on
  Windows, so its tests pass on Linux CI.

## [0.4.0] - 2026-09-23

### Added

- **Monthly reports from the dashboard.** `serve --monthly-report DIR`
  now writes last month's report into DIR when it's missing, checking at
  startup and every hour; a failure logs one line and is retried later.
- **Sessions record their profile.** Each session carries the profile
  active when it started (from the config hook, `apply` and undone
  applies), so `compare --a profile:<id>` selects real sessions and the
  session list shows the profile.
- **Usage-limit headroom.** For subscription users with usage-limit
  readings, the new `elasticity` section (Usage tab) shows how many
  tokens a full limit holds and how much of the weekly limit the last
  day used; `[thresholds.elasticity]` in `config.toml` now takes effect.
- Replies from a model with no price are listed under the Usage tab's
  advanced detail, and the "Some usage has no price" recommendation
  names those models.
- **Quality signals: is the work going well?** A cheaper model or lower
  effort only saves money if the work still gets done. The new
  `quality` section (Agents tab, `quality` command) counts, per agent
  type and per model and effort: agent runs that didn't finish
  (reported failure, stopped, never replied, or cut off, and those that
  most likely ran out of turns), failed tool calls and shell commands,
  denials, replies you stopped, your corrections (a yes/no from a fixed
  phrase list, text never kept), files edited again and replies cut off
  at the output limit. Setups are compared with the one each agent used
  most, with a per-run z-test and a Holm correction; "Your changes and
  what they did" compares them before and after each change; and the
  Quick actions check "Is any agent struggling?" turns both into fixes
  (`quality.py`). Agent outcomes are read from task notifications,
  including queued ones, and a workflow agent's from its run file. The
  parser keeps stop reasons, tool calls and errors by tool, and edit
  targets as salted hashes (`PARSER_VERSION` 10: sessions are re-read
  once).
- **Quick actions tab and `check` command.** Ten questions: nine, one per
  way of saving: the right model per agent, effort, the summary point
  (`autoCompactWindow`), cache lifetime, unused tools and MCP servers on
  agents, unused skills, CLAUDE.md size, large tool output
  (`BASH_MAX_OUTPUT_LENGTH`, `MAX_MCP_OUTPUT_TOKENS`) and habits, and
  whether any agent is struggling. Each
  always answers, including "nothing to do", with the evidence table,
  fixes (a prompt, and a `--dry-run` command for a plain setting) and
  tips (`quick_actions.py`, `GET /api/quick-actions`,
  `GET /api/quick-actions/<id>`).
- **Context files tab and `review claude-md|skills`.** Each CLAUDE.md
  file's size by section, how often it was sent and to which agents, its
  estimated cost, agent-only sections, duplicates and stale references,
  each with a fix prompt. Each skill's description, source, how often it
  was listed and used, and one fix that hides every unused skill
  (`skillOverrides`). Transcripts now keep per-file and per-skill sizes
  (never text); file text and skill descriptions are read on request and
  never stored (`claude_md_review.py`, `skills_review.py`,
  `context_files.py`, `GET /api/claude-md`, `GET /api/claude-md/<id>`,
  `GET /api/skills`).
- **Create a profile from a goal.** Pick a goal (spend less on
  subagents, cheaper models, cheaper cache, shorter conversations, less
  thinking, from my recommendations, from my current settings), tick the
  changes your data supports, see the what-if estimate update, name it
  and save it (`profiles/goals.py`, `whatif.py`,
  `GET /api/profile-goals`, `POST /api/whatif`). Profile details show
  their estimated effect.
- **Your changes and what they did** on the Profiles tab: each `apply`,
  undo or settings change, with sessions before against sessions after
  on the measures that change should move (`change_points.py`,
  `impact.py`, `GET /api/impact`).
- **Window picker in the header**, for every tab: the last hour, today,
  the last 24 hours, 7/30/90 days, all time, or since my last change
  (`?window=1h|today|24h|change|all`, or `?window_days=N`, on every
  report-backed route).
- **What this tool installed, and what to expect** on the Data quality
  tab (`GET /api/setup`) and in `changes`: it never uses your Claude
  tokens, the hook and statusline add none, and what each piece does
  and how to undo it. New `uninstall` command removes the hook,
  statusline and service, and with `--revert-changes`/`--delete-data`
  undoes applied changes and deletes the data folder.
- `init` connects the snapshot hook (and a statusline when you have
  none) after showing the exact `settings.json` diff and asking;
  `--connect` skips the question. `settings.json` is backed up first.
- `skillOverrides` joins the profile settings allowlist;
  `enabledPlugins` is now a name-to-on/off map merged into the existing
  object.
- Glossary entries for Window, Change point, Quick action, What-if
  estimate, CLAUDE.md and Skill.
- **Start here** on the Overview tab: the three most important
  recommendations for the selected window, with why and the estimated
  saving, and any scorecard area rated poor or worse.
- **"Why was this session expensive?"** in each session's detail:
  template sentences and a cost split from
  `GET /api/session/<id>/explain` (`service/explain.py`,
  `Store.session_parts`, `Store.median_session_cost`). No LLM.
- **Profiles tab redesigned.** Cards show "Suggested for you",
  built-in or yours, and which settings a profile changes. The detail
  table reads Setting / Now / After / Set in, marks settings locked by
  policy or already set, and ends with "Ask Claude to do it" (a
  prompt), "Or run this command" (`apply <id> --dry-run`) and "Or try
  it for one session". A form editor built from the schema ("Start
  from", settings, per-agent fields, JSON as an escape hatch) and
  "Save my current settings as a profile" write only to this tool's own
  profile store.
- **Glossary tab**, worded the same as the README's glossary (the two
  are kept in step by hand).
- New read-only routes `GET /api/profile-schema` (every allowlisted
  key with label, type, allowed values, description and trade-off) and
  `GET /api/profiles/<id>`, and `POST /api/profiles/from-current`,
  which saves the latest snapshot's allowlisted, non-managed settings as
  a user profile (`409` when no snapshot records config).
- `GET /api/profiles/<id>/diff` rows carry `setting`, `agent`, `label`,
  `description` and `where`; the response adds `dry_run_command` and a
  `prompt` (`fixes.profile_prompt`) that names each file, setting and
  value.
- **Cache misses Claude Code measured**: a `measured_miss_causes`
  table on the Cache tab lists the main session's misses by the cause
  Claude Code reported through the statusline, next to the
  transcript-inferred causes.
- `apply` and `apply --dry-run` explain each change (what the setting
  controls, now and after, where, the trade-off, how to undo it).
  Backup manifests record each key's old and new value and the SHA-256
  of what was written; `apply --revert` refuses, restoring nothing,
  when a file changed since, unless `--ignore-changes`.
- **Readable dashboard and reports** (readability stage 1). Every table
  and section can carry plain-English help (`Column.help`,
  `Table.help`/`value_labels`/`dashboard`, `Section.intro`/`help`, all
  defaulted in `model.py`), written by the new `helptext.py` after
  `recommend()` runs, so table names, column keys and row values are
  unchanged. The dashboard shows an intro, a "How to read this" block,
  a `?` per column and readable row labels (raw key on hover), and
  folds rarely needed tables into "Advanced detail". `report --explain`
  adds the same help to the Markdown report; the HTML report has it in
  collapsed blocks. The Agents, Subagent startup, Workstyle and
  Workflows sections are covered so far; `tests/test_help_coverage.py`
  keeps a shrinking list of the rest. House style:
  [`docs/writing-help.md`](docs/writing-help.md).
- **Subagent startup section** (`agent_startup`, `context_budget.py`):
  what each agent type is given before its first turn (task prompt,
  CLAUDE.md and memory, skills list, tool lists, hook output, other
  notes, and the system prompt and tool definitions when recorded, with
  the rest shown as "Not recorded"), what it was given but never used,
  and what most agent types receive alike. Forks are counted but kept
  out of the averages. `agent_startup_breakdown` also carries each
  agent's cache-write list price (`write_price`).
- **Per-part subagent recommendations** replace the single generic
  `spawn-cost` advice wherever the startup breakdown has data:
  `spawn-claude-md` (`omitClaudeMd`; never for Explore or Plan),
  `spawn-unused-skills` (add `Skill` to `disallowedTools`, marked
  unconfirmed), `spawn-unused-mcp` (`mcpServers`),
  `spawn-read-only-tools` (`tools`), `spawn-task-prompt` and
  `spawn-shared-claude-md`. Built-in agent types get a prompt to create
  a same-named override instead of a command; workflow subagents and
  forks get no override advice. `spawn-cost` remains for agent types
  without startup data.
- **Recommendations say what to change and how.** `Recommendation`
  gains `changes` (`SettingChange`: target, key, agent, value, current
  value, suggestion, notes), `estimated_saving`, `saving_basis`, `why`
  and `fixes`. The new `fixes.py` turns each change into a six-part
  explainer (what the setting controls, now and after, where and who it
  affects, expected effect, trade-off, how to undo it), an
  `apply --set ... --dry-run` command when the value is known, and a
  self-contained prompt for Claude. When a change needs work first
  (`omitClaudeMd`: move the CLAUDE.md rules the agent needs into its own
  agent file), the prompt asks Claude to do that before setting the key,
  and the command carries a `command_warning`. Shown on the dashboard cards, in the
  Markdown report and in the HTML report.
- **Amounts follow the billing mode** (`units.py`): dollars at list
  price for API billing; for Pro and Max plans, the share of the weekly
  usage limit (fitted by `elasticity.py` from your statusline readings)
  with the list-price equivalent next to it, or the list-price
  equivalent plus a hint when there are too few readings.
- **`apply --set KEY=VALUE [--agent NAME]`**: change one allowlisted
  setting or agent frontmatter field without a profile, with the same
  validation, backups, `--dry-run` and `--revert` as a profile apply.
  It never writes the active-profile marker. `mcpServers` joins the
  agent frontmatter allowlist.
- **`GET /api/diagnostics`**: the parse-quality counters as a labelled
  table, led by a check of the config snapshot hook.
- **Snapshot hook health** (`hook_health.py`): finds the SessionStart
  hook that runs `snapshot-config.py`, spots a Windows path broken by a
  single backslash in JSON (`\t` read as a tab), and reports how long
  ago the last snapshot was taken. `init` reports it and offers to fix
  the command; `init --repair-hook` fixes it without asking. Only that
  command string changes, after a `settings.json.bak-<timestamp>` backup.
- **`billing = "auto"`**, the new default: subscription once the usage
  log holds a usage-limit reading (only Pro and Max plans report them),
  API otherwise. `ReportMeta.billing_source` says why; the report header
  and the dashboard's Overview show it.
- **Host allowlist** for `serve` (DNS rebinding): every request whose
  `Host` header isn't a loopback name, the bind address or an
  `--allowed-host NAME` gets `403`.
- **`elasticity.py`** (v4-elasticity): fits how many percentage points
  of a `five_hour`/`seven_day`/`spend_limit` usage window one million
  tokens (or one list-price dollar) is actually worth, from consecutive
  `tools/log_usage.py` samples paired with the token volume this
  machine's own transcripts consumed between them (weighted
  least-squares through the origin, refusing to report a figure below
  8 pairs or an R² of 0.5). New `elasticity` report section
  (`elasticity_fit`, `elasticity_budget`, `elasticity_recent_burn`)
  derives the million-new-tokens-per-window budget and the last-24h
  burn share of the weekly window; new `express_in_window` function
  converts a USD saving into "≈ x% of your weekly window" for
  subscription-billed accounts; new `window-budget` recommendation
  rule (`elasticity.RULES`) states the derived budget/burn and points
  at the biggest other lever already on the report by id. See
  [`docs/elasticity.md`](docs/elasticity.md).
- **v4-carry-cost context carry cost per tool** (`carry.py`, work
  package v4-carry-cost): a tool result doesn't cost tokens only on the
  turn it's produced — it rides along in the cached prefix, re-read or
  re-written on every later turn until a compaction drops it. New
  `carry` report section (`carry_by_tool`, `carry_by_agent_type`,
  `carry_top_results`, `carry_truncation_savings`) prices that ongoing
  cost per tool and per agent type, and reports the exact saving from
  capping large results at 2,000/8,000 tokens. New `tool-output-carry`
  recommendation rule (`carry.RULES`) fires when a tool's carry cost
  exceeds a configurable share of the corpus's cache volume, naming a
  concrete truncation lever and citing the projected saving. See
  [`docs/carry.md`](docs/carry.md).
- **`model_swap.py`** (v4-model-swap): a model-swap counterfactual per
  agent type — reprices every already-observed priced turn at every
  model `pricing.toml` carries (same tokens, same observed 5m/1h
  cache-write split) and reports the ceiling saving from moving one
  tier down (fable -> opus -> sonnet -> haiku), plus a `model-tier`
  recommendation naming the exact `settings.json`/`<agent>.md` lever.
  Every saving is stated as a price ceiling at today's usage shape,
  never a prediction. See [`docs/model-swap.md`](docs/model-swap.md).
- **`compaction_sim.py`**: the `autoCompactWindow` sweep — replays every
  top-level transcript's priced turns under each of a fixed set of
  candidate auto-compaction windows (100k/150k/200k/250k/300k/400k/500k/
  none), estimating total cost under each against this corpus's own
  observed compression ratio and post-compaction rediscovery cost, with
  a fidelity self-check against the session's actual configured window
  and a `compaction-window` recommendation naming the cheapest one and
  its projected saving. See [`docs/compaction-sim.md`](docs/compaction-sim.md)
  and the `compaction_sim` entry in
  [`docs/sections-reference.md`](docs/sections-reference.md). Wired into
  `report.py`/`cli.py`/`recommend.py`/the service and UI in the v4
  wiring round below.
- **v4-wasted-turns spend tracking** (`waste.py`, work package
  v4-wasted-turns): prices every turn whose output the user never
  actually benefited from -- a failed tool call, a turn the user
  interrupted, one stopped by a tool denial, or every turn in a
  subagent transcript the harness killed before it could report back
  -- and attributes each to a cause with a lever, so the report can say
  not just what was spent but what's recoverable and how. New `waste`
  report section (`waste_summary`, `waste_by_cause`,
  `waste_by_agent_type`, `waste_top_sessions`), `compute_waste`/
  `WasteStats`/`build_section` entry points, `WasteThresholds`
  (`share_pct`, `min_sessions`, `min_turns`), and a new `wasted-turns`
  recommendation rule (`waste.RULES`) that fires when the wasted-cost
  share of total priced spend clears `WasteThresholds.share_pct`
  (default 10%), naming the dominant cause and its lever.
  `api-error-retry` (a turn preceded by a 529/retry gap)
  is counted alongside the other causes but never priced -- the harness
  already retried it automatically. Turns following a usage-cap pause
  (`Turn.gap_cause == "limit"`) are excluded outright, since
  `limits.py` already owns that attribution. Wired into
  `report.py`/`cli.py`/`recommend.py`/the service and UI in the v4
  wiring round below.
- **v4 wiring round**: `carry`, `compaction_sim`, `model_swap` and
  `waste` are now first-class report sections (`_SECTION_ORDER`:
  ...`limits`, `carry`, `compaction_sim`, `model_swap`, `waste`,
  `compactions`...), built by `report.build_report` from each module's
  own `from_config`/`build_section`, with `compaction_sim`'s
  `snapshot_windows` sourced the same way `context_budget.py` maps a
  session to its project's latest `autoCompactWindow` snapshot. Their
  four recommendation rules (`tool-output-carry`, `compaction-window`,
  `model-tier`, `wasted-turns`) are registered in `recommend.recommend()`
  alongside the existing baseline rules. New CLI subcommands `carry`,
  `compaction-sim`, `model-swap`, `waste` (`_REPORT_LIKE_SECTIONS`, same
  pattern as `limits`). New service routes `/api/carry`,
  `/api/compaction-sim`, `/api/model-swap`, `/api/waste` (see
  [`docs/api.md`](docs/api.md)). New UI "Savings" tab holding all four
  sections' tables (see [`docs/ui.md`](docs/ui.md)).
  `docs/sections-reference.md`'s section order now matches
  `report.py`'s assembly order exactly.
- **`savers.py`** (v4-saver-roi): third-party token-saver tool ROI —
  detects candidate "saver" MCP servers/plugins/skills via an explicit
  `config.toml` `[savers]` allowlist plus auto-detection (a
  case-insensitive name regex over MCP server names, config-snapshot
  `mcp_servers`/`enabled_plugins`, and `attribution_skill`), then reports
  each candidate's own overhead, its effect on cost/tokens/re-cache/
  compactions/turns in sessions where it was present versus absent
  (stratified by purpose/mode, gated on a 5-session-per-arm minimum),
  a search-substitution comparison against native `Grep`/`Glob`/`Read`/
  shell search calls (reusing `carry.compute_carry`'s per-turn pricing),
  and a net-saving-per-session verdict labelled "observed, not
  controlled". New `saver-tool-roi` recommendation rule (`savers.RULES`)
  recommends keeping or disabling a saver based on that net saving. See
  [`docs/savers.md`](docs/savers.md).

### Changed

- **README restructured**: a plain description, a three-step quick
  start, what each tab answers, acting on a recommendation, and a
  glossary, then the reference sections (renumbered). The token totals,
  how caching works, cache rebuild definitions and the TTL simulation
  assumptions moved to [`docs/concepts.md`](docs/concepts.md); links
  across the docs are updated.
- `render_patch_set` renders a recommendation's `changes` with current
  and proposed values instead of guessing from the lever text.
- The dashboard subtitle says what the tool is for; the Cache tab's
  statusline table is titled "Cache health per session, from your
  statusline". CLI wording changed; JSON and CSV keys are unchanged.
- **Recommendations in plain words.** Severity reads "Do this",
  "Worth considering" or "For your information"; cards say who a
  change is for and "What to do", fold multiple fixes, and put the
  evidence under "Show the numbers behind this", citing the table
  title and row label. Markdown and HTML reports use the same wording.
- **Totals for the window grouped and labelled with units**
  (Activity, Tokens, Cost, Context size) via display-only
  `Table.row_groups`/`row_kinds`; numbers in mixed metric tables get
  thousands separators.
- **Scorecard tiles explain themselves**: what each area measures,
  which way is better, and what the next rating needs, in words
  rather than threshold keys.
- CLI and dashboard wording: section, table and column titles are
  plainer; the Markdown and HTML reports show readable row labels. JSON
  and CSV output keep the raw keys and values.
- Dashboard: every report section is mapped to a tab (`sessions`,
  `context_budget`, `baseline_comparison`, `phases`, `agent_startup`,
  `scorecard`); the Config tab renders its tables once; the Cache tab
  explains the limit-expiry cause; tabs are renamed "Cache lifetime
  (TTL)" and "Data quality"; each tab has one heading and an intro.
- **Recommendations are written in plain words** by a new pass,
  `advice.py`, run at the end of `recommend()`: each card has a plain
  title, a `why` sentence, an action, and (where a setting is involved)
  `changes` with the current value, so it comes with an explainer, a
  command and a prompt. Cards that disagreed are consolidated:
  `compaction-window` replaces `compaction-churn` and takes the setting
  from `long-context-share`, and is dropped when `autoCompactWindow` is
  already at or below its floor; the per-agent `model-tier` cards merge
  into one with a change per agent type (as the `haiku`/`sonnet`/`opus`
  alias), skipping workflow subagents and forks. `spawn-cost` no longer
  fires for agents no file can change. Cards are ordered by severity,
  then by estimated saving (`Recommendation.saving_usd`). A setting
  locked by managed policy gets a prompt that drafts a request to your
  administrator instead of a command. The rules' ids, evidence and JSON
  keys are unchanged.
- `data-quality` fires on cache-write mismatches only when they are a
  real share of replies (the same bar as unreadable lines), not on a
  single odd reply.
- **`compaction_sim.py`'s `compaction-window` rule, conservatively
  rewritten**: the window sweep only ever charged a flat rediscovery
  allowance, so a smaller window always looked cheaper in isolation —
  against a real corpus this produced an incredible-looking "100k
  window saves 68%" recommendation. The rule now recommends a *floor*
  ("set autoCompactWindow to at least W"), not a single "best" point: the
  smallest candidate window whose simulated compactions-per-session stay
  at or below 2 and whose saving still clears threshold after subtracting
  an extra, more conservative rediscovery estimate derived from the
  corpus's own measured `topology_redundant_reads` (or, when that figure
  isn't available, simply doubling the flat allowance, and saying so in
  the note). The action text always says "modelled, not observed" and
  cites `compaction_sim_fidelity` when it has rows. This is the one
  change made to the module's own arithmetic during the v4 wiring round
  (every other module's arithmetic was left untouched).
- **`report.build_report`** gained an optional `config_dir` keyword,
  used only to tell `waste.WasteStats` where to read/write its salted
  session-id-hashing salt file. A caller that omits it (every pre-v4
  test, `baseline.py`, `team.py`) now falls back to an OS-temp-directory
  default rather than `waste.py`'s own default of the real
  `~/.claude/token-lens` — `build_report` must never touch a real user
  config directory unless a caller explicitly hands it one.
  `service/api.py` passes its own real `config_dir` so the running
  service's salt lives alongside its other state as intended.
- **`PARSER_VERSION` 5 -> 6** (`__init__.py`, v4-wasted-turns): two new
  additive `Turn` fields, `tool_error_count`/`tool_error_chars`,
  derived from each turn's own tool_result blocks that carry
  `is_error: true` (length only, never the error text itself -- see
  `model.py`'s module docstring). No pre-batch digest cache entry ever
  computed these, so any cache built under `PARSER_VERSION` 5 or
  earlier is invalidated and transcripts are reparsed on next use.

### Fixed

- `reconcile` no longer counts cache-write tokens twice when an Admin
  export has both the total column and the 5-minute/1-hour split, says
  correctly that local usage is grouped by UTC day, and groups an Admin
  date column holding full timestamps by UTC day too.
- Every command finds Claude Code's `settings.json` the same way:
  `--claude-root`, else `CLAUDE_CONFIG_DIR`, else `~/.claude`. `init`,
  `--repair-hook`, `changes`, `uninstall` and the dashboard used the
  folder above `--config-dir` and could change the wrong file. With a
  non-default `--config-dir`, the hook and statusline commands now carry
  it, and `--repair-hook` keeps those arguments.
- Running `init` again no longer restarts the capture window.
- On Windows, `uninstall-service` stops the running dashboard before
  removing the scheduled task, and every platform says which steps it did.
- The token-saver analysis stays out of the report; its docs now say why.
- The SessionStart hook and statusline commands name this Python and
  the script by full path. `py -3` fails where the launcher isn't on the
  `PATH`, and Git Bash doesn't expand `%USERPROFILE%`; either stopped the
  hook without any visible error. The hook health check now reports
  both, and `init --repair-hook` fixes them: it writes out a `%VAR%`
  and keeps your own interpreter when it's found, and otherwise names
  the base Python rather than a virtual environment's, since the hook
  needs only the standard library.
- The Data quality tab says when the statusline isn't logging (it runs
  only in Claude Code in a terminal).
- "All time" on the dashboard now means all time on every tab; it used
  to fall back to 30 days on report-backed tabs.
- A service from an older build no longer re-reads transcripts that a
  newer build already read. Two services sharing one store used to undo
  each other's work on every pass.
- The skills and CLAUDE.md checks say "not enough data" when no session
  in the window recorded those files, instead of "nothing to do".
- **An apply stamp was read as the latest config snapshot.** `apply`
  writes a small `{ts, schema_version, profile_id}` record into the
  snapshot directory; every reader took it for a snapshot with no
  settings, so the scorecard counted every setting as changed, profile
  diffs showed empty "Now" values, "save my current settings" saved
  nothing, and hook health dated the last snapshot from the apply. New
  `snapshots.records_config` skips records that hold no config.
- **The dashboard never saw the statusline usage log**: it now passes
  the log into its report, scoped to the window's sessions like the CLI
  (`statusline.scoped_usage_log_rows`).
- **Stored snapshots lost their config.** The watcher stored snapshots
  flattened, so the service's rebuilt snapshots had no effective
  config, managed keys or agents. It now stores the hook's own
  (redacted) document.
- **Dashboard cost no longer dips while the service rescans.** The
  watcher's placeholder session row (written before a session's
  subagents are parsed) reset the session's stored cost and tokens to
  zero until the fold finished, so a large session could briefly vanish
  from the totals. It is now insert-only (`Store.ensure_session`).
- **Context budget never found a project's settings snapshot.** The
  config hook stores `project_slug` as a hash (`slug:<12 hex>`), but
  `context_budget.py` looked snapshots up by the readable slug, so
  CLAUDE.md, agent-list and MCP estimates, the auto-compact setting and
  its drift check were always blank. New `snapshots.snapshot_project_key`
  computes the hook's key.
- **Sessions were joined to other projects' snapshots.**
  `snapshots.snapshot_for` takes an optional `project_key` and ignores
  snapshots from other projects (schema-1 snapshots with no project
  still match). Config diff, config drift, `compare` and `savers` pass it.
- **Subagents with no recorded type were counted as the main session**
  in carry, waste, cache rebuilds and limits; phases filed the main
  session under "unknown". All now use `model.agent_type_label`:
  `top-level` for the main session, `unknown` for an untyped subagent.
- `model_swap`: the unpriced-turns note now says those turns are still
  priced at the alternatives (so the saving is understated), and the
  lever for a built-in agent says to create an overriding agent file;
  workflow, fork and untyped subagents have none.
- Baseline `cost_per_session` no longer counts orphaned subagent bundles
  as sessions.
- Mixed "metric / value" tables (overview totals, compactions summary)
  show numbers with separators and at most 2 decimals instead of raw
  floats.
- TTL near-miss column labels follow `near_miss_window_s`, and the
  just-missed token count uses the same basis as its cost (the context
  rewritten).
- The scorecard's usage-limit share of cache rebuilds counted every
  write after a limit pause; it now counts only limit-expiry rebuilds.
- The auto-compact simulation (`compaction_sim`) shrank everything a
  session added after a simulated summary by the compression ratio, so
  context grew far too slowly afterwards and small windows looked much
  cheaper than they are. It now removes only the tokens the summary
  dropped. On the worked example the 100,000 saving falls from 69% to
  35%, and the recommended floor moves to 150,000.
- Attachment sizes are measured from the fields real transcripts carry
  (`skill_listing.content`, `instructions.files[]`,
  `deferred_tools_delta.addedLines` and others). They were always 0,
  because the code read a `rendered` field that real transcripts never
  have.
- The dashboard's recommendation cards no longer show a stale
  `apply <lever> --dry-run` hint.
- **The watcher never re-parsed a transcript whose file hadn't changed
  but whose stored `parser_version` had fallen behind** (`watcher.py`
  `FileWatcher._resolve`/`_needs_parse_this_tick`, `store.py`
  `Store.known_files`) — the re-parse decision compared only
  `(mtime_ns, size_bytes)` against `Store.known_files()`, so a
  `PARSER_VERSION` bump (e.g. 5 -> 6, above) only reached a transcript
  the next time its file actually changed; an untouched file kept
  serving fields computed under the old parser indefinitely.
  `known_files()` now also returns each transcript's stored
  `parser_version`, and the watcher treats a mismatch against the
  running `PARSER_VERSION` as needing a re-parse even when the file
  itself is unchanged, reusing the on-disk digest cache (already keyed
  on `parser_version`) so the rebuild costs one cold-ish tick per
  transcript, then nothing. A new `files_reparsed_stale_parser`
  `WatcherStats` counter (surfaced in `/api/health`'s `watcher` block)
  lets an operator see the rebuild actually happen.

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
  [README.md's "Applying a profile"](README.md#11-applying-a-profile),
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
