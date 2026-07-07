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
    # Quiet (small pending queue) → everyone LOW.
    quiet = AdaptiveConductor._compute_targets({"pending": 0})
    assert quiet["parser"] is Level.LOW

    # Moderate backlog (>= CONDUCTOR_PENDING_LOW=20) → parser MODERATE, enrichment LOW.
    mod = AdaptiveConductor._compute_targets({"pending": 30})
    assert mod["parser"] is Level.MODERATE
    assert mod["associator"] is Level.LOW

    # High backlog (>= CONDUCTOR_PENDING_HIGH=100) → parser HIGH, enrichment OFF (catch up).
    hot = AdaptiveConductor._compute_targets({"pending": 150})
    assert hot["parser"] is Level.HIGH
    assert hot["associator"] is Level.OFF
    assert hot["pattern-finder"] is Level.OFF


def test_compute_targets_ignores_inflated_ratio_on_small_queue():
    """#107 regression: a near-100% backlog_ratio must NOT dial the Parser up
    while the absolute pending queue is tiny. The ratio's denominator
    (pending+consolidated) shrinks as the Curator releases consolidated history,
    which falsely pinned a quiet substrate HIGH for a handful of real slices."""
    targets = AdaptiveConductor._compute_targets(
        {"pending": 6, "backlog_ratio": 0.99, "trend_bias": 0.2}
    )
    assert targets["parser"] is Level.LOW


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
async def test_backlog_excludes_aged_out_slices(booted):
    """Slices older than the Parser's 7-day selection horizon must NOT count as
    backlog. They can never be selected/consolidated and have no terminal state,
    so counting them pins the ratio high forever and starves enrichment (the
    undrainable-backlog feedback trap)."""
    import thoth_db

    await _seed_pending(booted, 3)  # 3 fresh passed/unconsolidated slices
    async with thoth_db.connection() as conn:
        # Backdate one of them past the 7-day horizon (same month → same
        # partition, no cross-partition row move).
        # Backdate the temporal cluster together to keep the
        # event<=perception<=ingest CHECK invariant (migration 0003) satisfied.
        await conn.execute(
            """
            UPDATE substrate_slices
               SET ingest_time_world     = now() - interval '8 days',
                   perception_time_world = now() - interval '8 days',
                   event_time_world      = now() - interval '8 days'
             WHERE slice_id = (
                 SELECT slice_id FROM substrate_slices
                  WHERE consolidation_state = 'unconsolidated'
                    AND sentinel_state = 'passed'
                  ORDER BY ingest_time_world DESC LIMIT 1)
            """
        )
    signals = await AdaptiveConductor(booted)._read_load()
    # 3 seeded, 1 aged out → only 2 count as drainable backlog.
    assert signals["pending"] == 2


@pytest.mark.asyncio
async def test_conductor_dials_parser_up_under_backlog(booted, monkeypatch):
    monkeypatch.setenv("THOTH_SUBSTRATE_CONDUCTOR", "1")
    monkeypatch.setenv("CONDUCTOR_PENDING_HIGH", "5")
    # 8 unconsolidated slices, HIGH threshold lowered to 5 → parser HIGH (catch up).
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


def test_trend_bias_is_telemetry_only_not_a_dial():
    """#107: backlog_ratio and its trend_bias are retained as telemetry but no
    longer gate the dial — only the absolute pending queue does. A near-100%
    ratio with a rising trend must not escalate a near-empty queue."""
    assert AdaptiveConductor._compute_targets(
        {"pending": 0, "backlog_ratio": 0.45, "trend_bias": 0.1}
    )["parser"] is Level.LOW


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


def test_compute_targets_budget_exhausted_throttles_everything(monkeypatch):
    """budget_ratio >= 1.0 → metabolic floor, overriding backlog AND the
    coherence-corrective dial: a hard cap must stop spend, and coherence
    recovery burns tokens like everything else."""
    floor = AdaptiveConductor._compute_targets(
        {"pending": 500, "coherence_low": True, "budget_ratio": 1.2}
    )
    assert floor["parser"] is Level.LOW
    assert floor["associator"] is Level.OFF
    assert floor["pattern-finder"] is Level.OFF
    assert floor["dreamer"] is Level.OFF
    assert floor["critic"] is Level.LOW
    assert floor["curator"] is Level.LOW


def test_compute_targets_budget_soft_ratio_suppresses_escalation():
    """Past the soft ratio (default 0.8) the base policy still runs but may
    not escalate: HIGH caps to MODERATE and the Dreamer is paused."""
    capped = AdaptiveConductor._compute_targets(
        {"pending": 500, "budget_ratio": 0.85}
    )
    assert capped["parser"] is Level.MODERATE  # HIGH capped
    assert capped["associator"] is Level.OFF  # non-HIGH dials untouched
    assert capped["dreamer"] is Level.OFF

    # Quiet substrate near the budget: nothing to cap, dreamer still paused.
    quiet = AdaptiveConductor._compute_targets(
        {"pending": 0, "budget_ratio": 0.85}
    )
    assert quiet["parser"] is Level.LOW
    assert quiet["dreamer"] is Level.OFF


