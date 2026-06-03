"""Unit tests for kanban_db PG connection-liveness recovery (no real PG).

These tests cover the evict-and-recover behaviour added to the kanban PG
shim: when a pooled connection has been half-open-severed and a query raises
a connection-lost error, the dead connection must be ``terminate``d (never
``release``d back into the pool) and the operation retried once on a fresh
connection.

Everything is driven through pure mocks — no PostgreSQL instance is touched.
A real ``asyncio`` loop runs the coroutines so ``_async_execute`` exercises
the actual query path against the mock asyncpg connections.
"""

from __future__ import annotations

import asyncio
import sys
import types

import asyncpg
import pytest

from thoth_cli import kanban_db


# ---------------------------------------------------------------------------
# Mock asyncpg connection
# ---------------------------------------------------------------------------

class _MockConn:
    """Stands in for an asyncpg.Connection.

    ``fetch_error`` (if set) is raised on the FIRST ``fetch``/``execute`` call,
    then cleared so subsequent calls succeed. Records whether ``terminate``
    and ``release`` were invoked.
    """

    def __init__(self, name, fetch_error=None, fetch_result=None):
        self.name = name
        self._fetch_error = fetch_error
        self._fetch_result = fetch_result if fetch_result is not None else []
        self.terminated = False
        self.fetch_calls = 0
        self.execute_calls = 0

    async def fetch(self, sql, *params):
        self.fetch_calls += 1
        if self._fetch_error is not None:
            err = self._fetch_error
            self._fetch_error = None
            raise err
        return self._fetch_result

    async def execute(self, sql, *params):
        self.execute_calls += 1
        if self._fetch_error is not None:
            err = self._fetch_error
            self._fetch_error = None
            raise err
        return "SELECT 1"

    def terminate(self):
        self.terminated = True

    def is_closed(self):
        return False


class _MockPool:
    """Hands out connections from ``fresh_conns`` on each ``acquire``."""

    def __init__(self, fresh_conns):
        self._fresh = list(fresh_conns)
        self.released = []

    async def acquire(self):
        return self._fresh.pop(0)

    async def release(self, conn):
        self.released.append(conn)


class _FakeHermesDB:
    """Mock of the hermes_db module used by kanban_db's lazy imports."""

    def __init__(self, pool):
        self._pool = pool

    def pool(self):
        return self._pool

    def run_sync(self, coro):
        # Drive the coroutine on a throwaway loop (the production code uses a
        # persistent loop, but for the unit test any loop works since the mock
        # connections are not loop-bound).
        if asyncio.iscoroutine(coro):
            return asyncio.run(coro)
        return coro


@pytest.fixture
def install_fake_hermes_db(monkeypatch):
    """Install a fake ``hermes_db`` module so kanban_db's ``import hermes_db``
    inside methods resolves to our mock."""

    def _install(pool):
        fake = types.ModuleType("hermes_db")
        impl = _FakeHermesDB(pool)
        fake.pool = impl.pool
        fake.run_sync = impl.run_sync
        monkeypatch.setitem(sys.modules, "hermes_db", fake)
        return fake

    return _install


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_connection_lost_errors_includes_asyncpg_types():
    errs = kanban_db._connection_lost_errors()
    assert asyncpg.exceptions.ConnectionDoesNotExistError in errs
    assert asyncpg.exceptions.InterfaceError in errs
    assert ConnectionResetError in errs


def test_execute_evicts_dead_conn_and_retries_on_fresh(install_fake_hermes_db):
    """A SELECT that raises ConnectionDoesNotExistError terminates the dead
    connection (does NOT release it) and retries once on a fresh connection,
    returning the retry's result."""
    dead = _MockConn(
        "dead",
        fetch_error=asyncpg.exceptions.ConnectionDoesNotExistError(
            "connection was closed in the middle of operation"
        ),
    )
    fresh = _MockConn("fresh", fetch_result=[{"id": 1, "title": "ok"}])
    pool = _MockPool([fresh])
    install_fake_hermes_db(pool)

    conn = kanban_db._PgConnection(dead, board_slug="default")
    cursor = conn.execute("SELECT id, title FROM kanban_tasks")

    # The dead connection was terminated, never released.
    assert dead.terminated is True
    assert dead not in pool.released
    # The fresh connection served the retry.
    assert conn._conn is fresh
    assert conn._conn_lost is True
    assert fresh.fetch_calls == 1
    # The retry's result is returned.
    rows = cursor.fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "ok"


