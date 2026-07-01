"""Offline recall-weight tuner — closes the loop the replay harness opened.

``replay.py`` (innovation #1) made a deliberate stop: it re-ranks logged
recalls under candidate weight vectors and *reports* — it never applies.
This module is the next step: **fit** a weight vector against the labelled
corpus and say, with guardrails, whether it is safe to promote.

The pipeline is pure end-to-end (the caller loads the corpus via
``replay.load_labeled_recalls`` and persists results via ``weights_store``):

  - :func:`split_corpus` — time-ordered train/holdout split. The corpus
    arrives newest-first; the *newest* fraction becomes the holdout so
    validation always points the same direction as deployment (fit on the
    past, judge on the most recent behaviour).
  - :func:`fit` — deterministic coordinate descent around the live baseline,
    maximising the replay harness's kept-vs-dropped outcome-separation
    metric on the train split, then scoring train AND holdout for both the
    baseline and the winner.
  - :class:`TuneResult` — the full verdict, including ``guardrails``: the
    list of reasons the result should NOT be promoted (empty ⇒ safe to
    apply). The tuner itself never writes anything.

Honesty inherited from the harness: the objective is the coarse v1
separation proxy, not NDCG — so the guardrails demand a real corpus, both
outcome populations present, and a holdout improvement margin before
recommending anything. A young or one-sided install gets "don't apply,
here's why", not silently-fit noise.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field, replace
from typing import Optional

from substrate.recall.replay import (
    LabeledRecall,
    SweepEntry,
    Weights,
    _evaluate,
    baseline_weights,
)


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Guardrail defaults (env-tunable, conservative)
# ---------------------------------------------------------------------------

#: Fewer labelled recalls than this → refuse to recommend anything.
DEFAULT_MIN_CORPUS = 50
#: Each outcome population (kept / dropped under the fitted weights, holdout)
#: must have at least this many members for the separation to mean anything.
DEFAULT_MIN_POPULATION = 5
#: The fitted weights must beat the baseline's holdout separation by at
#: least this margin — a tie or a hair's width is noise, not signal.
DEFAULT_MIN_IMPROVEMENT = 0.01

#: Multiplicative search bounds around the baseline for each weight — the
#: tuner refuses to wander into a qualitatively different scoring regime
#: (that would be a redesign, not a tune).
WEIGHT_BOUND_LOW = 0.25
WEIGHT_BOUND_HIGH = 4.0
#: Absolute clamp for the recency half-life, hours (1 hour … 2 weeks).
HALF_LIFE_MIN_H = 1.0
HALF_LIFE_MAX_H = 336.0

#: The dimensions coordinate descent moves. similarity/keyword stay at
#: baseline: the logged ``relevance`` is a *fixed* similarity term, so those
#: two only rescale within their own path — salience, recency and the
#: half-life are where the logged corpus carries real re-ranking signal
#: (same reasoning as ``replay.default_grid``).
_TUNED_FIELDS = ("salience", "recency", "half_life_hours")
_STEP_MULTIPLIERS = (0.5, 0.8, 1.25, 2.0)
_MAX_ROUNDS = 8
#: Improvements smaller than this don't count as progress (float jitter).
_EPSILON = 1e-9


# ---------------------------------------------------------------------------
# Corpus split
# ---------------------------------------------------------------------------


def split_corpus(
    corpus: list[LabeledRecall],
    *,
    holdout_fraction: float = 0.3,
) -> tuple[list[LabeledRecall], list[LabeledRecall]]:
    """Time-ordered ``(train, holdout)`` split of a newest-first corpus.

    The newest ``holdout_fraction`` of recalls becomes the holdout; the
    older remainder is the train set. Sorted defensively so a caller who
    assembled the corpus by hand can't accidentally leak future rows into
    training.
    """
    ordered = sorted(corpus, key=lambda r: r.requested_at, reverse=True)
    n_holdout = math.ceil(len(ordered) * holdout_fraction)
    return ordered[n_holdout:], ordered[:n_holdout]


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TuneResult:
    """The tuner's full verdict. Pure data; nothing here touches the DB."""

    baseline: Weights
    best: Weights
    # Separation metrics for both vectors on both splits.
    baseline_train: SweepEntry
    baseline_holdout: SweepEntry
    best_train: SweepEntry
    best_holdout: SweepEntry
    corpus_size: int
    train_size: int
    holdout_size: int
    #: Reasons the result should NOT be promoted. Empty ⇒ safe to apply.
    guardrails: list[str] = field(default_factory=list)

    @property
    def recommend(self) -> bool:
        """True when every guardrail passed AND the fit actually moved."""
        return not self.guardrails and self.best != self.baseline

    @property
    def holdout_improvement(self) -> float:
        return (
            self.best_holdout.separation - self.baseline_holdout.separation
        )


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------


