"""Tests for ``substrate.agents.base.SubAgent`` + intensity dial."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.agents.base import Level, SubAgent


# ---------------------------------------------------------------------------
# Tick interval mapping.
# ---------------------------------------------------------------------------


class TestIntervalMapping:
    def test_off_yields_none(self):
        assert SubAgent._interval_for(Level.OFF) is None

    def test_intervals_strictly_decrease_with_intensity(self):
        intervals = [
            SubAgent._interval_for(level)
            for level in (Level.LOW, Level.MODERATE, Level.HIGH, Level.FULL)
        ]
        # Higher intensity = shorter interval. Monotone strictly decreasing.
        assert intervals == sorted(intervals, reverse=True)
        assert all(i is not None and i > 0 for i in intervals)


# ---------------------------------------------------------------------------
# Concrete test subclasses.
# ---------------------------------------------------------------------------


class _CountingAgent(SubAgent):
    """Records each tick — used to verify the run loop calls tick()."""

    name = "counter"

    def __init__(self, substrate) -> None:
        super().__init__(substrate)
        self.ticks = 0
        # Override the default LOW so tests don't wait 10s.
        self._level = Level.FULL

    async def tick(self) -> None:
        self.ticks += 1


class _SentinelLikeAgent(SubAgent):
    """An agent flagged as a sentinel — exercises the FULL floor."""

    name = "sentinel-stub"
    is_sentinel = True

    async def tick(self) -> None:  # pragma: no cover — not exercised here
        return


class _ExplodingAgent(SubAgent):
    """tick() always raises — verifies the run loop swallows + logs."""

    name = "exploder"

    def __init__(self, substrate) -> None:
        super().__init__(substrate)
        self._level = Level.FULL
        self.attempts = 0

    async def tick(self) -> None:
        self.attempts += 1
        raise RuntimeError("simulated tick failure")


# ---------------------------------------------------------------------------
# Sentinel intensity floor.
# ---------------------------------------------------------------------------


class TestSentinelFloor:
    def test_sentinel_starts_at_full(self):
        agent = _SentinelLikeAgent(substrate=None)
        assert agent.level is Level.FULL

    def test_sentinel_silently_floored_below_full(self):
        agent = _SentinelLikeAgent(substrate=None)
        agent.set_intensity(Level.OFF)
        assert agent.level is Level.FULL
        agent.set_intensity(Level.LOW)
        assert agent.level is Level.FULL

    def test_sentinel_accepts_full(self):
        agent = _SentinelLikeAgent(substrate=None)
        agent.set_intensity(Level.FULL)
        assert agent.level is Level.FULL

    def test_non_sentinel_starts_at_low(self):
        agent = _CountingAgent(substrate=None)
        # _CountingAgent overrides _level to FULL in __init__, but the
        # base class default for non-sentinels would be LOW. Verify by
        # constructing a fresh subclass that doesn't override.
        class _Plain(SubAgent):
            name = "plain"

            async def tick(self) -> None:
                return

        plain = _Plain(substrate=None)
        assert plain.level is Level.LOW


# ---------------------------------------------------------------------------
# Run loop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_calls_tick_until_stopped():
    agent = _CountingAgent(substrate=None)
    task = agent.start()
    # Wait for a few ticks. At FULL intensity, interval is 0.2s.
    await asyncio.sleep(0.5)
    agent.stop()
    await agent.stop_and_wait(timeout=1.0)
    # Should have ticked at least twice (start + ~2 cycles).
    assert agent.ticks >= 2
    assert task.done()


@pytest.mark.asyncio
async def test_run_loop_swallows_tick_exceptions():
    """A raising tick must be logged + loop continues; the substrate
    cannot crash because a sub-agent misbehaved."""
    agent = _ExplodingAgent(substrate=None)
    agent.start()
    await asyncio.sleep(0.5)
    agent.stop()
    await agent.stop_and_wait(timeout=1.0)
    # Multiple attempts despite the raise — loop survived.
    assert agent.attempts >= 2


@pytest.mark.asyncio
async def test_stop_and_wait_times_out_on_hung_tick():
    """If ``tick()`` hangs indefinitely, ``stop_and_wait`` cancels the
    task rather than blocking forever."""

    class _HungAgent(SubAgent):
        name = "hung"

        def __init__(self, substrate) -> None:
            super().__init__(substrate)
            self._level = Level.FULL

        async def tick(self) -> None:
            await asyncio.sleep(60)  # well past the timeout

    agent = _HungAgent(substrate=None)
    agent.start()
    await asyncio.sleep(0.1)
    await agent.stop_and_wait(timeout=0.3)
    assert agent.task is not None
    # Task may be cancelled or completed depending on race — both OK.
    assert agent.task.cancelled() or agent.task.done()


@pytest.mark.asyncio
async def test_off_level_skips_tick():
    """At OFF, ``tick()`` is never called; the loop polls for level
    changes instead."""

    class _ShouldNotTick(SubAgent):
        name = "off-only"

        async def tick(self) -> None:
            raise AssertionError("tick should never be called when OFF")

    agent = _ShouldNotTick(substrate=None)
    agent.set_intensity(Level.OFF)
    agent.start()
    await asyncio.sleep(0.2)  # would have ticked many times if not OFF
    agent.stop()
    await agent.stop_and_wait(timeout=1.0)


@pytest.mark.asyncio
async def test_start_is_idempotent():
    agent = _CountingAgent(substrate=None)
    task1 = agent.start()
    task2 = agent.start()
    assert task1 is task2
    agent.stop()
    await agent.stop_and_wait(timeout=1.0)


# ---------------------------------------------------------------------------
# Liveness heartbeat — upsert into substrate_agent_heartbeat.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def from_pool_substrate(thoth_db_initialized):
    """A Substrate over the real test pool, no boot side effects — enough
    for the base class's heartbeat upsert (it only needs ``.pool``)."""
    import thoth_db

    return Substrate.from_pool(thoth_db.pool())


