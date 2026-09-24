#!/usr/bin/env python3
"""Claude Code hook: add Token Lens's metrics-capture note.

While metrics capture is on (``[capture]`` in Token Lens's ``config.toml``),
this adds a short note to Claude's context asking it to end its replies
with a one-line tag of closed-vocabulary words, such as
``[tl: task=bugfix brief=partial level=normal]``. Token Lens reads the
tags back from the transcripts.

It runs on three hook events, each added to Claude Code's settings.json
only when a chosen metric needs it (``claude-token-lens capture
connect``):

- ``SessionStart`` (matcher ``startup|clear|compact``): the main
  session's note. A resumed session already has it, so ``resume`` is not
  matched. A SessionStart inside a subagent (after it compacts) gets the
  subagent note: Claude Code sends no ``agent_id`` then, so a transcript
  under a ``subagents`` folder counts as one too.
- ``SubagentStart``: the subagent note, at every depth.
- ``PostToolUse`` (async): a one-line note after a large tool result or
  a web result, for the Deep level.

What the note says comes from ``capture-catalogue.json`` next to this
script, written from ``claude_token_lens.capture_catalogue``;
:func:`build_note` builds the same text as ``capture_catalogue.note_text``.

It adds nothing when capture is off, past its ``until`` time, outside
the sampled share of sessions (a hash of the session id, so a session's
subagents follow it), or in a project left out by ``[capture] projects``
or ``exclude_projects``. It uses only the standard library, and always
exits 0 without printing anything on an error, so it can never block or
break a session.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

CATALOGUE_FILE = "capture-catalogue.json"

#: Mirrors ``discovery.slug_for``: Claude Code's project folder name.
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")
_SLUG_MAX_CHARS = 200
_SLUG_HASH_HEX_CHARS = 8

#: Characters per token, for the large-output threshold.
_CHARS_PER_TOKEN = 4


def resolve_config_dir(cli_arg: str | None = None) -> Path:
    """``--config-dir`` (the token-lens folder itself), else
    ``<CLAUDE_CONFIG_DIR or ~/.claude>/token-lens``."""
    if cli_arg:
        return Path(cli_arg)
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "token-lens"


def load_catalogue(path: Path | None = None) -> dict:
    path = path or Path(__file__).resolve().with_name(CATALOGUE_FILE)
    return json.loads(path.read_text(encoding="utf-8"))


def load_config(config_dir: Path) -> dict:
    """``config.toml`` as a dict (``{}`` when there is none)."""
    try:
        text = (config_dir / "config.toml").read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    return tomllib.loads(text)


def slug_for(cwd: str) -> str:
    project_dir_name = os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME")
    if project_dir_name:
        return project_dir_name
    slug = _NON_ALNUM_RE.sub("-", cwd)
    if len(slug) <= _SLUG_MAX_CHARS:
        return slug
    digest = hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:_SLUG_HASH_HEX_CHARS]
    return f"{slug[:_SLUG_MAX_CHARS]}-{digest}"


def sampled_in(session_id: str, sample: int) -> bool:
    """Whether this session is in the captured share: the same answer for
    every hook call in the session, subagents included."""
    if sample >= 100:
        return True
    bucket = int(hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8], 16) % 100
    return bucket < sample


def project_allowed(slug: str, projects: list, exclude_projects: list) -> bool:
    """``projects`` holds slug patterns capture runs in, and ``!pattern``
    ones it skips; an empty list means every project. A project Token
    Lens leaves out altogether (``exclude_projects``) is skipped too."""
    for pattern in exclude_projects:
        if isinstance(pattern, str) and re.search(pattern, slug, re.IGNORECASE):
            return False
    includes = [p for p in projects if isinstance(p, str) and not p.startswith("!")]
    for pattern in projects:
        if isinstance(pattern, str) and pattern.startswith("!") and re.search(pattern[1:], slug, re.IGNORECASE):
            return False
    return not includes or any(re.search(p, slug, re.IGNORECASE) for p in includes)


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def active_ids(catalogue: dict, capture: dict) -> tuple[str, ...]:
    """The metrics switched on: a preset level's own, or the ``custom``
    list with what each needs, then the feedback toggles."""
    level = capture.get("level", "off")
    metrics = {m["id"]: m for m in catalogue["metrics"]}
    if level == "custom":
        wanted = {i for i in capture.get("metrics", []) if i in metrics}
        for i in list(wanted):
            wanted.update(metrics[i]["requires"])
        ids = tuple(m["id"] for m in catalogue["metrics"] if m["id"] in wanted)
    else:
        ids = tuple(catalogue["levels"].get(level, ()))
    feedback = set(capture.get("feedback", []))
    return ids + tuple(i for i in catalogue["feedback_ids"] if i in feedback)


def build_note(catalogue: dict, ids, scope: str, agent_type: str = "") -> str:
    """The note for ``scope`` (``"main"`` or ``"subagent"``); ``""`` when
    none of ``ids`` asks anything there. Same text as
    ``capture_catalogue.note_text``."""
    if scope == "subagent" and agent_type in catalogue["skip_agent_types"]:
        return ""
    wanted = set(ids)
    enabled = [m for m in catalogue["metrics"] if m["id"] in wanted]
    if scope == "subagent" and agent_type in catalogue["no_rules_agent_types"]:
        enabled = [m for m in enabled if m["id"] not in ("rules", "agent_brief")]
    main = scope == "main"
    lines = [m["main_line"] if main else m["sub_line"] for m in enabled]
    extras = [m["main_extra"] if main else m["sub_extra"] for m in enabled]
    codes = [m["id"] for m, line, x in zip(enabled, lines, extras) if line or x]
    if not codes:
        return ""
    text = catalogue["text"]
    out = [f"{catalogue['marker']}{catalogue['version']} {','.join(codes)}", text["intro"]]
    if any(lines):
        if main:
            out.append(text["main_tag_intro"])
        else:
            keys = any(line for m, line in zip(enabled, lines) if m["id"] != "result")
            out.append(text["sub_tag_intro"].format(tag=text["sub_tag_with_keys"] if keys else text["sub_tag"]))
        out += [line for line in lines if line]
        if main:
            out.append(text["skip_key_line"])
    out += [x for x in extras if x]
    return "\n".join(out)


def build_tool_note(catalogue: dict, metric_id: str) -> str:
    metric = next((m for m in catalogue["metrics"] if m["id"] == metric_id), None)
    if metric is None or not metric["tool_note"]:
        return ""
    return f"{catalogue['marker']}{catalogue['version']} {metric_id}\n{metric['tool_note']}"


def _result_chars(response) -> int:
    if isinstance(response, str):
        return len(response)
    try:
        return len(json.dumps(response, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def _in_subagent(payload: dict) -> bool:
    """Whether a SessionStart comes from a subagent's compaction. It
    carries no agent fields today, only the transcript it belongs to,
    which sits in the session's ``subagents`` folder."""
    if payload.get("agent_id"):
        return True
    transcript = payload.get("transcript_path")
    return isinstance(transcript, str) and Path(transcript.replace("\\", "/")).parent.name == "subagents"


