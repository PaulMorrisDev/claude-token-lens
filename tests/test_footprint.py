"""Tests for ``footprint.py`` and the ``init`` connect step, ``changes``
and ``uninstall`` commands: what claude-token-lens installs, and taking
every part of it back out, always showing the change and backing up
settings.json first."""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timezone

import pytest

from claude_token_lens import cli, footprint, hook_health, installer
from claude_token_lens.profiles import apply as apply_mod

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
HOOK_CMD = f'"{sys.executable}" "C:/x/token-lens/hooks/snapshot-config.py"'
STATUS_CMD = f'"{sys.executable}" -m claude_token_lens.statusline'


def _claude(tmp_path, settings=None):
    claude = tmp_path / "claude"
    config_dir = claude / "token-lens"
    config_dir.mkdir(parents=True)
    if settings is not None:
        (claude / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return config_dir


def test_plan_connect_adds_hook_and_statusline_and_changes_nothing(tmp_path):
    config_dir = _claude(tmp_path, {"model": "opus"})
    before = (config_dir.parent / "settings.json").read_text(encoding="utf-8")
    plan = hook_health.plan_connect(config_dir, hook_command=HOOK_CMD, statusline_command=STATUS_CMD)
    assert (config_dir.parent / "settings.json").read_text(encoding="utf-8") == before
    assert len(plan.changes) == 2
    assert "adds no tokens" in plan.changes[0]
    assert "terminal only" in plan.changes[1]
    after = json.loads(plan.new_text)
    entry = after["hooks"]["SessionStart"][0]["hooks"][0]
    assert entry == {"type": "command", "command": HOOK_CMD, "async": True}
    assert after["statusLine"]["command"] == STATUS_CMD
    assert after["model"] == "opus"
    assert "+  \"statusLine\"" in plan.diff


def test_plan_connect_never_replaces_your_own_statusline(tmp_path):
    config_dir = _claude(tmp_path, {"statusLine": {"type": "command", "command": "my-line"}})
    plan = hook_health.plan_connect(config_dir, hook_command=HOOK_CMD, statusline_command=STATUS_CMD)
    assert json.loads(plan.new_text)["statusLine"]["command"] == "my-line"
    assert len(plan.changes) == 1


def test_connect_backs_up_then_writes(tmp_path):
    config_dir = _claude(tmp_path, {"model": "opus"})
    plan = hook_health.plan_connect(config_dir, hook_command=HOOK_CMD, statusline_command=None)
    backup = hook_health.connect(plan, now=NOW)
    assert backup.name == "settings.json.bak-20260923T120000Z"
    assert json.loads(backup.read_text(encoding="utf-8")) == {"model": "opus"}
    again = hook_health.plan_connect(config_dir, hook_command=HOOK_CMD, statusline_command=None)
    assert again.new_text is None and again.changes == []


def test_uninstall_plan_removes_only_this_tools_entries(tmp_path):
    settings = {
        "model": "opus",
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "echo mine"}, {"type": "command", "command": HOOK_CMD}]}
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}],
        },
        "statusLine": {"type": "command", "command": STATUS_CMD},
    }
    config_dir = _claude(tmp_path, settings)
    plan = footprint.plan_uninstall(config_dir)
    assert len(plan.settings_changes) == 2
    after = json.loads(plan.new_settings_text)
    assert after["hooks"]["SessionStart"][0]["hooks"] == [{"type": "command", "command": "echo mine"}]
    assert after["hooks"]["Stop"] == settings["hooks"]["Stop"]
    assert "statusLine" not in after
    backup = footprint.remove_settings_entries(plan, now=NOW)
    assert json.loads(backup.read_text(encoding="utf-8")) == settings


