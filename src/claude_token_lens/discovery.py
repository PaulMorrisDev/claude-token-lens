"""Filesystem discovery: where transcripts live and which ones match a
query, without parsing any of them.

Layout assumed throughout (plan "Verified facts"): ``<projects_root>/
<slug>/<session_id>.jsonl`` for top-level sessions, ``<projects_root>/
<slug>/<session_id>/subagents/agent-<hex>.jsonl`` (+ sibling
``.meta.json``) for subagent transcripts, and ``<projects_root>/<slug>/
<session_id>/workflows/wf_*.json`` for workflow runs.

Deviation from the plan, proposed here rather than silently made: the
plan states project slugs are "truncated to 200 chars plus a hash when
longer" without naming the hash algorithm or how it's appended — that
exact scheme isn't published. ``slug_for`` here uses the first 8 hex
characters of a SHA-256 digest of the untruncated slug text, joined with
a ``-``, which is deterministic and collision-resistant but is this
module's own choice, not a restatement of a documented format.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import jsonl
from .model import TranscriptMeta

_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")
_SLUG_MAX_CHARS = 200
_SLUG_HASH_HEX_CHARS = 8


def projects_root() -> Path:
    """The root directory holding every ``<slug>/`` project directory.

    Honours ``CLAUDE_CONFIG_DIR`` (whole-tree override); falls back to
    ``~/.claude/projects``.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "projects"
    return Path.home() / ".claude" / "projects"


def slug_for(cwd: str | Path) -> str:
    """The project slug for a working directory: non-alphanumeric
    characters become ``-``, truncated to 200 characters plus an 8-hex
    hash when longer (see the module docstring's deviation note).

    Honours ``CLAUDE_CODE_PROJECT_DIR_NAME`` whenever it's set, whether or
    not ``CLAUDE_CONFIG_DIR`` also moves the whole config tree — the two
    env vars are independent documented overrides, not a package deal.
    """
    project_dir_name = os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME")
    if project_dir_name:
        return project_dir_name

    raw = str(cwd)
    slug = _NON_ALNUM_RE.sub("-", raw)
    if len(slug) <= _SLUG_MAX_CHARS:
        return slug
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_SLUG_HASH_HEX_CHARS]
    return f"{slug[:_SLUG_MAX_CHARS]}-{digest}"


def resolve_project_dirs(
    root: str | Path,
    slugs: list[str] | None = None,
    all_projects: bool = False,
    family_regex: str | None = None,
) -> list[Path]:
    """Resolve which project directories under ``root`` a query selects.

    Matching is case-insensitive (``slugs`` compares lower-cased names;
    ``family_regex`` is searched with ``re.IGNORECASE``). Results are
    de-duplicated by ``os.path.normcase(os.path.realpath(...))`` so
    Windows slug case variants of the same real directory, or a worktree
    symlinked back to its parent, only appear once.

    Precedence when more than one selector is given: ``all_projects``,
    then ``family_regex``, then ``slugs``. Returns ``[]`` if none of the
    three select anything (including when ``root`` doesn't exist).
    """
    root = Path(root)
    if not root.exists():
        return []
    candidates = sorted(p for p in root.iterdir() if p.is_dir())

    if all_projects:
        selected = candidates
    elif family_regex:
        pattern = re.compile(family_regex, re.IGNORECASE)
        selected = [p for p in candidates if pattern.search(p.name)]
    elif slugs:
        wanted = {s.lower() for s in slugs}
        selected = [p for p in candidates if p.name.lower() in wanted]
    else:
        selected = []

    seen: set[str] = set()
    result: list[Path] = []
    for p in selected:
        key = os.path.normcase(os.path.realpath(p))
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    return result


def _resolve_window(
    days: int | None, since: str | None, until: str | None
) -> tuple[datetime | None, datetime | None]:
    since_dt: datetime | None = None
    until_dt: datetime | None = None
    if since is not None:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    elif days is not None:
        since_dt = datetime.now(timezone.utc) - timedelta(days=days)
    if until is not None:
        until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
    return since_dt, until_dt


def _session_window_ts(path: Path, window_by: str) -> datetime | None:
    if window_by == "mtime":
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None
    if window_by == "timestamp":
        try:
            for _line_no, d in jsonl.iter_lines(path):
                if d.get("type") not in ("user", "assistant"):
                    continue
                ts_raw = d.get("timestamp")
                if not isinstance(ts_raw, str) or not ts_raw:
                    continue
                try:
                    return datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
        except OSError:
            return None
        return None
    raise ValueError(f"unknown window_by: {window_by!r}")


