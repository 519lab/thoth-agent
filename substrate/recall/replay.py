"""Offline recall-replay eval harness — innovation #1.

Pure, offline re-ranking of *already-logged* recalls so we can ask "would a
different weight vector have surfaced better context?" without re-running the
live pipeline. The signal that makes this possible is the per-turn
``outcome_score`` label (migration 0025 + ``agent/turn_outcome.py``) joined to
the per-candidate ranking inputs the recall path now stashes in
``substrate_recall_log.metadata['candidates']`` (no schema change).

Three layers:

  - :func:`load_labeled_recalls` — DB-touching: pull the labelled rows
    (``outcome_score IS NOT NULL``) and decode each into a :class:`LabeledRecall`
    with its candidates. The *only* part that needs a connection; everything
    below is pure.
  - :func:`replay_weights` — pure: re-rank one recall's candidates under a
    candidate :class:`Weights` vector and report which would be kept vs dropped
    under the relevance floor.
  - :func:`sweep` — pure: score a grid of weight vectors against a corpus of
    labelled recalls by the **v1 metric**: kept-vs-dropped outcome separation
    (mean outcome of recalls whose top-ranked candidate clears the floor minus
    those where it doesn't). Honest caveat below.

**v1 metric — read honestly.** We do NOT have per-slice graded relevance
labels yet (that arrives with #2's verdicts), so this is not NDCG. We have a
single ``outcome_score`` *per recall*, and the logged ``relevance`` as a fixed
similarity term. The separation metric asks: under weight vector *w*, do the
recalls that ended in good turns tend to retain a strong top candidate, while
the bad-turn recalls don't? A weight vector that ranks genuinely-relevant
candidates to the top should separate the two populations. It is a coarse
proxy, reported as such — the harness is **report-only** and never auto-applies
weights.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayCandidate:
    """One logged candidate's ranking inputs, decoded from metadata.

    Mirrors the dict the recall path writes in
    ``substrate/recall/api.py`` under ``metadata['candidates']``:
    ``slice_id``, ``salience``, ``event_time`` (ISO-8601), ``relevance``
    (the fixed similarity term in [0, 1]), ``path`` ("semantic"/"keyword").
    """

    slice_id: str
    salience: float
    event_time: datetime
    relevance: float
    path: str


@dataclass(frozen=True)
class LabeledRecall:
    """One labelled recall row + its candidates.

    ``requested_at`` is the recency reference clock — candidate recency is
    ``exp(-age / half_life)`` with age measured back from here, exactly as the
    live ranker measures it from ``t_now``.
    """

    log_id: int
    session_id: Optional[str]
    requested_at: datetime
    outcome_score: float
    candidates: list[ReplayCandidate] = field(default_factory=list)


@dataclass(frozen=True)
class Weights:
    """A candidate weight vector for a replay sweep.

    Only the salience / recency / similarity / keyword weights matter for
    replay (the logged ``relevance`` is the fixed similarity term — we sweep
    how much to lean on salience vs recency vs that term). ``half_life_hours``
    shapes the recency decay.
    """

    similarity: float
    keyword: float
    salience: float
    recency: float
    half_life_hours: float

    def label(self) -> str:
        return (
            f"sim={self.similarity:g} kw={self.keyword:g} "
            f"sal={self.salience:g} rec={self.recency:g} "
            f"hl={self.half_life_hours:g}h"
        )


# ---------------------------------------------------------------------------
# DB load (the only non-pure layer)
# ---------------------------------------------------------------------------


def _decode_candidates(raw) -> list[ReplayCandidate]:
    """Decode the ``metadata['candidates']`` list into ReplayCandidates.

    Tolerant: skips malformed entries (missing keys, unparseable times)
    rather than failing the whole row — old rows logged before the
    per-candidate record existed simply yield an empty list.
    """
    out: list[ReplayCandidate] = []
    if not raw:
        return out
    for entry in raw:
        try:
            out.append(
                ReplayCandidate(
                    slice_id=str(entry["slice_id"]),
                    salience=float(entry["salience"]),
                    event_time=_parse_dt(entry["event_time"]),
                    relevance=float(entry["relevance"]),
                    path=str(entry.get("path", "keyword")),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


async def load_labeled_recalls(
    conn: "asyncpg.Connection",
    *,
    since: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> list[LabeledRecall]:
    """Load labelled recall rows (``outcome_score IS NOT NULL``) with their
    candidates, newest first.

    Rows without a per-candidate ``metadata['candidates']`` record (logged
    before innovation #1) decode to an empty candidate list and are dropped —
    the replay has nothing to re-rank for them.
    """
    import json

    clauses = ["outcome_score IS NOT NULL"]
    params: list = []
    if since is not None:
        params.append(since)
        clauses.append(f"requested_at >= ${len(params)}")
    where = " AND ".join(clauses)
    sql = (
        "SELECT log_id, session_id, requested_at, outcome_score, "
        "       metadata->'candidates' AS candidates "
        "  FROM substrate_recall_log "
        f" WHERE {where} "
        " ORDER BY requested_at DESC"
    )
    if limit is not None:
        params.append(limit)
        sql += f" LIMIT ${len(params)}"

    rows = await conn.fetch(sql, *params)
    out: list[LabeledRecall] = []
    for r in rows:
        raw = r["candidates"]
        # asyncpg returns the JSONB sub-document as text when the column is
        # projected via ``->`` without a registered codec on the slice; decode
        # defensively so the harness works regardless of pool codec config.
        if isinstance(raw, (str, bytes)):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = None
        candidates = _decode_candidates(raw)
        if not candidates:
            continue
        out.append(
            LabeledRecall(
                log_id=r["log_id"],
                session_id=r["session_id"],
                requested_at=r["requested_at"],
                outcome_score=float(r["outcome_score"]),
                candidates=candidates,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Pure re-ranking
# ---------------------------------------------------------------------------


def baseline_weights() -> Weights:
    """The live operational defaults — the baseline the sweep compares against.

    Read from ``substrate/config.py`` (the values ``recall()`` actually runs
    with), NOT the ``projection.py`` module constants (which are the library
    fallbacks and have deliberately drifted from the operational defaults).
    """
    from substrate import config as _cfg

    return Weights(
        similarity=_cfg.RECALL_SIMILARITY_WEIGHT,
        keyword=_cfg.RECALL_KEYWORD_WEIGHT,
        salience=_cfg.RECALL_SALIENCE_WEIGHT,
        recency=_cfg.RECALL_RECENCY_WEIGHT,
        half_life_hours=_cfg.RECALL_RECENCY_HALF_LIFE_HOURS,
    )


def _score_candidate(
    cand: ReplayCandidate, weights: Weights, t_now: datetime
) -> float:
    """Reconstruct the composite ranking score for one logged candidate.

    Mirrors ``rank_candidates_scored`` in ``projection.py``:
    ``sal_w*salience + rec_w*recency + sim_term``, where ``sim_term`` uses the
    semantic or keyword weight depending on the logged path and the logged
    ``relevance`` as the fixed similarity term.
    """
    half_life = weights.half_life_hours if weights.half_life_hours > 0 else 1e-9
    age_hours = max(0.0, (t_now - cand.event_time).total_seconds() / 3600.0)
    recency = math.exp(-age_hours / half_life)
    sim_w = weights.similarity if cand.path == "semantic" else weights.keyword
    return (
        weights.salience * cand.salience
        + weights.recency * recency
        + sim_w * cand.relevance
    )


@dataclass(frozen=True)
class ReplayResult:
    """Per-recall replay outcome under one weight vector."""

    log_id: int
    outcome_score: float
    # Candidates ranked best-first under the replay weights.
    ranked_slice_ids: list[str]
    # Top candidate's reconstructed score and whether it clears the relative
    # relevance floor (kept) under these weights.
    top_score: float
    top_kept: bool


def replay_weights(
    recall: LabeledRecall,
    weights: Weights,
    *,
    relative_floor: Optional[float] = None,
) -> ReplayResult:
    """Re-rank one labelled recall's candidates under ``weights``.

    "Kept vs dropped" reuses the live relative-floor idea: a candidate is
    *kept* when its score clears ``relative_floor * top_score`` (the strongest
    hit always survives). v1 only inspects whether the **top** candidate clears
    an absolute-relevance proxy — i.e. whether the best-ranked candidate has any
    real topical signal — which is what the separation metric keys on.
    """
    if relative_floor is None:
        from substrate import config as _cfg

        relative_floor = _cfg.RECALL_RELATIVE_FLOOR

    scored = [
        (_score_candidate(c, weights, recall.requested_at), c)
        for c in recall.candidates
    ]
    # Best-first; tiebreak on event_time (more recent wins) to match the live
    # ranker's secondary sort key.
    scored.sort(key=lambda t: (-t[0], -t[1].event_time.timestamp()))
    ranked_ids = [c.slice_id for _, c in scored]
    top_score, top_cand = scored[0]
    # The top candidate is "kept" when it carries genuine topical relevance —
    # a top pick riding salience/recency alone (relevance ~0) is a weak recall.
    # ``relative_floor`` doubles as the bar: relevance must exceed it.
    top_kept = top_cand.relevance >= relative_floor
    return ReplayResult(
        log_id=recall.log_id,
        outcome_score=recall.outcome_score,
        ranked_slice_ids=ranked_ids,
        top_score=top_score,
        top_kept=top_kept,
    )


@dataclass(frozen=True)
class SweepEntry:
    """One weight vector's aggregate score over the corpus."""

    weights: Weights
    # Mean outcome of recalls whose top candidate was kept vs dropped.
    mean_outcome_kept: float
    mean_outcome_dropped: float
    n_kept: int
    n_dropped: int

    @property
    def separation(self) -> float:
        """The v1 ranking metric: how much better do kept-top recalls end up
        than dropped-top ones. Higher = the weight vector's top pick is a
        better predictor of a good turn. ``0.0`` when one population is empty
        (no signal either way)."""
        if self.n_kept == 0 or self.n_dropped == 0:
            return 0.0
        return self.mean_outcome_kept - self.mean_outcome_dropped


def _evaluate(corpus: list[LabeledRecall], weights: Weights) -> SweepEntry:
    kept_outcomes: list[float] = []
    dropped_outcomes: list[float] = []
    for recall in corpus:
        res = replay_weights(recall, weights)
        if res.top_kept:
            kept_outcomes.append(res.outcome_score)
        else:
            dropped_outcomes.append(res.outcome_score)
    mean_kept = (
        sum(kept_outcomes) / len(kept_outcomes) if kept_outcomes else 0.0
    )
    mean_dropped = (
        sum(dropped_outcomes) / len(dropped_outcomes)
        if dropped_outcomes
        else 0.0
    )
    return SweepEntry(
        weights=weights,
        mean_outcome_kept=mean_kept,
        mean_outcome_dropped=mean_dropped,
        n_kept=len(kept_outcomes),
        n_dropped=len(dropped_outcomes),
    )


def default_grid(baseline: Optional[Weights] = None) -> list[Weights]:
    """A small, deterministic grid around the baseline weights.

    Sweeps the salience and recency weights (the two the logged-relevance
    similarity term lets us vary) across a coarse multiplicative grid; the
    similarity / keyword / half-life terms stay at baseline so the grid is
    small and the result interpretable.
    """
    base = baseline or baseline_weights()
    grid: list[Weights] = []
    for sal_mult in (0.5, 1.0, 1.5):
        for rec_mult in (0.5, 1.0, 1.5):
            grid.append(
                Weights(
                    similarity=base.similarity,
                    keyword=base.keyword,
                    salience=base.salience * sal_mult,
                    recency=base.recency * rec_mult,
                    half_life_hours=base.half_life_hours,
                )
            )
    return grid


def sweep(
    corpus: list[LabeledRecall],
    grid: Optional[list[Weights]] = None,
) -> list[SweepEntry]:
    """Evaluate every weight vector in ``grid`` against ``corpus``.

    Returns entries sorted best-first by the v1 separation metric. Pure: no DB,
    no LLM, deterministic for a fixed corpus + grid. The caller (the CLI) reports
    this — it never applies the winning weights.
    """
    if grid is None:
        grid = default_grid()
    entries = [_evaluate(corpus, w) for w in grid]
    entries.sort(key=lambda e: e.separation, reverse=True)
    return entries


__all__ = [
    "ReplayCandidate",
    "LabeledRecall",
    "Weights",
    "ReplayResult",
    "SweepEntry",
    "load_labeled_recalls",
    "baseline_weights",
    "replay_weights",
    "default_grid",
    "sweep",
]
