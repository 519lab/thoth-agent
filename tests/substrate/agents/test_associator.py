"""Phase E1 Associator — co-occurrence + shared-neighbor edge weaving."""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.l1 import store as l1
from substrate.l2 import store as l2
from substrate.agents.associator import Associator


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


@pytest.fixture(autouse=True)
def _associator_on(monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_ASSOCIATOR", "1")


def _edge(edges, a, b, etype):
    pair = {a, b}
    return next(
        (e for e in edges if {e.src_id, e.dst_id} == pair and e.edge_type == etype),
        None,
    )


@pytest.mark.asyncio
async def test_co_occurrence_from_shared_citation(booted):
    a, _ = await l1.upsert_entity("Greg", "person")
    b, _ = await l1.upsert_entity("Thoth", "project")
    sid = uuid4()  # both cited by the same (synthetic) slice
    await l1.add_citation(entity_id=a, slice_id=sid, quote="Greg")
    await l1.add_citation(entity_id=b, slice_id=sid, quote="Thoth")

    await Associator(booted).tick()

    edges = await l2.get_associations_for_entity(a)
    e = _edge(edges, a, b, "co_occurrence")
    assert e is not None and e.weight >= 1.0


@pytest.mark.asyncio
async def test_shared_neighbor_from_common_partner(booted):
    a, _ = await l1.upsert_entity("Alice", "person")
    b, _ = await l1.upsert_entity("Bob", "person")
    c, _ = await l1.upsert_entity("ProjectX", "project")
    await l1.upsert_relationship(a, "works_on", c)
    await l1.upsert_relationship(b, "works_on", c)

    await Associator(booted).tick()

    edges = await l2.get_associations_for_entity(a)
    e = _edge(edges, a, b, "shared_neighbor")
    assert e is not None and e.weight >= 1.0


@pytest.mark.asyncio
async def test_associator_disabled_is_noop(booted, monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_ASSOCIATOR", "0")
    a, _ = await l1.upsert_entity("Solo1", "concept")
    b, _ = await l1.upsert_entity("Solo2", "concept")
    sid = uuid4()
    await l1.add_citation(entity_id=a, slice_id=sid)
    await l1.add_citation(entity_id=b, slice_id=sid)
    await Associator(booted).tick()
    assert await l2.get_associations_for_entity(a) == []


@pytest.mark.asyncio
async def test_associator_intensity_off_is_noop(booted):
    from substrate.agents.base import Level

    a, _ = await l1.upsert_entity("Off1", "concept")
    b, _ = await l1.upsert_entity("Off2", "concept")
    sid = uuid4()
    await l1.add_citation(entity_id=a, slice_id=sid)
    await l1.add_citation(entity_id=b, slice_id=sid)
    agent = Associator(booted)
    agent.set_intensity(Level.OFF)
    await agent.tick()
    assert await l2.get_associations_for_entity(a) == []
