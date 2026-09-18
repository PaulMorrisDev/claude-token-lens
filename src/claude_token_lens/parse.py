"""Single-pass transcript parsing: JSONL lines in, ``TranscriptResult`` out.

Implements the project plan's "Parsing" section: assistant lines are
grouped into ``Turn`` records by ``message.id`` (fallback ``requestId``,
then ``uuid``); every other line becomes an ``Event`` (via
``events.classify_line``) attached to the next finalised turn as
``preceding_event_kinds``/``preceding_attachment_types``/
``preceding_primary``.

Deviation from the plan, proposed here rather than silently made: the
plan's WP1 brief describes ``preceding_tool`` as "Bash"/"PowerShell"/
<first tool name>, which reads as a priority scan for Bash/PowerShell.
``model.Turn.preceding_tool``'s own docstring comment documents it as a
plain enumeration of possible *values*, not a scan order, and the fuller
plan text ("preceding tool and command-prefix tables" in the RE-CACHE
section) treats it as "whatever tool was used" for grouping. This module
takes ``preceding_tool`` to be simply the previous turn's first tool name
(Bash/PowerShell are just the common, RE-CACHE-relevant cases) — not a
scan that prefers Bash/PowerShell over an earlier different tool.

Privacy: no raw JSONL line, message content, tool_result content, file
path, or command is ever retained past the single line/block that
produces it. Only lengths, short prefixes (<=40 chars), names, and counts
survive into ``Turn``/``Event``/``Diagnostics``.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import events as events_mod
from . import jsonl
from .model import Diagnostics, Event, EventKind, Turn, TranscriptMeta, TranscriptResult

#: Tool names whose first ``input.command`` becomes a turn's ``cmd_prefix``.
_SHELL_TOOL_NAMES = ("Bash", "PowerShell")

#: tool name -> the input key holding the path to check against the
#: system temp dir for ``edit_kind`` ("scratch" vs "real").
_EDIT_TOOL_PATH_KEYS = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

_CMD_PREFIX_MAX_CHARS = 40


def _escape_newlines(text: str) -> str:
    return text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _parse_ts(ts_raw: str) -> datetime | None:
    if not ts_raw:
        return None
    try:
        return datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _turn_key(d: dict) -> str:
    """The grouping key for one assistant line: ``message.id``, falling
    back to ``requestId``, then ``uuid``. Prefixed by source so the three
    id spaces never collide with each other.
    """
    message = d.get("message")
    message_id = message.get("id") if isinstance(message, dict) else None
    if isinstance(message_id, str) and message_id:
        return f"id:{message_id}"
    request_id = d.get("requestId")
    if isinstance(request_id, str) and request_id:
        return f"req:{request_id}"
    uuid = d.get("uuid")
    if isinstance(uuid, str) and uuid:
        return f"uuid:{uuid}"
    return "key:"


@dataclass(slots=True)
class _PendingTurn:
    """Scalar accumulator for one in-progress ``message.id`` group. Never
    retains a raw line dict or message content past the block that fed
    it — only the small derived fields ``Turn`` itself needs.
    """

    message_id: str = ""
    request_id: str = ""
    ts_raw: str = ""
    model: str = ""
    service_tier: str | None = None
    is_synthetic: bool = False
    effort: str | None = None
    per_turn_effort: str | None = None
    has_usage: bool = False
    input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    web_search_requests: int = 0
    web_fetch_requests: int = 0
    cc_5m: int = 0
    cc_1h: int = 0
    inference_geo: str | None = None
    attribution_mcp_server: str | None = None
    attribution_mcp_tool: str | None = None
    attribution_skill: str | None = None
    tool_names: list[str] = field(default_factory=list)
    cmd_prefix: str | None = None
    edit_real_found: bool = False
    edit_scratch_found: bool = False


def _merge_content_blocks(pending: _PendingTurn, content, tool_use_names: dict[str, str]) -> None:
    if not isinstance(content, list):
        return
    tmpdir = tempfile.gettempdir().lower()
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name not in pending.tool_names:
            pending.tool_names.append(name)
        tool_use_id = block.get("id")
        if isinstance(tool_use_id, str) and tool_use_id:
            tool_use_names[tool_use_id] = name
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            continue
        if pending.cmd_prefix is None and name in _SHELL_TOOL_NAMES:
            command = tool_input.get("command")
            if isinstance(command, str) and command:
                pending.cmd_prefix = _escape_newlines(command)[:_CMD_PREFIX_MAX_CHARS]
        path_key = _EDIT_TOOL_PATH_KEYS.get(name)
        if path_key is not None:
            path_value = tool_input.get(path_key)
            if isinstance(path_value, str) and path_value:
                if path_value.lower().startswith(tmpdir):
                    pending.edit_scratch_found = True
                else:
                    pending.edit_real_found = True


def _new_pending(d: dict, tool_use_names: dict[str, str]) -> _PendingTurn:
    message = d.get("message")
    message = message if isinstance(message, dict) else {}
    usage = message.get("usage")

    pending = _PendingTurn()
    message_id = message.get("id")
    pending.message_id = message_id if isinstance(message_id, str) else ""
    request_id = d.get("requestId")
    pending.request_id = request_id if isinstance(request_id, str) else ""
    ts_raw = d.get("timestamp")
    pending.ts_raw = ts_raw if isinstance(ts_raw, str) else ""
    model = message.get("model")
    pending.model = model if isinstance(model, str) else ""
    pending.is_synthetic = pending.model == "<synthetic>" or bool(d.get("isApiErrorMessage"))
    effort = d.get("effort")
    pending.effort = effort if isinstance(effort, str) else None
    per_turn_effort = d.get("perTurnEffort")
    pending.per_turn_effort = per_turn_effort if isinstance(per_turn_effort, str) else None
    for attr, key in (
        ("attribution_mcp_server", "attributionMcpServer"),
        ("attribution_mcp_tool", "attributionMcpTool"),
        ("attribution_skill", "attributionSkill"),
    ):
        value = d.get(key)
        setattr(pending, attr, value if isinstance(value, str) else None)

    if isinstance(usage, dict):
        pending.has_usage = True
        pending.input_tokens = int(usage.get("input_tokens") or 0)
        pending.cache_creation_tokens = int(usage.get("cache_creation_input_tokens") or 0)
        pending.cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
        pending.output_tokens = int(usage.get("output_tokens") or 0)
        service_tier = usage.get("service_tier")
        pending.service_tier = service_tier if isinstance(service_tier, str) else None
        inference_geo = usage.get("inference_geo")
        pending.inference_geo = inference_geo if isinstance(inference_geo, str) else None
        details = usage.get("output_tokens_details")
        if isinstance(details, dict):
            pending.thinking_tokens = int(details.get("thinking_tokens") or 0)
        server_tool_use = usage.get("server_tool_use")
        if isinstance(server_tool_use, dict):
            pending.web_search_requests = int(server_tool_use.get("web_search_requests") or 0)
            pending.web_fetch_requests = int(server_tool_use.get("web_fetch_requests") or 0)
        cache_creation = usage.get("cache_creation")
        if isinstance(cache_creation, dict):
            pending.cc_5m = int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
            pending.cc_1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)

    _merge_content_blocks(pending, message.get("content"), tool_use_names)
    return pending


def _merge_into_pending(pending: _PendingTurn, d: dict, tool_use_names: dict[str, str]) -> None:
    if not pending.is_synthetic:
        message = d.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        if model == "<synthetic>" or d.get("isApiErrorMessage"):
            pending.is_synthetic = True
    message = d.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    _merge_content_blocks(pending, content, tool_use_names)


def _tool_result_length(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    total += len(text)
        return total
    return 0


def _accumulate_tool_results(
    d: dict,
    tool_use_names: dict[str, str],
    tool_result_chars: dict[str, int],
    tool_result_calls: dict[str, int],
) -> None:
    message = d.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tool_use_id = block.get("tool_use_id")
        name = tool_use_names.pop(tool_use_id, None) if isinstance(tool_use_id, str) else None
        if name is None:
            continue
        length = _tool_result_length(block.get("content"))
        tool_result_chars[name] = tool_result_chars.get(name, 0) + length
        tool_result_calls[name] = tool_result_calls.get(name, 0) + 1


def _resolve_preceding_tool(previous_turn: Turn | None) -> tuple[str, str | None]:
    if previous_turn is None:
        return "n/a", None
    if not previous_turn.tool_names:
        return "none", previous_turn.cmd_prefix
    return previous_turn.tool_names[0], previous_turn.cmd_prefix


def _finalize_turn(
    pending: _PendingTurn,
    pending_events: list[Event],
    pending_attachment_types: list[str],
    previous_turn: Turn | None,
    previous_non_synthetic_ts: datetime | None,
    priced_turn_count: int,
    diagnostics: Diagnostics,
) -> tuple[Turn, datetime | None, int]:
    ts_dt = _parse_ts(pending.ts_raw)
    ctx = pending.input_tokens + pending.cache_creation_tokens + pending.cache_read_tokens

    if pending.has_usage:
        if (pending.cc_5m + pending.cc_1h) != pending.cache_creation_tokens:
            diagnostics.ttl_sum_mismatch += 1
    else:
        diagnostics.turns_missing_usage += 1

    if pending.is_synthetic:
        diagnostics.synthetic_turns += 1

    turn_index = 0
    gap_s: float | None = None
    new_prev_ts = previous_non_synthetic_ts
    new_priced_count = priced_turn_count
    if not pending.is_synthetic and pending.has_usage:
        new_priced_count = priced_turn_count + 1
        turn_index = new_priced_count
        if previous_non_synthetic_ts is not None and ts_dt is not None:
            gap_s = (ts_dt - previous_non_synthetic_ts).total_seconds()
        if ts_dt is not None:
            new_prev_ts = ts_dt

    edit_kind: str | None
    if pending.edit_real_found:
        edit_kind = "real"
    elif pending.edit_scratch_found:
        edit_kind = "scratch"
    else:
        edit_kind = None

    preceding_tool, preceding_cmd_prefix = _resolve_preceding_tool(previous_turn)
    preceding_primary = events_mod.primary_kind(pending_events)

    turn = Turn(
        message_id=pending.message_id,
        request_id=pending.request_id,
        turn_index=turn_index,
        ts=pending.ts_raw,
        gap_s=gap_s,
        model=pending.model,
        service_tier=pending.service_tier,
        is_synthetic=pending.is_synthetic,
        effort=pending.effort,
        per_turn_effort=pending.per_turn_effort,
        input_tokens=pending.input_tokens,
        cache_creation_tokens=pending.cache_creation_tokens,
        cache_read_tokens=pending.cache_read_tokens,
        output_tokens=pending.output_tokens,
        thinking_tokens=pending.thinking_tokens,
        web_search_requests=pending.web_search_requests,
        web_fetch_requests=pending.web_fetch_requests,
        cc_5m=pending.cc_5m,
        cc_1h=pending.cc_1h,
        ctx=ctx,
        tool_names=tuple(pending.tool_names),
        cmd_prefix=pending.cmd_prefix,
        edit_kind=edit_kind,
        attribution_mcp_server=pending.attribution_mcp_server,
        attribution_mcp_tool=pending.attribution_mcp_tool,
        attribution_skill=pending.attribution_skill,
        preceding_tool=preceding_tool,
        preceding_cmd_prefix=preceding_cmd_prefix,
        preceding_event_kinds=tuple(event.kind for event in pending_events),
        preceding_attachment_types=tuple(pending_attachment_types),
        preceding_primary=preceding_primary,
        inference_geo=pending.inference_geo,
    )
    return turn, new_prev_ts, new_priced_count


def parse_transcript(path: str | Path, meta: TranscriptMeta) -> TranscriptResult:
    """Parse one transcript JSONL file in a single streaming pass.

    ``meta`` is provenance the caller already knows (from
    ``discovery.py``) — this function fills in ``turns``, ``events``,
    ``diagnostics``, ``tool_result_chars`` and ``tool_result_calls``
    around it; it never mutates ``meta``.
    """
    line_stats = jsonl.LineStats()
    diagnostics = Diagnostics()
    turns: list[Turn] = []
    events: list[Event] = []
    tool_result_chars: dict[str, int] = {}
    tool_result_calls: dict[str, int] = {}
    #: tool_use_id -> tool name, for attributing tool_result lengths.
    #: Deliberately not scoped to the current turn: a tool_result can
    #: reference a tool_use from an earlier turn.
    tool_use_names: dict[str, str] = {}

    pending_events: list[Event] = []
    pending_attachment_types: list[str] = []
    finalized_keys: set[str] = set()

    current: _PendingTurn | None = None
    current_key: str | None = None
    previous_turn: Turn | None = None
    previous_non_synthetic_ts: datetime | None = None
    priced_turn_count = 0

    for _line_no, d in jsonl.iter_lines(path, stats=line_stats):
        line_type = d.get("type")

        if line_type == "assistant":
            diagnostics.assistant_lines += 1
            key = _turn_key(d)
            if current is not None and key == current_key:
                _merge_into_pending(current, d, tool_use_names)
                continue
            if key in finalized_keys:
                diagnostics.late_duplicate_ids += 1
                continue
            if current is not None:
                turn, previous_non_synthetic_ts, priced_turn_count = _finalize_turn(
                    current,
                    pending_events,
                    pending_attachment_types,
                    previous_turn,
                    previous_non_synthetic_ts,
                    priced_turn_count,
                    diagnostics,
                )
                turns.append(turn)
                finalized_keys.add(current_key)  # type: ignore[arg-type]
                previous_turn = turn
                pending_events = []
                pending_attachment_types = []
            current = _new_pending(d, tool_use_names)
            current_key = key
            continue

        if line_type == "user":
            _accumulate_tool_results(d, tool_use_names, tool_result_chars, tool_result_calls)

        event = events_mod.classify_line(d)
        if event is None:
            diagnostics.ignored_line_types[line_type] = (
                diagnostics.ignored_line_types.get(line_type, 0) + 1
            )
            continue
        events.append(event)
        pending_events.append(event)
        if line_type == "attachment":
            pending_attachment_types.append(event.subkind or "")
        if event.kind == EventKind.UNKNOWN:
            diagnostics.ignored_line_types[line_type] = (
                diagnostics.ignored_line_types.get(line_type, 0) + 1
            )

    if current is not None:
        turn, previous_non_synthetic_ts, priced_turn_count = _finalize_turn(
            current,
            pending_events,
            pending_attachment_types,
            previous_turn,
            previous_non_synthetic_ts,
            priced_turn_count,
            diagnostics,
        )
        turns.append(turn)

    diagnostics.lines = line_stats.lines
    diagnostics.unparsable_lines = line_stats.unparsable_lines
    diagnostics.truncated_final_line = line_stats.truncated_final_line
    diagnostics.oversized_lines = line_stats.oversized_lines
    diagnostics.distinct_turns = len(turns)

    return TranscriptResult(
        meta=meta,
        turns=turns,
        events=events,
        diagnostics=diagnostics,
        tool_result_chars=tool_result_chars,
        tool_result_calls=tool_result_calls,
    )


__all__ = ["parse_transcript"]
