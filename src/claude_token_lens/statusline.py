"""Live status-line renderer (Feature 4, plan v0.2): one line for Claude
Code's ``statusLine`` setting answering "should I send the next message
now or lose the cache?"

Invoked via ``python -m claude_token_lens.statusline`` (both the Windows
and POSIX fragments in :func:`print_install_fragment` use ``-m``, not a
path — unlike ``hooks/snapshot-config.py`` this module is never copied out
on its own, so it imports from the rest of the package freely).

Contract (plan Appendix, "Statusline and live cache countdown" +
"Hook and statusline commands on Windows"): Claude Code writes a JSON
payload to stdin on every status-line refresh. The fields this module
reads, all tolerated when absent (per-field ``isinstance`` checks, no
required keys):

- ``model.display_name`` — not currently rendered into the line itself
  (the plan's own worked example omits it: ``ctx 143k | cache 92% | 5m
  TTL expires in 4m12s | 5h 37% | 7d 12%``), but the field is accepted
  without erroring so a future line format can add it back.
- ``context_window.used_tokens`` — the current session's token count.
- ``cost.total_cost_usd`` — accepted, not currently rendered (see above).
- ``rate_limits.{five_hour,seven_day}.used_percentage`` — plan usage
  window percentages.
- ``prompt_cache`` — cache hit-ratio source; the plan doesn't publish its
  exact shape (see :func:`_fmt_cache`'s deviation note below).
- ``transcript_path`` — read incrementally (last 64 KB only, scanned
  backwards) to find the last assistant turn's timestamp, for the TTL
  countdown.

S1-context-budget addition: when the payload's ``context_window`` object
also carries a numeric ``used_tokens``, this module appends a *second*,
independent row to the same usage-log CSV that :mod:`tools.log_usage`
already writes ``rate_limits`` rows to — the statusline payload is the
only place this tool ever sees the model's own live context-window
accounting (used tokens, the window's size, and wherever the payload
names its autocompact threshold), and :mod:`context_budget`'s
``context_budget_statusline``/``context_budget_autocompact`` tables need
it as ground truth alongside their own chars/4 estimates.

Rather than changing :data:`tools.log_usage.CSV_FIELDS` (a fixed
six-column contract other readers already depend on), this module
appends three new **trailing** columns directly with the stdlib
``csv`` module, in this fixed order, after the existing six
(``logged_at, session_id, window, used_percentage, resets_at, source``):

7. ``context_window_used_tokens`` -- ``context_window.used_tokens``.
8. ``context_window_size`` -- ``context_window.context_window_size``,
   falling back to ``context_window.total_tokens`` (the payload's own
   size key is not documented; both spellings are accepted).
9. ``context_window_autocompact_threshold`` -- the first numeric field
   on ``context_window`` whose key contains ``"autocompact"``
   (case-insensitive; the exact key name isn't documented either).

The row's own ``window`` column is the sentinel ``"context_window"`` (never
one of :data:`tools.log_usage.WINDOW_NAMES`, so a plain
``log_usage.load_usage_log`` read of the file is unaffected) and its
``used_percentage`` column carries ``context_window.used_percentage``
when present. A row is appended only when it differs from the last
``"context_window"`` row already in the file (same dedupe intent as
``log_usage.append_rows``, reimplemented locally since that function's
own dedupe key doesn't cover these new columns).

A file written before this addition existed has only six columns per
row; :func:`context_budget.load_context_window_rows` (the read-side
companion, in ``context_budget.py`` — not this module, which is
write-only) tolerates that by treating a short row as carrying no
context-window data rather than raising.

S1-exports addition: real ``prompt_cache`` ground truth. A research pass
against Claude Code v2.1.251+/v2.1.260+ confirmed the shape the plan's
own worked example (``cache 92%``) could only guess at — this module now
prefers that ground truth over the old estimate, and only falls back to
the estimate when a payload carries no usable ``prompt_cache``:

- **Fields used, confirmed from the research capture**: ``warm`` (bool),
  ``ttl`` (``"5m"``/``"1h"``), ``expires_at`` (epoch seconds), ``misses``
  (a running counter), ``last_miss_cause.causes`` (a list of strings,
  first element used), ``recache_tokens_if_cold``. ``miss_causes`` is
  accepted on the wire but not currently rendered or logged (no column
  needs it yet). Other confirmed-but-unused fields (``caching_observed``,
  ``requests``, ``expected_rebuilds``, ``hit_ratio``, ``cache_write_tokens``,
  ``miss_recache_tokens``, ``last_miss_at``) are simply ignored.
- **Line segment**: ``cache warm 5m 03:12`` (a ``MM:SS`` countdown to
  ``expires_at``) when warm, else ``cache cold`` with an optional
  trailing ``recache ~12k tokens`` when ``recache_tokens_if_cold`` is
  present. When the payload carries no usable ``prompt_cache`` at all,
  falls back to the old estimate, now labelled ``cache est`` and driven
  by a *transcript-derived* TTL hint rather than the ``effective_ttl_s``
  argument's own numeric value (see :func:`_fmt_cache_estimate`'s
  deviation note below) — ``effective_ttl_s`` is kept only as an on/off
  gate (``None`` disables the estimate segment entirely), preserving
  ``resolve_effective_ttl``'s existing meaning for every other caller.
- **Deviation (mapping choice, not published anywhere)**: the short
  cause tokens logged in ``cache_last_miss_cause`` are this module's own
  allowlist over the documented ``last_miss_cause.causes`` enum —
  ``tools_changed`` -> ``"tools"``, ``system_prompt_changed`` ->
  ``"sysprompt"``, ``ttl_expired_5m`` -> ``"ttl"``, ``likely_server_side``
  -> ``"server"``, anything else -> ``"other"``.
- **Estimate's TTL hint (not a deviation -- confirmed against this
  codebase's own parser)**: with no ``prompt_cache`` at all, the
  estimate's TTL is read from the transcript's own last assistant line
  rather than guessed from config/payload: a positive
  ``message.usage.cache_creation.ephemeral_1h_input_tokens`` implies 1h,
  else 5m. This exact nesting (``d["message"]["usage"]["cache_creation"]
  ["ephemeral_1h_input_tokens"]``) is what ``parse.py``'s own
  ``_new_pending`` already reads to populate ``Turn.cc_1h`` (see its
  comment there), so this is read straight off a real, already-parsed
  transcript field rather than guessed.
- **Trailing CSV columns 10-15** (after the three S1-context-budget
  columns above, so the file now has 15 columns total): ``cache_warm``
  (``0``/``1``), ``cache_ttl_s``, ``cache_expires_in_s`` (computed at
  log time, so it is *not* part of the dedupe key below), ``cache_misses``,
  ``cache_last_miss_cause`` (the short token above), and
  ``cache_recache_tokens_if_cold``. A row is written when *either* the
  context-window values or the cache values (or both) are present and
  differ from the last ground-truth row already on file; per the task
  spec, a change in ``cache_warm`` or ``cache_misses`` alone now also
  counts as "differs" even if the context-window columns are unchanged
  (see :func:`_last_context_window_key`).
- :func:`load_usage_log_ground_truth` is the tolerant reader for *both*
  generations of trailing columns at once (unlike
  ``context_budget.load_context_window_rows``, which only ever needed
  the context-window ones): it is what ``cli.py``'s ``report`` command
  and :func:`build_cache_ground_truth_table` (the ``usage`` section's
  new ``cache_ground_truth`` table, wired in by ``report.py`` via
  ``dataclasses.replace`` since ``usage.py`` itself is not writable for
  this work package) both use.

Never raises: :func:`main` wraps every step that touches the outside
world (stdin, the filesystem, config) in ``try``/``except Exception`` and
falls back to a minimal ``token-lens`` line on any failure, per the WP6
brief — a broken Python must never blank the status line, matching
``hooks/snapshot-config.py``'s "never fail a session start" contract for
the same underlying reason (this runs on every prompt, not just session
start).
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from .model import Column, Table
from .tools import log_usage

#: Printed by main() on any failure, and returned by render_status() when
#: the payload carries nothing renderable at all.
_FALLBACK_LINE = "token-lens"

#: Read only the last N bytes of a live transcript file for the TTL
#: countdown — "never load the file" (WP6 brief).
_TAIL_BYTES = 64 * 1024

_DEFAULT_TTL_S = 300

#: Seconds -> the human label used in the TTL segment, e.g. "5m TTL
#: expires in 4m12s". Anything else falls back to "{n}s TTL ...".
_TTL_LABELS = {300: "5m", 3600: "1h"}


# -- formatting helpers ---------------------------------------------------


def _fmt_ctx(context_window: object) -> str | None:
    if not isinstance(context_window, dict):
        return None
    used = context_window.get("used_tokens")
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    return f"ctx {round(used / 1000.0)}k"


#: Allowlist mapping ``last_miss_cause.causes[0]`` -> the short token
#: logged/rendered for it; anything else (including a value not in this
#: map) becomes "other". This mapping is this module's own choice (see
#: the module docstring's deviation note), not published anywhere.
_MISS_CAUSE_ALLOWLIST = {
    "tools_changed": "tools",
    "system_prompt_changed": "sysprompt",
    "ttl_expired_5m": "ttl",
    "likely_server_side": "server",
}


def _map_miss_cause(cause: object) -> str | None:
    if not isinstance(cause, str) or not cause:
        return None
    return _MISS_CAUSE_ALLOWLIST.get(cause, "other")


def _numeric(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return float(value)


def _format_mmss(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def _fmt_cache_ground_truth(prompt_cache: dict, now: datetime) -> str | None:
    """"cache warm 5m 03:12" / "cache cold recache ~12k tokens": the real
    ``prompt_cache`` ground truth (see the module docstring). Returns
    ``None`` when ``prompt_cache`` doesn't carry a boolean ``warm`` at
    all, so callers can fall back to the estimate instead.
    """
    warm = prompt_cache.get("warm")
    if not isinstance(warm, bool):
        return None

    if not warm:
        segment = "cache cold"
        recache = _numeric(prompt_cache.get("recache_tokens_if_cold"))
        if recache is not None:
            segment += f" recache ~{round(recache / 1000.0)}k tokens"
        return segment

    ttl_raw = prompt_cache.get("ttl")
    if isinstance(ttl_raw, str) and ttl_raw:
        ttl_label = ttl_raw
    else:
        parsed = _parse_ttl_value(ttl_raw)
        ttl_label = _TTL_LABELS.get(parsed, f"{parsed}s") if parsed is not None else "?"

    expires_at = _numeric(prompt_cache.get("expires_at"))
    if expires_at is None:
        return f"cache warm {ttl_label}"
    now_ts = (now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)).timestamp()
    remaining = expires_at - now_ts
    return f"cache warm {ttl_label} {_format_mmss(remaining)}"


def _ttl_hint_from_assistant_line(d: dict) -> int:
    """1h (3600) when the given last-assistant-line dict reports a
    positive ``message.usage.cache_creation.ephemeral_1h_input_tokens``,
    else 5m (300) -- see the module docstring's deviation note about this
    nesting."""
    message = d.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    cache_creation = usage.get("cache_creation") if isinstance(usage, dict) else None
    ephemeral_1h = cache_creation.get("ephemeral_1h_input_tokens") if isinstance(cache_creation, dict) else None
    value = _numeric(ephemeral_1h)
    if value is not None and value > 0:
        return 3600
    return _DEFAULT_TTL_S


def _fmt_cache_estimate(payload: dict, now: datetime, effective_ttl_s: int | None) -> str | None:
    """"cache est 5m 04:12": the pre-ground-truth fallback, used only
    when the payload carries no usable ``prompt_cache`` (see
    :func:`_fmt_cache_ground_truth`). ``effective_ttl_s`` is an on/off
    gate only (``None`` skips this segment entirely) -- the countdown's
    own TTL comes from :func:`_ttl_hint_from_assistant_line` instead, per
    the module docstring's deviation note.
    """
    if effective_ttl_s is None:
        return None
    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    d = _last_assistant_line(transcript_path)
    if d is None:
        return None
    ts_raw = d.get("timestamp")
    if not isinstance(ts_raw, str) or not ts_raw:
        return None
    try:
        last_ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)

    ttl_hint_s = _ttl_hint_from_assistant_line(d)
    label = _TTL_LABELS.get(ttl_hint_s, f"{ttl_hint_s}s")
    remaining = ttl_hint_s - (now_utc - last_ts).total_seconds()
    if remaining <= 0:
        return f"cache est {label} expired"
    return f"cache est {label} {_format_mmss(remaining)}"


def _fmt_cache_segment(payload: dict, now: datetime, effective_ttl_s: int | None) -> str | None:
    """The single cache/TTL segment: ground truth when the payload
    carries a usable ``prompt_cache``, else the transcript-derived
    estimate (see the module docstring)."""
    prompt_cache = payload.get("prompt_cache")
    if isinstance(prompt_cache, dict):
        ground_truth = _fmt_cache_ground_truth(prompt_cache, now)
        if ground_truth is not None:
            return ground_truth
    return _fmt_cache_estimate(payload, now, effective_ttl_s)


def _fmt_rate(rate_limits: object, key: str, label: str) -> str | None:
    if not isinstance(rate_limits, dict):
        return None
    window = rate_limits.get(key)
    if not isinstance(window, dict):
        return None
    pct = window.get("used_percentage")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    return f"{label} {round(pct)}%"


def _windows_long_path(path: Path) -> str:
    """Mirror ``jsonl._windows_long_path``'s \\\\?\\ prefixing (not
    imported directly: this module must stay resilient even if that
    private helper's shape ever changes, and the logic is a couple of
    lines).
    """
    text = str(path)
    if os.name != "nt" or len(text) < 255:
        return text
    resolved = str(path.resolve())
    if resolved.startswith("\\\\?\\"):
        return resolved
    if resolved.startswith("\\\\"):
        return "\\\\?\\UNC\\" + resolved.lstrip("\\")
    return "\\\\?\\" + resolved


def _last_assistant_line(transcript_path: str) -> dict | None:
    """The last ``type=assistant`` line, parsed, found by scanning only
    the final ``_TAIL_BYTES`` of ``transcript_path``, backwards. A
    truncated first line inside that tail window (the seek landed mid
    line) simply fails to parse as JSON and is skipped like any other bad
    line — if no assistant line is found in the tail at all, this returns
    ``None`` rather than reading further back (the file is never loaded
    in full, per the WP6 brief).
    """
    try:
        path = Path(transcript_path)
        size = path.stat().st_size
        with open(_windows_long_path(path), "rb") as fh:
            fh.seek(max(0, size - _TAIL_BYTES))
            tail = fh.read()
    except OSError:
        return None

    text = tail.decode("utf-8", errors="replace")
    # ``str.split("\n")`` rather than ``str.splitlines()``: JSONL is
    # newline-delimited by definition, and ``splitlines()`` also breaks on
    # ``\r``, ``\x0b``, ``\x1c``-``\x1e``, U+2028/U+2029 and friends — any
    # of which can appear *inside* a JSON string value (e.g. tool output
    # embedded in a message) without ending the record. Splitting on those
    # too would shear one JSONL line into several fragments, each of which
    # then fails to parse as JSON and gets silently skipped, hiding a real
    # assistant timestamp that happened to sit near such a byte.
    for raw_line in reversed(text.split("\n")):
        line = raw_line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("type") != "assistant":
            continue
        return d
    return None


def _last_assistant_ts(transcript_path: str) -> datetime | None:
    """The last assistant line's ``timestamp``, via :func:`_last_assistant_line`."""
    d = _last_assistant_line(transcript_path)
    if d is None:
        return None
    ts_raw = d.get("timestamp")
    if not isinstance(ts_raw, str) or not ts_raw:
        return None
    try:
        return datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# -- effective TTL resolution ---------------------------------------------


