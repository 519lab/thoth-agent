"""Tests for the Conductor → SubAgent push wiring — spec §10.5.

``StubConductor.set_intensity(name, level)`` now ALSO pushes to any
running sub-agent of the same name so intensity changes land within
one tick.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.agents import Curator, Level, StubConductor


@pytest_asyncio.fixture
async def booted(thoth_db_initialized):
    """Full boot — we need the conductor + the running curator."""
    sub = await Substrate.boot(start_subagents=True)
    yield sub
    await sub.shutdown()


@pytest_asyncio.fixture
async def booted_no_subagents(thoth_db_initialized):
    """Booted without spawning sub-agent tasks; the StubConductor
    exists but has no running agents to push to."""
    sub = await Substrate.boot(start_subagents=False)
    yield sub
    await sub.shutdown()


@pytest.mark.asyncio
async def test_conductor_push_lands_on_running_agent(booted):
    """set_intensity('curator', HIGH) updates the running Curator's
    _level within the same call."""
    conductor = booted.conductor
    assert isinstance(conductor, StubConductor)
    curator = booted.subagents["curator"]
    assert isinstance(curator, Curator)

    # Initial state.
    assert curator.level is Level.LOW

    # Push.
    conductor.set_intensity("curator", Level.HIGH)
    assert curator.level is Level.HIGH
    assert conductor.intensity_for("curator") is Level.HIGH


@pytest.mark.asyncio
async def test_push_off_is_honoured(booted):
    """OFF lands verbatim on the curator (operator opt-out)."""
    conductor = booted.conductor
    curator = booted.subagents["curator"]

    conductor.set_intensity("curator", Level.OFF)
    assert curator.level is Level.OFF


@pytest.mark.asyncio
async def test_push_moderate_full_land_verbatim(booted):
    conductor = booted.conductor
    curator = booted.subagents["curator"]

    for level in (Level.MODERATE, Level.HIGH, Level.FULL):
        conductor.set_intensity("curator", level)
        assert curator.level is level


@pytest.mark.asyncio
async def test_push_to_missing_agent_is_no_op(booted_no_subagents):
    """No agents running → set_intensity stores the level but doesn't
    raise. A later agent constructed under that name will read the
    level via the conductor's intensity_for accessor."""
    conductor = booted_no_subagents.conductor
    assert isinstance(conductor, StubConductor)
    # No agents.
    assert booted_no_subagents.subagents == {}

    conductor.set_intensity("nonexistent", Level.FULL)
    # Conductor still stored it.
    assert conductor.intensity_for("nonexistent") is Level.FULL


@pytest.mark.asyncio
async def test_push_respects_sentinel_floor(booted):
    """Sentinel floors at FULL — pushing LOW lands at FULL silently."""
    conductor = booted.conductor
    sentinel = booted.subagents["sentinel"]

    assert sentinel.level is Level.FULL
    conductor.set_intensity("sentinel", Level.LOW)
    # Sentinel demoted the push back to FULL.
    assert sentinel.level is Level.FULL
    # Conductor still records the operator's intent.
    assert conductor.intensity_for("sentinel") is Level.LOW
