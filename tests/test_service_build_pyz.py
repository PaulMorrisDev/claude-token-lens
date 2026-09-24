"""Tests for ``scripts/build-pyz.py`` (deliverable 2.e): the
dependency-free ``.pyz`` distribution build.

Loaded via ``importlib.util.spec_from_file_location`` rather than a
normal ``import`` statement -- ``build-pyz`` (with a hyphen) is not a
valid Python module name, and the script deliberately lives under
``scripts/`` rather than inside the installable package, so it is not
on ``sys.path`` either way.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build-pyz.py"


def _load_build_module():
    spec = importlib.util.spec_from_file_location("_build_pyz", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def built_pyz(tmp_path_factory) -> Path:
    build_pyz = _load_build_module()
    output = tmp_path_factory.mktemp("pyz-build") / "claude-token-lens.pyz"
    return build_pyz.build(output)


def test_build_script_exists() -> None:
    assert BUILD_SCRIPT.is_file()


def test_built_pyz_exists_and_is_a_zip(built_pyz: Path) -> None:
    assert built_pyz.is_file()
    assert built_pyz.stat().st_size > 0
    assert zipfile.is_zipfile(built_pyz)


def test_built_pyz_includes_the_static_ui_directory(built_pyz: Path) -> None:
    with zipfile.ZipFile(built_pyz) as zf:
        names = set(zf.namelist())
    for expected in (
        "claude_token_lens/service/static/index.html",
        "claude_token_lens/service/static/app.js",
        "claude_token_lens/service/static/app.css",
    ):
        assert expected in names, f"{expected!r} missing from pyz contents: {sorted(names)[:20]}..."


def test_built_pyz_excludes_pycache_and_tests(built_pyz: Path) -> None:
    with zipfile.ZipFile(built_pyz) as zf:
        names = zf.namelist()
    assert not any("__pycache__" in name for name in names)
    assert not any(name.startswith("tests/") for name in names)


def test_built_pyz_carries_pyc_files_next_to_their_source(built_pyz: Path) -> None:
    # ROB-P10: byte-compiled so zipimport never has to parse and compile
    # every .py from inside the zip on each run -- legacy layout (no
    # __pycache__, which the test above forbids), so "module.pyc" sits
    # right next to "module.py".
    with zipfile.ZipFile(built_pyz) as zf:
        names = set(zf.namelist())
    assert "claude_token_lens/cli.pyc" in names
    assert "claude_token_lens/__main__.pyc" in names


def test_built_pyz_still_imports_with_only_the_pyc_present(built_pyz: Path, tmp_path: Path) -> None:
    # Proves the shipped .pyc is actually what zipimport loads (not just
    # sitting there unused): with the package's .py source deleted from a
    # copy of the archive, the CLI still runs from bytecode alone. The
    # archive-root __main__.py (zipapp's own entry-point stub, added
    # after compileall runs, so it has no .pyc counterpart) is kept --
    # zipapp requires one of __main__.py/__main__.pyc to exist there.
    pyc_only = tmp_path / "pyc-only.pyz"
    with zipfile.ZipFile(built_pyz) as src, zipfile.ZipFile(pyc_only, "w") as dst:
        for item in src.infolist():
            if item.filename.endswith(".py") and item.filename != "__main__.py":
                continue
            dst.writestr(item, src.read(item.filename))
    result = subprocess.run(
        [sys.executable, str(pyc_only), "--version"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0
    assert "claude-token-lens" in result.stdout


def test_built_pyz_runs_version_and_exits_zero(built_pyz: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(built_pyz), "--version"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "claude-token-lens" in result.stdout


def test_built_pyz_propagates_a_non_zero_exit_code(built_pyz: Path) -> None:
    # v0.3: "init"/"baseline" are real subcommands now (no longer stubs),
    # so this uses "scrub-fixture" with none of its required flags
    # instead -- a real, always-available bad-input case (cli.py's
    # _cmd_scrub_fixture requires --session-dir/--out unless --verify is
    # given) -- proves the pyz's exit code isn't silently discarded to 0
    # the way targeting cli:main instead of __main__:main would (see
    # build-pyz.py's own module docstring).
    result = subprocess.run(
        [sys.executable, str(built_pyz), "scrub-fixture"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2


__all__: list[str] = []