def _parse_ttl_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("5m", "300", "300s"):
            return 300
        if low in ("1h", "3600", "3600s"):
            return 3600
        try:
            return int(low)
        except ValueError:
            return None
    return None


def _read_default_ttl_from_config(config_dir: Path | None) -> int | None:
    if config_dir is None:
        return None
    config_path = Path(config_dir) / "config.toml"
    try:
        raw = config_path.read_bytes()
    except OSError:
        return None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    return _parse_ttl_value(data.get("default_ttl"))


def resolve_effective_ttl(payload: dict, config_dir: Path | None = None) -> int:
    """The TTL (seconds) to assume for the countdown: the payload's own
    ``prompt_cache.cache_ttl`` (or top-level ``cache_ttl``) if present,
    else ``<config_dir>/config.toml``'s ``default_ttl``, else 300s.
    """
    prompt_cache = payload.get("prompt_cache") if isinstance(payload, dict) else None
    if isinstance(prompt_cache, dict):
        parsed = _parse_ttl_value(prompt_cache.get("cache_ttl"))
        if parsed is not None:
            return parsed
    if isinstance(payload, dict):
        parsed = _parse_ttl_value(payload.get("cache_ttl"))
        if parsed is not None:
            return parsed
    config_value = _read_default_ttl_from_config(config_dir)
    if config_value is not None:
        return config_value
    return _DEFAULT_TTL_S


