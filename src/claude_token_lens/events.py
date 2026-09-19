"""Event detection: turn one non-assistant JSONL line into an ``Event``.

Implements plan Appendix A2's detection table exactly, in the table's
order (first match wins), plus its ``PRECEDENCE`` list for resolving
``Turn.preceding_primary`` from the events collected since the previous
finalised turn.

Two members of ``model.EventKind`` are not covered by the plan's
precedence text (``LOCAL_COMMAND`` and ``META``) but are exercised by
other rows of the detection table. This module places them just above
``UNKNOWN`` at the bottom of ``PRECEDENCE`` (lowest precedence, below
``API_ERROR``) — a documented judgement call, not a plan restatement.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from .model import Event, EventKind

# -- Ignored outright but counted by the caller (returns None) ----------

#: Exact top-level ``type`` values that are ignored outright. The
#: prefix-matched families (``file-history-*``, ``artifact-*``) are
#: handled separately in ``_is_ignorable_type``.
_IGNORABLE_TYPES = frozenset(
    {
        "bridge-session",
        "last-prompt",
        "custom-title",
        "ai-title",
        "agent-name",
        "agent-setting",
        "pr-link",
        "atis-latch",
        "mode",
        "frame-link",
    }
)
_IGNORABLE_PREFIXES = ("file-history-", "artifact-")


def _is_ignorable_type(line_type: str | None) -> bool:
    if line_type in _IGNORABLE_TYPES:
        return True
    if isinstance(line_type, str):
        return line_type.startswith(_IGNORABLE_PREFIXES)
    return False


# -- Attachment-type buckets (plan A2's CACHE_SIGNAL/REMINDER/etc rows) --

_HOOK_ATTACHMENT_TYPES = frozenset(
    {
        "hook_success",
        "hook_non_blocking_error",
        "hook_blocking_error",
        "hook_system_message",
        "hook_additional_context",
        "hook_cancelled",
    }
)

_CACHE_SIGNAL_TYPES = frozenset(
    {
        "model",
        "thinking_stripped",
        "ultra_effort_enter",
        "ultra_effort_exit",
        "deferred_tools_delta",
        "deferred_tools_record",
        "mcp_instructions_delta",
        "agent_listing_delta",
        "plan_mode",
        "plan_mode_exit",
        "auto_mode",
        "auto_mode_exit",
        "output_style",
        "output_style_instructions",
    }
)

#: The subset of CACHE_SIGNAL subkinds that rank in the *high* precedence
#: band (just below MODEL_FALLBACK); every other CACHE_SIGNAL subkind
#: ranks in the *other* band (between SLASH_COMMAND and HOOK_OUTPUT).
_CACHE_SIGNAL_HIGH_SUBKINDS = frozenset(
    {"model", "thinking_stripped", "ultra_effort_enter", "ultra_effort_exit"}
)

#: attachment.type -> (added-list key, removed-list key) for the three
#: delta types whose ``Event.detail`` carries counts only (their raw
#: added-content lists, e.g. ``mcp_instructions_delta.addedBlocks``, hold
#: full instruction/skill text and must never be stored).
_DELTA_COUNT_KEYS = {
    "deferred_tools_delta": ("addedNames", "removedNames"),
    "agent_listing_delta": ("addedTypes", "removedTypes"),
    "mcp_instructions_delta": ("addedBlocks", "removedNames"),
}

_REMINDER_TYPES = frozenset(
    {
        "total_tokens_reminder",
        "batching_reminder_sent",
        "silent_turn_reminder",
        "task_reminder",
        "date",
        "date_change",
    }
)

_CONTEXT_INJECT_TYPES = frozenset(
    {
        "file",
        "edited_text_file",
        "read_truncation_notice",
        "nested_memory",
        "prompt_snapshot",
        "compact_file_reference",
        "plan_file_reference",
        "session_context",
        "environment",
        "instructions",
        "skill_listing",
        "invoked_skills",
        "directory",
        "inlined_image_paths",
        "remote_session_change",
        "workflow_keyword_request",
    }
)

_SLASH_COMMAND_PREFIXES = ("<command-name", "<local-command-stdout", "<local-command-caveat")
_SCHEDULED_TASK_PREFIXES = ("<scheduled-task", "[SYSTEM NOTIFICATION", "<<autonomous-loop")
_SCHEDULED_TASK_ORIGIN_KINDS = frozenset({"cron", "loop"})


def _user_str_content(d: dict) -> str | None:
    message = d.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def _user_has_text_or_image_list(d: dict) -> bool:
    message = d.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") in ("text", "image") for block in content
    )


def _user_has_interrupt_text_block(d: dict) -> bool:
    message = d.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.startswith("[Request interrupted"):
                return True
    return False


def _user_has_tool_result(d: dict) -> bool:
    message = d.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)


def _rendered_size_chars(d: dict) -> int | None:
    rendered = d.get("rendered")
    return len(rendered) if isinstance(rendered, str) else None


#: Matches an opening angle-bracket tag at the very start of a string,
#: e.g. ``<local-command-stdout>`` -> ``local-command-stdout``. Used only
#: to label META events with something more specific than "plain" — the
#: tag name itself is a structural marker, never message text, so it is
#: privacy-safe to store.
_LEADING_TAG_RE = re.compile(r"^<\s*/?\s*([A-Za-z][A-Za-z0-9_-]*)")


def _leading_tag_name(text: str) -> str | None:
    match = _LEADING_TAG_RE.match(text)
    return match.group(1) if match else None


#: Capture-improvements addition (A4, see model.py's ``Turn.
#: human_prompt_chars``/``human_prompt_has_paste`` docstrings): a text
#: block at or beyond this length is treated as pasted, same as the
#: ``[Pasted text`` marker Claude Code's own composer inserts.
_PASTE_CHAR_THRESHOLD = 2000
_PASTE_MARKER = "[Pasted text"


def _human_text_metrics(d: dict, str_content: str | None) -> tuple[int, bool]:
    """Chars and paste-flag for a HUMAN_TEXT line's own text content (A4):
    sums the plain string content, or every ``text`` block's length for a
    list-content line, and flags a paste when any one text block exceeds
    ``_PASTE_CHAR_THRESHOLD`` chars or contains ``_PASTE_MARKER`` — never
    retaining the text itself.
    """
    texts: list[str] = []
    if str_content is not None:
        texts.append(str_content)
    else:
        message = d.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        texts.append(text)
    total_chars = sum(len(text) for text in texts)
    has_paste = any(len(text) > _PASTE_CHAR_THRESHOLD or _PASTE_MARKER in text for text in texts)
    return total_chars, has_paste


def _delta_detail(attachment_type: str, attachment: dict) -> dict:
    keys = _DELTA_COUNT_KEYS.get(attachment_type)
    if keys is None:
        return {}
    added_key, removed_key = keys
    added = attachment.get(added_key)
    removed = attachment.get(removed_key)
    return {
        "added": len(added) if isinstance(added, list) else 0,
        "removed": len(removed) if isinstance(removed, list) else 0,
    }


def classify_line(d: dict) -> Event | None:
    """Classify one already-parsed JSONL line per plan Appendix A2.

    Returns ``None`` for assistant lines (handled separately by
    ``parse.py``, not as an Event) and for lines whose top-level ``type``
    is in the plan's "ignored outright but counted" list — the caller is
    expected to count those by ``type`` itself (``Diagnostics.
    ignored_line_types``). Every other line yields an ``Event``, ending
    with ``EventKind.UNKNOWN`` for anything the table's rows never
    matched (also meant to be counted by ``type`` — see A2's UNKNOWN row).
    """
    line_type = d.get("type")
    if line_type == "assistant":
        return None
    if _is_ignorable_type(line_type):
        return None

    ts = d.get("timestamp")
    str_content = _user_str_content(d)

    # 1. COMPACT_BOUNDARY
    if line_type == "system" and d.get("subtype") == "compact_boundary":
        meta = d.get("compactMetadata")
        meta = meta if isinstance(meta, dict) else {}
        return Event(
            kind=EventKind.COMPACT_BOUNDARY,
            subkind="compact_boundary",
            ts=ts,
            pre_tokens=meta.get("preTokens"),
            post_tokens=meta.get("postTokens"),
            dropped_tokens=meta.get("cumulativeDroppedTokens"),
            duration_ms=meta.get("durationMs"),
            trigger=meta.get("trigger"),
        )

    # 2. COMPACT_SUMMARY
    if line_type == "user" and (
        d.get("isCompactSummary")
        or (str_content is not None and str_content.startswith("This session is being continued"))
    ):
        return Event(kind=EventKind.COMPACT_SUMMARY, subkind=None, ts=ts)

    # 3. API_ERROR
    if line_type == "system" and d.get("subtype") == "api_error":
        error = d.get("error")
        detail: dict = {}
        if isinstance(error, dict) and error.get("status") is not None:
            detail["status"] = error.get("status")
        if d.get("retryAttempt") is not None:
            detail["retryAttempt"] = d.get("retryAttempt")
        return Event(kind=EventKind.API_ERROR, subkind="api_error", ts=ts, detail=detail)

    # 4. MODEL_FALLBACK
    if line_type == "system" and d.get("subtype") == "model_refusal_fallback":
        detail = {}
        if d.get("originalModel") is not None:
            detail["originalModel"] = d.get("originalModel")
        if d.get("fallbackModel") is not None:
            detail["fallbackModel"] = d.get("fallbackModel")
        return Event(
            kind=EventKind.MODEL_FALLBACK, subkind="model_refusal_fallback", ts=ts, detail=detail
        )

    # 5. LOCAL_COMMAND
    if line_type == "system" and d.get("subtype") == "local_command":
        return Event(kind=EventKind.LOCAL_COMMAND, subkind="local_command", ts=ts)

    attachment_type: str | None = None
    attachment: dict = {}
    size_chars: int | None = None
    if line_type == "attachment":
        raw_attachment = d.get("attachment")
        if isinstance(raw_attachment, dict):
            attachment = raw_attachment
            attachment_type = attachment.get("type")
        size_chars = _rendered_size_chars(d)

    # 6. HOOK_OUTPUT
    if line_type == "system" and d.get("subtype") == "stop_hook_summary":
        return Event(kind=EventKind.HOOK_OUTPUT, subkind="stop_hook_summary", ts=ts)
    if attachment_type in _HOOK_ATTACHMENT_TYPES:
        return Event(kind=EventKind.HOOK_OUTPUT, subkind=attachment_type, ts=ts, size_chars=size_chars)

    # 7. CACHE_SIGNAL
    if attachment_type in _CACHE_SIGNAL_TYPES:
        detail = {}
        if attachment_type == "model":
            identity = attachment.get("identity")
            if isinstance(identity, dict) and identity.get("modelId") is not None:
                detail["modelId"] = identity.get("modelId")
        elif attachment_type == "thinking_stripped":
            if attachment.get("scope") is not None:
                detail["scope"] = attachment.get("scope")
        elif attachment_type in _DELTA_COUNT_KEYS:
            detail = _delta_detail(attachment_type, attachment)
        return Event(
            kind=EventKind.CACHE_SIGNAL, subkind=attachment_type, ts=ts, size_chars=size_chars, detail=detail
        )

    # 8. REMINDER
    if attachment_type in _REMINDER_TYPES:
        return Event(kind=EventKind.REMINDER, subkind=attachment_type, ts=ts, size_chars=size_chars)

    # 9. CONTEXT_INJECT
    if attachment_type in _CONTEXT_INJECT_TYPES:
        detail = {}
        if attachment_type == "invoked_skills":
            names = attachment.get("names")
            # Counts only — skill names aren't in the privacy allowlist
            # and aren't needed for anything WP1 computes.
            if isinstance(names, list):
                detail["count"] = len(names)
        return Event(
            kind=EventKind.CONTEXT_INJECT, subkind=attachment_type, ts=ts, size_chars=size_chars, detail=detail
        )

    # 10. QUEUE_OPERATION
    if line_type == "queue-operation":
        return Event(kind=EventKind.QUEUE_OPERATION, subkind=d.get("operation"), ts=ts)
    if attachment_type == "queued_command":
        return Event(kind=EventKind.QUEUE_OPERATION, subkind=attachment_type, ts=ts, size_chars=size_chars)

    # 11. ATTACHMENT (catch-all for any attachment type not listed above)
    if line_type == "attachment":
        return Event(kind=EventKind.ATTACHMENT, subkind=attachment_type, ts=ts, size_chars=size_chars)

    # 12. TOOL_DENIAL
    tool_denial_kind = d.get("toolDenialKind")
    if line_type == "user" and tool_denial_kind:
        return Event(kind=EventKind.TOOL_DENIAL, subkind=tool_denial_kind, ts=ts)

    # 13. TOOL_RESULT
    if line_type == "user" and _user_has_tool_result(d):
        return Event(kind=EventKind.TOOL_RESULT, subkind=None, ts=ts)

    origin = d.get("origin")
    origin = origin if isinstance(origin, dict) else None
    origin_kind = origin.get("kind") if origin else None

    # 14. TASK_NOTIFICATION
    if line_type == "user" and origin_kind == "task-notification":
        return Event(kind=EventKind.TASK_NOTIFICATION, subkind=None, ts=ts)
    if str_content is not None and str_content.startswith("<task-notification"):
        return Event(kind=EventKind.TASK_NOTIFICATION, subkind=None, ts=ts)

    # 15. PEER_MESSAGE
    if line_type == "user" and origin_kind == "peer":
        return Event(kind=EventKind.PEER_MESSAGE, subkind=None, ts=ts)

    # 16. META (checked after the four user-line kinds above so a meta
    # line that also happens to carry a tool result/denial/task-notification/
    # peer origin is classified as that more specific kind instead).
    if line_type == "user" and d.get("isMeta"):
        if origin_kind is not None:
            meta_subkind = origin_kind
        else:
            tag = _leading_tag_name(str_content) if str_content is not None else None
            meta_subkind = tag if tag is not None else "plain"
        return Event(kind=EventKind.META, subkind=meta_subkind, ts=ts)

    # 17. SLASH_COMMAND
    if str_content is not None and str_content.startswith(_SLASH_COMMAND_PREFIXES):
        return Event(kind=EventKind.SLASH_COMMAND, subkind=None, ts=ts)

    # 18. SCHEDULED_TASK
    if str_content is not None and str_content.startswith(_SCHEDULED_TASK_PREFIXES):
        return Event(kind=EventKind.SCHEDULED_TASK, subkind=None, ts=ts)
    if origin_kind in _SCHEDULED_TASK_ORIGIN_KINDS:
        return Event(kind=EventKind.SCHEDULED_TASK, subkind=None, ts=ts)

    # 19. INTERRUPT
    if str_content is not None and str_content.startswith("[Request interrupted"):
        return Event(kind=EventKind.INTERRUPT, subkind=None, ts=ts)
    if line_type == "user" and _user_has_interrupt_text_block(d):
        return Event(kind=EventKind.INTERRUPT, subkind=None, ts=ts)

    # 20. HUMAN_TEXT
    if line_type == "user" and (
        origin_kind == "human" or d.get("promptSource") is not None or d.get("permissionMode") is not None
    ):
        human_chars, has_paste = _human_text_metrics(d, str_content)
        return Event(
            kind=EventKind.HUMAN_TEXT, subkind=None, ts=ts, size_chars=human_chars, detail={"has_paste": has_paste}
        )
    if line_type == "user" and (str_content is not None or _user_has_text_or_image_list(d)):
        human_chars, has_paste = _human_text_metrics(d, str_content)
        return Event(
            kind=EventKind.HUMAN_TEXT, subkind=None, ts=ts, size_chars=human_chars, detail={"has_paste": has_paste}
        )

    # 21. UNKNOWN
    return Event(kind=EventKind.UNKNOWN, subkind=None, ts=ts)


# -- Precedence -----------------------------------------------------------

#: plan A2's precedence order for ``preceding_primary``, highest first.
#: CACHE_SIGNAL appears twice because the plan splits it into two bands;
#: ``_PRECEDENCE_SUBKIND_FILTER`` (same length, positionally aligned)
#: restricts each occurrence to the subkinds that belong in that band.
PRECEDENCE: tuple[EventKind, ...] = (
    EventKind.COMPACT_BOUNDARY,
    EventKind.COMPACT_SUMMARY,
    EventKind.MODEL_FALLBACK,
    EventKind.CACHE_SIGNAL,  # high band: model, thinking_stripped, ultra_effort_*
    EventKind.INTERRUPT,
    EventKind.HUMAN_TEXT,
    EventKind.PEER_MESSAGE,
    EventKind.TASK_NOTIFICATION,
    EventKind.SCHEDULED_TASK,
    EventKind.SLASH_COMMAND,
    EventKind.LOCAL_COMMAND,  # not ranked by the plan; placed here (command-adjacent)
    EventKind.CACHE_SIGNAL,  # other band: every other CACHE_SIGNAL subkind
    EventKind.HOOK_OUTPUT,
    EventKind.QUEUE_OPERATION,
    EventKind.CONTEXT_INJECT,
    EventKind.REMINDER,
    EventKind.TOOL_DENIAL,
    EventKind.ATTACHMENT,
    EventKind.TOOL_RESULT,
    EventKind.API_ERROR,
    EventKind.META,  # not ranked by the plan; placed here, above UNKNOWN
    EventKind.UNKNOWN,
)

_PRECEDENCE_SUBKIND_FILTER: tuple[frozenset[str] | None, ...] = (
    None,
    None,
    None,
    _CACHE_SIGNAL_HIGH_SUBKINDS,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,  # other band: matches any CACHE_SIGNAL the high-band entry didn't
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
)


def _rank(event: Event) -> int:
    for index, kind in enumerate(PRECEDENCE):
        if kind != event.kind:
            continue
        subkind_filter = _PRECEDENCE_SUBKIND_FILTER[index]
        if subkind_filter is None or event.subkind in subkind_filter:
            return index
    return len(PRECEDENCE)


def primary_kind(events: Iterable[Event] | Sequence[Event]) -> EventKind:
    """Resolve the single highest-precedence kind among ``events`` per
    plan A2's ``PRECEDENCE`` list. Returns ``EventKind.UNKNOWN`` for an
    empty sequence — the same value ``Turn.preceding_primary`` defaults
    to, standing in for the table's "none".
    """
    best: Event | None = None
    best_rank = len(PRECEDENCE) + 1
    for event in events:
        rank = _rank(event)
        if rank < best_rank:
            best_rank = rank
            best = event
    return best.kind if best is not None else EventKind.UNKNOWN


__all__ = ["classify_line", "PRECEDENCE", "primary_kind"]
