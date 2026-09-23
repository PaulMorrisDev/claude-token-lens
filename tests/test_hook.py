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

from helpers import assert_privacy_deep

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
    env["HOME"] = str(config_dir.parent.parent)
    env["USERPROFILE"] = str(config_dir.parent.parent)
    # Claude Code's folder (settings.json, agents/) comes from
    # CLAUDE_CONFIG_DIR, never from --config-dir's parent.
    env["CLAUDE_CONFIG_DIR"] = str(config_dir.parent)
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
    snapshots_dir = config_dir / "snapshots"
    files = sorted(snapshots_dir.glob("*.json"))
    assert files, f"no snapshot written under {snapshots_dir}"
    return json.loads(files[-1].read_text(encoding="utf-8"))


@pytest.fixture()
def home(tmp_path):
    return _build_home(tmp_path)


@pytest.fixture()
def project(tmp_path):
    return _build_project(tmp_path)


def test_assert_privacy_is_blind_to_a_nested_dict_leak_but_deep_variant_catches_it():
    """Regression test for review fix #3: ``assert_privacy`` recurses into
    a dataclass/list/tuple but deliberately stops at a ``dict`` boundary,
    so a leak nested two dicts deep -- exactly the shape a schema-2
    snapshot's ``settings_layers``/``effective``/``claude_json``/
    ``content_layers`` fields are -- passes silently. ``assert_privacy_deep``
    must catch the same leak. This fails before the fix (no
    ``assert_privacy_deep`` existed) and passes after it."""
    from helpers import assert_privacy

    leaking = {"settings_layers": {"user": {"leak": r"C:\Users\alice\secret.txt"}}}

    # The shallow scan is blind to it -- this is the bug fix #3 reports,
    # pinned here so nobody "fixes" assert_privacy itself into breaking it.
    assert_privacy(leaking)

    # The deep scan must not be.
    with pytest.raises(AssertionError):
        assert_privacy_deep(leaking)


def test_hook_writes_redacted_snapshot(tmp_path, home, project):
    config_dir = home / ".claude" / "token-lens"
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
    # statusLine gets its own summary shape (schema 2): present/absent only,
    # never the command it runs -- see redact_settings_value's special case.
    assert user_settings["statusLine"] is True

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
    # Fix #1: the raw absolute transcript path must never be written --
    # only a hash, matching cwd_hash's own shape. The raw value passed on
    # stdin must not appear anywhere in the file.
    assert "transcript_path" not in snapshot
    assert snapshot["transcript_path_hash"].startswith("sha256:")
    assert "/x/transcript.jsonl" not in json.dumps(snapshot)
    assert snapshot["source"] == "startup"
    assert snapshot["cwd_hash"].startswith("sha256:")
    assert snapshot["content_hash"].startswith("sha256:")

    # Fix #3: assert_privacy's dataclass-field scan never sees this
    # dict-of-dicts at all -- assert_privacy_deep walks every key and
    # value in it, including the raw fixture paths (project/home live
    # under a realistic C:\Users\...\AppData\Local\Temp\... root here,
    # not a fake "/x/..." string a privacy scan would never trip on).
    assert_privacy_deep(snapshot)


def test_hook_flattens_nested_agent_frontmatter(home, project):
    config_dir = home / ".claude" / "token-lens"
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
    config_dir = home / ".claude" / "token-lens"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0

    snapshot_path = sorted((config_dir / "snapshots").glob("*.json"))[-1]
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
    config_dir = home / ".claude" / "token-lens"
    result = _run_hook(
        config_dir=config_dir, cwd=project, stdin_text="{not valid json!!!"
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_exit_zero_on_empty_stdin(home, project):
    config_dir = home / ".claude" / "token-lens"
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text="")
    assert result.returncode == 0


