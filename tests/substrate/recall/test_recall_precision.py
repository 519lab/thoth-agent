"""Recall precision polish — relevance floor, MMR dedup, provenance.

Addresses the volume-vs-precision feedback: rank scored, drop the
loosely-related tail, skip near-duplicate excerpts, and explain why each
slice was injected.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.l0 import commit_slice
from substrate.recall import recall
from substrate.recall.composer import compose_projection
from substrate.recall.projection import (
    RecallCandidate,
    ScoredCandidate,
    rank_candidates,
    rank_candidates_scored,
)
from substrate.storage.types import Address


def _cand(text, *, salience=0.5, age_h=0.0):
    t = datetime.now(timezone.utc) - timedelta(hours=age_h)
    sid = uuid4()
    return RecallCandidate(
        slice_id=sid,
        address=Address(uuid4(), t, t),
        stream_name="thoth.world.user_message.cli",
        payload=text,
        event_time_world=t,
        salience_score=salience,
        trust_score=0.9,
        metadata={},
        embedding=None,
    )


# ---------------------------------------------------------------------------
# scored ranking
# ---------------------------------------------------------------------------


def test_rank_scored_sorts_desc_with_path():
    now = datetime.now(timezone.utc)
    cands = [_cand("apple", salience=0.1), _cand("banana", salience=0.9)]
    scored = rank_candidates_scored(cands, "banana", t_now=now)
    assert isinstance(scored[0], ScoredCandidate)
    assert scored[0].score >= scored[1].score  # descending
    assert scored[0].path == "keyword"  # no embeddings → keyword path
    # the high-salience banana (matches query) ranks first
    assert scored[0].candidate.payload == "banana"


def test_rank_candidates_delegates_to_scored():
    now = datetime.now(timezone.utc)
    cands = [_cand("a", salience=0.2), _cand("b", salience=0.8)]
    plain = rank_candidates(cands, "b", t_now=now)
    scored = rank_candidates_scored(cands, "b", t_now=now)
    assert [c.slice_id for c in plain] == [s.candidate.slice_id for s in scored]


# ---------------------------------------------------------------------------
# compose: dedup + provenance
# ---------------------------------------------------------------------------


def test_compose_dedups_near_duplicates():
    dup_text = "the postgres migration is in progress and going well"
    cands = [_cand(dup_text), _cand(dup_text), _cand("a totally different topic")]
    text, composed, _ = compose_projection(
        cands, token_budget=2000, dedup_threshold=0.8
    )
    # The two identical excerpts collapse to one; the distinct one stays.
    payloads = [c.payload for c in composed]
    assert payloads.count(dup_text) == 1
    assert "a totally different topic" in payloads


def test_compose_no_dedup_when_threshold_zero():
    dup = "same text"
    text, composed, _ = compose_projection(
        [_cand(dup), _cand(dup)], token_budget=2000, dedup_threshold=0.0
    )
    assert len(composed) == 2  # dedup disabled


def test_compose_provenance_annotation_opt_in():
    c = _cand("hello world")
    prov = {c.slice_id: "0.73 semantic"}
    shown, _, _ = compose_projection(
        [c], token_budget=2000, provenance=prov, show_provenance=True
    )
    assert "· why: 0.73 semantic" in shown
    clean, _, _ = compose_projection(
        [c], token_budget=2000, provenance=prov, show_provenance=False
    )
    assert "why:" not in clean  # clean by default


# ---------------------------------------------------------------------------
# recall() relevance floor
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_embeddings(monkeypatch):
    from substrate.recall import embeddings

    monkeypatch.setenv(embeddings.MOCK_ENV_VAR, "1")
    embeddings.reset_client_cache()


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


async def _seed(substrate, text, *, salience=1.0):
    import hermes_db

    stream = await substrate.streams.get_by_name("thoth.world.user_message.cli")
    await commit_slice(
        substrate, stream.stream_id, text,
        event_time_world=datetime.now(timezone.utc), born_passed=True,
    )
    async with hermes_db.connection() as conn:
        await conn.execute(
            "UPDATE substrate_slices SET salience_score=$1 WHERE payload->>'text'=$2",
            salience, text,
        )


@pytest.mark.asyncio
async def test_recall_floor_drops_loosely_related(booted, monkeypatch):
    import substrate.config as cfg

    # Disable the L1 header so we isolate L0 composition.
    monkeypatch.setattr(cfg, "RECALL_INCLUDE_L1", False)

    # One strongly-relevant, high-salience slice + several weak, stale,
    # low-salience ones that the floor should drop.
    await _seed(booted, "the kubernetes deployment pipeline failed on staging", salience=0.95)
    for i in range(4):
        await _seed(booted, f"unrelated chit chat number {i}", salience=0.06)

    proj = await recall(booted, "kubernetes deployment pipeline")
    # Filtering happened: fewer composed than candidates seen.
    assert proj.candidates_seen >= 5
    assert len(proj.composed) < proj.candidates_seen
    # The relevant content survived.
    assert "kubernetes deployment" in proj.text
