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
        ("SessionEnd", ""),
        ("Notification", ""),
        ("PermissionRequest", ""),
    ]
    assert all(entry["timeout"] == 5 for _, _, entry in entries)
    assert [entry.get("async", False) for _, _, entry in entries] == [False, False, True, False, True, True]
    assert after["model"] == "opus"
    assert len(plan.changes) == 6 and all(line.startswith("Add the capture hook") for line in plan.changes)
    assert "Add the capture hook that runs capture-hook.py when Claude waits for you, in the background." in plan.changes


def test_plan_capture_is_a_no_op_once_connected(tmp_path):
    config_dir = _claude(tmp_path, {})
    hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[cat.HOOK_SCRIPT])
    hook_health.connect(hook_health.plan_capture(ESSENTIALS, _commands(config_dir)), now=NOW)
    again = hook_health.plan_capture(ESSENTIALS, _commands(config_dir))
    assert again.new_text is None and again.changes == []
    assert hook_health.check_capture(ESSENTIALS).ok


def test_a_changed_command_is_updated_in_place_not_duplicated(tmp_path):
    config_dir = _claude(tmp_path, {})
    old = {cat.HOOK_SCRIPT: '"/old/python" "/old/hooks/capture-hook.py"'}
    hook_health.connect(hook_health.plan_capture(ESSENTIALS, old), now=NOW)
    plan = hook_health.plan_capture(ESSENTIALS, _commands(config_dir))
    assert all(line.startswith("Update the capture hook") for line in plan.changes)
    entries = _entries(json.loads(plan.new_text))
    assert len(entries) == len(ESSENTIALS) and all("/old/python" not in entry["command"] for _, _, entry in entries)


def test_lowering_the_level_takes_out_entries_no_metric_needs(tmp_path):
    config_dir = _claude(tmp_path, {})
    hook_health.connect(hook_health.plan_capture(DEEP, _commands(config_dir)), now=NOW)
    plan = hook_health.plan_capture(ESSENTIALS, _commands(config_dir))
    assert plan.changes == ["Remove the capture hook that runs capture-hook.py after each tool result, in the background."]
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
    capture_entry = {"type": "command", "command": _commands(config_dir)[cat.HOOK_SCRIPT], "timeout": 5}
    (config_dir.parent / "settings.json").write_text(
        json.dumps({"hooks": {"SubagentStart": [{"hooks": [mine, capture_entry]}]}}), encoding="utf-8"
    )
    assert hook_health.check_capture(ESSENTIALS).missing == tuple(s for s in ESSENTIALS if s.event != "SubagentStart")
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
    assert not health.missing and health.extra == tuple(spec for spec in DEEP if spec not in ESSENTIALS)
    # The script isn't installed yet, so the entries point at nothing.
    assert any("does not exist" in problem for problem in health.problems)
    hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[cat.HOOK_SCRIPT])
    health = hook_health.check_capture(ESSENTIALS)
    assert health.ok and "add nothing" in health.summary()


def test_check_capture_spots_a_percent_variable(tmp_path):
    config_dir = _claude(tmp_path, {})
    hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[cat.HOOK_SCRIPT])
    commands = {cat.HOOK_SCRIPT: _commands(config_dir)[cat.HOOK_SCRIPT] + " --x %USERPROFILE%"}
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

    written = hook_health.install_hook_files(tmp_path, [cat.HOOK_SCRIPT, cat.CATALOGUE_FILE, hook_health.HOOK_SCRIPT_NAME])
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
    assert "+" in out and "capture-hook.py" in out
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
    assert (config_dir / "hooks" / cat.HOOK_SCRIPT).is_file() and (config_dir / "hooks" / cat.CATALOGUE_FILE).is_file()
    assert len((config_dir / "salt").read_bytes()) == 32  # the free signals hash session ids with it
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
    assert [e for e, _, _ in _entries(_settings(config_dir))] == [spec.event for spec in ESSENTIALS]


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
    assert len(_entries(_settings(config_dir))) == len(ESSENTIALS) + 1  # and the Stop entry of their own
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
    assert "Hooks: settings.json does not run capture-hook.py" in out


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
    assert "adds no tokens" in free[0][1] and "(Free)" in free[0][1]


