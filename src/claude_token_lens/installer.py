"""``claude-token-lens install-service``/``uninstall-service`` (v3
milestone): register ``claude-token-lens serve`` to start automatically
at logon/boot, on Windows (Scheduled Task), Linux (systemd user unit)
and macOS (LaunchAgent) -- the same three native paths
``docs/deploy.md`` already documents by hand, now driven from Python so
``init`` can offer it as its own final step.

Why this exists: Claude Code deletes a project's own transcripts after
``cleanupPeriodDays``, and this project's store is only ever fed by a
*running* ``serve`` watcher (``service/watcher.py``) -- a service that
only ever starts when someone remembers to run it manually loses
history the moment the underlying JSONL files are cleaned up. Only a
continuously running, logon-registered service actually keeps that
history.

Safety posture (this module never touches the machine on its own):

- **Building a plan never has side effects.** :func:`plan_service_install`
  only computes what *would* be written/run -- no file write, no
  ``subprocess`` call, not even a platform-specific import gated behind
  a live check. This is what makes ``init --dry-run``/
  ``install-service --dry-run`` and every test in this module possible
  without ever touching a real Scheduled Task, systemd unit or
  LaunchAgent.
- **Every side-effecting call goes through an injected ``runner``**
  (default :func:`subprocess.run`, overridable by any caller -- tests
  pass a recording stand-in that never actually invokes ``schtasks``/
  ``systemctl``/``launchctl``). :func:`install`/:func:`uninstall` both
  take one.
- **``dry_run`` always means "print exactly what would happen, then
  stop"** -- no file is written, no command is run, regardless of
  which platform's plan it is.

Nothing here duplicates ``scripts/windows/Register-TokenLensTask.ps1``/
``Unregister-TokenLensTask.ps1``/``scripts/systemd/claude-token-lens.service``
outright -- those remain the hand-run, copy-pasteable path
``docs/deploy.md`` documents; this module is the same idea driven from
``claude-token-lens init``/``install-service`` instead, and deliberately
mirrors their flags/hardening choices (``-RunLevel Limited``,
``ProtectHome=read-only`` + a carved-out ``ReadWritePaths``, ...) rather
than inventing new ones.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

__all__ = [
    "InstallerError",
    "InstallPlan",
    "detect_platform",
    "detect_pyz_path",
    "plan_service_install",
    "is_registered",
    "relaunch_after_exit",
    "registered_python",
    "install",
    "uninstall",
    "TASK_NAME",
    "SYSTEMD_UNIT_NAME",
    "LAUNCHD_LABEL",
]


class InstallerError(Exception):
    """An unsupported platform, or a command the injected runner
    reports as failed. Same "stand-alone, user-facing" convention as
    :class:`~claude_token_lens.config.ConfigError`.
    """


#: The three platforms this module knows how to plan for --
#: :func:`detect_platform` never returns anything outside this set.
_SUPPORTED_PLATFORMS = ("windows", "linux", "macos")

#: Names used consistently across the plan, the probe command and the
#: uninstall commands, so all three always agree on what to look for.
TASK_NAME = "ClaudeTokenLens"
SYSTEMD_UNIT_NAME = "claude-token-lens.service"
LAUNCHD_LABEL = "com.claude-token-lens"


def detect_platform() -> str:
    """``"windows"``/``"macos"``/``"linux"`` -- everything that isn't
    Windows or macOS is treated as a systemd-capable Linux, matching
    ``docs/deploy.md``'s own two native, non-Windows paths (systemd
    user unit; there is no fourth path documented).
    """
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def detect_pyz_path() -> Path | None:
    """The ``.pyz`` archive this process was launched from, or ``None``
    when running from an ordinary installed package/checkout.
    ``zipapp``'s own bootstrap runs as ``python <archive>.pyz ...``, so
    ``sys.argv[0]`` is the archive path in that case -- confirmed by
    ``scripts/build-pyz.py``/``tests/test_service_build_pyz.py``'s own
    build-and-run round trip. A real zip check (not just a ``.pyz``
    name check) avoids a false positive from a coincidentally-named
    file.

    Always returns an **absolute** path (``.resolve()``), even when
    ``sys.argv[0]`` itself was relative (e.g. the user ran ``py -3
    claude-token-lens.pyz ...`` from the archive's own directory) --
    every caller (:func:`_serve_argv`'s Scheduled-Task/systemd/launchd
    action, ``statusline.print_install_fragment``) embeds this path
    verbatim into a command line that a logon-triggered service or a
    pasted-in settings.json fragment will later run from a *different*
    working directory, where a relative path would silently fail to
    resolve.
    """
    import zipfile

    argv0 = sys.argv[0] if sys.argv else ""
    if not argv0:
        return None
    path = Path(argv0)
    try:
        if path.is_file() and zipfile.is_zipfile(path):
            return path.resolve()
    except OSError:
        return None
    return None


def _uid() -> str:
    """``os.getuid()`` as a string on a platform that has it, else the
    literal shell form ``$(id -u)`` this project's own docs/task
    description use -- reached only when planning for macOS from a
    machine that has no ``os.getuid`` at all (Windows), which only
    happens in a test that monkeypatches ``sys.platform`` to exercise
    the macOS plan shape without actually being on macOS.
    """
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return "$(id -u)"
    return str(getuid())


def _ps_quote(value: str) -> str:
    """Quote ``value`` as a single PowerShell string literal (wrap in
    single quotes, double any embedded single quote) -- the one escaping
    rule PowerShell itself defines for a literal string, so a path
    containing a space (``C:\\Users\\Jane Doe\\...``) round-trips intact
    the same way ``Register-TokenLensTask.ps1``'s own quoting already
    has to handle.
    """
    return "'" + value.replace("'", "''") + "'"


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _resolve_windows_exe(python_exe: str) -> str:
    """``pythonw.exe`` beside ``python_exe`` when it exists (no console
    window at logon, matching ``Register-TokenLensTask.ps1``), else
    ``python_exe`` itself.
    """
    pythonw = Path(python_exe).with_name("pythonw.exe")
    if pythonw.exists():
        return str(pythonw)
    return python_exe


def _serve_argv(exe: str, pyz_path: Path | None, serve_args: list[str]) -> list[str]:
    if pyz_path is not None:
        return [exe, str(pyz_path), *serve_args]
    return [exe, "-m", "claude_token_lens", *serve_args]


@dataclass(slots=True)
class InstallPlan:
    """Everything :func:`install`/:func:`uninstall` need, entirely
    precomputed by :func:`plan_service_install` -- building one never
    touches the filesystem or spawns a process (see module docstring).
    """

    platform: str
    #: One human-readable line describing what this plan does, printed
    #: verbatim by ``install``/``init``/``install-service`` before
    #: anything is written or run.
    description: str
    #: Files this plan writes on install (systemd/launchd only --
    #: empty for Windows, which builds the Scheduled Task entirely via
    #: inline PowerShell cmdlets, no file of its own).
    files_to_write: dict[Path, str] = field(default_factory=dict)
    #: Commands run, in order, by ``install()``.
    commands: list[list[str]] = field(default_factory=list)
    #: Commands run, in order, by ``uninstall()``.
    uninstall_commands: list[list[str]] = field(default_factory=list)
    #: One plain sentence per ``uninstall_commands`` entry, same order,
    #: printed by ``uninstall()`` once that command has succeeded, so the
    #: output says what actually happened (stopped, removed) rather than
    #: only what was about to be run.
    uninstall_done: list[str] = field(default_factory=list)
    #: Files removed by ``uninstall()`` (the same paths as
    #: ``files_to_write``'s keys, for the platforms that wrote any).
    uninstall_files: list[Path] = field(default_factory=list)
    #: The platform's own query command for :func:`is_registered` --
    #: also carried on the plan so ``install``/``install-service`` can
    #: print exactly what a later health check will run.
    probe_command: list[str] = field(default_factory=list)
    #: Extra operator notes printed alongside the plan (e.g. macOS/
    #: Linux's "keep running after logout" caveat) -- never anything
    #: this module can act on by itself.
    notes: list[str] = field(default_factory=list)


# -- platform-specific plan builders -----------------------------------


def _plan_windows(python_exe: str, serve_args: list[str], pyz_path: Path | None) -> InstallPlan:
    exe = _resolve_windows_exe(python_exe)
    argv = _serve_argv(exe, pyz_path, serve_args)
    action_exe, action_args = argv[0], argv[1:]
    argument_list = " ".join(action_args)

    # Mirrors Register-TokenLensTask.ps1's own cmdlet sequence exactly
    # (-AtLogOn scoped to this one account, -RunLevel Limited, no
    # execution time limit) -- see that script's own comments for why
    # each flag is there. Built as one semicolon-joined -Command string
    # so every cmdlet shares the same PowerShell process and its
    # variables ($action, $trigger, ...). It stops a copy the task already
    # started first and starts the new one last, so re-running
    # install-service after an update switches the dashboard to the new
    # code straight away (and a first install starts it without waiting
    # for the next logon, like systemctl --now and launchctl bootstrap).
    ps_script = (
        f"Stop-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -ErrorAction SilentlyContinue; "
        f"$action = New-ScheduledTaskAction -Execute {_ps_quote(action_exe)} "
        f"-Argument {_ps_quote(argument_list)}; "
        "$trigger = New-ScheduledTaskTrigger -AtLogOn -User \"$env:USERDOMAIN\\$env:USERNAME\"; "
        "$principal = New-ScheduledTaskPrincipal -UserId \"$env:USERDOMAIN\\$env:USERNAME\" "
        "-LogonType Interactive -RunLevel Limited; "
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 "
        "-RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero); "
        f"Register-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Action $action "
        "-Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null; "
        f"Start-ScheduledTask -TaskName {_ps_quote(TASK_NAME)}"
    )
    install_command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script]
    # Stop first: Unregister-ScheduledTask removes the definition but
    # leaves a copy the task already started running, so the dashboard
    # would keep serving (and holding service.db open) until the next
    # reboot. Stop-ScheduledTask ends the task's own process, the same
    # thing Unregister-TokenLensTask.ps1 does by hand.
    stop_command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        f"Stop-ScheduledTask -TaskName {_ps_quote(TASK_NAME)}",
    ]
    uninstall_command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        f"Unregister-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Confirm:$false",
    ]

    return InstallPlan(
        platform="windows",
        description=(
            f"Register Scheduled Task {TASK_NAME!r} (logon trigger, -RunLevel Limited, "
            f"no admin rights) and start it now, running: {action_exe} {argument_list}"
        ),
        commands=[install_command],
        uninstall_commands=[stop_command, uninstall_command],
        uninstall_done=[
            f"Stopped Scheduled Task {TASK_NAME!r} (a running dashboard is shut down).",
            f"Removed Scheduled Task {TASK_NAME!r}.",
        ],
        probe_command=["schtasks", "/Query", "/TN", TASK_NAME],
    )


#: Mirrors scripts/systemd/claude-token-lens.service's own hardening
#: (see that file's comments for the rationale behind each setting) --
#: the one difference is ExecStart/ReadWritePaths use this plan's own
#: resolved, absolute values rather than %h, since this module always
#: knows the real config_dir at plan-build time.
_SYSTEMD_UNIT_TEMPLATE = """\
# claude-token-lens serve -- systemd user unit, written by
# `claude-token-lens install-service` (mirrors
# scripts/systemd/claude-token-lens.service). See docs/deploy.md.

[Unit]
Description=claude-token-lens: local token/prompt-cache analytics service
After=network.target

[Service]
Type=simple
ExecStart={exec_line}
Restart=on-failure
RestartSec=5

ProtectHome=read-only
ReadWritePaths={config_dir}
ProtectSystem=strict
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
"""


def _plan_linux(python_exe: str, serve_args: list[str], pyz_path: Path | None, config_dir: Path) -> InstallPlan:
    argv = _serve_argv(python_exe, pyz_path, serve_args)
    exec_line = " ".join(argv)
    unit_path = Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
    unit_content = _SYSTEMD_UNIT_TEMPLATE.format(exec_line=exec_line, config_dir=config_dir)

    return InstallPlan(
        platform="linux",
        description=f"Write a systemd user unit at {unit_path} and enable it: {exec_line}",
        files_to_write={unit_path: unit_content},
        commands=[
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME],
            # --now leaves an already-running copy alone; restart it so
            # re-running install-service after an update runs the new code.
            ["systemctl", "--user", "restart", SYSTEMD_UNIT_NAME],
        ],
        # --now stops the running service as well as disabling it.
        uninstall_commands=[["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME]],
        uninstall_done=[f"Stopped and disabled {SYSTEMD_UNIT_NAME} (a running dashboard is shut down)."],
        uninstall_files=[unit_path],
        probe_command=["systemctl", "--user", "is-enabled", "claude-token-lens"],
        notes=[
            "For this to keep running after you log out (e.g. a headless server), "
            "also run once: loginctl enable-linger $USER"
        ],
    )


_LAUNCHD_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{program_args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
"""


def _plan_macos(python_exe: str, serve_args: list[str], pyz_path: Path | None) -> InstallPlan:
    argv = _serve_argv(python_exe, pyz_path, serve_args)
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    program_args_xml = "\n".join(f"        <string>{_xml_escape(a)}</string>" for a in argv)
    plist_content = _LAUNCHD_PLIST_TEMPLATE.format(label=LAUNCHD_LABEL, program_args=program_args_xml)
    uid = _uid()
    target = f"gui/{uid}/{LAUNCHD_LABEL}"

    return InstallPlan(
        platform="macos",
        description=f"Write a LaunchAgent at {plist_path} and bootstrap it: {' '.join(argv)}",
        files_to_write={plist_path: plist_content},
        commands=[["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)]],
        # bootout stops the running agent as well as unloading it.
        uninstall_commands=[["launchctl", "bootout", target]],
        uninstall_done=[f"Stopped and unloaded {LAUNCHD_LABEL} (a running dashboard is shut down)."],
        uninstall_files=[plist_path],
        probe_command=["launchctl", "print", target],
    )


def plan_service_install(
    python_exe: str,
    projects_root: Path | str | list[Path | str],
    config_dir: Path | str,
    *,
    port: int = 8765,
    bind: str = "127.0.0.1",
    platform: str | None = None,
    pyz_path: Path | None = None,
) -> InstallPlan:
    """Build (never run) the plan to register ``claude-token-lens
    serve --projects-root <projects_root> --config-dir <config_dir>
    --port <port> --bind <bind> --exit-on-code-change`` to start at
    logon/boot.
    ``projects_root`` may be a list: each folder gets its own
    ``--projects-root``. ``config.toml``'s ``extra_projects_roots`` are
    not passed here; ``serve`` reads them each time it starts.

    ``platform`` defaults to :func:`detect_platform` -- passed
    explicitly by tests that monkeypatch ``sys.platform`` and want a
    single, direct call rather than relying on the monkeypatch reaching
    this function's own default-argument evaluation (which happens
    once, at first import, like any other default argument).

    ``pyz_path`` defaults to :func:`detect_pyz_path` (``None`` unless
    this process was itself launched from a ``.pyz``) -- pass one
    explicitly to force the ".pyz action form" (``pythonw.exe
    <path-to-pyz> serve ...``) regardless of how this call is running.
    """
    plat = platform or detect_platform()
    if plat not in _SUPPORTED_PLATFORMS:
        raise InstallerError(f"unsupported platform: {plat!r}")

    if pyz_path is None:
        pyz_path = detect_pyz_path()

    roots = [projects_root] if isinstance(projects_root, (str, Path)) else list(projects_root)
    config_dir = Path(config_dir)
    serve_args = [
        "serve",
        *(arg for root in roots for arg in ("--projects-root", str(Path(root)))),
        "--config-dir",
        str(config_dir),
        "--port",
        str(port),
        "--bind",
        bind,
        # An update that lands without a restart (git pull in an editable
        # install, pip install -U) leaves serve running old code against
        # new files: it exits, and the service manager starts it again.
        "--exit-on-code-change",
    ]

    if plat == "windows":
        return _plan_windows(python_exe, serve_args, pyz_path)
    if plat == "linux":
        return _plan_linux(python_exe, serve_args, pyz_path, config_dir)
    return _plan_macos(python_exe, serve_args, pyz_path)


def _probe_command_for_platform(plat: str) -> list[str]:
    if plat == "windows":
        return ["schtasks", "/Query", "/TN", TASK_NAME]
    if plat == "linux":
        return ["systemctl", "--user", "is-enabled", "claude-token-lens"]
    if plat == "macos":
        return ["launchctl", "print", f"gui/{_uid()}/{LAUNCHD_LABEL}"]
    raise InstallerError(f"unsupported platform: {plat!r}")


def is_registered(
    platform: str | None = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool | None:
    """Best-effort probe: ``True``/``False`` when the platform's own
    query command ran and gave a clear answer, ``None`` when it
    couldn't be run at all (an unsupported platform, the query tool
    missing, or any other unexpected error) -- callers (``/api/health``'s
    ``service_registered`` field) must treat ``None`` as "unknown",
    never fold it into ``False``.
    """
    plat = platform or detect_platform()
    try:
        command = _probe_command_for_platform(plat)
    except InstallerError:
        return None
    try:
        result = runner(command, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    returncode = getattr(result, "returncode", None)
    if returncode is None:
        return None
    return returncode == 0


def relaunch_after_exit(
    pid: int,
    platform: str | None = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    spawner: Callable[..., object] = subprocess.Popen,
) -> bool:
    """Arrange for the registered service to start ``serve`` again once
    process ``pid`` (a ``serve --exit-on-code-change`` about to exit) has
    ended, and say whether it will: ``False`` means that ``serve`` should
    stay up instead. Never raises.

    systemd (``Restart=on-failure``) and launchd (``KeepAlive``) restart
    a non-zero exit by themselves, so there is nothing to do. Task
    Scheduler does not: its restart settings never ran the task again
    after its program exited with an error code (tried on Windows 11 with
    exit codes 3 and -3). So when the Scheduled Task is registered, a
    hidden PowerShell waits for ``pid`` to end and then runs
    ``Start-ScheduledTask``, as ``install-service`` does. The task starts
    the new copy, so ``Stop-ScheduledTask`` and ``uninstall-service``
    still reach it.
    """
    plat = platform or detect_platform()
    if plat != "windows":
        return True
    if is_registered(plat, runner=runner) is not True:
        return False
    script = (
        f"Wait-Process -Id {int(pid)} -ErrorAction SilentlyContinue; "
        # Task Scheduler notes the old run as finished a moment after its
        # process ends; a start before then finds the task still running.
        "Start-Sleep -Seconds 2; "
        f"Start-ScheduledTask -TaskName {_ps_quote(TASK_NAME)}"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        spawner(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return True


def registered_python(
    platform: str | None = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str | None:
    """The interpreter the registered service starts ``serve`` with (the
    Scheduled Task's action, the unit's ``ExecStart``, the LaunchAgent's
    first program argument), or ``None`` when there is none or it can't
    be read. ``update`` reads it before re-registering, to find the copy
    the dashboard ran from until now. Never raises."""
    plat = platform or detect_platform()
    try:
        if plat == "windows":
            script = (
                f"(Get-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -ErrorAction Stop).Actions "
                "| Select-Object -First 1 -ExpandProperty Execute"
            )
            result = runner(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if getattr(result, "returncode", 1) != 0:
                return None
            found = (result.stdout or "").strip().strip('"')
            return found or None
        if plat == "linux":
            unit = (Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME).read_text(encoding="utf-8")
            for line in unit.splitlines():
                if line.startswith("ExecStart="):
                    words = line[len("ExecStart="):].split()
                    return words[0] if words else None
            return None
        import plistlib

        with (Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist").open("rb") as fh:
            argv = plistlib.load(fh).get("ProgramArguments") or []
        return str(argv[0]) if argv else None
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return None


def _print_plan(action: str, plan: InstallPlan, *, commands: list[list[str]], files: list[Path]) -> None:
    print(f"claude-token-lens {action}: {plan.description}")
    for path in files:
        print(f"  will write: {path}")
    for command in commands:
        print(f"  will run:   {' '.join(command)}")
    for note in plan.notes:
        print(f"  note: {note}")


def install(
    plan: InstallPlan,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    dry_run: bool = False,
) -> int:
    """Write ``plan.files_to_write`` then run ``plan.commands``, in
    that order (a systemd/launchd unit must exist on disk before
    ``daemon-reload``/``bootstrap`` can see it). Always prints exactly
    what it is about to do before doing it. ``dry_run`` prints the same
    plan and returns without writing or running anything. Returns 0 on
    success; raises :class:`InstallerError` if any command's injected
    ``runner`` reports a non-zero exit code.
    """
    _print_plan("install-service", plan, commands=plan.commands, files=list(plan.files_to_write))

    if dry_run:
        print("Dry run -- nothing written or run.")
        return 0

    for path, content in plan.files_to_write.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"Wrote {path}")

    for command in plan.commands:
        result = runner(command, capture_output=True, text=True)
        returncode = getattr(result, "returncode", 0)
        if returncode != 0:
            stderr = getattr(result, "stderr", "") or ""
            raise InstallerError(f"command failed ({returncode}): {' '.join(command)}\n{stderr}")

    print("Service install complete.")
    return 0


def uninstall(
    plan: InstallPlan,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    dry_run: bool = False,
) -> int:
    """The inverse of :func:`install`: run ``plan.uninstall_commands``
    (each platform's first one also stops a running ``serve``) then
    remove ``plan.uninstall_files``, printing ``plan.uninstall_done``'s
    sentence for each command that succeeded. Best-effort past the printed
    plan -- a command or file removal that fails is reported and
    skipped rather than aborting the rest, the same "always tell you
    exactly what happened" posture as
    ``Unregister-TokenLensTask.ps1``/``cli.py``'s own ``serve --purge``.
    """
    _print_plan("uninstall-service", plan, commands=plan.uninstall_commands, files=plan.uninstall_files)

    if dry_run:
        print("Dry run -- nothing run or removed.")
        return 0

    failures: list[str] = []
    for index, command in enumerate(plan.uninstall_commands):
        try:
            result = runner(command, capture_output=True, text=True)
        except OSError as exc:
            failures.append(f"{' '.join(command)}: {exc}")
            continue
        returncode = getattr(result, "returncode", 0)
        if returncode != 0:
            stderr = getattr(result, "stderr", "") or ""
            failures.append(f"{' '.join(command)}: exit {returncode}: {stderr}".rstrip())
        elif index < len(plan.uninstall_done):
            print(plan.uninstall_done[index])

    for path in plan.uninstall_files:
        try:
            path.unlink()
            print(f"Removed {path}")
        except FileNotFoundError:
            pass
        except OSError as exc:
            failures.append(f"{path}: {exc}")

    if failures:
        print("Service uninstall finished with problems:")
        for failure in failures:
            print(f"  {failure}")
        return 1

    print("Service uninstall complete.")
    return 0
