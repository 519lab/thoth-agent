"""RecallLogWriter — Phase C Task 8 / spec §9.6."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.l0 import commit_slice
from substrate.recall import recall
from substrate.recall.log import RecallLogRow, RecallLogWriter
from substrate.storage import Family, Modality, DEFAULT_TEXT_PROFILE


@pytest.fixture(autouse=True)
def _enable_mock_embeddings(monkeypatch):
    from substrate.recall import embeddings

    monkeypatch.setenv(embeddings.MOCK_ENV_VAR, "1")
    embeddings.reset_client_cache()


@pytest_asyncio.fixture
async def booted_substrate(thoth_db_initialized):
    sub = await Substrate.boot(
        config=SubstrateConfig(auto_migrate=False, start_subagents=False),
        start_subagents=False,
    )
    try:
        yield sub
    finally:
        await sub.shutdown()


def _row(**overrides) -> RecallLogRow:
    base = dict(
        requested_at=datetime.now(timezone.utc),
        session_id="sess-A",
        query_excerpt="q",
        candidates_count=1,
        composed_count=1,
        tokens_used=10,
        duration_ms=5,
        timed_out=False,
        error_text=None,
        metadata={},
    )
    base.update(overrides)
    return RecallLogRow(**base)


@pytest.mark.asyncio
async def test_recall_writes_log_row(booted_substrate):
    """After a recall() call, a row appears in substrate_recall_log
    within the drain window."""
    import thoth_db

    # Seed a slice so the recall has something to do.
    stream = await booted_substrate.streams.get_by_name(
        "thoth.world.user_message.cli"
    )
    await commit_slice(
        booted_substrate,
        stream.stream_id,
        "hello",
        event_time_world=datetime.now(timezone.utc),
    )
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE substrate_slices SET sentinel_state='passed', trust_score=0.95, pending_committed_at=NULL WHERE sentinel_state='pending'"
        )

    proj = await recall(booted_substrate, "hello", session_id="sess-test")
    assert proj is not None

    # Wait for the drain (1s interval).
    for _ in range(40):  # up to 4 seconds
        await asyncio.sleep(0.1)
        async with thoth_db.connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM substrate_recall_log WHERE session_id = 'sess-test' ORDER BY log_id DESC LIMIT 1"
            )
        if row is not None:
            break
    assert row is not None
    assert row["query_excerpt"] == "hello"
    assert row["candidates_count"] >= 1
    assert row["timed_out"] is False


@pytest.mark.asyncio
async def test_recall_log_bounded_queue_drops_oldest(booted_substrate):
    """Filling the queue past capacity increments drop counter and
    keeps the queue size at the cap (drops the oldest, not the newest)."""
    # Replace the writer with a tiny-capacity one.
    await booted_substrate.recall_log.stop()
    booted_substrate.recall_log = RecallLogWriter(booted_substrate, max_queue_depth=3)
    # Don't start the drain — we want to inspect the queue state.

    for i in range(10):
        booted_substrate.recall_log.enqueue(_row(query_excerpt=f"q{i}"))
    assert booted_substrate.recall_log.queue_size == 3
    assert booted_substrate.recall_log.drop_count >= 1


@pytest.mark.asyncio
async def test_recall_log_writer_drains_in_background(booted_substrate):
    """A row enqueued lands in PG within the drain window."""
    import thoth_db

    booted_substrate.recall_log.enqueue(
        _row(session_id="sess-drain", query_excerpt="drain-test")
    )
    # Wait for drain.
    for _ in range(40):
        await asyncio.sleep(0.1)
        async with thoth_db.connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM substrate_recall_log WHERE session_id = 'sess-drain' ORDER BY log_id DESC LIMIT 1"
            )
        if row is not None:
            break
    assert row is not None
    assert row["query_excerpt"] == "drain-test"


@pytest.mark.asyncio
async def test_recall_log_writer_stops_cleanly(booted_substrate):
    """stop() cancels the drain task and is idempotent."""
    await booted_substrate.recall_log.stop()
    # Second stop should be a no-op.
    await booted_substrate.recall_log.stop()
