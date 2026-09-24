"""Switching metrics capture on and off: the ``capture`` command, the
settings.json entries it syncs (``hook_health.plan_capture``,
``check_capture``), and how ``changes`` and ``uninstall`` see them
(``footprint``). Every settings.json change is shown first, asked about,
and backed up; ``--dry-run`` writes nothing.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
import pytest

from claude_token_lens import capture_catalogue as cat
from claude_token_lens import cli, footprint, hook_health, installer
from claude_token_lens.config import CaptureConfig, load_config

NOW = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)
ESSENTIALS = hook_health.capture_specs(cat.level_metrics("essentials"))
DEEP = hook_health.capture_specs(cat.level_metrics("deep"))


@pytest.fixture(autouse=True)
def _claude_folder(tmp_path, monkeypatch):
    """settings.json lives in ``<tmp>/claude`` (``$CLAUDE_CONFIG_DIR``),
    never in the real ~/.claude."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(installer, "is_registered", lambda *a, **k: False)


def _claude(tmp_path, settings=None):
    claude = tmp_path / "claude"
    config_dir = claude / "token-lens"
    config_dir.mkdir(parents=True)
    if settings is not None:
        (claude / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return config_dir


def _settings(config_dir) -> dict:
    return json.loads((config_dir.parent / "settings.json").read_text(encoding="utf-8"))


def _commands(config_dir) -> dict[str, str]:
    return cli._capture_hook_commands(config_dir)


def _capture(config_dir, *argv, stdin="", now=NOW):
    """Run ``capture`` through the real parser; returns (rc, output)."""
    args = cli._make_parser().parse_args(["capture", *argv, "--config-dir", str(config_dir)])
    out = io.StringIO()
    rc = cli._cmd_capture(args, stdin=io.StringIO(stdin), stdout=out, now=now)
    return rc, out.getvalue()


def _entries(settings: dict) -> list[tuple[str, str, dict]]:
    found = []
    for event, groups in settings.get("hooks", {}).items():
        for group in groups:
            for entry in group["hooks"]:
                found.append((event, group.get("matcher", ""), entry))
    return found


# -- hook_health: syncing the capture entries ------------------------------


def test_plan_capture_adds_the_entries_a_level_needs_and_writes_nothing(tmp_path):
    config_dir = _claude(tmp_path, {"model": "opus"})
    before = (config_dir.parent / "settings.json").read_text(encoding="utf-8")
    plan = hook_health.plan_capture(DEEP, _commands(config_dir))
    assert (config_dir.parent / "settings.json").read_text(encoding="utf-8") == before
    after = json.loads(plan.new_text)
    entries = _entries(after)
    assert [(event, matcher) for event, matcher, _ in entries] == [
        ("SessionStart", "startup|clear|compact"),
        ("SubagentStart", ""),
        ("PostToolUse", ""),
    ]
    assert all(entry["timeout"] == 5 for _, _, entry in entries)
    assert [entry.get("async", False) for _, _, entry in entries] == [False, False, True]
    assert after["model"] == "opus"
    assert len(plan.changes) == 3 and all(line.startswith("Add the capture hook") for line in plan.changes)


def test_plan_capture_is_a_no_op_once_connected(tmp_path):
    config_dir = _claude(tmp_path, {})
    hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[cat.NOTE_SCRIPT])
    hook_health.connect(hook_health.plan_capture(ESSENTIALS, _commands(config_dir)), now=NOW)
    again = hook_health.plan_capture(ESSENTIALS, _commands(config_dir))
    assert again.new_text is None and again.changes == []
    assert hook_health.check_capture(ESSENTIALS).ok


def test_a_changed_command_is_updated_in_place_not_duplicated(tmp_path):
    config_dir = _claude(tmp_path, {})
    old = {cat.NOTE_SCRIPT: '"/old/python" "/old/hooks/capture-note.py"'}
    hook_health.connect(hook_health.plan_capture(ESSENTIALS, old), now=NOW)
    plan = hook_health.plan_capture(ESSENTIALS, _commands(config_dir))
    assert all(line.startswith("Update the capture hook") for line in plan.changes)
    entries = _entries(json.loads(plan.new_text))
    assert len(entries) == 2 and all("/old/python" not in entry["command"] for _, _, entry in entries)


def test_lowering_the_level_takes_out_entries_no_metric_needs(tmp_path):
    config_dir = _claude(tmp_path, {})
    hook_health.connect(hook_health.plan_capture(DEEP, _commands(config_dir)), now=NOW)
    plan = hook_health.plan_capture(ESSENTIALS, _commands(config_dir))
    assert plan.changes == ["Remove the capture hook that runs capture-note.py after each tool result, in the background."]
    assert "PostToolUse" not in json.loads(plan.new_text)["hooks"]


