import os
import pytest
import pytest_asyncio
import hermes_db


@pytest_asyncio.fixture
async def initialized_db(hermes_db_dsn):
    """`hermes_db_dsn` fixture comes from tests/conftest.py (Task 6).

    For Task 5's red phase the fixture does not exist yet; this test will
    fail at fixture collection. That's expected — Task 6 makes it pass.

    Defensive loop-binding check mirrors ``hermes_db_initialized`` in
    conftest: a prior sync test (or a ``run_sync`` call that lazy-
    bootstrapped the pool on ``hermes_db._sync_loop``) may have left
    ``hermes_db._pool`` bound to a different loop. ``init()`` is
    idempotent — it would silently return without rebinding — so the
    test body would inherit the stale pool and explode on teardown
    with a cross-loop error from ``PoolConnectionHolder.close()``.
    """
    import asyncio
    if hermes_db._pool is not None:
        current = asyncio.get_running_loop()
        pool_loop = getattr(hermes_db._pool, "_loop", None)
        if pool_loop is not current:
            try:
                hermes_db.run_sync(hermes_db.close())
            except Exception:
                hermes_db._pool = None
    await hermes_db.init(hermes_db_dsn)
    yield
    await hermes_db.close()


@pytest.mark.asyncio
async def test_pool_acquires_a_working_connection(initialized_db):
    async with hermes_db.connection() as conn:
        result = await conn.fetchval("SELECT 1")
    assert result == 1


@pytest.mark.asyncio
async def test_pool_is_singleton(initialized_db):
    p1 = hermes_db.pool()
    p2 = hermes_db.pool()
    assert p1 is p2


@pytest.mark.asyncio
async def test_transaction_commits_on_success(initialized_db):
    async with hermes_db.transaction() as conn:
        await conn.execute("CREATE TEMP TABLE t (x INT)")
        await conn.execute("INSERT INTO t VALUES (1)")
        n = await conn.fetchval("SELECT count(*) FROM t")
    assert n == 1


@pytest.mark.asyncio
async def test_transaction_rolls_back_on_exception(initialized_db):
    with pytest.raises(RuntimeError):
        async with hermes_db.transaction() as conn:
            await conn.execute("CREATE TEMP TABLE u (x INT) ON COMMIT DROP")
            await conn.execute("INSERT INTO u VALUES (1)")
            raise RuntimeError("boom")
    # Temp table is dropped at txn rollback; verify no leak in this connection.
    async with hermes_db.connection() as conn:
        exists = await conn.fetchval(
            "SELECT to_regclass('pg_temp.u') IS NOT NULL"
        )
    assert exists is False


def test_pool_raises_before_init(monkeypatch):
    # Reset module state for this synchronous test.
    hermes_db._pool = None
    # ``pool()`` now lazy-initialises from ``HERMES_PG_DSN`` when present, so
    # to assert the "init not called" RuntimeError we must also ensure no
    # DSN is in the environment (otherwise lazy init kicks in and returns a
    # real pool).
    monkeypatch.delenv("HERMES_PG_DSN", raising=False)
    monkeypatch.delenv("THOTH_PG_DSN", raising=False)
    with pytest.raises(RuntimeError, match="hermes_db.init"):
        hermes_db.pool()


@pytest.mark.asyncio
async def test_pool_refuses_lazy_bootstrap_inside_running_loop(monkeypatch):
    """From inside a running event loop, ``pool()`` must NOT lazy-bind a new
    pool to the persistent sync loop (the cross-loop footgun). It raises a
    clear, actionable error instead so an own-loop entry point that forgot
    to ``await init()`` gets a useful message, not a baffling
    ``got Future ... attached to a different loop`` later.
    """
    saved_pool = hermes_db._pool
    hermes_db._pool = None
    # A DSN is present (so the old code would have attempted lazy init);
    # the guard fires before any connection is opened, so the bogus host
    # is never contacted.
    monkeypatch.setenv("THOTH_PG_DSN", "postgresql://u:p@localhost:5432/db")
    try:
        with pytest.raises(RuntimeError, match="running event loop"):
            hermes_db.pool()
    finally:
        hermes_db._pool = saved_pool


