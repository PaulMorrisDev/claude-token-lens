"""Plan-window usage logging (WP6): turn a pasted ``get_usage`` result, or
the statusline's own ``rate_limits`` payload, into deduped rows in a small
CSV log.

``get_usage`` is a desktop-only MCP tool Claude can call inside a session
but this CLI cannot poll on its own (see the plan's "Compaction and usage
log" section) — so the two ways a usage sample reaches this module are:
a person pasting its JSON output through ``log-usage`` by hand, or
``statusline.py`` forwarding the ``rate_limits`` object it already reads
on every status-line refresh. :func:`parse_usage_json` accepts either
shape (see its docstring for the exact fields each one carries) and
normalises both down to the same flat row shape.

Deviation from the plan, reported rather than made silently (project
convention — see ``model.py``'s module docstring): the plan states the
statusline's ``rate_limits`` shape precisely (``five_hour``/``seven_day``/
``spend_limit``, each with ``used_percentage``/``resets_at``) but only
says the desktop ``get_usage`` tool "is callable only by Claude inside a
session" without publishing its JSON shape. :func:`parse_usage_json`
therefore also accepts the *same* three window names at the top level of
the payload (no ``rate_limits`` wrapper) as the ``get_usage`` shape, and
tolerates a couple of likely field-name variants (``utilization``/
``percent_used`` for ``used_percentage``; an epoch number for
``resets_at``) — this is this module's own reasonable guess at the
un-published shape, not a restatement of a documented contract, and
should be corrected against a real pasted sample the first time one is
available.

SIG-5: the log is written unconditionally on every statusline refresh
(capture opt-in or not), so left alone it grows forever. Two fixes for
that: :func:`_read_existing_keys` -- called on every :func:`append_rows`
-- now scans only a bounded tail of the file rather than loading it
whole (see its own docstring), and :func:`prune_usage_log` drops rows
older than a retention window, wired into ``serve``'s watcher tick and
the ``capture prune`` command next to ``signals.prune`` and
``config.prune_capture_log``.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import model as model_mod

#: The three usage windows this module understands, per the plan's
#: ``rate_limits.{five_hour, seven_day, spend_limit}``.
WINDOW_NAMES = ("five_hour", "seven_day", "spend_limit")

#: CSV columns, in file order, per the WP6 brief.
CSV_FIELDS = ("logged_at", "session_id", "window", "used_percentage", "resets_at", "source")

#: SIG-5: how far back :func:`_read_existing_keys` scans for its dedupe
#: set, instead of loading the whole (unboundedly growing) log on every
#: single ``append_rows`` call -- the same tail-bytes figure and
#: reasoning as ``statusline._last_context_window_key``.
_TAIL_BYTES = 64 * 1024

_USED_PERCENTAGE_KEYS = ("used_percentage", "usedPercentage", "utilization", "percent_used")
_RESETS_AT_KEYS = ("resets_at", "resetsAt", "reset_at")


# -- config dir -------------------------------------------------------------


def _default_config_dir() -> Path:
    """``~/.claude/token-lens``, or ``$CLAUDE_CONFIG_DIR/token-lens`` when
    that env var moves the whole config tree elsewhere. Mirrors
    ``pricing._default_token_lens_dir`` and ``discovery.projects_root`` —
    each module in this package keeps its own copy of this small lookup
    rather than sharing one, matching the project's existing convention.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base) if base else (Path.home() / ".claude")
    return root / "token-lens"


def resolve_config_dir(cli_arg: str | Path | None = None) -> Path:
    """``--config-dir`` wins; else ``$CLAUDE_CONFIG_DIR/token-lens``; else
    ``~/.claude/token-lens``.
    """
    if cli_arg:
        return Path(cli_arg)
    return _default_config_dir()


def default_usage_log_path(config_dir: str | Path | None = None) -> Path:
    return resolve_config_dir(config_dir) / "usage-log.csv"


# -- parsing ------------------------------------------------------------


