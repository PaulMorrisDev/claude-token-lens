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

Usage-limits batch (v3-limits, see model.py's module docstring for the
new ``EventKind``/``Turn``/``Diagnostics`` fields this adds): three new
rows in the detection table, inserted ahead of the more generic row they
would otherwise fall into --

- ``AGENT_TERMINATED`` is checked before ``TASK_NOTIFICATION`` (13.5):
  the "Agent terminated early due to ..." lines are shaped exactly like
  a task-notification (same ``origin.kind``/``<task-notification`` tag)
  but report a more specific harness event.
- ``LIMIT_RESUME`` is checked before ``HUMAN_TEXT`` (19.5): the desktop
  app's automatic resume ping is a human-origin, ``promptSource: "sdk"``
  line, which would otherwise match the generic HUMAN_TEXT row.
- ``LIMIT_HIT`` is *not* produced by :func:`classify_line` at all --
  the "You've hit your session/weekly limit" text lives on a synthetic
  *assistant* line (``model: "<synthetic>"``), and assistant lines are
  never events (see this function's docstring). ``parse.py`` classifies
  that text via :func:`classify_synthetic_text` and synthesises the
  ``LIMIT_HIT`` event itself once it knows the turn is synthetic.
  :func:`classify_synthetic_text` and :func:`parse_limit_reset_clause`
  live here anyway, alongside every other piece of text-shape knowledge.

Both new precedence entries (``LIMIT_HIT``, ``LIMIT_RESUME``) rank above
``INTERRUPT`` in ``PRECEDENCE`` per the v3-limits brief: a usage-cap
pause is a stronger explanation for a gap than a plain interrupt.
``AGENT_TERMINATED`` ranks just above ``TASK_NOTIFICATION`` (a judgement
call, not part of the brief's explicit precedence list — a terminated
subagent is a more specific/important signal than a generic
notification, but not as strong as an interrupt or a human message).
"""

from __future__ import annotations

import json
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


#: attachment.type -> the raw attachment fields holding the text the
#: model is shown, used when a line carries no ``rendered`` field (e.g.
#: ~15% of real ``skill_listing`` lines). Lengths only -- never stored.
_CONTENT_SIZE_FIELDS = {
    "skill_listing": ("content",),
    "deferred_tools_delta": ("addedLines",),
    "mcp_instructions_delta": ("addedBlocks",),
    "agent_listing_delta": ("addedLines",),
    "hook_additional_context": ("content",),
    "hook_system_message": ("content",),
    "hook_success": ("content",),
    "total_tokens_reminder": ("text",),
    "batching_reminder_sent": ("text",),
    "silent_turn_reminder": ("text",),
    "model": ("text",),
    "queued_command": ("prompt",),
    "edited_text_file": ("snippet",),
    "plan_file_reference": ("planContent",),
    "directory": ("content",),
}


def _text_chars(value: object) -> int:
    """Summed length of a string, or of every string in a list."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(len(item) for item in value if isinstance(item, str))
    return 0


def _attachment_content_chars(attachment: dict) -> int | None:
    """Size of an attachment measured from its own content fields, for a
    line with no ``rendered`` field. ``None`` for a type with no known
    content field."""
    attachment_type = attachment.get("type")
    if attachment_type == "instructions":
        files = attachment.get("files")
        if not isinstance(files, list):
            return None
        return sum(_text_chars(f.get("content")) for f in files if isinstance(f, dict))
    if attachment_type == "nested_memory":
        content = attachment.get("content")
        return _text_chars(content.get("content")) if isinstance(content, dict) else None
    fields = _CONTENT_SIZE_FIELDS.get(attachment_type)
    if fields is None:
        return None
    present = [attachment[field] for field in fields if field in attachment]
    if not present:
        return None
    return sum(_text_chars(value) for value in present)


def _rendered_size_chars(d: dict, attachment: dict) -> int | None:
    """Length of the text an attachment line puts in front of the model.

    Real transcripts carry it as a top-level ``rendered`` list of
    ``{"content": str}`` blocks (a bare string is accepted too, for older
    lines). Without ``rendered``, falls back to the attachment's own
    content fields (:func:`_attachment_content_chars`).
    """
    rendered = d.get("rendered")
    if isinstance(rendered, str):
        return len(rendered)
    if isinstance(rendered, list):
        blocks = [block.get("content") for block in rendered if isinstance(block, dict)]
        texts = [text for text in blocks if isinstance(text, str)]
        if texts:
            return sum(len(text) for text in texts)
    return _attachment_content_chars(attachment)


#: ``instructions.files[].type`` values kept as ``Event.detail`` keys
#: (a fixed label set, never a path) so startup context can be split by
#: where each instruction file comes from.
_INSTRUCTION_FILE_TYPES = frozenset({"User", "Project", "Local", "AutoMem", "Managed"})


def _path_hash(path: object) -> str | None:
    """Salted hash of an instruction file's path (``parse._read_target_hash``),
    so a file can be matched to the same file on disk later without the
    path itself being stored. ``None`` with no salt set or no path."""
    if not isinstance(path, str) or not path:
        return None
    from . import parse  # parse imports this module; resolved at call time

    return parse._read_target_hash(path)


def _instruction_file_record(entry: dict) -> dict:
    """One instruction file as ``{"hash", "type", "scoped", "chars"}``:
    a salted path hash, the type label, whether it loads only for
    matching paths (it has ``globs``), and its size. Never its text or
    path."""
    file_type = entry.get("type")
    record: dict = {
        "type": file_type if file_type in _INSTRUCTION_FILE_TYPES else "Other",
        "scoped": bool(entry.get("globs")),
        "chars": _text_chars(entry.get("content")),
    }
    hashed = _path_hash(entry.get("path"))
    if hashed is not None:
        record["hash"] = hashed
    return record


def _instructions_detail(attachment: dict) -> dict:
    """Chars per instruction-file type (``User``/``Project``/...), a file
    count, and one record per file (:func:`_instruction_file_record`).
    Unknown types are summed under ``Other``."""
    files = attachment.get("files")
    if not isinstance(files, list):
        return {}
    by_type: dict[str, int] = {}
    records: list[dict] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        record = _instruction_file_record(entry)
        records.append(record)
        by_type[record["type"]] = by_type.get(record["type"], 0) + record["chars"]
    return {"count": len(records), "chars_by_type": by_type, "files": records}


def _nested_memory_detail(attachment: dict) -> dict:
    """The one file a ``nested_memory`` attachment loads (a subfolder
    CLAUDE.md or a ``.claude/rules`` file), as a single file record."""
    content = attachment.get("content")
    if not isinstance(content, dict):
        return {}
    return {"files": [_instruction_file_record(content)]}


#: One ``- name: description`` line of a ``skill_listing``'s content.
_SKILL_LINE_RE = re.compile(r"^- ([^\s:][^\n]*?): ", re.MULTILINE)


def _skill_listing_detail(attachment: dict) -> dict:
    """Skill count, plus each listed skill's name and the characters its
    listing line takes. Names are kept (they are labels, like
    ``Turn.attribution_skill``); descriptions never are. A name is kept
    only when it is also in the attachment's own ``names`` list."""
    detail: dict = {}
    if isinstance(attachment.get("skillCount"), int):
        detail["count"] = attachment["skillCount"]
    content = attachment.get("content")
    names = attachment.get("names")
    if not isinstance(content, str) or not isinstance(names, list):
        return detail
    known = {name for name in names if isinstance(name, str)}
    starts = [(match.start(), match.group(1)) for match in _SKILL_LINE_RE.finditer(content)]
    skills: list[dict] = []
    for index, (start, name) in enumerate(starts):
        if name not in known:
            continue
        end = starts[index + 1][0] if index + 1 < len(starts) else len(content)
        skills.append({"name": name, "chars": len(content[start:end].rstrip("\n"))})
    if skills:
        detail["skills"] = skills
    return detail


def _invoked_skills_detail(attachment: dict) -> dict:
    """Skill count, plus each re-sent skill's name and the characters of
    its instructions. ``invoked_skills`` re-sends the full text of skills
    already used, e.g. after a conversation summary."""
    skills = attachment.get("skills")
    names = attachment.get("names")
    detail: dict = {}
    if isinstance(names, list):
        detail["count"] = len(names)
    if isinstance(skills, list):
        records = [
            {"name": skill["name"], "chars": _text_chars(skill.get("content"))}
            for skill in skills
            if isinstance(skill, dict) and isinstance(skill.get("name"), str)
        ]
        detail["count"] = len(records)
        if records:
            detail["skills"] = records
    return detail


def _prompt_snapshot_detail(attachment: dict) -> dict:
    """Sizes of the system prompt and tool definitions a ``prompt_snapshot``
    records. The snapshot is not itself sent as a message, so these sit in
    ``Event.detail`` rather than ``Event.size_chars`` (which feeds the
    injected-attachment totals)."""
    detail: dict = {"system_chars": _text_chars(attachment.get("systemPrompt"))}
    tools = attachment.get("tools")
    if isinstance(tools, list) and tools:
        detail["tool_count"] = len(tools)
        detail["tools_chars"] = sum(
            len(json.dumps(tool, separators=(",", ":"), ensure_ascii=False)) for tool in tools if isinstance(tool, dict)
        )
    return detail


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


#: Quality-signals addition: phrases that mark a message as correcting
#: Claude ("that's wrong", "still broken", "why did you", "undo that").
#: Matched in the first :data:`_CORRECTION_SCAN_CHARS` characters only,
#: and only the resulting yes/no is kept -- never the text. A bare "no"
#: is deliberately not a match: "no, go ahead" is as common as a
#: correction.
_CORRECTION_RE = re.compile(
    r"\b(?:"
    r"that'?s (?:wrong|not right|not what|incorrect|broken)"
    r"|th(?:is|at) (?:is|was) (?:wrong|broken|incorrect|not (?:right|working|what))"
    r"|it'?s (?:still )?(?:broken|wrong|not working|failing|incorrect)"
    r"|(?:still|it still) (?:broken|failing|wrong|not working|doesn'?t work|fails)"
    r"|(?:doesn'?t|does not|didn'?t|did not) work"
    r"|not what (?:i|we) (?:asked|wanted|meant|said)"
    r"|you (?:broke|missed|forgot|ignored|didn'?t (?:do|read|follow|check|run|fix))"
    r"|why (?:did|didn'?t|would|are|is) you"
    r"|(?:undo|revert|roll back) (?:that|this|it|the|your)"
    r"|that broke|you'?ve broken|try again|redo (?:it|that|this)"
    r"|wrong (?:file|approach|answer|place|branch|one)"
    r")\b",
    re.IGNORECASE,
)
_CORRECTION_SCAN_CHARS = 200


def _looks_like_correction(texts: list[str]) -> bool:
    return any(_CORRECTION_RE.search(text[:_CORRECTION_SCAN_CHARS]) for text in texts if text)


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


def _human_text_detail(d: dict, str_content: str | None) -> tuple[int, dict]:
    """Size and the detail flags for a HUMAN_TEXT line: ``has_paste``
    and (quality signals) ``correction``, whether the message looks like
    it corrects Claude. Flags only -- never the text."""
    human_chars, has_paste = _human_text_metrics(d, str_content)
    if str_content is not None:
        texts = [str_content]
    else:
        message = d.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        texts = [
            block.get("text")
            for block in (content if isinstance(content, list) else ())
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        ]
    return human_chars, {"has_paste": has_paste, "correction": _looks_like_correction(texts)}


#: Usage-limits addition (see module docstring): the six known synthetic
#: assistant texts, matched by ordered startswith/substring checks
#: verified against the real corpus. Never stored -- only the resulting
#: enum-like label survives onto ``Turn.synthetic_kind``.
_SESSION_LIMIT_PREFIX = "You've hit your session limit"
_WEEKLY_LIMIT_PREFIX = "You've hit your weekly limit"
_OVERLOADED_PREFIX = "API Error: 529"
_AUTOCOMPACT_THRASH_PREFIX = "Autocompact is thrashing"


def classify_synthetic_text(text: str | None) -> str:
    """Classify a synthetic assistant line's own text into one of six
    enum values: ``session_limit``, ``weekly_limit``, ``overloaded``,
    ``unsupported_model``, ``autocompact_thrash``, or (anything else,
    including no text at all) ``other_api_error``.
    """
    if not isinstance(text, str):
        return "other_api_error"
    if text.startswith(_SESSION_LIMIT_PREFIX):
        return "session_limit"
    if text.startswith(_WEEKLY_LIMIT_PREFIX):
        return "weekly_limit"
    if text.startswith(_OVERLOADED_PREFIX):
        return "overloaded"
    if text.startswith("API Error:") and "does not support" in text:
        return "unsupported_model"
    if text.startswith(_AUTOCOMPACT_THRASH_PREFIX):
        return "autocompact_thrash"
    return "other_api_error"


#: Usage-limits addition (see module docstring): the "resets H[:MM]am|pm
#: (IANA tz)" clause trailing a session/weekly-limit synthetic text.
_LIMIT_RESET_RE = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)", re.IGNORECASE)
#: Single-slash IANA zone names only (e.g. ``Europe/London``) -- a
#: multi-part name like ``America/Argentina/Buenos_Aires`` is deliberately
#: excluded rather than guessed at (see model.py's module docstring).
_IANA_TZ_RE = re.compile(r"^[A-Za-z_]+/[A-Za-z_]+$")


