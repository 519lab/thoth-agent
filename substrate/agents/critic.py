"""Phase F (core) Critic — writes L4 calibration + a coherence vital sign.

The Critic "reads everything, writes L4" (MVS §3.3). This first-cut is
**deterministic** — no LLM — and computes calibration the substrate can
measure about itself right now:

* **Parser reliability** — ok-rate over recent ``substrate_parser_log``.
* **Consolidation backlog** — passed-but-unconsolidated vs. consolidated
  slices (is the Parser keeping up?).
* **Pathological-forgetting pressure** — recent Curator alarm count.

…and folds them into a single **coherence** score in [0,1] — the design's
identity-health vital sign (MVS §3.7), written to L4 so its drift is
visible over time. Gated by ``THOTH_SUBSTRATE_CRITIC`` (default ON; set to 0 to disable).

Deferred (genuine research per the MVS spec's own deferrals, flagged for
review): the LLM Reflector's L3/L4 synthesis, the Dreamer, the *learned*
Conductor policy, and the L2-grounding coherence signal (needs entity
decay, not yet implemented). This Critic establishes the L4 surface those
later build on.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from substrate.agents.base import Level, SubAgent
from substrate.l4 import store as l4

if TYPE_CHECKING:  # pragma: no cover
    from substrate.facade import Substrate


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"} if raw else default


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


class Critic(SubAgent):
    """Deterministic calibration + coherence. Floor LOW."""

    name = "critic"
    is_sentinel = False

    def __init__(self, substrate: "Substrate") -> None:
        super().__init__(substrate)
        self._level = Level.LOW
        # Assess at most once per interval regardless of tick cadence.
        self._last_assess_mono: float = 0.0

    async def tick(self) -> None:
        if not _env_bool("THOTH_SUBSTRATE_CRITIC", default=True):
            return
        if self._level is Level.OFF:
            return

        import asyncio

        interval = _env_float("CRITIC_INTERVAL_S", 300.0)
        now_mono = asyncio.get_event_loop().time()
        if self._last_assess_mono and now_mono - self._last_assess_mono < interval:
            return

        signals = await self._compute_signals()
        await self._record(signals)
        self._last_assess_mono = now_mono
        await self._emit_self_state(signals)

    async def _compute_signals(self) -> dict:
        import thoth_db

        async with thoth_db.connection() as conn:
            pr = await conn.fetchrow(
                """
                SELECT COUNT(*) FILTER (WHERE outcome='ok')::int AS ok,
                       COUNT(*)::int AS total
                  FROM substrate_parser_log
                 WHERE t_call > now() - interval '24 hours'
                """
            )
            backlog = await conn.fetchrow(
                """
                SELECT COUNT(*) FILTER (
                          WHERE sl.consolidation_state='unconsolidated'
                            AND sl.sentinel_state='passed')::int AS pending,
                       COUNT(*) FILTER (WHERE sl.consolidation_state='consolidated')::int AS done
                  FROM substrate_slices sl
                  JOIN substrate_streams st ON st.stream_id = sl.stream_id
                 -- perceptual streams only; substrate.* is operational
                 -- telemetry (see substrate.storage.streams.is_perceptual).
                   AND st.name NOT LIKE 'substrate.%'
                """
            )
            # Curator alarms now live in substrate_telemetry (non-perceptual),
            # not as slices on substrate.self_state.
            alarms = await conn.fetchval(
                """
                SELECT COUNT(*)::int
                  FROM substrate_telemetry
                 WHERE event = 'curator.pathological_forgetting_alarm'
                   AND at > now() - interval '1 hour'
                """
            )
        reliability = (pr["ok"] / pr["total"]) if pr["total"] else None
        denom = backlog["pending"] + backlog["done"]
        backlog_ratio = (backlog["pending"] / denom) if denom else 0.0
        return {
            "parser_calls": pr["total"],
            "parser_reliability": reliability,
            "pending": backlog["pending"],
            "consolidated": backlog["done"],
            "backlog_ratio": backlog_ratio,
            "alarms_1h": int(alarms or 0),
        }

    @staticmethod
    def _coherence(signals: dict) -> float:
        score = 1.0
        score -= 0.4 * signals["backlog_ratio"]
        rel = signals["parser_reliability"]
        if rel is not None and rel < 0.8:
            score -= 0.3 * (0.8 - rel) / 0.8
        if signals["alarms_1h"] > 0:
            score -= 0.2
        return max(0.0, min(1.0, score))

    async def _record(self, signals: dict) -> None:
        # Parser + consolidation calibration are point-in-time operational
        # status — time-series telemetry, NOT durable self-model. They go to
        # ``substrate_telemetry`` via _emit_self_state. Writing them to L4 every
        # assessment is what flooded it ("Parser ok-rate 97%" x68, etc.).
        #
        # L4 keeps only the coherence vital sign, and as a SINGLE maintained
        # row (upsert) rather than one appended per assessment. The coherence
        # *trend* lives in telemetry; L4 holds the current value so
        # ``latest_coherence`` + the health rollup keep working.
        coherence = self._coherence(signals)
        await l4.upsert_coherence(
            f"coherence {coherence:.2f} "
            f"(backlog {signals['backlog_ratio']:.0%}, "
            f"alarms/1h {signals['alarms_1h']})",
            score=coherence,
            metadata=signals,
        )

    async def _emit_self_state(self, signals: dict) -> None:
        from substrate.telemetry import write as telemetry_write

        try:
            await telemetry_write(
                self._substrate,
                agent="critic",
                event="critic.assessed",
                payload={
                    "coherence": self._coherence(signals),
                    "backlog_ratio": signals["backlog_ratio"],
                    "parser_reliability": signals["parser_reliability"],
                    "parser_calls": signals["parser_calls"],
                    "pending": signals["pending"],
                    "consolidated": signals["consolidated"],
                    "alarms_1h": signals["alarms_1h"],
                },
            )
        except Exception:
            self._log.debug("critic.telemetry.emit_failed", exc_info=True)


__all__ = ["Critic"]