def _clamped(base: Weights, candidate: Weights) -> Weights:
    """Clamp ``candidate`` into the sane search box around ``base``."""

    def box(value: float, anchor: float) -> float:
        lo, hi = anchor * WEIGHT_BOUND_LOW, anchor * WEIGHT_BOUND_HIGH
        return min(max(value, lo), hi)

    return Weights(
        similarity=candidate.similarity,
        keyword=candidate.keyword,
        salience=box(candidate.salience, base.salience),
        recency=box(candidate.recency, base.recency),
        half_life_hours=min(
            max(candidate.half_life_hours, HALF_LIFE_MIN_H), HALF_LIFE_MAX_H
        ),
    )


def _coordinate_descent(
    train: list[LabeledRecall], base: Weights
) -> tuple[Weights, SweepEntry]:
    """Deterministic multiplicative coordinate descent from the baseline.

    One round tries every step multiplier on every tuned field, greedily
    keeping any move that improves train separation; stops after a round
    with no accepted move (or ``_MAX_ROUNDS``). Deterministic for a fixed
    corpus — same input, same weights out.
    """
    current = base
    current_entry = _evaluate(train, current)
    for _ in range(_MAX_ROUNDS):
        improved = False
        for fld in _TUNED_FIELDS:
            for mult in _STEP_MULTIPLIERS:
                candidate = _clamped(
                    base, replace(current, **{fld: getattr(current, fld) * mult})
                )
                if candidate == current:
                    continue
                entry = _evaluate(train, candidate)
                if entry.separation > current_entry.separation + _EPSILON:
                    current, current_entry = candidate, entry
                    improved = True
        if not improved:
            break
    return current, current_entry


def fit(
    corpus: list[LabeledRecall],
    *,
    baseline: Optional[Weights] = None,
    holdout_fraction: float = 0.3,
    min_corpus: Optional[int] = None,
    min_population: Optional[int] = None,
    min_improvement: Optional[float] = None,
) -> TuneResult:
    """Fit weights on the train split, judge on holdout, return the verdict.

    Never raises on a weak corpus — the weakness lands in ``guardrails`` so
    the CLI can print *why* nothing should be applied.
    """
    base = baseline or baseline_weights()
    min_corpus = (
        min_corpus
        if min_corpus is not None
        else _env_int("THOTH_RECALL_TUNE_MIN_CORPUS", DEFAULT_MIN_CORPUS)
    )
    min_population = (
        min_population
        if min_population is not None
        else _env_int("THOTH_RECALL_TUNE_MIN_POPULATION", DEFAULT_MIN_POPULATION)
    )
    min_improvement = (
        min_improvement
        if min_improvement is not None
        else _env_float(
            "THOTH_RECALL_TUNE_MIN_IMPROVEMENT", DEFAULT_MIN_IMPROVEMENT
        )
    )

    train, holdout = split_corpus(corpus, holdout_fraction=holdout_fraction)

    baseline_train = _evaluate(train, base)
    baseline_holdout = _evaluate(holdout, base)

    if train:
        best, best_train = _coordinate_descent(train, base)
    else:
        best, best_train = base, baseline_train
    best_holdout = _evaluate(holdout, best)

    guardrails: list[str] = []
    if len(corpus) < min_corpus:
        guardrails.append(
            f"corpus too small: {len(corpus)} labelled recall(s) < "
            f"min {min_corpus}"
        )
    if best_holdout.n_kept < min_population or (
        best_holdout.n_dropped < min_population
    ):
        guardrails.append(
            "holdout populations too thin under fitted weights: "
            f"kept={best_holdout.n_kept} dropped={best_holdout.n_dropped} "
            f"(need ≥ {min_population} each for the separation to mean "
            "anything)"
        )
    improvement = best_holdout.separation - baseline_holdout.separation
    if improvement < min_improvement:
        guardrails.append(
            f"holdout improvement {improvement:+.4f} below the "
            f"{min_improvement:+.4f} margin — indistinguishable from noise"
        )

    return TuneResult(
        baseline=base,
        best=best,
        baseline_train=baseline_train,
        baseline_holdout=baseline_holdout,
        best_train=best_train,
        best_holdout=best_holdout,
        corpus_size=len(corpus),
        train_size=len(train),
        holdout_size=len(holdout),
        guardrails=guardrails,
    )


__all__ = [
    "TuneResult",
    "fit",
    "split_corpus",
    "DEFAULT_MIN_CORPUS",
    "DEFAULT_MIN_POPULATION",
    "DEFAULT_MIN_IMPROVEMENT",
]