def test_ensure_pool_sync_returns_false_when_no_dsn(monkeypatch):
    """When no DSN is configured, ``ensure_pool_sync`` is a no-op that
    returns False — sync entry points use this to gracefully degrade.
    """
    monkeypatch.delenv("HERMES_PG_DSN", raising=False)
    monkeypatch.delenv("THOTH_PG_DSN", raising=False)
    # Don't close an existing pool (would interfere with later tests
    # that share the module-level singleton); just verify the no-DSN
    # branch returns False when called against an uninitialised state.
    saved_pool = hermes_db._pool
    hermes_db._pool = None
    try:
        assert hermes_db.ensure_pool_sync() is False
        assert hermes_db._pool is None
    finally:
        hermes_db._pool = saved_pool


def test_run_sync_executes_coroutine_synchronously():
    async def make_seven():
        return 7
    assert hermes_db.run_sync(make_seven()) == 7


def test_ssl_kwarg_skips_ssl_for_local_dsns():
    """Local DSNs (docker-compose PG via localhost/127.0.0.1/[::1] or
    any single-label compose service hostname) get ``ssl=False`` so
    asyncpg skips its SSL upgrade negotiation. The negotiation runs
    ``_create_ssl_connection`` even for ``sslmode=prefer``-then-
    downgrade and intermittently segfaults inside CPython's ssl module
    on GitHub Actions runners — taking pytest workers down with no
    Python traceback. Local compose PG has no SSL anyway; this is just
    skipping a no-op handshake."""
    for dsn in (
        "postgresql://hermes:thoth@localhost:5432/thoth",
        "postgresql://hermes:thoth@127.0.0.1:5433/thoth",
        "postgresql://hermes:thoth@[::1]:5432/thoth",
        "postgresql://hermes:thoth@postgres:5432/thoth",
        "postgresql://hermes:thoth@postgres-test:5432/thoth",
        "postgresql://hermes:thoth@db:5432/myapp",
    ):
        assert hermes_db._ssl_kwarg_for_dsn(dsn) == {"ssl": False}, dsn


def test_ssl_kwarg_respects_explicit_sslmode_in_dsn():
    """Operators who set ``sslmode=`` in their DSN have made an explicit
    choice — never override it. Without the explicit-sslmode bail, our
    local-host heuristic could downgrade a deliberately-configured TLS
    connection."""
    dsn = "postgresql://hermes:thoth@localhost:5432/thoth?sslmode=require"
    assert hermes_db._ssl_kwarg_for_dsn(dsn) == {}


def test_ssl_kwarg_lets_asyncpg_negotiate_remote_dsns():
    """Remote DSNs (anything outside the docker-compose host aliases)
    pass through unmodified so asyncpg's default sslmode=prefer negotiates
    with the remote cluster — which is the correct behaviour for Neon /
    Supabase / RDS / any production Postgres."""
    for dsn in (
        "postgresql://user:pw@db.neon.tech:5432/proddb",
        "postgresql://user:pw@some-rds.amazonaws.com:5432/db",
        "postgresql://user:pw@10.0.0.5:5432/db",
    ):
        assert hermes_db._ssl_kwarg_for_dsn(dsn) == {}, dsn


@pytest.mark.asyncio
async def test_jsonb_codec_returns_python_objects(initialized_db):
    # Per DECISIONS.md "Phase 0 delivered" ADR: never use ``::jsonb`` casts
    # — they corrupt asyncpg's statement type cache. Use a real jsonb-typed
    # column so the codec drives both encode (dict → jsonb) and decode
    # (jsonb → dict) paths through a parameterised binding.
    payload = {"a": 1, "b": [2, 3]}
    async with hermes_db.connection() as conn:
        await conn.execute("CREATE TEMP TABLE _jsonb_codec_probe (data jsonb)")
        try:
            await conn.execute(
                "INSERT INTO _jsonb_codec_probe (data) VALUES ($1)",
                payload,
            )
            result = await conn.fetchval("SELECT data FROM _jsonb_codec_probe")
        finally:
            await conn.execute("DROP TABLE _jsonb_codec_probe")
    assert result == payload


@pytest.mark.asyncio
async def test_pool_connections_have_tcp_keepalives_enabled(initialized_db):
    """Every pool connection must have SO_KEEPALIVE=1 so the OS can detect
    dead connections (server-initiated FIN buffered but unread) and evict
    them before any caller acquires a broken socket.

    Regression test for the bug where a docker-network event closed postgres
    connections server-side; the gateway pool handed them out and got
    ``ConnectionDoesNotExistError: connection was closed in the middle of
    operation`` with an orphaned "Future exception was never retrieved" every
    ~5 seconds.
    """
    import socket as _socket

    async with hermes_db.connection() as conn:
        raw = conn._transport.get_extra_info("socket")
        assert raw is not None, "no raw socket on transport"
        assert raw.getsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE) == 1
        if hasattr(_socket, "TCP_KEEPIDLE"):
            assert raw.getsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPIDLE) == 10
        if hasattr(_socket, "TCP_KEEPINTVL"):
            assert raw.getsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPINTVL) == 5
        if hasattr(_socket, "TCP_KEEPCNT"):
            assert raw.getsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPCNT) == 3


