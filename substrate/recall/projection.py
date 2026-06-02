"""Recall dataclasses + pure-Python ranker.

:class:`RecallCandidate` and :class:`RecallProjection` are the public
data shapes the recall pipeline exchanges. :func:`rank_candidates` is
the pure-function ranker — semantic cosine when both query and
candidate have embeddings, keyword-Jaccard fallback per-candidate
otherwise.

Per Phase C spec §4.1 and §5.2 — kept in one module so the dataclasses
and the ranker that consumes them stay co-located. The composer lives
in :mod:`substrate.recall.composer`; the orchestrator in
:mod:`substrate.recall.api`.
"""

from __future__ import annotations

import math
import re
import string
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Union
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover
    from substrate.storage.types import Address


# ---------------------------------------------------------------------------
# Dataclasses — frozen so they can be used as dict keys / set members in
# downstream consumers (e.g. dedup by Address).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecallCandidate:
    """One slice candidate returned by ``SliceRepo.recall_window``.

    ``slice_id`` is the UUID primary key — uniquely identifies the
    slice (Address triples can collide when multiple slices share the
    same stream + event time, e.g. duplicate user messages emitted in
    the same tick).

    ``payload`` is already decoded — text-modality slices arrive with
    ``{"text": ...}`` unwrapped to the bare string; structured-event
    payloads stay as dicts. ``embedding`` is the 1536-d vector when
    the Curator has backfilled, ``None`` otherwise — the ranker
    handles per-candidate fallback to keyword Jaccard.
    """

    slice_id: UUID
    address: "Address"
    stream_name: str
    payload: Union[str, dict]
    event_time_world: datetime
    salience_score: float
    trust_score: Optional[float]
    metadata: dict
    embedding: Optional[list[float]] = None


@dataclass(frozen=True)
class RecallProjection:
    """The composed projection returned by :func:`substrate.recall.api.recall`.

    ``text`` is the sanitised body (NOT fence-wrapped — the caller
    applies Thoth's ``build_memory_context_block`` wrapper). Empty
    when the pipeline produced no usable composition; ``empty_reason``
    distinguishes the cause for observability.
    """

    text: str
    tokens_used: int
    composed: list[RecallCandidate]
    candidates_seen: int
    duration_ms: int
    timed_out: bool
    empty_reason: Optional[str]


# ---------------------------------------------------------------------------
# Pure-Python ranker — Phase C spec §5.2.
# ---------------------------------------------------------------------------


# Default weights. Tunable via substrate/config.py (the kwargs on
# rank_candidates default to None and fall back to these module-level
# values so callers and tests can override per-call without going through
# config). The active "similarity" term and active "keyword" term are
# mutually exclusive per candidate so the composite is well-defined
# regardless of path.
DEFAULT_SIMILARITY_WEIGHT = 0.3
DEFAULT_KEYWORD_WEIGHT = 0.3
DEFAULT_SALIENCE_WEIGHT = 0.5
DEFAULT_RECENCY_WEIGHT = 0.2
DEFAULT_RECENCY_HALF_LIFE_HOURS = 12.0


_TOKEN_RE = re.compile(r"[^\s" + re.escape(string.punctuation) + r"]+")


def _tokenise(text: str) -> set[str]:
    """Lowercase + split on whitespace + drop punctuation. Returns a
    set so Jaccard is a single set operation downstream. Empty input
    returns an empty set (safe — empty intersection / union → 0)."""
    if not text:
        return set()
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text)}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity ``|A ∩ B| / |A ∪ B|``. Both empty → 0 (the
    safest convention here — "no signal" rather than "perfect match";
    rank_candidates relies on this so a query with no tokens doesn't
    boost every empty-payload candidate)."""
    if not a or not b:
        return 0.0
    union = len(a | b)
    if union == 0:
        return 0.0
    return len(a & b) / union


def _cosine(u: list[float], v: list[float]) -> float:
    """Cosine similarity over two equal-length vectors.

    Returns the raw dot product (real-model and mock-model embeddings are
    both unit-normalised, so dot product == cosine). The caller clamps
    to [0, 1] via ``(c + 1) / 2`` to keep all score terms in the same
    range. Vectors of unequal length raise — that's a dim-drift bug, not
    a runtime fallback.
    """
    if len(u) != len(v):
        raise ValueError(
            f"cosine: vector dim mismatch ({len(u)} vs {len(v)}); "
            "embedding model / column may be misconfigured"
        )
    return sum(a * b for a, b in zip(u, v))


def _payload_text(payload) -> str:
    """Best-effort text extraction for keyword-fallback tokenisation.
    Strings pass through; dicts get their ``text`` key if present, else
    the full json representation; anything else is str()'d."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        text_field = payload.get("text")
        if isinstance(text_field, str):
            return text_field
        # Fall back to a deterministic stringification — Jaccard over
        # the JSON soup is still better than nothing for structured
        # events that happen to share keywords with the query.
        import json

        try:
            return json.dumps(payload, sort_keys=True, separators=(",", ":"))
        except Exception:
            return str(payload)
    return str(payload)