# -- render_status ----------------------------------------------------------


def render_status(payload: dict, now: datetime, effective_ttl_s: int | None) -> str:
    """Build the one-line status text, e.g. ``ctx 143k | cache warm 5m
    03:12 | 5h 37% | 7d 12%`` (ground truth) or ``ctx 143k | cache est 5m
    04:12 | 5h 37% | 7d 12%`` (estimate fallback, no ``prompt_cache`` on
    the payload). Every segment is optional — a missing/malformed field
    simply drops its segment rather than raising. Never prints message
    text; kept under 120 characters by construction (each segment is a
    handful of tokens). Returns :data:`_FALLBACK_LINE` when nothing at
    all could be rendered (an (almost) empty payload).
    """
    if not isinstance(payload, dict):
        payload = {}

    segments: list[str] = []
    ctx_seg = _fmt_ctx(payload.get("context_window"))
    if ctx_seg:
        segments.append(ctx_seg)
    cache_seg = _fmt_cache_segment(payload, now, effective_ttl_s)
    if cache_seg:
        segments.append(cache_seg)
    five_h_seg = _fmt_rate(payload.get("rate_limits"), "five_hour", "5h")
    if five_h_seg:
        segments.append(five_h_seg)
    seven_d_seg = _fmt_rate(payload.get("rate_limits"), "seven_day", "7d")
    if seven_d_seg:
        segments.append(seven_d_seg)

    if not segments:
        return _FALLBACK_LINE
    return " | ".join(segments)


