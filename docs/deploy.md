# Deploying the service

`claude-token-lens serve` (`docs/api.md`) needs to run *somewhere*
continuously to keep its store fresh and its JSON API/web UI
(`docs/ui.md`) reachable. This document covers the three supported
hosting paths, in the order the project's plan prioritises them: a
native OS-level service first (no container runtime, no admin/root
rights), then Docker for anyone who'd rather manage it that way.

All three run the exact same `claude-token-lens serve` command
underneath; they differ only in how that process is started, kept
running, and sandboxed by the host.

## Path 1: Windows Scheduled Task (native, no admin rights)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\Register-TokenLensTask.ps1
```

Registers a Scheduled Task, triggered at logon, running:

```
pythonw -m claude_token_lens serve --projects-root "$env:USERPROFILE\.claude\projects" --config-dir "$env:USERPROFILE\.claude\token-lens"
```

- **Runs as the logged-in user, `-RunLevel Limited`** — no admin
  rights requested or required. The service only ever reads
  `%USERPROFILE%\.claude\projects` and reads/writes
  `%USERPROFILE%\.claude\token-lens`; nothing it does needs elevation.
- **`pythonw`, not `python`** — no console window appears at logon.
- **Falls back to `schtasks /create`** automatically if the
  `ScheduledTasks` PowerShell module is unavailable (some locked-down
  corporate images restrict it even for non-admin users).
- `-BillingMode {api,subscription}` forwards `--billing-mode` (see
  `docs/api.md`'s CLI flags section) if you want it set at
  registration time rather than via `config.toml`.

To remove it and stop any running instance:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\Unregister-TokenLensTask.ps1
```

This unregisters the task (by whichever mechanism registered it) and
searches for any `python.exe`/`pythonw.exe` process whose command line
invokes `claude_token_lens` via `Get-CimInstance Win32_Process`,
stopping it — a Scheduled Task's own action process carries no other
marker to find it by.

**What this path can/cannot touch:** everything runs as your own
Windows user account. It can read/write anything you can. It never
requests elevation, never installs a Windows service (a Scheduled Task
is not a Windows Service — no `services.msc` entry, no SYSTEM
account), and never touches the registry beyond the Task Scheduler's
own task definition store.

## Path 2: systemd user unit (native, POSIX)

```bash
mkdir -p ~/.config/systemd/user
cp scripts/systemd/claude-token-lens.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-token-lens.service
```

Runs `claude-token-lens serve --projects-root ~/.claude/projects
--config-dir ~/.claude/token-lens` as your own user, restarting on
failure (`Restart=on-failure`).

**What this path can/cannot touch:**

- **`ProtectHome=read-only`** makes your entire home directory
  read-only to the unit by default.
- **`ReadWritePaths=%h/.claude/token-lens`** carves out the one
  exception: the service's own SQLite store, config, and snapshots
  directory. It cannot write anywhere else under your home directory,
  even though it can *read* `~/.claude/projects` for transcripts (a
  read-only-by-default home directory still permits reads; only writes
  are blocked outside `ReadWritePaths`).
- **`ProtectSystem=strict`**, **`NoNewPrivileges=true`**,
  **`PrivateTmp=true`** — standard systemd sandboxing beyond the
  home-directory restriction above.
- **`PrivateNetwork`** is deliberately left at its default (network
  access available) rather than set to `yes` — unlike the Docker image
  below, this unit runs directly on the host and needs its bound port
  reachable the ordinary way. The "never opens an outbound connection"
  guarantee (`docs/api.md`'s "Local only" section) is enforced by the
  service's own code (`tests/test_service_egress.py`), not by a network
  namespace, for this path.

**Logs:** `journalctl --user -u claude-token-lens.service -f`.

**Surviving logout** (e.g. a headless server with no interactive
session): `loginctl enable-linger $USER` once, so the user unit keeps
running after you log out.

**Stopping it:** `systemctl --user disable --now claude-token-lens.service`.

## Path 3: Docker

```bash
docker compose up -d
```

Builds the image from the repository's own `Dockerfile` (deliverable
2.a) and starts it per `docker-compose.yml` (deliverable 2.b). Set
`CLAUDE_HOME` first if the default (`~/.claude`) isn't where your
Claude Code config lives, or if you want to be certain what path is
actually used regardless of Compose version — see the comment in
`docker-compose.yml` on `~`-expansion, and `docker compose config` to
check what it resolved to before `up`:

