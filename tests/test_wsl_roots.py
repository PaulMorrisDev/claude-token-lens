"""Several folders of projects: ``discovery.projects_roots``/
``source_label``/``find_wsl_projects_roots``, ``config.toml``'s
``extra_projects_roots``, ``init`` offering WSL folders, the logon
service registration with several ``--projects-root`` flags, and the
version check after it (``update`` is in test_update.py).
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from claude_token_lens import cli, discovery, installer, onboarding, setup_flow
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


@pytest.mark.skipif(sys.platform != "win32", reason="builds Windows network paths")
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


def _init(tmp_path, monkeypatch, *, wsl_root, stdin_text="", non_interactive=False, answers=None, advanced=False):
    import dataclasses

    real_project = tmp_path / "repo"
    real_project.mkdir()
    monkeypatch.chdir(real_project)
    argv = ["init", "--config-dir", str(tmp_path / "config"), "--projects-root", str(tmp_path / "projects")]
    argv += ["--no-install", "--no-service"]
    if non_interactive:
        argv.append("--non-interactive")
    if advanced:
        argv.append("--advanced")
    if answers is not None:
        answers_path = tmp_path / "answers.json"
        answers_path.write_text(answers, encoding="utf-8")
        argv += ["--answers", str(answers_path)]
    options, tools = cli._setup_flow_inputs(cli._make_parser().parse_args(argv))
    stdout = io.StringIO()
    rc = setup_flow.run(
        options,
        dataclasses.replace(tools, find_wsl_roots=lambda: [wsl_root]),
        stdin=io.StringIO(stdin_text),
        stdout=stdout,
    )
    assert rc == 0
    return load_config(tmp_path / "config"), stdout.getvalue()


def test_init_adds_a_found_wsl_folder_without_asking_and_names_it(tmp_path, monkeypatch):
    wsl_root = tmp_path / "wsl-projects"
    (wsl_root / "-home-alice-repo").mkdir(parents=True)
    # A plan; then Enter for every other question.
    config, out = _init(tmp_path, monkeypatch, wsl_root=wsl_root, stdin_text="1\n" + "\n" * 5)
    assert config.extra_projects_roots == [str(wsl_root)]
    assert "Include those sessions" not in out
    assert "Found Claude Code history in " in out and "No Claude Code history yet" not in out


def test_init_advanced_offers_a_found_wsl_folder_and_yes_is_the_default(tmp_path, monkeypatch):
    wsl_root = tmp_path / "wsl-projects"
    (wsl_root / "-home-alice-repo").mkdir(parents=True)
    # A plan; then Enter for every other question, taking each default.
    config, out = _init(tmp_path, monkeypatch, wsl_root=wsl_root, stdin_text="1\n" + "\n" * 20, advanced=True)
    assert config.extra_projects_roots == [str(wsl_root)]
    assert "Include those sessions" in out


def test_init_advanced_leaves_out_a_wsl_folder_on_no(tmp_path, monkeypatch):
    wsl_root = tmp_path / "wsl-projects"
    wsl_root.mkdir()
    # A plan; six more settings; no to the WSL folder; then the defaults.
    config, _ = _init(
        tmp_path, monkeypatch, wsl_root=wsl_root, stdin_text="1\n" + "\n" * 6 + "n\n" + "\n" * 5, advanced=True
    )
    assert config.extra_projects_roots == []


def test_init_non_interactive_adds_found_wsl_folders_and_says_so(tmp_path, monkeypatch):
    wsl_root = tmp_path / "wsl-projects"
    wsl_root.mkdir()
    config, out = _init(tmp_path, monkeypatch, wsl_root=wsl_root, non_interactive=True, advanced=True)
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


# -- install-service ---------------------------------------------------------------


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