# -- install fragment -------------------------------------------------------


def print_install_fragment() -> str:
    """The ``settings.json`` ``statusLine`` fragment to paste in, for
    Windows and POSIX (named ``print_...`` per the WP6 brief; like
    ``hooks/snapshot-config.py``'s ``hook_fragment_text``, it returns the
    text rather than printing it directly, so a caller can also test it).
    """
    windows_command = "py -3 -m claude_token_lens.statusline"
    posix_command = "python3 -m claude_token_lens.statusline"

    def _fragment(command: str) -> str:
        return json.dumps({"statusLine": {"type": "command", "command": command}}, indent=2)

    return (
        "Merge this into ~/.claude/settings.json (replaces any existing "
        '"statusLine" key):\n'
        "\n"
        "Windows:\n"
        f"{_fragment(windows_command)}\n"
        "\n"
        "POSIX (Linux/macOS):\n"
        f"{_fragment(posix_command)}"
    )


# -- context-window trailing columns (S1-context-budget) -------------------

#: Sentinel ``window`` value for a context-window row, distinct from
#: every name in ``log_usage.WINDOW_NAMES`` so a plain
#: ``log_usage.load_usage_log`` read of the file skips these rows.
_CONTEXT_WINDOW_SENTINEL = "context_window"


def _context_window_size(context_window: dict) -> float | None:
    size = _numeric(context_window.get("context_window_size"))
    if size is not None:
        return size
    return _numeric(context_window.get("total_tokens"))


