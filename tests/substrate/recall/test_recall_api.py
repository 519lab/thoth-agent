"""Public recall() contract — Phase C Task 7 / spec §9.1.

Async end-to-end tests against a real PG fixture. Each test seeds a
booted substrate via ``Substrate.boot`` (not from_pool — we need the
recall_log writer attached) and exercises the full pipeline.

Mock embeddings are used throughout (THOTH_RECALL_EMBEDDING_MOCK=1)
so tests run offline and produce deterministic vectors.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.l0 import commit_slice
from substrate.recall import recall, recall_sync
from substrate.recall.projection import RecallProjection
from substrate.storage import (
    DEFAULT_TEXT_PROFILE,
    Family,
    Modality,
)


@pytest.fixture(autouse=True)
def _enable_mock_embeddings(monkeypatch):
    """Use the deterministic mock embedding path for every test."""
    from substrate.recall import embeddings

    monkeypatch.setenv(embeddings.MOCK_ENV_VAR, "1")
    embeddings.reset_client_cache()


@pytest_asyncio.fixture
async def booted_substrate(thoth_db_initialized):
    """Boot the substrate fully (so recall_log writer attaches).
    start_subagents=False keeps the test deterministic — no Sentinel
    tick competing for the pending queue."""
    sub = await Substrate.boot(
        config=SubstrateConfig(auto_migrate=False, start_subagents=False),
        start_subagents=False,
    )
    try:
        yield sub
    finally:
        await sub.shutdown()


async def _seed_passed_slice(substrate, *, text: str, t_now: datetime) -> None:
    """Commit a slice and immediately flip it to passed."""
    import thoth_db

    stream = await substrate.streams.get_by_name(
        "thoth.world.user_message.cli"
    )
    assert stream is not None
    await commit_slice(
        substrate,
        stream.stream_id,
        text,
        event_time_world=t_now,
    )
    async with thoth_db.connection() as conn:
        await conn.execute(
            """
            UPDATE substrate_slices
               SET sentinel_state = 'passed',
                   trust_score = 0.95,
                   pending_committed_at = NULL
             WHERE sentinel_state = 'pending'
            """
        )


@pytest.mark.asyncio
async def test_recall_empty_substrate_returns_empty_projection(booted_substrate):
    """No slices → projection with empty_reason='no_candidates'."""
    proj = await recall(booted_substrate, "anything")
    assert isinstance(proj, RecallProjection)
    assert proj.text == ""
    assert proj.empty_reason == "no_candidates"
    assert proj.composed == []
    assert proj.candidates_seen == 0
    assert proj.timed_out is False


@pytest.mark.asyncio
async def test_recall_respects_token_budget(booted_substrate):
    """token_budget=50 produces a short composition."""
    t = datetime.now(timezone.utc)
    for i in range(5):
        await _seed_passed_slice(booted_substrate, text=f"slice content {i}", t_now=t)
    proj = await recall(booted_substrate, "slice", token_budget=50)
    assert proj.tokens_used <= 50
    # At least one candidate was seen; whether it composes depends on
    # the heuristic encoder's accuracy. The contract is the budget cap.
    assert proj.candidates_seen >= 5


@pytest.mark.asyncio
async def test_recall_respects_time_window(booted_substrate):
    """A slice outside the time window is excluded."""
    t = datetime.now(timezone.utc)
    await _seed_passed_slice(booted_substrate, text="recent thought", t_now=t)
    await _seed_passed_slice(
        booted_substrate, text="ancient thought",
        t_now=t - timedelta(hours=72),
    )
    proj = await recall(
        booted_substrate, "thought",
        time_window=timedelta(hours=24),
    )
    assert "recent thought" in proj.text
    assert "ancient thought" not in proj.text


@pytest.mark.asyncio
async def test_recall_default_streams_only(booted_substrate):
    """Slices on thoth.self_state.* aren't in the default projection;
    explicit stream_filter includes them."""
    import thoth_db

    t = datetime.now(timezone.utc)
    # Seed a slice on a non-default stream.
    self_stream = await booted_substrate.streams.get_by_name(
        "thoth.self_state.cron_dispatch"
    )
    assert self_stream is not None
    await commit_slice(
        booted_substrate,
        self_stream.stream_id,
        {"event": "secret"},
        event_time_world=t,
    )
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE substrate_slices SET sentinel_state='passed', trust_score=0.95, pending_committed_at=NULL WHERE sentinel_state='pending'"
        )
    # Default: not seen.
    default_proj = await recall(booted_substrate, "secret")
    assert "secret" not in default_proj.text
    # Explicit: seen.
    explicit_proj = await recall(
        booted_substrate, "secret",
        stream_filter=["thoth.self_state.cron_dispatch"],
    )
    assert "secret" in explicit_proj.text


@pytest.mark.asyncio
async def test_recall_returns_within_timeout(booted_substrate, monkeypatch):
    """When the SQL fetch exceeds recall_timeout_ms, the projection
    returns with timed_out=True and empty_reason='timeout'."""
    # Force a timeout by monkey-patching the fetch helper to sleep.
    async def _slow_fetch(*args, **kwargs):
        await asyncio.sleep(5.0)
        return []

    monkeypatch.setattr("substrate.recall.api._fetch_candidates", _slow_fetch)
    proj = await recall(booted_substrate, "x", recall_timeout_ms=50)
    assert proj.timed_out is True
    assert proj.empty_reason == "timeout"
    assert proj.text == ""


@pytest.mark.asyncio
async def test_recall_db_error_returns_empty_projection(booted_substrate, monkeypatch):
    """If the recall_window raises a non-timeout error, the projection
    is empty with empty_reason='db_error'."""
    async def _broken_fetch(*args, **kwargs):
        raise RuntimeError("simulated db error")

    monkeypatch.setattr("substrate.recall.api._fetch_candidates", _broken_fetch)
    proj = await recall(booted_substrate, "x")
    assert proj.timed_out is False
    assert proj.empty_reason == "db_error"
    assert proj.text == ""


def test_recall_sync_facade_outside_loop(thoth_db_initialized_sync):
    """recall_sync works from a sync test body."""
    import thoth_db

    # Build a from_pool substrate (no boot side-effects needed for the
    # sync test — recall_window doesn't depend on subagents). We use
    # the sync-loop fixture so the pool binds to thoth_db's sync loop.
    sub = Substrate.from_pool(thoth_db.pool())
    proj = recall_sync(sub, "anything")
    assert isinstance(proj, RecallProjection)
    # Empty substrate → no_candidates.
    assert proj.empty_reason == "no_candidates"


@pytest.mark.asyncio
async def test_recall_returns_a_recall_projection(booted_substrate):
    """Smoke: even when an unknown stream_filter is passed and no
    candidates exist, the function returns a RecallProjection."""
    proj = await recall(
        booted_substrate,
        "x",
        stream_filter=["does.not.exist"],
    )
    assert isinstance(proj, RecallProjection)
    assert proj.candidates_seen == 0
