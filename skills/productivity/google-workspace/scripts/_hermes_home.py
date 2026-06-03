"""Resolve HERMES_HOME for standalone skill scripts.

Skill scripts may run outside the Hermes process (e.g. system Python,
nix env, CI) where ``thoth_constants`` is not importable.  This module
provides the same ``get_thoth_home()`` and ``display_hermes_home()``
contracts as ``thoth_constants`` without requiring it on ``sys.path``.

When ``thoth_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``thoth_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``HERMES_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from thoth_constants import display_hermes_home as display_hermes_home
    from thoth_constants import get_thoth_home as get_thoth_home
except (ModuleNotFoundError, ImportError):

    def get_thoth_home() -> Path:
        """Return the Hermes home directory (default: ~/.hermes).

        Mirrors ``thoth_constants.get_thoth_home()``."""
        val = os.environ.get("HERMES_HOME", "").strip()
        return Path(val) if val else Path.home() / ".hermes"

    def display_hermes_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``thoth_constants.display_hermes_home()``."""
        home = get_thoth_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)