def _autocompact_field(context_window: dict) -> float | None:
    """The first numeric value on ``context_window`` whose key contains
    "autocompact" (case-insensitive) -- the payload's own key name for
    this isn't documented (see the module docstring's deviation note)."""
    for key in sorted(context_window):
        if "autocompact" not in key.lower():
            continue
        value = _numeric(context_window.get(key))
        if value is not None:
            return value
    return None


def _context_window_row_values(payload: dict) -> tuple[str, float | None, float, float | None, float | None] | None:
    """``(session_id, used_percentage, used_tokens, size, autocompact)``,
    or ``None`` when ``context_window`` is missing/not a dict, or its
    ``used_tokens`` isn't numeric (nothing worth logging otherwise)."""
    context_window = payload.get("context_window")
    if not isinstance(context_window, dict):
        return None
    used_tokens = _numeric(context_window.get("used_tokens"))
    if used_tokens is None:
        return None
    session_id = payload.get("session_id")
    session_id = session_id if isinstance(session_id, str) else ""
    used_percentage = _numeric(context_window.get("used_percentage"))
    size = _context_window_size(context_window)
    autocompact = _autocompact_field(context_window)
    return (session_id, used_percentage, used_tokens, size, autocompact)


# -- cache ground-truth trailing columns (S1-exports) -----------------------


