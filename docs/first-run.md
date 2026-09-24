# First run on a locked-down work machine

The one document to follow on a machine you don't fully control: no
admin rights, Python 3.11+ already there (or not), maybe no `git`,
maybe no proxy access to PyPI. The three install routes, `init`,
`install-service --dry-run`, `report` and `serve` with a health check
were run end to end against a synthetic, throwaway project when this
document was written.

On an ordinary machine, the README's [Quick
start](../README.md#quick-start) is the fast path. This document is
for the first five minutes on a machine you've never run it on before.

## 0. Check Python

```powershell
py -3 --version
```

Needs **3.11 or newer** (`pyproject.toml`'s `requires-python = ">=3.11"`
— the code itself uses the stdlib `tomllib` module, only available
from 3.11). No `py` launcher? See the troubleshooting table below. On
Linux/macOS: `python3 --version`.

## 1. Choose an install route

Three routes, in the order to try them on a machine you don't fully
trust:

| Route | Needs | Best for |
|---|---|---|
| **A. `.pyz` download** | Nothing beyond Python itself — no `pip`, no network, no `git` | The default choice on a locked-down machine |
| **B. `pip` from a local clone** | `pip`, no network required (installs from the folder on disk) | You already have the source checked out |
| **C. `pip` from GitHub** | `pip` + network; `git` only for the `git+https` form, not for the zip-archive form | A normal, unrestricted machine |

### Route A: `.pyz` (recommended for a locked-down machine)

Download `claude-token-lens.pyz` from the [Releases
page](https://github.com/PaulMorrisDev/claude-token-lens/releases) (every
tagged release attaches a pre-built one — no build step, no `pip`, no
`git`). Then:

```powershell
py -3 claude-token-lens.pyz --version
py -3 claude-token-lens.pyz init
```

If you'd rather build it yourself from a checkout (needs the source,
not just the interpreter):

```powershell
py -3 scripts\build-pyz.py
py -3 dist\claude-token-lens.pyz --version
```

Every subcommand works the same way from the archive: `py -3
claude-token-lens.pyz <subcommand> ...`, in place of `claude-token-lens
<subcommand> ...` everywhere else in this document.

### Route B: `pip` from a local clone (no network needed)

```powershell
py -3 -m venv .venv
.venv\Scripts\pip install <path-to-the-cloned-repo>
.venv\Scripts\claude-token-lens --version
```

If `.venv\Scripts\claude-token-lens.exe` isn't on `PATH` (it never is
unless you `activate` the venv), call it by its full path as above, or
fall back to:

```powershell
.venv\Scripts\python.exe -m claude_token_lens --version
```

**`pip install --user .` (outside a venv) does *not* usually put
`claude-token-lens.exe` on `PATH` either** — pip installs it under
`%APPDATA%\Python\Python3xx\Scripts`, which Windows doesn't add to
`PATH` by default, and pip prints a warning to that effect at install
time. `python -m claude_token_lens ...` is the reliable fallback in
every case above; it never depends on `PATH` at all.