```bash
export CLAUDE_HOME=/home/you/.claude   # or set it in a .env file next to docker-compose.yml
docker compose up -d
```

Windows (Docker Desktop) example: `CLAUDE_HOME=C:/Users/<you>/.claude`.

**What this path can/cannot touch:**

- **Read-only bind mount** of your Claude Code config directory to
  `/data/claude` inside the container (`:ro` — the container cannot
  write to it, even if a future bug tried).
- **A named volume** (`token-lens-data`) for the service's own SQLite
  store and any monthly reports — isolated from the host filesystem
  entirely; nothing outside the container can read it directly.
- **`127.0.0.1:8765:8765`** — the published port is loopback-only on
  the host, regardless of the container's own `--bind 0.0.0.0` (see the
  Dockerfile's comment: a process bound to `127.0.0.1` *inside* a
  container is unreachable from outside it too, so the container itself
  must bind `0.0.0.0` for port publishing to work at all — exposure
  still stays loopback-only from the host's perspective, enforced by
  the `127.0.0.1:` prefix on the port mapping, not by the container's
  own bind address).
- **`read_only: true`** root filesystem with a `tmpfs` `/tmp`, **`cap_drop:
  [ALL]`**, **`no-new-privileges:true`** — a compromised process inside
  the container can neither escalate privileges nor write anywhere but
  the two mounts above and `/tmp`.
- **`network_mode: bridge`**, explicitly — not `none`. `none` would
  also block the one connection this service is supposed to accept
  (the published port); see the next section for how to actually prove
  the service needs no *outbound* connectivity without disabling the
  inbound one it does need.

## Verifying no egress

Three layers, from "always runs" to "manual, occasional":

1. **`tests/test_service_egress.py`** — runs on every `pytest`
   invocation, monkeypatches `socket.socket.connect` for the lifetime
   of a real test server, and asserts every recorded connection target
   is the test client's own loopback address. This is the primary,
   always-on guarantee.
2. **`tests/test_service_docker.py`** — line-based checks that the
   Dockerfile/docker-compose.yml hardening (non-root user, no
   curl/wget/apt-get, `read_only`, `cap_drop`, loopback-only port)
   hasn't silently regressed.
3. **Manual Docker smoke test** — proves the *built image*, not just
   the Python code, needs no outbound connectivity, by removing network
   access from the container entirely and confirming the service still
   answers:

   ```bash
   docker build -t claude-token-lens:smoke .
   docker run -d --rm --network none --name ctl-smoke \
     -v "$CLAUDE_HOME:/data/claude:ro" \
     -v ctl-smoke-data:/data/token-lens \
     claude-token-lens:smoke \
     --projects-root /data/claude/projects --config-dir /data/token-lens \
     --bind 0.0.0.0 --allow-remote

   # From inside the container -- there is no host-published port to
   # curl from outside when --network none is used, so the check runs
   # where the service actually is:
   docker exec ctl-smoke python -c \
     "import json,urllib.request as u; r=u.urlopen('http://127.0.0.1:8765/api/health', timeout=4); assert json.load(r)['ok'] is True; print('OK')"

   docker stop ctl-smoke
   docker volume rm ctl-smoke-data
   ```

   `--projects-root`/`--config-dir` are passed explicitly here (review
   finding 4) rather than left to `claude-token-lens serve`'s own
   argparse defaults: any bare `docker run <image> <args>` replaces the
   image's `CMD` entirely (the fixed `ENTRYPOINT` in the `Dockerfile`
   only supplies `claude-token-lens serve`), so omitting them would
   silently fall back to a `~`-relative default inside the container
   instead of the `/data/claude`/`/data/token-lens` mount points this
   image and `docker-compose.yml` are actually built around. The named
   `ctl-smoke-data` volume in particular is what proves the Dockerfile's
   `chown -R token-lens:token-lens /data/token-lens` (finding 4) is
   doing its job: a *fresh* named volume is seeded from that path's
   ownership in the image, so the non-root `token-lens` user can create
   `service.db` in it on first start without a manual `docker exec ...
   chown` step.

   A `--network none` container has no network namespace connectivity
   at all beyond loopback — if `/api/health` still answers `ok: true`
   from inside it, the service provably made no outbound connection to
   get there. This has been run against this repository's own
   `Dockerfile`/`docker-compose.yml` (Docker 29.7.2) as part of
   S1-integration: build succeeds, a `--once` tick and a full `serve`
   both complete under `--network none`, and `/api/health` answers
   correctly from inside the container.

