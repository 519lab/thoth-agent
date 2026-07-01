"""Versioned recall weight sets — ``substrate_recall_weights``.

The tuner (``tuner.py``) is pure and never writes; this module is where a
fitted weight vector becomes operational state. Each row is one candidate
vector with the evidence that produced it (corpus size, train/holdout
separation, the baseline it beat); at most one row is ``active`` (partial
unique index), and the live recall path reads the active row through a
short-TTL cache in ``api.py``.

History is append-only: applying new weights deactivates the old row but
never deletes it, so ``thoth substrate recall weights`` is a full audit
trail and any promotion can be reverted (``deactivate_all``) back to the
config/env baseline.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Optional

from substrate.recall.replay import Weights

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = logging.getLogger(__name__)


def _encode(weights: Weights) -> str:
    return json.dumps(
        {
            "similarity": weights.similarity,
            "keyword": weights.keyword,
            "salience": weights.salience,
            "recency": weights.recency,
            "half_life_hours": weights.half_life_hours,
        }
    )


def decode_weights(raw: Any) -> Optional[Weights]:
    """Decode a stored JSONB payload back into :class:`Weights`.

    Tolerant: any missing/garbage field returns None rather than raising —
    a corrupt row must degrade to the config baseline, never break recall.
    """
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, dict):
        return None
    try:
        return Weights(
            similarity=float(raw["similarity"]),
            keyword=float(raw["keyword"]),
            salience=float(raw["salience"]),
            recency=float(raw["recency"]),
            half_life_hours=float(raw["half_life_hours"]),
        )
    except (KeyError, ValueError, TypeError):
        return None


async def save(
    conn: "asyncpg.Connection",
    *,
    weights: Weights,
    source: str = "cli",
    corpus_size: int = 0,
    train_metric: Optional[float] = None,
    holdout_metric: Optional[float] = None,
    baseline_holdout_metric: Optional[float] = None,
    activate: bool = False,
) -> str:
    """Insert one weight row; optionally promote it to active. Returns id."""
    async with conn.transaction():
        if activate:
            await conn.execute(
                "UPDATE substrate_recall_weights SET active = FALSE WHERE active"
            )
        row_id = await conn.fetchval(
            """
            INSERT INTO substrate_recall_weights
                (weights, source, corpus_size, train_metric,
                 holdout_metric, baseline_holdout_metric, active)
            VALUES ($1::jsonb, $2, $3, $4, $5, $6, $7)
            RETURNING id
            """,
            _encode(weights),
            source,
            corpus_size,
            train_metric,
            holdout_metric,
            baseline_holdout_metric,
            activate,
        )
    return str(row_id)


async def get_active(conn: "asyncpg.Connection") -> Optional[Weights]:
    """The currently active tuned weights, or None (= config baseline)."""
    raw = await conn.fetchval(
        "SELECT weights FROM substrate_recall_weights WHERE active LIMIT 1"
    )
    if raw is None:
        return None
    return decode_weights(raw)


async def activate(conn: "asyncpg.Connection", weights_id: str) -> bool:
    """Promote one stored row to active (demoting any current). False if
    ``weights_id`` doesn't exist."""
    async with conn.transaction():
        await conn.execute(
            "UPDATE substrate_recall_weights SET active = FALSE WHERE active"
        )
        status = await conn.execute(
            "UPDATE substrate_recall_weights SET active = TRUE WHERE id = $1",
            weights_id,
        )
    return status.endswith("1")


async def deactivate_all(conn: "asyncpg.Connection") -> int:
    """Revert to the config/env baseline. Returns rows demoted (0 or 1)."""
    status = await conn.execute(
        "UPDATE substrate_recall_weights SET active = FALSE WHERE active"
    )
    try:
        return int(status.rsplit(" ", 1)[-1])
    except ValueError:  # pragma: no cover — defensive
        return 0


async def history(
    conn: "asyncpg.Connection", *, limit: int = 10
) -> list[dict]:
    """Newest-first audit trail of stored weight sets."""
    rows = await conn.fetch(
        """
        SELECT id, created_at, weights, source, corpus_size,
               train_metric, holdout_metric, baseline_holdout_metric, active
          FROM substrate_recall_weights
         ORDER BY created_at DESC
         LIMIT $1
        """,
        limit,
    )
    out = []
    for r in rows:
        out.append(
            {
                "id": str(r["id"]),
                "created_at": r["created_at"],
                "weights": decode_weights(r["weights"]),
                "source": r["source"],
                "corpus_size": r["corpus_size"],
                "train_metric": r["train_metric"],
                "holdout_metric": r["holdout_metric"],
                "baseline_holdout_metric": r["baseline_holdout_metric"],
                "active": r["active"],
            }
        )
    return out


__all__ = [
    "decode_weights",
    "save",
    "get_active",
    "activate",
    "deactivate_all",
    "history",
]
