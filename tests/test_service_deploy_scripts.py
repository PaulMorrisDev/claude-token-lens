"""Line-based sanity checks for the native (non-Docker) deployment
scripts (deliverables 2.c/2.d): the Windows Scheduled Task
register/unregister scripts and the systemd user unit.

No PowerShell/systemd interpreter is assumed to be available in every
environment this suite runs in, so these are plain text checks (the
same posture ``tests/test_service_docker.py`` takes for the Docker
artefacts) plus, where the host actually has ``powershell.exe``, a real
syntax parse via ``System.Management.Automation.Language.Parser`` --
skipped everywhere else rather than failing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WINDOWS_DIR = REPO_ROOT / "scripts" / "windows"
REGISTER_SCRIPT = WINDOWS_DIR / "Register-TokenLensTask.ps1"
UNREGISTER_SCRIPT = WINDOWS_DIR / "Unregister-TokenLensTask.ps1"
SYSTEMD_UNIT = REPO_ROOT / "scripts" / "systemd" / "claude-token-lens.service"

#: PowerShell 5.1 lacks `&&`/`||` pipeline chain operators and the
#: ternary/null-coalescing/null-conditional operators -- a script using
#: any of these would fail to parse on a plain Windows PowerShell 5.1
#: host (only pwsh 7+ has them), which is exactly the environment this
#: script must run in without any extra install.
_PS_51_INCOMPATIBLE_SUBSTRINGS = ("&&", "||", "??", "?.")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _non_comment_non_string_lines(path: Path) -> list[str]:
    """Best-effort filter of comment-only lines, so a `.NOTES`/`.EXAMPLE`
    help-block sentence mentioning an operator by name (documentation,
    not code) doesn't trip the compatibility scan below."""
    lines = []
    in_block_comment = False
    for raw in _text(path).splitlines():
        stripped = raw.strip()
        if stripped.startswith("<#"):
            in_block_comment = True
        if in_block_comment:
            if stripped.endswith("#>"):
                in_block_comment = False
            continue
        if stripped.startswith("#"):
            continue
        lines.append(raw)
    return lines


# -- Windows Scheduled Task scripts (deliverable 2.c) ------------------------


def test_windows_scripts_exist() -> None:
    assert REGISTER_SCRIPT.is_file()
    assert UNREGISTER_SCRIPT.is_file()


@pytest.mark.parametrize("path", [REGISTER_SCRIPT, UNREGISTER_SCRIPT])
def test_windows_scripts_avoid_powershell_7_only_operators(path: Path) -> None:
    lines = _non_comment_non_string_lines(path)
    for i, line in enumerate(lines, start=1):
        for forbidden in _PS_51_INCOMPATIBLE_SUBSTRINGS:
            assert forbidden not in line, f"{path.name}:{i} uses PS7-only operator {forbidden!r}: {line!r}"


def test_register_script_runs_without_admin_rights() -> None:
    text = _text(REGISTER_SCRIPT)
    assert "-RunLevel Limited" in text
    code_lines = _non_comment_non_string_lines(REGISTER_SCRIPT)
    assert not any("highest" in line.lower() for line in code_lines)  # never requests elevation


def test_register_script_uses_pythonw_for_a_windowless_process() -> None:
    text = _text(REGISTER_SCRIPT)
    assert "pythonw" in text
    assert "claude_token_lens serve" in text or "claude_token_lens" in text


def test_register_script_has_a_schtasks_fallback() -> None:
    text = _text(REGISTER_SCRIPT)
    assert "Register-ScheduledTask" in text
    assert "schtasks" in text.lower()


def test_register_script_supports_billing_mode_override() -> None:
    text = _text(REGISTER_SCRIPT)
    assert "BillingMode" in text
    assert "--billing-mode" in text


def test_unregister_script_removes_the_task_and_stops_the_process() -> None:
    text = _text(UNREGISTER_SCRIPT)
    assert "Unregister-ScheduledTask" in text or "schtasks" in text.lower()
    assert "Get-CimInstance" in text
    assert "Win32_Process" in text
    assert "claude_token_lens" in text.lower()


@pytest.mark.parametrize("path", [REGISTER_SCRIPT, UNREGISTER_SCRIPT])
def test_windows_scripts_parse_with_a_real_powershell_when_available(path: Path) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("no powershell.exe on this host")

    script = (
        "$errors = $null; $tokens = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 } else { exit 0 }"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"{path.name} failed to parse:\n{result.stderr}"


# -- systemd user unit (deliverable 2.d) -------------------------------------


def test_systemd_unit_exists() -> None:
    assert SYSTEMD_UNIT.is_file()


def test_systemd_unit_is_a_simple_service_with_restart_on_failure() -> None:
    text = _text(SYSTEMD_UNIT)
    assert "[Service]" in text
    assert "Type=simple" in text
    assert "Restart=on-failure" in text


def test_systemd_unit_protects_home_but_allows_its_own_data_dir() -> None:
    text = _text(SYSTEMD_UNIT)
    assert "ProtectHome=read-only" in text
    assert "ReadWritePaths=" in text
    assert "token-lens" in text  # the carved-out path names the service's own dir


def test_systemd_unit_documents_private_network_choice() -> None:
    text = _text(SYSTEMD_UNIT)
    assert "PrivateNetwork" in text  # discussed even if left at its default


def test_systemd_unit_installs_for_the_default_user_target() -> None:
    text = _text(SYSTEMD_UNIT)
    assert "[Install]" in text
    assert "WantedBy=default.target" in text  # user-unit convention, not multi-user.target


def test_systemd_unit_runs_the_real_serve_command() -> None:
    text = _text(SYSTEMD_UNIT)
    assert "ExecStart=" in text
    assert "claude-token-lens serve" in text
    assert "--projects-root" in text
    assert "--config-dir" in text


__all__: list[str] = []
