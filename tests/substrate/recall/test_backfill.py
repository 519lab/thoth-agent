"""Tests for the standalone embedding-backfill primitive
(``substrate.recall.backfill``).

Proves the plumbing the grading harness relies on: NULL-embedding passed
slices get embedded on demand via the same ``embed()`` path the Curator
uses, idempotently, and with a clean no-provider guard. See
``eval/results/EMBEDDING-GAP-FINDING.md``.

NOTE: the mock embedder (``THOTH_RECALL_EMBEDDING_MOCK=1``) is SHA-256-seeded
and semantically meaningless — these tests prove the write/idempotency/batch
plumbing, NOT recall quality. The mock-vs-real code path is identical (both go
through ``embed()``), so a real OpenRouter key exercises the same code.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.l0 import commit_slice
from substrate.recall.backfill import (
    backfill_unembedded_slices,
    backfill_unembedded_slices_sync,
)


@pytest.fixture(autouse=True)
def _enable_mock_embeddings(monkeypatch):
    """Default the embedding client to the deterministic mock path."""
    from substrate.recall import embeddings

    monkeypatch.setenv(embeddings.MOCK_ENV_VAR, "1")
    embeddings.reset_client_cache()


@pytest_asyncio.fixture
async def booted_substrate(thoth_db_initialized):
    """Boot without sub-agents — no Curator ticks; we drive backfill directly."""
    sub = await Substrate.boot(
        config=SubstrateConfig(auto_migrate=False, start_subagents=False),
        start_subagents=False,
    )
    try:
        yield sub
    finally:
        await sub.shutdown()


async def _seed_passed_slice(substrate, *, text: str) -> None:
    """Commit a text slice and force it PASSED with a NULL embedding."""
    import thoth_db

    stream = await substrate.streams.get_by_name("thoth.world.user_message.cli")
    await commit_slice(
        substrate,
        stream.stream_id,
        text,
        event_time_world=datetime.now(timezone.utc),
    )
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE substrate_slices SET sentinel_state='passed', "
            "trust_score=0.95, pending_committed_at=NULL "
            "WHERE sentinel_state='pending'"
        )


async def _embedded_count(substrate, *, like: str = "%") -> int:
    import thoth_db

    async with thoth_db.connection() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_slices "
            "WHERE embedding IS NOT NULL AND payload->>'text' LIKE $1",
            like,
        )


@pytest.mark.asyncio
async def test_backfill_embeds_all_unembedded(booted_substrate):
    """N NULL-embedding passed slices → all N embedded; count returned."""
    n = 4
    for i in range(n):
        await _seed_passed_slice(booted_substrate, text=f"needs embedding {i}")

    embedded = await backfill_unembedded_slices(booted_substrate)

    assert embedded == n
    assert await _embedded_count(booted_substrate, like="needs embedding%") == n


@pytest.mark.asyncio
async def test_backfill_is_idempotent(booted_substrate):
    """A second pass over already-embedded slices writes nothing (embeds 0)."""
    for i in range(3):
        await _seed_passed_slice(booted_substrate, text=f"once only {i}")

    first = await backfill_unembedded_slices(booted_substrate)
    second = await backfill_unembedded_slices(booted_substrate)

    assert first == 3
    assert second == 0
    assert await _embedded_count(booted_substrate, like="once only%") == 3


@pytest.mark.asyncio
async def test_backfill_no_provider_returns_zero(booted_substrate, monkeypatch):
    """No embedding provider (mock off, keys absent) → returns 0, no raise,
    slices stay NULL."""
    from substrate.recall import embeddings

    # Turn off the mock path and force the provider resolution to None.
    monkeypatch.delenv(embeddings.MOCK_ENV_VAR, raising=False)
    monkeypatch.setattr(embeddings, "_resolve_embedding_provider", lambda: None)
    embeddings.reset_client_cache()

    await _seed_passed_slice(booted_substrate, text="orphan, no provider")

    embedded = await backfill_unembedded_slices(booted_substrate)

    assert embedded == 0
    assert await _embedded_count(booted_substrate, like="orphan%") == 0


@pytest.mark.asyncio
async def test_backfill_batch_size_and_max_batches(booted_substrate):
    """batch_size bounds one round; max_batches caps rounds."""
    for i in range(12):
        await _seed_passed_slice(booted_substrate, text=f"batch {i:02d}")

    # One batch of 5 → exactly 5 embedded, remainder untouched.
    embedded = await backfill_unembedded_slices(
        booted_substrate, batch_size=5, max_batches=1
    )
    assert embedded == 5
    assert await _embedded_count(booted_substrate, like="batch %") == 5

    # Drain the rest (no cap) → the remaining 7 embed.
    rest = await backfill_unembedded_slices(booted_substrate, batch_size=5)
    assert rest == 7
    assert await _embedded_count(booted_substrate, like="batch %") == 12


@pytest.mark.asyncio
async def test_backfill_only_touches_passed_slices(booted_substrate):
    """Pending (un-passed) slices are ignored — list_unembedded filters them."""
    import thoth_db

    stream = await booted_substrate.streams.get_by_name(
        "thoth.world.user_message.cli"
    )
    # Commit a slice but leave it PENDING (do not flip to passed).
    await commit_slice(
        booted_substrate,
        stream.stream_id,
        "still pending",
        event_time_world=datetime.now(timezone.utc),
    )

    embedded = await backfill_unembedded_slices(booted_substrate)
    assert embedded == 0
    async with thoth_db.connection() as conn:
        emb = await conn.fetchval(
            "SELECT embedding FROM substrate_slices "
            "WHERE payload->>'text' = 'still pending'"
        )
    assert emb is None


def test_backfill_sync_wrapper_bridges_via_run_sync(monkeypatch):
    """The sync facade runs the async primitive on the sync loop and returns
    its count — no running event loop required (the grading-harness path)."""
    import thoth_db
    from substrate.recall import backfill as backfill_mod

    called = {}

    async def _fake_async(substrate, *, batch_size=64, max_batches=None):
        called["substrate"] = substrate
        called["batch_size"] = batch_size
        return 9

    monkeypatch.setattr(backfill_mod, "backfill_unembedded_slices", _fake_async)

    # Stub run_sync so this stays a pure unit test (no DB) while still
    # executing the coroutine, so the sync wrapper's return value is real.
    def _run_on_fresh_loop(coro):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(thoth_db, "run_sync", _run_on_fresh_loop)

    sentinel = object()
    out = backfill_unembedded_slices_sync(sentinel, batch_size=16)

    assert out == 9
    assert called["substrate"] is sentinel
    assert called["batch_size"] == 16
