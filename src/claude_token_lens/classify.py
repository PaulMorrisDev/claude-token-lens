"""Session mode/purpose classification (WP5).

Implements the project plan's "Classification" section and Appendix A1's
``Classification``/``SessionRecord`` contracts. Three layers:

- :func:`extract_features` reduces one session (its top-level transcript
  plus every subagent transcript) to a small :class:`SessionFeatures`
  bundle of numeric/boolean signals — never message text or paths.
- :func:`classify_mode` and :func:`classify_purpose` are pure functions
  of :class:`SessionFeatures`: first-match-wins rule tables per the plan.
- :func:`classify_session` ties the two together, applying a
  ``~/.claude/token-lens/sessions.toml`` override (via
  ``config.load_session_overrides``) ahead of the rules when present.

:func:`build_session_record` and :func:`group_sessions`/:func:`build_section`
implement Appendix A1's ``SessionRecord`` construction and the report
section the plan's Classification section describes
(``--group-by mode|purpose|...``).

Scope decisions made here, not silently, because the plan's feature list
doesn't say which of ``top``/``subs`` each signal is drawn from:

- ``human_prompts``, the human-gap statistics, and ``start_local_hour``/
  ``end_local_hour`` are computed from ``top`` only. A subagent transcript
  has no human on the other end of it — its user-role lines are harness
  plumbing (tool results, task notifications), not a person typing — so
  counting them as "human prompts" would misrepresent how interactive a
  session actually was.
- ``assistant_turns`` is ``top`` only, for the same reason the plan's
  long-agentic rule needs it: "the top-level conversation kept turning
  itself with hardly any human input" is a statement about the top-level
  turn count, not the combined turn count across every spawned subagent
  (which ``subagent_count``/``has_chain`` already capture separately).
- Every other counter (``queue_ops``, ``compactions``, ``task_notifications``,
  ``peer_messages``, ``plan_mode_events``, and the purpose-signal counters
  ``local_llm_hits``/``agent_tool_calls``/``workflow_tool_calls``/
  ``test_tool_hits``/``review_markers``/``edit_turns``/``read_turns``) are
  summed across ``top`` *and every* transcript in ``subs``. Purpose
  classification asks "what kind of work did this session as a whole do",
  and in this codebase's own real sessions the actual editing/testing
  usually happens inside a spawned ``claude-implementer``/
  ``verification-runner`` subagent while the top-level turn just
  orchestrates — restricting these to ``top`` would make "refactor" and
  "test-triage" nearly unreachable.

Two features the brief calls out with an explicit instruction to skip:

- ``doc_edit_share`` ("edits whose... no path available: skip") is not
  implemented — no dataclass field anywhere retains a file path (see
  ``model.py``'s privacy invariant), so there is no way to tell a doc
  edit from any other edit. The "docs" purpose rule uses the brief's own
  fallback condition instead (see ``classify_purpose``), and is reported
  as the literal value ``"docs-or-light-edit"`` per the brief's wording.
- The plan's ``extract_features(top, subs, tz)`` signature has no
  parameter for ``workflows`` (a count) or ``entrypoint`` (a carry-through
  value), yet lists both among the features it produces. Neither is
  derivable from a ``TranscriptResult`` — ``workflows`` comes from
  ``WorkflowRun`` parsing (WP8, not yet written) and ``entrypoint`` has no
  home anywhere in the frozen ``model.py`` contract yet (see the "Other
  clients" plan section, which describes it as a future grouping axis
  without ever adding the field). Both are added here as optional
  keyword-only parameters with inert defaults (``workflows=0``,
  ``entrypoint=None``) so a caller that *does* have the data (once WP8
  exists) can supply it without ``extract_features`` guessing. Until
  then, ``group_sessions(records, key="entrypoint")`` always returns a
  single ``"unknown"`` bucket — flagged in this module's report as a
  proposed ``model.py``/``SessionRecord`` addition, not made silently.

Timezone conversion (``start_local_hour``/``end_local_hour``) uses
``zoneinfo.ZoneInfo``. On a machine with no system tz database and no
``tzdata`` package installed (a bare Windows install, common on this
project's own dev machine — confirmed by hand: ``ZoneInfo("America/
New_York")`` raises ``ZoneInfoNotFoundError`` here), a named zone that
can't be resolved degrades to the same behaviour as ``tz=None`` (the
machine's own local zone via ``datetime.astimezone()``, which needs no
tz database) rather than raising. This is a real limitation of a
stdlib-only, zero-dependency tool on Windows, not an edge case worth
hiding; ``tests/test_classify.py`` exercises both branches depending on
what the running machine actually has available.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .model import (
    Classification,
    Column,
    EventKind,
    Section,
    SessionRecord,
    Table,
    TranscriptResult,
    WorkflowRun,
)

# -- Thresholds -------------------------------------------------------------

#: Default thresholds for :func:`classify_mode`, overridable per call (and,
#: eventually, from ``Config.thresholds`` — see the module docstring: this
#: WP doesn't wire that up itself, since neither the brief's
#: ``classify_mode(f)`` signature nor ``config.py``'s ``thresholds: dict``
#: shape name a sub-key convention for it yet).
DEFAULT_MODE_THRESHOLDS: dict = {
    "overnight_span_s": 4 * 3600,
    "overnight_gap_s": 60 * 60,
    "long_agentic_max_human_prompts": 5,
    # Tuned from the plan's implied starting point of 50 down to 30
    # against the real RevIXO corpus (131 top-level sessions, see this
    # WP's report): at 50, sessions that were plainly autonomous
    # top-level runs (dozens of self-chained turns, at most a couple of
    # human nudges) but topped out under 50 turns fell through every
    # rule into "mixed" - 39% of that corpus's "mixed" sessions had
    # >=30 assistant_turns and <=8 human_prompts. Lowering just this one
    # threshold (leaving long_agentic_max_human_prompts,
    # interactive_gap_s, and the overnight thresholds at the plan's
    # values) took the corpus's mixed share from 23.7% to 17.6%, under
    # the <20% bar, without touching interactive/overnight's own share
    # of the corpus.
    "long_agentic_min_turns": 30,
    "interactive_gap_s": 5 * 60,
    "interactive_max_subagents": 2,
}

#: Default thresholds for :func:`classify_purpose`, same override shape.
DEFAULT_PURPOSE_THRESHOLDS: dict = {
    "local_llm_min_hits": 3,
    "agent_fanout_min_calls": 3,
    "test_triage_min_hits": 3,
    "planning_max_edit_turns": 2,
    "docs_min_assistant_turns": 5,
    "docs_min_edit_turns": 3,
    "refactor_min_edit_turns": 10,
    "refactor_min_test_hits": 1,
}

#: Bash ``cmd_prefix`` substrings identifying a call into a local LLM
#: server (plan "Classification" section / "Other clients").
_LOCAL_LLM_MARKERS = (
    "localhost:1234",
    "127.0.0.1:1234",
    "lmstudio",
    "ollama",
    "/v1/chat/completions",
)

#: ``cmd_prefix`` prefixes identifying a test-runner invocation.
_TEST_TOOL_PREFIXES = ("pytest", "dotnet test", "npm test", "npx vitest", "go test", "cargo test")


# -- SessionFeatures ----------------------------------------------------------


@dataclass(slots=True)
class SessionFeatures:
    """Numeric/boolean signals reduced from one session, never message
    text or paths. Feeds :func:`classify_mode`/:func:`classify_purpose`.
    """

    human_prompts: int = 0
    human_gap_median_s: float | None = None
    human_gap_max_s: float | None = None
    assistant_turns: int = 0
    subagent_count: int = 0
    max_spawn_depth: int = 0
    has_chain: bool = False
    span_s: float = 0.0
    queue_ops: int = 0
    compactions: int = 0
    task_notifications: int = 0
    peer_messages: int = 0
    #: Count of ``WorkflowRun``s for this session (WP8 not yet written;
    #: see the module docstring). Caller-supplied, default 0.
    workflows: int = 0
    #: Carry-through value (see the module docstring); caller-supplied.
    entrypoint: str | None = None
    local_llm_hits: int = 0
    agent_tool_calls: int = 0
    workflow_tool_calls: int = 0
    test_tool_hits: int = 0
    review_markers: int = 0
    plan_mode_events: int = 0
    edit_turns: int = 0
    read_turns: int = 0
    start_local_hour: int | None = None
    end_local_hour: int | None = None


# -- Timestamp / timezone helpers -------------------------------------------


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ts_range(transcripts: Iterable[TranscriptResult | None]) -> tuple[str | None, str | None]:
    """Earliest/latest ``Turn.ts`` across every turn in ``transcripts``, as
    the original ISO strings (not parsed ``datetime``s) so callers that
    just want to store them never need to reformat.
    """
    first: datetime | None = None
    first_raw: str | None = None
    last: datetime | None = None
    last_raw: str | None = None
    for result in transcripts:
        if result is None:
            continue
        for turn in result.turns:
            dt = _parse_ts(turn.ts)
            if dt is None:
                continue
            if first is None or dt < first:
                first, first_raw = dt, turn.ts
            if last is None or dt > last:
                last, last_raw = dt, turn.ts
    return first_raw, last_raw


def _span_seconds(first_ts: str | None, last_ts: str | None) -> float:
    first = _parse_ts(first_ts)
    last = _parse_ts(last_ts)
    if first is None or last is None:
        return 0.0
    return max(0.0, (last - first).total_seconds())


def _human_text_timestamps(top: TranscriptResult) -> list[datetime]:
    stamps = []
    for event in top.events:
        if event.kind != EventKind.HUMAN_TEXT:
            continue
        dt = _parse_ts(event.ts)
        if dt is not None:
            stamps.append(dt)
    stamps.sort()
    return stamps


def _median_and_max_gap(stamps: list[datetime]) -> tuple[float | None, float | None]:
    if len(stamps) < 2:
        return None, None
    gaps = [(later - earlier).total_seconds() for earlier, later in zip(stamps, stamps[1:])]
    return statistics.median(gaps), max(gaps)


def _local_hour(ts: str | None, tz: str | None) -> int | None:
    """Convert ``ts`` (a ``Turn``/``Event`` UTC ISO timestamp) to the hour
    of day in ``tz`` (an IANA name), or the machine's own local zone when
    ``tz`` is falsy or can't be resolved (see the module docstring).
    """
    dt = _parse_ts(ts)
    if dt is None:
        return None
    if tz:
        try:
            localized = dt.astimezone(ZoneInfo(tz))
        except (ZoneInfoNotFoundError, ValueError):
            localized = dt.astimezone()
    else:
        localized = dt.astimezone()
    return localized.hour


# -- extract_features ---------------------------------------------------------


def _matches_local_llm(cmd_prefix: str) -> bool:
    lowered = cmd_prefix.lower()
    return any(marker in lowered for marker in _LOCAL_LLM_MARKERS)


def _matches_test_tool(cmd_prefix: str) -> bool:
    return cmd_prefix.lstrip().startswith(_TEST_TOOL_PREFIXES)


def extract_features(
    top: TranscriptResult,
    subs: list[TranscriptResult] | None,
    tz: str | None = None,
    *,
    workflows: int = 0,
    entrypoint: str | None = None,
) -> SessionFeatures:
    """Reduce one session (``top`` plus its ``subs``) to a
    :class:`SessionFeatures` bundle. See the module docstring for which
    signals are ``top``-only vs. summed across ``top`` and every
    transcript in ``subs``, and for the ``workflows``/``entrypoint``
    keyword-only parameters (data ``TranscriptResult`` alone can't supply
    yet — see the module docstring).
    """
    subs = subs or []
    all_transcripts = (top, *subs)

    human_stamps = _human_text_timestamps(top)
    human_gap_median_s, human_gap_max_s = _median_and_max_gap(human_stamps)

    first_ts, last_ts = _ts_range(all_transcripts)
    span_s = _span_seconds(first_ts, last_ts)

    assistant_turns = sum(1 for turn in top.turns if not turn.is_synthetic)

    max_spawn_depth = max((s.meta.spawn_depth for s in subs), default=0)
    has_chain = max_spawn_depth >= 2 or any(s.meta.parent_agent_id for s in subs)

    queue_ops = 0
    compactions = 0
    task_notifications = 0
    peer_messages = 0
    plan_mode_events = 0
    for result in all_transcripts:
        for event in result.events:
            if event.kind == EventKind.QUEUE_OPERATION:
                queue_ops += 1
            elif event.kind == EventKind.COMPACT_BOUNDARY:
                compactions += 1
            elif event.kind == EventKind.TASK_NOTIFICATION:
                task_notifications += 1
            elif event.kind == EventKind.PEER_MESSAGE:
                peer_messages += 1
            elif event.kind == EventKind.CACHE_SIGNAL and event.subkind == "plan_mode":
                plan_mode_events += 1

    local_llm_hits = 0
    agent_tool_calls = 0
    workflow_tool_calls = 0
    test_tool_hits = 0
    review_markers = 0
    edit_turns = 0
    read_turns = 0
    for result in all_transcripts:
        if result.meta.agent_type and "review" in result.meta.agent_type.lower():
            review_markers += 1
        for turn in result.turns:
            if "Agent" in turn.tool_names:
                agent_tool_calls += 1
            if "Workflow" in turn.tool_names:
                workflow_tool_calls += 1
            if turn.edit_kind == "real":
                edit_turns += 1
            if any(name in turn.tool_names for name in ("Read", "Grep", "Glob")):
                read_turns += 1
            if turn.attribution_skill and "review" in turn.attribution_skill.lower():
                review_markers += 1
            if turn.cmd_prefix:
                if "Bash" in turn.tool_names and _matches_local_llm(turn.cmd_prefix):
                    local_llm_hits += 1
                if _matches_test_tool(turn.cmd_prefix):
                    test_tool_hits += 1

    start_local_hour = _local_hour(first_ts, tz)
    end_local_hour = _local_hour(last_ts, tz)

    return SessionFeatures(
        human_prompts=len(human_stamps),
        human_gap_median_s=human_gap_median_s,
        human_gap_max_s=human_gap_max_s,
        assistant_turns=assistant_turns,
        subagent_count=len(subs),
        max_spawn_depth=max_spawn_depth,
        has_chain=has_chain,
        span_s=span_s,
        queue_ops=queue_ops,
        compactions=compactions,
        task_notifications=task_notifications,
        peer_messages=peer_messages,
        workflows=workflows,
        entrypoint=entrypoint,
        local_llm_hits=local_llm_hits,
        agent_tool_calls=agent_tool_calls,
        workflow_tool_calls=workflow_tool_calls,
        test_tool_hits=test_tool_hits,
        review_markers=review_markers,
        plan_mode_events=plan_mode_events,
        edit_turns=edit_turns,
        read_turns=read_turns,
        start_local_hour=start_local_hour,
        end_local_hour=end_local_hour,
    )


# -- classify_mode / classify_purpose ----------------------------------------


def classify_mode(f: SessionFeatures, thresholds: dict | None = None) -> tuple[str, dict]:
    """First-match-wins mode classification (plan "Classification"
    section): overnight -> long-agentic -> interactive -> mixed.
    """
    t = {**DEFAULT_MODE_THRESHOLDS, **(thresholds or {})}

    if (
        f.span_s > t["overnight_span_s"]
        and f.human_gap_max_s is not None
        and f.human_gap_max_s > t["overnight_gap_s"]
    ):
        return "overnight", {"span_s": f.span_s, "human_gap_max_s": f.human_gap_max_s}

    if f.has_chain:
        return "long-agentic", {"has_chain": f.has_chain}
    if f.subagent_count >= 1 and f.human_prompts <= t["long_agentic_max_human_prompts"]:
        return "long-agentic", {
            "subagent_count": f.subagent_count,
            "human_prompts": f.human_prompts,
        }
    if (
        f.assistant_turns >= t["long_agentic_min_turns"]
        and f.human_prompts <= t["long_agentic_max_human_prompts"]
    ):
        return "long-agentic", {
            "assistant_turns": f.assistant_turns,
            "human_prompts": f.human_prompts,
        }

    if (
        f.human_gap_median_s is not None
        and f.human_gap_median_s < t["interactive_gap_s"]
        and f.subagent_count <= t["interactive_max_subagents"]
    ):
        return "interactive", {
            "human_gap_median_s": f.human_gap_median_s,
            "subagent_count": f.subagent_count,
        }

    return "mixed", {
        "span_s": f.span_s,
        "human_gap_max_s": f.human_gap_max_s,
        "human_gap_median_s": f.human_gap_median_s,
        "subagent_count": f.subagent_count,
        "assistant_turns": f.assistant_turns,
        "human_prompts": f.human_prompts,
        "has_chain": f.has_chain,
    }


def classify_purpose(f: SessionFeatures, thresholds: dict | None = None) -> tuple[str, dict]:
    """First-match-wins purpose classification (plan "Classification"
    section, with the brief's ``docs-or-light-edit`` substitution for the
    unreachable path-based "docs" rule — see the module docstring).
    """
    t = {**DEFAULT_PURPOSE_THRESHOLDS, **(thresholds or {})}

    if f.local_llm_hits >= t["local_llm_min_hits"]:
        return "local-llm-pipeline", {"local_llm_hits": f.local_llm_hits}

    if f.workflows >= 1 or f.workflow_tool_calls >= 1:
        return "workflow-run", {
            "workflows": f.workflows,
            "workflow_tool_calls": f.workflow_tool_calls,
        }

    if f.agent_tool_calls >= t["agent_fanout_min_calls"]:
        return "agent-fanout", {"agent_tool_calls": f.agent_tool_calls}

    if f.review_markers >= 1 and f.edit_turns == 0:
        return "review", {"review_markers": f.review_markers, "edit_turns": f.edit_turns}

    if f.test_tool_hits >= t["test_triage_min_hits"] and f.test_tool_hits >= f.edit_turns:
        return "test-triage", {"test_tool_hits": f.test_tool_hits, "edit_turns": f.edit_turns}

    if f.plan_mode_events >= 1 and f.edit_turns <= t["planning_max_edit_turns"]:
        return "planning", {
            "plan_mode_events": f.plan_mode_events,
            "edit_turns": f.edit_turns,
        }

    if (
        f.assistant_turns >= t["docs_min_assistant_turns"]
        and f.edit_turns >= t["docs_min_edit_turns"]
        and f.read_turns <= f.edit_turns
        and f.test_tool_hits == 0
    ):
        return "docs-or-light-edit", {
            "assistant_turns": f.assistant_turns,
            "edit_turns": f.edit_turns,
            "read_turns": f.read_turns,
        }

    if f.edit_turns >= t["refactor_min_edit_turns"] and f.test_tool_hits >= t["refactor_min_test_hits"]:
        return "refactor", {"edit_turns": f.edit_turns, "test_tool_hits": f.test_tool_hits}

    return "general-dev", {
        "assistant_turns": f.assistant_turns,
        "edit_turns": f.edit_turns,
        "test_tool_hits": f.test_tool_hits,
    }


def classify_session(
    top: TranscriptResult,
    subs: list[TranscriptResult] | None,
    overrides: dict,
    tz: str | None,
    *,
    workflows: int = 0,
    entrypoint: str | None = None,
    mode_thresholds: dict | None = None,
    purpose_thresholds: dict | None = None,
) -> Classification:
    """Classify one session, applying a ``sessions.toml`` override (see
    ``config.load_session_overrides``) ahead of the rules. ``overrides``
    is keyed by session id, each value optionally holding ``"mode"``
    and/or ``"purpose"`` (independently — a session can override one and
    let the other run through the rules).
    """
    features = extract_features(top, subs, tz, workflows=workflows, entrypoint=entrypoint)
    override = overrides.get(top.meta.session_id, {}) if overrides else {}

    if "mode" in override:
        mode = override["mode"]
        mode_source = "override"
        mode_evidence = {"value": mode}
    else:
        mode, mode_evidence = classify_mode(features, mode_thresholds)
        mode_source = "rule"

    if "purpose" in override:
        purpose = override["purpose"]
        purpose_source = "override"
        purpose_evidence = {"value": purpose}
    else:
        purpose, purpose_evidence = classify_purpose(features, purpose_thresholds)
        purpose_source = "rule"

    return Classification(
        mode=mode,
        mode_source=mode_source,
        mode_evidence=mode_evidence,
        purpose=purpose,
        purpose_source=purpose_source,
        purpose_evidence=purpose_evidence,
    )


# -- SessionRecord construction / grouping / report section -----------------


def build_session_record(
    top: TranscriptResult,
    subs: list[TranscriptResult],
    workflows: list[WorkflowRun],
    classification: Classification,
    slug: str,
) -> SessionRecord:
    """Build one ``SessionRecord``. ``archetype``/``snapshot_id``/
    ``profile_id`` are left ``None`` (WP8/WP10/WP12's job to populate).
    """
    first_ts, last_ts = _ts_range([top, *subs])
    span_s = _span_seconds(first_ts, last_ts)
    return SessionRecord(
        session_id=top.meta.session_id,
        slug=slug,
        first_ts=first_ts,
        last_ts=last_ts,
        span_s=span_s,
        top=top,
        subs=list(subs),
        workflows=list(workflows),
        classification=classification,
    )


_GROUP_KEYS = frozenset({"mode", "purpose", "project", "model", "agent", "entrypoint"})


def _dominant_model(top: TranscriptResult | None) -> str:
    if top is None:
        return "unknown"
    counts: dict[str, int] = {}
    for turn in top.turns:
        if turn.is_synthetic or not turn.model:
            continue
        counts[turn.model] = counts.get(turn.model, 0) + 1
    if not counts:
        return "unknown"
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _dominant_agent(record: SessionRecord) -> str:
    if record.top is not None:
        settings = record.top.diagnostics.agent_settings
        if settings:
            return max(settings.items(), key=lambda kv: (kv[1], kv[0]))[0]
    counts: dict[str, int] = {}
    for sub in record.subs:
        if sub.meta.agent_type:
            counts[sub.meta.agent_type] = counts.get(sub.meta.agent_type, 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return "unknown"


def _group_key_value(record: SessionRecord, key: str) -> str:
    if key == "mode":
        return record.classification.mode if record.classification else "unknown"
    if key == "purpose":
        return record.classification.purpose if record.classification else "unknown"
    if key == "project":
        return record.top.meta.project_slug if record.top else "unknown"
    if key == "model":
        return _dominant_model(record.top)
    if key == "agent":
        return _dominant_agent(record)
    if key == "entrypoint":
        # No data source exists yet — see the module docstring's proposed
        # model.py addition (SessionRecord.entrypoint).
        return "unknown"
    raise ValueError(f"unknown group_sessions key: {key!r} (expected one of {sorted(_GROUP_KEYS)})")


def group_sessions(records: list[SessionRecord], key: str) -> dict[str, list[SessionRecord]]:
    """Group ``records`` by ``key`` (``"mode"``, ``"purpose"``,
    ``"project"``, ``"model"``, ``"agent"``, or ``"entrypoint"``). See
    ``_group_key_value`` and the module docstring for how each key's
    value is derived (``"entrypoint"`` always groups into a single
    ``"unknown"`` bucket today).
    """
    if key not in _GROUP_KEYS:
        raise ValueError(f"unknown group_sessions key: {key!r} (expected one of {sorted(_GROUP_KEYS)})")
    groups: dict[str, list[SessionRecord]] = {}
    for record in records:
        group_key = _group_key_value(record, key)
        groups.setdefault(group_key, []).append(record)
    return groups


def _turns_count(record: SessionRecord) -> int:
    total = len(record.top.turns) if record.top else 0
    total += sum(len(s.turns) for s in record.subs)
    return total


def _group_summary_table(
    records: list[SessionRecord],
    features_by_id: dict[str, SessionFeatures],
    *,
    key: str,
    name: str,
    title: str,
) -> Table:
    groups = group_sessions(records, key)
    columns = [
        Column(key="value", label=key.capitalize(), kind="str"),
        Column(key="sessions", label="Sessions", kind="int"),
        Column(key="turns", label="Turns", kind="int"),
        Column(key="subagents", label="Subagents", kind="int"),
        Column(key="median_span_s", label="Median span", kind="secs"),
        Column(key="human_prompts_median", label="Human prompts (median)", kind="float"),
    ]
    rows: list[list] = []
    for value in sorted(groups, key=lambda v: (-len(groups[v]), v)):
        group = groups[value]
        turns_total = sum(_turns_count(r) for r in group)
        subagents_total = sum(len(r.subs) for r in group)
        spans = [r.span_s for r in group]
        prompts = [
            features_by_id[r.session_id].human_prompts
            for r in group
            if r.session_id in features_by_id
        ]
        rows.append(
            [
                value,
                len(group),
                turns_total,
                subagents_total,
                statistics.median(spans) if spans else None,
                statistics.median(prompts) if prompts else None,
            ]
        )
    return Table(name=name, title=title, columns=columns, rows=rows)


_PER_SESSION_ROW_CAP = 50


def _per_session_table(records: list[SessionRecord]) -> Table:
    columns = [
        Column(key="session_id", label="Session", kind="str"),
        Column(key="slug", label="Project", kind="str"),
        Column(key="mode", label="Mode", kind="str"),
        Column(key="purpose", label="Purpose", kind="str"),
        Column(key="sources", label="Sources", kind="str"),
        Column(key="first_ts", label="Started", kind="str"),
        Column(key="span_s", label="Span", kind="secs"),
        Column(key="turns", label="Turns", kind="int"),
        Column(key="subs", label="Subagents", kind="int"),
    ]
    ordered = sorted(records, key=lambda r: r.first_ts or "", reverse=True)
    shown = ordered[:_PER_SESSION_ROW_CAP]
    rows = []
    for record in shown:
        classification = record.classification
        mode = classification.mode if classification else ""
        purpose = classification.purpose if classification else ""
        sources = (
            f"{classification.mode_source}/{classification.purpose_source}" if classification else ""
        )
        rows.append(
            [
                record.session_id,
                record.slug,
                mode,
                purpose,
                sources,
                record.first_ts or "",
                record.span_s,
                _turns_count(record),
                len(record.subs),
            ]
        )
    notes = []
    if len(records) > _PER_SESSION_ROW_CAP:
        notes.append(
            f"Showing the {_PER_SESSION_ROW_CAP} most recently started of {len(records)} sessions."
        )
    return Table(name="sessions_detail", title="Sessions", columns=columns, rows=rows, notes=notes)


def build_section(records: list[SessionRecord]) -> Section:
    """Build the "Sessions" report section: summary tables by mode and by
    purpose, plus a per-session detail table capped at 50 rows.
    """
    features_by_id = {
        record.session_id: extract_features(record.top, record.subs, tz=None)
        for record in records
        if record.top is not None
    }
    mode_table = _group_summary_table(
        records, features_by_id, key="mode", name="sessions_by_mode", title="Sessions by mode"
    )
    purpose_table = _group_summary_table(
        records, features_by_id, key="purpose", name="sessions_by_purpose", title="Sessions by purpose"
    )
    per_session_table = _per_session_table(records)
    return Section(key="sessions", title="Sessions", tables=[mode_table, purpose_table, per_session_table])


__all__ = [
    "SessionFeatures",
    "DEFAULT_MODE_THRESHOLDS",
    "DEFAULT_PURPOSE_THRESHOLDS",
    "extract_features",
    "classify_mode",
    "classify_purpose",
    "classify_session",
    "build_session_record",
    "group_sessions",
    "build_section",
]
