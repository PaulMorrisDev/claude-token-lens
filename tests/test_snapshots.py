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
#: WP7 brief), not under the <config_dir>/snapshots/ layout load_snapshots()
#: expects on a real config dir (config_dir being the token-lens directory
#: itself -- see load_snapshots's docstring, fix config-dir). _load() below
#: reads them straight off disk into Snapshot objects for the join/diff/table
#: tests; test_load_snapshots_* separately proves load_snapshots() itself
#: walks that <config_dir>/snapshots/ layout correctly, using a copy of
#: these same fixture files.
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "snapshots"


def _load() -> list[snap_mod.Snapshot]:
    result = []
    for path in sorted(_FIXTURES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        result.append(snap_mod.Snapshot(path=path, ts=data["ts"], data=data))
    result.sort(key=lambda snap: snap.ts)
    return result


def _copy_fixtures_into_config_dir(config_dir: Path) -> None:
    # Fix config-dir: config_dir is the token-lens directory itself
    # (matching every caller's convention now -- see load_snapshots's
    # docstring), so snapshots live directly under it, not nested one
    # more "token-lens" level down.
    snapshots_dir = config_dir / "snapshots"
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
    snapshots_dir = tmp_path / "snapshots"
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
        note for note in table.notes if "changed at the same time" in note
    )
    assert "effortLevel (your settings)" in co_changed_note


def test_build_config_diff_table_no_co_changed_keys_note():
    snapshots = _load()
    table = snap_mod.build_config_diff_table(
        _sessions_with_metrics(), snapshots, "user_settings.model"
    )
    assert any("No other setting changed" in note for note in table.notes)


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


# -- schema 2: effective_config / effective_provenance / layers -------------


def _schema2_snapshot(
    project_slug: str = "proj-a",
    effective: dict | None = None,
    effective_provenance: dict | None = None,
    settings_layers: dict | None = None,
    ts: str = "20260901T000000Z",
    **extra,
) -> snap_mod.Snapshot:
    data = {
        "schema": 2,
        "ts": ts,
        "project_slug": project_slug,
        "effective": effective if effective is not None else {},
        "effective_provenance": effective_provenance if effective_provenance is not None else {},
        "settings_layers": settings_layers if settings_layers is not None else {},
        **extra,
    }
    return snap_mod.Snapshot(path=Path(f"{ts}.json"), ts=ts, data=data)


def test_effective_config_returns_the_effective_field():
    snap = _schema2_snapshot(effective={"model": "sonnet", "effortLevel": "high"})
    assert snap_mod.effective_config(snap) == {"model": "sonnet", "effortLevel": "high"}


def test_effective_config_schema1_snapshot_is_empty():
    snap = snap_mod.Snapshot(path=Path("x"), ts="t", data={"schema": 1})
    assert snap_mod.effective_config(snap) == {}


def test_effective_provenance_returns_the_provenance_field():
    snap = _schema2_snapshot(effective_provenance={"model": "project_local"})
    assert snap_mod.effective_provenance(snap) == {"model": "project_local"}


def test_effective_provenance_schema1_snapshot_is_empty():
    snap = snap_mod.Snapshot(path=Path("x"), ts="t", data={"schema": 1})
    assert snap_mod.effective_provenance(snap) == {}


def test_layers_returns_the_settings_layers_field():
    snap = _schema2_snapshot(settings_layers={"user": {"present": True}})
    assert snap_mod.layers(snap) == {"user": {"present": True}}


def test_layers_schema1_snapshot_is_empty():
    snap = snap_mod.Snapshot(path=Path("x"), ts="t", data={"schema": 1})
    assert snap_mod.layers(snap) == {}


# -- schema 2: latest_snapshot_per_project -----------------------------------


def test_latest_snapshot_per_project_keeps_the_last_one_seen():
    older = _schema2_snapshot(project_slug="proj-a", ts="20260901T000000Z", effective={"model": "sonnet"})
    newer = _schema2_snapshot(project_slug="proj-a", ts="20260910T000000Z", effective={"model": "opus"})
    other = _schema2_snapshot(project_slug="proj-b", ts="20260905T000000Z", effective={"model": "fable"})
    latest = snap_mod.latest_snapshot_per_project([older, newer, other])
    assert set(latest) == {"proj-a", "proj-b"}
    assert latest["proj-a"] is newer
    assert latest["proj-b"] is other


