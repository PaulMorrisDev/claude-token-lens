"""Test-only builders for synthetic Claude Code JSONL fixtures.

Not a package under test itself — imported directly by test modules that
need realistic-shaped transcript lines without depending on the parser
(which doesn't exist yet in WP0).
"""

from __future__ import annotations

import itertools
import json
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
