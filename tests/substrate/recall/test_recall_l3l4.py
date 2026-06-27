"""Recall L3/L4 extension — the ## Patterns and ## Self-model headers.

Added 2026-06-17: recall previously only surfaced L0 quotes + the L1 entity
header, so the substrate's higher-order abstractions (L3 patterns, L4
self-model observations) were unreachable from the foreground. These cover the
pure renderers and the recall() integration that prepends them.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.l0 import commit_slice
from substrate.l3 import store as l3_store
from substrate.l4 import store as l4_store
from substrate.recall import recall
from substrate.recall.composer import render_l3_header, render_l4_header


@pytest.fixture(autouse=True)
def _enable_mock_embeddings(monkeypatch):
    from substrate.recall import embeddings

    monkeypatch.setenv(embeddings.MOCK_ENV_VAR, "1")
    embeddings.reset_client_cache()


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


async def _seed_passed_slice(substrate, text):
    stream = await substrate.streams.get_by_name("thoth.world.user_message.cli")
    await commit_slice(
        substrate, stream.stream_id, text,
        event_time_world=datetime.now(timezone.utc), born_passed=True,
    )


# ---------------------------------------------------------------------------
# Pure header renderers
# ---------------------------------------------------------------------------


def test_render_l3_header_empty():
    assert render_l3_header([]) == ""


def test_render_l3_header_all_blank_statements():
    # A list whose statements are all empty yields no header (just a count
    # line would be misleading).
    assert render_l3_header([{"kind": "theme", "statement": "  "}]) == ""


def test_render_l3_header_formats_patterns():
    out = render_l3_header([
        {"kind": "theme", "statement": "user prefers terse answers",
         "cites": ["a1b2c3", "d4e5f6"]},
        {"kind": "generalization", "statement": "PG bugs cluster on loop affinity"},
    ])
    assert "## Patterns (2)" in out
    assert "- [theme] user prefers terse answers (cites: a1b2c3, d4e5f6)" in out
    assert "- [generalization] PG bugs cluster on loop affinity" in out


def test_render_l4_header_empty():
    assert render_l4_header([]) == ""


def test_render_l4_header_formats_observations():
    out = render_l4_header([
        {"kind": "calibration", "subject": "self",
         "statement": "overestimates recall precision"},
        {"kind": "bias", "subject": "", "statement": "favours recent context"},
    ])
    assert "## Self-model (2)" in out
    assert "- [calibration] self: overestimates recall precision" in out
    # Empty subject → no "subject: " prefix.
    assert "- [bias] favours recent context" in out


# ---------------------------------------------------------------------------
# recall() integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_prepends_l3_header_when_patterns_match(booted, monkeypatch):
    import substrate.config as cfg
    monkeypatch.setattr(cfg, "RECALL_INCLUDE_L3", True)

    await l3_store.upsert_pattern(
        "the postgresql migration plan spans several phases", "generalization"
    )
    await _seed_passed_slice(booted, "we discussed the postgresql migration today")

    proj = await recall(booted, "postgresql migration plan")
    assert "## Patterns" in proj.text
    assert "postgresql migration plan" in proj.text


@pytest.mark.asyncio
async def test_recall_prepends_l4_header_when_observations_match(booted, monkeypatch):
    import substrate.config as cfg
    monkeypatch.setattr(cfg, "RECALL_INCLUDE_L4", True)

    await l4_store.record_observation(
        "calibration", "self", "tends to overestimate postgresql migration risk"
    )
    await _seed_passed_slice(booted, "we discussed the postgresql migration today")

    proj = await recall(booted, "postgresql migration risk")
    assert "## Self-model" in proj.text
    assert "overestimate postgresql migration risk" in proj.text


@pytest.mark.asyncio
async def test_recall_no_l3l4_headers_when_disabled(booted, monkeypatch):
    import substrate.config as cfg
    monkeypatch.setattr(cfg, "RECALL_INCLUDE_L3", False)
    monkeypatch.setattr(cfg, "RECALL_INCLUDE_L4", False)

    await l3_store.upsert_pattern("postgresql migration is multi-phase", "theme")
    await l4_store.record_observation(
        "calibration", "self", "overestimates postgresql migration risk"
    )
    await _seed_passed_slice(booted, "postgresql migration discussion")

    proj = await recall(booted, "postgresql migration")
    assert "## Patterns" not in proj.text
    assert "## Self-model" not in proj.text


@pytest.mark.asyncio
async def test_recall_l4_header_excludes_coherence(booted, monkeypatch):
    # The coherence vital sign must never leak into the self-model header —
    # it's the recall relevance-floor pin, surfaced separately.
    import substrate.config as cfg
    monkeypatch.setattr(cfg, "RECALL_INCLUDE_L4", True)

    await l4_store.upsert_coherence(
        "coherence steady for the postgresql migration work", score=0.9
    )
    await _seed_passed_slice(booted, "postgresql migration discussion")

    proj = await recall(booted, "postgresql migration")
    # Coherence observation is excluded by kind, so no self-model header from it.
    assert "## Self-model" not in proj.text


# ---------------------------------------------------------------------------
# L3/L4 semantic ordering (innovation #3). When THOTH_RECALL_L3L4_SEMANTIC is
# on and a query embedding exists, the header stores order patterns/observations
# by cosine distance over the backfilled embedding column instead of trigram +
# salience. These exercise the new query_embedding path on get_patterns_for_query
# / get_observations_for_query directly against PG.
#
# DB-BACKED — written but NOT run by the innovation agent (a live Postgres on
# 5433 must not be touched). Run under the normal test harness.
# ---------------------------------------------------------------------------


async def _embed(text: str) -> list[float]:
    from substrate.recall import embeddings

    return await embeddings.embed_query(text)


@pytest.mark.asyncio
async def test_get_patterns_semantic_orders_by_embedding(booted):
    """With a query embedding, patterns rank by cosine distance — the row whose
    embedding is closest to the query embedding comes first, regardless of
    trigram overlap with the query string."""
    near = "the postgresql migration plan spans several phases"
    far = "the user prefers terse answers in chat"
    near_id, _ = await l3_store.upsert_pattern(near, "generalization")
    far_id, _ = await l3_store.upsert_pattern(far, "theme")
    # Embed each pattern statement so the semantic path has rows to order.
    await l3_store.set_embedding(near_id, await _embed(near))
    await l3_store.set_embedding(far_id, await _embed(far))

    # Query embedding matches the "near" statement (mock embeddings are
    # deterministic per-string), so it must sort first under semantic ordering.
    q_emb = await _embed(near)
    rows = await l3_store.get_patterns_for_query(
        "anything", limit=5, query_embedding=q_emb
    )
    assert rows, "semantic path returned no embedded patterns"
    assert rows[0].id == near_id


@pytest.mark.asyncio
async def test_get_patterns_trigram_fallback_when_no_embedding(booted):
    """No query embedding → the trigram + salience path (back-compat). A row
    with no embedding is still reachable via trigram match."""
    await l3_store.upsert_pattern(
        "the postgresql migration plan spans several phases", "generalization"
    )
    rows = await l3_store.get_patterns_for_query(
        "postgresql migration plan", limit=5
    )
    assert rows
    assert any("postgresql migration" in r.statement for r in rows)


@pytest.mark.asyncio
async def test_get_observations_semantic_orders_by_embedding(booted):
    """L4 mirror of the L3 semantic-ordering test."""
    near = "tends to overestimate postgresql migration risk"
    far = "favours recent context over older facts"
    near_id = await l4_store.record_observation("calibration", "self", near)
    far_id = await l4_store.record_observation("bias", "self", far)
    await l4_store.set_embedding(near_id, await _embed(near))
    await l4_store.set_embedding(far_id, await _embed(far))

    q_emb = await _embed(near)
    rows = await l4_store.get_observations_for_query(
        "anything", limit=5, query_embedding=q_emb
    )
    assert rows, "semantic path returned no embedded observations"
    assert rows[0].id == near_id


@pytest.mark.asyncio
async def test_get_observations_semantic_excludes_coherence(booted):
    """The coherence vital sign is excluded from the semantic path too, even
    when it carries an embedding."""
    coh = "coherence steady across the postgresql migration work"
    await l4_store.upsert_coherence(coh, score=0.9)
    # Give the coherence singleton an embedding so only the kind filter can
    # exclude it.
    obs = await l4_store.latest_coherence()
    await l4_store.set_embedding(obs.id, await _embed(coh))

    rows = await l4_store.get_observations_for_query(
        "postgresql migration", limit=5, query_embedding=await _embed(coh)
    )
    assert all(o.kind != "coherence" for o in rows)


@pytest.mark.asyncio
async def test_recall_l3_semantic_header_when_flag_on(booted, monkeypatch):
    """End-to-end: with the semantic flag on and embedded patterns, recall()
    threads the query embedding into the L3 header and still renders it."""
    import substrate.config as cfg
    monkeypatch.setattr(cfg, "RECALL_INCLUDE_L3", True)
    monkeypatch.setattr(cfg, "RECALL_L3L4_SEMANTIC", True)

    stmt = "the postgresql migration plan spans several phases"
    pid, _ = await l3_store.upsert_pattern(stmt, "generalization")
    await l3_store.set_embedding(pid, await _embed(stmt))
    await _seed_passed_slice(booted, "we discussed the postgresql migration today")

    proj = await recall(booted, "postgresql migration plan")
    assert "## Patterns" in proj.text
    assert "postgresql migration plan" in proj.text
