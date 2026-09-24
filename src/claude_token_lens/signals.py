"""The free signals metrics capture logs, read back.

While the ``session_end``, ``waits`` or ``permissions`` metric is on,
``hooks/capture-hook.py`` appends one line per SessionEnd, Notification
or PermissionRequest hook call to ``<config-dir>/signals/YYYY-MM.jsonl``::

    {"ts":"2026-09-24T06:10:00Z","sid":"3f1c...","e":"wait","kind":"permission"}

``sid`` is the session id hashed with Token Lens's salt, so the files
alone don't say which session is which; :func:`by_session` joins them
back to the sessions this tool already knows. The other field is a word
from a fixed list (why the session ended, what Claude waited for) or a
tool name. ``sub`` marks a call from inside a subagent.

:func:`load` keeps only lines of that exact shape, so a hand-edited or
foreign line can't carry anything else into a report. :func:`prune`
deletes month files older than ``retention_days``; the watcher calls it
on each tick, next to the store's own retention prune.

The transcripts already hold the other signals capture once planned a
hook for (instruction files loaded, commands and skills run, task lists,
API errors), so those are read from the transcripts instead.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .capture_catalogue import SESSION_END_REASONS, SIGNALS_DIR, WAIT_KINDS

#: A line's event code -> the field holding its value.
EVENT_FIELDS = {"end": "reason", "wait": "kind", "perm": "tool"}

_SID_RE = re.compile(r"[0-9a-f]{16}")
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_MONTH_FILE_RE = re.compile(r"(\d{4})-(\d{2})\.jsonl")


@dataclass(frozen=True, slots=True)
class Signal:
    """One logged hook call."""

    at: datetime
    session_hash: str
    #: ``end``, ``wait`` or ``perm``.
    event: str
    #: Why the session ended, what Claude waited for, or the tool that
    #: asked for permission.
    value: str
    subagent: bool = False


@dataclass(slots=True)
class SessionSignals:
    """What the signals say about one session."""

    #: Why it last ended (``""`` if no end was logged).
    end_reason: str = ""
    #: What Claude waited for -> how many times.
    waits: dict[str, int] = field(default_factory=dict)
    #: Tool name -> permission prompts.
    permission_prompts: dict[str, int] = field(default_factory=dict)
    #: How many of those came from inside a subagent.
    subagent_events: int = 0


def signals_dir(config_dir: str | Path) -> Path:
    return Path(config_dir) / SIGNALS_DIR


def session_hash(salt: bytes, session_id: str) -> str:
    """A session id as the hook logs it."""
    return hmac.new(salt, session_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def _month_files(config_dir: str | Path) -> list[tuple[datetime, Path]]:
    """``(first moment of the month, path)`` for each month file, oldest
    first."""
    try:
        entries = list(signals_dir(config_dir).iterdir())
    except OSError:
        return []
    months = []
    for path in entries:
        match = _MONTH_FILE_RE.fullmatch(path.name)
        if match and 1 <= int(match.group(2)) <= 12:
            months.append((datetime(int(match.group(1)), int(match.group(2)), 1, tzinfo=timezone.utc), path))
    return sorted(months)


def _next_month(start: datetime) -> datetime:
    return start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)


def _valid_value(event: str, value) -> bool:
    if not isinstance(value, str):
        return False
    if event == "end":
        return value in SESSION_END_REASONS
    if event == "wait":
        return value in WAIT_KINDS
    return bool(_TOOL_NAME_RE.fullmatch(value))


def _signal_from(record) -> Signal | None:
    if not isinstance(record, dict):
        return None
    event, sid, ts = record.get("e"), record.get("sid"), record.get("ts")
    if event not in EVENT_FIELDS or not isinstance(sid, str) or not _SID_RE.fullmatch(sid) or not isinstance(ts, str):
        return None
    value = record.get(EVENT_FIELDS[event])
    if not _valid_value(event, value):
        return None
    try:
        at = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return Signal(at=at, session_hash=sid, event=event, value=value, subagent=record.get("sub") == 1)


def load(config_dir: str | Path, *, since: datetime | None = None) -> list[Signal]:
    """Every well-formed signal, oldest first (from ``since`` on, when
    given). Unreadable files and malformed lines are skipped."""
    out: list[Signal] = []
    for start, path in _month_files(config_dir):
        if since is not None and _next_month(start) <= since:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                signal = _signal_from(json.loads(line))
            except ValueError:
                continue
            if signal is not None and (since is None or signal.at >= since):
                out.append(signal)
    out.sort(key=lambda s: s.at)
    return out


def by_session(signals: list[Signal], session_ids, salt: bytes) -> dict[str, SessionSignals]:
    """The signals of each of ``session_ids`` that has any, keyed by the
    session id."""
    lookup = {session_hash(salt, sid): sid for sid in session_ids}
    out: dict[str, SessionSignals] = {}
    for signal in signals:
        session_id = lookup.get(signal.session_hash)
        if session_id is None:
            continue
        seen = out.setdefault(session_id, SessionSignals())
        if signal.event == "end":
            seen.end_reason = signal.value
        elif signal.event == "wait":
            seen.waits[signal.value] = seen.waits.get(signal.value, 0) + 1
        else:
            seen.permission_prompts[signal.value] = seen.permission_prompts.get(signal.value, 0) + 1
        seen.subagent_events += signal.subagent
    return out


def prune(config_dir: str | Path, retention_days: int, now: datetime | None = None) -> int:
    """Delete the month files wholly older than ``retention_days``;
    returns how many went."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    removed = 0
    for start, path in _month_files(config_dir):
        if _next_month(start) <= cutoff:
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
    return removed


__all__ = [
    "EVENT_FIELDS",
    "SessionSignals",
    "Signal",
    "by_session",
    "load",
    "prune",
    "session_hash",
    "signals_dir",
]
