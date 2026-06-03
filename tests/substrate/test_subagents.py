"""Tests for the four Phase A sub-agent stubs:

* StubSentinel — passes every pending slice with a modality-derived
  trust score; records a batch-summary audit in substrate_telemetry.
* ForceRejectWorker — deletes slices past their stream's pending_ttl;
  records a force_reject_ttl audit (telemetry) per dropped slice.
* PartitionMaintenanceWorker — ensures rolling-window monthly
  partitions; fixed-cadence 24h tick regardless of intensity.
* StubConductor — pure data holder, per-agent intensity levels.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.agents import (
    ForceRejectWorker,
    Level,
    PartitionMaintenanceWorker,
    StubConductor,
    StubSentinel,
    _trust_for_modality,
)
from substrate.l0 import commit_slice
from substrate.storage import (
    DEFAULT_STRUCTURED_PROFILE,
    DEFAULT_TEXT_PROFILE,
    Family,
    Modality,
    SentinelState,
)


@pytest_asyncio.fixture
async def substrate(hermes_db_initialized):
    import thoth_db

    return Substrate.from_pool(thoth_db.pool())


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# StubSentinel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sentinel_transitions_pending_to_passed(substrate):
    """One tick passes every pending slice with a non-null trust
    score."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.sentinel_pass",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )
    for i in range(3):
        await commit_slice(
            substrate,
            stream.stream_id,
            {"i": i},
            event_time_world=_now_utc(),
        )

    sentinel = StubSentinel(substrate)
    await sentinel.tick()

    async with thoth_db.connection() as conn:
        rows = await conn.fetch(
            """
            SELECT sentinel_state, trust_score, pending_committed_at
              FROM substrate_slices
             WHERE stream_id = $1
            """,
            stream.stream_id,
        )
    assert len(rows) == 3
    for r in rows:
        assert r["sentinel_state"] == "passed"
        assert r["trust_score"] is not None
        # Trust score for structured_event = 0.7.
        assert r["trust_score"] == pytest.approx(0.7)
        # Decision clears the pending timestamp.
        assert r["pending_committed_at"] is None


