# v0.2 web UI

The service's UI is `static/index.html`, `app.css` and a set of native
ES modules with `app.js` as the entry point (see "Modules" below), served
by the same `http.server` process as the JSON API described in
[`docs/api.md`](api.md). Plan Milestone v0.2: "no framework, no CDN,
inline SVG charts, `prefers-color-scheme` dark."

## Constraints (binding on every file under `static/`)

- **No framework.** Plain DOM APIs (`document.createElement`,
  `fetch`, `addEventListener`). No React/Vue/htmx/jQuery, no build
  step, no bundler — `static/` is served as-is.
- **Native ES modules.** `index.html` loads
  `<script type="module" src="/static/app.js">`, which imports the
  other first-party modules with static `import` statements only (no
  `import()`, which the forbidden-substring scan rejects). The module
  graph has no cycles: a module that needs to open another view calls
  `core.js`'s `goTo`, which `app.js` wires to its router, and `api.js`
  redraws the sidebar's status line through `figures.notify` rather
  than importing `shell.js`. `tests/test_service_static.py` checks every
  module imports what it uses from another module, that every import
  names a real export, and that there is no cycle. The service serves
  `.js` as `text/javascript` (pinned in `api.py`, not left to
  `mimetypes`), because a module script with the wrong type fails to
  load under `X-Content-Type-Options: nosniff`.
- **No CDN, no external reference of any kind.** Same rule
  `render/html.py`'s module docstring already enforces for the CLI's
  standalone HTML report: no `<script src>`/`<link href>` pointing
  off-origin, no `@import`, no bare `http://`/`https://` literal in any
  first-party file, and `url(...)` only for a vendored font
  (`/static/fonts/`) or an in-page `#` fill. Everything the page needs
  ships in the repo and is served by the same process. A test mirroring
  `tests/test_render.py::test_html_has_no_external_references` greps
  every first-party file directly in `static/` for those substrings.
- **Vendored, pinned third-party files.** `static/vendor/` holds d3
  7.9.0 and `static/fonts/` holds Inter and JetBrains Mono, each with
  its licence. `static/THIRD_PARTY.sha256` pins every one of them, and
  each name carries its release, so a new release is a new URL.
  `tests/test_static_vendor.py` checks the hashes and that nothing
  unlisted is there. It also checks that `.gitattributes` keeps git
  from rewriting them, and that the wheel ships them. Nothing is
  fetched at runtime. Chart code reaches d3 only through `d3.js` (`import d3 from "./d3.js"`), and
  never calls d3's CSV/TSV parsers, which build code with
  `new Function` and so fail under `script-src 'self'`.
- **Inline SVG charts.** Every chart is `<svg>` markup built in the
  page from the JSON the API already returns: the session timeline
  (`page-spend.js`), the inline bars in report tables (`grid.js`) and
  the habit sparklines (`page-habits.js`). Colours come from the
  `--chart-*` tokens, so each has a dark-mode value.
