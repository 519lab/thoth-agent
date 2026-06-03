"""Tests for :func:`substrate.l0.api.commit_slice` (and its sync facade).

Covers the contract from Phase A spec §4.2:
* Pending state + RETURNING semantics.
* Stream lifecycle gate (only ACTIVE).
* Modality validation per stream contract.
* event-time skew + TZ-awareness checks.
* Optional ``conn=`` shares the caller's transaction (atomicity test).
* Sync facade behavior + run-loop-guard.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.l0 import commit_slice, commit_slice_sync
from substrate.storage import (
    DEFAULT_STRUCTURED_PROFILE,
    DEFAULT_TEXT_PROFILE,
    Family,
    Lifecycle,
    Modality,
    SentinelState,
)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def substrate(hermes_db_initialized):
    """A Substrate constructed from the test's thoth_db pool, no
    sub-agents started (we just need the L0 surface)."""
    import thoth_db

    return Substrate.from_pool(thoth_db.pool())


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Happy path — pending slice land.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_creates_pending_slice(substrate):
    stream = await substrate.streams.register(
        name="hermes.test.commit_pending",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )
    now = _now_utc()
    address = await commit_slice(
        substrate,
        stream.stream_id,
        {"hello": "world"},
        event_time_world=now,
        metadata={"test": True},
    )
    assert address.stream_id == stream.stream_id
    assert address.time_start_world == now

    # Verify the row exists with the expected shape. Look up by
    # stream_id only — ``commit`` may cap ``event_time_world`` to PG's
    # ``now()`` when the host clock is slightly ahead (CHECK-constraint
    # safety net), so equality on the original ``now`` value isn't
    # guaranteed.
    import thoth_db

    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT sentinel_state, payload, payload_modality, trust_score,
                   pending_committed_at IS NOT NULL AS pending_committed_at_set,
                   salience_score, metadata
              FROM substrate_slices
             WHERE stream_id = $1
             ORDER BY ingest_time_world DESC
             LIMIT 1
            """,
            stream.stream_id,
        )
    assert row is not None
    assert row["sentinel_state"] == "pending"
    assert row["payload"] == {"hello": "world"}
    assert row["payload_modality"] == "structured_event"
    assert row["trust_score"] is None
    assert row["pending_committed_at_set"] is True
    assert row["salience_score"] == pytest.approx(1.0)
    assert row["metadata"] == {"test": True}


@pytest.mark.asyncio
async def test_commit_text_wraps_payload_uniformly(substrate):
    """TEXT modality wraps str → ``{"text": "..."}`` so retrieval is
    uniform across modalities."""
    stream = await substrate.streams.register(
        name="hermes.test.text_wrap",
        family=Family.EXTEROCEPTIVE,
        modality=Modality.TEXT,
        source="cli",
        organ="gateway.cli",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    now = _now_utc()
    await commit_slice(substrate, stream.stream_id, "hello", event_time_world=now)

    import thoth_db

    async with thoth_db.connection() as conn:
        payload = await conn.fetchval(
            """
            SELECT payload FROM substrate_slices
             WHERE stream_id = $1
             ORDER BY ingest_time_world DESC
             LIMIT 1
            """,
            stream.stream_id,
        )
    # Uniform wrap: TEXT modality stores as ``{"text": "..."}``.
    assert payload == {"text": "hello"}


# ---------------------------------------------------------------------------
# Stream lifecycle.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_rejects_unknown_stream(substrate):
    with pytest.raises(ValueError, match="unknown stream_id"):
        await commit_slice(
            substrate, uuid4(), "hi", event_time_world=_now_utc()
        )


@pytest.mark.asyncio
async def test_commit_rejects_inactive_stream(substrate):
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.paused",
        family=Family.SELF_STATE,
        modality=Modality.TEXT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
        lifecycle_state=Lifecycle.PAUSED,
    )
    substrate.streams.invalidate(stream.stream_id)
    with pytest.raises(ValueError, match="not 'active'"):
        await commit_slice(
            substrate, stream.stream_id, "hi", event_time_world=_now_utc()
        )

    # And no row was inserted (the lifecycle check runs before INSERT).
    async with thoth_db.connection() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM substrate_slices WHERE stream_id = $1",
            stream.stream_id,
        )
    assert count == 0


