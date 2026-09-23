"""Quality signals: did the work go well, not just what it cost.

A cheaper model or a lower effort only saves money if the work still gets
done. This module measures, from the transcripts alone, the signs that it
didn't: agent runs that failed, were stopped or were cut off before they
answered; tool calls and shell commands that failed; tool calls you
denied; replies you stopped; your messages that corrected Claude; files
edited again after you had already replied; and replies cut off at the
output limit. Alongside them, neutral measures of how much work a run
took: replies, tool calls, output tokens, time and cost per run.

One *run* is one transcript: a main session or one subagent run. Every
signal is a ratio of two counts summed over runs (tool errors over tool
calls, say), so a table can show both counts and a comparison can test
the difference properly.

Comparing two sets of runs (before and after a change, or one agent's
setups against each other) uses a two-sided z-test on the ratio with a
variance estimated from the runs themselves (the delta method for a
ratio of sums), so fifty tool errors in one bad run count as one bad run,
not fifty independent failures. With many signals tested at once some
will differ by chance, so the p-values within one comparison are
Holm-corrected: a difference is "worse" or "better" only when it holds
after the correction, "possibly worse"/"possibly better" when it only
holds on its own, and "no clear change" otherwise. With fewer than
:data:`MIN_RUNS` runs (or :data:`MIN_DENOMINATOR` of what a rate counts)
on either side, it is "too little data". A share that moved by less than
:data:`MIN_SHARE_CHANGE` is "no clear change" however many runs back
it: with thousands of tool calls, 0.04% against none is real but not
worth acting on.

What is kept: counts and flags only. Whether a message of yours looks
like a correction is a yes/no from a fixed phrase list (see
``events._CORRECTION_RE``); the text is never stored. Files are known
only by a salted hash.

Where agent outcomes come from: a background agent's task notification
carries its task id and status (``completed``, ``failed``, ``stopped``);
the agent's own transcript is ``agent-<task id>.jsonl``, so the two join
on the id. (A notification that arrives while Claude is mid-reply is
queued first, so queued lines are read too; the last status wins.) A
synchronous agent's tool result carries its agent id and
status. A workflow agent gets no notification; its outcome is its own
state in the finished run file (done, error, or still in progress when
the workflow ended, which counts as stopped). A run with none of these
has no recorded outcome and is left out of the outcome rates (but not
of the others).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

from .fixes import _model_family
from .model import Column, EventKind, Section, Table, TranscriptResult
from .pricing import Pricing, price_turn

#: The group name of main-session runs; subagent runs are grouped by
#: agent type.
MAIN = "(main session)"
#: The pooled row of every subagent run.
ALL_AGENTS = "(all subagents)"

#: Runs needed on each side of a comparison.
MIN_RUNS = 5
#: Of what a rate counts (tool calls, messages...), needed on each side.
MIN_DENOMINATOR = 10
#: Two-sided significance level, before the Holm correction.
ALPHA = 0.05
#: The smallest move in a share (half a percentage point) worth a label.
MIN_SHARE_CHANGE = 0.005

_SHELL_TOOLS = ("Bash", "PowerShell")
#: Tools whose call is the agent's answer: a run that ends on one finished.
_ANSWER_TOOLS = frozenset({"StructuredOutput"})

#: Outcome statuses, normalised.
_OUTCOME = {
    "completed": "completed",
    "success": "completed",
    "failed": "failed",
    "error": "failed",
    "stopped": "stopped",
    "killed": "stopped",
    "cancelled": "stopped",
    "canceled": "stopped",
}


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


#: A workflow agent's end state in its finished run file, as an outcome.
#: "progress" in a finished run means the workflow ended while it ran.
_WORKFLOW_STATE = {"done": "completed", "error": "failed", "progress": "stopped"}


def _bare_id(agent_id: str | None) -> str:
    """``agent-abc`` and ``abc`` name the same agent."""
    return (agent_id or "").removeprefix("agent-")


# -- one run -----------------------------------------------------------------


@dataclass(slots=True)
class Run:
    """The counts for one transcript."""

    group: str = MAIN
    kind: str = "top-level"
    session_id: str = ""
    agent_id: str = ""
    start: datetime | None = None
    #: The model most of its replies used, and the effort most were sent at.
    model: str = ""
    effort: str = ""
    replies: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    shell_calls: int = 0
    shell_errors: int = 0
    denials: int = 0
    interrupts: int = 0
    api_errors: int = 0
    fallbacks: int = 0
    compactions: int = 0
    max_tokens: int = 0
    human_messages: int = 0
    corrections: int = 0
    edits: int = 0
    files_edited: int = 0
    #: Edits to a file already edited before your latest message.
    rework_edits: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    duration_s: float = 0.0
    cost: float = 0.0
    #: Subagents only: the last reply asked for a tool and never got to
    #: answer, or the run was stopped. None when that isn't known.
    cut_off: bool | None = None
    #: Subagents only: cut off after its last tool call's result came back,
    #: without being stopped. Nothing records why, but that is how a run
    #: ends when its ``maxTurns`` runs out (such runs cluster at round
    #: reply counts like 30, 60 and 200), so this is "likely ran out of turns".
    turn_limit: bool = False
    #: Subagents only: "completed", "failed", "stopped", "other", or None
    #: when no outcome was recorded.
    outcome: str | None = None
    #: The agent was ended early by Claude Code (a rate limit, say).
    terminated_early: bool = False
    tool_errors_by_tool: dict = field(default_factory=dict)

    @property
    def is_agent(self) -> bool:
        return self.kind != "top-level"


def _dominant(values: Iterable[str]) -> str:
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else ""


def _outcomes(transcripts: Iterable[TranscriptResult]) -> tuple[dict[str, str], set[str]]:
    """Agent id -> last reported status, and the ids ended early, from
    every task notification and agent result in a session."""
    status: dict[str, str] = {}
    terminated: set[str] = set()
    for result in transcripts:
        for event in result.events:
            detail = event.detail or {}
            if event.kind in (EventKind.TASK_NOTIFICATION, EventKind.AGENT_TERMINATED, EventKind.QUEUE_OPERATION):
                task_id = detail.get("task_id")
                if not task_id:
                    continue
                if event.kind == EventKind.AGENT_TERMINATED:
                    terminated.add(task_id)
                if detail.get("status"):
                    status[task_id] = str(detail["status"])
            elif event.kind == EventKind.TOOL_RESULT:
                for pair in detail.get("agents") or ():
                    if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] and pair[1]:
                        status[_bare_id(str(pair[0]))] = str(pair[1])
    return status, terminated


def run_facts(
    result: TranscriptResult,
    pricing: Pricing | None,
    *,
    outcomes: dict[str, str] | None = None,
    terminated: set[str] | None = None,
    session_id: str = "",
) -> Run:
    """The :class:`Run` for one transcript. ``outcomes``/``terminated``
    come from :func:`_outcomes` over the whole session."""
    meta = result.meta
    kind = meta.kind or "top-level"
    run = Run(
        group=MAIN if kind == "top-level" else (meta.agent_type or kind),
        kind=kind,
        session_id=session_id or meta.session_id,
        agent_id=_bare_id(meta.agent_id),
    )
    priced = [turn for turn in result.turns if turn.turn_index > 0]
    run.model = _dominant(turn.model for turn in priced)
    run.effort = _dominant(turn.effort or "" for turn in priced) or "default"
    times = [t for t in (_ts(turn.ts) for turn in priced) if t is not None]
    if times:
        run.start = min(times)
        run.duration_s = (max(times) - min(times)).total_seconds()
    edited_before: set[str] = set()
    edited_this_round: set[str] = set()
    for turn in result.turns:
        if turn.human_prompt_chars is not None and not run.is_agent:
            run.human_messages += 1
            run.corrections += int(turn.human_correction)
            edited_before |= edited_this_round
            edited_this_round = set()
        if turn.stop_reason == "max_tokens":
            run.max_tokens += 1
        calls = turn.tool_calls_by_tool or {}
        run.tool_calls += sum(calls.values()) if calls else len(turn.tool_use_ids)
        run.shell_calls += sum(calls.get(name, 0) for name in _SHELL_TOOLS)
        run.tool_errors += turn.tool_error_count
        run.shell_errors += sum((turn.tool_errors_by_tool or {}).get(name, 0) for name in _SHELL_TOOLS)
        for name, count in (turn.tool_errors_by_tool or {}).items():
            run.tool_errors_by_tool[name] = run.tool_errors_by_tool.get(name, 0) + count
        for target in turn.edit_target_hashes:
            run.edits += 1
            if target in edited_before:
                run.rework_edits += 1
            edited_this_round.add(target)
        if turn.turn_index > 0:
            run.replies += 1
            run.output_tokens += turn.output_tokens
            run.thinking_tokens += turn.thinking_tokens or 0
            if pricing is not None:
                run.cost += price_turn(turn, pricing.resolve_model(turn.model)).total
    run.files_edited = len(edited_before | edited_this_round)
    kinds = Counter(event.kind for event in result.events)
    run.denials = kinds[EventKind.TOOL_DENIAL]
    run.interrupts = kinds[EventKind.INTERRUPT]
    run.api_errors = kinds[EventKind.API_ERROR]
    run.fallbacks = kinds[EventKind.MODEL_FALLBACK]
    run.compactions = kinds[EventKind.COMPACT_BOUNDARY]
    if run.is_agent:
        # Cut off: stopped, never replied, or the last reply asked for a
        # tool and nothing came after it. A final StructuredOutput call is
        # a workflow agent's answer, not a cut-off. The last reply's own
        # stop_reason is often not recorded, so the tool calls decide.
        last = next((turn for turn in reversed(result.turns) if not turn.is_synthetic), None)
        if meta.stopped_by_user or last is None:
            run.cut_off = True
        else:
            run.cut_off = bool(last.tool_use_ids) and not set(last.tool_names) <= _ANSWER_TOOLS
            last_at = _ts(last.ts)
            run.turn_limit = run.cut_off and last_at is not None and any(
                event.kind == EventKind.TOOL_RESULT and (_ts(event.ts) or last_at) > last_at for event in result.events
            )
        status = (outcomes or {}).get(run.agent_id) or _WORKFLOW_STATE.get(meta.workflow_agent_state or "")
        if status is not None:
            run.outcome = _OUTCOME.get(status.lower(), "other")
        run.terminated_early = run.agent_id in (terminated or set())
    return run


def session_runs(bundle, pricing: Pricing | None) -> list[Run]:
    """Every run in one session: the main session first, then its
    subagents, each with its outcome joined in."""
    transcripts = ([bundle.top] if bundle.top is not None else []) + list(bundle.subs)
    outcomes, terminated = _outcomes(transcripts)
    return [
        run_facts(result, pricing, outcomes=outcomes, terminated=terminated, session_id=bundle.session_id)
        for result in transcripts
    ]


def corpus_runs(corpus, pricing: Pricing | None) -> list[Run]:
    return [run for bundle in corpus.sessions for run in session_runs(bundle, pricing)]


# -- signals ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Signal:
    key: str
    label: str
    #: The per-run numerator and denominator; the signal is their sums' ratio.
    num: Callable[[Run], float]
    den: Callable[[Run], float]
    #: "pct" (a share, shown as a percentage) or "per_run" (a mean per run).
    kind: str
    #: "higher" when a rise is worse; None for a neutral measure of work.
    worse: str | None
    #: Which runs it applies to: "all", "main" or "agents".
    scope: str
    #: What the denominator counts, for "3 of 40 tool calls".
    of: str
    #: How a per-run value is formatted: "count", "tokens", "money", "minutes".
    unit: str = "count"


def _unfinished(run: Run) -> float:
    return float(run.outcome in ("failed", "stopped") or bool(run.cut_off) or run.terminated_early)


def _finish_known(run: Run) -> float:
    return float(run.outcome is not None or run.cut_off is not None or run.terminated_early)


SIGNALS: tuple[Signal, ...] = (
    Signal("unfinished", "Agent runs that didn't finish", _unfinished, _finish_known, "pct", "higher", "agents",
           "agent runs"),
    Signal("failed", "Agent runs that reported failure", lambda r: float(r.outcome == "failed"),
           lambda r: float(r.outcome is not None), "pct", "higher", "agents", "agent runs with an outcome"),
    Signal("stopped", "Agent runs stopped", lambda r: float(r.outcome == "stopped"),
           lambda r: float(r.outcome is not None), "pct", "higher", "agents", "agent runs with an outcome"),
    Signal("turn_limit", "Agent runs that likely ran out of turns", lambda r: float(r.turn_limit),
           lambda r: float(r.cut_off is not None), "pct", "higher", "agents", "agent runs"),
    Signal("cut_off", "Agent runs cut off before answering", lambda r: float(bool(r.cut_off)),
           lambda r: float(r.cut_off is not None), "pct", "higher", "agents", "agent runs"),
    Signal("tool_errors", "Tool calls that failed", lambda r: r.tool_errors, lambda r: r.tool_calls, "pct",
           "higher", "all", "tool calls"),
    Signal("shell_errors", "Shell commands that failed", lambda r: r.shell_errors, lambda r: r.shell_calls, "pct",
           "higher", "all", "shell commands"),
    Signal("denials", "Tool calls you denied", lambda r: r.denials, lambda r: r.tool_calls, "pct", "higher", "all",
           "tool calls"),
    Signal("interrupts", "Replies you stopped", lambda r: r.interrupts, lambda r: r.replies, "pct", "higher", "main",
           "replies"),
    Signal("corrections", "Your messages that corrected Claude", lambda r: r.corrections, lambda r: r.human_messages,
           "pct", "higher", "main", "messages"),
    Signal("rework", "Edits to files already changed before your last message", lambda r: r.rework_edits,
           lambda r: r.edits, "pct", "higher", "main", "edits"),
    Signal("max_tokens", "Replies cut off at the output limit", lambda r: r.max_tokens, lambda r: r.replies, "pct",
           "higher", "all", "replies"),
    Signal("api_errors", "API errors per reply", lambda r: r.api_errors, lambda r: r.replies, "pct", "higher", "all",
           "replies"),
    Signal("fallbacks", "Replies after a fallback to another model", lambda r: r.fallbacks, lambda r: r.replies, "pct",
           "higher", "all", "replies"),
    Signal("replies", "Replies per run", lambda r: r.replies, lambda r: 1.0, "per_run", None, "all", "runs"),
    Signal("tool_calls", "Tool calls per run", lambda r: r.tool_calls, lambda r: 1.0, "per_run", None, "all", "runs"),
    Signal("edits_per_file", "Edits per file edited", lambda r: r.edits, lambda r: r.files_edited, "per_run", None,
           "all", "files edited"),
    Signal("output", "Output tokens per run", lambda r: r.output_tokens, lambda r: 1.0, "per_run", None, "all", "runs",
           "tokens"),
    Signal("thinking_share", "Share of output spent thinking", lambda r: r.thinking_tokens, lambda r: r.output_tokens,
           "pct", None, "all", "output tokens"),
    Signal("minutes", "Minutes per run", lambda r: r.duration_s / 60.0, lambda r: 1.0, "per_run", None, "all", "runs",
           "minutes"),
    Signal("cost", "Cost per run", lambda r: r.cost, lambda r: 1.0, "per_run", None, "all", "runs", "money"),
)
SIGNAL_BY_KEY = {signal.key: signal for signal in SIGNALS}


def signals_for(group: str) -> list[Signal]:
    agents = group != MAIN
    return [s for s in SIGNALS if s.scope == "all" or (s.scope == "agents") == agents]


@dataclass(frozen=True, slots=True)
class Estimate:
    """A signal over a set of runs."""

    value: float | None
    #: Summed numerator and denominator.
    num: float
    den: float
    #: Runs that contributed to the denominator.
    runs: int
    #: Variance of ``value`` (None with fewer than two runs).
    variance: float | None


def estimate(signal: Signal, runs: list[Run]) -> Estimate:
    pairs = [(float(signal.num(run)), float(signal.den(run))) for run in runs]
    pairs = [(y, x) for y, x in pairs if x > 0]
    total_x = sum(x for _y, x in pairs)
    total_y = sum(y for y, _x in pairs)
    if total_x <= 0:
        return Estimate(None, total_y, total_x, len(pairs), None)
    ratio = total_y / total_x
    n = len(pairs)
    variance = None
    if n >= 2:
        variance = n / (n - 1) * sum((y - ratio * x) ** 2 for y, x in pairs) / total_x**2
    return Estimate(ratio, total_y, total_x, n, variance)


def _p_value(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2.0))


def _enough(signal: Signal, est: Estimate) -> bool:
    if est.value is None or est.runs < MIN_RUNS:
        return False
    return signal.kind == "per_run" or est.den >= MIN_DENOMINATOR


def compare_runs(
    before: list[Run],
    after: list[Run],
    signals: Iterable[Signal],
    money: Callable[[float], str] | None = None,
) -> list[dict]:
    """One row per signal: the value on each side, the counts behind it,
    and a label (see the module docstring). Holm-corrected across the
    signals compared. ``money`` formats a cost (billing-mode units)."""
    rows = []
    for signal in signals:
        old, new = estimate(signal, before), estimate(signal, after)
        row = {
            "key": signal.key,
            "label": signal.label,
            "kind": signal.kind,
            "worse_when": signal.worse,
            "before": old.value,
            "after": new.value,
            "unit": signal.unit,
            "before_text": value_text(signal, old.value, money),
            "after_text": value_text(signal, new.value, money),
            "before_counts": counts_text(signal, old),
            "after_counts": counts_text(signal, new),
            "before_runs": old.runs,
            "after_runs": new.runs,
            "p": None,
            "label_key": "too_little_data",
        }
        if _enough(signal, old) and _enough(signal, new):
            variance = (old.variance or 0.0) + (new.variance or 0.0)
            diff = new.value - old.value
            if variance > 0:
                row["p"] = _p_value(diff / math.sqrt(variance))
            else:
                row["p"] = 1.0 if diff == 0 else 0.0
            row["label_key"] = "no_clear_change"
        rows.append(row)
    tested = sorted((r for r in rows if r["p"] is not None), key=lambda r: r["p"])
    m = len(tested)
    holding = True
    for rank, row in enumerate(tested):
        significant_alone = row["p"] < ALPHA
        holding = holding and row["p"] < ALPHA / (m - rank)
        if not significant_alone or (row["kind"] == "pct" and abs(row["after"] - row["before"]) < MIN_SHARE_CHANGE):
            continue
        rising = row["after"] > row["before"]
        if row["worse_when"] is None:
            direction = "higher" if rising else "lower"
            row["label_key"] = direction if holding else f"possibly_{direction}"
        else:
            direction = "worse" if rising else "better"
            row["label_key"] = direction if holding else f"possibly_{direction}"
    for row in rows:
        row["verdict"] = LABELS[row["label_key"]]
        if row["p"] is not None:
            row["p"] = round(row["p"], 4)
    return rows


LABELS = {
    "worse": "Worse",
    "better": "Better",
    "possibly_worse": "Possibly worse",
    "possibly_better": "Possibly better",
    "higher": "Higher",
    "lower": "Lower",
    "possibly_higher": "Possibly higher",
    "possibly_lower": "Possibly lower",
    "no_clear_change": "No clear change",
    "too_little_data": "Too little data",
}


def value_text(signal: Signal, value: float | None, money: Callable[[float], str] | None = None) -> str:
    if value is None:
        return "no data"
    if signal.kind == "pct":
        if value == 0:
            return "0%"
        if value < 0.001:
            return "under 0.1%"
        return f"{100.0 * value:.1f}%" if value < 0.1 else f"{100.0 * value:.0f}%"
    if signal.unit == "money":
        return money(value) if money is not None else f"${value:,.2f}"
    if signal.unit == "tokens":
        return f"{round(value):,} tokens"
    if signal.unit == "minutes":
        return f"{value:.1f} min"
    return f"{value:.1f}"


def counts_text(signal: Signal, est: Estimate) -> str:
    if signal.kind == "per_run":
        return f"{est.runs} {signal.of}" if signal.den(Run()) == 1.0 else f"{round(est.den):,} {signal.of}"
    return f"{round(est.num):,} of {round(est.den):,} {signal.of}"


def verdict(rows: list[dict]) -> str:
    """One line for a comparison's quality rows."""
    worse = [r for r in rows if r["label_key"] == "worse"]
    better = [r for r in rows if r["label_key"] == "better"]
    maybe = [r for r in rows if r["label_key"] == "possibly_worse"]
    tested = [r for r in rows if r["label_key"] != "too_little_data" and r["worse_when"]]
    if worse:
        return "Quality looks worse: " + "; ".join(
            f"{r['label'].lower()} rose from {r['before_text']} to {r['after_text']}" for r in worse
        ) + "."
    if better and not maybe:
        return "Quality looks better: " + "; ".join(
            f"{r['label'].lower()} fell from {r['before_text']} to {r['after_text']}" for r in better
        ) + "."
    if maybe:
        return "No clear sign of worse quality, but watch: " + "; ".join(
            f"{r['label'].lower()} ({r['before_text']} before, {r['after_text']} after)" for r in maybe
        ) + ". It may be chance; it firms up as more runs come in."
    if tested:
        return f"No clear change in quality on {len(tested)} signal{'s' if len(tested) != 1 else ''}."
    before = max((r["before_runs"] for r in rows), default=0)
    after = max((r["after_runs"] for r in rows), default=0)
    return (f"Too few runs to judge quality yet: {before} before and {after} after, and at least {MIN_RUNS} are "
            "needed on each side.")


