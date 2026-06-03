"""Sync wrapper around ``_AsyncSessionDB`` for tests written in the
pre-Phase-0 SQLite-API style.

Many existing tests (``tests/tools/test_session_search.py``,
``tests/agent/test_insights.py``, etc.) were written when
``SessionDB`` was a synchronous SQLite wrapper. Phase 0 moved the
storage to PostgreSQL behind ``_AsyncSessionDB``; ports left the
tests behind.

This helper exposes the same sync interface so test ports can stay
mostly mechanical: change the fixture, leave the test body alone.

Two pieces:

* ``SyncSessionDB`` — wraps an ``_AsyncSessionDB`` and dispatches
  every method through ``thoth_db.run_sync`` so callers don't need
  ``await``. Drop-in replacement for the old ``SessionDB`` in test
  bodies.

* ``set_session_meta_sync`` — a free function that issues the
  ``UPDATE sessions SET started_at = …, title = …`` pattern that
  the pre-Phase-0 seed functions used to issue directly via
  ``db._conn.execute``. Takes timezone-aware datetimes (PG's
  ``TIMESTAMPTZ`` type); test code that used to pass epoch ints
  must convert via ``datetime.fromtimestamp(epoch, timezone.utc)``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import thoth_db


class SyncSessionDB:
    """Drop-in sync replacement for the pre-Phase-0 SessionDB.

    Dispatches every method call through ``thoth_db.run_sync``. Async
    iterators / streams are not supported (no test in the affected
    suite uses them today).

    Usage in a test::

        @pytest.fixture
        def db(thoth_db_initialized_sync):
            from thoth_state import _AsyncSessionDB
            return SyncSessionDB(_AsyncSessionDB())

        def test_something(db):
            db.create_session("s1", source="cli")
            db.append_message("s1", role="user", content="hi")
    """

    def __init__(self, async_db) -> None:
        self._async_db = async_db

    def __getattr__(self, name: str):
        # Only dispatch through async if the underlying attribute is
        # async-callable. Sync attributes pass through unchanged so
        # ``db.some_property`` still works.
        attr = getattr(self._async_db, name)
        if not callable(attr):
            return attr

        import asyncio

        def _wrapper(*args, **kwargs):
            result = attr(*args, **kwargs)
            if asyncio.iscoroutine(result):
                return thoth_db.run_sync(result)
            return result

        return _wrapper

    @property
    def async_db(self):
        """Escape hatch — return the wrapped ``_AsyncSessionDB`` for
        tests that need to bypass the sync wrapper (e.g. to pass it
        to production code that expects the async form)."""
        return self._async_db


def set_session_meta_sync(
    session_id: str,
    *,
    started_at: Optional[datetime] = None,
    ended_at: Optional[datetime] = None,
    title: Optional[str] = None,
) -> None:
    """Pre-Phase-0 test convenience for backdating, ending, or retitling
    a session. Replaces ``db._conn.execute("UPDATE sessions SET …")``.

    All kwargs are optional; only the columns you pass get touched.
    Pass tz-aware ``datetime`` for ``started_at`` / ``ended_at`` (PG
    ``TIMESTAMPTZ``) — pre-Phase-0 tests passed epoch ints; the
    per-call conversion is ``datetime.fromtimestamp(epoch, timezone.utc)``.
    """
    updates: list[tuple[str, object]] = []
    if started_at is not None:
        updates.append(("started_at", started_at))
    if ended_at is not None:
        updates.append(("ended_at", ended_at))
    if title is not None:
        updates.append(("title", title))
    if not updates:
        return

    set_sql = ", ".join(f"{col} = ${i + 1}" for i, (col, _) in enumerate(updates))
    where_pos = len(updates) + 1

    async def _do():
        async with thoth_db.connection() as conn:
            await conn.execute(
                f"UPDATE sessions SET {set_sql} WHERE id = ${where_pos}",
                *(val for _, val in updates),
                session_id,
            )

    thoth_db.run_sync(_do())


__all__ = ["SyncSessionDB", "set_session_meta_sync"]