def test_inventory_lists_the_capture_hooks_only_when_there_are_any(tmp_path):
    config_dir = _claude(tmp_path, {})
    assert "capture_hooks" not in {item.key for item in footprint.inventory(config_dir, service_registered=False)}
    _capture(config_dir, "on", "--yes")
    items = {item.key: item for item in footprint.inventory(config_dir, service_registered=False)}
    item = items["capture_hooks"]
    assert item.status == "installed" and item.title == "Metrics capture hooks (5 entries)"
    assert "Essentials" in item.token_cost and "capture off" in item.undo
    _capture(config_dir, "level", "free", "--yes")
    item = {item.key: item for item in footprint.inventory(config_dir, service_registered=False)}["capture_hooks"]
    assert item.title == "Metrics capture hooks (3 entries)" and item.token_cost.startswith("None at Free")


def test_uninstall_takes_out_the_capture_entries(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", "--yes")
    plan = footprint.plan_uninstall(config_dir)
    assert plan.settings_changes == [
        "Remove the capture hook that runs capture-hook.py when a session starts, is cleared or compacts.",
        "Remove the capture hook that runs capture-hook.py when a subagent starts.",
        "Remove the capture hook that runs capture-hook.py when a session ends.",
        "Remove the capture hook that runs capture-hook.py when Claude waits for you, in the background.",
        "Remove the capture hook that runs capture-hook.py when Claude asks for permission, in the background.",
    ]
    assert json.loads(plan.new_settings_text) == {}


def test_changes_prints_the_capture_expectation(tmp_path, capsys):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", "--yes")
    rc = cli.main(["changes", "--config-dir", str(config_dir)])
    out = capsys.readouterr().out
    assert rc == 0 and "Metrics capture hooks (5 entries): installed" in out
    assert "It uses a few of your Claude tokens while capture is on" in out


# -- what it costs: capture status, and the init question -------------------------


def _session(config_dir, *, captured: bool = False, days_ago: float = 1, name: str = "s1"):
    """One recent session under ``<tmp>/claude/projects`` (the projects
    root while ``$CLAUDE_CONFIG_DIR`` points there): a message and a
    tagged reply, with the Essentials note first when ``captured``."""
    from helpers import attachment_line, turn_line, user_str_line, write_jsonl

    start = datetime.now(timezone.utc) - timedelta(days=days_ago)

    def ts(second):
        return (start + timedelta(seconds=second)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    lines = []
    if captured:
        text = cat.note_text(cat.level_metrics("essentials"), "main")
        note = attachment_line(
            "hook_additional_context",
            rendered=f"<system-reminder>\nSessionStart hook additional context: {text}\n</system-reminder>",
            content=[text], hookName="SessionStart", hookEvent="SessionStart", toolUseID="SessionStart",
        )
        note["timestamp"] = ts(0)
        lines.append(note)
    lines += [
        user_str_line("fix the failing test", origin={"kind": "human"}, timestamp=ts(1)),
        turn_line(content=[{"type": "text", "text": "Fixed.\n[tl: task=bugfix brief=clear]"}], timestamp=ts(2),
                  input_tokens=2000, output_tokens=400),
    ]
    project = config_dir.parent / "projects" / "C--work-app"
    project.mkdir(parents=True, exist_ok=True)
    write_jsonl(project / f"{name}.jsonl", lines)
    return start


def _api_billing(config_dir):
    (config_dir / "config.toml").write_text('billing = "api"\n', encoding="utf-8")


def test_status_while_off_estimates_each_level_from_your_sessions(tmp_path):
    config_dir = _claude(tmp_path, {})
    _api_billing(config_dir)
    _session(config_dir)
    rc, out = _capture(config_dir, "status")
    assert rc == 0
    assert "What each level would have cost over your last 14 days (1 session, 0 subagents):" in out
    assert "Free        nothing: it only logs a few events to a local file" in out
    for title in ("Essentials", "Standard", "Deep"):
        line = next(line for line in out.splitlines() if line.strip().startswith(title))
        assert "tokens and" in line and "a week" in line and "of what you spent" in line


def test_status_while_on_shows_what_it_measured(tmp_path):
    config_dir = _claude(tmp_path, {})
    _api_billing(config_dir)
    start = _session(config_dir, captured=True)
    from claude_token_lens.config import set_capture

    set_capture(config_dir, level="essentials", now=start - timedelta(hours=1))
    rc, out = _capture(config_dir, "status")
    assert f"Measured since {(start - timedelta(hours=1)).date().isoformat()}: 1 session and 0 subagents captured" in out
    assert "tokens of note and" in out and "of what those sessions cost" in out
    assert "Claude tagged 100.0% of your messages" in out


def test_status_while_on_before_any_captured_session_says_so(tmp_path):
    config_dir = _claude(tmp_path, {})
    _session(config_dir, days_ago=2)
    _capture(config_dir, "on", "--yes")
    rc, out = _capture(config_dir, "status")
    assert "No captured sessions yet" in out


def _init_args(config_dir, *argv):
    return cli._make_parser().parse_args(["init", "--config-dir", str(config_dir), *argv])


def _init_capture(config_dir, *argv, stdin=""):
    out = io.StringIO()
    cli._cmd_init_capture_step(
        _init_args(config_dir, *argv), config_dir=config_dir, claude_root=config_dir.parent,
        stdin=io.StringIO(stdin), stdout=out, now=NOW,
    )
    return out.getvalue()


def test_init_warns_shows_estimates_and_connects_after_a_yes(tmp_path):
    config_dir = _claude(tmp_path, {})
    _api_billing(config_dir)
    _session(config_dir)
    out = _init_capture(config_dir, stdin="standard\ny\n")
    assert "This uses your tokens" in out and "[tl: task=bugfix brief=clear], which you will see" in out
    assert "What each level would have cost over your last 14 days" in out
    assert "Metrics capture level: off, free, essentials, standard, deep [off]:" in out
    assert "Saved to config.toml: metrics capture Standard (since 2026-09-24)." in out
    assert load_config(config_dir).capture.level == "standard"
    assert len(_entries(_settings(config_dir))) == len(hook_health.capture_specs(cat.level_metrics("standard")))


@pytest.mark.parametrize("typed, level", [("", "off"), ("n", "off"), ("yes", "essentials"), ("Deep", "deep")])
def test_init_reads_yes_no_and_level_names(tmp_path, typed, level):
    config_dir = _claude(tmp_path, {})
    _init_capture(config_dir, stdin=f"{typed}\nn\n")
    assert load_config(config_dir).capture.level == level


def test_init_does_not_turn_it_on_for_a_word_it_does_not_know(tmp_path):
    config_dir = _claude(tmp_path, {})
    out = _init_capture(config_dir, stdin="max\n")
    assert "'max' isn't a level" in out and load_config(config_dir).capture.level == "off"


def test_non_interactive_init_leaves_it_off_and_says_so(tmp_path):
    config_dir = _claude(tmp_path, {})
    out = _init_capture(config_dir, "--non-interactive")
    assert "(derived) capture_level: not given in --answers; metrics capture left off" in out
    assert "This uses your tokens" not in out
    assert load_config(config_dir).capture.level == "off"


def test_init_capture_level_flag_without_connecting_prints_the_command(tmp_path):
    config_dir = _claude(tmp_path, {})
    out = _init_capture(config_dir, "--non-interactive", "--no-install", "--capture-level", "essentials")
    assert "This uses your tokens" in out
    assert "Add the hook entries it needs with: claude-token-lens capture connect" in out
    assert load_config(config_dir).capture.level == "essentials"
    assert _settings(config_dir) == {}


def test_init_answers_file_level_with_connect_writes_without_asking(tmp_path):
    config_dir = _claude(tmp_path, {})
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"capture_level": "free"}), encoding="utf-8")
    out = _init_capture(config_dir, "--non-interactive", "--connect", "--answers", str(answers))
    assert load_config(config_dir).capture.level == "free"
    assert len(_entries(_settings(config_dir))) == len(hook_health.capture_specs(cat.level_metrics("free")))
    assert "Make this change?" not in out


