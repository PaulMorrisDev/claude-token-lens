"""Tests for the v0.3 ``init`` milestone's
``src/claude_token_lens/onboarding.py``: :func:`~claude_token_lens.
onboarding.detect`, :func:`~claude_token_lens.onboarding.load_answers_file`/
:func:`~claude_token_lens.onboarding.gather_answers` (an ``--answers``
file, interactive stdin prompting, and ``--non-interactive`` derivation,
each exercised directly against :class:`Detection`/hand-built answers
data), and what ``init`` (``setup_flow.run``) writes from them, end to
end, against synthetic projects built with ``tests/helpers``.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_token_lens import cli, discovery, installer, onboarding, setup_flow
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
    (snapshots_dir / "one.json").write_text(json.dumps({"ts": "2026-09-19T00:00:00Z", "effective": {}}), encoding="utf-8")

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
    assert "How do you pay for Claude Code?" in stdout.getvalue()


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("Max", "subscription"), ("pro", "subscription"), (" API ", "api"), ("auto", "auto")],
)
def test_gather_answers_billing_accepts_plan_names(typed, expected):
    stdin = io.StringIO(typed + "\n" + "\n" * 6)
    answers = onboarding.gather_answers(
        detection=_detection(existing_config=Config(billing="api")),
        non_interactive=False,
        stdin=stdin,
        stdout=io.StringIO(),
    )
    assert answers.billing == expected
    assert not any(note.startswith("billing:") for note in answers.notes)


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
# init: end to end
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
    (projects_root / slug).mkdir(parents=True, exist_ok=True)
    return real_project_path, projects_root, slug


def _init(config_dir, projects_root, *argv, stdin="", now=None, rc=0) -> str:
    """``init --non-interactive --no-install --no-service`` plus ``argv``;
    returns the output."""
    args = cli._make_parser().parse_args(
        [
            "init", "--config-dir", str(config_dir), "--projects-root", str(projects_root),
            "--non-interactive", "--no-install", "--no-service", *argv,
        ]
    )
    stdout = io.StringIO()
    assert setup_flow.run(*cli._setup_flow_inputs(args), stdin=io.StringIO(stdin), stdout=stdout, now=now) == rc
    return stdout.getvalue()


def test_init_writes_config_only_and_advanced_writes_the_project_file(tmp_path, monkeypatch):
    real_project_path, projects_root, slug = _make_project(tmp_path)
    monkeypatch.chdir(real_project_path)
    config_dir = tmp_path / "config"

    out = _init(config_dir, projects_root)
    project_toml_path = config_dir / "projects" / f"{slug}.toml"
    assert (config_dir / "config.toml").is_file()
    assert not project_toml_path.exists()
    config = load_config(config_dir)
    assert config.capture_window == onboarding.DEFAULT_CAPTURE_WINDOW_DAYS
    assert config.capture_started is not None

    advanced = _init(config_dir, projects_root, "--advanced")
    assert project_toml_path.is_file()
    # Fix N3: everything init writes and prints is checked for private
    # paths and names.
    assert_privacy_deep(
        {
            "config_toml": (config_dir / "config.toml").read_text(encoding="utf-8"),
            "project_toml": project_toml_path.read_text(encoding="utf-8"),
            # The dashboard's own address is the one URL init prints.
            "stdout": out.replace(installer.DEFAULT_URL, ""),
            "advanced": advanced.replace(installer.DEFAULT_URL, ""),
        }
    )


# --------------------------------------------------------------------
# Fix S6: init's first baseline used to be hard-wired to the cwd
# project's own slug, ignoring --all-projects/--project/--project-family
# entirely. Each test below builds two synthetic project directories --
# the cwd project (no sessions of its own) and a second, unrelated
# project (with sessions) -- and checks that the baseline only picks up
# the second project's sessions when a selector says to include it.
# --------------------------------------------------------------------


def _two_projects(tmp_path, monkeypatch, sessions: int) -> tuple[Path, str]:
    real_project_path, projects_root, _slug = _make_project(tmp_path, name="my-proj")
    _other_project_path, _projects_root2, other_slug = _make_project(tmp_path, name="other-proj")
    for i in range(sessions):
        write_jsonl(
            projects_root / other_slug / f"session-{i}.jsonl",
            [turn_line(input_tokens=100 + i, output_tokens=20 + i)],
        )
    monkeypatch.chdir(real_project_path)
    return projects_root, other_slug


@pytest.mark.parametrize(
    ("argv", "sessions", "expected"),
    [
        # my-proj has no sessions of its own: the other project's must
        # not leak into the default (cwd-only) baseline.
        ((), 3, 0),
        (("--all-projects",), 3, 3),
        (("--project", "OTHER"), 2, 2),
        (("--project-family", "other-proj"), 5, 5),
    ],
)
def test_init_baseline_follows_the_project_selectors(tmp_path, monkeypatch, argv, sessions, expected):
    projects_root, other_slug = _two_projects(tmp_path, monkeypatch, sessions)
    config_dir = tmp_path / "config"
    out = _init(config_dir, projects_root, *(other_slug if arg == "OTHER" else arg for arg in argv))
    records = list_baselines(config_dir)
    assert len(records) == 1
    assert records[0]["sessions_analysed"] == expected
    scope = "this project's" if not argv else "the chosen projects'"
    assert f"Reading {scope} history for a first baseline... " in out


def test_init_no_install_leaves_claude_code_alone(tmp_path, monkeypatch):
    real_project_path, projects_root, _slug = _make_project(tmp_path)
    monkeypatch.chdir(real_project_path)
    out = _init(tmp_path / "config", projects_root)
    assert "Connect to Claude Code: skipped (--no-install)." in out
    assert "Claude Code's settings.json" not in out


def test_init_non_interactive_connects_only_with_the_flag(tmp_path, monkeypatch):
    real_project_path, projects_root, _slug = _make_project(tmp_path)
    monkeypatch.chdir(real_project_path)
    args = cli._make_parser().parse_args(
        ["init", "--config-dir", str(tmp_path / "config"), "--projects-root", str(projects_root),
         "--non-interactive", "--no-service"]
    )
    stdout = io.StringIO()
    assert setup_flow.run(*cli._setup_flow_inputs(args), stdin=io.StringIO(""), stdout=stdout) == 0
    out = stdout.getvalue()
    assert "(derived) connect: not given on the command line; left unconnected (pass --connect to connect)" in out
    assert "Connect to Claude Code: not now. Run 'claude-token-lens init --connect' to connect." in out


def test_init_runs_an_initial_baseline_when_sessions_exist(tmp_path, monkeypatch):
    real_project_path, projects_root, slug = _make_project(tmp_path)
    write_jsonl(
        projects_root / slug / "session-1.jsonl",
        [turn_line(input_tokens=100 + i, output_tokens=20 + i) for i in range(4)],
    )
    monkeypatch.chdir(real_project_path)
    config_dir = tmp_path / "config"
    out = _init(config_dir, projects_root)
    assert "Then read this project's history for a first baseline." in out
    assert "Reading this project's history for a first baseline... 1 session.\n" in out
    records = list_baselines(config_dir)
    assert len(records) == 1
    assert records[0]["sessions_analysed"] == 1


def test_init_skips_baseline_when_no_project_directory_exists_yet(tmp_path, monkeypatch):
    # Unlike test_init_saves_a_minimal_baseline_when_project_dir_has_
    # no_sessions_yet below, this project has never been recorded by
    # Claude Code at all -- projects_root/<slug> doesn't exist -- so
    # discovery.resolve_project_dirs finds nothing and init skips the
    # baseline without a word.
    real_project_path = tmp_path / "work" / "brand-new-project"
    real_project_path.mkdir(parents=True)
    projects_root = tmp_path / "projects"
    projects_root.mkdir(parents=True)
    monkeypatch.chdir(real_project_path)
    config_dir = tmp_path / "config"
    out = _init(config_dir, projects_root)
    assert list_baselines(config_dir) == []
    assert "first baseline" not in out


def test_init_saves_a_minimal_baseline_when_project_dir_has_no_sessions_yet(tmp_path, monkeypatch):
    # projects_root/<slug> exists (Claude Code has recorded this project)
    # but has no session transcripts in it yet -- build_baseline's own
    # "no sessions" branch still produces and saves a minimal,
    # provisional record rather than init skipping the call outright.
    real_project_path, projects_root, _slug = _make_project(tmp_path)
    monkeypatch.chdir(real_project_path)
    config_dir = tmp_path / "config"
    out = _init(config_dir, projects_root)
    records = list_baselines(config_dir)
    assert len(records) == 1
    assert records[0]["sessions_analysed"] == 0
    assert records[0]["provisional"] is True
    assert "first baseline... 0 sessions." in out


def _init_at(config_dir, projects_root, now, *, repair_hook=False):
    _init(config_dir, projects_root, *(["--repair-hook"] if repair_hook else []), now=now)
    return load_config(config_dir)


@pytest.mark.parametrize("repair_hook", [False, True])
def test_init_again_keeps_the_original_capture_start(tmp_path, monkeypatch, repair_hook):
    real_project_path, projects_root, _slug = _make_project(tmp_path)
    monkeypatch.chdir(real_project_path)
    config_dir = tmp_path / "config"
    first = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    later = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)

    assert _init_at(config_dir, projects_root, first).capture_started == first.isoformat()
    # A second init (or init --repair-hook) weeks later must not restart
    # the window the user is part-way through.
    assert _init_at(config_dir, projects_root, later, repair_hook=repair_hook).capture_started == first.isoformat()


def test_init_sets_capture_start_when_config_has_none(tmp_path, monkeypatch):
    real_project_path, projects_root, _slug = _make_project(tmp_path)
    monkeypatch.chdir(real_project_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    # A config.toml written by hand (or before capture windows existed).
    (config_dir / "config.toml").write_text('billing = "api"\n', encoding="utf-8")
    now = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
    assert _init_at(config_dir, projects_root, now).capture_started == now.isoformat()


def test_init_bad_answers_file_exits_2(tmp_path, monkeypatch):
    real_project_path, projects_root, _slug = _make_project(tmp_path)
    monkeypatch.chdir(real_project_path)
    answers_path = tmp_path / "answers.json"
    answers_path.write_text("{not valid json", encoding="utf-8")
    config_dir = tmp_path / "config"
    _init(config_dir, projects_root, "--answers", str(answers_path), rc=2)
    assert not config_dir.exists()


def test_init_invalid_answer_value_exits_2_without_writing(tmp_path, monkeypatch):
    real_project_path, projects_root, _slug = _make_project(tmp_path)
    monkeypatch.chdir(real_project_path)
    answers_path = tmp_path / "answers.json"
    answers_path.write_text(json.dumps({"billing": "not-a-real-mode"}), encoding="utf-8")
    config_dir = tmp_path / "config"
    _init(config_dir, projects_root, "--answers", str(answers_path), rc=2)
    assert not (config_dir / "config.toml").exists()


def test_init_derived_notes_are_printed(tmp_path, monkeypatch):
    real_project_path, projects_root, _slug = _make_project(tmp_path)
    monkeypatch.chdir(real_project_path)
    out = _init(tmp_path / "config", projects_root)
    assert "(derived) billing: not given in --answers; used 'auto', worked out from usage-limit readings" in out


def test_init_output_never_names_the_user_in_the_project_path(tmp_path, monkeypatch):
    # The project directory is nested under a "home-<name>" segment we
    # control here, so the user name in the project's path is known on
    # every platform, wherever the pytest tmp root sits.
    real_project_path = tmp_path / "home-reallife-username" / "work" / "secret-client-name"
    real_project_path.mkdir(parents=True)
    slug = discovery.slug_for(str(real_project_path))
    projects_root = tmp_path / "projects"
    (projects_root / slug).mkdir(parents=True)
    monkeypatch.chdir(real_project_path)
    out = _init(tmp_path / "config", projects_root, "--advanced")
    assert "reallife-username" not in out
    assert "Found 1 project with Claude Code history." in out


# -- the metrics capture question ---------------------------------------------------


def test_capture_question_works_out_estimates_only_when_it_shows_them():
    calls = []

    def estimates():
        calls.append(1)
        return ["  Essentials  about 900 tokens and 0.40 USD a week"]

    out = io.StringIO()
    level, notes = onboarding.ask_capture_level(estimates=estimates, non_interactive=True, stdout=out)
    assert (level, calls, out.getvalue()) == ("off", [], "")
    assert notes and "metrics capture left off" in notes[0]

    level, notes = onboarding.ask_capture_level(estimates=estimates, stdin=io.StringIO("yes\n"), stdout=out)
    assert (level, notes, calls) == ("essentials", [], [1])
    assert "This uses your tokens" in out.getvalue() and "0.40 USD a week" in out.getvalue()


def test_capture_answer_prefers_the_flag_then_the_answers_file(tmp_path):
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"capture_level": "deep"}), encoding="utf-8")
    assert onboarding.capture_answer(answers, "free") == "free"
    assert onboarding.capture_answer(answers) == "deep"
    assert onboarding.capture_answer(None) is None


# --------------------------------------------------------------------
# the time-box question (ask_capture_until / capture_no_limit_answer)
# --------------------------------------------------------------------

_NOW = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)


def test_capture_no_limit_answer_prefers_the_flag_then_the_answers_file(tmp_path):
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"capture_no_limit": True}), encoding="utf-8")
    assert onboarding.capture_no_limit_answer(answers, False) is False
    assert onboarding.capture_no_limit_answer(answers) is True
    assert onboarding.capture_no_limit_answer(None) is None


def test_ask_capture_until_defaults_to_a_14_day_box_on_a_blank_answer():
    out = io.StringIO()
    until, notes = onboarding.ask_capture_until(now=_NOW, stdin=io.StringIO("\n"), stdout=out)
    assert until == "2026-10-08T06:00:00+00:00"
    assert notes == []
    assert "switch itself off on 2026-10-08 06:00 UTC (14 days from now)" in out.getvalue()
    assert "claude-token-lens capture on --for 30d" in out.getvalue()
    assert "Turn off that time limit" in out.getvalue()


def test_ask_capture_until_yes_answer_means_no_limit():
    until, notes = onboarding.ask_capture_until(now=_NOW, stdin=io.StringIO("y\n"), stdout=io.StringIO())
    assert (until, notes) == ("", [])


def test_ask_capture_until_flag_skips_the_question():
    out = io.StringIO()
    until, notes = onboarding.ask_capture_until(now=_NOW, preset=True, stdout=out)
    assert (until, notes) == ("", [])
    assert out.getvalue() == ""


def test_ask_capture_until_answers_file_false_applies_the_default_box_without_asking(tmp_path):
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"capture_no_limit": False}), encoding="utf-8")
    out = io.StringIO()
    until, notes = onboarding.ask_capture_until(now=_NOW, answers_path=answers, stdout=out)
    assert until == "2026-10-08T06:00:00+00:00"
    assert notes == []
    assert out.getvalue() == ""


def test_ask_capture_until_non_interactive_without_an_answer_applies_the_default_box():
    # CAP-8: a scripted/unattended init is exactly the case the default
    # most needs to reach -- capture left running forever with nobody
    # watching is the failure mode this question exists to prevent, not
    # a surprise end date. This reverses the tool's earlier assumption
    # (leave `until` untouched under --non-interactive with no answer);
    # it now follows the same "no answer -> the derived default, and a
    # note says so" rule every other onboarding question already uses.
    until, notes = onboarding.ask_capture_until(now=_NOW, non_interactive=True, stdout=io.StringIO())
    assert until == "2026-10-08T06:00:00+00:00"
    assert notes and "capture_no_limit: not given in --answers; used default 'n'" in notes[0]


def test_ask_capture_until_non_interactive_with_a_preset_needs_no_stdin():
    until, notes = onboarding.ask_capture_until(now=_NOW, preset=False, non_interactive=True, stdout=io.StringIO())
    assert until == "2026-10-08T06:00:00+00:00"
    assert notes == []