def test_print_flag_emits_json_without_writing(tmp_path, home, project):
    config_dir = home / ".claude" / "token-lens"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(
        config_dir=config_dir,
        cwd=project,
        stdin_text=stdin,
        extra_args=["--print"],
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == 2
    assert not (config_dir / "snapshots").exists()


def test_min_interval_skips_identical_content(home, project):
    config_dir = home / ".claude" / "token-lens"
    stdin = json.dumps({"session_id": "s1", "cwd": str(project)})

    first = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert first.returncode == 0
    snapshots_dir = config_dir / "snapshots"
    assert len(list(snapshots_dir.glob("*.json"))) == 1

    # A different session_id (which is excluded from content_hash) run
    # immediately after, with the default 300s min-interval, must not
    # write a second file since the config itself hasn't changed.
    stdin2 = json.dumps({"session_id": "s2", "cwd": str(project)})
    second = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin2)
    assert second.returncode == 0
    assert len(list(snapshots_dir.glob("*.json"))) == 1


def test_content_change_writes_a_second_snapshot(home, project):
    config_dir = home / ".claude" / "token-lens"
    stdin = json.dumps({"session_id": "s1", "cwd": str(project)})

    first = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert first.returncode == 0
    snapshots_dir = config_dir / "snapshots"
    assert len(list(snapshots_dir.glob("*.json"))) == 1

    # Change the underlying config, then bypass the min-interval wait with
    # --min-interval 0 to prove a genuinely different config always writes
    # (the interval alone must never suppress a real content change).
    settings_path = config_dir.parent / "settings.json"
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
    config_dir = home / ".claude" / "token-lens"
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
    config_dir = home / ".claude" / "token-lens"
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
    config_dir = home / ".claude" / "token-lens"
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

    config_dir = home / ".claude" / "token-lens"
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



# -- schema 2: widened settings allowlist (coordinator addition) ------------


def test_autocompact_enabled_and_model_pricing_kept_safe(home, project):
    """autoCompactEnabled is a plain safe-allowlist boolean; modelPricing
    reduces to a present flag plus the model ids it overrides -- never the
    overridden numbers themselves (the whole point of pricing.toml staying
    user-editable and out of the tool's own reporting)."""
    config_dir = home / ".claude" / "token-lens"
    settings_path = home / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["autoCompactEnabled"] = False
    settings["modelPricing"] = {
        "claude-sonnet-5": {"input": 3.0, "output": 15.0},
        "claude-fable-5.1": {"input": 9.99, "output": 42.0},
    }
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0

    snapshot = _latest_snapshot(config_dir)
    raw_text = json.dumps(snapshot)
    user_settings = snapshot["user_settings"]
    assert user_settings["autoCompactEnabled"] is False
    assert user_settings["modelPricing"] == {
        "present": True,
        "model_ids": ["claude-fable-5.1", "claude-sonnet-5"],
    }
    # The overridden numbers themselves must never appear anywhere.
    assert "3.0" not in raw_text
    assert "15.0" not in raw_text
    assert "9.99" not in raw_text
    assert "42.0" not in raw_text


def test_model_pricing_absent_reduces_to_not_present():
    import importlib.util

    spec = importlib.util.spec_from_file_location("snapshot_config_hook", _HOOK_PATH)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    assert hook._redact_model_pricing(None) == {"present": False, "model_ids": []}
    assert hook._redact_model_pricing({}) == {"present": False, "model_ids": []}


def test_desktop_session_cleanup_period_days_kept_verbatim(home, project):
    config_dir = home / ".claude" / "token-lens"
    settings_path = home / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["desktopSessionCleanupPeriodDays"] = 45
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    assert snapshot["user_settings"]["desktopSessionCleanupPeriodDays"] == 45


# -- schema 2: widened env-name allowlist + numeric caps (coordinator) ------