# ---------------------------------------------------------------------------
# Modality validation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_text_rejects_dict_payload(substrate):
    stream = await substrate.streams.register(
        name="hermes.test.text_only",
        family=Family.EXTEROCEPTIVE,
        modality=Modality.TEXT,
        source="cli",
        organ="gateway.cli",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    with pytest.raises(TypeError, match="TEXT modality"):
        await commit_slice(
            substrate,
            stream.stream_id,
            {"oops": "wrong"},
            event_time_world=_now_utc(),
        )


@pytest.mark.asyncio
async def test_commit_structured_rejects_str_payload(substrate):
    stream = await substrate.streams.register(
        name="hermes.test.structured_only",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )
    with pytest.raises(TypeError, match="STRUCTURED_EVENT modality"):
        await commit_slice(
            substrate,
            stream.stream_id,
            "not a dict",
            event_time_world=_now_utc(),
        )


# ---------------------------------------------------------------------------
# Time invariants.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_rejects_naive_event_time(substrate):
    stream = await substrate.streams.register(
        name="hermes.test.naive_time",
        family=Family.SELF_STATE,
        modality=Modality.TEXT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    with pytest.raises(TypeError, match="timezone-aware"):
        await commit_slice(
            substrate,
            stream.stream_id,
            "hi",
            event_time_world=datetime.utcnow(),  # naive — deliberately
        )


@pytest.mark.asyncio
async def test_commit_rejects_clock_skew(substrate):
    stream = await substrate.streams.register(
        name="hermes.test.clock_skew",
        family=Family.SELF_STATE,
        modality=Modality.TEXT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    way_future = _now_utc() + timedelta(hours=1)
    with pytest.raises(ValueError, match="clock skew"):
        await commit_slice(
            substrate, stream.stream_id, "hi", event_time_world=way_future
        )


# ---------------------------------------------------------------------------
# Shared transaction via conn=.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_with_conn_rolls_back_on_outer_failure(substrate):
    """If the caller passes ``conn`` and the outer transaction rolls
    back, the slice INSERT is rolled back too. This is the atomicity
    guarantee that lets ``on_session_start`` share a txn with the
    ``sessions`` row INSERT."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.shared_txn",
        family=Family.SELF_STATE,
        modality=Modality.TEXT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    now = _now_utc()

    # Try-except on the outer txn — we deliberately raise inside the
    # block to force ROLLBACK.
    try:
        async with thoth_db.transaction() as conn:
            await commit_slice(
                substrate,
                stream.stream_id,
                "ephemeral",
                event_time_world=now,
                conn=conn,
            )
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass

    async with thoth_db.connection() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM substrate_slices WHERE stream_id = $1",
            stream.stream_id,
        )
    assert count == 0, "slice should have been rolled back with outer txn"


# ---------------------------------------------------------------------------
# Sync facade.
# ---------------------------------------------------------------------------


def test_commit_slice_sync_works_from_sync_context(thoth_db_dsn):
    """Sync facade from a sync test function: must work without
    raising. (Inside an event loop it would raise — covered by the
    next test.)

    We deliberately AVOID the async ``hermes_db_initialized`` fixture
    here: that fixture awaits ``thoth_db.init`` inside pytest-asyncio's
    per-test loop, which binds the asyncpg pool to that loop. The sync
    facade would then drive the pool from the persistent ``_sync_loop``
    instead — a cross-loop access that asyncpg flags as "another
    operation is in progress". Using the sync ``thoth_db_dsn`` fixture
    + ``ensure_pool_sync()`` keeps the pool on the persistent loop end
    to end.
    """
    import os

    import thoth_db
    from substrate.facade import Substrate

    # Manually init the pool on the persistent sync loop.
    os.environ["THOTH_PG_DSN"] = thoth_db_dsn
    os.environ["HERMES_PG_DSN"] = thoth_db_dsn
    assert thoth_db.ensure_pool_sync() is True
    try:
        substrate = Substrate.from_pool(thoth_db.pool())

        async def _setup():
            return await substrate.streams.register(
                name="hermes.test.sync_facade",
                family=Family.SELF_STATE,
                modality=Modality.TEXT,
                source="test",
                organ="pytest",
                decay_profile_id=DEFAULT_TEXT_PROFILE,
            )

        stream = thoth_db.run_sync(_setup())
        address = commit_slice_sync(
            substrate,
            stream.stream_id,
            "sync hello",
            event_time_world=datetime.now(timezone.utc),
        )
        assert address.stream_id == stream.stream_id
    finally:
        thoth_db.run_sync(thoth_db.close())


@pytest.mark.asyncio
async def test_commit_slice_sync_inside_event_loop_does_not_deadlock(substrate):
    """From inside a running event loop, the sync facade must not
    deadlock the loop. Two acceptable outcomes:

    * ``RuntimeError`` from the cross-loop guard (legacy behaviour
      before the worker-thread offload landed).
    * ``asyncpg.InterfaceError`` (or any non-RuntimeError) when the
      pool is bound to a different loop than the offload's
      ``_sync_loop`` — happens in this test because the ``substrate``
      fixture initialises the pool on pytest-asyncio's per-test loop
      via ``hermes_db_initialized``. The offload routes the call to
      ``_sync_loop``; asyncpg's loop-bound connection refuses, but
      raises promptly rather than hanging.
    * **Successful commit** when the pool is on ``_sync_loop``
      (the production binding via ``ensure_pool_sync``) — the offload
      legitimately works there and the slice persists.

    All three outcomes prove the invariant: ``commit_slice_sync``
    from inside an async context **returns** (success or exception)
    rather than spinning the loop.
    """
    stream = await substrate.streams.register(
        name="hermes.test.sync_in_async",
        family=Family.SELF_STATE,
        modality=Modality.TEXT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    try:
        commit_slice_sync(
            substrate,
            stream.stream_id,
            "may or may not fly",
            event_time_world=_now_utc(),
        )
    except Exception:
        # Any exception is acceptable; the only failure mode this test
        # is guarding against is the loop deadlocking and the test
        # timing out (pytest-timeout would fire after 30s).
        pass