# -- the report section ---------------------------------------------------------


def _groups(runs: list[Run]) -> list[tuple[str, list[Run]]]:
    by_group: dict[str, list[Run]] = {}
    for run in runs:
        by_group.setdefault(run.group, []).append(run)
    agents = sorted((g for g in by_group if g != MAIN), key=lambda g: (-len(by_group[g]), g))
    out = [(MAIN, by_group[MAIN])] if MAIN in by_group else []
    out += [(g, by_group[g]) for g in agents]
    agent_runs = [run for run in runs if run.is_agent]
    if len(agents) > 1:
        out.append((ALL_AGENTS, agent_runs))
    return out


def _value(signal: Signal, group: str, runs: list[Run]):
    applies = signal.scope == "all" or (signal.scope == "agents") == (group != MAIN)
    if not applies:
        return None
    value = estimate(signal, runs).value
    if value is None:
        return None
    return round(100.0 * value, 1) if signal.kind == "pct" else round(value, 4 if signal.unit == "money" else 1)


_BY_AGENT_SIGNALS = (
    "unfinished", "turn_limit", "tool_errors", "shell_errors", "denials", "interrupts", "corrections", "rework", "max_tokens",
    "replies", "cost",
)
_SETUP_SIGNALS = ("unfinished", "tool_errors", "shell_errors", "max_tokens", "replies", "tool_calls", "cost")