def test_widened_env_names_are_captured_by_name_only(home, project):
    config_dir = home / ".claude" / "token-lens"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(
        config_dir=config_dir,
        cwd=project,
        stdin_text=stdin,
        extra_env={
            "CLAUDE_CODE_SUBAGENT_MODEL": "haiku",
            "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-x",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-x",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-x",
            "ANTHROPIC_DEFAULT_FABLE_MODEL": "claude-fable-x",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "OTEL_SERVICE_NAME": "claude-code",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "DISABLE_NON_ESSENTIAL_MODEL_CALLS": "1",
        },
    )
    assert result.returncode == 0
    snapshot_path = sorted((config_dir / "snapshots").glob("*.json"))[-1]
    raw_text = snapshot_path.read_text(encoding="utf-8")
    snapshot = json.loads(raw_text)

    for name in (
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "OTEL_SERVICE_NAME",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "DISABLE_NON_ESSENTIAL_MODEL_CALLS",
    ):
        assert name in snapshot["env_names"], name

    # Values (model ids, endpoint URL) must never appear in the written file.
    for leaked_value in ("claude-opus-x", "claude-sonnet-x", "claude-haiku-x", "claude-fable-x", "localhost:4317"):
        assert leaked_value not in raw_text


def test_env_numeric_caps_record_the_integer_value(home, project):
    config_dir = home / ".claude" / "token-lens"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(
        config_dir=config_dir,
        cwd=project,
        stdin_text=stdin,
        extra_env={
            "MAX_THINKING_TOKENS": "50000",
            "MAX_MCP_OUTPUT_TOKENS": "100000",
            "BASH_MAX_OUTPUT_LENGTH": "20000",
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80",
        },
    )
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    assert snapshot["env_numeric_caps"] == {
        "MAX_THINKING_TOKENS": 50000,
        "MAX_MCP_OUTPUT_TOKENS": 100000,
        "BASH_MAX_OUTPUT_LENGTH": 20000,
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": 80,
    }
    # The names are also still recorded in the general env-names list.
    for name in snapshot["env_numeric_caps"]:
        assert name in snapshot["env_names"]


def test_env_numeric_cap_non_numeric_value_is_skipped(home, project):
    config_dir = home / ".claude" / "token-lens"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(
        config_dir=config_dir,
        cwd=project,
        stdin_text=stdin,
        extra_env={"MAX_THINKING_TOKENS": "not-a-number"},
    )
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    assert "MAX_THINKING_TOKENS" not in snapshot["env_numeric_caps"]
    # Still recorded by name even though the value couldn't be parsed.
    assert "MAX_THINKING_TOKENS" in snapshot["env_names"]


def test_env_numeric_caps_absent_when_unset(home, project):
    config_dir = home / ".claude" / "token-lens"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    assert snapshot["env_numeric_caps"] == {}


# -- schema 2: snapshot filename collision-proofing --------------------------


def test_snapshot_filenames_are_collision_proof_within_the_same_second(
    tmp_path, home, monkeypatch
):
    """Regression test for review fix #11: ``_TS_FORMAT`` has one-second
    resolution, so two snapshots for different projects (different
    content) that land in the same wall-clock second used to collide on
    ``<ts>.json`` and the later, non-atomic write silently destroyed the
    earlier one -- eight writes could produce a single file on disk. Force
    both writes into the exact same forged ``ts`` (rather than relying on
    real-clock timing, which is flaky) and confirm both survive as two
    distinct, individually-parseable files. This fails before the fix
    (``len(files) == 1``, the second project's write clobbering the
    first's) and passes after it.
    """
    hook = _load_hook_module()
    config_dir = home / ".claude" / "token-lens"

    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()

    monkeypatch.setattr(hook, "_now_ts", lambda now=None: "20260919T120000Z")

    path_a, written_a = hook.snapshot_and_get_path(config_dir, str(project_a), min_interval=0)
    path_b, written_b = hook.snapshot_and_get_path(config_dir, str(project_b), min_interval=0)

    assert written_a is True
    assert written_b is True
    assert path_a != path_b
    assert path_a.exists()
    assert path_b.exists()

    files = sorted((config_dir / "snapshots").glob("*.json"))
    assert len(files) == 2, f"expected 2 distinct snapshot files, got {[f.name for f in files]}"

    for f in files:
        assert f.name.startswith("20260919T120000Z-")
        # Each file must be a complete, valid JSON document -- proves the
        # write is atomic (temp file + os.replace), not merely
        # distinctly named.
        data = json.loads(f.read_text(encoding="utf-8"))
        assert "project_slug" in data


