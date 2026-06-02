"""Summarizer — retrospective compression of older slices (LLM mocked)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.l0 import commit_slice
from substrate.agents import summarizer as summ_mod
from substrate.agents.summarizer import SUMMARY_STREAM, Summarizer


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


@pytest.fixture(autouse=True)
def _summarizer_on(monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_SUMMARIZER", "1")
    monkeypatch.setenv("SUMMARIZER_MIN_SLICES", "3")
    monkeypatch.setattr(Summarizer, "_resolve_client", staticmethod(lambda: (object(), "mock")))


async def _commit_old(substrate, text, *, age_h):
    """Commit an OLD passed slice (event_time backdated) for a session."""
    import hermes_db

    stream = await substrate.streams.get_by_name("thoth.world.user_message.cli")
    t = datetime.now(timezone.utc) - timedelta(hours=age_h)
    await commit_slice(
        substrate, stream.stream_id, text, event_time_world=t,
        metadata={"session_id": "sess-old", "source": "cli"}, born_passed=True,
    )
    # commit_slice clamps event_time to <= now via SQL; force the backdate.
    async with hermes_db.connection() as conn:
        await conn.execute(
            "UPDATE substrate_slices SET event_time_world=$1 "
            "WHERE payload->>'text'=$2 AND event_time_world > $1",
            t, text,
        )


@pytest.mark.asyncio
async def test_summarizer_compresses_old_session(booted, monkeypatch):
    import hermes_db

    for i in range(5):
        await _commit_old(booted, f"old message {i} about the postgres migration", age_h=48)

    captured = {}

    async def _fake(texts, *, client=None, model=None, substrate=None):
        captured["n"] = len(texts)
        return "Discussed the postgres migration across several messages."

    monkeypatch.setattr(summ_mod, "_summarize", _fake)
    await Summarizer(booted).tick()

    assert captured.get("n", 0) >= 3  # the old batch reached the summarizer

    # A summary slice landed in the summary stream, citing the originals.
    stream = await booted.streams.get_by_name(SUMMARY_STREAM)
    assert stream is not None
    async with hermes_db.connection() as conn:
        srow = await conn.fetchrow(
            "SELECT payload, summary_of FROM substrate_slices WHERE stream_id=$1",
            stream.stream_id,
        )
        assert srow is not None
        assert "postgres migration" in srow["payload"]["text"]
        assert srow["summary_of"] and len(srow["summary_of"]) >= 3
        # Originals marked summarized + faded.
        faded = await conn.fetch(
            "SELECT salience_score, metadata FROM substrate_slices "
            "WHERE metadata->>'session_id'='sess-old' AND metadata->>'summarized'='true'"
        )
    assert len(faded) >= 3
    assert all(r["salience_score"] < 1.0 for r in faded)


@pytest.mark.asyncio
async def test_summarizer_skips_recent_slices(booted, monkeypatch):
    # Recent slices (age 0) must NOT be summarized.
    import hermes_db

    stream = await booted.streams.get_by_name("thoth.world.user_message.cli")
    for i in range(5):
        await commit_slice(
            booted, stream.stream_id, f"fresh {i}",
            event_time_world=datetime.now(timezone.utc),
            metadata={"session_id": "sess-fresh", "source": "cli"}, born_passed=True,
        )

    async def _boom(*a, **k):
        raise AssertionError("recent slices should not be summarized")

    monkeypatch.setattr(summ_mod, "_summarize", _boom)
    await Summarizer(booted).tick()  # no old slices → no LLM call


@pytest.mark.asyncio
async def test_summarizer_disabled_is_noop(booted, monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_SUMMARIZER", "0")
    for i in range(5):
        await _commit_old(booted, f"old {i}", age_h=48)

    async def _boom(*a, **k):
        raise AssertionError("should not summarize when disabled")

    monkeypatch.setattr(summ_mod, "_summarize", _boom)
    await Summarizer(booted).tick()


@pytest.mark.asyncio
async def test_summarizer_does_not_resummarize(booted, monkeypatch):
    import hermes_db

    for i in range(4):
        await _commit_old(booted, f"old item {i}", age_h=48)

    async def _fake(texts, *, client=None, model=None, substrate=None):
        return "summary one"

    monkeypatch.setattr(summ_mod, "_summarize", _fake)
    await Summarizer(booted).tick()
    # Second tick: originals already marked summarized → no new summary.
    await Summarizer(booted).tick()

    stream = await booted.streams.get_by_name(SUMMARY_STREAM)
    async with hermes_db.connection() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_slices WHERE stream_id=$1", stream.stream_id
        )
    assert n == 1  # exactly one summary, not re-created