def _column(signal: Signal) -> Column:
    kind = "pct" if signal.kind == "pct" else ("money" if signal.unit == "money" else "float")
    return Column(key=f"{signal.key}_{'pct' if signal.kind == 'pct' else 'per_run'}", label=signal.label, kind=kind)


def _by_agent_table(runs: list[Run]) -> Table:
    signals = [SIGNAL_BY_KEY[key] for key in _BY_AGENT_SIGNALS]
    return Table(
        name="quality_by_agent",
        title="Quality signals by agent",
        columns=[Column(key="agent_type", label="Agent", kind="str"), Column(key="runs", label="Runs", kind="int")]
        + [_column(s) for s in signals],
        rows=[[group, len(group_runs)] + [_value(s, group, group_runs) for s in signals]
              for group, group_runs in _groups(runs)],
    )


def _counts_table(runs: list[Run]) -> Table:
    fields = (
        ("replies", "Replies"), ("tool_calls", "Tool calls"), ("tool_errors", "Failed tool calls"),
        ("shell_calls", "Shell commands"), ("shell_errors", "Failed shell commands"), ("denials", "Denied"),
        ("interrupts", "Stopped by you"), ("human_messages", "Your messages"), ("corrections", "Corrections"),
        ("edits", "Edits"), ("rework_edits", "Edits to already-changed files"),
        ("max_tokens", "Cut off at output limit"), ("api_errors", "API errors"), ("fallbacks", "Model fallbacks"),
        ("compactions", "Summaries"),
    )
    rows = []
    for group, group_runs in _groups(runs):
        outcomes = Counter(run.outcome for run in group_runs if run.is_agent)
        rows.append(
            [group, len(group_runs)]
            + [sum(getattr(run, key) for run in group_runs) for key, _label in fields]
            + [
                outcomes["completed"], outcomes["failed"], outcomes["stopped"], outcomes["other"], outcomes[None],
                sum(1 for run in group_runs if run.cut_off),
                sum(1 for run in group_runs if run.turn_limit),
                sum(1 for run in group_runs if run.terminated_early),
                sum(1 for run in group_runs if not run.replies),
            ]
        )
    return Table(
        name="quality_counts",
        title="Quality signal counts",
        columns=[Column(key="agent_type", label="Agent", kind="str"), Column(key="runs", label="Runs", kind="int")]
        + [Column(key=key, label=label, kind="int") for key, label in fields]
        + [
            Column(key="outcome_completed", label="Reported done", kind="int"),
            Column(key="outcome_failed", label="Reported failure", kind="int"),
            Column(key="outcome_stopped", label="Stopped", kind="int"),
            Column(key="outcome_other", label="Other outcome", kind="int"),
            Column(key="outcome_unknown", label="No outcome recorded", kind="int"),
            Column(key="cut_off", label="Cut off", kind="int"),
            Column(key="turn_limit", label="Likely out of turns", kind="int"),
            Column(key="terminated_early", label="Ended early", kind="int"),
            Column(key="never_replied", label="Never replied", kind="int"),
        ],
        rows=rows,
    )