def test_other_hooks_are_never_touched(tmp_path):
    mine = {"type": "command", "command": "echo mine"}
    config_dir = _claude(tmp_path, {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [mine]}], "Stop": [{"hooks": [mine]}]}})
    hook_health.connect(hook_health.plan_capture(DEEP, _commands(config_dir)), now=NOW)
    removal = hook_health.plan_capture((), {})
    after = json.loads(removal.new_text)
    assert after == {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [mine]}], "Stop": [{"hooks": [mine]}]}}


def test_a_capture_entry_sharing_a_group_leaves_the_rest_of_the_group(tmp_path):
    mine = {"type": "command", "command": "echo mine"}
    config_dir = _claude(tmp_path, {})
    capture_entry = {"type": "command", "command": _commands(config_dir)[cat.NOTE_SCRIPT], "timeout": 5}
    (config_dir.parent / "settings.json").write_text(
        json.dumps({"hooks": {"SubagentStart": [{"hooks": [mine, capture_entry]}]}}), encoding="utf-8"
    )
    assert hook_health.check_capture(ESSENTIALS).missing == (ESSENTIALS[0],)
    after = json.loads(hook_health.plan_capture((), {}).new_text)
    assert after == {"hooks": {"SubagentStart": [{"hooks": [mine]}]}}


def test_a_settings_file_of_an_odd_shape_is_left_alone(tmp_path):
    config_dir = _claude(tmp_path, {"hooks": ["not", "a", "table"]})
    plan = hook_health.plan_capture(ESSENTIALS, _commands(config_dir))
    assert plan.new_text is None
    (config_dir.parent / "settings.json").write_text("{broken", encoding="utf-8")
    assert "could not be read" in hook_health.plan_capture(ESSENTIALS, _commands(config_dir)).changes[0]


def test_check_capture_names_missing_extra_and_broken_entries(tmp_path):
    config_dir = _claude(tmp_path, {})
    health = hook_health.check_capture(ESSENTIALS)
    assert health.missing == ESSENTIALS and not health.ok
    assert "capture connect" in health.summary()
    hook_health.connect(hook_health.plan_capture(DEEP, _commands(config_dir)), now=NOW)
    health = hook_health.check_capture(ESSENTIALS)
    assert not health.missing and health.extra == DEEP[2:]
    # The script isn't installed yet, so the entries point at nothing.
    assert any("does not exist" in problem for problem in health.problems)
    hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[cat.NOTE_SCRIPT])
    health = hook_health.check_capture(ESSENTIALS)
    assert health.ok and "add nothing" in health.summary()


def test_check_capture_spots_a_percent_variable(tmp_path):
    config_dir = _claude(tmp_path, {})
    hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[cat.NOTE_SCRIPT])
    commands = {cat.NOTE_SCRIPT: _commands(config_dir)[cat.NOTE_SCRIPT] + " --x %USERPROFILE%"}
    hook_health.connect(hook_health.plan_capture(ESSENTIALS, commands), now=NOW)
    assert any("%VARIABLE%" in problem for problem in hook_health.check_capture(ESSENTIALS).problems)


def test_backups_made_in_the_same_second_never_overwrite_each_other(tmp_path):
    config_dir = _claude(tmp_path, {"model": "opus"})
    first = hook_health.connect(hook_health.plan_capture(ESSENTIALS, _commands(config_dir)), now=NOW)
    second = hook_health.connect(hook_health.plan_capture((), {}), now=NOW)
    assert first.name == "settings.json.bak-20260924T060000Z"
    assert second.name == "settings.json.bak-20260924T060000Z-2"
    assert json.loads(first.read_text(encoding="utf-8")) == {"model": "opus"}


def test_hook_files_are_copied_from_the_package(tmp_path):
    from importlib import resources

    written = hook_health.install_hook_files(tmp_path, [cat.NOTE_SCRIPT, cat.CATALOGUE_FILE, hook_health.HOOK_SCRIPT_NAME])
    for path in written:
        packaged = resources.files("claude_token_lens") / "hooks" / path.name
        assert path.read_bytes() == packaged.read_bytes()
    assert sorted(p.name for p in (tmp_path / "hooks").iterdir()) == sorted(p.name for p in written)


def test_the_snapshot_hook_is_installed_from_the_package_too(tmp_path):
    hook = cli._load_snapshot_hook_module()
    dest = hook.install_hook(tmp_path)
    assert dest == tmp_path / "hooks" / hook_health.HOOK_SCRIPT_NAME and dest.is_file()


# -- the capture command ---------------------------------------------------


