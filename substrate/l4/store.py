"""L4 storage — append-only observations + latest-coherence read."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional
from uuid import UUID

from substrate.l4.schema import Observation


@asynccontextmanager
async def _acquire(conn) -> AsyncIterator[Any]:
    if conn is not None:
        yield conn
        return
    import thoth_db

    async with thoth_db.connection() as fresh:
        yield fresh


def _row(r) -> Observation:
    return Observation(
        id=r["id"],
        kind=r["kind"],
        subject=r["subject"],
        statement=r["statement"],
        score=None if r["score"] is None else float(r["score"]),
        created_at=r["created_at"],
        metadata=r["metadata"] or {},
    )


async def record_observation(
    kind: str,
    subject: str,
    statement: str,
    *,
    score: Optional[float] = None,
    metadata: Optional[dict] = None,
    conn=None,
) -> UUID:
    async with _acquire(conn) as c:
        return await c.fetchval(
            """
            INSERT INTO l4_observations (kind, subject, statement, score, metadata)
            VALUES ($1, $2, $3, $4, $5) RETURNING id
            """,
            kind,
            subject,
            statement,
            None if score is None else float(score),
            metadata or {},
        )


async def list_observations(
    *, subject: Optional[str] = None, kind: Optional[str] = None,
    limit: int = 20, conn=None,
) -> list[Observation]:
    async with _acquire(conn) as c:
        rows = await c.fetch(
            """
            SELECT * FROM l4_observations
             WHERE ($1::text IS NULL OR subject = $1)
               AND ($2::text IS NULL OR kind = $2)
             ORDER BY created_at DESC LIMIT $3
            """,
            subject,
            kind,
            limit,
        )
    return [_row(r) for r in rows]


async def get_observations_for_query(
    query: str,
    *,
    limit: int = 3,
    query_embedding: Optional[list[float]] = None,
    conn=None,
) -> list[Observation]:
    """Self-model observations most relevant to ``query``, excluding the
    ``coherence`` vital sign (that's a singleton surfaced separately, not
    recall content).

    When ``query_embedding`` is provided, rank by cosine distance over the
    backfilled ``embedding`` column (``embedding <=> $vec``, mirroring
    :func:`find_near_duplicates`), restricted to embedded rows — the semantic
    path used by the recall L4 header when ``RECALL_L3L4_SEMANTIC`` is on.
    Otherwise fall back to the trigram-similarity + salience path.

    Mirrors :func:`substrate.l3.store.get_patterns_for_query`. ``l4_observations``
    has no trigram GIN index on ``statement`` (unlike ``l3_patterns``), but the
    table is tiny — a sequential scan with the ``%`` operator from the
    globally-enabled ``pg_trgm`` extension is well within budget. Used by the
    recall L4 header (substrate/recall/api.py)."""
    if not (query or "").strip():
        return []
    async with _acquire(conn) as c:
        if query_embedding is not None:
            # Exact nearest-neighbour scan — see the matching note in
            # substrate.l3.store.get_patterns_for_query. The ivfflat index on
            # ``embedding`` is approximate (default ``ivfflat.probes = 1``) and
            # can MISS the true nearest row on this small table, dropping the L4
            # header. ``SET LOCAL`` forces an exact scan for this transaction
            # only. (innovation #3)
            async with c.transaction():
                await c.execute("SET LOCAL enable_indexscan = off")
                await c.execute("SET LOCAL enable_indexonlyscan = off")
                rows = await c.fetch(
                    """
                    SELECT * FROM l4_observations
                     WHERE kind <> 'coherence'
                       AND embedding IS NOT NULL
                     ORDER BY embedding <=> $1::vector
                     LIMIT $2
                    """,
                    query_embedding,
                    limit,
                )
        else:
            rows = await c.fetch(
                """
                SELECT * FROM l4_observations
                 WHERE kind <> 'coherence'
                   AND statement % $1
                 ORDER BY (similarity(statement, $1) + salience_score) DESC,
                          last_seen_at DESC
                 LIMIT $2
                """,
                query,
                limit,
            )
    return [_row(r) for r in rows]


async def upsert_coherence(
    statement: str, *, score: float, metadata: Optional[dict] = None, conn=None
) -> None:
    """Maintain a SINGLE current coherence/substrate vital-sign row (update in
    place) rather than appending one per assessment. The coherence *trend*
    lives in ``substrate_telemetry`` (``critic.assessed``); L4 keeps just the
    latest, pinned at salience 1.0 so it's never released. Keeps
    :func:`latest_coherence` and the health rollup working without flooding L4."""
    async with _acquire(conn) as c:
        updated = await c.fetchval(
            """
            UPDATE l4_observations
               SET statement = $1, score = $2, metadata = $3,
                   last_seen_at = now(), salience_updated_at = now(),
                   salience_score = 1.0
             WHERE id = (
                 SELECT id FROM l4_observations
                  WHERE kind = 'coherence' AND subject = 'substrate'
                  ORDER BY created_at DESC LIMIT 1
             )
            RETURNING id
            """,
            statement,
            float(score),
            metadata or {},
        )
        if updated is None:
            await c.execute(
                """
                INSERT INTO l4_observations
                    (kind, subject, statement, score, metadata, salience_score)
                VALUES ('coherence', 'substrate', $1, $2, $3, 1.0)
                """,
                statement,
                float(score),
                metadata or {},
            )


# ---------------------------------------------------------------------------
# Curation surface — Curator upper-layer phase. Mirrors L3 (substrate.l3.store)
# but merges within the same ``subject`` and has no citations.
# ---------------------------------------------------------------------------


async def set_embedding(obs_id: UUID, embedding: list[float], *, conn=None) -> bool:
    async with _acquire(conn) as c:
        tag = await c.execute(
            "UPDATE l4_observations SET embedding = $2::vector "
            "WHERE id = $1 AND embedding IS NULL",
            obs_id,
            embedding,
        )
    return tag.endswith(" 1")


async def list_unembedded(*, limit: int, conn=None) -> list[dict]:
    async with _acquire(conn) as c:
        rows = await c.fetch(
            "SELECT id, statement FROM l4_observations "
            "WHERE embedding IS NULL ORDER BY last_seen_at DESC LIMIT $1",
            limit,
        )
    return [{"id": r["id"], "statement": r["statement"]} for r in rows]


async def list_merge_seeds(*, limit: int, conn=None) -> list[dict]:
    """Embedded observations as merge seeds. The coherence vital sign is
    excluded — it's a maintained singleton, not a dedup target."""
    async with _acquire(conn) as c:
        rows = await c.fetch(
            "SELECT id, subject, statement, score, salience_score, last_seen_at "
            "FROM l4_observations "
            "WHERE embedding IS NOT NULL AND kind <> 'coherence' "
            "ORDER BY salience_score DESC, last_seen_at DESC LIMIT $1",
            limit,
        )
    return [dict(r) for r in rows]


