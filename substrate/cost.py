"""Non-perceptual per-call cost/usage sink — :func:`record_usage`.

This is where the substrate records the *cost of running itself*: one row
per LLM ``chat.completions.create`` a sub-agent makes (prompt/completion/
total token counts + wall-clock latency, tagged by agent + model). It is
the cost counterpart to :mod:`substrate.telemetry`:

* ``telemetry.write`` writes **operational telemetry** — the decisions the
  substrate's sub-agents make about running the substrate.
* ``cost.record_usage`` writes **operational cost** — what those decisions
  *spent* at the model API.

Both land in append-only ``substrate_*`` tables the awareness loop *never
reads*: no ``substrate_slices`` row, so a usage write can never increment
the consolidation backlog, enter the Curator's pending set, be counted by
the Conductor's load forecast, or be read back as recall. The schema-level
boundary is :func:`substrate.storage.streams.is_perceptual` — anything on a
``substrate.*`` stream is excluded from awareness-loop queries; this module
writes to the excluded side of that boundary.

Recording is strictly best-effort: a cost write must never break (or even
perturb) the LLM call it instruments. :func:`record_usage` swallows and
logs at debug; :func:`acreate_and_record` is a transparent pass-through
that returns the provider response unchanged.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover
    from substrate.facade import Substrate


logger = logging.getLogger(__name__)


_INSERT_SQL = """
    INSERT INTO substrate_agent_cost
        (agent, model, prompt_tokens, completion_tokens, total_tokens, latency_ms)
    VALUES ($1, $2, $3, $4, $5, $6)
"""


async def record_usage(
    substrate: "Substrate",
    *,
    agent: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    latency_ms: int,
) -> None:
    """Append one cost/usage row. Non-perceptual, best-effort by design.

    ``agent`` — the emitting sub-agent's ``name`` (e.g. ``"conductor"``).
    ``model`` — the model the call was made against.
    ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens`` — usage as
        reported by the provider response.
    ``latency_ms`` — measured wall-clock duration of the create() call.

    Like ``telemetry.write`` this never touches ``substrate_slices`` — so a
    cost write can never increment the consolidation backlog, enter the
    Curator's pending set, or be read back as perception. Failures are
    swallowed and logged at debug: instrumenting cost must never break the
    LLM call that produced it.
    """
    try:
        async with substrate.pool.acquire() as own_conn:
            await own_conn.execute(
                _INSERT_SQL,
                agent,
                model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                latency_ms,
            )
    except Exception:  # noqa: BLE001 — best-effort sink, never raise.
        logger.debug("substrate cost record failed", exc_info=True)


async def acreate_and_record(
    client: Any,
    *,
    substrate: "Optional[Substrate]",
    agent: str,
    **kwargs: Any,
) -> Any:
    """Transparent ``chat.completions.create`` wrapper that records cost.

    Calls ``await client.chat.completions.create(**kwargs)``, times it, reads
    usage off ``response.usage`` (guarding missing/None usage and partial
    attributes), records a ``substrate_agent_cost`` row via
    :func:`record_usage`, and returns the ORIGINAL response object unchanged
    — callers see exactly what the provider returned.

    ``model`` for the cost row is taken from ``kwargs.get("model", "")``.

    If ``substrate`` is None, recording is skipped but the create() call is
    still made and its response returned, so existing call paths/tests that
    have no substrate wired in are unaffected.
    """
    model = kwargs.get("model", "")
    start = time.monotonic()
    response = await client.chat.completions.create(**kwargs)
    latency_ms = int((time.monotonic() - start) * 1000)

    if substrate is None:
        return response

    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    total_tokens = int(getattr(usage, "total_tokens", 0) or 0)

    await record_usage(
        substrate,
        agent=agent,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
    )
    return response


__all__ = ["record_usage", "acreate_and_record"]