def test_latest_snapshot_per_project_schema1_snapshots_collapse_to_one_bucket():
    a = snap_mod.Snapshot(path=Path("a"), ts="20260901T000000Z", data={"schema": 1, "ts": "20260901T000000Z"})
    b = snap_mod.Snapshot(path=Path("b"), ts="20260902T000000Z", data={"schema": 1, "ts": "20260902T000000Z"})
    latest = snap_mod.latest_snapshot_per_project([a, b])
    assert list(latest) == ["(unknown project)"]
    assert latest["(unknown project)"] is b


# -- schema 2: build_effective_config_table ----------------------------------


def test_build_effective_config_table_one_row_per_project_and_key():
    proj_a = _schema2_snapshot(
        project_slug="proj-a",
        effective={"model": "sonnet", "effortLevel": "high"},
        effective_provenance={"model": "user", "effortLevel": "managed"},
    )
    proj_b = _schema2_snapshot(
        project_slug="proj-b", ts="20260902T000000Z", effective={"model": "opus"}, effective_provenance={"model": "project_shared"}
    )
    table = snap_mod.build_effective_config_table([proj_a, proj_b])
    rows_by_key = {(row[0], row[1]): row for row in table.rows}
    assert rows_by_key[("proj-a", "model")][2:] == ["sonnet", "user"]
    assert rows_by_key[("proj-a", "effortLevel")][2:] == ["high", "managed"]
    assert rows_by_key[("proj-b", "model")][2:] == ["opus", "project_shared"]
    assert table.notes == []


def test_build_effective_config_table_notes_when_no_schema2_data():
    schema1_snap = snap_mod.Snapshot(path=Path("x"), ts="t", data={"schema": 1, "ts": "t"})
    table = snap_mod.build_effective_config_table([schema1_snap])
    assert table.rows == []
    assert any("No schema-2 effective config" in note for note in table.notes)


def test_build_effective_config_table_empty_input_has_no_notes():
    table = snap_mod.build_effective_config_table([])
    assert table.rows == []
    assert table.notes == []


# -- schema 2: build_config_layers_table -------------------------------------


def test_build_config_layers_table_one_row_per_layer_per_project():
    snap = _schema2_snapshot(
        project_slug="proj-a",
        settings_layers={
            "managed": {"present": True},
            "project_local": {"present": False},
            "project_shared": {"present": True},
            "user": {"present": True},
        },
        content_layers={
            "agents_summary": {"count": 2},
            "skills": {"project": {"names": ["a"]}, "user": {"names": ["b", "c"]}},
            "rules": {"count": 3},
            "commands": {"count": 1},
            "claude_md": {"user_bytes": 10, "project_root_bytes": 20, "project_local_bytes": None, "nested_bytes": 5},
        },
        mcp_servers={"names": ["filesystem", "github"]},
    )
    table = snap_mod.build_config_layers_table([snap])
    assert len(table.rows) == 4  # one row per SETTINGS_LAYER_NAMES entry
    by_layer = {row[1]: row for row in table.rows}
    managed_row = by_layer["managed"]
    assert managed_row[0] == "proj-a"
    assert managed_row[2] is True
    assert managed_row[3] == 2  # agents
    assert managed_row[4] == 3  # skills (1 project + 2 user)
    assert managed_row[5] == 3  # rules
    assert managed_row[6] == 35  # claude_md_bytes (10+20+5, None skipped)
    assert managed_row[7] == 1  # commands
    assert managed_row[8] == 2  # mcp servers
    assert by_layer["project_local"][2] is False


def test_build_config_layers_table_missing_content_layers_degrades_to_zero():
    snap = _schema2_snapshot(project_slug="proj-a")
    table = snap_mod.build_config_layers_table([snap])
    assert len(table.rows) == 4
    for row in table.rows:
        assert row[3:] == [0, 0, 0, 0, 0, 0]


# -- schema 2: build_config_groups_table -------------------------------------


