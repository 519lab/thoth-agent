"""Partition-maintenance worker.

Keeps the rolling window of monthly partitions on ``substrate_slices``
ahead of ``now()`` so writes never land in the DEFAULT partition
during steady-state operation. Runs once at boot and then daily.

PG 17 propagates parent-table indexes to every present and future
partition — this worker only issues the ``CREATE TABLE IF NOT EXISTS
... PARTITION OF`` DDL, never re-creates indexes per partition.

This is the cheapest sub-agent in Phase A: a daily tick that issues
3 idempotent statements. The tick interval is fixed at 24h regardless
of the configured intensity level, because the underlying work is
calendar-bound rather than load-bound.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from substrate.agents.base import Level, SubAgent
from substrate.storage.partitions import (
    ensure_partitions,
    find_underindexed_partitions,
)

if TYPE_CHECKING:  # pragma: no cover
    from substrate.facade import Substrate


# Hard-coded cadence. The work is calendar-bound (a new month appears
# every 30-ish days), so intensity doesn't change how often we tick.
# Tests inject a shorter interval via ``set_interval()``.
_DEFAULT_INTERVAL_SECONDS = 86400.0  # 24h


class PartitionMaintenanceWorker(SubAgent):
    """Ensure ``current + ahead_months`` partitions exist; runs daily.

    Idempotent — running twice in the same hour is a no-op.
    """

    name = "partition-maintenance"
    is_sentinel = False

    def __init__(
        self,
        substrate: "Substrate",
        *,
        ahead_months: int = 2,
        interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(substrate)
        self._ahead_months = ahead_months
        self._interval_seconds = interval_seconds
        # FULL is fine here — the interval comes from
        # ``_interval_for`` below, not the level-table.
        self._level = Level.FULL

    async def tick(self) -> None:
        import thoth_db

        async with thoth_db.connection() as conn:
            created = await ensure_partitions(
                conn, ahead_months=self._ahead_months
            )
            self._log.debug(
                "partition.ensure.ok partitions=%s", ",".join(created)
            )

            # #284 health check: a partition provisioned while a parent index
            # was INVALID comes up under-indexed (inserts seq-scan, uniqueness
            # unenforced). PG won't self-heal it — surface it loudly so it's
            # caught before it matters, instead of only via a manual DB probe.
            underindexed = await find_underindexed_partitions(conn)

            # #286: age-based retention for the append-only operational-log
            # tables (substrate_telemetry + substrate_conductor_log) so they
            # stay bounded — they had no pruning and grew to ~30% of the live
            # DB. Runs on this daily calendar-bound cadence. Best-effort.
            try:
                from substrate.telemetry import prune as _prune_logs

                pruned = await _prune_logs(conn)
                if any(pruned.values()):
                    self._log.info("substrate_log.pruned %s", pruned)
            except Exception as exc:
                self._log.debug("substrate_log.prune failed: %s", exc)
        if underindexed:
            names = ", ".join(
                f"{p['partition']} ({p['index_count']}/{p['expected']})"
                for p in underindexed
            )
            self._log.warning(
                "partition.underindexed count=%d partitions=%s — recreate the "
                "parent indexes on substrate_slices so they re-propagate",
                len(underindexed),
                names,
            )
            try:
                from substrate.telemetry import write as telemetry_write

                await telemetry_write(
                    self._substrate,
                    agent="partition-maintenance",
                    event="partition.underindexed",
                    payload={"partitions": underindexed},
                )
            except Exception as exc:  # telemetry is best-effort, never fatal
                self._log.debug("partition.underindexed telemetry failed: %s", exc)

    # Override the base class interval so the daily cadence ignores
    # the Conductor's intensity dial. The worker is still
    # ``set_intensity(OFF)``-able (tested in the base class) which
    # halts ticks entirely; LOW/MODERATE/HIGH/FULL all map to the
    # same 24h.
    @staticmethod
    def _interval_for(level: Level) -> Optional[float]:  # type: ignore[override]
        if level is Level.OFF:
            return None
        return _DEFAULT_INTERVAL_SECONDS

    # Test seam — change cadence without monkey-patching the static
    # ``_interval_for``.
    def set_interval(self, interval_seconds: float) -> None:
        self._interval_seconds = interval_seconds


__all__ = ["PartitionMaintenanceWorker"]
