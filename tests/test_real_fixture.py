"""Real-corpus regression fixture (WP12a deliverable 3): a genuine Claude
Code session, scrubbed by ``tools/scrub.py`` from a real transcript into
``tests/fixtures/real/session-a/``, exercised end to end through
``discovery`` + ``parse_transcript``.

Skipped entirely when the fixture is absent (e.g. a checkout that hasn't
pulled the committed fixture yet) rather than failing, per the plan.

This file's own privacy regex scan is deliberately independent of
``tools/scrub.py --verify`` (which the fixture was already checked
against when it was produced): it re-derives the same shapes from
scratch here so a future change to ``scrub.py`` can't silently weaken
both checks at once.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from claudeglass import discovery
from claudeglass.model import EventKind, TranscriptMeta
from claudeglass.parse import parse_transcript

from helpers import assert_privacy

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "real" / "session-a"

pytestmark = pytest.mark.skipif(
    not FIXTURE_DIR.exists() or not any(FIXTURE_DIR.glob("*.jsonl")),
    reason="tests/fixtures/real/session-a/ not present (real fixture not checked out)",
)


def _load_fixture():
    top_paths = list(FIXTURE_DIR.glob("*.jsonl"))
    assert len(top_paths) == 1, f"expected exactly one top-level jsonl, found {top_paths}"
    top_path = top_paths[0]
    session_id = top_path.stem

    top_meta = TranscriptMeta(path=str(top_path), kind="top-level", session_id=session_id)
    top_result = parse_transcript(top_path, top_meta)

    sub_results = []
    for jsonl_path, _meta_dict in discovery.find_subagents(FIXTURE_DIR, session_id):
        meta_path = jsonl_path.with_name(jsonl_path.stem + ".meta.json")
        meta = discovery.load_meta(meta_path)
        sub_results.append(parse_transcript(jsonl_path, meta))

    return top_result, sub_results


def test_real_fixture_parses_with_zero_unparsable_lines():
    top_result, sub_results = _load_fixture()
    assert top_result.diagnostics.unparsable_lines == 0
    for sub in sub_results:
        assert sub.diagnostics.unparsable_lines == 0


def test_real_fixture_has_at_least_one_compaction():
    top_result, _sub_results = _load_fixture()
    compactions = sum(1 for e in top_result.events if e.kind == EventKind.COMPACT_BOUNDARY)
    assert compactions >= 1


def test_real_fixture_has_at_least_three_subagents():
    _top_result, sub_results = _load_fixture()
    assert len(sub_results) >= 3


def test_real_fixture_cache_creation_split_always_sums_to_the_flat_total():
    top_result, sub_results = _load_fixture()
    mismatches = []
    for result in [top_result] + sub_results:
        for turn in result.turns:
            if turn.cc_5m + turn.cc_1h != turn.cache_creation_tokens:
                mismatches.append((result.meta.session_id, turn.turn_index, turn.cc_5m, turn.cc_1h, turn.cache_creation_tokens))
    assert mismatches == [], mismatches


def test_real_fixture_passes_dataclass_privacy_scan():
    top_result, sub_results = _load_fixture()
    for result in [top_result] + sub_results:
        assert_privacy(result)


# -- independent raw-file regex scan (never trusts scrub.py's own check) --

_LEAK_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"[A-Za-z]:[\\/]"), "drive path"),
    (re.compile(r"/home/"), "/home/ path"),
    (re.compile(r"/Users/"), "/Users/ path"),
    (re.compile(r"\\Users\\"), "\\Users\\ path"),
    (re.compile(r"/[a-zA-Z]/"), "MSYS drive path"),
)

#: The A2 tag prefixes are deliberately preserved by scrub.py -- a match
#: against one of these literal, non-secret words is not a leak.
_ALLOWED_PREFIXES = (
    "<task-notification",
    "<command-name",
    "<local-command-stdout",
    "<local-command-caveat",
    "<scheduled-task",
    "<<autonomous-loop",
    "[SYSTEM NOTIFICATION",
    "[Request interrupted",
    "This session is being continued",
)


def _iter_all_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_all_strings(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_all_strings(item)


def test_real_fixture_files_never_contain_a_path_shaped_string():
    violations: list[str] = []
    for path in FIXTURE_DIR.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix == ".jsonl":
            for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    violations.append(f"{path.name}:{line_no}: invalid JSON")
                    continue
                for value in _iter_all_strings(d):
                    if any(value.startswith(p) for p in _ALLOWED_PREFIXES):
                        continue
                    for pattern, label in _LEAK_PATTERNS:
                        if pattern.search(value):
                            violations.append(f"{path.name}:{line_no}: {label}")
        elif path.suffix == ".json":
            try:
                d = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except ValueError:
                continue
            for value in _iter_all_strings(d):
                if any(value.startswith(p) for p in _ALLOWED_PREFIXES):
                    continue
                for pattern, label in _LEAK_PATTERNS:
                    if pattern.search(value):
                        violations.append(f"{path.name}: {label}")
    assert violations == [], violations


def test_real_fixture_manifest_has_expected_shape():
    manifest_path = FIXTURE_DIR / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["subagent_count"] >= 3
    assert isinstance(manifest["line_type_counts"], dict)
    assert manifest["scrub_tool_version"] >= 1


def test_real_fixture_stays_under_the_15mb_size_cap():
    total = sum(p.stat().st_size for p in FIXTURE_DIR.rglob("*") if p.is_file())
    assert total <= 15 * 1024 * 1024, total