def test_build_config_groups_table_groups_identical_effective_configs():
    proj_a = _schema2_snapshot(project_slug="proj-a", effective={"model": "sonnet"})
    proj_b = _schema2_snapshot(project_slug="proj-b", ts="20260902T000000Z", effective={"model": "sonnet"})
    proj_c = _schema2_snapshot(project_slug="proj-c", ts="20260903T000000Z", effective={"model": "opus"})
    table = snap_mod.build_config_groups_table([proj_a, proj_b, proj_c])
    assert len(table.rows) == 2
    by_count = sorted(table.rows, key=lambda r: -r[1])
    assert by_count[0][1] == 2
    assert by_count[0][2] == "proj-a, proj-b"
    assert by_count[1][1] == 1
    assert by_count[1][2] == "proj-c"


def test_build_config_groups_table_counts_sessions_per_group():
    proj_a = _schema2_snapshot(project_slug="proj-a", effective={"model": "sonnet"})
    sessions = [
        {"session_id": "s1", "first_ts": "2026-09-05T00:00:00.000Z"},
        {"session_id": "s2", "first_ts": "2026-09-06T00:00:00.000Z"},
    ]
    table = snap_mod.build_config_groups_table([proj_a], sessions)
    assert table.rows[0][3] == 2


def test_build_config_groups_table_no_snapshots_is_empty():
    table = snap_mod.build_config_groups_table([])
    assert table.rows == []


# -- schema 2: detect_drift / build_config_drift_table -----------------------


def test_detect_drift_finds_mismatched_keys():
    snap = _schema2_snapshot(effective={"model": "sonnet", "effortLevel": "high"})
    mismatches = snap_mod.detect_drift(snap, {"model": "opus", "effortLevel": "high"})
    assert mismatches == [("model", "sonnet", "opus")]


def test_detect_drift_skips_keys_absent_from_effective_config():
    snap = _schema2_snapshot(effective={"model": "sonnet"})
    mismatches = snap_mod.detect_drift(snap, {"unrelated_key": "value"})
    assert mismatches == []


def test_detect_drift_no_mismatch_returns_empty_list():
    snap = _schema2_snapshot(effective={"model": "sonnet"})
    assert snap_mod.detect_drift(snap, {"model": "sonnet"}) == []


def test_build_config_drift_table_one_row_per_mismatch():
    snapshots = [_schema2_snapshot(ts="20260901T000000Z", effective={"model": "sonnet"})]
    sessions_with_observed = [
        {"session_id": "s1", "first_ts": "2026-09-05T00:00:00.000Z", "observed": {"model": "opus"}},
    ]
    table = snap_mod.build_config_drift_table(sessions_with_observed, snapshots)
    assert len(table.rows) == 1
    assert table.rows[0] == ["s1", "model", "sonnet", "opus"]
    assert table.notes == []


def test_build_config_drift_table_excludes_sessions_predating_every_snapshot():
    snapshots = [_schema2_snapshot(ts="20260901T000000Z", effective={"model": "sonnet"})]
    sessions_with_observed = [
        {"session_id": "s1", "first_ts": "2026-08-01T00:00:00.000Z", "observed": {"model": "opus"}},
    ]
    table = snap_mod.build_config_drift_table(sessions_with_observed, snapshots)
    assert table.rows == []
    assert any("predate the earliest config snapshot" in note for note in table.notes)


def test_build_config_drift_table_no_drift_notes_it():
    snapshots = [_schema2_snapshot(ts="20260901T000000Z", effective={"model": "sonnet"})]
    sessions_with_observed = [
        {"session_id": "s1", "first_ts": "2026-09-05T00:00:00.000Z", "observed": {"model": "sonnet"}},
    ]
    table = snap_mod.build_config_drift_table(sessions_with_observed, snapshots)
    assert table.rows == []
    assert any("No drift detected" in note for note in table.notes)


# -- schema 2: claude_json_cross_check ----------------------------------------


def test_claude_json_cross_check_matches_by_session_id_and_finds_differences():
    snap = _schema2_snapshot(
        claude_json={
            "matched": True,
            "last_session": {
                "lastSessionId": "abc-123",
                "lastTotalInputTokens": 1000,
                "lastTotalOutputTokens": 200,
                "lastCost": 1.5,
            },
        }
    )
    result = snap_mod.claude_json_cross_check(
        snap,
        {
            "session_id": "abc-123",
            "input_tokens": 1000,
            "output_tokens": 999,  # deliberately mismatched
            "cost": 1.5,
        },
    )
    assert result["matched"] is True
    assert result["differences"] == {"output_tokens": (200, 999)}


