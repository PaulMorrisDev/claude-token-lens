"""What ``update`` checks once the new version is installed.

``update`` installs with pip, then hands over to the new version
(``update --finish``), so these checks are always the newest version's
own. Two things make an update look as if it didn't take:

* **Another Python.** pip installs for the Python it runs with. A copy
  installed for a different Python (the one the dashboard started from
  until now, or one found first on ``PATH``) keeps its old version.
  :func:`other_copies` finds them, so ``update`` can offer to remove the
  ones nothing uses any more.
* **An old dashboard holding the port.** One started by hand, outside
  the logon service, keeps port 8765 and the new one can't start.
  :func:`port_holder` names that process on Windows, so ``update`` can
  offer to stop it.

Everything here asks the machine through an injectable ``runner``
(``subprocess.run``) and never raises.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Callable

from . import invocation

Runner = Callable[..., subprocess.CompletedProcess]

#: The distribution name pip installs and uninstalls.
DIST = "claude-token-lens"

#: Run by each candidate Python: its prefix (which install it is, however
#: it was reached) and the version of this tool it has, if any.
_PROBE = "import sys, importlib.metadata as m\nprint(sys.prefix)\nprint(m.version(sys.argv[1]))"

#: A path at the end of a ``py -0p`` line: " -V:3.14 *   C:\\...\\python.exe".
_PY_LIST_PATH = re.compile(r"([A-Za-z]:\\.*?\.exe)\s*$", re.IGNORECASE)

_PYTHON_NAMES = {"python.exe", "pythonw.exe", "py.exe", "python", "python3"}


@dataclass(frozen=True, slots=True)
class Copy:
    """This tool, installed for another Python."""

    python: str
    version: str
    prefix: str


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _run(runner: Runner, command: list[str], timeout: int = 15) -> subprocess.CompletedProcess | None:
    try:
        return runner(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def candidate_pythons(*, also: list[str | None], runner: Runner, windows: bool, path: str | None = None) -> list[str]:
    """Pythons that could hold a copy: ``also`` (the one the service ran
    until now), every one the ``py`` launcher knows on Windows, and each
    ``python``/``python3`` on ``PATH``. Duplicates dropped; the same
    install reached by two paths is merged later, by its prefix."""
    found: list[str] = [invocation._terminal_python(p) for p in also if p]
    if windows:
        listing = _run(runner, ["py", "-0p"])
        if listing is not None and listing.returncode == 0:
            for line in (listing.stdout or "").splitlines():
                match = _PY_LIST_PATH.search(line)
                if match:
                    found.append(match.group(1))
    names = ("python.exe", "python3.exe") if windows else ("python3", "python")
    for folder in (path if path is not None else os.environ.get("PATH", "")).split(os.pathsep):
        for name in names:
            candidate = os.path.join(folder, name) if folder else ""
            if candidate and os.path.isfile(candidate):
                found.append(candidate)
    unique: list[str] = []
    for candidate in found:
        if not any(os.path.normcase(os.path.abspath(candidate)) == os.path.normcase(os.path.abspath(u)) for u in unique):
            unique.append(candidate)
    return unique


def other_copies(candidates: list[str], *, runner: Runner, this_prefix: str | None = None) -> list[Copy]:
    """The candidates that have this tool installed, other than this
    Python's own install (``this_prefix``, default ``sys.prefix``)."""
    this_prefix = this_prefix if this_prefix is not None else sys.prefix
    copies: list[Copy] = []
    for python in candidates:
        result = _run(runner, [python, "-c", _PROBE, DIST])
        if result is None or result.returncode != 0:
            continue  # no copy there, or not a working Python (a Store stub)
        lines = (result.stdout or "").strip().splitlines()
        if len(lines) < 2:
            continue
        prefix, version = lines[0].strip(), lines[-1].strip()
        if _same(prefix, this_prefix) or any(_same(prefix, c.prefix) for c in copies):
            continue
        copies.append(Copy(python=python, version=version, prefix=prefix))
    return copies


def remove_copy(copy: Copy, *, runner: Runner) -> bool:
    """``pip uninstall`` the copy, with its own Python. pip's output shows."""
    try:
        return runner([copy.python, "-m", "pip", "uninstall", "-y", DIST]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def port_holder(port: int, *, runner: Runner) -> tuple[int, str] | None:
    """The process listening on ``port`` (Windows): its id and program
    path, or ``None`` when nothing listens or it can't be told."""
    script = (
        f"$c = Get-NetTCPConnection -LocalPort {int(port)} -State Listen -ErrorAction SilentlyContinue "
        "| Select-Object -First 1; "
        "if ($c) { $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue; "
        "\"$($c.OwningProcess)|$($p.Path)\" }"
    )
    result = _run(runner, ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])
    if result is None or result.returncode != 0:
        return None
    pid, _, program = (result.stdout or "").strip().partition("|")
    return (int(pid), program.strip()) if pid.strip().isdigit() else None


def is_python(program: str) -> bool:
    """Whether a program path is a Python interpreter (so a dashboard of
    this tool, not Docker's or another program's). Read as a Windows
    path, which splits on either slash: the port holder's path comes
    from Windows."""
    return PureWindowsPath(program).name.lower() in _PYTHON_NAMES


def stop_process(pid: int, *, runner: Runner) -> bool:
    result = _run(runner, ["taskkill", "/PID", str(int(pid)), "/F"])
    return result is not None and result.returncode == 0
