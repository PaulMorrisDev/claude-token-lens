"""Tests for the v0.3 ``init`` milestone's
``src/claude_token_lens/onboarding.py``: :func:`~claude_token_lens.
onboarding.detect`, :func:`~claude_token_lens.onboarding.load_answers_file`/
:func:`~claude_token_lens.onboarding.gather_answers` (an ``--answers``
file, interactive stdin prompting, and ``--non-interactive`` derivation,
each exercised directly against :class:`Detection`/hand-built answers
data), and :func:`~claude_token_lens.onboarding.run_init`'s end-to-end
orchestration against synthetic projects built with ``tests/helpers``.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from claude_token_lens import discovery, onboarding
from claude_token_lens.baseline import list_baselines
from claude_token_lens.config import Config, load_config

from helpers import assert_privacy_deep, turn_line, write_jsonl


def _detection(**overrides) -> onboarding.Detection:
    defaults = dict(
        config_dir=Path("/does/not/matter"),
        config_dir_exists=False,
        existing_config=Config(),
        snapshot_count=0,
        usage_log_present=False,
        project_slug="redacted-slug",
        project_count=0,
    )
    defaults.update(overrides)
    return onboarding.Detection(**defaults)


# --------------------------------------------------------------------
# detect()
# --------------------------------------------------------------------


def test_detect_reports_absent_config_dir_and_no_projects(tmp_path):
    detection = onboarding.detect(tmp_path / "config", tmp_path / "projects")
    assert detection.config_dir_exists is False
    assert detection.snapshot_count == 0
    assert detection.usage_log_present is False
    assert detection.project_count == 0


def test_detect_counts_projects_and_snapshots_and_usage_log(tmp_path):
    config_dir = tmp_path / "config"
    projects_root = tmp_path / "projects"
    (projects_root / "proj-a").mkdir(parents=True)
    (projects_root / "proj-b").mkdir(parents=True)
    config_dir.mkdir(parents=True)
    (config_dir / "usage-log.csv").write_text("session_id\n", encoding="utf-8")
    snapshots_dir = config_dir / "snapshots"
    snapshots_dir.mkdir()
    (snapshots_dir / "one.json").write_text(json.dumps({"ts": "2026-09-19T00:00:00Z"}), encoding="utf-8")

    detection = onboarding.detect(config_dir, projects_root)
    assert detection.config_dir_exists is True
    assert detection.project_count == 2
    assert detection.usage_log_present is True
    assert detection.snapshot_count == 1


def test_detect_project_slug_is_redacted(tmp_path, monkeypatch):
    real_project_path = tmp_path / "Users" / "alice" / "work-thing"
    real_project_path.mkdir(parents=True)
    monkeypatch.chdir(real_project_path)
    detection = onboarding.detect(tmp_path / "config", tmp_path / "projects")
    assert "<user>" in detection.project_slug or "alice" not in detection.project_slug


def test_detect_tolerates_a_malformed_config_toml(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("not = [valid toml", encoding="utf-8")
    detection = onboarding.detect(config_dir, tmp_path / "projects")
    # Falls back to all-defaults rather than raising -- write_config_values
    # is where a bad file is actually surfaced, during run_init.
    assert detection.existing_config == Config()


# --------------------------------------------------------------------
# load_answers_file
# --------------------------------------------------------------------


def test_load_answers_file_parses_json(tmp_path):
    path = tmp_path / "answers.json"
    path.write_text(json.dumps({"billing": "subscription"}), encoding="utf-8")
    assert onboarding.load_answers_file(path) == {"billing": "subscription"}


def test_load_answers_file_missing_raises():
    with pytest.raises(onboarding.OnboardingError):
        onboarding.load_answers_file("/does/not/exist.json")


def test_load_answers_file_malformed_json_raises(tmp_path):
    path = tmp_path / "answers.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(onboarding.OnboardingError):
        onboarding.load_answers_file(path)


def test_load_answers_file_non_object_raises(tmp_path):
    path = tmp_path / "answers.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(onboarding.OnboardingError):
        onboarding.load_answers_file(path)


# --------------------------------------------------------------------
# gather_answers: answers file / non-interactive derivation / interactive
# --------------------------------------------------------------------


def test_gather_answers_from_file_wins_for_every_named_key(tmp_path):
    answers_path = tmp_path / "answers.json"
    answers_path.write_text(
        json.dumps(
            {
                "billing": "subscription",
                "exclude_projects": ["work-a", "work-b"],
                "launch_overlays": True,
                "shared_project_config": True,
                "tz": "Europe/London",
                "apply_scope": "repo",
                "capture_window": 14,
            }
        ),
        encoding="utf-8",
    )
    answers = onboarding.gather_answers(
        detection=_detection(), answers_path=answers_path, non_interactive=True
    )
    assert answers.billing == "subscription"
    assert answers.exclude_projects == ["work-a", "work-b"]
    assert answers.launch_overlays is True
    assert answers.shared_project_config is True
    assert answers.tz == "Europe/London"
    assert answers.apply_scope == "repo"
    assert answers.capture_window == 14
    assert answers.notes == []


def test_gather_answers_non_interactive_derives_from_existing_config_and_records_notes():
    existing = Config(billing="subscription", tz="Europe/London", apply_scope="repo")
    answers = onboarding.gather_answers(detection=_detection(existing_config=existing), non_interactive=True)
    assert answers.billing == "subscription"
    assert answers.tz == "Europe/London"
    assert answers.apply_scope == "repo"
    # Every one of the 7 questions was derived, not asked -- each is
    # recorded so a --non-interactive run never guesses silently.
    assert len(answers.notes) == 7
    assert all("not given in --answers" in note for note in answers.notes)


def test_gather_answers_interactive_prompts_and_accepts_input():
    stdin = io.StringIO(
        "subscription\n"  # billing
        "work-a, work-b\n"  # exclude_projects
        "y\n"  # launch_overlays
        "n\n"  # shared_project_config
        "Europe/London\n"  # tz
        "repo\n"  # apply_scope
        "10\n"  # capture_window
    )
    stdout = io.StringIO()
    answers = onboarding.gather_answers(
        detection=_detection(), non_interactive=False, stdin=stdin, stdout=stdout
    )
    assert answers.billing == "subscription"
    assert answers.exclude_projects == ["work-a", "work-b"]
    assert answers.launch_overlays is True
    assert answers.shared_project_config is False
    assert answers.tz == "Europe/London"
    assert answers.apply_scope == "repo"
    assert answers.capture_window == 10
    assert answers.notes == []
    assert "Billing mode" in stdout.getvalue()


def test_gather_answers_interactive_blank_lines_use_derived_defaults():
    existing = Config(billing="subscription")
    stdin = io.StringIO("\n" * 7)
    answers = onboarding.gather_answers(
        detection=_detection(existing_config=existing), non_interactive=False, stdin=stdin, stdout=io.StringIO()
    )
    assert answers.billing == "subscription"
    assert answers.exclude_projects == []
    assert answers.capture_window == onboarding.DEFAULT_CAPTURE_WINDOW_DAYS


def test_gather_answers_invalid_capture_window_falls_back_with_note():
    answers_path_data = {"capture_window": "not-a-number"}
    answers = onboarding.gather_answers(
        detection=_detection(),
        non_interactive=True,
    )
    # Baseline (no answers file) sanity check first.
    assert answers.capture_window == onboarding.DEFAULT_CAPTURE_WINDOW_DAYS

    stdin = io.StringIO("api\n\nn\nn\n\nuser\nnot-a-number\n")
    answers2 = onboarding.gather_answers(
        detection=_detection(), non_interactive=False, stdin=stdin, stdout=io.StringIO()
    )
    assert answers2.capture_window == onboarding.DEFAULT_CAPTURE_WINDOW_DAYS
    assert any("not an integer" in note for note in answers2.notes)


# --------------------------------------------------------------------
# run_init: end to end
# --------------------------------------------------------------------


def _make_project(tmp_path: Path, name: str = "my-proj") -> tuple[Path, Path, str]:
    """Returns ``(real_project_path, projects_root, slug)`` where
    ``projects_root/slug`` is the directory Claude Code itself would have
    created for ``real_project_path`` -- see ``discovery.slug_for``.
    """
    real_project_path = tmp_path / "work" / name
    real_project_path.mkdir(parents=True)
    slug = discovery.slug_for(str(real_project_path))
    projects_root = tmp_path / "projects"
    (projects_root / slug).mkdir(parents=True)
    return real_project_path, projects_root, slug


def test_run_init_writes_config_and_project_files(tmp_path, monkeypatch):
    real_project_path, projects_root, slug = _make_project(tmp_path)
    monkeypatch.chdir(real_project_path)
    config_dir = tmp_path / "config"

    rc = onboarding.run_init(
        config_dir=config_dir,
        projects_root_path=projects_root,
        non_interactive=True,
        no_install=True,
        hook_fragment="HOOK",
        statusline_fragment="STATUSLINE",
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
    )
    assert rc == 0
    assert (config_dir / "config.toml").is_file()
    assert (config_dir / "projects" / f"{slug}.toml").is_file()

    config = load_config(config_dir)
    assert config.capture_window == onboarding.DEFAULT_CAPTURE_WINDOW_DAYS
    assert config.capture_started is not None


def test_run_init_no_install_skips_fragments(tmp_path, monkeypatch):
    real_project_path, projects_root, _slug = _make_project(tmp_path)
    monkeypatch.chdir(real_project_path)
    stdout = io.StringIO()

    onboarding.run_init(
        config_dir=tmp_path / "config",
        projects_root_path=projects_root,
        non_interactive=True,
        no_install=True,
        hook_fragment="THE-HOOK-FRAGMENT",
        statusline_fragment="THE-STATUSLINE-FRAGMENT",
        stdin=io.StringIO(""),
        stdout=stdout,
    )
    out = stdout.getvalue()
    assert "THE-HOOK-FRAGMENT" not in out
    assert "THE-STATUSLINE-FRAGMENT" not in out
    assert "skipped (--no-install)" in out


def test_run_init_prints_install_fragments_by_default(tmp_path, monkeypatch):
    real_project_path, projects_root, _slug = _make_project(tmp_path)
    monkeypatch.chdir(real_project_path)
    stdout = io.StringIO()

    onboarding.run_init(
        config_dir=tmp_path / "config",
        projects_root_path=projects_root,
        non_interactive=True,
        no_install=False,
        hook_fragment="THE-HOOK-FRAGMENT",
        statusline_fragment="THE-STATUSLINE-FRAGMENT",
        stdin=io.StringIO(""),
        stdout=stdout,
    )
    out = stdout.getvalue()
    assert "THE-HOOK-FRAGMENT" in out
    assert "THE-STATUSLINE-FRAGMENT" in out


def test_run_init_runs_an_initial_baseline_when_sessions_exist(tmp_path, monkeypatch):
    real_project_path, projects_root, slug = _make_project(tmp_path)
    write_jsonl(
        projects_root / slug / "session-1.jsonl",
        [turn_line(input_tokens=100 + i, output_tokens=20 + i) for i in range(4)],
    )
    monkeypatch.chdir(real_project_path)
    config_dir = tmp_path / "config"
    stdout = io.StringIO()

    rc = onboarding.run_init(
        config_dir=config_dir,
        projects_root_path=projects_root,
        non_interactive=True,
        no_install=True,
        hook_fragment="HOOK",
        statusline_fragment="STATUSLINE",
        stdin=io.StringIO(""),
        stdout=stdout,
    )
    assert rc == 0
    assert "Wrote initial baseline" in stdout.getvalue()
    records = list_baselines(config_dir)
    assert len(records) == 1
    assert records[0]["sessions_analysed"] == 1


def test_run_init_skips_baseline_when_no_project_directory_exists_yet(tmp_path, monkeypatch):
    # Unlike test_run_init_saves_a_minimal_baseline_when_project_dir_has_
    # no_sessions_yet below, this project has never been recorded by
    # Claude Code at all -- projects_root/<slug> doesn't exist -- so
    # discovery.resolve_project_dirs finds nothing and run_init's own
    # early check skips calling build_baseline entirely.
    real_project_path = tmp_path / "work" / "brand-new-project"
    real_project_path.mkdir(parents=True)
    projects_root = tmp_path / "projects"
    projects_root.mkdir(parents=True)
    monkeypatch.chdir(real_project_path)
    config_dir = tmp_path / "config"
    stdout = io.StringIO()

    rc = onboarding.run_init(
        config_dir=config_dir,
        projects_root_path=projects_root,
        non_interactive=True,
        no_install=True,
        hook_fragment="HOOK",
        statusline_fragment="STATUSLINE",
        stdin=io.StringIO(""),
        stdout=stdout,
    )
    assert rc == 0
    assert list_baselines(config_dir) == []
    assert "skipping the initial baseline capture" in stdout.getvalue()


def test_run_init_saves_a_minimal_baseline_when_project_dir_has_no_sessions_yet(tmp_path, monkeypatch):
    # projects_root/<slug> exists (Claude Code has recorded this project)
    # but has no session transcripts in it yet -- build_baseline's own
    # "no sessions" branch still produces and saves a minimal,
    # provisional record rather than run_init skipping the call outright.
    real_project_path, projects_root, _slug = _make_project(tmp_path)
    monkeypatch.chdir(real_project_path)
    config_dir = tmp_path / "config"
    stdout = io.StringIO()

    rc = onboarding.run_init(
        config_dir=config_dir,
        projects_root_path=projects_root,
        non_interactive=True,
        no_install=True,
        hook_fragment="HOOK",
        statusline_fragment="STATUSLINE",
        stdin=io.StringIO(""),
        stdout=stdout,
    )
    assert rc == 0
    records = list_baselines(config_dir)
    assert len(records) == 1
    assert records[0]["sessions_analysed"] == 0
    assert records[0]["provisional"] is True


def test_run_init_prints_capture_window_status(tmp_path, monkeypatch):
    real_project_path, projects_root, _slug = _make_project(tmp_path)
    monkeypatch.chdir(real_project_path)
    stdout = io.StringIO()

    onboarding.run_init(
        config_dir=tmp_path / "config",
        projects_root_path=projects_root,
        non_interactive=True,
        no_install=True,
        hook_fragment="HOOK",
        statusline_fragment="STATUSLINE",
        stdin=io.StringIO(""),
        stdout=stdout,
    )
    assert "Capture window:" in stdout.getvalue()


def test_run_init_bad_answers_file_exits_2(tmp_path, monkeypatch):
    real_project_path, projects_root, _slug = _make_project(tmp_path)
    monkeypatch.chdir(real_project_path)
    answers_path = tmp_path / "answers.json"
    answers_path.write_text("{not valid json", encoding="utf-8")
    stdout = io.StringIO()

    rc = onboarding.run_init(
        config_dir=tmp_path / "config",
        projects_root_path=projects_root,
        answers_path=answers_path,
        non_interactive=True,
        no_install=True,
        hook_fragment="HOOK",
        statusline_fragment="STATUSLINE",
        stdin=io.StringIO(""),
        stdout=stdout,
    )
    assert rc == 2


def test_run_init_invalid_answer_value_exits_2_without_writing(tmp_path, monkeypatch):
    real_project_path, projects_root, _slug = _make_project(tmp_path)
    monkeypatch.chdir(real_project_path)
    answers_path = tmp_path / "answers.json"
    answers_path.write_text(json.dumps({"billing": "not-a-real-mode"}), encoding="utf-8")
    config_dir = tmp_path / "config"

    rc = onboarding.run_init(
        config_dir=config_dir,
        projects_root_path=projects_root,
        answers_path=answers_path,
        non_interactive=True,
        no_install=True,
        hook_fragment="HOOK",
        statusline_fragment="STATUSLINE",
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
    )
    assert rc == 2
    assert not (config_dir / "config.toml").exists()


def test_run_init_derived_notes_are_printed(tmp_path, monkeypatch):
    real_project_path, projects_root, _slug = _make_project(tmp_path)
    monkeypatch.chdir(real_project_path)
    stdout = io.StringIO()

    onboarding.run_init(
        config_dir=tmp_path / "config",
        projects_root_path=projects_root,
        non_interactive=True,
        no_install=True,
        hook_fragment="HOOK",
        statusline_fragment="STATUSLINE",
        stdin=io.StringIO(""),
        stdout=stdout,
    )
    out = stdout.getvalue()
    assert "(derived) billing:" in out


def test_run_init_current_project_line_uses_the_redacted_slug(tmp_path, monkeypatch):
    # discovery.redact_slug only scrubs the OS-username segment right
    # after a "Users-"/"home-" marker (a project's own name is expected
    # to appear -- that's the point of a slug); this pins the "current
    # project" line to exactly what detect()/redact_slug produce, rather
    # than a stronger guarantee redact_slug doesn't make.
    real_project_path, projects_root, slug = _make_project(tmp_path, name="secret-client-name")
    monkeypatch.chdir(real_project_path)
    stdout = io.StringIO()

    onboarding.run_init(
        config_dir=tmp_path / "config",
        projects_root_path=projects_root,
        non_interactive=True,
        no_install=True,
        hook_fragment="HOOK",
        statusline_fragment="STATUSLINE",
        stdin=io.StringIO(""),
        stdout=stdout,
    )
    out = stdout.getvalue()
    expected_slug = discovery.redact_slug(slug)
    assert f"- current project: {expected_slug}" in out
    assert "Users-<user>" in expected_slug
