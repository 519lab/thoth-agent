"""Standalone synchronous embedding-backfill primitive.

Embeds every unembedded *passed* slice on demand, in-process, using the
same ``embed()`` path the Curator's async emit loop uses
(``substrate.agents.curator.embed_backfill_batch``). This is a peer of
the Curator's background backfill — not a replacement — for callers that
need embeddings to land *now* rather than within the async Curator's
``RECALL_EMBEDDING_BACKFILL_INTERVAL_S`` window:

  * The **grading harness** (``eval/context_suite/runner.py``) drives this
    inline between turns so slices minted during grading are embedded
    BEFORE the next turn's recall/prefetch runs. Without it, every slice
    minted during grading kept a NULL embedding, semantic recall matched
    nothing, and the proactive-recall leg of the substrate context engine
    was structurally dead in all four graded rounds. Full write-up:
    ``eval/results/EMBEDDING-GAP-FINDING.md``. This makes the eval mirror
    production, where the Curator + a real embedding provider keep
    coverage near 100%.
  * A future **product change** (embed the hot eviction pointers at, or
    just after, mint) can reuse this same primitive — hence it is kept
    clean and independent of the eval.

Design notes:

  * Async under the hood (``backfill_unembedded_slices``); a sync wrapper
    (``backfill_unembedded_slices_sync``) bridges via ``thoth_db.run_sync``
    for callers on the sync loop, mirroring ``commit_slice`` /
    ``commit_slice_sync``.
  * Idempotent: persistence goes through ``SliceRepo.set_embedding``,
    which only writes under ``embedding IS NULL``. A second pass over the
    same slices embeds nothing and returns 0.
  * Guarded: if no embedding provider resolves (and the mock path is off),
    returns 0 without raising — logged once at debug. A backfill failure
    is never fatal to the caller.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from substrate.facade import Substrate

_log = logging.getLogger("substrate.recall.backfill")

_no_provider_warned = False  # one-shot debug log for the keyless case


async def backfill_unembedded_slices(
    substrate: "Substrate",
    *,
    batch_size: int = 64,
    max_batches: Optional[int] = None,
) -> int:
    """Embed unembedded passed slices in batches; return the count embedded.

    Lists unembedded passed slices (``SliceRepo.list_unembedded``), embeds
    their text via the shared Curator path
    (``curator.embed_backfill_batch`` → ``embed()``), and persists each
    result via ``SliceRepo.set_embedding`` (idempotent under
    ``embedding IS NULL``).

    ``batch_size`` bounds each ``list_unembedded`` / ``embed`` round-trip.
    ``max_batches`` caps the number of rounds (``None`` = drain the whole
    backlog). Returns the total number of slices that got a non-NULL
    embedding written by *this* call.

    Guard: if no embedding provider resolves (and the deterministic mock
    path is off), returns 0 immediately without touching the DB or raising
    — logged once at debug so a keyless environment degrades loudly-once,
    not silently-forever. See ``eval/results/EMBEDDING-GAP-FINDING.md``.
    """
    global _no_provider_warned

    import thoth_db

    # Shared, single embedding-call path — byte-identical to the Curator's
    # async emit loop so mock-vs-real behaviour is the same in eval and prod.
    from substrate.agents.curator import (
        _extract_text_for_embedding,
        embed_backfill_batch,
    )
    from substrate.recall.embeddings import (
        _is_mock_enabled,
        _resolve_embedding_provider,
    )

    # No provider (and not the mock path) → embed() would return all-None
    # for every batch. Short-circuit so we neither read the DB nor spin.
    if not _is_mock_enabled() and _resolve_embedding_provider() is None:
        if not _no_provider_warned:
            _no_provider_warned = True
            _log.debug(
                "backfill_unembedded_slices: no embedding provider configured "
                "and mock off — skipping (0 embedded). Recall stays "
                "keyword-only for slices minted this session."
            )
        return 0

    total = 0
    batches = 0
    while max_batches is None or batches < max_batches:
        async with thoth_db.connection() as conn:
            rows = await substrate.slices.list_unembedded(conn, limit=batch_size)
        if not rows:
            break
        batches += 1

        texts = [_extract_text_for_embedding(r["payload"]) for r in rows]
        try:
            vectors = await embed_backfill_batch(texts)
        except Exception as exc:
            # embed() itself swallows provider errors (returns None per item);
            # a raise here is unexpected. Best-effort: log at debug and stop.
            _log.debug("backfill embed batch raised: %s", exc)
            break

        wrote = 0
        async with thoth_db.connection() as conn:
            async with conn.transaction():
                for row, vec in zip(rows, vectors):
                    if vec is None:
                        continue
                    try:
                        if await substrate.slices.set_embedding(
                            conn, row["slice_id"], vec
                        ):
                            wrote += 1
                    except Exception as exc:
                        _log.debug(
                            "backfill set_embedding for %s failed: %s",
                            row["slice_id"],
                            exc,
                        )
        total += wrote

        # Zero written despite non-empty rows → the provider is failing every
        # call (timeout / dim mismatch / unreachable), or a racing writer
        # already embedded them. Either way the same NULL rows would refetch
        # forever; bail rather than spin (mirrors the CLI's inline backfill).
        if wrote == 0:
            break

    return total


def backfill_unembedded_slices_sync(
    substrate: "Substrate",
    *,
    batch_size: int = 64,
    max_batches: Optional[int] = None,
) -> int:
    """Sync facade for :func:`backfill_unembedded_slices`.

    Bridges via :func:`thoth_db.run_sync`. Must NOT be called from inside a
    running event loop (the underlying ``run_sync`` raises). This is the
    entry point the grading harness uses from its worker thread.
    """
    import thoth_db

    return thoth_db.run_sync(
        backfill_unembedded_slices(
            substrate, batch_size=batch_size, max_batches=max_batches
        )
    )


__all__ = [
    "backfill_unembedded_slices",
    "backfill_unembedded_slices_sync",
]
