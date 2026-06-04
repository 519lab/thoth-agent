"""Tests for ``Substrate.boot()`` end-to-end (spec §8 + §11.2 test cases).

Exercises:
* Alembic-head check — refuses to boot if the schema is on an earlier
  revision (mocked).
* All §9 streams auto-register at boot.
* Sub-agent tasks are spawned and running.
* ``shutdown()`` stops the sub-agents within the timeout.
* The substrate does NOT open its own asyncpg pool.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.events import hermes_hooks
from substrate.facade import _autoregister_specs


@pytest_asyncio.fixture
async def booted_no_subagents(thoth_db_initialized):
    sub = await Substrate.boot(start_subagents=False)
    yield sub
    await sub.shutdown()


@pytest_asyncio.fixture
async def booted_with_subagents(thoth_db_initialized):
    sub = await Substrate.boot(start_subagents=True)
    yield sub
    await sub.shutdown()


# ---------------------------------------------------------------------------
# Alembic head check.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_passes_alembic_head_check(booted_no_subagents):
    """The Phase-0 ``thoth_db_initialized`` fixture already ran alembic
    upgrade head; boot should accept the revision without raising."""
    # If we got here without raising, the head check passed.
    assert booted_no_subagents is not None


@pytest.mark.asyncio
async def test_boot_refuses_old_alembic_head(thoth_db_initialized, monkeypatch):
    """When the DB is on an older revision (mocked), boot raises so the
    caller knows to migrate (or set HERMES_AUTO_MIGRATE=1)."""
    import thoth_db

    # Replace the version_num value via SQL — pretend the DB is one
    # revision behind.
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE alembic_version SET version_num = '20260522_0002'"
        )

    with pytest.raises(RuntimeError, match="alembic head"):
        await Substrate.boot(start_subagents=False)


# ---------------------------------------------------------------------------
# Stream auto-registration.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streams_autoregister(booted_no_subagents):
    """After boot, every §9 stream exists as ACTIVE.

    The migration seeds ``substrate.self_state``; boot adds the rest.
    The full §9 list is 15 streams.
    """
    import thoth_db

    expected_names = {name for (name, *_rest) in _autoregister_specs()}
    expected_names.add("substrate.self_state")
    assert len(expected_names) == 15  # spec §9

    async with thoth_db.connection() as conn:
        rows = await conn.fetch(
            "SELECT name, lifecycle_state FROM substrate_streams"
        )
    by_name = {r["name"]: r["lifecycle_state"] for r in rows}
    for name in expected_names:
        assert name in by_name, f"stream missing after boot: {name}"
        assert by_name[name] == "active"


@pytest.mark.asyncio
async def test_streams_autoregister_idempotent(booted_no_subagents):
    """Re-running auto-register doesn't duplicate streams (the ON
    CONFLICT (name) DO NOTHING path)."""
    import thoth_db

    async with thoth_db.connection() as conn:
        before = await conn.fetchval("SELECT count(*) FROM substrate_streams")

    # Run the auto-register helper again on the same booted substrate.
    await booted_no_subagents._autoregister_streams()

    async with thoth_db.connection() as conn:
        after = await conn.fetchval("SELECT count(*) FROM substrate_streams")
    assert before == after


# ---------------------------------------------------------------------------
# Sub-agent lifecycle.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagents_running(booted_with_subagents):
    """Sentinel + force-reject + partition-maintenance + curator + parser
    tasks exist and are running after boot."""
    agents = booted_with_subagents.subagents
    assert set(agents.keys()) == {
        "sentinel",
        "force-reject",
        "partition-maintenance",
        "curator",  # Phase B
        "parser",   # Phase D (tick no-ops unless HERMES_SUBSTRATE_PARSER=1)
        "associator",  # Phase E1 (tick no-ops unless HERMES_SUBSTRATE_ASSOCIATOR=1)
        "pattern-finder",  # Phase E2 (tick no-ops unless HERMES_SUBSTRATE_PATTERNFINDER=1)
        "critic",  # Phase F (tick no-ops unless HERMES_SUBSTRATE_CRITIC=1)
        "conductor",  # Phase F adaptive policy (tick no-ops unless HERMES_SUBSTRATE_CONDUCTOR=1)
        "reflector",  # Phase F (tick no-ops unless HERMES_SUBSTRATE_REFLECTOR=1)
        "dreamer",  # Phase F (tick no-ops unless HERMES_SUBSTRATE_DREAMER=1)
        "summarizer",  # polish (retrospective summarization)
        "skill-scout",  # self-improvement Tier 1 (tick no-ops unless HERMES_SUBSTRATE_SKILL_SCOUT=1)
    }
    for name, agent in agents.items():
        task = agent.task
        assert task is not None, f"{name} has no task"
        assert not task.done(), f"{name} task already exited: {task}"


@pytest.mark.asyncio
async def test_subagents_not_started_when_disabled(booted_no_subagents):
    """``start_subagents=False`` constructs the substrate without
    spawning any tasks; conductor is still instantiated."""
    assert booted_no_subagents.subagents == {}
    assert booted_no_subagents.conductor is not None


@pytest.mark.asyncio
async def test_shutdown_stops_subagents(thoth_db_initialized):
    """``shutdown()`` cancels every sub-agent task within the bounded
    shutdown timeout."""
    sub = await Substrate.boot(start_subagents=True)
    tasks = [agent.task for agent in sub.subagents.values()]
    await sub.shutdown()

    # Give the loop one cycle to settle the cancellation.
    await asyncio.sleep(0)
    for task in tasks:
        assert task is None or task.done()
    assert sub.subagents == {}
    assert sub.conductor is None


@pytest.mark.asyncio
async def test_shutdown_unbinds_hooks(booted_with_subagents):
    """``shutdown()`` resets the module-global so subsequent hook calls
    are silent no-ops."""
    # Sanity: the binding is live while booted.
    assert hermes_hooks._substrate is booted_with_subagents
    await booted_with_subagents.shutdown()
    assert hermes_hooks._substrate is None


# ---------------------------------------------------------------------------
# Pool ownership — substrate must not create its own pool.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_does_not_open_own_pool(monkeypatch, thoth_db_initialized):
    """``Substrate.boot()`` must reuse ``thoth_db.pool()`` — it must
    NOT call ``asyncpg.create_pool`` directly."""
    import asyncpg

    create_calls = 0
    real_create = asyncpg.create_pool

    async def _spy(*args, **kwargs):
        nonlocal create_calls
        create_calls += 1
        return await real_create(*args, **kwargs)

    monkeypatch.setattr("asyncpg.create_pool", _spy)
    sub = await Substrate.boot(start_subagents=False)
    try:
        # The thoth_db fixture initialised the pool BEFORE we replaced
        # the symbol, so any create_pool call during ``Substrate.boot``
        # would be by substrate code itself.
        assert create_calls == 0, "Substrate must not call asyncpg.create_pool"
    finally:
        await sub.shutdown()


@pytest.mark.asyncio
async def test_shutdown_does_not_close_pool(booted_with_subagents):
    """``Substrate.shutdown()`` does NOT close ``thoth_db.pool()`` —
    that's the responsibility of ``thoth_db.close()`` which is owned
    by Thoth's own shutdown sequence."""
    import thoth_db

    pool = thoth_db.pool()
    await booted_with_subagents.shutdown()
    # The pool is still usable after substrate shutdown.
    async with pool.acquire() as conn:
        v = await conn.fetchval("SELECT 1")
    assert v == 1


