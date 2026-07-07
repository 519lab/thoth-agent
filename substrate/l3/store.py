"""L3 storage — async PG helpers over ``l3_patterns``."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional
from uuid import UUID

from substrate.l3.schema import Pattern, normalise_kind


@asynccontextmanager
async def _acquire(conn) -> AsyncIterator[Any]:
    if conn is not None:
        yield conn
        return
    import thoth_db

    async with thoth_db.connection() as fresh:
        yield fresh


def _row_to_pattern(r) -> Pattern:
    return Pattern(
        id=r["id"],
        kind=r["kind"],
        statement=r["statement"],
        cites=list(r["cites"] or []),
        salience_score=float(r["salience_score"]),
        confidence=float(r["confidence"]),
        created_at=r["created_at"],
        last_seen_at=r["last_seen_at"],
        metadata=r["metadata"] or {},
    )


async def upsert_pattern(
    statement: str,
    kind: str,
    *,
    cites: Optional[list[str]] = None,
    confidence: float = 0.5,
    conn=None,
) -> tuple[UUID, bool]:
    """Insert or merge a pattern on ``(kind, statement)``. Re-finding bumps
    salience (capped at 1.0) + last_seen and unions citations. Returns
    ``(pattern_id, created)``."""
    k = normalise_kind(kind)
    statement = (statement or "").strip()
    cites_list = [str(c) for c in (cites or [])]
    async with _acquire(conn) as c:
        row = await c.fetchrow(
            """
            INSERT INTO l3_patterns (kind, statement, cites, confidence)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (kind, statement) DO UPDATE SET
                last_seen_at = now(),
                salience_updated_at = now(),
                salience_score = LEAST(1.0, l3_patterns.salience_score + 0.1),
                confidence = GREATEST(l3_patterns.confidence, EXCLUDED.confidence),
                cites = (
                    SELECT jsonb_agg(DISTINCT e)
                      FROM jsonb_array_elements(l3_patterns.cites || EXCLUDED.cites) AS e
                )
            RETURNING id, (xmax = 0) AS created
            """,
            k,
            statement,
            cites_list,
            float(confidence),
        )
    return row["id"], bool(row["created"])


async def get_patterns_for_query(
    query: str,
    *,
    limit: int = 5,
    query_embedding: Optional[list[float]] = None,
    conn=None,
) -> list[Pattern]:
    """Patterns most relevant to ``query``.

    When ``query_embedding`` is provided, rank by cosine distance over the
    backfilled ``embedding`` column (``embedding <=> $vec``, mirroring
    :func:`find_near_duplicates`), restricted to embedded rows — the semantic
    path used by the recall L3 header when ``RECALL_L3L4_SEMANTIC`` is on.
    Otherwise fall back to the trigram-similarity + salience path."""
    if not (query or "").strip():
        return []
    async with _acquire(conn) as c:
        if query_embedding is not None:
            # Exact nearest-neighbour scan. The ``embedding`` column carries an
            # ivfflat (approximate) index, which with the default
            # ``ivfflat.probes = 1`` probes a single list and can MISS the true
            # nearest row on a small/sparse table — surfacing as an empty L3
            # header even though a matching pattern exists. ``l3_patterns`` is a
            # small curated table (abstractions, not raw slices), so an exact
            # scan is cheap and correct. ``SET LOCAL`` scopes the planner hint to
            # this transaction only. (innovation #3)
            async with c.transaction():
                await c.execute("SET LOCAL enable_indexscan = off")
                await c.execute("SET LOCAL enable_indexonlyscan = off")
                rows = await c.fetch(
                    """
                    SELECT * FROM l3_patterns
                     WHERE embedding IS NOT NULL
                     ORDER BY embedding <=> $1::vector
                     LIMIT $2
                    """,
                    query_embedding,
                    limit,
                )
        else:
            rows = await c.fetch(
                """
                SELECT * FROM l3_patterns
                 WHERE statement % $1
                 ORDER BY (similarity(statement, $1) + salience_score) DESC,
                          last_seen_at DESC
                 LIMIT $2
                """,
                query,
                limit,
            )
    return [_row_to_pattern(r) for r in rows]


async def list_patterns(
    *, kind: Optional[str] = None, limit: int = 20, conn=None
) -> list[Pattern]:
    async with _acquire(conn) as c:
        rows = await c.fetch(
            """
            SELECT * FROM l3_patterns
             WHERE ($1::text IS NULL OR kind = $1)
             ORDER BY salience_score DESC, last_seen_at DESC
             LIMIT $2
            """,
            kind,
            limit,
        )
    return [_row_to_pattern(r) for r in rows]


# ---------------------------------------------------------------------------
# Curation surface — used by the Curator's upper-layer tick phase to embed,
# semantically merge near-duplicates, decay, and release patterns. Mirrors
# the L0 (substrate_slices) embedding-backfill + decay/release lifecycle.
# ---------------------------------------------------------------------------


async def set_embedding(pattern_id: UUID, embedding: list[float], *, conn=None) -> bool:
    """Persist an embedding for a pattern (idempotent via ``embedding IS NULL``).
    Mirrors ``SliceRepo.set_embedding`` — Python list bound to ``$2::vector``."""
    async with _acquire(conn) as c:
        tag = await c.execute(
            "UPDATE l3_patterns SET embedding = $2::vector "
            "WHERE id = $1 AND embedding IS NULL",
            pattern_id,
            embedding,
        )
    return tag.endswith(" 1")


async def list_unembedded(*, limit: int, conn=None) -> list[dict]:
    """Patterns still needing an embedding (newest-first)."""
    async with _acquire(conn) as c:
        rows = await c.fetch(
            "SELECT id, statement FROM l3_patterns "
            "WHERE embedding IS NULL ORDER BY last_seen_at DESC LIMIT $1",
            limit,
        )
    return [{"id": r["id"], "statement": r["statement"]} for r in rows]


async def list_merge_seeds(*, limit: int, conn=None) -> list[dict]:
    """Embedded patterns to consider as merge seeds (highest-salience first)."""
    async with _acquire(conn) as c:
        rows = await c.fetch(
            "SELECT id, kind, statement, salience_score, confidence, cites, last_seen_at "
            "FROM l3_patterns WHERE embedding IS NOT NULL "
            "ORDER BY salience_score DESC, last_seen_at DESC LIMIT $1",
            limit,
        )
    return [dict(r) for r in rows]


async def find_near_duplicates(
    seed_id: UUID, *, max_distance: float, limit: int, same_kind: bool = True, conn=None
) -> list[dict]:
    """Patterns within cosine ``max_distance`` of ``seed_id`` (excluding the
    seed). With ``same_kind=True`` (default) restricts to the seed's kind; with
    ``same_kind=False`` matches across kinds — used at a tighter distance to
    fold the same fact stored under different kinds. Distance is computed
    server-side against the seed row, so no embedding round-trips."""
    kind_clause = "AND d.kind = s.kind" if same_kind else ""
    async with _acquire(conn) as c:
        rows = await c.fetch(
            f"""
            SELECT d.id, d.statement, d.salience_score, d.confidence,
                   d.cites, d.last_seen_at,
                   (d.embedding <=> s.embedding) AS distance
              FROM l3_patterns d
              JOIN l3_patterns s ON s.id = $1
             WHERE d.id <> s.id
               {kind_clause}
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
    canonical_id: UUID,
    *,
    cites: list[str],
    salience: float,
    confidence: float,
    conn=None,
) -> None:
    """Fold a merged cluster's union-cites / max-salience / max-confidence onto
    the canonical row and reset its decay clock. Victims are removed separately
    via :func:`delete_patterns`."""
    async with _acquire(conn) as c:
        await c.execute(
            """
            UPDATE l3_patterns
               SET cites = $2,
                   salience_score = LEAST(1.0, $3),
                   confidence = $4,
                   last_seen_at = now(),
                   salience_updated_at = now()
             WHERE id = $1
            """,
            canonical_id,
            [str(x) for x in cites],
            float(salience),
            float(confidence),
        )


