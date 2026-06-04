"""Curator upper-layer (L3/L4) curation + generator change-gating.

Regression for the L3/L4 over-extraction: PatternFinder/Reflector were
re-deriving from static inputs every tick and the upper layers had no
semantic dedup and no decay/release, so reworded near-duplicates accumulated
without bound (~7.8k L3 / ~3.7k L4 from a few hundred entities).
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.agents.curator import Curator
from substrate.agents.pattern_finder import PatternFinder
from substrate.l1 import store as l1
from substrate.l3 import store as l3


def _vec(*lead: float) -> list[float]:
    """A HERMES_EMBEDDING_DIM-length vector with the given leading components.
    ``_vec(1, 0)`` ≈ ``_vec(1, 0)`` (cosine distance 0); ``_vec(0, 1)`` is
    orthogonal (distance 1)."""
    dim = int(os.environ.get("HERMES_EMBEDDING_DIM") or 1536)
    v = [0.0] * dim
    for i, x in enumerate(lead):
        v[i] = float(x)
    return v


@pytest_asyncio.fixture
async def substrate(thoth_db_initialized):
    import thoth_db

    return Substrate.from_pool(thoth_db.pool())


@pytest.mark.asyncio
async def test_curator_merges_near_duplicate_patterns(substrate):
    """Differently-worded patterns with near-identical embeddings collapse to
    one (recency-weighted canonical), unioning citations; a semantically
    distinct pattern is left alone."""
    id1, _ = await l3.upsert_pattern(
        "GracefulHeart domains span multiple providers", "theme", cites=["e1"]
    )
    id2, _ = await l3.upsert_pattern(
        "Multiple GracefulHeart domains across different providers", "theme", cites=["e2"]
    )
    id3, _ = await l3.upsert_pattern("llm-rig serves Qwen via vLLM", "theme", cites=["e3"])
    await l3.set_embedding(id1, _vec(1, 0))
    await l3.set_embedding(id2, _vec(1, 0))   # identical → near-duplicate of id1
    await l3.set_embedding(id3, _vec(0, 1))   # orthogonal → distinct

    await Curator(substrate)._merge_l3()

    remaining = await l3.list_patterns(limit=100)
    assert len(remaining) == 2, "the two near-dupes should collapse to one"
    survivor = next(p for p in remaining if "GracefulHeart" in p.statement)
    assert set(survivor.cites) == {"e1", "e2"}, "merged pattern unions citations"
    assert any("vLLM" in p.statement for p in remaining), "distinct pattern untouched"


@pytest.mark.asyncio
async def test_release_stale_uncited_patterns(substrate):
    """Decayed, stale, uncited patterns are released; reinforced/cited ones stay."""
    import thoth_db

    keep, _ = await l3.upsert_pattern("durable cited pattern", "theme", cites=["e9"])
    drop, _ = await l3.upsert_pattern("ephemeral low-value pattern", "other")
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE l3_patterns SET salience_score=0.05, "
            "last_seen_at = now() - interval '30 days', cites='[]'::jsonb WHERE id=$1",
            drop,
        )

    n = await l3.release_stale(floor=0.15, stale_seconds=7 * 86400)
    assert n == 1
    remaining = {p.id for p in await l3.list_patterns(limit=100)}
    assert keep in remaining and drop not in remaining


@pytest.mark.asyncio
async def test_decay_lowers_salience_over_time(substrate):
    """Decay anchored on salience_updated_at reduces salience of un-refreshed
    patterns."""
    import thoth_db

    pid, _ = await l3.upsert_pattern("aging pattern", "theme")
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE l3_patterns SET salience_score=0.8, "
            "salience_updated_at = now() - interval '7 days' WHERE id=$1",
            pid,
        )
    # One half-life (7 days) elapsed → ~halve.
    await l3.decay(half_life_seconds=7 * 86400)
    async with thoth_db.connection() as conn:
        sal = await conn.fetchval("SELECT salience_score FROM l3_patterns WHERE id=$1", pid)
    assert 0.35 <= sal <= 0.45


@pytest.mark.asyncio
async def test_patternfinder_change_gating(substrate, monkeypatch):
    """The PatternFinder only runs when L1 gained/updated entities since the
    last run — not every tick on a static L1."""
    monkeypatch.setenv("PATTERNFINDER_INTERVAL_S", "0")  # isolate the change-gate
    await l1.upsert_entity("Thing1", "concept")
    pf = PatternFinder(substrate)

    assert await pf._should_run() is True   # first run sets the watermark
    assert await pf._should_run() is False  # no new L1 since → skip
    await l1.upsert_entity("Thing2", "concept")
    assert await pf._should_run() is True   # new L1 → run again


@pytest.mark.asyncio
async def test_cross_kind_merge_collapses_same_fact(substrate):
    """The same fact stored under different kinds (theme vs generalization) is
    NOT caught by within-kind merge, but the tight cross-kind pass folds it."""
    a, _ = await l3.upsert_pattern(
        "UniFi uses modern and legacy endpoints", "theme", cites=["e1"]
    )
    b, _ = await l3.upsert_pattern(
        "UniFi relies on modern and legacy endpoints", "generalization", cites=["e2"]
    )
    await l3.set_embedding(a, _vec(1, 0))
    await l3.set_embedding(b, _vec(1, 0))   # identical → cross-kind near-dup

    c = Curator(substrate)
    c.UPPER_MERGE_CROSS_KIND_MAX_DISTANCE = 0.1   # enable cross-kind
    await c._merge_l3()

    remaining = await l3.list_patterns(limit=100)
    assert len(remaining) == 1, "cross-kind near-dupes should collapse to one"
    assert set(remaining[0].cites) == {"e1", "e2"}


@pytest.mark.asyncio
async def test_cross_kind_disabled_keeps_separate(substrate):
    """Cross-kind merging off (distance 0) → the same fact in two kinds stays."""
    a, _ = await l3.upsert_pattern("X via A and B", "theme")
    b, _ = await l3.upsert_pattern("X relies on A and B", "generalization")
    await l3.set_embedding(a, _vec(1, 0))
    await l3.set_embedding(b, _vec(1, 0))

    c = Curator(substrate)
    c.UPPER_MERGE_CROSS_KIND_MAX_DISTANCE = 0.0   # disabled
    await c._merge_l3()
    assert len(await l3.list_patterns(limit=100)) == 2


def test_merge_thresholds_env_tunable(monkeypatch):
    """The merge distances are operator-tunable via env (no redeploy)."""
    monkeypatch.setenv("HERMES_SUBSTRATE_MERGE_MAX_DISTANCE", "0.25")
    monkeypatch.setenv("HERMES_SUBSTRATE_MERGE_CROSS_KIND_DISTANCE", "0.09")
    c = Curator(None)  # these attrs don't touch the DB
    assert c.UPPER_MERGE_MAX_DISTANCE == 0.25
    assert c.UPPER_MERGE_CROSS_KIND_MAX_DISTANCE == 0.09
