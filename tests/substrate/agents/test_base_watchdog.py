"""Per-tick watchdog + independent heartbeat (innovation #4).

These tests are deliberately DB-free: every agent is constructed with
``substrate=None`` and the heartbeat is stubbed, so the suite exercises the run
loop's timeout/decoupling behaviour with tiny timeouts and no Postgres.
"""

from __future__ import annotations

import asyncio

import pytest

from substrate.agents import base as base_mod
from substrate.agents.base import Level, SubAgent


# ---------------------------------------------------------------------------
# Concrete test subclasses.
# ---------------------------------------------------------------------------


class _SlowTickAgent(SubAgent):
    """tick() sleeps well past the (tiny) ceiling — every tick times out."""

    name = "slow-tick"

    def __init__(self, substrate) -> None:
        super().__init__(substrate)
        self._level = Level.FULL
        # Tiny watchdog ceiling so the test runs fast; the tick sleeps far
        # longer, so wait_for fires a TimeoutError every iteration.
        self.tick_timeout_s = 0.02
        self.attempts = 0

    async def tick(self) -> None:
        self.attempts += 1
        await asyncio.sleep(5.0)  # never completes within the ceiling


class _HangingHeartbeatAgent(SubAgent):
    """tick() hangs forever; used to prove the heartbeat beats anyway."""

    name = "hang-hb"

    def __init__(self, substrate) -> None:
        super().__init__(substrate)
        self._level = Level.FULL
        # No watchdog timeout here — we WANT the tick to stay wedged so we can
        # observe the independent heartbeat firing despite a frozen tick.
        self.tick_timeout_s = 3600.0
        self.beats = 0
        self.beat_tick_counts: list[int] = []

    async def tick(self) -> None:
        await asyncio.sleep(3600)  # wedged for the duration of the test

    async def _maybe_heartbeat(self, *, force: bool = False) -> None:
        # Stub: record that a beat happened (and the tick_count it reported)
        # instead of touching a DB. Proves the heartbeat loop runs decoupled
        # from the (hung) tick loop.
        self.beats += 1
        self.beat_tick_counts.append(self._tick_count)


# ---------------------------------------------------------------------------
# Tick-ceiling derivation.
# ---------------------------------------------------------------------------


class TestTickCeiling:
    def test_per_agent_override_wins(self):
        agent = _SlowTickAgent(substrate=None)
        agent.tick_timeout_s = 123.0
        # Override beats the interval-derived ceiling at every level.
        assert agent._tick_ceiling_for(Level.FULL) == 123.0
        assert agent._tick_ceiling_for(Level.LOW) == 123.0

    def test_floor_applies_for_fast_levels(self):
        agent = _SlowTickAgent(substrate=None)
        agent.tick_timeout_s = None
        # FULL interval (0.2s) * mult is below the 30s floor → floored.
        assert agent._tick_ceiling_for(Level.FULL) == base_mod._TICK_TIMEOUT_FLOOR_S

    def test_derived_from_interval_for_slow_levels(self):
        agent = _SlowTickAgent(substrate=None)
        agent.tick_timeout_s = None
        low_interval = agent._interval_for(Level.LOW)
        expected = max(
            base_mod._TICK_TIMEOUT_FLOOR_S,
            low_interval * base_mod._TICK_TIMEOUT_MULT,
        )
        assert agent._tick_ceiling_for(Level.LOW) == expected

    def test_off_has_no_ceiling(self):
        agent = _SlowTickAgent(substrate=None)
        agent.tick_timeout_s = None
        assert agent._tick_ceiling_for(Level.OFF) is None


# ---------------------------------------------------------------------------
# Watchdog: a long tick times out, is counted, the loop continues, and
# _tick_count does NOT advance.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_tick_times_out_and_loop_continues():
    agent = _SlowTickAgent(substrate=None)
    # Stub the heartbeat so the run loop never touches a DB.
    agent._maybe_heartbeat = _noop_heartbeat.__get__(agent, type(agent))

    agent.start()
    # Long enough for several tick attempts at a 0.02s ceiling.
    await asyncio.sleep(0.3)
    agent.stop()
    await agent.stop_and_wait(timeout=1.0)

    # Multiple ticks were attempted (the loop survived each timeout)…
    assert agent.attempts >= 2
    # …every one timed out…
    assert agent._tick_timeout_count >= 2
    # …and a timed-out tick must NOT advance the success counter.
    assert agent._tick_count == 0


# ---------------------------------------------------------------------------
# Decoupling: a hung tick still produces heartbeat calls.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_beats_while_tick_hangs(monkeypatch):
    # Squeeze the heartbeat cadence so several beats land inside the test.
    monkeypatch.setattr(base_mod, "_HEARTBEAT_INTERVAL_S", 0.02)

    agent = _HangingHeartbeatAgent(substrate=None)
    agent.start()
    # The tick is wedged for the whole window; only the independent heartbeat
    # task can make progress here.
    await asyncio.sleep(0.2)
    agent.stop()
    await agent.stop_and_wait(timeout=1.0)

    # The startup beat + several cadence beats fired despite the frozen tick…
    assert agent.beats >= 2
    # …and every beat reported the frozen tick_count (still 0 — tick never
    # completed), proving the heartbeat read live state independently.
    assert agent.beat_tick_counts and all(c == 0 for c in agent.beat_tick_counts)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


async def _noop_heartbeat(self, *, force: bool = False) -> None:
    """Bound-method stub: skip the DB upsert entirely in the run loop."""
    return