- **Design tokens, light and dark.** `app.css` opens with every
  colour, type size, space, radius, shadow, layer and motion value as
  a custom property on `:root`. Dark values are declared twice with the
  same content: under `@media (prefers-color-scheme: dark)` guarded by
  `:root:not([data-theme="light"])`, and under
  `:root[data-theme="dark"]`. `theme-boot.js`, a classic script in
  `<head>`, sets `data-theme` from `localStorage` before the first
  paint, so a chosen theme never flashes the other one first.
  `tests/test_ui_tokens.py` checks that the two dark blocks declare
  the same values, shadows included.
  It checks that every text/surface pair reaches 4.5:1 in both themes,
  and every focus ring and control outline 3:1. It also checks that
  motion has a `prefers-reduced-motion` alternative, and that no card
  uses a thick side stripe as a colour cue, in any spelling: a side
  border, a logical border or an inset shadow. The CLI's standalone HTML
  report keeps its own palette (`render/html.py`'s `_STYLE`).
- **One focus ring.** A global `:focus-visible` rule draws a 2px ring
  in `--focus` on every focusable element; nothing removes it.
- **Icons are inline SVG.** `icons.js` draws every icon on a 16px grid
  in `currentColor`. The static scans ban emoji, arrow and check-mark
  characters, so those glyphs are icons too. Status is always an icon
  and a label, never colour alone, and each icon keeps one meaning:
  the octagon (`critical`) means act on this ("Do this", a check's
  "Worth a look"), the triangle (`warning`) means worth considering,
  the circled check (`success`) means nothing to do, and the circled i
  (`info`) is for your information. A recommendation's severity chip
  sits inside its heading, so a screen reader moving by headings hears
  the severity before the title.
- **One way to write a change.** A signed percentage goes through
  `format.js`'s `signedPercent`: "+12%", or a true minus sign
  (U+2212), which is as wide as the plus, so signed columns line up.
- **No inline secrets, no auth token in the DOM or a cookie.** The
  service has no login — it binds to localhost and relies on that for
  access control (plan: "port bound to localhost only"), so there is
  nothing to store client-side beyond per-viewer UI state, never data
  the server should be the source of truth for. The `localStorage`
  keys are `tls:view` (the last view shown), `tls:window` (chosen
  window), `tls:sort:<table>` (a table's sort), `tls:theme` (`light` or
  `dark`; anything else follows the system), `tls:sidebar` (`rail` or
  `full`) and the capture banner's `tls:captureNotesHidden`. Two older
  keys are read once: `tls:activeTab` (the old tab bar's last tab,
  opened as its view and then removed) and `tls:overviewWindow`, when
  `tls:window` is unset.

## Pages

One page (`index.html`) with a sidebar of pages (`PAGES` in `links.js`).
A page with more than one part shows them as segments, a row of links
beside its title. Each page, or page and segment, is a *view* with its
own address, `#/<page>[/<segment>]?w=<window>`, and each view renders
from its own `/api/*` route(s). A view is drawn the first time it is
opened and kept until the window changes; views have no background
poll. Seventeen views ship, in the sidebar's order below.

**The router** (`app.js`) reads the address on load and on every
`hashchange`. Every in-app link goes through `goTo` (`core.js`, which
`app.js` wires to the router, so `links.js`'s `pageLink` and the page
modules reach it without importing `app.js`). It adds one history
entry, so Back, Forward and bookmarks work. An address that leaves out
the segment or the window is rewritten in place to the full form, and a
page named alone opens the segment last used there. An address that
names no view opens the last one shown (`tls:view`); on the first visit
after the tab bar was replaced, that is the view the old tab bar last
had selected (`tls:activeTab`, read once and removed). Each view keeps
its scroll position: Back and Forward return to it, and a link to
another view opens at the top. A "Skip to the page" link before the
sidebar moves focus to the page title (`#page-title`, the one `h1`).

**The sidebar** lists the seven main pages, with a count of "Do this"
recommendations on Actions, then Data quality and the Glossary, then
the status line. A toggle narrows it to a 56px rail of icons, with each
page's name as a tooltip on hover and on keyboard focus
(`tls:sidebar`); below 1024px wide it is always the rail.

**The page header** stays pinned while the view scrolls, with a
hairline once content passes under it. It holds the page title, the
segments, the window picker and the theme toggle (same as the system,
light or dark: `tls:theme`).

**The health banner and status line** are on every view, from
`/api/health` (`pollHealth()`: every 3 seconds while its `status` is
`"starting"`, every minute otherwise). The banner, under the page
header, is hidden while the status is `"ok"`. Otherwise it shows the
route's `message`: the first scan's progress (with a progress bar)
while `"starting"`, and a warning with the restart command when
`"degraded"` or `"stale"`, or when the service can't be reached at all.
When a scan that was in progress finishes, the banner and the status
line offer **Redraw figures**, which drops every drawn view and the
report cache and redraws the view on screen; views never redraw
themselves under the reader. The status line, at the foot of the
sidebar, gives the status in words beside a coloured dot (Up to date,
Scanning your history, Last scan failed, Not updating, Can't reach the
service), when the last scan finished, the time the oldest figures
drawn are from (`X-Figures-As-Of`) once a report has loaded, the
capture level as a link to Setup › Capture, and the running version.
A view keeps the figures it drew; a reload, a new window or **Redraw
figures** picks up a newer report.

**The capture banner** sits under the health banner
(`#capture-banner`, `role="status"`). `pollHealth()` hands it
`/api/health`'s `capture` block; it fetches `/api/capture` when that
block changes, or every five minutes for fresh figures. The status line
always shows the capture level, so the banner shows only when there is
something to act on: notes when the end time has passed, a hook entry
is missing, no notes have been seen, Claude tags too few messages, or
enough has been collected to lower the level, plus, once capture has
run long enough to price a weekly cost, a note weighing that cost
against what the habits worth trying that depend on its reports or your
feedback are worth a week (or that nothing measured yet relies on it).
It then leads with the headline ("Metrics capture: Essentials · since
<date> · N tokens · <amount> (x% of spend) · tagged on P% of
messages", in billing units) and links to Setup › Capture and Work
habits. **Dismiss for a week** hides the notes until they change
(`localStorage` `tls:captureNotesHidden`). The feedback note ("Finished
a piece of work? Run /tl-feedback ...") shows while that item is on,
and a note with the `capture feedback on` command while the
`/tl-feedback` skill is on but its file needs installing, or the
`capture brief on` command while brief templates are on but the
`/tl-brief` skill's file needs installing.

**The dashboard never changes Claude Code's settings.** There is no
Apply button. Every fix is a prompt to paste into Claude Code or an
`apply ... --dry-run` command to run yourself, each with a Copy button.
The only things the dashboard writes are profile files in this tool's
own folder, session tags and ratings in its own store and the `[capture]` table
in its own `config.toml` (Setup › Capture). Amounts follow the
billing mode (`docs/writing-help.md`, "Amounts").

**The window picker** is a menu button in the page header (the arrow
keys move through it; Esc closes it and returns focus): Last hour,
Today, Last 24 hours, Last 7/30/90 days (30 by default), All time, or
Since my last change (`WINDOW_OPTIONS`). It is sent to every
report-backed route as `window=<name>` or `window_days=N`
(`withWindow()`), carried in the address as `?w=`, and remembered in
`localStorage` (`tls:window`, read from the older `tls:overviewWindow`
key when it is missing). A change drops every drawn view and redraws
the one on screen, so no view keeps showing the previous window's
numbers (review finding 21). `loadReport()`'s cache is keyed by the
window for the same reason. The short windows (last hour, today, last
24 hours, since my last change) carry a note: a session active in the
window counts in full. A few panels always cover all history and ignore
the picker: the rebuild counts on Cache › Rebuilds, the baseline
panel, "Your changes and what they did", the setup panel and service
health. Setup › Capture's figures don't depend on the window, so there
the picker gives way to a note, "The window doesn't apply here"; the
Glossary has no figures and shows neither.

1. **Overview** — top to bottom: a line naming the billing mode and
   why it was chosen (from `report.meta`); **Start here**; four stat
   cards from `/api/summary` (sessions, transcripts, cost at list
   price, tokens); the **Scorecard** tiles; the `totals` table from the
   report's overview section (`/api/report.json`, same window; its
   `by_model` table is on Spend › Usage); and **Service health** from `/api/health` (watcher status,
   plus a warning when the service is not registered to start at
   logon). **Start here** lists the three most important items from
   `/api/recommendations` (most severe first, each with its severity in
   plain words, `why` and estimated saving, and links to Actions ›
   Recommendations and Actions › Checks), then every scorecard area
   rated poor or worse, each as a sentence about its number.
2. **Actions › Recommendations** — `/api/recommendations`: one card per
   `Recommendation`, grouped by `severity` under "Do this" (action),
   "Worth considering" (advice) and "For your information" (info).
   Every card shows `why`, "For: <agent>", the estimated saving
   (phrased for the billing mode), "What to do", and a collapsed "Show
   the numbers behind this" with its evidence line(s) (`label:
   formatted value (from <table title>, <row label>)`, same value
   formatting `render/tables.py::format_evidence_value` gives the CLI's
   Markdown/HTML output). Per entry in `fixes` (each collapsed when
   there are several) it shows "What you're changing" (the six-part
   explainer), "Ask Claude to do it" (the prompt, with a Copy button)
   and, for a plain setting, "Or run this command" (the
   `apply --set ... --dry-run` line), then the reminder to restart
   Claude Code to pick up the change (`fixes.RESTART_NOTE`), which
   every fix on every page ends with. A card with a
   `lever` but no `fixes` names the setting and where it lives in plain
   words; a `scope: "managed"` card instead shows "managed by policy,
   raise with your administrator" (plan "Enterprise use") and no fix.
   The same "capture window open: provisional" notice as Setup ›
   Settings' baseline panel appears above the list while a capture window is
   in progress (`/api/baseline`'s `capture_status`).
3. **Actions › Checks** — `/api/quick-actions`: one card per check, each a
   question (for example "Is a cheaper model enough for any of your
   agents?") with a status badge (Worth a look / Nothing to do / Not
   enough data) and its one-line answer. "Show the evidence" (with the
   number of fixes and tips, for example "Show the evidence, 2 fixes
   and 1 tip"; no button when there is not enough data) loads
   `/api/quick-actions/<id>`: the evidence table, fix cards rendered by
   the same `renderFix` Actions › Recommendations uses, and habit tips.
   The same checks run in the terminal as `claude-token-lens check`.
4. **Spend › Usage** — cost by model (the overview section's `by_model`
   table, placed here by `TABLE_PAGE_MAP`), the
   `usage`/`elasticity`/`compactions`/`phases` report sections plus a raw
   `/api/compactions` list (windowed, newest first, the first 50 shown).
5. **Spend › Savings** (v4 wiring round) — the four newly-wired analytics
   sections, each fetched directly from its own report-backed route the
   same way TTL fetches `/api/ttl` (rather than waiting on the full
   `/api/report.json`), and each rendered with the same generic
   Section/Table renderer: `/api/carry` (context carry cost per tool and
   agent type, top carried results, truncation-cap savings), `/api/compaction-sim`
   (the `autoCompactWindow` sweep: cost per candidate window, the
   per-agent-type best window, and the fidelity check), `/api/model-swap`
   (ceiling saving from moving a model/subagent type one tier down), and
   `/api/waste` (spend on turns whose output was never used, by cause,
   agent type and top session). The `tool-output-carry`/
   `compaction-window`/`model-tier`/`wasted-turns` recommendations these
   sections' rules produce are not duplicated here — they show up as
   cards on Actions › Recommendations like every other recommendation.
6. **Spend › Sessions** — `/api/sessions`, 50 rows a page, newest first, with
   Previous/Next buttons (windowed, like the rest of the view); the report's
   `sessions` section follows below it. A row click (or Enter) renders
   that session's detail inline in the same panel rather
   than switching to a separate view (feature #5, "root-causing one
   expensive session", folded into Sessions rather than given its own
   view). The detail view fetches `/api/session/<id>` and renders an
   inline-SVG context-size-over-turns timeline from its `turn_series`/
   `markers` fields (`docs/api.md`) — markers for re-cache, compaction,
   spawn and human-message events; clicking a turn marker shows its
   context composition (feature #1) and the events immediately
   preceding it. A session with no stored top-level transcript digest
   yet shows an explicit "no per-turn data for this session" notice
   instead of a chart. Usage-limit events (v3-limits wiring) draw as a
   fourth marker kind, `limit_markers`, in the blank strip above the
   context line rather than on the line itself — see "Session timeline"
   below for why they're positioned by timestamp instead of turn index.
   The detail runs, top to bottom: a summary list, **Why was this
   session expensive?** (`/api/session/<id>/explain`: the headline, its
   sentences, and the cost split as a small table with share bars),
   "Mode override" and "Purpose override" selects with an "Apply tags"
   button (`POST /api/sessions/<id>/tags`, stored in this tool's own
   store), **Rate this session** while the dashboard rating is on (the
   `/tl-feedback` questions as checkboxes and radio buttons, from
   `feedback_questions`; **Save rating** and **Clear** send
   `POST /api/sessions/<id>/feedback` and redraw the detail), a
   **Transcripts** table, then the timeline.
7. **Cache › Rebuilds** — `/api/recache`: stat cards for cache rebuilds by cause
   (expired while idle, invalidated by a change, expired during a
   usage-limit pause — `recache.SIGNATURES`), headed "all history"
   because the route takes no window, plus the `recache`/`limits`
   report sections for the chosen window.
8. **Cache › Lifetime (TTL)** — `/api/ttl`: per-agent-type observed/simulated cost, the
   5m/1h recommendation and its fidelity — same figures as the CLI's
   `ttl` subcommand, including the fidelity-exceeds-bound suppression
   note.
9. **Agents & context › Subagents** — the `agent_startup` and `agents`
   report sections, from `/api/report.json`: what each subagent type
   is given at startup (and what it never used), cost per run, skills
   and MCP cost, effort, and what fills the context window.
10. **Agents & context › Quality** — the `quality`, `workflows` and
    `workstyle` report sections. The `quality` section, **Is the work going well?**, comes first:
    quality signals per agent type, then per model and
    effort with each setup compared with the one that agent used most
    ([concepts](concepts.md#7-quality-signals)), then **Agent runs
    retried on a larger model** (each agent and model whose runs the same
    agent redid on a larger model) and **Why agents were run again** (the
    reasons retries gave, when Claude writes the `[retry: ...]` marker);
    the failing-tools, counts and markers tables sit under the advanced
    toggle.
11. **Agents & context › Context** — what Claude reads at the start of every
    session and subagent. **CLAUDE.md files** (`/api/claude-md`): one
    row per file with its level, size in tokens, how often it was sent
    and to whom, the cost and its findings; "Review" loads
    `/api/claude-md/<id>` with the sections by size, duplicates, stale
    references and fix prompts. **Skills** (`/api/skills`): each skill's
    description, source, how often it was listed and used, and what the
    listing cost, with a "Show only skills Claude never used" checkbox
    and, when two or more are unused, one fix that hides them all. File text and skill descriptions are read when
    the view asks and never stored. The terminal equivalent is
    `claude-token-lens review claude-md|skills`. The report's
    `context_budget` section follows: what fills the context window at
    the start, and what can go.
12. **Work habits** — the report's `habits` section: the "Weekly pace"
    digest as cards (the three habits worth the most a week, what the
    habits you already picked up save, what a piece of work that met
    its goal cost, and how many messages Claude tagged), then "Habits
    worth trying" as cards, each with its saving a week in billing
    units, what your sessions show, an example to copy (Copy button),
    how often it was seen, its source (reported, inferred or your
    feedback), confidence, its trend with a by-week bar chart, and how
    the saving is worked out. Then the brief templates, one card per
    kind of task with a Copy button (`claude-token-lens capture brief
    on` installs the `/tl-brief` skill that asks for the same lines),
    then the section's other tables and its notes (capture off, no
    feedback yet). Every item is a way of working to try: nothing on
    this page changes a setting.
13. **Setup › Settings** — `/api/config-diff?auto_keys=1`: `effective_config`/`config_layers`/
    `config_groups`/`config_drift` and the per-key diff tables, rendered
    once (the config section is skipped when the view walks the full
    report for `baseline_comparison`) (plan "Configuration layers and
    per-project effective config" section) — which layer supplied each
    key, and which projects share an identical effective config. A
    snapshot with no project attribution (`project_slug: null`, see
    `docs/api.md`) is shown as a user-level layer rather than a project's.
    A "Latest baseline" panel below the drift table renders
    `/api/baseline` (v0.3): the capture window's one-line status
    (`capture_status.summary`), the latest capture (or "no baseline
    captured yet"), and every past capture in a history table — with a
    "capture window open: provisional" notice whenever
    `capture_status.started && !capture_status.complete`.
14. **Setup › Profiles** — a "Save my current settings as a profile" button
    comes first (`POST /api/profiles/from-current`; if a copy already
    exists it asks before replacing it). Then **Create a profile**: pick
    a goal from `/api/profile-goals` (spend less on subagents, cheaper
    models, cheaper cache, shorter conversations, less thinking, start
    from my recommendations, or start from my current settings, whose
    "Start here" presses the save button above). The goal's
    draft is a table of candidate changes (setting, now, after,
    estimated effect, why and the trade-off) with the ones your data
    supports already ticked; each tick re-posts the chosen changes to
    `POST /api/whatif` and updates the running total. The goal "A
    profile for one kind of task" adds a "Kind of task" picker (it
    reloads the draft with `task=`) and a note on what was found, and
    names the profile after the task. Name it and save
    (`POST /api/profiles`). **Best setup for each kind of task** follows:
    the report's `habits_setups` table (a note and a link to Setup ›
    Capture while nothing is tagged). Then **Your profiles and the built-in
    ones**: one card per profile from `/api/profiles` (the
    catalogue's seven shipped profiles plus every user profile): name,
    "Built in" or "Yours", who it is for, and "Changes N settings: ..."
    listed by their plain labels (from `/api/profiles/<id>` and
    `/api/profile-schema`). The card matching the latest baseline's
    `suggested_profile_id` carries a "Suggested for you" badge. "Show what
    it changes" opens the detail view from `/api/profiles/<id>/diff`,
    with a scope picker ("Apply it to:", in plain words): one table of
    Setting / Now / After / Set in (unchanged and policy-locked rows are
    greyed and say so), an **Estimated effect** table from
    `POST /api/whatif` (Change / Effect / How it was worked out), then
    "Ask Claude to do it" (the route's `prompt`), "Or run this command"
    (`dry_run_command`), the reminder to restart Claude Code afterwards,
    and "Or try it for one session" (`launch_command`,
    `claude-token-lens apply <id> --launch`, which saves the overlay in
    this tool's folder and prints the `claude --settings` command; it
    says when the profile's agent or environment changes can't come
    along), each with a Copy button, and the unified diff in a collapsed
    block. The UI never runs a command itself, and never
    fills in a project directory on the user's behalf (`docs/api.md`'s
    own note on why that route never accepts one).
    **Your changes and what they did** (`/api/impact`, all history):
    each `apply`, undo or settings change the hook saw, with the
    sessions before against those after on the measures that change
    should move, then a "Quality, <agent>:" line per agent the change
    touched (or the main session) with a collapsed Signal / Before /
    After / Verdict table (agents with too few runs yet share one line),
    and, for an apply, "To undo it:
    `claude-token-lens apply --revert <backup_ts>`". A metrics capture
    change is measured by capture's tokens per session and the share of
    messages tagged, and its card gives the `capture level <old>` (or
    `capture off`) command that changes it back.
    "Make your own profile" is a form built from
    `/api/profile-schema`: "Start from" any profile, one field per
    setting (a select for fixed values and on/off, a number box with
    the allowed range, or a comma-separated list), an "Add an agent"
    block per agent, and "Edit as JSON instead" as an escape hatch. It
    posts to `POST /api/profiles` and shows the server's validation
    error inline. It sits last, in a collapsed "Edit settings directly"
    block.
15. **Setup › Capture** — `/api/capture`: the cost warning, then where
    capture stands (its setting, what it has cost since it was turned
    on by scope, how often Claude tagged, and what the estimates
    replay), a line weighing what capture costs a week against what the
    habits worth trying that depend on it or your feedback are worth a
    week once there's enough time since it began to price it (`roi`;
    the same wording as the banner's note, and left out while that's
    `null`), a warning with the `capture connect` command
    (Copy button) when `settings.json` lacks a hook entry a chosen
    metric needs, the level cards (Off, Free, Essentials, Standard,
    Deep, Custom) each with what it adds and its weekly estimate, the
    sampling and end-time selects, and every metric grouped by where
    it is captured: a checkbox, what it captures, what Claude writes
    for it, why, what it helps with, its estimate against its actual
    cost, and how much has been collected. The feedback skill's row
    shows its runs over the last 14 days and, while its file is
    missing or out of date, a **Needs installing** badge with the
    `capture feedback on` command (Copy button): the dashboard never
    writes Claude Code's folder. The brief templates row does the same
    for the `/tl-brief` skill, with `capture brief on`. The status-line rows say so when
    Claude Code's status line isn't this tool's. Metrics that are always
    measured can't be switched off. Switching off a metric switches
    off the ones that need it. Every change that asks Claude for more
    (a level, a metric or a larger sample) first shows the cost
    warning again in a dialog. Changes are sent to `POST /api/capture`
    and the view and banner redraw from its answer; when the file
    can't be written, the view shows the CLI commands to run instead.
16. **Data quality** — **What this tool installed, and what to expect**
    first (`/api/setup`): what to expect in plain words (it never uses
    your Claude tokens, the hook and statusline add none, the first scan
    takes a while, nothing changes until you apply it), then each thing
    installed with where it is, what it does, its token cost and how to
    undo it, and the uninstall command under "Remove everything". Then
    any report section no other view claims (the fallback in
    `SECTION_PAGE_MAP`, below). Then `/api/diagnostics`: whether the
    snapshot hook and the statusline are working, then the parse-quality
    counters (`Diagnostics` dataclass fields), as one labelled table,
    each row with what it means (`helptext.diagnostics_table`) — same
    figures as the CLI report's Diagnostics section, so a user comparing
    the UI against a CLI run for the same window sees identical numbers.
17. **Glossary** — the `GLOSSARY` constant in `page-glossary.js`: each term the
    dashboard uses, in plain English. The README's glossary is the same
    list, word for word.

## Help and labels

The page title in the header is the one `h1` (the page's label from
`PAGES`), and each view opens with its one-line intro from `PAGES`
(`viewIntro`). Sections are `h2`, tables `h3`, and nothing goes deeper
than `h4`; a table shown on its own in a view it was moved to takes the
`h2` itself. The words come from `helptext.py` (house style:
`docs/writing-help.md`), applied to the report model by
`helptext.annotate` and rendered generically:

- **Section intro and "How to read this".** `Section.intro` as a line
  under the heading; `Section.help`/`Table.help` as a collapsed
  `<details>` with "What it shows", "How to read it", "When to act".
- **Column help.** A `?` button in the header (a real `<button>` with
  `aria-expanded`, keyboard and touch operable, never a `title=`-only
  tooltip) shows that column's `Column.help` in a line above the table.
  It stops propagation so it never also sorts the column.
- **Value labels.** `Table.value_labels` replaces raw row values such
  as `top-level` with "Main session"; the raw value stays in the
  cell's `title` and `data-raw`.
- **Placement.** `Table.dashboard`: `keep` tables are shown,
  `advanced` tables go into one collapsed "Advanced detail (N)" block
  per section, and `report` tables are left to the CLI report with a
  one-line note.

## Data flow

Every view's data comes from `fetch('/api/...')` returning the envelope
`docs/api.md` describes; `api.js`'s `fetchJson` unwraps `{"ok": true, "data": ...}`
and renders, or shows the `error.message` inline (never a raw stack
trace — the API never sends one, per its own `error.code`/`message`
contract) on `{"ok": false, ...}`. No view holds state the server
doesn't already have; a page reload is always safe.

## Testing

`tests/test_service_static.py` greps every first-party file in
`static/` (each `*.html`, `*.js` and `*.css` directly in it) for
the forbidden substrings above (mirroring `test_render.py`'s HTML
egress test), then starts a real service with canned JSON for every
route and checks, among other things, that `app.js` and `app.css` are
served with the right content type, that
the modules fetch every `GET /api/...` route this document's sibling
`docs/api.md` documents under a `### \`GET ...\`` heading, that `PAGES`
and the view renderers name the same views, that the heading policy
holds, and that every section `report._SECTION_ORDER` can emit is mapped
to a view. `tests/test_ui_copy.py` holds the dashboard's own words to
`docs/writing-help.md` ("Dashboard copy"). There is no headless browser: `urllib.request` plus string checks is enough for a
stdlib-only test suite.

## Modules

| File | Holds |
|---|---|
| `app.js` | the entry point: the router (`resolveRoute`, `showView`, `VIEW_RENDERERS`), the sidebar, the page header, the window picker and the theme toggle |
| `core.js` | `el`/`clear`, `localStorage` helpers, the shared `state`, `WINDOW_OPTIONS`, `renderedViews` and the `goTo` hook |
| `format.js` | `formatCell`, `money`/`moneyText` (the `Units.money` mirror), `thousands`, `shortTs` |
| `api.js` | `fetchJson`, `loadInto`, `postJson`, `withWindow`, `loadReport` (cached per window), the figures-as-of stamp |
| `ui.js` | notices, `emptyState`, badges, `codeBlockWithCopy`, fix blocks and `RESTART_NOTE` |
| `grid.js` | report tables (`renderTable`, sorting, help), `renderMappedSections`, `simpleTable` |
| `links.js` | `PAGES` (pages, segments, intros), `SECTION_PAGE_MAP`/`TABLE_PAGE_MAP`, `parseHash`/`formatHash`, `viewIntro`, `pageLink`/`captureLink` |
| `shell.js` | what is on every view: the health banner, the sidebar's status line, the capture banner |
| `icons.js` | the icon set: `icon(name, opts)` returns an inline 16px SVG |
| `d3.js` | the one door to the vendored d3 (`import d3 from "./d3.js"`) |
| `theme-boot.js` | a classic script, not a module: sets `data-theme` before the first paint |
| `page-overview.js` | Overview |
| `page-actions.js` | Actions › Recommendations and Checks |
| `page-spend.js` | Spend › Usage, Savings and Sessions (with the session timeline) |
| `page-cache.js` | Cache › Rebuilds and Lifetime (TTL) |
| `page-agents.js` | Agents & context › Subagents, Quality and Context |
| `page-habits.js` | Work habits |
| `page-setup.js` | Setup › Settings and Profiles (with impact and backtest) |
| `page-capture.js` | Setup › Capture |
| `page-data.js` | Data quality |
| `page-glossary.js` | Glossary (`GLOSSARY`) |

## Implementation notes (S1-ui)

The UI shipped in `static/` (`index.html`, `app.css` and the modules above) follows
this document's Constraints, Data flow and Testing sections, and the
Pages section above describes the shipped structure: seven main pages,
Data quality and the Glossary, seventeen views in all.

**Generic Section/Table rendering** is driven by two explicit maps in
`links.js`. `SECTION_PAGE_MAP` gives each report section key its view
(`recache`/`recache_by_group`/`limits` -> Cache › Rebuilds, `ttl` ->
Lifetime, `agent_startup`/`agents` -> Subagents,
`quality`/`workflows`/`workstyle` -> Quality, `context_budget` ->
Context, `sessions` -> Sessions, `config`/`baseline_comparison` ->
Settings, `usage`/`elasticity`/`compactions`/`phases` -> Usage
(`elasticity` only under subscription billing with usage-limit
readings), `scorecard`/`overview` -> Overview, anything else -> Data
quality), so an unrecognised section key still lands somewhere visible
instead of being silently dropped. `TABLE_PAGE_MAP` moves single tables
away from their section's view: `overview.by_model` to Spend › Usage,
and `habits.habits_setups` to Setup › Profiles. `renderMappedSections`
draws a section in full on its own view, and a moved table on its new
view under its own `h2`. `tests/test_service_static.py` checks every
section `report._SECTION_ORDER` can emit is in the map, and every table
key names a real section. `recache_by_group` is mapped even though it
never arrives as a section's own `key` today -- `report.py`'s
`_build_recache_section` appends it as an extra *table* inside the
`"recache"` section rather than a section of its own (review finding
20) -- so a future refactor that promotes it to its own section needs
no corresponding dashboard change. `limits` (v3-limits wiring) sits
with the rebuilds rather than falling to Data quality: a usage-cap
pause forces exactly the full-expiry re-cache cost `recache`/`ttl`
already attribute, so its six tables read naturally alongside them.
`habits` maps to Work habits, and `capture` to Setup › Capture, whose
one table is report-only: that view draws its own figures from
`/api/capture`.

**Spend › Savings (v4 wiring round).** `carry`/`compaction_sim`/
`model_swap`/`waste` are mapped to the `"spend/savings"` view in the
same `SECTION_PAGE_MAP` — not because anything calls
`renderMappedSections(report, "spend/savings", ...)` (nothing does),
but because an entry there is the only thing standing between a section
and the Data quality fallback, exactly the same as `ttl`'s own entry
above (the Lifetime view never calls `renderMappedSections` either).
The Savings view instead fetches its own four sections directly
— `/api/carry`, `/api/compaction-sim`, `/api/model-swap`, `/api/waste`
— the same one-route-per-view pattern `renderTtl` already used, via a
new shared `renderReportBackedSection` helper factored out of what used
to be `renderTtlData`'s own body (identical behaviour, now shared by
five call sites instead of duplicated). This keeps the four new
sections off the full `/api/report.json` fetch entirely for this view,
matching TTL's existing "own dedicated route" precedent rather than
introducing a second pattern.

**Session timeline (S1-integration fix 1.g).** `/api/session/<id>` now
carries `turn_series`/`markers` (`docs/api.md`) whenever the watcher has
stored a top-level transcript digest for that session.
`buildSessionTimeline`/`findPerTurnSeries` consume exactly that shape —
`turn_series` as `[turn_index, ctx, cache_creation_tokens, is_recache,
preceding_primary]` rows, `markers` as turn-index lists keyed by
`compactions`/`spawns`/`human` — replacing the placeholder box this
document previously described. A session with no stored digest yet
(e.g. ingested before the watcher parsed a top-level transcript, or a
digest that failed to decode) still falls back to an explicit "no
per-turn data for this session" notice rather than a fabricated curve.
A single-turn session (exactly one point) draws as a dot rather than a
`<polyline>`, which needs at least two points to render anything
(review finding 11). The chart's maximum-context axis scale is computed
with a plain loop rather than `Math.max.apply` (review finding 10),
which could otherwise exceed the engine's call-stack/argument-count
limit on a session with tens of thousands of turns. A session over
`Store.MAX_TURN_SERIES_POINTS` turns has its `turn_series` downsampled
server-side (`truncated: true`, `docs/api.md`); the timeline shows a
note saying so rather than presenting the thinned-out chart as complete.

**Usage-limit markers (v3-limits wiring).** `/api/session/<id>`'s
`limit_markers` (`docs/api.md`, `docs/limits.md`'s "Session-timeline
marker contract") ride the same `<svg>`, but a usage-cap
pause/resume/agent-terminated event's own `ts` falls *inside* the gap
between two turns, not at one of `turn_series`'s own points — there is
no turn index to pin it to the way a compaction/spawn/human marker is
pinned. `buildSessionTimeline` instead interpolates each marker's `ts`
between the session's own `first_ts`/`last_ts` and draws it in the
blank strip above the context-size line, in one of three colours
(`limit_hit`/`limit_resume`/`agent_terminated`, distinct from the four
turn-indexed marker colours), with its own legend entries (shown only
for kinds actually present) and a tooltip naming the kind and, when
present, `detail.subkind` (`session_limit`/`weekly_limit` for a hit,
`rate_limit`/`other` for a termination). A marker whose `ts` doesn't
parse is skipped rather than mis-plotted at a wrong-but-plausible
position.

**Shape-defensive rendering for routes `api.py` hadn't shipped yet.**
At the time this UI was first built, `service/api.py` did not exist (a
sibling work package's deliverable), so `/api/ttl`, `/api/config-diff`
and `/api/baseline`'s exact response shapes were pinned only loosely by
`docs/api.md` ("same shape as ... Section/Table encoding" without a
worked example). `renderTtlData`/`renderConfigDiff` still check for
more than one plausible shape (a bare `Section` dict, a list of `Table`
dicts, or a flat row list) and fall back to a plain "no data for this
window" notice rather than rendering nothing on a mismatch — this is
still live for those two routes. `renderBaseline`'s own defensive
branch was trimmed once v0.3's `api.py` landed with a confirmed,
frozen shape (`{"baseline", "history", "capture_status"}` —
`docs/api.md`); it now reads that shape directly rather than guessing
at a bare list vs. a single object.

**Inline SVG without a namespace-URI literal.** Bar cells and the
timeline chart are built as HTML strings (e.g. `'<svg viewBox="..."
class="bar-svg">...'`) assigned via `innerHTML`, relying on HTML5's
foreign-content parsing to place `<svg>`/`<rect>`/`<polyline>`/etc. in
the correct namespace — not `document.createElementNS`, which would
otherwise require embedding the literal
`http://www.w3.org/2000/svg` namespace URI and trip the "no bare
`http://`/`https://` literal" rule this same document states above.
