"""Repository tests for Phase A Task 6.

Covers ``DecayProfileRepo``, ``StreamRepo``, and ``SliceRepo`` against
the real PG fixture. The high-level :func:`commit_slice` API (Task 7)
gets its own test file; here we exercise the repos directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from substrate.storage import (
    DEFAULT_STRUCTURED_PROFILE,
    DEFAULT_TEXT_PROFILE,
    DecayProfileRepo,
    Family,
    Lifecycle,
    Modality,
    SentinelState,
    SliceRepo,
    StreamRepo,
)


# ---------------------------------------------------------------------------
# DecayProfileRepo — read-only against seeded rows.
# ---------------------------------------------------------------------------


class TestDecayProfileRepo:
    @pytest.mark.asyncio
    async def test_get_seeded_profile_by_id(self, thoth_db_initialized):
        import thoth_db

        repo = DecayProfileRepo(thoth_db.pool())
        profile = await repo.get(DEFAULT_TEXT_PROFILE)
        assert profile is not None
        assert profile.name == "default-text"
        assert profile.natural_half_life == timedelta(hours=1)
        assert profile.applies_to_modality is Modality.TEXT

    @pytest.mark.asyncio
    async def test_get_by_name(self, thoth_db_initialized):
        import thoth_db

        repo = DecayProfileRepo(thoth_db.pool())
        profile = await repo.get_by_name("default-structured")
        assert profile is not None
        assert profile.profile_id == DEFAULT_STRUCTURED_PROFILE
        assert profile.consolidation_window == timedelta(minutes=5)

    @pytest.mark.asyncio
    async def test_get_unknown_returns_none(self, thoth_db_initialized):
        import thoth_db

        repo = DecayProfileRepo(thoth_db.pool())
        profile = await repo.get(uuid4())
        assert profile is None

    def test_default_for_modality_returns_stable_uuid(self):
        assert DecayProfileRepo.default_for_modality(Modality.TEXT) == DEFAULT_TEXT_PROFILE
        assert (
            DecayProfileRepo.default_for_modality(Modality.STRUCTURED_EVENT)
            == DEFAULT_STRUCTURED_PROFILE
        )


# ---------------------------------------------------------------------------
# StreamRepo — registration + cache behavior.
# ---------------------------------------------------------------------------


class TestStreamRepo:
    @pytest.mark.asyncio
    async def test_register_returns_new_stream(self, thoth_db_initialized):
        import thoth_db

        repo = StreamRepo(thoth_db.pool())
        stream = await repo.register(
            name="thoth.test.unique_stream",
            family=Family.EXTEROCEPTIVE,
            modality=Modality.TEXT,
            source="test",
            organ="pytest",
            decay_profile_id=DEFAULT_TEXT_PROFILE,
        )
        assert stream.name == "thoth.test.unique_stream"
        assert stream.lifecycle_state is Lifecycle.ACTIVE
        assert stream.family is Family.EXTEROCEPTIVE

    @pytest.mark.asyncio
    async def test_register_is_idempotent_on_name_conflict(self, thoth_db_initialized):
        import thoth_db

        repo = StreamRepo(thoth_db.pool())
        first = await repo.register(
            name="thoth.test.dupe",
            family=Family.SELF_STATE,
            modality=Modality.STRUCTURED_EVENT,
            source="test",
            organ="pytest",
            decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
        )
        # Same name + different params → no error, returns the original.
        second = await repo.register(
            name="thoth.test.dupe",
            family=Family.SELF_ACTION,  # would-be conflict
            modality=Modality.TEXT,
            source="other",
            organ="other",
            decay_profile_id=DEFAULT_TEXT_PROFILE,
        )
        assert second.stream_id == first.stream_id
        # First-wins semantics: family from the *first* INSERT survives.
        assert second.family is Family.SELF_STATE

    @pytest.mark.asyncio
    async def test_get_hits_cache_on_second_read(self, thoth_db_initialized):
        import thoth_db

        repo = StreamRepo(thoth_db.pool())
        stream = await repo.register(
            name="thoth.test.cache_probe",
            family=Family.SELF_STATE,
            modality=Modality.TEXT,
            source="test",
            organ="pytest",
            decay_profile_id=DEFAULT_TEXT_PROFILE,
        )
        # Pre-register populates the cache (StreamRepo._remember).
        cached = await repo.get(stream.stream_id)
        # Identity comparison — same Python object means we hit the cache.
        assert cached is stream

    @pytest.mark.asyncio
    async def test_get_by_name_uses_reverse_index(self, thoth_db_initialized):
        import thoth_db

        repo = StreamRepo(thoth_db.pool())
        stream = await repo.register(
            name="thoth.test.by_name",
            family=Family.SELF_STATE,
            modality=Modality.TEXT,
            source="test",
            organ="pytest",
            decay_profile_id=DEFAULT_TEXT_PROFILE,
        )
        cached = await repo.get_by_name("thoth.test.by_name")
        assert cached is stream

    @pytest.mark.asyncio
    async def test_invalidate_drops_cache_entry(self, thoth_db_initialized):
        import thoth_db

        repo = StreamRepo(thoth_db.pool())
        stream = await repo.register(
            name="thoth.test.invalidate",
            family=Family.SELF_STATE,
            modality=Modality.TEXT,
            source="test",
            organ="pytest",
            decay_profile_id=DEFAULT_TEXT_PROFILE,
        )
        repo.invalidate(stream.stream_id)
        # Next read must hit the DB, returning a fresh Stream instance
        # (identity comparison no longer holds).
        fresh = await repo.get(stream.stream_id)
        assert fresh is not None
        assert fresh is not stream
        # But equality by stream_id still holds.
        assert fresh.stream_id == stream.stream_id


# ---------------------------------------------------------------------------
# SliceRepo — commit + list + decide + force_reject_expired.
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_slice_repo_commit_inserts_pending(thoth_db_initialized):
    import thoth_db
    from substrate.storage import DEFAULT_STRUCTURED_PROFILE

    streams = StreamRepo(thoth_db.pool())
    slices = SliceRepo(thoth_db.pool())
    stream = await streams.register(
        name="thoth.test.commit_pending",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )
    now = _now_utc()

    async with thoth_db.connection() as conn:
        sid, ingest = await slices.commit(
            conn=conn,
            stream_id=stream.stream_id,
            time_start_world=now,
            time_end_world=now,
            event_time_world=now,
            perception_time_world=now,
            payload={"hello": "world"},
            payload_blob_ref=None,
            payload_modality=Modality.STRUCTURED_EVENT,
            metadata={"test": True},
        )
        s = await slices.get_by_id(conn, sid)

    assert s is not None
    assert s.sentinel_state is SentinelState.PENDING
    assert s.payload == {"hello": "world"}
    assert s.metadata == {"test": True}
    assert s.salience_score == 1.0
    assert s.trust_score is None
    assert s.pending_committed_at is not None
    assert s.ingest_time_world == ingest


@pytest.mark.asyncio
async def test_slice_repo_list_pending_orders_oldest_first(thoth_db_initialized):
    import asyncio

    import thoth_db

    streams = StreamRepo(thoth_db.pool())
    slices = SliceRepo(thoth_db.pool())
    stream = await streams.register(
        name="thoth.test.list_pending",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )

    sids = []
    async with thoth_db.connection() as conn:
        for i in range(3):
            now = _now_utc()
            sid, _ = await slices.commit(
                conn=conn,
                stream_id=stream.stream_id,
                time_start_world=now,
                time_end_world=now,
                event_time_world=now,
                perception_time_world=now,
                payload={"i": i},
                payload_blob_ref=None,
                payload_modality=Modality.STRUCTURED_EVENT,
                metadata={},
            )
            sids.append(sid)
            await asyncio.sleep(0.005)  # space out pending_committed_at
        pending = await slices.list_pending(conn, limit=10)

    # Oldest first — order is by pending_committed_at, set at INSERT.
    # Restrict to just the slices we created (the DB may carry other
    # rows from earlier tests in the same session).
    ours = [s for s in pending if s.slice_id in set(sids)]
    assert [s.slice_id for s in ours] == sids


@pytest.mark.asyncio
async def test_slice_repo_decide_transitions_pending_to_passed(thoth_db_initialized):
    import thoth_db

    streams = StreamRepo(thoth_db.pool())
    slices = SliceRepo(thoth_db.pool())
    stream = await streams.register(
        name="thoth.test.decide_one",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )
    now = _now_utc()

    async with thoth_db.connection() as conn:
        sid, _ = await slices.commit(
            conn=conn,
            stream_id=stream.stream_id,
            time_start_world=now,
            time_end_world=now,
            event_time_world=now,
            perception_time_world=now,
            payload={},
            payload_blob_ref=None,
            payload_modality=Modality.STRUCTURED_EVENT,
            metadata={},
        )
        await slices.decide(
            conn,
            sid,
            outcome=SentinelState.PASSED,
            trust_score=0.75,
            reason=None,
        )
        s = await slices.get_by_id(conn, sid)

    assert s is not None
    assert s.sentinel_state is SentinelState.PASSED
    assert s.trust_score == pytest.approx(0.75)
    assert s.pending_committed_at is None  # cleared on decision


@pytest.mark.asyncio
async def test_slice_repo_decide_rejects_pending_outcome(thoth_db_initialized):
    """``decide`` only accepts PASSED or QUARANTINED — PENDING is not a
    legal target (Sentinel only moves *away* from pending)."""
    import thoth_db

    slices = SliceRepo(thoth_db.pool())
    async with thoth_db.connection() as conn:
        with pytest.raises(ValueError, match="invalid sentinel outcome"):
            await slices.decide(
                conn,
                uuid4(),
                outcome=SentinelState.PENDING,
                trust_score=0.5,
            )


@pytest.mark.asyncio
async def test_slice_repo_decide_many_batch(thoth_db_initialized):
    import thoth_db

    streams = StreamRepo(thoth_db.pool())
    slices = SliceRepo(thoth_db.pool())
    stream = await streams.register(
        name="thoth.test.decide_many",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )
    now = _now_utc()

    sids = []
    async with thoth_db.connection() as conn:
        for _ in range(5):
            sid, _ = await slices.commit(
                conn=conn,
                stream_id=stream.stream_id,
                time_start_world=now,
                time_end_world=now,
                event_time_world=now,
                perception_time_world=now,
                payload={},
                payload_blob_ref=None,
                payload_modality=Modality.STRUCTURED_EVENT,
                metadata={},
            )
            sids.append(sid)

        decisions = [
            (sid, SentinelState.PASSED, 0.6, None) for sid in sids
        ]
        count = await slices.decide_many(conn, decisions)
        assert count == 5

        # All five are now passed.
        for sid in sids:
            s = await slices.get_by_id(conn, sid)
            assert s is not None
            assert s.sentinel_state is SentinelState.PASSED


@pytest.mark.asyncio
async def test_slice_repo_force_reject_expired_uses_ttl(thoth_db_initialized):
    """A slice past its stream's ``pending_ttl`` is force-rejected.

    The default-structured profile has ``pending_ttl = 15 seconds``. We
    backdate ``pending_committed_at`` directly via UPDATE so the test
    doesn't need to sleep 16 seconds; the worker only cares about
    ``pending_committed_at + dp.pending_ttl < now()``.
    """
    import thoth_db
    from datetime import timedelta as td

    streams = StreamRepo(thoth_db.pool())
    slices = SliceRepo(thoth_db.pool())
    stream = await streams.register(
        name="thoth.test.force_reject",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )
    now = _now_utc()

    async with thoth_db.connection() as conn:
        sid, _ = await slices.commit(
            conn=conn,
            stream_id=stream.stream_id,
            time_start_world=now,
            time_end_world=now,
            event_time_world=now,
            perception_time_world=now,
            payload={},
            payload_blob_ref=None,
            payload_modality=Modality.STRUCTURED_EVENT,
            metadata={},
        )
        # Backdate pending_committed_at by 1 minute — well past the 15s
        # TTL on default-structured.
        await conn.execute(
            """
            UPDATE substrate_slices
               SET pending_committed_at = now() - INTERVAL '1 minute'
             WHERE slice_id = $1
            """,
            sid,
        )

        deleted = await slices.force_reject_expired(conn, limit=10)
        deleted_ids = {s.slice_id for s in deleted}
        assert sid in deleted_ids

        gone = await slices.get_by_id(conn, sid)
        assert gone is None
