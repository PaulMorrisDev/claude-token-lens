"""Report assembly (WP10a): turn a :class:`~claude_token_lens.corpus.Corpus`
into the whole :class:`~claude_token_lens.model.ReportModel` every renderer
(Markdown, JSON, CSV, HTML) consumes.

This is the integration layer every other WP4-WP9 analytics module feeds
into: it walks every session bundle once, folds each transcript into the
existing accumulators (:class:`recache.RecacheStats`,
:class:`ttl.TtlStats`, :class:`compaction.CompactionStats`,
:class:`topology.TopologyStats`, :func:`classify.build_session_record`,
:class:`workstyle.SessionFeatures`/:func:`workstyle.detect_archetype`,
optionally :class:`phases.PhaseStats`), and assembles their
``build_section`` outputs into ``ReportModel.sections`` in a fixed order.

Section order and keys: ``overview``, ``usage``, ``sessions``, ``recache``,
``ttl``, ``compactions``, ``agents``, ``workstyle``, ``workflows``,
``phases`` (only when ``phases=True``), ``config`` (only when snapshots
are supplied), ``scorecard``. ``include``, when given, keeps only
sections whose key is in it (used by the ``recache``/``ttl``/
``compactions``/``sessions`` subcommands to render a single focused
section rather than the whole report).

Deviations from the task brief, reported rather than made silently (see
``model.py``'s module docstring for this project's convention):

- The brief's ordered section-key list ends in ``"diagnostics"``, but
  ``model.py``'s frozen contract already has a *dedicated*
  ``ReportModel.diagnostics: Diagnostics`` field, and every renderer
  (confirmed in ``render/markdown.py``'s own module docstring: "6.
  Diagnostics") already renders it from that field directly, outside the
  ``sections`` list. Adding a *second*, table-shaped ``Section(key=
  "diagnostics", ...)`` would both duplicate that and not fit
  ``Diagnostics``'s scalar/dict shape into ``Table``'s rows-of-values
  shape well. This module aggregates Diagnostics into
  ``ReportModel.diagnostics`` (as instructed) and does not also emit a
  ``"diagnostics"`` Section — the existing renderer contract already
  covers it.
- The "config" section's task description ("config (only when snapshots
  present)") doesn't specify which config key(s) to diff — a report-wide
  ``build_report`` call has no single "the one key that changed" the way
  a ``config-diff <key>`` CLI subcommand would. This module calls
  :func:`snapshots.diff_keys` to find every key that changed across the
  supplied snapshots, and renders one :func:`snapshots.build_config_diff_table`
  per changed key (capped — see :data:`_MAX_CONFIG_DIFF_KEYS`), rather
  than reusing :func:`snapshots.build_config_section` directly (which
  takes exactly one ``key`` and returns ``Section(key="config_diff", ...)``
  — a different section key than this task specifies).
- ``allow_titles`` is accepted (matching the required signature) but is
  currently a no-op: nothing in ``model.py``/``parse.py``/``events.py``
  captures ``customTitle``/``ai-title`` line text anywhere, even
  conditionally (``events.py`` ignores both outright, unconditionally —
  see its ``_IGNORABLE_TYPES``). There is no title data for this module
  to gate. Wiring ``--allow-titles`` up would mean touching ``parse.py``
  and probably adding a field to ``TranscriptMeta``/``Event`` in
  ``model.py`` — both out of this work package's file list — so this is
  flagged here rather than silently implemented as a real gate.
- ``build_report`` has no ``config_dir`` parameter, so per-session
  ``sessions.toml`` overrides (``config.load_session_overrides``) cannot
  be resolved and loaded here; ``classify.classify_session`` is called
  with an empty ``overrides`` dict unless a caller passes its own
  ``session_overrides`` (see ``build_report``'s own docstring — WP10b
  added that parameter as the proposed fix for this). WP10-merge: ``cli.py``
  is now that caller — every ``report``/``sessions``/``recache``/``ttl``/
  ``compactions`` subcommand loads ``<config_dir>/sessions.toml`` via
  ``config.load_session_overrides(config_dir)`` and passes the result as
  ``session_overrides=``, so this deviation is closed for the CLI path;
  it remains true only for a caller of ``build_report`` that omits the
  keyword.
"""

from __future__ import annotations

import dataclasses
import statistics
from datetime import datetime, timezone
from typing import Callable

