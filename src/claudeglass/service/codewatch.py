"""Notice when this package's own files change under a running ``serve``.

``serve`` runs for days. The modules it has imported stay in memory as
they were when it started, but many are imported lazily, by the first
request or scan that needs them (``api.py``'s report routes, say). When
an update lands in place without a restart -- a ``git pull`` in an
editable install's checkout, or ``pip install -U`` over it -- those lazy
imports read the new files and run them against the old modules already
in memory: routes fail with ``ImportError`` while ``/api/health`` still
reports the old version.

:class:`CodeWatch` fingerprints the package when ``serve`` starts, and
``serve.py`` calls :meth:`CodeWatch.check` after every watcher tick
(``FileWatcher``'s ``after_tick``):

- Each check stats the package's files: ``*.py``, ``*.toml`` and
  ``*.json`` under the package folder, leaving out ``__pycache__``,
  dot-files and the dashboard's ``service/static`` (read from disk on
  every request, so never stale). About a hundred ``stat`` calls.
- Only when those sizes and times move does it hash the files' contents
  and compare them with the hash taken at start, so a checkout that
  rewrites files without changing them (a branch switched away and back)
  is not a change.
- Once the contents have differed, the state stays changed even if they
  change back: a lazy import may already have loaded the other code, and
  only a restart makes the process whole again.

A ``.pyz`` build is watched as its one archive file.

Nothing here imports anything outside the standard library and this
package's own already-loaded modules: it has to keep working when the
code on disk no longer matches the code in memory.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__
from .contracts import CodeState

#: The package files whose change means the running code is out of date.
_WATCHED_SUFFIXES = frozenset({".py", ".toml", ".json"})

#: Folders, relative to the package, left out: the dashboard's own files
#: are served from disk on every request.
_SKIPPED_SUBTREES = frozenset({("service", "static")})

_VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)

#: How many hex digits of the start-up hash ``CodeState.id`` carries.
_ID_LENGTH = 12


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def package_root() -> Path:
    """The ``claudeglass`` package folder this process runs from
    (inside the archive, for a ``.pyz``)."""
    return Path(__file__).absolute().parent.parent


def _archive_of(root: Path) -> Path | None:
    """The ``.pyz`` holding ``root``, when ``root`` lives inside one."""
    for parent in root.parents:
        if parent.is_file():
            return parent
    return None


def _watched_files(root: Path) -> list[tuple[str, Path]]:
    """``(name, path)`` for every watched file, sorted by name: paths
    relative to ``root``, or the archive's own file name for a ``.pyz``."""
    if root.is_dir():
        found: list[tuple[str, Path]] = []
        for dirpath, dirnames, filenames in os.walk(root):
            parts = Path(dirpath).relative_to(root).parts
            dirnames[:] = [
                name
                for name in dirnames
                if name != "__pycache__" and not name.startswith(".") and (*parts, name) not in _SKIPPED_SUBTREES
            ]
            for name in filenames:
                if not name.startswith(".") and os.path.splitext(name)[1] in _WATCHED_SUFFIXES:
                    found.append(("/".join((*parts, name)), Path(dirpath) / name))
        return sorted(found)
    archive = _archive_of(root)
    return [(archive.name, archive)] if archive is not None else []


def _stat_key(files: list[tuple[str, Path]]) -> tuple:
    """Each file's name, size and modification time: cheap, and moves
    whenever a file is written, added or removed."""
    key = []
    for name, path in files:
        try:
            st = path.stat()
        except OSError:  # removed since the listing
            continue
        key.append((name, st.st_size, st.st_mtime_ns))
    return tuple(key)


def _digest(files: list[tuple[str, Path]]) -> str:
    """One hash over every file's name and contents."""
    sha = hashlib.sha256()
    for name, path in files:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        sha.update(name.encode("utf-8"))
        sha.update(b"\0")
        sha.update(data)
        sha.update(b"\0")
    return sha.hexdigest()


def version_on_disk(root: Path | None = None) -> str | None:
    """``__version__`` as the package's ``__init__.py`` on disk has it
    now, or ``None`` when it can't be read."""
    root = root if root is not None else package_root()
    try:
        if root.is_dir():
            text = (root / "__init__.py").read_text(encoding="utf-8")
        else:
            archive = _archive_of(root)
            if archive is None:
                return None
            with zipfile.ZipFile(archive) as zf:
                text = zf.read(f"{root.relative_to(archive).as_posix()}/__init__.py").decode("utf-8")
    except (OSError, KeyError, ValueError, UnicodeDecodeError, zipfile.BadZipFile):
        return None
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


class CodeWatch:
    """The package's fingerprint at start, and whether the files on disk
    still match it. See the module docstring. Thread-safe: the watcher
    thread checks after each tick, a request thread after an
    ``ImportError``, and ``/api/health`` reads :meth:`state`."""

    def __init__(self, root: Path | None = None, *, now=None) -> None:
        self.root = root if root is not None else package_root()
        self._now = now or _now_iso
        self._lock = threading.Lock()
        files = _watched_files(self.root)
        self._seen = _stat_key(files)
        self._loaded = _digest(files)
        #: Whether the last check saw the same sizes and times as the one
        #: before it (see :meth:`settled`).
        self._steady = True
        self._changed_at: str | None = None
        self._version_on_disk: str | None = __version__

    def check(self) -> CodeState:
        """Compare the files on disk with the ones loaded at start, and
        return the result. Never raises for a file that can't be read."""
        with self._lock:
            files = _watched_files(self.root)
            seen = _stat_key(files)
            self._steady = seen == self._seen
            if not self._steady:
                self._seen = seen
                if self._changed_at is None and _digest(files) != self._loaded:
                    self._changed_at = self._now()
                if self._changed_at is not None:
                    self._version_on_disk = version_on_disk(self.root)
            return self._state()

    def state(self) -> CodeState:
        """The result of the last :meth:`check`, without looking at disk."""
        with self._lock:
            return self._state()

    def settled(self) -> bool:
        """The code has changed, and the last check found the files just
        as the one before it did: an update is no longer being written
        (a ``git pull`` or ``pip install`` part way through), so a restart
        now would load all of it."""
        with self._lock:
            return self._changed_at is not None and self._steady

    def _state(self) -> CodeState:
        return CodeState(
            changed=self._changed_at is not None,
            changed_at=self._changed_at,
            version_on_disk=self._version_on_disk,
            id=self._loaded[:_ID_LENGTH],
        )


__all__ = ["CodeWatch", "package_root", "version_on_disk"]
