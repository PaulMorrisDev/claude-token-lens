"""Small standalone-ish command-line tools bundled with claudeglass.

Unlike ``hooks/snapshot-config.py`` (which must stay importable with zero
package dependency because it is copied out and run by path), everything
under this package is invoked via ``python -m claudeglass.tools.*``
or ``python -m claudeglass.<name>``, so it is free to import from the
rest of the package normally.
"""

from __future__ import annotations
