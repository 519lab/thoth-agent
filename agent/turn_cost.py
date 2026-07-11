"""Per-turn cost/latency recording — always-on operator visibility.

The main conversation loop already tracks canonical token usage and an
estimated cost on the agent's session counters (``session_input_tokens``,
``session_estimated_cost_usd``, …, accumulated in
``agent/chat_completion_helpers.py``), but until now nothing persisted them:
a default install could not answer "what did today cost?" or "what's p95
turn latency?" without the opt-in Langfuse plugin.

Three deliberately separate pieces, mirroring :mod:`agent.turn_outcome`:

  - :class:`TurnCostSnapshot` / :func:`snapshot_turn_cost` — capture the
    session-cumulative counters at turn start so the post-turn block can
    compute *this turn's* deltas (the counters survive across turns).

  - :func:`record_turn_cost` — sync convenience called from the post-turn
    block: computes the deltas, skips no-op turns, and bridges the insert to
    the DB loop via ``thoth_db.run_sync``. Best-effort by contract: it
    swallows everything, because instrumenting a turn must never break the
    response the turn just produced. Kill-switch: ``THOTH_TURN_COST=0``.

  - ``fetch_*`` rollup queries — windowed aggregates over ``agent_turn_cost``
    (and its substrate sibling ``substrate_agent_cost``) consumed by
    ``thoth cost`` and the gateway ``/metrics`` endpoint.

Like ``substrate_agent_cost``, the ``agent_turn_cost`` table is append-only
operator telemetry the awareness loop never reads — no slice, no backlog,
no recall.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


def turn_cost_enabled() -> bool:
    """True unless ``THOTH_TURN_COST`` disables recording (default: on)."""
    return os.getenv("THOTH_TURN_COST", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


@dataclass(frozen=True)
class TurnCostSnapshot:
    """Session-cumulative counters captured at turn start."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    monotonic_start: float = 0.0


