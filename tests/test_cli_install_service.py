"""CLI wiring tests for the v3 ``install-service``/``uninstall-service``
subcommands and ``init``'s new "run the service at logon?" final step
(``src/claude_token_lens/cli.py``'s ``_cmd_install_service``/
``_cmd_uninstall_service``/``_cmd_init_service_step``).

Every test here either uses ``--dry-run`` (the real code path, which by
construction never writes a file or spawns a process -- see
``installer.py``'s module docstring) or monkeypatches
``cli.installer_mod``'s functions directly, the same convention
``tests/test_service_api.py``'s ``_install_fake_rebuild`` already uses
for a sibling work package's module. This file must never let a real
``schtasks``/``systemctl``/``launchctl``/``powershell.exe`` invocation
reach this machine.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from claude_token_lens import cli, installer as installer_mod


# --------------------------------------------------------------------
# install-service / uninstall-service: --dry-run (the real code path)
# --------------------------------------------------------------------


def test_install_service_dry_run_writes_nothing(tmp_path, capsys):
    config_dir = tmp_path / "config"
    exit_code = cli.main(
        [
            "install-service",
            "--dry-run",
            "--config-dir",
            str(config_dir),
            "--projects-root",
            str(tmp_path / "projects"),
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "install-service" in out
    assert "Dry run" in out
    # Nothing under config_dir was created by this command.
    assert not config_dir.exists()


def test_install_service_dry_run_never_probes(tmp_path, capsys, monkeypatch):
    called = []
    monkeypatch.setattr(cli, "_probe_service_after_install", lambda *a, **k: called.append((a, k)))
    exit_code = cli.main(
        [
            "install-service",
            "--dry-run",
            "--config-dir",
            str(tmp_path / "config"),
            "--projects-root",
            str(tmp_path / "projects"),
        ]
    )
    assert exit_code == 0
    assert called == []


def test_uninstall_service_dry_run_removes_nothing(tmp_path, capsys):
    config_dir = tmp_path / "config"
    exit_code = cli.main(
        [
            "uninstall-service",
            "--dry-run",
            "--config-dir",
            str(config_dir),
            "--projects-root",
            str(tmp_path / "projects"),
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "uninstall-service" in out
    assert "Dry run" in out


def test_install_service_rejects_unsupported_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(installer_mod, "detect_platform", lambda: "amiga")
    exit_code = cli.main(
        [
            "install-service",
            "--dry-run",
            "--config-dir",
            str(tmp_path / "config"),
            "--projects-root",
            str(tmp_path / "projects"),
        ]
    )
    assert exit_code == 2


# --------------------------------------------------------------------
# install-service: the real (non-dry-run) path, with installer.py's own
# side-effecting functions stubbed out -- never actually reaching a
# real subprocess or file write.
# --------------------------------------------------------------------


def test_install_service_calls_install_and_then_probes(tmp_path, monkeypatch):
    calls = []

    def fake_install(plan, *, dry_run=False):
        calls.append(("install", plan, dry_run))
        return 0

    probed = []
    monkeypatch.setattr(installer_mod, "install", fake_install)
    monkeypatch.setattr(cli, "_probe_service_after_install", lambda platform, **kw: probed.append((platform, kw)))

    exit_code = cli.main(
        [
            "install-service",
            "--port",
            "9999",
            "--bind",
            "127.0.0.1",
            "--config-dir",
            str(tmp_path / "config"),
            "--projects-root",
            str(tmp_path / "projects"),
        ]
    )
    assert exit_code == 0
    assert len(calls) == 1
    _, plan, dry_run = calls[0]
    assert dry_run is False
    flattened = " ".join(a for cmd in plan.commands for a in cmd) + " ".join(plan.files_to_write.values())
    assert "9999" in flattened
    assert len(probed) == 1
    probed_platform, probed_kwargs = probed[0]
    assert probed_platform == plan.platform
    assert probed_kwargs.get("bind") == "127.0.0.1"
    assert probed_kwargs.get("port") == 9999


def test_uninstall_service_calls_uninstall(tmp_path, monkeypatch):
    calls = []

    def fake_uninstall(plan, *, dry_run=False):
        calls.append((plan, dry_run))
        return 0

    monkeypatch.setattr(installer_mod, "uninstall", fake_uninstall)

    exit_code = cli.main(
        [
            "uninstall-service",
            "--config-dir",
            str(tmp_path / "config"),
            "--projects-root",
            str(tmp_path / "projects"),
        ]
    )
    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0][1] is False


# --------------------------------------------------------------------
# init: the "run the service at logon?" final step
# --------------------------------------------------------------------


def _run_init(tmp_path, extra_args, stdin_text: str = ""):
    config_dir = tmp_path / "config"
    projects_root = tmp_path / "projects"
    exit_code = cli.main(
        [
            "init",
            "--no-install",
            "--config-dir",
            str(config_dir),
            "--projects-root",
            str(projects_root),
            *extra_args,
        ]
    )
    return exit_code


def test_init_non_interactive_defaults_to_not_installing_the_service(tmp_path, capsys):
    exit_code = _run_init(tmp_path, ["--non-interactive"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "(derived) run_service" in out
    assert "Service not installed" in out


def test_init_no_service_skips_the_step_entirely(tmp_path, capsys, monkeypatch):
    install_called = []
    monkeypatch.setattr(installer_mod, "install", lambda *a, **k: install_called.append(1))
    exit_code = _run_init(tmp_path, ["--non-interactive", "--no-service"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Service-at-logon step skipped (--no-service)" in out
    assert install_called == []


def test_init_install_service_flag_installs_without_asking(tmp_path, capsys):
    exit_code = _run_init(tmp_path, ["--non-interactive", "--install-service", "--dry-run"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "(derived) run_service" not in out
    assert "install-service:" in out
    assert "Dry run" in out


#: gather_answers() prompts interactively for 7 questions (billing,
#: exclude_projects, launch_overlays, shared_project_config, tz,
#: apply_scope, capture_window) before init's own new "run the service
#: at logon?" question -- a blank line per question accepts its
#: printed default, so these tests can reach the new prompt without
#: exercising (or caring about) the rest of that Q&A.
_ONBOARDING_DEFAULTS = "\n" * 7


def test_init_interactive_yes_installs(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(_ONBOARDING_DEFAULTS + "y\n"))
    exit_code = _run_init(tmp_path, ["--dry-run"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Run the service at logon?" in out
    assert "install-service:" in out


def test_init_interactive_blank_line_defaults_to_yes(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(_ONBOARDING_DEFAULTS + "\n"))
    exit_code = _run_init(tmp_path, ["--dry-run"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "install-service:" in out


def test_init_interactive_no_declines(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(_ONBOARDING_DEFAULTS + "n\n"))
    exit_code = _run_init(tmp_path, [])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Run the service at logon?" in out
    assert "Service not installed" in out


def test_init_dry_run_writes_no_service_files(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(_ONBOARDING_DEFAULTS + "y\n"))
    exit_code = _run_init(tmp_path, ["--dry-run"])
    assert exit_code == 0
    # config.toml IS written (init's own config step is unaffected by
    # --dry-run, which only governs the logon-service step) -- only the
    # *service* registration must be untouched.
    assert (tmp_path / "config" / "config.toml").is_file()


# --------------------------------------------------------------------
# _probe_service_after_install / _http_health_ok: injectable dependencies
# --------------------------------------------------------------------


def test_probe_service_after_install_reports_registered_and_healthy(capsys):
    slept = []
    cli._probe_service_after_install(
        "linux",
        is_registered_fn=lambda: True,
        health_check=lambda url: True,
        sleep_fn=lambda s: slept.append(s),
    )
    out = capsys.readouterr().out
    assert "confirmed" in out
    assert "already responding" in out
    assert slept == [cli._POST_INSTALL_PROBE_DELAY_S]


def test_probe_service_after_install_reports_unregistered_and_unreachable(capsys):
    cli._probe_service_after_install(
        "windows",
        is_registered_fn=lambda: None,
        health_check=lambda url: False,
        sleep_fn=lambda s: None,
    )
    out = capsys.readouterr().out
    assert "could not be determined" in out
    assert "not responding yet" in out


def test_http_health_ok_false_on_connection_error():
    # Nothing is listening on this port -- a real connection attempt,
    # but to loopback only and expected to fail fast (short timeout).
    assert cli._http_health_ok("http://127.0.0.1:1") is False
