#!/usr/bin/env python3
"""Build ``dist/claude-token-lens.pyz`` -- a single-file, dependency-free
distribution of claude-token-lens (``docs/deploy.md``'s ".pyz build"
path), for a machine where ``pip install`` is unavailable or unwanted.

Usage::

    python scripts/build-pyz.py [--output PATH]

Uses the stdlib :mod:`zipapp` module directly (rather than shelling out
to ``python -m zipapp``) so this script has a normal argparse CLI and a
testable ``build()`` function, but the archive it produces is bit-for-bit
what ``python -m zipapp src -m "claude_token_lens.__main__:main" -o
dist/claude-token-lens.pyz -p "/usr/bin/env python3"`` would produce
from this repository's own ``src/`` layout.

Why ``claude_token_lens.__main__:main``, not ``claude_token_lens.cli:main``:
``zipapp``'s generated archive-root ``__main__.py`` (built from the
``main=`` argument) is just::

    import claude_token_lens.__main__
    claude_token_lens.__main__.main()

``claude_token_lens/__main__.py`` itself is a *module*, not a function --
its own top-level statement is ``sys.exit(main())`` (see that file),
which runs the instant ``import claude_token_lens.__main__`` executes,
propagating ``cli.main()``'s real exit code via the ``SystemExit`` that
raises. Pointing zipapp at ``claude_token_lens.cli:main`` instead would
produce a wrapper that calls ``cli.main()`` and discards its returned
int, silently exiting 0 regardless of what the CLI actually returned --
exactly the bug ``__main__.py``'s own docstring warns about for `python
-m claude_token_lens`` itself. Targeting ``__main__:main`` sidesteps
that: the module import raises ``SystemExit`` with the real code before
the generated wrapper's own ``.main()`` call is ever reached.

Only ``src/claude_token_lens/`` is archived -- no ``tests/``, no build
tooling, no repository metadata -- so the archive is exactly what a
``pip install .`` of this package would have put on `sys.path`, nothing
more. ``service/static/*`` (the web UI) is an ordinary package-data
tree already living under ``src/claude_token_lens/service/static/``, so
it is included automatically; no separate step is needed.
"""

from __future__ import annotations

import argparse
import compileall
import py_compile
import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
PACKAGE_DIR = SRC_DIR / "claude_token_lens"
DEFAULT_OUTPUT = REPO_ROOT / "dist" / "claude-token-lens.pyz"

#: Never let a stray build artefact from the developer's own environment
#: end up inside the shipped archive: byte-code caches, and any dot-file
#: or dot-folder (an editor's or a tool's own cache, which may hold local
#: paths).
_EXCLUDED_DIR_NAMES = {"__pycache__"}


def _copy_source_tree(dest: Path) -> None:
    """Copy ``src/claude_token_lens`` into ``dest`` (a fresh temp
    directory), skipping ``__pycache__`` and dot-files -- ``zipapp.create_archive``
    has no include/exclude filter of its own, so this is done with a
    plain filtered copy first rather than archiving ``src/`` in place.
    """

    def _ignore(_dir: str, names: list[str]) -> set[str]:
        return {name for name in names if name in _EXCLUDED_DIR_NAMES or name.startswith(".")}

    shutil.copytree(PACKAGE_DIR, dest / "claude_token_lens", ignore=_ignore)


def build(output: Path = DEFAULT_OUTPUT) -> Path:
    """Build the ``.pyz`` at ``output`` (creating parent directories as
    needed) and return its path.
    """
    if not PACKAGE_DIR.is_dir():
        raise FileNotFoundError(f"expected package source at {PACKAGE_DIR}, not found")

    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="claude-token-lens-pyz-") as tmp:
        tmp_path = Path(tmp)
        _copy_source_tree(tmp_path)
        # ROB-P10: byte-compile before archiving, so the shipped .pyz
        # carries .pyc files and zipimport never has to parse and
        # compile every .py from inside the zip on each run it starts.
        # legacy=True writes "module.pyc" next to "module.py" (no
        # __pycache__ directory -- test_built_pyz_excludes_pycache_and_tests
        # forbids one, and zipimport only ever reads that legacy layout
        # from inside a zip, never PEP 3147's __pycache__ one).
        # UNCHECKED_HASH: source and bytecode are built together right
        # here and shipped as one immutable archive, so there is nothing
        # to invalidate against at import time -- a timestamp-based pyc
        # would depend on mtimes surviving the copy and the zip write
        # unchanged, which zipapp makes no promise about.
        compiled_ok = compileall.compile_dir(
            str(tmp_path),
            quiet=1,
            legacy=True,
            workers=1,
            invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        )
        if not compiled_ok:
            raise RuntimeError("claude-token-lens.pyz build failed: compileall could not byte-compile the source tree")
        zipapp.create_archive(
            source=tmp_path,
            target=output,
            interpreter="/usr/bin/env python3",
            main="claude_token_lens.__main__:main",
        )

    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        metavar="PATH",
        help=f"default: {DEFAULT_OUTPUT.relative_to(REPO_ROOT)}",
    )
    args = parser.parse_args(argv)

    output = build(args.output)
    print(f"built {output} ({output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
