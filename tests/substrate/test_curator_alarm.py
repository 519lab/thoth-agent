"""Tests for the pathological-forgetting alarm — spec §10.3.

Slices whose consolidation_state is 'unconsolidated' past their
profile's consolidation_window get bumped (reinforce by
reinforcement_bump) and emit a curator.pathological_forgetting_alarm
self-state slice.
"""

from __future__ import annotations

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
    Modality,
)


@pytest_asyncio.fixture
async def substrate(thoth_db_initialized):
    import thoth_db

    return Substrate.from_pool(thoth_db.pool())


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _register_profile_with_short_window(
    pool, name: str, *, window_seconds: int = 60,
    reinforcement_bump: float = 0.2,
) -> UUID:
    profile_id = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO substrate_decay_profiles
                (profile_id, name, natural_half_life, consolidation_window,
                 reinforcement_bump, min_salience_to_retain,
                 release_after_consolidation, pending_ttl,
                 tombstone_policy, applies_to_modality)
            VALUES
                ($1, $2, interval '1 hour', make_interval(secs => $3),
                 $4, 0.05, FALSE, interval '30 seconds',
                 'thin', 'structured_event')
            """,
            profile_id, name, float(window_seconds), float(reinforcement_bump),
        )
    return profile_id


async def _seed_passed_unconsolidated(
    substrate, stream_id: UUID, *,
    ingest_offset_seconds: float = 120.0,
    salience: float = 0.4,
) -> UUID:
    """Commit + mark passed + age the row so it's past the
    consolidation_window."""
    import thoth_db

    await commit_slice(
        substrate, stream_id, {"k": uuid4().hex[:6]},
        event_time_world=_now_utc(),
    )
    async with thoth_db.connection() as conn:
        # Move event_time_world / perception_time_world / ingest_time_world
        # all back by the same offset so the
        # ``event ≤ perception ≤ ingest`` CHECK constraint holds.
        slice_id = await conn.fetchval(
            """
            UPDATE substrate_slices
               SET sentinel_state = 'passed',
                   trust_score = 0.5,
                   pending_committed_at = NULL,
                   salience_score = $2,
                   event_time_world      = now() - make_interval(secs => $3),
                   perception_time_world = now() - make_interval(secs => $3),
                   ingest_time_world     = now() - make_interval(secs => $3),
                   time_start_world      = now() - make_interval(secs => $3),
                   time_end_world        = now() - make_interval(secs => $3),
                   salience_updated_at   = now() - make_interval(secs => $3)
             WHERE slice_id = (
                 SELECT slice_id FROM substrate_slices
                  WHERE stream_id = $1
                  ORDER BY ingest_time_world DESC LIMIT 1
             )
            RETURNING slice_id
            """,
            stream_id, salience, float(ingest_offset_seconds),
        )
    return slice_id


def _curator_no_cooldown(substrate) -> Curator:
    """Curator with cooldown disabled — for tests that seed a slice and
    immediately expect an alarm. Production default is 1 hour; we drop
    it to 0 here so freshly-seeded data fires on the first tick."""
    c = Curator(substrate)
    c.ALARM_COOLDOWN_SECONDS = 0
    return c


@pytest.mark.asyncio
async def test_alarm_fires_when_consolidation_window_passed(substrate):
    profile_id = await _register_profile_with_short_window(
        substrate.pool, "test-alarm-fires", window_seconds=60,
    )
    stream = await substrate.streams.register(
        name="thoth.test.alarm_fires",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )
    # 120s old, 60s window → past consolidation_window.
    slice_id = await _seed_passed_unconsolidated(
        substrate, stream.stream_id,
        ingest_offset_seconds=120.0,
        salience=0.4,
    )

    curator = _curator_no_cooldown(substrate)
    alarmed = await curator._alarm_pathological()
    assert len(alarmed) == 1
    assert alarmed[0]["slice_id"] == slice_id


@pytest.mark.asyncio
async def test_alarm_does_not_bump_salience(substrate):
    """Post-fix contract (was test_alarm_bumps_salience): alarm is
    observational. It does NOT modify salience_score — bumping defeats
    the decay loop and produced the production amplification observed
    on 2026-05-26 (913 alarms/hour saturating slices at salience 1.0)."""
    import thoth_db

    profile_id = await _register_profile_with_short_window(
        substrate.pool, "test-alarm-no-bump",
        window_seconds=60, reinforcement_bump=0.25,
    )
    stream = await substrate.streams.register(
        name="thoth.test.alarm_no_bump",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )
    slice_id = await _seed_passed_unconsolidated(
        substrate, stream.stream_id,
        ingest_offset_seconds=120.0,
        salience=0.3,
    )

    curator = _curator_no_cooldown(substrate)
    alarmed = await curator._alarm_pathological()
    assert len(alarmed) == 1
    # ``bumped_to`` now reflects current (unchanged) salience.
    assert alarmed[0]["bumped_to"] == pytest.approx(0.3, abs=0.001)

    async with thoth_db.connection() as conn:
        salience = await conn.fetchval(
            "SELECT salience_score FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
    # DB salience untouched by the alarm.
    assert salience == pytest.approx(0.3, abs=0.001)


@pytest.mark.asyncio
async def test_alarm_cooldown_suppresses_repeat_alarms(substrate):
    """Within ALARM_COOLDOWN_SECONDS of an alarm, the same slice must
    not re-fire. Production observed the same slice being alarmed every
    Curator tick — without a cooldown the salience landscape never
    settles."""
    profile_id = await _register_profile_with_short_window(
        substrate.pool, "test-alarm-cooldown", window_seconds=60,
    )
    stream = await substrate.streams.register(
        name="thoth.test.alarm_cooldown",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )
    await _seed_passed_unconsolidated(
        substrate, stream.stream_id,
        ingest_offset_seconds=120.0,
        salience=0.4,
    )

    # Production cooldown (3600s) — freshly-touched slice should NOT alarm.
    curator = Curator(substrate)
    assert await curator._alarm_pathological() == []

    # With cooldown=0 the same slice fires once...
    curator.ALARM_COOLDOWN_SECONDS = 0
    first = await curator._alarm_pathological()
    assert len(first) == 1

    # ...and re-running immediately with the production cooldown back
    # in place suppresses it (the first alarm touched
    # salience_updated_at to now()).
    curator.ALARM_COOLDOWN_SECONDS = 3600
    assert await curator._alarm_pathological() == []


@pytest.mark.asyncio
async def test_alarm_excludes_substrate_self_state_stream(substrate):
    """Every ``substrate.*`` stream is excluded from the alarm-eligible
    set. Operational telemetry now lives in ``substrate_telemetry``, but
    the historical ``substrate.self_state`` slices (and any future
    ``substrate.*`` stream) must still never be alarm-eligible: without
    the exclusion they age past their own consolidation_window and become
    alarm-eligible themselves — the feedback loop that saturated
    production at 900+ alarms/hour."""
    # The ``substrate.self_state`` stream is seeded by Alembic. Look it
    # up rather than registering a duplicate.
    self_state = await substrate.streams.get_by_name("substrate.self_state")
    assert self_state is not None, (
        "substrate.self_state should be seeded by the substrate-skeleton migration"
    )

    await _seed_passed_unconsolidated(
        substrate, self_state.stream_id,
        ingest_offset_seconds=86400.0,  # 1 day old — well past any window
        salience=0.5,
    )

    curator = _curator_no_cooldown(substrate)
    alarmed = await curator._alarm_pathological()
    # Old, unconsolidated, on substrate.self_state → still NOT alarmed.
    assert alarmed == []


@pytest.mark.asyncio
async def test_alarm_does_not_fire_for_consolidated(substrate):
    import thoth_db

    profile_id = await _register_profile_with_short_window(
        substrate.pool, "test-alarm-consolidated", window_seconds=60,
    )
    stream = await substrate.streams.register(
        name="thoth.test.alarm_consolidated",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )
    slice_id = await _seed_passed_unconsolidated(
        substrate, stream.stream_id,
        ingest_offset_seconds=120.0, salience=0.4,
    )
    # Flip to consolidated.
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE substrate_slices SET consolidation_state='consolidated' WHERE slice_id = $1",
            slice_id,
        )

    curator = Curator(substrate)
    alarmed = await curator._alarm_pathological()
    assert alarmed == []


@pytest.mark.asyncio
async def test_alarm_does_not_fire_within_window(substrate):
    profile_id = await _register_profile_with_short_window(
        substrate.pool, "test-alarm-young", window_seconds=600,
    )
    stream = await substrate.streams.register(
        name="thoth.test.alarm_young",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )
    # 60s old, 600s window — well within consolidation_window.
    await _seed_passed_unconsolidated(
        substrate, stream.stream_id,
        ingest_offset_seconds=60.0, salience=0.4,
    )
    curator = Curator(substrate)
    alarmed = await curator._alarm_pathological()
    assert alarmed == []


@pytest.mark.asyncio
async def test_alarm_bounded_by_batch_limit(substrate):
    """250 alarming slices, one tick emits exactly 100 (ALARM_BATCH_LIMIT)."""
    profile_id = await _register_profile_with_short_window(
        substrate.pool, "test-alarm-bounded", window_seconds=60,
    )
    stream = await substrate.streams.register(
        name="thoth.test.alarm_bounded",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )
    for _ in range(150):
        await _seed_passed_unconsolidated(
            substrate, stream.stream_id,
            ingest_offset_seconds=120.0, salience=0.4,
        )

    curator = _curator_no_cooldown(substrate)
    alarmed = await curator._alarm_pathological()
    assert len(alarmed) == Curator.ALARM_BATCH_LIMIT  # 100