def test_compute_targets_no_budget_leaves_policy_ungoverned():
    """budget_ratio None (no budget configured / spend read failed) must
    reproduce the pre-budget policy exactly — no dreamer key introduced."""
    hot = AdaptiveConductor._compute_targets({"pending": 500, "budget_ratio": None})
    assert hot["parser"] is Level.HIGH
    assert "dreamer" not in hot
    # Below the soft ratio the governor stays out of the way too.
    below = AdaptiveConductor._compute_targets({"pending": 500, "budget_ratio": 0.5})
    assert below["parser"] is Level.HIGH
    assert "dreamer" not in below


async def _seed_spend(total_tokens: int) -> None:
    """Record trailing-window sub-agent spend in substrate_agent_cost."""
    import thoth_db

    async with thoth_db.connection() as conn:
        await conn.execute(
            "INSERT INTO substrate_agent_cost "
            "  (agent, model, prompt_tokens, completion_tokens, total_tokens, latency_ms) "
            "VALUES ('parser', 'test-model', $1, 0, $1, 5)",
            total_tokens,
        )


@pytest.mark.asyncio
async def test_conductor_reads_spend_only_when_budget_set(booted, monkeypatch):
    """No budget env → no spend read (budget_ratio None); with a budget the
    trailing-hour SUM over substrate_agent_cost lands in the signals."""
    monkeypatch.delenv("THOTH_SUBSTRATE_HOURLY_TOKEN_BUDGET", raising=False)
    await _seed_spend(4_000)
    c = AdaptiveConductor(booted)
    signals = await c._read_load()
    assert signals["budget_ratio"] is None
    assert signals["spend_tokens_1h"] is None

    monkeypatch.setenv("THOTH_SUBSTRATE_HOURLY_TOKEN_BUDGET", "10000")
    signals = await c._read_load()
    assert signals["spend_tokens_1h"] == 4_000
    assert signals["budget_ratio"] == pytest.approx(0.4)
    assert signals["hourly_token_budget"] == pytest.approx(10_000)


@pytest.mark.asyncio
async def test_conductor_budget_exhausted_tick_dials_floor(booted, monkeypatch):
    """Live tick: spend over budget throttles the crew to floor levels even
    with a hot backlog that would otherwise dial the Parser HIGH."""
    monkeypatch.setenv("THOTH_SUBSTRATE_CONDUCTOR", "1")
    monkeypatch.setenv("CONDUCTOR_PENDING_HIGH", "5")
    monkeypatch.setenv("THOTH_SUBSTRATE_HOURLY_TOKEN_BUDGET", "1000")
    await _seed_pending(booted, 8)  # would be HIGH without governance
    await _seed_spend(1_500)  # 150% of budget
    await AdaptiveConductor(booted).tick()

    snap = booted.conductor.snapshot()
    assert snap["parser"] is Level.LOW
    assert snap["dreamer"] is Level.OFF
    assert snap["critic"] is Level.LOW


@pytest.mark.asyncio
async def test_conductor_coherence_none_leaves_backlog_policy(booted, monkeypatch):
    """No coherence observation → latch untouched, backlog policy unchanged."""
    monkeypatch.setenv("THOTH_SUBSTRATE_CONDUCTOR", "1")
    monkeypatch.setenv("CONDUCTOR_PENDING_HIGH", "5")
    # No coherence row written. Backlog over threshold → normal HIGH catch-up.
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


@pytest.mark.asyncio
async def test_idle_dial_is_throttled_and_change_logs_immediately(booted, monkeypatch):
    """Repeated idle ticks with an unchanged decision write at most one
    conductor_log row (heartbeat); a changed decision logs immediately (#285)."""
    import thoth_db

    monkeypatch.setenv("THOTH_SUBSTRATE_CONDUCTOR", "1")

    async def _log_count():
        async with thoth_db.connection() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM substrate_conductor_log")

    c = AdaptiveConductor(booted)
    # Idle (no backlog): first tick logs, then unchanged ticks are throttled.
    await c.tick()
    assert await _log_count() == 1
    await c.tick()
    await c.tick()
    assert await _log_count() == 1, "unchanged idle ticks must not write new rows"

    # A real change (backlog escalates the parser dial) logs immediately.
    monkeypatch.setenv("CONDUCTOR_PENDING_HIGH", "5")
    await _seed_pending(booted, 8)
    await c.tick()
    assert await _log_count() == 2, "a changed decision must log immediately"
