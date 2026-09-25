"""Ignored recommendations (``ignores.py``): kept per profile and per
project, and shown again once the recommendation suggests something else."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from claudeglass import ignores
from claudeglass.model import Recommendation, SettingChange

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _rec(key="compaction-window", value=100_000, saving="$12", agent=None, **kwargs) -> Recommendation:
    return Recommendation(
        id=key.split(":")[0],
        key=key,
        title="Compact sooner",
        estimated_saving=saving,
        changes=[SettingChange(key="autoCompactWindow", value=value, agent=agent)],
        **kwargs,
    )


def _marks(config_dir, recs, project=None):
    return ignores.annotate(recs, ignores.load(config_dir), ignores.active_profile(config_dir), project)


def test_the_fingerprint_follows_what_it_suggests_not_its_numbers():
    base = ignores.fingerprint(_rec())
    assert ignores.fingerprint(_rec(saving="$40", evidence=[("x", 1, "t", "r")], why="new words")) == base
    assert ignores.fingerprint(_rec(value=120_000)) != base
    assert ignores.fingerprint(_rec(agent="claude-implementer")) != base
    # No changes: its rule, agent type and lever.
    plain = Recommendation(id="spawn-shared-claude-md", key="spawn-shared-claude-md", agent_type="x")
    assert ignores.fingerprint(plain) != ignores.fingerprint(
        Recommendation(id="spawn-shared-claude-md", key="spawn-shared-claude-md", agent_type="y")
    )


def test_an_ignore_round_trips_and_stops(tmp_path):
    rec = _rec()
    assert _marks(tmp_path, [rec]) == [{"ignored": False, "ignored_at": None, "ignored_in": None, "ignored_before": None}]
    assert ignores.set_ignored(tmp_path, [rec], ignored=True, project=None, now=NOW) == "none"
    [mark] = _marks(tmp_path, [rec])
    assert mark["ignored"] is True and mark["ignored_at"] == "2026-09-20T12:00:00+00:00" and mark["ignored_in"] == "all"
    stored = json.loads((tmp_path / ignores.FILENAME).read_text(encoding="utf-8"))
    entry = stored["profiles"]["none"]["*"]["compaction-window"]
    assert entry["changes"] == [{"agent": None, "key": "autoCompactWindow", "value": 100_000}]
    assert entry["title"] == "Compact sooner"

    ignores.set_ignored(tmp_path, [rec], ignored=False, project=None)
    assert _marks(tmp_path, [rec])[0]["ignored"] is False
    assert json.loads((tmp_path / ignores.FILENAME).read_text(encoding="utf-8"))["profiles"] == {}


def test_every_project_ignore_applies_in_a_project_and_a_project_ignore_stays_there(tmp_path):
    everywhere, here = _rec("compaction-window"), _rec("ttl-1h", value="1h")
    ignores.set_ignored(tmp_path, [everywhere], ignored=True, project=None, now=NOW)
    ignores.set_ignored(tmp_path, [here], ignored=True, project="proj-a", now=NOW)
    in_a = _marks(tmp_path, [everywhere, here], project="proj-a")
    assert [m["ignored"] for m in in_a] == [True, True]
    assert [m["ignored_in"] for m in in_a] == ["all", "project"]
    assert [m["ignored"] for m in _marks(tmp_path, [everywhere, here], project="proj-b")] == [True, False]
    assert [m["ignored"] for m in _marks(tmp_path, [everywhere, here])] == [True, False]

    # Stopping from one project's view removes the entry that applied:
    # the every-project one, so it's back everywhere.
    ignores.set_ignored(tmp_path, [everywhere], ignored=False, project="proj-a")
    assert _marks(tmp_path, [everywhere], project="proj-b")[0]["ignored"] is False


def test_ignores_are_kept_per_profile(tmp_path):
    rec = _rec()
    ignores.set_ignored(tmp_path, [rec], ignored=True, project=None, now=NOW)
    (tmp_path / "active-profile").write_text("cost-saver\n", encoding="utf-8")
    assert ignores.active_profile(tmp_path) == "cost-saver"
    assert _marks(tmp_path, [rec])[0]["ignored"] is False
    ignores.set_ignored(tmp_path, [rec], ignored=True, project=None, now=NOW)
    (tmp_path / "active-profile").write_text("\n", encoding="utf-8")
    assert ignores.active_profile(tmp_path) == "none"
    assert _marks(tmp_path, [rec])[0]["ignored"] is True
    assert set(ignores.load(tmp_path)) == {"none", "cost-saver"}


def test_it_shows_again_once_it_suggests_something_else(tmp_path):
    ignores.set_ignored(tmp_path, [_rec(value=100_000)], ignored=True, project=None, now=NOW)
    [mark] = _marks(tmp_path, [_rec(value=120_000)])
    assert mark["ignored"] is False
    assert mark["ignored_before"] == {
        "ignored_at": "2026-09-20T12:00:00+00:00",
        "changes": [{"agent": None, "key": "autoCompactWindow", "value": 100_000}],
    }
    # A moving saving alone doesn't bring it back.
    assert _marks(tmp_path, [_rec(saving="$99")])[0]["ignored"] is True


def test_skip_keys_holds_only_what_still_matches(tmp_path):
    ignores.set_ignored(tmp_path, [_rec("a"), _rec("b")], ignored=True, project=None, now=NOW)
    recs = [_rec("a"), _rec("b", value=1), _rec("c")]
    assert ignores.skip_keys(tmp_path, recs, None) == frozenset({"a"})


def test_an_unreadable_file_means_nothing_is_ignored(tmp_path):
    (tmp_path / ignores.FILENAME).write_text("{not json", encoding="utf-8")
    assert ignores.load(tmp_path) == {}
    assert _marks(tmp_path, [_rec()])[0]["ignored"] is False
    # And the next ignore replaces it.
    ignores.set_ignored(tmp_path, [_rec()], ignored=True, project=None, now=NOW)
    assert _marks(tmp_path, [_rec()])[0]["ignored"] is True
