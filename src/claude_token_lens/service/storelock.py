"""One ``serve`` per store: an OS-level lock on ``<store>.lock`` that a
``serve`` process takes before it opens the SQLite store and holds for
its whole life.

Two processes writing the same ``service.db`` (a second ``serve``
started by hand beside the logon service, say) fight over SQLite's write
lock; the loser's scans fail with "database is locked" and its
dashboard stops updating. The lock turns that into a clear refusal at
startup instead.

The operating system drops the lock when the holder exits, however it
exits, so a crash never leaves a stale lock behind. The lock file itself
is never deleted (deleting a lock file others may hold open lets two
processes each lock a different file of the same name); it stays beside
the store holding the last holder's details as JSON: ``pid``, ``port``,
``bind``, ``version``, ``started_at``. Those details are only believed
while the lock is actually held.

Windows locks a byte range (``msvcrt.locking``); the locked byte sits
far past the JSON, so another process can still read the holder's
details while the lock is held. POSIX uses ``flock``, which belongs to
the open file, so even a second attempt from the same process is
refused.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

#: Where Windows' locked byte sits: well past any JSON this module
#: writes, which starts at offset 0.
_LOCK_OFFSET = 1 << 30

#: The most of a lock file :func:`holder` will read.
_MAX_INFO_BYTES = 64 * 1024


class StoreLockedError(Exception):
    """Another process holds the store's lock. ``holder`` is what that
    process wrote into the lock file (``{}`` when unreadable)."""

    def __init__(self, lock_path: Path, holder: dict) -> None:
        super().__init__("the store is in use by another process")
        self.lock_path = lock_path
        self.holder = holder


def lock_path_for(store_path: Path) -> Path:
    """``<store>.lock``, beside the store file."""
    store_path = Path(store_path)
    return store_path.with_name(store_path.name + ".lock")


def _try_lock(fd: int) -> bool:
    try:
        if os.name == "nt":
            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _open(lock_path: Path) -> int:
    return os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)


def _read_info(lock_path: Path) -> dict:
    try:
        with open(lock_path, "rb") as f:
            data = json.loads(f.read(_MAX_INFO_BYTES).decode("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def holder(store_path: Path) -> dict | None:
    """The details the process holding ``store_path``'s lock wrote
    (``{}`` when unreadable), or ``None`` when nothing holds it."""
    lock_path = lock_path_for(store_path)
    if not lock_path.exists():
        return None
    try:
        fd = _open(lock_path)
    except OSError:
        return None
    try:
        if _try_lock(fd):
            _unlock(fd)
            return None
    finally:
        os.close(fd)
    return _read_info(lock_path)


class StoreLock:
    """The lock on one store, for one process. :meth:`acquire` once,
    :meth:`release` (or process exit) to let go."""

    def __init__(self, store_path: Path) -> None:
        self.path = lock_path_for(store_path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, info: dict, *, wait_s: float = 0.0) -> None:
        """Take the lock and record ``info`` in the lock file. Retries
        for up to ``wait_s`` seconds (a restarted service can start
        before the copy it replaces has quite exited), then raises
        :class:`StoreLockedError`."""
        if self._fd is not None:
            raise RuntimeError("store lock already held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = _open(self.path)
        deadline = time.monotonic() + wait_s
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                os.close(fd)
                raise StoreLockedError(self.path, _read_info(self.path))
            time.sleep(0.2)
        self._fd = fd
        self.update(info)

    def update(self, info: dict) -> None:
        """Replace the recorded details (``serve`` learns its real port
        only once it has bound)."""
        if self._fd is None:
            raise RuntimeError("store lock not held")
        os.ftruncate(self._fd, 0)
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, json.dumps(info, sort_keys=True).encode("utf-8"))

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
            _unlock(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


__all__ = ["StoreLock", "StoreLockedError", "holder", "lock_path_for"]
