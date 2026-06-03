"""Tests for Curator release + per-tombstone-policy execution — spec §10.2.

Covers the three policy paths (full / thin / none), the
release_after_consolidation gate, concurrency-safe selection, and the
LIMIT bound.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.agents import Curator
from substrate.l0 import commit_slice
from substrate.storage import (
    DEFAULT_STRUCTURED_PROFILE,
    Family,
    Lifecycle,
    Modality,
    SentinelState,
)


@pytest_asyncio.fixture
async def substrate(hermes_db_initialized):
    import thoth_db

    return Substrate.from_pool(thoth_db.pool())


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _register_profile(
    pool,
    name: str,
    *,
    half_life_seconds: int = 3600,
    min_salience: float = 0.05,
    tombstone_policy: str = "thin",
    release_after_consolidation: bool = False,
    consolidation_window_seconds: int = 600,
    justification: str = None,
) -> UUID:
    """Insert a custom decay profile so tests can pick policy + the
    ``release_after_consolidation`` gate."""
    profile_id = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO substrate_decay_profiles
                (profile_id, name, natural_half_life, consolidation_window,
                 reinforcement_bump, min_salience_to_retain,
                 release_after_consolidation, pending_ttl,
                 tombstone_policy, tombstone_none_justification,
                 applies_to_modality)
            VALUES
                ($1, $2, make_interval(secs => $3), make_interval(secs => $4),
                 0.2, $5, $6, interval '30 seconds',
                 $7, $8, 'structured_event')
            """,
            profile_id, name, float(half_life_seconds),
            float(consolidation_window_seconds),
            float(min_salience), release_after_consolidation,
            tombstone_policy, justification,
        )
    return profile_id


async def _seed_releasable_slice(
    substrate,
    stream_id: UUID,
    *,
    salience: float = 0.01,
    consolidation_state: str = "unconsolidated",
) -> UUID:
    """Commit a slice + set sentinel=passed + force salience low + set
    consolidation_state. Returns slice_id."""
    import thoth_db

    await commit_slice(
        substrate, stream_id, {"k": uuid4().hex[:6]},
        event_time_world=_now_utc(),
    )
    async with thoth_db.connection() as conn:
        slice_id = await conn.fetchval(
            """
            UPDATE substrate_slices
               SET sentinel_state = 'passed',
                   trust_score = 0.5,
                   pending_committed_at = NULL,
                   salience_score = $2,
                   consolidation_state = $3,
                   salience_updated_at = now() - interval '1 minute'
             WHERE slice_id = (
                 SELECT slice_id FROM substrate_slices
                  WHERE stream_id = $1
                  ORDER BY ingest_time_world DESC LIMIT 1
             )
            RETURNING slice_id
            """,
            stream_id, salience, consolidation_state,
        )
    return slice_id