@pytest.mark.asyncio
async def test_sentinel_emits_batch_summary(substrate):
    """After the batch tick, a ``sentinel_batch_decision`` telemetry row
    records the decided slice IDs (operational telemetry, not a slice)."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.sentinel_audit",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )
    await commit_slice(
        substrate, stream.stream_id, {"x": 1}, event_time_world=_now_utc()
    )

    sentinel = StubSentinel(substrate)
    await sentinel.tick()

    async with thoth_db.connection() as conn:
        audit = await conn.fetchrow(
            """
            SELECT agent, event, payload
              FROM substrate_telemetry
             WHERE event = 'sentinel_batch_decision'
             ORDER BY at DESC
             LIMIT 1
            """
        )
    assert audit is not None
    assert audit["agent"] == "sentinel"
    payload = audit["payload"]
    assert payload["count"] >= 1
    assert isinstance(payload["slice_ids"], list)


@pytest.mark.asyncio
async def test_sentinel_audit_is_telemetry_not_a_slice(substrate):
    """Regression (2026-05-26→27 incident): the Sentinel's
    ``sentinel_batch_decision`` audit must NOT be a slice on
    ``substrate.self_state``. It used to be a born-pending L0 slice the
    next tick re-decided → audits-of-audits → 414k-slice runaway. It is now
    a ``substrate_telemetry`` row, so it can never re-enter the pending
    queue.

    Commit one ordinary pending slice, tick the Sentinel twice, and assert
    the audit is a telemetry row (not a self_state slice) and the second
    tick adds no new audit (nothing pending left to re-decide).
    """
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.sentinel_no_reentry",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )
    await commit_slice(
        substrate, stream.stream_id, {"x": 1}, event_time_world=_now_utc()
    )

    self_state = await substrate.streams.get_by_name("substrate.self_state")

    sentinel = StubSentinel(substrate)
    # First tick decides the seeded slice + records one telemetry audit.
    await sentinel.tick()
    async with thoth_db.connection() as conn:
        telem_after_first = await conn.fetchval(
            "SELECT count(*) FROM substrate_telemetry "
            "WHERE event = 'sentinel_batch_decision'"
        )
        # The audit is NOT a slice: nothing on substrate.self_state, and no
        # pending slices anywhere (so list_pending can't pick an audit up).
        self_state_slices = await conn.fetchval(
            "SELECT count(*) FROM substrate_slices WHERE stream_id = $1",
            self_state.stream_id,
        )
        pending_any = await conn.fetchval(
            "SELECT count(*) FROM substrate_slices WHERE sentinel_state = 'pending'"
        )
    assert telem_after_first == 1, "First tick recorded exactly one telemetry audit"
    assert self_state_slices == 0, (
        "audit must be a telemetry row, not a substrate.self_state slice"
    )
    assert pending_any == 0, (
        "no slice left pending → no audit-of-audit re-entry is possible"
    )

    # Second tick: nothing pending to decide → returns before emitting.
    await sentinel.tick()
    async with thoth_db.connection() as conn:
        telem_after_second = await conn.fetchval(
            "SELECT count(*) FROM substrate_telemetry "
            "WHERE event = 'sentinel_batch_decision'"
        )
    assert telem_after_second == 1, (
        f"Second Sentinel tick recorded another audit (total now "
        f"{telem_after_second}) — the audit-of-audit loop is back."
    )


@pytest.mark.asyncio
async def test_sentinel_empty_pending_queue_is_noop(substrate):
    """A tick with no pending slices does not record an audit."""
    import thoth_db

    async with thoth_db.connection() as conn:
        before = await conn.fetchval(
            "SELECT count(*) FROM substrate_telemetry "
            "WHERE event = 'sentinel_batch_decision'"
        )

    sentinel = StubSentinel(substrate)
    await sentinel.tick()

    async with thoth_db.connection() as conn:
        after = await conn.fetchval(
            "SELECT count(*) FROM substrate_telemetry "
            "WHERE event = 'sentinel_batch_decision'"
        )
    assert after == before


@pytest.mark.asyncio
async def test_sentinel_concurrent_ticks_no_double_decide(substrate):
    """Two concurrent Sentinel ticks against the same pending queue
    must not double-decide rows. SKIP LOCKED on ``list_pending`` is
    the contract that makes this safe.
    """
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.sentinel_concurrent",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )
    for i in range(10):
        await commit_slice(
            substrate, stream.stream_id, {"i": i}, event_time_world=_now_utc()
        )

    s1 = StubSentinel(substrate)
    s2 = StubSentinel(substrate)
    await asyncio.gather(s1.tick(), s2.tick())

    async with thoth_db.connection() as conn:
        rows = await conn.fetch(
            """
            SELECT sentinel_state, trust_score
              FROM substrate_slices
             WHERE stream_id = $1
            """,
            stream.stream_id,
        )
    # All 10 slices passed exactly once (decide_many UPDATE is
    # idempotent on the WHERE sentinel_state='pending' guard).
    assert len(rows) == 10
    for r in rows:
        assert r["sentinel_state"] == "passed"
        assert r["trust_score"] is not None


def test_trust_for_modality():
    """Pure-Python sanity check on the modality → trust mapping."""
    assert _trust_for_modality(Modality.TEXT) == 0.5
    assert _trust_for_modality(Modality.STRUCTURED_EVENT) == 0.7
    assert _trust_for_modality(Modality.SIGNAL) == 0.3


# ---------------------------------------------------------------------------
# ForceRejectWorker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_reject_deletes_expired_pending(substrate):
    """Slice past TTL is force-rejected + audit emitted."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.force_reject",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,  # 15s pending_ttl
    )
    address = await commit_slice(
        substrate, stream.stream_id, {"doomed": True}, event_time_world=_now_utc()
    )
    # Backdate so the TTL has expired without waiting.
    async with thoth_db.connection() as conn:
        slice_id = await conn.fetchval(
            "SELECT slice_id FROM substrate_slices WHERE stream_id = $1",
            stream.stream_id,
        )
        await conn.execute(
            """
            UPDATE substrate_slices
               SET pending_committed_at = now() - INTERVAL '1 minute'
             WHERE slice_id = $1
            """,
            slice_id,
        )

    worker = ForceRejectWorker(substrate)
    await worker.tick()

    async with thoth_db.connection() as conn:
        gone = await conn.fetchval(
            "SELECT count(*) FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
    assert gone == 0  # row deleted

    # Audit recorded as a force_reject_ttl telemetry row (non-perceptual).
    async with thoth_db.connection() as conn:
        audit = await conn.fetchrow(
            """
            SELECT agent, event, payload
              FROM substrate_telemetry
             WHERE event = 'force_reject_ttl'
             ORDER BY at DESC
             LIMIT 1
            """
        )
    assert audit is not None
    assert audit["agent"] == "force-reject"
    assert audit["payload"]["slice_id"] == str(slice_id)


@pytest.mark.asyncio
async def test_force_reject_leaves_unexpired_pending(substrate):
    """Slice within TTL is NOT touched."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.force_reject_healthy",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )
    await commit_slice(
        substrate, stream.stream_id, {"alive": True}, event_time_world=_now_utc()
    )

    worker = ForceRejectWorker(substrate)
    await worker.tick()

    async with thoth_db.connection() as conn:
        survives = await conn.fetchval(
            """
            SELECT sentinel_state FROM substrate_slices WHERE stream_id = $1
            """,
            stream.stream_id,
        )
    assert survives == "pending"


@pytest.mark.asyncio
async def test_force_reject_empty_queue_no_audit(substrate):
    """No expired rows → no force_reject_ttl telemetry recorded."""
    import thoth_db

    async with thoth_db.connection() as conn:
        before = await conn.fetchval(
            "SELECT count(*) FROM substrate_telemetry WHERE event = 'force_reject_ttl'"
        )
    worker = ForceRejectWorker(substrate)
    await worker.tick()
    async with thoth_db.connection() as conn:
        after = await conn.fetchval(
            "SELECT count(*) FROM substrate_telemetry WHERE event = 'force_reject_ttl'"
        )
    assert after == before


# ---------------------------------------------------------------------------
# PartitionMaintenanceWorker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partition_maintenance_creates_ahead_window(substrate):
    """One tick creates partitions for current month + 2 ahead."""
    import thoth_db
    from substrate.storage.partitions import list_existing_partitions

    worker = PartitionMaintenanceWorker(substrate, ahead_months=2)
    await worker.tick()

    async with thoth_db.connection() as conn:
        names = await list_existing_partitions(conn)

    # Default partition + at least 3 month partitions present.
    assert "substrate_slices_default" in names
    month_partitions = [n for n in names if n != "substrate_slices_default"]
    assert len(month_partitions) >= 3


def test_partition_maintenance_interval_is_daily_regardless_of_level():
    """The static ``_interval_for`` override returns 24h for every
    non-OFF level."""
    assert PartitionMaintenanceWorker._interval_for(Level.LOW) == 86400.0
    assert PartitionMaintenanceWorker._interval_for(Level.HIGH) == 86400.0
    assert PartitionMaintenanceWorker._interval_for(Level.FULL) == 86400.0
    # OFF still halts the worker.
    assert PartitionMaintenanceWorker._interval_for(Level.OFF) is None


# ---------------------------------------------------------------------------
# StubConductor
# ---------------------------------------------------------------------------


class TestStubConductor:
    def test_intensity_for_unset_agent_defaults_to_low(self):
        # substrate=None — Conductor doesn't actually touch the
        # substrate facade until Phase B+.
        conductor = StubConductor(substrate=None)
        assert conductor.intensity_for("anyone") is Level.LOW

    def test_intensity_for_sentinel_defaults_to_full(self):
        conductor = StubConductor(substrate=None)
        assert conductor.intensity_for("sentinel", is_sentinel=True) is Level.FULL

    def test_set_and_get_intensity_round_trips(self):
        conductor = StubConductor(substrate=None)
        conductor.set_intensity("force-reject", Level.HIGH)
        assert conductor.intensity_for("force-reject") is Level.HIGH

    def test_snapshot_returns_copy(self):
        conductor = StubConductor(substrate=None)
        conductor.set_intensity("a", Level.MODERATE)
        conductor.set_intensity("b", Level.HIGH)
        snap = conductor.snapshot()
        assert snap == {"a": Level.MODERATE, "b": Level.HIGH}
        # Mutating the snapshot does not affect the conductor's state.
        snap["c"] = Level.FULL
        assert "c" not in conductor.snapshot()
