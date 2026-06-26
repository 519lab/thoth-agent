"""Phase G — `thoth substrate health` operator rollup."""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.cli import inspect as inspect_mod
from substrate.l1 import store as l1
from substrate.l4 import store as l4


@pytest_asyncio.fixture
async def booted(thoth_db_initialized):
    sub = await Substrate.boot(start_subagents=False)
    yield sub
    await sub.shutdown()


def test_register_subparser_health():
    parser = argparse.ArgumentParser(prog="thoth")
    sub = parser.add_subparsers(dest="command")
    inspect_mod.register_subparser(sub)
    assert callable(parser.parse_args(["substrate", "health"]).func)


@pytest.mark.asyncio
async def test_health_reports_all_sections(booted):
    import thoth_db

    # Seed a bit of every layer so the counts are non-trivial.
    await l1.upsert_entity("Greg", "person")
    await l4.record_observation("coherence", "substrate", "coherence 0.91", score=0.91)

    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_health(conn)
    out = buf.getvalue()

    assert "Substrate health @" in out
    assert "Worker:" in out
    assert "Coherence: 0.91" in out
    assert "Layers:" in out
    assert "L0 perception" in out
    assert "L1 knowledge" in out
    assert "L4 self-model" in out


@pytest.mark.asyncio
async def test_health_worker_down_when_no_heartbeat(booted):
    """Booted with sub-agents off + no worker → the rollup flags DOWN."""
    import thoth_db

    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_health(conn)
    assert "DOWN" in buf.getvalue()


@pytest.mark.asyncio
async def test_layer_counts_present(booted):
    import thoth_db

    await l1.upsert_entity("X", "concept")
    async with thoth_db.connection() as conn:
        counts = await inspect_mod._layer_counts(conn)
    assert counts["l1_entities"] >= 1
    # All layer keys present (tables migrated this far).
    for k in ("l0_passed", "l1_relationships", "l2_associations",
              "l3_patterns", "l4_observations"):
        assert k in counts


@pytest.mark.asyncio
async def test_health_coherence_age_uses_last_seen_not_created(booted):
    """The Critic upserts a single coherence row, so 'assessed Xago' must read
    last_seen_at (bumped each assessment), not created_at (frozen at first
    creation — which would grow forever even while the Critic is healthy)."""
    import thoth_db

    await l4.upsert_coherence("coherence 0.90", score=0.90)
    # Backdate created_at far into the past; a fresh upsert bumps last_seen now.
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE l4_observations SET created_at = now() - interval '3 hours' "
            "WHERE kind='coherence'"
        )
    await l4.upsert_coherence("coherence 0.91", score=0.91)

    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_health(conn)
    coh_line = next(
        line for line in buf.getvalue().splitlines() if line.startswith("Coherence:")
    )
    assert "0.91" in coh_line
    assert "h ago" not in coh_line, f"age used created_at, not last_seen: {coh_line!r}"


@pytest.mark.asyncio
async def test_layer_counts_exclude_substrate_streams(booted):
    """`health` L0 counts are perceptual-only. The historical
    substrate.self_state ghost rows must not inflate awaiting-parse /
    backlog — that was the misleading 100% the operator saw post-fix."""
    import thoth_db
    from substrate.l0 import commit_slice

    self_state = await booted.streams.get_by_name("substrate.self_state")
    for i in range(4):
        await commit_slice(
            booted, self_state.stream_id, {"event": f"noise{i}"},
            event_time_world=datetime.now(timezone.utc), born_passed=True,
        )

    async with thoth_db.connection() as conn:
        counts = await inspect_mod._layer_counts(conn)
    # Non-perceptual substrate.* slices count toward neither passed nor backlog.
    assert counts["l0_passed"] == 0
    assert counts["l0_pending_parse"] == 0