def _setup(run: Run) -> tuple[str, str]:
    return run.model or "(unknown)", run.effort or "default"


def setup_rows(runs: list[Run]) -> list[dict]:
    """Per agent (and the main session), per model and effort: the setup
    signals, and how each setup compares with the agent's most-used one.
    Runs with no reply have no model, so they are left out here (the
    other tables count them)."""
    out = []
    for group, group_runs in _groups(runs):
        if group == ALL_AGENTS:
            continue
        by_setup: dict[tuple[str, str], list[Run]] = {}
        for run in group_runs:
            if run.replies:
                by_setup.setdefault(_setup(run), []).append(run)
        if not by_setup:
            continue
        ordered = sorted(by_setup, key=lambda k: (-len(by_setup[k]), k))
        base = ordered[0]
        signals = [s for s in (SIGNAL_BY_KEY[k] for k in _SETUP_SIGNALS) if s in signals_for(group)]
        for setup in ordered:
            setup_runs = by_setup[setup]
            row = {
                "agent_type": group,
                "model": setup[0],
                "effort": setup[1],
                "runs": len(setup_runs),
                "values": {s.key: _value(s, group, setup_runs) for s in signals},
                "compared_with": "",
                "compared_model": "",
                "compared_effort": "",
                "difference": "",
                "setup_verdict": "only",
                "comparison": [],
            }
            if setup != base:
                comparison = compare_runs(by_setup[base], setup_runs, signals_for(group))
                row["compared_with"] = f"{base[0]}, effort {base[1]}"
                row["compared_model"], row["compared_effort"] = base
                row["comparison"] = comparison
                row["difference"] = _difference(comparison)
                row["setup_verdict"] = setup_verdict(comparison)
            elif len(ordered) > 1:
                row["difference"] = "The most-used setup; others are compared with it."
                row["setup_verdict"] = "baseline"
            out.append(row)
    return out


