"""Tests for ``service.rebuild.corpus_from_store``: the round trip that
lets a report be built from the SQLite store alone, with no transcript
files on disk (see that module's own docstring for why this matters —
it's what lets the store outlive Claude Code's own transcript
retention).

The headline test (``test_watcher_then_rebuild_matches_fresh_corpus_report``)
is the round-trip requirement from this work package's brief: load
``tests/fixtures/real/session-a`` and every ``tests/fixtures/diversity/*``
fixture as a fresh :class:`~claude_token_lens.corpus.Corpus`, build a
report from it, then separately copy the same fixture files into a
temporary ``projects_root``, run one :class:`FileWatcher` tick into a
temporary store, rebuild a corpus from *that* store with
``corpus_from_store``, and build a second report. The two reports' JSON
renderings must be byte-for-byte identical once ``meta.generated_at`` is
zeroed in both (the one field that's allowed, expected, to differ, since
each ``build_report`` call stamps it with the current wall-clock time).

Both fixture trees are skipped (not failed) when absent, matching the
existing convention in ``tests/test_real_fixture.py``/``test_report.py``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from claude_token_lens.config import Config
from claude_token_lens.corpus import load_corpus
from claude_token_lens.pricing import load_pricing
from claude_token_lens.report import build_report
from claude_token_lens.render.json_out import render_json
from claude_token_lens.service.contracts import ServeOptions
from claude_token_lens.service.rebuild import corpus_from_store
from claude_token_lens.service.store import Store
from claude_token_lens.service.watcher import FileWatcher

from helpers import assert_privacy, turn_line, write_jsonl

PRICING = load_pricing()

REAL_DIR = Path(__file__).parent / "fixtures" / "real" / "session-a"
DIVERSITY_ROOT = Path(__file__).parent / "fixtures" / "diversity"

_DIVERSITY_DIRS = sorted(DIVERSITY_ROOT.glob("*")) if DIVERSITY_ROOT.exists() else []

pytestmark = pytest.mark.skipif(
    not REAL_DIR.exists() or not any(REAL_DIR.glob("*.jsonl")) or not _DIVERSITY_DIRS,
    reason="tests/fixtures/real/session-a or tests/fixtures/diversity/* not present",
)


def _fixture_project_dirs() -> list[Path]:
    return [REAL_DIR, *_DIVERSITY_DIRS]


def _zero_generated_at(rendered: str) -> dict:
    data = json.loads(rendered)
    data["report"]["meta"]["generated_at"] = ""
    return data


def test_watcher_then_rebuild_matches_fresh_corpus_report(tmp_path: Path):
    project_dirs = _fixture_project_dirs()
    slugs = tuple(d.name for d in project_dirs)

    fresh_corpus = load_corpus(project_dirs)
    fresh_report = build_report(fresh_corpus, PRICING, Config(), projects=slugs, window="round trip")
    fresh_json = _zero_generated_at(render_json(fresh_report))

    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    for project_dir in project_dirs:
        shutil.copytree(project_dir, projects_root / project_dir.name)

    config_dir = tmp_path / "config"
    options = ServeOptions(projects_root=projects_root, config_dir=config_dir)
    store = Store(":memory:")
    store.open()
    watcher = FileWatcher(store, options)
    stats = watcher.run_once()
    assert_privacy(stats)
    assert stats.errors == 0

    rebuilt_corpus = corpus_from_store(store)
    rebuilt_report = build_report(rebuilt_corpus, PRICING, Config(), projects=slugs, window="round trip")
    rebuilt_json = _zero_generated_at(render_json(rebuilt_report))

    assert rebuilt_json == fresh_json


def test_corpus_from_store_window_filters_like_discovery(tmp_path: Path):
    """A session outside the requested window is excluded, the same way
    ``discovery.find_sessions(..., days=N)`` would never have surfaced it
    to a fresh ``load_corpus`` call."""
    root = tmp_path / "projects"
    project_dir = root / "proj-a"
    project_dir.mkdir(parents=True)
    old_path = project_dir / "sess-old.jsonl"
    new_path = project_dir / "sess-new.jsonl"
    write_jsonl(old_path, [turn_line(timestamp="2020-01-01T00:00:00.000Z")])
    write_jsonl(new_path, [turn_line(timestamp="2026-09-18T00:00:00.000Z")])

    import os
    import time

    old_mtime = time.time() - 10 * 365 * 24 * 3600
    os.utime(old_path, (old_mtime, old_mtime))
    new_mtime = time.time() - 3600
    os.utime(new_path, (new_mtime, new_mtime))

    options = ServeOptions(projects_root=root, config_dir=tmp_path / "config")
    store = Store(":memory:")
    store.open()
    watcher = FileWatcher(store, options)
    watcher.run_once()

    corpus_all = corpus_from_store(store)
    assert {b.session_id for b in corpus_all.sessions} == {"sess-old", "sess-new"}

    corpus_recent = corpus_from_store(store, days=30)
    assert {b.session_id for b in corpus_recent.sessions} == {"sess-new"}


def test_corpus_from_store_skips_session_with_no_stored_transcripts(tmp_path: Path):
    """A ``sessions`` row with no matching ``transcripts`` rows at all
    (shouldn't normally arise from the watcher, but is cheap to guard) is
    skipped rather than producing a bundle with ``top=None``."""
    store = Store(":memory:")
    store.open()
    store.upsert_session(session_id="ghost", project_slug="proj-a", slug="proj-a")

    corpus = corpus_from_store(store)
    assert corpus.sessions == []