# ---------------------------------------------------------------------------
# Boot-time benchmark — Phase A spec §12 acceptance #6 (< 200 ms cold).
# ---------------------------------------------------------------------------


# Local ceiling on developer hardware: spec target with a 2.5× headroom
# (the work is 1 alembic head fetch + ensure_partitions + 14 idempotent
# stream INSERTs + 3 asyncio task spawns, ~50ms locally on docker-compose).
_BOOT_CEILING_MS_LOCAL = 500.0

# Relaxed ceiling under CI — shared runners can spike PG round-trip latency
# the same way the commit_slice perf test sees it (see test_commit_perf.py).
# Boot is ~20 PG round-trips, so a 10 ms tail per call would push the wall
# clock past the local ceiling without any code slowdown.
_BOOT_CEILING_MS_CI = 2000.0


def _boot_ceiling_ms() -> float:
    import os

    if os.environ.get("CI", "").lower() in {"1", "true", "yes", "on"}:
        return _BOOT_CEILING_MS_CI
    return _BOOT_CEILING_MS_LOCAL


@pytest.mark.asyncio
async def test_boot_completes_under_ceiling(thoth_db_initialized):
    """Substrate.boot() finishes well under the wall-clock ceiling
    (spec §12 acceptance #6 — substrate startup adds < 200 ms).

    We measure with sub-agents OFF so the timing is purely the boot work
    (alembic head check + ensure_partitions + 14 stream registrations +
    1 hook bind), not asyncio task spawn overhead. ``start_subagents=True``
    is exercised in ``test_subagents_running`` for correctness; this test
    focuses on cold-path latency.
    """
    import time

    t0 = time.perf_counter()
    sub = await Substrate.boot(start_subagents=False)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    try:
        ceiling = _boot_ceiling_ms()
        env = "CI" if ceiling == _BOOT_CEILING_MS_CI else "local"
        print(
            f"\nSubstrate.boot(start_subagents=False) elapsed: "
            f"{elapsed_ms:.1f} ms  (ceiling={ceiling}ms, env={env})"
        )
        assert elapsed_ms < ceiling, (
            f"boot took {elapsed_ms:.1f} ms — exceeds {env} ceiling "
            f"{ceiling} ms. Profile the boot path."
        )
    finally:
        await sub.shutdown()


