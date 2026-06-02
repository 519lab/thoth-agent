"""Phase F — adaptive Conductor policy (the executive's first real loop).

The Conductor is the substrate's executive function (MVS §3.6): it reads
operational state and dials sub-agent intensities. Phase A/B shipped the
``StubConductor`` — an intensity registry that holds + pushes levels but
has no policy. This adds a **deterministic adaptive policy** that ticks,
reads observable load, and drives the StubConductor:

* **Consolidation backlog high** → raise the Parser (catch up), and pause
  the enrichment agents (Associator / Pattern-finder → OFF) so scarce
  cycles go to parsing first.
* **Backlog low** → everyone back to baseline LOW.

It mutates intensities only through ``substrate.conductor.set_intensity``
(which pushes to running agents + enforces each agent's floor — Sentinel
stays FULL, Curator stays ≥ LOW). Gated by ``HERMES_SUBSTRATE_CONDUCTOR``
(default ON; set to 0 to disable → the static Phase A/B behaviour is preserved exactly).

This is the *deterministic* Conductor. The **learned** Conductor
(opportunity forecasting, intensity-policy learning, worklist scheduling,
wake anticipation — MVS §3.6) remains deferred research, flagged for review.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

from substrate.agents.base import Level, SubAgent

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


class AdaptiveConductor(SubAgent):
    """Deterministic intensity policy. Floor LOW; gated OFF by default."""

    name = "conductor"
    is_sentinel = False

    def __init__(self, substrate: "Substrate") -> None:
        super().__init__(substrate)
        self._level = Level.LOW
        # Forecast: an EMA of backlog the Conductor learns over time. None
        # until seeded (from the persistent log on first tick, so the learned
        # rhythm survives restarts — MVS §3.6).
        self._ema: Optional[float] = None
        self._seeded = False
        # Hysteresis latch for coherence-driven corrective re-prioritization:
        # trips True below the floor, clears only once coherence recovers to
        # the (higher) recovery threshold — avoids flapping in the band between.
        self._coherence_low = False

    def forecast(self) -> Optional[float]:
        """The Conductor's current backlog forecast (EMA), or None if it
        hasn't observed anything yet. Exposed so the foreground can query
        the mind's self-knowledge of its rhythm (MVS §3.6)."""
        return self._ema

    async def tick(self) -> None:
        if not _env_bool("HERMES_SUBSTRATE_CONDUCTOR", default=True):
            return
        if self._level is Level.OFF:
            return

        if not self._seeded:
            await self._seed_forecast()
            self._seeded = True

        signals = await self._read_load()
        backlog = signals["backlog_ratio"]

        # Update the forecast (EMA) and derive a trend bias: backlog rising
        # above the forecast ⇒ escalate sooner; falling below ⇒ relax sooner.
        alpha = _env_float("CONDUCTOR_EMA_ALPHA", 0.3)
        prev = self._ema if self._ema is not None else backlog
        self._ema = alpha * backlog + (1 - alpha) * prev
        trend_bias = max(-0.2, min(0.2, backlog - prev))
        signals["trend_bias"] = trend_bias
        signals["forecast"] = self._ema

        # Coherence hysteresis: trip the corrective latch below the floor,
        # clear it only once coherence recovers to the (higher) recovery
        # threshold. In the band between the two we hold the prior latch
        # value (no flapping). A None reading leaves the latch untouched.
        coherence = signals.get("coherence")
        if coherence is not None:
            floor = _env_float("THOTH_CONDUCTOR_COHERENCE_FLOOR", 0.5)
            recovery = _env_float("THOTH_CONDUCTOR_COHERENCE_RECOVERY", 0.6)
            if coherence < floor:
                self._coherence_low = True
            elif coherence >= recovery:
                self._coherence_low = False
        signals["coherence_low"] = self._coherence_low

        targets = self._compute_targets(signals)
        conductor = getattr(self._substrate, "conductor", None)
        if conductor is None:
            return
        for agent_name, level in targets.items():
            conductor.set_intensity(agent_name, level)
        await self._log_decision(signals, targets)
        await self._emit_self_state(signals, targets)

    async def _seed_forecast(self) -> None:
        """Reconstruct the EMA from the persistent decision log so a restart
        resumes the learned rhythm instead of cold-starting."""
        import hermes_db

        try:
            async with hermes_db.connection() as conn:
                val = await conn.fetchval(
                    "SELECT forecast FROM substrate_conductor_log ORDER BY at DESC LIMIT 1"
                )
            if val is not None:
                self._ema = float(val)
        except Exception:
            self._log.debug("conductor.seed_forecast.failed", exc_info=True)

    async def _log_decision(self, signals: dict, targets: dict) -> None:
        import hermes_db

        try:
            async with hermes_db.connection() as conn:
                await conn.execute(
                    "INSERT INTO substrate_conductor_log "
                    "(backlog_ratio, forecast, targets) VALUES ($1, $2, $3)",
                    signals["backlog_ratio"],
                    signals.get("forecast", signals["backlog_ratio"]),
                    {k: v.value for k, v in targets.items()},
                )
        except Exception:
            self._log.debug("conductor.log_decision.failed", exc_info=True)

    async def _read_load(self) -> dict:
        import hermes_db

        async with hermes_db.connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) FILTER (
                          WHERE sl.consolidation_state='unconsolidated'
                            AND sl.sentinel_state='passed')::int AS pending,
                       COUNT(*) FILTER (WHERE sl.consolidation_state='consolidated')::int AS done
                  FROM substrate_slices sl
                  JOIN substrate_streams st ON st.stream_id = sl.stream_id
                 -- Only perceptual streams count as consolidation backlog.
                 -- substrate.* is the substrate's own operational telemetry
                 -- (now in substrate_telemetry); counting it here is what
                 -- pinned the Parser HIGH against an undrainable 414k-slice
                 -- backlog. See substrate.storage.streams.is_perceptual.
                   AND st.name NOT LIKE 'substrate.%'
                """
            )
        denom = row["pending"] + row["done"]
        signals = {
            "pending": row["pending"],
            "consolidated": row["done"],
            "backlog_ratio": (row["pending"] / denom) if denom else 0.0,
        }

        # Coherence vital sign (the Critic's self-assessed identity health).
        # Drives corrective re-prioritization when it dips below the floor.
        # Best-effort: a missing/erroring read leaves coherence None and the
        # backlog policy runs unchanged.
        coherence: Optional[float] = None
        try:
            from substrate.l4 import store as l4

            obs = await l4.latest_coherence()
            coherence = obs.score if obs else None
        except Exception:
            self._log.debug("conductor.read_coherence.failed", exc_info=True)
            coherence = None
        signals["coherence"] = coherence
        return signals

    @staticmethod
    def _compute_targets(signals: dict) -> dict[str, Level]:
        """Policy → target intensity per agent. Pure (testable).

        Thresholds on an *effective* backlog = observed backlog + a trend
        bias (rising backlog escalates sooner, falling relaxes sooner). With
        no ``trend_bias`` in signals the bias is 0 and this is the plain
        deterministic policy.

        Coherence override: when the coherence latch is tripped
        (``coherence_low``), corrective re-prioritization takes precedence
        over backlog — drive the Parser HIGH and the Critic to MODERATE
        (re-ground + re-assess identity) and pause enrichment + dreaming so
        scarce cycles go to recovering coherence first."""
        if signals.get("coherence_low") is True:
            return {
                "parser": Level.HIGH,
                "critic": Level.MODERATE,
                "associator": Level.OFF,
                "pattern-finder": Level.OFF,
                "dreamer": Level.OFF,
                "curator": Level.LOW,
            }
        high = _env_float("CONDUCTOR_BACKLOG_HIGH", 0.5)
        low = _env_float("CONDUCTOR_BACKLOG_LOW", 0.1)
        backlog = signals["backlog_ratio"] + signals.get("trend_bias", 0.0)

        if backlog >= high:
            # Catch up: parser hard, enrichment paused.
            return {
                "parser": Level.HIGH,
                "associator": Level.OFF,
                "pattern-finder": Level.OFF,
                "curator": Level.LOW,
            }
        if backlog >= low:
            return {
                "parser": Level.MODERATE,
                "associator": Level.LOW,
                "pattern-finder": Level.LOW,
                "curator": Level.LOW,
            }
        # Quiet: baseline.
        return {
            "parser": Level.LOW,
            "associator": Level.LOW,
            "pattern-finder": Level.LOW,
            "curator": Level.LOW,
        }

    async def _emit_self_state(self, signals: dict, targets: dict) -> None:
        from substrate.telemetry import write as telemetry_write

        try:
            await telemetry_write(
                self._substrate,
                agent="conductor",
                event="conductor.dialed",
                payload={
                    "backlog_ratio": signals["backlog_ratio"],
                    "coherence": signals.get("coherence"),
                    "coherence_low": signals.get("coherence_low", False),
                    "targets": {k: v.value for k, v in targets.items()},
                },
            )
        except Exception:
            self._log.debug("conductor.telemetry.emit_failed", exc_info=True)


__all__ = ["AdaptiveConductor"]