#: ``setup_verdict`` values, strongest first: a setup is as bad as its
#: worst signal that has a direction, except that clearly worse on some
#: signals and clearly better on others is "mixed".
SETUP_VERDICTS = (
    "worse",
    "mixed",
    "possibly_worse",
    "better",
    "possibly_better",
    "no_clear_difference",
    "too_little_data",
)


def setup_verdict(rows: list[dict]) -> str:
    """One word for a comparison, from its signals that have a direction.
    Clearly worse on one signal and clearly better on another (more failed
    tool calls, but every run finished) is "mixed": no reason to switch
    either way."""
    keys = {r["label_key"] for r in rows if r["worse_when"]}
    if {"worse", "better"} <= keys:
        return "mixed"
    for key in ("worse", "possibly_worse", "better", "possibly_better"):
        if key in keys:
            return key
    return "no_clear_difference" if keys - {"too_little_data"} else "too_little_data"


def worse_models(setup_rows: Iterable[dict]) -> dict[tuple[str, str], dict]:
    """``{(agent, model family): row}`` for each ``quality_by_setup`` row
    whose setup did clearly worse than the agent's most-used one on a
    different model, so the models check doesn't suggest that model to
    that agent. The main session is ``"top-level"``, as in the model-swap
    table."""
    out: dict[tuple[str, str], dict] = {}
    for row in setup_rows:
        family = _model_family(str(row.get("model") or ""))
        if row.get("setup_verdict") != "worse" or family == _model_family(str(row.get("compared_model") or "")):
            continue
        agent = "top-level" if row.get("agent_type") == MAIN else row.get("agent_type")
        out.setdefault((agent, family), row)
    return out