def test_status_when_off(tmp_path):
    config_dir = _claude(tmp_path)
    rc, out = _capture(config_dir)
    assert rc == 0 and out.startswith("Metrics capture: Off")
    assert "capture level off|free|essentials|standard|deep" in out


def test_on_dry_run_shows_the_cost_and_the_diff_and_writes_nothing(tmp_path):
    config_dir = _claude(tmp_path, {"model": "opus"})
    before = (config_dir.parent / "settings.json").read_text(encoding="utf-8")
    rc, out = _capture(config_dir, "on", "--dry-run", stdin="y\ny\n")
    assert rc == 0
    assert "Off -> Essentials" in out
    assert "This makes Claude use more of your tokens" in out
    assert f"about {cat.rough_tokens(cat.level_metrics('essentials'))['session_note']} tokens of note" in out
    assert "Dry run: config.toml left unchanged." in out and "Dry run: settings.json left unchanged." in out
    assert "+" in out and "capture-note.py" in out
    assert (config_dir.parent / "settings.json").read_text(encoding="utf-8") == before
    assert not (config_dir / "config.toml").exists() and not (config_dir / "hooks").exists()


def test_a_no_to_the_cost_question_changes_nothing(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, "on", stdin="n\n")
    assert rc == 1 and "Left unchanged." in out
    assert not (config_dir / "config.toml").exists()


def test_a_no_to_the_settings_change_keeps_the_level_and_says_how_to_connect(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, "on", "--level", "standard", stdin="y\nn\n")
    assert rc == 0
    assert load_config(config_dir=config_dir).capture.level == "standard"
    assert _settings(config_dir) == {}
    assert "claude-token-lens capture connect" in out and "can't be captured" in out


def test_on_yes_connects_everything_and_a_second_run_changes_nothing(tmp_path):
    config_dir = _claude(tmp_path, {"model": "opus"})
    rc, out = _capture(config_dir, "on", "--yes", "--sample", "25")
    assert rc == 0 and "Restart Claude Code" in out
    capture = load_config(config_dir=config_dir).capture
    assert (capture.level, capture.sample, capture.enabled_at) == ("essentials", 25, "2026-09-24T06:00:00+00:00")
    assert (config_dir / "hooks" / cat.NOTE_SCRIPT).is_file() and (config_dir / "hooks" / cat.CATALOGUE_FILE).is_file()
    assert hook_health.check_capture(ESSENTIALS).ok
    rc, out = _capture(config_dir, "on", "--yes")
    assert "already Essentials" in out and "already runs the capture hooks" in out


def test_levels_up_and_down_sync_the_entries(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", "--yes")
    _capture(config_dir, "level", "deep", "--yes")
    assert hook_health.check_capture(DEEP).ok
    rc, out = _capture(config_dir, "level", "essentials", "--yes")
    assert "This makes Claude use more" not in out  # lowering asks nothing about cost
    assert [e for e, _, _ in _entries(_settings(config_dir))] == ["SessionStart", "SubagentStart"]


def test_disabling_a_metric_takes_what_needs_it_along(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "level", "standard", "--yes")
    rc, out = _capture(config_dir, "disable", "result", "--yes")
    assert rc == 0 and "fit, rules, agent_brief need result, so they go too." in out
    capture = load_config(config_dir=config_dir).capture
    assert capture.level == "custom" and not {"result", "fit", "rules", "agent_brief"} & set(capture.metrics)


def test_enabling_one_metric_brings_what_it_needs(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, "enable", "fit", "--yes")
    assert rc == 0 and "Off -> Custom" in out
    assert load_config(config_dir=config_dir).capture.metrics == ["result", "fit"]


def test_feedback_toggles_are_switched_on_their_own_list(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "enable", "feedback_skill", "feedback_note", "--yes")
    capture = load_config(config_dir=config_dir).capture
    assert capture.feedback == ["feedback_skill", "feedback_note"] and capture.level == "off"
    _capture(config_dir, "disable", "feedback_note", "--yes")
    assert load_config(config_dir=config_dir).capture.feedback == ["feedback_skill"]


@pytest.mark.parametrize("argv, message", [
    (["enable", "mood"], "unknown metric mood"),
    (["enable"], "needs one or more metric ids"),
    (["enable", "prompt_features"], "always measured"),
    (["level", "max"], "'capture level' needs one of"),
    (["on", "--for", "soon"], "--for 'soon'"),
    (["on", "--for", "0d"], "--for '0d'"),
    (["on", "--for", "7d", "--until", "2026-10-01"], "not both"),
    (["on", "--until", "next week", "--yes"], "'capture.until'"),
])
def test_bad_arguments_are_named_and_change_nothing(tmp_path, argv, message):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, *argv)
    assert rc == 2 and message in out
    assert not (config_dir / "config.toml").exists()


