"""Summarizer — compress older perception into summary slices.

Long sessions inject raw history; this keeps recent slices verbatim but
folds *older* ones (per session) into a single dense summary slice in a
paired ``hermes.self_action.summary`` stream, citing the originals via
``summary_of`` (MVS §4.7 retrospective summarization). The summary is a
fresh, high-salience perception that carries the originals' meaning
forward; the originals are then faded so the substrate's past compresses
instead of accumulating (automatic summarization of older context).

LLM-driven (mockable ``_summarize`` seam); gated by
``HERMES_SUBSTRATE_SUMMARIZER`` (default ON, like the other cognitive
sub-agents — set to 0 to disable). Conservative: only touches slices
older than ``SUMMARIZER_MIN_AGE_HOURS`` (recent context is never
compressed), never deletes originals (it lowers their salience so the
Curator fades them naturally), and never summarizes the summary stream
itself (no recursion).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from substrate.agents.base import Level, SubAgent

if TYPE_CHECKING:  # pragma: no cover
    from substrate.facade import Substrate


SUMMARY_STREAM = "thoth.self_action.summary"


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


async def _summarize(texts: list[str], *, client, model, substrate=None) -> str:
    """LLM summarization seam (mocked in tests). Returns a dense summary
    that preserves key entities + decisions."""
    from substrate import cost

    joined = "\n".join(f"- {t}" for t in texts)
    prompt = (
        "Summarize the following older messages from one conversation into a "
        "compact note. Preserve key entities (people, projects, files), "
        "decisions, and open threads; drop chit-chat. 2-5 sentences.\n\n"
        f"{joined}"
    )
    resp = await cost.acreate_and_record(
        client,
        substrate=substrate,
        agent="summarizer",
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return (resp.choices[0].message.content or "").strip()


class Summarizer(SubAgent):
    """Retrospective summarization of older perception. Floor LOW."""

    name = "summarizer"
    is_sentinel = False

    def __init__(self, substrate: "Substrate") -> None:
        super().__init__(substrate)
        self._level = Level.LOW
        self._stream_ready = False

    async def tick(self) -> None:
        if not _env_bool("HERMES_SUBSTRATE_SUMMARIZER", default=True):
            return
        if self._level is Level.OFF:
            return

        sessions = await self._sessions_with_old_slices()
        max_sessions = _env_int("SUMMARIZER_MAX_SESSIONS_PER_TICK", 4)
        for sid in sessions[:max_sessions]:
            try:
                await self._summarize_session(sid)
            except Exception:
                self._log.exception("summarizer.session.error session=%s", sid)

    async def _ensure_stream(self):
        """Register the summary stream (idempotent). Returns its record."""
        from substrate.storage import DEFAULT_TEXT_PROFILE, Family, Modality

        stream = await self._substrate.streams.get_by_name(SUMMARY_STREAM)
        if stream is None:
            await self._substrate.streams.register(
                name=SUMMARY_STREAM,
                family=Family.SELF_ACTION,
                modality=Modality.TEXT,
                source="agent",
                organ="summarizer",
                decay_profile_id=DEFAULT_TEXT_PROFILE,
            )
            stream = await self._substrate.streams.get_by_name(SUMMARY_STREAM)
        return stream

    async def _sessions_with_old_slices(self) -> list[str]:
        import hermes_db

        min_age = _env_float("SUMMARIZER_MIN_AGE_HOURS", 24.0)
        min_slices = _env_int("SUMMARIZER_MIN_SLICES", 8)
        async with hermes_db.connection() as conn:
            rows = await conn.fetch(
                """
                SELECT sl.metadata->>'session_id' AS session_id
                  FROM substrate_slices sl
                  JOIN substrate_streams st ON st.stream_id = sl.stream_id
                 WHERE sl.sentinel_state = 'passed'
                   AND sl.consolidation_state <> 'released'
                   AND sl.metadata->>'session_id' IS NOT NULL
                   AND sl.payload_modality = 'text'
                   AND st.name <> $1
                   AND COALESCE((sl.metadata->>'summarized')::bool, false) = false
                   AND sl.event_time_world < now() - ($2 || ' hours')::interval
                 GROUP BY sl.metadata->>'session_id'
                HAVING COUNT(*) >= $3
                 ORDER BY MIN(sl.event_time_world) ASC
                 LIMIT 50
                """,
                SUMMARY_STREAM,
                str(min_age),
                min_slices,
            )
        return [r["session_id"] for r in rows]

    async def _summarize_session(self, session_id: str) -> None:
        import hermes_db
        from substrate.l0.api import commit_slice
        from substrate.storage.types import Address

        min_age = _env_float("SUMMARIZER_MIN_AGE_HOURS", 24.0)
        batch = _env_int("SUMMARIZER_BATCH", 40)
        async with hermes_db.connection() as conn:
            rows = await conn.fetch(
                """
                SELECT sl.slice_id, sl.stream_id, sl.time_start_world,
                       sl.time_end_world, sl.payload, sl.event_time_world
                  FROM substrate_slices sl
                  JOIN substrate_streams st ON st.stream_id = sl.stream_id
                 WHERE sl.sentinel_state = 'passed'
                   AND sl.consolidation_state <> 'released'
                   AND sl.metadata->>'session_id' = $1
                   AND sl.payload_modality = 'text'
                   AND st.name <> $2
                   AND COALESCE((sl.metadata->>'summarized')::bool, false) = false
                   AND sl.event_time_world < now() - ($3 || ' hours')::interval
                 ORDER BY sl.event_time_world ASC
                 LIMIT $4
                """,
                session_id, SUMMARY_STREAM, str(min_age), batch,
            )
        if not rows:
            return

        texts = [_text(r["payload"]) for r in rows]
        texts = [t for t in texts if t]
        if not texts:
            return

        client, model = self._resolve_client()
        if client is None:
            return
        timeout_s = _env_int("SUMMARIZER_TIMEOUT_S", 30)
        try:
            summary = await asyncio.wait_for(
                _summarize(texts, client=client, model=model, substrate=self._substrate),
                timeout=timeout_s,
            )
        except Exception:
            self._log.debug("summarizer.llm.degraded", exc_info=True)
            return
        if not summary:
            return

        stream = await self._ensure_stream()
        if stream is None:
            return
        addresses = [
            Address(r["stream_id"], r["time_start_world"], r["time_end_world"])
            for r in rows
        ]
        now = datetime.now(timezone.utc)
        await commit_slice(
            self._substrate,
            stream_id=stream.stream_id,
            payload=summary,  # TEXT modality → bare string (stored as {"text": …})
            event_time_world=now,
            summary_of=addresses,
            metadata={"session_id": session_id, "summarizes_n": len(rows),
                      "agent": "summarizer"},
            born_passed=True,
        )

        # Mark originals summarized + fade them so the dense summary carries
        # their salience forward and the raw history compresses over time.
        source_decay = _env_float("SUMMARIZER_SOURCE_DECAY", 0.5)
        slice_ids = [r["slice_id"] for r in rows]
        async with hermes_db.connection() as conn:
            await conn.execute(
                """
                UPDATE substrate_slices
                   SET metadata = metadata || '{"summarized": true}'::jsonb,
                       salience_score = salience_score * $2,
                       salience_updated_at = now()
                 WHERE slice_id = ANY($1::uuid[]) AND NOT pinned
                """,
                slice_ids, source_decay,
            )
        await self._emit_self_state(session_id, len(rows))

    @staticmethod
    def _resolve_client():
        from agent.auxiliary_client import get_async_text_auxiliary_client

        return get_async_text_auxiliary_client("summarizer")

    async def _emit_self_state(self, session_id, n) -> None:
        from substrate.telemetry import write as telemetry_write

        try:
            await telemetry_write(
                self._substrate,
                agent="summarizer",
                event="summarizer.compressed",
                payload={"session_id": session_id, "slices_summarized": n},
            )
        except Exception:
            self._log.debug("summarizer.telemetry.emit_failed", exc_info=True)


def _text(payload) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("text"), str):
        return payload["text"]
    return ""


__all__ = ["Summarizer", "SUMMARY_STREAM", "_summarize"]
