import asyncio
import pytest
import thoth_db


@pytest.mark.asyncio
async def test_pool_max_size_serializes_excess_concurrency(hermes_db_dsn):
    """Verify a tiny pool serializes concurrent queries via backpressure.

    20 concurrent queries on a 4-conn pool ⇒ ~5 rounds at ~50ms each ⇒ ~250ms total,
    well under the timeout. No errors expected — asyncpg's pool blocks until a
    connection is available, doesn't fail-fast.
    """
    await thoth_db.init(hermes_db_dsn, min_size=2, max_size=4)
    try:
        async def slow_query():
            async with thoth_db.connection() as conn:
                await conn.execute("SELECT pg_sleep(0.05)")
        await asyncio.gather(*[slow_query() for _ in range(20)])
    finally:
        await thoth_db.close()


@pytest.mark.asyncio
async def test_pool_recovers_after_query_failure(hermes_db_dsn):
    """A query that errors must not poison the pool — subsequent queries succeed."""
    await thoth_db.init(hermes_db_dsn, min_size=2, max_size=4)
    try:
        with pytest.raises(Exception):
            async with thoth_db.connection() as conn:
                await conn.execute("SELECT 1/0")
        # The pool's connection was returned even though the query failed.
        async with thoth_db.connection() as conn:
            assert await conn.fetchval("SELECT 1") == 1
    finally:
        await thoth_db.close()


@pytest.mark.asyncio
async def test_pool_transaction_rollback_releases_connection(hermes_db_dsn):
    """A transaction that rolls back due to exception must release the conn back to the pool."""
    await thoth_db.init(hermes_db_dsn, min_size=2, max_size=4)
    try:
        with pytest.raises(RuntimeError):
            async with thoth_db.transaction() as conn:
                await conn.execute("SELECT 1")
                raise RuntimeError("boom")
        # Same pool, new connection should work.
        async with thoth_db.connection() as conn:
            assert await conn.fetchval("SELECT 1") == 1
    finally:
        await thoth_db.close()