def _normalize_resets_at(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return (
                datetime.fromtimestamp(float(value), tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value:
        return value
    return None


def _extract_used_percentage(d: dict) -> float | None:
    for key in _USED_PERCENTAGE_KEYS:
        if key in d and d[key] is not None:
            try:
                return float(d[key])
            except (TypeError, ValueError):
                return None
    return None


def _extract_resets_at(d: dict) -> str | None:
    for key in _RESETS_AT_KEYS:
        if key in d:
            return _normalize_resets_at(d[key])
    return None


def _extract_model(d: dict) -> str | None:
    model = d.get("model")
    if isinstance(model, str):
        return model
    if isinstance(model, dict):
        for key in ("display_name", "id", "name"):
            value = model.get(key)
            if isinstance(value, str):
                return value
    return None


def _extract_session_id(d: dict) -> str | None:
    session_id = d.get("session_id")
    return session_id if isinstance(session_id, str) else None


def _row_from_window_dict(
    window: str, window_data: object, session_id: str | None, model: str | None, source: str
) -> dict | None:
    if not isinstance(window_data, dict):
        return None
    used_percentage = _extract_used_percentage(window_data)
    if used_percentage is None:
        return None
    return {
        "session_id": session_id,
        "window": window,
        "used_percentage": used_percentage,
        "resets_at": _extract_resets_at(window_data),
        "model": model,
        "source": source,
    }


def _rows_from_object(d: dict) -> list[dict]:
    if not isinstance(d, dict):
        return []

    # Already-flat row, e.g. round-tripped from this module's own CSV or
    # an export: pass it through (re-validating/normalising) rather than
    # looking for a nested window object that won't be there.
    if isinstance(d.get("window"), str) and d["window"] in WINDOW_NAMES:
        used_percentage = _extract_used_percentage(d)
        if used_percentage is None:
            return []
        return [
            {
                "session_id": _extract_session_id(d),
                "window": d["window"],
                "used_percentage": used_percentage,
                "resets_at": _extract_resets_at(d),
                "model": _extract_model(d),
                "source": d.get("source") if isinstance(d.get("source"), str) else None,
            }
        ]

    session_id = _extract_session_id(d)
    model = _extract_model(d)

    rate_limits = d.get("rate_limits")
    if isinstance(rate_limits, dict):
        # The statusline shape: windows live under "rate_limits".
        windows_source = rate_limits
        source = "statusline"
    else:
        # The (unpublished) desktop get_usage shape: windows are assumed
        # to live at the top level instead — see the module docstring's
        # deviation note.
        windows_source = d
        source = "get_usage"

    rows = []
    for window in WINDOW_NAMES:
        row = _row_from_window_dict(window, windows_source.get(window), session_id, model, source)
        if row is not None:
            rows.append(row)
    return rows


def parse_usage_json(text: str) -> list[dict]:
    """Parse ``text`` (raw JSON, as read from stdin) into a list of flat
    usage rows: ``{session_id, window, used_percentage, resets_at, model,
    source}``. Accepts a single object (either shape described in the
    module docstring) or a JSON array of them. Never raises — malformed
    JSON, an unexpected top-level type, or a window entry missing
    ``used_percentage`` all degrade to that entry (or the whole call)
    contributing no rows, rather than an exception.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []

    if isinstance(data, list):
        rows: list[dict] = []
        for item in data:
            if isinstance(item, dict):
                rows.extend(_rows_from_object(item))
        return rows
    if isinstance(data, dict):
        return _rows_from_object(data)
    return []


# -- CSV append / dedupe -------------------------------------------------


def _dedupe_key(row: dict) -> tuple:
    used = row.get("used_percentage")
    try:
        used_key = float(used) if used not in (None, "") else None
    except (TypeError, ValueError):
        used_key = used
    return (row.get("session_id") or None, row.get("window") or None, row.get("resets_at") or None, used_key)


def _read_existing_keys(csv_path: Path) -> set[tuple]:
    """The dedupe keys already on file, scanned from only the final
    :data:`_TAIL_BYTES` of ``csv_path`` rather than the whole thing.

    SIG-5: this used to load the entire log into memory on every single
    :func:`append_rows` call -- i.e. every statusline refresh, against a
    file that only ever grows -- the same unbounded-read problem
    ``statusline._last_context_window_key`` had (see its docstring for
    the identical fix and reasoning). A window's dedupe key
    (``session_id``, ``window``, ``resets_at``, ``used_percentage``)
    only changes when that window's ``resets_at`` rolls over, so the
    same key repeats on nearly every refresh in between -- a bounded
    tail almost always still contains it; the rare miss just means one
    row that could have been deduped gets written again, a bounded cost
    for a diagnostic log, not a correctness bug.
    """
    if not csv_path.exists():
        return set()
    try:
        size = csv_path.stat().st_size
        with open(csv_path, "rb") as fh:
            fh.seek(max(0, size - _TAIL_BYTES))
            tail = fh.read()
    except OSError:
        return set()

    text = tail.decode("utf-8", errors="replace")
    reader = csv.DictReader(text.split("\n"), fieldnames=CSV_FIELDS, restkey="_extra")
    keys: set[tuple] = set()
    for row in reader:
        if row.get("logged_at") == "logged_at":
            continue  # the header row, if it landed inside the tail window
        keys.add(_dedupe_key(row))
    return keys


def append_rows(
    csv_path: str | Path,
    rows: list[dict],
    *,
    source: str = "manual",
    now: datetime | None = None,
) -> int:
    """Append ``rows`` to ``csv_path`` (creating it with a header if
    absent), skipping any row whose ``(session_id, window, resets_at,
    used_percentage)`` already exists in the file or earlier in this same
    call. Returns the number of rows actually written.

    ``source`` is the fallback recorded in the ``source`` column for a row
    that doesn't already carry its own (``parse_usage_json`` tags every
    row it produces, so this is mainly for hand-built rows in tests).
    """
    csv_path = Path(csv_path)
    existing_keys = _read_existing_keys(csv_path)
    is_new_file = not csv_path.exists()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    logged_at = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")

    added = 0
    seen_this_call: set[tuple] = set()
    with open(csv_path, "a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if is_new_file:
            writer.writeheader()
        for row in rows:
            key = _dedupe_key(row)
            if key in existing_keys or key in seen_this_call:
                continue
            writer.writerow(
                {
                    "logged_at": logged_at,
                    "session_id": row.get("session_id") or "",
                    "window": row.get("window") or "",
                    "used_percentage": row.get("used_percentage"),
                    "resets_at": row.get("resets_at") or "",
                    "source": row.get("source") or source,
                }
            )
            seen_this_call.add(key)
            added += 1
    return added


def load_usage_log(csv_path: str | Path) -> list[dict]:
    """Every row in ``csv_path``, in file order, with ``used_percentage``
    parsed back to ``float`` where possible. Returns ``[]`` when the file
    doesn't exist — the log is optional, never required.

    ``restkey="_extra"`` (fix for review finding 6, defence-in-depth
    alongside ``statusline._ensure_ground_truth_header``'s primary fix):
    this module's own header only ever names :data:`CSV_FIELDS`, but a
    row written by ``statusline.py`` carries additional trailing
    ground-truth columns. Without an explicit ``restkey``,
    ``csv.DictReader`` bins every one of those extra fields under a
    literal ``None`` key, which is awkward to detect and easy to trip
    over accidentally; naming it ``"_extra"`` instead keeps the row a
    well-formed ``dict`` and makes the overflow columns available (as a
    list) to a caller that wants them, without this module needing to
    know their names.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        return []
    rows: list[dict] = []
    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, restkey="_extra")
        for raw_row in reader:
            row = dict(raw_row)
            used = row.get("used_percentage")
            if used not in (None, ""):
                try:
                    row["used_percentage"] = float(used)
                except ValueError:
                    pass
            rows.append(row)
    return rows


def prune_usage_log(csv_path: str | Path, retention_days: int, now: datetime | None = None) -> int:
    """Drop every ``usage-log.csv`` row older than ``retention_days`` by
    its own ``logged_at`` column. Returns how many rows were removed; a
    missing file, or one with nothing to remove, is a no-op returning 0.

    SIG-5: unlike the capture signal files and ``capture-log.jsonl``
    (``signals.prune`` / ``config.prune_capture_log``, both run on every
    ``serve`` tick regardless of capture opt-in), this file was never
    pruned at all -- despite being written unconditionally on every
    statusline refresh, capture on or off (``statusline.main`` appends to
    it via both :func:`append_rows` and its own
    ``_append_context_window_row``). Mirrors
    :func:`~claude_token_lens.config.prune_capture_log`'s shape: keep the
    header line as-is (whatever width it happens to be -- this function
    doesn't care how many trailing ground-truth columns a row carries,
    only its first column), keep any row whose ``logged_at`` parses and
    falls within the window, drop the rest, rewrite atomically via a
    temp file plus ``os.replace`` (the same pattern
    ``statusline._ensure_ground_truth_header`` already uses for this same
    file). A row whose ``logged_at`` is missing or doesn't parse is
    dropped along with the rest -- never written by this module or
    ``statusline.py``, but a prune pass is also a chance to repair the
    file, not just trim it.
    """
    csv_path = Path(csv_path)
    try:
        with open(csv_path, "r", encoding="utf-8", newline="") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return 0
    if len(lines) < 2:
        return 0

    header, body = lines[0], lines[1:]
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    kept = []
    for line in body:
        row = next(csv.reader([line]), None)
        if not row:
            continue
        try:
            ts = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        except (ValueError, IndexError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            kept.append(line)

    removed = len(body) - len(kept)
    if removed == 0:
        return 0

    tmp_path = csv_path.with_name(f"{csv_path.name}.tmp-{os.getpid()}")
    with open(tmp_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(header + "\n")
        for line in kept:
            fh.write(line + "\n")
    os.replace(tmp_path, csv_path)
    return removed


# -- report section: latest-per-window + optional regression --------------


def _least_squares(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """Ordinary least squares ``y = slope*x + intercept``. ``None`` when
    there are fewer than 2 points or every ``x`` is identical (a vertical
    fit, undefined slope).
    """
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    intercept = mean_y - slope * mean_x
    return slope, intercept


_MIN_REGRESSION_SAMPLES = 3


def _regression_for_window(
    window_rows: list[dict], token_totals: dict[str, int]
) -> tuple[float, float, int, str | None] | None:
    """Fit "used_percentage per million new tokens" for the most recent
    reset period (grouped by ``resets_at``) in ``window_rows`` that has at
    least ``_MIN_REGRESSION_SAMPLES`` rows with a matching entry in
    ``token_totals`` (keyed by each row's ``logged_at``). Returns
    ``(slope_per_million, intercept, samples_used, resets_at)``, or
    ``None`` if no period qualifies.
    """
    by_period: dict[str, list[dict]] = {}
    order: list[str] = []
    for row in window_rows:
        period = row.get("resets_at") or ""
        if period not in by_period:
            by_period[period] = []
            order.append(period)
        by_period[period].append(row)

    for period in reversed(order):  # most recent period first
        period_rows = by_period[period]
        xs: list[float] = []
        ys: list[float] = []
        for row in period_rows:
            tokens = token_totals.get(row.get("logged_at", ""))
            if tokens is None:
                continue
            used = row.get("used_percentage")
            if used is None:
                continue
            try:
                xs.append(float(tokens) / 1_000_000.0)
                ys.append(float(used))
            except (TypeError, ValueError):
                continue
        if len(xs) < _MIN_REGRESSION_SAMPLES:
            continue
        fit = _least_squares(xs, ys)
        if fit is None:
            continue
        slope, intercept = fit
        return slope, intercept, len(xs), period or None
    return None


def build_section(rows: list[dict], token_totals_by_window: dict | None = None):
    """The "Usage windows" report section (key ``usage_windows``).

    ``rows`` is whatever :func:`load_usage_log` returned. When empty, the
    section still comes back with (empty) tables and a note — it must
    never be the reason a report fails to render.

    ``token_totals_by_window``, when given, maps a window name to
    ``{logged_at: cumulative_new_tokens_in_that_reset_period_at_that_
    sample}`` — see :func:`_regression_for_window`. This exact shape is
    proposed by this module (deviation note, see the module docstring):
    the plan only says "when token totals per window are supplied by
    WP10", without naming WP10's shape, since WP10 doesn't exist yet in
    this worktree. WP10 should conform to this shape or update this
    function.
    """
    latest_columns = [
        model_mod.Column(key="window", label="Window", kind="str"),
        model_mod.Column(key="latest_used_pct", label="Latest used %", kind="pct"),
        model_mod.Column(key="resets_at", label="Resets at", kind="str"),
        model_mod.Column(key="samples", label="Samples", kind="int"),
    ]
    regression_columns = [
        model_mod.Column(key="window", label="Window", kind="str"),
        model_mod.Column(key="slope", label="% per million tokens", kind="float"),
        model_mod.Column(key="intercept", label="Intercept (%)", kind="float"),
        model_mod.Column(key="samples", label="Samples used", kind="int"),
        model_mod.Column(key="resets_at", label="Reset period", kind="str"),
    ]

    notes: list[str] = []
    if not rows:
        notes.append(
            "No usage-log rows found. Run `claude-token-lens log-usage` after "
            "pasting a get_usage result, or install the statusline logger "
            "(`claude-token-lens statusline --print-install-fragment`)."
        )
        return model_mod.Section(
            key="usage_windows",
            title="Usage windows",
            tables=[
                model_mod.Table(name="usage_windows_latest", title="Latest usage", columns=latest_columns),
                model_mod.Table(
                    name="usage_windows_regression", title="Usage regression", columns=regression_columns
                ),
            ],
            notes=notes,
        )

    by_window: dict[str, list[dict]] = {name: [] for name in WINDOW_NAMES}
    for row in rows:
        window = row.get("window")
        if window in by_window:
            by_window[window].append(row)

    latest_rows: list[list] = []
    for window in WINDOW_NAMES:
        window_rows = by_window[window]
        if not window_rows:
            continue
        window_rows_sorted = sorted(window_rows, key=lambda r: r.get("logged_at") or "")
        latest = window_rows_sorted[-1]
        used = latest.get("used_percentage")
        try:
            used_val = float(used) if used not in (None, "") else None
        except (TypeError, ValueError):
            used_val = None
        latest_rows.append([window, used_val, latest.get("resets_at") or None, len(window_rows)])

    regression_rows: list[list] = []
    if token_totals_by_window:
        for window in WINDOW_NAMES:
            window_rows = by_window[window]
            if not window_rows:
                continue
            totals = token_totals_by_window.get(window)
            if not totals:
                notes.append(f"No token totals supplied for {window!r}; regression skipped.")
                continue
            window_rows_sorted = sorted(window_rows, key=lambda r: r.get("logged_at") or "")
            fit = _regression_for_window(window_rows_sorted, totals)
            if fit is None:
                notes.append(
                    f"Fewer than {_MIN_REGRESSION_SAMPLES} usage samples with matching "
                    f"token totals in the same reset period for {window!r}; "
                    "regression skipped."
                )
                continue
            slope, intercept, samples_used, period = fit
            regression_rows.append([window, slope, intercept, samples_used, period])
    else:
        notes.append(
            "No token totals supplied (WP10 provides these); the empirical "
            "% per million tokens regression is skipped."
        )

    return model_mod.Section(
        key="usage_windows",
        title="Usage windows",
        tables=[
            model_mod.Table(
                name="usage_windows_latest",
                title="Latest usage",
                columns=latest_columns,
                rows=latest_rows,
            ),
            model_mod.Table(
                name="usage_windows_regression",
                title="Usage regression",
                columns=regression_columns,
                rows=regression_rows,
            ),
        ],
        notes=notes,
    )


# -- CLI entry point ------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="claude-token-lens log-usage")
    parser.add_argument("--config-dir", default=None, help="default: ~/.claude/token-lens")
    parser.add_argument("--source", default="manual")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Read a ``get_usage``/statusline JSON payload from stdin, append its
    rows to ``<config-dir>/usage-log.csv``, and print how many were added.
    Malformed or empty stdin adds zero rows rather than failing.
    """
    argv = sys.argv[1:] if argv is None else list(argv)
    args = _parse_args(argv)

    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""

    rows = parse_usage_json(raw)
    csv_path = default_usage_log_path(args.config_dir)
    added = append_rows(csv_path, rows, source=args.source)
    print(f"{added} row(s) added to {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "WINDOW_NAMES",
    "CSV_FIELDS",
    "resolve_config_dir",
    "default_usage_log_path",
    "parse_usage_json",
    "append_rows",
    "load_usage_log",
    "prune_usage_log",
    "build_section",
    "main",
]