# -- schema 2: project slug --------------------------------------------------


def _load_hook_module():
    """Dynamically import the standalone hook script (see
    ``test_model_pricing_absent_reduces_to_not_present`` for the same
    pattern) so a test can call its redaction helpers directly to build
    an expected value, without duplicating their algorithm."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("snapshot_config_hook", _HOOK_PATH)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    return hook


def test_project_slug_is_redacted_hash_not_the_raw_cwd(home, project):
    """Fix #2: ``project_slug`` used to be the absolute cwd with
    punctuation swapped for "-" (e.g. ``C--Users-alice-work-acme-client``)
    -- the username and full directory structure, stored verbatim and
    later rendered in report tables / probe-config Markdown. The stored
    value must now be an opaque ``slug:<hash>`` join key that reveals
    neither the path nor its structure.
    """
    hook = _load_hook_module()
    config_dir = home / ".claude" / "token-lens"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    slug = snapshot["project_slug"]

    raw_slug = hook._project_slug(str(project))
    assert slug == hook._redact_slug(raw_slug)
    assert slug.startswith("slug:")
    # Never a raw path separator, a drive-letter colon, the raw
    # (unredacted) slug, or the cwd itself.
    assert "/" not in slug
    assert "\\" not in slug
    assert raw_slug not in slug
    assert str(project) not in slug


def test_project_slug_honours_project_dir_name_env_override(home, project):
    hook = _load_hook_module()
    config_dir = home / ".claude" / "token-lens"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(
        config_dir=config_dir,
        cwd=project,
        stdin_text=stdin,
        extra_env={"CLAUDE_CODE_PROJECT_DIR_NAME": "my-fixed-slug"},
    )
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    # The override still flows through the same redaction as any other
    # slug -- it is not a way to bypass fix #2 and get a raw slug stored.
    assert snapshot["project_slug"] == hook._redact_slug("my-fixed-slug")


# -- schema 2: settings layers / effective config / provenance --------------


def test_settings_layers_presence_and_precedence(tmp_path, home, project):
    config_dir = home / ".claude" / "token-lens"
    managed_path = tmp_path / "managed-settings.json"
    managed_path.write_text(json.dumps({"effortLevel": "low"}), encoding="utf-8")

    (project / ".claude" / "settings.local.json").write_text(
        json.dumps({"effortLevel": "high", "model": "opus"}), encoding="utf-8"
    )
    # project/.claude/settings.json (project_shared) already written by
    # _build_project() with {"model": "sonnet", ...}.

    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(
        config_dir=config_dir,
        cwd=project,
        stdin_text=stdin,
        extra_args=["--managed-path", str(managed_path)],
    )
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)

    layers = snapshot["settings_layers"]
    assert set(layers) == {"managed", "project_local", "project_shared", "user"}
    assert layers["managed"]["present"] is True
    assert layers["project_local"]["present"] is True
    assert layers["project_shared"]["present"] is True
    assert layers["user"]["present"] is True
    # Never a raw path -- only a hash.
    for layer in layers.values():
        assert layer["source_path_hash"].startswith("sha256:")

    # Precedence high to low: managed > project_local > project_shared > user.
    # "effortLevel" is set by managed (low) and project_local (high) ->
    # managed wins.
    assert snapshot["effective"]["effortLevel"] == "low"
    assert snapshot["effective_provenance"]["effortLevel"] == "managed"
    # "model" is set by project_local (opus), project_shared (sonnet) and
    # user (fable[1m]) but not managed -> project_local wins.
    assert snapshot["effective"]["model"] == "opus"
    assert snapshot["effective_provenance"]["model"] == "project_local"


def test_settings_layer_absent_when_file_missing(home, project):
    # project has no settings.local.json in the base fixture.
    config_dir = home / ".claude" / "token-lens"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    assert snapshot["settings_layers"]["project_local"]["present"] is False
    assert snapshot["settings_layers"]["project_local"]["content_hash"] is None


def test_settings_layer_permissions_and_hooks_are_counts_only(home, project):
    config_dir = home / ".claude" / "token-lens"
    settings_path = home / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["permissions"] = {
        "allow": ["Bash(git *)", "Read(**)"],
        "deny": ["Bash(curl *)"],
        "ask": [],
        "defaultMode": "acceptEdits",
    }
    settings["hooks"] = {
        "SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}],
        "PreToolUse": [{"hooks": [{"type": "command", "command": "a"}]}, {"hooks": [{"type": "command", "command": "b"}]}],
    }
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    raw_text = json.dumps(snapshot)

    user_layer = snapshot["settings_layers"]["user"]
    assert user_layer["permissions"] == {
        "allow_count": 2,
        "deny_count": 1,
        "ask_count": 0,
        "default_mode": "acceptEdits",
    }
    assert user_layer["hooks"] == {"SessionStart": 1, "PreToolUse": 2}
    assert "Bash(curl" not in raw_text
    assert "echo hi" not in raw_text


# -- schema 2: effective_agents -----------------------------------------------


def test_effective_agents_reduced_shape(home, project):
    config_dir = home / ".claude" / "token-lens"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)

    effective_agents = snapshot["effective_agents"]
    verifier = effective_agents["verification-runner"]
    assert verifier == {
        "source": "user",
        "experimental_cache_ttl": "1h",
        "model": "sonnet",
        "effort": "medium",
        "max_turns": 80,
    }
    implementer = effective_agents["claude-implementer"]
    assert implementer["experimental_cache_ttl"] is None


def test_project_agent_shadows_user_agent_of_the_same_name(home, project):
    config_dir = home / ".claude" / "token-lens"
    project_agents_dir = project / ".claude" / "agents"
    project_agents_dir.mkdir(parents=True)
    (project_agents_dir / "claude-implementer.md").write_text(
        """---