def note_for(payload: dict, config: dict, catalogue: dict, now: datetime | None = None) -> str:
    """The note this hook call should add, or ``""``."""
    capture = config.get("capture")
    if not isinstance(capture, dict) or capture.get("level", "off") == "off":
        return ""
    until = capture.get("until") or ""
    if until:
        stop = _parse_time(until)
        if stop is None or (now or datetime.now(timezone.utc)) >= stop:
            return ""
    if not sampled_in(str(payload.get("session_id") or ""), int(capture.get("sample", 100))):
        return ""
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        exclude = config.get("exclude_projects", [])
        if not project_allowed(slug_for(cwd), capture.get("projects", []), exclude if isinstance(exclude, list) else []):
            return ""
    ids = active_ids(catalogue, capture)
    event = payload.get("hook_event_name")
    agent_type = str(payload.get("agent_type") or "")
    if event == "SubagentStart":
        return build_note(catalogue, ids, "subagent", agent_type)
    if event == "SessionStart":
        if payload.get("source") == "resume":
            return ""
        return build_note(catalogue, ids, "subagent" if _in_subagent(payload) else "main", agent_type)
    if event == "PostToolUse":
        tool = payload.get("tool_name")
        if "web" in ids and tool in catalogue["web_tools"]:
            return build_tool_note(catalogue, "web")
        threshold = catalogue["big_output_tokens"] * _CHARS_PER_TOKEN
        if "big_output" in ids and _result_chars(payload.get("tool_response")) >= threshold:
            return build_tool_note(catalogue, "big_output")
    return ""


def _run(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="capture-note.py")
    parser.add_argument("--config-dir", default=None)
    args = parser.parse_args(argv)
    # Claude Code sends UTF-8 whatever the console's code page is.
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    payload = json.loads(raw) if raw.strip() else {}
    if not isinstance(payload, dict):
        return
    try:
        config = load_config(resolve_config_dir(args.config_dir))
    except (OSError, ValueError):
        return  # an unreadable or half-written config reads as off
    note = note_for(payload, config, load_catalogue())
    if note:
        output = {"hookSpecificOutput": {"hookEventName": payload.get("hook_event_name"), "additionalContext": note}}
        sys.stdout.write(json.dumps(output))


def main(argv: list[str] | None = None) -> int:
    try:
        _run(sys.argv[1:] if argv is None else list(argv))
    except BaseException:  # noqa: BLE001 - must never fail or block a session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
