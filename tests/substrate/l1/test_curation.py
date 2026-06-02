"""L1 curation/hygiene — merge, forget, edit, duplicate suggestion + CLI."""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout

import pytest

from substrate.l1 import store
from substrate.cli import inspect as inspect_mod


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_repoints_relationships_and_citations(hermes_db_initialized):
    from uuid import uuid4

    greg1, _ = await store.upsert_entity("Greg", "person", aliases=["g"])
    greg2, _ = await store.upsert_entity("Greg Grace", "person")
    hermes, _ = await store.upsert_entity("Thoth", "project")
    # greg2 works_on thoth; greg2 cited by a slice.
    await store.upsert_relationship(greg2, "works_on", hermes)
    sid = uuid4()
    await store.add_citation(entity_id=greg2, slice_id=sid)

    assert await store.merge_entities(greg2, greg1) is True

    # greg2 gone; relationship + citation now point at greg1.
    assert await store.get_entity_by_id(greg2) is None
    rels = await store.list_relationships_for_entity(greg1, direction="out")
    assert any(r.predicate == "works_on" and r.object_id == hermes for r in rels)
    cites = await store.list_citations_for_entity(greg1)
    assert any(c.slice_id == sid for c in cites)
    # alias union absorbed the old name.
    merged = await store.get_entity_by_id(greg1)
    assert "Greg Grace" in merged.aliases


@pytest.mark.asyncio
async def test_merge_dedups_duplicate_relationships(hermes_db_initialized):
    a, _ = await store.upsert_entity("A", "concept")
    b, _ = await store.upsert_entity("B", "concept")
    target, _ = await store.upsert_entity("Target", "concept")
    # Both A and B relate to Target with the same predicate.
    await store.upsert_relationship(a, "rel", target)
    await store.upsert_relationship(b, "rel", target)
    await store.merge_entities(b, a)  # a now would have two (a,rel,target)?
    rels = await store.list_relationships_for_entity(a, direction="out")
    # Collapsed to one (a, rel, target).
    assert len([r for r in rels if r.object_id == target]) == 1


@pytest.mark.asyncio
async def test_merge_unknown_or_identical_returns_false(hermes_db_initialized):
    from uuid import uuid4

    a, _ = await store.upsert_entity("Solo", "concept")
    assert await store.merge_entities(a, a) is False
    assert await store.merge_entities(uuid4(), a) is False


# ---------------------------------------------------------------------------
# forget / edit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forget_entity_cascades(hermes_db_initialized):
    import hermes_db

    e, _ = await store.upsert_entity("Ephemeral", "concept")
    o, _ = await store.upsert_entity("Other", "concept")
    await store.upsert_relationship(e, "rel", o)
    assert await store.forget_entity(e) is True
    assert await store.get_entity_by_id(e) is None
    async with hermes_db.connection() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM l1_relationships WHERE subject_id=$1", e
        )
    assert n == 0  # cascaded


@pytest.mark.asyncio
async def test_edit_entity_summary_and_name(hermes_db_initialized):
    e, _ = await store.upsert_entity("Widget", "concept", summary="old")
    assert await store.edit_entity(e, summary="new summary", canonical_name="Gadget") is True
    ent = await store.get_entity_by_id(e)
    assert ent.summary == "new summary" and ent.name == "Gadget"
    # No-op edit returns False.
    assert await store.edit_entity(e) is False


# ---------------------------------------------------------------------------
# duplicate suggestion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_candidates_suggests_similar(hermes_db_initialized):
    await store.upsert_entity("Kubernetes", "project")
    await store.upsert_entity("Kubernetes ", "project")  # near-dup
    await store.upsert_entity("Banana", "project")
    pairs = await store.duplicate_candidates(threshold=0.5)
    names = {(p["a_name"].strip(), p["b_name"].strip()) for p in pairs}
    assert any("Kubernetes" in a and "Kubernetes" in b for a, b in names)


# ---------------------------------------------------------------------------
# CLI wiring + behaviour
# ---------------------------------------------------------------------------


def test_register_subparser_l1_curation():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    inspect_mod.register_subparser(sub)
    assert callable(parser.parse_args(["substrate", "l1", "dupes"]).func)
    ns = parser.parse_args(["substrate", "l1", "merge", "--from", "A", "--into", "B"])
    assert ns.from_name == "A" and ns.into_name == "B"
    assert callable(parser.parse_args(["substrate", "l1", "forget", "X"]).func)
    ns = parser.parse_args(["substrate", "l1", "edit", "X", "--summary", "s"])
    assert ns.summary == "s"


@pytest.mark.asyncio
async def test_cli_merge_and_forget_behaviour(hermes_db_initialized):
    import hermes_db

    await store.upsert_entity("Foo", "concept")
    await store.upsert_entity("Foo2", "concept")
    async with hermes_db.connection() as conn:
        buf = io.StringIO()
        with redirect_stdout(buf):
            await inspect_mod._do_l1_merge(conn, "Foo2", "Foo")
        assert "merged" in buf.getvalue()
        # ambiguous/unknown resolution prints an error, doesn't crash.
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            await inspect_mod._do_l1_forget(conn, "does-not-exist")
        assert "no entity named" in buf2.getvalue()
