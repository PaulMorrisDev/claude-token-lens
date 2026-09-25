"""Tests for ``service.codewatch.CodeWatch``: noticing that the package's
own files changed on disk under a running ``serve``.

Every test watches a small fake package under ``tmp_path``; one checks
the real package's own files are read without error.
"""

from __future__ import annotations

import os
import zipfile
from itertools import count
from pathlib import Path

from claude_token_lens.service import codewatch
from claude_token_lens.service.codewatch import CodeWatch

#: Each write gets its own modification time, so a rewrite always moves
#: it, however coarse the file system's clock.
_MTIMES = count(1_700_000_000_000_000_000, 1_000_000_000)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = next(_MTIMES)
    os.utime(path, ns=(stamp, stamp))


def _package(tmp_path: Path) -> Path:
    root = tmp_path / "claude_token_lens"
    _write(root / "__init__.py", '__version__ = "1.0.0"\n')
    _write(root / "report.py", "def build():\n    return 1\n")
    _write(root / "pricing.toml", "[models]\n")
    _write(root / "service" / "api.py", "ROUTES = {}\n")
    _write(root / "service" / "static" / "app.js", "console.log(1);\n")
    _write(root / "__pycache__" / "report.cpython-311.pyc", "bytecode")
    return root


def _clock():
    stamps = iter(f"2026-09-25T12:0{i}:00Z" for i in range(10))
    return lambda: next(stamps)


def test_an_untouched_package_is_not_changed(tmp_path: Path):
    watch = CodeWatch(_package(tmp_path))
    state = watch.check()
    assert state.changed is False
    assert state.changed_at is None
    assert watch.settled() is False


def test_rewriting_a_file_with_the_same_contents_is_not_a_change(tmp_path: Path):
    """A checkout switched to another branch and back rewrites files
    without changing them."""
    root = _package(tmp_path)
    watch = CodeWatch(root)
    _write(root / "report.py", "def build():\n    return 1\n")
    assert watch.check().changed is False


def test_a_changed_module_is_seen_with_the_version_now_on_disk(tmp_path: Path):
    root = _package(tmp_path)
    watch = CodeWatch(root, now=_clock())
    _write(root / "__init__.py", '__version__ = "2.0.0"\n')
    state = watch.check()
    assert state.changed is True
    assert state.changed_at == "2026-09-25T12:00:00Z"
    assert state.version_on_disk == "2.0.0"


def test_a_new_or_removed_module_is_a_change(tmp_path: Path):
    root = _package(tmp_path)
    watch = CodeWatch(root)
    _write(root / "quick_actions.py", "ACTIONS = []\n")
    assert watch.check().changed is True

    root2 = _package(tmp_path / "second")
    watch2 = CodeWatch(root2)
    (root2 / "report.py").unlink()
    assert watch2.check().changed is True


def test_the_change_stays_seen_after_the_files_change_back(tmp_path: Path):
    """A lazy import may have loaded the other code meanwhile."""
    root = _package(tmp_path)
    watch = CodeWatch(root, now=_clock())
    _write(root / "report.py", "def build():\n    return 2\n")
    first = watch.check()
    _write(root / "report.py", "def build():\n    return 1\n")
    again = watch.check()
    assert again.changed is True
    assert again.changed_at == first.changed_at


def test_static_bytecode_and_dot_files_are_not_watched(tmp_path: Path):
    """The dashboard's files are read from disk on every request, and
    bytecode and an editor's scratch files are no code change."""
    root = _package(tmp_path)
    watch = CodeWatch(root)
    _write(root / "service" / "static" / "app.js", "console.log(2);\n")
    _write(root / "__pycache__" / "report.cpython-311.pyc", "other bytecode")
    _write(root / ".report.py.swp", "scratch")
    _write(root / "notes.txt", "not code")
    assert watch.check().changed is False


def test_settled_waits_for_one_check_with_no_further_writes(tmp_path: Path):
    """An update still being written (a pull part way through) is not
    yet worth restarting on."""
    root = _package(tmp_path)
    watch = CodeWatch(root)
    _write(root / "report.py", "def build():\n    return 2\n")
    watch.check()
    assert watch.settled() is False
    _write(root / "service" / "api.py", "ROUTES = {'x': 1}\n")
    watch.check()
    assert watch.settled() is False
    watch.check()
    assert watch.settled() is True


def test_state_reports_the_last_check_without_reading_disk(tmp_path: Path):
    root = _package(tmp_path)
    watch = CodeWatch(root)
    _write(root / "report.py", "def build():\n    return 2\n")
    assert watch.state().changed is False
    watch.check()
    assert watch.state().changed is True


def test_id_names_the_code_loaded_at_start(tmp_path: Path):
    root = _package(tmp_path)
    first = CodeWatch(root).state().id
    assert len(first) == 12
    assert CodeWatch(root).state().id == first
    _write(root / "report.py", "def build():\n    return 2\n")
    assert CodeWatch(root).state().id != first


def test_a_pyz_is_watched_as_its_archive(tmp_path: Path):
    archive = tmp_path / "claude-token-lens.pyz"

    def build(version: str) -> None:
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("claude_token_lens/__init__.py", f'__version__ = "{version}"\n')
            zf.writestr("claude_token_lens/report.py", "def build():\n    return 1\n")
        stamp = next(_MTIMES)
        os.utime(archive, ns=(stamp, stamp))

    build("1.0.0")
    root = archive / "claude_token_lens"
    watch = CodeWatch(root)
    assert watch.check().changed is False
    build("2.0.0")
    state = watch.check()
    assert state.changed is True
    assert state.version_on_disk == "2.0.0"


def test_version_on_disk_is_none_when_unreadable(tmp_path: Path):
    assert codewatch.version_on_disk(tmp_path / "missing") is None
    _write(tmp_path / "pkg" / "__init__.py", "# no version here\n")
    assert codewatch.version_on_disk(tmp_path / "pkg") is None


def test_the_real_package_reads_as_unchanged():
    from claude_token_lens import __version__

    watch = CodeWatch()
    assert watch.root == codewatch.package_root()
    assert (watch.root / "service" / "codewatch.py").is_file()
    state = watch.check()
    assert state.changed is False
    assert state.version_on_disk == __version__
    assert codewatch.version_on_disk() == __version__