def parse_limit_reset_clause(text: str | None) -> tuple[int | None, str | None]:
    """Parse a synthetic limit-hit line's trailing "resets ..." clause
    into ``(reset_minutes_of_day, reset_tz)``. ``reset_tz`` is ``None``
    unless the parenthesised zone name matches the single-slash IANA
    form. Returns ``(None, None)`` when ``text`` doesn't carry a
    matching clause at all.
    """
    if not isinstance(text, str):
        return None, None
    match = _LIMIT_RESET_RE.search(text)
    if not match:
        return None, None
    hour_raw, minute_raw, meridiem, tz_raw = match.groups()
    try:
        hour = int(hour_raw)
        minute = int(minute_raw) if minute_raw else 0
    except ValueError:
        return None, None
    if not (1 <= hour <= 12) or not (0 <= minute <= 59):
        return None, None
    hour24 = hour % 12
    if meridiem.lower() == "pm":
        hour24 += 12
    minutes_of_day = hour24 * 60 + minute
    tz = tz_raw if _IANA_TZ_RE.match(tz_raw) else None
    return minutes_of_day, tz


#: Usage-limits addition (see module docstring): the desktop app's
#: automatic resume ping after a usage-limit pause.
_LIMIT_RESUME_PREFIX = "I hit my usage limit while you were working, but it has reset now"

