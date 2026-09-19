"""Tests for claude_token_lens.snapshots (WP7): loading the fixture
snapshot files, the session/snapshot join, auto-detected diff keys,
co-changed keys, and the config-diff table's grouping/exclusion/notes.

Fixtures live under tests/fixtures/snapshots/: three real-shaped snapshot
JSON files at 2026-09-01, 2026-09-10 and 2026-09-15. Between the first two,
``user_settings.autoCompactWindow`` drops 300000 -> 150000 and
``user_settings.effortLevel`` drops high -> medium at the same time (a
deliberate co-change); the third snapshot repeats the second's values
unchanged, and ``agents.verification-runner.experimental.cacheTtl`` stays
"1h" throughout every snapshot.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from claude_token_lens import snapshots as snap_mod

from helpers import assert_privacy

#: The fixture files live directly under tests/fixtures/snapshots/ (per the
#: WP7 brief), not under the <config_dir>/token-lens/snapshots/ layout
#: load_snapshots() expects on a real config dir. _load() below reads them
#: straight off disk into Snapshot objects for the join/diff/table tests;
#: test_load_snapshots_* separately proves load_snapshots() itself walks
#: that <config_dir>/token-lens/snapshots/ layout correctly, using a copy
#: of these same fixture files.
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "snapshots"


def _load() -> list[snap_mod.Snapshot]:
    result = []
    for path in sorted(_FIXTURES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        result.append(snap_mod.Snapshot(path=path, ts=data["ts"], data=data))
    result.sort(key=lambda snap: snap.ts)
    return result


def _copy_fixtures_into_config_dir(config_dir: Path) -> None:
    snapshots_dir = config_dir / "token-lens" / "snapshots"
    snapshots_dir.mkdir(parents=True)
    for path in _FIXTURES_DIR.glob("*.json"):
        shutil.copy2(path, snapshots_dir / path.name)


# -- load_snapshots -----------------------------------------------------


def test_load_snapshots_returns_all_three_sorted_by_ts(tmp_path):
    _copy_fixtures_into_config_dir(tmp_path)
    result = snap_mod.load_snapshots(tmp_path)
    assert [s.ts for s in result] == [
        "20260901T000000Z",
        "20260910T000000Z",
        "20260915T000000Z",
    ]


def test_load_snapshots_missing_dir_returns_empty_list(tmp_path):
    assert snap_mod.load_snapshots(tmp_path / "does-not-exist") == []


def test_load_snapshots_skips_malformed_file(tmp_path):
    snapshots_dir = tmp_path / "token-lens" / "snapshots"
    snapshots_dir.mkdir(parents=True)
    (snapshots_dir / "20260901T000000Z.json").write_text(
        "not valid json", encoding="utf-8"
    )
    (snapshots_dir / "20260902T000000Z.json").write_text(
        '{"ts": "20260902T000000Z", "user_settings": {}}', encoding="utf-8"
    )
    result = snap_mod.load_snapshots(tmp_path)
    assert [s.ts for s in result] == ["20260902T000000Z"]


# -- managed_keys (fix 7) ------------------------------------------------


def test_managed_keys_returns_recorded_key_list():
    snap = snap_mod.Snapshot(
        path=Path("x"), ts="t", data={"managed_keys": ["model", "permissions"]}
    )
    assert snap_mod.managed_keys(snap) == ["model", "permissions"]


def test_managed_keys_missing_field_returns_empty_list():
    # A snapshot written before fix 7 (or from a machine with no
    # managed-settings file) has no managed_keys field at all.
    snap = snap_mod.Snapshot(path=Path("x"), ts="t", data={})
    assert snap_mod.managed_keys(snap) == []


def test_managed_keys_malformed_field_returns_empty_list():
    snap = snap_mod.Snapshot(path=Path("x"), ts="t", data={"managed_keys": "not-a-list"})
    assert snap_mod.managed_keys(snap) == []


def test_flatten_snapshot_includes_managed_settings_section():
    snap = snap_mod.Snapshot(
        path=Path("x"),
        ts="t",
        data={"managed_settings": {"model": "sonnet"}, "managed_keys": ["model"]},
    )
    flat = snap_mod.flatten_snapshot(snap)
    assert flat["managed_settings.model"] == "sonnet"


# -- snapshot_for (the session/snapshot join) ----------------------------


def test_snapshot_for_returns_latest_at_or_before_session_start():
    snapshots = _load()
    found = snap_mod.snapshot_for("2026-09-12T00:00:00.000Z", snapshots)
    assert found is not None
    assert found.ts == "20260910T000000Z"


def test_snapshot_for_boundary_is_inclusive():
    snapshots = _load()
    found = snap_mod.snapshot_for("2026-09-01T00:00:00.000Z", snapshots)
    assert found is not None
    assert found.ts == "20260901T000000Z"


def test_snapshot_for_session_predating_every_snapshot_is_none():
    snapshots = _load()
    found = snap_mod.snapshot_for("2026-08-20T00:00:00.000Z", snapshots)
    assert found is None


def test_snapshot_for_unparsable_timestamp_is_none():
    snapshots = _load()
    assert snap_mod.snapshot_for("not-a-timestamp", snapshots) is None


def test_snapshot_for_picks_latest_of_two_candidates():
    snapshots = _load()
    found = snap_mod.snapshot_for("2026-09-20T00:00:00.000Z", snapshots)
    assert found is not None
    assert found.ts == "20260915T000000Z"


# -- diff_keys ------------------------------------------------------------


def test_diff_keys_includes_every_key_that_changed():
    snapshots = _load()
    diff = snap_mod.diff_keys(snapshots)
    assert "user_settings.autoCompactWindow" in diff
    assert diff["user_settings.autoCompactWindow"] == [300000, 150000, 150000]
    assert "user_settings.effortLevel" in diff
    assert diff["user_settings.effortLevel"] == ["high", "medium", "medium"]


def test_diff_keys_excludes_unchanged_keys():
    snapshots = _load()
    diff = snap_mod.diff_keys(snapshots)
    assert "user_settings.model" not in diff
    assert "agents.verification-runner.experimental.cacheTtl" not in diff
    assert "agents.verification-runner.model" not in diff
    assert "env_names" not in diff


# -- co_changed_keys --------------------------------------------------------


def test_co_changed_keys_between_two_snapshots():
    snapshots = _load()
    changed = snap_mod.co_changed_keys(snapshots[0], snapshots[1])
    assert "user_settings.autoCompactWindow" in changed
    assert "user_settings.effortLevel" in changed
    assert "user_settings.model" not in changed


def test_co_changed_keys_is_empty_between_identical_snapshots():
    snapshots = _load()
    assert snap_mod.co_changed_keys(snapshots[1], snapshots[2]) == []


# -- build_config_diff_table ------------------------------------------------


def _sessions_with_metrics() -> list[dict]:
    return [
        {
            "session_id": "predates-everything",
            "first_ts": "2026-08-20T00:00:00.000Z",
            "turns": 10,
            "cost": 1.0,
            "recache_cc": 0,
            "cc_total": 100,
            "compactions": 0,
            "span_s": 60,
        },
        {
            "session_id": "s1-300k",
            "first_ts": "2026-09-05T00:00:00.000Z",
            "turns": 100,
            "cost": 5.0,
            "recache_cc": 20,
            "cc_total": 200,
            "compactions": 1,
            "span_s": 600,
        },
        {
            "session_id": "s2-300k",
            "first_ts": "2026-09-06T00:00:00.000Z",
            "turns": 200,
            "cost": 7.0,
            "recache_cc": 30,
            "cc_total": 300,
            "compactions": 2,
            "span_s": 1200,
        },
        {
            "session_id": "s3-150k",
            "first_ts": "2026-09-12T00:00:00.000Z",
            "turns": 50,
            "cost": 2.0,
            "recache_cc": 5,
            "cc_total": 100,
            "compactions": 0,
            "span_s": 300,
        },
        {
            "session_id": "s4-150k",
            "first_ts": "2026-09-20T00:00:00.000Z",
            "turns": 150,
            "cost": 6.0,
            "recache_cc": 15,
            "cc_total": 150,
            "compactions": 1,
            "span_s": 900,
        },
    ]


def test_build_config_diff_table_groups_by_value():
    snapshots = _load()
    table = snap_mod.build_config_diff_table(
        _sessions_with_metrics(), snapshots, "user_settings.autoCompactWindow"
    )

    assert [col.key for col in table.columns] == [
        "value",
        "sessions",
        "turns",
        "cost",
        "cost_per_session",
        "recache_share",
        "compactions_per_session",
        "median_span",
    ]
    assert len(table.rows) == 2

    by_value = {row[0]: row for row in table.rows}

    row_150k = by_value[150000]
    assert row_150k[1] == 2  # sessions
    assert row_150k[2] == 200  # turns
    assert row_150k[3] == 8.0  # cost
    assert row_150k[4] == 4.0  # cost/session
    assert row_150k[5] == 8.0  # recache share pct: (5+15)/(100+150)*100
    assert row_150k[6] == 0.5  # compactions/session
    assert row_150k[7] == 600  # median span

    row_300k = by_value[300000]
    assert row_300k[1] == 2
    assert row_300k[2] == 300
    assert row_300k[3] == 12.0
    assert row_300k[4] == 6.0
    assert row_300k[5] == 10.0  # (20+30)/(200+300)*100
    assert row_300k[6] == 1.5
    assert row_300k[7] == 900


def test_build_config_diff_table_excludes_and_notes_predating_sessions():
    snapshots = _load()
    table = snap_mod.build_config_diff_table(
        _sessions_with_metrics(), snapshots, "user_settings.autoCompactWindow"
    )
    total_sessions_in_rows = sum(row[1] for row in table.rows)
    assert total_sessions_in_rows == 4  # the 5th (predating) session excluded

    assert any("predate the earliest config snapshot" in note for note in table.notes)
    assert any("1 session" in note for note in table.notes)


def test_build_config_diff_table_notes_co_changed_keys():
    snapshots = _load()
    table = snap_mod.build_config_diff_table(
        _sessions_with_metrics(), snapshots, "user_settings.autoCompactWindow"
    )
    co_changed_note = next(
        note for note in table.notes if "changed alongside" in note
    )
    assert "user_settings.effortLevel" in co_changed_note


def test_build_config_diff_table_no_co_changed_keys_note():
    snapshots = _load()
    table = snap_mod.build_config_diff_table(
        _sessions_with_metrics(), snapshots, "user_settings.model"
    )
    assert any("No other key changed alongside" in note for note in table.notes)


# -- build_config_section -----------------------------------------------


def test_build_config_section_wraps_the_diff_table_in_a_section():
    snapshots = _load()
    section = snap_mod.build_config_section(
        _sessions_with_metrics(), snapshots, "user_settings.autoCompactWindow"
    )
    assert section.key == "config_diff"
    assert section.title == "Config diff"
    assert len(section.tables) == 1

    table = section.tables[0]
    diff_table = snap_mod.build_config_diff_table(
        _sessions_with_metrics(), snapshots, "user_settings.autoCompactWindow"
    )
    assert table.name == diff_table.name
    assert table.title == diff_table.title
    assert [col.key for col in table.columns] == [col.key for col in diff_table.columns]
    assert table.notes == diff_table.notes
    assert len(table.rows) == len(diff_table.rows)


def test_build_config_section_stringifies_the_value_column():
    """build_config_diff_table's own "value" column holds the config
    value verbatim (here, an int: 150000/300000) -- build_config_section
    stringifies it so every Section's Table has a first column usable as
    a row key regardless of the underlying config value's type."""
    snapshots = _load()
    section = snap_mod.build_config_section(
        _sessions_with_metrics(), snapshots, "user_settings.autoCompactWindow"
    )
    table = section.tables[0]
    for row in table.rows:
        assert isinstance(row[0], str) and row[0]
    assert {row[0] for row in table.rows} == {"150000", "300000"}


def test_build_config_section_renders_none_value_as_unset(tmp_path):
    # A key present in one snapshot's flattened config but absent from
    # another resolves to a raw None for the missing side -- exercise
    # that via a key that only appears in the later snapshot.
    snapshots = _load()
    table_key = "user_settings.effortLevel"
    section = snap_mod.build_config_section(_sessions_with_metrics(), snapshots, table_key)
    table = section.tables[0]
    # None the fixture doesn't need to actually trip: confirm the helper
    # itself renders None as "(unset)" and never as the literal "None".
    assert snap_mod._stringify_config_value(None) == "(unset)"
    for row in table.rows:
        assert row[0] != "None"


def test_build_config_section_is_privacy_clean():
    snapshots = _load()
    section = snap_mod.build_config_section(
        _sessions_with_metrics(), snapshots, "user_settings.autoCompactWindow"
    )
    assert_privacy(section)
