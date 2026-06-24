"""Recall-replay harness — innovation #1.

Pure tests: deterministic re-ranking, kept-vs-dropped separation, and the
sweep ranking the known-best weight vector first. No DB (``load_labeled_recalls``
is exercised by the pg-backed ``test_recall_outcome.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from substrate.recall.replay import (
    LabeledRecall,
    ReplayCandidate,
    Weights,
    baseline_weights,
    default_grid,
    replay_weights,
    sweep,
)


_NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)


def _cand(
    slice_id: str,
    *,
    salience: float,
    relevance: float,
    age_hours: float = 0.0,
    path: str = "semantic",
) -> ReplayCandidate:
    return ReplayCandidate(
        slice_id=slice_id,
        salience=salience,
        event_time=_NOW - timedelta(hours=age_hours),
        relevance=relevance,
        path=path,
    )


def _recall(
    log_id: int,
    *,
    outcome: float,
    candidates: list[ReplayCandidate],
    requested_at: datetime | None = None,
) -> LabeledRecall:
    return LabeledRecall(
        log_id=log_id,
        session_id="sess",
        requested_at=requested_at or _NOW,
        outcome_score=outcome,
        candidates=candidates,
    )


def test_baseline_weights_read_from_config():
    from substrate import config as _cfg

    w = baseline_weights()
    assert w.similarity == _cfg.RECALL_SIMILARITY_WEIGHT
    assert w.salience == _cfg.RECALL_SALIENCE_WEIGHT
    assert w.recency == _cfg.RECALL_RECENCY_WEIGHT
    assert w.half_life_hours == _cfg.RECALL_RECENCY_HALF_LIFE_HOURS


def test_replay_ranks_higher_relevance_first_under_similarity_weight():
    # Two candidates: a high-relevance one and a high-salience-but-irrelevant
    # one. With a similarity-leaning vector, the relevant one ranks first.
    relevant = _cand("relevant", salience=0.1, relevance=0.9)
    salient = _cand("salient", salience=0.9, relevance=0.05)
    rec = _recall(1, outcome=1.0, candidates=[salient, relevant])
    w = Weights(
        similarity=1.0, keyword=1.0, salience=0.1, recency=0.0,
        half_life_hours=12.0,
    )
    res = replay_weights(rec, w)
    assert res.ranked_slice_ids[0] == "relevant"
    assert res.top_kept is True


def test_replay_top_kept_false_when_top_is_salience_only():
    # A salience-dominated vector puts the irrelevant high-salience slice on
    # top; its relevance is below the floor, so top_kept is False.
    relevant = _cand("relevant", salience=0.1, relevance=0.9)
    salient = _cand("salient", salience=0.9, relevance=0.01)
    rec = _recall(1, outcome=0.0, candidates=[relevant, salient])
    w = Weights(
        similarity=0.0, keyword=0.0, salience=1.0, recency=0.0,
        half_life_hours=12.0,
    )
    res = replay_weights(rec, w, relative_floor=0.5)
    assert res.ranked_slice_ids[0] == "salient"
    assert res.top_kept is False


def test_replay_is_deterministic():
    rec = _recall(
        1,
        outcome=1.0,
        candidates=[
            _cand("a", salience=0.5, relevance=0.5),
            _cand("b", salience=0.6, relevance=0.4),
        ],
    )
    w = baseline_weights()
    first = replay_weights(rec, w)
    second = replay_weights(rec, w)
    assert first.ranked_slice_ids == second.ranked_slice_ids
    assert first.top_score == second.top_score


def test_sweep_ranks_known_best_weights_first():
    # Construct a corpus where good outcomes correlate with high-RELEVANCE
    # top candidates and bad outcomes with high-SALIENCE-only top candidates.
    # A similarity-leaning weight vector should separate them; a
    # salience-leaning one should not. The sweep must rank the separating
    # vector first.
    good = [
        _recall(
            i,
            outcome=1.0,
            candidates=[
                _cand("rel", salience=0.2, relevance=0.9),
                _cand("noise", salience=0.95, relevance=0.02),
            ],
        )
        for i in range(5)
    ]
    bad = [
        _recall(
            10 + i,
            outcome=0.0,
            candidates=[
                # No genuinely-relevant candidate — only salient noise.
                _cand("noise1", salience=0.95, relevance=0.02),
                _cand("noise2", salience=0.9, relevance=0.01),
            ],
        )
        for i in range(5)
    ]
    corpus = good + bad

    sim_vector = Weights(
        similarity=1.0, keyword=1.0, salience=0.05, recency=0.0,
        half_life_hours=12.0,
    )
    sal_vector = Weights(
        similarity=0.0, keyword=0.0, salience=1.0, recency=0.0,
        half_life_hours=12.0,
    )
    entries = sweep(corpus, [sal_vector, sim_vector])

    # Best-first by separation.
    assert entries[0].weights == sim_vector
    assert entries[0].separation > 0.0
    # The similarity vector keeps the good recalls' relevant top pick and drops
    # the bad recalls' irrelevant top pick → clean separation.
    assert entries[0].mean_outcome_kept == pytest.approx(1.0)
    assert entries[0].mean_outcome_dropped == pytest.approx(0.0)


def test_sweep_separation_zero_when_one_population_empty():
    # Every top candidate clears the floor → no dropped population → the metric
    # has no signal and reports 0.0 rather than a misleading number.
    corpus = [
        _recall(
            i,
            outcome=1.0,
            candidates=[_cand("rel", salience=0.2, relevance=0.9)],
        )
        for i in range(3)
    ]
    w = Weights(
        similarity=1.0, keyword=1.0, salience=0.1, recency=0.0,
        half_life_hours=12.0,
    )
    entries = sweep(corpus, [w])
    assert entries[0].n_dropped == 0
    assert entries[0].separation == 0.0


def test_default_grid_includes_baseline():
    base = baseline_weights()
    grid = default_grid(base)
    labels = {w.label() for w in grid}
    assert base.label() in labels
    # 3 salience multipliers x 3 recency multipliers.
    assert len(grid) == 9
