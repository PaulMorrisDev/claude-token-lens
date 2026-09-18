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
