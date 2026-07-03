"""Recall-tuner watch — report-only auto-tune visibility (issue #288).

The recall-weights tuner (PR #283) is manual-by-design: promotion happens
only via ``thoth substrate recall tune --apply`` after guardrails pass.
That design left a gap: nothing ever *ran* the report, so on the live
install the labeled corpus grew past the nominal minimum while
``substrate_recall_weights`` stayed empty and nobody knew whether the
corpus was ripe.

This agent closes the visibility gap without touching the promotion
model: once a day it loads the labeled corpus, runs the pure
:func:`substrate.recall.tuner.fit` (one SELECT + CPU-only coordinate
descent — no LLM, no writes to any recall table), and records the verdict
as one ``recall_tuner.report`` row in ``substrate_telemetry``. When the
guardrails go green it logs loudly that the corpus is ready to promote —
the operator still runs ``--apply`` themselves.

Gated by ``THOTH_SUBSTRATE_TUNER_WATCH`` (default ON — the tick is a
cheap daily SELECT). Never auto-applies weights.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

from substrate.agents.base import Level, SubAgent

if TYPE_CHECKING:  # pragma: no cover
    from substrate.facade import Substrate


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# Daily — the corpus grows by at most a few dozen labels a day, so more
# frequent fits would just re-report the same verdict.
_DEFAULT_INTERVAL_SECONDS = 86400.0


class RecallTunerWatch(SubAgent):
    """Daily report-only recall-weights fit; verdict goes to telemetry."""

    name = "recall-tuner-watch"
    is_sentinel = False

    def __init__(
        self,
        substrate: "Substrate",
        *,
        interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(substrate)
        self._interval_seconds = interval_seconds
        self._level = Level.LOW

    async def tick(self) -> None:
        if not _env_bool("THOTH_SUBSTRATE_TUNER_WATCH", default=True):
            return
        if self._level is Level.OFF:
            return

        import thoth_db

        from substrate.recall import tuner
        from substrate.recall.replay import load_labeled_recalls
        from substrate.telemetry import write as telemetry_write

        async with thoth_db.connection() as conn:
            corpus = await load_labeled_recalls(conn)

        result = tuner.fit(corpus)

        payload = {
            "corpus_size": result.corpus_size,
            "train_size": result.train_size,
            "holdout_size": result.holdout_size,
            "guardrails": result.guardrails,
            "recommend": result.recommend,
            "baseline": result.baseline.label(),
            "best": result.best.label(),
            "baseline_holdout_separation": round(
                result.baseline_holdout.separation, 4
            ),
            "best_holdout_separation": round(
                result.best_holdout.separation, 4
            ),
        }
        await telemetry_write(
            self._substrate,
            agent=self.name,
            event="recall_tuner.report",
            payload=payload,
        )

        if result.recommend:
            self._log.warning(
                "recall_tuner.ready_to_promote corpus=%d holdout_sep=%+.4f->"
                "%+.4f — run `thoth substrate recall tune --apply` to promote",
                result.corpus_size,
                result.baseline_holdout.separation,
                result.best_holdout.separation,
            )
        else:
            self._log.info(
                "recall_tuner.report corpus=%d guardrails=%d",
                result.corpus_size,
                len(result.guardrails),
            )

    # Instance method (not the base's static seam) so the daily cadence
    # ignores the Conductor's intensity dial but stays test-adjustable
    # via ``set_interval``. OFF still halts ticks entirely.
    def _interval_for(self, level: Level) -> Optional[float]:  # type: ignore[override]
        if level is Level.OFF:
            return None
        return self._interval_seconds

    def set_interval(self, seconds: float) -> None:
        """Test seam — change cadence without monkey-patching
        ``_interval_for``."""
        self._interval_seconds = seconds
