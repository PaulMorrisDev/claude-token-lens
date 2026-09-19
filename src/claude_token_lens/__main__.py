"""``python -m claude_token_lens`` entry point.

Also the entry point a ``python -m zipapp``-built ``claude-token-lens.pyz``
uses (see the plan's "Distribution without pip" section): zipapp runs
``__main__.py`` at the archive root, and without ``sys.exit(main())``
here the process would always exit 0 regardless of what ``main()``
returned, silently losing every non-zero exit code (no-data, bad-input,
...) a caller or CI script depends on.
"""

from __future__ import annotations

import sys

from .cli import main

sys.exit(main())
