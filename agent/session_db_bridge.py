"""Small sync bridge helpers for SessionDB calls.

Thoth can run with either the SQLite ``SessionDB`` (sync methods) or an
async-backed SessionDB implementation (for example Postgres).  Some agent
paths are intentionally synchronous: CLI/gateway initialization, the main
conversation loop, and background threads.  Calling an async SessionDB method
from those paths returns a coroutine object; if the caller neither awaits nor
bridges it, Python emits ``RuntimeWarning: coroutine ... was never awaited``
and the intended DB operation never happens.
"""

from __future__ import annotations

import inspect
from typing import Any


def resolve_maybe_awaitable(value: Any) -> Any:
    """Return ``value``, resolving it first if it is awaitable.

    This preserves compatibility with synchronous tests/fakes and the default
    SQLite session store while letting sync call sites safely use async-backed
    SessionDB methods.  ``hermes_db.run_sync`` owns the event-loop bridging
    details used elsewhere in the project.
    """
    if not inspect.isawaitable(value):
        return value

    import hermes_db as _hermes_db

    return _hermes_db.run_sync(value)
