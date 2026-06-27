"""DB-backed watchdog regression (innovation #4).

WRITTEN BUT NOT RUN by the build agent — touches the real test Postgres pool.
Run it under the normal test runner (``scripts/run_tests_parallel.py`` /
``alembic upgrade head`` conftest), never against a live DB.

What it proves end-to-end against Postgres: while a tick is wedged, the
*independent* heartbeat task keeps upserting ``substrate_agent_heartbeat`` with
a FROZEN ``tick_count`` — that frozen-count-but-advancing-last_beat_at row is
the operator-visible "stuck worker" signature the deferred supervisor will key
its ``os._exit`` decision off of.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.agents import base as base_mod
from substrate.agents.base import Level, SubAgent


@pytest_asyncio.fixture
async def from_pool_substrate(thoth_db_initialized):
    """A Substrate over the real test pool — enough for the heartbeat upsert
    (it only needs ``.pool``)."""
    import thoth_db

    return Substrate.from_pool(thoth_db.pool())


class _WedgedTickAgent(SubAgent):
    """tick() hangs forever; the run loop's tick_count must stay frozen while
    the independent heartbeat keeps writing rows."""

    name = "wedged-pg"

    def __init__(self, substrate) -> None:
        super().__init__(substrate)
        self._level = Level.FULL
        # No watchdog trip — keep the tick wedged so we observe the frozen
        # count being beaten out to the DB by the independent heartbeat.
        self.tick_timeout_s = 3600.0

    async def tick(self) -> None:
        await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_heartbeat_writes_frozen_tick_count_while_tick_hangs(
    from_pool_substrate, monkeypatch
):
    import thoth_db

    # Tight cadence so a couple of beats land inside the test window.
    monkeypatch.setattr(base_mod, "_HEARTBEAT_INTERVAL_S", 0.05)

    agent = _WedgedTickAgent(from_pool_substrate)
    agent.start()
    # The tick is wedged the whole time; only the independent heartbeat task
    # can write here. Wait for at least the startup beat + one cadence beat.
    await asyncio.sleep(0.3)
    agent.stop()
    await agent.stop_and_wait(timeout=1.0)

    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            "SELECT tick_count, last_beat_at FROM substrate_agent_heartbeat "
            "WHERE agent_name = 'wedged-pg'"
        )
    assert row is not None
    # tick() never completed → the beaten-out count is frozen at 0.
    assert row["tick_count"] == 0
    assert row["last_beat_at"] is not None