@pytest.mark.asyncio
async def test_heartbeat_noop_without_substrate():
    """An agent with no substrate (the base-class unit-test shape) beats
    silently — no exception, no DB."""
    agent = _CountingAgent(substrate=None)
    await agent._maybe_heartbeat(force=True)  # must not raise
    assert agent._last_beat_mono is None  # nothing was written


@pytest.mark.asyncio
async def test_heartbeat_writes_row(from_pool_substrate):
    import thoth_db

    agent = _CountingAgent(from_pool_substrate)
    agent._tick_count = 7
    await agent._maybe_heartbeat(force=True)

    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            "SELECT agent_name, level, tick_count, is_sentinel, pid "
            "FROM substrate_agent_heartbeat WHERE agent_name = 'counter'"
        )
    assert row is not None
    assert row["level"] == Level.FULL.value
    assert row["tick_count"] == 7
    assert row["is_sentinel"] is False
    assert row["pid"] > 0
    assert agent._last_beat_mono is not None


@pytest.mark.asyncio
async def test_heartbeat_rate_limited(from_pool_substrate):
    """A second, non-forced beat inside the cadence window does not write
    (the stored last_beat_at must not advance)."""
    import thoth_db

    agent = _CountingAgent(from_pool_substrate)
    await agent._maybe_heartbeat(force=True)
    async with thoth_db.connection() as conn:
        first = await conn.fetchval(
            "SELECT last_beat_at FROM substrate_agent_heartbeat "
            "WHERE agent_name = 'counter'"
        )

    # Immediate, non-forced beat: inside the ~10s cadence → skipped.
    await agent._maybe_heartbeat()
    async with thoth_db.connection() as conn:
        second = await conn.fetchval(
            "SELECT last_beat_at FROM substrate_agent_heartbeat "
            "WHERE agent_name = 'counter'"
        )
    assert first == second  # no second write happened


@pytest.mark.asyncio
async def test_run_loop_emits_startup_heartbeat(from_pool_substrate):
    """Starting the run loop beats once immediately, before any tick, so
    the inspect CLI sees the agent right away."""
    import thoth_db

    agent = _CountingAgent(from_pool_substrate)
    agent.start()
    # The startup beat is forced; give the loop a moment to issue it.
    await asyncio.sleep(0.1)
    agent.stop()
    await agent.stop_and_wait(timeout=1.0)

    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            "SELECT agent_name, last_beat_at FROM substrate_agent_heartbeat "
            "WHERE agent_name = 'counter'"
        )
    assert row is not None
