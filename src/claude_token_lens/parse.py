"""Single-pass transcript parsing: JSONL lines in, ``TranscriptResult`` out.

Implements the project plan's "Parsing" section: assistant lines are
grouped into ``Turn`` records by ``message.id`` (fallback ``requestId``,
then ``uuid``); every other line becomes an ``Event`` (via
``events.classify_line``) attached to the turn it PRECEDES as
``preceding_event_kinds``/``preceding_attachment_types``/
``preceding_primary`` — i.e. the turn whose assistant line comes right
after it, not the turn whose assistant line came right before it. A
task-notification line sitting between turn A and turn B describes what
happened just before B ran, so it must attach to B; it says nothing about
what preceded A. This is implemented with two rotating buffers
(``events_for_current``/``events_since_current`` in ``parse_transcript``):
whatever accumulates while a turn is the in-progress ``current`` precedes
the *next* turn, not this one, so it's held back and only handed to
``_finalize_turn`` once the next turn actually starts. Events seen after
the transcript's last assistant line have no later turn to attach to and
are counted in ``Diagnostics.trailing_events`` instead.

``preceding_tool`` (for a non-first turn) is "Bash" if the previous turn's
``tool_names`` contains Bash, else "PowerShell" if it contains PowerShell,
else the previous turn's first tool name, else "none" if it had no tools
at all — a priority scan, not simply the previous turn's first tool name,
so a shell call is surfaced even when it wasn't the first tool invoked in
that turn. The first turn in a transcript has no previous turn, so its
``preceding_tool`` is "n/a".

Replay dedup: a rewind/resume can re-emit a whole block of lines verbatim
(same ``uuid``) later in the same file. Every line (including assistant
lines, which are also deduped separately by message id) is skipped and
counted in ``Diagnostics.replayed_lines`` the second and later time its
``uuid`` is seen; lines with no ``uuid`` (``queue-operation``,
``bridge-session``) are never subject to this check.

``gap_s`` is measured request-start to request-start: the interval between
the *first* JSONL line's timestamp of one priced turn and the first line's
timestamp of the previous priced turn, not (say) a turn's finalisation
time or its last line. ``discovery.find_sessions``'s ``--window-by
timestamp`` mode is the same convention applied at the session level: it
reads the first ``user``/``assistant`` line's timestamp, not the file's
own mtime. Both are deliberate, not an oversight — a turn/session's
*start* is the meaningful instant for gap and window calculations, and
it's the one value guaranteed to exist before any tool call or streaming
delay could skew it.

Privacy: no raw JSONL line, message content, tool_result content, file
path, or command is ever retained past the single line/block that
produces it. Only lengths, short prefixes (<=40 chars), names, and counts
survive into ``Turn``/``Event``/``Diagnostics``. Absolute- and
relative-path-shaped tokens, URLs, and any ``@``-bearing token (an
``ssh user@host`` target, an email address) inside a Bash/PowerShell
command are redacted to ``<path>``/``<url>``/``<user@host>`` before the
40-char truncation (see ``_redact_paths``), so a path, host, or address
near the cutoff can never leak a partial drive letter, username, or
domain.

Batch C addition: ``meta`` is provenance the caller already knows (from
``discovery.py``) and this function never mutates the object it was
handed — but the ``TranscriptResult.meta`` it *returns* can carry three
more fields than the input, derived from the transcript's own content:
``claude_version``/``entrypoint`` (first non-empty ``version``/
``entrypoint`` field seen on any raw line) and ``provider`` (from the
first turn with a model, via ``detect_provider``). A field already set on
the input ``meta`` (e.g. by ``discovery.load_meta``, which derives
``provider`` from a subagent's ``.meta.json`` model alias before any
turn is known) is left as-is, never overwritten by the scan — except
``provider``, where the transcript's own per-turn model is the more
authoritative source and takes precedence once a turn with a model
exists.

Usage-limits batch (v3-limits, see model.py's/events.py's module
docstrings): a synthetic assistant line's own text is classified with
``events.classify_synthetic_text``/``events.parse_limit_reset_clause``
in ``_new_pending`` and stored on ``Turn.synthetic_kind``. For the two
kinds that mean a usage cap was hit (``session_limit``/``weekly_limit``),
``parse_transcript``'s main loop synthesises a ``LIMIT_HIT`` event itself
right after ``current`` is set (the synthetic text lives on an
*assistant* line, and assistant lines never reach ``events.classify_line``
— see that module's docstring) and appends it to both ``events`` and
``events_since_current``, so — per the two-buffer scheme above — it
correctly precedes the *next* turn, not the synthetic line's own. The
reset instant (``Event.detail["reset_ts"]``) prefers the line's own
``quotaLimits.resetsAt`` epoch (see :func:`_limit_reset_ts`); the
parsed local-time-plus-zone clause is only a fallback for the ~24% of
limit-hit lines that don't carry ``quotaLimits``. ``Turn.gap_cause`` is
set to ``"limit"`` in ``_finalize_turn`` whenever a ``LIMIT_HIT``/
``LIMIT_RESUME`` event precedes that turn.

Wasted-turns batch (v4-wasted-turns, see model.py's ``Turn.
tool_error_count``/``tool_error_chars`` docstrings): ``_accumulate_tool_
results`` already walks every ``tool_result`` block answering the current
turn's own ``tool_use_ids`` to build ``tool_result_chars_by_tool``; the
same loop now also checks each block's own ``is_error`` field (``bool``
on the real corpus, verified read-only against the full local project
tree before implementing) and, when it is ``True``, increments
``current.tool_error_count`` and adds the same ``_tool_result_length``
figure already computed for that block onto ``current.tool_error_chars``
— no second pass over the content list, keeping the parser single-pass.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
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

#: Capture-improvements addition (A2): tool_use names whose own ``prompt``
#: input string is a spawned-agent brief. Only its length is ever kept
#: (``Turn.agent_brief_chars``) — never the text.
_AGENT_TOOL_NAMES = ("Agent", "Task")

#: Capture-improvements addition (A3): tool name -> the input key holding
#: the read/write target path to hash (``Turn.read_target_hashes``).
#: Deliberately a superset-compatible sibling of ``_EDIT_TOOL_PATH_KEYS``
#: (adds ``Read``, drops ``MultiEdit`` which has no single top-level path
#: key) rather than reusing it, since the two fields answer different
#: questions (edit-location classification vs. read/write-target hashing).
_READ_TARGET_PATH_KEYS = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}

_CMD_PREFIX_MAX_CHARS = 40

#: Usage-limits addition (see module docstring): event kinds whose
#: presence among a turn's preceding events marks its gap as a usage-cap
#: pause rather than idle/behavioural time (``Turn.gap_cause``).
_LIMIT_GAP_KINDS = (EventKind.LIMIT_HIT, EventKind.LIMIT_RESUME)

#: Absolute-path token shapes to redact out of a command prefix before it
#: is truncated (privacy criterion: zero drive letters/usernames in
#: exports). Matches a whole "word" starting with one of these shapes,
#: stopping at the first whitespace or quote so the verb and flags around
#: it survive. Order doesn't matter — each alternative is anchored to a
#: distinct prefix shape.
#:
#: R4 fix: the previous alternatives only caught *absolute* forms
#: (``\Users\...``, ``/home/...``) — a *relative* Windows path with no
#: leading separator (``cd Users\paulm\proj``, the shape a shell prints
#: for a path relative to the drive root) survived untouched. The new
#: last alternative catches just the ``Users``/``home``/``Documents and
#: Settings`` segment plus its own username component (stopping at the
#: next separator, same as the bare-backslash alternative above it) —
#: deliberately narrower than the greedy absolute-path alternatives,
#: since only the username segment is sensitive; a following relative
#: path component (a repo name, say) isn't. The ``(?<![\w:])`` lookbehind
#: keeps it from firing mid-word or right after a drive-letter colon,
#: where the absolute-path alternatives above already have it covered.
_ABS_PATH_TOKEN_RE = re.compile(
    r"""
    [A-Za-z]:[\\/][^\s"']*                       # C:\... or C:/...
    | /(?:home|Users|tmp|var|mnt|etc)/[^\s"']*    # POSIX absolute homes/tmp
    | /[a-zA-Z]/[^\s"']*                          # MSYS/Git Bash drive form: /c/Dev/x
    | \\(?:Users|home)\\[^\s"']*                  # bare \Users\... or \home\... (no drive letter)
    | ~[\\/][^\s"']*                              # ~/... or ~\...
    | %[A-Z_]+%[^\s"']*                           # %USERPROFILE%\...
    | \$HOME[^\s"']*                              # $HOME/...
    | (?<![\w:])(?:Users|home|Documents\ and\ Settings)[\\/][^\s\\/]+
                                                   # relative Users\name / home/name (no leading separator)
    """,
    re.VERBOSE,
)

#: Fix item 3: URLs in a command prefix are as identity-leaking as an
#: absolute path (a bug tracker link, an internal hostname, a signed
#: URL's query string) and were previously left untouched by
#: ``_redact_paths``. Matches an ``http``/``https`` URL, or a bare
#: ``www.`` form with no scheme, up to the next whitespace/quote.
_URL_TOKEN_RE = re.compile(r"""https?://[^\s"']+|www\.[^\s"']+""")

#: R4 fix: any whitespace-delimited token containing ``@`` is redacted
#: wholesale — this covers both an ``ssh user@host`` target and a bare
#: email address (e.g. inside a commit message), neither of which the
#: path/URL patterns above ever matched. Deliberately not anchored to
#: a stricter user@host/email shape: a bare ``@`` in a command is
#: already a strong enough identity signal (an account/host name) that
#: erring toward over-redaction here is the right trade-off.
_AT_TOKEN_RE = re.compile(r"""[^\s"']*@[^\s"']*""")


def _redact_paths(text: str) -> str:
    """Replace every absolute- or relative-path-shaped token in ``text``
    with ``<path>``, every URL with ``<url>``, and every ``@``-bearing
    token (an ``ssh user@host`` target, an email address) with
    ``<user@host>`` — keeping the surrounding verb/flags intact. Called
    before truncation so a path, URL, or user@host/email near the
    40-char cutoff can't leak a partial drive letter, username fragment,
    query string, or domain.

    URLs are redacted first: ``_ABS_PATH_TOKEN_RE``'s drive-letter
    alternative (``[A-Za-z]:[\\/]``) is happy to match the single
    letter before a scheme's ``://`` (e.g. the "s" in "https://"),
    which would otherwise mangle a URL into "http<path>" before the URL
    regex ever saw it intact. The ``@`` rule runs next (a URL's own
    ``user:pass@host`` form, if any, has already been swallowed whole
    into ``<url>`` by then), and the path rule runs last.
    """
    without_urls = _URL_TOKEN_RE.sub("<url>", text)
    without_at = _AT_TOKEN_RE.sub("<user@host>", without_urls)
    return _ABS_PATH_TOKEN_RE.sub("<path>", without_at)


#: Capture-improvements addition (A3): module-level salt used by
#: ``_read_target_hash``. Threaded through a setter rather than a
#: ``parse_transcript`` parameter so the function's public signature
#: stays stable (per this task's contract) while still letting a
#: ``ProcessPoolExecutor`` worker (spawned fresh under Windows ``spawn``)
#: initialise it once before parsing any file — see ``set_salt``.
_SALT: bytes | None = None

_SALT_FILENAME = "salt"

#: ``0`` on POSIX (no such flag; binary is the only mode ``open()``/
#: ``os.open()`` ever use there). On Windows, ``os.open()`` without this
#: flag defaults to *text* mode, which silently rewrites any ``b"\n"``
#: (0x0a) byte in the data to ``b"\r\n"`` on write -- fatal for a random
#: 32-byte salt, where roughly one in eight salts contains at least one
#: 0x0a byte. Omitting it was a real, intermittent bug (not just a flaky
#: test): the freshly written salt and the salt read back moments later
#: would silently differ whenever the random salt happened to contain a
#: newline byte, corrupting every hash taken with it as "the" salt for
#: this config dir.
_O_BINARY = getattr(os, "O_BINARY", 0)


#: The only valid salt length -- ``secrets.token_bytes(32)``'s own output
#: size. Enforced by both :func:`set_salt` and :func:`load_or_create_salt`
#: (fix #4): a shorter salt collapses HMAC-SHA256's effective key space and
#: a longer one is simply not what this module ever writes, so either is
#: treated as a corrupted/foreign file rather than accepted silently.
_SALT_LENGTH_BYTES = 32


def set_salt(salt: bytes) -> None:
    """Set the process-wide salt used by ``_read_target_hash`` for
    ``Turn.read_target_hashes``. Must be called once (per process) before
    ``parse_transcript`` if hashed read targets are wanted — with no salt
    set, ``Turn.read_target_hashes`` is always empty (see
    ``_read_target_hash``). A plain module global, not a
    ``parse_transcript`` argument, so callers running under
    ``ProcessPoolExecutor`` (``corpus.py``) can initialise each worker
    process once via an initializer rather than threading the salt
    through every call.

    Raises ``ValueError`` if ``salt`` is not exactly
    :data:`_SALT_LENGTH_BYTES` long (fix #4) — an unsalted-strength hash
    from a truncated or foreign salt is worse than no hash at all (see
    ``_read_target_hash``'s own "``None`` rather than unsalted" contract).
    """
    if len(salt) != _SALT_LENGTH_BYTES:
        raise ValueError(f"salt must be {_SALT_LENGTH_BYTES} bytes, got {len(salt)}")
    global _SALT
    _SALT = salt


def _default_token_lens_dir() -> Path:
    """``$CLAUDE_CONFIG_DIR/token-lens``, else ``~/.claude/token-lens``.
    Mirrors ``cli.py``'s own ``_resolve_config_dir`` (see its docstring on
    why each module keeps its own copy of this lookup rather than
    importing one from another) — this module deliberately doesn't import
    ``cli.py``/``config.py`` to stay a leaf dependency.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base) if base else (Path.home() / ".claude")
    return root / "token-lens"


def load_or_create_salt(config_dir: str | Path | None = None) -> bytes:
    """Load the 32-byte salt at ``<config_dir>/salt``, creating it with
    ``secrets.token_bytes(32)`` on first use. ``config_dir`` defaults to
    ``_default_token_lens_dir()``. Does not call ``set_salt`` itself — the
    caller decides when the process-wide salt is wired up.

    Fix #4: any read failure — not just a missing file (``PermissionError``,
    ``IsADirectoryError``, a dead network mount, ...) — is treated as "no
    salt yet" rather than propagating and crashing the caller, and a salt
    file whose length is not exactly :data:`_SALT_LENGTH_BYTES` (a
    zero-byte file from an interrupted first write, a truncated sync, a
    hand-edited file) is likewise treated as absent and regenerated —
    returning it unsalted would defeat the whole hashing mechanism (a
    zero-length salt makes ``_read_target_hash`` produce a plain,
    rainbow-table-able HMAC). The replacement file is created via
    ``os.open`` with ``O_CREAT`` and mode ``0o600`` together, so a
    brand-new file is never briefly world-readable between creation and a
    separate ``chmod`` call; ``chmod`` still runs afterwards (best-effort,
    ignored on Windows, which has no equivalent bit) to cover the
    overwrite-an-existing-but-invalid-file branch, where ``O_CREAT``'s mode
    argument has no effect on an already-existing inode's permissions.
    """
    directory = Path(config_dir) if config_dir is not None else _default_token_lens_dir()
    directory.mkdir(parents=True, exist_ok=True)
    salt_path = directory / _SALT_FILENAME
    try:
        existing = salt_path.read_bytes()
    except OSError:
        existing = None
    if existing is not None and len(existing) == _SALT_LENGTH_BYTES:
        return existing

    salt = secrets.token_bytes(_SALT_LENGTH_BYTES)
    try:
        fd = os.open(salt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
    except FileExistsError:
        # The file exists but was rejected above (missing/unreadable/wrong
        # length) -- overwrite it in place rather than trying (and racing)
        # to delete-then-recreate it.
        fd = os.open(salt_path, os.O_WRONLY | os.O_TRUNC | _O_BINARY)
    try:
        os.write(fd, salt)
    finally:
        os.close(fd)
    try:
        os.chmod(salt_path, 0o600)
    except OSError:
        pass
    return salt


def _normalize_path_for_hash(path_value: str) -> str:
    """Normalise a tool_use target path before hashing, so the same real
    location hashes the same way regardless of slash direction or case
    (Windows paths are case-insensitive) — deliberately *not* resolving
    against the filesystem (``Path.resolve()``), since this path may not
    exist on this machine (e.g. a subagent transcript scrubbed for
    fixtures) and a hash function must never raise on its input.
    """
    return path_value.replace("\\", "/").casefold()


def _read_target_hash(path_value: str) -> str | None:
    """Salted HMAC-SHA256 of a normalised read/write target path, truncated
    to 16 hex chars — ``None`` when no salt has been set yet (see
    ``set_salt``), so a caller that never wires up hashing simply gets no
    hashes rather than an unsalted (crackable) one.
    """
    if _SALT is None:
        return None
    return path_hash(path_value, _SALT)


def path_hash(path_value: str, salt: bytes) -> str:
    """The same salted hash as :func:`_read_target_hash`, with the salt
    passed in, so a file found on disk can be matched to the records
    transcripts keep for it (``claude_md_review``)."""
    normalized = _normalize_path_for_hash(path_value)
    return hmac.new(salt, normalized.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def detect_provider(model_id: str | None) -> str | None:
    """Classify which API surface a model id was billed through, from the
    id's own form (plan Enterprise-use section): a Bedrock id is prefixed
    ``anthropic.``/``us.anthropic.`` or suffixed ``-v1:0``; a Vertex id
    carries an ``@<date>`` suffix; anything else is a direct Anthropic API
    id. Returns ``None`` for an empty/missing id — nothing to classify.

    Shared with ``discovery.load_meta``, which derives a subagent's
    ``provider`` from its ``.meta.json`` model alias before any turn is
    parsed; ``parse_transcript`` recomputes it from the transcript's own
    turns once one exists, since a turn's ``model`` is the authoritative
    source (see this module's docstring).
    """
    if not model_id:
        return None
    lowered = model_id.lower()
    if lowered.startswith("anthropic.") or lowered.startswith("us.anthropic.") or lowered.endswith("-v1:0"):
        return "bedrock"
    if "@" in model_id:
        return "vertex"
    return "anthropic"


def _escape_newlines(text: str) -> str:
    return text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _parse_ts(ts_raw: str) -> datetime | None:
    if not ts_raw:
        return None
    try:
        return datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _synthetic_text(content: object) -> str | None:
    """The text of a synthetic assistant line's ``message.content``,
    which the real corpus always shows as a list of content blocks (a
    plain string is tolerated defensively but not observed) -- for
    classification only, never retained.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    return text
    return None


def _limit_reset_ts(
    d: dict, ts_dt: datetime | None, minutes_of_day: int | None, reset_tz: str | None
) -> str | None:
    """UTC ISO timestamp of a limit-hit's reset. Prefers the line's own
    ``quotaLimits.resetsAt`` (a precise Unix epoch, UTC) -- present on
    only around three-quarters of limit-hit lines in the sampled corpus
    -- over reconstructing one from the parsed local-time-of-day clause
    plus ``reset_tz``, which needs ``zoneinfo`` to resolve the zone (may
    be unavailable, e.g. missing tzdata on a bare Windows install) and a
    reference date (this line's own timestamp, in that zone) to anchor
    "today" vs. "tomorrow". Returns ``None`` when neither source is
    usable.
    """
    quota_limits = d.get("quotaLimits")
    if isinstance(quota_limits, dict):
        resets_at = quota_limits.get("resetsAt")
        if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool):
            try:
                return (
                    datetime.fromtimestamp(float(resets_at), tz=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            except (OverflowError, OSError, ValueError):
                pass

    if minutes_of_day is None or reset_tz is None or ts_dt is None:
        return None
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(reset_tz)
    except Exception:
        # Missing tzdata, or a name zoneinfo doesn't recognise -- never
        # let this fall through to an exception escaping the parser.
        return None
    try:
        local_ts = ts_dt.astimezone(zone)
        reset_hour, reset_minute = divmod(minutes_of_day, 60)
        candidate = local_ts.replace(hour=reset_hour, minute=reset_minute, second=0, microsecond=0)
        if candidate <= local_ts:
            candidate = candidate + timedelta(days=1)
        return candidate.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
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
    #: Set when the raw ``cache_creation_input_tokens`` flat field didn't
    #: equal ``cc_5m + cc_1h`` for this turn's usage, computed once at
    #: ``_new_pending`` time (before any reconciliation) so a later
    #: correction to ``cache_creation_tokens`` doesn't erase the signal.
    ttl_sum_mismatch: bool = False
    #: Set when ``usage`` had no nested ``cache_creation`` object at all
    #: (older, pre-split JSONL) - see ``Turn.ttl_split_unknown``. Never
    #: set alongside ``ttl_sum_mismatch``: a missing split is a format
    #: difference, not a sum invariant breach.
    ttl_split_unknown: bool = False
    inference_geo: str | None = None
    attribution_mcp_server: str | None = None
    attribution_mcp_tool: str | None = None
    attribution_skill: str | None = None
    tool_names: list[str] = field(default_factory=list)
    #: Batch C addition (see model.py's ``Turn.tool_use_ids`` docstring):
    #: every ``tool_use`` block's ``id`` in this turn, in encounter order.
    tool_use_ids: list[str] = field(default_factory=list)
    cmd_prefix: str | None = None
    edit_real_found: bool = False
    edit_scratch_found: bool = False
    #: Capture-improvements addition (A2, see model.py's ``Turn.
    #: agent_brief_chars``/``tool_input_chars_by_tool`` docstrings).
    agent_brief_chars: int | None = None
    tool_input_chars_by_tool: dict[str, int] = field(default_factory=dict)
    #: Capture-improvements addition (A3, see model.py's ``Turn.
    #: read_target_hashes`` docstring).
    read_target_hashes: list[str] = field(default_factory=list)
    #: Capture-improvements addition (A1, see model.py's ``Turn.
    #: tool_result_chars_by_tool``/``tool_wait_s``/``model_latency_s``
    #: docstrings): populated by ``_accumulate_tool_results`` for
    #: tool_result lines answering *this* turn's own ``tool_use_ids``.
    tool_result_chars_by_tool: dict[str, int] = field(default_factory=dict)
    tool_result_ts_values: list[str] = field(default_factory=list)
    #: Usage-limits addition (see module docstring): set only when
    #: ``is_synthetic`` is True.
    synthetic_kind: str | None = None
    reset_minutes_of_day: int | None = None
    reset_tz: str | None = None
    reset_ts: str | None = None
    #: Wasted-turns addition (see model.py's ``Turn.tool_error_count``/
    #: ``tool_error_chars`` docstrings): populated by
    #: ``_accumulate_tool_results`` for tool_result lines answering this
    #: turn's own ``tool_use_ids`` that carry ``is_error: true``.
    tool_error_count: int = 0
    tool_error_chars: int = 0
    #: Context-files addition (see model.py's ``Turn.skills_invoked``).
    skills_invoked: list[str] = field(default_factory=list)
    #: Quality-signals addition (see model.py's ``Turn.stop_reason``/
    #: ``tool_errors_by_tool``/``edit_target_hashes``).
    stop_reason: str | None = None
    tool_calls_by_tool: dict[str, int] = field(default_factory=dict)
    tool_errors_by_tool: dict[str, int] = field(default_factory=dict)
    edit_target_hashes: list[str] = field(default_factory=list)


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
        pending.tool_calls_by_tool[name] = pending.tool_calls_by_tool.get(name, 0) + 1
        tool_use_id = block.get("id")
        if isinstance(tool_use_id, str) and tool_use_id:
            tool_use_names[tool_use_id] = name
            pending.tool_use_ids.append(tool_use_id)
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            continue
        if pending.cmd_prefix is None and name in _SHELL_TOOL_NAMES:
            command = tool_input.get("command")
            if isinstance(command, str) and command:
                redacted = _redact_paths(_escape_newlines(command))
                pending.cmd_prefix = redacted[:_CMD_PREFIX_MAX_CHARS]
        path_key = _EDIT_TOOL_PATH_KEYS.get(name)
        if path_key is not None:
            path_value = tool_input.get(path_key)
            if isinstance(path_value, str) and path_value:
                if path_value.lower().startswith(tmpdir):
                    pending.edit_scratch_found = True
                else:
                    pending.edit_real_found = True

        # A2: agent-brief size -- the Agent/Task tool_use's own `prompt`
        # input length, never the prompt text itself -- plus a per-tool
        # total of every tool_use's JSON-encoded input size.
        if name in _AGENT_TOOL_NAMES:
            prompt = tool_input.get("prompt")
            if isinstance(prompt, str) and prompt:
                pending.agent_brief_chars = (pending.agent_brief_chars or 0) + len(prompt)
        input_chars = len(json.dumps(tool_input, ensure_ascii=False, default=str))
        pending.tool_input_chars_by_tool[name] = (
            pending.tool_input_chars_by_tool.get(name, 0) + input_chars
        )

        # A3: hash Read/Edit/Write/NotebookEdit targets instead of ever
        # storing the path itself.
        read_target_key = _READ_TARGET_PATH_KEYS.get(name)
        if read_target_key is not None:
            target_value = tool_input.get(read_target_key)
            if isinstance(target_value, str) and target_value:
                hashed = _read_target_hash(target_value)
                if hashed is not None:
                    pending.read_target_hashes.append(hashed)
                    if name in _EDIT_TOOL_PATH_KEYS:
                        pending.edit_target_hashes.append(hashed)

        if name == "Skill":
            skill_name = tool_input.get("skill")
            if isinstance(skill_name, str) and skill_name:
                pending.skills_invoked.append(skill_name)


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
    if pending.is_synthetic:
        # Usage-limits addition (see module docstring): classify the
        # synthetic text and, for a usage-cap hit, its reset clause.
        text = _synthetic_text(message.get("content"))
        pending.synthetic_kind = events_mod.classify_synthetic_text(text)
        if pending.synthetic_kind in ("session_limit", "weekly_limit"):
            minutes_of_day, reset_tz = events_mod.parse_limit_reset_clause(text)
            pending.reset_minutes_of_day = minutes_of_day
            pending.reset_tz = reset_tz
            pending.reset_ts = _limit_reset_ts(d, _parse_ts(pending.ts_raw), minutes_of_day, reset_tz)
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

            # Reconcile the flat cache_creation_input_tokens field against
            # the 5m/1h split: some usage payloads under-report the flat
            # field relative to its own ephemeral breakdown. The mismatch
            # is always recorded (even when nothing needs correcting, e.g.
            # the flat field is *larger* than the split); ctx (computed
            # from cache_creation_tokens in _finalize_turn) picks up the
            # corrected value automatically.
            ttl_sum = pending.cc_5m + pending.cc_1h
            if ttl_sum != pending.cache_creation_tokens:
                pending.ttl_sum_mismatch = True
            if pending.cache_creation_tokens < ttl_sum:
                pending.cache_creation_tokens = ttl_sum
        else:
            # Coordinator follow-up (WP12a diversity fixtures): older,
            # pre-5m/1h-split Claude Code JSONL has no nested
            # cache_creation object at all. cc_5m/cc_1h stay 0 (the split
            # was never recorded, not that nothing was written) and the
            # flat cache_creation_input_tokens is kept as the write total
            # unchanged - this is a format difference, not a sum mismatch,
            # so it must never set ttl_sum_mismatch (0 != a nonzero flat
            # value is not evidence of anything broken here).
            pending.ttl_split_unknown = True

    _merge_content_blocks(pending, message.get("content"), tool_use_names)
    _merge_stop_reason(pending, message)
    return pending


def _merge_stop_reason(pending: _PendingTurn, message) -> None:
    """Quality-signals addition: keep the last non-null ``stop_reason``
    across the lines of one message (streamed lines carry null until the
    final one)."""
    stop_reason = message.get("stop_reason") if isinstance(message, dict) else None
    if isinstance(stop_reason, str) and stop_reason:
        pending.stop_reason = stop_reason[:32]


def _merge_into_pending(pending: _PendingTurn, d: dict, tool_use_names: dict[str, str]) -> None:
    if not pending.is_synthetic:
        message = d.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        if model == "<synthetic>" or d.get("isApiErrorMessage"):
            pending.is_synthetic = True
    message = d.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    _merge_content_blocks(pending, content, tool_use_names)
    _merge_stop_reason(pending, message)


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
    current: _PendingTurn | None = None,
) -> None:
    message = d.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    #: A1 addition: this tool_result line's own timestamp, recorded on
    #: ``current`` only for the tool_use_ids that are actually its own
    #: (see model.py's ``Turn.tool_wait_s``/``tool_result_chars_by_tool``
    #: docstrings) -- a tool_result can answer a tool_use from an earlier
    #: turn, which must not pollute this turn's own timing/composition.
    ts_raw = d.get("timestamp")
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
        if (
            current is not None
            and isinstance(tool_use_id, str)
            and tool_use_id in current.tool_use_ids
        ):
            current.tool_result_chars_by_tool[name] = (
                current.tool_result_chars_by_tool.get(name, 0) + length
            )
            if isinstance(ts_raw, str) and ts_raw:
                current.tool_result_ts_values.append(ts_raw)
            # Wasted-turns addition (see model.py's ``Turn.
            # tool_error_count``/``tool_error_chars`` docstrings): a
            # tool_result answering this turn's own tool_use flagged
            # ``is_error: true`` -- length only, never the error content.
            if block.get("is_error") is True:
                current.tool_error_count += 1
                current.tool_error_chars += length
                current.tool_errors_by_tool[name] = current.tool_errors_by_tool.get(name, 0) + 1


def _resolve_preceding_tool(previous_turn: Turn | None) -> tuple[str, str | None]:
    if previous_turn is None:
        return "n/a", None
    if not previous_turn.tool_names:
        return "none", previous_turn.cmd_prefix
    if "Bash" in previous_turn.tool_names:
        return "Bash", previous_turn.cmd_prefix
    if "PowerShell" in previous_turn.tool_names:
        return "PowerShell", previous_turn.cmd_prefix
    return previous_turn.tool_names[0], previous_turn.cmd_prefix


def _finalize_turn(
    pending: _PendingTurn,
    pending_events: list[Event],
    pending_attachment_types: list[str],
    previous_turn: Turn | None,
    previous_non_synthetic_ts: datetime | None,
    priced_turn_count: int,
    diagnostics: Diagnostics,
    next_ts_raw: str | None = None,
) -> tuple[Turn, datetime | None, int]:
    ts_dt = _parse_ts(pending.ts_raw)
    ctx = pending.input_tokens + pending.cache_creation_tokens + pending.cache_read_tokens

    if pending.has_usage:
        if pending.ttl_sum_mismatch:
            diagnostics.ttl_sum_mismatch += 1
        if pending.ttl_split_unknown:
            diagnostics.pre_split_turns += 1
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
        if pending.ts_raw and ts_dt is None:
            diagnostics.timestamp_parse_failures += 1
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

    # A1: timing either side of this turn's own tool calls, from the
    # tool_result timestamp(s) ``_accumulate_tool_results`` recorded onto
    # this pending turn (see model.py's ``Turn.tool_wait_s``/
    # ``model_latency_s`` docstrings). Both stay None when this turn made
    # no tool calls, or a timestamp is missing/unparsable.
    tool_wait_s: float | None = None
    model_latency_s: float | None = None
    if pending.tool_result_ts_values:
        parsed_result_ts = [
            parsed for parsed in (_parse_ts(raw) for raw in pending.tool_result_ts_values) if parsed is not None
        ]
        if parsed_result_ts:
            max_tool_result_ts = max(parsed_result_ts)
            if ts_dt is not None:
                tool_wait_s = (max_tool_result_ts - ts_dt).total_seconds()
            next_ts_dt = _parse_ts(next_ts_raw) if next_ts_raw else None
            if next_ts_dt is not None:
                model_latency_s = (next_ts_dt - max_tool_result_ts).total_seconds()

    # A4: human-prompt size/paste-flag, from any HUMAN_TEXT event(s) that
    # preceded this turn (see model.py's ``Turn.human_prompt_chars``/
    # ``human_prompt_has_paste`` docstrings).
    human_prompt_chars: int | None = None
    human_prompt_has_paste = False
    human_correction = False
    for pending_event in pending_events:
        if pending_event.kind != EventKind.HUMAN_TEXT:
            continue
        chars = pending_event.size_chars or 0
        human_prompt_chars = chars if human_prompt_chars is None else human_prompt_chars + chars
        if pending_event.detail.get("has_paste"):
            human_prompt_has_paste = True
        if pending_event.detail.get("correction"):
            human_correction = True

    # Usage-limits addition (see module docstring): a limit-hit/resume
    # among the events preceding this turn means the gap to the previous
    # turn was (at least in part) a usage-cap pause, not idle time.
    gap_cause = (
        "limit"
        if any(pending_event.kind in _LIMIT_GAP_KINDS for pending_event in pending_events)
        else None
    )

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
        ttl_split_unknown=pending.ttl_split_unknown,
        ctx=ctx,
        tool_names=tuple(pending.tool_names),
        tool_use_ids=tuple(pending.tool_use_ids),
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
        tool_wait_s=tool_wait_s,
        model_latency_s=model_latency_s,
        tool_result_chars_by_tool=dict(pending.tool_result_chars_by_tool),
        agent_brief_chars=pending.agent_brief_chars,
        tool_input_chars_by_tool=dict(pending.tool_input_chars_by_tool),
        read_target_hashes=tuple(pending.read_target_hashes),
        human_prompt_chars=human_prompt_chars,
        human_prompt_has_paste=human_prompt_has_paste,
        synthetic_kind=pending.synthetic_kind,
        gap_cause=gap_cause,
        tool_error_count=pending.tool_error_count,
        tool_error_chars=pending.tool_error_chars,
        skills_invoked=tuple(pending.skills_invoked),
        stop_reason=pending.stop_reason,
        tool_calls_by_tool=dict(pending.tool_calls_by_tool),
        tool_errors_by_tool=dict(pending.tool_errors_by_tool),
        edit_target_hashes=tuple(pending.edit_target_hashes),
        human_correction=human_correction,
    )
    return turn, new_prev_ts, new_priced_count


def parse_transcript(path: str | Path, meta: TranscriptMeta) -> TranscriptResult:
    """Parse one transcript JSONL file in a single streaming pass.

    ``meta`` is provenance the caller already knows (from
    ``discovery.py``) — this function fills in ``turns``, ``events``,
    ``diagnostics``, ``tool_result_chars`` and ``tool_result_calls``
    around it; it never mutates ``meta`` (see module docstring for the
    ``claude_version``/``entrypoint``/``provider`` derivation this
    function's *returned* meta copy adds on top).
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
    #: Batch C addition: first non-empty ``entrypoint``/``version`` field
    #: seen on any raw line, in file order. Every line type carries these
    #: (when present), not just assistant lines.
    first_entrypoint: str | None = None
    first_claude_version: str | None = None
    #: Batch C addition: provider derived from the first turn with a
    #: model, overriding whatever ``meta.provider`` already held (see
    #: ``detect_provider``/module docstring).
    provider = meta.provider

    #: Events attached to the turn currently being accumulated in
    #: ``current`` (i.e. observed *before* ``current`` started): what
    #: ``current`` finalises with. ``*_since_current`` accumulates events
    #: seen *while* ``current`` is in progress — these precede the NEXT
    #: turn, not this one, and become ``*_for_current`` when that next
    #: turn starts (see the module docstring's two-buffer fix).
    events_for_current: list[Event] = []
    attachments_for_current: list[str] = []
    events_since_current: list[Event] = []
    attachments_since_current: list[str] = []
    finalized_keys: set[str] = set()
    #: Lines already processed once, by ``uuid`` — a rewind/resume can
    #: replay a whole block of user/attachment/system (and, rarely,
    #: assistant) lines verbatim with the same uuid; the replay is
    #: skipped outright and counted, not reprocessed as new activity.
    #: ``queue-operation``/``bridge-session`` lines carry no uuid, so they
    #: fall through this check untouched (guarded by the ``isinstance``/
    #: truthiness check below) rather than being treated as replays.
    seen_uuids: set[str] = set()

    current: _PendingTurn | None = None
    current_key: str | None = None
    previous_turn: Turn | None = None
    previous_non_synthetic_ts: datetime | None = None
    priced_turn_count = 0

    for _line_no, d in jsonl.iter_lines(path, stats=line_stats):
        uuid_val = d.get("uuid")
        if isinstance(uuid_val, str) and uuid_val:
            if uuid_val in seen_uuids:
                diagnostics.replayed_lines += 1
                continue
            seen_uuids.add(uuid_val)

        line_type = d.get("type")

        if first_entrypoint is None:
            entrypoint_raw = d.get("entrypoint")
            if isinstance(entrypoint_raw, str) and entrypoint_raw:
                first_entrypoint = entrypoint_raw
        if first_claude_version is None:
            version_raw = d.get("version")
            if isinstance(version_raw, str) and version_raw:
                first_claude_version = version_raw

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
                    events_for_current,
                    attachments_for_current,
                    previous_turn,
                    previous_non_synthetic_ts,
                    priced_turn_count,
                    diagnostics,
                    next_ts_raw=d.get("timestamp"),
                )
                turns.append(turn)
                finalized_keys.add(current_key)  # type: ignore[arg-type]
                previous_turn = turn
            # Rotate regardless of whether `current` was None: whatever
            # accumulated since it started (or, for the first turn,
            # since the file began) precedes the turn about to start.
            events_for_current = events_since_current
            attachments_for_current = attachments_since_current
            events_since_current = []
            attachments_since_current = []
            current = _new_pending(d, tool_use_names)
            current_key = key
            # Usage-limits addition (see module docstring): a usage-cap
            # hit lives on the synthetic assistant line's own text, which
            # never reaches events.classify_line (assistant lines aren't
            # events) -- synthesise the LIMIT_HIT event here instead, and
            # route it through events_since_current so it precedes the
            # *next* turn per the two-buffer scheme, not this synthetic one.
            if current.synthetic_kind in ("session_limit", "weekly_limit"):
                limit_detail: dict = {}
                if current.reset_minutes_of_day is not None:
                    limit_detail["reset_minutes_of_day"] = current.reset_minutes_of_day
                if current.reset_tz is not None:
                    limit_detail["reset_tz"] = current.reset_tz
                if current.reset_ts is not None:
                    limit_detail["reset_ts"] = current.reset_ts
                limit_event = Event(
                    kind=EventKind.LIMIT_HIT,
                    subkind=current.synthetic_kind,
                    ts=current.ts_raw or None,
                    detail=limit_detail,
                )
                events.append(limit_event)
                events_since_current.append(limit_event)
                diagnostics.limit_hits += 1
            continue

        if line_type == "user":
            _accumulate_tool_results(d, tool_use_names, tool_result_chars, tool_result_calls, current)
        elif line_type == "agent-setting":
            value = d.get("agentSetting")
            if isinstance(value, str) and value:
                diagnostics.agent_settings[value] = diagnostics.agent_settings.get(value, 0) + 1
        elif line_type == "mode":
            value = d.get("mode")
            if isinstance(value, str) and value:
                diagnostics.modes[value] = diagnostics.modes.get(value, 0) + 1

        event = events_mod.classify_line(d)
        if event is None:
            diagnostics.ignored_line_types[line_type] = (
                diagnostics.ignored_line_types.get(line_type, 0) + 1
            )
            continue
        events.append(event)
        events_since_current.append(event)
        if line_type == "attachment":
            attachments_since_current.append(event.subkind or "")
        if event.kind == EventKind.UNKNOWN:
            diagnostics.ignored_line_types[line_type] = (
                diagnostics.ignored_line_types.get(line_type, 0) + 1
            )
        if event.kind == EventKind.ATTACHMENT:
            subkind = event.subkind or ""
            diagnostics.attachment_catch_all[subkind] = (
                diagnostics.attachment_catch_all.get(subkind, 0) + 1
            )
        # Usage-limits addition (see module docstring): LIMIT_RESUME and
        # AGENT_TERMINATED both reach here via events_mod.classify_line
        # (unlike LIMIT_HIT, synthesised above from a synthetic assistant
        # line).
        if event.kind == EventKind.LIMIT_RESUME:
            diagnostics.limit_resumes += 1
        elif event.kind == EventKind.AGENT_TERMINATED:
            diagnostics.agents_terminated += 1

    if current is not None:
        turn, previous_non_synthetic_ts, priced_turn_count = _finalize_turn(
            current,
            events_for_current,
            attachments_for_current,
            previous_turn,
            previous_non_synthetic_ts,
            priced_turn_count,
            diagnostics,
        )
        turns.append(turn)

    # Events observed after the last finalised turn's assistant line have
    # no later turn to attach to (see the module docstring).
    diagnostics.trailing_events = len(events_since_current)

    diagnostics.lines = line_stats.lines
    diagnostics.unparsable_lines = line_stats.unparsable_lines
    diagnostics.truncated_final_line = line_stats.truncated_final_line
    diagnostics.oversized_lines = line_stats.oversized_lines
    diagnostics.distinct_turns = len(turns)

    # Batch C addition: the transcript's own first turn with a model is
    # the authoritative provider signal (see module docstring), taking
    # precedence over whatever meta.provider already held.
    for turn in turns:
        if turn.model:
            detected = detect_provider(turn.model)
            if detected:
                provider = detected
            break

    final_meta = replace(
        meta,
        entrypoint=meta.entrypoint if meta.entrypoint is not None else first_entrypoint,
        claude_version=meta.claude_version if meta.claude_version is not None else first_claude_version,
        provider=provider,
    )

    return TranscriptResult(
        meta=final_meta,
        turns=turns,
        events=events,
        diagnostics=diagnostics,
        tool_result_chars=tool_result_chars,
        tool_result_calls=tool_result_calls,
    )


#: Capture-improvements addition (A6): top-level keys this module actually
#: looks up, per raw line ``type``, kept alongside the parser so
#: ``probe.compare_with_parser`` can audit real transcripts for keys the
#: parser never reads without re-deriving the parser's own control flow.
#: Base keys are read for *every* line regardless of type: ``type``/
#: ``uuid`` (dedup, in ``parse_transcript``'s main loop) and
#: ``entrypoint``/``version`` (first-seen capture, also in the main loop,
#: before any type dispatch). A type not listed here (every other
#: ``events._IGNORABLE_TYPES`` member, plus the ``file-history-*``/
#: ``artifact-*`` prefix families) is read no further than those four
#: base keys -- ``classify_line`` returns ``None`` for them before even
#: computing ``timestamp``/``message``.
#:
#: ``user``/``system``/``attachment``/``queue-operation`` all reach
#: ``classify_line``, which unconditionally reads ``timestamp`` and
#: ``message`` (via ``_user_str_content``) before any type-specific
#: check -- except ``attachment``/``queue-operation``, which always
#: return via their own unconditional catch-all (checks 11 and 10) before
#: classify_line's later, type-unguarded ``origin`` read; ``system``
#: (when its ``subtype`` matches none of the earlier checks) and ``user``
#: can both fall through as far as that ``origin`` read, so both list it.
_BASE_READ_KEYS = frozenset({"type", "uuid", "entrypoint", "version"})

READ_KEYS: dict[str, frozenset[str]] = {
    "assistant": _BASE_READ_KEYS
    | frozenset(
        {
            "message",
            "requestId",
            "timestamp",
            "effort",
            "perTurnEffort",
            "attributionMcpServer",
            "attributionMcpTool",
            "attributionSkill",
            "isApiErrorMessage",
            "quotaLimits",
        }
    ),
    "user": _BASE_READ_KEYS
    | frozenset(
        {
            "timestamp",
            "message",
            "isCompactSummary",
            "toolDenialKind",
            "origin",
            "isMeta",
            "promptSource",
            "permissionMode",
        }
    ),
    "system": _BASE_READ_KEYS
    | frozenset(
        {
            "timestamp",
            "message",
            "subtype",
            "compactMetadata",
            "error",
            "retryAttempt",
            "retryInMs",
            "source",
            "originalModel",
            "fallbackModel",
            "origin",
        }
    ),
    "attachment": _BASE_READ_KEYS | frozenset({"timestamp", "message", "attachment", "rendered"}),
    "queue-operation": _BASE_READ_KEYS | frozenset({"timestamp", "message", "operation"}),
    "agent-setting": _BASE_READ_KEYS | frozenset({"agentSetting"}),
    "mode": _BASE_READ_KEYS | frozenset({"mode"}),
}


__all__ = ["parse_transcript", "detect_provider", "set_salt", "load_or_create_salt", "READ_KEYS"]
