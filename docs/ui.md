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
- **Inline SVG charts.** Every chart is `<svg>` built in the page by
  d3 from the JSON the API already returns, through `renderChart` in
  `charts-types.js` (see "Charts"); the grid's inline bars stay plain
  elements. Colours come from the `--chart-*`, `--div-*` and ink
  tokens, so each has a dark-mode value.
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
- **Forced colours.** Under Windows High Contrast
  (`@media (forced-colors: active)`) the system's colours replace the
  theme, and app.css keeps the dashboard readable in them. Charts keep
  their own colours (`forced-color-adjust: none` on `.chart-svg`,
  `.sparkline` and `.swatch`): a mark's colour and hatching are what
  the legend names, and the system's few colours can't keep the series
  apart. Axis text, labels and rules take CanvasText, gridlines
  GrayText, a linked rule label LinkText, and the chart's keyboard
  cursor and brush Highlight. Focus rings are Highlight. Chips, tiles,
  panels, menus, popovers, the drawer, tooltips and toasts keep a
  CanvasText border, the status dot is CanvasText, and a meter's lit
  segments are filled while the rest are outlined.
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
- **One number format.** `format.js` owns every rule, so the same
  value reads the same on every page:
  - money: 2 decimals under 10, 1 under 100, none above ("$12.34",
    "$56.7", "$1,962"), and "<$0.01" for a positive amount that rounds
    to nothing;
  - shares: 1 decimal ("12.4%");
  - tokens: 3 significant figures, compacted ("1.24M"), with the full
    count in the cell's tooltip;
  - durations: "2h 14m";
  - times: one absolute form ("2026-09-23 10:44 UTC", `shortTs`), and
    "5 min ago" (`relativeTime`) where freshness is the point, with the
    absolute time on hover;
  - a signed percentage goes through `signedPercent`: "+12%", or a true
    minus sign (U+2212), which is as wide as the plus, so signed
    columns line up.
- **Amounts follow the billing mode.** Every amount outside a grid goes
  through `money`, `moneyText`, `moneyNode` or `moneyParts` (the
  `units.Units.money` mirror): dollars on the API, a share of the
  weekly limit on Pro or Max when the service can work one out, and the
  list-price equivalent otherwise. A grid's money column stays a plain
  number, sortable, with its unit once in the header (`moneyUnit`: "$",
  or "list-price $" on a plan). The service writes an amount the CLI's
  way ("1,962.05 USD"); `fetchJson` runs every response through
  `readableAmounts`, so its text reads "$1,962.05" like the
  dashboard's own, and a unit in a label reads "($)". Another pricing
  currency reads the same both ways ("12.34 EUR"). No page writes
  "USD" itself (`tests/test_ui_copy.py`).
