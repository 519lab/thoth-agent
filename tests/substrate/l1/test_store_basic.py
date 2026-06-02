"""L1 store — upsert/merge, citations, read helpers (no LLM)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from substrate.l1 import store


@pytest.mark.asyncio
async def test_upsert_entity_creates_then_merges(hermes_db_initialized):
    eid1, created1 = await store.upsert_entity("Greg", "person", summary="A maintainer")
    assert created1 is True

    eid2, created2 = await store.upsert_entity(
        "Greg", "person", summary="Thoth substrate maintainer", aliases=["gg"]
    )
    assert created2 is False
    assert eid2 == eid1  # merged on (name, entity_type)

    ent = await store.get_entity_by_id(eid1)
    assert ent is not None
    assert ent.summary == "Thoth substrate maintainer"  # non-empty new summary wins
    assert "gg" in ent.aliases


@pytest.mark.asyncio
async def test_upsert_entity_normalises_type(hermes_db_initialized):
    eid, _ = await store.upsert_entity("Widget", "Gadget")  # unknown kind
    ent = await store.get_entity_by_id(eid)
    assert ent.entity_type == "other"


@pytest.mark.asyncio
async def test_upsert_relationship_dedup(hermes_db_initialized):
    subj, _ = await store.upsert_entity("Greg", "person")
    obj, _ = await store.upsert_entity("Thoth", "project")
    rid1, c1 = await store.upsert_relationship(subj, "works_on", obj, confidence=0.6)
    rid2, c2 = await store.upsert_relationship(subj, "works_on", obj, confidence=0.9)
    assert c1 is True and c2 is False
    assert rid1 == rid2

    rels = await store.list_relationships_for_entity(subj, direction="out")
    assert len(rels) == 1
    assert rels[0].confidence == pytest.approx(0.9)  # higher confidence kept


@pytest.mark.asyncio
async def test_add_citation_requires_exactly_one_target(hermes_db_initialized):
    sid = uuid4()
    with pytest.raises(ValueError):
        await store.add_citation(slice_id=sid)  # neither
    eid, _ = await store.upsert_entity("X", "concept")
    rid_subj, _ = await store.upsert_entity("Y", "concept")
    with pytest.raises(ValueError):
        await store.add_citation(slice_id=sid, entity_id=eid, relationship_id=rid_subj)


@pytest.mark.asyncio
async def test_get_entities_for_query_ranks_matches(hermes_db_initialized):
    await store.upsert_entity("PostgreSQL migration", "concept", summary="moving to PG")
    await store.upsert_entity("Banana bread", "concept", summary="a recipe")
    hits = await store.get_entities_for_query("postgres migration", limit=5)
    names = [e.name for e in hits]
    assert "PostgreSQL migration" in names
    assert "Banana bread" not in names


@pytest.mark.asyncio
async def test_find_entities_by_name_fuzzy(hermes_db_initialized):
    await store.upsert_entity("Teknium", "person")
    hits = await store.find_entities_by_name("teknius", fuzzy=True)
    assert any(e.name == "Teknium" for e in hits)
