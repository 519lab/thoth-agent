"""Phase F adaptive Conductor — deterministic intensity policy."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.agents.base import Level
from substrate.agents.conductor_policy import AdaptiveConductor
from substrate.l0 import commit_slice


def test_compute_targets_policy():
    # Quiet → everyone LOW.
    quiet = AdaptiveConductor._compute_targets({"backlog_ratio": 0.0})
    assert quiet["parser"] is Level.LOW

    # Moderate backlog → parser MODERATE, enrichment LOW.
    mod = AdaptiveConductor._compute_targets({"backlog_ratio": 0.3})
    assert mod["parser"] is Level.MODERATE
    assert mod["associator"] is Level.LOW

    # High backlog → parser HIGH, enrichment OFF (catch up).
    hot = AdaptiveConductor._compute_targets({"backlog_ratio": 0.9})
    assert hot["parser"] is Level.HIGH
    assert hot["associator"] is Level.OFF
    assert hot["pattern-finder"] is Level.OFF


@pytest_asyncio.fixture
async def booted(hermes_db_initialized):
    sub = await Substrate.boot(
        config=SubstrateConfig(auto_migrate=False, start_subagents=False),
        start_subagents=False,
    )
    try:
        yield sub
    finally:
        await sub.shutdown()


async def _seed_pending(substrate, n):
    """Commit n passed-but-unconsolidated slices to create backlog."""
    stream = await substrate.streams.get_by_name("thoth.world.user_message.cli")
    for i in range(n):
        await commit_slice(
            substrate, stream.stream_id, f"m{i}",
            event_time_world=datetime.now(timezone.utc), born_passed=True,
        )


@pytest.mark.asyncio
async def test_conductor_disabled_is_noop(booted, monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_CONDUCTOR", "0")
    await _seed_pending(booted, 10)
    await AdaptiveConductor(booted).tick()
    # Nothing dialed → conductor snapshot empty.
    assert booted.conductor.snapshot() == {}


@pytest.mark.asyncio
async def test_conductor_dials_parser_up_under_backlog(booted, monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_CONDUCTOR", "1")
    monkeypatch.setenv("CONDUCTOR_BACKLOG_HIGH", "0.5")
    # All slices pending, none consolidated → backlog_ratio = 1.0 (>= high).
    await _seed_pending(booted, 8)
    await AdaptiveConductor(booted).tick()

    snap = booted.conductor.snapshot()
    assert snap["parser"] is Level.HIGH
    assert snap["associator"] is Level.OFF


@pytest.mark.asyncio
async def test_conductor_intensity_off_is_noop(booted, monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_CONDUCTOR", "1")
    await _seed_pending(booted, 8)
    c = AdaptiveConductor(booted)
    c.set_intensity(Level.OFF)
    await c.tick()
    assert booted.conductor.snapshot() == {}


def test_trend_bias_escalates_sooner():
    """A rising-backlog trend pushes the effective backlog over the HIGH
    threshold even when raw backlog is just below it."""
    # Raw 0.45 is below default HIGH (0.5) → MODERATE without bias.
    assert AdaptiveConductor._compute_targets({"backlog_ratio": 0.45})["parser"] is Level.MODERATE
    # +0.1 trend bias → effective 0.55 ≥ HIGH → escalate to HIGH.
    biased = AdaptiveConductor._compute_targets({"backlog_ratio": 0.45, "trend_bias": 0.1})
    assert biased["parser"] is Level.HIGH


@pytest.mark.asyncio
async def test_conductor_forecasts_and_logs(booted, monkeypatch):
    import hermes_db

    monkeypatch.setenv("HERMES_SUBSTRATE_CONDUCTOR", "1")
    await _seed_pending(booted, 8)
    c = AdaptiveConductor(booted)
    await c.tick()

    # Forecast (EMA) is now populated and a decision was logged.
    assert c.forecast() is not None
    async with hermes_db.connection() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM substrate_conductor_log")
    assert n == 1


@pytest.mark.asyncio
async def test_conductor_seeds_forecast_from_log(booted, monkeypatch):
    import hermes_db

    monkeypatch.setenv("HERMES_SUBSTRATE_CONDUCTOR", "1")
    # Pre-seed a prior forecast in the persistent log.
    async with hermes_db.connection() as conn:
        await conn.execute(
            "INSERT INTO substrate_conductor_log (backlog_ratio, forecast) VALUES (0.7, 0.65)"
        )
    c = AdaptiveConductor(booted)
    await c._seed_forecast()
    assert c.forecast() == pytest.approx(0.65)  # resumed the learned rhythm