- **Readable names.** A project slug (`C--Dev-claude-token-lens`) reads
  as its folder (`projectName`: "claude-token-lens"). It drops the
  drive, a Windows home folder and the one parent folder your projects
  share, and names a worktree after its project ("claude-token-lens /
  ui-redesign"). The full slug stays in the tooltip. A slug can't tell
  a folder's hyphen from a path separator, so the name is a best guess.
  A long name ends in an ellipsis rather than wrapping inside a grid
  cell.
- **No inline secrets, no auth token in the DOM or a cookie.** The
  service has no login — it binds to localhost and relies on that for
  access control (plan: "port bound to localhost only"), so there is
  nothing to store client-side beyond per-viewer UI state, never data
  the server should be the source of truth for. The `localStorage`
  keys are `tls:view` (the last view shown), `tls:window` (chosen
  window), `tls:sort:<table>` (a table's sort), `tls:cols:<table>` (the
  columns chosen for a wide table), `tls:theme` (`light` or
  `dark`; anything else follows the system), `tls:sidebar` (`rail` or
  `full`) and the capture banner's `tls:captureNotesHidden`. Two older
  keys are read once: `tls:activeTab` (the old tab bar's last tab,
  opened as its view and then removed) and `tls:overviewWindow`, when
  `tls:window` is unset. The picked project is never stored: it lives in
  the address only (`?project=`), so a filter never outlives the visit
  that chose it and quietly narrows the next one.

## Components

`ui.js` holds the pieces every page is built from, and `grid.js` the
data grid. Each has a loading, an empty, an error and a stale state.

- **Button** (`button`): a label that says what happens ("Copy prompt",
  "Save tags"). Variants: primary (the one main action in a group),
  quiet, icon-only (with an accessible name) and link. The helper
  refuses a label starting with "Apply": the dashboard offers prompts
  and dry-run commands and never changes Claude Code's settings itself.
- **Chips:** `severityChip` (Do this, Worth considering, For your
  information: an icon and a label, never colour alone), `statusBadge`,
  `basisChip` (Estimate, At most, Simulated, Calibrated; a measured
  figure carries none) and `deltaChip` (a change on the previous period,
  coloured by whether up is good, neutral within 1%; three times or more
  reads as a multiple, "3.2 times", and an empty earlier period as
  "None before").
- **Metric tile** (`tile`, `tileRow`): a sentence-case label, the value
  at 28px with its unit in the quieter ink, then an optional basis
  chip, delta chip and hint. Tiles sit in a row that fits as many as
  the width allows.
- **Panel** (`panel`): a surface with a hairline border, a header slot
  and a body. Panels are never nested. Sections themselves sit on the
  page, 40px apart with a hairline between them.
- **Callout** (`callout`, `errorNotice`): info, success, warning or
  critical, as a tinted background with an icon and a label, with
  optional actions. An error says what happened and offers "Try
  again" when a retry can help.
- **Empty state** (`emptyState`): what happened, why, and what would
  fill it, as a link where one helps: "No sessions in the last 24
  hours. Pick a longer window to see older ones." Never "No data".
- **Skeleton** (`skeleton`, `loadingNode`): grey bars in the shape of
  what is loading, with a shimmer that stops under reduced motion.
- **Command block** (`commandBlock`, `renderFix`): the ways to make a
  change as tabs (a prompt for Claude, a dry-run command, and a trial
  for one session where there is one), each with a Copy button. Below
  them, the explainer `fixes.build_fix` writes, as a definition list:
  what the setting controls, where and who it affects, the trade-off
  and how to undo it, then the restart note.
- **Popover and tooltip** (`popoverButton`, `helpButton`,
  `attachTooltip`): the (i) "How to read" help and a column's (?) open
  a popover; a tooltip shows on hover and focus, value first. Both use
  `textContent` only.
- **Drawer** (`drawer`): a panel that slides in from the right, 560px or
  720px, with a title, a close button and Esc. It keeps focus inside
  while open and gives it back to the control that opened it.
- **Toast** (`toast`): one at a time, bottom right, `role="status"`, for
  a copy or a save. It goes after 3.5 seconds, and waits while the
  pointer or focus is on it.
- **Confirm dialog** (`confirmDialog`): a native `<dialog>`, used before
  a change with a warning, such as a Capture level that costs more.

### Search and keyboard shortcuts

`palette.js`. **Search** (Ctrl+K, or the Search button at the right of
the page header) is a modal `<dialog>` near the top of the window. It
finds:

- every page and segment, named as the sidebar names them (from
  `VIEW_KEYS`, so a new page is found without more work);
- the report's sections and tables: a section opens its page at its
  first table, and a table opens where it is shown or, for a table no
  page shows, in the table drawer (as an evidence link does);
- the window's recommendations and checks, each opening selected in its
  inbox (`?id=`);
- glossary terms and the How costs work cards (`?term=`, `?card=`);
- the window's 20 most recent sessions, each opening its drawer.

It also finds the window's projects, each showing only that project.
And it runs commands: **Set window: …**, **Show all projects** (while
one is picked), the three themes, **Show
keyboard shortcuts**, and **Copy prompt: …** for each recommendation
with a prompt (if the browser refuses the clipboard, the recommendation
opens so the prompt can be copied from there). Search never changes
Claude Code: it reads, opens pages and copies.

With nothing typed it lists the pages, then the commands. Typing narrows
every group at once: each typed word must match a result's name or its
other words, at the start of the name first, then at the start of a
word, inside a word, and finally as letters in order with at most two
gaps ("rbld" finds "rebuild"). Each group shows its 8 best, the group
with the best match first, with the typed words in bold.

The box is an ARIA combobox: focus stays in it while Up, Down, Page Up
and Page Down move `aria-activedescendant` through a `listbox` of
`group`s; Enter opens the one picked, Esc or a click outside closes it
and focus returns to what opened it. The list is `aria-busy` until the
window's actions, tables and sessions arrive (fetched once per window);
the pages and commands are there at once. A polite status line gives
the count.

**Shortcuts** (`?` shows them all in a sheet):

| Keys | What they do |
|---|---|
| Ctrl+K | Search |
| G, then O, A, S, C, E, H or U | Overview, Actions, Spend, Cache, Agents & context, Work habits, Setup (E and U because A and S are taken) |
| `[` and `]` | The page's previous or next segment |
| J and K | The next or previous item in the page's list: the Actions inbox, or the first grid whose rows open something (Sessions, CLAUDE.md files); Enter opens it |
| `?` | The shortcut sheet |
| Esc | Closes a drawer, menu, popover, search or the sheet |

A key typed in a text box, pressed while a dialog, menu or popover is
open, or pressed with Ctrl, Alt or the Windows key is left alone (Ctrl+K
apart), so shortcuts never take a letter meant for a field.

### Data grid

Every table on every page is `dataGrid`:

- a header that stays in view once a table passes 20 rows, and numbers
  right-aligned in even-width digits;
- short text (a name, a model, a key) on one line, so
  "claude-haiku-4-5" never breaks; a column holding sentences wraps as
  prose;
- a table that shares its section's title doesn't repeat it;
- a sort per table that is kept (`tls:sort:<table>`) and read back, so
  it survives a window change or a reload;
- the first 7 columns (or a table's lead columns) for a wide table, with
  a chooser for the rest (`tls:cols:<table>`) and a sticky first column
  while it scrolls sideways;
- a thin bar in the lead measure's cells, the default ranking picture,
  so a ranking needs no separate chart; an optional tint by value (the
  Quality grid);
- a report table of more than 12 rows opens on its first 10, in the
  order it is sorted, with a "Show all N rows" button (a grouped or
  per-row-kind table stays whole, and a table of more than 200 rows
  scrolls instead); a dated table listed oldest first (`NEWEST_LAST`:
  by day, week, month and five-hour block) opens on its latest 10 until
  it is sorted; an evidence link to a later row shows them all first;
- only the visible rows drawn once a table passes 200 rows;
- an evidence link's row scrolled into view and briefly highlighted
  (`pulseRow`): a glow under the row's text fades over 1.2 seconds.
  With reduced motion the glow holds still until the next click or key.

A report table of one row with `lead_columns` (a summary: "Cache
rebuilds at a glance") reads as up to four tiles of those figures,
amounts in the billing mode, with every figure in an **All figures (N)**
disclosure under them; an evidence link to the row opens it. Notes
under a table or section stay in view when there are one or two short
ones; more, or longer, fold into **How these figures are worked out
(N notes)**.

A section whose table a catalogue chart reads (compaction summaries,
idle gaps, lifetime by agent type, startup context) draws that chart
between its intro and its tables. `grid.js` can't import the charts
(they draw their Table view with it), so `app.js` hands it
`charts-types.js`'s `sectionChart` through `setSectionChart`. A mark
leads to the action its row feeds, or else to its row in the table
below.

### Service unreachable

When the local service stops answering, a callout under the page
header says so and retries after 2, 4, 8, 16 and then every 30
seconds. The last figures stay on the page, dimmed and marked stale,
so the page is never blank. Once the service answers again, the loads
that failed run again (`retryOnReconnect`), and the callout goes.

## Charts

`charts.js` is the frame every chart shares; `charts-types.js` draws
the marks. A page draws a chart with one call,
`renderChart(container, key, data, opts)`, and nothing else in
`static/` builds a chart.

### The catalogue and the rule for adding a chart

`CHART_SPECS` in `charts.js` is closed: it holds these eight rows, and
`tests/test_service_static.py` fails if a row is added or removed
without this table changing too.

| Key | Question (the chart's title) | Data | Form |
|---|---|---|---|
| `daily-spend` | Is spend rising, and did my changes move it? | `/api/daily-usage` | stacked columns, main session and subagents (or model tier), with a rule for each change |
| `savings-levers` | Which change saves the most, and how sure is it? | the `carry`, `compaction_sim`, `model_swap` and `waste` totals | horizontal bars, hatched when the figure is not measured |
| `summary-point` | Would summarising conversations at a different size cost less? | `compaction_sim.compaction_sim_by_window` | dashed line (simulated) with "Now" and "Cheapest" marked |
| `session-outliers` | Which sessions are the expensive outliers? | `/api/sessions` | scatter on a log scale, coloured by work mode, with a time brush |
| `session-context` | Where in this session did context grow or reset? | `/api/session/<id>` | line with shape markers per turn, and limit events in a strip above, one lane per kind |
| `idle-gaps` | Do idle gaps outlast the cache? | `recache.recache_gap_buckets` | column histogram with 5-minute and 1-hour rules |
| `lifetime-by-agent` | Which agent types gain from a 1-hour cache lifetime? | `ttl.ttl_break_even_share` | diverging bars around zero |
| `startup-context` | What fills each agent's context before it starts? | `agent_startup.agent_startup_breakdown` | stacked horizontal bars, at most 12 agent types |

A new chart needs a new row, and a row must meet both tests:

- it shows at a glance something a sorted grid with inline bars
  can't: a trend over time, a distribution against a threshold, the
  sign across entities, a part-to-whole of 8 or fewer parts, or
  outliers in two dimensions;
- it leads somewhere: an action, a drawer or a filtered grid.

A ranking is a grid with an inline bar, never a chart. There are no
pies, donuts, treemaps, gauges, calendar heatmaps, 3D or dual-axis
charts. A chart with fewer than 3 points says so in its frame instead
of drawing, except daily spend, where one day is still a reading.

### The frame

Every chart has the same parts, top to bottom:

- the question as its title (`h3`) and a **Show as table** toggle
  (`aria-pressed`), which swaps the plot for a grid of the same rows;
- a summary sentence filled from the data (`fillSummary`), which is
  also the plot's accessible name;
- a legend whenever there are 2 or more series, each entry a swatch or
  the marker's own shape, so no series is told apart by colour alone;
- the plot: bars at most 24px thick with a 4px rounded end, 2px lines,
  hairline grid lines, a 2px gap between stacked parts, text in ink;
- a note for what the reader needs to trust it, such as "Days run
  midnight to midnight UTC" or why a bar is hatched.

Money axes use `moneyAxis`, the chart mirror of `Units.money`: "% of
your weekly usage limit" when there is a share, "list-price $" when
there isn't, and plain "$" (no unit label) for the API. Token axes
compact (1.2M).

### Colour follows the thing, not its place

`ENTITY_COLOURS` fixes a colour per entity: the main session is
`--chart-1` and subagents `--chart-3`; model tiers (Opus and Fable,
Sonnet, Haiku) have their own slots; work modes and startup-context
parts too. Anything else is `--chart-other`. A chart never picks
colours by rank, so changing the window never repaints what stays on
screen.

### Reading a chart

- **Pointer:** a tooltip shows the value first, then the label. Every
  mark has a hit area at least 24px across. Clicking a mark that leads
  somewhere (`opts.open`) opens it.
- **Keyboard:** the plot is one tab stop. The arrow keys move a cursor
  from mark to mark and read it in the tooltip, Home and End jump to
  the ends, Enter opens the mark where it leads somewhere, and Esc
  lets go. On the session scatter, Shift with the arrow keys picks
  the sessions from where the cursor started, as the brush does. A
  change label on a daily spend chart is a link of its own: the next
  Tab stop after the plot, named "<change>, changed on <day>: see what
  it did", opened with Enter or Space. Screen readers skip the rest of
  the drawing (every other layer is `aria-hidden`; `layer(name,
  {links: true})` keeps one reachable).
- **Brush:** on the session scatter, dragging across a time range calls
  `opts.brushed` with the range, so the grid below can list only those
  sessions.
- **Linked highlight:** a grid given `link: {scope, key(row)}` shares
  hover and focus with the chart marks of the same scope (a day, a
  session, an agent type) through `highlight`/`listenHighlight` in
  `core.js`. Hovering either lights the other and dims the rest.
  `swatch(row)` puts the entity's colour in the row's first cell.

### Motion

When the data changes (a new window), `renderChart` keeps the frame
and d3 moves the marks to their new places over 600ms, unless there
are more than 1,500 of them. While new data loads, `holdChart` keeps
the last drawing at 0.55 opacity, so nothing jumps. The first drawing
draws in once. `opts.delay` holds a draw-in or morph back for that
many milliseconds. A chart resized while it is still drawing in or
morphing (`redraw`: a new width, or `setChartHeight`) carries on to
the new size in the time it had left, rather than finishing at the
old size; a settled chart is redrawn at once. Under reduced motion
every transition has zero duration.

The rest of the dashboard moves only `transform` and `opacity`, and
none of it runs under reduced motion or in a hidden tab:

- **A new view** fades in over the old one: `app.js`'s `changeView`
  runs the route change inside `document.startViewTransition`, where
  the browser has it and the view really changes. The old view fades
  out over 120ms (`--dur-page-out`, `--ease-in`) and the new one fades
  in over 220ms (`--dur-page`, `--ease-out`), rising 6px. While it
  runs, `html` has `view-changing`, which names `.views`, the sidebar,
  the page header and the toasts, so only the views fade and the rest
  change at once. The old view keeps its place on screen however far
  the page scrolls for the new one (`--view-shift`). Focus and scroll
  are still `showView`'s, so Back and Forward restore the scroll as
  before. No transition runs while a dialog (the drawer) is open.
- **The Overview's entrance.** The headline figures count up over
  700ms (`ui.js`'s `countUp`, expo-out): from 0 the first time, from
  the figures last shown after a window change. The tile is drawn with
  its final text first, and a timer and the tab being hidden both
  finish the count, so the final text is always what stays. Money is
  written by the billing mode's own format. The daily spend chart
  draws in 80ms after the figures start, and the next best actions
  arrive 24ms apart from 160ms (`enterInTurn`, app.css's
  `.is-entering`); a seventh row or later arrives with the sixth.

### Micro-forms

`sparkline(values)` draws a small trend for a tile, with no axes: the
tile's value is the reading. `meter(level, opts)` draws a level out of
5 as segments (`role="meter"`), with the level always written beside
it and the status as a word, not only a colour. `habitSparkline` is
the weekly pace line on a Work habits card.

## Pages

One page (`index.html`) with a sidebar of pages (`PAGES` in `links.js`).
A page with more than one part shows them as segments, a row of links
beside its title. Each page, or page and segment, is a *view* with its
own address, `#/<page>[/<segment>]?w=<window>&project=<slug>` (the
window first, then the project, left out for all projects), and each
view renders from its own `/api/*` route(s). A view is drawn the first
time it is opened and kept until the window or project changes; views
have no background poll. Eighteen views ship, in the sidebar's order below.

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
another view opens at the top. Two more parameters say what to open on
a view. `id` picks an item in an inbox
(`#/actions/recommendations?id=<key>`, where `key` is the
recommendation's `key`: its id, plus the agent type for a rule that
fires per agent, so a member's key opens its group on that agent; and
`#/actions/checks?id=<check id>`). Picking another item rewrites `id`
in place (`replaceParams`), so the address always names what is on
screen without adding history. An id the window doesn't have opens the
first item with a note saying so. `t=<section.table>&row=<row key>`
names the row behind a number: the view opens at it, opens the "More
tables" or Details that hides it, scrolls it into view and pulses it
(`evidence.js`'s `revealEvidence`, which waits for the table to be
drawn). Changing the window keeps `id` and drops `t` and `row`. A
view's module hears a new `id` through `onParams` (`core.js`), so a
link to the view already open selects without redrawing it.

**Evidence links** (`evidence.js`). A recommendation's evidence names a
report table and a row (`[label, value, "section.table", row_key]`).
The link opens the view `TABLE_PAGE_MAP` names for that table, else the
one `SECTION_PAGE_MAP` names for its section, and pulses the row there;
the scorecard strip on the Overview is tagged like a table
(`data-table-name="dimensions"`, a `data-row-key` per area), so its
items pulse the same way. A table no page shows (one placed `report`, a
section no page shows such as `savers`, or a table the page didn't
draw for this window) opens in the **table drawer** instead: the table
as a grid, the row pulsed, and a line saying where else it appears. A
test checks every evidence table the rules can name resolves one of
these ways. A "Skip to the page" link before the
sidebar moves focus to the page title (`#page-title`, the one `h1`).

**The sidebar** lists the seven main pages, with a count of "Do this"
recommendations on Actions, then Data quality and the Glossary, then
the status line. A toggle narrows it to a 56px rail of icons, with each
page's name as a tooltip on hover and on keyboard focus
(`tls:sidebar`); below 1024px wide it is always the rail.

**The page header** stays pinned while the view scrolls, with a
hairline once content passes under it. It holds the page title, the
segments, Search, the project picker, the window picker and the theme
toggle (same as the system, light or dark: `tls:theme`). Both pickers
are one component, `menuControl` (`app.js`): a menu button with radio
rows, moved through with the arrow keys, Home and End, or the first
letter of a row; Esc closes it and returns focus, and a click outside
or Tab closes it.

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

**The project picker** sits beside it: All projects (the default), then
every project with a session in the window, the most expensive first
(`report.meta.projects` from every project's report for the window,
`loadProjects()`). Each row reads as its folder (`projectName`), with
the full slug on hover. Picking one sends `project=<slug>` with every
window-aware request (`withWindow()`; the Overview's previous window
adds it through `withProject()`), so every figure that follows the
window covers only that project, and the header button keeps the
accent while it does. It is carried in the address as `?project=`,
never stored, and hides wherever the window picker does. The report
and recommendation caches are keyed by window and project together
(`scopeKey()`), and a report for one project never replaces the list of
project names (`setKnownProjects`). A project with no session in the
window stays in the menu, marked "No sessions in this window", and the
Overview says "No sessions in <project> <when>. Pick a longer window, or
all projects." An address naming a project the service doesn't know
(an old bookmark, a moved folder) is checked once (`checkProject`,
which keeps the answer): the dashboard shows every project and a toast
says why, and does so at once if an address names it again. Every
address the dashboard writes, from links, the pickers or a page saying
what it has open, carries the window and project through `scopeParams()`
(`links.js`), so none drops the project. Panels that
cover every project whatever the picker says (Settings' changes,
estimates and baseline; Cache › Rebuilds' causes) carry an "All
projects" chip while one is picked. Search lists the projects too, and
offers **Show all projects** while one is picked.

1. **Overview** — answers "What should I change next?". Top to bottom:
   - the logon warning, only when `/api/health` says the service is not
     registered to start at logon (`renderLogonNotice`; the full health
     detail is on Data quality);
   - **the summary sentence** (18px): what the window cost, the change
     on the period of the same length before, and how many changes are
     worth making and what the ways to save come to. It is built from
     fixed wording and numbers only, in the billing mode ("you spent
     $2,663" on the API, "you used about 38% of your weekly usage limit"
     on a plan). "All time" and "Since my last change" have no earlier
     period. The previous period is the N days before a last-N-days
     window, the hour or 24 hours before, or the same hours yesterday
     for Today (from local midnight), fetched as
     `/api/summary?since=&until=`: deltas compare a summary with a
     summary, never with a report figure. Its other forms: no sessions in
     the window ("No sessions in the last 7 days. Pick a longer window to
     see older ones."), no change recorded for "Since my last change",
     and, before any session is read, "What Token Lens does for you" in
     three lines (saying the first scan is running while `/api/health`'s
     `scan.scanning` is true, otherwise linking to Data quality);
   - **four tiles**: Spend (with its change and a daily sparkline from 3
     days), Available saving (the four ways to save from Spend ›
     Savings added up, plus any priced action no lever counts, such as
     lower effort, marked "At most" because they overlap; the model lever
     is every agent type's cheapest alternative added up, so no action
     shows more than the tile), Saved by
     the cache (`/api/summary`'s `cache_saved`, marked Estimate, with
     what a cache read costs against fresh input on the model that read
     most from the cache, from `report.meta.rates`) and Sessions (with
     the subagent runs: transcripts less sessions). Each links to its
     page;
   - **daily spend** (chart 1, `/api/daily-usage` with the window and
     `split=agent`), with your settings changes from `/api/impact` as
     labelled rules; a day opens Spend › Sessions and a change Setup ›
     Settings. Beside it from 1440px (under it at 1280), **Next best
     actions**: the top five of `/api/recommendations`, most important
     first, then biggest `saving_usd`, each with its severity, title
     (a link to Actions › Recommendations), estimated saving and a Copy
     prompt button. Side by side, the chart grows (300px to 560px) to
     the actions' height, redrawn in place with no morph
     (`setChartHeight`), so neither panel ends in a blank band; a
     draw-in still running carries on to the new height;
   - **How your setup scores**: the overall level, set by the lowest
     area, then the five scorecard areas as segmented meters (level 5-4
     good, 3 fair, 2 poor, 1 very poor, 0 not measured), each with a
     sentence about its number, which way is better, a link to where to
     look, and "What moves it:" naming a matching recommendation from
     this window when the area is below 5;
   - **Totals, and how amounts are counted** (collapsed): the billing
     mode and why it was chosen (`report.meta`), and the report's
     `overview.totals` table (its `by_model` table is on Spend › Usage).
   Every load starts at once; the drawing waits for the report, which
   sets the billing mode. A newer draw (a new window) drops the answers
   of an older one, and the chart is held dimmed while new figures load.
2. **Actions › Recommendations** — `/api/recommendations` as an
   inbox: the list to pick from on the left (360px, and it stays in view
   while the detail scrolls), the one picked on the right. Filter chips
   above the list narrow it by importance (Do this, Worth considering,
   For your information) and by area (Models, Cache, Context, Agents,
   Habits, Data and settings), each with its count. The area comes from
   `RULE_AREA` in `page-actions.js`, since a recommendation's `category`
   only says settings, workflow or data; a test keeps it in step with
   every rule id the service can send. A rule that fires once per agent
   type (`ttl-switch`, `spawn-*` and the rest) is one list item for all
   of them, titled for all of them ("7 agent types are sent your
   CLAUDE.md files every time they start"), most important first and
   in the service's order (by saving) within. Each item shows its
   severity as an icon, its title, "severity · area · N agent types"
   and its saving.
   The detail opens with the severity chip inside the `h2` and the
   title, then chips for who it's for, its area and where the change
   lands. **How this saves you money** follows, in three rows: what it
   costs you now (`why`), what the change does to the price (one or two
   sentences with the multiplier from your pricing, `report.meta.rates`
   through `fraction()`: "Reading from the cache costs a tenth of the
   input price...", "Sonnet 5 costs 40% of Opus 5's price, and Haiku 4.5
   a fifth of it"), and what
   you could save, with its basis chip (At most, Estimate, Simulated,
   Calibrated, from `saving_basis`) and how it was worked out. **What to
   do** gives the action, then, when there is more than one change (the
   model changes for six agent types, or a group), one table of them:
   the agent, the value it sets ("Set model to", with the model's
   name; a switch as On or Off), the saving and a Copy button per row.
   The value now is said once above the table when every agent shares
   it, else under each agent's name. In a
   group, picking a row shows that agent's change, and the address
   follows. Then the fixes: one command block, or several collapsed
   with the first open, each with the prompt, the `--dry-run` command,
   the six-part explainer and the reminder to restart Claude Code
   (`fixes.RESTART_NOTE`). A card with a `lever` but no `fixes` names the
   setting and where it lives; a `scope: "managed"` one says your
   organisation's policy sets it and shows no fix. **The numbers behind
   this** lists each report row the recommendation cites, as a link
   ("Cost of each agent type on other models, revixo-reviewer on Spend ›
   Savings") with the values taken from it, formatted as the CLI's
   `format_evidence_value` does. **The check this answers** links to the
   checks whose `rule_ids` name it. The same "capture window open"
   notice as Setup › Settings' baseline panel appears above the inbox
   while a capture window is in progress (`/api/baseline`'s
   `capture_status`).
3. **Actions › Checks** — `/api/quick-actions` in the same inbox: each
   check is a question with its status (Worth a look, Nothing to do, Not
   enough data, in that order), filtered by status. The detail gives the
   status, why it matters and the answer, then loads
   `/api/quick-actions/<id>`: **The numbers** (its table), the fixes
   (the same command blocks as Recommendations) and **Habits that
   help**. A check with not enough data says why instead. **The
   recommendation it leads to** links to each recommendation whose id is
   in its `rule_ids`. The same checks run in the terminal as
   `claude-token-lens check`.
4. **Spend › Usage** — chart 1, daily spend, the same chart as the
   Overview's with a **Split by** choice: main session and subagents
   (`/api/daily-usage?split=agent`) or model (`split=model`, one colour
   per tier). The choice is kept in the address (`?split=model`), so a
   reload or a link opens it, and it stays through a window change and a
   visit to another page; a day leads to its sessions
   (`#/spend/sessions?day=YYYY-MM-DD`) and a change marker to what the
   change did. Then cost by model (the overview section's `by_model`
   table, placed here by `TABLE_PAGE_MAP`), the
   `usage`/`elasticity`/`compactions`/`phases` report sections plus a raw
   `/api/compactions` list (windowed, newest first, the first 50 shown).
5. **Spend › Savings** — chart 2, **Which change would save the most,
   and how sure is it?**, heads the page: the four ways to save side by
   side, built from the same four responses as the sections below, and
   hatched unless measured. A bar leads to the row its figure comes
   from (`?t=<section.table>&row=`). The compaction-window section draws
   chart 3 through the section-chart hook (below). Then the four
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
6. **Spend › Sessions** — chart 4, **Which sessions are the expensive
   outliers?**: every session in the window (one `/api/sessions` fetch,
   newest 2,000 at most, with a note when the window holds more) by
   start time and cost on a log scale, coloured by how it ran. Dragging
   across the chart lists only the sessions that started in that
   stretch; a `?day=YYYY-MM-DD` from a daily spend chart lists the
   sessions active on that UTC day. A line above the list says what it
   is narrowed to, with **Show all sessions**. A row and its dot light
   up together, and the row carries the dot's colour. The report's
   `sessions` section follows. A row click (or Enter), or a dot, opens
   the session in a drawer. The detail view fetches `/api/session/<id>` and renders an
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
7. **Cache › Rebuilds** — opens with **What the cache does for you**:
   three tiles in your own numbers for the window, each with its price
   multiplier from `report.meta.rates` (via `fraction()`) and a link to
   its card in Glossary › How costs work. What reading from the cache
   saved (`ttl_cache_economy`'s overall `net_saving_usd`, an estimate),
   what avoidable rebuilds cost (`recache_summary`'s
   `avoidable_cost_usd`) and how many rebuilds that covers (the sum of
   `recache_signature_split`'s `turns` without the usage-limit pause
   row, `limit-expiry`, as the cost leaves it out; `costs.js`'s
   `avoidableRebuilds`, which Glossary › How costs work counts with
   too), and how many agent types a 1-hour lifetime would help (`ttl_break_even_share` rows with a
   positive `margin`, linking to Cache › Lifetime). Then
   `/api/recache`: stat cards for cache rebuilds by cause
   (expired while idle, invalidated by a change, expired during a
   usage-limit pause — `recache.SIGNATURES`), headed "all history"
   because the route takes no window, plus the `recache`/`limits`
   report sections for the chosen window; the `recache` section draws
   chart 6, the idle-gap histogram.
8. **Cache › Lifetime (TTL)** — `/api/ttl`, with chart 7 (net saving of
   a 1-hour lifetime per agent type) over its tables: per-agent-type observed/simulated cost, the
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
    toggle. The per-agent signals grid is a heat grid: each percentage
    cell is shaded against its column's largest value (`TINT_TABLES` in
    `grid.js`), with the value always shown.
11. **Agents & context › Context** — what Claude reads at the start of every
    session and subagent. **CLAUDE.md files** (`/api/claude-md`): a grid,
    one row per file with who reads it, its size in tokens, how often it
    was sent and to whom, the cost and its number of fixes; a row opens a
    drawer with its findings and `/api/claude-md/<id>`'s sections by
    size, duplicates, stale references and fix prompts. **Skills**
    (`/api/skills`): the listing's size and cost, then, when two or more
    skills are unused, one folded fix that hides them all, then a grid of
    skills (where each comes from, status, listing size, uses, listing
    cost) with a "Show only skills Claude never used" checkbox; a row
    opens a drawer with the skill's description, facts and its own fixes.
    File text and skill descriptions are read when
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
    then **Kinds of task**, and the section's other breakdowns (briefs,
    agents, effort, outcomes, Claude's own reports) folded under "More
    tables" (their `helptext.PLACEMENT` is `advanced`), then its notes
    (capture off, no feedback yet). Every item is a way of working to try: nothing on
    this page changes a setting.
13. **Setup › Settings** — what your changes did, then the settings
    themselves. **Your changes and what they did** (`/api/impact`, all
    history) comes first: each `apply`, undo or settings change the
    hook saw, with the sessions before against those after on the
    measures that change should move, then a "Quality, <agent>:" line
    per agent the change touched (or the main session) with a collapsed
    Signal / Before / After / Verdict table (agents with too few runs
    yet share one line), and, for an apply, "To undo it:
    `claude-token-lens apply --revert <backup_ts>`". A metrics capture
    change is measured by capture's tokens per session and the share of
    messages tagged, and its card gives the `capture level <old>` (or
    `capture off`) command that changes it back. A change marker on the
    daily spend chart opens this view with `?day=`, and that day's
    change is pulsed. **Did your estimates come true?**
    (`/api/backtest`) follows: each estimated effect Profiles showed,
    against what happened after a matching change. Then
    `/api/config-diff?auto_keys=1`: `effective_config`/`config_layers`/
    `config_groups`/`config_drift` and the per-key diff tables, rendered
    once (the config section is skipped when the view walks the full
    report for `baseline_comparison`) — which layer supplied each
    key, and which projects share an identical effective config. A
    snapshot with no project attribution (`project_slug: null`, see
    `docs/api.md`) is shown as a user-level layer rather than a project's.
    A "Latest baseline" section renders
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
    profile for one kind of task" adds a "Kind of task" picker (each by
    its plain name from the draft's `task_labels`; it reloads the draft
    with `task=`) and a note on what was found, and names the profile
    after the task ("Bug fix tasks"). Name it and save
    (`POST /api/profiles`). **Your profiles and the built-in
    ones** follows: one card per profile from `/api/profiles` (the
    catalogue's seven shipped profiles plus every user profile): name,
    "Built in" or "Yours", who it is for, and "Changes N settings: ..."
    listed by their plain labels (from `/api/profiles/<id>` and
    `/api/profile-schema`). The card matching the latest baseline's
    `suggested_profile_id` carries a "Suggested for you" badge. "Show what
    it changes" opens the profile in a drawer, from `/api/profiles/<id>/diff`,
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
    **Best setup for each kind of task** follows: the report's
    `habits_setups` table (a note and a link to Setup › Capture while
    nothing is tagged). "Make your own profile" is a form built from
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
    it is captured. Each group folds ("Main session (3 of 12 on)"), open
    only when one of its metrics needs a hook entry or an install, or
    carries a note that its status line won't show. A
    metric is a row: a checkbox, what it captures, its estimate against
    its actual cost and how much has been collected, then "Why it helps
    and what Claude writes" folded (why, the tag Claude writes, what it
    helps with). The feedback skill's row
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
    **Service health** (`/api/health`): the logon warning when it
    applies, then the status, version, last scan, the watcher's counts
    and any recent errors. Then
    any report section no other view claims (the fallback in
    `SECTION_PAGE_MAP`, below). Then `/api/diagnostics`: whether the
    snapshot hook and the statusline are working, then the parse-quality
    counters (`Diagnostics` dataclass fields), as one labelled table,
    each row with what it means (`helptext.diagnostics_table`) — same
    figures as the CLI report's Diagnostics section, so a user comparing
    the UI against a CLI run for the same window sees identical numbers.
17. **Glossary › Terms** — the `GLOSSARY` constant, now in `links.js` (so
    any view can link a term): each term the dashboard uses, in plain
    English. The README's glossary is the same list, word for word.
    Each entry is a `<div>` wrapping its `dt`/`dd` pair, so a link with
    `?term=<slug>` (`termLink`/`termSlug`) scrolls to it and pulses it
    the way an evidence link does (`pulseNode`). A term named in a How
    costs work card's own `terms` (`COST_CARDS`) also carries a "Why it
    matters" line: the same rule sentence the card states, from
    `costs.js`'s `cardRuleText` (one source read by both segments), with
    a link back to the card.
18. **Glossary › How costs work** — one card per `COST_CARDS` entry
    (`links.js`), in the order they're listed: cache reads, cache writes
    and lifetime (TTL), cache rebuilds, model choice, startup context,
    tool output kept, conversation summaries, and billing mode. Each
    card states the rule with the multiplier from your own pricing
    (`format.js`'s `fraction()`, via `costs.js`'s `priced()` — never a
    number typed into the page), your own figures for the window from
    `report.json`, and a link to the page or recommendation that acts on
    it; a card whose section has no data for the window says so in a
    line instead of a number. The cache rebuilds card counts the
    rebuilds its cost covers, as Cache › Rebuilds does
    (`avoidableRebuilds`), and the billing mode card says how amounts
    read: money, a share of the weekly limit, or list-price equivalents
    on a plan until usage-limit readings give a share
    (`units.share_per_usd` is `null`). A link with `?card=<slug>` (`cardLink`)
    scrolls to and pulses its card, the same way a term link does.

## Help and labels

The page title in the header is the one `h1` (the page's label from
`PAGES`), and each view opens with its one-line intro from `PAGES`
(`viewIntro`). Sections are `h2`, tables `h3`, and nothing goes deeper
than `h4`; a table shown on its own in a view it was moved to takes the
`h2` itself. The words come from `helptext.py` (house style:
`docs/writing-help.md`), applied to the report model by
`helptext.annotate` and rendered generically:

- **Section intro and "How to read this".** `Section.intro` as a line
  under the heading; `Section.help`/`Table.help` behind one (i) button
  beside the heading, a popover with "What it shows", "How to read it"
  and "When to act". A section whose figures rest on a price the
  Glossary explains (cache rebuilds, cache lifetime, model choice,
  startup context, tool output kept, conversation summaries, billing
  mode: `SECTION_CARDS` in `grid.js`) ends its popover with a link to
  that card in **Glossary › How costs work**.
- **Column help.** A `?` button in the header (a real `<button>` with
  `aria-expanded`, keyboard and touch operable, never a `title=`-only
  tooltip) opens that column's `Column.help` in a popover. It stops
  propagation so it never also sorts the column.
- **Value labels.** `Table.value_labels` replaces raw row values such
  as `top-level` with "Main session"; the raw value stays in the
  cell's `title` and `data-raw`.
- **Placement.** `Table.dashboard`: `keep` tables are shown,
  `advanced` tables go into one collapsed "More tables (N)" block per
  section, and `report` tables are left to the CLI report with a
  one-line note.

### Links in text, glossary terms and the actions a table feeds

- **Page links.** Server text may point at a page with a
  `{{page:<page>}}` or `{{page:<page>/<segment>}}` token
  (`docs/writing-help.md`, "Linking to another page"). `ui.js`'s
  `prose(text, seen)` renders it through `links.js`'s `linkText`: each
  token becomes a link to that view, named as the sidebar and the page
  title name it (a page with segments opens on its first). Every place
  the dashboard shows server text uses `prose()`: help, intros, notes,
  table and column help, callouts, empty states, recommendation and
  check detail, tips, habit cards, capture metrics, the setup list and
  a session's explanation. Where a link can't go (a tooltip, a toast, a
  grid cell, whose row opens its own detail) `plainText` gives the
  page's name instead, as `pages.plain()` does for the CLI. A token
  naming no page is left as written rather than dropped.
- **Glossary terms.** With a `seen` set, `prose()` also finds the words
  in `links.js`'s `JARGON` (each a `GLOSSARY` term, with the plural and
  other forms it goes by) and turns the first use of each in a card or
  section into a button styled as the word with a dotted underline. It
  opens the term's definition, the Glossary's own wording, with a link
  to its entry (`#/glossary/terms?term=<slug>`). Later uses in the same
  card or section stay plain words. Page intros, the Glossary itself,
  and popovers (help, column help) show no term buttons: a popover
  inside a popover would lose its place.
- **Feeds N actions.** `api.js`'s `actionIndex()` reads the window's
  recommendations (`loadRecommendations()`, one fetch per window shared
  by the Actions badge and inbox, the Overview and the grids) and
  indexes their evidence by report table and by row. A table that is
  evidence for a recommendation shows **Feeds N actions** beside its
  heading, and each row a recommendation cites carries a small mark at
  the end of its first cell; both open a list of those actions, each a
  link to its detail in Actions (`?id=<key>`). N counts inbox items: a
  rule that fires for several agent types is one action. A mark sits
  inside the row's line box, so a marked row is as tall as the rest.
- **Popovers close on a link.** Following any link inside a popover
  closes it, so it never floats over the view the link opens.

## Data flow

Every view's data comes from `fetch('/api/...')` returning the envelope
`docs/api.md` describes; `api.js`'s `fetchJson` unwraps `{"ok": true, "data": ...}`
and renders, or shows the `error.message` inline (never a raw stack
trace — the API never sends one, per its own `error.code`/`message`
contract) on `{"ok": false, ...}`. No view holds state the server
doesn't already have; a page reload is always safe.

## Performance

The budget, measured on Chrome at 1440px against a local service with
its report already built:

| Moment | Budget | Measured (median of 9 loads) |
|---|---|---|
| First contentful paint | under 300ms | 64ms (48ms to 220ms; the slowest is a cold browser) |
| Shell ready (`performance.mark("tl-shell-ready")` at the end of `init()`) | under 500ms | 177ms (at most 217ms) |
| A page or segment already drawn, to its first frame | under 100ms | 19ms to 35ms, the fade included |

Once the Overview's chart is drawn, `api.js`'s `prefetchActions` asks
for the recommendations and the checks (`/api/quick-actions`) while
the browser is idle (`requestIdleCallback`, at most 2 seconds; half a
second later where there's no idle callback). Both are cached per
window (`loadRecommendations`, `loadQuickActions`), so Actions opens
from them without a fetch. A new window or Redraw figures clears the
cache.

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
`docs/writing-help.md` ("Dashboard copy"). The linking tests check that
every `JARGON` word is a Glossary term, that the client's token pattern
is the server's, that each place server text shows goes through
`prose()` (and `plainText` where a link can't go), that no string the
dashboard shows says "tab", and that tables name the actions they feed. The chart tests hold `CHART_SPECS` to the catalogue above, check every chart's data names a real route or report table, and keep bars thin, colours tied to entities and money axes in step with the billing mode. `tests/test_ui_motion.py` holds the motion, forced colours and load budget above to their source: the View Transition's guards and timing, the count-up's final text, the stagger's cap, the reduced-motion and forced-colours rules, the shell-ready mark and the idle prefetch. There is no headless browser: `urllib.request` plus string checks is enough for a
stdlib-only test suite.

## Modules

| File | Holds |
|---|---|
| `app.js` | the entry point: the router (`resolveRoute`, `showView`, `VIEW_RENDERERS`), the sidebar, the page header, the project and window pickers (`menuControl`) and the theme toggle |
| `core.js` | `el`/`clear`, `localStorage` helpers, the shared `state`, `WINDOW_OPTIONS`, `renderedViews`, the `goTo` and `pickProject` hooks (`setRouteHandler`, `setProjectHandler`) and the linked-highlight bus (`highlight`, `listenHighlight`) |
| `format.js` | the one number format: `formatCell`, `money`/`moneyText`/`moneyNode`/`moneyParts` (the `Units.money` mirror), `currencyAmount`, `moneyUnit`, `readableAmounts`, `compactNumber`, `signedPercent`, `fraction` (a price ratio in words: "a tenth of"), `shortTs`/`relativeTime`, `projectName` |
| `api.js` | `fetchJson`, `loadInto`, `postJson`, `withWindow`/`withProject`, `scopeKey`, `loadReport`, `loadRecommendations` and `loadQuickActions` (each cached per window and project), `prefetchActions`, `loadProjects` (the project picker's list), the figures-as-of stamp, and the connection state behind "Service unreachable" |
| `ui.js` | the components (see "Components"): buttons, chips, tiles, panels, callouts, empty states, skeletons, command blocks and `RESTART_NOTE`, popovers, tooltips, drawers, toasts and the confirm dialog |
| `grid.js` | the data grid (`dataGrid`, with `link` and `swatch` for linked highlight), report tables (`renderTable`, `renderPlacedTables`), `renderMappedSections`, `simpleTable`, `pulseRow` |
| `charts.js` | the chart frame: `CHART_SPECS`, `fillSummary`, `ENTITY_COLOURS`/`entityColour`, axes, the tooltip, keyboard reading, the table view, resize, `drawChart`/`holdChart`/`chartError` |
| `charts-types.js` | the chart forms and `renderChart`, `sessionContextChart`, `savingsLevers`, and the micro-forms `sparkline`, `meter`, `habitSparkline` |
| `links.js` | `PAGES` (pages, segments, intros), `SECTION_PAGE_MAP`/`TABLE_PAGE_MAP`, `parseHash`/`formatHash`, `scopeParams` (the window and project every address carries), `viewIntro`, `pageLink`/`captureLink`, `GLOSSARY`/`termLink`/`termSlug`, `COST_CARDS`/`cardLink` |
| `costs.js` | the pricing helpers Actions and Glossary both need, from `report.meta.rates`: `pricingFacts`, `priced`, `modelSentence`, and `cardRuleText` (the rule sentence for each `COST_CARDS` concept — the one source Glossary's two segments both read) |
| `shell.js` | what is on every view: the health banner, the sidebar's status line, the capture banner; the health detail (`renderHealth`) and logon warning (`renderLogonNotice`) the Overview and Data quality show |
| `icons.js` | the icon set: `icon(name, opts)` returns an inline 16px SVG |
| `palette.js` | search (Ctrl+K: `openPalette`, `matchScore`) and the keyboard shortcuts (`GO_KEYS`, `showShortcuts`) |
| `d3.js` | the one door to the vendored d3 (`import d3 from "./d3.js"`) |
| `theme-boot.js` | a classic script, not a module: sets `data-theme` before the first paint |
| `page-overview.js` | Overview |
| `page-actions.js` | Actions › Recommendations and Checks |
| `page-spend.js` | Spend › Usage, Savings and Sessions (with the session drawer) |
| `page-cache.js` | Cache › Rebuilds and Lifetime (TTL) |
| `page-agents.js` | Agents & context › Subagents, Quality and Context |
| `page-habits.js` | Work habits |
| `page-setup.js` | Setup › Settings (with impact and backtest) and Profiles |
| `page-capture.js` | Setup › Capture |
| `page-data.js` | Data quality |
| `page-glossary.js` | Glossary › Terms and How costs work |

## Implementation notes (S1-ui)

The UI shipped in `static/` (`index.html`, `app.css` and the modules above) follows
this document's Constraints, Data flow and Testing sections, and the
Pages section above describes the shipped structure: seven main pages,
Data quality and the Glossary, eighteen views in all.

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
