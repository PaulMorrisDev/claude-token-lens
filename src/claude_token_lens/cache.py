"""On-disk digest cache for parsed transcripts (WP11).

Implements the plan's Parsing-paragraph cache: one JSON file per
transcript under ``<config_dir>/cache/<sha256 of normcase(realpath)>.json``,
holding a small provenance header plus the transcript's own
``TranscriptResult`` serialised through :func:`encode_result`. A
subsequent :meth:`DigestCache.get` call returns a hit only when every one
of ``mtime_ns``, ``size_bytes``, ``SCHEMA_VERSION`` (``model.py``'s
dataclass-contract version) and ``PARSER_VERSION`` (``parse.py``'s own
parsing-logic version, see ``__init__.py``) still match — any one
mismatch is treated as an ordinary cache miss (the file is left in place;
a later :meth:`put` overwrites it), never an error.

A transcript whose ``mtime_ns`` is within the last 60 seconds is treated
as still being written by an active Claude Code session (the plan's
Parsing paragraph: "live files (mtime < 60 s) never cached") — it is
never read from or written to the cache, since a session file's content
can change again on the very next turn.

A cache file that can't be read as JSON, isn't a JSON object, or is
missing its ``header``/``result`` half is corrupt: :meth:`get` returns a
miss AND deletes it (so a half-written or bit-rotted entry doesn't sit
around being retried forever); a version/size/mtime mismatch is just a
stale miss and is left alone for :meth:`put` to overwrite.

:func:`encode_result`/:func:`result_from_jsonable` are a matching
encoder/decoder pair for exactly the dataclasses ``model.py`` freezes
(``TranscriptResult`` and everything it nests: ``TranscriptMeta``,
``Diagnostics``, ``Turn``, ``Event``, and the ``EventKind`` enum).
``encode_result`` is deliberately styled like ``render.json_out.
to_jsonable`` (dataclasses -> dicts, enums -> their ``.value``, tuples ->
lists) but is its own, separate function: ``to_jsonable`` rounds floats
to 6 decimal places for stable report output, which would silently
corrupt a byte-for-byte round trip (``Turn.gap_s`` is computed from a
``timedelta.total_seconds()`` and can carry more precision than that).
``result_from_jsonable`` walks the *declared* field types of each
dataclass (via ``typing.get_type_hints``, resolved once per class and
cached) to decide how to rebuild each value: a ``tuple[X, ...]``
annotation rebuilds a ``list`` back into a ``tuple`` (plain JSON has no
tuple type, so this is the one piece of information a generic decoder
can't recover from the JSON shape alone), an ``EventKind``-typed field
re-wraps its string value as the enum member, and a nested dataclass type
recurses. This makes ``parse_transcript(path, meta) ==
result_from_jsonable(encode_result(parse_transcript(path, meta)))`` true
by construction (dataclass equality), which is exactly what
``tests/test_cache.py``'s round-trip test asserts against every fixture.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import os
import tempfile
import time
import types
import typing
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from . import PARSER_VERSION, SCHEMA_VERSION, __version__
from .model import TranscriptMeta, TranscriptResult

#: A transcript whose file was modified within this many seconds of "now"
#: is assumed to still be an active session and is never read from or
#: written to the digest cache (plan Parsing paragraph).
LIVE_FILE_WINDOW_S = 60

_CACHE_SUBDIR = "cache"


# -- encode/decode --------------------------------------------------------


def _encode(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    if isinstance(value, (tuple, list)):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    return value


def encode_result(result: TranscriptResult) -> dict:
    """Convert one ``TranscriptResult`` into a JSON-serialisable dict,
    preserving exact numeric values (never rounded, unlike
    ``render.json_out.to_jsonable`` — see the module docstring)."""
    return _encode(result)


#: A decode "plan" for one dataclass is a tuple of ``(field_name,
#: decoder)`` pairs, built once per class (see :func:`_plan_for`) by
#: walking its declared field types with ``typing``/``dataclasses``
#: introspection. Decoding an actual instance then only ever calls the
#: pre-built ``decoder`` closures — no ``typing.get_origin``/``get_args``
#: calls happen per field per instance. This matters: a transcript can
#: have tens of thousands of ``Turn``s, and the first (correct but naive)
#: version of this decoder re-derived each field's type shape from
#: scratch on every single value, which measured as the dominant cost of
#: a warm cache read on the real corpus (16s for ~1,800 transcripts,
#: against a target of "warm <= 5s for the whole corpus").
def _identity(value):
    return value


def _is_union(origin) -> bool:
    return origin is typing.Union or origin is types.UnionType


def _unwrap_optional(tp):
    """``X | None`` -> ``X`` (every optional field in this contract is a
    two-armed union with ``None``); anything else is returned unchanged.
    """
    origin = typing.get_origin(tp)
    if _is_union(origin):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if args:
            return args[0]
    return tp


def _decoder_for(tp):
    """Build (once, at plan-construction time) the closure that decodes
    one field's value given its declared type ``tp`` (already unwrapped
    of an outer ``Optional``, though a nested ``Optional`` — e.g. a
    tuple's own element type — is unwrapped here too).
    """
    tp = _unwrap_optional(tp)
    origin = typing.get_origin(tp)

    if dataclasses.is_dataclass(tp) and isinstance(tp, type):
        plan = _plan_for(tp)

        def _decode_dataclass(value, _tp=tp, _plan=plan):
            return None if value is None else _apply_plan(_tp, _plan, value)

        return _decode_dataclass

    if isinstance(tp, type) and issubclass(tp, Enum):

        def _decode_enum(value, _tp=tp):
            return None if value is None else _tp(value)

        return _decode_enum

    if origin is tuple:
        args = typing.get_args(tp)
        elem_type = args[0] if (len(args) == 2 and args[1] is Ellipsis) else None
        elem_decoder = _decoder_for(elem_type) if elem_type is not None else _identity
        if elem_decoder is _identity:

            def _decode_tuple_identity(value):
                return None if value is None else tuple(value)

            return _decode_tuple_identity

        def _decode_tuple(value, _d=elem_decoder):
            return None if value is None else tuple(_d(v) for v in value)

        return _decode_tuple

    if origin is list:
        args = typing.get_args(tp)
        elem_type = args[0] if args else None
        elem_decoder = _decoder_for(elem_type) if elem_type is not None else _identity
        if elem_decoder is _identity:

            def _decode_list_identity(value):
                return None if value is None else list(value)

            return _decode_list_identity

        def _decode_list(value, _d=elem_decoder):
            return None if value is None else [_d(v) for v in value]

        return _decode_list

    if origin is dict or tp is dict:

        def _decode_dict(value):
            return None if value is None else dict(value)

        return _decode_dict

    if tp is datetime:

        def _decode_datetime(value):
            return None if value is None else datetime.fromisoformat(value)

        return _decode_datetime

    return _identity


def _build_plan(cls) -> tuple:
    hints = typing.get_type_hints(cls)
    return tuple((f.name, _decoder_for(hints[f.name])) for f in dataclasses.fields(cls))


@functools.lru_cache(maxsize=None)
def _plan_for(cls) -> tuple:
    """The cached decode plan for one dataclass type — built once no
    matter how many instances of ``cls`` are decoded across the whole
    cache-read (see the plan-based decoder's own docstring above).
    """
    return _build_plan(cls)


def _apply_plan(cls, plan: tuple, data: dict):
    kwargs = {name: decoder(data[name]) for name, decoder in plan if name in data}
    return cls(**kwargs)


def result_from_jsonable(data: dict) -> TranscriptResult:
    """Rebuild a ``TranscriptResult`` (and every ``TranscriptMeta``,
    ``Diagnostics``, ``Turn``, ``Event`` and ``EventKind`` it nests) from
    the dict :func:`encode_result` produced. See the module docstring.
    """
    return _apply_plan(TranscriptResult, _plan_for(TranscriptResult), data)


# -- DigestCache ------------------------------------------------------------


@dataclass(slots=True)
class CacheStats:
    """Summary of what's currently on disk under ``<config_dir>/cache/``."""

    files: int = 0
    bytes: int = 0


class DigestCache:
    """One JSON file per transcript under ``<config_dir>/cache/``, keyed
    by the SHA-256 of ``os.path.normcase(os.path.realpath(path))`` — the
    same case-insensitive, symlink-resolved identity ``discovery.
    resolve_project_dirs`` uses for de-duplicating project directories,
    so a Windows slug-case variant of the same file always hashes to the
    same cache entry.
    """

    def __init__(self, config_dir: str | Path):
        self.config_dir = Path(config_dir)
        self.cache_dir = self.config_dir / _CACHE_SUBDIR

    # -- key/path helpers ----------------------------------------------

    def _key(self, path: str | Path) -> str:
        realpath = os.path.realpath(str(path))
        normalized = os.path.normcase(realpath)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _cache_path(self, path: str | Path) -> Path:
        return self.cache_dir / f"{self._key(path)}.json"

    @staticmethod
    def _is_live(meta: TranscriptMeta) -> bool:
        age_ns = time.time_ns() - meta.mtime_ns
        return age_ns < LIVE_FILE_WINDOW_S * 1_000_000_000

    @staticmethod
    def _safe_delete(cache_path: Path) -> None:
        try:
            cache_path.unlink()
        except OSError:
            pass

    # -- public API -------------------------------------------------------

    def get(self, path: str | Path, meta: TranscriptMeta) -> TranscriptResult | None:
        """A hit only when the cache entry exists, is well-formed, and its
        header's ``schema_version``/``parser_version``/``mtime_ns``/
        ``size_bytes`` all match ``meta`` and the running code. A live
        file (see the module docstring) is always a miss, regardless of
        what's on disk. A corrupt entry is deleted; a stale (version- or
        size/mtime-mismatched) one is left for :meth:`put` to overwrite.
        """
        if self._is_live(meta):
            return None

        cache_path = self._cache_path(path)
        try:
            raw_text = cache_path.read_text(encoding="utf-8")
        except OSError:
            return None

        try:
            raw = json.loads(raw_text)
        except ValueError:
            self._safe_delete(cache_path)
            return None
        if not isinstance(raw, dict):
            self._safe_delete(cache_path)
            return None

        header = raw.get("header")
        body = raw.get("result")
        if not isinstance(header, dict) or not isinstance(body, dict):
            self._safe_delete(cache_path)
            return None

        if (
            header.get("schema_version") != SCHEMA_VERSION
            or header.get("parser_version") != PARSER_VERSION
            or header.get("mtime_ns") != meta.mtime_ns
            or header.get("size_bytes") != meta.size_bytes
        ):
            return None

        try:
            return result_from_jsonable(body)
        except (KeyError, TypeError, ValueError):
            self._safe_delete(cache_path)
            return None

    def put(self, path: str | Path, meta: TranscriptMeta, result: TranscriptResult) -> None:
        """Write (or overwrite) the cache entry for ``path``. A no-op for
        a live file (see the module docstring) — never caches, and never
        clobbers a genuinely fresh entry with one that would immediately
        be treated as live anyway. Written atomically (temp file +
        ``os.replace``) so a crash mid-write can never leave a corrupt
        file for the next :meth:`get` to have to detect and delete.
        """
        if self._is_live(meta):
            return

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        key = self._key(path)
        header = {
            "schema_version": SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "tool_version": __version__,
            "realpath_hash": key,
            "mtime_ns": meta.mtime_ns,
            "size_bytes": meta.size_bytes,
        }
        payload = {"header": header, "result": encode_result(result)}
        text = json.dumps(payload)

        cache_path = self.cache_dir / f"{key}.json"
        fd, tmp_name = tempfile.mkstemp(dir=str(self.cache_dir), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp_name, cache_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def purge(self, older_than_days: int | None = None, all: bool = False) -> int:
        """Delete cache entries. ``all=True`` removes every entry
        regardless of age (used by ``--rebuild-cache``). Otherwise,
        ``older_than_days`` removes entries whose cache FILE's own mtime
        (i.e. when it was last written by :meth:`put`) is older than that
        many days; ``None`` (the default, with ``all=False``) removes
        nothing and returns 0. Returns the number of files removed;
        never raises on an individual file that can't be stat'd or
        removed (e.g. a concurrent purge or a permissions hiccup).
        """
        if not self.cache_dir.exists():
            return 0

        cutoff: float | None = None
        if not all and older_than_days is not None:
            cutoff = time.time() - older_than_days * 86400
        elif not all and older_than_days is None:
            return 0

        removed = 0
        for cache_file in self.cache_dir.glob("*.json"):
            if not all:
                try:
                    if cache_file.stat().st_mtime >= cutoff:  # type: ignore[operator]
                        continue
                except OSError:
                    continue
            try:
                cache_file.unlink()
            except OSError:
                continue
            removed += 1
        return removed

    def stats(self) -> CacheStats:
        """Count and total size of every entry currently on disk."""
        if not self.cache_dir.exists():
            return CacheStats()
        files = 0
        total_bytes = 0
        for cache_file in self.cache_dir.glob("*.json"):
            try:
                total_bytes += cache_file.stat().st_size
            except OSError:
                continue
            files += 1
        return CacheStats(files=files, bytes=total_bytes)


__all__ = [
    "LIVE_FILE_WINDOW_S",
    "CacheStats",
    "DigestCache",
    "encode_result",
    "result_from_jsonable",
]
