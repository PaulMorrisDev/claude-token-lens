"""Tests for the v3 ``install-service``/``uninstall-service`` milestone's
:mod:`claude_token_lens.installer`: platform detection, the three
platform plans (Windows/Linux/macOS), dry-run (writes/runs nothing),
install/uninstall call sequences via a recording runner, the ``.pyz``
action form, and :func:`~claude_token_lens.installer.is_registered`.

Every test here uses an injected recording runner instead of the real
``subprocess.run`` -- this module (like the rest of the suite) must
never register a real Scheduled Task, systemd unit or LaunchAgent, or
invoke ``schtasks``/``systemctl``/``launchctl`` for real.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from claude_token_lens import installer


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _RecordingRunner:
    """Records every call it receives and returns a canned result --
    never spawns a real process."""

    def __init__(self, returncode: int = 0):
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        return _FakeResult(returncode=self.returncode)


# --------------------------------------------------------------------
# detect_platform
# --------------------------------------------------------------------


def test_detect_platform_windows(monkeypatch):
    monkeypatch.setattr(installer.sys, "platform", "win32")
    assert installer.detect_platform() == "windows"


def test_detect_platform_macos(monkeypatch):
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    assert installer.detect_platform() == "macos"


def test_detect_platform_linux(monkeypatch):
    monkeypatch.setattr(installer.sys, "platform", "linux")
    assert installer.detect_platform() == "linux"


def test_detect_platform_other_posix_falls_back_to_linux(monkeypatch):
    monkeypatch.setattr(installer.sys, "platform", "freebsd13")
    assert installer.detect_platform() == "linux"


# --------------------------------------------------------------------
# detect_pyz_path
# --------------------------------------------------------------------


def test_detect_pyz_path_none_for_an_ordinary_script(monkeypatch, tmp_path):
    script = tmp_path / "cli.py"
    script.write_text("# not a zip", encoding="utf-8")
    monkeypatch.setattr(installer.sys, "argv", [str(script)])
    assert installer.detect_pyz_path() is None


def test_detect_pyz_path_finds_a_real_zip(monkeypatch, tmp_path):
    archive = tmp_path / "claude-token-lens.pyz"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("__main__.py", "print('hi')\n")
    monkeypatch.setattr(installer.sys, "argv", [str(archive)])
    assert installer.detect_pyz_path() == archive


def test_detect_pyz_path_none_when_argv_empty(monkeypatch):
    monkeypatch.setattr(installer.sys, "argv", [])
    assert installer.detect_pyz_path() is None


def test_detect_pyz_path_resolves_a_relative_argv0(monkeypatch, tmp_path):
    """A user who ``cd``s into the archive's own directory and runs ``py -3
    claude-token-lens.pyz ...`` gets a relative ``sys.argv[0]`` -- but the
    Scheduled Task/systemd/launchd action this feeds
    (``_serve_argv``/``plan_service_install``) runs from a different
    working directory (e.g. Windows starts a logon task from
    ``%SystemRoot%\\System32``), where a relative path would silently fail
    to resolve. ``detect_pyz_path`` must always hand back an absolute path.
    """
    archive = tmp_path / "claude-token-lens.pyz"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("__main__.py", "print('hi')\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(installer.sys, "argv", ["claude-token-lens.pyz"])
    result = installer.detect_pyz_path()
    assert result is not None
    assert result.is_absolute()
    assert result == archive.resolve()


# --------------------------------------------------------------------
# plan_service_install: per-platform shape
# --------------------------------------------------------------------


def test_plan_windows_uses_python_exe_when_no_pythonw_beside_it(tmp_path):
    python_exe = tmp_path / "python.exe"
    python_exe.write_text("", encoding="utf-8")
    plan = installer.plan_service_install(
        str(python_exe), tmp_path / "projects", tmp_path / "config", platform="windows"
    )
    assert plan.platform == "windows"
    assert plan.files_to_write == {}
    assert len(plan.commands) == 1
    command = plan.commands[0]
    assert command[0] == "powershell.exe"
    script = command[-1]
    assert str(python_exe) in script
    assert "serve" in script
    assert "--projects-root" in script
    assert "New-ScheduledTaskTrigger -AtLogOn" in script
    assert "-RunLevel Limited" in script
    assert "ExecutionTimeLimit ([TimeSpan]::Zero)" in script
    # Re-registering after an update swaps the running dashboard for the
    # new code: stop any running copy first, start the new one last.
    assert script.startswith(f"Stop-ScheduledTask -TaskName '{installer.TASK_NAME}' -ErrorAction SilentlyContinue; ")
    assert script.endswith(f"Start-ScheduledTask -TaskName '{installer.TASK_NAME}'")
    assert script.index("Register-ScheduledTask") < script.index("Start-ScheduledTask")
    assert plan.probe_command == ["schtasks", "/Query", "/TN", installer.TASK_NAME]
    # Stop the running task first: unregistering alone leaves serve running.
    assert [c[0] for c in plan.uninstall_commands] == ["powershell.exe", "powershell.exe"]
    assert plan.uninstall_commands[0][-1] == f"Stop-ScheduledTask -TaskName '{installer.TASK_NAME}'"
    assert "Unregister-ScheduledTask" in plan.uninstall_commands[1][-1]
    assert len(plan.uninstall_done) == len(plan.uninstall_commands)


def test_plan_windows_prefers_pythonw_beside_the_interpreter(tmp_path):
    python_exe = tmp_path / "python.exe"
    pythonw_exe = tmp_path / "pythonw.exe"
    python_exe.write_text("", encoding="utf-8")
    pythonw_exe.write_text("", encoding="utf-8")
    plan = installer.plan_service_install(
        str(python_exe), tmp_path / "projects", tmp_path / "config", platform="windows"
    )
    script = plan.commands[0][-1]
    assert str(pythonw_exe) in script
    assert str(python_exe) + " " not in script.replace(str(pythonw_exe), "")


def test_plan_windows_pyz_action_form(tmp_path):
    python_exe = tmp_path / "python.exe"
    python_exe.write_text("", encoding="utf-8")
    pyz_path = tmp_path / "claude-token-lens.pyz"
    plan = installer.plan_service_install(
        str(python_exe),
        tmp_path / "projects",
        tmp_path / "config",
        platform="windows",
        pyz_path=pyz_path,
    )
    script = plan.commands[0][-1]
    assert str(pyz_path) in script
    # The .pyz form invokes the archive directly, not `-m claude_token_lens`.
    assert "-m claude_token_lens" not in script


def test_plan_linux_writes_systemd_unit_with_real_execstart(tmp_path):
    python_exe = "/usr/bin/python3"
    projects_root = tmp_path / "projects"
    config_dir = tmp_path / "config"
    plan = installer.plan_service_install(python_exe, projects_root, config_dir, platform="linux")

    assert plan.platform == "linux"
    assert len(plan.files_to_write) == 1
    (unit_path, content), = plan.files_to_write.items()
    assert unit_path.name == installer.SYSTEMD_UNIT_NAME
    assert str(python_exe) in content
    assert f"--projects-root {projects_root}" in content
    assert f"--config-dir {config_dir}" in content
    assert f"ReadWritePaths={config_dir}" in content
    assert plan.commands == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", installer.SYSTEMD_UNIT_NAME],
        ["systemctl", "--user", "restart", installer.SYSTEMD_UNIT_NAME],
    ]
    assert plan.probe_command == ["systemctl", "--user", "is-enabled", "claude-token-lens"]
    assert plan.uninstall_commands == [["systemctl", "--user", "disable", "--now", installer.SYSTEMD_UNIT_NAME]]
    assert plan.uninstall_files == [unit_path]
    assert any("enable-linger" in note for note in plan.notes)


def test_plan_linux_pyz_action_form(tmp_path):
    pyz_path = tmp_path / "claude-token-lens.pyz"
    plan = installer.plan_service_install(
        "/usr/bin/python3", tmp_path / "projects", tmp_path / "config", platform="linux", pyz_path=pyz_path
    )
    (_, content), = plan.files_to_write.items()
    assert str(pyz_path) in content
    assert "-m claude_token_lens" not in content


def test_plan_macos_writes_launch_agent_plist(tmp_path):
    plan = installer.plan_service_install(
        "/usr/bin/python3", tmp_path / "projects", tmp_path / "config", platform="macos"
    )
    assert plan.platform == "macos"
    (plist_path, content), = plan.files_to_write.items()
    assert plist_path.name == f"{installer.LAUNCHD_LABEL}.plist"
    assert "LaunchAgents" in str(plist_path)
    assert "<key>Label</key>" in content
    assert installer.LAUNCHD_LABEL in content
    assert "/usr/bin/python3" in content
    assert plan.commands[0][0] == "launchctl"
    assert plan.commands[0][1] == "bootstrap"
    assert plan.probe_command[0:2] == ["launchctl", "print"]
    assert plan.uninstall_commands[0][0:2] == ["launchctl", "bootout"]
    assert plan.uninstall_files == [plist_path]


def test_plan_service_install_rejects_unsupported_platform(tmp_path):
    with pytest.raises(installer.InstallerError):
        installer.plan_service_install(
            "/usr/bin/python3", tmp_path / "projects", tmp_path / "config", platform="amiga"
        )


# --------------------------------------------------------------------
# install(): dry-run vs real, via a recording runner
# --------------------------------------------------------------------


def test_install_dry_run_writes_nothing_and_never_calls_the_runner(tmp_path, capsys):
    plan = installer.plan_service_install(
        "/usr/bin/python3", tmp_path / "projects", tmp_path / "config", platform="linux"
    )
    runner = _RecordingRunner()
    rc = installer.install(plan, runner=runner, dry_run=True)
    assert rc == 0
    assert runner.calls == []
    for path in plan.files_to_write:
        assert not path.exists()
    out = capsys.readouterr().out
    assert "Dry run" in out


def test_install_writes_files_then_runs_commands_in_order(tmp_path):
    config_dir = tmp_path / "config"
    plan = installer.plan_service_install("/usr/bin/python3", tmp_path / "projects", config_dir, platform="linux")
    runner = _RecordingRunner()
    rc = installer.install(plan, runner=runner)
    assert rc == 0
    (unit_path, content), = plan.files_to_write.items()
    assert unit_path.read_text(encoding="utf-8") == content
    assert runner.calls == plan.commands


def test_install_windows_runs_the_single_powershell_command(tmp_path):
    python_exe = tmp_path / "python.exe"
    python_exe.write_text("", encoding="utf-8")
    plan = installer.plan_service_install(
        str(python_exe), tmp_path / "projects", tmp_path / "config", platform="windows"
    )
    runner = _RecordingRunner()
    rc = installer.install(plan, runner=runner)
    assert rc == 0
    assert plan.files_to_write == {}
    assert runner.calls == plan.commands


def test_install_raises_on_a_failed_command(tmp_path):
    plan = installer.plan_service_install(
        "/usr/bin/python3", tmp_path / "projects", tmp_path / "config", platform="linux"
    )
    runner = _RecordingRunner(returncode=1)
    with pytest.raises(installer.InstallerError):
        installer.install(plan, runner=runner)


# --------------------------------------------------------------------
# uninstall(): dry-run vs real
# --------------------------------------------------------------------


def test_uninstall_dry_run_removes_nothing_and_never_calls_the_runner(tmp_path):
    config_dir = tmp_path / "config"
    plan = installer.plan_service_install("/usr/bin/python3", tmp_path / "projects", config_dir, platform="linux")
    installer.install(plan, runner=_RecordingRunner())
    (unit_path, _), = plan.files_to_write.items()
    assert unit_path.exists()

    runner = _RecordingRunner()
    rc = installer.uninstall(plan, runner=runner, dry_run=True)
    assert rc == 0
    assert runner.calls == []
    assert unit_path.exists()


def test_uninstall_runs_commands_then_removes_files(tmp_path):
    plan = installer.plan_service_install(
        "/usr/bin/python3", tmp_path / "projects", tmp_path / "config", platform="linux"
    )
    installer.install(plan, runner=_RecordingRunner())
    (unit_path, _), = plan.files_to_write.items()
    assert unit_path.exists()

    runner = _RecordingRunner()
    rc = installer.uninstall(plan, runner=runner)
    assert rc == 0
    assert runner.calls == plan.uninstall_commands
    assert not unit_path.exists()


def test_uninstall_missing_file_is_not_an_error(tmp_path):
    plan = installer.plan_service_install(
        "/usr/bin/python3", tmp_path / "projects", tmp_path / "config", platform="linux"
    )
    # Never installed -- the unit file was never written.
    rc = installer.uninstall(plan, runner=_RecordingRunner())
    assert rc == 0


def test_uninstall_reports_a_failed_command_but_keeps_going(tmp_path):
    plan = installer.plan_service_install(
        "/usr/bin/python3", tmp_path / "projects", tmp_path / "config", platform="linux"
    )
    installer.install(plan, runner=_RecordingRunner())
    (unit_path, _), = plan.files_to_write.items()

    rc = installer.uninstall(plan, runner=_RecordingRunner(returncode=1))
    # The command "failed" but file removal still happens.
    assert rc == 1
    assert not unit_path.exists()


def test_uninstall_windows_stops_the_task_before_removing_it(tmp_path, capsys):
    python_exe = tmp_path / "python.exe"
    python_exe.write_text("", encoding="utf-8")
    plan = installer.plan_service_install(
        str(python_exe), tmp_path / "projects", tmp_path / "config", platform="windows"
    )
    runner = _RecordingRunner()
    rc = installer.uninstall(plan, runner=runner)
    assert rc == 0
    assert "Stop-ScheduledTask" in runner.calls[0][-1]
    assert "Unregister-ScheduledTask" in runner.calls[1][-1]
    out = capsys.readouterr().out
    assert "Stopped Scheduled Task 'ClaudeTokenLens' (a running dashboard is shut down)." in out
    assert "Removed Scheduled Task 'ClaudeTokenLens'." in out
    assert out.index("Stopped Scheduled Task") < out.index("Removed Scheduled Task")


def test_uninstall_does_not_claim_a_step_that_failed(tmp_path, capsys):
    python_exe = tmp_path / "python.exe"
    python_exe.write_text("", encoding="utf-8")
    plan = installer.plan_service_install(
        str(python_exe), tmp_path / "projects", tmp_path / "config", platform="windows"
    )
    rc = installer.uninstall(plan, runner=_RecordingRunner(returncode=1))
    assert rc == 1
    out = capsys.readouterr().out
    assert "Stopped Scheduled Task" not in out
    assert "Removed Scheduled Task" not in out
    assert "finished with problems" in out


@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_uninstall_posix_stop_is_part_of_the_removal_command(tmp_path, capsys, monkeypatch, platform):
    # systemctl --user disable --now and launchctl bootout both stop a
    # running serve as well as removing the registration.
    monkeypatch.setattr(installer, "_uid", lambda: "501")
    plan = installer.plan_service_install(
        "/usr/bin/python3", tmp_path / "projects", tmp_path / "config", platform=platform
    )
    assert "--now" in plan.uninstall_commands[0] or plan.uninstall_commands[0][:2] == ["launchctl", "bootout"]
    rc = installer.uninstall(plan, runner=_RecordingRunner())
    assert rc == 0
    assert "Stopped and " in capsys.readouterr().out


# --------------------------------------------------------------------
# is_registered
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "platform,expected_probe",
    [
        ("windows", ["schtasks", "/Query", "/TN", installer.TASK_NAME]),
        ("linux", ["systemctl", "--user", "is-enabled", "claude-token-lens"]),
    ],
)
def test_is_registered_true_on_zero_exit(platform, expected_probe):
    runner = _RecordingRunner(returncode=0)
    assert installer.is_registered(platform, runner=runner) is True
    assert runner.calls == [expected_probe]


def test_is_registered_macos_probe_command():
    runner = _RecordingRunner(returncode=0)
    assert installer.is_registered("macos", runner=runner) is True
    assert runner.calls[0][:2] == ["launchctl", "print"]


def test_is_registered_false_on_nonzero_exit():
    runner = _RecordingRunner(returncode=1)
    assert installer.is_registered("linux", runner=runner) is False


def test_is_registered_none_when_the_runner_raises():
    def raising_runner(command, **kwargs):
        raise FileNotFoundError("no such command")

    assert installer.is_registered("linux", runner=raising_runner) is None


def test_is_registered_none_for_unsupported_platform():
    assert installer.is_registered("amiga", runner=_RecordingRunner()) is None


def test_is_registered_defaults_to_detect_platform(monkeypatch):
    monkeypatch.setattr(installer.sys, "platform", "linux")
    runner = _RecordingRunner(returncode=0)
    assert installer.is_registered(runner=runner) is True
    assert runner.calls == [["systemctl", "--user", "is-enabled", "claude-token-lens"]]


# --------------------------------------------------------------------
# serve --exit-on-code-change, and relaunch_after_exit
# --------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["windows", "linux", "macos"])
def test_the_registered_service_exits_on_a_code_change(tmp_path, platform):
    """So an update that lands without a restart (an editable install
    after a pull) restarts the dashboard instead of leaving old modules
    in memory to lazily import new ones."""
    plan = installer.plan_service_install(
        "/usr/bin/python3", tmp_path / "projects", tmp_path / "config", platform=platform
    )
    registered = plan.commands[0][-1] if platform == "windows" else next(iter(plan.files_to_write.values()))
    assert "--exit-on-code-change" in registered


class _RecordingSpawner:
    def __init__(self, error: Exception | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self.error = error

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        if self.error is not None:
            raise self.error


@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_relaunch_after_exit_leaves_it_to_systemd_and_launchd(platform):
    spawner = _RecordingSpawner()
    runner = _RecordingRunner()
    assert installer.relaunch_after_exit(1234, platform, runner=runner, spawner=spawner) is True
    assert spawner.calls == [] and runner.calls == []


def test_relaunch_after_exit_starts_the_task_again_once_this_process_ends():
    """Task Scheduler reruns nothing by exit status, so a hidden helper
    waits for this process to end and starts the task again."""
    spawner = _RecordingSpawner()
    runner = _RecordingRunner(returncode=0)
    assert installer.relaunch_after_exit(1234, "windows", runner=runner, spawner=spawner) is True
    assert runner.calls == [["schtasks", "/Query", "/TN", installer.TASK_NAME]]
    (command, kwargs), = spawner.calls
    assert command[0] == "powershell.exe"
    script = command[-1]
    assert script.startswith("Wait-Process -Id 1234 ")
    assert script.index("Wait-Process") < script.index("Start-ScheduledTask")
    assert script.endswith(f"Start-ScheduledTask -TaskName '{installer.TASK_NAME}'")
    # Detached, so it outlives this process, and silent.
    assert kwargs["stdin"] is installer.subprocess.DEVNULL
    assert kwargs["stdout"] is installer.subprocess.DEVNULL


@pytest.mark.parametrize("returncode", [1, None])
def test_relaunch_after_exit_refuses_without_the_task(returncode):
    """Not running as the ClaudeTokenLens task (or can't tell): starting
    it would start the wrong thing, or nothing, so serve stays up."""
    spawner = _RecordingSpawner()
    if returncode is None:

        def runner(command, **kwargs):
            raise FileNotFoundError("schtasks")

    else:
        runner = _RecordingRunner(returncode=returncode)
    assert installer.relaunch_after_exit(1234, "windows", runner=runner, spawner=spawner) is False
    assert spawner.calls == []


def test_relaunch_after_exit_false_when_the_helper_cannot_start():
    spawner = _RecordingSpawner(error=FileNotFoundError("powershell.exe"))
    assert installer.relaunch_after_exit(1234, "windows", runner=_RecordingRunner(), spawner=spawner) is False