name: claude-implementer
description: A project-level override of the same agent name.
model: fable
effort: high
maxTurns: 40
---

Project override body.
""",
        encoding="utf-8",
    )

    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)

    agents = snapshot["agents"]
    implementer = agents["claude-implementer"]
    assert implementer["source"] == "project"
    assert implementer["shadowed_by_project"] is True
    assert implementer["model"] == "fable"

    verifier = agents["verification-runner"]
    assert verifier["source"] == "user"
    assert "shadowed_by_project" not in verifier

    content_layers = snapshot["content_layers"]
    assert content_layers["agents_summary"]["count"] == 2
    assert content_layers["agents_summary"]["user_count"] == 1
    assert content_layers["agents_summary"]["project_count"] == 1
    assert content_layers["agents_summary"]["shadowed_count"] == 1


# -- schema 2: ~/.claude.json cross-check ------------------------------------


def test_claude_json_matches_project_by_normcase_realpath(home, project):
    """~/.claude.json's own project keys are observed on real machines to
    hold the same directory under several spellings (forward slashes,
    backslashes, drive-letter case, a trailing separator) -- confirm the
    match survives a differently-spelled key, and that the raw matching
    key itself never appears in the snapshot.

    Drive-letter/backslash case only exists on Windows, and a case
    variant is only ever the *same* path there too: ``os.path.normcase``
    is the identity function on a case-sensitive filesystem (Linux/most
    POSIX), where ``FOO`` and ``foo`` are genuinely different paths, so
    only fold the key's case when normcase itself would fold it.
    """
    config_dir = home / ".claude" / "token-lens"
    weird_key = str(project).replace("\\", "/") + "/"
    if os.path.normcase("A") == os.path.normcase("a"):
        # normcase actually folds case here (Windows) -- exercise that too.
        weird_key = weird_key.upper()
    dot_claude_json = {
        "numStartups": 42,
        "autoUpdates": True,
        "projects": {
            weird_key: {
                "mcpServers": {"filesystem": {}},
                "enabledMcpjsonServers": ["filesystem"],
                "disabledMcpjsonServers": [],
                "allowedTools": ["Bash", "Read", "Edit"],
                "hasTrustDialogAccepted": True,
                "lastCost": 1.23,
                "lastDuration": 4567,
                "lastAPIDuration": 4000,
                "lastTotalInputTokens": 1000,
                "lastTotalOutputTokens": 200,
                "lastTotalCacheCreationInputTokens": 50,
                "lastTotalCacheReadInputTokens": 500,
                "lastSessionId": "11111111-1111-1111-1111-111111111111",
                "lastLinesAdded": 10,
                "lastLinesRemoved": 3,
            }
        },
    }
    (home / ".claude.json").write_text(json.dumps(dot_claude_json), encoding="utf-8")

    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    raw_text = json.dumps(snapshot)

    claude_json = snapshot["claude_json"]
    assert claude_json["matched"] is True
    assert claude_json["mcp_servers"] == ["filesystem"]
    assert claude_json["allowed_tools_count"] == 3
    assert claude_json["has_trust_dialog_accepted"] is True
    assert claude_json["last_session"]["lastCost"] == 1.23
    assert claude_json["last_session"]["lastSessionId"] == "11111111-1111-1111-1111-111111111111"
    assert claude_json["top_level"]["num_projects"] == 1
    assert claude_json["top_level"]["scalars"]["numStartups"] == 42
    assert claude_json["top_level"]["scalars"]["autoUpdates"] is True

    # The raw project-key spelling (a path) must never be recorded verbatim.
    assert weird_key not in raw_text

    # Fix #3: deep-scan the whole snapshot, not just the fields this test
    # already names -- catches a leak anywhere in claude_json's nested
    # dicts (top_level, last_session, ...), which assert_privacy cannot
    # reach through a dataclass field.
    assert_privacy_deep(snapshot)


def test_claude_json_no_matching_project_entry(home, project):
    config_dir = home / ".claude" / "token-lens"
    (home / ".claude.json").write_text(
        json.dumps({"projects": {"/some/other/project": {}}}), encoding="utf-8"
    )
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    assert snapshot["claude_json"]["matched"] is False


def test_claude_json_missing_file_degrades_cleanly(home, project):
    config_dir = home / ".claude" / "token-lens"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    assert snapshot["claude_json"] == {"matched": False}


def test_claude_json_corrupt_file_never_raises(home, project):
    config_dir = home / ".claude" / "token-lens"
    (home / ".claude.json").write_text("{not valid json!!!", encoding="utf-8")
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    assert snapshot["claude_json"] == {"matched": False}


# -- schema 2: content layers -------------------------------------------------


def test_content_layers_claude_md_rules_commands_and_skills(home, project):
    config_dir = home / ".claude" / "token-lens"

    (home / ".claude" / "CLAUDE.md").write_text("user memory " * 5, encoding="utf-8")
    (project / "CLAUDE.md").write_text("project root memory " * 3, encoding="utf-8")
    (project / "CLAUDE.local.md").write_text("local only", encoding="utf-8")

    nested_dir = project / "sub" / "deeper"
    nested_dir.mkdir(parents=True)
    (nested_dir / "CLAUDE.md").write_text("nested memory", encoding="utf-8")
    # A directory the walk must skip.
    skipped_dir = project / "node_modules" / "pkg"
    skipped_dir.mkdir(parents=True)
    (skipped_dir / "CLAUDE.md").write_text("must not be counted", encoding="utf-8")

    rules_dir = project / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "one.md").write_text("rule one", encoding="utf-8")
    (rules_dir / "two.md").write_text("rule two", encoding="utf-8")

    commands_dir = project / ".claude" / "commands" / "nested"
    commands_dir.mkdir(parents=True)
    (commands_dir / "cmd.md").write_text("command body", encoding="utf-8")

    project_skill_dir = project / ".claude" / "skills" / "my-skill"
    project_skill_dir.mkdir(parents=True)
    (project_skill_dir / "SKILL.md").write_text("skill body", encoding="utf-8")

    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    content = snapshot["content_layers"]
    raw_text = json.dumps(snapshot)

    claude_md = content["claude_md"]
    assert claude_md["user_bytes"] == len("user memory " * 5)
    assert claude_md["project_root_bytes"] == len("project root memory " * 3)
    assert claude_md["project_local_bytes"] == len("local only")
    assert claude_md["nested_count"] == 1
    assert claude_md["nested_bytes"] == len("nested memory")

    assert content["rules"] == {"count": 2, "bytes": len("rule one") + len("rule two")}
    assert content["commands"] == {"count": 1, "bytes": len("command body")}
    assert content["skills"]["project"]["names"] == ["my-skill"]
    assert content["skills"]["project"]["total_bytes"] == len("skill body")

    # Fix #3: deep-scan the whole snapshot -- content_layers is itself a
    # dict of dicts (claude_md, rules, commands, skills), the exact shape
    # assert_privacy cannot see into.
    assert_privacy_deep(snapshot)

    # Never raw content, only sizes/counts/names.
    assert "project root memory" not in raw_text
    assert "rule one" not in raw_text
    assert "command body" not in raw_text
    assert "skill body" not in raw_text
    assert "must not be counted" not in raw_text


def test_content_layers_mcp_json_and_claude_config_dir_flag(home, project):
    config_dir = home / ".claude" / "token-lens"
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"stripe": {}, "sentry": {}}}), encoding="utf-8"
    )

    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(
        config_dir=config_dir,
        cwd=project,
        stdin_text=stdin,
        extra_env={"CLAUDE_CONFIG_DIR": str(home / ".claude")},
    )
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    content = snapshot["content_layers"]
    assert content["mcp_json"] == {"present": True, "names": ["sentry", "stripe"]}
    assert content["claude_config_dir_set"] is True


def test_content_layers_absent_content_degrades_to_zero_counts(home, project):
    config_dir = home / ".claude" / "token-lens"
    stdin = json.dumps({"session_id": "s", "cwd": str(project)})
    result = _run_hook(config_dir=config_dir, cwd=project, stdin_text=stdin)
    assert result.returncode == 0
    snapshot = _latest_snapshot(config_dir)
    content = snapshot["content_layers"]
    assert content["rules"] == {"count": 0, "bytes": 0}
    assert content["commands"] == {"count": 0, "bytes": 0}
    assert content["mcp_json"] == {"present": False, "names": []}
    assert content["managed_mcp_present"] is False
    assert content["memory"] == {"present": False, "files": 0, "bytes": 0}


def test_min_interval_zero_always_writes_even_with_identical_content(home, project):
    config_dir = home / ".claude" / "token-lens"
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
    snapshots_dir = config_dir / "snapshots"
    assert len(list(snapshots_dir.glob("*.json"))) == 2


def test_snapshot_project_key_matches_the_hooks_stored_slug():
    import importlib.util

    from claude_token_lens import snapshots as snap_mod

    spec = importlib.util.spec_from_file_location("snapshot_config_hook", _HOOK_PATH)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    raw = hook._project_slug("/home/alice/my-project")
    assert snap_mod.snapshot_project_key(raw) == hook._redact_slug(raw)


def test_hook_with_config_dir_elsewhere_reads_claude_settings_not_its_parent(tmp_path, home, project):
    # init adds --config-dir to the hook command when the data folder is
    # not <claude folder>/token-lens; settings.json and agents/ still come
    # from Claude Code's own folder ($CLAUDE_CONFIG_DIR), not the parent.
    data = tmp_path / "elsewhere" / "tl-data"
    data.mkdir(parents=True)
    (data.parent / "settings.json").write_text(json.dumps({"model": "decoy"}), encoding="utf-8")
    stdin = json.dumps({"session_id": "sess-9", "cwd": str(project), "source": "startup"})
    env = {"HOME": str(home), "USERPROFILE": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude")}
    result = _run_hook(config_dir=data, cwd=project, stdin_text=stdin, extra_env=env)
    assert result.returncode == 0
    snapshot = _latest_snapshot(data)
    assert snapshot["user_settings"]["model"] == "fable[1m]"
