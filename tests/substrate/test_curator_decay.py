"""Tests for the Curator's natural-decay tick — spec §10.1 + reinforcement.

Covers the closed-form decay arithmetic (Phase B spec §4 + archived plan
Task 5.2), the 1-second minimum interval guard, and the reinforcement
contract (Phase B spec §5).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.agents import Curator, Level
from substrate.l0 import commit_slice, reinforce_slice
from substrate.storage import (
    DEFAULT_STRUCTURED_PROFILE,
    DEFAULT_TEXT_PROFILE,
    Family,
    Modality,
    SentinelState,
)


@pytest_asyncio.fixture
async def substrate(thoth_db_initialized):
    """Substrate built via from_pool — no sub-agent loops running.
    Tests drive Curator.tick() directly so the timing is deterministic.
    """
    import thoth_db

    return Substrate.from_pool(thoth_db.pool())


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _make_passed_slice(
    substrate: Substrate,
    stream_id: UUID,
    payload: dict,
    *,
    salience: float = 1.0,
    salience_updated_at_offset_s: float = 3600.0,
) -> UUID:
    """Commit a slice, mark it passed, and force salience to a known
    value with a specific updated_at offset (default 1 hour ago)."""
    import thoth_db

    address = await commit_slice(
        substrate,
        stream_id,
        payload,
        event_time_world=_now_utc(),
    )
    async with thoth_db.connection() as conn:
        slice_id = await conn.fetchval(
            """
            UPDATE substrate_slices
               SET sentinel_state = 'passed',
                   trust_score = 0.7,
                   pending_committed_at = NULL,
                   salience_score = $2,
                   salience_updated_at = now() - make_interval(secs => $3)
             WHERE slice_id = (
                 SELECT slice_id FROM substrate_slices
                  WHERE stream_id = $1
                  ORDER BY ingest_time_world DESC
                  LIMIT 1
             )
            RETURNING slice_id
            """,
            stream_id,
            salience,
            float(salience_updated_at_offset_s),
        )
    return slice_id


# ---------------------------------------------------------------------------
# Reinforcement contract (spec §5).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reinforce_bumps_by_profile_default(substrate):
    stream = await substrate.streams.register(
        name="hermes.test.reinforce_default",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )
    slice_id = await _make_passed_slice(
        substrate, stream.stream_id, {"k": 1}, salience=0.3
    )
    await reinforce_slice(substrate, slice_id)

    import thoth_db

    async with thoth_db.connection() as conn:
        salience = await conn.fetchval(
            "SELECT salience_score FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
    # default-structured profile has reinforcement_bump = 0.20.
    assert salience == pytest.approx(0.5, abs=0.001)


@pytest.mark.asyncio
async def test_reinforce_caps_at_1(substrate):
    stream = await substrate.streams.register(
        name="hermes.test.reinforce_cap",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )
    slice_id = await _make_passed_slice(
        substrate, stream.stream_id, {"k": 1}, salience=0.9
    )
    await reinforce_slice(substrate, slice_id)
    import thoth_db

    async with thoth_db.connection() as conn:
        salience = await conn.fetchval(
            "SELECT salience_score FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
    assert salience == pytest.approx(1.0, abs=0.001)


@pytest.mark.asyncio
async def test_reinforce_explicit_bump_overrides_profile(substrate):
    stream = await substrate.streams.register(
        name="hermes.test.reinforce_explicit",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )
    slice_id = await _make_passed_slice(
        substrate, stream.stream_id, {"k": 1}, salience=0.3
    )
    await reinforce_slice(substrate, slice_id, bump=0.05)
    import thoth_db

    async with thoth_db.connection() as conn:
        salience = await conn.fetchval(
            "SELECT salience_score FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
    assert salience == pytest.approx(0.35, abs=0.001)


@pytest.mark.asyncio
async def test_reinforce_updates_salience_updated_at(substrate):
    stream = await substrate.streams.register(
        name="hermes.test.reinforce_touches_ts",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )
    slice_id = await _make_passed_slice(
        substrate, stream.stream_id, {"k": 1}, salience=0.3
    )
    await reinforce_slice(substrate, slice_id)
    import thoth_db

    async with thoth_db.connection() as conn:
        age_seconds = await conn.fetchval(
            """
            SELECT EXTRACT(EPOCH FROM (now() - salience_updated_at))::float
              FROM substrate_slices WHERE slice_id = $1
            """,
            slice_id,
        )
    assert age_seconds < 1.0


# ---------------------------------------------------------------------------
# Natural decay (spec §10.1).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decay_halves_salience_after_one_half_life(substrate):
    """Slice with salience 1.0 on the default-text profile (half-life
    1h), ``salience_updated_at`` = 1 hour ago → after one tick salience
    ≈ 0.5 ± 0.02 (PG clock granularity wobble)."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.decay_half_life",
        family=Family.EXTEROCEPTIVE,
        modality=Modality.TEXT,
        source="cli",
        organ="gateway.cli",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    slice_id = await _make_passed_slice(
        substrate, stream.stream_id, "hello",
        salience=1.0, salience_updated_at_offset_s=3600.0,
    )

    curator = Curator(substrate)
    await curator._apply_natural_decay()

    async with thoth_db.connection() as conn:
        salience = await conn.fetchval(
            "SELECT salience_score FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
    assert salience == pytest.approx(0.5, abs=0.02)


@pytest.mark.asyncio
async def test_decay_does_not_touch_recently_updated(substrate):
    """Slice updated <1s ago is skipped by the decay UPDATE."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.decay_recent_skip",
        family=Family.EXTEROCEPTIVE,
        modality=Modality.TEXT,
        source="cli",
        organ="gateway.cli",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    # offset_s = 0.1 — well under the 1-second guard.
    slice_id = await _make_passed_slice(
        substrate, stream.stream_id, "fresh",
        salience=1.0, salience_updated_at_offset_s=0.1,
    )
    curator = Curator(substrate)
    await curator._apply_natural_decay()
    async with thoth_db.connection() as conn:
        salience = await conn.fetchval(
            "SELECT salience_score FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
    assert salience == pytest.approx(1.0, abs=0.001)


@pytest.mark.asyncio
async def test_decay_skips_released_slices(substrate):
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.decay_released",
        family=Family.EXTEROCEPTIVE,
        modality=Modality.TEXT,
        source="cli",
        organ="gateway.cli",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    slice_id = await _make_passed_slice(
        substrate, stream.stream_id, "x",
        salience=0.0, salience_updated_at_offset_s=3600.0,
    )
    # Mark released. Curator should skip it.
    async with thoth_db.connection() as conn:
        await conn.execute(
            """
            UPDATE substrate_slices
               SET consolidation_state = 'released',
                   payload = NULL, salience_score = 0
             WHERE slice_id = $1
            """,
            slice_id,
        )
        ts_before = await conn.fetchval(
            "SELECT salience_updated_at FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
    curator = Curator(substrate)
    await curator._apply_natural_decay()
    async with thoth_db.connection() as conn:
        ts_after = await conn.fetchval(
            "SELECT salience_updated_at FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
    assert ts_after == ts_before  # untouched


@pytest.mark.asyncio
async def test_decay_skips_pending_slices(substrate):
    """``sentinel_state = 'pending'`` is the design §5.8 hard floor."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.decay_pending",
        family=Family.EXTEROCEPTIVE,
        modality=Modality.TEXT,
        source="cli",
        organ="gateway.cli",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    # commit_slice leaves the slice in pending state; explicit force-
    # set salience + age it.
    await commit_slice(substrate, stream.stream_id, "p", event_time_world=_now_utc())
    async with thoth_db.connection() as conn:
        slice_id = await conn.fetchval(
            """
            UPDATE substrate_slices
               SET salience_score = 1.0,
                   salience_updated_at = now() - interval '1 hour'
             WHERE slice_id = (
                 SELECT slice_id FROM substrate_slices
                  WHERE stream_id = $1 AND sentinel_state = 'pending'
                  ORDER BY ingest_time_world DESC LIMIT 1
             )
            RETURNING slice_id
            """,
            stream.stream_id,
        )

    curator = Curator(substrate)
    await curator._apply_natural_decay()
    async with thoth_db.connection() as conn:
        salience = await conn.fetchval(
            "SELECT salience_score FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
    assert salience == pytest.approx(1.0, abs=0.001)


@pytest.mark.asyncio
async def test_decay_skips_quarantined_slices(substrate):
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.decay_quarantined",
        family=Family.EXTEROCEPTIVE,
        modality=Modality.TEXT,
        source="cli",
        organ="gateway.cli",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    await commit_slice(substrate, stream.stream_id, "q", event_time_world=_now_utc())
    async with thoth_db.connection() as conn:
        slice_id = await conn.fetchval(
            """
            UPDATE substrate_slices
               SET sentinel_state = 'quarantined',
                   trust_score = 0,
                   pending_committed_at = NULL,
                   salience_score = 1.0,
                   salience_updated_at = now() - interval '1 hour'
             WHERE slice_id = (
                 SELECT slice_id FROM substrate_slices
                  WHERE stream_id = $1
                  ORDER BY ingest_time_world DESC LIMIT 1
             )
            RETURNING slice_id
            """,
            stream.stream_id,
        )
    curator = Curator(substrate)
    await curator._apply_natural_decay()
    async with thoth_db.connection() as conn:
        salience = await conn.fetchval(
            "SELECT salience_score FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
    assert salience == pytest.approx(1.0, abs=0.001)


# ---------------------------------------------------------------------------
# Intensity gating.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curator_floor_at_low(substrate):
    """Curator's intensity floor is LOW (OFF allowed; anything between
    OFF and LOW would be demoted)."""
    curator = Curator(substrate)
    # Initial state.
    assert curator.level is Level.LOW
    # OFF is honoured (operator opt-out).
    curator.set_intensity(Level.OFF)
    assert curator.level is Level.OFF
    # MODERATE / HIGH / FULL land verbatim.
    curator.set_intensity(Level.MODERATE)
    assert curator.level is Level.MODERATE
    curator.set_intensity(Level.FULL)
    assert curator.level is Level.FULL
    # Setting to LOW is the no-op identity.
    curator.set_intensity(Level.LOW)
    assert curator.level is Level.LOW