def test_execute_retries_only_once_then_raises(install_fake_hermes_db):
    """If the fresh connection ALSO fails with a connection-lost error, the
    error propagates (we retry exactly once, not in a loop)."""
    dead = _MockConn(
        "dead",
        fetch_error=asyncpg.exceptions.ConnectionDoesNotExistError("boom"),
    )
    # Fresh conn raises on its first (and our only retry) fetch too.
    fresh = _MockConn(
        "fresh",
        fetch_error=asyncpg.exceptions.ConnectionDoesNotExistError("still dead"),
    )
    pool = _MockPool([fresh])
    install_fake_hermes_db(pool)

    conn = kanban_db._PgConnection(dead, board_slug="default")
    with pytest.raises(asyncpg.exceptions.ConnectionDoesNotExistError):
        conn.execute("SELECT id FROM kanban_tasks")

    assert dead.terminated is True


def test_execute_in_txn_terminates_and_raises_without_retry(install_fake_hermes_db):
    """Mid-transaction, a half-open connection cannot be safely replayed:
    terminate + raise, no retry."""
    dead = _MockConn(
        "dead",
        fetch_error=asyncpg.exceptions.ConnectionDoesNotExistError("boom"),
    )
    fresh = _MockConn("fresh", fetch_result=[{"id": 1}])
    pool = _MockPool([fresh])
    install_fake_hermes_db(pool)

    conn = kanban_db._PgConnection(dead, board_slug="default")
    conn._in_txn = True

    with pytest.raises(asyncpg.exceptions.ConnectionDoesNotExistError):
        conn.execute("SELECT id FROM kanban_tasks")

    # Dead conn terminated; fresh conn was acquired (eviction) but NOT used
    # for a retry (no fetch issued against it).
    assert dead.terminated is True
    assert fresh.fetch_calls == 0


def test_execute_healthy_conn_no_terminate(install_fake_hermes_db):
    """A healthy SELECT must not terminate or evict anything."""
    healthy = _MockConn("healthy", fetch_result=[{"id": 1, "title": "fine"}])
    pool = _MockPool([])  # no fresh conns needed
    install_fake_hermes_db(pool)

    conn = kanban_db._PgConnection(healthy, board_slug="default")
    cursor = conn.execute("SELECT id, title FROM kanban_tasks")

    assert healthy.terminated is False
    assert conn._conn_lost is False
    assert conn._conn is healthy
    assert cursor.fetchall()[0]["title"] == "fine"


def test_connection_reset_error_also_recovers(install_fake_hermes_db):
    """A bare ConnectionResetError (builtin) triggers the same eviction path."""
    dead = _MockConn("dead", fetch_error=ConnectionResetError("reset by peer"))
    fresh = _MockConn("fresh", fetch_result=[{"id": 2}])
    pool = _MockPool([fresh])
    install_fake_hermes_db(pool)

    conn = kanban_db._PgConnection(dead, board_slug="default")
    cursor = conn.execute("SELECT id FROM kanban_tasks")

    assert dead.terminated is True
    assert dead not in pool.released
    assert cursor.fetchall()[0]["id"] == 2


# ---------------------------------------------------------------------------
# Handle.close() behaviour after a connection loss
# ---------------------------------------------------------------------------

def test_handle_close_terminates_dead_releases_fresh(install_fake_hermes_db):
    """After recovery, the handle's close() must terminate the original dead
    conn and release the FRESH replacement (never release the dead one)."""
    dead = _MockConn(
        "dead",
        fetch_error=asyncpg.exceptions.ConnectionDoesNotExistError("boom"),
    )
    fresh = _MockConn("fresh", fetch_result=[{"id": 1}])
    pool = _MockPool([dead, fresh])  # first acquire (handle init) -> dead
    install_fake_hermes_db(pool)

    handle = kanban_db._PgConnectionHandle(board_slug="default")
    assert handle._raw_conn is dead

    # Trigger the failure + recovery on the inner connection.
    handle.execute("SELECT id FROM kanban_tasks")
    assert handle._inner._conn is fresh

    handle.close()

    # Dead conn terminated and NOT released; fresh conn released back to pool.
    assert dead.terminated is True
    assert dead not in pool.released
    assert fresh in pool.released


def test_handle_close_healthy_releases_conn(install_fake_hermes_db):
    """A healthy handle close() releases the connection back to the pool
    exactly as before — no terminate."""
    healthy = _MockConn("healthy", fetch_result=[{"id": 1}])
    pool = _MockPool([healthy])
    install_fake_hermes_db(pool)

    handle = kanban_db._PgConnectionHandle(board_slug="default")
    handle.execute("SELECT id FROM kanban_tasks")
    handle.close()

    assert healthy.terminated is False
    assert healthy in pool.released
