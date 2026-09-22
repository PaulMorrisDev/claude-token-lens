# First run on a locked-down work machine

The one document to follow on a machine you don't fully control: no
admin rights, Python 3.11+ already there (or not), maybe no `git`,
maybe no proxy access to PyPI. Every command below was actually run
end-to-end against a synthetic, throwaway project during this
document's own verification pass (three install routes, `init`,
`install-service --dry-run`, `report`, `serve` + a health check) —
nothing here is aspirational.

If you're comfortable with the tool already, [section 2 of the
README](../README.md#2-installing-and-first-run) is the fast path. This document is
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
pip install git+https://github.com/PaulMorrisDev/claude-token-lens
```

No `git` on the machine? This works without it (a plain HTTPS
download, no `git clone`):

```powershell
pip install https://github.com/PaulMorrisDev/claude-token-lens/archive/refs/heads/main.zip
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
| Billing mode (api/subscription) | Which Claude Code billing model to price against |
| Comma-separated project slugs to always exclude | Skip these projects' transcripts everywhere (reports, dashboard, exports) |
| Do you launch Claude Code with `--settings`/`CLAUDE_CONFIG_DIR` overlays | Affects where a later `apply` writes a profile |
| Are this project's agents/skills shared with colleagues | Same — affects `apply`'s default scope |
| Timezone | Used for day-boundary grouping in reports; blank uses the machine's own local zone |
| Default scope for applying a profile | `user`/`project-local`/`repo` — where a future `apply` writes by default |
| Onboarding capture window length in days | How long `baseline` waits before it has enough data for a confident first read (default 7) |

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

**What `init` writes**, all under `<config-dir>` (nothing outside it,
and nothing to `settings.json` directly — see the next point):

- `config.toml` — your answers above.
- `projects\<slug>.toml` — this project's own settings (merged into an
  existing file key-by-key; if the shape can't be merged automatically,
  `config.toml.new` is written instead and `init` says so, leaving the
  original untouched).
- `baselines\<id>.json` + `<id>.md` — an initial baseline, if any
  sessions were already found for this project.

**`settings.json` fragments — printed, never written.** `init` prints
two JSON snippets (the `SessionStart` hook and the `statusLine`
command) for you to merge into `~/.claude/settings.json` by hand — this
tool never edits that file itself during `init` (only `claude-token-lens
apply`, a separate, optional command, does — and *that* command backs
up every file it touches first, to `<config-dir>\backups\<timestamp>\`,
before writing anything). Skip printing them with `--no-install`.

## 3. The logon service

`init`'s last step asks whether to register `claude-token-lens serve`
to start automatically at logon (default yes; `--no-service` skips the
question entirely, `--install-service` answers it yes non-interactively).
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

Confirm it actually registered, two ways:

```powershell
schtasks /Query /TN ClaudeTokenLens
curl http://127.0.0.1:8765/api/health
```

The second's JSON body includes `"service_registered": true` once
`is_registered()`'s own platform probe (the same `schtasks` query
above) has confirmed it — `false` or `null` (probe inconclusive) means
check the output `install-service` printed. The dashboard's Overview
tab also shows a banner when this comes back `false`.

## 4. Open the dashboard

```
http://127.0.0.1:8765
```

Live once the service is running (either just now via
`install-service`, or by running `claude-token-lens serve` directly in
a terminal you leave open). Loopback-only by default — nothing outside
this machine can reach it unless you explicitly pass `--allow-remote`.

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

## 7. Uninstall completely

```powershell
claude-token-lens uninstall-service   # removes the Scheduled Task / systemd unit / LaunchAgent
```

Then by hand:

1. Remove the two fragments `init` had you paste into
   `~/.claude/settings.json` (the `SessionStart` hook entry and the
   `statusLine` entry) — or restore a backup from `<config-dir>\backups\`
   if `apply` wrote them for you.
2. Delete `<config-dir>` (default `%USERPROFILE%\.claude\token-lens`) —
   the SQLite store, config, snapshots, and baselines all live there
   and nowhere else.
3. If installed via `pip`: `pip uninstall claude-token-lens`. Via
   `.pyz`: just delete the one file.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `claude-token-lens` not found | Use the full path to the venv's `Scripts\claude-token-lens.exe`, or `python -m claude_token_lens` (works regardless of `PATH`) |
| `py` launcher missing (`'py' is not recognized`) | Use `python`/`python3` directly, or reinstall Python from python.org with "py launcher" checked |
| Python 3.10 or older | `pip install` refuses (`Requires-Python`); the `.pyz` fails at import with a `tomllib`-related error. Install 3.11+ (a user-level install needs no admin rights) |
| Execution policy blocks a `.ps1` script | `install-service`/`init` never need this — they shell out via `powershell.exe -ExecutionPolicy Bypass -Command ...` themselves. Only affects the legacy `scripts\windows\Register-TokenLensTask.ps1` path; run it the same way: `powershell -ExecutionPolicy Bypass -File scripts\windows\Register-TokenLensTask.ps1` |
| Corporate proxy blocks `pip`/PyPI/GitHub | Use Route A (`.pyz`) — no network access needed once downloaded |
| `CLAUDE_CONFIG_DIR` already set (for Claude Code itself) | Harmless — `claude-token-lens` reads it too and uses `<CLAUDE_CONFIG_DIR>\token-lens` as its own subdirectory, never touching Claude Code's own files there. Override with `--config-dir` if you want this tool somewhere else entirely |
| Port 8765 already in use | `claude-token-lens serve --port <other>` (and pass the same `--port` to `install-service`); `/api/health`'s URL and the dashboard link both change to match |

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
pip install git+https://github.com/PaulMorrisDev/claude-token-lens
pip install https://github.com/PaulMorrisDev/claude-token-lens/archive/refs/heads/main.zip

claude-token-lens init
claude-token-lens install-service --dry-run   # writes a systemd user unit / LaunchAgent plan
curl http://127.0.0.1:8765/api/health
claude-token-lens report
claude-token-lens uninstall-service
```

Config defaults to `~/.claude/token-lens`/`~/.claude/projects`
(or `$CLAUDE_CONFIG_DIR/token-lens`/`.../projects`). See
[`docs/deploy.md`](deploy.md) for what `install-service` actually
registers on Linux (`systemctl --user`) and macOS (`launchctl`), and
[`docs/onboarding.md`](onboarding.md) for the full `init`/`baseline`
question set and report shape.