# ---------------------------------------------------------------------------
# Writer / worker boot-mode split — added 2026-05-26 after the gateway
# cross-loop-pool incident. boot_writer (gateway/CLI/cron) starts no
# sub-agents but binds hooks + recall log. boot_worker (the dedicated
# substrate-worker subprocess) starts sub-agents but skips hooks + recall.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_writer_skips_subagents_keeps_hooks_and_recall(
    thoth_db_initialized,
):
    """Writer mode: no sub-agent tick tasks; hooks bound; recall_log started."""
    sub = await Substrate.boot_writer()
    try:
        # No tick loops spawned.
        assert sub._subagents == {}, (
            f"boot_writer started sub-agent tasks ({list(sub._subagents)}); "
            "those belong in the worker subprocess. The cross-loop pool "
            "incident on 2026-05-26 is why we split."
        )
        # Conductor instance exists (no tick, just state).
        assert sub._conductor is not None, "boot_writer should still hold a Conductor for inspect"
        # Hooks bound so perception emit works in this process.
        assert hermes_hooks._substrate_for_tests() is sub, (
            "boot_writer must bind perception hooks so gateway/CLI emits land"
        )
        # Recall log writer started so recall() can log calls.
        assert sub.recall_log is not None, (
            "boot_writer must start the recall log writer — writer processes "
            "serve recall queries and need to log them"
        )
    finally:
        await sub.shutdown()


@pytest.mark.asyncio
async def test_boot_worker_starts_subagents_skips_hooks_and_recall(
    thoth_db_initialized,
):
    """Worker mode: sub-agent tick tasks running; hooks NOT bound;
    recall_log NOT started.

    The worker subprocess doesn't receive perception hook calls (those
    fire in the writer process that owns the chat/gateway loop) and
    doesn't serve recall queries, so both subsystems stay off."""
    # Pre-condition: clear hook binding so we can assert worker mode
    # leaves it untouched even when it starts clean.
    hermes_hooks._unbind()
    sub = await Substrate.boot_worker()
    try:
        # Sub-agent tasks must be alive.
        assert sub._subagents, "boot_worker MUST start sub-agent tick tasks"
        # Specifically: Sentinel + Curator + ForceReject + PartitionMaintenance.
        names = set(sub._subagents)
        for required in ("sentinel", "curator"):
            assert required in names, (
                f"boot_worker missing required sub-agent {required!r}; "
                f"got {sorted(names)}"
            )
        # Hooks NOT bound — the writer process owns that binding.
        assert hermes_hooks._substrate_for_tests() is None, (
            "boot_worker must NOT bind perception hooks; doing so would "
            "redirect Thoth hook emits to a process that doesn't have "
            "the chat/gateway loop. Only writer processes bind hooks."
        )
        # Recall log writer NOT started.
        assert sub.recall_log is None, (
            "boot_worker must NOT start recall_log; the worker subprocess "
            "doesn't serve recall queries."
        )
    finally:
        await sub.shutdown()
