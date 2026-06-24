"""Recall reranker — innovation #3.

Pure-function tests against :func:`substrate.recall.rerank.rerank`. No DB, no
LLM — the judge is a plain async mock. Covers the three contract properties:

  1. a judge ordering reorders the top-K head,
  2. ANY judge failure falls back to the *pre-rerank* (scored) order,
  3. the rerank window is capped at ``MAX_RERANK_K`` — the tail beyond the cap
     keeps its scored order and is appended unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from substrate.recall.projection import RecallCandidate, ScoredCandidate
from substrate.recall.rerank import MAX_RERANK_K, rerank
from substrate.storage.types import Address


def _scored(text: str, *, score: float) -> ScoredCandidate:
    now = datetime.now(timezone.utc)
    cand = RecallCandidate(
        slice_id=uuid4(),
        address=Address(uuid4(), now, now),
        stream_name="test.stream",
        payload=text,
        event_time_world=now,
        salience_score=0.5,
        trust_score=None,
        metadata={},
        embedding=None,
    )
    return ScoredCandidate(cand, score, "keyword", 0.0)


def _texts(items: list[ScoredCandidate]) -> list[str]:
    return [c.candidate.payload for c in items]


@pytest.mark.asyncio
async def test_rerank_reorders_by_judge_ordering():
    """A judge that flips the order produces a flipped result."""
    scored = [_scored("a", score=0.9), _scored("b", score=0.5), _scored("c", score=0.1)]

    async def judge(query, excerpts):
        # Reverse: best-first → [2, 1, 0].
        return list(reversed(range(len(excerpts))))

    out = await rerank("q", scored, judge=judge)
    assert _texts(out) == ["c", "b", "a"]


@pytest.mark.asyncio
async def test_rerank_partial_order_backfills_dropped_indices():
    """A judge that returns only some indices keeps the rest in scored order."""
    scored = [_scored("a", score=0.9), _scored("b", score=0.5), _scored("c", score=0.1)]

    async def judge(query, excerpts):
        return [2]  # only promote 'c'; 'a','b' backfill in scored order

    out = await rerank("q", scored, judge=judge)
    assert _texts(out) == ["c", "a", "b"]


@pytest.mark.asyncio
async def test_rerank_falls_back_on_judge_exception():
    """ANY judge error → the pre-rerank (scored) order, never raising."""
    scored = [_scored("a", score=0.9), _scored("b", score=0.5), _scored("c", score=0.1)]

    async def judge(query, excerpts):
        raise RuntimeError("judge blew up")

    out = await rerank("q", scored, judge=judge)
    assert _texts(out) == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_rerank_falls_back_on_non_list_judge_output():
    """A malformed (non-list) judge return is treated as a failure."""
    scored = [_scored("a", score=0.9), _scored("b", score=0.5)]

    async def judge(query, excerpts):
        return "not a list"  # type: ignore[return-value]

    out = await rerank("q", scored, judge=judge)
    assert _texts(out) == ["a", "b"]


@pytest.mark.asyncio
async def test_rerank_ignores_out_of_range_and_duplicate_indices():
    """Garbage indices are dropped; survivors + backfill stay a valid perm."""
    scored = [_scored("a", score=0.9), _scored("b", score=0.5), _scored("c", score=0.1)]

    async def judge(query, excerpts):
        # 99 out of range, 1 duplicated, -1 negative — all dropped.
        return [1, 99, 1, -1, 0]

    out = await rerank("q", scored, judge=judge)
    # Valid order from input: [1, 0]; 'c' (idx 2) backfills last.
    assert _texts(out) == ["b", "a", "c"]


@pytest.mark.asyncio
async def test_rerank_caps_window_and_appends_tail_unchanged():
    """Only the first MAX_RERANK_K are reranked; the tail keeps scored order."""
    n = MAX_RERANK_K + 3
    scored = [_scored(f"s{i}", score=1.0 - i * 0.01) for i in range(n)]
    head_texts = [f"s{i}" for i in range(MAX_RERANK_K)]
    tail_texts = [f"s{i}" for i in range(MAX_RERANK_K, n)]

    captured: dict = {}

    async def judge(query, excerpts):
        captured["n"] = len(excerpts)
        return list(reversed(range(len(excerpts))))

    out = await rerank("q", scored, judge=judge)
    # Judge only ever saw the capped head.
    assert captured["n"] == MAX_RERANK_K
    # Head reversed, tail untouched and appended after.
    assert _texts(out)[:MAX_RERANK_K] == list(reversed(head_texts))
    assert _texts(out)[MAX_RERANK_K:] == tail_texts


@pytest.mark.asyncio
async def test_rerank_noop_when_judge_none():
    scored = [_scored("a", score=0.9), _scored("b", score=0.5)]
    out = await rerank("q", scored, judge=None)
    assert out is scored


@pytest.mark.asyncio
async def test_rerank_noop_on_empty_or_blank_query():
    scored = [_scored("a", score=0.9), _scored("b", score=0.5)]

    async def judge(query, excerpts):  # pragma: no cover — must not be called
        raise AssertionError("judge should not run on a blank query")

    out = await rerank("   ", scored, judge=judge)
    assert out is scored


@pytest.mark.asyncio
async def test_rerank_noop_on_single_candidate():
    """A single-item head has nothing to reorder — skip the judge entirely."""
    scored = [_scored("only", score=0.9)]

    async def judge(query, excerpts):  # pragma: no cover — must not be called
        raise AssertionError("judge should not run on a single candidate")

    out = await rerank("q", scored, judge=judge)
    assert out is scored
