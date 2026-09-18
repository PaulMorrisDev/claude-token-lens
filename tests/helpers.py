"""Test-only builders for synthetic Claude Code JSONL fixtures.

Not a package under test itself — imported directly by test modules that
need realistic-shaped transcript lines without depending on the parser
(which doesn't exist yet in WP0).
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import re
from pathlib import Path
from typing import Any

_counter = itertools.count(1)


def turn_line(**overrides: Any) -> dict:
    """Build one assistant JSONL line with a realistic shape:
    ``type``, ``message.{id, model, usage{input_tokens,
    cache_creation_input_tokens, cache_read_input_tokens, output_tokens,
    cache_creation{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}},
    content}``, ``requestId``, ``uuid``, ``timestamp``.

    Convenience kwargs (all optional) flatten the common fields callers
    want to vary: ``message_id``, ``model``, ``input_tokens``,
    ``cache_creation_input_tokens``, ``cache_read_input_tokens``,
    ``output_tokens``, ``ephemeral_5m_input_tokens``,
    ``ephemeral_1h_input_tokens``, ``content``, ``request_id``, ``uuid``,
    ``timestamp``. Any other keyword is merged into the top-level line
    dict as-is (e.g. ``isApiErrorMessage=True``), which also lets a
    caller override ``type`` or replace ``message`` wholesale.
    """
    n = next(_counter)

    message_id = overrides.pop("message_id", f"msg_{n:06d}")
    model = overrides.pop("model", "claude-sonnet-5")
    input_tokens = overrides.pop("input_tokens", 100)
    cache_creation_input_tokens = overrides.pop("cache_creation_input_tokens", 0)
    cache_read_input_tokens = overrides.pop("cache_read_input_tokens", 0)
    output_tokens = overrides.pop("output_tokens", 50)
    ephemeral_5m_input_tokens = overrides.pop("ephemeral_5m_input_tokens", 0)
    ephemeral_1h_input_tokens = overrides.pop("ephemeral_1h_input_tokens", 0)
    content = overrides.pop("content", [{"type": "text", "text": "ok"}])
    request_id = overrides.pop("request_id", f"req_{n:06d}")
    uuid = overrides.pop("uuid", f"uuid_{n:06d}")
    timestamp = overrides.pop("timestamp", "2026-09-18T12:00:00.000Z")

    line: dict[str, Any] = {
        "type": "assistant",
        "message": {
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
                "cache_read_input_tokens": cache_read_input_tokens,
                "output_tokens": output_tokens,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": ephemeral_5m_input_tokens,
                    "ephemeral_1h_input_tokens": ephemeral_1h_input_tokens,
                },
            },
            "content": content,
        },
        "requestId": request_id,
        "uuid": uuid,
        "timestamp": timestamp,
    }
    line.update(overrides)
    return line


def write_jsonl(path: Path, dicts: list[dict]) -> None:
    """Write an iterable of dicts to ``path`` as newline-delimited JSON."""
    with open(path, "w", encoding="utf-8") as fh:
        for d in dicts:
            fh.write(json.dumps(d))
            fh.write("\n")


# -- WP1 additions: non-assistant line builders --------------------------
#
# ``turn_line``/``write_jsonl`` above are WP0. WP1 (jsonl.py, events.py,
# parse.py, discovery.py) needs realistic-shaped non-assistant lines too,
# for every EventKind/subtype in plan Appendix A2. Same convention:
# convenience kwargs for the common fields, ``**overrides`` merges into
# the top-level dict as-is for anything else.


def _base_line(line_type: str, **overrides: Any) -> dict:
    n = next(_counter)
    timestamp = overrides.pop("timestamp", "2026-09-18T12:00:00.000Z")
    uuid = overrides.pop("uuid", f"uuid_{n:06d}")
    line: dict[str, Any] = {
        "type": line_type,
        "timestamp": timestamp,
        "uuid": uuid,
        "sessionId": "session_test",
    }
    line.update(overrides)
    return line


def user_str_line(content: str, **overrides: Any) -> dict:
    """Build a ``type=user`` line whose ``message.content`` is a plain
    string (the shape used by compaction summaries, slash commands,
    scheduled-task/task-notification/interrupt markers, and genuine human
    text prompts).
    """
    message = overrides.pop("message", None)
    if message is None:
        message = {"role": "user", "content": content}
    line = _base_line("user", **overrides)
    line["message"] = message
    return line


def user_block_line(content: list[dict], **overrides: Any) -> dict:
    """Build a ``type=user`` line whose ``message.content`` is a block
    list (tool_result / text / image blocks)."""
    message = overrides.pop("message", None)
    if message is None:
        message = {"role": "user", "content": content}
    line = _base_line("user", **overrides)
    line["message"] = message
    return line


def tool_result_block(tool_use_id: str, content: str | list[dict], **overrides: Any) -> dict:
    """Build one ``tool_result`` content block for ``user_block_line``."""
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    block.update(overrides)
    return block


def tool_use_block(name: str, tool_use_id: str, input: dict | None = None, **overrides: Any) -> dict:
    """Build one ``tool_use`` content block for ``turn_line(content=...)``."""
    block = {"type": "tool_use", "id": tool_use_id, "name": name, "input": input or {}}
    block.update(overrides)
    return block


def system_line(subtype: str, **overrides: Any) -> dict:
    """Build a ``type=system`` line (compact_boundary, api_error,
    model_refusal_fallback, local_command, stop_hook_summary)."""
    line = _base_line("system", **overrides)
    line["subtype"] = subtype
    return line


def attachment_line(attachment_type: str, rendered: str | None = None, **attachment_overrides: Any) -> dict:
    """Build a ``type=attachment`` line with ``attachment.type`` set and
    the rest of ``attachment_overrides`` merged into the nested
    ``attachment`` dict (e.g. ``addedNames=[...]`` for a delta type).
    ``rendered``, when given, becomes the top-level ``rendered`` field
    ``Event.size_chars`` is measured from.
    """
    line = _base_line("attachment")
    attachment = {"type": attachment_type}
    attachment.update(attachment_overrides)
    line["attachment"] = attachment
    if rendered is not None:
        line["rendered"] = rendered
    return line


def queue_operation_line(operation: str, **overrides: Any) -> dict:
    """Build a ``type=queue-operation`` line."""
    line = _base_line("queue-operation", **overrides)
    line["operation"] = operation
    return line


def ignorable_line(line_type: str, **overrides: Any) -> dict:
    """Build a line whose top-level ``type`` is one of the plan's
    "ignored outright but counted" values (e.g. ``bridge-session``)."""
    return _base_line(line_type, **overrides)


# -- Independent-review follow-up: reusable privacy regex scan -----------
#
# Task 6's redaction fix (parse.py's ``_redact_paths``) removes absolute-
# path-shaped tokens from ``cmd_prefix``/``preceding_cmd_prefix``, but
# the privacy criterion is broader than that one field: no dataclass
# field anywhere in a ``TranscriptResult`` should ever match a drive
# letter, a POSIX home path, a Windows ``\Users\`` path, an MSYS/Git Bash
# drive path (``/c/...``), or a bare "@". ``assert_privacy`` is the
# reusable scan for that, on top of test_privacy.py's existing
# length-based walk.

_PRIVACY_DRIVE_RE = re.compile(r"[A-Za-z]:\\")
_PRIVACY_POSIX_HOME_RE = re.compile(r"/home/")
_PRIVACY_WIN_USERS_RE = re.compile(r"\\Users\\")
#: MSYS/Git Bash drive form, e.g. ``/c/Dev/x`` — the same leak shape as
#: ``C:\`` but produced by a Bash tool call on a Windows machine.
_PRIVACY_MSYS_DRIVE_RE = re.compile(r"/[a-zA-Z]/")
_PRIVACY_AT_RE = re.compile(r"@")
#: Independent-review item 3: a URL is as identity-leaking as an
#: absolute path or a bare "@" (query strings, hostnames, tokens in the
#: path segment), so it gets the same forbidden-pattern treatment.
_PRIVACY_URL_RE = re.compile(r"https?://|www\.")

#: Field names holding values that are allowed to contain the above
#: shapes by design, not by accident.
_PRIVACY_EXCLUDED_FIELDS = {
    # TranscriptMeta.path is the transcript's own source file path,
    # kept deliberately for provenance - never derived from message
    # content, so it's out of scope for this leak scan (mirrors
    # test_privacy.py's own _LONG_FIELD_ALLOWLIST treatment of "path").
    "path",
}

#: Field names where a bare "@" is a legitimate identifier shape (a
#: cloud-provider model id's Vertex "@YYYYMMDD" date suffix), not a
#: username/email leak - excluded from the "@" check only.
_PRIVACY_AT_SIGN_ALLOWED_FIELDS = {"model"}


def assert_privacy(result) -> None:
    """Recursively scan ``result`` for string values shaped like an
    absolute path or username/email/URL leak: a drive letter (``C:\\``),
    a POSIX ``/home/`` path, a Windows ``\\Users\\`` path, an MSYS/Git
    Bash drive path (``/c/...``), a bare ``@``, or a URL (``https?://``
    / ``www.``). Raises via ``assert`` with every violation listed, so a
    failure names exactly which field/index and value tripped it.

    ``result`` may be a ``TranscriptResult`` (the original, most common
    shape - walks ``meta``, ``diagnostics``, every ``Turn`` in ``turns``,
    every ``Event`` in ``events``), or - independent-review item 3 -
    any other dataclass (``Table``, ``Section``, ``Recommendation``,
    ...), a plain ``list``/``tuple``, or a ``dict``. Nested lists/tuples
    reached through a dataclass field (e.g. ``Table.rows``, a
    ``list[list]``, or ``Recommendation.evidence``, a ``list[tuple]``)
    are walked all the way down, not just one level - the earlier
    version silently skipped a list-of-lists because it only recursed
    into an item when the item was itself a dataclass.

    Dict values reached through a dataclass field are intentionally not
    walked, matching test_privacy.py's scope note: they're free-form
    small counters the module controls (``Event.detail``,
    ``Diagnostics.agent_settings``, ...), not a place message text/paths
    could leak through structurally. A dict passed as ``result`` itself
    (the top-level argument) *is* walked, since item 3 requires
    ``assert_privacy`` to accept a plain ``dict`` as input.
    """
    violations: list[str] = []

    def _check(value: str, where: str, field_name: str) -> None:
        if _PRIVACY_DRIVE_RE.search(value):
            violations.append(f"{where} matches a Windows drive path: {value!r}")
        if _PRIVACY_POSIX_HOME_RE.search(value):
            violations.append(f"{where} matches a POSIX /home/ path: {value!r}")
        if _PRIVACY_WIN_USERS_RE.search(value):
            violations.append(f"{where} matches a \\Users\\ path: {value!r}")
        if _PRIVACY_MSYS_DRIVE_RE.search(value):
            violations.append(f"{where} matches an MSYS drive path: {value!r}")
        if field_name not in _PRIVACY_AT_SIGN_ALLOWED_FIELDS and _PRIVACY_AT_RE.search(value):
            violations.append(f"{where} contains '@': {value!r}")
        if _PRIVACY_URL_RE.search(value):
            violations.append(f"{where} contains a URL: {value!r}")

    def _walk_value(value, where: str, field_name: str) -> None:
        """Walk a value reached via a dataclass field (or the top-level
        argument): strings are checked, dataclasses/lists/tuples are
        recursed into fully, dicts are left alone (see docstring)."""
        if isinstance(value, str):
            _check(value, where, field_name)
        elif isinstance(value, (tuple, list)):
            for i, item in enumerate(value):
                _walk_value(item, f"{where}[{i}]", field_name)
        elif dataclasses.is_dataclass(value) and not isinstance(value, type):
            _walk_dataclass(value, where)
        # dict: intentionally not walked when reached through a field.

    def _walk_dataclass(obj, where: str) -> None:
        for f in dataclasses.fields(obj):
            if f.name in _PRIVACY_EXCLUDED_FIELDS:
                continue
            _walk_value(getattr(obj, f.name), f"{where}.{f.name}", f.name)

    is_transcript_result = all(
        hasattr(result, attr) for attr in ("meta", "diagnostics", "turns", "events")
    )
    if is_transcript_result:
        _walk_value(result.meta, "meta", "meta")
        _walk_value(result.diagnostics, "diagnostics", "diagnostics")
        for i, turn in enumerate(result.turns):
            _walk_value(turn, f"turns[{i}]", "turns")
        for i, event in enumerate(result.events):
            _walk_value(event, f"events[{i}]", "events")
    elif isinstance(result, dict):
        for key, value in result.items():
            if isinstance(key, str):
                _check(key, "root.<key>", "")
            _walk_value(value, f"root[{key!r}]", "")
    else:
        _walk_value(result, "root", "")

    assert violations == [], violations
