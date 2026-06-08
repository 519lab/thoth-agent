"""The asyncpg pool must disable prepared-statement caching by default.

Regression for the recurring substrate crash::

    asyncpg.exceptions.InvalidCachedStatementError: cached statement plan is
    invalid due to a database schema or configuration change

Runtime DDL (``thoth embed reshape`` ALTERs the embedding vector columns;
alembic migrations on boot) invalidates cached statement plans on long-lived
pooled connections, crashing Sentinel/Curator ticks until the connection
cycles. ``statement_cache_size=0`` eliminates the class.

Pure unit test — ``asyncpg.create_pool`` is mocked; no real DB.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import thoth_db


@pytest.mark.asyncio
async def test_pool_disables_statement_cache_by_default(monkeypatch):
    captured: dict = {}

    async def _fake_create_pool(dsn, **kw):
        captured.update(kw)
        return MagicMock()

    monkeypatch.setattr(thoth_db, "_pool", None)
    monkeypatch.setattr(thoth_db.asyncpg, "create_pool", _fake_create_pool)
    monkeypatch.delenv("THOTH_PG_STATEMENT_CACHE_SIZE", raising=False)

    await thoth_db.init("postgresql://u:p@localhost:5432/db")

    assert captured.get("statement_cache_size") == 0


@pytest.mark.asyncio
async def test_statement_cache_size_env_override(monkeypatch):
    captured: dict = {}

    async def _fake_create_pool(dsn, **kw):
        captured.update(kw)
        return MagicMock()

    monkeypatch.setattr(thoth_db, "_pool", None)
    monkeypatch.setattr(thoth_db.asyncpg, "create_pool", _fake_create_pool)
    monkeypatch.setenv("THOTH_PG_STATEMENT_CACHE_SIZE", "100")

    await thoth_db.init("postgresql://u:p@localhost:5432/db")

    assert captured.get("statement_cache_size") == 100
