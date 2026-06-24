"""LLM-judge rerank pass for recall (innovation #3).

The pure ranker (:func:`substrate.recall.projection.rank_candidates_scored`)
produces a composite scalar (similarity + salience + recency). That scalar is
cheap and deterministic but blind to *direct* query relevance the way a reader
is — a slice can ride salience/recency into the top-K while being only loosely
on-topic. This module inserts an optional reorder of the top-K window using an
aux LLM-judge, between scoring and the relevance floor in
:mod:`substrate.recall.api`.

Design constraints (spec §3, plan #3):

* **Recall never raises.** ANY failure — judge error, timeout, malformed
  output, empty window — falls back to the *pre-rerank* order. The caller gets
  back a list that is always a permutation (or identity) of the input.
* **Bounded.** Only the top ``K`` candidates are reranked (``K`` capped at
  :data:`MAX_RERANK_K`); the tail keeps its scored order, appended after the
  reranked head.
* **One call.** The whole window is batched into a single judge invocation —
  no per-candidate round-trips.
* **Reuses the aux judge.** The ``judge`` is injected (built from
  ``get_async_text_auxiliary_client("recall_reranker")`` by the api layer);
  this module stays dependency-light and unit-testable with a mock judge.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover
    from substrate.recall.projection import ScoredCandidate

_log = logging.getLogger("substrate.recall.rerank")


# Hard cap on the rerank window regardless of the configured K — keeps the
# judge's context + latency bounded even if the env var is set high. Mirrors
# the "≤15" guidance in the plan.
MAX_RERANK_K = 15


# A judge takes the query and the ordered top-K excerpts and returns a new
# ordering as a list of 0-based indices into the input list. It may return a
# partial / permuted / padded list — :func:`rerank` sanitises it. The judge is
# async (it wraps an LLM call) and is responsible for its own timeout; on any
# internal failure it should raise (rerank swallows and falls back).
Judge = Callable[[str, list[str]], Awaitable[list[int]]]


def _excerpt(candidate: "ScoredCandidate", *, max_chars: int = 400) -> str:
    """Best-effort short text for one candidate, for the judge prompt.

    Mirrors the keyword-fallback text extraction in projection.py so the judge
    sees the same payload the scalar ranker scored. Truncated so the batched
    prompt stays bounded."""
    from substrate.recall.projection import _payload_text

    text = _payload_text(candidate.candidate.payload)
    text = " ".join((text or "").split())
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text


def _sanitise_order(order: list[int], n: int) -> list[int]:
    """Coerce a judge's raw index list into a full permutation of ``range(n)``.

    Drops out-of-range / duplicate / non-int indices, then appends any indices
    the judge omitted (in their original scored order) so nothing is lost. The
    result is always a permutation of ``range(n)`` — never shorter, never
    longer, never raising — so the caller can index the input safely."""
    seen: set[int] = set()
    out: list[int] = []
    for idx in order:
        if not isinstance(idx, int) or isinstance(idx, bool):
            continue
        if 0 <= idx < n and idx not in seen:
            seen.add(idx)
            out.append(idx)
    # Backfill any candidates the judge dropped, preserving scored order.
    if len(out) < n:
        out.extend(i for i in range(n) if i not in seen)
    return out


async def rerank(
    query: str,
    scored_topk: list["ScoredCandidate"],
    *,
    judge: Optional[Judge],
) -> list["ScoredCandidate"]:
    """Reorder the top-K scored candidates by LLM-judged query relevance.

    ``scored_topk`` is the already-scored, highest-first window (the caller
    passes ``scored[:K]``); the tail beyond K is handled by the caller. Returns
    a reordered copy of the head followed by any beyond-cap remainder, or — on
    ANY failure — the input list unchanged. Never raises.

    The cap :data:`MAX_RERANK_K` is applied here so an over-large configured K
    can't blow the judge budget: only the first ``MAX_RERANK_K`` are reranked;
    the remainder keeps its scored order and is appended unchanged."""
    if judge is None or not scored_topk or not (query or "").strip():
        return scored_topk

    head = scored_topk[:MAX_RERANK_K]
    tail = scored_topk[MAX_RERANK_K:]
    if len(head) < 2:
        # Nothing to reorder — single (or empty) head; skip the judge call.
        return scored_topk

    try:
        excerpts = [_excerpt(c) for c in head]
        order = await judge(query, excerpts)
        if not isinstance(order, list):
            raise TypeError(f"judge returned {type(order).__name__}, expected list")
        perm = _sanitise_order(order, len(head))
        reranked = [head[i] for i in perm]
    except Exception as exc:
        # Best-effort: a failed rerank degrades to the scored order, never an
        # error (recall must never raise — same idiom as the L1/L3/L4 headers).
        _log.debug("recall rerank failed, falling back to scored order: %s", exc)
        return scored_topk

    return reranked + tail


__all__ = ["rerank", "Judge", "MAX_RERANK_K"]