## Distribution without pip: the `.pyz` build

For a machine where `pip install` is unavailable or unwanted (no
internet access to PyPI, a locked-down environment, or just "copy one
file and run it"), `claude-token-lens` has zero third-party
dependencies (`pyproject.toml`'s `dependencies = []`), which makes a
single-file [zipapp](https://docs.python.org/3/library/zipapp.html)
distribution straightforward:

```bash
python scripts/build-pyz.py
# -> dist/claude-token-lens.pyz

python dist/claude-token-lens.pyz --version
python dist/claude-token-lens.pyz serve --projects-root ~/.claude/projects --config-dir ~/.claude/token-lens
```

Equivalent, if you'd rather invoke `zipapp` yourself directly, to:

```bash
python -m zipapp src -m "claude_token_lens.__main__:main" -o dist/claude-token-lens.pyz -p "/usr/bin/env python3"
```

`scripts/build-pyz.py` does the same thing (via the `zipapp` module's
Python API rather than shelling out), plus: copies `src/claude_token_lens/`
into a clean temporary directory first, skipping `__pycache__`, so a
stray compiled-bytecode cache from your own dev environment never ends
up inside the shipped archive; and includes `service/static/*` (the web
UI) automatically, since it's just an ordinary file tree already living
under `src/claude_token_lens/service/static/` — no separate packaging
step needed.

**Why `claude_token_lens.__main__:main`, not `claude_token_lens.cli:main`:**
zipapp's generated archive-root `__main__.py` (from the `-m`/`main=`
argument) is just `import <module>; <module>.<function>()` — it does
**not** wrap that call in `sys.exit(...)`, so a target function's
returned int exit code would otherwise be silently discarded and the
process would always exit 0. `claude_token_lens/__main__.py` (the
existing `python -m claude_token_lens` entry point) already solves this
for itself: its own top-level statement is `sys.exit(main())`, which
runs the instant `import claude_token_lens.__main__` executes and
raises `SystemExit` with the real code — propagating out through the
zipapp-generated wrapper's own `import` line before its `.main()` call
is ever reached. Pointing zipapp at `cli:main` instead would reproduce
exactly the bug `__main__.py`'s own docstring warns about.

`tests/test_service_build_pyz.py` builds the archive and actually runs
it (`subprocess`, real Python interpreter) to confirm `--version` works
and a non-zero exit code (a stub subcommand) survives the round trip —
verified locally as part of S1-integration.

## Retention and purge

A transcript file the watcher can no longer find on disk (removed by
Claude Code's own `cleanupPeriodDays`, or by hand) is never deleted from
the store on the spot — `Store.remove_missing` only marks its
`missing_since` timestamp (clearing it again if a file at the same path
reappears). `report.*`/the UI keep including it exactly like a
transcript still on disk (`GET /api/health`'s `transcripts_missing`
reports the current count; see [docs/api.md](api.md)). The service
store is deliberately designed to outlive Claude Code's own retention
window, not mirror it — the two options below are the *only* things
that actually delete a row.

- **`--retention-days N`** (existing `serve` flag): every watcher poll
  tick prunes sessions whose transcripts were all last active more than
  `N` days ago (`Store.retention_prune`) — this is what actually deletes
  a marked-missing (or still-present) transcript's row, not the
  missing-file check itself. Off by default — nothing is ever pruned
  unless you opt in.
- **`serve --purge`** (deliverable 2.e): deletes `<config-dir>/service.db`
  and its `-wal`/`-shm` sidecars, then exits — never starts the watcher
  or API. Always prints exactly which files it would delete first; only
  actually deletes them with `--yes`:

  ```bash
  claude-token-lens serve --config-dir ~/.claude/token-lens --purge
  # claude-token-lens serve --purge: will delete:
  #   /home/you/.claude/token-lens/service.db
  # Re-run with --yes to actually delete these files.

  claude-token-lens serve --config-dir ~/.claude/token-lens --purge --yes
  # Deleted 1 file(s).
  ```

  Safe at any time: the store is always a derived cache, never source
  of truth (`service/store.py`'s module docstring) — the next `serve`
  run simply rebuilds it from the transcripts already on disk, the same
  way a schema-version bump's drop-and-rebuild migration
  (`Store.migrate()`) does.
