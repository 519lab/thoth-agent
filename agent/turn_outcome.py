"""Per-turn outcome labelling — innovation #1 (recall-replay eval harness).

Two halves, deliberately split so the scoring logic is pure and unit-testable
independent of any DB:

  - :func:`compute_outcome_score` — pure: turns the live per-turn signals
    (``completed`` / ``failed`` / ``interrupted`` + the tool-call counters)
    into a single ``outcome_score`` in [0, 1]. Reused by #2 (skill efficacy).

  - :func:`write_recall_outcome` — async: stamps that score onto the
    ``substrate_recall_log`` rows the turn consumed, via a
    ``(session_id, requested_at)`` windowed UPDATE. The recall log writer is
    fire-and-forget and returns no ``log_id`` (see ``substrate/recall/log.py``),
    so we can't carry a row handle out of the recall call — we correlate by the
    turn-start timestamp captured atop ``run_conversation`` instead. The
    ``outcome_score IS NULL`` guard makes the write idempotent: re-running it
    for the same turn (or a later turn whose window overlaps an already-labelled
    row) is a no-op.

Both halves are called from the post-turn block in
``agent/conversation_loop.py`` (and the parallel ``agent/codex_runtime.py``
path) as best-effort work — the caller swallows any error, the recall hot path
is untouched.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


def compute_outcome_score(
    *,
    completed: bool,
    failed: bool,
    interrupted: bool,
    tool_calls: int = 0,
    tool_failures: int = 0,
    tool_failure_penalty: float = 0.5,
) -> float:
    """Collapse the live per-turn signals into a recall outcome label.

    v1 proxy (spec §#1): a turn that finished cleanly is a 1.0; a turn that
    failed or was interrupted, or never completed, is a 0.0. From there we
    dock ``tool_failure_penalty * (tool_failures / tool_calls)`` so a turn
    that technically "completed" but flailed through a string of failing tool
    calls scores below a clean one. Clamped to [0, 1].

    This is a *proxy*, not ground truth — "no user re-ask" (the strongest
    success signal) is cross-turn and deferred to a later retroactive
    downgrade pass. Keep it pure so #2 (skill efficacy) can reuse it and so
    it stays trivially unit-testable.
    """
    base = 1.0 if (completed and not failed and not interrupted) else 0.0
    if base > 0.0 and tool_calls > 0 and tool_failures > 0:
        ratio = tool_failures / max(1, tool_calls)
        base -= tool_failure_penalty * ratio
    # Clamp into [0, 1].
    if base < 0.0:
        return 0.0
    if base > 1.0:
        return 1.0
    return base


async def write_recall_outcome(
    substrate,
    *,
    session_id: Optional[str],
    turn_started_at: datetime,
    outcome_score: float,
) -> None:
    """Label every recall the turn consumed with ``outcome_score``.

    Correlate-and-update by ``(session_id, requested_at)``: any recall_log row
    for this session whose ``requested_at`` is at or after the turn's start and
    is still unlabelled gets the score. The ``outcome_score IS NULL`` guard
    keeps it idempotent.

    Returns ``None`` — the recall log writer assigns no ``log_id`` we could
    hand back, and the caller doesn't need one. Raises nothing the caller has
    to handle beyond the best-effort swallow it already does, but we also guard
    internally so a missing session or DB hiccup degrades to a no-op rather than
    bubbling into the post-turn block.

    Gated by ``THOTH_RECALL_OUTCOME_LABEL`` at the call site (kill-switch);
    this function does the write unconditionally when called.
    """
    if not session_id:
        # Without a session id we can't scope the windowed UPDATE — every
        # recall row is keyed by session. Nothing to label.
        return

    # Late import: keep this module importable without the DB pool (the pure
    # half above is used by tests that never touch Postgres).
    import thoth_db

    try:
        async with thoth_db.transaction() as conn:
            await conn.execute(
                """
                UPDATE substrate_recall_log
                   SET outcome_score = $1
                 WHERE session_id = $2
                   AND requested_at >= $3
                   AND outcome_score IS NULL
                """,
                outcome_score,
                session_id,
                turn_started_at,
            )
    except Exception as exc:
        # Best-effort: labelling is observability for the offline replay
        # harness, never load-bearing for the turn itself.
        logger.debug("recall outcome write failed: %s", exc)


__all__ = ["compute_outcome_score", "write_recall_outcome"]