def snapshot_turn_cost(agent: Any) -> TurnCostSnapshot:
    """Capture the agent's session counters + a monotonic clock at turn start."""
    return TurnCostSnapshot(
        input_tokens=int(getattr(agent, "session_input_tokens", 0) or 0),
        output_tokens=int(getattr(agent, "session_output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(agent, "session_cache_read_tokens", 0) or 0),
        cache_write_tokens=int(getattr(agent, "session_cache_write_tokens", 0) or 0),
        reasoning_tokens=int(getattr(agent, "session_reasoning_tokens", 0) or 0),
        total_tokens=int(getattr(agent, "session_total_tokens", 0) or 0),
        estimated_cost_usd=float(getattr(agent, "session_estimated_cost_usd", 0.0) or 0.0),
        monotonic_start=time.monotonic(),
    )


_INSERT_SQL = """
    INSERT INTO agent_turn_cost
        (agent, session_id, platform, model, provider,
         input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
         reasoning_tokens, total_tokens, api_calls,
         cost_usd, cost_status, duration_ms)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
"""


async def write_turn_cost(
    *,
    session_id: Optional[str],
    platform: str,
    model: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    reasoning_tokens: int,
    total_tokens: int,
    api_calls: int,
    cost_usd: Optional[float],
    cost_status: str,
    duration_ms: int,
    agent_name: str = "main",
) -> None:
    """Append one turn-cost row. Best-effort: failures degrade to a no-op."""
    # Late import: keep the pure half importable without the DB pool.
    import thoth_db

    try:
        async with thoth_db.transaction() as conn:
            await conn.execute(
                _INSERT_SQL,
                agent_name,
                session_id,
                platform,
                model,
                provider,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_write_tokens,
                reasoning_tokens,
                total_tokens,
                api_calls,
                cost_usd,
                cost_status,
                duration_ms,
            )
    except Exception as exc:
        # Observability, never load-bearing for the turn itself.
        logger.debug("turn cost write failed: %s", exc)


def record_turn_cost(agent: Any, snapshot: TurnCostSnapshot, *, api_calls: int) -> None:
    """Compute this turn's deltas from ``snapshot`` and persist them.

    Called from the post-turn block (sync thread) — bridges to the DB loop
    via ``thoth_db.run_sync``. Swallows everything: the caller has already
    produced the response and nothing here may perturb it. Turns that made
    no API call and consumed no tokens are skipped (nothing to bill).
    """
    if not turn_cost_enabled():
        return
    try:
        duration_ms = int((time.monotonic() - snapshot.monotonic_start) * 1000)
        d_input = max(0, int(getattr(agent, "session_input_tokens", 0) or 0) - snapshot.input_tokens)
        d_output = max(0, int(getattr(agent, "session_output_tokens", 0) or 0) - snapshot.output_tokens)
        d_cache_read = max(
            0, int(getattr(agent, "session_cache_read_tokens", 0) or 0) - snapshot.cache_read_tokens
        )
        d_cache_write = max(
            0, int(getattr(agent, "session_cache_write_tokens", 0) or 0) - snapshot.cache_write_tokens
        )
        d_reasoning = max(
            0, int(getattr(agent, "session_reasoning_tokens", 0) or 0) - snapshot.reasoning_tokens
        )
        d_total = max(0, int(getattr(agent, "session_total_tokens", 0) or 0) - snapshot.total_tokens)
        if api_calls <= 0 and d_total == 0:
            return  # no LLM traffic this turn — nothing to record

        cost_status = str(getattr(agent, "session_cost_status", "") or "unknown")
        d_cost: Optional[float]
        if cost_status in ("unknown", "n/a"):
            d_cost = None
        else:
            d_cost = max(
                0.0,
                float(getattr(agent, "session_estimated_cost_usd", 0.0) or 0.0)
                - snapshot.estimated_cost_usd,
            )

        import thoth_db

        thoth_db.run_sync(
            write_turn_cost(
                session_id=getattr(agent, "session_id", None),
                platform=str(getattr(agent, "platform", None) or ""),
                model=str(getattr(agent, "model", "") or ""),
                provider=str(getattr(agent, "provider", "") or ""),
                input_tokens=d_input,
                output_tokens=d_output,
                cache_read_tokens=d_cache_read,
                cache_write_tokens=d_cache_write,
                reasoning_tokens=d_reasoning,
                total_tokens=d_total,
                api_calls=int(api_calls),
                cost_usd=d_cost,
                cost_status=cost_status,
                duration_ms=duration_ms,
            )
        )
    except Exception as exc:
        logger.debug("turn cost record failed: %s", exc)


# ----------------------------------------------------------------------
# Windowed rollups — consumed by `thoth cost` and the gateway /metrics.
# ----------------------------------------------------------------------

_SUMMARY_SQL = """
    SELECT count(*)                              AS turns,
           COALESCE(SUM(input_tokens), 0)::bigint        AS input_tokens,
           COALESCE(SUM(output_tokens), 0)::bigint       AS output_tokens,
           COALESCE(SUM(cache_read_tokens), 0)::bigint   AS cache_read_tokens,
           COALESCE(SUM(cache_write_tokens), 0)::bigint  AS cache_write_tokens,
           COALESCE(SUM(reasoning_tokens), 0)::bigint    AS reasoning_tokens,
           COALESCE(SUM(total_tokens), 0)::bigint        AS total_tokens,
           COALESCE(SUM(api_calls), 0)::bigint           AS api_calls,
           SUM(cost_usd)                         AS cost_usd,
           count(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_turns,
           percentile_cont(0.5)  WITHIN GROUP (ORDER BY duration_ms) AS p50_duration_ms,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_duration_ms
      FROM agent_turn_cost
     WHERE at > now() - make_interval(hours => $1)
"""

_MODEL_BREAKDOWN_SQL = """
    SELECT model,
           count(*)                       AS turns,
           COALESCE(SUM(total_tokens), 0)::bigint AS total_tokens,
           SUM(cost_usd)                  AS cost_usd
      FROM agent_turn_cost
     WHERE at > now() - make_interval(hours => $1)
     GROUP BY model
     ORDER BY COALESCE(SUM(cost_usd), 0) DESC, SUM(total_tokens) DESC
     LIMIT $2
"""

_SUBSTRATE_SUMMARY_SQL = """
    SELECT count(*)                       AS calls,
           COALESCE(SUM(total_tokens), 0)::bigint AS total_tokens,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency_ms
      FROM substrate_agent_cost
     WHERE at > now() - make_interval(hours => $1)
"""


async def fetch_turn_summary(*, hours: float) -> dict:
    """Main-agent rollup over the trailing ``hours`` window."""
    import thoth_db

    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(_SUMMARY_SQL, float(hours))
    return dict(row) if row is not None else {}


async def fetch_model_breakdown(*, hours: float, limit: int = 10) -> list:
    """Per-model turns/tokens/cost over the trailing ``hours`` window."""
    import thoth_db

    async with thoth_db.connection() as conn:
        rows = await conn.fetch(_MODEL_BREAKDOWN_SQL, float(hours), int(limit))
    return [dict(r) for r in rows]


async def fetch_substrate_summary(*, hours: float) -> dict:
    """Substrate crew spend (``substrate_agent_cost``) over the same window."""
    import thoth_db

    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(_SUBSTRATE_SUMMARY_SQL, float(hours))
    return dict(row) if row is not None else {}


__all__ = [
    "TurnCostSnapshot",
    "snapshot_turn_cost",
    "record_turn_cost",
    "write_turn_cost",
    "turn_cost_enabled",
    "fetch_turn_summary",
    "fetch_model_breakdown",
    "fetch_substrate_summary",
]