from . import __version__ as _TOOL_VERSION
from . import classify, compaction, recache, scorecard, snapshots as snapshots_mod, topology, ttl, workflows, workstyle
from .config import Config
from .corpus import Corpus, SessionBundle
from .model import (
    Column,
    Diagnostics,
    EventKind,
    PricingMeta,
    ReportMeta,
    ReportModel,
    Section,
    SessionRecord,
    Table,
    TranscriptResult,
    Turn,
    WorkflowRun,
)
from .phases import PhaseStats
from .phases import build_section as build_phases_section
from .pricing import Pricing, PricingCoverage, price_turn
from .recommend import recommend
from .snapshots import Snapshot

#: Fixed section order (before ``include`` filtering). Matches the task
#: brief exactly, minus "diagnostics" (see module docstring).
_SECTION_ORDER: tuple[str, ...] = (
    "overview",
    "usage",
    "sessions",
    "recache",
    "ttl",
    "compactions",
    "agents",
    "workstyle",
    "workflows",
    "phases",
    "config",
    "scorecard",
)

#: How many changed config keys get their own diff table in the "config"
#: section (see module docstring's deviation note) — a corpus tracked
#: over a long window against a churning config could otherwise produce
#: an unbounded number of tables.
_MAX_CONFIG_DIFF_KEYS = 20


def _priced_turns(result: TranscriptResult) -> list[Turn]:
    """Turns that actually got a ``turn_index`` (excludes synthetic and
    missing-usage turns). Deliberately duplicated rather than imported —
    same one-line-helper convention ``workflows.py``/``phases.py``
    document in their own module docstrings.
    """
    return [t for t in result.turns if t.turn_index > 0]