@dataclass(frozen=True)
class ScoredCandidate:
    """A ranked candidate with its composite score and ranking path.

    ``path`` is ``"semantic"`` (cosine over embeddings) or ``"keyword"``
    (Jaccard fallback) — surfaced for the "why was this injected?"
    provenance the recall log records and the optional inline annotation.
    """

    candidate: RecallCandidate
    score: float
    path: str


def rank_candidates_scored(
    candidates: list[RecallCandidate],
    query: str,
    query_embedding: Optional[list[float]] = None,
    *,
    t_now: datetime,
    similarity_weight: Optional[float] = None,
    keyword_overlap_weight: Optional[float] = None,
    salience_weight: Optional[float] = None,
    recency_weight: Optional[float] = None,
    recency_half_life_hours: Optional[float] = None,
) -> list["ScoredCandidate"]:
    """Like :func:`rank_candidates` but returns each candidate with its
    composite score + ranking path, highest first. The recall API uses
    this to apply a relevance floor and record per-slice provenance;
    :func:`rank_candidates` is the score-stripped convenience wrapper."""
    sim_w = similarity_weight if similarity_weight is not None else DEFAULT_SIMILARITY_WEIGHT
    kw_w = (
        keyword_overlap_weight if keyword_overlap_weight is not None
        else DEFAULT_KEYWORD_WEIGHT
    )
    sal_w = salience_weight if salience_weight is not None else DEFAULT_SALIENCE_WEIGHT
    rec_w = recency_weight if recency_weight is not None else DEFAULT_RECENCY_WEIGHT
    half_life = (
        recency_half_life_hours if recency_half_life_hours is not None
        else DEFAULT_RECENCY_HALF_LIFE_HOURS
    )
    if half_life <= 0:
        half_life = 1e-9

    query_tokens = _tokenise(query)
    scored: list[tuple[float, float, ScoredCandidate]] = []
    for c in candidates:
        delta = t_now - c.event_time_world
        age_hours = max(0.0, delta.total_seconds() / 3600.0)
        recency = math.exp(-age_hours / half_life)
        if query_embedding is not None and c.embedding is not None:
            cos = _cosine(query_embedding, c.embedding)
            sim_term = sim_w * ((cos + 1.0) / 2.0)
            path = "semantic"
        else:
            payload_tokens = _tokenise(_payload_text(c.payload))
            sim_term = kw_w * _jaccard(query_tokens, payload_tokens)
            path = "keyword"
        score = sal_w * float(c.salience_score) + rec_w * recency + sim_term
        scored.append(
            (-score, -c.event_time_world.timestamp(), ScoredCandidate(c, score, path))
        )
    scored.sort(key=lambda t: (t[0], t[1]))
    return [sc for _, _, sc in scored]


def rank_candidates(
    candidates: list[RecallCandidate],
    query: str,
    query_embedding: Optional[list[float]] = None,
    *,
    t_now: datetime,
    similarity_weight: Optional[float] = None,
    keyword_overlap_weight: Optional[float] = None,
    salience_weight: Optional[float] = None,
    recency_weight: Optional[float] = None,
    recency_half_life_hours: Optional[float] = None,
) -> list[RecallCandidate]:
    """Rank ``candidates`` against ``query`` (with optional embedding).

    Composite score per candidate:
        score = salience_weight * candidate.salience_score
              + recency_weight  * exp(-age_hours / half_life)
              + similarity_term

    where ``similarity_term`` is:
      * ``similarity_weight * (cosine + 1) / 2`` when both
        ``query_embedding`` and ``candidate.embedding`` are present
        (clamps cosine in [-1, 1] to [0, 1] so all terms share a range)
      * ``keyword_overlap_weight * jaccard(query_tokens, payload_tokens)``
        when either side is missing

    Tiebreak: more recent ``event_time_world`` wins.

    Pure function: no SQL, no I/O. Delegates to
    :func:`rank_candidates_scored` and strips the scores.
    """
    return [
        sc.candidate
        for sc in rank_candidates_scored(
            candidates, query, query_embedding,
            t_now=t_now,
            similarity_weight=similarity_weight,
            keyword_overlap_weight=keyword_overlap_weight,
            salience_weight=salience_weight,
            recency_weight=recency_weight,
            recency_half_life_hours=recency_half_life_hours,
        )
    ]


__all__ = [
    "RecallCandidate",
    "RecallProjection",
    "ScoredCandidate",
    "rank_candidates",
    "rank_candidates_scored",
    "DEFAULT_SIMILARITY_WEIGHT",
    "DEFAULT_KEYWORD_WEIGHT",
    "DEFAULT_SALIENCE_WEIGHT",
    "DEFAULT_RECENCY_WEIGHT",
    "DEFAULT_RECENCY_HALF_LIFE_HOURS",
]