#: Usage-limits addition (see module docstring): a subagent killed
#: mid-task by the harness, and the structured "error type X" clause
#: used to tell a usage-limit kill apart from any other reason.
_TERMINATED_EARLY_MARKER = "terminated early due to"
_ERROR_TYPE_RE = re.compile(r"error type ([a-zA-Z_]+)")


#: Quality-signals addition: a task notification's own ``<task-id>`` and
#: ``<status>`` tags (``completed``/``failed``/``stopped`` observed). The
#: id is the agent's id for a background agent (its transcript is
#: ``agent-<id>.jsonl``) or a background shell task's id.
_TASK_ID_RE = re.compile(r"<task-id>([A-Za-z0-9_-]{1,64})</task-id>")
_TASK_STATUS_RE = re.compile(r"<status>([a-z_]{1,24})</status>")


def _task_notification_detail(text: str | None) -> dict:
    if not text or "<task-id>" not in text:
        return {}
    detail = {}
    task_id = _TASK_ID_RE.search(text)
    status = _TASK_STATUS_RE.search(text)
    if task_id:
        detail["task_id"] = task_id.group(1)
    if status:
        detail["status"] = status.group(1)
    return detail


def _agent_results_detail(d: dict) -> dict:
    """Quality-signals addition: a synchronous agent's result
    (``toolUseResult`` with an ``agentId`` and a final ``status``) as
    ``{"agents": [[agent_id, status]]}``; ``{}`` for any other tool
    result, including a background launch (``async_launched``)."""
    result = d.get("toolUseResult")
    if not isinstance(result, dict):
        return {}
    agent_id, status = result.get("agentId"), result.get("status")
    if not isinstance(agent_id, str) or not isinstance(status, str) or result.get("isAsync"):
        return {}
    if status == "async_launched" or not _TASK_ID_RE.fullmatch(f"<task-id>{agent_id}</task-id>"):
        return {}
    return {"agents": [[agent_id, status[:24]]]}


