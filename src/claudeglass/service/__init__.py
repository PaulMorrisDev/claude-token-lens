"""``claudeglass serve``: a local, read-only HTTP service over a
SQLite store built from the same transcripts the CLI reads (plan
Milestone v0.2, "Docker service and web UI").

Package layout (v0.2; modules other than ``schema``/``store``/
``contracts`` are built against the contracts frozen here, not yet
present at the time this docstring was written):

- ``schema.py`` — the SQLite table/index definitions and
  :data:`~claudeglass.service.schema.SCHEMA_VERSION`.
- ``store.py`` — :class:`~claudeglass.service.store.Store`, the
  only code that talks to the SQLite file: writers that fold a parsed
  transcript/session into it, and read queries that return plain
  ``dict``/``list[dict]`` data ready to serialise as the JSON API.
- ``contracts.py`` — the dataclasses (``ServeOptions``, ``WatcherStats``,
  ``ApiError``) and ``Protocol`` signatures (``Watcher``, ``ApiHandler``)
  the watcher thread and the ``http.server`` API handler are implemented
  against, so those two pieces (and their tests) can be built in
  parallel from this one frozen file.
- ``watcher.py`` — polls ``--projects-root`` on a timer and re-parses
  each new-or-changed transcript in full (nit 25: there is no incremental
  "resume from the last byte offset" path -- ``FileWatcher._resolve``
  decides *whether* a file needs re-parsing this tick from its
  ``(mtime_ns, size_bytes)`` against ``Store.known_files()``, but the
  re-parse itself, when triggered, always runs ``parse_transcript`` over
  the whole file), and folds the result into the store via
  ``Store.upsert_transcript``/``upsert_session``.
- ``api.py`` (not yet built) — the ``http.server`` JSON API described in
  ``docs/api.md``, built against ``contracts.ApiHandler``.

Privacy rule (binding on this whole package, not just the schema —
restated here because it governs ``store.py``'s and the future
``api.py``'s behaviour, not only the table shapes ``schema.py``
declares): **no column, and no value returned by any ``Store`` read
query or any ``/api/*`` endpoint, may ever hold message text, a path
found *inside* a transcript (a file a tool read/wrote, a working
directory mentioned in tool output), or a shell command.** The store's
own bookkeeping is the one narrow exception — ``transcripts.path``,
``projects.root_path`` and ``profiles.toml_path`` hold real local
filesystem paths *of the store's own files*, needed so the watcher can
reopen/re-scan them — and even those three columns must never be
selected into an API-facing result. See ``schema.py``'s module
docstring for the full statement and ``docs/api.md`` for the contract
as it appears to a client. ``tests/test_service_store.py`` enforces this
by construction: every ``Store`` read query is round-tripped against a
store containing a deliberately distinctive fake local path, and the
test asserts that exact string never appears in any query's result.
"""

from __future__ import annotations

from .schema import SCHEMA_VERSION

__all__ = ["SCHEMA_VERSION"]
