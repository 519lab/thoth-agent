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

  - :func:`note_turn_pricing` — called once per priced or unpriced model
    call during the turn. The session's ``session_cost_status`` is whatever
    the last call wrote, so the turn row must not read it.

  - :func:`record_turn_cost` — sync convenience called from the post-turn
    block: computes the deltas, skips turns that observed no usage, and
    bridges the insert to the DB loop via ``thoth_db.run_sync`` with a short
    timeout. Best-effort by contract: it swallows everything, because
    instrumenting a turn must never break the response the turn just
    produced. Kill-switch: ``THOTH_TURN_COST=0``.

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

# A stuck pool must not hold the reply. The insert keeps running on the DB
# loop if this fires; the turn just stops waiting.
_RECORD_TIMEOUT_S = 2.0


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


@dataclass
class TurnCostTracker:
    """Per-turn pricing notes. Mutable; one tracker per turn.

    ``session_cost_status`` is last-write-wins across the whole session, so
    a later unpriced call would otherwise null out dollars this turn already
    accumulated. These counters are the turn's own tally.
    """

    snapshot: TurnCostSnapshot
    priced_calls: int = 0
    unpriced_calls: int = 0
    priced_status: str = "estimated"


def snapshot_turn_cost(agent: Any) -> TurnCostTracker:
    """Capture the agent's session counters and attach a per-turn tracker.

    The tracker is stored on ``agent._turn_cost_tracker`` so call sites that
    price a call (the conversation loop, delegate rollup) can note it without
    threading the object through the stack.
    """
    tracker = TurnCostTracker(
        snapshot=TurnCostSnapshot(
            input_tokens=int(getattr(agent, "session_input_tokens", 0) or 0),
            output_tokens=int(getattr(agent, "session_output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(agent, "session_cache_read_tokens", 0) or 0),
            cache_write_tokens=int(getattr(agent, "session_cache_write_tokens", 0) or 0),
            reasoning_tokens=int(getattr(agent, "session_reasoning_tokens", 0) or 0),
            total_tokens=int(getattr(agent, "session_total_tokens", 0) or 0),
            estimated_cost_usd=float(getattr(agent, "session_estimated_cost_usd", 0.0) or 0.0),
            monotonic_start=time.monotonic(),
        )
    )
    try:
        setattr(agent, "_turn_cost_tracker", tracker)
    except Exception:
        logger.debug("turn cost tracker attach failed", exc_info=True)
    return tracker


def note_turn_pricing(agent: Any, *, status: str, amount_usd: Any) -> None:
    """Record one model call's pricing outcome on the active turn tracker.

    ``amount_usd is None`` or status ``unknown`` is an unpriced call. Any
    other status with an amount, including a genuine ``0`` (included or
    free), counts as priced. No tracker means the caller is outside a
    tracked turn; that is a no-op.
    """
    tracker = getattr(agent, "_turn_cost_tracker", None)
    if not isinstance(tracker, TurnCostTracker):
        return
    if status == "unknown" or amount_usd is None:
        tracker.unpriced_calls += 1
        return
    tracker.priced_calls += 1
    tracker.priced_status = status


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


def _resolve_tracker(agent: Any, snapshot: Any) -> tuple[TurnCostSnapshot, TurnCostTracker]:
    if isinstance(snapshot, TurnCostTracker):
        return snapshot.snapshot, snapshot
    attached = getattr(agent, "_turn_cost_tracker", None)
    if isinstance(attached, TurnCostTracker):
        return attached.snapshot, attached
    base = snapshot if isinstance(snapshot, TurnCostSnapshot) else TurnCostSnapshot()
    return base, TurnCostTracker(snapshot=base)


def record_turn_cost(agent: Any, snapshot: Any, *, api_calls: int) -> None:
    """Compute this turn's deltas from ``snapshot`` and persist them.

    Called from the post-turn block (sync thread). Bridges to the DB loop
    via ``thoth_db.run_sync`` and stops waiting after ``_RECORD_TIMEOUT_S``.
    Swallows everything: the caller has already produced the response and
    nothing here may perturb it.

    A turn that never moved the token counters and has no
    :func:`note_turn_pricing` calls is skipped. That includes the Codex
    app-server path, which does not feed usage today — writing it would
    count as an unpriced turn. A mid-turn counter reset still records
    (the delta clamps to zero).
    When notes exist, ``unknown`` is stored only if every noted call was
    unpriced. Otherwise the row keeps the priced dollar delta.
    """
    if not turn_cost_enabled():
        return
    try:
        base, tracker = _resolve_tracker(agent, snapshot)
        duration_ms = int((time.monotonic() - base.monotonic_start) * 1000)
        d_input = max(0, int(getattr(agent, "session_input_tokens", 0) or 0) - base.input_tokens)
        d_output = max(0, int(getattr(agent, "session_output_tokens", 0) or 0) - base.output_tokens)
        d_cache_read = max(
            0, int(getattr(agent, "session_cache_read_tokens", 0) or 0) - base.cache_read_tokens
        )
        d_cache_write = max(
            0, int(getattr(agent, "session_cache_write_tokens", 0) or 0) - base.cache_write_tokens
        )
        d_reasoning = max(
            0, int(getattr(agent, "session_reasoning_tokens", 0) or 0) - base.reasoning_tokens
        )
        d_total = max(0, int(getattr(agent, "session_total_tokens", 0) or 0) - base.total_tokens)
        d_cost = max(
            0.0,
            float(getattr(agent, "session_estimated_cost_usd", 0.0) or 0.0) - base.estimated_cost_usd,
        )
        noted = tracker.priced_calls + tracker.unpriced_calls
        # A counter reset mid-turn clamps the delta to 0 but the turn still
        # happened. A path that never moved the counters (Codex today) did
        # not observe usage and must not land as an unpriced row.
        counters_reset = int(getattr(agent, "session_total_tokens", 0) or 0) < base.total_tokens
        if noted == 0 and d_total == 0 and not counters_reset:
            return

        cost_usd: Optional[float]
        if noted:
            if tracker.priced_calls == 0:
                cost_usd = None
                cost_status = "unknown"
            else:
                cost_usd = d_cost
                cost_status = tracker.priced_status
        else:
            # Callers that only bump the cumulative counters (tests, and any
            # path that has not called note_turn_pricing) still record from
            # the session status. Production turns note each call.
            cost_status = str(getattr(agent, "session_cost_status", "") or "unknown")
            cost_usd = None if cost_status == "unknown" else d_cost

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
                cost_usd=cost_usd,
                cost_status=cost_status,
                duration_ms=duration_ms,
            ),
            timeout=_RECORD_TIMEOUT_S,
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


def render_turn_metrics(
    *,
    uptime_s: float,
    turn: Optional[dict],
    crew: Optional[dict],
    scrape_error: int,
) -> str:
    """Prometheus text exposition for one ``/metrics`` scrape.

    ``turn`` or ``crew`` of None means that rollup failed and its gauges
    are omitted. An empty dict still emits zeros. ``cost_usd`` and the
    latency quantiles are omitted when the value is None.
    """
    lines: list[str] = []

    def gauge(name: str, value: Any, help_text: str, labels: str = "") -> None:
        if value is None:
            return
        if not any(line.startswith(f"# HELP {name} ") for line in lines):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
        label_part = f"{{{labels}}}" if labels else ""
        lines.append(f"{name}{label_part} {float(value):g}")

    gauge(
        "thoth_api_server_uptime_seconds",
        uptime_s,
        "Seconds since the API server adapter started.",
    )
    if turn is not None:
        gauge("thoth_turns_24h", turn.get("turns", 0),
              "Main-agent turns completed in the trailing 24h.")
        gauge("thoth_api_calls_24h", turn.get("api_calls", 0),
              "Main-agent LLM API calls in the trailing 24h.")
        token_help = "Main-agent tokens by kind in the trailing 24h."
        for kind in ("input", "output", "cache_read", "cache_write", "reasoning"):
            gauge("thoth_tokens_24h", turn.get(f"{kind}_tokens", 0),
                  token_help, labels=f'kind="{kind}"')
        gauge("thoth_cost_usd_24h", turn.get("cost_usd"),
              "Estimated main-agent spend (USD) in the trailing 24h; "
              "absent when no turn in the window had a priced route.")
        gauge("thoth_unpriced_turns_24h", turn.get("unpriced_turns", 0),
              "Turns in the trailing 24h with no cost estimate "
              "(unknown pricing route).")
        duration_help = "Main-agent turn wall-clock duration quantiles over the trailing 24h."
        gauge("thoth_turn_duration_ms", turn.get("p50_duration_ms"),
              duration_help, labels='quantile="0.5"')
        gauge("thoth_turn_duration_ms", turn.get("p95_duration_ms"),
              duration_help, labels='quantile="0.95"')
    if crew is not None:
        gauge("thoth_substrate_llm_calls_24h", crew.get("calls", 0),
              "Substrate sub-agent LLM calls in the trailing 24h.")
        gauge("thoth_substrate_tokens_24h", crew.get("total_tokens", 0),
              "Substrate sub-agent total tokens in the trailing 24h.")
        gauge("thoth_substrate_llm_latency_ms", crew.get("p95_latency_ms"),
              "Substrate sub-agent LLM call latency over the trailing 24h.",
              labels='quantile="0.95"')
    gauge("thoth_metrics_scrape_errors", scrape_error,
          "1 when the cost/latency rollup query failed this scrape.")
    return "\n".join(lines) + "\n"


__all__ = [
    "TurnCostSnapshot",
    "TurnCostTracker",
    "snapshot_turn_cost",
    "note_turn_pricing",
    "record_turn_cost",
    "write_turn_cost",
    "turn_cost_enabled",
    "fetch_turn_summary",
    "fetch_model_breakdown",
    "fetch_substrate_summary",
    "render_turn_metrics",
]
