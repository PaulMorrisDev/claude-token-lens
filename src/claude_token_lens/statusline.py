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

Never raises: :func:`main` wraps every step that touches the outside
world (stdin, the filesystem, config) in ``try``/``except Exception`` and
falls back to a minimal ``token-lens`` line on any failure, per the WP6
brief — a broken Python must never blank the status line, matching
``hooks/snapshot-config.py``'s "never fail a session start" contract for
the same underlying reason (this runs on every prompt, not just session
start).
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

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


def _fmt_cache(prompt_cache: object) -> str | None:
    """"cache NN%": the prefix-cache hit ratio.

    Deviation (reported rather than made silently, see ``model.py``'s
    module docstring for this project's convention): the plan names
    ``prompt_cache`` as a stdin field without publishing its shape. This
    accepts a direct ``hit_percentage``/``hit_rate`` (the latter treated
    as a 0-1 fraction) if present, else computes
    ``cache_read / (cache_read + cache_creation + input)`` from whichever
    of ``cache_read_tokens``/``cache_creation_tokens``/``input_tokens`` are
    present — this module's own reasonable guess, to be corrected against
    a real payload sample.
    """
    if not isinstance(prompt_cache, dict):
        return None
    hit_pct = prompt_cache.get("hit_percentage")
    if isinstance(hit_pct, (int, float)) and not isinstance(hit_pct, bool):
        return f"cache {round(hit_pct)}%"
    hit_rate = prompt_cache.get("hit_rate")
    if isinstance(hit_rate, (int, float)) and not isinstance(hit_rate, bool):
        return f"cache {round(hit_rate * 100)}%"

    read = prompt_cache.get("cache_read_tokens")
    creation = prompt_cache.get("cache_creation_tokens")
    if not (isinstance(read, (int, float)) and isinstance(creation, (int, float))):
        return None
    if isinstance(read, bool) or isinstance(creation, bool):
        return None
    input_tokens = prompt_cache.get("input_tokens")
    input_val = input_tokens if isinstance(input_tokens, (int, float)) and not isinstance(input_tokens, bool) else 0
    denom = read + creation + input_val
    if denom <= 0:
        return None
    return f"cache {round(100.0 * read / denom)}%"


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


def _last_assistant_ts(transcript_path: str) -> datetime | None:
    """The last ``type=assistant`` line's ``timestamp`` found by scanning
    only the final ``_TAIL_BYTES`` of ``transcript_path``, backwards. A
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
        ts_raw = d.get("timestamp")
        if not isinstance(ts_raw, str) or not ts_raw:
            continue
        try:
            return datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    minutes, secs = divmod(total, 60)
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _fmt_ttl(payload: dict, now: datetime, effective_ttl_s: int | None) -> str | None:
    if effective_ttl_s is None:
        return None
    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    last_ts = _last_assistant_ts(transcript_path)
    if last_ts is None:
        return None
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    remaining = effective_ttl_s - (now_utc - last_ts).total_seconds()
    label = _TTL_LABELS.get(effective_ttl_s, f"{effective_ttl_s}s")
    if remaining <= 0:
        return f"{label} TTL expired"
    return f"{label} TTL expires in {_format_duration(remaining)}"


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
    """Build the one-line status text, e.g. ``ctx 143k | cache 92% | 5m
    TTL expires in 4m12s | 5h 37% | 7d 12%``. Every segment is optional —
    a missing/malformed field simply drops its segment rather than
    raising. Returns :data:`_FALLBACK_LINE` when nothing at all could be
    rendered (an (almost) empty payload).
    """
    if not isinstance(payload, dict):
        payload = {}

    segments: list[str] = []
    ctx_seg = _fmt_ctx(payload.get("context_window"))
    if ctx_seg:
        segments.append(ctx_seg)
    cache_seg = _fmt_cache(payload.get("prompt_cache"))
    if cache_seg:
        segments.append(cache_seg)
    ttl_seg = _fmt_ttl(payload, now, effective_ttl_s)
    if ttl_seg:
        segments.append(ttl_seg)
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

    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "render_status",
    "resolve_effective_ttl",
    "print_install_fragment",
    "main",
]
