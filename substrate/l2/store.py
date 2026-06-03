"""L2 storage — async PG helpers over ``substrate_associations`` + edits.

Edges are undirected and canonicalised to ``src_id < dst_id`` (one row per
pair per type). ``bump_edge`` is the single mutator: it upserts the weight
and appends an edit row in one statement-pair so the history can never
diverge from the current weight.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional
from uuid import UUID

from substrate.l2.schema import Association, AssociationEdit


@asynccontextmanager
async def _acquire(conn) -> AsyncIterator[Any]:
    if conn is not None:
        yield conn
        return
    import thoth_db

    async with thoth_db.connection() as fresh:
        yield fresh


def _canonical(a: UUID, b: UUID) -> tuple[UUID, UUID]:
    """Order a pair so the smaller UUID is ``src`` (undirected edge)."""
    return (a, b) if str(a) < str(b) else (b, a)


def _row_to_assoc(r) -> Association:
    return Association(
        assoc_id=r["assoc_id"],
        src_id=r["src_id"],
        dst_id=r["dst_id"],
        edge_type=r["edge_type"],
        weight=float(r["weight"]),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        metadata=r["metadata"] or {},
    )


async def bump_edge(
    a: UUID, b: UUID, edge_type: str, *, delta: float, reason: str, conn=None
) -> Optional[UUID]:
    """Create or strengthen the ``edge_type`` edge between *a* and *b* by
    *delta*, appending an edit row. A self-edge (a == b) is a no-op
    (returns None) — an entity never associates with itself."""
    if a == b:
        return None
    src, dst = _canonical(a, b)
    async with _acquire(conn) as c:
        row = await c.fetchrow(
            """
            INSERT INTO substrate_associations (src_id, dst_id, edge_type, weight)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (src_id, dst_id, edge_type) DO UPDATE SET
                weight = substrate_associations.weight + EXCLUDED.weight,
                updated_at = now()
            RETURNING assoc_id, weight,
                      (xmax = 0) AS created,
                      weight - $4 AS old_weight
            """,
            src,
            dst,
            edge_type,
            float(delta),
        )
        assoc_id = row["assoc_id"]
        old_weight = None if row["created"] else float(row["old_weight"])
        await c.execute(
            """
            INSERT INTO substrate_association_edits
                (assoc_id, old_weight, new_weight, reason)
            VALUES ($1, $2, $3, $4)
            """,
            assoc_id,
            old_weight,
            float(row["weight"]),
            reason,
        )
    return assoc_id


async def get_associations_for_entity(
    entity_id: UUID, *, limit: int = 20, conn=None
) -> list[Association]:
    async with _acquire(conn) as c:
        rows = await c.fetch(
            """
            SELECT * FROM substrate_associations
             WHERE src_id = $1 OR dst_id = $1
             ORDER BY weight DESC, updated_at DESC
             LIMIT $2
            """,
            entity_id,
            limit,
        )
    return [_row_to_assoc(r) for r in rows]


async def densest_edges(*, limit: int = 20, conn=None) -> list[Association]:
    async with _acquire(conn) as c:
        rows = await c.fetch(
            "SELECT * FROM substrate_associations ORDER BY weight DESC, updated_at DESC LIMIT $1",
            limit,
        )
    return [_row_to_assoc(r) for r in rows]


async def get_edits(assoc_id: UUID, *, limit: int = 20, conn=None) -> list[AssociationEdit]:
    async with _acquire(conn) as c:
        rows = await c.fetch(
            "SELECT * FROM substrate_association_edits WHERE assoc_id = $1 "
            "ORDER BY at DESC LIMIT $2",
            assoc_id,
            limit,
        )
    return [
        AssociationEdit(
            edit_id=r["edit_id"],
            assoc_id=r["assoc_id"],
            at=r["at"],
            old_weight=None if r["old_weight"] is None else float(r["old_weight"]),
            new_weight=float(r["new_weight"]),
            reason=r["reason"],
        )
        for r in rows
    ]


__all__ = [
    "bump_edge",
    "get_associations_for_entity",
    "densest_edges",
    "get_edits",
]
