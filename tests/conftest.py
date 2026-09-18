"""Shared pytest configuration.

Makes ``import claude_token_lens`` work even when the package hasn't been
pip-installed (editable or otherwise) — e.g. running ``python -m pytest``
directly against a checkout with ``PYTHONPATH`` unset. If the package is
already importable (installed, or PYTHONPATH=src is already set), this is
a no-op.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture(autouse=True)
def _isolated_claude_config_dir(tmp_path_factory, monkeypatch):
    """Point every test at a per-test throwaway config dir instead of the
    real ``~/.claude``. ``CLAUDE_CONFIG_DIR`` is what ``pricing.py`` and
    ``hooks/snapshot-config.py`` consult directly; ``HOME``/``USERPROFILE``
    cover anything that falls back to ``Path.home()``. Individual tests can
    still override any of these with their own ``monkeypatch.setenv`` calls.

    Uses ``tmp_path_factory`` (its own base temp dir) rather than the
    test's own ``tmp_path`` fixture, so this fixture never leaks an extra
    "home" entry into a test that lists/asserts on the full contents of
    its own ``tmp_path``.
    """
    fake_home = tmp_path_factory.mktemp("claude_home")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fake_home / ".claude"))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
