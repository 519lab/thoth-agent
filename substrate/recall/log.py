"""Recall log writer — Phase C Task 8 / spec §5.5.

Background asyncio writer for the ``substrate_recall_log`` observability
table. Bounded queue: when full, the OLDEST row is silently dropped
(observability is best-effort; the recall hot path must never block on
the log writer). A periodic warning surfaces the drop count.

Lifecycle is owned by ``Substrate.boot/shutdown`` via the
``recall_log`` attribute. Tests can construct the writer directly via
``RecallLogWriter(substrate)``; ``start()`` spawns the drain task,
``stop()`` cancels it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from substrate.facade import Substrate


_DRAIN_INTERVAL_S = 1.0
_DRAIN_BATCH_SIZE = 100


@dataclass(frozen=True)
class RecallLogRow:
    """One row to insert into ``substrate_recall_log``."""

    requested_at: datetime
    session_id: Optional[str]
    query_excerpt: str
    candidates_count: int
    composed_count: int
    tokens_used: int
    duration_ms: int
    timed_out: bool
    error_text: Optional[str] = None
    metadata: dict = field(default_factory=dict)


class RecallLogWriter:
    """Background async writer with a bounded queue.

    Drops the OLDEST entry when the queue is full so the most-recent
    activity stays visible. Dropping the newest would mean the inspect
    CLI's recent-sample query shows stale data after a burst.
    """

    def __init__(
        self,
        substrate: "Substrate",
        max_queue_depth: int = 1024,
    ) -> None:
        self._substrate = substrate
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_depth)
        self._stopped = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._drop_count = 0
        self._log = logging.getLogger("substrate.recall.log")

    def enqueue(self, row: RecallLogRow) -> None:
        """Queue a row for background insertion. Non-blocking.

        On queue-full, evicts the oldest queued row and inserts the new
        one. ``drop_count`` is incremented; every 100 drops a WARNING
        surfaces (avoids log spam under sustained pressure)."""
        try:
            self._queue.put_nowait(row)
            return
        except asyncio.QueueFull:
            pass
        # Try to make room by dropping the oldest. If that fails (queue
        # was drained between checks), retry the put once.
        try:
            self._queue.get_nowait()
            self._drop_count += 1
            if self._drop_count % 100 == 1:
                self._log.warning(
                    "recall log queue full; dropped %d row(s) total",
                    self._drop_count,
                )
        except asyncio.QueueEmpty:
            pass
        try:
            self._queue.put_nowait(row)
        except asyncio.QueueFull:
            # Highly unlikely; drop this row too.
            self._drop_count += 1

    @property
    def drop_count(self) -> int:
        """How many rows have been dropped since process start."""
        return self._drop_count

    @property
    def queue_size(self) -> int:
        """Current queue depth — useful for tests."""
        return self._queue.qsize()

    def start(self) -> asyncio.Task:
        """Spawn the drain loop. Idempotent — re-calling returns the
        existing task."""
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(
            self._drain_loop(), name="substrate-recall-log-writer"
        )
        return self._task

    async def stop(self) -> None:
        """Cancel the drain task and wait for it to finish. In-flight
        queue contents are dropped — the substrate shutdown budget is
        bounded so we don't synchronously flush."""
        self._stopped.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await self._task
        self._task = None

    async def _drain_loop(self) -> None:
        """Sleep, drain up to ``_DRAIN_BATCH_SIZE`` rows, INSERT them.

        Exceptions inside the loop are logged + swallowed; observability
        misfires must not crash the substrate. The drain interval is
        fixed at 1 second — small enough that operator-facing latency
        stays within "this is a live system" feel, large enough to
        amortise the asyncpg roundtrip across many recall calls.
        """
        # Late import — keep this module light at import time.
        import thoth_db

        while not self._stopped.is_set():
            try:
                await asyncio.sleep(_DRAIN_INTERVAL_S)
            except asyncio.CancelledError:
                return

            batch: list[RecallLogRow] = []
            while batch.__len__() < _DRAIN_BATCH_SIZE:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if not batch:
                continue

            try:
                async with thoth_db.transaction() as conn:
                    await conn.executemany(
                        """
                        INSERT INTO substrate_recall_log
                            (requested_at, session_id, query_excerpt,
                             candidates_count, composed_count, tokens_used,
                             duration_ms, timed_out, error_text, metadata)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                        """,
                        [
                            (
                                r.requested_at,
                                r.session_id,
                                r.query_excerpt,
                                r.candidates_count,
                                r.composed_count,
                                r.tokens_used,
                                r.duration_ms,
                                r.timed_out,
                                r.error_text,
                                r.metadata,
                            )
                            for r in batch
                        ],
                    )
            except Exception as exc:
                self._log.warning(
                    "recall log drain failed; %d row(s) lost: %s",
                    len(batch),
                    exc,
                )


__all__ = ["RecallLogRow", "RecallLogWriter"]