async def find_near_duplicates(
    seed_id: UUID, *, max_distance: float, limit: int, conn=None
) -> list[dict]:
    """Observations within cosine ``max_distance`` of ``seed_id`` and the SAME
    subject (excluding the seed and the coherence vital sign)."""
    async with _acquire(conn) as c:
        rows = await c.fetch(
            """
            SELECT d.id, d.statement, d.score, d.salience_score, d.last_seen_at,
                   (d.embedding <=> s.embedding) AS distance
              FROM l4_observations d
              JOIN l4_observations s ON s.id = $1
             WHERE d.subject = s.subject
               AND d.kind <> 'coherence'
               AND d.id <> s.id
               AND d.embedding IS NOT NULL
               AND (d.embedding <=> s.embedding) <= $2
             ORDER BY d.embedding <=> s.embedding
             LIMIT $3
            """,
            seed_id,
            max_distance,
            limit,
        )
    return [dict(r) for r in rows]


async def apply_merge(
    canonical_id: UUID, *, salience: float, score: Optional[float], conn=None
) -> None:
    async with _acquire(conn) as c:
        await c.execute(
            """
            UPDATE l4_observations
               SET salience_score = LEAST(1.0, $2),
                   score = COALESCE($3, score),
                   last_seen_at = now(),
                   salience_updated_at = now()
             WHERE id = $1
            """,
            canonical_id,
            float(salience),
            None if score is None else float(score),
        )


async def delete_observations(ids: list[UUID], *, conn=None) -> int:
    if not ids:
        return 0
    async with _acquire(conn) as c:
        tag = await c.execute(
            "DELETE FROM l4_observations WHERE id = ANY($1::uuid[])", ids
        )
    return int(tag.rsplit(" ", 1)[-1]) if tag else 0


async def decay(*, half_life_seconds: float, min_age_seconds: float = 1.0, conn=None) -> None:
    """Exponential salience decay. The coherence vital sign (pinned at 1.0 and
    refreshed each assessment) is left alone."""
    async with _acquire(conn) as c:
        await c.execute(
            """
            UPDATE l4_observations
               SET salience_score = salience_score
                     -- Cap exponent at 60 half-lives so a very stale row can't
                     -- underflow POWER(0.5, huge) and abort the decay UPDATE
                     -- (see substrate/agents/curator.py _apply_natural_decay).
                     * POWER(0.5, LEAST(EXTRACT(EPOCH FROM (now() - salience_updated_at))
                                  / GREATEST($1, 0.001), 60.0)),
                   salience_updated_at = now()
             WHERE kind <> 'coherence'
               AND now() - salience_updated_at > make_interval(secs => $2)
            """,
            float(half_life_seconds),
            float(min_age_seconds),
        )


async def release_stale(
    *, floor: float, stale_seconds: float, limit: int = 500, conn=None
) -> int:
    """Delete decayed, stale observations (salience below ``floor``, not
    refreshed within ``stale_seconds``). Never touches the coherence vital sign."""
    async with _acquire(conn) as c:
        tag = await c.execute(
            """
            DELETE FROM l4_observations
             WHERE id IN (
                 SELECT id FROM l4_observations
                  WHERE kind <> 'coherence'
                    AND salience_score < $1
                    AND last_seen_at < now() - make_interval(secs => $2)
                  ORDER BY salience_score ASC
                  LIMIT $3
             )
            """,
            float(floor),
            float(stale_seconds),
            limit,
        )
    return int(tag.rsplit(" ", 1)[-1]) if tag else 0


async def latest_coherence(*, conn=None) -> Optional[Observation]:
    """The most recent ``coherence`` observation — the substrate's current
    self-assessed identity-health vital sign, or None if the Critic hasn't
    run yet."""
    async with _acquire(conn) as c:
        r = await c.fetchrow(
            "SELECT * FROM l4_observations WHERE kind='coherence' "
            "ORDER BY created_at DESC LIMIT 1"
        )
    return _row(r) if r else None


__all__ = [
    "record_observation",
    "list_observations",
    "get_observations_for_query",
    "latest_coherence",
    "upsert_coherence",
    "set_embedding",
    "list_unembedded",
    "list_merge_seeds",
    "find_near_duplicates",
    "apply_merge",
    "delete_observations",
    "decay",
    "release_stale",
]
