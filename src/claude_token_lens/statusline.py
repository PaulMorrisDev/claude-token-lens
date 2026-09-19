"""Live status-line renderer (Feature 4, plan v0.2): one line for Claude
Code's ``statusLine`` setting answering "should I send the next message
now or lose the cache?"

Invoked via ``python -m claude_token_lens.statusline`` when this package is
an ordinary installed/checked-out package -- unlike ``hooks/snapshot-
config.py`` this module is never copied out on its own, so it imports from
the rest of the package freely. That ``-m`` form does **not** work when
running from the ``.pyz`` build (``scripts/build-pyz.py``): the package
lives inside the zip archive, not on ``sys.path``, so ``python -m
claude_token_lens.statusline`` raises ``No module named
claude_token_lens.statusline``. :func:`print_install_fragment` therefore
mirrors :func:`~claude_token_lens.installer.plan_service_install`'s own
pyz-awareness (see that module's ``detect_pyz_path``/``_serve_argv``): when
this process was itself launched from a ``.pyz`` (or one is passed
explicitly), the fragment instead names the archive directly --
``"<python>" "<abs path to .pyz>" statusline`` -- rather than the ``-m``
form.

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
  first element used), ``recache_tokens_if_cold``. ``miss_causes`` (a
  dict of cumulative per-cause counts) is now also read and logged --
  see the ``cache_miss_causes`` (fix for review finding 5) point below.
  Other confirmed-but-unused fields (``caching_observed``, ``requests``,
  ``expected_rebuilds``, ``hit_ratio``, ``cache_write_tokens``,
  ``miss_recache_tokens``, ``last_miss_at``) are simply ignored. The
  research capture's own §2 notes that every field here reflects only
  the *main conversation* -- subagent requests are excluded from
  ``prompt_cache`` entirely, so a session that spends most of its
  tokens in subagent turns will show a cache picture that looks
  healthier (or emptier) than the session's total token spend would
  suggest. This module does not attempt to correct for that; it is a
  property of the payload, not a bug in how this module reads it.
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
- **Trailing CSV columns 10-16** (after the three S1-context-budget
  columns above, so the file now has 16 columns total): ``cache_warm``
  (``0``/``1``), ``cache_ttl_s``, ``cache_expires_in_s`` (computed at
  log time, so it is *not* part of the dedupe key below), ``cache_misses``,
  ``cache_last_miss_cause`` (the short token above),
  ``cache_recache_tokens_if_cold``, and ``cache_miss_causes`` (fix for
  review finding 5, see below). A row is written when *either* the
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
- **``cache_miss_causes`` (fix for review finding 5)**: ``top_miss_causes``
  used to be built by incrementing a counter for
  ``prompt_cache.last_miss_cause`` once per *logged row* -- but that
  field is sticky (it stays set across refreshes until the next miss),
  so one real miss got re-counted on every quiet subsequent turn,
  contradicting the ``misses`` column right beside it. The wire's own
  ``prompt_cache.miss_causes`` field is documented (research capture §2)
  as *cumulative counts for the session, per cause* -- exactly what
  ``top_miss_causes`` should summarise -- so it is now read, mapped
  through the same short-token allowlist as ``last_miss_cause``, and
  persisted as this compact ``cause:count;cause:count`` column (sorted,
  sanitised the same way every other echoed string field is -- see the
  statusline-hardening note below). ``build_cache_ground_truth_table``
  now takes ``top_miss_causes`` from the *last* row's cumulative
  snapshot per session (an overwrite, not a per-row accumulation), with
  a documented fallback for a log carrying no ``cache_miss_causes`` data
  at all (old-format rows, or a payload that never sent the field):
  count ``cache_last_miss_cause`` only on a row whose ``cache_misses``
  increased over the previous row for that session, exactly the
  finding's own suggested fallback.
- **Statusline hardening (fix for review findings 3/4)**: the line was
  promised "kept under 120 characters by construction" and "one line",
  but neither was actually enforced -- an adversarial or just
  differently-shaped ``prompt_cache`` (``expires_at``/
  ``recache_tokens_if_cold`` as an extreme float, epoch **milliseconds**
  instead of the documented epoch seconds, or a ``ttl`` string carrying
  an embedded newline) could blow the line past 300+ characters or print
  a second line outright. Now: ``ttl`` is only ever echoed when it
  matches ``^\\d+[smh]$`` (else the numeric/label fallback, else ``"?"``),
  every echoed string field (the sanitised ``ttl`` label, a miss-cause
  token) is further restricted to ``[A-Za-z0-9_.-]`` and capped at 16
  characters, ``expires_at`` above ``1e11`` is treated as epoch
  milliseconds (divided down), the warm countdown is clamped to
  ``[0, ttl_s or 3600]`` (and renders ``expiring`` rather than a
  clock-skew-stuck ``00:00`` once past zero -- nit 13), the cold-path
  recache-token estimate is capped at 10,000,000 before formatting, and
  :func:`render_status` bounds the whole assembled line to 120
  characters (truncating the cache segment first, since it's the one
  built from the least-trusted fields) and strips any embedded newline
  as a last resort.
- **``context_window`` field-name fallbacks**: the confirmed payload
  field names (research capture §2: ``total_input_tokens``,
  ``total_output_tokens``, ``context_window_size``, ``used_percentage``,
  ``remaining_percentage``, ``current_usage.*``) don't actually list
  ``used_tokens``, the key this module has read from the start (nit 16)
  -- so a real payload may never have populated the ``ctx NNk`` segment
  or the context-window trailing columns at all. Both now try, in
  order: ``used_tokens``, then ``total_input_tokens``, then the sum of
  ``current_usage.{input_tokens, cache_creation_input_tokens,
  cache_read_input_tokens}`` for the "used tokens" figure (see
  :func:`_context_window_used_tokens`); ``context_window_size``, then
  ``total_tokens``, then ``size`` for the window size
  (:func:`_context_window_size`); and ``used_percentage``, else
  ``100 - remaining_percentage``, for the percentage
  (:func:`_context_window_used_percentage`). This is this module's own
  reasonable guess at reconciling two partially-overlapping field-name
  lists, not a restatement of a single documented contract -- see the
  next point for how a real payload's actual shape gets recorded so a
  later release can settle it for good.
- **Payload key-name recording** (:func:`record_payload_keys`): every
  statusline invocation now writes the payload's own key names --
  recursively, dotted (e.g. ``prompt_cache.expires_at``), **names only,
  never values**, capped at 200 -- to
  ``<config_dir>/statusline-keys.json``, but only rewrites the file when
  the recorded set actually differs from what a real payload has been
  sending. This makes the real, currently-deployed statusline payload
  shape observable ground truth (see ``docs/exports.md`` and
  ``SECURITY.md`` for the "names only" privacy guarantee) instead of a
  one-off research capture that can drift as Claude Code's own payload
  evolves. Wrapped in ``try``/``except`` like every other filesystem step
  in :func:`main` -- it must never affect the printed line.

Never raises: :func:`main` wraps every step that touches the outside
world (stdin, the filesystem, config) in ``try``/``except Exception`` and
falls back to a minimal ``token-lens`` line on any failure, per the WP6
brief — a broken Python must never blank the status line, matching
``hooks/snapshot-config.py``'s "never fail a session start" contract for
the same underlying reason (this runs on every prompt, not just session
start).

v3-limits addition: two small, self-contained changes so a usage-cap hit
(or a near-hit) is visible live, not only after the fact in a report.

1. **Near-cap warning in the line itself**: :func:`_fmt_rate` now appends
   a trailing ``"!"`` to a ``5h``/``7d`` segment once that window's
   ``used_percentage`` clears :data:`_RATE_WARNING_PCT` (90%) — e.g.
   ``5h 92%!`` — giving the same live warning the plan's "should I send
   the next message now" framing already gives the cache segment, but for
   the account-wide cap a harness-level pause (see ``limits.py``'s module
   docstring) may follow shortly after. One extra character per segment
   can never itself push :func:`render_status` past :data:`_MAX_LINE_LEN`
   given every other segment's existing headroom, so no change was needed
   to the truncation logic.
2. **Tagging an exhausted row in the usage log**: :func:`main` now runs
   every row from ``log_usage.parse_usage_json`` through
   :func:`_tag_limit_hit_rows` before appending — a ``five_hour``/
   ``seven_day`` row read back at or above :data:`_LIMIT_HIT_EXHAUSTION_PCT`
   (100%) gets its ``source`` overridden to ``"limit_hit"`` instead of the
   usual ``"statusline"``. ``limits.csv_cross_check`` doesn't actually
   need this tag — it counts a CSV row as an exhaustion signal from
   ``used_percentage`` alone (see that function's own docstring) — but a
   human or another tool reading the raw CSV benefits from the row saying
   outright that this reading *was* a cap hit rather than merely
   "nearly exhausted". :data:`_LIMIT_HIT_EXHAUSTION_PCT` is a local
   constant rather than an import of ``limits.LimitThresholds`` (kept in
   sync by value, documented here) — the statusline's hot path stays free
   of any dependency whose own failure mode could threaten the "never
   raises" contract above.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from . import installer as installer_mod
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

# -- statusline hardening (fix for review findings 3/4) ----------------------

#: The whole assembled line is bounded to this many characters (see
#: render_status) -- the module docstring's "kept under 120 characters"
#: promise, now actually enforced rather than assumed from each
#: segment's own small size.
_MAX_LINE_LEN = 120

#: Every echoed string field (a sanitised ttl label, a miss-cause token)
#: is restricted to this character class and length.
_ECHO_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_.-]")
_MAX_ECHO_LEN = 16

#: A raw `ttl` string is only ever echoed verbatim when it matches this
#: shape (e.g. "5m", "300s") -- anything else (including a newline
#: injection attempt) falls back to the numeric/label path or "?".
_TTL_RAW_RE = re.compile(r"^\d+[smh]$")

#: A payload `expires_at` above this is treated as epoch milliseconds
#: rather than the documented epoch seconds (a real epoch-seconds value
#: for any date in this project's lifetime is comfortably below 1e11).
_EPOCH_MS_THRESHOLD = 1e11

#: Upper bound for the cold-path "recache ~Nk tokens" estimate, so a
#: wildly out-of-range payload value can't blow the line up.
_MAX_RECACHE_TOKENS = 10_000_000

#: Fallback cap (seconds) for the warm countdown when no numeric TTL is
#: known at all -- "clamp remaining to [0, ttl_s or 3600]".
_MAX_REMAINING_FALLBACK_S = 3600


def _sanitize_echo(text: str) -> str:
    """Strip an echoed string field down to ``[A-Za-z0-9_.-]``, capped at
    16 characters -- belt-and-suspenders against a malformed/hostile
    payload smuggling a newline or an oversized string into the status
    line (see the module docstring's statusline-hardening note)."""
    return _ECHO_SANITIZE_RE.sub("", text)[:_MAX_ECHO_LEN]


def _sanitize_ttl_label(ttl_raw: object) -> str:
    """The TTL label to render: ``ttl_raw`` itself, but only when it's a
    string shaped like ``^\\d+[smh]$`` (fix for review finding 4 -- the
    previous code echoed *any* string verbatim, so a ``ttl`` value
    containing a newline could print a second line); otherwise the
    numeric/label fallback :func:`_parse_ttl_value` + :data:`_TTL_LABELS`
    already used for a non-string ``ttl``, or ``"?"`` when nothing
    parses. Always passed through :func:`_sanitize_echo` as a final
    belt-and-suspenders step.
    """
    if isinstance(ttl_raw, str):
        stripped = ttl_raw.strip()
        if _TTL_RAW_RE.match(stripped):
            return _sanitize_echo(stripped)
    parsed = _parse_ttl_value(ttl_raw)
    if parsed is not None:
        return _sanitize_echo(_TTL_LABELS.get(parsed, f"{parsed}s"))
    return "?"


# -- formatting helpers ---------------------------------------------------


def _context_window_used_tokens(context_window: dict) -> float | None:
    """The "used tokens" figure, trying field names in the order the
    module docstring documents (nit 16 + the ``context_window``
    field-name-fallbacks note): ``used_tokens`` (this module's original
    guess), then ``total_input_tokens``, then the sum of
    ``current_usage.{input_tokens, cache_creation_input_tokens,
    cache_read_input_tokens}``."""
    used = _numeric(context_window.get("used_tokens"))
    if used is not None:
        return used
    used = _numeric(context_window.get("total_input_tokens"))
    if used is not None:
        return used
    current_usage = context_window.get("current_usage")
    if isinstance(current_usage, dict):
        parts = [
            _numeric(current_usage.get("input_tokens")),
            _numeric(current_usage.get("cache_creation_input_tokens")),
            _numeric(current_usage.get("cache_read_input_tokens")),
        ]
        numeric_parts = [p for p in parts if p is not None]
        if numeric_parts:
            return sum(numeric_parts)
    return None


def _context_window_used_percentage(context_window: dict) -> float | None:
    """``used_percentage``, else ``100 - remaining_percentage`` when only
    the latter is present (see the module docstring)."""
    used_percentage = _numeric(context_window.get("used_percentage"))
    if used_percentage is not None:
        return used_percentage
    remaining_percentage = _numeric(context_window.get("remaining_percentage"))
    if remaining_percentage is not None:
        return 100.0 - remaining_percentage
    return None


def _fmt_ctx(context_window: object) -> str | None:
    if not isinstance(context_window, dict):
        return None
    used = _context_window_used_tokens(context_window)
    if used is None:
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


def _format_cause_counts(counts: dict[str, int]) -> str:
    """``prompt_cache.miss_causes`` (already mapped through the short-token
    allowlist and summed per short token) as the compact
    ``cause:count;cause:count`` column persisted for review finding 5 --
    sorted for determinism, each token passed through
    :func:`_sanitize_echo`."""
    parts = [f"{_sanitize_echo(cause)}:{int(count)}" for cause, count in sorted(counts.items()) if cause]
    return ";".join(parts)


def _parse_cause_counts(text: str | None) -> dict[str, int]:
    """The inverse of :func:`_format_cause_counts`, tolerant of a blank/
    malformed column (an old-format row, or a corrupted field) by simply
    skipping any segment that doesn't parse."""
    if not text:
        return {}
    counts: dict[str, int] = {}
    for part in text.split(";"):
        if ":" not in part:
            continue
        cause, _, count_raw = part.partition(":")
        cause = cause.strip()
        if not cause:
            continue
        try:
            counts[cause] = int(count_raw)
        except ValueError:
            continue
    return counts


def _miss_causes_from_payload(prompt_cache: dict) -> dict[str, int]:
    """``prompt_cache.miss_causes`` mapped through the short-token
    allowlist and summed per short token (several raw causes can map to
    the same short token, e.g. anything unrecognised collapses to
    ``"other"``). Returns ``{}`` when the field is missing/not a dict."""
    raw = prompt_cache.get("miss_causes")
    if not isinstance(raw, dict):
        return {}
    counts: dict[str, int] = {}
    for raw_cause, raw_count in raw.items():
        mapped = _map_miss_cause(raw_cause)
        count = _numeric(raw_count)
        if mapped is None or count is None:
            continue
        counts[mapped] = counts.get(mapped, 0) + int(count)
    return counts


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

    Hardened per review findings 3/4 and nit 13 (see the module
    docstring's "Statusline hardening" note): the ``ttl`` label is only
    ever echoed verbatim when it matches ``^\\d+[smh]$``, an
    ``expires_at`` above :data:`_EPOCH_MS_THRESHOLD` is treated as epoch
    milliseconds, the cold-path recache estimate is capped at
    :data:`_MAX_RECACHE_TOKENS`, and the warm countdown is clamped to
    ``[0, ttl_s or _MAX_REMAINING_FALLBACK_S]`` -- once past zero this
    renders ``expiring`` rather than a clock-skew-stuck ``00:00``.
    """
    warm = prompt_cache.get("warm")
    if not isinstance(warm, bool):
        return None

    if not warm:
        segment = "cache cold"
        recache = _numeric(prompt_cache.get("recache_tokens_if_cold"))
        if recache is not None:
            recache = min(max(recache, 0.0), _MAX_RECACHE_TOKENS)
            segment += f" recache ~{round(recache / 1000.0)}k tokens"
        return segment

    ttl_raw = prompt_cache.get("ttl")
    ttl_label = _sanitize_ttl_label(ttl_raw)
    ttl_s = _parse_ttl_value(ttl_raw)

    expires_at = _numeric(prompt_cache.get("expires_at"))
    if expires_at is None:
        return f"cache warm {ttl_label}"
    if expires_at > _EPOCH_MS_THRESHOLD:
        expires_at = expires_at / 1000.0
    now_ts = (now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)).timestamp()
    raw_remaining = expires_at - now_ts
    if raw_remaining <= 0:
        return f"cache warm {ttl_label} expiring"
    cap = ttl_s or _MAX_REMAINING_FALLBACK_S
    remaining = min(raw_remaining, cap)
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


#: A rate-window segment gets a trailing "!" warning marker once its
#: used_percentage clears this bound -- close enough to the account-wide
#: cap that a harness-level pause (see limits.py's module docstring) may
#: follow shortly, distinct from the cap already being fully hit at 100%
#: (see :data:`_LIMIT_HIT_EXHAUSTION_PCT` below).
_RATE_WARNING_PCT = 90.0


def _fmt_rate(rate_limits: object, key: str, label: str) -> str | None:
    if not isinstance(rate_limits, dict):
        return None
    window = rate_limits.get(key)
    if not isinstance(window, dict):
        return None
    pct = window.get("used_percentage")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    segment = f"{label} {round(pct)}%"
    if pct >= _RATE_WARNING_PCT:
        segment += "!"
    return segment


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
    text. Returns :data:`_FALLBACK_LINE` when nothing at all could be
    rendered (an (almost) empty payload).

    Fix for review findings 3/4 (see the module docstring's "Statusline
    hardening" note): the assembled line is now actually bounded to
    :data:`_MAX_LINE_LEN` characters rather than merely assumed to be
    short -- the cache segment (built from the least-trusted payload
    fields) is truncated first, and dropped entirely if even an empty
    truncation wouldn't fit; any embedded newline is stripped as a
    last-resort guarantee that this never becomes a second line.
    """
    if not isinstance(payload, dict):
        payload = {}

    segments: list[str] = []
    cache_index: int | None = None

    ctx_seg = _fmt_ctx(payload.get("context_window"))
    if ctx_seg:
        segments.append(ctx_seg)
    cache_seg = _fmt_cache_segment(payload, now, effective_ttl_s)
    if cache_seg:
        cache_index = len(segments)
        segments.append(cache_seg)
    five_h_seg = _fmt_rate(payload.get("rate_limits"), "five_hour", "5h")
    if five_h_seg:
        segments.append(five_h_seg)
    seven_d_seg = _fmt_rate(payload.get("rate_limits"), "seven_day", "7d")
    if seven_d_seg:
        segments.append(seven_d_seg)

    if not segments:
        return _FALLBACK_LINE

    line = " | ".join(segments)
    if len(line) > _MAX_LINE_LEN and cache_index is not None:
        other_len = sum(len(s) for i, s in enumerate(segments) if i != cache_index)
        separators_len = 3 * max(0, len(segments) - 1)  # " | " between each pair
        budget = max(0, _MAX_LINE_LEN - other_len - separators_len)
        truncated_cache = segments[cache_index][:budget].rstrip()
        segments = list(segments)
        if truncated_cache:
            segments[cache_index] = truncated_cache
        else:
            del segments[cache_index]
        line = " | ".join(segments)

    line = line.replace("\n", " ").replace("\r", " ")
    return line[:_MAX_LINE_LEN]


# -- install fragment -------------------------------------------------------


def print_install_fragment(pyz_path: Path | None = None) -> str:
    """The ``settings.json`` ``statusLine`` fragment to paste in, for
    Windows and POSIX (named ``print_...`` per the WP6 brief; like
    ``hooks/snapshot-config.py``'s ``hook_fragment_text``, it returns the
    text rather than printing it directly, so a caller can also test it).

    ``pyz_path`` mirrors ``installer.plan_service_install``'s own
    parameter: defaults to :func:`~claude_token_lens.installer.detect_pyz_path`
    (``None`` unless this process was itself launched from a ``.pyz``) --
    pass one explicitly to force the ".pyz" fragment form regardless of how
    this call is running (as ``cli.py``'s ``install-service`` planning
    already does for the service action).

    When running from a ``.pyz`` archive, ``python -m
    claude_token_lens.statusline`` does not work -- the package lives
    inside the zip, not on ``sys.path`` (see the module docstring) -- so
    the fragment instead names the absolute archive path directly:
    ``"<python>" "<abs path to .pyz>" statusline``, the same
    ``[exe, str(pyz_path), *args]`` shape
    ``installer._serve_argv`` already uses for the logon-service action.
    Otherwise (an ordinary installed package/checkout) the fragment keeps
    the original ``-m`` form.
    """
    if pyz_path is None:
        pyz_path = installer_mod.detect_pyz_path()

    if pyz_path is not None:
        abs_pyz = str(Path(pyz_path).resolve())
        windows_command = f'py -3 "{abs_pyz}" statusline'
        posix_command = f'python3 "{abs_pyz}" statusline'
    else:
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

#: The full 16-column ground-truth header this module writes (fix for
#: review finding 6 -- see :func:`_ensure_ground_truth_header`). Kept as
#: a single source of truth so the "write a new file" and "upgrade an
#: old file" paths can't drift apart.
_GROUND_TRUTH_TRAILING_COLUMNS = (
    "context_window_used_tokens",
    "context_window_size",
    "context_window_autocompact_threshold",
    "cache_warm",
    "cache_ttl_s",
    "cache_expires_in_s",
    "cache_misses",
    "cache_last_miss_cause",
    "cache_recache_tokens_if_cold",
    "cache_miss_causes",
)
_GROUND_TRUTH_HEADER = list(log_usage.CSV_FIELDS) + list(_GROUND_TRUTH_TRAILING_COLUMNS)


def _ensure_ground_truth_header(csv_path: Path) -> None:
    """Fix for review finding 6: a usage-log CSV written before this
    module's trailing columns existed (or before ``cache_miss_causes``
    was added) has fewer columns than :data:`_GROUND_TRUTH_HEADER`. A
    plain append would then leave that file with two different row
    shapes forever, silently breaking any positional read (this
    module's own :func:`load_usage_log_ground_truth` tolerates a short
    row, but a naive ``csv.DictReader`` elsewhere would misalign).

    When the file exists and its header has fewer columns than the
    current writer, this rewrites the file *once*: read every row, pad
    each out to the new header's width with empty strings, then write
    the new header plus the padded rows to a temp file in the same
    directory and ``os.replace`` it over the original -- atomic on both
    POSIX and Windows, so a crash mid-rewrite never leaves a truncated
    file in place. A no-op when the file doesn't exist yet (the normal
    append path below creates it with the full header) or already has
    at least as many columns.
    """
    if not csv_path.exists():
        return
    try:
        with open(csv_path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            rows = list(reader)
    except OSError:
        return
    if not rows:
        return
    header = rows[0]
    if len(header) >= len(_GROUND_TRUTH_HEADER):
        return

    width = len(_GROUND_TRUTH_HEADER)
    padded_rows = [row + [""] * (width - len(row)) if len(row) < width else row for row in rows[1:]]

    tmp_path = csv_path.with_name(f"{csv_path.name}.tmp-{os.getpid()}")
    with open(tmp_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(_GROUND_TRUTH_HEADER)
        writer.writerows(padded_rows)
    os.replace(tmp_path, csv_path)


def _context_window_size(context_window: dict) -> float | None:
    """The window's size, trying ``context_window_size``, then
    ``total_tokens``, then ``size`` (nit 16 + field-name-fallbacks
    note)."""
    size = _numeric(context_window.get("context_window_size"))
    if size is not None:
        return size
    size = _numeric(context_window.get("total_tokens"))
    if size is not None:
        return size
    return _numeric(context_window.get("size"))


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
    used_tokens = _context_window_used_tokens(context_window)
    if used_tokens is None:
        return None
    session_id = payload.get("session_id")
    session_id = session_id if isinstance(session_id, str) else ""
    used_percentage = _context_window_used_percentage(context_window)
    size = _context_window_size(context_window)
    autocompact = _autocompact_field(context_window)
    return (session_id, used_percentage, used_tokens, size, autocompact)


# -- cache ground-truth trailing columns (S1-exports) -----------------------


def _cache_row_values(
    payload: dict,
) -> tuple[float | None, int | None, float | None, float | None, str | None, float | None, str | None] | None:
    """``(warm, ttl_s, expires_at, misses, last_miss_cause,
    recache_tokens_if_cold, miss_causes_str)`` from
    ``payload["prompt_cache"]``, or ``None`` when there is nothing at all
    worth logging (``prompt_cache`` missing/not a dict, or every one of
    these fields absent). ``warm`` is kept as ``0.0``/``1.0`` (not a
    bool) so it slots into the same numeric CSV/dedupe-key handling as
    every other value here. ``miss_causes_str`` is the fix for review
    finding 5 (see the module docstring): ``prompt_cache.miss_causes``,
    mapped and summed by :func:`_miss_causes_from_payload`, formatted by
    :func:`_format_cause_counts`.
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
    miss_causes_counts = _miss_causes_from_payload(prompt_cache)
    miss_causes_str = _format_cause_counts(miss_causes_counts) if miss_causes_counts else None

    if (
        warm is None
        and ttl_s is None
        and expires_at is None
        and misses is None
        and last_miss_cause is None
        and recache_tokens_if_cold is None
        and miss_causes_str is None
    ):
        return None
    return (warm, ttl_s, expires_at, misses, last_miss_cause, recache_tokens_if_cold, miss_causes_str)


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
    doesn't exist or carries no such row yet. Columns 9 (``cache_warm``),
    12 (``cache_misses``) and 15 (``cache_miss_causes``) are included per
    the S1-exports spec (and review finding 5's cumulative-counts
    column): a change in any of them alone counts as a new row even when
    every context-window column stays the same (see the module
    docstring)."""
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
                    row[15] if len(row) > 15 else "",
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
        (
            cache_warm,
            cache_ttl_s,
            cache_expires_at,
            cache_misses,
            cache_last_miss_cause,
            cache_recache,
            cache_miss_causes_str,
        ) = cache_values
    else:
        cache_warm = cache_ttl_s = cache_expires_at = cache_misses = cache_recache = None
        cache_last_miss_cause = cache_miss_causes_str = None

    session_id = payload.get("session_id")
    session_id = session_id if isinstance(session_id, str) else ""

    key = (
        session_id,
        used_percentage,
        used_tokens,
        size,
        autocompact,
        cache_warm,
        cache_misses,
        cache_miss_causes_str or "",
    )
    _ensure_ground_truth_header(csv_path)
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
            writer.writerow(_GROUND_TRUTH_HEADER)
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
                cache_miss_causes_str or "",
            ]
        )


def load_usage_log_ground_truth(csv_path: str | Path) -> list[dict]:
    """Tolerant reader for *every* ground-truth trailing column this
    module writes -- both the S1-context-budget ``context_window_*``
    columns and the S1-exports ``cache_*`` columns -- as one dict per
    row: ``{"logged_at", "session_id", "context_window_used_percentage",
    "context_window_used_tokens", "context_window_size",
    "context_window_autocompact_threshold", "cache_warm", "cache_ttl_s",
    "cache_expires_in_s", "cache_misses", "cache_last_miss_cause",
    "cache_recache_tokens_if_cold", "cache_miss_causes"}``. ``logged_at``
    is the row's own first column (the shared ``log_usage.CSV_FIELDS``
    timestamp) -- ``cli.py``'s ``report``/``monthly-report`` commands
    filter on it to scope these rows to a reporting window (review
    finding 8).

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
            miss_causes_raw = raw[15].strip() if len(raw) > 15 and raw[15].strip() else None
            rows.append(
                {
                    "logged_at": raw[0] if len(raw) > 0 else "",
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
                    "cache_miss_causes": miss_causes_raw,
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

    ``top_miss_causes`` is the fix for review finding 5: it comes from
    the *last* row's cumulative ``cache_miss_causes`` snapshot for that
    session (an overwrite, not summed across rows -- the field is
    already a running total), falling back to counting
    ``cache_last_miss_cause`` once per row whose ``cache_misses``
    increased over the previous row, only for a session whose rows never
    carry ``cache_miss_causes`` data at all (old-format rows, or a
    payload that never sent the field).
    """
    per_session: dict[str, dict] = {}
    for row in usage_log_rows or []:
        if row.get("cache_warm") is None:
            continue
        session_id = row.get("session_id") or ""
        bucket = per_session.setdefault(
            session_id,
            {
                "rows": 0,
                "warm": 0,
                "misses_max": 0.0,
                "recache_values": [],
                "last_miss_causes": None,
                "prev_misses": None,
                "fallback_causes": {},
            },
        )
        bucket["rows"] += 1
        if row.get("cache_warm"):
            bucket["warm"] += 1
        misses = row.get("cache_misses")
        if isinstance(misses, (int, float)):
            bucket["misses_max"] = max(bucket["misses_max"], misses)

        # Fix for review finding 5: prefer the wire's own cumulative
        # ``cache_miss_causes`` snapshot (an overwrite per row, since it's
        # already a running total -- see the module docstring), and only
        # fall back to counting the sticky ``cache_last_miss_cause`` once
        # per genuine miss (a row whose ``cache_misses`` increased over
        # the previous row for this session) when no row in the whole
        # session ever carried ``cache_miss_causes`` data at all.
        miss_causes_raw = row.get("cache_miss_causes")
        if miss_causes_raw:
            bucket["last_miss_causes"] = miss_causes_raw
        else:
            cause = row.get("cache_last_miss_cause")
            prev_misses = bucket["prev_misses"]
            if cause and isinstance(misses, (int, float)) and (prev_misses is None or misses > prev_misses):
                bucket["fallback_causes"][cause] = bucket["fallback_causes"].get(cause, 0) + 1
        if isinstance(misses, (int, float)):
            bucket["prev_misses"] = misses

        recache = row.get("cache_recache_tokens_if_cold")
        if isinstance(recache, (int, float)):
            bucket["recache_values"].append(recache)

    rows_out: list[list] = []
    for session_id, bucket in sorted(per_session.items()):
        rows_count = bucket["rows"]
        warm_share = (100.0 * bucket["warm"] / rows_count) if rows_count else 0.0
        if bucket["last_miss_causes"]:
            cause_counts = _parse_cause_counts(bucket["last_miss_causes"])
        else:
            cause_counts = bucket["fallback_causes"]
        top_causes = sorted(cause_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
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
            "Warm share is the percentage of *logged rows* (statusline "
            "refreshes) that were warm, not a share of wall-clock session "
            "time -- refreshes are not evenly spaced, so a session with "
            "many quick warm refreshes and one long cold stretch can show "
            "a high warm share despite spending most of its wall-clock "
            "time cold, and vice versa (nit 20).",
        ],
    )


# -- payload key-name recording ---------------------------------------------

#: Hard cap on the number of dotted key names recorded per invocation
#: (see :func:`record_payload_keys`) -- a defensive bound against a
#: pathological/hostile payload with a huge or deeply-nested key set.
_MAX_RECORDED_KEYS = 200

_PAYLOAD_KEYS_FILENAME = "statusline-keys.json"


def _collect_dotted_keys(value: object, prefix: str, out: set[str]) -> None:
    """Recursively collect every dict key under ``value`` as a dotted
    name (e.g. ``prompt_cache.expires_at``) into ``out``, stopping once
    :data:`_MAX_RECORDED_KEYS` distinct names have been collected.
    **Names only -- never values** (see the module docstring's
    "Payload key-name recording" note and ``SECURITY.md``): this walks
    the payload's structure, not its contents, so nothing a user typed
    or any token/session/path value can end up in the recorded set.
    """
    if len(out) >= _MAX_RECORDED_KEYS:
        return
    if isinstance(value, dict):
        for key, sub_value in value.items():
            if not isinstance(key, str):
                continue
            dotted = f"{prefix}.{key}" if prefix else key
            out.add(dotted)
            if len(out) >= _MAX_RECORDED_KEYS:
                return
            _collect_dotted_keys(sub_value, dotted, out)
            if len(out) >= _MAX_RECORDED_KEYS:
                return
    elif isinstance(value, list):
        # A list's own items aren't named, but a dict inside one (e.g. a
        # future ``causes: [...]``-shaped list of objects) still has keys
        # worth recording under the same dotted prefix.
        for item in value:
            if isinstance(item, dict):
                _collect_dotted_keys(item, prefix, out)
                if len(out) >= _MAX_RECORDED_KEYS:
                    return


def record_payload_keys(payload: dict, config_dir: Path) -> None:
    """Write the payload's own key names -- recursively, dotted, names
    only, capped at :data:`_MAX_RECORDED_KEYS` -- to
    ``<config_dir>/statusline-keys.json``, but only when the recorded set
    actually differs from what's already stored there (per the locked
    decision: this makes a real, currently-deployed payload's shape
    observable ground truth for a later release, without rewriting the
    file on every single invocation). Never raises -- callers (``main``)
    already wrap this in ``try``/``except`` like every other filesystem
    step, but this function is defensive on its own account too, since
    it's also directly unit-testable.
    """
    if not isinstance(payload, dict):
        return
    keys: set[str] = set()
    _collect_dotted_keys(payload, "", keys)
    sorted_keys = sorted(keys)

    keys_path = Path(config_dir) / _PAYLOAD_KEYS_FILENAME
    try:
        existing_raw = keys_path.read_text(encoding="utf-8")
        existing = json.loads(existing_raw)
        existing_keys = existing.get("keys") if isinstance(existing, dict) else None
    except (OSError, ValueError):
        existing_keys = None

    if existing_keys == sorted_keys:
        return

    keys_path.parent.mkdir(parents=True, exist_ok=True)
    keys_path.write_text(
        json.dumps({"keys": sorted_keys}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# -- v3-limits: tagging an exhausted usage-log row --------------------------

#: A ``five_hour``/``seven_day`` usage-log row at or above this
#: used_percentage is tagged ``source="limit_hit"`` by
#: :func:`_tag_limit_hit_rows` (see the module docstring). Matches
#: ``limits.LimitThresholds.csv_exhaustion_pct``'s default by value --
#: kept as a local constant rather than an import so this hot-path module
#: never depends on another module's config-reading code (see the module
#: docstring's "never raises" contract).
_LIMIT_HIT_EXHAUSTION_PCT = 100.0

#: Windows a "limit_hit" source tag can apply to -- matches
#: ``limits.CSV_WINDOWS``.
_LIMIT_HIT_WINDOWS = ("five_hour", "seven_day")


def _tag_limit_hit_rows(rows: list[dict]) -> list[dict]:
    """Return ``rows`` (from ``log_usage.parse_usage_json``) with a
    qualifying row's ``source`` overridden to ``"limit_hit"``: a
    ``five_hour``/``seven_day`` window read back at or above
    :data:`_LIMIT_HIT_EXHAUSTION_PCT` used. Every other row is passed
    through unchanged. Rows are shallow-copied rather than mutated in
    place, so the caller's own list is never modified underneath it.
    """
    tagged: list[dict] = []
    for row in rows:
        used = row.get("used_percentage")
        is_exhausted = (
            row.get("window") in _LIMIT_HIT_WINDOWS
            and isinstance(used, (int, float))
            and not isinstance(used, bool)
            and used >= _LIMIT_HIT_EXHAUSTION_PCT
        )
        tagged.append(dict(row, source="limit_hit") if is_exhausted else row)
    return tagged


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

    # ``--config-dir PATH`` (forwarded by cli.py's _cmd_statusline, which
    # parses it as one of the "common" flags every subcommand accepts --
    # see --help). Parsed by hand rather than via argparse to match this
    # module's existing minimal argv handling above.
    config_dir_arg: str | None = None
    if "--config-dir" in argv:
        i = argv.index("--config-dir")
        if i + 1 < len(argv):
            config_dir_arg = argv[i + 1]

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
        config_dir = log_usage.resolve_config_dir(config_dir_arg)
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
                rows = _tag_limit_hit_rows(rows)
                csv_path = config_dir / "usage-log.csv"
                log_usage.append_rows(csv_path, rows, source="statusline")
    except Exception:
        pass

    try:
        context_window_csv_path = config_dir / "usage-log.csv"
        _append_context_window_row(context_window_csv_path, payload, datetime.now(timezone.utc))
    except Exception:
        pass

    try:
        record_payload_keys(payload, config_dir)
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
    "record_payload_keys",
]
