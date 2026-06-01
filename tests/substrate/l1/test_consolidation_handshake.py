"""L1 consolidation handshake — persist_extraction + mark_slices_consolidated.

Exercises the design §5.7 contract: produce L1 from cited slices and flip
the source slices to ``consolidated`` with a ``consolidated_to`` address
list, all in one transaction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.l0 import commit_slice
from substrate.l1 import store
from substrate.l1.schema import ParsedEntity, ParsedRelationship, ParserResult


@pytest_asyncio.fixture
async def booted(hermes_db_initialized):
    sub = await Substrate.boot(
        config=SubstrateConfig(auto_migrate=False, start_subagents=False),
        start_subagents=False,
    )
    try:
        yield sub
    finally:
        await sub.shutdown()


async def _commit_passed_slices(substrate, texts):
    """Commit slices, flip them passed, return their slice_ids in order."""
    import hermes_db

    stream = await substrate.streams.get_by_name("thoth.world.user_message.cli")
    for t in texts:
        await commit_slice(
            substrate, stream.stream_id, t, event_time_world=datetime.now(timezone.utc)
        )
    async with hermes_db.connection() as conn:
        await conn.execute(
            "UPDATE substrate_slices SET sentinel_state='passed', "
            "pending_committed_at=NULL WHERE sentinel_state='pending'"
        )
        rows = await conn.fetch(
            "SELECT slice_id FROM substrate_slices WHERE stream_id=$1 "
            "ORDER BY ingest_time_world",
            stream.stream_id,
        )
    return [r["slice_id"] for r in rows]


@pytest.mark.asyncio
async def test_persist_extraction_writes_l1_and_returns_addresses(hermes_db_initialized):
    import hermes_db

    sid = uuid4()  # citations have no FK; a synthetic slice id is fine here
    result = ParserResult(
        entities=[
            ParsedEntity("Greg", "person", "maintainer", source_slice_ids=[sid], quote="Greg"),
            ParsedEntity("Thoth", "project", "the agent", source_slice_ids=[sid], quote="Thoth"),
        ],
        relationships=[
            ParsedRelationship(
                "Greg", "person", "works_on", "Thoth", "project",
                confidence=0.9, source_slice_ids=[sid], quote="Greg works on Thoth",
            )
        ],
    )

    async with hermes_db.transaction() as conn:
        addresses = await store.persist_extraction(result, conn=conn)

    kinds = sorted(a["kind"] for a in addresses)
    assert kinds == ["entity", "entity", "relationship"]

    # Entities + relationship landed.
    greg = (await store.find_entities_by_name("Greg", entity_type="person"))[0]
    rels = await store.list_relationships_for_entity(greg.id, direction="out")
    assert len(rels) == 1 and rels[0].predicate == "works_on"
    cites = await store.list_citations_for_entity(greg.id)
    assert len(cites) == 1 and cites[0].slice_id == sid


@pytest.mark.asyncio
async def test_mark_slices_consolidated_sets_state_and_addresses(booted):
    import hermes_db

    ids = await _commit_passed_slices(booted, ["msg one", "msg two"])
    addresses = [{"layer": "l1", "kind": "entity", "id": str(uuid4())}]
    n = await store.mark_slices_consolidated(ids, addresses)
    assert n == 2

    async with hermes_db.connection() as conn:
        rows = await conn.fetch(
            "SELECT consolidation_state, consolidated_to FROM substrate_slices "
            "WHERE slice_id = ANY($1::uuid[])",
            ids,
        )
    assert all(r["consolidation_state"] == "consolidated" for r in rows)
    assert all(r["consolidated_to"] == addresses for r in rows)


@pytest.mark.asyncio
async def test_full_handshake_atomic(booted):
    """persist_extraction + mark_slices_consolidated in one transaction."""
    import hermes_db

    ids = await _commit_passed_slices(booted, ["Greg ships the substrate"])
    result = ParserResult(
        entities=[ParsedEntity("Greg", "person", "ships things", source_slice_ids=ids)],
    )
    async with hermes_db.transaction() as conn:
        addresses = await store.persist_extraction(result, conn=conn)
        n = await store.mark_slices_consolidated(ids, addresses, conn=conn)
    assert n == 1
    async with hermes_db.connection() as conn:
        state = await conn.fetchval(
            "SELECT consolidation_state FROM substrate_slices WHERE slice_id=$1", ids[0]
        )
    assert state == "consolidated"


@pytest.mark.asyncio
async def test_mark_skips_released_slices(booted):
    import hermes_db

    ids = await _commit_passed_slices(booted, ["already gone"])
    async with hermes_db.connection() as conn:
        await conn.execute(
            "UPDATE substrate_slices SET consolidation_state='released' WHERE slice_id=$1",
            ids[0],
        )
    n = await store.mark_slices_consolidated(ids, [], )
    assert n == 0  # released slice not resurrected
    async with hermes_db.connection() as conn:
        state = await conn.fetchval(
            "SELECT consolidation_state FROM substrate_slices WHERE slice_id=$1", ids[0]
        )
    assert state == "released"


@pytest.mark.asyncio
async def test_mark_empty_slice_list_is_noop(hermes_db_initialized):
    assert await store.mark_slices_consolidated([], []) == 0
