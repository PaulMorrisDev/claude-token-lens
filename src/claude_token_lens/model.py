"""Frozen data-model and table contract for claude-token-lens.

This module is the frozen contract referenced throughout the project plan
(Appendix A1). Every dataclass and enum here is what every later work
package (parsing, pricing, RE-CACHE, TTL, classification, rendering, the
service store, profiles) builds on top of. To keep that possible:

- Later work packages MAY add new fields, as long as they carry a default
  so existing call sites keep constructing valid instances.
- Later work packages MUST NOT rename or remove an existing field, or
  change its meaning, without a coordinated ``SCHEMA_VERSION`` bump in
  ``__init__.py`` (a bump invalidates the on-disk digest cache).
- This module imports only the standard library. It must never import
  from any other ``claude_token_lens`` submodule, so that every later
  package can depend on it without a cycle.
- No field here may ever hold message text, tool result content, a full
  file path, or a full shell command. See SECURITY.md; ``cmd_prefix`` and
  ``preceding_cmd_prefix`` are capped at 40 characters for this reason.

Deviations from the plan's Appendix A1 that were necessary to make it a
concrete, constructible contract (proposed here, not silently changed):

- ``ReportModel.meta`` is specified in the plan as a nested structure
  ``(tool_version, generated_at, window, projects, pricing {path,
  version, sha8, currency, coverage_pct}, thresholds, billing_mode)``
  without naming the nested dataclasses. Two small dataclasses,
  ``PricingMeta`` and ``ReportMeta``, are introduced to hold that
  structure so ``ReportModel`` can be constructed with defaults like
  every other contract type.

WP1 (parse.py, events.py, jsonl.py, discovery.py) additions, all with
defaults so every existing call site keeps working:

- ``Event.size_chars: int | None`` — length of an attachment line's
  ``rendered`` field, when present. Requested by the WP1 brief for
  CONTEXT_INJECT/REMINDER/CACHE_SIGNAL/HOOK_OUTPUT/ATTACHMENT sizing.
- ``Event.detail: dict`` — small numeric/short-string detail a kind needs
  beyond kind/subkind (API_ERROR's ``status``/``retryAttempt``,
  MODEL_FALLBACK's ``originalModel``/``fallbackModel``, and the three
  delta attachment types' ``added``/``removed`` counts). Never message
  text, a full path, or a command.
- ``Turn.inference_geo: str | None`` — from ``usage.inference_geo``,
  needed for the documented 1.1x geo multiplier (plan Pricing section).
- ``Diagnostics.oversized_lines: int`` — lines ``jsonl.iter_lines`` skips
  for exceeding ``MAX_LINE_BYTES`` (8 MB), counted without being parsed.
  Not in Appendix A1's ``Diagnostics`` list; added because the WP1 brief
  requires counting them somewhere and no existing field fits.

- ``ReportMeta.assumptions`` (``list[str]``, default empty) is added by
  WP9 (renderers). The plan's Markdown/HTML layout requires an
  ``## Assumptions`` block (e.g. the TTL simulation's stated
  assumptions), and analytics packages (TTL, RE-CACHE, ...) need a place
  to push that text onto the report without the renderers inventing it.
  Defaulting to ``[]`` keeps every existing call site valid.

Independent-review follow-up fixes (post-WP1/WP2/WP7), all with defaults:

- ``Diagnostics.trailing_events: int = 0`` — events observed after the
  last finalised turn in a transcript (nothing left to attach them to).
  See ``parse.py``'s two-buffer rewrite.
- ``Diagnostics.replayed_lines: int = 0`` — non-blank lines skipped
  because their ``uuid`` was already seen earlier in the same file
  (transcripts replay whole blocks on rewind/resume).
- ``Diagnostics.timestamp_parse_failures: int = 0`` — a priced turn's
  ``timestamp`` field was present but could not be parsed.
- ``TranscriptMeta.tool_use_id: str | None = None`` — a subagent's
  ``.meta.json`` ``toolUseId``, linking the subagent back to the parent
  turn that spawned it (topology/workstyle, plan Appendix A1).
- ``Diagnostics.agent_settings: dict``, ``Diagnostics.modes: dict`` —
  ``agent-setting``/``mode`` lines stay ignored as ``Event``s (too
  harness-plumbing to attach to a turn) but their *value* is carried as a
  counter instead of being dropped outright, since it's evidence for
  archetype detection (which persona/mode a session ran under).
- ``Diagnostics.attachment_catch_all: dict`` — attachment types that
  fell into the generic ``ATTACHMENT`` kind, counted by type, so a new
  attachment type shows up in Diagnostics the moment it's seen rather
  than only via a manual scan of ``TranscriptResult.events``.

Batch C additive fields (all with defaults, per this module's own rule):

- ``Turn.tool_use_ids: tuple[str, ...] = ()`` — the ``id`` of every
  ``tool_use`` content block in this turn, in encounter order. Lets
  ``topology.py``'s skill roll-up join a subagent back to its parent turn
  from already-parsed ``Turn``s instead of re-scanning the raw JSONL (see
  ``topology.index_tool_use_ids``, now a fallback for transcripts parsed
  before this field existed).
- ``WorkflowRun.status: str | None = None`` — the run file's own
  ``status`` (``"completed"``/``"killed"`` observed), filled by
  ``workflows.parse_workflow_file``.
- ``WorkflowRun.phase_titles: tuple[str, ...] = ()`` — each phase entry's
  ``title`` only, never its ``detail`` (workflow source/prompt text) —
  filled by ``workflows.parse_workflow_file``.
- ``SessionRecord.entrypoint: str | None = None`` — carried through from
  ``top.meta.entrypoint`` by ``classify.build_session_record``; previously
  a caller-supplied ``extract_features`` keyword with no real data source
  (see ``classify.py``'s module docstring).
- ``TranscriptMeta.provider: str | None = None`` — the API surface a
  transcript's turns were billed through (``"anthropic"`` | ``"bedrock"``
  | ``"vertex"`` | ``"foundry"``), derived from model-id form.
- ``TranscriptMeta.entrypoint: str | None = None`` — the first non-empty
  ``entrypoint`` field seen anywhere in the transcript's raw lines
  (``cli``/``sdk-python``/... observed), set by ``parse.parse_transcript``
  alongside the existing ``claude_version`` field (also first-seen, from
  each line's own ``version`` field — previously declared but never
  populated by any module).

Discovery fix (workflow-nested subagents), also additive:

- ``TranscriptMeta.workflow_run_id: str | None = None`` — the run id of
  the ``<session_id>/subagents/workflows/<run_id>/agent-*.jsonl``
  directory a workflow-nested subagent lives under (see
  ``workflows.py``'s module docstring on this layout), set by
  ``discovery.load_meta``. ``None`` for every other transcript kind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class EventKind(StrEnum):
    """Every non-assistant JSONL line becomes one Event of one of these
    kinds. Ordered as in plan Appendix A2's detection table (roughly
    highest to lowest ``preceding_primary`` precedence, with the harness
    plumbing kinds — QUEUE_OPERATION through TOOL_RESULT — in the middle
    and the catch-alls last).
    """

    COMPACT_BOUNDARY = "compact_boundary"
    COMPACT_SUMMARY = "compact_summary"
    API_ERROR = "api_error"
    MODEL_FALLBACK = "model_fallback"
    LOCAL_COMMAND = "local_command"
    HOOK_OUTPUT = "hook_output"
    CACHE_SIGNAL = "cache_signal"
    REMINDER = "reminder"
    CONTEXT_INJECT = "context_inject"
    QUEUE_OPERATION = "queue_operation"
    ATTACHMENT = "attachment"
    META = "meta"
    TOOL_DENIAL = "tool_denial"
    TOOL_RESULT = "tool_result"
    TASK_NOTIFICATION = "task_notification"
    PEER_MESSAGE = "peer_message"
    SLASH_COMMAND = "slash_command"
    SCHEDULED_TASK = "scheduled_task"
    INTERRUPT = "interrupt"
    HUMAN_TEXT = "human_text"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class Event:
    """One non-assistant JSONL line, attached to the next finalised turn."""

    kind: EventKind = EventKind.UNKNOWN
    #: attachment.type | system.subtype | operation | toolDenialKind | None
    subkind: str | None = None
    ts: str | None = None
    # Compaction-only fields (COMPACT_BOUNDARY); None for every other kind.
    pre_tokens: int | None = None
    post_tokens: int | None = None
    dropped_tokens: int | None = None
    duration_ms: int | None = None
    trigger: str | None = None
    #: WP1 addition (plan deviation, see module docstring): length of the
    #: attachment's ``rendered`` field, when present. Never the rendered
    #: text itself.
    size_chars: int | None = None
    #: WP1 addition (plan deviation, see module docstring): small numeric
    #: detail for kinds whose meaning needs more than kind/subkind, e.g.
    #: API_ERROR's ``status``/``retryAttempt``, MODEL_FALLBACK's
    #: ``originalModel``/``fallbackModel``, or the delta attachment types'
    #: ``added``/``removed`` counts. Never message text or full paths.
    detail: dict = field(default_factory=dict)


@dataclass(slots=True)
class Turn:
    """One priced assistant turn: assistant lines grouped by ``message.id``
    (fallback ``request_id``, then ``uuid``), with the events that preceded
    it since the previous finalised turn.
    """

    message_id: str = ""
    request_id: str = ""
    #: 1-based index over priced turns in this transcript.
    turn_index: int = 0
    ts: str = ""
    gap_s: float | None = None
    model: str = ""
    service_tier: str | None = None
    is_synthetic: bool = False
    effort: str | None = None
    per_turn_effort: str | None = None

    input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    web_search_requests: int = 0
    web_fetch_requests: int = 0
    #: cache_creation.ephemeral_5m_input_tokens / ephemeral_1h_input_tokens
    cc_5m: int = 0
    cc_1h: int = 0
    ctx: int = 0

    tool_names: tuple[str, ...] = ()
    cmd_prefix: str | None = None  # <= 40 chars
    edit_kind: str | None = None  # "real" | "scratch" | None

    attribution_mcp_server: str | None = None
    attribution_mcp_tool: str | None = None
    attribution_skill: str | None = None

    #: "Bash" | "PowerShell" | <tool name> | "none" | "n/a"
    preceding_tool: str = "none"
    preceding_cmd_prefix: str | None = None  # <= 40 chars
    preceding_event_kinds: tuple[EventKind, ...] = ()
    preceding_attachment_types: tuple[str, ...] = ()
    preceding_primary: EventKind = EventKind.UNKNOWN

    is_recache: bool = False
    #: "full-expiry" | "prefix-invalidated" | None
    recache_signature: str | None = None

    #: WP1 addition (plan deviation, see module docstring): from
    #: ``usage.inference_geo``. Drives the documented 1.1x geo multiplier.
    inference_geo: str | None = None

    #: Batch C addition (see module docstring): every ``tool_use`` block's
    #: ``id`` in this turn, in encounter order. Ids only — never the tool
    #: input or result content.
    tool_use_ids: tuple[str, ...] = ()

    #: Coordinator follow-up (WP12a diversity fixtures): set when this
    #: turn's ``usage`` had no nested ``cache_creation`` object at all
    #: (older, pre-5m/1h-split Claude Code JSONL) - ``cc_5m``/``cc_1h``
    #: are both 0 even though ``cache_creation_tokens`` may be nonzero,
    #: because the write's TTL split was never recorded, not because
    #: nothing was written. ``ttl.py``'s simulation functions normalize
    #: this by attributing the flat total to the transcript's own
    #: ``dominant_ttl`` (or "5m" when that's "mixed"/"none") before
    #: replaying any policy - see ``ttl.normalize_ttl_split``.
    ttl_split_unknown: bool = False


@dataclass(slots=True)
class TranscriptMeta:
    """Identity and provenance of one transcript file."""

    path: str = ""
    kind: str = "top-level"  # "top-level" | "subagent" | "workflow-agent"
    session_id: str = ""
    agent_id: str | None = None
    agent_type: str | None = None
    description_len: int = 0
    spawn_depth: int = 0
    parent_agent_id: str | None = None
    agent_model_alias: str | None = None
    request_shape: str | None = None
    worktree_branch_present: bool = False
    stopped_by_user: bool | None = None
    project_slug: str = ""
    claude_version: str | None = None
    mtime_ns: int = 0
    size_bytes: int = 0
    #: Independent-review addition (see module docstring): the subagent's
    #: ``.meta.json`` ``toolUseId``, linking it to the parent turn that
    #: spawned it.
    tool_use_id: str | None = None
    #: Batch C addition (see module docstring): the API surface these
    #: turns were billed through, derived from model-id form.
    provider: str | None = None
    #: Batch C addition (see module docstring): the first non-empty
    #: ``entrypoint`` field seen anywhere in the transcript's raw lines.
    entrypoint: str | None = None
    #: Discovery fix addition (see module docstring): the workflow run id
    #: a workflow-nested subagent lives under.
    workflow_run_id: str | None = None


@dataclass(slots=True)
class Diagnostics:
    """Parse-quality counters surfaced in the Diagnostics report section."""

    lines: int = 0
    unparsable_lines: int = 0
    truncated_final_line: bool = False
    assistant_lines: int = 0
    distinct_turns: int = 0
    synthetic_turns: int = 0
    turns_missing_usage: int = 0
    ttl_sum_mismatch: int = 0
    late_duplicate_ids: int = 0
    ignored_line_types: dict = field(default_factory=dict)
    #: WP1 addition (plan deviation, see module docstring): lines skipped
    #: by ``jsonl.iter_lines`` for exceeding ``MAX_LINE_BYTES``, counted
    #: without being parsed.
    oversized_lines: int = 0
    #: Independent-review addition (see module docstring): events observed
    #: after the last finalised turn, attached to no turn.
    trailing_events: int = 0
    #: Independent-review addition (see module docstring): lines skipped
    #: because their ``uuid`` had already been seen earlier in this file.
    replayed_lines: int = 0
    #: Independent-review addition (see module docstring): a priced turn's
    #: timestamp was present but failed to parse.
    timestamp_parse_failures: int = 0
    #: Independent-review addition: ``agent-setting`` lines are ignored as
    #: events (see events.py) but their ``agentSetting`` value is useful
    #: for archetype detection, so it's counted here instead of dropped
    #: entirely: value -> occurrence count.
    agent_settings: dict = field(default_factory=dict)
    #: Independent-review addition: ``mode`` lines are ignored as events
    #: but their value (normal/plan/auto) is counted here: value -> count.
    modes: dict = field(default_factory=dict)
    #: Independent-review addition: attachment types that fell into the
    #: generic ATTACHMENT catch-all kind (A2's "any attachment type not
    #: listed above" row), counted by type for next-release triage.
    attachment_catch_all: dict = field(default_factory=dict)
    #: Coordinator follow-up (WP12a diversity fixtures): turns whose
    #: usage had no nested ``cache_creation`` object at all (see
    #: ``Turn.ttl_split_unknown``). This is a format difference, not an
    #: invariant breach, so it's counted separately from
    #: ``ttl_sum_mismatch`` rather than folded into it.
    pre_split_turns: int = 0


@dataclass(slots=True)
class TranscriptResult:
    """The parsed output of one transcript file: its turns and events plus
    parse diagnostics and raw tool-result size accounting.
    """

    meta: TranscriptMeta = field(default_factory=TranscriptMeta)
    turns: list[Turn] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    tool_result_chars: dict = field(default_factory=dict)
    tool_result_calls: dict = field(default_factory=dict)


@dataclass(slots=True)
class WorkflowRun:
    """One ``<session>/workflows/wf_*.json`` run."""

    run_id: str = ""
    session_id: str = ""
    agent_count: int = 0
    phases: int = 0
    started: str | None = None
    finished: str | None = None
    cost: float = 0.0
    #: Batch C addition (see module docstring): the run file's own
    #: ``status`` (``"completed"``/``"killed"`` observed).
    status: str | None = None
    #: Batch C addition (see module docstring): each phase's ``title``
    #: only — never ``detail``, which carries workflow source/prompt text.
    phase_titles: tuple[str, ...] = ()


@dataclass(slots=True)
class Classification:
    """Mode/purpose classification for one session, with the evidence that
    produced it.
    """

    mode: str = ""
    mode_source: str = ""
    mode_evidence: dict = field(default_factory=dict)
    purpose: str = ""
    purpose_source: str = ""
    purpose_evidence: dict = field(default_factory=dict)


@dataclass(slots=True)
class SessionRecord:
    """One top-level session: its own transcript, its subagent and
    workflow transcripts, and the classification derived from them.
    """

    session_id: str = ""
    slug: str = ""
    first_ts: str | None = None
    last_ts: str | None = None
    span_s: float = 0.0
    top: TranscriptResult | None = None
    subs: list[TranscriptResult] = field(default_factory=list)
    workflows: list[WorkflowRun] = field(default_factory=list)
    classification: Classification | None = None
    archetype: str | None = None
    snapshot_id: str | None = None
    profile_id: str | None = None
    #: Batch C addition (see module docstring): carried through from
    #: ``top.meta.entrypoint`` by ``classify.build_session_record``.
    entrypoint: str | None = None


@dataclass(slots=True)
class CostBreakdown:
    """Priced cost for one turn or one aggregate, split by token type."""

    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_write_cost: float = 0.0
    cache_read_cost: float = 0.0
    total: float = 0.0
    long_context_applied: bool = False
    model_known: bool = False


@dataclass(slots=True)
class Column:
    """One table column: what it is called and how its values render."""

    key: str = ""
    label: str = ""
    #: "str" | "int" | "float" | "pct" | "money" | "tokens" | "secs"
    kind: str = "str"
    align: str | None = None


@dataclass(slots=True)
class Table:
    """A renderer-agnostic table: columns plus rows of raw values (not yet
    formatted — ``render.tables.format_cell`` does that per-column at
    render time).
    """

    name: str = ""
    title: str = ""
    columns: list[Column] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Section:
    """One report section: a heading, its tables, and free-text notes."""

    key: str = ""
    title: str = ""
    tables: list[Table] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Recommendation:
    """One actionable finding, with evidence traceable back into a
    specific report table so a test can assert every value it cites
    actually exists there.
    """

    id: str = ""
    severity: str = "info"  # "info" | "advice" | "action"
    category: str = "workflow"  # "settings" | "workflow" | "data"
    archetypes: tuple[str, ...] = ()
    title: str = ""
    action: str = ""
    lever: str | None = None  # settings key / frontmatter path / None
    #: list of (label, value, source_table, row_key)
    evidence: list = field(default_factory=list)


@dataclass(slots=True)
class PricingMeta:
    """Pricing provenance printed in the report header. Not itself named
    in plan Appendix A1 — see the module docstring's deviation note.
    """

    path: str | None = None
    version: str | None = None
    sha8: str | None = None
    currency: str = "USD"
    coverage_pct: float = 0.0


@dataclass(slots=True)
class ReportMeta:
    """``ReportModel.meta``: run identity, window and pricing provenance.
    Not itself named in plan Appendix A1 — see the module docstring's
    deviation note.
    """

    tool_version: str = ""
    generated_at: str = ""
    window: str = ""
    projects: tuple[str, ...] = ()
    pricing: PricingMeta = field(default_factory=PricingMeta)
    thresholds: dict = field(default_factory=dict)
    billing_mode: str = "api"  # "api" | "subscription"
    #: TTL/RE-CACHE/etc. assumption text, rendered as the report's
    #: "## Assumptions" block. See the module docstring's deviation note.
    assumptions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ReportModel:
    """The whole assembled report: what every renderer (Markdown, JSON,
    CSV, HTML) consumes.
    """

    meta: ReportMeta = field(default_factory=ReportMeta)
    sections: list[Section] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)


__all__ = [
    "EventKind",
    "Event",
    "Turn",
    "TranscriptMeta",
    "Diagnostics",
    "TranscriptResult",
    "WorkflowRun",
    "Classification",
    "SessionRecord",
    "CostBreakdown",
    "Column",
    "Table",
    "Section",
    "Recommendation",
    "PricingMeta",
    "ReportMeta",
    "ReportModel",
]