def test_claude_json_cross_check_session_id_mismatch_is_unmatched():
    snap = _schema2_snapshot(
        claude_json={"matched": True, "last_session": {"lastSessionId": "abc-123"}}
    )
    result = snap_mod.claude_json_cross_check(snap, {"session_id": "different-session"})
    assert result == {"matched": False, "differences": {}}


def test_claude_json_cross_check_no_last_session_is_unmatched():
    snap = _schema2_snapshot(claude_json={"matched": False})
    result = snap_mod.claude_json_cross_check(snap, {"session_id": "abc-123"})
    assert result == {"matched": False, "differences": {}}


def test_claude_json_cross_check_missing_field_on_either_side_is_skipped():
    snap = _schema2_snapshot(
        claude_json={
            "matched": True,
            "last_session": {"lastSessionId": "abc-123", "lastTotalInputTokens": 1000},
        }
    )
    # observed has no "input_tokens" key at all -> skipped, not reported.
    result = snap_mod.claude_json_cross_check(snap, {"session_id": "abc-123"})
    assert result == {"matched": True, "differences": {}}


# -- schema 2: privacy over the new tables -----------------------------------


def test_schema2_tables_are_privacy_clean():
    snap = _schema2_snapshot(
        project_slug="proj-a",
        effective={"model": "sonnet"},
        effective_provenance={"model": "user"},
        settings_layers={"user": {"present": True, "source_path_hash": "sha256:aaaa"}},
        content_layers={"agents_summary": {"count": 1}},
        mcp_servers={"names": ["filesystem"]},
    )
    assert_privacy(snap_mod.build_effective_config_table([snap]))
    assert_privacy(snap_mod.build_config_layers_table([snap]))
    assert_privacy(snap_mod.build_config_groups_table([snap]))
    assert_privacy(
        snap_mod.build_config_drift_table(
            [{"session_id": "s1", "first_ts": "20260901T000000Z", "observed": {"model": "opus"}}],
            [snap],
        )
    )


def test_build_config_section_include_effective_appends_schema2_tables():
    snapshots = _load()
    section = snap_mod.build_config_section(
        _sessions_with_metrics(),
        snapshots,
        "user_settings.autoCompactWindow",
        include_effective=True,
    )
    assert [table.name for table in section.tables] == [
        f"config-diff-user_settings.autoCompactWindow",
        "effective-config",
        "config-layers",
        "config-groups",
    ]


def test_build_config_section_sessions_with_observed_appends_drift_table():
    snapshots = _load()
    section = snap_mod.build_config_section(
        _sessions_with_metrics(),
        snapshots,
        "user_settings.autoCompactWindow",
        sessions_with_observed=[
            {"session_id": "s1", "first_ts": "2026-09-05T00:00:00.000Z", "observed": {"unrelated": "x"}}
        ],
    )
    assert section.tables[-1].name == "config-drift"


def test_build_config_section_without_new_kwargs_stays_one_table():
    # Backward compatibility: an existing caller passing only the three
    # positional arguments must still get exactly the diff table.
    snapshots = _load()
    section = snap_mod.build_config_section(
        _sessions_with_metrics(), snapshots, "user_settings.autoCompactWindow"
    )
    assert len(section.tables) == 1


def test_snapshot_for_with_project_key_ignores_other_projects():
    mine = snap_mod.Snapshot(path=None, ts="20260901T000000Z", data={"project_slug": "slug:aaa"})
    other = snap_mod.Snapshot(path=None, ts="20260910T000000Z", data={"project_slug": "slug:bbb"})
    legacy = snap_mod.Snapshot(path=None, ts="20260905T000000Z", data={})
    snaps = [mine, legacy, other]
    assert snap_mod.snapshot_for("2026-09-12T00:00:00Z", snaps, "slug:aaa") is legacy
    assert snap_mod.snapshot_for("2026-09-12T00:00:00Z", [mine, other], "slug:aaa") is mine
    assert snap_mod.snapshot_for("2026-09-12T00:00:00Z", snaps) is other
