"""Phase D Parser — the first LLM-driven sub-agent (L0 → L1).

Each tick (default 30s, intensity-dialled), the Parser finds sessions with
enough unconsolidated ``passed`` slices, sends a bounded batch to the
configured auxiliary chat model, and writes the extracted entities +
relationships + citations — then runs the consolidation handshake
(design §5.7) flipping the source slices to ``consolidated`` so the
Curator can release the raw text while its meaning lives on in L1.

Gated by ``THOTH_SUBSTRATE_PARSER`` (default ON; set to 0 to disable): the agent registers
regardless, but its tick is a no-op when the env var is off, so no LLM
calls happen until an operator opts in. Every outcome (ok / empty /
timeout / parse_error / llm_error) is written to ``substrate_parser_log``
and successful ticks emit a ``parser.extracted`` self-state slice.

Per the Phase D spec (2026-05-25-phase-d-l1-parser.md) §4–§5. Note: this
fork stores ``session_id`` in ``substrate_slices.metadata``, not a column,
so session selection groups on ``metadata->>'session_id'``.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING, Optional

from substrate.agents.base import Level, SubAgent
from substrate.l1 import extract, store

if TYPE_CHECKING:  # pragma: no cover
    from substrate.facade import Substrate


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _payload_text(payload, modality: str) -> str:
    """Extract LLM-readable text from a slice payload (text or
    structured-event). Mirrors the recall composer's extraction."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("text"), str):
            return payload["text"]
        import json

        try:
            return json.dumps(payload, sort_keys=True, separators=(",", ":"))[:1000]
        except (TypeError, ValueError):
            return str(payload)
    return str(payload)


