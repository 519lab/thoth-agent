"""ACP-suite-local fixtures.

The ACP session tests were written against SQLite SessionDB instances backed
by a per-test ``tmp_path/state.db`` file — each test started with a clean
database. After the PostgreSQL migration the SessionManager talks to the
shared ``thoth_db`` pool, so without explicit cleanup row-state from one
test leaks into the next (and breaks every assertion of the form
``assert manager.list_sessions() == []``).

This conftest restores the per-test clean-slate invariant by truncating
the session-shaped tables before and after each test. The pool itself is
left in place so we don't pay the connect / Alembic-replay cost per test.
"""

from __future__ import annotations

import os

import pytest


def _truncate_session_tables() -> None:
    """Drop all rows from session-related tables in the configured DB."""
    if not (os.environ.get("THOTH_PG_DSN") or os.environ.get("THOTH_PG_DSN")):
        return

    import thoth_db

    # Bootstrap the pool first. This fixture runs between tests (outside
    # any event loop) so ``ensure_pool_sync`` can drive the bootstrap on
    # the persistent ``_sync_loop`` cleanly; without this, the inner
    # ``run_sync(_truncate())`` would hit ``pool()`` from inside its own
    # loop and have no way to initialise.
    if not thoth_db.ensure_pool_sync():
        return

    async def _truncate() -> None:
        async with thoth_db.connection() as conn:
            # CASCADE wipes dependent rows (messages, telegram_dm_*); we
            # restart identity sequences too so id-comparisons stay stable.
            await conn.execute(
                "TRUNCATE TABLE sessions RESTART IDENTITY CASCADE"
            )

    try:
        thoth_db.run_sync(_truncate())
    except Exception:
        # If the DB doesn't have the expected tables (e.g. the test
        # environment isn't fully migrated), silently no-op so we don't
        # mask the real test failure with a fixture error.
        pass


def _reset_substrate_state() -> None:
    """Tear down any module-level substrate state from a prior test.

    Some acp tests (``test_entry``) invoke ``entry.main([])`` which calls
    ``bootstrap_substrate_sync``. That sets ``thoth_bootstrap._substrate_booted
    = True`` and binds ``substrate.events.thoth_hooks._substrate`` to the
    booted instance.

    After the per-test fixture closes the asyncpg pool, those module-level
    references are stale — the substrate instance holds a dead pool ref and
    the bound hooks dispatch into ``streams.get_by_name(...)`` which races
    on a closed connection. The downstream symptom in the next test:
    ``SessionDB.create_session`` opens a transaction, fires the
    ``on_session_start_async`` hook, the hook raises (caught by the outer
    try/except), but the transaction COMMITS the INSERT to a database the
    next test isn't querying — so ``get_session`` returns None.

    Resetting the bootstrap flags + unbinding hooks makes every acp test
    start with a clean slate. The actual ``Substrate.boot()`` runs again
    on demand in tests that need it; the cost is one extra
    ``ensure_partitions`` + ``_autoregister_streams`` round-trip per test
    that actually boots the substrate, which is negligible.
    """
    try:
        import thoth_bootstrap
        thoth_bootstrap._substrate_booted = False
        thoth_bootstrap._substrate_handle = None
    except Exception:
        pass
    try:
        from substrate.events import thoth_hooks
        thoth_hooks._unbind()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_acp_session_db(thoth_db_initialized_sync):
    """Per-test cleanup: wipe sessions/messages before and after each test.

    Depends on ``thoth_db_initialized_sync`` so the per-test PG database
    is created (Alembic upgrade head) and the asyncpg pool is bound to
    THAT database BEFORE the truncate call runs. Without this dependency
    the autouse fixture initialises the pool with whatever
    ``THOTH_PG_DSN`` the container started with (the shared compose
    database), and subsequent calls to ``ensure_pool_sync`` see
    ``_pool is not None`` and skip re-init — so the test ends up querying
    a different database from the one Alembic just migrated, and
    everything 500s with ``relation "sessions" does not exist``.

    Also resets substrate-module-level state in setup AND teardown so a
    prior test that booted the substrate (notably ``test_entry``) can't
    leak a stale ``thoth_bootstrap._substrate_handle`` or a bound
    ``substrate.events.thoth_hooks._substrate`` into the next test.
    """
    _reset_substrate_state()
    _truncate_session_tables()
    yield
    _reset_substrate_state()
    _truncate_session_tables()
