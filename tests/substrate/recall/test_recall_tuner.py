"""Recall-weight tuner — learned-recall-weights innovation.

Pure tests: the time-ordered split, coordinate descent recovering a known
better weight vector from a synthetic corpus, the guardrails refusing weak
corpora, and the search staying inside its bounds. No DB (persistence is
covered by ``test_recall_weights_store.py``; the live resolution path by
``test_recall_api.py``-style monkeypatching in
``test_recall_tuned_weights.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from substrate.recall.replay import (
    LabeledRecall,
    ReplayCandidate,
    Weights,
)
from substrate.recall.tuner import (
    HALF_LIFE_MAX_H,
    HALF_LIFE_MIN_H,
    WEIGHT_BOUND_HIGH,
    WEIGHT_BOUND_LOW,
    fit,
    split_corpus,
)


_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

_BASELINE = Weights(
    similarity=0.4, keyword=0.4, salience=0.35, recency=0.15,
    half_life_hours=12.0,
)


def _cand(
    slice_id: str,
    *,
    salience: float,
    relevance: float,
    event_time: datetime,
    path: str = "keyword",
) -> ReplayCandidate:
    return ReplayCandidate(
        slice_id=slice_id,
        salience=salience,
        event_time=event_time,
        relevance=relevance,
        path=path,
    )


def _salience_trap_corpus(n_good: int = 40, n_bad: int = 40) -> list[LabeledRecall]:
    """A corpus where the baseline's salience weight is the failure mode.

    Good-outcome recalls contain a genuinely relevant candidate (relevance
    0.6, salience 0.0) buried behind a salience-riding distractor (relevance
    0.0, salience 1.0). Under the baseline (sal 0.35 > kw 0.4 × 0.6 = 0.24
    + sal 0.0) the distractor tops every ranking, so top_kept is False for
    good and bad recalls alike → zero separation. Halving the salience
    weight surfaces the relevant candidate in good recalls only → full
    separation. Good/bad recalls interleave in time so both populations
    land in the holdout split.
    """
    corpus: list[LabeledRecall] = []
    for i in range(n_good + n_bad):
        requested_at = _NOW - timedelta(minutes=i)
        event_time = requested_at - timedelta(hours=1)
        good = i % 2 == 0
        if good:
            candidates = [
                _cand(f"distractor-{i}", salience=1.0, relevance=0.0,
                      event_time=event_time),
                _cand(f"relevant-{i}", salience=0.0, relevance=0.6,
                      event_time=event_time),
            ]
            outcome = 1.0
        else:
            candidates = [
                _cand(f"noise-a-{i}", salience=1.0, relevance=0.0,
                      event_time=event_time),
                _cand(f"noise-b-{i}", salience=0.4, relevance=0.0,
                      event_time=event_time),
            ]
            outcome = 0.0
        corpus.append(
            LabeledRecall(
                log_id=i,
                session_id="sess",
                requested_at=requested_at,
                outcome_score=outcome,
                candidates=candidates,
            )
        )
    return corpus


def test_split_corpus_is_time_ordered():
    """The NEWEST fraction is the holdout — validate in the deployment
    direction (fit on the past, judge on the most recent behaviour)."""
    corpus = _salience_trap_corpus(10, 10)
    train, holdout = split_corpus(corpus, holdout_fraction=0.3)
    assert len(holdout) == 6 and len(train) == 14
    oldest_holdout = min(r.requested_at for r in holdout)
    newest_train = max(r.requested_at for r in train)
    assert newest_train < oldest_holdout


def test_fit_recovers_lower_salience_weight():
    """Coordinate descent finds the salience-trap escape: a lower salience
    weight that lets topical relevance beat the distractor, taking holdout
    separation from ~0 to ~1 — and every guardrail passes."""
    result = fit(_salience_trap_corpus(), baseline=_BASELINE)

    assert result.best.salience < _BASELINE.salience
    assert result.baseline_holdout.separation == 0.0  # nothing kept at baseline
    assert result.best_holdout.separation > 0.9
    assert result.guardrails == []
    assert result.recommend is True
    assert result.holdout_improvement > 0.9


def test_fit_refuses_tiny_corpus():
    """A young install must get 'don't apply, here's why' — never a
    recommendation fit on noise."""
    result = fit(_salience_trap_corpus(4, 4), baseline=_BASELINE)
    assert result.recommend is False
    assert any("corpus too small" in g for g in result.guardrails)


def test_fit_refuses_when_nothing_improves():
    """A corpus with no exploitable signal (every candidate irrelevant, all
    outcomes identical) must not move off the baseline or recommend."""
    corpus: list[LabeledRecall] = []
    for i in range(60):
        requested_at = _NOW - timedelta(minutes=i)
        corpus.append(
            LabeledRecall(
                log_id=i,
                session_id="sess",
                requested_at=requested_at,
                outcome_score=0.5,
                candidates=[
                    _cand(f"c-{i}", salience=0.5, relevance=0.0,
                          event_time=requested_at - timedelta(hours=1)),
                ],
            )
        )
    result = fit(corpus, baseline=_BASELINE)
    assert result.recommend is False
    assert result.guardrails  # thin populations and/or no improvement


def test_fit_stays_inside_search_bounds():
    """The fitted vector never leaves the sane box around the baseline —
    a tune is a tune, not a scoring-regime redesign."""
    result = fit(_salience_trap_corpus(), baseline=_BASELINE)
    best = result.best
    assert (
        _BASELINE.salience * WEIGHT_BOUND_LOW
        <= best.salience
        <= _BASELINE.salience * WEIGHT_BOUND_HIGH
    )
    assert (
        _BASELINE.recency * WEIGHT_BOUND_LOW
        <= best.recency
        <= _BASELINE.recency * WEIGHT_BOUND_HIGH
    )
    assert HALF_LIFE_MIN_H <= best.half_life_hours <= HALF_LIFE_MAX_H
    # similarity/keyword are not tuned dimensions — untouched.
    assert best.similarity == _BASELINE.similarity
    assert best.keyword == _BASELINE.keyword


def test_fit_is_deterministic():
    """Same corpus in, same weights out — the tuner is replayable evidence,
    not a stochastic search."""
    corpus = _salience_trap_corpus()
    a = fit(corpus, baseline=_BASELINE)
    b = fit(corpus, baseline=_BASELINE)
    assert a.best == b.best
    assert a.best_holdout.separation == b.best_holdout.separation