# ---------------------------------------------------------------------------
# Per-policy release.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_thin_policy_nulls_payload_and_marks_released(substrate):
    import thoth_db

    profile_id = await _register_profile(
        substrate.pool, "test-thin-release",
        tombstone_policy="thin",
        release_after_consolidation=False,
    )
    stream = await substrate.streams.register(
        name="hermes.test.release_thin",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )
    slice_id = await _seed_releasable_slice(substrate, stream.stream_id, salience=0.01)

    curator = Curator(substrate)
    released = await curator._evaluate_releases()
    assert len(released) == 1
    assert released[0].slice_id == slice_id
    assert released[0].tombstone_policy == "thin"

    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT payload, payload_blob_ref, salience_score,
                   consolidation_state, metadata
              FROM substrate_slices WHERE slice_id = $1
            """,
            slice_id,
        )
    assert row is not None  # row kept (thin tombstone)
    assert row["payload"] is None
    assert row["payload_blob_ref"] is None
    assert row["salience_score"] == 0.0
    assert row["consolidation_state"] == "released"
    # Metadata is intentionally retained per tombstone semantics.
    assert row["metadata"] is not None


@pytest.mark.asyncio
async def test_release_full_policy_keeps_salience(substrate):
    import thoth_db

    profile_id = await _register_profile(
        substrate.pool, "test-full-release",
        tombstone_policy="full",
        release_after_consolidation=False,
    )
    stream = await substrate.streams.register(
        name="hermes.test.release_full",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )
    slice_id = await _seed_releasable_slice(substrate, stream.stream_id, salience=0.02)

    curator = Curator(substrate)
    released = await curator._evaluate_releases()
    assert len(released) == 1

    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            "SELECT salience_score, consolidation_state, payload FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
    assert row["payload"] is None
    assert row["consolidation_state"] == "released"
    # 'full' keeps salience at release-time value (0.02), NOT zeroed.
    assert row["salience_score"] == pytest.approx(0.02, abs=0.001)


@pytest.mark.asyncio
async def test_release_none_policy_deletes_row(substrate):
    import thoth_db

    profile_id = await _register_profile(
        substrate.pool, "test-none-release",
        tombstone_policy="none",
        release_after_consolidation=False,
        justification="ephemeral test stream",
    )
    stream = await substrate.streams.register(
        name="hermes.test.release_none",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )
    slice_id = await _seed_releasable_slice(substrate, stream.stream_id, salience=0.01)

    curator = Curator(substrate)
    released = await curator._evaluate_releases()
    assert len(released) == 1

    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            "SELECT slice_id FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
    assert row is None  # full row delete


@pytest.mark.asyncio
async def test_release_respects_release_after_consolidation(substrate):
    """Profile with release_after_consolidation=TRUE does NOT release
    unconsolidated slices, even if salience is below threshold."""
    import thoth_db

    profile_id = await _register_profile(
        substrate.pool, "test-gate-release",
        tombstone_policy="thin",
        release_after_consolidation=True,  # the gate
    )
    stream = await substrate.streams.register(
        name="hermes.test.release_gate",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )
    slice_id = await _seed_releasable_slice(
        substrate, stream.stream_id,
        salience=0.001,
        consolidation_state="unconsolidated",  # the gate-relevant state
    )

    curator = Curator(substrate)
    released = await curator._evaluate_releases()
    assert released == []

    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            "SELECT payload, consolidation_state FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
    assert row["payload"] is not None  # untouched
    assert row["consolidation_state"] == "unconsolidated"


@pytest.mark.asyncio
async def test_release_passes_when_consolidated(substrate):
    """Same profile (release_after_consolidation=TRUE) DOES release
    a slice whose consolidation_state is 'consolidated'."""
    import thoth_db

    profile_id = await _register_profile(
        substrate.pool, "test-gate-consol",
        tombstone_policy="thin",
        release_after_consolidation=True,
    )
    stream = await substrate.streams.register(
        name="hermes.test.release_gate_consol",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )
    slice_id = await _seed_releasable_slice(
        substrate, stream.stream_id, salience=0.01,
        consolidation_state="consolidated",
    )

    curator = Curator(substrate)
    released = await curator._evaluate_releases()
    assert len(released) == 1


@pytest.mark.asyncio
async def test_release_skips_already_released(substrate):
    """A slice already at consolidation_state='released' is not re-touched."""
    import thoth_db

    profile_id = await _register_profile(
        substrate.pool, "test-already-released",
        tombstone_policy="thin",
        release_after_consolidation=False,
    )
    stream = await substrate.streams.register(
        name="hermes.test.already_released",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )
    slice_id = await _seed_releasable_slice(
        substrate, stream.stream_id, salience=0.0,
        consolidation_state="released",
    )

    curator = Curator(substrate)
    released = await curator._evaluate_releases()
    assert released == []


@pytest.mark.asyncio
async def test_release_bounded_by_batch_limit(substrate):
    """500 eligible slices, one tick releases exactly 200."""
    import thoth_db

    profile_id = await _register_profile(
        substrate.pool, "test-bounded",
        tombstone_policy="thin",
        release_after_consolidation=False,
    )
    stream = await substrate.streams.register(
        name="hermes.test.bounded",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )
    # Seed 250 releasable slices (>200 so one tick saturates).
    for _ in range(250):
        await _seed_releasable_slice(substrate, stream.stream_id, salience=0.01)

    curator = Curator(substrate)
    released = await curator._evaluate_releases()
    assert len(released) == 200


@pytest.mark.asyncio
async def test_release_concurrent_curators_no_double_release(substrate):
    """Two Curators racing on the same eligibility set don't release
    the same slice twice (FOR UPDATE SKIP LOCKED gate)."""
    import thoth_db

    profile_id = await _register_profile(
        substrate.pool, "test-concurrent",
        tombstone_policy="thin",
        release_after_consolidation=False,
    )
    stream = await substrate.streams.register(
        name="hermes.test.concurrent",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )
    # Seed 50 releasable slices.
    for _ in range(50):
        await _seed_releasable_slice(substrate, stream.stream_id, salience=0.01)

    curator_a = Curator(substrate)
    curator_b = Curator(substrate)
    rel_a, rel_b = await asyncio.gather(
        curator_a._evaluate_releases(),
        curator_b._evaluate_releases(),
    )
    seen = {r.slice_id for r in rel_a} | {r.slice_id for r in rel_b}
    # No duplicates across the two callers; total releases ≤ 50.
    assert len(rel_a) + len(rel_b) == len(seen)
    assert len(seen) <= 50