async def delete_patterns(ids: list[UUID], *, conn=None) -> int:
    if not ids:
        return 0
    async with _acquire(conn) as c:
        tag = await c.execute("DELETE FROM l3_patterns WHERE id = ANY($1::uuid[])", ids)
    return int(tag.rsplit(" ", 1)[-1]) if tag else 0


async def decay(*, half_life_seconds: float, min_age_seconds: float = 1.0, conn=None) -> None:
    """Exponential salience decay anchored on ``salience_updated_at`` (mirrors
    the L0 Curator decay). Skips rows touched within ``min_age_seconds``."""
    async with _acquire(conn) as c:
        await c.execute(
            """
            UPDATE l3_patterns
               SET salience_score = salience_score
                     -- Cap exponent at 60 half-lives so a very stale row can't
                     -- underflow POWER(0.5, huge) and abort the decay UPDATE
                     -- (see substrate/agents/curator.py _apply_natural_decay).
                     * POWER(0.5, LEAST(EXTRACT(EPOCH FROM (now() - salience_updated_at))
                                  / GREATEST($1, 0.001), 60.0)),
                   salience_updated_at = now()
             WHERE now() - salience_updated_at > make_interval(secs => $2)
            """,
            float(half_life_seconds),
            float(min_age_seconds),
        )


async def release_stale(
    *, floor: float, stale_seconds: float, limit: int = 500, conn=None
) -> int:
    """Delete decayed, uncited, stale patterns: salience below ``floor``, not
    re-found within ``stale_seconds``, and citing no L1 entities. Reinforced or
    cited patterns are retained (reinforcement-based soft TTL)."""
    async with _acquire(conn) as c:
        tag = await c.execute(
            """
            DELETE FROM l3_patterns
             WHERE id IN (
                 SELECT id FROM l3_patterns
                  WHERE salience_score < $1
                    AND last_seen_at < now() - make_interval(secs => $2)
                    AND jsonb_array_length(COALESCE(cites, '[]'::jsonb)) = 0
                  ORDER BY salience_score ASC
                  LIMIT $3
             )
            """,
            float(floor),
            float(stale_seconds),
            limit,
        )
    return int(tag.rsplit(" ", 1)[-1]) if tag else 0


__all__ = [
    "upsert_pattern",
    "get_patterns_for_query",
    "list_patterns",
    "set_embedding",
    "list_unembedded",
    "list_merge_seeds",
    "find_near_duplicates",
    "apply_merge",
    "delete_patterns",
    "decay",
    "release_stale",
]