def _transcripts_of(bundle: SessionBundle) -> list[TranscriptResult]:
    transcripts: list[TranscriptResult] = []
    if bundle.top is not None:
        transcripts.append(bundle.top)
    transcripts.extend(bundle.subs)
    return transcripts


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _dominant_transcript_model(tr: TranscriptResult) -> str | None:
    """The most-observed ``Turn.model`` across ``tr``'s own priced turns,
    ties broken lexicographically. ``None`` when nothing resolves (no
    priced turns, or none carried a model id) — the caller then resolves
    against ``None``, which prices at zero with ``model_known=False``
    rather than guessing a rate.
    """
    counts: dict[str, int] = {}
    for turn in _priced_turns(tr):
        if not turn.model:
            continue
        counts[turn.model] = counts.get(turn.model, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _recommend_min_sample_values(config: Config) -> tuple[int, int]:
    """The min-sample values ``recommend()`` actually gates recommendations
    on, for display in the report's thresholds header -- NOT
    ``config.min_sessions``/``config.min_turns`` directly.
    ``recommend.RecommendThresholds`` has its own ``min_sessions``/
    ``min_turns`` defaults, independently overridable via
    ``config.thresholds["recommend"]`` (a ``[thresholds.recommend]`` TOML
    table distinct from the top-level ``Config.min_sessions``/
    ``min_turns``), so printing the ``Config`` fields verbatim can show a
    stale number when a corpus's ``[thresholds.recommend]`` overrides
    them. Prefers ``recommend.effective_min_sample(th)`` when that
    function exists (a future recommend.py addition this module doesn't
    own and can't rely on), otherwise reads ``RecommendThresholds``'s own
    resolved fields directly; falls back to the ``Config`` fields only if
    ``recommend.RecommendThresholds`` itself isn't importable.
    """
    from . import recommend as recommend_mod

    recommend_th_cls = getattr(recommend_mod, "RecommendThresholds", None)
    if recommend_th_cls is None:
        return config.min_sessions, config.min_turns

    recommend_th = recommend_th_cls.from_config(
        config.thresholds.get("recommend") if isinstance(config.thresholds, dict) else None
    )

    effective_min_sample = getattr(recommend_mod, "effective_min_sample", None)
    if effective_min_sample is not None:
        result = effective_min_sample(recommend_th)
        if isinstance(result, tuple) and len(result) == 2:
            return result

    return recommend_th.min_sessions, recommend_th.min_turns


def _merge_diagnostics(acc: Diagnostics, d: Diagnostics) -> None:
    """Fold one transcript's :class:`Diagnostics` into the running
    corpus-wide total: sum every int counter, merge every dict counter
    key-by-key, OR every bool.
    """
    acc.lines += d.lines
    acc.unparsable_lines += d.unparsable_lines
    acc.truncated_final_line = acc.truncated_final_line or d.truncated_final_line
    acc.assistant_lines += d.assistant_lines
    acc.distinct_turns += d.distinct_turns
    acc.synthetic_turns += d.synthetic_turns
    acc.turns_missing_usage += d.turns_missing_usage
    acc.ttl_sum_mismatch += d.ttl_sum_mismatch
    acc.late_duplicate_ids += d.late_duplicate_ids
    acc.oversized_lines += d.oversized_lines
    acc.trailing_events += d.trailing_events
    acc.replayed_lines += d.replayed_lines
    acc.timestamp_parse_failures += d.timestamp_parse_failures
    acc.pre_split_turns += d.pre_split_turns
    for key, value in d.ignored_line_types.items():
        acc.ignored_line_types[key] = acc.ignored_line_types.get(key, 0) + value
    for key, value in d.agent_settings.items():
        acc.agent_settings[key] = acc.agent_settings.get(key, 0) + value
    for key, value in d.modes.items():
        acc.modes[key] = acc.modes.get(key, 0) + value
    for key, value in d.attachment_catch_all.items():
        acc.attachment_catch_all[key] = acc.attachment_catch_all.get(key, 0) + value


# -- workstyle feature extraction (no existing helper does this: see
# workstyle.py's own module docstring, "this module never reads a
# TranscriptResult ... directly") -------------------------------------


def _extract_workstyle_features(
    top: TranscriptResult, subs: list[TranscriptResult], workflow_runs: list[WorkflowRun]
) -> workstyle.SessionFeatures:
    top_level_models = tuple(sorted({t.model for t in _priced_turns(top) if t.model}))

    subagent_models: list[tuple[str | None, str | None]] = []
    for sub in subs:
        model = _dominant_transcript_model(sub)
        subagent_models.append((model, sub.meta.agent_model_alias))

    effort_turn_counts: dict[str, int] = {}
    for tr in [top, *subs]:
        for turn in _priced_turns(tr):
            if turn.effort:
                effort_turn_counts[turn.effort] = effort_turn_counts.get(turn.effort, 0) + 1

    plan_mode_seen = False
    plan_mode_exit_ts: str | None = None
    for event in top.events:
        if event.kind != EventKind.CACHE_SIGNAL:
            continue
        if event.subkind == "plan_mode":
            plan_mode_seen = True
        elif event.subkind == "plan_mode_exit" and plan_mode_exit_ts is None:
            plan_mode_exit_ts = event.ts

    top_tier = max((workstyle.model_tier(m) for m in top_level_models), default=-1)
    post_plan_lower_tier = False
    if plan_mode_seen and plan_mode_exit_ts:
        exit_dt = _parse_ts(plan_mode_exit_ts)
        if exit_dt is not None:
            for turn in _priced_turns(top):
                turn_dt = _parse_ts(turn.ts)
                if turn_dt is None or turn_dt <= exit_dt:
                    continue
                tier = workstyle.model_tier(turn.model)
                if tier != -1 and tier < top_tier:
                    post_plan_lower_tier = True
                    break
            if not post_plan_lower_tier:
                for sub in subs:
                    first_priced = next(iter(_priced_turns(sub)), None)
                    sub_dt = _parse_ts(first_priced.ts) if first_priced is not None else None
                    if sub_dt is None or sub_dt <= exit_dt:
                        continue
                    tier = workstyle.model_tier(_dominant_transcript_model(sub), sub.meta.agent_model_alias)
                    if tier != -1 and tier < top_tier:
                        post_plan_lower_tier = True
                        break

    top_level_tool_names = frozenset(
        name for turn in _priced_turns(top) for name in turn.tool_names
    )

    return workstyle.SessionFeatures(
        top_level_models=top_level_models,
        subagent_models=tuple(subagent_models),
        spawn_count=len(subs),
        has_workflow=bool(workflow_runs),
        effort_turn_counts=effort_turn_counts,
        plan_mode_seen=plan_mode_seen,
        post_plan_lower_tier=post_plan_lower_tier,
        top_level_tool_names=top_level_tool_names,
        agent_settings=dict(top.diagnostics.agent_settings),
    )


# -- overview -------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class _ModelCell:
    turns: int = 0
    input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0


@dataclasses.dataclass(slots=True)
class _OverviewAcc:
    sessions: int = 0
    top_level_transcripts: int = 0
    subagent_transcripts: int = 0
    workflow_runs: int = 0
    priced_turns: int = 0
    input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    total_cost: float = 0.0
    cache_read_cost: float = 0.0
    by_model: dict[str, _ModelCell] = dataclasses.field(default_factory=dict)


def _build_overview_section(
    acc: _OverviewAcc,
    cache_economy_totals: dict,
    top_level_median_ctx: float | None,
    top_level_turns_ctx_ge_200k_pct: float | None,
) -> Section:
    usage_tokens = acc.input_tokens + acc.cache_creation_tokens + acc.cache_read_tokens + acc.output_tokens
    new_tokens = acc.input_tokens + acc.cache_creation_tokens + acc.output_tokens
    cache_read_cost_share = 100.0 * acc.cache_read_cost / acc.total_cost if acc.total_cost else None

    totals_table = Table(
        name="totals",
        title="Overview totals",
        columns=[
            Column(key="metric", label="Metric", kind="str"),
            Column(key="value", label="Value", kind="str"),
        ],
        rows=[
            ["sessions", acc.sessions],
            ["top_level_transcripts", acc.top_level_transcripts],
            ["subagent_transcripts", acc.subagent_transcripts],
            ["workflow_runs", acc.workflow_runs],
            ["priced_turns", acc.priced_turns],
            ["input_tokens", acc.input_tokens],
            ["cache_creation_tokens", acc.cache_creation_tokens],
            ["cache_read_tokens", acc.cache_read_tokens],
            ["output_tokens", acc.output_tokens],
            ["usage_tokens", usage_tokens],
            ["new_tokens", new_tokens],
            ["total_cost_usd", acc.total_cost],
            ["cache_read_cost_share_pct", cache_read_cost_share],
            ["cache_roi", cache_economy_totals.get("cache_roi", 0.0)],
            # Top-level-only (agent_type == "top-level") ctx stats -- see
            # _top_level_ctx_values's docstring for why subagent transcripts
            # are excluded. Added so the "long-context share of recent
            # top-level turns" verification anchor has a turn-count-basis,
            # top-level-only table to check against (a subagent's ctx runs
            # far larger and would otherwise skew this upward).
            ["top_level_median_ctx", top_level_median_ctx],
            ["top_level_turns_ctx_ge_200k_pct", top_level_turns_ctx_ge_200k_pct],
        ],
    )

    by_model_rows = [
        [
            model,
            cell.turns,
            cell.input_tokens,
            cell.cache_creation_tokens,
            cell.cache_read_tokens,
            cell.output_tokens,
            cell.cost,
        ]
        for model, cell in sorted(acc.by_model.items(), key=lambda kv: (-kv[1].cost, kv[0]))
    ]
    by_model_table = Table(
        name="by_model",
        title="Overview by model",
        columns=[
            Column(key="model", label="Model", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="input_tokens", label="Input tokens", kind="tokens"),
            Column(key="cache_creation_tokens", label="Cache-creation tokens", kind="tokens"),
            Column(key="cache_read_tokens", label="Cache-read tokens", kind="tokens"),
            Column(key="output_tokens", label="Output tokens", kind="tokens"),
            Column(key="cost", label="Cost", kind="money"),
        ],
        rows=by_model_rows,
    )

    notes = []
    if acc.sessions == 0:
        notes.append("No sessions found in this window.")
    return Section(key="overview", title="Overview", tables=[totals_table, by_model_table], notes=notes)


# -- config section (see module docstring's deviation note) -------------


def _build_config_section(sessions_with_metrics: list[dict], snaps: list[Snapshot]) -> Section:
    changed_keys = sorted(snapshots_mod.diff_keys(snaps).keys())
    shown_keys = changed_keys[:_MAX_CONFIG_DIFF_KEYS]

    tables: list[Table] = []
    for key in shown_keys:
        diff_table = snapshots_mod.build_config_diff_table(sessions_with_metrics, snaps, key)
        rows = [[_stringify(row[0]), *row[1:]] for row in diff_table.rows]
        tables.append(
            Table(
                name=diff_table.name,
                title=diff_table.title,
                columns=diff_table.columns,
                rows=rows,
                notes=diff_table.notes,
            )
        )

    notes = []
    if not changed_keys:
        notes.append("No config key changed across the supplied snapshots in this window.")
    elif len(changed_keys) > _MAX_CONFIG_DIFF_KEYS:
        notes.append(
            f"Showing the first {_MAX_CONFIG_DIFF_KEYS} of {len(changed_keys)} changed "
            "config keys, alphabetically."
        )
    return Section(key="config", title="Config", tables=tables, notes=notes)


def _stringify(value: object) -> str:
    if value is None:
        return "(unset)"
    return str(value)


# -- recache group-by breakdown ------------------------------------------


#: ``group_by`` values that must be keyed per-*transcript* rather than
#: per-*session* (see ``_transcript_key_lookup``'s docstring for why).
_TRANSCRIPT_GROUP_KEYS = frozenset({"agent", "model", "entrypoint"})


def _group_key_lookup(records: list[SessionRecord], group_by: str) -> Callable[[TranscriptResult], str]:
    groups = classify.group_sessions(records, group_by)
    label_by_session: dict[str, str] = {}
    for label, group_records in groups.items():
        for record in group_records:
            label_by_session[record.session_id] = label

    def _key(result: TranscriptResult) -> str:
        return label_by_session.get(result.meta.session_id, "unknown")

    return _key


def _transcript_key_lookup(group_by: str) -> Callable[[TranscriptResult], str]:
    """Per-*transcript* group-key lookup for ``group_by in
    _TRANSCRIPT_GROUP_KEYS`` (``"agent"``/``"model"``/``"entrypoint"``).

    ``_group_key_lookup`` labels every transcript in a session with that
    *session's* one dominant group (``classify.group_sessions`` computes a
    single label per :class:`SessionRecord`), so a subagent inherits its
    session's key rather than its own -- fine for ``mode``/``purpose``/
    ``project`` (genuinely session-level properties) but wrong for
    ``agent``/``model``/``entrypoint``, which vary *per transcript* within
    one session (e.g. a session that spawns both a ``claude-implementer``
    and a ``general-purpose`` subagent has no single "session agent type").
    This keys each transcript by its own ``TranscriptMeta.agent_type``,
    dominant model, or ``TranscriptMeta.entrypoint`` instead, matching how
    :meth:`recache.RecacheStats.add` itself derives ``agent_type`` for the
    ``recache_by_agent_type`` table (``result.meta.agent_type or
    "top-level"``) so ``recache_by_group`` (grouped by ``"agent"``) sums to
    the same per-agent-type totals as that table.
    """

    def _key(result: TranscriptResult) -> str:
        if group_by == "agent":
            return result.meta.agent_type or "top-level"
        if group_by == "model":
            return _dominant_transcript_model(result) or "unknown"
        if group_by == "entrypoint":
            return result.meta.entrypoint or "unknown"
        raise ValueError(f"not a transcript-keyed group_by: {group_by!r}")

    return _key


def _build_recache_section(
    stats: recache.RecacheStats, pricing: Pricing, th: recache.RecacheThresholds, group_by: str | None
) -> Section:
    section = recache.build_section(stats, pricing, th, group=None)
    if not group_by:
        return section

    base_columns = section.tables[0].columns
    group_rows = []
    for group_label in stats.groups():
        group_section = recache.build_section(stats, pricing, th, group=group_label)
        summary_row = group_section.tables[0].rows[0]
        group_rows.append([group_label, *summary_row])

    group_table = Table(
        name="recache_by_group",
        title=f"Re-cache summary by {group_by}",
        columns=[Column(key="group", label=group_by.capitalize(), kind="str"), *base_columns],
        rows=group_rows,
    )
    return dataclasses.replace(section, tables=[*section.tables, group_table])


# -- build_report -----------------------------------------------------------


def build_report(
    corpus: Corpus,
    pricing: Pricing,
    config: Config,
    *,
    projects: tuple[str, ...],
    window: str,
    group_by: str | None = None,
    phases: bool = False,
    snapshots: list[Snapshot] | None = None,
    allow_titles: bool = False,
    include: set[str] | None = None,
    session_overrides: dict | None = None,
) -> ReportModel:
    """Assemble the whole :class:`ReportModel` for ``corpus``. See the
    module docstring for section order/keys and the deviations from the
    task brief this function documents rather than silently resolves.

    ``session_overrides`` (WP10b addition) is the ``sessions.toml``-shaped
    dict ``config.load_session_overrides`` produces (session id ->
    ``{"mode": ..., "purpose": ...}``), passed straight through to
    :func:`classify.classify_session`. ``build_report`` still has no
    ``config_dir`` parameter of its own (see the module docstring's
    deviation note above), so it still cannot *load* the overrides file
    itself -- a caller that wants overrides applied loads it via
    ``config.load_session_overrides`` and passes the result here.
    Defaults to ``{}`` when omitted, matching the previous hardcoded
    behaviour exactly.
    """
    from . import usage as usage_mod  # local import: avoids a cycle risk with any future usage<->report coupling

    recache_th = recache.RecacheThresholds.from_config(config.thresholds)
    ttl_th = ttl.TtlThresholds.from_config(config.thresholds)
    mode_thresholds, purpose_thresholds = classify.mode_and_purpose_thresholds_from_config(config.thresholds)
    scorecard_th = scorecard.ScorecardThresholds.from_config(
        config.thresholds.get("scorecard") if isinstance(config.thresholds, dict) else None
    )

    session_overrides = session_overrides or {}

    session_records: list[SessionRecord] = []
    session_cost: dict[str, float] = {}
    session_cc_total: dict[str, int] = {}
    session_recache_cc: dict[str, int] = {}
    all_workflow_runs: list[WorkflowRun] = []

    overview = _OverviewAcc()
    pricing_coverage = PricingCoverage()
    diagnostics = Diagnostics()

    rs = recache.RecacheStats(recache_th)
    ts = ttl.TtlStats()
    cs = compaction.CompactionStats()
    tp = topology.TopologyStats()
    ph = PhaseStats() if phases else None

    for bundle in corpus.sessions:
        top = bundle.top
        if top is None:
            continue
        subs = bundle.subs
        transcripts = _transcripts_of(bundle)

        classification = classify.classify_session(
            top,
            subs,
            session_overrides,
            config.tz,
            workflows=len(bundle.workflows),
            entrypoint=top.meta.entrypoint,
            mode_thresholds=mode_thresholds,
            purpose_thresholds=purpose_thresholds,
        )
        record = classify.build_session_record(top, subs, bundle.workflows, classification, bundle.slug)

        features = _extract_workstyle_features(top, subs, bundle.workflows)
        archetype, _evidence = workstyle.detect_archetype(features)
        record.archetype = archetype
        session_records.append(record)
        all_workflow_runs.extend(bundle.workflows)

        overview.sessions += 1
        overview.top_level_transcripts += 1
        overview.subagent_transcripts += len(subs)
        overview.workflow_runs += len(bundle.workflows)

        session_cost_total = 0.0
        session_cc_total_tokens = 0
        session_recache_cc_tokens = 0

        for tr in transcripts:
            _merge_diagnostics(diagnostics, tr.diagnostics)

            rs.add(tr, pricing.resolve_model)
            ts.add(tr, pricing.resolve_model, ttl_th)

            dominant_model = _dominant_transcript_model(tr)
            dominant_rate = pricing.resolve_model(dominant_model) if dominant_model else None
            cs.add_transcript(tr, dominant_rate, recache_th)

            if ph is not None:
                ph.add_transcript(tr, pricing)

            flagged = recache.detect(tr.turns, recache_th)
            session_recache_cc_tokens += sum(t.cache_creation_tokens for t in flagged)

            for turn in _priced_turns(tr):
                resolved = pricing.resolve_model(turn.model)
                breakdown = price_turn(turn, resolved)
                pricing_coverage.add(turn, breakdown)

                overview.priced_turns += 1
                overview.input_tokens += turn.input_tokens
                overview.cache_creation_tokens += turn.cache_creation_tokens
                overview.cache_read_tokens += turn.cache_read_tokens
                overview.output_tokens += turn.output_tokens
                overview.total_cost += breakdown.total
                overview.cache_read_cost += breakdown.cache_read_cost

                model_key = turn.model or "<unknown>"
                cell = overview.by_model.setdefault(model_key, _ModelCell())
                cell.turns += 1
                cell.input_tokens += turn.input_tokens
                cell.cache_creation_tokens += turn.cache_creation_tokens
                cell.cache_read_tokens += turn.cache_read_tokens
                cell.output_tokens += turn.output_tokens
                cell.cost += breakdown.total

                session_cost_total += breakdown.total
                session_cc_total_tokens += turn.cache_creation_tokens

        tp.add_session(record.session_id, top, list(subs), pricing)

        session_cost[record.session_id] = session_cost_total
        session_cc_total[record.session_id] = session_cc_total_tokens
        session_recache_cc[record.session_id] = session_recache_cc_tokens

    if group_by:
        # "agent"/"model"/"entrypoint" vary per transcript within a
        # session (see _transcript_key_lookup's docstring — R5 fix);
        # everything else (mode/purpose/project/...) is a genuinely
        # session-level property, so it keeps the session-keyed lookup.
        if group_by in _TRANSCRIPT_GROUP_KEYS:
            rs.group_key = _transcript_key_lookup(group_by)
        else:
            rs.group_key = _group_key_lookup(session_records, group_by)
        # RecacheStats folds groups in during .add(); since grouping was
        # decided only after the fact (group_key needs every session
        # classified first), re-fold every transcript now that the
        # lookup is known.
        rs = recache.RecacheStats(recache_th, group_key=rs.group_key)
        for bundle in corpus.sessions:
            if bundle.top is None:
                continue
            for tr in _transcripts_of(bundle):
                rs.add(tr, pricing.resolve_model)

    # -- cache economy (overview's cache ROI): summed per-transcript, not
    # over one concatenated turns list -- ttl.normalize_ttl_split/
    # dominant_ttl work from a single transcript's own whole-turns view
    # (see ttl.py's docstring), so combining every transcript's turns
    # into one list first would misattribute pre-TTL-split writes to a
    # corpus-wide dominant side instead of each transcript's own. -------
    cache_economy_totals = {"write_usd": 0.0, "net_saving_usd": 0.0, "cache_roi": 0.0}
    total_write_usd = 0.0
    total_net_saving = 0.0
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        for tr in _transcripts_of(bundle):
            econ = ttl.cache_economy(tr.turns, pricing.resolve_model, ttl_th)
            total_write_usd += econ["write_usd"]
            total_net_saving += econ["net_saving_usd"]
    cache_economy_totals["write_usd"] = total_write_usd
    cache_economy_totals["net_saving_usd"] = total_net_saving
    cache_economy_totals["cache_roi"] = total_net_saving / total_write_usd if total_write_usd > 0 else 0.0

    # -- top-level-only ctx stats (R7 + coordinator follow-up): computed
    # once here, off the final ``rs`` (post group-by re-fold, if any),
    # and reused by both the overview totals table and the scorecard's
    # context-hygiene dimension. See _top_level_ctx_values's docstring.
    top_level_ctx_values = _top_level_ctx_values(rs)
    top_level_median_ctx = statistics.median(top_level_ctx_values) if top_level_ctx_values else None
    top_level_turns_ctx_ge_200k_pct = (
        100.0 * sum(1 for c in top_level_ctx_values if c >= recache_th.huge_ctx) / len(top_level_ctx_values)
        if top_level_ctx_values
        else None
    )

    # -- assemble sections ---------------------------------------------

    sections: list[Section] = []

    def _want(key: str) -> bool:
        return include is None or key in include

    if _want("overview"):
        sections.append(
            _build_overview_section(
                overview, cache_economy_totals, top_level_median_ctx, top_level_turns_ctx_ge_200k_pct
            )
        )

    if _want("usage"):
        sections.append(usage_mod.build_section(corpus, pricing, config))

    if _want("sessions"):
        sections.append(classify.build_section(session_records, mode_thresholds))

    if _want("recache"):
        sections.append(_build_recache_section(rs, pricing, recache_th, group_by))

    if _want("ttl"):
        sections.append(ttl.build_section(ts, billing_mode=config.billing, thresholds=ttl_th))

    if _want("compactions"):
        sections.append(compaction.build_section(cs))

    if _want("agents"):
        sections.append(topology.build_section(tp))

    if _want("workstyle"):
        sections.append(workstyle.build_section(session_records))

    if _want("workflows"):
        sections.append(workflows.build_section(all_workflow_runs))

    if phases and ph is not None and _want("phases"):
        sections.append(build_phases_section(ph))

    if snapshots and _want("config"):
        sessions_with_metrics = [
            {
                "session_id": record.session_id,
                "first_ts": record.first_ts,
                "turns": len(record.top.turns) + sum(len(s.turns) for s in record.subs) if record.top else 0,
                "cost": session_cost.get(record.session_id, 0.0),
                "recache_cc": session_recache_cc.get(record.session_id, 0),
                "cc_total": session_cc_total.get(record.session_id, 0),
                "compactions": next(
                    (count for sid, count, _dropped, _cost in cs.per_session_summary() if sid == record.session_id),
                    0,
                ),
                "span_s": record.span_s,
            }
            for record in session_records
        ]
        sections.append(_build_config_section(sessions_with_metrics, snapshots))

    if _want("scorecard"):
        sections.append(_build_scorecard_section(rs, ts, tp, cs, pricing_coverage, diagnostics, session_records, snapshots, config, scorecard_th))

    # -- meta -------------------------------------------------------------

    recommend_min_sessions, recommend_min_turns = _recommend_min_sample_values(config)
    thresholds_dict: dict = {
        "recache": {
            "ctx_floor": recache_th.ctx_floor,
            "cr_ratio": recache_th.cr_ratio,
            "full_expiry_cr": recache_th.full_expiry_cr,
            "huge_ctx": recache_th.huge_ctx,
        },
        "ttl": dataclasses.asdict(ttl_th) if dataclasses.is_dataclass(ttl_th) else {},
        "classify_mode": mode_thresholds,
        "classify_purpose": purpose_thresholds,
        # The min-sample gate recommend() actually applies, NOT
        # config.min_sessions/config.min_turns directly -- see
        # _recommend_min_sample_values's docstring (min-sample header fix).
        "min_sessions": recommend_min_sessions,
        "min_turns": recommend_min_turns,
    }

    assumptions: list[str] = list(ttl.ASSUMPTIONS) + list(recache.ASSUMPTIONS)

    meta = ReportMeta(
        tool_version=_TOOL_VERSION,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z",
        window=window,
        projects=tuple(projects),
        pricing=PricingMeta(
            path=pricing.path,
            version=pricing.version,
            sha8=pricing.sha8,
            currency=pricing.currency,
            coverage_pct=pricing_coverage.coverage_pct,
        ),
        thresholds=thresholds_dict,
        billing_mode=config.billing,
        assumptions=assumptions,
    )

    report_model = ReportModel(meta=meta, sections=sections, recommendations=[], diagnostics=diagnostics)

    # WP10b: recommendations are computed from the already-assembled
    # report (see recommend.py's module docstring for why it works from
    # rendered tables rather than the raw accumulators above), using the
    # corpus's majority archetype and the latest config snapshot (if any)
    # as of "now" -- a per-session snapshot join is not attempted here,
    # matching how ``_build_config_section``/``_build_scorecard_section``
    # already treat ``snapshots`` as a single corpus-wide input.
    corpus_archetype, _archetype_evidence = workstyle.corpus_archetype(session_records)
    latest_snapshot = snapshots[-1] if snapshots else None
    report_model.recommendations = recommend(
        report_model, config=config, archetype=corpus_archetype, snapshot=latest_snapshot
    )

    return report_model


def _top_level_ctx_values(rs: recache.RecacheStats) -> list[int]:
    """Sorted ``ctx`` values from top-level-only turns in ``rs.records``
    (R7 fix): ``recache.RecacheStats.add`` stamps every record's
    ``agent_type`` as ``result.meta.agent_type or "top-level"`` (see
    ``recache.py``), so filtering to ``"top-level"`` here excludes every
    subagent transcript. A subagent's own ctx runs far larger than its
    parent's (subagents typically start from a large system-prompt/task
    payload) and would otherwise skew both the scorecard's
    context-hygiene dimension and the overview's long-context-share
    metric upward, hiding an actually-healthy top-level session behind
    its subagents' naturally bigger context windows.
    """
    return sorted(r.turn.ctx for r in rs.records if r.agent_type == "top-level" and r.turn.ctx)


def _build_scorecard_section(
    rs: recache.RecacheStats,
    ts: ttl.TtlStats,
    tp: topology.TopologyStats,
    cs: compaction.CompactionStats,
    pricing_coverage: PricingCoverage,
    diagnostics: Diagnostics,
    session_records: list[SessionRecord],
    snaps: list[Snapshot] | None,
    config: Config,
    th: scorecard.ScorecardThresholds,
) -> Section:
    all_turns = [r.turn for r in rs.records]
    recache_turns = [t for t in all_turns if t.is_recache]
    total_cc_all = sum(t.cache_creation_tokens for t in all_turns)
    total_cc_recache = sum(t.cache_creation_tokens for t in recache_turns)
    recache_share_pct = 100.0 * total_cc_recache / total_cc_all if total_cc_all else None

    total_read = sum(t.cache_read_tokens for t in all_turns)
    total_input = sum(t.input_tokens for t in all_turns)
    hit_denom = total_read + total_cc_all + total_input
    cache_hit_ratio_pct = 100.0 * total_read / hit_denom if hit_denom else None

    # Top-level-only (excludes subagent transcripts) -- see
    # _top_level_ctx_values's docstring. Was previously built from
    # ``all_turns`` (every transcript, top-level and subagent alike).
    top_level_ctx = _top_level_ctx_values(rs)
    median_ctx = statistics.median(top_level_ctx) if top_level_ctx else None
    p90_ctx = None
    if top_level_ctx:
        idx = max(0, min(len(top_level_ctx) - 1, int(round(0.9 * (len(top_level_ctx) - 1)))))
        p90_ctx = float(top_level_ctx[idx])

    has_spawns = tp.total_spawns > 0
    agent_variance = None
    if has_spawns:
        per_type_mean = {
            agent_type: statistics.mean(costs)
            for agent_type, costs in tp.cost_by_agent_type.items()
            if costs
        }
        if per_type_mean:
            median_cost = statistics.median(per_type_mean.values())
            max_cost = max(per_type_mean.values())
            agent_variance = max_cost / median_cost if median_cost else None

    has_snapshot = bool(snaps)
    changed_keys = len(snapshots_mod.diff_keys(snaps)) if snaps else 0

    parse_error_rate_pct = (
        100.0 * diagnostics.unparsable_lines / diagnostics.lines if diagnostics.lines else 0.0
    )

    inputs = scorecard.ScorecardInputs(
        recache_share_pct=recache_share_pct,
        cache_hit_ratio_pct=cache_hit_ratio_pct,
        median_top_level_ctx=median_ctx,
        p90_top_level_ctx=p90_ctx,
        compaction_count=len(cs.records),
        dropped_share_pct=cs.dropped_share_of_new_tokens,
        has_spawns=has_spawns,
        agent_cost_variance_ratio=agent_variance,
        has_snapshot=has_snapshot,
        changed_config_keys=changed_keys,
        pricing_coverage_pct=pricing_coverage.coverage_pct,
        parse_error_rate_pct=parse_error_rate_pct,
    )
    return scorecard.build_section(inputs, th)


__all__ = ["build_report"]