def test_init_leaves_capture_that_is_already_on_alone(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", "--level", "standard", "--yes")
    out = _init_capture(config_dir, stdin="off\n")
    assert "Metrics capture is Standard (since 2026-09-24)." in out
    assert load_config(config_dir).capture.level == "standard"
    out = _init_capture(config_dir, "--capture-level", "off")
    assert "Metrics capture switched off." in out and load_config(config_dir).capture.level == "off"


def test_init_asks_about_capture_last(tmp_path, monkeypatch, capsys):
    config_dir = _claude(tmp_path, {})
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["init", "--non-interactive", "--no-install", "--no-service", "--config-dir", str(config_dir),
                   "--capture-level", "essentials"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.rindex("Metrics capture (optional)") > out.index("Wrote config.toml")
    assert load_config(config_dir).capture.level == "essentials"


# -- feedback: the /tl-feedback skill --------------------------------------------


def _skill(config_dir):
    return config_dir.parent / "skills" / "tl-feedback" / "SKILL.md"


def test_feedback_on_shows_the_skill_and_writes_it_after_a_yes(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, "feedback", "on", stdin="y\n")
    assert rc == 0 and "Nothing is added to Claude's context until you run the skill." in out
    assert f"This adds the /tl-feedback skill, {_skill(config_dir)}:" in out
    assert "    name: tl-feedback" in out and "Add it? (y/n) [n]:" in out
    assert _skill(config_dir).read_text(encoding="utf-8") == cat.feedback_skill_text()
    capture = load_config(config_dir=config_dir).capture
    assert capture.feedback == ["feedback_skill", "feedback_note"] and capture.level == "off"
    # settings.json is never touched: the skill needs no hook.
    assert _settings(config_dir) == {}
    rc, out = _capture(config_dir, "feedback", "on")
    assert "Feedback is already on." in out and "The /tl-feedback skill is in place" in out


def test_feedback_on_dry_run_and_a_no_write_no_skill(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, "feedback", "on", "--dry-run")
    assert "Dry run: config.toml left unchanged." in out and "Dry run: the skill is left as it is." in out
    assert not _skill(config_dir).exists() and not (config_dir / "config.toml").exists()
    rc, out = _capture(config_dir, "feedback", "on", stdin="n\n")
    assert "Left as it is. Run 'claude-token-lens capture feedback on'" in out
    assert not _skill(config_dir).exists()
    rc, out = _capture(config_dir, "status")
    assert "The /tl-feedback skill isn't installed: claude-token-lens capture feedback on" in out


def test_an_old_skill_is_shown_as_a_diff_and_someone_elses_is_left_alone(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "feedback", "on", "--yes")
    skill = _skill(config_dir)
    skill.write_text(cat.feedback_skill_text().replace("four quick", "three quick"), encoding="utf-8")
    assert "out of date" in _capture(config_dir, "status")[1]
    rc, out = _capture(config_dir, "feedback", "on", "--yes")
    assert "This updates the /tl-feedback skill" in out and "-description:" in out
    assert skill.read_text(encoding="utf-8") == cat.feedback_skill_text()
    skill.write_text("---\nname: tl-feedback\n---\nmine\n", encoding="utf-8")
    rc, out = _capture(config_dir, "feedback", "on", "--yes")
    assert "holds a skill this tool didn't write, so it is left alone" in out
    rc, out = _capture(config_dir, "feedback", "off", "--yes")
    assert skill.read_text(encoding="utf-8") == "---\nname: tl-feedback\n---\nmine\n"


def test_feedback_off_removes_the_skill_and_its_reminders_but_not_the_rating(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "enable", "dashboard_rating", "feedback_reminder", "--yes")
    _capture(config_dir, "feedback", "on", "--yes")
    rc, out = _capture(config_dir, "feedback", "off", stdin="y\n")
    assert "This removes the /tl-feedback skill" in out and "Removed." in out
    assert not _skill(config_dir).exists() and not _skill(config_dir).parent.exists()
    assert load_config(config_dir=config_dir).capture.feedback == ["dashboard_rating"]


def test_enabling_or_disabling_the_skill_metric_installs_or_removes_it(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "enable", "feedback_skill", "--yes")
    assert _skill(config_dir).is_file()
    _capture(config_dir, "disable", "feedback_skill", "--yes")
    assert not _skill(config_dir).exists()


def test_remove_keeps_the_skill_and_says_how_to_take_it_out(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", "--yes")
    _capture(config_dir, "feedback", "on", "--yes")
    rc, out = _capture(config_dir, "remove", "--yes")
    assert "The /tl-feedback skill stays: it works with capture off." in out
    assert _skill(config_dir).is_file()


@pytest.mark.parametrize("argv", [["feedback"], ["feedback", "maybe"], ["feedback", "on", "off"]])
def test_feedback_needs_on_or_off(tmp_path, argv):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, *argv)
    assert rc == 2 and "'capture feedback' needs on or off" in out


def test_the_skill_is_listed_and_taken_out_by_uninstall(tmp_path, monkeypatch, capsys):
    config_dir = _claude(tmp_path, {})
    assert "feedback_skill" not in {i.key for i in footprint.inventory(config_dir, service_registered=False)}
    _capture(config_dir, "feedback", "on", "--yes")
    item = {i.key: i for i in footprint.inventory(config_dir, service_registered=False)}["feedback_skill"]
    assert item.status == "installed" and item.undo == "claude-token-lens capture feedback off"
    assert "None until you run it" in item.token_cost
    plan = footprint.plan_uninstall(config_dir)
    assert plan.feedback_skill == _skill(config_dir)
    rc = cli.main(["uninstall", "--yes", "--config-dir", str(config_dir)])
    out = capsys.readouterr().out
    assert "The /tl-feedback skill:" in out and "Removed." in out
    assert not _skill(config_dir).exists()


def _init_feedback(config_dir, *argv, stdin=""):
    out = io.StringIO()
    cli._cmd_init_feedback_step(
        _init_args(config_dir, *argv), config_dir=config_dir, claude_root=config_dir.parent,
        stdin=io.StringIO(stdin), stdout=out, now=NOW,
    )
    return out.getvalue()


def test_init_offers_the_skill_and_writes_it_after_two_yeses(tmp_path):
    config_dir = _claude(tmp_path, {})
    out = _init_feedback(config_dir, stdin="y\ny\n")
    assert "Feedback after a piece of work (optional)" in out and "It works at any capture level" in out
    assert "Add the /tl-feedback skill? (y/n) [n]:" in out and "Add it? (y/n) [n]:" in out
    assert _skill(config_dir).is_file()
    assert load_config(config_dir).capture.feedback == ["feedback_skill", "feedback_note"]
    out = _init_feedback(config_dir, stdin="n\n")
    assert "The /tl-feedback skill is on." in out and _skill(config_dir).is_file()


def test_init_feedback_no_and_non_interactive_leave_it_off(tmp_path):
    config_dir = _claude(tmp_path, {})
    assert "Feedback left off." in _init_feedback(config_dir, stdin="\n")
    out = _init_feedback(config_dir, "--non-interactive")
    assert "(derived) feedback: not given in --answers; left off" in out
    assert "Feedback after a piece of work" not in out
    assert not _skill(config_dir).exists() and load_config(config_dir).capture.feedback == []


def test_init_feedback_flag_without_connecting_prints_the_command(tmp_path):
    config_dir = _claude(tmp_path, {})
    out = _init_feedback(config_dir, "--non-interactive", "--no-install", "--feedback", "on")
    assert "Add the skill with: claude-token-lens capture feedback on" in out
    assert not _skill(config_dir).exists()
    assert load_config(config_dir).capture.feedback == ["feedback_skill", "feedback_note"]


def test_init_feedback_answers_file_with_connect_writes_without_asking(tmp_path):
    config_dir = _claude(tmp_path, {})
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"feedback": True}), encoding="utf-8")
    out = _init_feedback(config_dir, "--non-interactive", "--connect", "--answers", str(answers))
    assert _skill(config_dir).is_file() and "Add it?" not in out
    out = _init_feedback(config_dir, "--non-interactive", "--connect", "--feedback", "off")
    assert "Saved to config.toml: feedback off." in out and not _skill(config_dir).exists()


def test_init_asks_about_feedback_after_capture(tmp_path, monkeypatch, capsys):
    config_dir = _claude(tmp_path, {})
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["init", "--non-interactive", "--no-install", "--no-service", "--config-dir", str(config_dir),
                   "--capture-level", "off", "--feedback", "on"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("Add the skill with: claude-token-lens capture feedback on") > out.index("Metrics capture (optional)")


def test_status_says_when_the_status_line_is_someone_elses(tmp_path):
    config_dir = _claude(tmp_path, {"statusLine": {"type": "command", "command": "my-own-line"}})
    _capture(config_dir, "feedback", "on", "--yes")
    rc, out = _capture(config_dir, "status")
    assert "Your status line isn't Token Lens's, so this second line won't show there" in out
    (config_dir.parent / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "claude-token-lens statusline"}}), encoding="utf-8"
    )
    assert "Your status line isn't" not in _capture(config_dir, "status")[1]
