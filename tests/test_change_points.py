"""Change points: applies, their undo, and settings changes the config
snapshots show (``change_points``)."""

from __future__ import annotations

import json
from pathlib import Path

from claude_token_lens import change_points
from claude_token_lens.profiles import apply as apply_mod
from claude_token_lens.profiles.schema import load_dict


def _apply(tmp_path: Path, settings: dict):
    claude_root = tmp_path / ".claude"
    config_dir = claude_root / "token-lens"
    claude_root.mkdir(exist_ok=True)
    profile = load_dict({"id": "one-off", "settings": settings})
    plan = apply_mod.plan_apply(profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root)
    return config_dir, apply_mod.execute(plan, config_dir=config_dir)


def _snapshot(config_dir: Path, ts: str, effective: dict) -> None:
    folder = config_dir / "snapshots"
    folder.mkdir(parents=True, exist_ok=True)
    doc = {"ts": ts, "schema_version": 2, "project_slug": "slug:abc", "effective": effective}
    (folder / f"{ts}.json").write_text(json.dumps(doc), encoding="utf-8")


def test_an_apply_and_its_undo_are_change_points_with_their_keys(tmp_path):
    config_dir, result = _apply(tmp_path, {"effortLevel": "medium"})
    [point] = change_points.change_points(config_dir)
    assert point.source == "apply"
    assert point.label == "Applied a one-off change"
    assert point.keys == ["effortLevel"]
    assert point.changes[0]["new"] == "medium"
    apply_mod.revert(result.ts, config_dir=config_dir)
    points = change_points.change_points(config_dir)
    assert [p.source for p in points] == ["apply", "revert"]
    assert points[0].reverted
    assert change_points.latest(config_dir).source == "revert"


def test_a_settings_change_between_snapshots_is_a_change_point(tmp_path):
    config_dir = tmp_path / "tl"
    _snapshot(config_dir, "20260920T100000Z", {"model": "opus"})
    _snapshot(config_dir, "20260921T100000Z", {"model": "opus"})
    _snapshot(config_dir, "20260922T100000Z", {"model": "sonnet"})
    [point] = change_points.change_points(config_dir)
    assert point.source == "config"
    assert point.iso() == "2026-09-22T10:00:00Z"
    assert point.keys == ["effective.model"]


def test_a_snapshot_difference_spanning_an_apply_is_not_counted_twice(tmp_path):
    _snapshot(tmp_path / ".claude" / "token-lens", "20000101T000000Z", {"effortLevel": "high"})
    config_dir, _result = _apply(tmp_path, {"effortLevel": "medium"})
    _snapshot(config_dir, "20990101T000000Z", {"effortLevel": "medium"})
    assert [p.source for p in change_points.change_points(config_dir)] == ["apply"]


def test_no_changes_means_no_latest(tmp_path):
    assert change_points.latest(tmp_path) is None


def test_an_older_apply_without_recorded_changes_names_keys_from_its_backup(tmp_path):
    """Applies from before changes were recorded: the keys come from the
    backed-up file against the file now."""
    config_dir, result = _apply(tmp_path, {"effortLevel": "medium"})
    manifest_path = config_dir / "backups" / result.ts / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["entries"]:
        entry.pop("changes", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    [point] = change_points.change_points(config_dir)
    assert point.keys == ["effortLevel"]


# -- metrics capture changes ---------------------------------------------------


def test_each_capture_change_is_a_change_point(tmp_path):
    from datetime import datetime, timezone

    from claude_token_lens import config as config_mod

    config_mod.set_capture(tmp_path, level="essentials", now=datetime(2026, 9, 1, 9, tzinfo=timezone.utc))
    config_mod.set_capture(tmp_path, level="standard", now=datetime(2026, 9, 8, 9, tzinfo=timezone.utc))
    config_mod.set_capture(tmp_path, sample=50, now=datetime(2026, 9, 9, 9, tzinfo=timezone.utc))
    config_mod.set_capture(tmp_path, level="off", now=datetime(2026, 9, 15, 9, tzinfo=timezone.utc))
    points = change_points.change_points(tmp_path)
    assert [p.source for p in points] == ["capture"] * 4
    assert [p.label for p in points] == [
        "Turned metrics capture on: Essentials",
        "Metrics capture level: Standard",
        "Changed metrics capture",
        "Turned metrics capture off",
    ]
    assert points[0].keys == ["capture.level"]
    assert points[0].changes == [{"key": "capture.level", "agent": None, "old": "off", "new": "essentials"}]
    assert points[2].keys == ["capture.sample"]
    assert change_points.latest(tmp_path).label == "Turned metrics capture off"


def test_a_broken_capture_log_line_is_skipped(tmp_path):
    (tmp_path / "capture-log.jsonl").write_text(
        'not json\n{"ts": "2026-09-01T09:00:00+00:00", "level": "free", "changed": {}}\n'
        '{"ts": "2026-09-02T09:00:00+00:00", "level": "free", "changed": {"level": {"from": "off", "to": "free"}}}\n',
        encoding="utf-8",
    )
    [point] = change_points.change_points(tmp_path)
    assert point.label == "Turned metrics capture on: Free"
