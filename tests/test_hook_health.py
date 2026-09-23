"""Tests for ``src/claude_token_lens/hook_health.py``: finding the
SessionStart snapshot hook in ``settings.json``, spotting a Windows path
broken by JSON escaping, and repairing only that command after a backup
(also through ``init --repair-hook``)."""

from __future__ import annotations

import io
import json
import os
import sys
from datetime import datetime, timezone

import pytest

from claude_token_lens import helptext, hook_health, onboarding
from claude_token_lens.model import Diagnostics

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)

windows_only = pytest.mark.skipif(os.name != "nt", reason="the escaping bug needs Windows backslash paths")


@pytest.fixture(autouse=True)
def _claude_folder(tmp_path, monkeypatch):
    """Claude Code's folder is ``$CLAUDE_CONFIG_DIR`` (``discovery.claude_root``),
    never worked out from the data folder: point it at the ``claude``
    folder these tests build ``settings.json`` in."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))


def _claude_dir(tmp_path, command=None, *, script=True):
    """``<tmp>/claude`` with ``token-lens/`` as the config dir and a
    ``settings.json`` whose one SessionStart hook runs ``command``
    (``None`` means a good command pointing at the real script)."""
    claude = tmp_path / "claude"
    config_dir = claude / "token-lens"
    script_path = config_dir / "hooks" / "snapshot-config.py"
    script_path.parent.mkdir(parents=True)
    if script:
        script_path.write_text("# hook\n", encoding="utf-8")
    good = f'"{sys.executable}" "{script_path}"'
    settings = {
        "model": "opus",
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command or good}]}]},
    }
    (claude / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return config_dir, good


def _broken(good: str) -> str:
    # Backslash-t decodes to a tab, so "\token-lens" becomes TAB + "oken-lens".
    return good.replace("\\token-lens", "\token-lens")


def _write_snapshot(config_dir, ts: str) -> None:
    snaps = config_dir / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    (snaps / "s.json").write_text(json.dumps({"ts": ts, "effective": {}}), encoding="utf-8")


def test_an_apply_stamp_is_not_a_hook_snapshot(tmp_path):
    # apply marks the active profile with a config-free stamp; it says
    # nothing about whether the hook still runs.
    config_dir, _good = _claude_dir(tmp_path)
    _write_snapshot(config_dir, "2026-09-10T12:00:00Z")
    (config_dir / "snapshots" / "stamp.json").write_text(
        json.dumps({"ts": "2026-09-22T11:00:00Z", "schema_version": 2, "profile_id": "lean"}), encoding="utf-8"
    )
    assert hook_health.check(config_dir, now=NOW).last_snapshot_days == pytest.approx(12.0)


def test_good_hook_is_ok(tmp_path):
    config_dir, good = _claude_dir(tmp_path)
    _write_snapshot(config_dir, "2026-09-20T12:00:00Z")
    health = hook_health.check(config_dir, now=NOW)
    assert health.ok
    assert health.command == good
    assert health.fixed_command is None
    assert health.last_snapshot_days == pytest.approx(2.0)
    assert health.summary() == "Last config snapshot: 2 days ago. The SessionStart hook is set up."


@windows_only
def test_mis_escaped_path_is_found_and_a_fix_worked_out(tmp_path):
    config_dir, good = _claude_dir(tmp_path)
    broken = _broken(good)
    settings_path = config_dir.parent / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = broken
    settings_path.write_text(json.dumps(data), encoding="utf-8")

    health = hook_health.check(config_dir, now=NOW)
    assert not health.ok
    assert health.mis_escaped
    assert not health.script_exists
    assert health.fixed_command == good
    assert "init --repair-hook" in health.summary()
    assert health.summary().startswith("No automatic config snapshot has been taken yet.")


def test_missing_script_has_no_fix(tmp_path):
    config_dir, _ = _claude_dir(tmp_path, script=False)
    health = hook_health.check(config_dir, now=NOW)
    assert not health.ok
    assert not health.mis_escaped
    assert health.fixed_command is None
    assert "does not exist" in health.summary()


def test_no_hook_and_no_settings(tmp_path):
    config_dir = tmp_path / "claude" / "token-lens"
    config_dir.mkdir(parents=True)
    health = hook_health.check(config_dir, now=NOW)
    assert health.command is None
    assert not health.ok
    assert "No SessionStart hook runs snapshot-config.py" in health.summary()

    (config_dir.parent / "settings.json").write_text("{not json", encoding="utf-8")
    assert hook_health.check(config_dir, now=NOW).command is None


@windows_only
def test_repair_backs_up_and_changes_only_the_command(tmp_path):
    config_dir, good = _claude_dir(tmp_path)
    settings_path = config_dir.parent / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = _broken(good)
    before = json.dumps(data)
    settings_path.write_text(before, encoding="utf-8")

    backup = hook_health.repair(hook_health.check(config_dir, now=NOW), now=NOW)

    assert backup.name == "settings.json.bak-20260922T120000Z"
    assert backup.read_text(encoding="utf-8") == before
    after = json.loads(settings_path.read_text(encoding="utf-8"))
    assert after["hooks"]["SessionStart"][0]["hooks"][0]["command"] == good
    assert after["model"] == "opus"
    assert hook_health.check(config_dir, now=NOW).ok


def test_repair_refuses_when_there_is_nothing_to_fix(tmp_path):
    config_dir, _ = _claude_dir(tmp_path)
    with pytest.raises(ValueError):
        hook_health.repair(hook_health.check(config_dir, now=NOW), now=NOW)


def test_diagnostics_table_leads_with_the_hook_row(tmp_path):
    config_dir, _ = _claude_dir(tmp_path)
    table = helptext.diagnostics_table(Diagnostics(), hook=hook_health.check(config_dir, now=NOW))
    assert table.rows[0][0] == "snapshot_hook"
    assert table.rows[0][1] == "working"
    assert table.value_labels["snapshot_hook"] == "Config snapshot hook"


def _run_init(config_dir, tmp_path, *, repair_hook=False, non_interactive=True, answer=""):
    stdout = io.StringIO()
    rc = onboarding.run_init(
        config_dir=config_dir,
        projects_root_path=tmp_path / "projects",
        non_interactive=non_interactive,
        no_install=True,
        hook_fragment="HOOK",
        statusline_fragment="STATUSLINE",
        stdin=io.StringIO(answer),
        stdout=stdout,
        now=NOW,
        repair_hook=repair_hook,
    )
    assert rc == 0
    return stdout.getvalue()


@windows_only
def test_init_reports_but_does_not_repair_without_the_flag(tmp_path):
    config_dir, good = _claude_dir(tmp_path)
    settings_path = config_dir.parent / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = _broken(good)
    settings_path.write_text(json.dumps(data), encoding="utf-8")

    out = _run_init(config_dir, tmp_path)
    assert "- config snapshot hook:" in out
    assert "init --repair-hook" in out
    assert not list(settings_path.parent.glob("settings.json.bak-*"))


@windows_only
def test_init_repair_hook_fixes_it(tmp_path):
    config_dir, good = _claude_dir(tmp_path)
    settings_path = config_dir.parent / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = _broken(good)
    settings_path.write_text(json.dumps(data), encoding="utf-8")

    out = _run_init(config_dir, tmp_path, repair_hook=True)
    assert "Fixed. The previous settings.json is at" in out
    assert json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]["hooks"][0]["command"] == good


def test_missing_interpreter_is_found_and_fixed_with_full_paths(tmp_path):
    # "py -3" with no Python launcher installed: the script exists, yet
    # the hook never runs.
    config_dir, _ = _claude_dir(tmp_path)
    script = config_dir / "hooks" / "snapshot-config.py"
    settings_path = config_dir.parent / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = f'no-such-python-launcher -3 "{script}"'
    settings_path.write_text(json.dumps(data), encoding="utf-8")

    health = hook_health.check(config_dir, now=NOW, python="/usr/bin/python3")
    assert health.script_exists
    assert not health.interpreter_found
    assert not health.ok
    assert "not installed or not on your PATH" in health.summary()
    assert health.fixed_command == f'"/usr/bin/python3" "{script.resolve()}"'


def test_percent_variable_is_written_out_keeping_the_users_interpreter(tmp_path, monkeypatch):
    # Git Bash, which Claude Code uses on Windows, passes %VAR% through
    # unexpanded.
    config_dir, _ = _claude_dir(tmp_path)
    monkeypatch.setenv("TL_TEST_ROOT", str(config_dir))
    settings_path = config_dir.parent / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = (
        f'"{sys.executable}" "%TL_TEST_ROOT%{os.sep}hooks{os.sep}snapshot-config.py"'
    )
    settings_path.write_text(json.dumps(data), encoding="utf-8")

    health = hook_health.check(config_dir, now=NOW)
    assert health.percent_vars
    assert not health.ok
    assert "%VARIABLE%" in health.summary()
    script = config_dir / "hooks" / "snapshot-config.py"
    assert health.fixed_command == f'"{sys.executable}" "{script}"'
    hook_health.repair(health, now=NOW)
    assert hook_health.check(config_dir, now=NOW).ok


def test_percent_variable_with_a_missing_interpreter_names_a_python_by_full_path(tmp_path, monkeypatch):
    config_dir, _ = _claude_dir(tmp_path)
    monkeypatch.setenv("TL_TEST_ROOT", str(config_dir))
    settings_path = config_dir.parent / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = (
        f'no-such-python-launcher -3 "%TL_TEST_ROOT%{os.sep}hooks{os.sep}snapshot-config.py"'
    )
    settings_path.write_text(json.dumps(data), encoding="utf-8")

    health = hook_health.check(config_dir, now=NOW)
    script = (config_dir / "hooks" / "snapshot-config.py").resolve()
    assert health.fixed_command == f'"{hook_health.stable_python()}" "{script}"'


def test_a_hook_command_prefers_the_base_interpreter_over_a_venv(monkeypatch, tmp_path):
    base = tmp_path / "python.exe"
    base.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "_base_executable", str(base), raising=False)
    assert hook_health.stable_python() == str(base)
    monkeypatch.setattr(sys, "_base_executable", str(tmp_path / "gone.exe"), raising=False)
    assert hook_health.stable_python() == sys.executable


def _with_statusline(tmp_path, rows=()):
    config_dir, _ = _claude_dir(tmp_path)
    settings_path = config_dir.parent / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["statusLine"] = {"type": "command", "command": "python -m claude_token_lens.statusline"}
    settings_path.write_text(json.dumps(data), encoding="utf-8")
    lines = ["logged_at,session_id,window,used_percentage,resets_at,source"] + list(rows)
    (config_dir / "usage-log.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_dir


def test_statusline_check_explains_desktop_only_use(tmp_path):
    config_dir = _with_statusline(tmp_path)
    working, sentence = hook_health.statusline_check(config_dir, {"claude-desktop": {"count": 5, "last_ts": "2026-09-22"}})
    assert not working
    assert "All 5 of your sessions ran outside a terminal" in sentence


def test_statusline_check_working_and_stale(tmp_path):
    config_dir = _with_statusline(tmp_path, ["2026-09-20T10:00:00Z,s1,five_hour,12,,statusline"])
    working, sentence = hook_health.statusline_check(config_dir, {"cli": {"count": 2, "last_ts": "2026-09-20T09:00:00Z"}})
    assert working and "2026-09-20 10:00" in sentence
    working, sentence = hook_health.statusline_check(config_dir, {"cli": {"count": 2, "last_ts": "2026-09-22T09:00:00Z"}})
    assert not working and "terminal sessions ran later" in sentence


def test_statusline_check_not_set_up(tmp_path):
    config_dir, _ = _claude_dir(tmp_path)
    working, sentence = hook_health.statusline_check(config_dir, {})
    assert not working and "init --connect" in sentence


def test_repair_keeps_arguments_after_the_script(tmp_path, monkeypatch):
    # A hook command for a non-default data folder carries --config-dir;
    # rebuilding it around a working Python must keep that.
    claude = tmp_path / "claude"
    config_dir = claude / "token-lens"
    script = config_dir / "hooks" / "snapshot-config.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    command = f'nosuchpython-xyz "{script}" --config-dir "{config_dir}"'
    settings = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}
    (claude / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    health = hook_health.check(config_dir, now=NOW, python=sys.executable)
    assert not health.interpreter_found
    assert health.fixed_command == f'"{sys.executable}" "{script.resolve()}" --config-dir "{config_dir}"'
