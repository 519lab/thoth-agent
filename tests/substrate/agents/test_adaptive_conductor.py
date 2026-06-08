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
async def booted(thoth_db_initialized):
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
    monkeypatch.setenv("THOTH_SUBSTRATE_CONDUCTOR", "0")
    await _seed_pending(booted, 10)
    await AdaptiveConductor(booted).tick()
    # Nothing dialed → conductor snapshot empty.
    assert booted.conductor.snapshot() == {}


@pytest.mark.asyncio
async def test_conductor_dials_parser_up_under_backlog(booted, monkeypatch):
    monkeypatch.setenv("THOTH_SUBSTRATE_CONDUCTOR", "1")
    monkeypatch.setenv("CONDUCTOR_BACKLOG_HIGH", "0.5")
    # All slices pending, none consolidated → backlog_ratio = 1.0 (>= high).
    await _seed_pending(booted, 8)
    await AdaptiveConductor(booted).tick()

    snap = booted.conductor.snapshot()
    assert snap["parser"] is Level.HIGH
    assert snap["associator"] is Level.OFF


@pytest.mark.asyncio
async def test_conductor_intensity_off_is_noop(booted, monkeypatch):
    monkeypatch.setenv("THOTH_SUBSTRATE_CONDUCTOR", "1")
    await _seed_pending(booted, 8)
    c = AdaptiveConductor(booted)
    c.set_intensity(Level.OFF)
    await c.tick()
    assert booted.conductor.snapshot() == {}


def test_compute_targets_coherence_low_overrides_backlog():
    """A tripped coherence latch drives the corrective dial regardless of
    backlog — Parser HIGH, Critic MODERATE, enrichment + dreamer OFF."""
    # Quiet backlog but coherence latch tripped → corrective, not baseline.
    corrective = AdaptiveConductor._compute_targets(
        {"backlog_ratio": 0.0, "coherence_low": True}
    )
    assert corrective["parser"] is Level.HIGH
    assert corrective["critic"] is Level.MODERATE
    assert corrective["associator"] is Level.OFF
    assert corrective["pattern-finder"] is Level.OFF
    assert corrective["dreamer"] is Level.OFF
    assert corrective["curator"] is Level.LOW


def test_compute_targets_coherence_not_low_falls_through_to_backlog():
    """coherence_low False (or absent) leaves the backlog policy unchanged —
    no Critic/Dreamer keys are introduced."""
    base = AdaptiveConductor._compute_targets(
        {"backlog_ratio": 0.0, "coherence_low": False}
    )
    assert base["parser"] is Level.LOW
    assert "critic" not in base
    assert "dreamer" not in base


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
    import thoth_db

    monkeypatch.setenv("THOTH_SUBSTRATE_CONDUCTOR", "1")
    await _seed_pending(booted, 8)
    c = AdaptiveConductor(booted)
    await c.tick()

    # Forecast (EMA) is now populated and a decision was logged.
    assert c.forecast() is not None
    async with thoth_db.connection() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM substrate_conductor_log")
    assert n == 1


@pytest.mark.asyncio
async def test_conductor_seeds_forecast_from_log(booted, monkeypatch):
    import thoth_db

    monkeypatch.setenv("THOTH_SUBSTRATE_CONDUCTOR", "1")
    # Pre-seed a prior forecast in the persistent log.
    async with thoth_db.connection() as conn:
        await conn.execute(
            "INSERT INTO substrate_conductor_log (backlog_ratio, forecast) VALUES (0.7, 0.65)"
        )
    c = AdaptiveConductor(booted)
    await c._seed_forecast()
    assert c.forecast() == pytest.approx(0.65)  # resumed the learned rhythm


async def _set_coherence(score):
    """Write the current coherence vital sign so latest_coherence() reads it."""
    from substrate.l4 import store as l4

    await l4.upsert_coherence(f"coherence {score:.2f}", score=score)


@pytest.mark.asyncio
async def test_conductor_coherence_below_floor_dials_corrective(booted, monkeypatch):
    """Coherence below the floor trips the latch → corrective dial overrides
    backlog (Parser HIGH, Critic MODERATE, Dreamer OFF) even when quiet."""
    monkeypatch.setenv("THOTH_SUBSTRATE_CONDUCTOR", "1")
    monkeypatch.setenv("THOTH_CONDUCTOR_COHERENCE_FLOOR", "0.5")
    monkeypatch.setenv("THOTH_CONDUCTOR_COHERENCE_RECOVERY", "0.6")
    # No backlog (no pending slices) → would otherwise be baseline LOW.
    await _set_coherence(0.3)
    c = AdaptiveConductor(booted)
    await c.tick()

    assert c._coherence_low is True
    snap = booted.conductor.snapshot()
    assert snap["parser"] is Level.HIGH
    assert snap["critic"] is Level.MODERATE
    assert snap["dreamer"] is Level.OFF
    assert snap["associator"] is Level.OFF


@pytest.mark.asyncio
async def test_conductor_coherence_hysteresis(booted, monkeypatch):
    """Latch stays tripped in the band between floor and recovery, and only
    clears once coherence reaches the recovery threshold."""
    monkeypatch.setenv("THOTH_SUBSTRATE_CONDUCTOR", "1")
    monkeypatch.setenv("THOTH_CONDUCTOR_COHERENCE_FLOOR", "0.5")
    monkeypatch.setenv("THOTH_CONDUCTOR_COHERENCE_RECOVERY", "0.6")
    c = AdaptiveConductor(booted)

    # Below floor → trips.
    await _set_coherence(0.3)
    await c.tick()
    assert c._coherence_low is True

    # In the band [floor, recovery) → stays low (no flap).
    await _set_coherence(0.55)
    await c.tick()
    assert c._coherence_low is True
    assert booted.conductor.snapshot()["critic"] is Level.MODERATE

    # At/above recovery → clears.
    await _set_coherence(0.6)
    await c.tick()
    assert c._coherence_low is False
    # Back to backlog policy (no backlog → baseline LOW, no critic key pushed).
    assert booted.conductor.snapshot()["parser"] is Level.LOW


@pytest.mark.asyncio
async def test_conductor_coherence_none_leaves_backlog_policy(booted, monkeypatch):
    """No coherence observation → latch untouched, backlog policy unchanged."""
    monkeypatch.setenv("THOTH_SUBSTRATE_CONDUCTOR", "1")
    monkeypatch.setenv("CONDUCTOR_BACKLOG_HIGH", "0.5")
    # No coherence row written. Heavy backlog → normal HIGH catch-up policy.
    await _seed_pending(booted, 8)
    c = AdaptiveConductor(booted)
    await c.tick()

    assert c._coherence_low is False
    snap = booted.conductor.snapshot()
    assert snap["parser"] is Level.HIGH
    assert snap["associator"] is Level.OFF
    # Corrective-only keys must not appear under the plain backlog policy.
    assert "critic" not in snap
    assert "dreamer" not in snap
