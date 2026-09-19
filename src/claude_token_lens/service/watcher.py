"""``FileWatcher``: the polling loop that keeps the v0.2 service's
:class:`~claude_token_lens.service.store.Store` up to date with whatever
is currently under ``ServeOptions.projects_root``, implementing
``service.contracts.Watcher``.

Each :meth:`FileWatcher.run_once` tick:

1. Discovers every project directory under ``options.projects_root``
   (``discovery.resolve_project_dirs(..., all_projects=True,
   exclude_projects=options.exclude_projects)``), then every top-level
   session file, subagent transcript (ordinary and workflow-nested — see
   ``discovery.find_subagents``'s own docstring) and workflow run file
   under each, honouring the same four shapes ``discovery.py`` documents.
2. Diffs the discovered ``(path, mtime_ns, size_bytes)`` triples against
   ``Store.known_files()`` to decide, per file, whether to re-parse it
   this tick (see :meth:`FileWatcher._resolve`'s docstring for the exact
   new/changed/live decision table).
3. Folds every parsed (or previously-stored, for an unchanged file)
   transcript into the store via ``Store.upsert_transcript``, and every
   session's classification/cost totals via ``Store.upsert_session``.
4. Removes rows for files no longer on disk (``Store.remove_missing``),
   prunes old sessions when ``options.retention_days`` is set, and
   ingests any new config-snapshot file under
   ``options.config_dir/snapshots/`` (see :meth:`_scan_snapshots`).

Never raises out of :meth:`run_once` for a single bad file or session —
each is wrapped in its own ``try``/``except`` and recorded in
``WatcherStats.error_messages`` as a short, path-free message (privacy
rule restated from ``service/__init__.py``'s module docstring: no stat,
log line or exception message that escapes this module may ever contain
a path or transcript text).

S1-integration closed every contract gap this module originally
documented here (``ServeOptions.billing_mode``, ``Store.upsert_snapshot``
de-duplication, ``WorkflowRun`` persistence) — see ``service/contracts.py``,
``service/schema.py`` and ``service/store.py`` for the resulting shapes.
One attribution choice remains, carried over unchanged:

- The snapshot-config hook (``hooks/snapshot-config.py``) writes one
  global ``<config_dir>/snapshots/<ts>.json`` per machine, never one per
  project (only a redacted ``cwd_hash`` survives on the snapshot itself,
  never a usable project slug) — but ``Store.upsert_snapshot`` requires a
  ``project_slug``. Every snapshot is attributed to the synthetic project
  slug ``store.GLOBAL_PROJECT_SLUG`` rather than fabricating a false
  per-project association; ``Store.snapshots()`` maps that sentinel back
  to a ``None`` ``project_slug`` for any reader, so the attribution is
  never mistaken for a real project.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .. import PARSER_VERSION, classify, discovery, recache, workflows as workflows_mod, workstyle
from ..cache import DigestCache, encode_result, result_from_jsonable
from ..compaction import compaction_records_for_transcript
from ..model import TranscriptMeta, TranscriptResult
from ..parse import parse_transcript
from ..pricing import Pricing, PricingError, load_pricing, price_turn
from ..report import _dominant_transcript_model, _extract_workstyle_features
from .. import snapshots as snapshots_mod
from .contracts import ServeOptions, WatcherStats
from .store import GLOBAL_PROJECT_SLUG, Store

#: A file whose mtime is under this many seconds old is assumed to still
#: be an active Claude Code session (same convention/value as
#: ``cache.LIVE_FILE_WINDOW_S``, duplicated here rather than imported so
#: this module never has to import ``cache.DigestCache`` just for the
#: constant).
LIVE_FILE_WINDOW_S = 60.0


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _default_pricing() -> Pricing:
    """The packaged default rate card, used to price every turn folded
    into ``turns_agg``/``sessions.total_cost``. Mirrors ``corpus.py``'s
    own ``_default_rates`` (duplicated rather than imported — that
    function is module-private to ``corpus.py`` and this module has no
    other reason to import from it): a corrupted/missing packaged
    ``pricing.toml`` degrades to an all-unknown rate card (every
    ``price_turn`` call prices at zero, ``model_known=False``) rather
    than failing every watcher tick outright.
    """
    try:
        return load_pricing()
    except PricingError:
        return Pricing(
            path="none",
            version="none",
            currency="USD",
            source_url=None,
            retrieved=None,
            notes=None,
            sha256="",
        )


def _build_top_meta(top_path: Path, session_id: str, project_slug: str) -> TranscriptMeta:
    """Same construction as ``corpus.py``'s own (private)
    ``_build_top_meta`` — duplicated rather than imported, per this
    project's established convention for small cross-module helpers.
    """
    meta = TranscriptMeta(path=str(top_path), kind="top-level", session_id=session_id, project_slug=project_slug)
    try:
        stat = top_path.stat()
    except OSError:
        return meta
    meta.mtime_ns = stat.st_mtime_ns
    meta.size_bytes = stat.st_size
    return meta


def _priced_turns(result: TranscriptResult):
    return [t for t in result.turns if t.turn_index > 0]


def _parse_ts(ts: str | None):
    from datetime import datetime

    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _turn_day(turn) -> str:
    """The turn's own UTC calendar day, e.g. ``"2026-09-18"``. Unlike
    ``usage.py``'s ``_day_key`` (which buckets by ``config.tz``'s local
    day for the report's own Usage section), the watcher has no
    ``Config``/timezone to read (``ServeOptions`` carries none) — UTC is
    the only zone available without one, so ``turns_agg.day`` is a UTC
    calendar day, not a local one. ``Store.daily_usage`` inherits this.
    """
    dt = _parse_ts(turn.ts)
    if dt is None:
        return "unknown"
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d")
    from datetime import timezone

    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _build_turns_agg(result: TranscriptResult, pricing: Pricing) -> list[dict]:
    """Per-day, per-model rollups (``turns_agg`` rows) for one
    transcript's priced turns, cost via ``pricing.price_turn`` at default
    pricing (the packaged rate card — no per-project/CLI override is
    plumbed through ``ServeOptions``)."""
    buckets: dict[tuple[str, str], dict] = {}
    for turn in _priced_turns(result):
        model = turn.model or "<unknown>"
        key = (_turn_day(turn), model)
        bucket = buckets.setdefault(
            key,
            {
                "day": key[0],
                "model": model,
                "turns": 0,
                "input_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "cc_5m": 0,
                "cc_1h": 0,
                "cost": 0.0,
            },
        )
        resolved = pricing.resolve_model(turn.model)
        breakdown = price_turn(turn, resolved)
        bucket["turns"] += 1
        bucket["input_tokens"] += turn.input_tokens
        bucket["cache_creation_tokens"] += turn.cache_creation_tokens
        bucket["cache_read_tokens"] += turn.cache_read_tokens
        bucket["output_tokens"] += turn.output_tokens
        bucket["thinking_tokens"] += turn.thinking_tokens
        bucket["cc_5m"] += turn.cc_5m
        bucket["cc_1h"] += turn.cc_1h
        bucket["cost"] += breakdown.total
    return list(buckets.values())


def _build_recache_turns(result: TranscriptResult, thresholds: recache.RecacheThresholds) -> list[dict]:
    """``recache_turns`` rows for one transcript, via ``recache.detect``
    at default thresholds (``ServeOptions`` carries no threshold
    overrides — same posture as pricing above)."""
    flagged = recache.detect(result.turns, thresholds)
    return [
        {
            "turn_index": turn.turn_index,
            "signature": turn.recache_signature or "",
            "cache_creation_tokens": turn.cache_creation_tokens,
            "preceding_primary": turn.preceding_primary.value if turn.preceding_primary is not None else None,
            "gap_s": turn.gap_s,
        }
        for turn in flagged
    ]


def _build_events(result: TranscriptResult) -> list[dict]:
    return [
        {
            "kind": event.kind.value,
            "subkind": event.subkind,
            "ts": event.ts,
            "dropped_tokens": event.dropped_tokens,
            "duration_ms": event.duration_ms,
        }
        for event in result.events
    ]


def _build_compactions(
    result: TranscriptResult, pricing: Pricing, thresholds: recache.RecacheThresholds
) -> list[dict]:
    dominant_model = _dominant_transcript_model(result)
    rates = pricing.resolve_model(dominant_model) if dominant_model else None
    records = compaction_records_for_transcript(result, rates, thresholds)
    return [
        {
            "ts": record.ts,
            "pre_tokens": record.pre_tokens,
            "post_tokens": record.post_tokens,
            "dropped_tokens": record.dropped_tokens,
            "trigger": record.trigger,
            "join_delta_s": record.join_delta_s,
        }
        for record in records
        if record.ts is not None  # schema's compactions.ts is NOT NULL
    ]


class FileWatcher:
    """``contracts.Watcher`` implementation over one
    :class:`~claude_token_lens.service.store.Store`. See the module
    docstring for the per-tick algorithm.
    """

    def __init__(
        self,
        store: Store,
        options: ServeOptions,
        *,
        cache: DigestCache | None = None,
        now=None,
    ) -> None:
        self.store = store
        self.options = options
        self.cache = cache
        self._now = now or time.time
        self._pricing = _default_pricing()
        self._recache_thresholds = recache.RecacheThresholds()

        #: Paths force-parsed once while still "live" (see :meth:`_resolve`)
        #: that must be re-parsed on a later tick once stable, even if
        #: their ``(mtime_ns, size_bytes)`` never changes again in the
        #: meantime. In-memory only — a watcher restart loses this, which
        #: only means a file that finished writing in the exact window
        #: between two process lifetimes keeps its first (possibly
        #: incomplete) parse until it's next touched; a strictly better
        #: outcome than the alternative of never getting a first parse at
        #: all until the file is touched again.
        self._pending_stabilize: set[str] = set()

        self._loaded_snapshots: list[snapshots_mod.Snapshot] = []
        self._snapshot_ids_by_ts: dict[str, int] = {}
        #: ``{str(path): mtime_ns}`` as of the last tick that actually
        #: upserted that snapshot file (nit 29) -- lets _scan_snapshots
        #: skip re-flattening/re-upserting a snapshot file that hasn't
        #: changed since the previous tick instead of doing so on every
        #: single poll regardless.
        self._snapshot_file_mtimes: dict[str, int] = {}

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()

        #: ``contracts.Watcher.last_stats`` -- the most recent
        #: :meth:`run_once` tick's stats, kept up to date so
        #: ``serve.run`` never has to guess (S1-integration fix 1.e).
        self.last_stats: WatcherStats | None = None

    # -- contracts.Watcher ------------------------------------------------

    def run_once(self) -> WatcherStats:
        self.store.open()  # idempotent; ensures this thread's own connection has the schema
        stats = WatcherStats(started_at=_now_iso())
        t0 = time.monotonic()
        try:
            self._run_once(stats)
        except Exception as exc:  # never let one bad tick raise out of run_once
            stats.errors += 1
            stats.error_messages = stats.error_messages + (f"tick failed: {type(exc).__name__}",)
        stats.duration_s = time.monotonic() - t0
        stats.finished_at = _now_iso()
        self.last_stats = stats
        return stats

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            thread = threading.Thread(target=self._loop, name="claude-token-lens-watcher", daemon=True)
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_event.set()
            thread = self._thread
        if thread is not None:
            thread.join()
        with self._lifecycle_lock:
            self._thread = None

    def _loop(self) -> None:
        self.store.open()
        while not self._stop_event.is_set():
            self.run_once()
            self._stop_event.wait(self.options.poll_interval_s)

    # -- per-tick algorithm -------------------------------------------------

    def _run_once(self, stats: WatcherStats) -> None:
        self._scan_snapshots(stats)

        known = self.store.known_files()
        seen_paths: set[str] = set()

        project_dirs = discovery.resolve_project_dirs(
            self.options.projects_root,
            all_projects=True,
            exclude_projects=list(self.options.exclude_projects),
        )
        for project_dir in project_dirs:
            slug = project_dir.name
            for top_path in discovery.find_sessions(project_dir):
                self._scan_session(project_dir, slug, top_path, known, seen_paths, stats)

        if not project_dirs:
            # Finding 3 (second failure mode): an empty project_dirs list
            # is ambiguous between "genuinely no projects yet" and
            # "--projects-root is misconfigured/unmounted this tick" --
            # calling remove_missing(set()) here would mark every single
            # known transcript missing on the strength of that ambiguity
            # alone. Skip the missing-marking step entirely and record it,
            # so a transient/misconfigured root never mass-marks a whole
            # corpus missing; a real "no projects" installation is still
            # visible as an explicit, non-error note rather than silence.
            stats.error_messages = stats.error_messages + (
                "projects root returned no projects; skipped missing check",
            )
        else:
            stats.files_removed = self.store.remove_missing(seen_paths)

        stats.transcripts_missing = self.store.count_missing_transcripts()

        if self.options.retention_days is not None:
            self.store.retention_prune(self.options.retention_days)

    def _scan_session(
        self,
        project_dir: Path,
        slug: str,
        top_path: Path,
        known: dict[str, tuple[int, int]],
        seen_paths: set[str],
        stats: WatcherStats,
    ) -> None:
        session_id = top_path.stem
        stats.files_scanned += 1
        top_path_str = str(top_path)
        seen_paths.add(top_path_str)

        try:
            top_meta = _build_top_meta(top_path, session_id, slug)
            top_result, top_was_parsed = self._resolve(top_path_str, top_meta, known, stats)
        except Exception as exc:
            stats.errors += 1
            stats.error_messages = stats.error_messages + (f"top-level parse error: {type(exc).__name__}",)
            return

        # ``transcripts.session_id`` is a foreign key onto ``sessions.id``
        # (enforced -- ``Store`` runs with ``PRAGMA foreign_keys = ON``),
        # so a session row must exist before any transcript row naming it
        # can be written. This placeholder (bare session/project identity
        # only) is deliberately minimal -- ``_fold_session`` below always
        # overwrites it with the fully computed record once classification
        # and cost are known, via the same idempotent ``upsert_session``.
        # It's only reached once the top-level file itself resolved
        # successfully, so a session whose one-and-only top-level file
        # never parses still never gets a row at all (nothing to fold).
        self.store.upsert_session(session_id=session_id, project_slug=slug, project_root_path=str(project_dir), slug=slug)

        if top_was_parsed:
            self._upsert_transcript_row(session_id, top_path_str, top_meta, top_result)

        subs: list[TranscriptResult] = []
        try:
            # nit 26: find_subagents/find_workflows are generators that
            # walk the filesystem lazily -- an OSError raised mid-walk
            # (a directory removed/permission-denied between discovery
            # and this iteration) previously escaped the surrounding
            # try/except entirely, because the generator itself, not the
            # loop body, is where the exception would actually surface.
            # Materializing the listing up front brings that failure
            # under the same per-session error handling as everything
            # else in this method.
            subagent_entries = list(discovery.find_subagents(project_dir, session_id))
        except OSError as exc:
            stats.errors += 1
            stats.error_messages = stats.error_messages + (f"subagent discovery error: {type(exc).__name__}",)
            subagent_entries = []
        for jsonl_path, _raw_meta in subagent_entries:
            stats.files_scanned += 1
            sub_path_str = str(jsonl_path)
            seen_paths.add(sub_path_str)
            try:
                sub_meta = discovery.load_meta(jsonl_path.with_name(jsonl_path.stem + ".meta.json"))
                sub_result, sub_was_parsed = self._resolve(sub_path_str, sub_meta, known, stats)
                if sub_was_parsed:
                    self._upsert_transcript_row(session_id, sub_path_str, sub_meta, sub_result)
                subs.append(sub_result)
            except Exception as exc:
                stats.errors += 1
                stats.error_messages = stats.error_messages + (f"subagent parse error: {type(exc).__name__}",)
                continue

        # Workflow run files (S1-integration fix 1.d): parsed via the
        # same workflows.py functions corpus.load_corpus uses, linked to
        # this session's already-collected subagent transcripts (subs
        # already includes any workflow-nested agents -- see
        # discovery.find_subagents's own docstring), and persisted so
        # service/rebuild.py can read them back into
        # SessionBundle.workflows.
        try:
            workflow_paths = list(discovery.find_workflows(project_dir, session_id))
        except OSError as exc:
            stats.errors += 1
            stats.error_messages = stats.error_messages + (f"workflow discovery error: {type(exc).__name__}",)
            workflow_paths = []
        for workflow_path in workflow_paths:
            stats.files_scanned += 1
            try:
                run = workflows_mod.parse_workflow_file(workflow_path)
                workflows_mod.link_workflow_agents(run, subs, self._pricing)
                self.store.upsert_workflow_run(
                    session_id=session_id,
                    run_id=run.run_id,
                    agent_count=run.agent_count,
                    phase_titles=list(run.phase_titles),
                    started=run.started,
                    finished=run.finished,
                    cost=run.cost,
                    status=run.status,
                )
            except Exception as exc:
                stats.errors += 1
                stats.error_messages = stats.error_messages + (f"workflow parse error: {type(exc).__name__}",)

        try:
            self._fold_session(session_id, slug, project_dir, top_result, subs, stats)
        except Exception as exc:
            stats.errors += 1
            stats.error_messages = stats.error_messages + (f"session fold error: {type(exc).__name__}",)

    def _resolve(
        self,
        path_str: str,
        meta: TranscriptMeta,
        known: dict[str, tuple[int, int]],
        stats: WatcherStats,
    ) -> tuple[TranscriptResult, bool]:
        """Decide whether ``path_str`` needs (re-)parsing this tick, and
        do it if so. Returns ``(result, was_parsed)`` — ``was_parsed``
        tells the caller whether to write a fresh ``transcripts`` row
        (``True``) or reuse the one already there (``False``, for a file
        this tick chose not to touch).

        Decision table (see the module docstring's algorithm summary):

        - Unchanged since the last tick (same ``(mtime_ns, size_bytes)``
          as ``known``) and not pending a forced re-parse: reuse the
          store's existing digest, no re-parse.
        - New-or-changed, but live (mtime under
          :data:`LIVE_FILE_WINDOW_S`) and already known from a previous
          tick: skip parsing this tick (``files_skipped_live``), reuse
          whatever is already stored — it will be seen as changed again
          next tick (``known`` wasn't updated) and re-examined then.
        - New-or-changed and either not live, or live but never seen
          before: parse now. A live-and-never-seen file is also added to
          :attr:`_pending_stabilize` so a later tick re-parses it even if
          its ``(mtime_ns, size_bytes)`` doesn't change again before it
          goes stable.
        """
        prior = known.get(path_str)
        never_seen = prior is None
        current_key = (meta.mtime_ns, meta.size_bytes)
        changed = never_seen or current_key != prior
        forced = path_str in self._pending_stabilize
        live = self._is_live(meta.mtime_ns)

        if not changed and not forced:
            existing = self._load_existing(path_str)
            if existing is not None:
                return existing, False
            # No prior digest despite a known_files entry -- shouldn't
            # normally happen, but parse rather than return nothing.

        elif live and not never_seen and not forced:
            stats.files_skipped_live += 1
            existing = self._load_existing(path_str)
            if existing is not None:
                return existing, False
            # Fall through to parse: known_files() said we'd seen this
            # path before, but there's no digest to reuse.

        result = self._parse(path_str, meta)
        if live:
            self._pending_stabilize.add(path_str)
        else:
            self._pending_stabilize.discard(path_str)
        stats.files_parsed += 1
        return result, True

    def _is_live(self, mtime_ns: int) -> bool:
        age_s = self._now() - (mtime_ns / 1_000_000_000)
        return age_s < LIVE_FILE_WINDOW_S

    def _parse(self, path_str: str, meta: TranscriptMeta) -> TranscriptResult:
        if self.cache is not None:
            hit = self.cache.get(path_str, meta)
            if hit is not None:
                return hit
        result = parse_transcript(path_str, meta)
        if self.cache is not None:
            self.cache.put(path_str, meta, result)
        return result

    def _load_existing(self, path_str: str) -> TranscriptResult | None:
        """The already-stored ``TranscriptResult`` for ``path_str``, decoded
        from its ``transcripts.digest_json`` column — the same encoding
        ``cache.py`` uses (see :func:`~claude_token_lens.cache.encode_result`),
        so this is a lossless round trip, not a re-parse."""
        row = self.store._connection().execute(
            "SELECT digest_json FROM transcripts WHERE path = ?", (path_str,)
        ).fetchone()
        if row is None:
            return None
        try:
            return result_from_jsonable(json.loads(row["digest_json"]))
        except (KeyError, TypeError, ValueError):
            return None

    def _upsert_transcript_row(
        self, session_id: str, path_str: str, meta: TranscriptMeta, result: TranscriptResult
    ) -> None:
        digest_json = json.dumps(encode_result(result))
        self.store.upsert_transcript(
            session_id=session_id,
            path=path_str,
            kind=meta.kind,
            agent_id=meta.agent_id,
            agent_type=meta.agent_type,
            spawn_depth=meta.spawn_depth,
            parent_agent_id=meta.parent_agent_id,
            mtime_ns=meta.mtime_ns,
            size_bytes=meta.size_bytes,
            parser_version=PARSER_VERSION,
            digest_json=digest_json,
            turns_agg=_build_turns_agg(result, self._pricing),
            recache_turns=_build_recache_turns(result, self._recache_thresholds),
            events=_build_events(result),
            compactions=_build_compactions(result, self._pricing, self._recache_thresholds),
        )

    def _fold_session(
        self,
        session_id: str,
        slug: str,
        project_dir: Path,
        top: TranscriptResult,
        subs: list[TranscriptResult],
        stats: WatcherStats,
    ) -> None:
        classification = classify.classify_session(top, subs, {}, None, workflows=0, entrypoint=top.meta.entrypoint)
        record = classify.build_session_record(top, subs, [], classification, slug)
        features = _extract_workstyle_features(top, subs, [])
        archetype, _evidence = workstyle.detect_archetype(features)

        total_cost = 0.0
        total_tokens = 0
        for transcript in [top, *subs]:
            for turn in _priced_turns(transcript):
                resolved = self._pricing.resolve_model(turn.model)
                breakdown = price_turn(turn, resolved)
                total_cost += breakdown.total
                total_tokens += (
                    turn.input_tokens + turn.cache_creation_tokens + turn.cache_read_tokens + turn.output_tokens
                )

        snapshot_id: int | None = None
        if record.first_ts and self._loaded_snapshots:
            snap = snapshots_mod.snapshot_for(record.first_ts, self._loaded_snapshots)
            if snap is not None:
                snapshot_id = self._snapshot_ids_by_ts.get(snap.ts)

        self.store.upsert_session(
            session_id=session_id,
            project_slug=slug,
            project_root_path=str(project_dir),
            slug=slug,
            first_ts=record.first_ts,
            last_ts=record.last_ts,
            span_s=record.span_s,
            archetype=archetype,
            mode=classification.mode,
            mode_source=classification.mode_source,
            purpose=classification.purpose,
            purpose_source=classification.purpose_source,
            entrypoint=record.entrypoint,
            billing_mode=self.options.billing_mode,
            snapshot_id=snapshot_id,
            profile_id=None,
            total_cost=total_cost,
            total_tokens=total_tokens,
        )
        stats.sessions_upserted += 1

    def _scan_snapshots(self, stats: WatcherStats) -> None:
        """Ingest every ``options.config_dir/snapshots/*.json`` file this
        tick, unconditionally -- ``Store.upsert_snapshot`` now dedupes by
        its own natural key ``(project_id, ts, schema_version)`` (schema
        v2's ``ON CONFLICT``), so re-ingesting an already-known snapshot
        on a later tick just updates its existing row rather than growing
        a duplicate one (S1-integration fix 1.b; this replaces the
        previous pre-check-and-skip workaround against
        ``Store.snapshots()``). Populates
        :attr:`_loaded_snapshots`/:attr:`_snapshot_ids_by_ts` for
        :meth:`_fold_session`'s ``snapshot_id`` lookup.
        """
        loaded = snapshots_mod.load_snapshots(self.options.config_dir)
        self._loaded_snapshots = loaded

        ids_by_ts: dict[str, int] = {}
        fresh_mtimes: dict[str, int] = {}
        for snap in loaded:
            path_key = str(snap.path)
            try:
                mtime_ns = snap.path.stat().st_mtime_ns
            except OSError:
                mtime_ns = None

            # nit 29: a snapshot file only actually changes once per
            # Claude Code session start, but this method previously
            # re-flattened and re-upserted every loaded snapshot on every
            # single poll tick (every 30s by default) regardless. Reuse
            # the id already on record when the file's own mtime hasn't
            # moved since the tick that last upserted it.
            if (
                mtime_ns is not None
                and self._snapshot_file_mtimes.get(path_key) == mtime_ns
                and snap.ts in self._snapshot_ids_by_ts
            ):
                ids_by_ts[snap.ts] = self._snapshot_ids_by_ts[snap.ts]
                fresh_mtimes[path_key] = mtime_ns
                continue

            try:
                digest_json = json.dumps(snapshots_mod.flatten_snapshot(snap), sort_keys=True)
                schema_version = int(snap.data.get("schema", 1)) if isinstance(snap.data, dict) else 1
                new_id = self.store.upsert_snapshot(
                    project_slug=GLOBAL_PROJECT_SLUG,
                    project_root_path="",
                    ts=snap.ts,
                    schema_version=schema_version,
                    digest_json=digest_json,
                )
                ids_by_ts[snap.ts] = new_id
                if mtime_ns is not None:
                    fresh_mtimes[path_key] = mtime_ns
            except Exception as exc:
                stats.errors += 1
                stats.error_messages = stats.error_messages + (f"snapshot ingest error: {type(exc).__name__}",)
        self._snapshot_ids_by_ts = ids_by_ts
        self._snapshot_file_mtimes = fresh_mtimes


__all__ = ["FileWatcher", "LIVE_FILE_WINDOW_S"]
