"""Round-4 forensic finding D: eviction-pointer relevance floor + cap.

The graded runs composed 141 ``context_evicted`` pointers into recall
projections and the model dereferenced <7% of them — the stream flooded the
foreground with restorable handles the model rarely wanted. ``recall`` now gates
eviction pointers through :func:`_gate_evicted_candidates` BEFORE composition:
each must clear a dedicated relevance floor (``RECALL_EVICTED_MIN_RELEVANCE``,
default 0.55 — well above the general 0.05) and at most ``RECALL_EVICTED_MAX``
(default 2) may enter a projection. Regular candidates are untouched.

Relevance is computed inside the live pipeline from embeddings / keyword match
against the query, so it can't be pinned above/below 0.55 deterministically in a
PG recall — these drive the pure gate directly with fixed relevance values.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from substrate import config as _cfg
from substrate.recall.api import _gate_evicted_candidates
from substrate.recall.composer import _CONTEXT_EVICTED_STREAM
from substrate.recall.projection import RecallCandidate, ScoredCandidate
from substrate.storage.types import Address


def _scored(*, stream: str, relevance: float, score: float) -> ScoredCandidate:
    now = datetime.now(timezone.utc)
    cand = RecallCandidate(
        slice_id=uuid4(),
        address=Address(uuid4(), now, now),
        stream_name=stream,
        payload={"text": "x"},
        event_time_world=now,
        salience_score=0.5,
        trust_score=None,
        metadata={},
        embedding=None,
    )
    return ScoredCandidate(candidate=cand, score=score, path="semantic", relevance=relevance)


_REGULAR = "thoth.world.user_message.cli"


def test_low_relevance_eviction_excluded_high_relevance_composed():
    below = _scored(stream=_CONTEXT_EVICTED_STREAM, relevance=0.40, score=0.9)
    above = _scored(stream=_CONTEXT_EVICTED_STREAM, relevance=0.70, score=0.8)
    out = _gate_evicted_candidates([below, above])
    assert below not in out          # below the 0.55 floor → dropped
    assert above in out              # cleared the floor → kept


def test_eviction_cap_respected_keeps_highest_scored():
    # Three eviction pointers all clear the floor; only RECALL_EVICTED_MAX (2)
    # may compose, and they are the highest-scored (input is score-desc).
    e1 = _scored(stream=_CONTEXT_EVICTED_STREAM, relevance=0.9, score=0.90)
    e2 = _scored(stream=_CONTEXT_EVICTED_STREAM, relevance=0.9, score=0.80)
    e3 = _scored(stream=_CONTEXT_EVICTED_STREAM, relevance=0.9, score=0.70)
    out = _gate_evicted_candidates([e1, e2, e3])
    evicted = [sc for sc in out if sc.candidate.stream_name == _CONTEXT_EVICTED_STREAM]
    assert evicted == [e1, e2]       # top-2 by score; e3 dropped by the cap


def test_regular_candidates_unaffected_by_gate():
    # Regular candidates pass through untouched regardless of relevance/count —
    # even a low-relevance regular candidate is kept, and order is preserved.
    r_low = _scored(stream=_REGULAR, relevance=0.01, score=0.95)
    r_mid = _scored(stream=_REGULAR, relevance=0.30, score=0.60)
    r_more = _scored(stream=_REGULAR, relevance=0.20, score=0.10)
    out = _gate_evicted_candidates([r_low, r_mid, r_more])
    assert out == [r_low, r_mid, r_more]


def test_gate_preserves_interleaved_order_and_gates_only_eviction():
    r1 = _scored(stream=_REGULAR, relevance=0.02, score=0.99)
    e_ok = _scored(stream=_CONTEXT_EVICTED_STREAM, relevance=0.60, score=0.50)
    e_bad = _scored(stream=_CONTEXT_EVICTED_STREAM, relevance=0.10, score=0.40)
    r2 = _scored(stream=_REGULAR, relevance=0.03, score=0.30)
    out = _gate_evicted_candidates([r1, e_ok, e_bad, r2])
    assert out == [r1, e_ok, r2]     # regulars kept in place; e_bad dropped


def test_env_tunable_floor_and_cap(monkeypatch):
    # Tightening the floor drops a pointer that would otherwise pass; raising the
    # cap admits more. The gate reads the config module attributes at call time.
    monkeypatch.setattr(_cfg, "RECALL_EVICTED_MIN_RELEVANCE", 0.80)
    monkeypatch.setattr(_cfg, "RECALL_EVICTED_MAX", 3)
    e1 = _scored(stream=_CONTEXT_EVICTED_STREAM, relevance=0.85, score=0.9)
    e2 = _scored(stream=_CONTEXT_EVICTED_STREAM, relevance=0.85, score=0.8)
    e3 = _scored(stream=_CONTEXT_EVICTED_STREAM, relevance=0.85, score=0.7)
    e_below = _scored(stream=_CONTEXT_EVICTED_STREAM, relevance=0.70, score=0.95)
    out = _gate_evicted_candidates([e_below, e1, e2, e3])
    assert e_below not in out                     # 0.70 < tightened 0.80 floor
    assert [e1, e2, e3] == [sc for sc in out
                            if sc.candidate.stream_name == _CONTEXT_EVICTED_STREAM]