def _agent_terminated_subkind(text: str) -> str:
    match = _ERROR_TYPE_RE.search(text)
    if match and match.group(1).lower() == "rate_limit":
        return "rate_limit"
    return "other"


def _first_user_text(d: dict, str_content: str | None) -> str | None:
    """The first text a ``type=user`` line carries, for structural
    prefix/substring matching only -- never stored on any ``Event`` (see
    SECURITY.md). Handles both a plain-string ``message.content`` and a
    list of content blocks (mixed shapes observed in the real corpus for
    task-notification and resume-prompt lines, unlike the always
    list-shaped ``isApiErrorMessage`` lines).
    """
    if str_content is not None:
        return str_content
    message = d.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    return text
    return None


def _delta_detail(attachment_type: str, attachment: dict) -> dict:
    keys = _DELTA_COUNT_KEYS.get(attachment_type)
    if keys is None:
        return {}
    added_key, removed_key = keys
    added = attachment.get(added_key)
    removed = attachment.get(removed_key)
    detail = {
        "added": len(added) if isinstance(added, list) else 0,
        "removed": len(removed) if isinstance(removed, list) else 0,
    }
    if attachment_type == "deferred_tools_delta" and isinstance(added, list):
        # How many of the added deferred tools come from MCP servers
        # (``mcp__<server>__<tool>`` names) -- a count, never the names.
        detail["mcp_added"] = sum(1 for name in added if isinstance(name, str) and name.startswith("mcp__"))
    return detail


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
    origin = d.get("origin")
    origin = origin if isinstance(origin, dict) else None
    origin_kind = origin.get("kind") if origin else None

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
        # Usage-limits addition (see module docstring): retryInMs/source
        # alongside the existing status/retryAttempt.
        if d.get("retryInMs") is not None:
            detail["retryInMs"] = d.get("retryInMs")
        if d.get("source") is not None:
            detail["source"] = d.get("source")
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
        size_chars = _rendered_size_chars(d, attachment)

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
            detail = _invoked_skills_detail(attachment)
        elif attachment_type == "skill_listing":
            detail = _skill_listing_detail(attachment)
        elif attachment_type == "instructions":
            detail = _instructions_detail(attachment)
        elif attachment_type == "nested_memory":
            detail = _nested_memory_detail(attachment)
        elif attachment_type == "prompt_snapshot":
            detail = _prompt_snapshot_detail(attachment)
            size_chars = None
        return Event(
            kind=EventKind.CONTEXT_INJECT, subkind=attachment_type, ts=ts, size_chars=size_chars, detail=detail
        )

    # 10. QUEUE_OPERATION
    # A task notification that arrives while Claude is mid-reply is
    # queued (and then attached) rather than sent as a user line, so its
    # task id and status are read here too (quality signals).
    if line_type == "queue-operation":
        content = d.get("content")
        return Event(
            kind=EventKind.QUEUE_OPERATION,
            subkind=d.get("operation"),
            ts=ts,
            detail=_task_notification_detail(content if isinstance(content, str) else None),
        )
    if attachment_type == "queued_command":
        prompt = attachment.get("prompt") if isinstance(attachment, dict) else None
        return Event(
            kind=EventKind.QUEUE_OPERATION,
            subkind=attachment_type,
            ts=ts,
            size_chars=size_chars,
            detail=_task_notification_detail(prompt if isinstance(prompt, str) else None),
        )

    # 11. ATTACHMENT (catch-all for any attachment type not listed above)
    if line_type == "attachment":
        return Event(kind=EventKind.ATTACHMENT, subkind=attachment_type, ts=ts, size_chars=size_chars)

    # 12. TOOL_DENIAL
    tool_denial_kind = d.get("toolDenialKind")
    if line_type == "user" and tool_denial_kind:
        return Event(kind=EventKind.TOOL_DENIAL, subkind=tool_denial_kind, ts=ts)

    # 13. TOOL_RESULT
    if line_type == "user" and _user_has_tool_result(d):
        return Event(kind=EventKind.TOOL_RESULT, subkind=None, ts=ts, detail=_agent_results_detail(d))

    is_task_notification_line = line_type == "user" and (
        origin_kind == "task-notification" or (str_content is not None and str_content.startswith("<task-notification"))
    )

    # 13.5. AGENT_TERMINATED (usage-limits addition, see module docstring:
    # checked before TASK_NOTIFICATION so a terminated-early notification
    # is classified as this more specific kind instead of the generic one).
    if is_task_notification_line:
        text = _first_user_text(d, str_content)
        if text is not None and _TERMINATED_EARLY_MARKER in text:
            return Event(
                kind=EventKind.AGENT_TERMINATED,
                subkind=_agent_terminated_subkind(text),
                ts=ts,
                detail=_task_notification_detail(text),
            )

    # 14. TASK_NOTIFICATION
    if is_task_notification_line:
        return Event(
            kind=EventKind.TASK_NOTIFICATION,
            subkind=None,
            ts=ts,
            detail=_task_notification_detail(_first_user_text(d, str_content)),
        )

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

    # 19.5. LIMIT_RESUME (usage-limits addition, see module docstring:
    # checked before HUMAN_TEXT so the desktop app's automatic resume
    # ping -- a human-origin, promptSource:"sdk" line -- is classified as
    # this more specific kind instead of the generic one. Ignores the
    # queue-operation/last-prompt variants of similar text: those are a
    # different top-level ``type``, already filtered out at the top of
    # this function.)
    if line_type == "user" and d.get("promptSource") == "sdk" and origin_kind == "human":
        text = _first_user_text(d, str_content)
        if text is not None and text.startswith(_LIMIT_RESUME_PREFIX):
            return Event(kind=EventKind.LIMIT_RESUME, subkind=None, ts=ts)

    # 20. HUMAN_TEXT
    if line_type == "user" and (
        origin_kind == "human" or d.get("promptSource") is not None or d.get("permissionMode") is not None
    ):
        human_chars, detail = _human_text_detail(d, str_content)
        return Event(kind=EventKind.HUMAN_TEXT, subkind=None, ts=ts, size_chars=human_chars, detail=detail)
    if line_type == "user" and (str_content is not None or _user_has_text_or_image_list(d)):
        human_chars, detail = _human_text_detail(d, str_content)
        return Event(kind=EventKind.HUMAN_TEXT, subkind=None, ts=ts, size_chars=human_chars, detail=detail)

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
    # Usage-limits addition (see module docstring): ranked above INTERRUPT
    # per the v3-limits brief -- a usage-cap pause outranks a plain
    # interrupt as the explanation for a gap.
    EventKind.LIMIT_HIT,
    EventKind.LIMIT_RESUME,
    EventKind.INTERRUPT,
    EventKind.HUMAN_TEXT,
    EventKind.PEER_MESSAGE,
    # Usage-limits addition: not ranked by the plan (it predates this
    # kind); placed just above TASK_NOTIFICATION -- a documented
    # judgement call, see module docstring.
    EventKind.AGENT_TERMINATED,
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
    None,  # LIMIT_HIT
    None,  # LIMIT_RESUME
    None,
    None,
    None,
    None,  # AGENT_TERMINATED
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


__all__ = [
    "classify_line",
    "PRECEDENCE",
    "primary_kind",
    "classify_synthetic_text",
    "parse_limit_reset_clause",
]