def _difference(rows: list[dict]) -> str:
    # Cost has its own column, in the report's billing units.
    notable = [
        r for r in rows if r["label_key"] not in ("no_clear_change", "too_little_data") and r["unit"] != "money"
    ]
    if not notable:
        tested = [r for r in rows if r["label_key"] != "too_little_data"]
        return "No clear difference." if tested else "Too little data to compare."
    return "; ".join(f"{r['verdict']}: {r['label'].lower()} {r['after_text']} against {r['before_text']}"
                     for r in notable) + "."


def _by_setup_table(rows: list[dict]) -> Table:
    signals = [SIGNAL_BY_KEY[key] for key in _SETUP_SIGNALS]
    return Table(
        name="quality_by_setup",
        title="Quality by model and effort",
        columns=[
            Column(key="agent_type", label="Agent", kind="str"),
            Column(key="model", label="Model", kind="str"),
            Column(key="effort", label="Effort", kind="str"),
            Column(key="runs", label="Runs", kind="int"),
        ]
        + [_column(s) for s in signals]
        + [
            Column(key="compared_with", label="Compared with", kind="str"),
            Column(key="setup_verdict", label="Verdict", kind="str"),
            Column(key="difference", label="Difference", kind="str"),
            Column(key="compared_model", label="Compared model", kind="str"),
            Column(key="compared_effort", label="Compared effort", kind="str"),
        ],
        rows=[
            [r["agent_type"], r["model"], r["effort"], r["runs"]]
            + [r["values"].get(s.key) for s in signals]
            + [r["compared_with"], r["setup_verdict"], r["difference"], r["compared_model"], r["compared_effort"]]
            for r in rows
        ],
    )


