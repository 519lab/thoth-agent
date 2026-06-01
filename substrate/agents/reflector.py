"""Phase F Reflector — meta-cognition: L1–L3 → L3 reflections + L4 self-model.

Where the Critic *measures* the substrate deterministically, the Reflector
*synthesizes* — an LLM reads the substrate's own accumulated structure (L3
patterns, the L1 entity-type distribution, the latest coherence) and writes
higher-order meta-observations: reflections about themes (→ L3) and notes
about the mind's own shape and biases (→ L4 self-model). MVS §3.3
(Reflector: L1–L3 → L3, L4).

LLM-driven (mockable seam ``_synthesize``); gated by
``HERMES_SUBSTRATE_REFLECTOR`` (default ON: registers + heartbeats, tick
no-op). No new schema — reflections land in the existing ``l3_patterns``
and ``l4_observations`` tables. Degrades silently on any LLM error.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from substrate.agents.base import Level, SubAgent
from substrate.l3 import store as l3
from substrate.l4 import store as l4

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


@dataclass(frozen=True)
class Reflection:
    statement: str
    layer: str = "l4"        # 'l3' (a pattern) | 'l4' (a self-model note)
    kind: str = "other"
    confidence: float = 0.5


@dataclass(frozen=True)
class ReflectorResult:
    reflections: list[Reflection] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.reflections


_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reflections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "statement": {"type": "string"},
                    "layer": {"type": "string", "enum": ["l3", "l4"]},
                    "kind": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["statement", "layer"],
            },
        }
    },
    "required": ["reflections"],
}


def resolve_reflector_client():
    from agent.auxiliary_client import get_async_text_auxiliary_client

    return get_async_text_auxiliary_client("reflector")


def _coerce(data) -> ReflectorResult:
    if not isinstance(data, dict) or not isinstance(data.get("reflections"), list):
        return ReflectorResult()
    out = []
    for r in data["reflections"]:
        if not isinstance(r, dict) or not r.get("statement"):
            continue
        layer = "l3" if str(r.get("layer")).lower() == "l3" else "l4"
        try:
            conf = float(r.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        out.append(
            Reflection(
                statement=str(r["statement"]).strip()[:500],
                layer=layer,
                kind=str(r.get("kind") or "other").strip().lower()[:40] or "other",
                confidence=max(0.0, min(1.0, conf)),
            )
        )
    return ReflectorResult(reflections=out)


async def _synthesize(context: str, *, client, model) -> ReflectorResult:
    """LLM synthesis seam (mocked in tests). JSON-schema → plain fallback."""
    from substrate.l1.extract import _strip_fences

    prompt = (
        "You are the reflective faculty of a Thoth Agent's memory. Below is "
        "a summary of what the agent currently knows about its world and "
        "itself. Synthesize 1-3 HIGHER-ORDER meta-observations: reflections "
        "on recurring themes (layer 'l3') or notes about the shape, gaps, or "
        "biases of the agent's own knowledge (layer 'l4'). Be concise and "
        "only state what the summary supports.\n\n"
        f"{context}\n\n"
        f"Return JSON matching: {json.dumps(_SCHEMA)}"
    )
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_schema",
                             "json_schema": {"name": "reflections", "schema": _SCHEMA}},
            temperature=0.4,
        )
        raw = resp.choices[0].message.content or ""
    except Exception:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user",
                       "content": prompt + "\n\nReturn ONLY the JSON object."}],
            temperature=0.4,
        )
        raw = resp.choices[0].message.content or ""
    try:
        return _coerce(json.loads(_strip_fences(raw)))
    except (json.JSONDecodeError, ValueError):
        return ReflectorResult()


class Reflector(SubAgent):
    """L1–L3 → L3/L4 synthesis. Floor LOW."""

    name = "reflector"
    is_sentinel = False

    def __init__(self, substrate: "Substrate") -> None:
        super().__init__(substrate)
        self._level = Level.LOW
        # Change-gating (see PatternFinder). Gate on L1 — reflection follows
        # new *knowledge*. Gating on L3 would be circular: the Reflector writes
        # L3, which would re-trigger it off its own output.
        self._last_run_mono: float = 0.0
        self._last_l1_max_seen = None

    async def tick(self) -> None:
        if not _env_bool("HERMES_SUBSTRATE_REFLECTOR", default=True):
            return
        if self._level is Level.OFF:
            return
        if not await self._should_run():
            return

        context, have_material = await self._build_context()
        if not have_material:
            return
        client, model = resolve_reflector_client()
        if client is None:
            return

        timeout_s = _env_int("REFLECTOR_TIMEOUT_S", 25)
        try:
            result = await asyncio.wait_for(
                _synthesize(context, client=client, model=model), timeout=timeout_s
            )
        except Exception:
            self._log.debug("reflector.tick.degraded", exc_info=True)
            return
        if result.is_empty:
            return

        n_l3 = n_l4 = 0
        for r in result.reflections:
            if r.layer == "l3":
                await l3.upsert_pattern(r.statement, "theme", confidence=r.confidence)
                n_l3 += 1
            else:
                await l4.record_observation(
                    "other", "self", r.statement, score=r.confidence,
                    metadata={"source": "reflector", "kind": r.kind},
                )
                n_l4 += 1
        await self._emit_self_state(n_l3, n_l4, model)

    async def _should_run(self) -> bool:
        """Throttle + only reflect when L1 gained/updated entities since the
        last run. Without this the Reflector re-synthesizes a static L3 every
        tick, flooding L3/L4 with reworded near-duplicates (and partly feeding
        on its own L3 output)."""
        import asyncio

        import hermes_db

        interval = _env_int("REFLECTOR_INTERVAL_S", 600)
        now_mono = asyncio.get_event_loop().time()
        if self._last_run_mono and (now_mono - self._last_run_mono) < interval:
            return False
        async with hermes_db.connection() as conn:
            l1_max = await conn.fetchval("SELECT max(last_seen_at) FROM l1_entities")
        if (
            l1_max is not None
            and self._last_l1_max_seen is not None
            and l1_max <= self._last_l1_max_seen
        ):
            self._last_run_mono = now_mono
            return False
        self._last_run_mono = now_mono
        self._last_l1_max_seen = l1_max
        return True

    async def _build_context(self) -> tuple[str, bool]:
        import hermes_db

        n_entities = _env_int("REFLECTOR_MIN_PATTERNS", 1)
        async with hermes_db.connection() as conn:
            patterns = await conn.fetch(
                "SELECT kind, statement FROM l3_patterns "
                "ORDER BY salience_score DESC, last_seen_at DESC LIMIT 30"
            )
            dist = await conn.fetch(
                "SELECT entity_type, COUNT(*)::int n FROM l1_entities "
                "GROUP BY entity_type ORDER BY n DESC"
            )
            coh = await conn.fetchval(
                "SELECT score FROM l4_observations WHERE kind='coherence' "
                "ORDER BY created_at DESC LIMIT 1"
            )
        if len(patterns) < n_entities:
            return "", False
        lines = ["Patterns the agent has found:"]
        for p in patterns:
            lines.append(f"- ({p['kind']}) {p['statement']}")
        if dist:
            lines.append("\nEntity-type distribution: " +
                         ", ".join(f"{d['entity_type']}={d['n']}" for d in dist))
        if coh is not None:
            lines.append(f"Current self-assessed coherence: {coh:.2f}")
        return "\n".join(lines), True

    async def _emit_self_state(self, n_l3, n_l4, model) -> None:
        from substrate.telemetry import write as telemetry_write

        try:
            await telemetry_write(
                self._substrate,
                agent="reflector",
                event="reflector.synthesized",
                payload={
                    "l3_reflections": n_l3,
                    "l4_notes": n_l4,
                    "model": model,
                },
            )
        except Exception:
            self._log.debug("reflector.telemetry.emit_failed", exc_info=True)


__all__ = ["Reflector", "Reflection", "ReflectorResult", "_coerce", "_synthesize"]