@pytest.mark.asyncio
async def test_run_on_pool_loop_direct_when_same_loop(initialized_db):
    """When the caller's loop IS the pool's loop, run_on_pool_loop awaits the
    coroutine directly (no thread hop)."""
    async def _q():
        async with hermes_db.connection() as conn:
            return await conn.fetchval("SELECT 5")
    assert await hermes_db.run_on_pool_loop(_q()) == 5


def test_run_on_pool_loop_routes_across_loops(hermes_db_dsn):
    """Regression for the gateway's recurring ConnectionDoesNotExistError.

    The gateway's pool is bound to ``_sync_loop`` (main()'s ensure_pool_sync
    creates it before the gateway's asyncio.run loop exists). Async-native
    callers on the gateway's own loop — the handoff watcher,
    /title /resume /branch handlers — would ``await connection()`` cross-loop
    and hit ``ConnectionDoesNotExistError`` / "another operation is in
    progress". ``run_on_pool_loop`` must route such a coroutine onto the
    pool's loop and return the result.
    """
    import asyncio

    # Clean slate, then bind the pool to _sync_loop exactly like the gateway.
    if hermes_db._pool is not None:
        hermes_db.run_sync(hermes_db.close())
    assert hermes_db.ensure_pool_sync() is True
    try:
        assert hermes_db._pool._loop is hermes_db._get_sync_loop()

        async def on_other_loop():
            running = asyncio.get_running_loop()
            # Prove we're genuinely cross-loop before routing.
            assert hermes_db._pool._loop is not running

            async def _q():
                async with hermes_db.connection() as conn:
                    return await conn.fetchval("SELECT 7")

            return await hermes_db.run_on_pool_loop(_q())

        # asyncio.run builds a fresh loop — the L_gw analogue.
        assert asyncio.run(on_other_loop()) == 7
    finally:
        hermes_db.run_sync(hermes_db.close())


def test_db_loop_runs_continuously_on_its_own_thread():
    """The DB loop is driven forever by a dedicated daemon thread, so tasks
    scheduled on it keep running between run_sync calls (vs. a one-shot
    run_until_complete loop that stops the moment the call returns)."""
    import threading

    loop = hermes_db._get_sync_loop()
    assert loop.is_running()
    assert hermes_db._db_thread is not None
    assert hermes_db._db_thread.is_alive()
    assert hermes_db._db_thread.daemon is True
    assert hermes_db._db_thread is not threading.current_thread()


def test_run_sync_works_from_inside_a_different_running_loop(hermes_db_dsn):
    """run_sync must work when the caller's thread already drives another
    loop (the gateway's L_gw, pytest-asyncio). It submits to the DB loop
    rather than raising "loop already running"."""
    import asyncio

    if hermes_db._pool is not None:
        hermes_db.run_sync(hermes_db.close())
    assert hermes_db.ensure_pool_sync() is True
    try:
        async def _outer():
            async def _q():
                async with hermes_db.connection() as conn:
                    return await conn.fetchval("SELECT 11")
            # Called from within this running loop; routes to the DB loop.
            return hermes_db.run_sync(_q())

        assert asyncio.run(_outer()) == 11
    finally:
        hermes_db.run_sync(hermes_db.close())


def test_run_sync_accepts_non_coroutine_awaitables(hermes_db_dsn):
    """Regression: run_sync(pool().acquire()) — a PoolAcquireContext is an
    awaitable but not a coroutine. run_coroutine_threadsafe rejects those,
    so run_sync coerces awaitables into coroutines (matching the old
    run_until_complete behavior). This is what the kanban DB layer relies on."""
    if hermes_db._pool is not None:
        hermes_db.run_sync(hermes_db.close())
    assert hermes_db.ensure_pool_sync() is True
    try:
        conn = hermes_db.run_sync(hermes_db.pool().acquire())
        try:
            assert hermes_db.run_sync(conn.fetchval("SELECT 3")) == 3
        finally:
            hermes_db.run_sync(hermes_db.pool().release(conn))
    finally:
        hermes_db.run_sync(hermes_db.close())