def _cache_row_values(
    payload: dict,
) -> tuple[float | None, int | None, float | None, float | None, str | None, float | None] | None:
    """``(warm, ttl_s, expires_at, misses, last_miss_cause, recache_tokens_if_cold)``
    from ``payload["prompt_cache"]``, or ``None`` when there is nothing at
    all worth logging (``prompt_cache`` missing/not a dict, or every one
    of these fields absent). ``warm`` is kept as ``0.0``/``1.0`` (not a
    bool) so it slots into the same numeric CSV/dedupe-key handling as
    every other value here.
    """
    prompt_cache = payload.get("prompt_cache")
    if not isinstance(prompt_cache, dict):
        return None

    warm_raw = prompt_cache.get("warm")
    warm = 1.0 if warm_raw is True else (0.0 if warm_raw is False else None)
    ttl_s = _parse_ttl_value(prompt_cache.get("ttl"))
    expires_at = _numeric(prompt_cache.get("expires_at"))
    misses = _numeric(prompt_cache.get("misses"))
    last_miss_cause = None
    causes_holder = prompt_cache.get("last_miss_cause")
    if isinstance(causes_holder, dict):
        causes = causes_holder.get("causes")
        if isinstance(causes, list) and causes:
            last_miss_cause = _map_miss_cause(causes[0])
    recache_tokens_if_cold = _numeric(prompt_cache.get("recache_tokens_if_cold"))

    if warm is None and ttl_s is None and expires_at is None and misses is None and last_miss_cause is None and recache_tokens_if_cold is None:
        return None
    return (warm, ttl_s, expires_at, misses, last_miss_cause, recache_tokens_if_cold)


