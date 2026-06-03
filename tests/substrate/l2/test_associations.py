"""L2 store — edge bump/canonicalisation + edit history + inspect surface."""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout

import pytest

from substrate.l1 import store as l1
from substrate.l2 import store as l2
from substrate.cli import inspect as inspect_mod


async def _two_entities():
    a, _ = await l1.upsert_entity("Alpha", "concept")
    b, _ = await l1.upsert_entity("Beta", "concept")
    return a, b


@pytest.mark.asyncio
async def test_bump_canonicalises_and_accumulates(hermes_db_initialized):
    import thoth_db

    a, b = await _two_entities()
    id1 = await l2.bump_edge(a, b, "co_occurrence", delta=2.0, reason="co_occurrence_bump")
    # Reverse order → same canonical edge, weight accumulates.
    id2 = await l2.bump_edge(b, a, "co_occurrence", delta=3.0, reason="co_occurrence_bump")
    assert id1 == id2

    async with thoth_db.connection() as conn:
        rows = await conn.fetch("SELECT src_id, dst_id, weight FROM substrate_associations")
    assert len(rows) == 1
    assert rows[0]["weight"] == pytest.approx(5.0)
    assert str(rows[0]["src_id"]) < str(rows[0]["dst_id"])  # canonical ordering

    edits = await l2.get_edits(id1)
    assert len(edits) == 2
    assert edits[0].new_weight == pytest.approx(5.0)  # most recent first
    assert edits[-1].old_weight is None  # first edit had no prior weight


@pytest.mark.asyncio
async def test_self_edge_is_noop(hermes_db_initialized):
    a, _ = await _two_entities()
    assert await l2.bump_edge(a, a, "co_occurrence", delta=1.0, reason="x") is None


@pytest.mark.asyncio
async def test_distinct_edge_types_coexist(hermes_db_initialized):
    a, b = await _two_entities()
    await l2.bump_edge(a, b, "co_occurrence", delta=1.0, reason="co_occurrence_bump")
    await l2.bump_edge(a, b, "shared_neighbor", delta=1.0, reason="shared_neighbor_bump")
    edges = await l2.get_associations_for_entity(a)
    types = {e.edge_type for e in edges}
    assert types == {"co_occurrence", "shared_neighbor"}


@pytest.mark.asyncio
async def test_densest_edges_ordered(hermes_db_initialized):
    a, b = await _two_entities()
    c, _ = await l1.upsert_entity("Gamma", "concept")
    await l2.bump_edge(a, b, "co_occurrence", delta=1.0, reason="r")
    await l2.bump_edge(a, c, "co_occurrence", delta=9.0, reason="r")
    densest = await l2.densest_edges(limit=10)
    assert densest[0].weight >= densest[-1].weight
    assert densest[0].weight == pytest.approx(9.0)


def test_register_subparser_l2():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    inspect_mod.register_subparser(sub)
    assert callable(parser.parse_args(["substrate", "l2", "associations", "--limit", "3"]).func)


@pytest.mark.asyncio
async def test_print_l2_associations(hermes_db_initialized):
    import thoth_db

    a, b = await _two_entities()
    await l2.bump_edge(a, b, "co_occurrence", delta=4.0, reason="r")
    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_l2_associations(conn)
    out = buf.getvalue()
    assert "Alpha" in out and "Beta" in out and "co_occurrence" in out
