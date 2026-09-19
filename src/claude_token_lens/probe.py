"""Content-free schema histogram (plan "Risks and gaps" item 4: the
transcript format is undocumented and drifts, so users on a Claude Code
version this parser has never seen need a way to describe what they
have without pasting message text into a GitHub issue).

This module deliberately does NOT depend on ``parse.py``: a probe must
stay useful even against a line shape the real parser rejects outright,
since "what does an unrecognised line look like" is exactly what a bug
report needs. It reads each JSONL line with the stdlib ``json`` module
directly and only ever records:

- how many files/lines were read, and how many lines failed to parse as
  JSON at all;
- a count per top-level ``type`` value;
- a count per key name seen at the top level of each ``type`` (never a
  value -- key names only);
- a count per ``attachment.type`` value (``type=attachment`` lines);
- a count per ``subtype`` value (``type=system`` lines);
- a count per top-level ``version`` field value.

The last three are the plan's "listed enum-like fields" -- short,
non-secret tokens (a line type, an attachment type, a subtype, a version
string) that are safe to print verbatim because they identify a *shape*,
not content. Every recorded string is also defensively truncated to
:data:`MAX_VALUE_CHARS`, so even a pathological transcript can't smuggle
a long, message-shaped string through this module's output.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

#: Longest string this module will ever print verbatim (a defensive cap,
#: not something real transcripts are expected to approach -- every
#: enum-like field this module records is normally a handful of
#: characters).
MAX_VALUE_CHARS = 64

_MISSING_TYPE = "<missing type>"


def _clip(value: object) -> str:
    text = str(value)
    if len(text) > MAX_VALUE_CHARS:
        return text[:MAX_VALUE_CHARS] + "...(truncated)"
    return text


@dataclass(slots=True)
class ProbeResult:
    """Counts only -- no message text, tool input/output, file paths, or
    command strings ever pass through this dataclass. See the module
    docstring for exactly what each field counts.
    """

    files: int = 0
    lines: int = 0
    unparsable_lines: int = 0
    line_types: Counter = field(default_factory=Counter)
    #: ``{line_type: Counter({key_name: count})}``.
    keys_by_type: dict = field(default_factory=dict)
    attachment_types: Counter = field(default_factory=Counter)
    system_subtypes: Counter = field(default_factory=Counter)
    version_values: Counter = field(default_factory=Counter)


def probe_line(d: dict, result: ProbeResult) -> None:
    """Fold one already-parsed JSON object into ``result``."""
    raw_type = d.get("type")
    line_type = _clip(raw_type) if isinstance(raw_type, str) else _MISSING_TYPE
    result.line_types[line_type] += 1

    keys = result.keys_by_type.setdefault(line_type, Counter())
    for key in d.keys():
        if isinstance(key, str):
            keys[_clip(key)] += 1

    if line_type == "attachment":
        attachment = d.get("attachment")
        if isinstance(attachment, dict):
            atype = attachment.get("type")
            if isinstance(atype, str):
                result.attachment_types[_clip(atype)] += 1

    if line_type == "system":
        subtype = d.get("subtype")
        if isinstance(subtype, str):
            result.system_subtypes[_clip(subtype)] += 1

    version = d.get("version")
    if isinstance(version, (str, int, float)) and not isinstance(version, bool):
        result.version_values[_clip(version)] += 1


def probe_file(path: str | Path, result: ProbeResult | None = None) -> ProbeResult:
    """Probe one JSONL file, folding its lines into ``result`` (a new one
    if not given). Tolerates a truncated final line (a live session file
    being written by Claude Code) and any line that isn't valid JSON or
    isn't a JSON object -- both are counted, never fatal.
    """
    result = result if result is not None else ProbeResult()
    result.files += 1
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            result.lines += 1
            try:
                d = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                result.unparsable_lines += 1
                continue
            if isinstance(d, dict):
                probe_line(d, result)
            else:
                result.unparsable_lines += 1
    return result


def probe_paths(paths, result: ProbeResult | None = None) -> ProbeResult:
    """Probe every path in ``paths`` into one combined :class:`ProbeResult`."""
    result = result if result is not None else ProbeResult()
    for path in paths:
        probe_file(path, result)
    return result


def render_probe(result: ProbeResult) -> str:
    """Render ``result`` as plain text suitable for pasting into a bug
    report: counts and key/enum names only, never a value from the
    transcript itself.
    """
    lines: list[str] = ["# claude-token-lens probe", ""]
    lines.append(f"files: {result.files}")
    lines.append(f"lines: {result.lines}")
    lines.append(f"unparsable_lines: {result.unparsable_lines}")

    lines.append("")
    lines.append("## line types")
    if result.line_types:
        for line_type, count in result.line_types.most_common():
            lines.append(f"- {line_type}: {count}")
    else:
        lines.append("(none)")

    lines.append("")
    lines.append("## keys by line type")
    if result.keys_by_type:
        for line_type in sorted(result.keys_by_type):
            lines.append(f"### {line_type}")
            for key, count in result.keys_by_type[line_type].most_common():
                lines.append(f"- {key}: {count}")
    else:
        lines.append("(none)")

    lines.append("")
    lines.append("## attachment types")
    if result.attachment_types:
        for atype, count in result.attachment_types.most_common():
            lines.append(f"- {atype}: {count}")
    else:
        lines.append("(none)")

    lines.append("")
    lines.append("## system subtypes")
    if result.system_subtypes:
        for subtype, count in result.system_subtypes.most_common():
            lines.append(f"- {subtype}: {count}")
    else:
        lines.append("(none)")

    lines.append("")
    lines.append("## version field values")
    if result.version_values:
        for value, count in result.version_values.most_common():
            lines.append(f"- {value}: {count}")
    else:
        lines.append("(none)")

    return "\n".join(lines) + "\n"


__all__ = [
    "MAX_VALUE_CHARS",
    "ProbeResult",
    "probe_line",
    "probe_file",
    "probe_paths",
    "render_probe",
]
