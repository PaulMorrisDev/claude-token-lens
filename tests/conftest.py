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

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
