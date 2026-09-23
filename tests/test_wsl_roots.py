"""Several folders of projects: ``discovery.projects_roots``/
``source_label``/``find_wsl_projects_roots``, ``config.toml``'s
``extra_projects_roots``, ``init`` offering WSL folders, the logon
service registration with several ``--projects-root`` flags, and the
``update`` command.
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from claude_token_lens import cli, discovery, installer, onboarding
from claude_token_lens.config import ConfigError, load_config

# conftest replaces this with a stub for every test; keep the real one.
_REAL_FIND_WSL = discovery.find_wsl_projects_roots

_UBUNTU = r"\\wsl.localhost\Ubuntu\home\alice\.claude\projects"


# -- discovery ---------------------------------------------------------------


def test_projects_roots_defaults_to_this_computer_then_adds_extras(tmp_path):
    extra = tmp_path / "wsl"
    roots = discovery.projects_roots(None, [str(extra)])
    assert roots == [discovery.projects_root(), extra]


def test_projects_roots_explicit_replaces_the_default_and_drops_repeats(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert discovery.projects_roots([a, b], [str(a) + "/"]) == [a, b]


@pytest.mark.parametrize(
    ("path", "label"),
    [
        (_UBUNTU + r"\-home-alice-repo\s.jsonl", "WSL: Ubuntu"),
        (r"\\wsl$\Debian\home\bob\.claude\projects\x.jsonl", "WSL: Debian"),
        ("//wsl.localhost/Ubuntu-22.04/home/a/x.jsonl", "WSL: Ubuntu-22.04"),
        (r"C:\Users\alice\.claude\projects\x.jsonl", "This computer"),
        ("/home/alice/.claude/projects/x.jsonl", "This computer"),
        (None, "This computer"),
    ],
)
def test_source_label(path, label):
    assert discovery.source_label(path) == label


def test_resolve_project_dirs_reads_every_root_and_skips_a_missing_one(tmp_path):
    (tmp_path / "win" / "C--Users-alice-repo").mkdir(parents=True)
    (tmp_path / "wsl" / "-home-alice-repo").mkdir(parents=True)
    dirs = discovery.resolve_project_dirs(
        [tmp_path / "win", tmp_path / "wsl", tmp_path / "gone"], all_projects=True
    )
    assert [d.name for d in dirs] == ["C--Users-alice-repo", "-home-alice-repo"]


def test_find_wsl_projects_roots_is_empty_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert _REAL_FIND_WSL(run=lambda *a, **k: pytest.fail("must not run wsl.exe")) == []


def test_find_wsl_projects_roots_is_empty_without_wsl(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def missing(*args, **kwargs):
        raise FileNotFoundError("wsl.exe")

    assert _REAL_FIND_WSL(run=missing) == []


def test_find_wsl_projects_roots_reads_utf16_names_and_skips_docker(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    listing = "\ufeffUbuntu\r\ndocker-desktop\r\n".encode("utf-16-le")
    looked_at: list[str] = []
    real_is_dir = Path.is_dir

    def fake_is_dir(self):
        text = str(self)
        if "wsl.localhost" not in text:
            return real_is_dir(self)
        looked_at.append(text)
        return text.lower().endswith(("home", "alice", "projects")) and "root" not in text.split("\\")

    def fake_iterdir(self):
        return iter([self / "alice"])

    monkeypatch.setattr(Path, "is_dir", fake_is_dir)
    monkeypatch.setattr(Path, "iterdir", fake_iterdir)
    found = _REAL_FIND_WSL(run=lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=listing))

    assert [str(p) for p in found] == [_UBUNTU]
    assert not any("docker" in text for text in looked_at)


# -- config ------------------------------------------------------------------


def test_config_reads_extra_projects_roots(tmp_path):
    (tmp_path / "config.toml").write_text(
        "extra_projects_roots = ['" + _UBUNTU + "']\n",
        encoding="utf-8",
    )
    assert load_config(tmp_path).extra_projects_roots == [_UBUNTU]


def test_config_rejects_a_non_list_extra_projects_roots(tmp_path):
    (tmp_path / "config.toml").write_text("extra_projects_roots = 'x'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="extra_projects_roots"):
        load_config(tmp_path)


# -- init ----------------------------------------------------------------------


def _init(tmp_path, monkeypatch, *, wsl_root, stdin_text="", non_interactive=False, answers=None):
    real_project = tmp_path / "repo"
    real_project.mkdir()
    monkeypatch.chdir(real_project)
    stdout = io.StringIO()
    answers_path = None
    if answers is not None:
        answers_path = tmp_path / "answers.json"
        answers_path.write_text(answers, encoding="utf-8")
    rc = onboarding.run_init(
        config_dir=tmp_path / "config",
        projects_root_path=tmp_path / "projects",
        find_wsl_roots=lambda: [wsl_root],
        answers_path=answers_path,
        non_interactive=non_interactive,
        no_install=True,
        hook_fragment="HOOK",
        statusline_fragment="STATUSLINE",
        stdin=io.StringIO(stdin_text),
        stdout=stdout,
    )
    assert rc == 0
    return load_config(tmp_path / "config"), stdout.getvalue()


def test_init_offers_a_found_wsl_folder_and_yes_is_the_default(tmp_path, monkeypatch):
    wsl_root = tmp_path / "wsl-projects"
    (wsl_root / "-home-alice-repo").mkdir(parents=True)
    # Enter for every question, taking each default.
    config, out = _init(tmp_path, monkeypatch, wsl_root=wsl_root, stdin_text="\n" * 20)
    assert config.extra_projects_roots == [str(wsl_root)]
    assert "Include those sessions" in out


def test_init_leaves_out_a_wsl_folder_on_no(tmp_path, monkeypatch):
    wsl_root = tmp_path / "wsl-projects"
    wsl_root.mkdir()
    config, _ = _init(tmp_path, monkeypatch, wsl_root=wsl_root, stdin_text="\n" * 7 + "n\n")
    assert config.extra_projects_roots == []


def test_init_non_interactive_adds_found_wsl_folders_and_says_so(tmp_path, monkeypatch):
    wsl_root = tmp_path / "wsl-projects"
    wsl_root.mkdir()
    config, out = _init(tmp_path, monkeypatch, wsl_root=wsl_root, non_interactive=True)
    assert config.extra_projects_roots == [str(wsl_root)]
    assert "(derived) extra_projects_roots" in out


def test_init_answers_file_list_replaces_the_found_folders(tmp_path, monkeypatch):
    wsl_root = tmp_path / "wsl-projects"
    wsl_root.mkdir()
    config, _ = _init(
        tmp_path, monkeypatch, wsl_root=wsl_root, non_interactive=True, answers='{"extra_projects_roots": []}'
    )
    assert config.extra_projects_roots == []


def test_init_does_not_offer_a_folder_config_already_lists(tmp_path, monkeypatch):
    wsl_root = tmp_path / "wsl-projects"
    wsl_root.mkdir()
    _init(tmp_path, monkeypatch, wsl_root=wsl_root, non_interactive=True)
    detection = onboarding.detect(tmp_path / "config", tmp_path / "projects", find_wsl_roots=lambda: [wsl_root])
    assert detection.wsl_roots == []


# -- installer -----------------------------------------------------------------


def test_service_registration_passes_every_projects_root(tmp_path):
    plan = installer.plan_service_install(
        "python", [tmp_path / "a", tmp_path / "b"], tmp_path / "cfg", platform="linux", pyz_path=None
    )
    text = "\n".join(plan.files_to_write.values())
    assert text.count("--projects-root") == 2


# -- update ----------------------------------------------------------------------


class _Runner:
    def __init__(self, *, pip_rc=0, version="9.9.9", restart_rc=0):
        self.calls: list[list[str]] = []
        self.pip_rc, self.version, self.restart_rc = pip_rc, version, restart_rc

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        if "pip" in command:
            return subprocess.CompletedProcess(command, self.pip_rc)
        if "-c" in command:
            return subprocess.CompletedProcess(command, 0, stdout=self.version + "\n")
        return subprocess.CompletedProcess(command, self.restart_rc)


def _update_args(*extra):
    return cli._make_parser().parse_args(["update", *extra])


def test_update_dry_run_prints_both_steps_and_runs_nothing(capsys):
    assert cli.main(["update", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "--force-reinstall" in out
    assert "install-service" in out
    assert "Dry run" in out


def test_update_installs_then_restarts_a_registered_dashboard(capsys):
    runner = _Runner()
    rc = cli._cmd_update(_update_args(), runner=runner, is_registered_fn=lambda: True)
    assert rc == 0
    assert runner.calls[0][1:4] == ["-m", "pip", "install"]
    assert runner.calls[0][-1] == cli.UPDATE_SOURCE
    assert runner.calls[-1][1:4] == ["-m", "claude_token_lens", "install-service"]
    assert "Installed version 9.9.9" in capsys.readouterr().out


def test_update_stops_when_pip_fails(capsys):
    runner = _Runner(pip_rc=1)
    assert cli._cmd_update(_update_args(), runner=runner, is_registered_fn=lambda: True) == 1
    assert len(runner.calls) == 1
    assert "Nothing else was changed" in capsys.readouterr().err


def test_update_does_not_register_a_dashboard_that_was_not_registered(capsys):
    runner = _Runner()
    assert cli._cmd_update(_update_args(), runner=runner, is_registered_fn=lambda: False) == 0
    assert not any("install-service" in call for call in runner.calls)
    assert "nothing to restart" in capsys.readouterr().out


def test_update_from_a_local_folder(capsys):
    runner = _Runner()
    cli._cmd_update(_update_args("--from", ".", "--no-service"), runner=runner, is_registered_fn=lambda: True)
    assert runner.calls[0][-1] == "."
    assert not any("install-service" in call for call in runner.calls)


def test_install_service_probe_flags_an_old_copy_on_the_port(capsys):
    cli._probe_service_after_install(
        "windows",
        is_registered_fn=lambda: True,
        health_check=lambda url: True,
        sleep_fn=lambda s: None,
        version_check=lambda url: "0.3.0",
    )
    out = capsys.readouterr().out
    assert "answering with version 0.3.0" in out
    assert "An old dashboard won't go away" in out
