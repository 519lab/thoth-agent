"""Pending-TTL force-reject worker.

Periodically removes slices that have been pending longer than their
stream's :attr:`DecayProfile.pending_ttl`. Independent of the Sentinel
decider — even if Sentinel hangs or falls behind, the pending queue
is bounded by the TTL. Hostile streams trying to inflate the queue are
defeated by the worker, not the Sentinel.

The TTL check is done **server-side** via native PG interval math
(``pending_committed_at + dp.pending_ttl < now()``) — no Python-side
date parsing, no string-encoded durations. The CTE-based DELETE keeps
the partition-key columns in both predicates so PG prunes to a single
partition per matched row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from substrate.agents.base import Level, SubAgent

if TYPE_CHECKING:  # pragma: no cover
    from substrate.facade import Substrate


class ForceRejectWorker(SubAgent):
    """Drop pending slices past their stream's pending_ttl.

    Default intensity = LOW (10s tick) — the queue is checked
    frequently enough to bound it, but we're not burning CPU on what
    is hopefully a rarely-fired path.

    Audit: one row in ``substrate_telemetry`` per dropped slice (operational
    telemetry, not a perceptual slice). Each row lists the dropped slice's
    id, stream, and the reason code ``force_reject_ttl`` so a human
    investigator can correlate with the producing path.
    """

    name = "force-reject"
    is_sentinel = False  # but its floor is LOW, not OFF — see __init__

    def __init__(self, substrate: "Substrate") -> None:
        super().__init__(substrate)
        # Even at LOW the worker runs every 10 seconds — that's the
        # right cadence given decay-profile pending_ttls are 5–120
        # seconds. Phase B+ Conductor may dial this up under attack.
        self._level = Level.LOW
        # Per-tick deletion cap. We don't want a single tick scanning
        # the entire pending queue when the substrate is healthy and
        # the queue is small; nor do we want it locking up the table
        # when something has gone catastrophically wrong.
        self._batch_limit = 100

    async def tick(self) -> None:
        import thoth_db

        # Single transaction for the CTE + DELETE + RETURNING. The
        # audit emissions run AFTER commit so a slow audit doesn't
        # hold the row locks open.
        async with thoth_db.transaction() as conn:
            expired = await self._substrate.slices.force_reject_expired(
                conn, limit=self._batch_limit
            )

        if not expired:
            return

        for s in expired:
            await self._emit_audit(s)

    async def _emit_audit(self, slice_obj) -> None:
        """Emit one telemetry row per force-rejection. ``slice_obj`` is
        already-deleted; the row references it by id only."""
        from substrate.telemetry import write as telemetry_write

        # The deleted slice's metadata contains caller context the
        # auditor likely wants — copy it through so an investigator
        # can correlate via original_metadata.session_id etc.
        await telemetry_write(
            self._substrate,
            agent="force-reject",
            event="force_reject_ttl",
            payload={
                "slice_id": str(slice_obj.slice_id),
                "stream_id": str(slice_obj.stream_id),
                "payload_modality": slice_obj.payload_modality.value,
                "pending_since": slice_obj.pending_committed_at.isoformat()
                if slice_obj.pending_committed_at
                else None,
                "original_metadata": slice_obj.metadata,
            },
        )


__all__ = ["ForceRejectWorker"]
