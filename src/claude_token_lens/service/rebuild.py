"""Reconstruct a :class:`~claude_token_lens.corpus.Corpus` from a
:class:`~claude_token_lens.service.store.Store` alone, with no transcript
files on disk.

This is what lets the store outlive Claude Code's own
``cleanupPeriodDays`` transcript retention: once ``FileWatcher`` (see
``service/watcher.py``) has folded a transcript's
``TranscriptResult`` into ``transcripts.digest_json`` (via
``cache.encode_result`` — the exact same encoding
``cache.DigestCache`` uses on disk, per that column's own docstring in
``service/schema.py``), the original ``.jsonl`` file is no longer needed
to answer a report query against that window: :func:`corpus_from_store`
decodes every stored digest back into a full ``TranscriptResult``
(``cache.result_from_jsonable`` — a lossless round trip, the same one
``tests/test_cache.py`` already asserts for the on-disk cache) and
reassembles them into the same ``SessionBundle``/``Corpus`` shape
``corpus.load_corpus`` would have produced from the live files, so
``report.build_report(corpus, ...)`` runs unmodified against either.

**Workflow runs** (S1-integration fix 1.d): ``SessionBundle.workflows``
is rebuilt from the ``workflow_runs`` table the watcher now populates
(``workflows.parse_workflow_file``/``link_workflow_agents``, already
cost-linked at write time). One deliberate, documented approximation
survives: ``workflow_runs.phases`` stores only ``WorkflowRun
.phase_titles`` (names — never ``detail``, which carries workflow
source/prompt text), so a rebuilt ``WorkflowRun.phases`` count is
``len(phase_titles)`` rather than the fresh parse's own count of *every*
phase entry in the run file — the two differ only when some phase entry
in the original file had no ``title`` at all, which none of this work
package's fixtures (including the new ``tests/fixtures/diversity/
workflow-session``) do, so the round-trip test still matches byte for
byte.

What else does NOT round-trip, and why:

- **``SessionBundle.project_dir``.** Always ``""`` here — nothing in
  ``report.build_report``'s own code path reads it (grepped: only
  ``corpus.py`` itself references ``.project_dir``), so this is a
  no-op loss, not a reportable one.
- Everything else on ``TranscriptResult``/``TranscriptMeta`` (including
  the three provenance fields ``schema.py`` singles out as
  store-internal-only — ``path``, seen here only for grouping rows by
  ``session_id``/``kind``, never copied into a rebuilt dataclass field)
  round-trips exactly, because ``digest_json`` already *is* the encoded
  form of the whole dataclass, not a lossy summary of it.
"""

from __future__ import annotations

import json

from ..cache import result_from_jsonable
from ..corpus import Corpus, SessionBundle, _session_sort_key
from ..discovery import _resolve_window
from ..model import WorkflowRun
from .store import Store


def _window_ts(top_row, window_by: str):
    """The same "window key" concept ``discovery._session_window_ts``
    computes from a live file, derived instead from what the store
    already has for that transcript.

    ``window_by="mtime"`` uses the stored ``transcripts.mtime_ns`` for
    the session's top-level transcript (exact match to the live-file
    behaviour). ``window_by="timestamp"`` is necessarily an
    approximation: the store has no raw JSONL lines left to find "the
    first user/assistant line's timestamp" from, so this uses the
    earliest turn timestamp across the (already-decoded) top transcript
    instead — the same value :func:`~claude_token_lens.corpus
    ._session_first_ts` would compute for the reassembled bundle, just
    computed one transcript early so it can gate inclusion before the
    rest of the session's transcripts are even decoded.
    """
    if window_by == "mtime":
        from datetime import datetime, timezone

        mtime_ns = top_row["mtime_ns"]
        if mtime_ns is None:
            return None
        return datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=timezone.utc)
    if window_by == "timestamp":
        top = result_from_jsonable(json.loads(top_row["digest_json"]))
        for turn in top.turns:
            if turn.ts:
                from datetime import datetime

                try:
                    return datetime.fromisoformat(turn.ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
        return None
    raise ValueError(f"unknown window_by: {window_by!r}")


def corpus_from_store(
    store: Store,
    *,
    days: int | None = None,
    since: str | None = None,
    until: str | None = None,
    window_by: str = "mtime",
) -> Corpus:
    """Rebuild a :class:`Corpus` entirely from ``store`` — no transcript
    files read. See the module docstring for what this makes possible
    and the two fields that don't survive the round trip.

    ``days``/``since``/``until``/``window_by`` mirror
    ``discovery.find_sessions``'s own parameters (same
    ``discovery._resolve_window`` resolution): a session whose top-level
    transcript falls outside the resolved window is skipped, exactly as
    it would never have been discovered by a fresh ``load_corpus`` call
    over the same window. A session with no stored top-level transcript
    at all (shouldn't normally happen — ``FileWatcher`` always upserts
    one alongside any of a session's subagents) is skipped rather than
    guessed at, matching ``report.build_report``'s own
    ``if top is None: continue`` posture for a bundle with no top.
    """
    conn = store._connection()
    since_dt, until_dt = _resolve_window(days, since, until)
    has_window_filter = since_dt is not None or until_dt is not None

    session_rows = conn.execute("SELECT id, slug FROM sessions").fetchall()

    bundles: list[SessionBundle] = []
    total_bytes = 0
    total_files = 0

    for session_row in session_rows:
        session_id = session_row["id"]
        slug = session_row["slug"]

        transcript_rows = conn.execute(
            "SELECT kind, digest_json, mtime_ns, size_bytes FROM transcripts "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        if not transcript_rows:
            continue

        top_row = next((row for row in transcript_rows if row["kind"] == "top-level"), None)
        if top_row is None:
            continue

        if has_window_filter:
            window_ts = _window_ts(top_row, window_by)
            if window_ts is None:
                continue
            if since_dt is not None and window_ts < since_dt:
                continue
            if until_dt is not None and window_ts > until_dt:
                continue

        top = result_from_jsonable(json.loads(top_row["digest_json"]))
        subs = [
            result_from_jsonable(json.loads(row["digest_json"]))
            for row in transcript_rows
            if row["kind"] != "top-level"
        ]

        for row in transcript_rows:
            total_files += 1
            total_bytes += row["size_bytes"] or 0

        workflow_rows = conn.execute(
            "SELECT run_id, agent_count, phases, started, finished, cost, status "
            "FROM workflow_runs WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        workflow_runs = []
        for wrow in workflow_rows:
            try:
                phase_titles = tuple(json.loads(wrow["phases"]))
            except (TypeError, ValueError):
                phase_titles = ()
            workflow_runs.append(
                WorkflowRun(
                    run_id=wrow["run_id"],
                    session_id=session_id,
                    agent_count=wrow["agent_count"],
                    phases=len(phase_titles),
                    started=wrow["started"],
                    finished=wrow["finished"],
                    cost=wrow["cost"],
                    status=wrow["status"],
                    phase_titles=phase_titles,
                )
            )

        bundles.append(
            SessionBundle(
                session_id=session_id,
                slug=slug,
                top=top,
                subs=subs,
                workflows=workflow_runs,
                project_dir="",
            )
        )

    bundles.sort(key=_session_sort_key)

    return Corpus(
        sessions=bundles,
        total_files=total_files,
        total_bytes=total_bytes,
        cache_hits=0,
        cache_misses=0,
        elapsed_s=0.0,
    )


__all__ = ["corpus_from_store"]
