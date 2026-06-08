"""Phase F Dreamer — counterfactual exploration with a persistent log.

The Dreamer roams the substrate's knowledge in counterfactual / open-ended
mode — "what connects these two things?", "what might follow from this
pattern?" — and checkpoints its explorations to ``substrate_dreamer_log``,
a persistent log that survives restarts (MVS §3.8). The mind has
intellectual life the foreground doesn't witness; unlike the other
sub-agents the Dreamer's output is exploratory, not authoritative — it
seeds curiosity, it doesn't assert facts into L1–L4.

LLM-driven (mockable ``_dream`` seam); gated by ``THOTH_SUBSTRATE_DREAMER``
(default ON; set to 0 to disable). In the design the Dreamer runs at FULL only in
sleep-dreaming mode and OFF when awake — until the learned Conductor drives
that, the env gate + LOW floor stand in.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from substrate.agents.base import Level, SubAgent

if TYPE_CHECKING:  # pragma: no cover
    from substrate.facade import Substrate


# Dreaming is deep-cycle work: the Dreamer's run-loop tick fires on the
# intensity interval (LOW → every 10s), but an LLM exploration should NOT
# run that often. This guards the actual dream to its own slow interval —
# the same pattern as the Curator's hourly L3/L4 pass — so the Dreamer
# checkpoints occasionally instead of burning a model call every 10s.
# (Without it, fixing the model endpoint unmasked a ~200 dreams/hour runaway.)
_DREAM_INTERVAL_S = 1800.0  # 30 min default; env-tunable


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"} if raw else default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


async def append_dream(seed: str, exploration: str, *, metadata=None, conn=None) -> UUID:
    """Append one exploration to the persistent dreamer log."""
    import thoth_db

    async def _go(c):
        return await c.fetchval(
            "INSERT INTO substrate_dreamer_log (seed, exploration, metadata) "
            "VALUES ($1, $2, $3) RETURNING id",
            seed[:500], exploration[:2000], metadata or {},
        )

    if conn is not None:
        return await _go(conn)
    async with thoth_db.connection() as c:
        return await _go(c)


async def list_dreams(*, limit: int = 20, conn=None) -> list[dict]:
    import thoth_db

    async def _go(c):
        rows = await c.fetch(
            "SELECT id, seed, exploration, created_at FROM substrate_dreamer_log "
            "ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        return [dict(r) for r in rows]

    if conn is not None:
        return await _go(conn)
    async with thoth_db.connection() as c:
        return await _go(c)


async def _dream(seed: str, *, client, model, substrate=None) -> str:
    """LLM exploration seam (mocked in tests). Returns a free-text note."""
    from substrate import cost

    prompt = (
        "You are the dreaming faculty of an AI agent's memory — free, "
        "associative, counterfactual. Given this seed from what the agent "
        "knows, explore one open-ended thought: a connection, a 'what if', a "
        "question worth pursuing. 2-4 sentences, speculative is fine.\n\n"
        f"Seed: {seed}"
    )
    resp = await cost.acreate_and_record(
        client,
        substrate=substrate,
        agent="dreamer",
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.9,
    )
    return (resp.choices[0].message.content or "").strip()


class Dreamer(SubAgent):
    """Counterfactual exploration → persistent log. Floor LOW."""

    name = "dreamer"
    is_sentinel = False

    def __init__(self, substrate: "Substrate") -> None:
        super().__init__(substrate)
        self._level = Level.LOW
        # Deep-cycle throttle: dream at most once per this interval, even
        # though the run loop ticks every ~10s at LOW. Read per-tick (not
        # cached) so an operator can retune live. ``-inf`` so the first
        # eligible tick dreams immediately.
        self._last_dream_at: float = float("-inf")

    async def tick(self) -> None:
        if not _env_bool("THOTH_SUBSTRATE_DREAMER", default=True):
            return
        if self._level is Level.OFF:
            return
        # Deep-cycle interval gate — skip cheaply on the vast majority of
        # ticks so the Dreamer does occasional exploration, not a model
        # call every 10s.
        interval_s = _env_float("THOTH_SUBSTRATE_DREAM_INTERVAL_S", _DREAM_INTERVAL_S)
        now = time.monotonic()
        if now - self._last_dream_at < interval_s:
            return

        seed = await self._pick_seed()
        if not seed:
            return
        client, model = self._resolve_client()
        if client is None:
            return
        # Stamp now (before the call) so a failing dream waits the full
        # interval rather than retrying every tick.
        self._last_dream_at = now
        timeout_s = _env_int("DREAMER_TIMEOUT_S", 25)
        try:
            exploration = await asyncio.wait_for(
                _dream(seed, client=client, model=model, substrate=self._substrate),
                timeout=timeout_s,
            )
        except Exception:
            self._log.debug("dreamer.tick.degraded", exc_info=True)
            return
        if not exploration:
            return
        await append_dream(seed, exploration, metadata={"model": model})
        await self._emit_self_state(seed)

    @staticmethod
    def _resolve_client():
        from agent.auxiliary_client import get_async_text_auxiliary_client

        return get_async_text_auxiliary_client("dreamer")

    async def _pick_seed(self) -> Optional[str]:
        """Seed from a salient L3 pattern, else a couple of L1 entities."""
        import thoth_db

        async with thoth_db.connection() as conn:
            pat = await conn.fetchval(
                "SELECT statement FROM l3_patterns "
                "ORDER BY salience_score DESC, last_seen_at DESC LIMIT 1"
            )
            if pat:
                return pat
            ents = await conn.fetch(
                "SELECT name FROM l1_entities ORDER BY last_seen_at DESC LIMIT 3"
            )
        if len(ents) >= 2:
            return "What connects: " + ", ".join(e["name"] for e in ents)
        return None

    async def _emit_self_state(self, seed: str) -> None:
        from substrate.telemetry import write as telemetry_write

        try:
            await telemetry_write(
                self._substrate,
                agent="dreamer",
                event="dreamer.explored",
                payload={"seed": seed[:120]},
            )
        except Exception:
            self._log.debug("dreamer.telemetry.emit_failed", exc_info=True)


__all__ = ["Dreamer", "append_dream", "list_dreams", "_dream"]
