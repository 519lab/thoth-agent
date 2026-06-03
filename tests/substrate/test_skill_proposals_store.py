"""Skill-proposal store round-trips (self-improvement Tier 1).

Backs the SkillScout (stages pending proposals) and the ``skill_proposal`` tool
(lists / shows / decides). Also exercises migration 0022's table + unique slug.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from substrate.skill_proposals import store


@pytest_asyncio.fixture
async def _db(hermes_db_initialized):
    # Migrated per-test DB; the fixture's value isn't needed directly — the
    # store opens its own connections via thoth_db.
    return None


@pytest.mark.asyncio
async def test_insert_and_get_round_trip(_db):
    pid = await store.insert_proposal(
        slug="unifi-site-query",
        title="Query UniFi sites",
        draft_content="---\nname: unifi-site-query\n---\n# body",
        rationale="recurring manual task",
        source_l3_ids=["11111111-1111-1111-1111-111111111111"],
        source_l4_ids=["22222222-2222-2222-2222-222222222222"],
        salience=0.82,
    )
    assert pid is not None

    p = await store.get_proposal("unifi-site-query")
    assert p is not None
    assert p.slug == "unifi-site-query"
    assert p.title == "Query UniFi sites"
    assert p.status == "pending"
    assert p.rationale == "recurring manual task"
    assert p.source_l3_ids == ["11111111-1111-1111-1111-111111111111"]
    assert p.source_l4_ids == ["22222222-2222-2222-2222-222222222222"]
    assert 0.81 <= p.salience <= 0.83
    assert p.decided_at is None and p.decided_by is None


@pytest.mark.asyncio
async def test_unique_slug_dedup(_db):
    """Re-proposing the same slug is a no-op (None) — the dedup gate that stops
    the SkillScout re-raising a need it already proposed/decided."""
    first = await store.insert_proposal(
        slug="dup", title="A", draft_content="x", salience=0.5
    )
    second = await store.insert_proposal(
        slug="dup", title="B (different draft)", draft_content="y", salience=0.9
    )
    assert first is not None
    assert second is None
    # The original row is untouched.
    p = await store.get_proposal("dup")
    assert p.title == "A"
    assert await store.has_similar("dup") is True
    assert await store.has_similar("never-seen") is False


@pytest.mark.asyncio
async def test_set_status_and_listing(_db):
    await store.insert_proposal(slug="keep", title="K", draft_content="x", salience=0.5)
    await store.insert_proposal(slug="nope", title="N", draft_content="y", salience=0.5)

    assert await store.count_pending() == 2

    assert await store.set_status("nope", "rejected", by="greg") is True
    rejected = await store.get_proposal("nope")
    assert rejected.status == "rejected"
    assert rejected.decided_by == "greg"
    assert rejected.decided_at is not None

    assert await store.count_pending() == 1
    pending = await store.list_proposals(status="pending")
    assert [p.slug for p in pending] == ["keep"]
    assert {p.slug for p in await store.list_proposals()} == {"keep", "nope"}


@pytest.mark.asyncio
async def test_set_status_unknown_slug_returns_false(_db):
    assert await store.set_status("ghost", "approved", by="x") is False


@pytest.mark.asyncio
async def test_eval_fields_round_trip(_db):
    """Phase 2: an evaluator verdict supplied at insert time round-trips, and
    evaluated_at is stamped only when a verdict is present."""
    await store.insert_proposal(
        slug="judged",
        title="J",
        draft_content="x",
        salience=0.6,
        eval_verdict="flag",
        eval_reasons=["vague design", "scope creep"],
        eval_model="judge-x",
    )
    p = await store.get_proposal("judged")
    assert p.eval_verdict == "flag"
    assert p.eval_reasons == ["vague design", "scope creep"]
    assert p.eval_model == "judge-x"
    assert p.evaluated_at is not None

    # No verdict supplied → null verdict, no evaluated_at (Phase-1 / un-vetted path).
    await store.insert_proposal(slug="unjudged", title="U", draft_content="x", salience=0.5)
    u = await store.get_proposal("unjudged")
    assert u.eval_verdict is None
    assert u.eval_reasons == []
    assert u.evaluated_at is None
