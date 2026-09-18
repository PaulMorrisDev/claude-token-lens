"""Subprocess tests for src/claude_token_lens/hooks/snapshot-config.py (WP7).

The hook is standalone stdlib and imports nothing from the package, so it
is exercised the way it actually runs: as a separate ``python`` process
fed the SessionStart JSON on stdin, against a temporary HOME/config dir
built from scratch in each test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HOOK_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "claude_token_lens"
    / "hooks"
    / "snapshot-config.py"
)

_AGENT_WITH_CACHE_TTL = """---
name: verification-runner
description: Read-only source verifier that runs proportionate builds and tests.
model: sonnet
effort: medium
disallowedTools: Write, Edit, NotebookEdit
maxTurns: 80
experimental:
  cacheTtl: 1h
---

Body text is not frontmatter and must never appear in a snapshot.
"""

_AGENT_PLAIN = """---
name: claude-implementer
description: Sonnet implementation worker.
model: sonnet
effort: medium
maxTurns: 60
---

Body text for the second agent.
"""


def _build_home(tmp_path: Path) -> Path:
    """A temp HOME with a user settings.json (some allowlisted keys, some
    not) and two agent frontmatter files, one with a nested map.
    """
    home = tmp_path / "home"
    agents_dir = home / ".claude" / "agents"
    agents_dir.mkdir(parents=True)

    user_settings = {
        "model": "fable[1m]",
        "effortLevel": "high",
        "autoCompactWindow": 300000,
        "promptCacheTtl": None,
        "alwaysThinkingEnabled": True,
        # Not on the allowlist and not bool/int -> must redact to dict(n).
        "permissions": {"allow": ["Bash(git *)"], "deny": []},
        # Not on the allowlist and not bool/int -> must redact to str(len).
        "statusLine": {"type": "command", "command": "some-status-command"},
        "mcpServers": {"filesystem": {}, "github": {}},
        "enabledMcpjsonServers": ["filesystem"],
        "enabledPlugins": {"my-plugin@marketplace": True},
    }
    (home / ".claude" / "settings.json").write_text(
        json.dumps(user_settings), encoding="utf-8"
    )
    (agents_dir / "verification-runner.md").write_text(
        _AGENT_WITH_CACHE_TTL, encoding="utf-8"
    )
    (agents_dir / "claude-implementer.md").write_text(_AGENT_PLAIN, encoding="utf-8")
    return home


def _build_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    project_settings = {"model": "sonnet", "permissions": {"allow": ["Read(**)"]}}
    (project / ".claude" / "settings.json").write_text(
        json.dumps(project_settings), encoding="utf-8"
    )
    return project


def _run_hook(
    *,
    config_dir: Path,
    cwd: Path | None,
    stdin_text: str,
    extra_env: dict | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["ANTHROPIC_FAKE"] = "secret"
    env["CLAUDE_X"] = "1"
    # A decoy that must NOT be picked up (wrong prefix).
    env["UNRELATED_VAR"] = "should-not-appear"
    # Point Path.home() at config_dir's parent (which has no .claude.json of
    # its own) instead of the real machine's home, so the hook's "~/.claude
    # .json if readable" MCP-server read can never pick up the real user's
    # config and break test determinism.
    env["HOME"] = str(config_dir.parent)
    env["USERPROFILE"] = str(config_dir.parent)
    if extra_env:
        env.update(extra_env)

    args = [sys.executable, str(_HOOK_PATH), "--config-dir", str(config_dir)]
    if cwd is not None:
        args += ["--cwd", str(cwd)]
    if extra_args:
        args += extra_args

    return subprocess.run(
        args,
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _latest_snapshot(config_dir: Path) -> dict:
    snapshots_dir = config_dir / "token-lens" / "snapshots"
    files = sorted(snapshots_dir.glob("*.json"))
    assert files, f"no snapshot written under {snapshots_dir}"
    return json.loads(files[-1].read_text(encoding="utf-8"))


@pytest.fixture()
def home(tmp_path):
    return _build_home(tmp_path)


@pytest.fixture()
def project(tmp_path):
    return _build_project(tmp_path)


def test_hook_writes_redacted_snapshot(tmp_path, home, project):
    config_dir = home / ".claude"
    stdin = json.dumps(
        {
            "session_id": "sess-1",
            "cwd": str(project),
            "transcript_path": "/x/transcript.jsonl",
            "source": "startup",
        }
    )
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)

    assert result.returncode == 0
    assert result.stdout == ""

    snapshot = _latest_snapshot(config_dir)

    # Safe-allowlist keys kept verbatim.
    user_settings = snapshot["user_settings"]
    assert user_settings["model"] == "fable[1m]"
    assert user_settings["effortLevel"] == "high"
    assert user_settings["autoCompactWindow"] == 300000
    assert user_settings["promptCacheTtl"] is None
    # bool kept even though not on the allowlist by name.
    assert user_settings["alwaysThinkingEnabled"] is True

    # Non-allowlisted dict/str values reduced to shape markers.
    assert user_settings["permissions"] == "dict(2)"
    assert user_settings["statusLine"] == "dict(2)"

    # MCP server names only, plus enabled/disabled lists.
    assert snapshot["mcp_servers"]["names"] == ["filesystem", "github"]
    assert snapshot["mcp_servers"]["enabled_mcpjson_servers"] == ["filesystem"]

    # Plugin keys only.
    assert snapshot["enabled_plugins"] == ["my-plugin@marketplace"]

    # Project settings keyed by a path hash, same redaction applied.
    assert len(snapshot["project_settings"]) == 1
    project_entry = next(iter(snapshot["project_settings"].values()))
    assert project_entry["model"] == "sonnet"
    assert project_entry["permissions"] == "dict(1)"

    assert snapshot["session_id"] == "sess-1"
    assert snapshot["transcript_path"] == "/x/transcript.jsonl"
    assert snapshot["source"] == "startup"
    assert snapshot["cwd_hash"].startswith("sha256:")
    assert snapshot["content_hash"].startswith("sha256:")


def test_hook_flattens_nested_agent_frontmatter(home, project):
    config_dir = home / ".claude"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0

    snapshot = _latest_snapshot(config_dir)
    agents = snapshot["agents"]

    verifier = agents["verification-runner"]
    assert verifier["experimental.cacheTtl"] == "1h"
    assert verifier["model"] == "sonnet"
    assert verifier["effort"] == "medium"
    assert verifier["maxTurns"] == 80
    assert verifier["disallowedTools"] == "Write, Edit, NotebookEdit"
    # description is redacted to a length marker, never the raw text.
    assert verifier["description"].startswith("str(")
    assert "Read-only source verifier" not in json.dumps(snapshot)

    implementer = agents["claude-implementer"]
    assert implementer["model"] == "sonnet"
    assert "experimental.cacheTtl" not in implementer


def test_env_names_only_never_values(home, project):
    config_dir = home / ".claude"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0

    snapshot_path = sorted((config_dir / "token-lens" / "snapshots").glob("*.json"))[-1]
    raw_text = snapshot_path.read_text(encoding="utf-8")
    snapshot = json.loads(raw_text)

    assert "ANTHROPIC_FAKE" in snapshot["env_names"]
    assert "CLAUDE_X" in snapshot["env_names"]
    assert "UNRELATED_VAR" not in snapshot["env_names"]

    # The secret value must appear nowhere in the written file, and no raw
    # env value should have leaked in under any key.
    assert "secret" not in raw_text
    assert "should-not-appear" not in raw_text


def test_exit_zero_on_malformed_stdin(home, project):
    config_dir = home / ".claude"
    result = _run_hook(
        config_dir=config_dir, cwd=project, stdin_text="{not valid json!!!"
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_exit_zero_on_empty_stdin(home, project):
    config_dir = home / ".claude"
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text="")
    assert result.returncode == 0


def test_print_flag_emits_json_without_writing(tmp_path, home, project):
    config_dir = home / ".claude"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(
        config_dir=config_dir,
        cwd=project,
        stdin_text=stdin,
        extra_args=["--print"],
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == 1
    assert not (config_dir / "token-lens" / "snapshots").exists()


def test_min_interval_skips_identical_content(home, project):
    config_dir = home / ".claude"
    stdin = json.dumps({"session_id": "s1", "cwd": str(project)})

    first = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert first.returncode == 0
    snapshots_dir = config_dir / "token-lens" / "snapshots"
    assert len(list(snapshots_dir.glob("*.json"))) == 1

    # A different session_id (which is excluded from content_hash) run
    # immediately after, with the default 300s min-interval, must not
    # write a second file since the config itself hasn't changed.
    stdin2 = json.dumps({"session_id": "s2", "cwd": str(project)})
    second = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin2)
    assert second.returncode == 0
    assert len(list(snapshots_dir.glob("*.json"))) == 1


def test_content_change_writes_a_second_snapshot(home, project):
    config_dir = home / ".claude"
    stdin = json.dumps({"session_id": "s1", "cwd": str(project)})

    first = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert first.returncode == 0
    snapshots_dir = config_dir / "token-lens" / "snapshots"
    assert len(list(snapshots_dir.glob("*.json"))) == 1

    # Change the underlying config, then bypass the min-interval wait with
    # --min-interval 0 to prove a genuinely different config always writes
    # (the interval alone must never suppress a real content change).
    settings_path = config_dir / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["effortLevel"] = "low"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    # The snapshot filename is second-precision; make sure the two writes
    # land in different files even if the two subprocess calls happen
    # within the same wall-clock second.
    time.sleep(1.1)
    second = _run_hook(
        config_dir=config_dir,
        cwd=project,
        stdin_text=stdin,
        extra_args=["--min-interval", "0"],
    )
    assert second.returncode == 0
    files = sorted(snapshots_dir.glob("*.json"))
    assert len(files) == 2

    latest = json.loads(files[-1].read_text(encoding="utf-8"))
    assert latest["user_settings"]["effortLevel"] == "low"


def test_managed_settings_captured_and_redacted_with_keys_recorded(tmp_path, home, project):
    config_dir = home / ".claude"
    managed_path = tmp_path / "managed-settings.json"
    managed_settings = {
        "model": "sonnet",
        "permissions": {"deny": ["Bash(curl *)"]},
    }
    managed_path.write_text(json.dumps(managed_settings), encoding="utf-8")

    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(
        config_dir=config_dir,
        cwd=project,
        stdin_text=stdin,
        extra_args=["--managed-path", str(managed_path)],
    )
    assert result.returncode == 0

    snapshot = _latest_snapshot(config_dir)
    # Allowlisted scalar kept verbatim, same redaction rule as user_settings.
    assert snapshot["managed_settings"]["model"] == "sonnet"
    # Non-allowlisted dict reduced to a shape marker -- the value never leaks.
    assert snapshot["managed_settings"]["permissions"] == "dict(1)"
    # Key *names* (not values) are recorded so a report can say "managed by
    # policy" for any recommendation whose lever is one of these keys.
    assert snapshot["managed_keys"] == ["model", "permissions"]
    assert "Bash(curl" not in json.dumps(snapshot)


def test_managed_settings_absent_file_degrades_to_empty(tmp_path, home, project):
    config_dir = home / ".claude"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    # Point --managed-path at a file that doesn't exist -- must never fail
    # the hook, and must degrade to an empty dict/list.
    missing_path = tmp_path / "does-not-exist-managed-settings.json"
    result = _run_hook(
        config_dir=config_dir,
        cwd=project,
        stdin_text=stdin,
        extra_args=["--managed-path", str(missing_path)],
    )
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    assert snapshot["managed_settings"] == {}
    assert snapshot["managed_keys"] == []


def test_managed_settings_default_platform_path_used_when_no_override(home, project):
    # Without --managed-path, the hook falls back to the platform default
    # (default_managed_settings_path()); on a machine with no such file it
    # must still degrade cleanly rather than erroring.
    config_dir = home / ".claude"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    assert "managed_settings" in snapshot
    assert "managed_keys" in snapshot


@pytest.mark.skipif(sys.platform != "win32", reason="exercises the Windows default path branch")
def test_managed_settings_default_windows_path_honours_programdata_env(tmp_path, home, project):
    # No --managed-path override: point the well-known ProgramData env var
    # at a throwaway directory and prove default_managed_settings_path()'s
    # Windows branch (<ProgramData>/ClaudeCode/managed-settings.json) is
    # what actually gets read, not a hardcoded literal path.
    fake_program_data = tmp_path / "fake-programdata"
    managed_dir = fake_program_data / "ClaudeCode"
    managed_dir.mkdir(parents=True)
    (managed_dir / "managed-settings.json").write_text(
        json.dumps({"effortLevel": "high"}), encoding="utf-8"
    )

    config_dir = home / ".claude"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(
        config_dir=config_dir,
        cwd=project,
        stdin_text=stdin,
        extra_env={"ProgramData": str(fake_program_data)},
    )
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    assert snapshot["managed_settings"]["effortLevel"] == "high"
    assert snapshot["managed_keys"] == ["effortLevel"]


def test_min_interval_zero_always_writes_even_with_identical_content(home, project):
    config_dir = home / ".claude"
    stdin = json.dumps({"session_id": "s1", "cwd": str(project)})

    first = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert first.returncode == 0
    # Ensure the second run's timestamp-based filename cannot collide with
    # the first if the clock hasn't ticked a whole second yet.
    time.sleep(1.1)
    second = _run_hook(
        config_dir=config_dir,
        cwd=project,
        stdin_text=stdin,
        extra_args=["--min-interval", "0"],
    )
    assert second.returncode == 0
    snapshots_dir = config_dir / "token-lens" / "snapshots"
    assert len(list(snapshots_dir.glob("*.json"))) == 2