def _parse_csv_number(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _last_context_window_key(csv_path: Path) -> tuple | None:
    """The dedupe key of the last row in ``csv_path`` whose ``window``
    column is :data:`_CONTEXT_WINDOW_SENTINEL`, or ``None`` if the file
    doesn't exist or carries no such row yet. Columns 9 (``cache_warm``)
    and 12 (``cache_misses``) are included per the S1-exports spec: a
    change in either alone counts as a new row even when every
    context-window column stays the same (see the module docstring)."""
    if not csv_path.exists():
        return None
    last_key: tuple | None = None
    try:
        with open(csv_path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            next(reader, None)  # header
            for row in reader:
                if len(row) < 3 or row[2] != _CONTEXT_WINDOW_SENTINEL:
                    continue
                last_key = (
                    row[1] if len(row) > 1 else "",
                    _parse_csv_number(row[3]) if len(row) > 3 else None,
                    _parse_csv_number(row[6]) if len(row) > 6 else None,
                    _parse_csv_number(row[7]) if len(row) > 7 else None,
                    _parse_csv_number(row[8]) if len(row) > 8 else None,
                    _parse_csv_number(row[9]) if len(row) > 9 else None,
                    _parse_csv_number(row[12]) if len(row) > 12 else None,
                )
    except OSError:
        return None
    return last_key


def _append_context_window_row(csv_path: Path, payload: dict, now: datetime) -> None:
    """Append one ground-truth row to ``csv_path`` (see the module
    docstring for the full trailing-column contract): context-window
    values, cache values, or both. Skipped entirely when neither is
    present, and deduped against the last such row already on file.
    """
    context_values = _context_window_row_values(payload)
    cache_values = _cache_row_values(payload)
    if context_values is None and cache_values is None:
        return

    if context_values is not None:
        _, used_percentage, used_tokens, size, autocompact = context_values
    else:
        used_percentage = used_tokens = size = autocompact = None
    if cache_values is not None:
        cache_warm, cache_ttl_s, cache_expires_at, cache_misses, cache_last_miss_cause, cache_recache = cache_values
    else:
        cache_warm = cache_ttl_s = cache_expires_at = cache_misses = cache_recache = None
        cache_last_miss_cause = None

    session_id = payload.get("session_id")
    session_id = session_id if isinstance(session_id, str) else ""

    key = (session_id, used_percentage, used_tokens, size, autocompact, cache_warm, cache_misses)
    if key == _last_context_window_key(csv_path):
        return

    cache_expires_in_s: float | None = None
    if cache_expires_at is not None:
        now_ts = (now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)).timestamp()
        cache_expires_in_s = cache_expires_at - now_ts

    is_new_file = not csv_path.exists()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    logged_at = now.isoformat().replace("+00:00", "Z")

    with open(csv_path, "a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        if is_new_file:
            writer.writerow(
                list(log_usage.CSV_FIELDS)
                + [
                    "context_window_used_tokens",
                    "context_window_size",
                    "context_window_autocompact_threshold",
                    "cache_warm",
                    "cache_ttl_s",
                    "cache_expires_in_s",
                    "cache_misses",
                    "cache_last_miss_cause",
                    "cache_recache_tokens_if_cold",
                ]
            )
        writer.writerow(
            [
                logged_at,
                session_id,
                _CONTEXT_WINDOW_SENTINEL,
                used_percentage if used_percentage is not None else "",
                "",
                "statusline",
                used_tokens if used_tokens is not None else "",
                size if size is not None else "",
                autocompact if autocompact is not None else "",
                "" if cache_warm is None else int(cache_warm),
                cache_ttl_s if cache_ttl_s is not None else "",
                cache_expires_in_s if cache_expires_in_s is not None else "",
                cache_misses if cache_misses is not None else "",
                cache_last_miss_cause or "",
                cache_recache if cache_recache is not None else "",
            ]
        )


def load_usage_log_ground_truth(csv_path: str | Path) -> list[dict]:
    """Tolerant reader for *every* ground-truth trailing column this
    module writes -- both the S1-context-budget ``context_window_*``
    columns and the S1-exports ``cache_*`` columns -- as one dict per
    row: ``{"session_id", "context_window_used_percentage",
    "context_window_used_tokens", "context_window_size",
    "context_window_autocompact_threshold", "cache_warm", "cache_ttl_s",
    "cache_expires_in_s", "cache_misses", "cache_last_miss_cause",
    "cache_recache_tokens_if_cold"}``.

    Unlike :func:`context_budget.load_context_window_rows` (which only
    ever needed the context-window columns, and so skips a row lacking a
    numeric ``used_tokens``), this reader surfaces *any* ground-truth
    row -- context-only, cache-only, or both -- since ``cli.py``'s
    ``report`` command and :func:`build_cache_ground_truth_table` both
    need the cache-only rows a context-focused reader would drop. A
    missing column (an old-format row, or a row written before the
    cache columns existed) simply yields ``None`` for that key, matching
    ``context_budget.load_context_window_rows``'s own "short row -> no
    data, not an error" contract. Returns ``[]`` when the file doesn't
    exist.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        return []

    rows: list[dict] = []
    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            return []
        for raw in reader:
            if len(raw) < 3 or raw[2] != _CONTEXT_WINDOW_SENTINEL:
                continue

            def _at(index: int) -> float | None:
                return _parse_csv_number(raw[index]) if len(raw) > index else None

            cache_warm_raw = _at(9)
            cause_raw = raw[13].strip() if len(raw) > 13 and raw[13].strip() else None
            rows.append(
                {
                    "session_id": raw[1] if len(raw) > 1 else "",
                    "context_window_used_percentage": _at(3),
                    "context_window_used_tokens": _at(6),
                    "context_window_size": _at(7),
                    "context_window_autocompact_threshold": _at(8),
                    "cache_warm": None if cache_warm_raw is None else bool(cache_warm_raw),
                    "cache_ttl_s": _at(10),
                    "cache_expires_in_s": _at(11),
                    "cache_misses": _at(12),
                    "cache_last_miss_cause": cause_raw,
                    "cache_recache_tokens_if_cold": _at(14),
                }
            )
    return rows


def build_cache_ground_truth_table(usage_log_rows: list[dict] | None) -> Table:
    """Per-session ``cache_ground_truth`` table (registered by
    ``report.py`` next to the ``usage`` section, since ``usage.py`` is
    not writable for this work package -- see the module docstring):
    rows logged, warm share, peak miss counter, top miss causes, and
    mean recache-if-cold tokens, all built from
    :func:`load_usage_log_ground_truth`'s rows. A row lacking any cache
    data at all (``cache_warm`` is ``None``) is excluded -- it has
    nothing to contribute here even if it carries context-window data.
    """
    per_session: dict[str, dict] = {}
    for row in usage_log_rows or []:
        if row.get("cache_warm") is None:
            continue
        session_id = row.get("session_id") or ""
        bucket = per_session.setdefault(
            session_id,
            {"rows": 0, "warm": 0, "misses_max": 0.0, "cause_counts": {}, "recache_values": []},
        )
        bucket["rows"] += 1
        if row.get("cache_warm"):
            bucket["warm"] += 1
        misses = row.get("cache_misses")
        if isinstance(misses, (int, float)):
            bucket["misses_max"] = max(bucket["misses_max"], misses)
        cause = row.get("cache_last_miss_cause")
        if cause:
            bucket["cause_counts"][cause] = bucket["cause_counts"].get(cause, 0) + 1
        recache = row.get("cache_recache_tokens_if_cold")
        if isinstance(recache, (int, float)):
            bucket["recache_values"].append(recache)

    rows_out: list[list] = []
    for session_id, bucket in sorted(per_session.items()):
        rows_count = bucket["rows"]
        warm_share = (100.0 * bucket["warm"] / rows_count) if rows_count else 0.0
        top_causes = sorted(bucket["cause_counts"].items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        top_causes_str = ", ".join(f"{cause}:{count}" for cause, count in top_causes)
        recache_values = bucket["recache_values"]
        mean_recache = (sum(recache_values) / len(recache_values)) if recache_values else None
        rows_out.append(
            [
                session_id,
                rows_count,
                warm_share,
                int(bucket["misses_max"]),
                top_causes_str,
                mean_recache,
            ]
        )

    return Table(
        name="cache_ground_truth",
        title="Cache ground truth (from statusline)",
        columns=[
            Column(key="session_id", label="Session", kind="str"),
            Column(key="rows_logged", label="Rows logged", kind="int"),
            Column(key="warm_share", label="Warm share", kind="pct"),
            Column(key="misses", label="Misses", kind="int"),
            Column(key="top_miss_causes", label="Top miss causes", kind="str"),
            Column(key="mean_recache_tokens_if_cold", label="Mean recache-if-cold", kind="tokens"),
        ],
        rows=rows_out,
        notes=[
            "Built from the statusline's prompt_cache ground-truth trailing "
            "columns in the usage-log CSV (see statusline.py's module "
            "docstring); sessions with no logged cache data are absent "
            "from this table.",
        ],
    )


# -- CLI entry point ------------------------------------------------------


def _safe_print(text: str) -> None:
    """``print()`` that never raises.

    Claude Code reads this process's stdout on every prompt refresh, so a
    print failure here (a closed/broken pipe if the host is already
    tearing down, or a console codepage that can't encode a character
    despite the ``errors="replace"`` reconfiguration below) must not
    surface as an exception -- that would blank the status line exactly
    like an unhandled error anywhere else in this module.
    """
    try:
        print(text)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    """Read the status-line JSON payload from stdin, print one line, and
    (when ``rate_limits`` is present) append a deduped usage-log row.
    Always exits 0 and never lets an exception escape.
    """
    try:
        # Windows consoles / redirected pipes can default to a narrow
        # codepage (e.g. cp1252) that raises UnicodeEncodeError on
        # anything outside it. Force UTF-8 with lossy replacement instead
        # of failing outright; older runtimes without ``reconfigure``
        # (or a stdout that isn't a real TextIOWrapper, e.g. under some
        # test harnesses) are tolerated too.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in ("--print-install-fragment", "--install"):
        _safe_print(print_install_fragment())
        return 0

    try:
        raw = sys.stdin.read()
    except Exception:
        _safe_print(_FALLBACK_LINE)
        return 0

    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    try:
        config_dir = log_usage.resolve_config_dir(None)
        effective_ttl_s = resolve_effective_ttl(payload, config_dir)
        now = datetime.now(timezone.utc)
        line = render_status(payload, now, effective_ttl_s)
    except Exception:
        _safe_print(_FALLBACK_LINE)
        return 0

    _safe_print(line)

    try:
        rate_limits = payload.get("rate_limits")
        if isinstance(rate_limits, dict):
            rows = log_usage.parse_usage_json(json.dumps(payload))
            if rows:
                csv_path = config_dir / "usage-log.csv"
                log_usage.append_rows(csv_path, rows, source="statusline")
    except Exception:
        pass

    try:
        context_window_csv_path = config_dir / "usage-log.csv"
        _append_context_window_row(context_window_csv_path, payload, datetime.now(timezone.utc))
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "render_status",
    "resolve_effective_ttl",
    "print_install_fragment",
    "main",
    "load_usage_log_ground_truth",
    "build_cache_ground_truth_table",
]