**A genuinely offline machine (no PyPI access at all) will fail this
route**, even from a local clone: this project's own `dependencies` are
empty, but `pip install` still needs `setuptools`/`wheel` as *build*
dependencies for a source install (PEP 517), and a fresh venv on Python
3.12+ doesn't come with `setuptools` pre-installed. If `pip install
--no-index <path>` is the only thing you can try, expect `No matching
distribution found for setuptools>=68` — that's the signal to fall
back to Route A.

### Route C: `pip` from GitHub

```powershell
py -3 -m pip install git+https://github.com/PaulMorrisDev/claude-token-lens
```

No `git` on the machine? This works without it (a plain HTTPS
download, no `git clone`):

```powershell
py -3 -m pip install https://github.com/PaulMorrisDev/claude-token-lens/archive/refs/heads/main.zip
```

Both need outbound HTTPS access to GitHub (and, same as Route B, to
PyPI for the `setuptools`/`wheel` build dependencies) — if a corporate
proxy blocks either, use Route A instead.

## 2. Run `init`

```powershell
claude-token-lens init
```

(or `py -3 claude-token-lens.pyz init` for Route A). One line each for
what it asks and why — every question has a sensible default, so
pressing Enter through all of them is a reasonable first pass:

| Question | Means |
|---|---|
| How do you pay for Claude Code? | `subscription` for a Pro, Max, Team or Enterprise plan (typing the plan name works too), `api` for pay-per-token, `auto` to decide from usage-limit readings. Sets whether amounts are shown as money or as a share of your usage limits |
| Projects to always leave out | Folder names under `~/.claude/projects`; their transcripts are skipped everywhere (reports, dashboard, exports) |
| Do you start Claude Code with `--settings` or `CLAUDE_CONFIG_DIR` | Affects where a later `apply` writes a profile. Most people answer no |
| Is this project's `.claude` folder committed to a repo colleagues use | Same: affects `apply`'s default scope |
| Time zone | Used to group reports by day; blank uses this computer's |
| Where should changes you apply go by default | `user` (all your projects), `project-local` (this project, just you) or `repo` (this project, everyone) |
| How many days to collect data before the first baseline | How long `baseline` waits before it has enough data for a confident first read (default 7) |
| Claude Code also runs in WSL: Ubuntu on this computer. Include those sessions? | Asked only when `init` finds Claude Code sessions inside a WSL distro (it runs `wsl -l -q` and looks in each distro's `/home/*/.claude/projects`). Yes adds the folder to `config.toml`'s `extra_projects_roots`, and the dashboard and every command read it alongside your Windows folder. Default yes; `--non-interactive` adds it and says so |

Running it unattended (a script, or just to skip the prompts) derives
every answer instead of asking, and prints exactly what it derived and
why:

```powershell
claude-token-lens init --non-interactive --no-install --no-service `
  --config-dir C:\path\to\config --projects-root C:\path\to\projects
```

`--config-dir`/`--projects-root` are optional — omitted, they default
to `%USERPROFILE%\.claude\token-lens` and `%USERPROFILE%\.claude\projects`
respectively (or `%CLAUDE_CONFIG_DIR%\token-lens`/`...\projects` when
that variable is set) — the same place Claude Code itself already
keeps its transcripts, so on an ordinary machine you don't need to pass
either.

Unattended, `init` doesn't touch `settings.json`. Without `--no-install`
it prints the hook and statusline fragments for you to add by hand. Add
`--connect` to make
that change without asking (it is still printed, and the file backed up
first).

**What `init` writes under `<config-dir>`:**

- `config.toml` — your answers above. An existing file is merged
  key-by-key. If its shape can't be merged automatically,
  `config.toml.new` is written instead and `init` says so, leaving the
  original untouched.
- `projects\<slug>.toml` — this project's own settings.
- `baselines\<id>.json` + `<id>.md` — an initial baseline, if any
  sessions were already found for this project.
- `hooks\snapshot-config.py` — a copy of the hook script, made during
  the connect step below.

Running `init` again keeps the capture window where it is: only the
first `init` sets its start. To start a new window, delete the
`capture_started` line from `config.toml` and run `init` again.

**Connecting to Claude Code — shown, then asked.** `init` then shows
the exact change to Claude Code's own `settings.json`
(`%USERPROFILE%\.claude\settings.json`, or `%CLAUDE_CONFIG_DIR%\settings.json`
when that is set; `--claude-root` names another folder):

- a `SessionStart` hook that records your settings when a session
  starts, added only if no hook runs `snapshot-config.py` yet;
- a `statusLine` command, added only if you have no statusline. Yours
  is never replaced.

It writes the change only after you answer yes (the default is no). It
first copies the file to `settings.json.bak-<UTC time>` beside it. The
hook command names your main Python install and the script by full
path; the script needs only the standard library, so a deleted
virtual environment can't break it. The statusline command names the
Python you installed claude-token-lens into. Neither needs the `py`
launcher or a `%VARIABLE%`, so both run under Git Bash. Say no and
`settings.json` is left as it was; `claude-token-lens init --connect`
makes the change later. `--no-install` skips this step.

If your existing hook command is broken (a mis-escaped path, a missing
interpreter or a `%VARIABLE%`), `init` shows the fixed command at the
start and asks before changing it. See the troubleshooting table.

## What to expect

- **It never uses your Claude tokens.** It only reads files Claude Code
  has already written. It never calls Claude or any other service, so
  there is no bump in usage from running it, however often.
- **The hook and statusline add nothing to your conversations.** The
  hook starts a short Python process in the background when a session
  starts (well under a second) and prints nothing. The statusline draws
  a line under the prompt, in the terminal only. Neither is sent to
  Claude.
- **The first scan takes a while.** The service reads every transcript
  once (seconds to a few minutes for a large history), then only new or
  changed files. The dashboard opens straight away and shows the scan's
  progress; figures fill in as it goes.
- **It reads; it doesn't change.** Nothing about how Claude works
  changes until you apply a change yourself, through a prompt you give
  Claude or `claude-token-lens apply`. A change takes effect in the next
  session you start.
- **Cheaper isn't free.** A cheaper model, lower effort or an earlier
  summary can make Claude less thorough. Each change says what it trades
  away. Pick **Since my last change** in the window picker, or look at
  **Profiles > Your changes and what they did**, to check the effect.
- **Amounts on a Pro or Max plan are list-price equivalents** until the
  statusline has logged enough usage-limit readings.

`claude-token-lens changes` prints the same list with everything the
tool installed and the command that undoes each.

## 3. The logon service

`init`'s service step asks whether to register `claude-token-lens serve`
to start automatically at logon (default yes). `--no-service` skips the
question. `--install-service` answers yes without asking. Under
`--non-interactive` without `--install-service`, the answer is no.
This matters because Claude Code deletes its own transcripts after
`cleanupPeriodDays` — only a service that's actually *running* when
that happens keeps the history.

Preview exactly what registration would do, without doing it:

```powershell
claude-token-lens install-service --dry-run
```

On Windows this prints the PowerShell it would run to register a
**Scheduled Task named `ClaudeTokenLens`**, triggered at your own
logon, `-RunLevel Limited` (no admin rights requested or required, and
none needed). Running from a `.pyz`? The printed command already
points at that exact archive's absolute path (not `python -m
claude_token_lens`, which cannot work once the code is inside a zip) —
confirm the line contains the full path to your `.pyz`, not a bare
`claude_token_lens` module reference.

Registering also starts it straight away, on every system. Re-running
`install-service` is safe: on Windows it stops the running copy,
re-registers the task for the Python you ran it with, and starts it
again.

Confirm it actually registered, two ways:

```powershell
schtasks /Query /TN ClaudeTokenLens
curl http://127.0.0.1:8765/api/health
```

The second answers only while the service is running. Its JSON body
includes `"service_registered": true` once
`is_registered()`'s own platform probe (the same `schtasks` query
above) has confirmed it — `false` or `null` (probe inconclusive) means
check the output `install-service` printed. The dashboard's Overview
tab also shows a banner when this comes back `false`.

## 4. Open the dashboard

```
http://127.0.0.1:8765
```

Live once the service is running (started by the logon task, or by
running `claude-token-lens serve` directly in a terminal you leave
open). Loopback-only by default — nothing outside
this machine can reach it unless you pass both `--bind <address>` and
`--allow-remote`. It has no login, so don't do that on a shared network.

## 5. Run the first report

```powershell
claude-token-lens report
```

(`report` is the default subcommand — `claude-token-lens` with no
arguments does the same thing.) Scopes to the current directory's
project by default; add `--all-projects` to report across every
project under the projects root, or `--project <slug>` for a specific
one.

## 6. Verify nothing left the machine

Read [`SECURITY.md`](../SECURITY.md) for the full guarantee (loopback
bind by default, no message text/file contents/shell commands ever
written to the on-disk store, a project slug's username segment
redacted to `<user>` before it reaches any API response). If you have
the source checked out and a dev environment (`pip install .[test]`),
the same check this project runs on every commit is runnable directly:

```powershell
python -m pytest tests\test_privacy.py tests\test_scrub.py -q
```

(Not available from a bare `.pyz` install, which ships no tests —
`SECURITY.md`'s written guarantee and this project's public CI are the
proof in that case.)

## 7. Undo a change, or uninstall completely

**Undo one change.** Every `apply` prints the command that undoes it.
To find it again:

```powershell
claude-token-lens apply --list-backups
claude-token-lens apply --revert <TS>
```

`--revert` restores the backup `apply` made under
`<config-dir>\backups\<TS>\`. If a file was edited after the apply, it
refuses and restores nothing; `--ignore-changes` restores it anyway,
discarding those edits.

**Take everything back out.** Look first:

```powershell
claude-token-lens uninstall --revert-changes --delete-data --dry-run
```

Then run it without `--dry-run`. It shows each step and asks before
making it:

1. Removes this tool's `SessionStart` hook and statusline from
   `settings.json`. The diff is shown, and the file is copied to
   `settings.json.bak-<UTC time>` first. A statusline of your own is
   left alone.
2. Removes the logon service, if registered, stopping the running
   dashboard first on every system.
3. With `--revert-changes`: undoes every `apply` still in place, newest
   first. If a file was edited after an apply, that apply is not undone
   at all, and the file is named. Without `--revert-changes`, each one
   is listed with its `apply --revert` command.
4. With `--delete-data`: deletes `<config-dir>` (default
   `%USERPROFILE%\.claude\token-lens`): the database, snapshots, usage
   log, profiles and backups. It refuses while any applied change is
   still in place, because the backups are the only way to undo it.

`--yes` answers yes to every question (each change is still printed).
Nothing removes the `settings.json.bak-*` copies; delete them yourself
once you're happy.

Finally, if installed via `pip`: `pip uninstall claude-token-lens`. Via
`.pyz`: delete the one file.

## 8. Update to a newer version

From version 0.5 on, one command does it all for routes B and C:

```powershell
py -3 -m claude_token_lens update
```

It installs the newest version from GitHub (`--from <path-to-the-cloned-repo>`
for Route B, after a `git pull`), then runs the new copy's
`install-service`, which restarts the dashboard on it and checks the
version that answers on port 8765. `--dry-run` prints both commands
without running them. A `.pyz` can't update itself: `update` says so and
links the download.

On 0.4 or older, or to do it by hand, install the new version the same
way you installed the first one:

| Route | Update |
|---|---|
| A (`.pyz`) | Download the new `claude-token-lens.pyz` from the [latest release](https://github.com/PaulMorrisDev/claude-token-lens/releases/latest) over the old file |
| B (local clone) | `git pull` in the clone, then `.venv\Scripts\pip install --force-reinstall <path-to-the-cloned-repo>` |
| C (GitHub) | `py -3 -m pip install --force-reinstall git+https://github.com/PaulMorrisDev/claude-token-lens` |

`--force-reinstall` is needed because pip skips a copy whose version
number hasn't changed. Then point the logon task at the new version and
restart it, with the same Python you just updated:

```powershell
py -3 -m claude_token_lens install-service
```

(Route A: `py -3 claude-token-lens.pyz install-service`; Route B:
`.venv\Scripts\python.exe -m claude_token_lens install-service`.) It stops the
running dashboard, re-registers the task for this install and starts it
again. On Linux it restarts the service too; on macOS run
`launchctl kickstart -k gui/$(id -u)/com.claude-token-lens` instead.
The dashboard's footer shows the version it is running. If a new
version reads transcripts differently, the dashboard re-reads them once
after the restart, so the first page load can be slow.

## Troubleshooting

| Symptom | Fix |
|---|---|
| WSL sessions missing from the dashboard | Run `init` again and say yes to the WSL folder, or add it to `extra_projects_roots` in `config.toml` and run `install-service` to restart the dashboard. See the README's [Using Claude Code in WSL too](../README.md#using-claude-code-in-wsl-too) |
| Dashboard still shows the old version after an update (see its footer) | Something else still holds port 8765: an older copy started by hand, from another Python install, or from Docker. The README's [An old dashboard won't go away](../README.md#an-old-dashboard-wont-go-away) shows how to find and stop it; then run `install-service` with the Python you updated (section 8) |
| `claude-token-lens` not found | Use the full path to the venv's `Scripts\claude-token-lens.exe`, or `python -m claude_token_lens` (works regardless of `PATH`) |
| The Data quality tab says the SessionStart hook isn't running | The hook command names a Python that isn't installed (`py` with no launcher), uses `%USERPROFILE%` (Claude Code runs hooks through Git Bash, which doesn't expand it), or has a path broken by single backslashes in JSON. Run `claude-token-lens init --repair-hook`: it shows the fixed command and changes it without asking, after copying `settings.json` to `settings.json.bak-<UTC time>`. It keeps your own Python when it's found and writes any `%VARIABLE%` out in full; otherwise it names your main Python install by full path. It can only fix a command whose script exists: if the script is missing, run `claude-token-lens init --connect` first, which copies it back into `<config-dir>\hooks\` |
| No usage-limit readings | The statusline runs only in Claude Code in a terminal, not in the desktop app or an IDE. Amounts stay list-price equivalents until readings arrive |
| `py` launcher missing (`'py' is not recognized`) | Use `python`/`python3` directly, or reinstall Python from python.org with "py launcher" checked |
| Python 3.10 or older | `pip install` refuses (`Requires-Python`); the `.pyz` fails at import with a `tomllib`-related error. Install 3.11+ (a user-level install needs no admin rights) |
| Execution policy blocks a `.ps1` script | `install-service`/`init` never need this — they shell out via `powershell.exe -ExecutionPolicy Bypass -Command ...` themselves. Only affects the legacy `scripts\windows\Register-TokenLensTask.ps1` path; run it the same way: `powershell -ExecutionPolicy Bypass -File scripts\windows\Register-TokenLensTask.ps1` |
| Corporate proxy blocks `pip`/PyPI/GitHub | Use Route A (`.pyz`) — no network access needed once downloaded |
| `CLAUDE_CONFIG_DIR` already set (for Claude Code itself) | Harmless — `claude-token-lens` reads it too and keeps its own files in `<CLAUDE_CONFIG_DIR>\token-lens`. The connect step, `--repair-hook`, `apply`, `changes`, `uninstall` and the dashboard all use `<CLAUDE_CONFIG_DIR>\settings.json`; the ones that change it show the change first. `--config-dir` moves only this tool's own folder: `settings.json` is never looked for beside it, and the hook and statusline commands `init` adds then carry the same `--config-dir`, so snapshots and the usage log land where the dashboard reads them. Pass `--claude-root` to name Claude Code's folder yourself |
| Port 8765 already in use | `claude-token-lens serve --port <other>`, or `claude-token-lens install-service --port <other>` for the logon task (`init`'s service step always uses 8765). The dashboard and `/api/health` URLs change to match |

## POSIX (Linux/macOS) quick variant

Everything above works the same way; the differences are the launcher
name and default paths.

```bash
python3 --version                       # needs 3.11+

# Route A: .pyz
python3 claude-token-lens.pyz --version
python3 claude-token-lens.pyz init

# Route B: pip from a local clone
python3 -m venv .venv
.venv/bin/pip install <path-to-the-cloned-repo>
.venv/bin/claude-token-lens --version    # or: .venv/bin/python -m claude_token_lens --version

# Route C: pip from GitHub
python3 -m pip install git+https://github.com/PaulMorrisDev/claude-token-lens
python3 -m pip install https://github.com/PaulMorrisDev/claude-token-lens/archive/refs/heads/main.zip

claude-token-lens init
claude-token-lens install-service --dry-run   # prints the systemd user unit / LaunchAgent plan; writes nothing
curl http://127.0.0.1:8765/api/health
claude-token-lens report
claude-token-lens uninstall --revert-changes --delete-data --dry-run   # look first, then run without --dry-run
```

Config defaults to `~/.claude/token-lens`/`~/.claude/projects`
(or `$CLAUDE_CONFIG_DIR/token-lens`/`.../projects`). See
[`docs/deploy.md`](deploy.md) for what `install-service` actually
registers on Linux (`systemctl --user`) and macOS (`launchctl`), and
[`docs/onboarding.md`](onboarding.md) for the full `init`/`baseline`
question set and report shape.
