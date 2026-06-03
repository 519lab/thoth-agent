"""Phase E2 Pattern-finder — generalizes L1 knowledge into L3 patterns.

Reads recent L1 entities + their relationships, asks the auxiliary chat
model for higher-order patterns (generalizations / themes / recurring
structures), and upserts them into L3 citing the entities they generalize.

Gated by ``HERMES_SUBSTRATE_PATTERNFINDER`` (default ON; set to 0 to disable): registers +
heartbeats, tick no-op until opted in — same staged rollout as the Parser.
Per the Phase E2 spec.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING

from substrate.agents.base import Level, SubAgent
from substrate.l1 import store as l1
from substrate.l3 import extract, store as l3

if TYPE_CHECKING:  # pragma: no cover
    from substrate.facade import Substrate


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"} if raw else default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


class PatternFinder(SubAgent):
    """L1 → L3 generalization. Floor intensity LOW (deep-cycle work)."""

    name = "pattern-finder"
    is_sentinel = False

    def __init__(self, substrate: "Substrate") -> None:
        super().__init__(substrate)
        self._level = Level.LOW
        # Change-gating: throttle + a watermark of the newest L1 entity seen
        # at the last run. Re-generalizing a static L1 every tick is what
        # flooded L3 with reworded near-duplicates.
        self._last_run_mono: float = 0.0
        self._last_l1_max_seen = None

    async def tick(self) -> None:
        if not _env_bool("HERMES_SUBSTRATE_PATTERNFINDER", default=True):
            return
        if self._level is Level.OFF:
            return
        if not await self._should_run():
            return

        context, entity_by_name = await self._build_context()
        min_entities = _env_int("PATTERNFINDER_MIN_ENTITIES", 5)
        if len(entity_by_name) < min_entities:
            return

        client, model = extract.resolve_pattern_client()
        if client is None:
            return

        started = time.monotonic()
        timeout_s = _env_int("PATTERNFINDER_TIMEOUT_S", 25)
        try:
            result = await asyncio.wait_for(
                extract.call_pattern_llm(
                    context, client=client, model=model, substrate=self._substrate
                ),
                timeout=timeout_s,
            )
        except (asyncio.TimeoutError, extract.PatternError, Exception):
            # Degrade silently — patterns are best-effort enrichment.
            self._log.debug("pattern_finder.tick.degraded", exc_info=True)
            return

        if result.is_empty:
            return

        n = 0
        for p in result.patterns:
            cites = [str(entity_by_name[nm.lower()])
                     for nm in p.entity_names if nm.lower() in entity_by_name]
            await l3.upsert_pattern(
                p.statement, p.kind, cites=cites, confidence=p.confidence
            )
            n += 1
        await self._emit_self_state(n, time.monotonic() - started, model)

    async def _should_run(self) -> bool:
        """Gate deep-cycle generalization: an interval throttle AND a check
        that L1 actually gained/updated entities since the last run. On a
        static L1 there's nothing new to generalize — running anyway just
        produces reworded duplicates of patterns we already have."""
        import asyncio

        import thoth_db

        interval = _env_int("PATTERNFINDER_INTERVAL_S", 300)
        now_mono = asyncio.get_event_loop().time()
        if self._last_run_mono and (now_mono - self._last_run_mono) < interval:
            return False
        async with thoth_db.connection() as conn:
            l1_max = await conn.fetchval("SELECT max(last_seen_at) FROM l1_entities")
        if (
            l1_max is not None
            and self._last_l1_max_seen is not None
            and l1_max <= self._last_l1_max_seen
        ):
            # No new knowledge since last run; don't re-generalize.
            self._last_run_mono = now_mono  # honour the throttle on the next check
            return False
        self._last_run_mono = now_mono
        self._last_l1_max_seen = l1_max
        return True

    async def _build_context(self):
        """Recent L1 entities (+ a few relationships each) → a text block the
        model reads, plus a name→id map for citation resolution."""
        import thoth_db

        limit = _env_int("PATTERNFINDER_CONTEXT_ENTITIES", 40)
        async with thoth_db.connection() as conn:
            ents = await conn.fetch(
                """
                SELECT id, name, entity_type, summary
                  FROM l1_entities
                 ORDER BY last_seen_at DESC
                 LIMIT $1
                """,
                limit,
            )
            rels = await conn.fetch(
                """
                SELECT s.name AS subj, r.predicate, o.name AS obj
                  FROM l1_relationships r
                  JOIN l1_entities s ON s.id = r.subject_id
                  JOIN l1_entities o ON o.id = r.object_id
                 ORDER BY r.last_seen_at DESC
                 LIMIT $1
                """,
                limit,
            )
        lines = ["Entities:"]
        entity_by_name = {}
        for e in ents:
            entity_by_name[e["name"].lower()] = e["id"]
            summary = f" — {e['summary']}" if e["summary"] else ""
            lines.append(f"- {e['name']} ({e['entity_type']}){summary}")
        if rels:
            lines.append("\nRelationships:")
            for r in rels:
                lines.append(f"- {r['subj']} {r['predicate']} {r['obj']}")
        return "\n".join(lines), entity_by_name

    async def _emit_self_state(self, n_patterns, elapsed_s, model) -> None:
        from substrate.telemetry import write as telemetry_write

        try:
            await telemetry_write(
                self._substrate,
                agent="pattern-finder",
                event="patternfinder.found",
                payload={
                    "patterns": n_patterns,
                    "model": model,
                    "latency_ms": int(elapsed_s * 1000),
                },
            )
        except Exception:
            self._log.debug("patternfinder.telemetry.emit_failed", exc_info=True)


__all__ = ["PatternFinder"]
