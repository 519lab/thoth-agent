"""Phase E1 Associator — weaves the L2 associative graph over L1 entities.

Each tick (intensity-dialled, LOW floor), the Associator looks at entities
touched since its last tick and strengthens two kinds of *discovered*
association (distinct from the explicit L1 relationships the Parser
extracts):

* ``co_occurrence`` — entities cited by the same L0 slice.
* ``shared_neighbor`` — entities that share a relationship partner.

Every weight change appends a ``substrate_association_edits`` row, so the
graph carries its own history (MVS §3.2) — the raw material the Critic
later audits against the salience landscape (MVS §3.7/§5.6, Phase F).

Gated by ``THOTH_SUBSTRATE_ASSOCIATOR`` (default ON; set to 0 to disable): registers +
heartbeats, but its tick is a no-op until an operator opts in — the same
staged-rollout pattern as the Parser. Per the Phase E1 spec §3.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from substrate.agents.base import Level, SubAgent
from substrate.l2 import store

if TYPE_CHECKING:  # pragma: no cover
    from substrate.facade import Substrate


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"} if raw else default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


# Entities first seen at the dawn of time → process everything on the first
# tick (bounded by the batch limit).
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class Associator(SubAgent):
    """L1 → L2 association weaving. Floor intensity LOW (deep-cycle work)."""

    name = "associator"
    is_sentinel = False

    def __init__(self, substrate: "Substrate") -> None:
        super().__init__(substrate)
        self._level = Level.LOW
        # Entities touched after this watermark are reconsidered next tick.
        self._last_tick: datetime = _EPOCH

    async def tick(self) -> None:
        if not _env_bool("THOTH_SUBSTRATE_ASSOCIATOR", default=True):
            return
        if self._level is Level.OFF:
            return

        tick_start = datetime.now(timezone.utc)
        touched = await self._touched_entities(self._last_tick)
        if not touched:
            self._last_tick = tick_start
            return

        edges = 0
        for entity_id in touched:
            edges += await self._link_co_occurrence(entity_id)
            edges += await self._link_shared_neighbor(entity_id)

        # Advance the watermark only after a full pass so a mid-tick crash
        # re-processes (idempotent: bump is additive but the same shared
        # slices won't be double-counted within a single pass).
        self._last_tick = tick_start
        if edges:
            await self._emit_self_state(len(touched), edges)

    # ------------------------------------------------------------------

    async def _touched_entities(self, since: datetime) -> list:
        import thoth_db

        limit = _env_int("ASSOCIATOR_BATCH_SIZE", 32)
        async with thoth_db.connection() as conn:
            rows = await conn.fetch(
                "SELECT id FROM l1_entities WHERE last_seen_at > $1 "
                "ORDER BY last_seen_at DESC LIMIT $2",
                since,
                limit,
            )
        return [r["id"] for r in rows]

    async def _link_co_occurrence(self, entity_id) -> int:
        """Strengthen co_occurrence edges to entities sharing a cited slice."""
        import thoth_db

        async with thoth_db.connection() as conn:
            rows = await conn.fetch(
                """
                SELECT c2.entity_id AS other, COUNT(DISTINCT c1.slice_id) AS shared
                  FROM l1_citations c1
                  JOIN l1_citations c2
                    ON c2.slice_id = c1.slice_id
                   AND c2.entity_id IS NOT NULL
                   AND c2.entity_id <> c1.entity_id
                 WHERE c1.entity_id = $1
                 GROUP BY c2.entity_id
                """,
                entity_id,
            )
        n = 0
        for r in rows:
            await store.bump_edge(
                entity_id, r["other"], "co_occurrence",
                delta=float(r["shared"]), reason="co_occurrence_bump",
            )
            n += 1
        return n

    async def _link_shared_neighbor(self, entity_id) -> int:
        """Strengthen shared_neighbor edges to entities sharing a partner."""
        import thoth_db

        async with thoth_db.connection() as conn:
            rows = await conn.fetch(
                """
                WITH my_partners AS (
                    SELECT object_id AS p FROM l1_relationships WHERE subject_id = $1
                    UNION
                    SELECT subject_id AS p FROM l1_relationships WHERE object_id = $1
                ),
                others AS (
                    SELECT subject_id AS other, object_id AS p FROM l1_relationships
                    UNION ALL
                    SELECT object_id AS other, subject_id AS p FROM l1_relationships
                )
                SELECT o.other, COUNT(DISTINCT o.p) AS shared
                  FROM others o
                  JOIN my_partners mp ON mp.p = o.p
                 WHERE o.other <> $1
                 GROUP BY o.other
                """,
                entity_id,
            )
        n = 0
        for r in rows:
            await store.bump_edge(
                entity_id, r["other"], "shared_neighbor",
                delta=float(r["shared"]), reason="shared_neighbor_bump",
            )
            n += 1
        return n

    async def _emit_self_state(self, n_entities: int, n_edges: int) -> None:
        from substrate.telemetry import write as telemetry_write

        try:
            await telemetry_write(
                self._substrate,
                agent="associator",
                event="associator.linked",
                payload={
                    "entities_processed": n_entities,
                    "edges_touched": n_edges,
                },
            )
        except Exception:
            self._log.debug("associator.telemetry.emit_failed", exc_info=True)


__all__ = ["Associator"]
