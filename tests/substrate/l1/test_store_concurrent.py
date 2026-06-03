"""Concurrent upsert collapses to one row via PG's ON CONFLICT."""

from __future__ import annotations

import asyncio

import pytest

from substrate.l1 import store


@pytest.mark.asyncio
async def test_concurrent_upsert_same_entity_one_row(hermes_db_initialized):
    import thoth_db

    # Fire several upserts of the same (name, type) concurrently. Each
    # acquires its own pooled connection; PG serialises the ON CONFLICT so
    # exactly one row exists afterwards.
    results = await asyncio.gather(
        *(store.upsert_entity("Concurrent", "concept") for _ in range(6))
    )
    ids = {eid for eid, _ in results}
    assert len(ids) == 1  # all resolved to the same entity

    async with thoth_db.connection() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM l1_entities WHERE name='Concurrent' AND entity_type='concept'"
        )
    assert count == 1