def find_sessions(
    project_dir: str | Path,
    days: int | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    window_by: str = "mtime",
) -> list[Path]:
    """List top-level session transcripts (``<project_dir>/*.jsonl``)
    matching a window, newest first.

    ``window_by="mtime"`` (default, matches the seed's parity mode) uses
    the file's modification time; ``window_by="timestamp"`` reads the
    first ``user``/``assistant`` line's ``timestamp`` field instead —
    slower (a partial parse per file) but immune to a file being touched
    without new content (e.g. a filesystem backup).

    A file whose window key can't be determined (e.g. no user/assistant
    line, stat failure) is included only when no ``days``/``since``/
    ``until`` filter is active, and sorts last.
    """
    project_dir = Path(project_dir)
    if not project_dir.exists():
        return []
    since_dt, until_dt = _resolve_window(days, since, until)
    has_window_filter = since_dt is not None or until_dt is not None

    dated: list[tuple[Path, datetime | None]] = []
    for candidate in sorted(project_dir.glob("*.jsonl")):
        ts = _session_window_ts(candidate, window_by)
        if ts is None:
            if has_window_filter:
                continue
            dated.append((candidate, None))
            continue
        if since_dt is not None and ts < since_dt:
            continue
        if until_dt is not None and ts > until_dt:
            continue
        dated.append((candidate, ts))

    dated.sort(key=lambda pair: pair[1] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    paths = [p for p, _ts in dated]
    if limit is not None:
        paths = paths[:limit]
    return paths


def find_subagents(project_dir: str | Path, session_id: str) -> list[tuple[Path, dict]]:
    """List ``(jsonl_path, meta_dict)`` pairs for a session's subagent
    transcripts. ``meta_dict`` is the raw parsed ``.meta.json`` content
    (``{}`` if missing or unparsable) — callers needing a
    ``TranscriptMeta`` should pass it through ``load_meta`` instead, this
    is the raw form for callers that want individual keys.
    """
    subagents_dir = Path(project_dir) / session_id / "subagents"
    if not subagents_dir.exists():
        return []
    results: list[tuple[Path, dict]] = []
    for jsonl_path in sorted(subagents_dir.glob("agent-*.jsonl")):
        meta_path = jsonl_path.with_name(jsonl_path.stem + ".meta.json")
        meta: dict = {}
        if meta_path.exists():
            try:
                loaded = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                loaded = None
            if isinstance(loaded, dict):
                meta = loaded
        results.append((jsonl_path, meta))
    return results


def find_workflows(project_dir: str | Path, session_id: str) -> list[Path]:
    """List a session's ``workflows/wf_*.json`` run files. Not parsed
    here — the plan defers ``WorkflowRun`` parsing to WP8.
    """
    workflows_dir = Path(project_dir) / session_id / "workflows"
    if not workflows_dir.exists():
        return []
    return sorted(workflows_dir.glob("wf_*.json"))


#: meta.json key -> TranscriptMeta attribute, for the fields that copy
#: straight across with a type check and no transformation.
_META_DIRECT_FIELDS: tuple[tuple[str, str, type], ...] = (
    ("agentType", "agent_type", str),
    ("spawnDepth", "spawn_depth", int),
    ("parentAgentId", "parent_agent_id", str),
    ("requestShape", "request_shape", str),
)


def load_meta(path: str | Path) -> TranscriptMeta:
    """Load one subagent ``.meta.json`` file into a ``TranscriptMeta``.

    Maps ``agentType``, ``description`` (length only, never the text),
    ``spawnDepth``, ``parentAgentId``, ``model`` (-> ``agent_model_alias``
    — ``model`` on ``TranscriptMeta`` isn't a field; the transcript's own
    turns carry the real per-turn ``model``), ``requestShape``,
    ``worktreeBranch`` (-> ``worktree_branch_present``, a bool: never the
    branch name itself), ``stoppedByUser`` and ``toolUseId`` (->
    ``tool_use_id``, linking the subagent back to the parent turn that
    spawned it). Returns a ``kind="subagent"`` ``TranscriptMeta`` with
    defaults for anything missing or the file being absent/unparsable —
    this never raises.

    ``agent_id`` and ``session_id`` are derived from ``path`` itself
    rather than the file's content, per the documented layout
    ``<projects_root>/<slug>/<session_id>/subagents/agent-<hex>.jsonl``
    (paired with ``agent-<hex>.meta.json``): ``agent_id`` is the filename
    stem with a trailing ``.meta.json``/``.json`` stripped, and
    ``session_id`` is the grandparent directory's name (``path``'s
    parent is ``subagents/``, its parent is ``<session_id>/``). A ``path``
    that isn't actually two levels under a session directory (e.g. a
    test fixture that hands ``load_meta`` a bare file) still derives
    *some* value for each — never raises — it just won't be meaningful.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        raw = {}

    meta = TranscriptMeta(kind="subagent")
    for json_key, attr, expected_type in _META_DIRECT_FIELDS:
        value = raw.get(json_key)
        if isinstance(value, expected_type) and not isinstance(value, bool):
            setattr(meta, attr, value)

    description = raw.get("description")
    if isinstance(description, str):
        meta.description_len = len(description)

    model = raw.get("model")
    if isinstance(model, str):
        meta.agent_model_alias = model

    meta.worktree_branch_present = bool(raw.get("worktreeBranch"))

    stopped_by_user = raw.get("stoppedByUser")
    if isinstance(stopped_by_user, bool):
        meta.stopped_by_user = stopped_by_user

    tool_use_id = raw.get("toolUseId")
    if isinstance(tool_use_id, str):
        meta.tool_use_id = tool_use_id

    name = path.name
    if name.endswith(".meta.json"):
        meta.agent_id = name[: -len(".meta.json")]
    else:
        meta.agent_id = path.stem
    meta.session_id = path.parent.parent.name

    return meta


__all__ = [
    "projects_root",
    "slug_for",
    "resolve_project_dirs",
    "find_sessions",
    "find_subagents",
    "find_workflows",
    "load_meta",
]
