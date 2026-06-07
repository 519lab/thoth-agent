"""L0 public write API — :func:`commit_slice` (async) + sync facade.

This is the **only** public write surface in Phase A. Sentinel + the
force-reject worker read from ``SliceRepo`` directly because they're
internal substrate machinery; everything outside the substrate package
calls into ``commit_slice``.

Contract (from Phase A spec §4.2):

* Returns the slice's :class:`Address` once the PG INSERT is
  acknowledged. Does NOT block waiting for Sentinel.
* Validates stream lifecycle (must be ``ACTIVE``), modality vs payload
  shape, event-time skew (≤ now + 5 min), and TZ-awareness of
  ``event_time_world``.
* Reads ``payload_modality`` from the stream — the caller does not pass
  it (the stream owns its modality contract).
* If ``conn`` is passed, the INSERT runs on that connection so the
  caller can wrap it in a transaction shared with e.g.
  ``messages.append_message``. Otherwise the function acquires a
  connection from ``thoth_db.pool()`` for the duration of the call.
* Never uses ``::jsonb`` casts (the pool's JSONB codec handles
  encode/decode, per the Phase 0 ADR).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional, Union
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg
    from substrate.facade import Substrate

from substrate.storage.types import Address, Lifecycle, Modality

# Maximum allowed skew between caller-provided ``event_time_world`` and
# the PG server's wall clock. 5 minutes is generous enough to absorb
# realistic NTP-drift / VM-pause / cron-batch-emit skew while still
# catching obvious bugs (year-2000 timestamps, naive datetime → far
# future after a UTC offset misread).
_MAX_EVENT_TIME_SKEW = timedelta(minutes=5)


async def commit_slice(
    substrate: "Substrate",
    stream_id: UUID,
    payload: Optional[Union[str, dict]],
    *,
    event_time_world: datetime,
    perception_time_world: Optional[datetime] = None,
    summary_of: Optional[list[Address]] = None,
    trust_hint: Optional[float] = None,
    metadata: Optional[dict] = None,
    payload_blob_ref: Optional[str] = None,
    conn: "Optional[asyncpg.Connection]" = None,
    born_passed: bool = False,
    born_consolidated: bool = False,
) -> Address:
    """Commit a slice durably. Default ``sentinel_state='pending'``;
    set ``born_passed=True`` for self-emitted audit slices that must
    bypass the Sentinel queue (Sentinel/Curator self-state events).

    Set ``born_consolidated=True`` for session-less perceptual events
    the Parser can never select (it groups by ``metadata->>'session_id'``)
    — e.g. ``thoth.self_state.cron_dispatch``. They stay perceptual but
    are born ``consolidation_state='consolidated'`` so they don't pile up
    in an undrainable parse backlog. See :meth:`SliceRepo.commit`.

    See module docstring for the full contract.

    ``trust_hint`` is accepted for forward-compatibility with Phase B+
    Sentinel logic but is ignored in Phase A — the stub Sentinel
    computes trust from the stream's modality.

    ``born_passed=True`` is mandatory for any agent that writes its own
    audit-trail slices on ``substrate.self_state`` (or any other stream
    its own ``tick()`` will subsequently process). Without it, the
    Sentinel sees the audit slice on its next tick, decides it, emits
    a new audit slice ABOUT the previous audit, and that audit becomes
    pending — unbounded recursion. The 2026-05-26 production incident
    that motivated this kwarg had the stub Sentinel emit 398,014
    sentinel_batch_decision audit slices about itself in ~12 hours.
    """
    # ---- 1. Validate inputs that don't need DB access ------------------
    if event_time_world.tzinfo is None:
        raise TypeError(
            "event_time_world must be timezone-aware (UTC); got naive datetime"
        )
    now = datetime.now(timezone.utc)
    if event_time_world > now + _MAX_EVENT_TIME_SKEW:
        raise ValueError(
            f"clock skew: event_time_world={event_time_world.isoformat()} "
            f"is more than {_MAX_EVENT_TIME_SKEW} ahead of wall-clock now"
        )

    if perception_time_world is None:
        perception_time_world = now
    elif perception_time_world.tzinfo is None:
        raise TypeError(
            "perception_time_world must be timezone-aware (UTC); got naive datetime"
        )

    # ---- 2. Look up the stream + validate lifecycle ----------------------
    stream = await substrate.streams.get(stream_id, conn=conn)
    if stream is None:
        raise ValueError(f"unknown stream_id: {stream_id}")
    if stream.lifecycle_state is not Lifecycle.ACTIVE:
        raise ValueError(
            f"stream {stream.name!r} lifecycle = "
            f"{stream.lifecycle_state.value!r}, not 'active'"
        )

    # ---- 3. Normalise the payload per modality --------------------------
    modality = stream.modality
    normalised_payload: Optional[dict]
    if modality is Modality.TEXT:
        if not isinstance(payload, str):
            raise TypeError(
                f"stream {stream.name!r} is TEXT modality but "
                f"payload is {type(payload).__name__}, expected str"
            )
        # Uniform retrieval shape — every modality lands in JSONB as a
        # dict so downstream Reflector/Curator code doesn't branch on
        # type. ``{"text": "..."}`` is the agreed convention.
        normalised_payload = {"text": payload}
    elif modality is Modality.STRUCTURED_EVENT:
        if not isinstance(payload, dict):
            raise TypeError(
                f"stream {stream.name!r} is STRUCTURED_EVENT modality but "
                f"payload is {type(payload).__name__}, expected dict"
            )
        normalised_payload = payload
    elif modality is Modality.BINARY_BLOB:
        if payload is not None:
            raise TypeError(
                f"stream {stream.name!r} is BINARY_BLOB modality; payload "
                f"must be None and payload_blob_ref must be set"
            )
        if not payload_blob_ref:
            raise TypeError(
                f"stream {stream.name!r} is BINARY_BLOB modality but "
                f"payload_blob_ref is missing"
            )
        normalised_payload = None
    elif modality is Modality.SIGNAL:
        if not isinstance(payload, dict):
            raise TypeError(
                f"stream {stream.name!r} is SIGNAL modality but "
                f"payload is {type(payload).__name__}, expected dict"
            )
        normalised_payload = payload
    else:  # pragma: no cover — enum exhaustion guard
        raise TypeError(f"unsupported modality: {modality!r}")

    # ---- 4. Build the summary_of JSONB shape -----------------------------
    summary_of_json: Optional[list[dict]] = None
    if summary_of:
        summary_of_json = [
            {
                "stream_id": str(addr.stream_id),
                "t_start": addr.time_start_world.isoformat(),
                "t_end": addr.time_end_world.isoformat(),
            }
            for addr in summary_of
        ]

    # ---- 5. INSERT ------------------------------------------------------
    # ``time_start_world`` / ``time_end_world`` are equal to
    # ``event_time_world`` for instantaneous events; spans are deferred
    # to a future ``commit_span`` variant.
    meta = metadata or {}
    if conn is not None:
        slice_id, _ingest = await substrate.slices.commit(
            conn=conn,
            stream_id=stream_id,
            time_start_world=event_time_world,
            time_end_world=event_time_world,
            event_time_world=event_time_world,
            perception_time_world=perception_time_world,
            payload=normalised_payload,
            payload_blob_ref=payload_blob_ref,
            payload_modality=modality,
            metadata=meta,
            summary_of=summary_of_json,
            born_passed=born_passed,
            born_consolidated=born_consolidated,
        )
    else:
        async with substrate.pool.acquire() as own_conn:
            slice_id, _ingest = await substrate.slices.commit(
                conn=own_conn,
                stream_id=stream_id,
                time_start_world=event_time_world,
                time_end_world=event_time_world,
                event_time_world=event_time_world,
                perception_time_world=perception_time_world,
                payload=normalised_payload,
                payload_blob_ref=payload_blob_ref,
                payload_modality=modality,
                metadata=meta,
                summary_of=summary_of_json,
                born_passed=born_passed,
                born_consolidated=born_consolidated,
            )

    return Address(
        stream_id=stream_id,
        time_start_world=event_time_world,
        time_end_world=event_time_world,
    )


def commit_slice_sync(
    substrate: "Substrate",
    stream_id: UUID,
    payload: Optional[Union[str, dict]],
    *,
    event_time_world: datetime,
    perception_time_world: Optional[datetime] = None,
    summary_of: Optional[list[Address]] = None,
    trust_hint: Optional[float] = None,
    metadata: Optional[dict] = None,
    payload_blob_ref: Optional[str] = None,
    born_consolidated: bool = False,
) -> Address:
    """Sync facade — bridges to the async ``commit_slice`` via
    :func:`thoth_db.run_sync`.

    Must NOT be called from inside a running event loop. The Phase 0
    ``run_sync`` helper raises ``RuntimeError`` in that case
    (preventing nested-loop deadlocks); this facade just propagates.

    Used by sync entry points (cron job dispatch, legacy CLI paths).
    Async callers should ``await commit_slice(...)`` directly.
    """
    # Imported here, not at module top, so this module can be imported
    # in environments where thoth_db isn't loaded yet (e.g. doc-only
    # tooling that just wants to read the function signatures).
    import thoth_db

    return thoth_db.run_sync(
        commit_slice(
            substrate,
            stream_id,
            payload,
            event_time_world=event_time_world,
            perception_time_world=perception_time_world,
            summary_of=summary_of,
            trust_hint=trust_hint,
            metadata=metadata,
            payload_blob_ref=payload_blob_ref,
            born_consolidated=born_consolidated,
        )
    )


# ---------------------------------------------------------------------------
# Phase B: reinforcement API. Public way for any caller (recall API in
# Phase C, consolidation handshake in Phase D, manual operator nudges) to
# bump a slice's salience.
# ---------------------------------------------------------------------------


async def reinforce_slice(
    substrate: "Substrate",
    slice_id: UUID,
    *,
    bump: Optional[float] = None,
    conn: "Optional[asyncpg.Connection]" = None,
) -> None:
    """Reinforce ``slice_id`` — bump salience and update timestamp.

    ``bump=None`` uses the slice's decay-profile ``reinforcement_bump``
    value (the default reinforcement contract). An explicit ``bump``
    overrides — used when a caller has stronger signal than the default
    (e.g. a recall hit that was directly relevant gets a larger bump
    than one that was returned but irrelevant).

    Salience is capped at 1.0 SQL-side. Reinforcing a slice that's
    already at the cap is a harmless no-op for the score but DOES
    update ``salience_updated_at`` so subsequent decay starts from now.

    If ``conn`` is passed, the UPDATE runs on that connection so the
    caller can wrap reinforcement into a larger transaction (e.g. the
    consolidation acknowledgment in Phase D).
    """
    if conn is not None:
        await substrate.slices.reinforce(conn, slice_id, bump=bump)
        return
    async with substrate.pool.acquire() as own:
        await substrate.slices.reinforce(own, slice_id, bump=bump)


def reinforce_slice_sync(
    substrate: "Substrate",
    slice_id: UUID,
    *,
    bump: Optional[float] = None,
) -> None:
    """Sync facade for :func:`reinforce_slice`. Bridges via
    :func:`thoth_db.run_sync`. Must NOT be called from inside a
    running event loop (the underlying ``run_sync`` raises).
    """
    import thoth_db

    return thoth_db.run_sync(
        reinforce_slice(substrate, slice_id, bump=bump)
    )


async def set_slice_pinned(
    slice_id: UUID, pinned: bool, *, conn: "Optional[asyncpg.Connection]" = None
) -> bool:
    """Pin / unpin a slice. A pinned slice is exempt from Curator decay +
    release (the "never forget this" control). Pinning also lifts salience
    to 1.0 so the slice surfaces in recall and won't be sitting near the
    release threshold if later unpinned. Returns True if a row changed."""
    import thoth_db

    sql = (
        "UPDATE substrate_slices "
        "   SET pinned = $2, "
        "       salience_score = CASE WHEN $2 THEN 1.0 ELSE salience_score END, "
        "       salience_updated_at = now() "
        " WHERE slice_id = $1"
    )

    async def _go(c):
        tag = await c.execute(sql, slice_id, pinned)
        return tag.split()[-1] != "0"

    if conn is not None:
        return await _go(conn)
    async with thoth_db.connection() as own:
        return await _go(own)


async def forget_slice(
    slice_id: UUID, *, conn: "Optional[asyncpg.Connection]" = None
) -> bool:
    """Forget a slice: drop its salience to 0 and unpin it, so the
    Curator releases it (per its decay-profile tombstone policy) on its
    next cycle. Returns True if a row changed."""
    import thoth_db

    sql = (
        "UPDATE substrate_slices "
        "   SET salience_score = 0, pinned = FALSE, salience_updated_at = now() "
        " WHERE slice_id = $1 AND consolidation_state <> 'released'"
    )

    async def _go(c):
        tag = await c.execute(sql, slice_id)
        return tag.split()[-1] != "0"

    if conn is not None:
        return await _go(conn)
    async with thoth_db.connection() as own:
        return await _go(own)


__all__ = [
    "commit_slice",
    "commit_slice_sync",
    "reinforce_slice",
    "reinforce_slice_sync",
    "set_slice_pinned",
    "forget_slice",
]
