"""Curator embedding-emit loop — Phase C Task 9b / spec §9.6c."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.agents.curator import Curator
from substrate.config import SubstrateConfig
from substrate.l0 import commit_slice
from substrate.storage import DEFAULT_TEXT_PROFILE, Family, Modality


@pytest.fixture(autouse=True)
def _enable_mock_embeddings(monkeypatch):
    """Default the embedding client to the deterministic mock path."""
    from substrate.recall import embeddings

    monkeypatch.setenv(embeddings.MOCK_ENV_VAR, "1")
    embeddings.reset_client_cache()


@pytest_asyncio.fixture
async def booted_substrate(hermes_db_initialized):
    """Boot without sub-agents — we drive the Curator manually via tick()."""
    sub = await Substrate.boot(
        config=SubstrateConfig(auto_migrate=False, start_subagents=False),
        start_subagents=False,
    )
    try:
        yield sub
    finally:
        await sub.shutdown()


async def _seed_passed_slice(substrate, *, text: str) -> None:
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
            "UPDATE substrate_slices SET sentinel_state='passed', trust_score=0.95, pending_committed_at=NULL WHERE sentinel_state='pending'"
        )


def _force_interval_zero(monkeypatch):
    """Make the Curator's embed-backfill interval 0 so consecutive
    ``_maybe_emit_embeddings`` calls actually do work."""
    from substrate import config as _cfg

    monkeypatch.setattr(_cfg, "RECALL_EMBEDDING_BACKFILL_INTERVAL_S", 0.0)


@pytest.mark.asyncio
async def test_curator_embeds_unembedded_slice(booted_substrate, monkeypatch):
    """Commit a slice; run one Curator cycle → its embedding is non-NULL."""
    _force_interval_zero(monkeypatch)
    await _seed_passed_slice(booted_substrate, text="needs embedding")

    curator = Curator(booted_substrate)
    await curator._emit_embeddings_for_unembedded()

    import thoth_db
    async with thoth_db.connection() as conn:
        emb = await conn.fetchval(
            "SELECT embedding FROM substrate_slices WHERE payload->>'text' = 'needs embedding'"
        )
    assert emb is not None
    assert len(emb) == 1536


@pytest.mark.asyncio
async def test_curator_idempotent_under_concurrent_emit(booted_substrate, monkeypatch):
    """A second set_embedding call on an already-embedded slice is a no-op."""
    _force_interval_zero(monkeypatch)
    await _seed_passed_slice(booted_substrate, text="immune to overwrite")

    import thoth_db
    async with thoth_db.connection() as conn:
        sid = await conn.fetchval(
            "SELECT slice_id FROM substrate_slices WHERE payload->>'text' = 'immune to overwrite'"
        )
        first = [0.1] * 1536
        second = [0.9] * 1536
        wrote1 = await booted_substrate.slices.set_embedding(conn, sid, first)
        wrote2 = await booted_substrate.slices.set_embedding(conn, sid, second)
        stored = await conn.fetchval(
            "SELECT embedding FROM substrate_slices WHERE slice_id = $1", sid
        )
    assert wrote1 is True
    assert wrote2 is False
    assert stored[0] == pytest.approx(0.1, abs=1e-5)


@pytest.mark.asyncio
async def test_curator_backfill_batch_size(booted_substrate, monkeypatch):
    """Commit ``2 * BATCH_SIZE`` slices, run one cycle → exactly
    BATCH_SIZE are embedded that cycle; remainder waits for next."""
    from substrate import config as _cfg

    _force_interval_zero(monkeypatch)
    monkeypatch.setattr(_cfg, "RECALL_EMBEDDING_BATCH_SIZE", 5)

    for i in range(12):
        await _seed_passed_slice(booted_substrate, text=f"batch_{i}")

    curator = Curator(booted_substrate)
    await curator._emit_embeddings_for_unembedded()

    import thoth_db
    async with thoth_db.connection() as conn:
        embedded_count = await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_slices WHERE embedding IS NOT NULL AND payload->>'text' LIKE 'batch_%'"
        )
    assert embedded_count == 5

    # Second cycle picks up the next batch.
    await curator._emit_embeddings_for_unembedded()
    async with thoth_db.connection() as conn:
        embedded_count_2 = await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_slices WHERE embedding IS NOT NULL AND payload->>'text' LIKE 'batch_%'"
        )
    assert embedded_count_2 == 10


@pytest.mark.asyncio
async def test_curator_embedding_api_failure_leaves_slice_unembedded(
    booted_substrate, monkeypatch
):
    """When the embed call raises, the slice stays NULL; next cycle retries."""
    from substrate.recall import embeddings as _emb

    _force_interval_zero(monkeypatch)
    await _seed_passed_slice(booted_substrate, text="api fails")

    call_count = 0

    async def _raising_embed(texts, **kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("simulated api outage")

    monkeypatch.setattr("substrate.agents.curator.embed", _raising_embed)

    curator = Curator(booted_substrate)
    await curator._emit_embeddings_for_unembedded()

    import thoth_db
    async with thoth_db.connection() as conn:
        emb = await conn.fetchval(
            "SELECT embedding FROM substrate_slices WHERE payload->>'text' = 'api fails'"
        )
    assert emb is None
    assert call_count == 1
    # The slice is still in list_unembedded — not yet marked failed.
    async with thoth_db.connection() as conn:
        rows = await booted_substrate.slices.list_unembedded(conn, limit=100)
    assert any(
        (r["payload"].get("text") if isinstance(r["payload"], dict) else None) == "api fails"
        for r in rows
    )


@pytest.mark.asyncio
async def test_curator_embedding_repeated_failures_mark_slice(
    booted_substrate, monkeypatch
):
    """3+ failures mark embedding_failed=true; list_unembedded excludes it."""
    from substrate import config as _cfg

    _force_interval_zero(monkeypatch)
    monkeypatch.setattr(_cfg, "RECALL_EMBEDDING_BACKFILL_MAX_RETRIES", 3)
    await _seed_passed_slice(booted_substrate, text="poisoned payload")

    async def _raising_embed(texts, **kwargs):
        raise RuntimeError("permanent failure")

    monkeypatch.setattr("substrate.agents.curator.embed", _raising_embed)

    curator = Curator(booted_substrate)
    for _ in range(4):
        await curator._emit_embeddings_for_unembedded()

    import thoth_db
    async with thoth_db.connection() as conn:
        marked = await conn.fetchval(
            "SELECT (metadata->>'embedding_failed')::bool FROM substrate_slices WHERE payload->>'text' = 'poisoned payload'"
        )
    assert marked is True
    # And list_unembedded excludes it now.
    async with thoth_db.connection() as conn:
        rows = await booted_substrate.slices.list_unembedded(conn, limit=100)
    assert not any(
        (r["payload"].get("text") if isinstance(r["payload"], dict) else None) == "poisoned payload"
        for r in rows
    )


@pytest.mark.asyncio
async def test_recall_still_works_for_unembedded_slice(booted_substrate, monkeypatch):
    """A slice never embedded still surfaces in recall() via keyword Jaccard."""
    from substrate.recall import recall

    await _seed_passed_slice(booted_substrate, text="findable by keyword")
    # Do NOT run the Curator's embed loop — the slice stays unembedded.
    proj = await recall(booted_substrate, "keyword")
    assert "findable by keyword" in proj.text
