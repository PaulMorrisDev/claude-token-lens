"""``discovery.py``: slug derivation (incl. truncation), case-insensitive
project-dir de-dup, session window filters, and meta.json mapping.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from claude_token_lens import discovery


# -- projects_root ---------------------------------------------------------


def test_projects_root_defaults_to_home_dot_claude(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert discovery.projects_root() == Path.home() / ".claude" / "projects"


def test_projects_root_honours_claude_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert discovery.projects_root() == tmp_path / "projects"


# -- slug_for ---------------------------------------------------------------


def test_slug_for_replaces_non_alphanumerics():
    assert discovery.slug_for("C:\\Dev\\RevIXO") == "C--Dev-RevIXO"


def test_slug_for_short_path_is_unchanged_length():
    slug = discovery.slug_for("/home/me/project")
    assert len(slug) == len("/home/me/project")


def test_slug_for_truncates_long_paths_with_a_hash_suffix():
    long_path = "/" + ("a" * 300)
    slug = discovery.slug_for(long_path)
    assert len(slug) < len(long_path)
    # 200-char truncated prefix + "-" + 8 hex chars.
    prefix, _, suffix = slug.rpartition("-")
    assert len(prefix) == 200
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)
    # Deterministic: same input, same slug.
    assert discovery.slug_for(long_path) == slug


def test_slug_for_honours_project_dir_name_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere")
    monkeypatch.setenv("CLAUDE_CODE_PROJECT_DIR_NAME", "my-custom-slug")
    assert discovery.slug_for("C:\\Dev\\RevIXO") == "my-custom-slug"


def test_slug_for_ignores_project_dir_name_without_config_dir(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_PROJECT_DIR_NAME", "my-custom-slug")
    assert discovery.slug_for("C:\\Dev\\RevIXO") == "C--Dev-RevIXO"


# -- resolve_project_dirs ----------------------------------------------------


def test_resolve_project_dirs_slug_matching_is_case_insensitive(tmp_path):
    (tmp_path / "C--Dev-RevIXO").mkdir()
    # Query with different casing than the on-disk directory name.
    result = discovery.resolve_project_dirs(tmp_path, slugs=["c--dev-revixo"])
    assert [p.name for p in result] == ["C--Dev-RevIXO"]


def test_resolve_project_dirs_dedups_by_realpath(tmp_path, monkeypatch):
    """A worktree slug and its parent project can realpath to the same
    directory (e.g. a worktree symlinked back to the main checkout).
    Exercised by faking ``os.path.realpath`` rather than creating an
    actual symlink, since Windows symlink creation needs a privilege this
    sandbox may not have — the de-dup logic under test is
    ``os.path.normcase(os.path.realpath(p))``, independent of *how* two
    directory entries end up pointing at the same target.
    """
    (tmp_path / "proj-a").mkdir()
    (tmp_path / "proj-a--claude-worktrees-foo-123456").mkdir()
    canonical = str(tmp_path / "canonical-target")
    monkeypatch.setattr(discovery.os.path, "realpath", lambda p: canonical)

    result = discovery.resolve_project_dirs(tmp_path, all_projects=True)
    assert len(result) == 1


def test_resolve_project_dirs_all_projects(tmp_path):
    (tmp_path / "proj-a").mkdir()
    (tmp_path / "proj-b").mkdir()
    (tmp_path / "not-a-dir.txt").write_text("x")
    result = discovery.resolve_project_dirs(tmp_path, all_projects=True)
    assert {p.name for p in result} == {"proj-a", "proj-b"}


def test_resolve_project_dirs_family_regex(tmp_path):
    (tmp_path / "C--Dev-RevIXO").mkdir()
    (tmp_path / "C--Dev-RevIXO--claude-worktrees-foo-abc123").mkdir()
    (tmp_path / "C--Dev-other-project").mkdir()
    result = discovery.resolve_project_dirs(tmp_path, family_regex="RevIXO")
    assert {p.name for p in result} == {"C--Dev-RevIXO", "C--Dev-RevIXO--claude-worktrees-foo-abc123"}


def test_resolve_project_dirs_missing_root_returns_empty(tmp_path):
    assert discovery.resolve_project_dirs(tmp_path / "does-not-exist", all_projects=True) == []


def test_resolve_project_dirs_no_selector_returns_empty(tmp_path):
    (tmp_path / "proj-a").mkdir()
    assert discovery.resolve_project_dirs(tmp_path) == []


# -- find_sessions -----------------------------------------------------------


def _touch_jsonl(path: Path, mtime: float | None = None) -> None:
    path.write_text('{"type":"user","timestamp":"2026-09-18T12:00:00.000Z"}\n')
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_find_sessions_sorts_newest_first_by_mtime(tmp_path):
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    now = time.time()
    _touch_jsonl(old, mtime=now - 3600)
    _touch_jsonl(new, mtime=now)
    result = discovery.find_sessions(tmp_path, window_by="mtime")
    assert result == [new, old]


def test_find_sessions_limit(tmp_path):
    now = time.time()
    for i in range(5):
        _touch_jsonl(tmp_path / f"s{i}.jsonl", mtime=now - i)
    result = discovery.find_sessions(tmp_path, window_by="mtime", limit=2)
    assert len(result) == 2


def test_find_sessions_days_filter_excludes_old_files(tmp_path):
    now = time.time()
    recent = tmp_path / "recent.jsonl"
    stale = tmp_path / "stale.jsonl"
    _touch_jsonl(recent, mtime=now)
    _touch_jsonl(stale, mtime=now - 30 * 86400)
    result = discovery.find_sessions(tmp_path, days=7, window_by="mtime")
    assert result == [recent]


def test_find_sessions_window_by_timestamp_reads_first_line(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(
        '{"type":"user","timestamp":"2020-01-01T00:00:00.000Z"}\n'
        '{"type":"assistant","timestamp":"2020-01-01T00:00:05.000Z"}\n'
    )
    # File mtime is "now", but window_by=timestamp must use the 2020 line,
    # so a since=2025 filter must exclude it.
    result = discovery.find_sessions(tmp_path, since="2025-01-01T00:00:00Z", window_by="timestamp")
    assert result == []
    result_all = discovery.find_sessions(tmp_path, window_by="timestamp")
    assert result_all == [path]


def test_find_sessions_missing_dir_returns_empty(tmp_path):
    assert discovery.find_sessions(tmp_path / "nope") == []


def test_find_sessions_unknown_window_by_raises(tmp_path):
    path = tmp_path / "s.jsonl"
    _touch_jsonl(path)
    with pytest.raises(ValueError):
        discovery.find_sessions(tmp_path, window_by="bogus")


# -- find_subagents / find_workflows -----------------------------------------


def test_find_subagents_pairs_jsonl_with_meta(tmp_path):
    session_id = "sess-1"
    subagents_dir = tmp_path / session_id / "subagents"
    subagents_dir.mkdir(parents=True)
    (subagents_dir / "agent-abc123.jsonl").write_text("")
    (subagents_dir / "agent-abc123.meta.json").write_text(json.dumps({"agentType": "claude-implementer"}))
    (subagents_dir / "agent-def456.jsonl").write_text("")  # no meta file

    result = discovery.find_subagents(tmp_path, session_id)
    assert len(result) == 2
    by_name = {p.name: meta for p, meta in result}
    assert by_name["agent-abc123.jsonl"] == {"agentType": "claude-implementer"}
    assert by_name["agent-def456.jsonl"] == {}


def test_find_subagents_missing_dir_returns_empty(tmp_path):
    assert discovery.find_subagents(tmp_path, "no-such-session") == []


def test_find_workflows_lists_wf_json_files(tmp_path):
    session_id = "sess-1"
    workflows_dir = tmp_path / session_id / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "wf_001.json").write_text("{}")
    (workflows_dir / "not-a-workflow.json").write_text("{}")
    result = discovery.find_workflows(tmp_path, session_id)
    assert [p.name for p in result] == ["wf_001.json"]


# -- load_meta ----------------------------------------------------------------


def test_load_meta_maps_documented_keys(tmp_path):
    meta_path = tmp_path / "agent-abc.meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "agentType": "claude-implementer",
                "description": "a fairly long description of the task at hand",
                "spawnDepth": 2,
                "parentAgentId": "agent-parent-1",
                "model": "sonnet",
                "requestShape": "isolated",
                "worktreeBranch": "wp1-parse",
                "stoppedByUser": False,
                "toolUseId": "tu_01abc",
            }
        )
    )
    meta = discovery.load_meta(meta_path)
    assert meta.kind == "subagent"
    assert meta.agent_type == "claude-implementer"
    assert meta.description_len == len("a fairly long description of the task at hand")
    assert meta.spawn_depth == 2
    assert meta.parent_agent_id == "agent-parent-1"
    assert meta.agent_model_alias == "sonnet"
    assert meta.request_shape == "isolated"
    assert meta.worktree_branch_present is True  # bool only, never the branch name
    assert meta.stopped_by_user is False
    assert meta.tool_use_id == "tu_01abc"


def test_load_meta_derives_agent_id_and_session_id_from_path(tmp_path):
    # Realistic layout: <projects_root>/<slug>/<session_id>/subagents/agent-<hex>.meta.json
    subagents_dir = tmp_path / "session-xyz-789" / "subagents"
    subagents_dir.mkdir(parents=True)
    meta_path = subagents_dir / "agent-deadbeef.meta.json"
    meta_path.write_text(json.dumps({"agentType": "claude-implementer"}))

    meta = discovery.load_meta(meta_path)
    assert meta.agent_id == "agent-deadbeef"
    assert meta.session_id == "session-xyz-789"


def test_load_meta_missing_file_returns_defaults(tmp_path):
    meta = discovery.load_meta(tmp_path / "does-not-exist.meta.json")
    assert meta.kind == "subagent"
    assert meta.agent_type is None
    assert meta.worktree_branch_present is False


def test_load_meta_malformed_json_returns_defaults(tmp_path):
    meta_path = tmp_path / "bad.meta.json"
    meta_path.write_text("{not valid json")
    meta = discovery.load_meta(meta_path)
    assert meta.kind == "subagent"
    assert meta.agent_type is None


def test_load_meta_never_stores_worktree_branch_name(tmp_path):
    meta_path = tmp_path / "agent-abc.meta.json"
    meta_path.write_text(json.dumps({"worktreeBranch": "some-branch-name-that-must-not-leak"}))
    meta = discovery.load_meta(meta_path)
    assert meta.worktree_branch_present is True
    # Only a bool field exists for this on TranscriptMeta — there's no
    # attribute to have leaked the branch name into.
    assert not hasattr(meta, "worktree_branch")
