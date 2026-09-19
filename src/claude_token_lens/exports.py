"""Aggregate, privacy-safe exports of a corpus (S1-exports, plan
"Feeds existing tooling" / "Aggregation without surveillance" / feature
9 "Team aggregate"): ``claude-token-lens export --format
csv-flat|json|otel-jsonl``.

For team leads: aggregate-only is the default (no session ids, no
per-session rows) and project slugs are hashed by default whenever
aggregate-only is in effect (see :func:`resolve_export_options`), since
a bare aggregate-only export that still names every project by its real
slug leaks a repo name -- and a repo name can itself identify a team or
a client. Per-person/per-session detail is opt-in (``--per-session``);
raw slugs are opt-in (``--no-hash-slugs``). No text (prompts, tool
output, file paths) is ever in an export; every column here is a count,
a token total or a cost.

Row grain (``csv-flat``/``json``): one row per (day, project, model,
entrypoint, agent_type), plus ``session_id`` as an extra grouping
dimension when per-session is in effect. This mirrors ``usage.py``'s own
day/project/entrypoint axes and its ``_day_key``/``_to_local``/
``_parse_ts``/``_priced_turns`` helpers -- duplicated here rather than
imported, per this project's established small-helper convention (see
``usage.py``'s and ``workflows.py``'s own module docstrings) -- plus
``model`` and ``agent_type``, since a BI import wants those split out
rather than pre-summed away.

Formats:

- ``csv-flat``: the row grain above as plain CSV (raw, unformatted
  values, matching ``render/csv_out.py``'s own convention).
- ``json``: the same rows as a JSON list under ``"rows"``, plus a
  ``"meta"`` block (tool version, window, pricing version,
  ``generated_at``, ``hash_slugs``).
- ``otel-jsonl``: one JSON line per (day, model, token type) shaped like
  an OpenTelemetry metric data point, using the metric names Claude
  Code's own OTel integration documents (``claude_code.token.usage``
  with attribute ``type`` in ``input``/``output``/``cacheRead``/
  ``cacheCreation``, and ``claude_code.cost.usage``, both carrying a
  ``model`` attribute) so an existing collector's dashboards for those
  names can ingest this file. This is an **offline approximation**
  built from transcripts after the fact, not a live OTel exporter --
  there is no resource/scope metadata and no real collector transport,
  and ``time_unix_nano`` is simply the UTC instant of local-day start
  for the day the tokens were attributed to, not the moment they were
  actually used. This format carries no project/session attribute at
  all (the documented metric names don't have one), so
  ``--aggregate-only``/``--hash-slugs`` don't change its output.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__ as _TOOL_VERSION
from .config import Config
from .corpus import Corpus, SessionBundle
from .model import TranscriptResult, Turn
from .parse import load_or_create_salt
from .pricing import Pricing, price_turn

#: Fixed CSV/JSON row column order for the aggregate grain (before an
#: optional trailing "session_id" in per-session mode).
_ROW_FIELDS: tuple[str, ...] = (
    "day",
    "project",
    "model",
    "entrypoint",
    "agent_type",
    "turns",
    "input_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "cache_read_tokens",
    "output_tokens",
    "thinking_tokens",
    "cost",
    "recache_turns",
    "recache_cache_creation",
)


# -- small helpers duplicated per this project's convention ------------------


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_local(dt: datetime, tz: str | None) -> datetime:
    if tz:
        try:
            return dt.astimezone(ZoneInfo(tz))
        except (ZoneInfoNotFoundError, ValueError):
            return dt.astimezone()
    return dt.astimezone()


def _priced_turns(result: TranscriptResult) -> list[Turn]:
    return [t for t in result.turns if t.turn_index > 0]


def _transcripts_of(bundle: SessionBundle) -> list[TranscriptResult]:
    transcripts: list[TranscriptResult] = []
    if bundle.top is not None:
        transcripts.append(bundle.top)
    transcripts.extend(bundle.subs)
    return transcripts


def _day_key(local_dt: datetime) -> str:
    return local_dt.strftime("%Y-%m-%d")


def _agent_type_of(tr: TranscriptResult) -> str:
    return tr.meta.agent_type or tr.meta.kind or "unknown"


# -- --aggregate-only / --hash-slugs resolution -------------------------------


@dataclass(slots=True)
class ExportOptions:
    fmt: str
    aggregate_only: bool
    hash_slugs: bool


def resolve_export_options(fmt: str, aggregate_only: bool | None, hash_slugs: bool | None) -> ExportOptions:
    """Resolve the CLI's ``--aggregate-only``/``--per-session`` and
    ``--hash-slugs``/``--no-hash-slugs`` flags (each ``None`` until the
    user picks a side -- see ``cli.py``'s ``_add_export_args``) into
    concrete booleans. ``aggregate_only`` defaults to ``True``.
    ``hash_slugs`` defaults to whatever ``aggregate_only`` resolved to
    (hashed whenever aggregate-only is in effect, matching the plan's
    "Aggregation without surveillance" guarantee) unless the caller
    picked a side explicitly -- an explicit ``--no-hash-slugs`` is
    honoured even together with ``--aggregate-only``, since that is the
    exporter's own informed choice, not a default.
    """
    resolved_aggregate_only = True if aggregate_only is None else aggregate_only
    resolved_hash_slugs = resolved_aggregate_only if hash_slugs is None else hash_slugs
    return ExportOptions(fmt=fmt, aggregate_only=resolved_aggregate_only, hash_slugs=resolved_hash_slugs)


def _hash_slug(slug: str, salt: bytes) -> str:
    """First 12 hex characters of ``sha256(salt + slug)`` -- the same
    salted-hash construction as ``parse.py``'s own path/session-id
    hashing, reusing ``parse.load_or_create_salt`` rather than a new
    salt file (see the plan's "Locate files via ... hashed slugs"
    guarantee)."""
    return hashlib.sha256(salt + slug.encode("utf-8")).hexdigest()[:12]


# -- csv-flat / json row grain -------------------------------------------


@dataclass(slots=True)
class _Cell:
    turns: int = 0
    input_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cost: float = 0.0
    recache_turns: int = 0
    recache_cache_creation: int = 0


def build_export_rows(corpus: Corpus, pricing: Pricing, config: Config, *, per_session: bool) -> list[dict]:
    """One row per (day, project, model, entrypoint, agent_type)
    [, session_id], per the module docstring. Turns with no parseable
    local timestamp are skipped (nothing to bucket them by day with) --
    the same "day_cell only when local_dt is not None" rule
    ``usage.py``'s ``build_section`` already applies to its own by-day
    table.
    """
    cells: dict[tuple, _Cell] = {}
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        for tr in _transcripts_of(bundle):
            entrypoint = tr.meta.entrypoint or "unknown"
            agent_type = _agent_type_of(tr)
            for turn in _priced_turns(tr):
                parsed = _parse_ts(turn.ts)
                if parsed is None:
                    continue
                local_dt = _to_local(parsed, config.tz)
                day = _day_key(local_dt)
                model = turn.model or "<unknown>"

                key: tuple = (day, bundle.slug, model, entrypoint, agent_type)
                if per_session:
                    key = key + (bundle.session_id,)

                cell = cells.setdefault(key, _Cell())
                cell.turns += 1
                cell.input_tokens += turn.input_tokens
                cell.cache_write_5m_tokens += turn.cc_5m
                cell.cache_write_1h_tokens += turn.cc_1h
                cell.cache_read_tokens += turn.cache_read_tokens
                cell.output_tokens += turn.output_tokens
                cell.thinking_tokens += turn.thinking_tokens
                resolved = pricing.resolve_model(turn.model)
                cell.cost += price_turn(turn, resolved).total
                if turn.is_recache:
                    cell.recache_turns += 1
                    cell.recache_cache_creation += turn.cache_creation_tokens

    rows: list[dict] = []
    for key, cell in sorted(cells.items(), key=lambda kv: kv[0]):
        if per_session:
            day, slug, model, entrypoint, agent_type, session_id = key
        else:
            day, slug, model, entrypoint, agent_type = key
            session_id = None

        row = {
            "day": day,
            "project": slug,
            "model": model,
            "entrypoint": entrypoint,
            "agent_type": agent_type,
            "turns": cell.turns,
            "input_tokens": cell.input_tokens,
            "cache_write_5m_tokens": cell.cache_write_5m_tokens,
            "cache_write_1h_tokens": cell.cache_write_1h_tokens,
            "cache_read_tokens": cell.cache_read_tokens,
            "output_tokens": cell.output_tokens,
            "thinking_tokens": cell.thinking_tokens,
            "cost": cell.cost,
            "recache_turns": cell.recache_turns,
            "recache_cache_creation": cell.recache_cache_creation,
        }
        if per_session:
            row["session_id"] = session_id
        rows.append(row)
    return rows


def _apply_hash_slugs(rows: list[dict], config_dir: str | Path) -> None:
    salt = load_or_create_salt(config_dir)
    for row in rows:
        row["project"] = _hash_slug(row["project"], salt)


def render_csv_flat(rows: list[dict], *, per_session: bool) -> str:
    fieldnames = list(_ROW_FIELDS) + (["session_id"] if per_session else [])
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def render_json(rows: list[dict], meta: dict) -> str:
    return json.dumps({"meta": meta, "rows": rows}, indent=2, sort_keys=True)


# -- otel-jsonl ---------------------------------------------------------


@dataclass(slots=True)
class _OtelCell:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost: float = 0.0
    day_start_unix_nano: int = 0


def _day_start_unix_nano(local_dt: datetime) -> int:
    day_start = local_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(day_start.timestamp() * 1_000_000_000)


def build_otel_cells(corpus: Corpus, pricing: Pricing, config: Config) -> dict[tuple[str, str], _OtelCell]:
    """Per (day, model) totals for :func:`render_otel_jsonl`. No
    project/entrypoint/agent_type/session dimension at all -- the
    documented OTel metric names this format mirrors don't carry one
    (see the module docstring)."""
    cells: dict[tuple[str, str], _OtelCell] = {}
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        for tr in _transcripts_of(bundle):
            for turn in _priced_turns(tr):
                parsed = _parse_ts(turn.ts)
                if parsed is None:
                    continue
                local_dt = _to_local(parsed, config.tz)
                day = _day_key(local_dt)
                model = turn.model or "<unknown>"
                key = (day, model)
                cell = cells.setdefault(key, _OtelCell(day_start_unix_nano=_day_start_unix_nano(local_dt)))
                cell.input_tokens += turn.input_tokens
                cell.output_tokens += turn.output_tokens
                cell.cache_read_tokens += turn.cache_read_tokens
                cell.cache_creation_tokens += turn.cache_creation_tokens
                resolved = pricing.resolve_model(turn.model)
                cell.cost += price_turn(turn, resolved).total
    return cells


def render_otel_jsonl(corpus: Corpus, pricing: Pricing, config: Config) -> str:
    cells = build_otel_cells(corpus, pricing, config)
    lines: list[str] = []
    for (day, model), cell in sorted(cells.items(), key=lambda kv: kv[0]):
        for otel_type, value in (
            ("input", cell.input_tokens),
            ("output", cell.output_tokens),
            ("cacheRead", cell.cache_read_tokens),
            ("cacheCreation", cell.cache_creation_tokens),
        ):
            lines.append(
                json.dumps(
                    {
                        "name": "claude_code.token.usage",
                        "attributes": {"type": otel_type, "model": model},
                        "time_unix_nano": cell.day_start_unix_nano,
                        "value": value,
                    },
                    sort_keys=True,
                )
            )
        lines.append(
            json.dumps(
                {
                    "name": "claude_code.cost.usage",
                    "attributes": {"model": model},
                    "time_unix_nano": cell.day_start_unix_nano,
                    "value": cell.cost,
                },
                sort_keys=True,
            )
        )
    return "\n".join(lines) + ("\n" if lines else "")


# -- top-level entry point ------------------------------------------------


def build_export_text(
    corpus: Corpus,
    pricing: Pricing,
    config: Config,
    config_dir: str | Path,
    options: ExportOptions,
    *,
    window: str = "",
    generated_at: str | None = None,
) -> str:
    """The full export text for ``options.fmt``, ready to print to
    stdout or write to a file (see ``cli.py``'s ``_cmd_export``)."""
    if options.fmt == "otel-jsonl":
        return render_otel_jsonl(corpus, pricing, config)

    per_session = not options.aggregate_only
    rows = build_export_rows(corpus, pricing, config, per_session=per_session)
    if options.hash_slugs:
        _apply_hash_slugs(rows, config_dir)

    if options.fmt == "csv-flat":
        return render_csv_flat(rows, per_session=per_session)
    if options.fmt == "json":
        meta = {
            "tool_version": _TOOL_VERSION,
            "window": window,
            "pricing_version": pricing.version,
            "generated_at": generated_at or datetime.now().astimezone().isoformat(),
            "hash_slugs": options.hash_slugs,
            "aggregate_only": options.aggregate_only,
        }
        return render_json(rows, meta)
    raise ValueError(f"unknown export format: {options.fmt!r}")


__all__ = [
    "ExportOptions",
    "resolve_export_options",
    "build_export_rows",
    "render_csv_flat",
    "render_json",
    "build_otel_cells",
    "render_otel_jsonl",
    "build_export_text",
]
