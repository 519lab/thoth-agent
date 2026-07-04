"""Context-management telemetry — Phase 0b of ``plans/substrate-context-engine.md``.

Persists per-turn context/token behaviour and compression events to
``substrate_telemetry`` (the non-perceptual operational sink — see
``substrate/telemetry.py``) so context-engine changes can be measured
against real distributions instead of grepping logs.

Contract mirrors ``substrate.events.thoth_hooks``: best-effort, guarded,
silent no-op when the substrate isn't booted. A telemetry failure must
never affect the turn that produced it.

Event kinds (``agent`` column = ``"context"``):

- ``context.turn`` — one row per ``run_conversation`` call: exit reason,
  api calls, iteration budget, message shape, per-turn token deltas
  (prompt / completion / cache read / cache write / reasoning / cost),
  cache hit ratio, duration.
- ``context.compressed`` — one row per compression pass: message and
  token counts before/after, duration, trigger, aborted / summary-fallback
  flags, session rotation ids.

Future kinds arrive with the substrate context engine (plan §4):
``context.evicted``, ``context.pagein``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Session-accumulator fields snapshotted at turn start and diffed at turn
# end. Keys are the payload names; values are the AIAgent attribute names.
_COUNTER_ATTRS = {
    "prompt_tokens": "session_prompt_tokens",
    "completion_tokens": "session_completion_tokens",
    "cache_read_tokens": "session_cache_read_tokens",
    "cache_write_tokens": "session_cache_write_tokens",
    "reasoning_tokens": "session_reasoning_tokens",
    "cost_usd": "session_estimated_cost_usd",
    "api_calls": "session_api_calls",
}


def snapshot_turn_counters(agent: Any) -> Dict[str, float]:
    """Capture the agent's session accumulators at turn start.

    Diffed against the same accumulators at turn end to produce per-turn
    deltas — the accumulators themselves span the whole session.
    """
    return {
        key: getattr(agent, attr, 0) or 0
        for key, attr in _COUNTER_ATTRS.items()
    }


def emit_turn_event(
    agent: Any,
    *,
    snapshot: Dict[str, float],
    exit_reason: str,
    api_calls: int,
    messages: list,
    interrupted: bool,
    response_len: int,
    started_at: datetime,
) -> None:
    """Emit one ``context.turn`` row summarising a completed turn."""
    try:
        deltas = {
            key: (getattr(agent, attr, 0) or 0) - snapshot.get(key, 0)
            for key, attr in _COUNTER_ATTRS.items()
        }
        # Round the cost delta — float subtraction noise isn't signal.
        deltas["cost_usd"] = round(deltas["cost_usd"], 6)

        prompt_delta = deltas["prompt_tokens"]
        cache_hit_pct: Optional[float] = None
        if prompt_delta > 0:
            cache_hit_pct = round(
                100.0 * deltas["cache_read_tokens"] / prompt_delta, 1
            )

        budget = getattr(agent, "iteration_budget", None)
        engine = getattr(agent, "context_compressor", None)

        n_tool_msgs = 0
        n_assistant_tool_turns = 0
        for m in messages:
            if not isinstance(m, dict):
                continue
            if m.get("role") == "tool":
                n_tool_msgs += 1
            elif m.get("role") == "assistant" and m.get("tool_calls"):
                n_assistant_tool_turns += 1

        payload = {
            "session_id": getattr(agent, "session_id", None),
            "model": getattr(agent, "model", None),
            "provider": getattr(agent, "provider", None) or None,
            "platform": getattr(agent, "platform", None) or None,
            "exit_reason": exit_reason,
            "interrupted": bool(interrupted),
            "api_calls": api_calls,
            "budget_used": budget.used if budget else None,
            "budget_max": budget.max_total if budget else None,
            "messages_total": len(messages),
            "tool_result_msgs": n_tool_msgs,
            "tool_call_turns": n_assistant_tool_turns,
            "tool_calls": getattr(agent, "_turn_tool_calls", 0),
            "tool_failures": getattr(agent, "_turn_tool_failures", 0),
            "response_len": response_len,
            "context_tokens_end": getattr(engine, "last_prompt_tokens", None)
            if engine
            else None,
            "compression_count": getattr(engine, "compression_count", None)
            if engine
            else None,
            "cache_hit_pct": cache_hit_pct,
            "duration_s": round(
                (datetime.now(timezone.utc) - started_at).total_seconds(), 2
            ),
            "deltas": deltas,
        }
        _emit("context.turn", payload)
    except Exception:
        logger.debug("context.turn emit failed", exc_info=True)


def emit_compression_event(
    agent: Any,
    *,
    trigger: str,
    messages_before: int,
    messages_after: int,
    tokens_before: Optional[int],
    tokens_after: Optional[int],
    duration_s: float,
    aborted: bool,
    summary_fallback: bool = False,
    old_session_id: Optional[str] = None,
    new_session_id: Optional[str] = None,
) -> None:
    """Emit one ``context.compressed`` row per compression pass."""
    try:
        payload = {
            "trigger": trigger,
            "model": getattr(agent, "model", None),
            "platform": getattr(agent, "platform", None) or None,
            "messages_before": messages_before,
            "messages_after": messages_after,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "tokens_saved": (tokens_before - tokens_after)
            if (tokens_before is not None and tokens_after is not None)
            else None,
            "duration_s": round(duration_s, 2),
            "aborted": bool(aborted),
            "summary_fallback": bool(summary_fallback),
            "old_session_id": old_session_id,
            "new_session_id": new_session_id,
            "compression_count": getattr(
                getattr(agent, "context_compressor", None),
                "compression_count",
                None,
            ),
        }
        _emit("context.compressed", payload)
    except Exception:
        logger.debug("context.compressed emit failed", exc_info=True)


def _emit(event: str, payload: Dict[str, Any]) -> None:
    """Write one telemetry row. Guarded: no-op when the substrate isn't
    booted; log + swallow on any error (same contract as thoth_hooks)."""
    try:
        from substrate import get_bound_substrate, telemetry

        substrate = get_bound_substrate()
        if substrate is None:
            return

        import thoth_db

        thoth_db.run_sync(
            telemetry.write(
                substrate, agent="context", event=event, payload=payload
            )
        )
    except Exception:
        logger.debug("context telemetry write failed (event=%s)", event, exc_info=True)


__all__ = [
    "snapshot_turn_counters",
    "emit_turn_event",
    "emit_compression_event",
]
