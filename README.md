# claude-token-lens

claude-token-lens shows where your Claude Code tokens and money go, and
what to change to spend less. It reads the transcripts Claude Code
already keeps on your machine, works out what each session, subagent and
cache rebuild cost, and suggests setting changes with the trade-offs
spelled out. Nothing leaves your machine: no network calls, no
telemetry. It never changes your Claude Code settings on its own; every
change is a prompt you give Claude or a command you run.

## Quick start

About five minutes. You need Claude Code, already used for a while so
there are sessions to look at, and Python 3.11 or newer. The commands
below are for **Windows PowerShell**; on macOS or Linux, type them in
Terminal with `python3` in place of `python`.

### Where to install it

Anywhere: you don't install it into a repository, and it doesn't matter
which folder your terminal is in. Claude Code keeps a transcript of
every session, for every repository, in one folder
(`%USERPROFILE%\.claude\projects` on Windows, `~/.claude/projects`
elsewhere). claude-token-lens reads that folder, so the dashboard shows
all your repositories at once. Only two commands care where you run
them: `init` names the repository you're in as "this project", and
`report` and `check` look at just that repository unless you add
`--all-projects`. Also run Claude Code inside WSL? Still install it on
Windows; see [Using Claude Code in WSL too](#using-claude-code-in-wsl-too).

### 1. Check Python

Open PowerShell and run:

```powershell
python --version
```

It should print `Python 3.11` or higher. If it says `python` isn't
recognized, or shows an older version, install Python from
[python.org](https://www.python.org/downloads/), tick **Add python.exe
to PATH** in the installer, then open a new PowerShell window.

### 2. Install

```powershell
python -m pip install git+https://github.com/PaulMorrisDev/claude-token-lens
python -m claude_token_lens --version
```

The second line should print `claude-token-lens 0.5.2` or later.

This guide always runs the tool as `python -m claude_token_lens`. The
shorter `claude-token-lens` works too, but only when pip's Scripts
folder is on your `PATH`, which it often isn't (pip prints a yellow
WARNING when it isn't).

- **No git on this machine?** Install from the zip instead:
  `python -m pip install https://github.com/PaulMorrisDev/claude-token-lens/archive/refs/heads/main.zip`
- **Can't use pip at all** (a locked-down work machine)? Download
  [`claude-token-lens.pyz`](https://github.com/PaulMorrisDev/claude-token-lens/releases/latest/download/claude-token-lens.pyz)
  and run `python claude-token-lens.pyz` wherever this guide says
  `python -m claude_token_lens`. [`docs/first-run.md`](docs/first-run.md)
  covers this route step by step.

### 3. Set up

```powershell
python -m claude_token_lens init
```

It asks a few questions. The first matters most: **how you pay for
Claude Code**. Type `subscription` for a Pro, Max, Team or Enterprise
plan, or `api` if you pay per token with an API key. For the rest,
pressing Enter accepts the default, which suits most people. It then
offers these, and asks before each:

- **Connect to Claude Code.** It adds a small hook to your Claude Code
  `settings.json` that records your settings when a session starts, so
  the dashboard can show what changed and what that did. It shows you
  the exact change first and backs the file up.
- **Include your WSL sessions** (only if you run Claude Code inside
  WSL too). It finds them itself; say yes.
- **Start the dashboard when you log on.** Say yes. It starts straight
  away, and again every time you log on. Claude Code deletes old
  transcripts after a while (30 days by default), and the dashboard
  keeps their figures only if it is running.

### 4. Open the dashboard

Go to **http://127.0.0.1:8765** in your browser. The first visit can take
a minute while it reads your history. Start with **Start here** on the
Overview tab, then **Quick actions**, which answers one question per way
of saving, such as "Is each agent on the cheapest model that does the
job?". The footer shows the version that is running.

It only runs on your machine; nobody else can open it.

### Just want a quick look?

You can skip `init` and the dashboard:

```powershell
python -m claude_token_lens check --all-projects
python -m claude_token_lens report --all-projects
```

`check` answers the quick-action questions, and `report` prints the full
analysis. Both print to the terminal and change nothing.

### Updating

```powershell
python -m claude_token_lens update
```

That one command installs the newest version, restarts the dashboard on
it, and checks that the dashboard answering on port 8765 is the new one.
If an older copy is still holding the port, it says so; see
[An old dashboard won't go away](#an-old-dashboard-wont-go-away). Add
`--dry-run` to see the commands it would run first. On macOS, restart the
dashboard afterwards with
`launchctl kickstart -k gui/$(id -u)/com.claude-token-lens`.

**On version 0.4 or older** (`update` says it's an invalid choice), run
the two steps it replaces once; after that, `update` works:

```powershell
python -m pip install --force-reinstall git+https://github.com/PaulMorrisDev/claude-token-lens
python -m claude_token_lens install-service
```

Check the dashboard's footer shows the new version. After an update it
may re-read your history once, so the first page load can be slow.
[`CHANGELOG.md`](CHANGELOG.md) lists what changed.

### Using Claude Code in WSL too

If you also run Claude Code inside WSL (Ubuntu on Windows), its sessions
are kept inside Linux, in `\\wsl.localhost\<distro>\home\<you>\.claude\projects`.
Install claude-token-lens on **Windows** as above, not inside WSL, and
run `python -m claude_token_lens init`: it finds those folders itself
and asks whether to include them. Say yes, and the dashboard shows your
Windows and WSL sessions together, with a **Where** column on the
Sessions tab saying which is which ("This computer" or "WSL: Ubuntu").

- It only reads those folders, the same as your Windows one. It changes
  nothing inside WSL.
- While the dashboard runs, it looks in them every 30 seconds, which
  keeps WSL running in the background. If WSL is shut down, the
  dashboard carries on with what it already has and picks the rest up
  when WSL is back.
- The "Connect to Claude Code" hook is for Claude Code on Windows. The
  copy of Claude Code inside WSL has its own settings and doesn't need
  it.
- Added a WSL distro later? Run `init` again. To list the folders by
  hand, put them in `config.toml` (in `%USERPROFILE%\.claude\token-lens`)
  and restart the dashboard with `python -m claude_token_lens install-service`:

  ```toml
  extra_projects_roots = ['\\wsl.localhost\Ubuntu\home\alice\.claude\projects']
  ```

  A one-off command can take several folders too:
  `python -m claude_token_lens report --all-projects --projects-root <folder> --projects-root <another>`.

### If something goes wrong

| What you see | What to do |
|---|---|
| `claude-token-lens` "is not recognized as a name of a cmdlet" or "command not found" | pip's Scripts folder isn't on your `PATH`. Use `python -m claude_token_lens` instead; everything else stays the same |
| The dashboard still looks old after updating (its footer shows an old version, or has no version at all) | Something else is still serving port 8765, such as an older copy started by hand, from another Python install, or from Docker. See [An old dashboard won't go away](#an-old-dashboard-wont-go-away) |
| http://127.0.0.1:8765 doesn't open | The first start reads your whole history, which can take a minute. If it still doesn't open, run `python -m claude_token_lens serve` in a PowerShell window and leave it open; any error prints there |
| Sessions you ran in WSL are missing | Run `python -m claude_token_lens init` again and say yes when it offers the WSL folder. It only finds a distro that is installed for your Windows user; `wsl -l -v` lists them. See [Using Claude Code in WSL too](#using-claude-code-in-wsl-too) |
| Amounts are in dollars but you're on a plan | Run `python -m claude_token_lens init` again and answer `subscription` to "How do you pay for Claude Code?" |

[`docs/first-run.md`](docs/first-run.md#troubleshooting) has more.

#### An old dashboard won't go away

Only one program can use port 8765. If an old copy holds it, the new one
can't start, and your browser keeps showing the old one. See what is
using the port:

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen | ForEach-Object { Get-Process -Id $_.OwningProcess } | Format-Table Id, ProcessName, Path
```

- **`python`, `pythonw` or `py`:** an old copy. Stop it, then start the
  new one:

  ```powershell
  Get-NetTCPConnection -LocalPort 8765 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
  python -m claude_token_lens install-service
  ```

- **Anything with `docker` in its name:** the old Docker setup. Run
  `docker compose down` in the folder you started it from (or stop the
  container in Docker Desktop), then run
  `python -m claude_token_lens install-service`.

Then reload http://127.0.0.1:8765 and check the version in the footer.

### Uninstalling

Look at what would be removed first:

```powershell
python -m claude_token_lens uninstall --revert-changes --delete-data --dry-run
```

Then run the same command without `--dry-run`. It removes the hook and
the logon service, undoes any setting changes you made through this
tool, and deletes its data, asking before each step. Finally:

```powershell
python -m pip uninstall claude-token-lens
```

## What it does to Claude Code, and how to undo it

- **It never uses your Claude tokens.** It reads files Claude Code has
  already written; it never calls Claude or any other service.
- **It changes nothing on its own.** `init` offers two optional
  additions, each shown and confirmed first: a SessionStart hook that
  copies your settings into a local snapshot (well under a second per
  session, in the background; adds no tokens to the conversation) and a statusline command
  that logs usage-limit readings (terminal only; adds no tokens).
- **Settings change only when you say so**, through a prompt you give
  Claude or `claude-token-lens apply`, which backs the file up first and
  prints the command that undoes it. A change takes effect in the next
  session you start, not the one you have open.
- **Cheaper isn't free.** A cheaper model, lower effort or an earlier
  summary can make Claude less thorough. Each change says what it trades
  away. Try one change at a time and check **Profiles > Your changes and
  what they did** after a few sessions.

To see everything it installed, run `claude-token-lens changes` or open
the Data quality tab; both say how to undo each item. To remove it
completely, see [Uninstalling](#uninstalling).

## What each tab answers

| Tab | The question it answers |
|---|---|
| Overview | How much did I use, and what should I look at first? |
| Quick actions | For each way of saving (models, effort, summaries, cache, tools, skills, CLAUDE.md, tool output, habits), and whether any agent is struggling: is there anything to do, and what exactly? |
| Sessions | Which sessions cost the most? Pick one to see why it was expensive. |
| Cache | When did Claude Code rebuild the prompt cache, and what caused it? |
| Cache lifetime (TTL) | Would a 1-hour cache lifetime have paid for itself? |
| Savings | What would shorter tool output, earlier summaries, cheaper models or fewer wasted replies save? |
| Agents | What do my subagents cost, what are they given when they start, what do they send back, and is their work going well (failed tool calls, runs that don't finish, per model and effort)? |
| Context files | What does each CLAUDE.md file and skill cost, who is it sent to, and what can be trimmed, moved or hidden? |
| Config | What are my settings, and did changing them change my costs? |
| Profiles | Make a profile from a goal with an estimate of what it saves, compare it with my settings, and see what each change I made did. |
| Recommendations | What exactly should I change, where, and what is the trade-off? |
| Usage | How is my usage spread over days, projects and five-hour blocks? |
| Data quality | What did this tool install, what should I expect, and could every transcript be read and priced? |
| Glossary | What does a term on the dashboard mean? |

The window picker at the top sets the time every tab covers: the last
hour, today or the last 24 hours to see the effect of a change straight
away, 7 to 90 days or all time for the long view, or **since my last
change**.

Amounts follow your billing mode. On a Pro or Max plan, savings are a
share of your usage limits once your statusline has logged enough
usage-limit readings, and list-price equivalents (what the tokens would
cost at Anthropic's published prices) until then. On pay-per-token
billing they are what you pay.

## Acting on a recommendation

Each recommendation card explains the change before offering it: what
the setting controls, its value now and after, which file it is written
to and who that affects, the expected effect, the trade-off, and how to
undo it. Then it gives you two ways to make the change:

- **Ask Claude to do it.** Copy the prompt into Claude Code. It names
  the file, the setting and the value, says why, and asks Claude to
  restate the change and show you the diff before saving. Claude Code
  asks your permission before editing files under `.claude`; that is
  expected.
- **Or run the command.** For a plain setting the card shows a command
  such as:

  ```bash
  claude-token-lens apply --set effortLevel=medium --scope user --dry-run
  ```

  `--dry-run` explains the change and prints the diff without writing
  anything. Run it again without `--dry-run` to make the change: the
  file is backed up first, and the output ends with the exact
  `claude-token-lens apply --revert <TS>` command that undoes it. A
  revert refuses to run if the file was edited after the change (so it
  can't throw away your later edits) unless you add `--ignore-changes`.

Profiles on the Profiles tab work the same way, for several settings at
once: a prompt, `claude-token-lens apply <profile> --dry-run`, or a
one-session trial (`apply <profile> --launch`) that leaves your settings
files alone. See [section 11](#11-applying-a-profile)
and [`docs/profiles.md`](docs/profiles.md).

## Glossary

- **Session**: One conversation with Claude Code, from start to exit. Resuming it continues the same session.
- **Main session**: The conversation you type into, as opposed to the subagents it starts.
- **Subagent**: A separate Claude that your session starts for one task, such as a search or a review. It has its own context and reports back when done.
- **Transcript**: The log file Claude Code writes for a session or a subagent run. Everything here is read from these files on your machine.
- **Reply**: One response from Claude, including any tool calls it makes. Every reply is billed for the whole context it reads.
- **Token**: The unit models read and write, roughly three quarters of a word. Prices are per million tokens.
- **Context**: Everything Claude reads on a reply: system prompt, tools, CLAUDE.md files and the conversation so far.
- **Startup context**: What Claude reads before your first message, or before a subagent's task: system prompt, tool list, CLAUDE.md files, skills and more.
- **Prompt cache**: A copy of the start of the context kept on Anthropic's side, so the next reply can re-read it cheaply instead of paying full price.
- **Cache read**: Re-reading context from the prompt cache. About a tenth of the normal input price.
- **Cache write**: Putting context into the prompt cache. Costs more than normal input: 1.25 times for a 5-minute lifetime, 2 times for 1 hour.
- **Cache rebuild**: Writing context to the cache again because the cached copy expired or something early in the conversation changed.
- **Cache lifetime (TTL)**: How long the prompt cache stays warm after a reply: 5 minutes by default, or 1 hour. A pause longer than this means a rebuild.
- **Conversation summary**: When the context gets too large, Claude Code replaces the conversation so far with a summary. Also called compaction.
- **List price**: Anthropic's published price per token. On a Pro or Max plan you don't pay this; it is shown to compare costs.
- **Usage limits**: On a Pro or Max plan, the share of your five-hour and weekly allowance you have used.
- **Billing mode**: Whether amounts are shown for a Pro or Max plan (a share of your usage limits when there are enough readings, otherwise a list-price equivalent) or as money for pay-per-token billing.
- **Effort level**: How hard Claude thinks before replying. Thinking is billed as output, the most expensive token type.
- **Scorecard**: Five areas rated 1 (very poor) to 5 (excellent), each from one number in your data.
- **Recommendation**: A change worth making, with what it changes, the trade-off, a prompt you can give Claude and a command you can run.
- **Profile**: A named group of settings you can compare with yours, try for one session, or apply.
- **Scope**: Where a change is written: your user settings (every project), this project on your machine only, or this project for everyone.
- **Managed setting**: A setting your organisation's policy controls. Only your administrator can change it.
- **Snapshot**: A record of your Claude Code settings at one moment, taken so changes can be compared over time.
- **Window**: The stretch of time the numbers cover, picked at the top of the dashboard: the last hour, today, the last 24 hours, 7, 30 or 90 days, all time, or since your last change. A session counts, in full, when it was last active in the window.
- **Change point**: A moment your settings changed: an apply, its undo, or a change the settings snapshot saw. The dashboard compares the sessions before it with those after it.
- **Quick action**: One question about a way to spend less, such as whether a cheaper model would do for an agent, answered from your own sessions with the evidence and a fix you can copy.
- **What-if estimate**: What a change would have saved over the window, worked out from your own sessions. It is an estimate: cheaper settings can change how Claude works, which the estimate can't see.
- **CLAUDE.md**: Instruction files Claude reads at the start of every session, and of most subagents: yours, each project's, and rule files. Every line is paid for on every reply that re-reads it.
- **Skill**: A packaged set of instructions Claude can load when a task needs it. Its name and description are listed to Claude at the start of every session, used or not.
- **Quality signal**: A sign of whether the work went well, not just what it cost: tool calls that failed, agent runs that didn't finish, your corrections. Compared across models and efforts, and before and after each change you make.

## Reference

The rest of this file is reference material. The concepts behind the
numbers (the two token totals, how Claude Code's prompt cache works,
what counts as a cache rebuild, and what the cache-lifetime simulation
assumes) are in [`docs/concepts.md`](docs/concepts.md).

## 1. What it is, what it measures, and what it cannot

### Status

**Version 0.5, pre-release.** Every command in the table below works
and is covered by tests. This README describes what the code does
today; the roadmap in [section 9](#9-licence-contributing-roadmap) lists
what is still missing, and
[`docs/sections-reference.md`](docs/sections-reference.md) describes
each report section in detail.

### What it reads, and what it cannot do

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
  other, not a real invoice. `ReportMeta.billing_mode` and
  `Config.billing` model `"api"` vs `"subscription"` in
  [`model.py`](src/claude_token_lens/model.py) and
  [`config.py`](src/claude_token_lens/config.py), and the report is
  billing-mode aware: the Usage section's "Usage by day/week/month"
  tables label subscription money columns as list-price-equivalent, and
  its five-hour usage blocks table is only populated for
  `billing = "subscription"` — under `"api"` it prints a one-line note
  explaining the skip instead (see [section 3](#3-reading-the-report-sections)).
  `billing` in `config.toml` is `"auto"` by default: subscription once
  the usage log holds a usage-limit reading (Claude Code only reports
  usage limits to Pro and Max plans), `"api"` otherwise. Set
  `billing = "subscription"` or `"api"` to choose yourself. The report
  header and the dashboard's Overview say which mode is in use and why.
- **The JSONL format is observed, not a published API.** Every field
  name this tool reads was found by inspecting real transcripts, not
  from documentation, and Claude Code's own docs describe the transcript
  format as internal and unstable. The parser tolerates unknown line
  types and fields rather than failing on them (see
  [Windows notes](#5-windows-notes) and `Diagnostics.ignored_line_types`),
  but a future Claude Code release can still change field names under
  it. Two integration points *are* documented and stable, and are the
  better choice if you need a durable contract instead of a
  best-effort parser: Claude Code's OpenTelemetry metrics
  (`CLAUDE_CODE_ENABLE_TELEMETRY=1`, metrics `claude_code.token.usage`
  and `claude_code.cost.usage`), and `claude -p --output-format json`
  for scripted single-shot invocations.

## 2. Installing and first run

The [Quick start](#quick-start) covers the usual route. For a work
machine without `pip`, `git` or network access, or to install from a
local copy of the code, follow [`docs/first-run.md`](docs/first-run.md).
Every route in short:

| Route | Command | Needs |
|---|---|---|
| From GitHub | `python -m pip install git+https://github.com/PaulMorrisDev/claude-token-lens` | pip, git and network |
| From GitHub, without git | `python -m pip install https://github.com/PaulMorrisDev/claude-token-lens/archive/refs/heads/main.zip` | pip and network |
| From a local copy | `python -m pip install <folder>` (or `pipx install <folder>` to keep it apart from other Python tools) | pip |
| Single file | download `claude-token-lens.pyz` from the [latest release](https://github.com/PaulMorrisDev/claude-token-lens/releases/latest), then `python claude-token-lens.pyz <command>` | Python only |

The package has no third-party dependencies; `rich` is an optional
extra for nicer terminal output. Building the `.pyz` yourself is
covered in [`docs/deploy.md`](docs/deploy.md#distribution-without-pip-the-pyz-build).

### What `init` does

`claude-token-lens init` detects what is already set up (an existing
config, settings snapshots, a usage log), then asks a few questions it
can't work out itself: your billing mode, projects to always leave out,
whether you start Claude Code with extra settings files, your time
zone, where `apply` writes by default, and how long the first
"capture window" should run (7 days by default). It then:

1. writes `config.toml` into its own folder (`~/.claude/token-lens`);
2. shows the change that connects the SessionStart hook (and a
   statusline, if you have none) to Claude Code's `settings.json`, and
   makes it only after you say yes (`--no-install` skips this);
3. records a first baseline for the current project;
4. offers to start the dashboard at every logon (`--no-service` skips
   this, `--install-service` says yes up front).

Running `init` again is safe: it keeps your existing capture window and
shows any change before making it. `init --repair-hook` fixes a hook
command that stopped working. For scripts or CI:

```bash
claude-token-lens init --non-interactive --no-install
```

Unanswered questions are then worked out from what `init` found, and it
prints what it chose and why. [`docs/onboarding.md`](docs/onboarding.md)
lists every question and the baseline's fields.

The logon service matters because Claude Code deletes its own
transcripts after `cleanupPeriodDays`; only a dashboard that is running
keeps their figures. [Section 10](#10-running-the-service) and
[`docs/deploy.md`](docs/deploy.md) describe what it registers on each
system (`install-service --dry-run` previews it, `uninstall-service`
removes it).

When the capture window has enough data (or straight away with
`--finalise`):

```bash
claude-token-lens baseline
```

suggests a workstyle profile, a caching saving and a few next steps.
`baseline --list` and `baseline --show ID` read back earlier baselines.

`claude-token-lens report` works without any of this setup.

### Subcommands

Generated against this branch's `--help` output (`claude-token-lens
<subcommand> --help` for the authoritative, always-current list). Every
subcommand also accepts the [global flags](#global-flags-clipy) below;
this table only lists what's specific to each one.

| Subcommand | What it does | Extra flags |
| --- | --- | --- |
| `report` | Full report: every section in [section 3](#3-reading-the-report-sections) (`overview`, `usage`, `sessions`, `recache`, `ttl`, `limits`, `carry`, `compaction_sim`, `model_swap`, `waste`, `compactions`, `agents`, `quality`, `workstyle`, `workflows`, `config` when snapshots exist, `scorecard`, `recommendations`), printed as Markdown by default. This is the default subcommand — `claude-token-lens` with no arguments runs it. | `--json` (print the whole report as JSON instead), `--html PATH` (also write a single-file HTML report), `--csv-dir DIR` (also write one CSV per table plus an index), `--phases` (add the DISCOVERY/IMPLEMENTATION/VERIFICATION phase-split section), `--patch-set` (also print the recommendation set as unified-diff-style settings/frontmatter patches), `--explain` (add each section's and table's "what it shows / how to read it" help to the Markdown), `--baseline ID\|latest` (add a comparison against a saved `baseline` record) |
| `sessions` | Focused view: just `overview` + `sessions` | Same output flags as `report` except `--patch-set` (recommendations aren't part of a focused view) |
| `recache` | Focused view: just `overview` + `recache` | Same as `sessions` |
| `ttl` | Focused view: just `overview` + `ttl` | Same as `sessions` |
| `limits` | Focused view: just `overview` + `limits` (usage-cap pauses, agent terminations, resumes — see [`docs/limits.md`](docs/limits.md)) | Same as `sessions` |
| `carry` | Focused view: just `overview` + `carry` (cost of re-reading/re-writing a tool result on every later turn it keeps riding along in the cached prefix — see [`docs/carry.md`](docs/carry.md)) | Same as `sessions` |
| `compaction-sim` | Focused view: just `overview` + `compaction_sim` (modelled cost under other `autoCompactWindow` settings — see [`docs/compaction-sim.md`](docs/compaction-sim.md)) | Same as `sessions` |
| `model-swap` | Focused view: just `overview` + `model_swap` (ceiling saving from moving a model/subagent type one tier down — see [`docs/model-swap.md`](docs/model-swap.md)) | Same as `sessions` |
| `waste` | Focused view: just `overview` + `waste` (spend on turns whose output was never used — see [`docs/waste.md`](docs/waste.md)) | Same as `sessions` |
| `compactions` | Focused view: just `overview` + `compactions` | Same as `sessions` |
| `quality` | Focused view: just `overview` + `quality` (failed tool calls, agent runs that didn't finish, corrections, and each agent's model and effort compared with the one it used most — see [`docs/concepts.md`](docs/concepts.md#7-quality-signals)) | Same as `sessions` |
| `config-diff` | Compare sessions grouped by one (or every changed) config key's value, from captured `snapshot-config` snapshots. Prints its own plain-text table(s), independent of `report`'s renderers. | `--key KEY` **or** `--auto-keys` (mutually exclusive, one required): diff one named flattened config key, or every key that changed across the available snapshots |
| `snapshot-config` | Capture (or print/install) the SessionStart config-snapshot hook — see [section 4](#4-installing-the-sessionstart-hook-and-the-statusline) | `--print-hook` (print the settings.json fragment), `--install-hook` (copy the hook script into `<config-dir>/hooks/`), `--managed-path PATH` (override the platform managed-settings.json path), `--project-dir PATH` (take the snapshot for this project directory instead of the current one), `--min-interval SECONDS` (skip the write when an identical snapshot is younger than this; default 300) |
| `probe-config` | Scan a project's config layers directly from the filesystem, without needing a captured session — the same layered-config view `snapshot-config` captures, on demand (schema 2) | `--project-dir PATH` (project directory to scan; default: the current directory), `--managed-path PATH` (override the platform managed-settings.json path) |
| `log-usage` | Read a pasted `get_usage` JSON payload from stdin and append its rows to the local usage-window CSV log | none beyond the global flags |
| `pricing-check` | Print the resolved rate card's provenance and rate table, and (with `--models`) how specific model ids resolve against it — flagging a resolution that only landed on the closest registered id, not that model's own rate, as `(closest match, not this model's own rate)` | `--models ID,ID,...` |
| `scrub-fixture` | Turn a real `<project_dir>/<session_id>` directory into a privacy-scrubbed test fixture, or verify an already-scrubbed one | `--session-dir PATH --out PATH` (scrub), or `--verify OUT_DIR` (audit an existing scrub), plus optional `--key-seed SEED` (deterministic HMAC key — tests only) |
| `probe` | Content-free schema histogram (line types, key names, attachment types, `version` values — every string capped at 64 chars) of a project or one transcript file, safe to paste into a bug report | `--file PATH` (probe a single transcript file instead of a project) |
| `statusline` | Claude Code `statusLine` handler — reads a JSON payload from stdin on every refresh (see [section 4](#4-installing-the-sessionstart-hook-and-the-statusline)) | `--print-install-fragment` / `--install` (print the settings.json fragment instead of reading stdin) |
| `export` | Aggregate, privacy-safe export of a corpus for BI/observability tooling (see [section 6](#6-for-team-leads-and-enterprise) and [`docs/exports.md`](docs/exports.md)) | `--format {csv-flat,json,otel-jsonl}` (default `csv-flat`), `--aggregate-only` / `--per-session` (mutually exclusive, default `--aggregate-only`), `--hash-slugs` / `--no-hash-slugs` (mutually exclusive, default hashed in every mode), `--out PATH` (default: stdout), `--generated-at ISO8601` (fix the `json` export's timestamp so reruns are byte-identical), `--aggregate` (write a team-aggregate JSON document for `import`/`team-report` instead), `--include-projects` (with `--aggregate`: add hashed project slugs) |
| `monthly-report` | Write a habit-forming finance summary (cost/tokens by model/project/entrypoint, five-hour blocks under subscription billing) plus the `usage` section for one calendar month, as both Markdown and HTML (see [section 6](#6-for-team-leads-and-enterprise) and [`docs/exports.md`](docs/exports.md)) | `--out DIR` (required), `--month YYYY-MM` (default: the previous calendar month), `--generated-at ISO8601` (fix the trailing "Generated at" line so reruns are byte-identical) |
| `import` | Validate and copy one or more `export --aggregate` team documents into `<config_dir>/team/` for `team-report` (see [section 6](#6-for-team-leads-and-enterprise) and [`docs/team.md`](docs/team.md)) | `FILE...` (one or more team-document paths); exits 2 with the reason on the first invalid file |
| `team-report` | Cross-machine per-archetype/per-agent-type comparison built from every document already imported into `<config_dir>/team/` (see [section 6](#6-for-team-leads-and-enterprise) and [`docs/team.md`](docs/team.md)) | `--min-sessions N` (default 5), plus the same `--json`/`--html PATH`/`--csv-dir DIR` output flags as `report` |
| `init` | Detect what's already set up, ask (or, non-interactively, derive) a short question set, write `config.toml` and this project's `projects/<slug>.toml`, print the hook/statusline install fragments, run an initial onboarding baseline, show the exact `settings.json` change that connects the snapshot hook (and a statusline when you have none) and make it only after you say yes, and — as its last step — offer to register the service to run at logon (`docs/deploy.md`) — see [`docs/onboarding.md`](docs/onboarding.md) | `--claude-root PATH` (Claude Code's folder holding `settings.json`; default `$CLAUDE_CONFIG_DIR`, else `~/.claude`), `--answers FILE` (JSON file supplying any subset of the answers), `--non-interactive` (derive unanswered questions instead of prompting; derives to *not* installing the service unless `--install-service` is also given), `--no-install` (skip connecting to Claude Code), `--connect` (make the `settings.json` change without asking; it is still shown, and the file backed up first), `--install-service` (register the service without asking), `--no-service` (skip the logon-service step entirely), `--dry-run` (show the `settings.json` change and the logon-service plan without making either; `config.toml` and the baseline are still written), `--repair-hook` (fix a broken SessionStart hook command without asking: a path a single backslash in JSON broke, an interpreter that can't be found such as a missing `py` launcher, or a `%VARIABLE%` Git Bash doesn't expand; it keeps your own Python when it's found and writes the folder out in full; `settings.json` is backed up first) |
| `baseline` | Capture (or list/show) an onboarding baseline: mode mix, dominant purposes, suggested profile, projected saving — see [`docs/onboarding.md`](docs/onboarding.md) | `--finalise` (treat the baseline as final even if the capture window hasn't elapsed), `--list` (list saved baselines), `--show ID` (print a previously saved baseline's report) |
| `apply` | Apply a catalogue or custom profile's settings/agent/env levers to a project or your user config, with backup/`--revert` — see [section 11](#11-applying-a-profile) and [`docs/profiles.md`](docs/profiles.md) | `PROFILE` (catalogue id or path to a profile TOML file), or `--set KEY=VALUE` (repeatable; one allowlisted setting, no profile needed) with `--agent NAME` for agent frontmatter, `--scope {user,project-local,repo}` (default `user`, or `project-local` once `--project-dir` is given), `--project-dir PATH`, `--claude-root PATH` (default: `$CLAUDE_CONFIG_DIR`, else `~/.claude`), `--dry-run`, `--launch` (one-session overlay instead of a persisted apply), `--allow-tracked`, `--force` (create a missing agent file from scratch), `--revert TS`, `--ignore-changes` (with `--revert`), `--list-backups` |
| `serve` | Run the local JSON API + watcher service (`service/serve.py`) — see [`docs/api.md`](docs/api.md) and [`docs/ui.md`](docs/ui.md) | `--port N` (default 8765), `--bind ADDRESS` (default `127.0.0.1`, loopback only), `--allow-remote` (allow `--bind` to a non-loopback address, refused by default), `--poll-interval SECONDS` (watcher poll interval, default 30), `--retention-days N` (prune sessions older than N days on every poll tick; default: `config.toml`'s `retention_days`, else keep forever), `--exclude-project SLUG` (repeatable; project slug never scanned), `--billing-mode {api,subscription}` (stamped onto every session; default: `config.toml`'s `billing`, resolved as for `report`), `--allowed-host NAME` (repeatable; an extra host name the dashboard answers to — every other `Host` header gets `403`, see [`docs/api.md`](docs/api.md#host-allowlist-dns-rebinding)), `--monthly-report DIR` (while serving, write the previous calendar month's report — the same files `monthly-report --out DIR` writes, covering every scanned project — into `DIR` when it is missing; checked at startup and hourly on a background thread, and once under `--once`; failures are logged and retried at the next check), `--once` (run a single watcher tick, print its stats, and exit instead of serving), `--purge --yes` (delete `<config-dir>/service.db` and its WAL/SHM sidecars, then exit) |
| `install-service` | Register `claude-token-lens serve` to run at logon for the current platform (Windows Scheduled Task, systemd user unit, or macOS LaunchAgent) — this is what `init`'s last step, and the manual paths in [section 10](#10-running-the-service), both call — see [`docs/deploy.md`](docs/deploy.md) | `--port N` (default 8765), `--bind ADDRESS` (default `127.0.0.1`), `--dry-run` (print exactly what would be written/run, without writing or running anything) |
| `changes` | List everything this tool has installed or changed on this machine, what each costs in tokens, what to expect, and the command that undoes each | `--claude-root PATH` (as for `init`) |
| `uninstall` | Take it back out: remove the SessionStart hook and statusline from `settings.json` (diff shown, file backed up first) and the logon service | `--claude-root PATH` (as for `init`), `--revert-changes` (also undo every `apply` still in place, newest first), `--delete-data` (also delete the data folder), `--dry-run` (show every step without changing anything), `--yes` (make the changes without asking; they are still printed) |
| `check` | Quick actions: answer one token question (or all of them) from your own sessions, with the evidence, fixes and tips — the Quick actions tab in the terminal | `ID` (optional: `models`, `effort`, `compaction`, `cache`, `tools`, `skills`, `claude-md`, `tool-output`, `habits` or `quality`), plus the global `--days`/`--since`/`--until` |
| `review` | Review your CLAUDE.md files or skills: size, how often each is sent, cost, and fixes — the Context files tab in the terminal | `claude-md` or `skills`, plus the global window flags |
| `update` | Install the newest version with pip, then, when the dashboard starts at logon, run the new copy's `install-service` to restart it on that version and check which version answers on the port | `--from SOURCE` (what pip installs from; default the GitHub repository, a local folder also works), `--no-service` (install but leave the dashboard alone), `--port`, `--bind`, `--dry-run` (print both commands without running either) |
| `uninstall-service` | Remove whatever `install-service` (or `init`) registered — stops the dashboard it is running (`Stop-ScheduledTask` on Windows; `systemctl --user disable --now` and `launchctl bootout` stop it on Linux and macOS), then deletes the task/unit/agent definition it wrote, and says which steps it did | `--dry-run` (print what would be removed, without removing anything) |
| `compare` | A/B compare two arms of sessions (`window:`/`key:`/`profile:`/`project:` specs), stratified by purpose/mode with a minimum-sample gate — see [`docs/compare.md`](docs/compare.md) | `--a SPEC` / `--b SPEC` (required), `--stratify purpose,mode` (default), `--min-sessions N` (default: `config.toml`'s `min_sessions`), plus the same `--json`/`--html PATH`/`--csv-dir DIR` output flags as `report` |
| `reconcile` | Compare local usage/cost accounting against an Admin API CSV export, entirely offline — see [`docs/compare.md`](docs/compare.md) | `--admin-csv FILE` (required), `--by {day,model,"day,model"}` (default `day`), plus the same `--json`/`--html PATH`/`--csv-dir DIR` output flags as `report` (the window comes from the global `--days`/`--since`/`--until` flags, not a separate flag) |

`usage`, `agents`, `workstyle`, `workflows` and `scorecard` are real
report sections (see [section 3](#3-reading-the-report-sections)) but
don't have their own focused subcommand the way `sessions`, `recache`,
`ttl`, `limits`, `carry`, `compaction-sim`, `model-swap`, `waste` and
`compactions` do. Get them via `report` (or `report --json` and pull out
that section).

### Exit codes

Every subcommand uses the same three codes:

| Code | Meaning |
| --- | --- |
| `0` | Ok — the subcommand ran and printed its output. |
| `1` | No data — an empty corpus for the given projects/window, or (for `config-diff`) no config snapshots found. Always paired with a one-line reason on stderr naming the projects root and window. |
| `2` | Bad input — a `ConfigError`/`PricingError` (e.g. an unreadable `--pricing` file), a bad flag combination argparse itself doesn't already catch, or an unrecognised subcommand (argparse's own `choices` list rejects it before `main()`'s own "not implemented" fallback would ever run — every subcommand it allows is a real, implemented one today). |

### Global flags (`cli.py`)

These are the flags every subcommand parses (the per-subcommand table
above lists what each one adds on top):

| Flag | Meaning |
|---|---|
| `--projects-root PATH` | override the `<projects_root>` directory (default: `~/.claude/projects`, or `$CLAUDE_CONFIG_DIR/projects`). Repeatable, to read several folders at once. `config.toml`'s `extra_projects_roots` (such as a WSL distro's folder, which `init` offers) are always read as well |
| `--project NAME` | repeatable; a project slug to include (default: the current directory's own slug) |
| `--all-projects` | include every project under the projects root |
| `--project-family REGEX` | group worktree slugs matching a regex as one project family |
| `--days N` / `--since DATE` | window start (mutually exclusive) |
| `--until DATE` | window end |
| `--limit N` | cap the number of sessions considered |
| `--window-by {last-reply,mtime,timestamp}` | what places a session in the window: its last reply (default), its file's modification time, or its first reply |
| `--pricing PATH` | use a rate card other than the packaged default / config-dir override |
| `--config-dir PATH` | override `~/.claude/token-lens` (or `$CLAUDE_CONFIG_DIR/token-lens`) |
| `--tz ZONE` | IANA time zone for this run only (e.g. `America/New_York`); default: `config.toml`'s `tz`, else the machine's own zone |
| `--group-by {agent,entrypoint,mode,model,project,purpose}` | grouping axis for tables that support it |
| `--no-cache` / `--rebuild-cache` | mutually exclusive. `--no-cache` skips the on-disk digest cache entirely; `--rebuild-cache` purges it first, then repopulates as it parses. Both are wired through to `corpus.load_corpus` for every subcommand that loads a corpus. |
| `--jobs N` | parallel parsing workers (default: 1) |
| `--quiet` / `--verbose` | verbosity (mutually exclusive); `--verbose` also prints a `[corpus] files=... cache_hits=... cache_misses=... elapsed_s=...` line to stderr |
| `--version` | print the tool version and exit |

### Performance

Timings against a real 1.6 GB corpus (`~/.claude/projects`, `--all-projects`,
30-day window — 120 sessions, 1,644 subagent transcripts, 29 workflow runs),
measured 2026-09-19 on the owner's own machine. The corpus was live (being
actively written to by ordinary Claude Code use) during measurement, so
treat these as representative rather than lab-controlled numbers:

| Run | Time |
|---|---|
| Cold (`--rebuild-cache`, empty digest cache, `--jobs 1`) | `22.1` s |
| Warm (unchanged corpus, digest cache populated) | `7.6` s |
| Warm, `--jobs 4` | `12.6` s |

The digest cache that makes the warm numbers possible lives under
`<config-dir>/cache/` (`<config-dir>` defaults to `~/.claude/token-lens`,
or `$CLAUDE_CONFIG_DIR/token-lens`) — one JSON file per transcript, keyed
by that transcript's path plus `model.py`'s `SCHEMA_VERSION` and
`parse.py`'s own `PARSER_VERSION`, so a future release that changes
either the dataclass contract or the parsing logic invalidates exactly
the entries it needs to and nothing else (see
[`cache.py`](src/claude_token_lens/cache.py)). Delete the directory, or
pass `--rebuild-cache`, to force a full re-parse.

## 3. Reading the report sections

`report.build_report` (`report.py`) assembles every section below into
one `ReportModel`, in the fixed order the table follows, and `claude-token-lens
report` prints it (Markdown by default; `--json`/`--html`/`--csv-dir` for
the other renderers — see [section 2](#2-installing-and-first-run)). Each section is
still, independently, a `build_section(...)` function returning a
`Section` of `Table`s ([`model.py`](src/claude_token_lens/model.py)),
fully tested and runnable on its own from a short Python script against
your own transcripts — that direct-call path is how the worked example in
[`docs/sections-reference.md`](docs/sections-reference.md#worked-example)
was produced, and is still useful if you want one section in isolation.

| Section key | Title | Module | What it answers |
|---|---|---|---|
| `overview` | Overview | `report.py` | corpus-wide totals (sessions, transcripts, turns, the four raw token counts, cost, cache-read cost share, cache ROI) plus a per-model breakdown |
| `usage` | Usage | `usage.py` | day/week/month/project/entrypoint cost and token breakdowns, plus five-hour usage blocks (subscription billing only — see [section 1](#1-what-it-is-what-it-measures-and-what-it-cannot)) |
| `sessions` | Sessions | `classify.py` | mode (interactive/long-agentic/overnight/mixed) and purpose (docs/refactor/test-triage/...) per session, with the evidence that produced each classification |
| `recache` | Re-cache events | `recache.py` | which turns paid to re-write a prefix that should have been a cache hit, why, and what it cost — see [`docs/concepts.md`](docs/concepts.md#3-cache-rebuild-definitions-and-signatures) |
| `ttl` | Cache TTL break-even | `ttl.py` | per agent type: observed cost vs. simulated 5m-only/1h-only cost, plus the utilisation metrics below |
| `limits` | Usage limits | `limits.py` | usage-cap pauses (5-hour/weekly), harness-forced subagent terminations, and the desktop app's resume pings, as first-class attributable facts instead of behavioural noise — see [`docs/limits.md`](docs/limits.md) |
| `carry` | Context carry cost per tool | `carry.py` | cost of a tool result riding along in the cached prefix on every turn after the one it entered on, by tool and by agent type, plus the saving a truncation cap would have made — see [`docs/carry.md`](docs/carry.md) |
| `compaction_sim` | Compaction-window sweep | `compaction_sim.py` | modelled cost under other `autoCompactWindow` settings, a fidelity check against each session's actually-configured window, and a conservative "at least W" recommendation — see [`docs/compaction-sim.md`](docs/compaction-sim.md) |
| `model_swap` | Model-swap counterfactual | `model_swap.py` | ceiling saving from repricing every already-observed turn one model tier down, per agent type and corpus-wide — see [`docs/model-swap.md`](docs/model-swap.md) |
| `waste` | Wasted-turn spend | `waste.py` | spend on turns whose output was never used (tool error, interrupt, tool denial, harness-killed subagent), by cause, agent type and top session — see [`docs/waste.md`](docs/waste.md) |
| `compactions` | Compactions | `compaction.py` | compaction count, trigger mix, pre/post/dropped tokens, and the re-cache cost of the turn right after each compaction |
| `agents` | Agents and information flow | `topology.py` | downward cost (briefing/system-prompt writes into each agent type), upward cost (`Agent`/`Workflow` tool-result sizes flowing back), skill roll-ups, spawn-depth chains |
| `quality` | Quality signals | `quality.py` | whether the work went well: agent runs that didn't finish or likely ran out of turns, failed tool calls and shell commands, denials, corrections, edits redone, per agent type and per model and effort, with a significance test — see [`docs/concepts.md`](docs/concepts.md#7-quality-signals) |
| `workstyle` | Workstyle | `workstyle.py` | one archetype per session/corpus: `overseer-fanout`, `plan-high-implement-low`, `workflow-heavy`, `effort-varied`, `chat-only`, `single-model`, with the evidence features |
| `workflows` | Workflows | `workflows.py` | per-run agent count, phase count, duration and cost from `<session>/workflows/wf_*.json` |
| `phases` | Phases | `phases.py` | cost split across DISCOVERY (read/search only), IMPLEMENTATION (real edits or an ordinary shell command), VERIFICATION (a test/build tool, or a scratch-file edit), OTHER — only in the report when `--phases` is given |
| `config` | Config | `report.py` via `snapshots.py` | one diff table per config key that changed across the window's snapshots (capped at 20 keys) — only present when `snapshot-config` snapshots exist for the window |
| `scorecard` | Scorecard | `scorecard.py` | five 1-5 levels (cache efficiency, context hygiene, agent efficiency, config fit, data quality) plus an overall level (the minimum of the first four, never an average) |

Two further pieces of the report aren't in the table above because
they aren't `Section`s: **Recommendations** (`recommend.py`) is a
dedicated `ReportModel.recommendations: list[Recommendation]` field —
see [the Recommendations block](#the-recommendations-block) below — and
**Diagnostics** (parse-quality counters: unparsable lines, synthetic
turns, TTL-sum mismatches, ...) is `ReportModel.diagnostics`, rendered
directly by every renderer rather than as a table.

Two CLI-only, non-`report` consumers use a different section shape
entirely: `config-diff --key K` prints one standalone plain-text table
from `snapshots.build_config_diff_table` (see [section 2](#2-installing-and-first-run)
— it does not go through `build_report`, so it isn't the same code path
as the report's own `config` section above), and `usage_windows`
(`tools/log_usage.py`) — your 5-hour/7-day plan usage-window percentages
over time, from a `log-usage` paste or the statusline logger, with a
token-volume regression per window — has a `build_section` function but
nothing in `report.py`/`cli.py` wires it into the assembled report yet;
call it directly, the same as any other module, until that's closed.

### The Recommendations block

`recommend.recommend()` turns the assembled report into a list of
`Recommendation`s (`model.py`), each with:

| Field | Meaning |
|---|---|
| `id` | stable identifier for the rule that fired (e.g. `ttl-switch`, `compaction-churn`) |
| `severity` | `"info"` \| `"advice"` \| `"action"` |
| `category` | `"settings"` \| `"workflow"` \| `"data"` |
| `scope` | where the lever named below applies: `"user"` (`~/.claude/settings.json`), `"repo"` (a project `.claude/settings.json` or agent frontmatter path), or `"managed"` (an org-pushed `managed-settings.json` key the user can't change locally — the action text then also says "raise with your administrator") |
| `lever` | the bare settings key or frontmatter path the recommendation would change (e.g. `promptCacheTtl`, `experimental.cacheTtl` in `<agent>.md`), or `None` |
| `evidence` | one or more `(label, value, source_table, row_key)` tuples, each citing a real cell from a table already in the report — a test walks every recommendation this module produces and confirms the value it cites is genuine, not recomputed |
| `why` | one plain sentence on why it matters |
| `estimated_saving` / `saving_basis` | the saving phrased for your billing mode (`units.py`), and how it was worked out |
| `changes` | the concrete edits proposed: `SettingChange` (`target` `settings`/`agent`, `key`, `agent`, `value` or a `suggested` description when the value needs your judgement, `current`, `note`, `unconfirmed`, `new_agent_file`) |
| `fixes` | per change, from `fixes.py`: a six-part `explainer`, an `apply --set ... --dry-run` `command` (when the value is known), a `command_warning` when the command alone isn't enough, and a `prompt` to give Claude |

The dashboard never changes your Claude Code config. Each change comes
with the prompt and, for a plain setting, the command; the command's
`--dry-run` shows the diff first, and a real run prints how to undo it.

`report --patch-set` renders the whole recommendation set as
unified-diff-style text (`recommend.render_patch_set`) showing the
before/after value for each lever, with managed-scope levers marked
`# managed by policy -- shown for reference only`.

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

## 4. Installing the SessionStart hook and the statusline

### SessionStart config-capture hook

[`hooks/snapshot-config.py`](src/claude_token_lens/hooks/snapshot-config.py)
is a standalone stdlib script — it
deliberately imports nothing from this package, so it keeps working if
copied on its own onto a machine that only has a bare Python
interpreter.

**The easy way:** `claude-token-lens init` installs the script, shows
the exact `settings.json` change and makes it only after you say yes
(the file is backed up first). `claude-token-lens uninstall` takes it
out again. The rest of this section is for doing it by hand.

**What it costs:** the hook runs once when a session starts, takes a few
milliseconds and prints nothing, so it adds no tokens to the
conversation. `init` registers it with `"async": true`, so it never
delays a session. The by-hand fragments below leave that key out; add
it next to `"command"` if you want the same.

To install it by hand:

```bash
claude-token-lens snapshot-config --install-hook   # copies the script into <config-dir>/hooks/
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
            "command": "\"C:\\path\\to\\python.exe\" \"C:\\Users\\<you>\\.claude\\token-lens\\hooks\\snapshot-config.py\""
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

On Windows the command names the Python and the script by full path.
Claude Code may run hooks through Git Bash, which doesn't expand
`%USERPROFILE%`, and the `py` launcher isn't always on the `PATH`; either
one stops the hook running without any visible error. The Data quality
tab flags both, and `claude-token-lens init --repair-hook` fixes them:
it writes the folder out in full and keeps your own Python when it's
found. The hook needs only the standard library, so a new install names
your main Python rather than a virtual environment's, which could later
be deleted.

The hook always exits 0 and prints nothing on success (or a single
stderr line on failure) so a broken Python can never block a session
start. It writes one snapshot JSON file per run (skipped when an
unchanged snapshot already exists within `--min-interval`, default 300s)
to `<config-dir>/snapshots/<UTC compact timestamp>.json`,
capturing:

- **Environment variable names only, never values**, for every name
  matching `ANTHROPIC_*` / `CLAUDE_*`.
- **Settings values, but only for a small allowlist**: `model`,
  `effortLevel`, `outputStyle`, `autoCompactWindow`,
  `autoCompactEnabled`, `promptCacheTtl`,
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

The statusline is a separate, self-contained entry point. Either
`claude-token-lens statusline --print-install-fragment` (the CLI
subcommand delegates straight to the module) or invoking the module
directly work identically:

```bash
python -m claude_token_lens.statusline --print-install-fragment
```

Merge the result into `~/.claude/settings.json` (replaces any existing
`"statusLine"` key):

Windows:

```json
{ "statusLine": { "type": "command", "command": "\"C:\\path\\to\\python.exe\" -m claude_token_lens.statusline" } }
```

POSIX (Linux/macOS):

```json
{ "statusLine": { "type": "command", "command": "python3 -m claude_token_lens.statusline" } }
```

The statusline runs only in Claude Code in a terminal. Sessions in the
desktop app or an IDE never run it, so usage-limit readings come only
from terminal sessions (the Data quality tab says when none are
arriving). Like the hook, it adds no tokens to the conversation.

On every status-line refresh, Claude Code writes a JSON payload to this
script's stdin; the statusline reads `context_window.used_tokens` (a
compact `ctx NNk` figure), `prompt_cache` (real cache ground truth — see
below), `rate_limits.{five_hour,seven_day}.used_percentage` (plan
usage-window percentages — also appended to
`~/.claude/token-lens/usage-log.csv`, deduped by session/reset-time/
percentage), and the last 64 KB of `transcript_path` (never the whole
file) as a fallback TTL hint when no usable `prompt_cache` is present. It
never raises: any failure anywhere in that path falls back to printing a
minimal `token-lens` line rather than blanking the status bar.

**Cache segment.** When the payload carries a real `prompt_cache` object,
the line prints ground truth, not an estimate: `cache warm 5m 03:12` (a
`MM:SS` countdown to `prompt_cache.expires_at`) while warm, or
`cache cold` with an optional trailing `recache ~12k tokens` hint (from
`prompt_cache.recache_tokens_if_cold`) once it has expired. With no
usable `prompt_cache` at all, the line falls back to an estimate labelled
`cache est`, whose TTL is read from the transcript's own last assistant
line (a positive `message.usage.cache_creation.ephemeral_1h_input_tokens`
implies a 1h TTL, else 5m) rather than guessed. The whole line stays
under 120 characters and never prints message text. The numeric
`prompt_cache` fields (`warm`, TTL, expiry, misses, miss cause,
recache-if-cold tokens) are also appended to `usage-log.csv` as six
trailing columns, so `claude-token-lens report` can render a
`cache_ground_truth` table (see
[`docs/sections-reference.md`](docs/sections-reference.md)) summarising
real cache-warmth across sessions instead of a live-only estimate.

## 5. Windows notes

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
- **Where `settings.json` is.** Every command that reads or changes
  Claude Code's `settings.json` (`init` and `init --repair-hook`,
  `apply`, `changes`, `uninstall`, the dashboard's hook and statusline
  checks, the snapshot hook itself) looks in the same place:
  `--claude-root` when given (`init`, `apply`, `changes`, `uninstall`),
  else `$CLAUDE_CONFIG_DIR`, else `~/.claude`. `--config-dir` moves only
  this tool's own folder; `settings.json` is never looked for beside it.
  When `--config-dir` is not the default, the hook and statusline
  commands `init` adds end with the same `--config-dir`, so what they
  record lands where the CLI and dashboard read.
- **`CLAUDE_CODE_PROJECT_DIR_NAME`** overrides the computed slug for the
  *current* project directory only, independently of whether
  `CLAUDE_CONFIG_DIR` also moved the whole tree.

## 6. For team leads and enterprise

What's implemented today:

- **`exclude_projects`** (`config.toml`, a list of slug regexes) excludes
  matching projects from discovery entirely — for a confidential repo
  you never want scanned, not just hidden from output. A malformed regex
  is skipped, never fatal.
- **`retention_days`** (`config.toml`, an integer) is how many days of
  sessions `serve` keeps in its local store (`service.db`); older ones
  are pruned on every poll tick. `serve --retention-days N` overrides it
  for one run. Unset means keep forever.
- **Managed settings** are captured by the snapshot hook (see [section 4](#4-installing-the-sessionstart-hook-and-the-statusline))
  into `managed_settings` (redacted the same as user settings) and
  `managed_keys` (raw key names only), and `recommend.py` renders them:
  any recommendation whose lever's settings key appears in a window's
  `managed_keys` gets `scope: "managed"` and its action text says "this
  lever is managed by policy, raise with your administrator" instead of
  proposing a change the user can't actually make locally — see
  [the Recommendations block](#the-recommendations-block).
- **Provider detection.** `parse.detect_provider` classifies each turn's
  billing surface from the model id's own shape: an
  `anthropic.`/`us.anthropic.`-prefixed or `-v1:0`-suffixed id is
  Bedrock, an `@<date>`-suffixed id is Vertex, anything else is direct
  Anthropic API. This is recorded on `TranscriptMeta.provider` and
  `Config.provider`. **Not yet implemented:** Foundry detection (only
  `anthropic`/`bedrock`/`vertex` are recognised today), and any
  per-provider rate override in `pricing.toml` — every provider is
  currently priced against the same Anthropic-direct rate card.

### Exports and scheduled reports

`claude-token-lens export --format csv-flat|json|otel-jsonl` (S1-exports)
feeds a corpus into existing BI/observability tooling with the same
privacy posture as the report itself: **aggregate-only is the default**
(one row per day/project/model/entrypoint/agent-type — no session ids)
and **project slugs are hashed by default in every mode**, including
`--per-session`, via a salted HMAC-SHA256 construction (the same
HMAC-SHA256 construction, and the same `load_or_create_salt`/config-dir
salt file, as every other hashed value in this project — just a
different truncation length and domain-separation tag). Per-session
detail is opt-in (`--per-session`); the raw-slug opt-out
(`--no-hash-slugs`, honoured even together with `--aggregate-only` as an
explicit, informed choice) doesn't print the fully raw slug either — it
redacts just the OS-username segment (`Users-<name>-`/`home-<name>-` ->
`<user>`) and warns on stderr. No prompt text, tool output, or file path
is ever in an export — every column is a count, a token total, or a
cost. `otel-jsonl` mirrors Claude Code's own OpenTelemetry metric names
(`claude_code.token.usage`, `claude_code.cost.usage`) as an **offline
approximation** for feeding an existing collector's dashboards, not a
live exporter. Full column reference and format details:
[`docs/exports.md`](docs/exports.md).

`claude-token-lens monthly-report --out DIR [--month YYYY-MM]`
(S1-exports) writes `DIR/claude-token-lens-YYYY-MM.md` and the matching
`.html` for one calendar month (default: the previous month, resolved
against `config.tz`) — a short finance header (total cost, tokens,
sessions, cost by model/project/entrypoint, five-hour blocks used under
subscription billing) followed by the `usage` section, sized for a
recurring habit rather than the full multi-section report. The same
`(corpus, pricing, config, month)` produces files identical apart from a
trailing "Generated at" line/comment; pass `--generated-at` (or set
`SOURCE_DATE_EPOCH`) for a genuinely byte-identical run, so it is safe to
schedule even when diffing raw bytes.

Together these make claude-token-lens usable as a team tool without
running claude-token-lens's own modules by hand against each person's own
`~/.claude/projects` — see [`docs/exports.md`](docs/exports.md) for the
full picture, including the entry point (`monthly.write_monthly_report`).
If the dashboard runs as a service, `serve --monthly-report DIR` does
this for you: it writes last month's report into `DIR` when it is
missing, checking at startup and every hour.

### For team leads: cross-machine comparison (v0.3)

`claude-token-lens export --aggregate` / `import` / `team-report`
(v0.3 Task 1) let a team lead compare archetypes and agent-type usage
across several people's machines, with the same privacy floor as
everything else in this project:

- **Aggregate only.** Each team member's document holds per-group sums
  and means (by archetype, mode, purpose, agent type, model) plus
  scorecard levels — never a session id, never per-session rows.
- **Hashed, not named.** A stable-but-non-reversible `machine_id` (a
  salted HMAC over the hostname) identifies "the same machine across
  two imports" without naming it; project slugs appear only as the
  same kind of hash, and only when the person exporting explicitly
  passes `--include-projects`.
- **Opt-in per person.** Nobody's usage reaches a team report unless
  they run `export --aggregate` themselves and hand the file over —
  there is no automatic collection or upload.
- **No text, ever.** Same guarantee as every other export/report
  surface in this project: every field is a count, a percentage, a
  cost or a level, never a prompt or a tool result.

```bash
# each team member:
claude-token-lens export --aggregate --out my-machine.json

# the team lead:
claude-token-lens import my-machine.json colleague-a.json colleague-b.json
claude-token-lens team-report
```

`team-report` renders per-archetype and per-agent-type comparison
tables with one column per machine (its short hashed id, never a
hostname), gated by the same minimum-sample rule the rest of this
project uses (5 sessions per cell; below that a cell reads `n<5`), and
carries an "observed, not controlled" note — a difference between
machines may reflect different work, not a settings difference. Full
flow and field reference: [`docs/team.md`](docs/team.md).

## 7. Privacy and security

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

The same audit applies to `report --json`/`report --html PATH` output,
or to JSON/HTML you generate by calling `render.json_out`/`render.html`
directly — run it by hand over the file:

```bash
grep -RnoE '[^"]{65,}|[A-Za-z]:\\\\|/home/|\\\\Users\\\\|/c/Users/|@' report.json report.html
```

Treat any hit as a bug and open an issue with the field name (never the
value).

## 8. Prior art and credits

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

## 9. Licence, contributing, roadmap

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

**Roadmap** (see the project plan for full detail; report assembly, the
recommendation engine and the optimisation scorecard shipped in v0.1 —
see the Status note above):

- **v0.2** — `claude-token-lens serve` (a local read-only service:
  watcher thread, SQLite store, `http.server` JSON API and a
  dependency-free static web UI), Docker packaging, a live countdown in
  the statusline, an aggregate-only `export` command, and a monthly
  report. Shipped — see [section 10](#10-running-the-service).
- **v0.3** — a profile schema and catalogue, `init` and a `baseline`/
  onboarding capture window; `apply`/`--revert` for writing a chosen
  profile into `settings.json`/agent frontmatter; a `compare` command
  and a `reconcile` pass against real billing data; and team aggregate
  import across machines (`import`/`team-report`). Shipped — see
  [`docs/onboarding.md`](docs/onboarding.md),
  [`docs/profiles.md`](docs/profiles.md), [section 11](#11-applying-a-profile),
  [`docs/compare.md`](docs/compare.md) and [`docs/team.md`](docs/team.md).
- **v0.4** — Quick actions, goal-first profile creation with what-if
  estimates, CLAUDE.md and skills review (Context files tab), "Your
  changes and what they did", quality signals per agent and setup, the
  dashboard-wide window picker, and `serve --monthly-report`. Shipped —
  see [CHANGELOG.md](CHANGELOG.md).
- **v0.5** — sessions from WSL alongside Windows ones (found by
  `init`), and one-command `update`. Shipped.
- **Next** — a budget guardrail that exits non-zero past a
  weekly token or daily dollar limit (planned as `check --weekly-tokens
  N --daily-usd N`, before `check` became the quick-actions command, so
  it will need another name), anomaly-outlier detection, and an opt-in `--show-paths` local file view. See
  [CHANGELOG.md](CHANGELOG.md)'s `[0.3.0]` "Planned" notes.

## 10. Running the service

`claude-token-lens serve` runs a local watcher thread, a SQLite store,
a read-only JSON API and a dependency-free static web UI, so you can
keep a live dashboard open instead of re-running `report` by hand. It
never opens an outbound connection and binds to `127.0.0.1` unless you
explicitly pass `--allow-remote` (see [SECURITY.md](SECURITY.md)).

```bash
claude-token-lens serve --projects-root ~/.claude/projects --config-dir ~/.claude/token-lens
# then open http://127.0.0.1:8765
```

Running it once by hand, like this, is fine for a quick look, but it
only lasts until you close the terminal — and Claude Code deletes
transcripts older than `cleanupPeriodDays` on its own, so history you
haven't captured yet is gone for good once that happens. `init`'s last
step (or the standalone `install-service` subcommand) registers
`serve` to run continuously from logon instead, so nothing has to be
started by hand:

```bash
claude-token-lens install-service          # registers it for this platform
claude-token-lens install-service --dry-run  # preview the plan first, writes/runs nothing
claude-token-lens uninstall-service         # removes it again
```

`install-service` detects the platform and does one of the following —
full detail, including what each path can/cannot touch and how to
verify no egress, is in [docs/deploy.md](docs/deploy.md):

- **Windows:** registers a logon-triggered Scheduled Task
  (`-RunLevel Limited`, no admin rights required).
- **Linux:** writes `~/.config/systemd/user/claude-token-lens.service`
  and runs `systemctl --user enable --now` (a hardened user unit —
  `ProtectHome=read-only` plus a carved-out `ReadWritePaths` for its
  own data directory). Prints a `loginctl enable-linger` note for
  headless servers with no interactive session.
- **macOS:** writes `~/Library/LaunchAgents/com.claude-token-lens.plist`
  and runs `launchctl bootstrap`.

`claude-token-lens serve`'s own `GET /api/health` reports whether the
service is currently registered (`service_registered: true|false|null`
— see [`docs/api.md`](docs/api.md)), and the dashboard's Overview tab
shows a banner if it isn't.

The store is always a derived cache, never source of truth: delete and
rebuild it any time with `claude-token-lens serve --purge --yes`, and
prune old sessions automatically with `--retention-days N`.

### Other ways to run the service

Prefer to wire it up yourself, or run somewhere `install-service`
doesn't support? These are the exact mechanisms `install-service` uses
under the hood, runnable directly:

- **Windows, no admin rights:**
  `powershell -ExecutionPolicy Bypass -File scripts\windows\Register-TokenLensTask.ps1`
  registers the same Scheduled Task by hand. Remove it with
  `Unregister-TokenLensTask.ps1`.
- **Linux/macOS, no root:**
  `systemctl --user enable --now claude-token-lens.service` after
  copying `scripts/systemd/claude-token-lens.service` to
  `~/.config/systemd/user/`.
- **Docker:** `docker compose up -d` builds and runs the hardened image
  in this repository's `Dockerfile`/`docker-compose.yml` (non-root
  user, read-only root filesystem, `cap_drop: [ALL]`, loopback-only
  published port). `install-service` does not manage Docker containers
  — use Compose's own restart policy for "start on boot" here.

No `pip install`? `python scripts/build-pyz.py` produces a single
dependency-free `dist/claude-token-lens.pyz` you can copy anywhere and
run with `python dist/claude-token-lens.pyz serve ...` (or
`... install-service`, which builds a Scheduled Task/unit/agent action
that re-invokes the very same `.pyz` — see `docs/deploy.md`).

## 11. Applying a profile

`claude-token-lens apply` writes one of the seven shipped catalogue
profiles (or your own profile TOML file) into `settings.json`/agent
frontmatter/env-var guidance for a project or your user config — the
host-side half of v0.3's profile system (`docs/profiles.md` is the full
schema/catalogue/diff reference; this is the applying-it walkthrough).

```bash
# Preview the exact diff, nothing written:
claude-token-lens apply interactive-chat --dry-run

# Apply it to the current project (writes .claude/settings.local.json):
claude-token-lens apply interactive-chat --project-dir .

# Undo it:
claude-token-lens apply --revert 20260919T100252Z

# One-session overlay instead of a persisted apply:
claude-token-lens apply interactive-chat --launch

# One setting, no profile (the command a recommendation card shows):
claude-token-lens apply --set omitClaudeMd=true --agent code-reviewer --scope user --dry-run
```

| Flag | Meaning |
|---|---|
| `--scope {user,project-local,repo}` | Which settings file is written (default: `user`, or `project-local` once `--project-dir` is given) |
| `--claude-root PATH` | The Claude Code folder holding `settings.json` and `agents/` for `user` scope (default: `$CLAUDE_CONFIG_DIR`, else `~/.claude`) |
| `--project-dir PATH` | Project directory for `project-local`/`repo` scope. Named `--project-dir`, not `--project` — the global `--project` flag already means "a repeatable project slug to filter a report by", the same collision `snapshot-config`/`probe-config` resolve the same way |
| `--dry-run` | Explain each change in words (what it controls, now and after, where, trade-off, undo), then print the diff and the command to run, without writing anything |
| `--launch` | Write a one-session `<config-dir>/profiles/<id>.settings.json` overlay instead of a persisted apply |
| `--allow-tracked` | Allow writing a target file that a git repository already tracks (refused by default for `project-local`/`repo` scope) |
| `--force` | Create a missing `.claude/agents/<name>.md` file from scratch instead of refusing |
| `--revert TS` | Undo a previous apply, byte for byte, named by the timestamp `apply` printed at the time. Refused if a file was edited after that apply, since restoring it would discard those edits |
| `--ignore-changes` | With `--revert`: restore the backup anyway |
| `--list-backups` | List previous applies (timestamp, profile, scope, file count) and exit |
| `--set KEY=VALUE` | Change one allowlisted key instead of applying a profile (repeatable). Lists are comma-separated (`tools=Read,Grep`), booleans `true`/`false`. Validated like a profile; recorded as profile `one-off`; never marks a profile active |
| `--agent NAME` | With `--set`: change `.claude/agents/NAME.md` frontmatter instead of `settings.json` |

Every real apply backs up whatever it overwrites first, so `--revert`
always restores the exact prior state; a managed-settings key is never
written regardless of scope or flags; and `env` values are printed as
`export NAME=value` guidance only, never written to any file. See
[docs/profiles.md](docs/profiles.md#applying-a-profile) for the full
detail on scopes, backups, and the tracked-file/missing-agent-file
refusals.
