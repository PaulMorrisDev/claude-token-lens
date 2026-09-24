"""What metrics capture costs.

Two views, both in list-price USD (``units.Units.money`` phrases them
for the billing mode):

- :func:`usage` measures what capture actually cost while it ran, from
  the transcripts: each capture note is a ``capture_note`` event whose
  size is exactly what the model was shown, carried from where it was
  injected until the next compaction (a cache write, then a cache read
  per turn, and a write again whenever the cache was rebuilt); each tag
  is output its writer paid for at that turn's own rate, fast mode and
  data residency included (``pricing.effective_rates``). It also says
  how often Claude tagged what it was asked to (coverage), and how much
  of each metric has been collected.
- :func:`history` replays your own recent sessions to price one
  character of note or tag in each place capture puts them, so
  :func:`estimate` can price any level or set of metrics before you turn
  it on, and :func:`metric_estimates` what each metric adds on its own.

A *prompt cycle* (:func:`prompt_cycles`) is one message of yours and
everything Claude did about it: the turn after a human message up to
the next one, with the subagents those turns started, at any depth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone

from . import capture_catalogue as catalogue
from .context_files import _Carry, _parse_ts
from .model import EventKind, TranscriptResult, Turn
from .pricing import Pricing, effective_rates, price_turn
from .topology import agent_key

CHARS_PER_TOKEN = 4

#: Days of your own sessions replayed to estimate what capture would
#: cost (``capture status``, ``init`` and the Capture tab).
HISTORY_DAYS = 14

#: What Claude Code adds around a hook note, by hook event.
_WRAP = {event: catalogue.NOTE_WRAP_CHARS + len(event) for event in ("SessionStart", "SubagentStart", "PostToolUse")}

#: Characters of "[tl: " and "]" around a reply tag, and the line break
#: before it.
_TAG_FRAME_CHARS = 7

#: ``CaptureTag`` field -> the metric it answers, in a main-session tag
#: and in a subagent's ``[result: ...]`` tag.
_MAIN_FIELDS = {
    "task": "task", "brief": "brief", "level": "level", "shift": "shift", "size": "size", "missing": "missing",
    "plan": "plan", "skill": "skill", "found": "found", "prior": "prior", "detour": "detour", "check": "check",
    "out": "big_output", "useful": "web",
}
_SUB_FIELDS = {"fit": "fit", "rules": "rules", "brief": "agent_brief", "missing": "agent_brief"}

#: How many answers a metric needs before its suggestions are firm; the
#: Capture page says when a metric has enough and could be turned off.
ENOUGH = {"main": 40, "subagent": 25, "brief": 20, "tool": 15, "signal": 20}


def _scope_of(metric_id: str) -> str:
    m = catalogue.METRICS_BY_ID[metric_id]
    if m.group == "free":
        return "signal"
    if m.tool_note:
        return "tool"
    if m.main_extra or m.sub_extra:
        return "brief"
    return "main" if m.main_line else "subagent"


def enough_target(metric_id: str) -> int:
    return ENOUGH[_scope_of(metric_id)] if metric_id in catalogue.METRICS_BY_ID else 0


# -- prompt cycles -------------------------------------------------------------


@dataclass(slots=True)
class Cycle:
    """One message of yours and the work that answered it."""

    #: Indexes into the transcript's priced turns: ``start`` is the turn
    #: after your message, ``end`` the first turn of the next cycle.
    start: int
    end: int
    turns: list[Turn] = field(default_factory=list)
    #: Subagent transcripts started in this cycle, at any depth.
    subs: list[TranscriptResult] = field(default_factory=list)

    @property
    def tag(self):
        """The last ``[tl: ...]`` tag in the cycle (the last one wins)."""
        return next((t.cap for t in reversed(self.turns) if t.cap is not None and t.cap.has_tl), None)


def _priced(result: TranscriptResult) -> list[Turn]:
    return [turn for turn in result.turns if turn.turn_index > 0]


def prompt_cycles(top: TranscriptResult, subs=()) -> list[Cycle]:
    """The prompt cycles of a main-session transcript, each with the
    subagents it started. Turns before your first message (a resumed
    session's leftovers) make no cycle."""
    turns = _priced(top)
    starts = [i for i, turn in enumerate(turns) if turn.human_prompt_chars is not None]
    cycles = [
        Cycle(start=s, end=e, turns=turns[s:e]) for s, e in zip(starts, starts[1:] + [len(turns)])
    ]
    if not cycles:
        return cycles
    cycle_of_use: dict[str, int] = {}
    for n, cycle in enumerate(cycles):
        for turn in cycle.turns:
            for tool_use_id in turn.tool_use_ids:
                cycle_of_use[tool_use_id] = n
    by_agent = {agent_key(sub.meta.agent_id): sub for sub in subs if sub.meta.agent_id}
    for sub in subs:
        n = _cycle_for(sub, cycle_of_use, by_agent)
        if n is not None:
            cycles[n].subs.append(sub)
    return cycles


def _cycle_for(sub, cycle_of_use, by_agent) -> int | None:
    """The cycle a subagent belongs to: the one whose turn started it,
    or its parent agent's, for a nested spawn."""
    seen = set()
    while sub is not None and id(sub) not in seen:
        seen.add(id(sub))
        if sub.meta.tool_use_id in cycle_of_use:
            return cycle_of_use[sub.meta.tool_use_id]
        sub = by_agent.get(agent_key(sub.meta.parent_agent_id)) if sub.meta.parent_agent_id else None
    return None


# -- measured use --------------------------------------------------------------


@dataclass(slots=True)
class ScopeUse:
    note_tokens: int = 0
    note_cost: float = 0.0
    tag_tokens: int = 0
    tag_cost: float = 0.0

    @property
    def cost(self) -> float:
        return self.note_cost + self.tag_cost


@dataclass(slots=True)
class CaptureUsage:
    """What capture cost from ``since`` on (every captured session when
    ``since`` is empty)."""

    since: str = ""
    #: Sessions and subagents that carried a capture note.
    sessions: int = 0
    subagents: int = 0
    notes: int = 0
    #: ``main``, ``subagent``, ``tool`` (notes after tool results) and
    #: ``brief`` (the ``[spawn: ...]``/``[retry: ...]`` words a brief
    #: starts with).
    scopes: dict[str, ScopeUse] = field(default_factory=dict)
    #: metric id -> USD, the note and tag cost split by each metric's share.
    by_metric: dict[str, float] = field(default_factory=dict)
    #: metric id -> answers collected.
    answers: dict[str, int] = field(default_factory=dict)
    #: ISO date -> USD.
    daily: dict[str, float] = field(default_factory=dict)
    #: Everything the captured sessions cost, capture included.
    spend: float = 0.0
    cycles: int = 0
    tagged_cycles: int = 0
    reports: int = 0
    tagged_reports: int = 0

    @property
    def note_tokens(self) -> int:
        return sum(s.note_tokens for s in self.scopes.values())

    @property
    def tag_tokens(self) -> int:
        return sum(s.tag_tokens for s in self.scopes.values())

    @property
    def cost(self) -> float:
        return sum(s.cost for s in self.scopes.values())

    @property
    def share(self) -> float | None:
        """Capture's percentage of what the captured sessions cost."""
        return 100.0 * self.cost / self.spend if self.spend > 0 else None

    @property
    def coverage(self) -> float | None:
        """Percentage of your messages whose answer ended with a tag."""
        return 100.0 * self.tagged_cycles / self.cycles if self.cycles else None

    @property
    def report_coverage(self) -> float | None:
        return 100.0 * self.tagged_reports / self.reports if self.reports else None

    def _add(self, scope: str, *, note_chars: int = 0, note_cost: float = 0.0, tag_chars: int = 0,
             tag_cost: float = 0.0, day: str = "") -> None:
        use = self.scopes.setdefault(scope, ScopeUse())
        use.note_tokens += round(note_chars / CHARS_PER_TOKEN)
        use.note_cost += note_cost
        use.tag_tokens += round(tag_chars / CHARS_PER_TOKEN)
        use.tag_cost += tag_cost
        if day:
            self.daily[day] = self.daily.get(day, 0.0) + note_cost + tag_cost

    def _split(self, weights: dict[str, float], cost: float) -> None:
        total = sum(weights.values())
        for metric_id, weight in weights.items():
            if total > 0:
                self.by_metric[metric_id] = self.by_metric.get(metric_id, 0.0) + cost * weight / total

    def _count(self, metric_id: str) -> None:
        self.answers[metric_id] = self.answers.get(metric_id, 0) + 1


def _day(ts: str | None) -> str:
    moment = _parse_ts(ts)
    return moment.astimezone(timezone.utc).date().isoformat() if moment is not None else ""


def _output_usd_per_char(turn: Turn | None, pricing: Pricing | None) -> float:
    if turn is None or pricing is None:
        return 0.0
    resolved = pricing.resolve_model(turn.model)
    rates = effective_rates(turn, resolved) if resolved is not None else None
    return rates.output / CHARS_PER_TOKEN / 1_000_000 if rates is not None else 0.0


def _note_weights(codes, scope: str) -> dict[str, float]:
    """Each metric's share of a note: the length of what it adds there."""
    weights = {}
    for code in codes:
        m = catalogue.METRICS_BY_ID.get(code)
        if m is None:
            continue
        if scope == "tool":
            weights[code] = len(m.tool_note)
        elif scope == "main":
            weights[code] = len(m.main_line) + len(m.main_extra)
        else:
            weights[code] = len(m.sub_line) + len(m.sub_extra)
    return {k: v for k, v in weights.items() if v > 0}


def _tag_weights(turn: Turn, subagent: bool) -> dict[str, float]:
    """The metrics a tag answered, weighted by what each usually costs."""
    weights: dict[str, float] = {}
    tag = turn.cap
    fields = _SUB_FIELDS if subagent else _MAIN_FIELDS
    if tag is not None:
        for name, metric_id in fields.items():
            value = getattr(tag, name, None)
            if value not in (None, ()):
                weights[metric_id] = catalogue.METRICS_BY_ID[metric_id].out_chars
    if subagent and turn.result_marker:
        weights["result"] = catalogue.METRICS_BY_ID["result"].out_chars
    return weights


def _segment_ends(result: TranscriptResult, carry: _Carry) -> list[int]:
    """Turn indexes where a compaction drops what was carried."""
    return sorted(
        carry.index_at(event.ts) for event in result.events if event.kind == EventKind.COMPACT_BOUNDARY and event.ts
    )


def _add_notes(use: CaptureUsage, result: TranscriptResult, carry: _Carry, subagent: bool, since) -> None:
    ends = _segment_ends(result, carry)
    for event in result.events:
        if event.subkind != "capture_note" or not event.size_chars:
            continue
        moment = _parse_ts(event.ts)
        if since is not None and (moment is None or moment < since):
            continue
        hook_event = event.detail.get("hook")
        scope = "tool" if hook_event == "PostToolUse" else "subagent" if subagent else "main"
        start = carry.index_at(event.ts)
        end = next((e for e in ends if e > start), len(carry.turns))
        cost = carry.cost(event.size_chars, start, end)
        use.notes += 1
        use._add(scope, note_chars=event.size_chars, note_cost=cost, day=_day(event.ts))
        use._split(_note_weights(event.detail.get("codes", ()), scope), cost)


def _add_tags(use: CaptureUsage, result: TranscriptResult, pricing, subagent: bool, since) -> None:
    for turn in _priced(result):
        moment = _parse_ts(turn.ts)
        if since is not None and (moment is None or moment < since):
            continue
        chars = turn.cap.chars if turn.cap is not None else 0
        if subagent and turn.result_marker and (turn.cap is None or not turn.cap.chars):
            chars = len(f"[result: {turn.result_marker}]")
        if not chars:
            continue
        cost = (chars + 1) * _output_usd_per_char(turn, pricing)
        use._add("subagent" if subagent else "main", tag_chars=chars + 1, tag_cost=cost, day=_day(turn.ts))
        weights = _tag_weights(turn, subagent)
        use._split(weights, cost)
        for metric_id in weights:
            use._count(metric_id)


def _add_brief_markers(use: CaptureUsage, sub: TranscriptResult, spawner: Turn | None, pricing, since) -> None:
    first = next(iter(_priced(sub)), None)
    if first is None:
        return
    moment = _parse_ts(first.ts)
    if since is not None and (moment is None or moment < since):
        return
    for metric_id, word in (("spawn", first.spawn_marker), ("retry", first.retry_marker)):
        if not word:
            continue
        chars = len(f"[{metric_id}: {word}]") + 1
        cost = chars * _output_usd_per_char(spawner or first, pricing)
        use._add("brief", tag_chars=chars, tag_cost=cost, day=_day(first.ts))
        use._split({metric_id: 1.0}, cost)
        use._count(metric_id)


def _spend(result: TranscriptResult, pricing, since) -> float:
    if pricing is None:
        return 0.0
    total = 0.0
    for turn in _priced(result):
        moment = _parse_ts(turn.ts)
        if since is not None and (moment is None or moment < since):
            continue
        total += price_turn(turn, pricing.resolve_model(turn.model)).total
    return total


def usage(corpus, pricing: Pricing | None, since: str = "") -> CaptureUsage:
    """What capture cost across ``corpus`` from ``since`` (an ISO time)
    on. A session counts once its main transcript carries a capture note;
    its subagents count with it."""
    use = CaptureUsage(since=since)
    start = _parse_ts(since) if since else None
    if start is not None and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    for bundle in corpus.sessions:
        top = bundle.top
        captured_top = top is not None and top.meta.cap_injections > 0
        subs = [sub for sub in bundle.subs if captured_top or sub.meta.cap_injections > 0]
        if not captured_top and not subs:
            continue
        spawners: dict[str, Turn] = {}
        for result in ([top] if captured_top else []) + list(bundle.subs):
            for turn in result.turns:
                for tool_use_id in turn.tool_use_ids:
                    spawners[tool_use_id] = turn
        if captured_top:
            use.sessions += 1
            carry = _Carry(top, pricing)
            _add_notes(use, top, carry, False, start)
            _add_tags(use, top, pricing, False, start)
            use.spend += _spend(top, pricing, start)
            for cycle in prompt_cycles(top):
                moment = _parse_ts(cycle.turns[0].ts) if cycle.turns else None
                if start is not None and (moment is None or moment < start):
                    continue
                use.cycles += 1
                tag = cycle.tag
                if tag is not None:
                    use.tagged_cycles += 1
        for sub in subs:
            use.subagents += 1
            carry = _Carry(sub, pricing)
            _add_notes(use, sub, carry, True, start)
            _add_tags(use, sub, pricing, True, start)
            _add_brief_markers(use, sub, spawners.get(sub.meta.tool_use_id or ""), pricing, start)
            use.spend += _spend(sub, pricing, start)
            turns = _priced(sub)
            if sub.meta.agent_type in catalogue.SKIP_AGENT_TYPES or not turns:
                continue
            moment = _parse_ts(turns[0].ts)
            if start is not None and (moment is None or moment < start):
                continue
            use.reports += 1
            if any(turn.result_marker for turn in turns):
                use.tagged_reports += 1
    return use


# -- estimates from your own history ---------------------------------------------


@dataclass(slots=True)
class History:
    """What one character costs in each place capture would put it,
    summed over the sessions replayed (see :func:`history`)."""

    days: int = 0
    sessions: int = 0
    cycles: int = 0
    subagents: int = 0
    #: Notes a level would inject: one per session start and compaction.
    main_notes: int = 0
    sub_notes: int = 0
    #: USD per note character, summed over every injection.
    main_note: float = 0.0
    sub_note: float = 0.0
    #: The same for Explore and Plan agents, whose note leaves out
    #: ``rules`` and ``agent_brief``.
    sub_note_no_rules: float = 0.0
    #: USD per tag character: one per prompt cycle, per subagent report,
    #: per brief.
    reply_tag: float = 0.0
    report_tag: float = 0.0
    brief_tag: float = 0.0
    #: Tool results a Deep tool note would follow, what one character of
    #: that note costs carried onward, and one character of the word
    #: Claude adds to its tag.
    big_outputs: int = 0
    big_output_note: float = 0.0
    big_output_tag: float = 0.0
    web_results: int = 0
    web_note: float = 0.0
    web_tag: float = 0.0
    #: What the replayed sessions cost.
    spend: float = 0.0


def _carry_per_char(carry: _Carry, start: int, end: int) -> float:
    return carry.cost(1_000_000, start, end) / 1_000_000


def _segments(result: TranscriptResult, carry: _Carry) -> list[tuple[int, int]]:
    """``(start, end)`` turn ranges between compactions: a note is
    injected at each start."""
    cuts = [0] + [e for e in _segment_ends(result, carry) if 0 < e < len(carry.turns)]
    cuts = sorted(set(cuts))
    return [(s, e) for s, e in zip(cuts, cuts[1:] + [len(carry.turns)]) if e > s]


def _big_output(turn: Turn) -> bool:
    threshold = catalogue.BIG_OUTPUT_TOKENS * CHARS_PER_TOKEN
    return any(chars >= threshold for chars in turn.tool_result_chars_by_tool.values())


def _web_calls(turn: Turn) -> int:
    return sum(turn.tool_calls_by_tool.get(tool, 0) for tool in catalogue.WEB_TOOLS)


def history(corpus, pricing: Pricing | None, days: int = 14) -> History:
    """Replay ``corpus`` (load it for the last ``days`` days) as if
    capture had been on for every session."""
    out = History(days=days)
    for bundle in corpus.sessions:
        top = bundle.top
        spawners: dict[str, Turn] = {}
        if top is not None and _priced(top):
            out.sessions += 1
            carry = _Carry(top, pricing)
            _replay_notes(out, top, carry, "main")
            for turn in _priced(top):
                for tool_use_id in turn.tool_use_ids:
                    spawners[tool_use_id] = turn
            for cycle in prompt_cycles(top):
                out.cycles += 1
                out.reply_tag += _output_usd_per_char(cycle.turns[-1], pricing)
            out.spend += _spend(top, pricing, None)
        for sub in bundle.subs:
            turns = _priced(sub)
            out.spend += _spend(sub, pricing, None)
            if not turns or sub.meta.agent_type in catalogue.SKIP_AGENT_TYPES:
                continue
            out.subagents += 1
            carry = _Carry(sub, pricing)
            scope = "no_rules" if sub.meta.agent_type in catalogue.NO_RULES_AGENT_TYPES else "sub"
            _replay_notes(out, sub, carry, scope)
            out.report_tag += _output_usd_per_char(turns[-1], pricing)
            out.brief_tag += _output_usd_per_char(spawners.get(sub.meta.tool_use_id or "", turns[0]), pricing)
    return out


def _replay_notes(out: History, result: TranscriptResult, carry: _Carry, scope: str) -> None:
    segments = _segments(result, carry)
    for start, end in segments:
        per_char = _carry_per_char(carry, start, end)
        if scope == "main":
            out.main_notes += 1
            out.main_note += per_char
        else:
            out.sub_notes += 1
            if scope == "no_rules":
                out.sub_note_no_rules += per_char
            else:
                out.sub_note += per_char
    for start, end in segments:
        for index in range(start, end):
            turn = carry.turns[index]
            # A tool result arrives with the next turn, and the note with it.
            follow = min(index + 1, end - 1)
            if index + 1 >= end:
                continue
            if _big_output(turn):
                out.big_outputs += 1
                out.big_output_note += _carry_per_char(carry, follow, end)
                out.big_output_tag += _output_usd_per_char(carry.turns[follow], carry.pricing)
            web = _web_calls(turn)
            if web:
                out.web_results += web
                out.web_note += web * _carry_per_char(carry, follow, end)
                out.web_tag += web * _output_usd_per_char(carry.turns[follow], carry.pricing)


@dataclass(frozen=True, slots=True)
class Estimate:
    """What a set of metrics would have cost over the replayed days."""

    cost: float = 0.0
    #: Note tokens injected and tag tokens written over those days.
    note_tokens: int = 0
    tag_tokens: int = 0
    days: int = 0
    spend: float = 0.0

    @property
    def per_week(self) -> float:
        return self.cost * 7 / self.days if self.days else 0.0

    @property
    def share(self) -> float | None:
        return 100.0 * self.cost / self.spend if self.spend > 0 else None


def _note_chars(ids, scope: str, agent_type: str = "") -> int:
    text = catalogue.note_text(ids, scope, agent_type)
    return len(text) + _WRAP["SessionStart" if scope == "main" else "SubagentStart"] if text else 0


def estimate(past: History, ids, sample: int = 100) -> Estimate:
    """What the metrics in ``ids`` would have cost over ``past``, with
    ``sample`` percent of sessions captured."""
    ids = tuple(ids)
    wanted = set(ids)
    main = _note_chars(ids, "main")
    sub = _note_chars(ids, "subagent", "general-purpose")
    no_rules = _note_chars(ids, "subagent", "Explore")
    enabled = [catalogue.METRICS_BY_ID[i] for i in ids if i in catalogue.METRICS_BY_ID]
    reply = sum(m.out_chars for m in enabled if m.main_line)
    reply = reply + _TAG_FRAME_CHARS if reply else 0
    report = sum(m.out_chars for m in enabled if m.sub_line)
    report = report + _TAG_FRAME_CHARS if report else 0
    # A brief's [spawn:]/[retry:] words, per subagent; the feedback
    # reminder's line, at most once per message of yours.
    brief = sum(m.out_chars for m in enabled if (m.main_extra or m.sub_extra) and m.group != "feedback")
    reminder = sum(m.out_chars for m in enabled if m.main_extra and m.group == "feedback")
    cost = (
        main * past.main_note
        + sub * past.sub_note
        + no_rules * past.sub_note_no_rules
        + (reply + reminder) * past.reply_tag
        + report * past.report_tag
        + brief * past.brief_tag
    )
    note_tokens = main * past.main_notes + max(sub, no_rules) * past.sub_notes
    tag_tokens = (reply + reminder) * past.cycles + report * past.subagents + brief * past.subagents
    for metric_id, count, note, tag in (
        ("big_output", past.big_outputs, past.big_output_note, past.big_output_tag),
        ("web", past.web_results, past.web_note, past.web_tag),
    ):
        if metric_id in wanted:
            chars = len(catalogue.tool_note_text(metric_id)) + _WRAP["PostToolUse"]
            out_chars = catalogue.METRICS_BY_ID[metric_id].out_chars
            cost += chars * note + out_chars * tag
            note_tokens += chars * count
            tag_tokens += out_chars * count
    share = max(0, min(100, sample)) / 100
    return Estimate(
        cost=cost * share,
        note_tokens=round(note_tokens * share / CHARS_PER_TOKEN),
        tag_tokens=round(tag_tokens * share / CHARS_PER_TOKEN),
        days=past.days,
        spend=past.spend,
    )


def level_estimates(past: History, sample: int = 100) -> dict[str, Estimate]:
    """:func:`estimate` for each level from Free to Deep."""
    return {level: estimate(past, catalogue.level_metrics(level), sample) for level in catalogue.LEVELS[1:]}


def metric_estimates(past: History, ids, sample: int = 100) -> dict[str, float]:
    """What each metric in ``ids`` adds to them, in USD over the replayed
    days: the estimate with it minus the estimate without it (and
    without what needs it). Its share of the note's fixed lines is
    included, so the parts don't sum exactly to the whole."""
    ids = tuple(ids)
    whole = estimate(past, ids, sample).cost
    out = {}
    for metric_id in ids:
        if metric_id not in catalogue.METRICS_BY_ID:
            continue
        without = tuple(
            i for i in ids if i != metric_id and metric_id not in catalogue.METRICS_BY_ID[i].requires
        )
        out[metric_id] = max(0.0, whole - estimate(past, without, sample).cost)
    return out


def enough_data(use: CaptureUsage, metric_id: str, signal_sessions: int = 0) -> tuple[int, int]:
    """``(answers collected, answers wanted)`` for one metric."""
    target = enough_target(metric_id)
    if metric_id in catalogue.METRICS_BY_ID and catalogue.METRICS_BY_ID[metric_id].group == "free":
        return signal_sessions, target
    return use.answers.get(metric_id, 0), target


__all__ = [
    "CaptureUsage",
    "Cycle",
    "ENOUGH",
    "Estimate",
    "HISTORY_DAYS",
    "History",
    "ScopeUse",
    "enough_data",
    "enough_target",
    "estimate",
    "history",
    "level_estimates",
    "metric_estimates",
    "prompt_cycles",
    "usage",
]
