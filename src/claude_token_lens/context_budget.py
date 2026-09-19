"""Context budget analytics (S1-context-budget): a clearly labelled
*estimate* of how a session's context window is spent before the model
sees any real work, plus whatever ground truth is available.

Motivation (the owner question this package answers): does this tool
track preloaded skills, the system prompt, and the autocompact buffer?
Claude Code's own ``/context`` view breaks the context window into
system prompt, system tools, MCP tools, custom agents, memory files
(the ``CLAUDE.md`` family), skills, messages, free space and the
autocompact buffer. A transcript never carries those sizes directly --
this module reconstructs an approximation from what IS captured (a
first-turn ``cache_creation`` baseline, HUMAN_TEXT/attachment
``size_chars``, a schema-2 config snapshot's ``content_layers``) and
says, in every column label and table note, that it is an estimate, not
Claude Code's own accounting. Where a genuine measurement exists (the
statusline payload's own ``context_window`` object, once
:mod:`statusline` has logged it -- see that module's docstring), this
module surfaces it as a separate, unlabelled-as-"est" table instead.

Three tables (:func:`build_section`, section key ``"context_budget"``):

- ``context_budget_baseline`` -- per project, plus one "all" row summing
  every project: the measured mean/median top-level first-turn
  ``cache_creation`` (:mod:`topology`'s own "session baseline" metric,
  duplicated here rather than read back off that section's table so this
  module works from ``TranscriptResult`` objects directly, matching the
  rest of this package's convention of small per-module accumulators),
  next to estimated buckets in tokens for human prompt, skills listing,
  memory files, custom agents and MCP tools, then a residual
  "system prompt and tools" bucket (the baseline minus every other known
  bucket, floored at 0).
- ``context_budget_autocompact`` -- per project: the configured
  ``autoCompactWindow`` from the latest schema-2 snapshot, the model's
  context window size (from a statusline ground-truth row when one is
  available, else an assumed 1,000,000/200,000 split on a ``"[1m]"``
  model alias), the *observed* effective autocompact threshold (median
  ``compactMetadata.preTokens`` over ``trigger == "auto"`` compactions --
  :func:`compaction.effective_autocompact_threshold`), the implied
  buffer, how many auto compactions were observed, and whether the
  observed threshold has drifted more than 10% from the configured
  window.
- ``context_budget_statusline`` -- one real, non-estimated line per
  session, present only once at least one usage-log row carries
  ``context_window`` fields (see :mod:`statusline`'s module docstring for
  how those columns get there).

Every chars/bytes-to-tokens conversion in this module uses the same
``chars / 4`` approximation the rest of the codebase already documents
(``topology._CHARS_PER_TOKEN_APPROX``, duplicated here as
:data:`_CHARS_PER_TOKEN_APPROX` per this project's established
"small helper constants are duplicated, not imported across modules"
convention -- see e.g. ``compaction.py``'s and ``usage.py``'s own module
docstrings for the same convention stated explicitly). No tokenizer is
ever run over transcript content.

Accumulator pattern: :class:`ContextBudgetStats` is fed one top-level
session at a time via :meth:`ContextBudgetStats.add_session` (mirroring
``topology.TopologyStats``/``compaction.CompactionStats``), keyed by
project slug; :func:`build_section` turns the finished accumulator (plus
the corpus's config snapshots and, optionally, statusline usage-log
rows) into the report ``Section``. Snapshot and usage-log-row lookups are
deferred to :func:`build_section` rather than threaded through
``add_session`` -- a project's own snapshot doesn't vary per session, and
this keeps the call site in ``report.py`` to exactly one line per
session (see that module's own wiring for this section).

Privacy: nothing here retains message/attachment text or a full path --
only counts, byte/char lengths already present on ``Turn``/``Event``,
and the small numeric/flag fields a schema-2 snapshot already exposes
(``content_layers``, ``mcp_servers.names`` length only, never the names
themselves).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import fmean, median
from typing import Sequence

from . import compaction
from . import snapshots as snapshots_mod
from .model import Column, Event, EventKind, Section, Table, TranscriptResult, Turn
from .snapshots import Snapshot
from .tools import log_usage

#: No tokenizer is run over transcript content (privacy rule) -- see the
#: module docstring. Duplicated from ``topology._CHARS_PER_TOKEN_APPROX``
#: per this project's small-constant-duplication convention.
_CHARS_PER_TOKEN_APPROX = 4

#: Per-agent-listing token constant for the ``custom_agents_est`` bucket
#: (a snapshot's ``content_layers.agents_summary.count`` names how many
#: agent frontmatter files load, not their rendered size) -- a rough,
#: labelled-as-such stand-in for "one short frontmatter+description
#: listing costs about this many tokens".
_AGENT_LISTING_TOKENS_PER_AGENT = 60

#: Same literal ``pricing._CONTEXT_WINDOW_SUFFIX`` value, duplicated
#: rather than imported (that name is a private module constant of
#: ``pricing.py``, and this project's convention is to duplicate a small
#: constant like this rather than reach across a module's underscore
#: boundary -- see e.g. ``compaction.py``'s own ``RecacheThresholds``
#: import instead of copying ctx_floor/cr_ratio, which is the opposite
#: choice made for a *shared, evolving* threshold pair; this one is a
#: single frozen string literal).
_CONTEXT_WINDOW_SUFFIX = "[1m]"

#: A snapshot's ``autoCompactWindow``/observed-threshold pair counts as
#: "drifted" once they disagree by more than this fraction.
_DRIFT_RATIO = 0.10

#: Assumed model context window sizes when no statusline ground truth is
#: available for a project -- see the module docstring.
_ASSUMED_CONTEXT_WINDOW_1M = 1_000_000
_ASSUMED_CONTEXT_WINDOW_DEFAULT = 200_000


# -- small shared helpers (duplicated per this project's convention -----
# see e.g. report.py's/topology.py's own module docstrings) -------------


def _priced_turns(result: TranscriptResult) -> list[Turn]:
    return [t for t in result.turns if t.turn_index > 0]


def _first_priced_turn(result: TranscriptResult) -> Turn | None:
    for turn in result.turns:
        if turn.turn_index == 1:
            return turn
    return None


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _mean(values: Sequence[float]) -> float | None:
    return fmean(values) if values else None


def _median(values: Sequence[float]) -> float | None:
    return median(values) if values else None


def _dominant_model(top: TranscriptResult) -> str | None:
    counts: dict[str, int] = {}
    for turn in _priced_turns(top):
        if turn.model:
            counts[turn.model] = counts.get(turn.model, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _events_before_first_turn(top: TranscriptResult, first_turn: Turn | None) -> list[Event]:
    """Every event in ``top.events`` that precedes ``first_turn`` --
    everything the transcript carries before the first priced turn even
    starts (there is no "previous finalised turn" to bound the window
    from the other side, unlike ``Turn.preceding_event_kinds``, which
    only names *kinds*, not the events themselves or their
    ``size_chars``). Falls back to the whole event list when there is no
    first turn, or its timestamp can't be parsed -- a session-start
    estimate degrading to "count everything" rather than "count nothing"
    on a malformed timestamp.
    """
    if first_turn is None:
        return list(top.events)
    target = _parse_ts(first_turn.ts)
    if target is None:
        return list(top.events)
    result: list[Event] = []
    for event in top.events:
        event_dt = _parse_ts(event.ts)
        if event_dt is None or event_dt < target:
            result.append(event)
    return result


def _human_prompt_est_tokens(first_turn: Turn | None, events_before: list[Event]) -> float:
    """The first HUMAN_TEXT event's own ``size_chars`` found among
    ``events_before``, else ``first_turn.human_prompt_chars`` (already
    the summed length of every human-text line preceding that turn --
    see ``model.py``'s module docstring -- used as a fallback for a
    transcript shape where no matching HUMAN_TEXT event was found, e.g.
    the very first prompt arriving via a non-text content shape this
    module doesn't specifically look for)."""
    for event in events_before:
        if event.kind == EventKind.HUMAN_TEXT and event.size_chars is not None:
            return event.size_chars / _CHARS_PER_TOKEN_APPROX
    if first_turn is not None and first_turn.human_prompt_chars is not None:
        return first_turn.human_prompt_chars / _CHARS_PER_TOKEN_APPROX
    return 0.0


def _skills_listing_est_tokens(events_before: list[Event]) -> float:
    total_chars = sum(
        event.size_chars or 0
        for event in events_before
        if event.kind == EventKind.CONTEXT_INJECT and event.subkind == "skill_listing"
    )
    return total_chars / _CHARS_PER_TOKEN_APPROX


def _memory_files_est_tokens(snapshot: Snapshot | None) -> float | None:
    """CLAUDE.md family bytes + rules bytes, divided by four (bytes
    treated the same as chars for this approximation -- see the module
    docstring). ``None`` when there is no snapshot, or the snapshot
    predates schema 2's ``content_layers`` field."""
    if snapshot is None:
        return None
    content = snapshot.data.get("content_layers")
    if not isinstance(content, dict):
        return None
    claude_md = content.get("claude_md") or {}
    claude_md_bytes = sum(
        value
        for value in (
            claude_md.get("user_bytes"),
            claude_md.get("project_root_bytes"),
            claude_md.get("project_local_bytes"),
            claude_md.get("nested_bytes"),
        )
        if isinstance(value, (int, float))
    )
    rules = content.get("rules") or {}
    rules_bytes = rules.get("bytes")
    total_bytes = claude_md_bytes + (rules_bytes if isinstance(rules_bytes, (int, float)) else 0)
    return total_bytes / _CHARS_PER_TOKEN_APPROX


def _custom_agents_est_tokens(snapshot: Snapshot | None) -> float | None:
    """``content_layers.agents_summary.count`` custom agents, each priced
    at :data:`_AGENT_LISTING_TOKENS_PER_AGENT` tokens (a labelled
    constant, not a measurement -- see the module docstring). ``None``
    without a snapshot (or a schema-1 one, which predates
    ``content_layers``)."""
    if snapshot is None:
        return None
    content = snapshot.data.get("content_layers")
    if not isinstance(content, dict):
        return None
    agents_summary = content.get("agents_summary") or {}
    count = agents_summary.get("count", 0)
    if not isinstance(count, (int, float)):
        count = 0
    return count * _AGENT_LISTING_TOKENS_PER_AGENT


def _mcp_tools_flag(snapshot: Snapshot | None) -> str | None:
    """``"present, size unknown"`` when the snapshot names at least one
    MCP server, else ``None`` -- this module has no way to measure an MCP
    server's own tool-schema size, only whether one is configured at
    all (mirrors ``snapshots.build_config_layers_table``'s own
    ``mcp_servers.names`` reading)."""
    if snapshot is None:
        return None
    mcp_servers = snapshot.data.get("mcp_servers")
    names = (mcp_servers or {}).get("names") if isinstance(mcp_servers, dict) else None
    return "present, size unknown" if names else None


# -- accumulator -----------------------------------------------------------


@dataclass(slots=True)
class _ProjectAcc:
    project: str = ""
    sessions: int = 0
    baseline_writes: list[int] = field(default_factory=list)
    human_prompt_est_tokens: list[float] = field(default_factory=list)
    skills_listing_est_tokens: list[float] = field(default_factory=list)
    compaction_records: list[compaction.CompactionRecord] = field(default_factory=list)
    saw_1m_alias: bool = False
    session_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ContextBudgetStats:
    """Corpus-wide context-budget accumulator, fed one top-level session
    at a time via :meth:`add_session`. See the module docstring."""

    projects: dict[str, _ProjectAcc] = field(default_factory=dict)

    def add_session(self, project: str, top: TranscriptResult) -> None:
        """Fold one session's top-level transcript into ``project``'s
        running totals. Only the top-level transcript is read -- the
        context-budget baseline is specifically about what a session
        pays *before any work happens*, which is a top-level-only
        question (a subagent's own baseline is already covered by
        ``topology``'s downward/spawn-write table).
        """
        acc = self.projects.setdefault(project, _ProjectAcc(project=project))
        acc.sessions += 1
        if top.meta.session_id:
            acc.session_ids.append(top.meta.session_id)

        first = _first_priced_turn(top)
        acc.baseline_writes.append(first.cache_creation_tokens if first is not None else 0)

        events_before = _events_before_first_turn(top, first)
        acc.human_prompt_est_tokens.append(_human_prompt_est_tokens(first, events_before))
        acc.skills_listing_est_tokens.append(_skills_listing_est_tokens(events_before))

        model = _dominant_model(top)
        if model and model.endswith(_CONTEXT_WINDOW_SUFFIX):
            acc.saw_1m_alias = True

        # rates=None: only trigger/pre_tokens/dropped_tokens are read by
        # this module (all come straight from the COMPACT_BOUNDARY event,
        # never from pricing) -- next_turn_write_cost, the only field an
        # unresolved rate affects, is never read here. See
        # compaction_records_for_transcript's own docstring: an
        # unresolved rate simply leaves that field at 0.0.
        acc.compaction_records.extend(compaction.compaction_records_for_transcript(top, None))


# -- usage-log (statusline) row helpers -------------------------------------

#: Trailing CSV columns ``statusline.py`` appends after
#: ``log_usage.CSV_FIELDS``'s existing six -- see that module's own
#: docstring for the write-side contract this mirrors. Duplicated here
#: (not imported) since ``log_usage.CSV_FIELDS`` intentionally stays a
#: fixed six-tuple (this project's convention -- see ``tools/log_usage.py``'s
#: module docstring) and these three extra columns are this work
#: package's own addition, read positionally rather than by name.
_STATUSLINE_TRAILING_FIELDS = (
    "context_window_used_tokens",
    "context_window_size",
    "context_window_autocompact_threshold",
)


def _parse_number(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def load_context_window_rows(csv_path: str | Path) -> list[dict]:
    """Every usage-log CSV row carrying a numeric
    ``context_window_used_tokens`` trailing value (written by
    ``statusline.main``'s context-window append -- see that module's
    docstring), as ``{"session_id", "context_window_used_tokens",
    "context_window_size", "context_window_used_percentage",
    "context_window_autocompact_threshold"}`` dicts, in file order.

    Reads the file positionally via ``csv.reader`` rather than
    ``log_usage.load_usage_log``'s ``csv.DictReader`` (a fixed six-column
    ``log_usage.CSV_FIELDS``): an old-format row (six columns, written
    before this work package existed) simply has nothing at the extra
    positions and is skipped -- there is no context_window data to
    report for it -- rather than raising or misreading a later column as
    an earlier one. Returns ``[]`` when the file doesn't exist, matching
    ``log_usage.load_usage_log``'s own "the log is optional" contract.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        return []
    rows: list[dict] = []
    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            return []
        for raw_row in reader:
            if len(raw_row) <= len(log_usage.CSV_FIELDS):
                continue
            used_tokens = _parse_number(raw_row[6]) if len(raw_row) > 6 else None
            if used_tokens is None:
                continue
            rows.append(
                {
                    "session_id": raw_row[1] if len(raw_row) > 1 else "",
                    "context_window_used_tokens": used_tokens,
                    "context_window_size": _parse_number(raw_row[7]) if len(raw_row) > 7 else None,
                    "context_window_used_percentage": _parse_number(raw_row[3]) if len(raw_row) > 3 else None,
                    "context_window_autocompact_threshold": (
                        _parse_number(raw_row[8]) if len(raw_row) > 8 else None
                    ),
                }
            )
    return rows


def _latest_statusline_window_by_project(
    usage_log_rows: list[dict] | None, session_to_project: dict[str, str]
) -> dict[str, float]:
    result: dict[str, float] = {}
    if not usage_log_rows:
        return result
    for row in usage_log_rows:
        project = session_to_project.get(row.get("session_id"))
        if project is None:
            continue
        size = row.get("context_window_size")
        if isinstance(size, (int, float)) and not isinstance(size, bool):
            result[project] = size  # last one wins -- rows are in file order
    return result


# -- Section/Table assembly --------------------------------------------------


def _baseline_row(
    project: str,
    sessions: int,
    baseline_writes: list[int],
    human_list: list[float],
    skills_list: list[float],
    snapshot: Snapshot | None,
) -> list:
    mean_baseline = _mean(baseline_writes)
    median_baseline = _median(baseline_writes)
    human_est = _mean(human_list) or 0.0
    skills_est = _mean(skills_list) or 0.0
    memory_est = _memory_files_est_tokens(snapshot)
    agents_est = _custom_agents_est_tokens(snapshot)
    mcp_flag = _mcp_tools_flag(snapshot)

    known_total = human_est + skills_est
    if isinstance(memory_est, (int, float)):
        known_total += memory_est
    if isinstance(agents_est, (int, float)):
        known_total += agents_est

    residual = max(0.0, mean_baseline - known_total) if isinstance(mean_baseline, (int, float)) else None

    return [
        project,
        sessions,
        mean_baseline,
        median_baseline,
        human_est,
        skills_est,
        memory_est,
        agents_est,
        mcp_flag,
        residual,
    ]


def _build_baseline_table(stats: ContextBudgetStats, latest_snapshots: dict[str, Snapshot]) -> Table:
    columns = [
        Column(key="project", label="Project", kind="str"),
        Column(key="sessions", label="Sessions", kind="int"),
        Column(key="mean_baseline", label="Mean baseline (measured)", kind="tokens"),
        Column(key="median_baseline", label="Median baseline (measured)", kind="tokens"),
        Column(key="human_prompt_est", label="Human prompt (est)", kind="tokens"),
        Column(key="skills_listing_est", label="Skills listing (est)", kind="tokens"),
        Column(key="memory_files_est", label="Memory files (est)", kind="tokens"),
        Column(key="custom_agents_est", label="Custom agents (est)", kind="tokens"),
        Column(key="mcp_tools_est", label="MCP tools (est)", kind="str"),
        Column(key="system_prompt_and_tools_est", label="System prompt and tools (est)", kind="tokens"),
    ]

    all_baseline: list[int] = []
    all_human: list[float] = []
    all_skills: list[float] = []
    all_sessions = 0

    rows: list[list] = []
    for project in sorted(stats.projects):
        acc = stats.projects[project]
        all_baseline.extend(acc.baseline_writes)
        all_human.extend(acc.human_prompt_est_tokens)
        all_skills.extend(acc.skills_listing_est_tokens)
        all_sessions += acc.sessions

        snapshot = latest_snapshots.get(project)
        rows.append(
            _baseline_row(project, acc.sessions, acc.baseline_writes, acc.human_prompt_est_tokens,
                          acc.skills_listing_est_tokens, snapshot)
        )

    rows.insert(0, _baseline_row("all", all_sessions, all_baseline, all_human, all_skills, None))

    return Table(
        name="context_budget_baseline",
        title="Context budget: baseline",
        columns=columns,
        rows=rows,
        notes=[
            "Every column ending \"(est)\" approximates tokens as "
            f"characters/bytes divided by {_CHARS_PER_TOKEN_APPROX} -- no "
            "tokenizer runs over transcript content. Claude Code's own "
            "/context view is the authoritative breakdown of the context "
            "window; treat every (est) figure here as a rough proxy, never "
            "as ground truth.",
            "The \"all\" row sums every project's own sessions into one "
            "mean/median baseline; its memory files, custom agents and MCP "
            "tools buckets are null because those figures come from each "
            "project's own config snapshot, which cannot be meaningfully "
            "combined across different projects.",
            "\"System prompt and tools (est)\" is the residual: mean "
            "baseline minus every other known (est) bucket, floored at 0 -- "
            "it also silently absorbs any bucket that could not be "
            "estimated at all (e.g. no config snapshot for that project), "
            "so a large residual does not necessarily mean a large system "
            "prompt.",
        ],
    )


def _build_autocompact_table(
    stats: ContextBudgetStats,
    latest_snapshots: dict[str, Snapshot],
    usage_log_rows: list[dict] | None,
) -> Table:
    columns = [
        Column(key="project", label="Project", kind="str"),
        Column(key="configured_window", label="Configured autoCompactWindow", kind="tokens"),
        Column(key="context_window_size", label="Model context window", kind="tokens"),
        Column(key="context_window_source", label="Context window source", kind="str"),
        Column(key="observed_threshold", label="Observed effective threshold", kind="tokens"),
        Column(key="implied_buffer", label="Implied buffer", kind="tokens"),
        Column(key="auto_compactions", label="Auto compactions", kind="int"),
        Column(key="drift", label="Drift (>10%)", kind="str"),
    ]

    session_to_project = {
        session_id: project for project, acc in stats.projects.items() for session_id in acc.session_ids
    }
    statusline_window_by_project = _latest_statusline_window_by_project(usage_log_rows, session_to_project)

    rows: list[list] = []
    for project in sorted(stats.projects):
        acc = stats.projects[project]
        snapshot = latest_snapshots.get(project)

        configured_window = None
        if snapshot is not None:
            value = snapshots_mod.effective_config(snapshot).get("autoCompactWindow")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                configured_window = value

        window_size = statusline_window_by_project.get(project)
        source = "statusline"
        if window_size is None:
            window_size = _ASSUMED_CONTEXT_WINDOW_1M if acc.saw_1m_alias else _ASSUMED_CONTEXT_WINDOW_DEFAULT
            source = "assumed"

        observed_threshold = compaction.effective_autocompact_threshold(acc.compaction_records)
        implied_buffer = window_size - observed_threshold if observed_threshold is not None else None
        auto_compactions = sum(1 for record in acc.compaction_records if record.trigger == "auto")

        drift = None
        if (
            isinstance(configured_window, (int, float))
            and configured_window
            and observed_threshold is not None
        ):
            drift = abs(observed_threshold - configured_window) / configured_window > _DRIFT_RATIO

        rows.append(
            [
                project,
                configured_window,
                window_size,
                source,
                observed_threshold,
                implied_buffer,
                auto_compactions,
                drift,
            ]
        )

    return Table(
        name="context_budget_autocompact",
        title="Context budget: autocompact",
        columns=columns,
        rows=rows,
        notes=[
            "Observed effective threshold is the median "
            "compactMetadata.preTokens over this project's own compactions "
            "whose trigger is \"auto\" (compaction.effective_autocompact_threshold); "
            "null when no auto-triggered compaction was observed.",
            "Model context window is read from a statusline usage-log row's "
            "own context_window fields when one is available for a session "
            "in this project (context_window_source = \"statusline\"); "
            "otherwise it is assumed as 1,000,000 for a \"[1m]\" model alias "
            "or 200,000 otherwise (context_window_source = \"assumed\").",
            "Drift is true when the observed effective threshold differs "
            "from the configured autoCompactWindow by more than 10%; null "
            "when either figure is unavailable.",
        ],
    )


def _build_statusline_table(usage_log_rows: list[dict] | None) -> Table:
    columns = [
        Column(key="session", label="Session", kind="str"),
        Column(key="used_tokens", label="Last used tokens", kind="tokens"),
        Column(key="context_window_size", label="Window size", kind="tokens"),
        Column(key="used_percentage", label="Used %", kind="pct"),
    ]

    by_session: dict[str, dict] = {}
    for row in usage_log_rows or []:
        used_tokens = row.get("context_window_used_tokens")
        if not isinstance(used_tokens, (int, float)) or isinstance(used_tokens, bool):
            continue
        session_id = row.get("session_id") or "(unknown)"
        by_session[session_id] = row  # last one wins -- rows are in file order

    rows = [
        [
            session_id,
            row.get("context_window_used_tokens"),
            row.get("context_window_size")
            if isinstance(row.get("context_window_size"), (int, float))
            else None,
            row.get("context_window_used_percentage")
            if isinstance(row.get("context_window_used_percentage"), (int, float))
            else None,
        ]
        for session_id, row in sorted(by_session.items())
    ]

    notes: list[str] = []
    if not rows:
        notes.append(
            "No usage-log row carries context_window fields yet -- install "
            "the statusline logger (claude-token-lens statusline "
            "--print-install-fragment) to populate this table with ground "
            "truth from Claude Code's own payload."
        )
    return Table(
        name="context_budget_statusline",
        title="Context budget: statusline ground truth",
        columns=columns,
        rows=rows,
        notes=notes,
    )


def build_section(
    stats: ContextBudgetStats,
    *,
    snapshots: list[Snapshot] | None = None,
    usage_log_rows: list[dict] | None = None,
) -> Section:
    """Build the "Context budget" report section (key
    ``"context_budget"``). See the module docstring for the three tables.

    ``snapshots`` is the same corpus-wide snapshot list every other
    section that reads config takes (``report.py``'s own ``snapshots``
    parameter); this function joins each project to its own *latest*
    snapshot via :func:`snapshots.latest_snapshot_per_project` rather
    than requiring a caller to have done that join already.

    ``usage_log_rows`` is whatever :func:`load_context_window_rows`
    returns (or an equivalent hand-built list of the same dict shape, as
    every test in this work package uses) -- ``None``/empty is tolerated
    throughout; the autocompact table then falls back to an assumed
    context-window size and the statusline table renders empty with an
    explanatory note.

    Skips cleanly (no tables, one note) when ``stats`` has never seen a
    top-level transcript at all -- the same "still return a Section,
    never omit it" convention every other section in this codebase
    follows for its own precondition-not-met case.
    """
    if not stats.projects:
        return Section(
            key="context_budget",
            title="Context budget",
            tables=[],
            notes=[
                "No top-level transcripts in this corpus; a context budget "
                "cannot be estimated."
            ],
        )

    latest_snapshots = snapshots_mod.latest_snapshot_per_project(snapshots) if snapshots else {}

    tables = [
        _build_baseline_table(stats, latest_snapshots),
        _build_autocompact_table(stats, latest_snapshots, usage_log_rows),
        _build_statusline_table(usage_log_rows),
    ]

    return Section(key="context_budget", title="Context budget", tables=tables, notes=[])


__all__ = [
    "ContextBudgetStats",
    "build_section",
    "load_context_window_rows",
]