def test_uninstall_drops_empty_hook_groups(tmp_path):
    config_dir = _claude(tmp_path, {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": HOOK_CMD}]}]}})
    after = json.loads(footprint.plan_uninstall(config_dir).new_settings_text)
    assert "hooks" not in after


def test_uninstall_leaves_someone_elses_statusline(tmp_path):
    config_dir = _claude(tmp_path, {"statusLine": {"type": "command", "command": "my-line"}})
    assert footprint.plan_uninstall(config_dir).new_settings_text is None


def _fake_backup(config_dir, ts, *, reverted=False):
    folder = config_dir / "backups" / ts
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(
        json.dumps({"ts": ts, "profile_id": "one-off", "scope": "user", "entries": []}), encoding="utf-8"
    )
    if reverted:
        (folder / apply_mod.REVERTED_FILENAME).write_text(json.dumps({"reverted_at": "2026-09-23T00:00:00Z"}))


def test_inventory_lists_every_part_with_an_undo(tmp_path):
    config_dir = _claude(tmp_path, {"statusLine": {"type": "command", "command": STATUS_CMD}})
    _fake_backup(config_dir, "20260920T000000Z")
    _fake_backup(config_dir, "20260921T000000Z", reverted=True)
    items = {item.key: item for item in footprint.inventory(config_dir, service_registered=None)}
    assert items["snapshot_hook"].status == "not installed"
    assert items["statusline"].status == "installed"
    assert items["service"].status == "unknown"
    assert items["apply:20260920T000000Z"].status == "in place"
    assert items["apply:20260920T000000Z"].undo == "claude-token-lens apply --revert 20260920T000000Z"
    assert items["apply:20260920T000000Z"].title.startswith("Applied one-off change")
    assert items["apply:20260921T000000Z"].status == "undone"
    assert items["data"].status == "installed"
    for item in items.values():
        assert item.token_cost and item.what_it_does and item.undo


def test_list_backups_reads_the_reverted_marker(tmp_path):
    config_dir = _claude(tmp_path)
    _fake_backup(config_dir, "20260921T000000Z", reverted=True)
    assert apply_mod.list_backups(config_dir)[0].reverted_at == "2026-09-23T00:00:00Z"


def _run(argv, monkeypatch, capsys, stdin=""):
    monkeypatch.setattr(installer, "is_registered", lambda *a, **k: False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    rc = cli.main(argv)
    return rc, capsys.readouterr().out


def test_changes_command_prints_undo_commands(tmp_path, monkeypatch, capsys):
    config_dir = _claude(tmp_path, {"statusLine": {"type": "command", "command": STATUS_CMD}})
    rc, out = _run(["changes", "--config-dir", str(config_dir)], monkeypatch, capsys)
    assert rc == 0
    assert "Statusline: installed" in out
    assert "Tokens: None." in out
    assert "claude-token-lens uninstall" in out


def test_uninstall_dry_run_changes_nothing(tmp_path, monkeypatch, capsys):
    config_dir = _claude(tmp_path, {"statusLine": {"type": "command", "command": STATUS_CMD}})
    _fake_backup(config_dir, "20260920T000000Z")
    before = (config_dir.parent / "settings.json").read_text(encoding="utf-8")
    rc, out = _run(
        ["uninstall", "--config-dir", str(config_dir), "--dry-run", "--revert-changes", "--delete-data"],
        monkeypatch,
        capsys,
    )
    assert rc == 0
    assert "Dry run: settings.json left unchanged." in out
    assert "Would undo 20260920T000000Z" in out
    assert (config_dir.parent / "settings.json").read_text(encoding="utf-8") == before
    assert config_dir.is_dir()


def test_uninstall_keeps_data_while_changes_are_in_place(tmp_path, monkeypatch, capsys):
    config_dir = _claude(tmp_path, {"model": "opus"})
    _fake_backup(config_dir, "20260920T000000Z")
    rc, out = _run(["uninstall", "--config-dir", str(config_dir), "--yes", "--delete-data"], monkeypatch, capsys)
    assert rc == 1
    assert "Not deleted" in out
    assert config_dir.is_dir()


def test_uninstall_yes_removes_entries_and_data(tmp_path, monkeypatch, capsys):
    config_dir = _claude(tmp_path, {"model": "opus", "statusLine": {"type": "command", "command": STATUS_CMD}})
    rc, out = _run(["uninstall", "--config-dir", str(config_dir), "--yes", "--delete-data"], monkeypatch, capsys)
    assert rc == 0, out
    assert json.loads((config_dir.parent / "settings.json").read_text(encoding="utf-8")) == {"model": "opus"}
    assert not config_dir.exists()
    assert "pip uninstall claude-token-lens" in out


def test_uninstall_asks_and_a_no_changes_nothing(tmp_path, monkeypatch, capsys):
    config_dir = _claude(tmp_path, {"statusLine": {"type": "command", "command": STATUS_CMD}})
    rc, out = _run(["uninstall", "--config-dir", str(config_dir)], monkeypatch, capsys, stdin="n\n")
    assert "Left unchanged." in out
    assert "statusLine" in json.loads((config_dir.parent / "settings.json").read_text(encoding="utf-8"))