class Parser(SubAgent):
    """L0 → L1 extraction sub-agent. Floor intensity LOW (deep-cycle work)."""

    name = "parser"
    is_sentinel = False

    def __init__(self, substrate: "Substrate") -> None:
        super().__init__(substrate)
        self._level = Level.LOW

    async def tick(self) -> None:
        # Master kill-switch + intensity gate. Both checked before any DB
        # work so a disabled Parser costs nothing.
        if not _env_bool("THOTH_SUBSTRATE_PARSER", default=True):
            return
        if self._level is Level.OFF:
            return

        sessions = await self._select_sessions()
        max_sessions = _env_int("PARSER_MAX_SESSIONS_PER_TICK", 4)
        for sid in sessions[:max_sessions]:
            try:
                await self._tick_session(sid)
            except Exception:
                self._log.exception("parser.tick_session.error session=%s", sid)

    # ------------------------------------------------------------------
    # Session selection + batch fetch
    # ------------------------------------------------------------------

    async def _select_sessions(self) -> list[str]:
        import thoth_db

        min_pending = _env_int("PARSER_MIN_PENDING_SLICES", 5)
        limit = _env_int("PARSER_MAX_SESSIONS_PER_TICK", 4)
        async with thoth_db.connection() as conn:
            rows = await conn.fetch(
                """
                SELECT metadata->>'session_id' AS session_id
                  FROM substrate_slices
                 WHERE sentinel_state = 'passed'
                   AND consolidation_state = 'unconsolidated'
                   AND metadata->>'session_id' IS NOT NULL
                   AND payload_modality IN ('text','structured_event')
                   AND ingest_time_world > now() - interval '7 days'
                 GROUP BY metadata->>'session_id'
                HAVING COUNT(*) >= $1
                 ORDER BY MIN(ingest_time_world) ASC
                 LIMIT $2
                """,
                min_pending,
                limit,
            )
        return [r["session_id"] for r in rows]

    async def _fetch_batch(self, session_id: str) -> list[extract.SliceText]:
        import thoth_db

        batch_size = _env_int("PARSER_BATCH_SLICES", 20)
        async with thoth_db.connection() as conn:
            rows = await conn.fetch(
                """
                SELECT sl.slice_id, sl.payload, sl.payload_modality, st.name AS stream_name
                  FROM substrate_slices sl
                  JOIN substrate_streams st ON st.stream_id = sl.stream_id
                 WHERE sl.sentinel_state = 'passed'
                   AND sl.consolidation_state = 'unconsolidated'
                   AND sl.metadata->>'session_id' = $1
                   AND sl.payload_modality IN ('text','structured_event')
                 ORDER BY sl.ingest_time_world DESC
                 LIMIT $2
                """,
                session_id,
                batch_size,
            )
        return [
            extract.SliceText(
                slice_id=r["slice_id"],
                stream_name=r["stream_name"],
                text=_payload_text(r["payload"], r["payload_modality"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Per-session tick
    # ------------------------------------------------------------------

    async def _tick_session(self, session_id: str) -> None:
        import thoth_db

        batch = await self._fetch_batch(session_id)
        if not batch:
            return
        slice_ids = [s.slice_id for s in batch]

        client, model = extract.resolve_parser_client()
        if client is None:
            await self._audit_log(
                "llm_error", session_id, len(batch), 0.0, model="",
                error="no_parser_client",
            )
            return

        started = time.monotonic()
        timeout_s = _env_int("PARSER_TIMEOUT_S", 20)
        try:
            result = await asyncio.wait_for(
                extract.call_parser_llm(
                    batch, client=client, model=model, substrate=self._substrate
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            await self._audit_log("timeout", session_id, len(batch),
                            time.monotonic() - started, model=model, error="timeout")
            return
        except extract.ParseError as exc:
            # Malformed output: still consolidate so we don't re-process the
            # same bad input forever (spec §4.4).
            await store.mark_slices_consolidated(slice_ids, [])
            await self._audit_log("parse_error", session_id, len(batch),
                            time.monotonic() - started, model=model,
                            error=str(exc)[:200], slices_consolidated=len(slice_ids))
            return
        except Exception as exc:
            await self._audit_log("llm_error", session_id, len(batch),
                            time.monotonic() - started, model=model,
                            error=type(exc).__name__)
            return

        if result.is_empty:
            await store.mark_slices_consolidated(slice_ids, [])
            await self._audit_log("empty", session_id, len(batch),
                            time.monotonic() - started, model=model,
                            slices_consolidated=len(slice_ids))
            return

        async with thoth_db.transaction() as conn:
            addresses = await store.persist_extraction(result, conn=conn)
            n = await store.mark_slices_consolidated(slice_ids, addresses, conn=conn)

        await self._emit_self_state(session_id, result, len(batch), model)
        await self._audit_log("ok", session_id, len(batch),
                        time.monotonic() - started, model=model, result=result,
                        slices_consolidated=n)

    # ------------------------------------------------------------------
    # Self-state emission + audit log
    # ------------------------------------------------------------------

    async def _emit_self_state(self, session_id, result, batch_size, model) -> None:
        from substrate.telemetry import write as telemetry_write

        try:
            await telemetry_write(
                self._substrate,
                agent="parser",
                event="parser.extracted",
                payload={
                    "session_id": session_id,
                    "batch_size": batch_size,
                    "entities_emitted": len(result.entities),
                    "relationships_emitted": len(result.relationships),
                    "model": model,
                },
            )
        except Exception:
            self._log.debug("parser.telemetry.emit_failed", exc_info=True)

    async def _audit_log(
        self, outcome, session_id, batch_size, elapsed_s, *,
        model="", error=None, result=None, slices_consolidated=0,
    ) -> None:
        import thoth_db

        ents = len(result.entities) if result else 0
        rels = len(result.relationships) if result else 0
        cites = 0
        if result:
            cites = sum(len(e.source_slice_ids) for e in result.entities) + sum(
                len(r.source_slice_ids) for r in result.relationships
            )
        try:
            async with thoth_db.connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO substrate_parser_log
                        (session_id, batch_size, entities_emitted,
                         relationships_emitted, citations_emitted,
                         slices_consolidated, latency_ms, model, outcome, error)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    """,
                    session_id, batch_size, ents, rels, cites,
                    slices_consolidated, int(elapsed_s * 1000), model or "",
                    outcome, error,
                )
        except Exception:
            self._log.debug("parser.audit_log.failed outcome=%s", outcome, exc_info=True)


__all__ = ["Parser"]
