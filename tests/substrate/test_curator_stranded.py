"""Stranded-slice drain — issue #287.

A passed + unconsolidated slice older than the Parser's 7-day fetch
horizon can never consolidate, and under ``release_after_consolidation``
profiles can never release — an immortal slice the pathological-forgetting
alarm re-finds every cooldown, forever (live: 1,602 alarms over 100 slices,
holding the Critic's ``alarms_1h`` coherence penalty at -0.2 permanently).

Contract under test:
* slices past ``STRANDED_DRAIN_SECONDS`` are tombstoned once
  (``mark_slices_consolidated(ids, [])``) and audited once
  (``curator.slice_stranded`` telemetry) — never re-drained;
* the alarm no longer fires on the beyond-horizon set (disjoint stages);
* ``substrate.*`` streams stay untouched;
* a drained slice becomes release-eligible, so decay actually retires it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.agents import Curator
from substrate.l0 import commit_slice
from substrate.storage import Family, Modality


@pytest_asyncio.fixture
async def substrate(thoth_db_initialized):
    import thoth_db

    return Substrate.from_pool(thoth_db.pool())


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _register_profile(
    pool,
    name: str,
    *,
    window_seconds: int = 60,
    release_after_consolidation: bool = False,
    min_salience: float = 0.05,
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
                 0.2, $4, $5, interval '30 seconds',
                 'thin', 'structured_event')
            """,
            profile_id,
            name,
            float(window_seconds),
            float(min_salience),
            release_after_consolidation,
        )
    return profile_id


async def _seed_passed_unconsolidated(
    substrate,
    stream_id: UUID,
    *,
    ingest_offset_seconds: float,
    salience: float = 0.4,
    session_id: str | None = None,
) -> UUID:
    """Commit + mark passed + age the row by ``ingest_offset_seconds``."""
    import thoth_db

    metadata = {"session_id": session_id} if session_id else None
    await commit_slice(
        substrate,
        stream_id,
        {"k": uuid4().hex[:6]},
        event_time_world=_now_utc(),
        metadata=metadata,
    )
    async with thoth_db.connection() as conn:
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
            stream_id,
            salience,
            float(ingest_offset_seconds),
        )
    return slice_id


async def _register_stream(substrate, profile_id: UUID, name: str):
    return await substrate.streams.register(
        name=name,
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )


def _curator(substrate, *, horizon_seconds: int = 3600) -> Curator:
    """Curator with a short drain horizon so tests age slices by hours,
    not days, and no alarm cooldown."""
    c = Curator(substrate)
    c.ALARM_COOLDOWN_SECONDS = 0
    c.STRANDED_DRAIN_SECONDS = horizon_seconds
    return c


@pytest.mark.asyncio
async def test_stranded_slice_is_drained(substrate):
    import thoth_db

    profile_id = await _register_profile(substrate.pool, "test-strand-drain")
    stream = await _register_stream(substrate, profile_id, "thoth.test.strand_drain")
    # 2h old vs a 1h drain horizon → stranded.
    slice_id = await _seed_passed_unconsolidated(
        substrate, stream.stream_id,
        ingest_offset_seconds=7200.0,
        session_id="sess-stranded",
    )

    curator = _curator(substrate)
    stranded = await curator._drain_stranded()
    assert [s["slice_id"] for s in stranded] == [slice_id]
    assert stranded[0]["session_id"] == "sess-stranded"

    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            "SELECT consolidation_state, consolidated_to FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
    assert row["consolidation_state"] == "consolidated"
    assert row["consolidated_to"] in ([], None, "[]")  # empty extraction tombstone


@pytest.mark.asyncio
async def test_stranded_drain_is_one_shot(substrate):
    profile_id = await _register_profile(substrate.pool, "test-strand-once")
    stream = await _register_stream(substrate, profile_id, "thoth.test.strand_once")
    await _seed_passed_unconsolidated(
        substrate, stream.stream_id, ingest_offset_seconds=7200.0
    )

    curator = _curator(substrate)
    assert len(await curator._drain_stranded()) == 1
    # Second pass: the slice is consolidated now — predicate can't match.
    assert await curator._drain_stranded() == []


@pytest.mark.asyncio
async def test_alarm_and_drain_are_disjoint(substrate):
    """Fresh-overdue slices alarm (and are NOT drained); beyond-horizon
    slices drain (and are NOT alarmed). Regression both ways."""
    profile_id = await _register_profile(
        substrate.pool, "test-strand-disjoint", window_seconds=60
    )
    stream = await _register_stream(substrate, profile_id, "thoth.test.strand_disjoint")
    old_id = await _seed_passed_unconsolidated(
        substrate, stream.stream_id, ingest_offset_seconds=7200.0
    )
    fresh_id = await _seed_passed_unconsolidated(
        substrate, stream.stream_id, ingest_offset_seconds=120.0
    )

    curator = _curator(substrate)  # horizon 1h

    alarmed = await curator._alarm_pathological()
    assert [a["slice_id"] for a in alarmed] == [fresh_id]

    stranded = await curator._drain_stranded()
    assert [s["slice_id"] for s in stranded] == [old_id]


@pytest.mark.asyncio
async def test_stranded_excludes_substrate_streams(substrate):
    self_state = await substrate.streams.get_by_name("substrate.self_state")
    assert self_state is not None
    await _seed_passed_unconsolidated(
        substrate, self_state.stream_id, ingest_offset_seconds=7200.0
    )

    curator = _curator(substrate)
    assert await curator._drain_stranded() == []


@pytest.mark.asyncio
async def test_drained_slice_becomes_release_eligible(substrate):
    """End-to-end immortality break: under a release_after_consolidation
    profile, a stranded slice at floor salience could never release. After
    the drain it is consolidated, so the release stage retires it."""
    import thoth_db

    profile_id = await _register_profile(
        substrate.pool,
        "test-strand-release",
        release_after_consolidation=True,
        min_salience=0.05,
    )
    stream = await _register_stream(substrate, profile_id, "thoth.test.strand_release")
    slice_id = await _seed_passed_unconsolidated(
        substrate, stream.stream_id,
        ingest_offset_seconds=7200.0,
        salience=0.0,  # fully decayed — but immortal pre-fix
    )

    curator = _curator(substrate)

    # Pre-fix state: not releasable while unconsolidated.
    await curator._evaluate_releases()
    async with thoth_db.connection() as conn:
        state = await conn.fetchval(
            "SELECT consolidation_state FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
    assert state == "unconsolidated"

    # Drain, then release.
    assert len(await curator._drain_stranded()) == 1
    await curator._evaluate_releases()
    async with thoth_db.connection() as conn:
        state = await conn.fetchval(
            "SELECT consolidation_state FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
    assert state == "released"


@pytest.mark.asyncio
async def test_stranded_stage_emits_one_telemetry_row_per_slice(substrate):
    import thoth_db

    profile_id = await _register_profile(substrate.pool, "test-strand-audit")
    stream = await _register_stream(substrate, profile_id, "thoth.test.strand_audit")
    slice_id = await _seed_passed_unconsolidated(
        substrate, stream.stream_id, ingest_offset_seconds=7200.0
    )

    curator = _curator(substrate)
    await curator._stranded_stage()
    # Re-run: no new rows (one-shot).
    await curator._stranded_stage()

    async with thoth_db.connection() as conn:
        rows = await conn.fetch(
            """
            SELECT payload FROM substrate_telemetry
             WHERE event = 'curator.slice_stranded'
               AND payload->>'slice_id' = $1
            """,
            str(slice_id),
        )
    assert len(rows) == 1
