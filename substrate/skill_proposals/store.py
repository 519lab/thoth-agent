"""Skill-proposal storage — async PG helpers over ``substrate_skill_proposals``.

Mirrors the L3/L4 store conventions (``substrate.l3.store``): an ``_acquire``
context manager so callers may pass their own connection or get a fresh one,
and row→dataclass mappers. Backs the SkillScout (writes pending proposals) and
the ``skill_proposal`` tool (lists / shows / approves / rejects).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional
from uuid import UUID

from substrate.skill_proposals.schema import SkillProposal


@asynccontextmanager
async def _acquire(conn) -> AsyncIterator[Any]:
    if conn is not None:
        yield conn
        return
    import thoth_db

    async with thoth_db.connection() as fresh:
        yield fresh


def _row(r) -> SkillProposal:
    return SkillProposal(
        id=r["id"],
        slug=r["slug"],
        title=r["title"],
        draft_content=r["draft_content"],
        rationale=r["rationale"] or "",
        status=r["status"],
        source_l3_ids=[str(x) for x in (r["source_l3_ids"] or [])],
        source_l4_ids=[str(x) for x in (r["source_l4_ids"] or [])],
        salience=float(r["salience"]),
        created_at=r["created_at"],
        decided_at=r["decided_at"],
        decided_by=r["decided_by"],
        eval_verdict=r["eval_verdict"],
        eval_reasons=[str(x) for x in (r["eval_reasons"] or [])],
        eval_model=r["eval_model"],
        evaluated_at=r["evaluated_at"],
    )


async def insert_proposal(
    *,
    slug: str,
    title: str,
    draft_content: str,
    rationale: str = "",
    source_l3_ids: Optional[list[str]] = None,
    source_l4_ids: Optional[list[str]] = None,
    salience: float = 0.0,
    eval_verdict: Optional[str] = None,
    eval_reasons: Optional[list[str]] = None,
    eval_model: Optional[str] = None,
    conn=None,
) -> Optional[UUID]:
    """Stage a new pending proposal. ``slug`` is unique — if one already exists
    (pending or decided) this is a no-op returning ``None``, so the SkillScout
    never re-proposes a need it already raised. Returns the new id on insert.

    The Phase-2 evaluator runs before insert, so its verdict is written at
    creation time; ``evaluated_at`` is stamped when a verdict is supplied."""
    async with _acquire(conn) as c:
        row = await c.fetchrow(
            """
            INSERT INTO substrate_skill_proposals
                (slug, title, draft_content, rationale,
                 source_l3_ids, source_l4_ids, salience,
                 eval_verdict, eval_reasons, eval_model,
                 evaluated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    CASE WHEN $8::text IS NULL THEN NULL ELSE now() END)
            ON CONFLICT (slug) DO NOTHING
            RETURNING id
            """,
            slug,
            title,
            draft_content,
            rationale or "",
            [str(x) for x in (source_l3_ids or [])],
            [str(x) for x in (source_l4_ids or [])],
            float(salience),
            eval_verdict,
            [str(x) for x in (eval_reasons or [])],
            eval_model,
        )
    return row["id"] if row else None


async def list_proposals(
    *, status: Optional[str] = None, limit: int = 50, conn=None
) -> list[SkillProposal]:
    async with _acquire(conn) as c:
        rows = await c.fetch(
            """
            SELECT * FROM substrate_skill_proposals
             WHERE ($1::text IS NULL OR status = $1)
             ORDER BY (status = 'pending') DESC, created_at DESC
             LIMIT $2
            """,
            status,
            limit,
        )
    return [_row(r) for r in rows]


async def get_proposal(slug: str, *, conn=None) -> Optional[SkillProposal]:
    async with _acquire(conn) as c:
        r = await c.fetchrow(
            "SELECT * FROM substrate_skill_proposals WHERE slug = $1", slug
        )
    return _row(r) if r else None


async def set_status(
    slug: str, status: str, *, by: Optional[str] = None, conn=None
) -> bool:
    """Transition a proposal's status (e.g. pending→approved/rejected) and stamp
    the decision. Returns True if a row was updated."""
    async with _acquire(conn) as c:
        tag = await c.execute(
            """
            UPDATE substrate_skill_proposals
               SET status = $2, decided_at = now(), decided_by = $3
             WHERE slug = $1
            """,
            slug,
            status,
            by,
        )
    return tag.endswith(" 1")


async def has_similar(slug: str, *, conn=None) -> bool:
    """Whether a proposal already exists for this slug in ANY status — the
    SkillScout's dedup gate (don't re-raise a need already proposed/decided)."""
    async with _acquire(conn) as c:
        return bool(
            await c.fetchval(
                "SELECT 1 FROM substrate_skill_proposals WHERE slug = $1", slug
            )
        )


async def count_pending(*, conn=None) -> int:
    """Number of proposals awaiting a decision — the SkillScout's max-pending
    cap reads this so it doesn't flood the user with drafts."""
    async with _acquire(conn) as c:
        return int(
            await c.fetchval(
                "SELECT count(*) FROM substrate_skill_proposals "
                "WHERE status = 'pending'"
            )
        )


__all__ = [
    "insert_proposal",
    "list_proposals",
    "get_proposal",
    "set_status",
    "has_similar",
    "count_pending",
]