def _failing_tools_table(runs: list[Run]) -> Table:
    errors: Counter = Counter()
    for run in runs:
        errors.update({(run.group, name): count for name, count in run.tool_errors_by_tool.items()})
    return Table(
        name="quality_failing_tools",
        title="Which tools failed",
        columns=[
            Column(key="agent_type", label="Agent", kind="str"),
            Column(key="tool", label="Tool", kind="str"),
            Column(key="errors", label="Failed calls", kind="int"),
            Column(key="runs_with_errors", label="Runs with a failure", kind="int"),
        ],
        rows=[
            [group, tool, count,
             sum(1 for run in runs if run.group == group and run.tool_errors_by_tool.get(tool))]
            for (group, tool), count in errors.most_common(25)
        ],
    )


def build_section(runs: list[Run]) -> Section:
    setups = setup_rows(runs)
    notes = [
        "An agent run counts as cut off when it was stopped, never replied, or its last reply asked for a tool and "
        "nothing came after it. Ending on a StructuredOutput call is a workflow agent's answer, so it counts as "
        "finished. One cut off after its last tool result came back, without being stopped, most likely ran out of "
        "turns (its maxTurns); Claude Code doesn't record why. The quality-by-setup table leaves out runs that never "
        "replied, since they have no model.",
        f"A difference is marked only when it holds in a z-test at {ALPHA:.0%} (two-sided) after a Holm correction "
        "for the number of signals compared; \"possibly\" when it holds on its own but not after the correction. "
        f"Fewer than {MIN_RUNS} runs (or {MIN_DENOMINATOR} of what a rate counts) on either side is too little data. "
        f"A share that moved by less than {100 * MIN_SHARE_CHANGE:.1f} percentage points is not marked.",
        "Setups are compared across the whole window, so a setup used for different kinds of work, or in a different "
        "week, can differ for that reason alone. Profiles compares before and after each change you made.",
        "Corrections are messages that start or contain a fixed phrase such as \"that's wrong\" or \"still "
        "broken\". Only the yes/no is kept, never the text.",
    ]
    return Section(
        key="quality",
        title="Quality signals",
        tables=[_by_agent_table(runs), _by_setup_table(setups), _failing_tools_table(runs), _counts_table(runs)],
        notes=notes,
    )


ASSUMPTIONS: tuple[str, ...] = (
    "Quality signals are counted from the transcripts only: a failed tool call is one whose result is marked as an "
    "error, and an agent's outcome is the status its notification or result reported.",
    "A message counts as a correction when it contains a fixed phrase such as \"that's wrong\" or \"still "
    "broken\"; plain disagreement worded differently is missed, so the rate is a floor.",
)


__all__ = [
    "ALL_AGENTS",
    "ASSUMPTIONS",
    "LABELS",
    "MAIN",
    "MIN_DENOMINATOR",
    "MIN_SHARE_CHANGE",
    "MIN_RUNS",
    "Run",
    "SIGNALS",
    "SIGNAL_BY_KEY",
    "Signal",
    "build_section",
    "compare_runs",
    "corpus_runs",
    "estimate",
    "run_facts",
    "session_runs",
    "SETUP_VERDICTS",
    "worse_models",
    "setup_rows",
    "setup_verdict",
    "signals_for",
    "verdict",
]
