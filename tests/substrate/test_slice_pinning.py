"""Slice pinning — decay-immune memories + forget (polish #4/#5).

A pinned slice must survive the Curator's decay + release; forget drops
salience to 0 so the Curator releases it.
"""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.agents.curator import Curator
from substrate.l0 import commit_slice
from substrate.l0.api import forget_slice, set_slice_pinned
from substrate.cli import inspect as inspect_mod


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


async def _commit_passed(substrate, text):
    stream = await substrate.streams.get_by_name("thoth.world.user_message.cli")
    await commit_slice(
        substrate, stream.stream_id, text,
        event_time_world=datetime.now(timezone.utc), born_passed=True,
    )
    import thoth_db

    async with thoth_db.connection() as conn:
        return await conn.fetchval(
            "SELECT slice_id FROM substrate_slices WHERE stream_id=$1 "
            "ORDER BY ingest_time_world DESC LIMIT 1",
            stream.stream_id,
        )


async def _row(slice_id):
    import thoth_db

    async with thoth_db.connection() as conn:
        return await conn.fetchrow(
            "SELECT pinned, salience_score, consolidation_state "
            "FROM substrate_slices WHERE slice_id=$1",
            slice_id,
        )


@pytest.mark.asyncio
async def test_set_pinned_and_unpin(booted):
    sid = await _commit_passed(booted, "remember the prod DSN rotation plan")
    assert await set_slice_pinned(sid, True) is True
    r = await _row(sid)
    assert r["pinned"] is True
    assert r["salience_score"] == pytest.approx(1.0)  # pin lifts salience
    assert await set_slice_pinned(sid, False) is True
    assert (await _row(sid))["pinned"] is False


@pytest.mark.asyncio
async def test_pinned_slice_survives_decay(booted):
    import thoth_db

    sid = await _commit_passed(booted, "pinned memory")
    await set_slice_pinned(sid, True)
    # Backdate salience_updated_at so a decay pass would normally erode it.
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE substrate_slices SET salience_updated_at = now() - interval '30 days' "
            "WHERE slice_id=$1",
            sid,
        )
    await Curator(booted)._apply_natural_decay()
    r = await _row(sid)
    assert r["salience_score"] == pytest.approx(1.0)  # untouched by decay


@pytest.mark.asyncio
async def test_pinned_slice_not_released(booted):
    import thoth_db

    sid = await _commit_passed(booted, "do not release me")
    await set_slice_pinned(sid, True)
    # Drive salience below any retain threshold — but pinned ⇒ not eligible.
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE substrate_slices SET salience_score=0.0 WHERE slice_id=$1", sid
        )
        eligible = await booted.slices.release_eligible(conn, limit=100)
    assert all(e.slice_id != sid for e in eligible)


@pytest.mark.asyncio
async def test_forget_drops_salience_and_unpins(booted):
    sid = await _commit_passed(booted, "forget me")
    await set_slice_pinned(sid, True)
    assert await forget_slice(sid) is True
    r = await _row(sid)
    assert r["salience_score"] == pytest.approx(0.0)
    assert r["pinned"] is False  # forget overrides pin


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_register_subparser_pin_forget():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    inspect_mod.register_subparser(sub)
    for cmd in (["substrate", "pin", "x"], ["substrate", "unpin", "x"], ["substrate", "forget", "x"]):
        assert callable(parser.parse_args(cmd).func)


@pytest.mark.asyncio
async def test_cli_pin_and_bad_id(booted):
    import thoth_db

    sid = await _commit_passed(booted, "cli pin target")
    async with thoth_db.connection() as conn:
        buf = io.StringIO()
        with redirect_stdout(buf):
            await inspect_mod._do_pin(conn, str(sid), True)
        assert "pinned slice" in buf.getvalue()
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            await inspect_mod._do_pin(conn, "not-a-uuid", True)
        assert "not a valid slice id" in buf2.getvalue()
