"""Tests for ``profiles/apply.py``: ``plan_apply``, ``execute``,
``revert``, ``list_backups``, ``write_launch_overlay`` and
``env_lines_for_profile``.

Every test builds its own ``home``/``project``/``config_dir`` under
``tmp_path`` -- this module is the one place in the package that writes
real files outside a fixture directory, so nothing here should ever
touch the machine's actual ``~/.claude``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from helpers import assert_privacy

from claude_token_lens.profiles import apply as apply_mod
from claude_token_lens.profiles.diff import diff_against_effective, render_unified_diff
from claude_token_lens.profiles.frontmatter import parse_frontmatter
from claude_token_lens.profiles.schema import load_dict
from claude_token_lens.snapshots import Snapshot


def _profile(**overrides):
    doc = {"id": "sample", "settings": {"effortLevel": "high"}}
    doc.update(overrides)
    return load_dict(doc)


def _snapshot(**data):
    return Snapshot(path=Path("snap.json"), ts="20260101T000000Z", data=data)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _init_git_repo_with_file(project: Path, rel_path: str, content: str) -> None:
    """A minimal git repo whose ``rel_path`` is already committed --
    used by the tracked-file refusal tests."""
    project.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=project)
    target = project / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git("add", rel_path, cwd=project)
    _git("-c", "user.email=a@example.com", "-c", "user.name=test", "commit", "-q", "-m", "init", cwd=project)


# --------------------------------------------------------------------
# plan_apply: scope resolution
# --------------------------------------------------------------------


def test_plan_apply_user_scope_targets_home_settings(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".claude" / "token-lens"
    plan = apply_mod.plan_apply(_profile(), scope="user", project_path=None, config_dir=config_dir, home=home)
    settings_action = next(a for a in plan.actions if a.kind == "settings")
    assert settings_action.path == home / ".claude" / "settings.json"


def test_plan_apply_project_local_scope_targets_settings_local(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "token-lens"
    plan = apply_mod.plan_apply(
        _profile(), scope="project-local", project_path=project, config_dir=config_dir, home=home
    )
    settings_action = next(a for a in plan.actions if a.kind == "settings")
    assert settings_action.path == project / ".claude" / "settings.local.json"


def test_plan_apply_repo_scope_targets_shared_settings(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "token-lens"
    plan = apply_mod.plan_apply(_profile(), scope="repo", project_path=project, config_dir=config_dir, home=home)
    settings_action = next(a for a in plan.actions if a.kind == "settings")
    assert settings_action.path == project / ".claude" / "settings.json"


def test_plan_apply_rejects_unknown_scope(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".claude" / "token-lens"
    with pytest.raises(ValueError):
        apply_mod.plan_apply(_profile(), scope="not-a-scope", project_path=None, config_dir=config_dir, home=home)


def test_plan_apply_project_scope_without_project_path_raises(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".claude" / "token-lens"
    with pytest.raises(ValueError):
        apply_mod.plan_apply(_profile(), scope="repo", project_path=None, config_dir=config_dir, home=home)


# --------------------------------------------------------------------
# dry-run diff: provably the same computation diff.py's own render uses
# --------------------------------------------------------------------


def test_plan_apply_diff_text_matches_diff_module_computed_independently(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".claude" / "token-lens"
    profile = _profile(settings={"effortLevel": "high"})
    snapshot = _snapshot(effective={"effortLevel": "medium"}, effective_provenance={"effortLevel": "user"})

    plan = apply_mod.plan_apply(
        profile, scope="user", project_path=None, config_dir=config_dir, home=home, snapshot=snapshot
    )

    expected_diff = diff_against_effective(
        profile,
        effective={"effortLevel": "medium"},
        effective_agents={},
        provenance={"effortLevel": "user"},
        managed_keys=set(),
    )
    expected_text = render_unified_diff(expected_diff, scope="user")
    assert plan.diff_text == expected_text
    assert "-effortLevel: medium" in plan.diff_text
    assert "+effortLevel: high" in plan.diff_text


def test_plan_apply_diff_text_is_privacy_clean_without_project_path(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".claude" / "token-lens"
    profile = _profile(settings={"effortLevel": "high"}, agents={"reviewer": {"effort": "high"}})
    for scope in ("user", "project-local", "repo"):
        project_path = tmp_path / "proj" if scope != "user" else None
        plan = apply_mod.plan_apply(
            profile, scope=scope, project_path=project_path, config_dir=config_dir, home=home
        )
        assert_privacy({"diff": plan.diff_text})


# --------------------------------------------------------------------
# apply -> revert round trip
# --------------------------------------------------------------------


def test_apply_then_revert_restores_byte_identical_content(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "token-lens"
    agents_dir = project / ".claude" / "agents"
    agents_dir.mkdir(parents=True)

    settings_path = project / ".claude" / "settings.local.json"
    settings_path.write_text('{\n  "outputStyle": "concise"\n}\n', encoding="utf-8")
    agent_path = agents_dir / "reviewer.md"
    original_agent_text = (
        "---\n"
        "model: opus\n"
        "effort: low  # keep this reviewer careful\n"
        "---\n\n"
        "# Reviewer\n\nBody text unrelated to any lever.\n"
    )
    agent_path.write_text(original_agent_text, encoding="utf-8")
    original_settings_bytes = settings_path.read_bytes()
    original_agent_bytes = agent_path.read_bytes()

    profile = _profile(
        settings={"effortLevel": "high"}, agents={"reviewer": {"effort": "high"}}
    )
    plan = apply_mod.plan_apply(
        profile, scope="project-local", project_path=project, config_dir=config_dir, home=home
    )
    assert not plan.blocked
    result = apply_mod.execute(plan, config_dir=config_dir)

    # Content actually changed, and unrelated content survived.
    new_settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert new_settings["outputStyle"] == "concise"
    assert new_settings["effortLevel"] == "high"
    new_agent_text = agent_path.read_text(encoding="utf-8")
    assert "model: opus" in new_agent_text
    assert "# keep this reviewer careful" in new_agent_text
    assert "Body text unrelated to any lever." in new_agent_text
    assert parse_frontmatter(new_agent_text)["effort"] == "high"
    assert settings_path.read_bytes() != original_settings_bytes
    assert agent_path.read_bytes() != original_agent_bytes

    revert_result = apply_mod.revert(result.ts, config_dir=config_dir)
    assert settings_path in revert_result.restored
    assert agent_path in revert_result.restored
    assert settings_path.read_bytes() == original_settings_bytes
    assert agent_path.read_bytes() == original_agent_bytes


def test_apply_then_revert_deletes_files_that_did_not_exist_before(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".claude" / "token-lens"

    profile = _profile(settings={"effortLevel": "high"})
    plan = apply_mod.plan_apply(profile, scope="user", project_path=None, config_dir=config_dir, home=home)
    result = apply_mod.execute(plan, config_dir=config_dir)

    settings_path = home / ".claude" / "settings.json"
    assert settings_path.exists()
    active_path = config_dir / "active-profile"
    assert active_path.read_text(encoding="utf-8").strip() == "sample"

    revert_result = apply_mod.revert(result.ts, config_dir=config_dir)
    assert settings_path in revert_result.deleted
    assert active_path in revert_result.deleted
    assert not settings_path.exists()
    assert not active_path.exists()


def test_revert_raises_for_unknown_timestamp(tmp_path):
    config_dir = tmp_path / "home" / ".claude" / "token-lens"
    with pytest.raises(apply_mod.ApplyError):
        apply_mod.revert("20200101T000000Z", config_dir=config_dir)


def test_execute_raises_and_writes_nothing_when_plan_is_blocked(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "token-lens"
    _init_git_repo_with_file(project, ".claude/settings.json", "{}\n")

    profile = _profile(settings={"effortLevel": "high"})
    plan = apply_mod.plan_apply(profile, scope="repo", project_path=project, config_dir=config_dir, home=home)
    assert plan.blocked
    with pytest.raises(apply_mod.ApplyError) as excinfo:
        apply_mod.execute(plan, config_dir=config_dir)
    assert list(excinfo.value.reasons) == list(plan.blocked)
    assert not (config_dir / "backups").exists()


# --------------------------------------------------------------------
# git-tracked file refusal + --allow-tracked
# --------------------------------------------------------------------


def test_plan_apply_blocks_tracked_settings_file(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "token-lens"
    _init_git_repo_with_file(project, ".claude/settings.json", "{}\n")

    profile = _profile(settings={"effortLevel": "high"})
    plan = apply_mod.plan_apply(profile, scope="repo", project_path=project, config_dir=config_dir, home=home)
    assert any("tracked by git" in reason for reason in plan.blocked)


def test_allow_tracked_permits_writing_a_tracked_settings_file(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "token-lens"
    _init_git_repo_with_file(project, ".claude/settings.json", "{}\n")

    profile = _profile(settings={"effortLevel": "high"})
    plan = apply_mod.plan_apply(
        profile, scope="repo", project_path=project, config_dir=config_dir, home=home, allow_tracked=True
    )
    assert not plan.blocked
    result = apply_mod.execute(plan, config_dir=config_dir)
    assert (project / ".claude" / "settings.json") in result.written


def test_plan_apply_blocks_tracked_agent_file(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "token-lens"
    _init_git_repo_with_file(
        project, ".claude/agents/reviewer.md", "---\neffort: low\n---\n\nBody.\n"
    )

    profile = _profile(settings={}, agents={"reviewer": {"effort": "high"}})
    plan = apply_mod.plan_apply(
        profile, scope="project-local", project_path=project, config_dir=config_dir, home=home
    )
    assert any("tracked by git" in reason for reason in plan.blocked)

    plan_allowed = apply_mod.plan_apply(
        profile,
        scope="project-local",
        project_path=project,
        config_dir=config_dir,
        home=home,
        allow_tracked=True,
    )
    assert not plan_allowed.blocked
    apply_mod.execute(plan_allowed, config_dir=config_dir)
    assert parse_frontmatter((project / ".claude" / "agents" / "reviewer.md").read_text())["effort"] == "high"


# --------------------------------------------------------------------
# missing agent file refusal + --force
# --------------------------------------------------------------------


def test_plan_apply_blocks_missing_agent_file(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "token-lens"
    (project / ".claude" / "agents").mkdir(parents=True)

    profile = _profile(settings={}, agents={"ghost": {"model": "opus"}})
    plan = apply_mod.plan_apply(
        profile, scope="project-local", project_path=project, config_dir=config_dir, home=home
    )
    assert any("no agent file found" in reason for reason in plan.blocked)
    assert not (project / ".claude" / "agents" / "ghost.md").exists()


def test_force_creates_missing_agent_file_from_scratch(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "token-lens"
    (project / ".claude" / "agents").mkdir(parents=True)

    profile = _profile(settings={}, agents={"ghost": {"model": "opus"}})
    plan = apply_mod.plan_apply(
        profile, scope="project-local", project_path=project, config_dir=config_dir, home=home, force=True
    )
    assert not plan.blocked
    apply_mod.execute(plan, config_dir=config_dir)
    agent_path = project / ".claude" / "agents" / "ghost.md"
    assert agent_path.exists()
    assert parse_frontmatter(agent_path.read_text(encoding="utf-8"))["model"] == "opus"


def test_force_does_not_affect_allow_tracked(tmp_path):
    """--force's only job is creating a missing agent file from scratch;
    it must not also waive the tracked-file refusal (that is
    --allow-tracked's job specifically, per the module docstring)."""
    home = tmp_path / "home"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "token-lens"
    _init_git_repo_with_file(project, ".claude/settings.json", "{}\n")

    profile = _profile(settings={"effortLevel": "high"})
    plan = apply_mod.plan_apply(
        profile, scope="repo", project_path=project, config_dir=config_dir, home=home, force=True
    )
    assert any("tracked by git" in reason for reason in plan.blocked)


# --------------------------------------------------------------------
# managed keys: excluded from writes, listed in skipped_managed
# --------------------------------------------------------------------


def test_managed_settings_key_is_excluded_and_reported(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".claude" / "token-lens"
    profile = _profile(settings={"effortLevel": "high", "outputStyle": "concise"})
    snapshot = _snapshot(managed_keys=["effortLevel"])

    plan = apply_mod.plan_apply(
        profile, scope="user", project_path=None, config_dir=config_dir, home=home, snapshot=snapshot
    )
    assert "settings.effortLevel" in plan.skipped_managed
    settings_action = next(a for a in plan.actions if a.kind == "settings")
    written = json.loads(settings_action.new_bytes.decode("utf-8"))
    assert "effortLevel" not in written
    assert written["outputStyle"] == "concise"


def test_managed_agent_key_is_excluded_via_agents_wildcard(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "token-lens"
    agents_dir = project / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text("---\nmodel: opus\n---\n", encoding="utf-8")

    profile = _profile(settings={}, agents={"reviewer": {"effort": "high"}})
    snapshot = _snapshot(managed_keys=["agents"])
    plan = apply_mod.plan_apply(
        profile,
        scope="project-local",
        project_path=project,
        config_dir=config_dir,
        home=home,
        snapshot=snapshot,
    )
    assert "agents.reviewer.effort" in plan.skipped_managed
    assert not any(a.kind == "agent_frontmatter" for a in plan.actions)


def test_managed_env_key_is_excluded_from_env_lines(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".claude" / "token-lens"
    profile = _profile(
        settings={}, env={"CLAUDE_CODE_PROMPT_CACHE_TTL": "5m", "MAX_THINKING_TOKENS": "1024"}
    )
    snapshot = _snapshot(managed_keys=["CLAUDE_CODE_PROMPT_CACHE_TTL"])
    plan = apply_mod.plan_apply(
        profile, scope="user", project_path=None, config_dir=config_dir, home=home, snapshot=snapshot
    )
    assert "env.CLAUDE_CODE_PROMPT_CACHE_TTL" in plan.skipped_managed
    assert not any(line.startswith("CLAUDE_CODE_PROMPT_CACHE_TTL=") for line in plan.env_lines)
    assert "MAX_THINKING_TOKENS=1024" in plan.env_lines


# --------------------------------------------------------------------
# env_lines_for_profile
# --------------------------------------------------------------------


def test_env_lines_for_profile_orders_by_allowlist_and_excludes_managed():
    profile = _profile(
        settings={},
        env={
            "MAX_THINKING_TOKENS": "1024",
            "CLAUDE_CODE_PROMPT_CACHE_TTL": "5m",
        },
    )
    lines = apply_mod.env_lines_for_profile(profile, managed_keys=set())
    # ENV_ALLOWLIST order lists CLAUDE_CODE_PROMPT_CACHE_TTL before
    # MAX_THINKING_TOKENS -- assert the profile's own two lines come out
    # in that fixed order, not insertion/dict order.
    assert lines == ("CLAUDE_CODE_PROMPT_CACHE_TTL=5m", "MAX_THINKING_TOKENS=1024")

    lines_excluded = apply_mod.env_lines_for_profile(profile, managed_keys={"MAX_THINKING_TOKENS"})
    assert lines_excluded == ("CLAUDE_CODE_PROMPT_CACHE_TTL=5m",)


# --------------------------------------------------------------------
# settings file that isn't valid JSON
# --------------------------------------------------------------------


def test_plan_apply_raises_on_unparseable_existing_settings_json(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".claude" / "token-lens"
    settings_path = home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("not json at all", encoding="utf-8")

    profile = _profile(settings={"effortLevel": "high"})
    with pytest.raises(apply_mod.ApplyError):
        apply_mod.plan_apply(profile, scope="user", project_path=None, config_dir=config_dir, home=home)


# --------------------------------------------------------------------
# --launch: writes only the settings overlay, nothing else
# --------------------------------------------------------------------


def test_write_launch_overlay_writes_only_the_overlay_file(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".claude" / "token-lens"
    profile = _profile(settings={"effortLevel": "high", "outputStyle": "concise"})

    path = apply_mod.write_launch_overlay(profile, config_dir=config_dir)
    assert path == config_dir / "profiles" / "sample.settings.json"
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {"effortLevel": "high", "outputStyle": "concise"}

    # Nothing else was created under config_dir -- no backups, no
    # manifest, no active-profile marker, no snapshot stamp.
    all_paths = sorted(p.relative_to(config_dir) for p in config_dir.rglob("*") if p.is_file())
    assert all_paths == [Path("profiles") / "sample.settings.json"]


def test_write_launch_overlay_excludes_managed_keys(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".claude" / "token-lens"
    profile = _profile(settings={"effortLevel": "high", "outputStyle": "concise"})
    path = apply_mod.write_launch_overlay(profile, config_dir=config_dir, managed_keys={"effortLevel"})
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {"outputStyle": "concise"}


# --------------------------------------------------------------------
# list_backups
# --------------------------------------------------------------------


def test_list_backups_empty_when_no_backups_dir(tmp_path):
    config_dir = tmp_path / "home" / ".claude" / "token-lens"
    assert apply_mod.list_backups(config_dir) == []


def test_list_backups_reports_ts_profile_scope_and_file_count(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".claude" / "token-lens"

    profile_a = _profile(settings={"effortLevel": "high"})
    plan_a = apply_mod.plan_apply(profile_a, scope="user", project_path=None, config_dir=config_dir, home=home)
    result_a = apply_mod.execute(plan_a, config_dir=config_dir)

    profile_b = load_dict({"id": "other", "settings": {"outputStyle": "concise"}})
    plan_b = apply_mod.plan_apply(profile_b, scope="user", project_path=None, config_dir=config_dir, home=home)
    result_b = apply_mod.execute(plan_b, config_dir=config_dir)

    # Both executes can legitimately land in the same wall-clock second;
    # execute()'s own collision-avoidance (see its docstring/comment)
    # gives them distinct backup dirs regardless, which is exactly what
    # this test is asserting.
    assert result_a.ts != result_b.ts

    backups = apply_mod.list_backups(config_dir)
    ts_values = [b.ts for b in backups]
    assert ts_values == sorted(ts_values)
    assert result_a.ts in ts_values
    assert result_b.ts in ts_values
    by_ts = {b.ts: b for b in backups}
    assert by_ts[result_a.ts].profile_id == "sample"
    assert by_ts[result_a.ts].scope == "user"
    assert by_ts[result_a.ts].file_count >= 1
    assert by_ts[result_b.ts].profile_id == "other"


def test_execute_avoids_backup_collision_within_the_same_second(tmp_path, monkeypatch):
    """Force both executes to compute the exact same base timestamp
    (simulating two applies within the same UTC second) and confirm the
    first apply's backup survives untouched -- the collision this
    module's ``execute()`` disambiguates against."""
    home = tmp_path / "home"
    config_dir = home / ".claude" / "token-lens"

    import claude_token_lens.profiles.apply as apply_module

    class _FrozenDatetime(apply_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return apply_module.datetime(2026, 1, 1, tzinfo=tz)

    monkeypatch.setattr(apply_module, "datetime", _FrozenDatetime)

    profile_a = _profile(settings={"effortLevel": "high"})
    plan_a = apply_mod.plan_apply(profile_a, scope="user", project_path=None, config_dir=config_dir, home=home)
    result_a = apply_mod.execute(plan_a, config_dir=config_dir)

    profile_b = load_dict({"id": "other", "settings": {"outputStyle": "concise"}})
    plan_b = apply_mod.plan_apply(profile_b, scope="user", project_path=None, config_dir=config_dir, home=home)
    result_b = apply_mod.execute(plan_b, config_dir=config_dir)

    assert result_a.ts == "20260101T000000Z"
    assert result_b.ts == "20260101T000000Z-2"

    manifest_a = json.loads((result_a.backup_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest_b = json.loads((result_b.backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_a["profile_id"] == "sample"
    assert manifest_b["profile_id"] == "other"

    revert_a = apply_mod.revert(result_a.ts, config_dir=config_dir)
    assert revert_a.deleted or revert_a.restored


def test_list_backups_skips_unparseable_manifest(tmp_path):
    config_dir = tmp_path / "home" / ".claude" / "token-lens"
    bad_dir = config_dir / "backups" / "20260101T000000Z"
    bad_dir.mkdir(parents=True)
    (bad_dir / "manifest.json").write_text("not json", encoding="utf-8")
    assert apply_mod.list_backups(config_dir) == []


# --------------------------------------------------------------------
# privacy: apply results/backups never leak paths beyond what the
# caller itself supplied (home/project_path/config_dir)
# --------------------------------------------------------------------


def test_apply_result_paths_are_all_under_caller_supplied_roots(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "token-lens"
    (project / ".claude" / "agents").mkdir(parents=True)

    profile = _profile(settings={"effortLevel": "high"})
    plan = apply_mod.plan_apply(
        profile, scope="project-local", project_path=project, config_dir=config_dir, home=home
    )
    result = apply_mod.execute(plan, config_dir=config_dir)

    for path in result.written:
        assert str(path).startswith(str(project)) or str(path).startswith(str(home))
    assert str(result.snapshot_path).startswith(str(config_dir))
    assert str(result.active_profile_path).startswith(str(config_dir))