def test_for_sets_the_end_time(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", "--for", "2w", "--yes")
    capture = load_config(config_dir=config_dir).capture
    assert capture.until == (NOW + timedelta(weeks=2)).isoformat(timespec="seconds")
    rc, out = _capture(config_dir)
    assert "until 2026-10-08 06:00" in out


def test_off_keeps_the_entries_and_remove_takes_them_out(tmp_path):
    mine = {"type": "command", "command": "echo mine"}
    config_dir = _claude(tmp_path, {"hooks": {"Stop": [{"hooks": [mine]}]}})
    _capture(config_dir, "on", "--yes")
    rc, out = _capture(config_dir, "off")
    assert rc == 0 and "add nothing while capture is off" in out and "capture remove" in out
    assert load_config(config_dir=config_dir).capture == CaptureConfig(level="off")
    assert len(_entries(_settings(config_dir))) == 3
    rc, out = _capture(config_dir, "remove", "--dry-run")
    assert "Dry run: settings.json left unchanged. Run 'claude-token-lens capture remove'" in out
    rc, out = _capture(config_dir, "remove", "--yes")
    assert _settings(config_dir) == {"hooks": {"Stop": [{"hooks": [mine]}]}}


def test_connect_while_off_says_nothing_is_needed(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, "connect")
    assert rc == 0 and "no hook entries are needed" in out and _settings(config_dir) == {}


def test_status_reports_hooks_that_are_missing(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", stdin="y\nn\n")
    rc, out = _capture(config_dir, "status")
    assert "Metrics capture: Essentials (since 2026-09-24)" in out
    assert "Hooks: settings.json does not run capture-note.py" in out


def test_a_bad_config_is_reported_not_overwritten(tmp_path):
    config_dir = _claude(tmp_path, {})
    (config_dir / "config.toml").write_text('[capture]\nlevel = "max"\n', encoding="utf-8")
    rc, out = _capture(config_dir, "on", "--yes")
    assert rc == 2 and "config.toml has a problem" in out
    assert (config_dir / "config.toml").read_text(encoding="utf-8") == '[capture]\nlevel = "max"\n'


def test_a_data_folder_elsewhere_is_passed_to_the_hook(tmp_path):
    _claude(tmp_path, {})
    elsewhere = tmp_path / "data"
    elsewhere.mkdir()
    _capture(elsewhere, "on", "--yes")
    commands = [entry["command"] for _, _, entry in _entries(json.loads((tmp_path / "claude" / "settings.json").read_text(encoding="utf-8")))]
    assert commands and all(f'--config-dir "{elsewhere}"' in command for command in commands)


# -- changes and uninstall -------------------------------------------------


def test_expectations_say_capture_uses_tokens_only_while_it_does():
    assert footprint.expectations(None) == footprint.EXPECTATIONS
    assert footprint.expectations(CaptureConfig()) == footprint.EXPECTATIONS
    on = footprint.expectations(CaptureConfig(level="standard"))
    assert on[0][0] == "It uses a few of your Claude tokens while capture is on" and "(Standard)" in on[0][1]
    assert on[1:] == footprint.EXPECTATIONS[1:]
    free = footprint.expectations(CaptureConfig(level="free"))
    assert "adds no tokens" in free[0][1]


def test_inventory_lists_the_capture_hooks_only_when_there_are_any(tmp_path):
    config_dir = _claude(tmp_path, {})
    assert "capture_hooks" not in {item.key for item in footprint.inventory(config_dir, service_registered=False)}
    _capture(config_dir, "on", "--yes")
    items = {item.key: item for item in footprint.inventory(config_dir, service_registered=False)}
    item = items["capture_hooks"]
    assert item.status == "installed" and item.title == "Metrics capture hooks (2 entries)"
    assert "Essentials" in item.token_cost and "capture off" in item.undo


def test_uninstall_takes_out_the_capture_entries(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", "--yes")
    plan = footprint.plan_uninstall(config_dir)
    assert plan.settings_changes == [
        "Remove the capture hook that runs capture-note.py when a session starts, is cleared or compacts.",
        "Remove the capture hook that runs capture-note.py when a subagent starts.",
    ]
    assert json.loads(plan.new_settings_text) == {}


def test_changes_prints_the_capture_expectation(tmp_path, capsys):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", "--yes")
    rc = cli.main(["changes", "--config-dir", str(config_dir)])
    out = capsys.readouterr().out
    assert rc == 0 and "Metrics capture hooks (2 entries): installed" in out
    assert "It uses a few of your Claude tokens while capture is on" in out
