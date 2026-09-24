#!/usr/bin/env python3
"""Claude Code hook: Token Lens's metrics capture.

While metrics capture is on (``[capture]`` in Token Lens's ``config.toml``),
this adds a short note to Claude's context asking it to end its replies
with a one-line tag of closed-vocabulary words, such as
``[tl: task=bugfix brief=partial level=normal]``. Token Lens reads the
tags back from the transcripts. It also logs a few free signals that
cost no tokens.

It runs on these hook events, each added to Claude Code's settings.json
only when a chosen metric needs it (``claude-token-lens capture
connect``):

- ``SessionStart`` (matcher ``startup|clear|compact``): the main
  session's note. A resumed session already has it, so ``resume`` is not
  matched. A SessionStart inside a subagent (after it compacts) gets the
  subagent note: it carries an ``agent_id``, and if a Claude Code
  version leaves that out, a transcript under a ``subagents`` folder
  counts as one too.
- ``SubagentStart``: the subagent note, at every depth.
- ``PostToolUse``: a one-line note after a large tool result or a web
  result, for the Deep level. Claude Code ignores what a background
  hook prints, so this entry runs in the foreground, matched only to
  tools whose results can be large, and returns at once for the rest.
- ``SessionEnd``, ``Notification`` and ``PermissionRequest`` (the last
  two async): one line each in ``<config-dir>/signals/YYYY-MM.jsonl``
  saying why a session ended, what Claude waited for, or which tool
  asked for permission. A line holds the time, a salted hash of the
  session id, and a word from a fixed list or a tool name; never a
  message, a tool's input or a path. Nothing is logged until Token Lens
  has made its salt.

What the note says comes from ``capture-catalogue.json`` next to this
script, written from ``claude_token_lens.capture_catalogue``;
:func:`build_note` builds the same text as ``capture_catalogue.note_text``.

It adds nothing when capture is off, past its ``until`` time, outside
the sampled share of sessions (a hash of the session id, so a session's
subagents follow it), or in a project left out by ``[capture] projects``
or ``exclude_projects``, and logs nothing then either. It uses only the
standard library, and always exits 0 without printing anything on an
error, so it can never block or break a session.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
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

#: Token Lens's salt (``parse.load_or_create_salt``), which the session
#: id is hashed with, and its length.
SALT_FILE = "salt"
_SALT_BYTES = 32

#: The short event names in a signal line.
_SIGNAL_CODES = {"SessionEnd": "end", "Notification": "wait", "PermissionRequest": "perm"}

#: Notification types (and, for older Claude Code versions without them,
#: the start of the message) -> what Claude waited for.
_WAIT_TYPES = {"permission_prompt": "permission", "idle_prompt": "idle", "elicitation_dialog": "question"}
_WAIT_MESSAGES = (("Claude needs your permission", "permission"), ("Claude is waiting for your input", "idle"))

#: What a tool name may look like to be logged; anything else is "other".
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


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


def _capture_for(payload: dict, config: dict, now: datetime) -> dict | None:
    """The ``[capture]`` table when capture applies to this hook call:
    on, not past its end, this session sampled in, and the project not
    left out. ``None`` otherwise."""
    capture = config.get("capture")
    if not isinstance(capture, dict) or capture.get("level", "off") == "off":
        return None
    until = capture.get("until") or ""
    if until:
        stop = _parse_time(until)
        if stop is None or now >= stop:
            return None
    if not sampled_in(str(payload.get("session_id") or ""), int(capture.get("sample", 100))):
        return None
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        exclude = config.get("exclude_projects", [])
        if not project_allowed(slug_for(cwd), capture.get("projects", []), exclude if isinstance(exclude, list) else []):
            return None
    return capture


def note_for(payload: dict, config: dict, catalogue: dict, now: datetime | None = None) -> str:
    """The note this hook call should add, or ``""``."""
    capture = _capture_for(payload, config, now or datetime.now(timezone.utc))
    if capture is None:
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


def read_salt(config_dir: Path) -> bytes | None:
    """Token Lens's salt, or ``None`` when it isn't there (yet) or is the
    wrong length: a line hashed with anything else could never be joined
    to its session."""
    try:
        salt = (config_dir / SALT_FILE).read_bytes()
    except OSError:
        return None
    return salt if len(salt) == _SALT_BYTES else None


def session_hash(salt: bytes, session_id: str) -> str:
    """The session id as a signal line keeps it (``signals.session_hash``)."""
    return hmac.new(salt, session_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def _wait_kind(payload: dict) -> str:
    notification_type = payload.get("notification_type")
    if notification_type:
        return _WAIT_TYPES.get(str(notification_type), "other")
    message = payload.get("message")
    if isinstance(message, str):
        for start, found in _WAIT_MESSAGES:
            if message.startswith(start):
                return found
    return "other"


def signal_for(payload: dict, config: dict, catalogue: dict, salt: bytes | None, now: datetime | None = None) -> dict | None:
    """The line to log for a SessionEnd, Notification or PermissionRequest
    call, or ``None`` when its metric is off or there's no salt."""
    event = payload.get("hook_event_name")
    metric = catalogue["signal_events"].get(event) if isinstance(event, str) else None
    session_id = payload.get("session_id")
    if metric is None or salt is None or not isinstance(session_id, str) or not session_id:
        return None
    now = now or datetime.now(timezone.utc)
    capture = _capture_for(payload, config, now)
    if capture is None or metric not in active_ids(catalogue, capture):
        return None
    record = {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "sid": session_hash(salt, session_id), "e": _SIGNAL_CODES[event]}
    if event == "SessionEnd":
        reason = payload.get("reason")
        record["reason"] = reason if reason in catalogue["session_end_reasons"] else "other"
    elif event == "Notification":
        record["kind"] = _wait_kind(payload)
    else:
        tool = payload.get("tool_name")
        record["tool"] = tool if isinstance(tool, str) and _TOOL_NAME_RE.fullmatch(tool) else "other"
    if payload.get("agent_id"):
        record["sub"] = 1
    return record


def write_signal(config_dir: Path, catalogue: dict, record: dict) -> None:
    """Append ``record`` to this month's signal file, as one write."""
    folder = config_dir / catalogue["signals_dir"]
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{record['ts'][:7]}.jsonl", "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def _run(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="capture-hook.py")
    parser.add_argument("--config-dir", default=None)
    args = parser.parse_args(argv)
    # Claude Code sends UTF-8 whatever the console's code page is.
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    payload = json.loads(raw) if raw.strip() else {}
    if not isinstance(payload, dict):
        return
    config_dir = resolve_config_dir(args.config_dir)
    try:
        config = load_config(config_dir)
    except (OSError, ValueError):
        return  # an unreadable or half-written config reads as off
    catalogue = load_catalogue()
    if payload.get("hook_event_name") in catalogue["signal_events"]:
        record = signal_for(payload, config, catalogue, read_salt(config_dir))
        if record:
            write_signal(config_dir, catalogue, record)
        return
    note = note_for(payload, config, catalogue)
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
