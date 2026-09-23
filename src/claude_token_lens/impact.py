"""Did it work? For each change point (:mod:`change_points`), the
sessions started before it against those started after it, on the
measures that change should move.

Per session, not per window, so a quiet week after a change doesn't
read as a saving: cost per session, cost per reply, the share of cache
writes that rebuilt expired context, summaries per session, the context
at session start, and, for a change to one agent, that agent's cost and
start-up context per spawn. "Before" is the sessions started in the
:data:`LOOKBACK_DAYS` before the change (and after the change before
it); "after" is those started from the change until the next one.

Sessions differ in size and kind of work, so a difference is a signal,
not proof; with fewer than :data:`MIN_SESSIONS` on either side there is
no verdict at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import recache
from .change_points import ChangePoint
from .model import EventKind, TranscriptResult
from .pricing import Pricing, price_turn
from .units import Units

#: Sessions needed on each side of a change before comparing.
MIN_SESSIONS = 3
#: How far back "before" reaches.
LOOKBACK_DAYS = 14
#: A change smaller than this (either way) reads as "about the same".
NOISE_PCT = 5.0
#: Changes this close together (one apply writing several files, say)
#: share their before and after instead of cutting each other's short.
TOGETHER = timedelta(minutes=10)

CAVEAT = (
    "Sessions differ in size and kind of work, so read a difference as a signal, not proof. "
    "It firms up as more sessions run after the change."
)


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _priced(result: TranscriptResult):
    return [turn for turn in result.turns if turn.turn_index > 0]


@dataclass(slots=True)
class _Transcript:
    cost: float = 0.0
    turns: int = 0
    startup_tokens: int = 0
    peak_context: int = 0
    rebuild_tokens: int = 0
    write_tokens: int = 0
    summaries: int = 0


@dataclass(slots=True)
class SessionFacts:
    start: datetime
    main: _Transcript
    #: (agent type, facts) per subagent spawn.
    spawns: list[tuple[str, _Transcript]] = field(default_factory=list)

    @property
    def cost(self) -> float:
        return self.main.cost + sum(spawn.cost for _agent, spawn in self.spawns)


def _transcript(result: TranscriptResult, pricing: Pricing) -> _Transcript:
    facts = _Transcript()
    turns = _priced(result)
    if turns:
        first = turns[0]
        facts.startup_tokens = first.input_tokens + first.cache_creation_tokens + first.cache_read_tokens
    rebuilt = {t.message_id for t in recache.detect(turns, recache.RecacheThresholds()) if t.message_id}
    for turn in turns:
        resolved = pricing.resolve_model(turn.model)
        facts.cost += price_turn(turn, resolved).total
        facts.turns += 1
        facts.peak_context = max(
            facts.peak_context, turn.input_tokens + turn.cache_creation_tokens + turn.cache_read_tokens
        )
        facts.write_tokens += turn.cache_creation_tokens
        if turn.message_id in rebuilt:
            facts.rebuild_tokens += turn.cache_creation_tokens
    facts.summaries = sum(1 for event in result.events if event.kind == EventKind.COMPACT_BOUNDARY)
    return facts


def session_facts(corpus, pricing: Pricing) -> list[SessionFacts]:
    """One :class:`SessionFacts` per session with a top-level transcript
    and a start time, oldest first."""
    out: list[SessionFacts] = []
    for bundle in corpus.sessions:
        top = bundle.top
        if top is None:
            continue
        start = next((_ts(turn.ts) for turn in _priced(top) if _ts(turn.ts)), None)
        if start is None:
            continue
        out.append(
            SessionFacts(
                start=start,
                main=_transcript(top, pricing),
                spawns=[(sub.meta.agent_type or "(unknown)", _transcript(sub, pricing)) for sub in bundle.subs],
            )
        )
    out.sort(key=lambda s: s.start)
    return out


# -- measures ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Measure:
    key: str
    label: str
    #: "money" or "tokens" or "pct" or "count".
    kind: str
    agent: str | None = None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _value(measure: Measure, sessions: list[SessionFacts]) -> tuple[float | None, int]:
    """The measure over ``sessions``, and how many sessions or spawns it rests on."""
    if measure.agent is not None:
        spawns = [facts for s in sessions for agent, facts in s.spawns if agent == measure.agent]
        if measure.key == "agent_cost":
            return _mean([f.cost for f in spawns]), len(spawns)
        return _mean([float(f.startup_tokens) for f in spawns]), len(spawns)
    if measure.key == "cost_per_session":
        return _mean([s.cost for s in sessions]), len(sessions)
    if measure.key == "cost_per_turn":
        turns = sum(s.main.turns for s in sessions)
        return (sum(s.main.cost for s in sessions) / turns if turns else None), len(sessions)
    if measure.key == "rebuild_share":
        writes = sum(s.main.write_tokens for s in sessions)
        return (100.0 * sum(s.main.rebuild_tokens for s in sessions) / writes if writes else None), len(sessions)
    if measure.key == "summaries":
        return _mean([float(s.main.summaries) for s in sessions]), len(sessions)
    if measure.key == "peak_context":
        return _mean([float(s.main.peak_context) for s in sessions]), len(sessions)
    if measure.key == "startup_tokens":
        return _mean([float(s.main.startup_tokens) for s in sessions]), len(sessions)
    return None, 0  # pragma: no cover


_COST = Measure("cost_per_session", "Cost per session", "money")
_TURN = Measure("cost_per_turn", "Cost per reply", "money")
_REBUILD = Measure("rebuild_share", "Cache writes that rebuilt expired context", "pct")
_SUMMARIES = Measure("summaries", "Conversation summaries per session", "count")
_PEAK = Measure("peak_context", "Largest context per session", "tokens")
_STARTUP = Measure("startup_tokens", "Context at the start of a session", "tokens")


def measures_for(point: ChangePoint) -> list[Measure]:
    """The measures a change should move, most telling first; cost per
    session always comes last as the overall check."""
    chosen: list[Measure] = []

    def add(measure: Measure) -> None:
        if measure not in chosen:
            chosen.append(measure)

    for label in point.keys or ():
        agent, _, key = label.rpartition(": ")
        key = key.split(".")[-1] if key.startswith(("effective.", "user_settings.", "project_settings.")) else key
        if label.startswith("agents."):
            parts = label.split(".")
            agent, key = (parts[1], parts[-1]) if len(parts) >= 3 else ("", key)
        if agent:
            add(Measure("agent_cost", f"{agent}: cost per spawn", "money", agent))
            add(Measure("agent_startup", f"{agent}: context at the start of each spawn", "tokens", agent))
        elif key in ("model", "effortLevel", "alwaysThinkingEnabled", "MAX_THINKING_TOKENS", "fastMode"):
            add(_TURN)
        elif key == "autoCompactWindow":
            add(_SUMMARIES)
            add(_PEAK)
        elif "ttl" in key.lower():
            add(_REBUILD)
        elif key in ("skillOverrides", "enabledPlugins", "enabledMcpjsonServers", "disabledMcpjsonServers") or key.startswith(
            ("mcp_servers", "enabled_plugins")
        ):
            add(_STARTUP)
    if not chosen:
        add(_STARTUP)
    add(_COST)
    return chosen


# -- comparing -------------------------------------------------------------


def _text(kind: str, value: float | None, units: Units) -> str:
    if value is None:
        return "no data"
    if kind == "money":
        amount = units.money(value)
        return amount.text() if amount is not None else "none"
    if kind == "pct":
        return f"{value:.0f}%"
    if kind == "count":
        return f"{value:.1f}"
    return f"{round(value):,} tokens"


def compare(
    point: ChangePoint,
    sessions: list[SessionFacts],
    units: Units,
    *,
    previous: ChangePoint | None = None,
    following: ChangePoint | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    start = point.ts - timedelta(days=LOOKBACK_DAYS)
    if previous is not None and previous.ts > start:
        start = previous.ts
    end = following.ts if following is not None else now
    before = [s for s in sessions if start <= s.start < point.ts]
    after = [s for s in sessions if point.ts <= s.start < end]
    enough = len(before) >= MIN_SESSIONS and len(after) >= MIN_SESSIONS
    rows = []
    for measure in measures_for(point):
        old, old_n = _value(measure, before)
        new, new_n = _value(measure, after)
        change_pct = ((new - old) / old * 100.0) if old and new is not None else None
        rows.append(
            {
                "label": measure.label,
                "before": _text(measure.kind, old, units),
                "after": _text(measure.kind, new, units),
                "before_n": old_n,
                "after_n": new_n,
                "change_pct": round(change_pct, 1) if change_pct is not None else None,
                "direction": (
                    None if change_pct is None else "same" if abs(change_pct) < NOISE_PCT
                    else "lower" if change_pct < 0 else "higher"
                ),
            }
        )
    return {
        "change": point.to_dict(),
        "before_sessions": len(before),
        "after_sessions": len(after),
        "enough": enough,
        "verdict": _verdict(rows, len(before), len(after), enough),
        "measures": rows,
    }


def _verdict(rows: list[dict], before: int, after: int, enough: bool) -> str:
    if not enough:
        if after < MIN_SESSIONS:
            return (
                f"Too few sessions since the change to compare yet: {after} so far. "
                f"Check back after {MIN_SESSIONS}."
            )
        return f"Too few sessions before the change to compare: {before}."
    lead = next((row for row in rows if row["change_pct"] is not None), None)
    if lead is None:
        return "No data on the measures this change should move."
    if lead["direction"] == "same":
        return f"{lead['label']}: about the same ({lead['before']} before, {lead['after']} after)."
    word = "fell" if lead["direction"] == "lower" else "rose"
    return (
        f"{lead['label']} {word} {abs(lead['change_pct']):.0f}%, from {lead['before']} to {lead['after']} "
        f"({before} sessions before, {after} after)."
    )


def impact(points: list[ChangePoint], sessions: list[SessionFacts], units: Units, *, limit: int = 10) -> list[dict]:
    """Newest change first, at most ``limit``. A change made within
    :data:`TOGETHER` of another doesn't bound its before or after."""
    out = []
    for index in range(len(points) - 1, -1, -1):
        point = points[index]
        previous = next((p for p in reversed(points[:index]) if point.ts - p.ts > TOGETHER), None)
        following = next((p for p in points[index + 1 :] if p.ts - point.ts > TOGETHER), None)
        out.append(compare(point, sessions, units, previous=previous, following=following))
        if len(out) >= limit:
            break
    return out


__all__ = ["CAVEAT", "LOOKBACK_DAYS", "MIN_SESSIONS", "SessionFacts", "compare", "impact", "measures_for", "session_facts"]