def test_long_lived_task_survives_after_routed_boot_returns(hermes_db_dsn):
    """Regression for the orphaned substrate RecallLogWriter task.

    Phase A booted the substrate via a one-shot run on a loop that then
    stopped, so the writer's long-lived recall-log drain task was destroyed
    ("Task was destroyed but it is pending"). The DB loop now runs forever,
    so a task spawned during a routed boot keeps ticking after the boot
    call returns.
    """
    import asyncio
    import time

    if hermes_db._pool is not None:
        hermes_db.run_sync(hermes_db.close())
    assert hermes_db.ensure_pool_sync() is True
    ticks = {"n": 0}
    try:
        async def _boot_that_spawns_long_lived_task():
            async def _drain():
                while True:
                    ticks["n"] += 1
                    await asyncio.sleep(0.01)
            # create_task on the DB loop, mimicking RecallLogWriter.start().
            asyncio.get_running_loop().create_task(_drain())
            return "booted"

        assert hermes_db.run_sync(_boot_that_spawns_long_lived_task()) == "booted"
        # The boot call has returned. A one-shot loop would freeze _drain;
        # the always-running DB loop keeps it progressing.
        first = ticks["n"]
        deadline = time.monotonic() + 2.0
        while ticks["n"] <= first and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ticks["n"] > first, "long-lived task did not progress after boot returned"
    finally:
        hermes_db.run_sync(hermes_db.close())


def test_run_sync_reentrant_from_db_loop_raises(hermes_db_dsn):
    """Calling run_sync from inside the DB loop thread would deadlock; it
    must raise instead so the mis-wiring is loud rather than a hang."""
    if hermes_db._pool is not None:
        hermes_db.run_sync(hermes_db.close())
    assert hermes_db.ensure_pool_sync() is True
    try:
        async def _on_db_loop():
            async def _q():
                return 1
            with pytest.raises(RuntimeError, match="inside the DB loop"):
                hermes_db.run_sync(_q())
            return "ok"

        # run_on_pool_loop routes _on_db_loop onto the DB loop, where the
        # nested run_sync trips the re-entrant guard.
        assert hermes_db.run_sync(_on_db_loop()) == "ok"
    finally:
        hermes_db.run_sync(hermes_db.close())


class TestPoolMaxInactiveLifetime:
    """The pool recycles idle connections via ``max_inactive_connection_lifetime``
    (env-tunable ``HERMES_PG_POOL_MAX_INACTIVE_S``) so connections severed while
    idle — laptop suspend/resume, NAT idle-kill, a brief DB/network blip — get
    dropped within ~2 min instead of lingering ~5 min and spamming
    ``ConnectionDoesNotExistError`` each time a poller re-grabs a dead one."""

    @pytest.fixture(autouse=True)
    def _reset_pool(self):
        # init() is idempotent on a live _pool; clear it so each test exercises
        # create_pool, and never leak a mock pool to later tests.
        saved = hermes_db._pool
        hermes_db._pool = None
        yield
        hermes_db._pool = saved if saved is not None else None
        if saved is None:
            hermes_db._pool = None

    async def _capture_create_pool_kwargs(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        with patch("asyncpg.create_pool", AsyncMock(return_value=MagicMock())) as cp:
            await hermes_db.init("postgresql://u:p@localhost:5432/db")
        hermes_db._pool = None  # drop the mock pool
        return cp.await_args.kwargs

    @pytest.mark.asyncio
    async def test_default_is_120s(self, monkeypatch):
        monkeypatch.delenv("HERMES_PG_POOL_MAX_INACTIVE_S", raising=False)
        kwargs = await self._capture_create_pool_kwargs()
        assert kwargs["max_inactive_connection_lifetime"] == 120.0

    @pytest.mark.asyncio
    async def test_env_override(self, monkeypatch):
        monkeypatch.setenv("HERMES_PG_POOL_MAX_INACTIVE_S", "30")
        kwargs = await self._capture_create_pool_kwargs()
        assert kwargs["max_inactive_connection_lifetime"] == 30.0

    @pytest.mark.asyncio
    async def test_env_zero_disables(self, monkeypatch):
        monkeypatch.setenv("HERMES_PG_POOL_MAX_INACTIVE_S", "0")
        kwargs = await self._capture_create_pool_kwargs()
        assert kwargs["max_inactive_connection_lifetime"] == 0.0

    @pytest.mark.asyncio
    async def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HERMES_PG_POOL_MAX_INACTIVE_S", "not-a-number")
        kwargs = await self._capture_create_pool_kwargs()
        assert kwargs["max_inactive_connection_lifetime"] == 120.0
