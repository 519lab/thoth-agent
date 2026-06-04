"""SliceRepo — the high-volume substrate write surface.

Most methods take an explicit ``asyncpg.Connection`` so the caller
controls transactions. Sentinel batches and force-reject sweeps run
inside ``thoth_db.transaction()`` blocks; the L0 ``commit_slice``
helper passes its own ``conn`` so a Thoth hook can join the caller's
transaction (e.g. ``on_session_start`` shares a txn with the
``sessions`` INSERT in ``SessionDB.create_session``).

The ``commit`` method is here (rather than in ``substrate.l0.api``) so
test code and the Sentinel batch summary can call it directly without
duplicating the prepared INSERT. The public L0 surface lives in
:mod:`substrate.l0.api` and wraps this method with validation +
caller-friendly errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional
from uuid import UUID, uuid4

from substrate.storage.rows import _slice_from_row
from substrate.storage.types import (
    Address,
    Modality,
    SentinelState,
    Slice,
)

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg
    from substrate.recall.projection import RecallCandidate


@dataclass(frozen=True)
class ReleaseRecord:
    """One released slice as returned by ``SliceRepo.release_eligible``.

    Carries the data the Curator's audit emission needs without a
    second SELECT (Phase B spec §6.3). ``salience_at_release`` is the
    salience score at the moment of release decision — useful for
    Reflector/Critic calibration on Curator's threshold choices.
    """

    slice_id: UUID
    stream_id: UUID
    tombstone_policy: str
    salience_at_release: float


class SliceRepo:
    """Reads + writes for ``substrate_slices``.

    Each method declares whether it requires an explicit ``conn`` (txn
    participation) or can acquire from the pool on its own (no txn).
    """

    def __init__(self, pool: "asyncpg.Pool") -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # Write: commit a fresh pending slice.
    # ------------------------------------------------------------------

    async def commit(
        self,
        *,
        conn: "asyncpg.Connection",
        stream_id: UUID,
        time_start_world: datetime,
        time_end_world: datetime,
        event_time_world: datetime,
        perception_time_world: datetime,
        payload: Optional[dict],
        payload_blob_ref: Optional[str],
        payload_modality: Modality,
        metadata: dict,
        summary_of: Optional[list[dict]] = None,
        born_passed: bool = False,
    ) -> tuple[UUID, datetime]:
        """Insert a fresh slice. Default ``sentinel_state='pending'``;
        set ``born_passed=True`` for self-emitted audit slices that
        must NOT re-enter the Sentinel queue.

        Always called from within a caller-supplied transaction; the
        L0 :func:`commit_slice` API acquires/opens that transaction as
        needed. Returns the freshly-allocated ``(slice_id,
        ingest_time_world)`` tuple from ``RETURNING`` so the L0
        wrapper can build an :class:`Address`.

        Notes:

        * ``payload`` is a Python ``dict`` (or ``None`` for
          binary-blob slices). The asyncpg pool's JSONB codec encodes
          dicts directly — *never use ``::jsonb`` casts* (corrupts the
          prepared-statement type cache per Phase 0 ADR).
        * ``salience_score`` defaults to 1.0 on insert; Curator will
          decay it later (Phase B+).
        * ``trust_score`` is NULL while pending; Sentinel sets it on
          decision.
        * ``pending_committed_at`` is set to ``now()`` so the
          partial-index queries (Section 5.2 of the spec) can sort by
          oldest pending. ``born_passed`` rows set it to NULL so they
          are immediately excluded from the pending-queue partial
          index — without that, the Sentinel sees its own audit slices
          on the next tick and emits MORE audits per audit, in an
          unbounded recursion (the 398k-slice production incident on
          2026-05-26 that motivated this kwarg).
        """
        slice_id = uuid4()
        # Clock-skew safety: cap caller-supplied timestamps to PG's
        # ``now()`` inside the INSERT so the
        # ``event <= perception <= ingest`` CHECK constraints hold even
        # when the Thoth host's wall clock drifts a few ms ahead of
        # PG's wall clock. This is the realistic case when Thoth runs
        # in a VM separate from the Postgres container.
        #
        # Formula (let E, P be caller's event_time_world,
        # perception_time_world; now() be PG's wall clock at INSERT):
        #   stored_event      = LEAST(E, now())
        #   stored_perception = LEAST(GREATEST(P, E), now())
        # Walk through:
        #   - E ≤ now, P ≤ now, P ≥ E: stored = (E, P).
        #   - E ≤ now, P > now (skew):  stored = (E, now).
        #   - E > now (skew):           stored = (now, now).
        # In every case stored_event ≤ stored_perception ≤ now (ingest),
        # so the CHECK constraints are satisfied.
        # ``born_passed`` sets sentinel_state directly to 'passed' and
        # NULLs pending_committed_at so the row never matches the
        # ``WHERE sentinel_state='pending'`` partial-index queries the
        # Sentinel uses to find work. Reserved for self-emitted audits
        # (Sentinel/Curator status events on substrate.self_state) and
        # any future agent that writes its own audit trail — using
        # ``born_passed=False`` for those would create an unbounded
        # audit-of-audits recursion. The trust_score column stays NULL:
        # there's no Sentinel decision attached to a born-passed row,
        # so callers/readers should not interpret a non-NULL trust on
        # these rows.
        state_value = "passed" if born_passed else "pending"
        # When born_passed, pending_committed_at must be NULL so the
        # partial indexes (queue depth, oldest pending, list_pending)
        # exclude this row. When pending, set to now() so the indexes
        # can sort by oldest pending.
        pending_committed_clause = "NULL" if born_passed else "now()"
        row = await conn.fetchrow(
            f"""
            INSERT INTO substrate_slices
                (slice_id, stream_id,
                 time_start_world, time_end_world,
                 event_time_world, perception_time_world,
                 payload, payload_blob_ref, payload_modality,
                 sentinel_state, pending_committed_at,
                 salience_score, metadata, summary_of)
            VALUES
                ($1, $2, $3, $4,
                 LEAST($5, now()),
                 LEAST(GREATEST($6, $5), now()),
                 $7, $8, $9,
                 '{state_value}', {pending_committed_clause}, 1.0, $10, $11)
            RETURNING slice_id, ingest_time_world
            """,
            slice_id,
            stream_id,
            time_start_world,
            time_end_world,
            event_time_world,
            perception_time_world,
            payload,
            payload_blob_ref,
            payload_modality.value,
            metadata,
            summary_of,
        )
        # ``RETURNING`` always emits a row on a successful INSERT — assert
        # so a future schema change that drops the columns fails loudly
        # instead of silently returning ``None`` here.
        assert row is not None, "INSERT RETURNING produced no row"
        return row["slice_id"], row["ingest_time_world"]

    # ------------------------------------------------------------------
    # Sentinel / force-reject reads.
    # ------------------------------------------------------------------

    async def list_pending(
        self,
        conn: "asyncpg.Connection",
        *,
        limit: int = 100,
        lock: bool = False,
    ) -> list[Slice]:
        """Return oldest-pending-first slices.

        With ``lock=True``, uses ``FOR UPDATE SKIP LOCKED`` so multiple
        Sentinel workers (Phase B+ horizontal scaling) never decide the
        same row twice. ``SKIP LOCKED`` makes contended rows invisible
        to other workers rather than blocking — exactly the semantics a
        batch-tick worker wants.
        """
        # Perceptual streams only. substrate.* is the substrate's own
        # operational telemetry (it lives in substrate_telemetry, not as
        # slices) — but this JOIN is the airtight guard: even if a future
        # component commits a born-pending slice on a substrate.* stream, the
        # Sentinel never decides it 'passed' and so it can never enter the
        # consolidation backlog. See substrate.storage.streams.is_perceptual.
        sql = """
            SELECT sl.* FROM substrate_slices sl
              JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE sl.sentinel_state = 'pending'
               AND st.name NOT LIKE 'substrate.%'
             ORDER BY sl.pending_committed_at ASC NULLS LAST
             LIMIT $1
        """
        if lock:
            sql += " FOR UPDATE OF sl SKIP LOCKED"
        rows = await conn.fetch(sql, limit)
        return [_slice_from_row(r) for r in rows]

    async def get_by_id(
        self,
        conn: "asyncpg.Connection",
        slice_id: UUID,
    ) -> Optional[Slice]:
        """Return a slice by id, or ``None`` if not present.

        Note: no ``ingest_time_world`` argument — slice_id is globally
        unique (UUIDv4), so PG searches every partition. Cheap in the
        steady state because partial indexes cover the common cases;
        used mostly by tests.
        """
        row = await conn.fetchrow(
            "SELECT * FROM substrate_slices WHERE slice_id = $1",
            slice_id,
        )
        return _slice_from_row(row) if row else None

    # ------------------------------------------------------------------
    # Sentinel decisions.
    # ------------------------------------------------------------------

    async def decide(
        self,
        conn: "asyncpg.Connection",
        slice_id: UUID,
        *,
        outcome: SentinelState,
        trust_score: float,
        reason: Optional[str] = None,
    ) -> None:
        """Atomically transition a slice from PENDING to ``outcome``.

        Raises ``ValueError`` if ``outcome`` is not PASSED or
        QUARANTINED (PENDING is not a legal target — Sentinel only
        moves *away* from pending). Raises ``RuntimeError`` if the slice
        was no longer pending when the UPDATE ran (concurrent decision
        race — caller should treat as benign and skip).
        """
        if outcome not in (SentinelState.PASSED, SentinelState.QUARANTINED):
            raise ValueError(f"invalid sentinel outcome: {outcome!r}")
        tag = await conn.execute(
            """
            UPDATE substrate_slices
               SET sentinel_state       = $1,
                   trust_score          = $2,
                   sentinel_reason      = $3,
                   pending_committed_at = NULL
             WHERE slice_id = $4
               AND sentinel_state = 'pending'
            """,
            outcome.value,
            trust_score,
            reason,
            slice_id,
        )
        # asyncpg returns the command tag as a string like "UPDATE 1".
        # The last token is the affected row count.
        if not tag.endswith(" 1"):
            raise RuntimeError(
                f"slice {slice_id} not pending (UPDATE tag: {tag!r})"
            )

    async def decide_many(
        self,
        conn: "asyncpg.Connection",
        decisions: list[tuple[UUID, SentinelState, float, Optional[str]]],
    ) -> int:
        """Multi-row decide via ``executemany``. ``decisions`` is a list
        of ``(slice_id, outcome, trust_score, reason)`` tuples.

        Returns the count of decision tuples submitted (asyncpg's
        ``executemany`` doesn't return per-row affected counts; callers
        that care about misses re-fetch). Returns 0 for an empty input.
        """
        if not decisions:
            return 0
        for sid, outcome, _trust, _reason in decisions:
            if outcome not in (SentinelState.PASSED, SentinelState.QUARANTINED):
                raise ValueError(
                    f"invalid sentinel outcome for {sid}: {outcome!r}"
                )
        rows = [
            (outcome.value, trust, reason, sid)
            for (sid, outcome, trust, reason) in decisions
        ]
        await conn.executemany(
            """
            UPDATE substrate_slices
               SET sentinel_state       = $1,
                   trust_score          = $2,
                   sentinel_reason      = $3,
                   pending_committed_at = NULL
             WHERE slice_id = $4
               AND sentinel_state = 'pending'
            """,
            rows,
        )
        return len(rows)

    # ------------------------------------------------------------------
    # Force-reject: delete pending slices past their TTL.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Phase B: reinforcement.
    # ------------------------------------------------------------------

    async def reinforce(
        self,
        conn: "asyncpg.Connection",
        slice_id: UUID,
        *,
        bump: Optional[float] = None,
    ) -> None:
        """Bump salience by ``bump`` or by the profile's
        ``reinforcement_bump``. Caps at 1.0.

        Updates ``salience_updated_at`` so subsequent decay starts from
        now. Does NOT update any other field — reinforcement is
        salience-only. The bump is applied SQL-side via
        ``LEAST(1.0, salience + bump)`` so concurrent reinforces don't
        trample each other.

        Reinforcing a released slice is harmless: salience stays at 0
        (already capped) and the timestamp updates. ``consolidation_state``
        is NOT brought back from ``released`` — release is one-way.
        """
        await conn.execute(
            """
            UPDATE substrate_slices sl
               SET salience_score = LEAST(1.0,
                       sl.salience_score + COALESCE($2::real, dp.reinforcement_bump)),
                   salience_updated_at = now()
              FROM substrate_streams        st
              JOIN substrate_decay_profiles dp ON dp.profile_id = st.decay_profile_id
             WHERE sl.slice_id = $1
               AND sl.stream_id = st.stream_id
            """,
            slice_id,
            bump,
        )

    # ------------------------------------------------------------------
    # Phase B: release eligible slices per tombstone policy.
    # ------------------------------------------------------------------

    async def release_eligible(
        self,
        conn: "asyncpg.Connection",
        *,
        limit: int = 200,
    ) -> list[ReleaseRecord]:
        """Release up to ``limit`` slices whose salience has fallen
        below their profile's ``min_salience_to_retain``.

        Three SQL paths per ``tombstone_policy``:
          * ``none`` — DELETE the row entirely. Requires the profile's
            ``tombstone_none_justification`` to be set (the CHECK
            constraint from Phase A migration enforces this; we don't
            need a Python-side check).
          * ``thin`` — NULL ``payload`` + ``payload_blob_ref``, zero
            salience, mark ``consolidation_state='released'``.
          * ``full`` — NULL ``payload`` + ``payload_blob_ref``, mark
            ``consolidation_state='released'`` (keep salience at its
            release-time value).

        The eligibility CTE uses ``FOR UPDATE OF sl SKIP LOCKED`` so
        concurrent Curators don't fight over the same rows.

        Returns ``ReleaseRecord`` per released slice so the caller can
        emit per-release audit slices without a second SELECT.
        """
        eligible = await conn.fetch(
            """
            SELECT sl.slice_id, sl.ingest_time_world, st.stream_id,
                   dp.tombstone_policy, sl.salience_score
              FROM substrate_slices         sl
              JOIN substrate_streams        st ON st.stream_id  = sl.stream_id
              JOIN substrate_decay_profiles dp ON dp.profile_id = st.decay_profile_id
             WHERE sl.sentinel_state      = 'passed'
               AND sl.consolidation_state <> 'released'
               AND NOT sl.pinned
               AND sl.salience_score      < dp.min_salience_to_retain
               AND (NOT dp.release_after_consolidation
                    OR sl.consolidation_state = 'consolidated')
             ORDER BY sl.salience_score ASC
             LIMIT $1
             FOR UPDATE OF sl SKIP LOCKED
            """,
            limit,
        )

        released: list[ReleaseRecord] = []
        for r in eligible:
            policy = r["tombstone_policy"]
            slice_id = r["slice_id"]
            ingest = r["ingest_time_world"]
            if policy == "none":
                await conn.execute(
                    """
                    DELETE FROM substrate_slices
                     WHERE slice_id = $1 AND ingest_time_world = $2
                    """,
                    slice_id,
                    ingest,
                )
            elif policy == "thin":
                await conn.execute(
                    """
                    UPDATE substrate_slices
                       SET payload = NULL,
                           payload_blob_ref = NULL,
                           salience_score = 0,
                           consolidation_state = 'released'
                     WHERE slice_id = $1 AND ingest_time_world = $2
                    """,
                    slice_id,
                    ingest,
                )
            elif policy == "full":
                await conn.execute(
                    """
                    UPDATE substrate_slices
                       SET payload = NULL,
                           payload_blob_ref = NULL,
                           consolidation_state = 'released'
                     WHERE slice_id = $1 AND ingest_time_world = $2
                    """,
                    slice_id,
                    ingest,
                )
            else:  # pragma: no cover — CHECK constraint guards this
                raise ValueError(f"unknown tombstone_policy: {policy!r}")
            released.append(
                ReleaseRecord(
                    slice_id=slice_id,
                    stream_id=r["stream_id"],
                    tombstone_policy=policy,
                    salience_at_release=float(r["salience_score"]),
                )
            )
        return released

    # ------------------------------------------------------------------
    # Phase B: salience pressure read (for inspect CLI).
    # ------------------------------------------------------------------

    async def salience_pressure(
        self,
        conn: "asyncpg.Connection",
        *,
        window_seconds: int = 300,
    ) -> list[dict]:
        """Per-stream salience density + update rate over ``window_seconds``.

        Returns one row per active stream:
          * ``name`` — stream name
          * ``density`` — mean salience across non-released slices
          * ``count`` — number of non-released slices
          * ``update_rate`` — count of salience_updated_at writes in the
             last ``window_seconds`` (proxy for Curator + reinforcement
             activity)

        Surfaced by ``thoth substrate curator pressure``. Phase
        B doesn't consume this programmatically — it's an operator
        observability window into what Phase F's real Conductor will
        eventually read for opportunity-forecast inputs.
        """
        rows = await conn.fetch(
            """
            SELECT st.name AS name,
                   COALESCE(AVG(sl.salience_score), 0)::real AS density,
                   COUNT(sl.slice_id)::int AS count,
                   COUNT(*) FILTER (
                       WHERE sl.salience_updated_at > now() - make_interval(secs => $1)
                   )::int AS update_rate
              FROM substrate_streams st
              LEFT JOIN substrate_slices sl
                     ON sl.stream_id = st.stream_id
                    AND sl.consolidation_state <> 'released'
             WHERE st.lifecycle_state = 'active'
             GROUP BY st.name
             ORDER BY st.name
            """,
            window_seconds,
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Phase C: recall (read-side window + embedding persistence).
    # ------------------------------------------------------------------

    async def recall_window(
        self,
        conn: "asyncpg.Connection",
        *,
        t_now: datetime,
        time_window: "timedelta",
        stream_names: list[str],
        min_salience: float,
        limit: int,
    ) -> "list[RecallCandidate]":
        """Read recent passed slices across the named streams.

        Single statement; PG plans this as a per-partition + per-stream
        scan using ``substrate_slices_stream_time_idx`` from Phase A.
        Returns ``RecallCandidate`` instances with payload decoded
        (text wrappers unwrapped) and the optional ``embedding`` column
        populated (None when the Curator hasn't backfilled yet).

        Filters:
          * ``stream_name = ANY($1)`` — caller restricts to one or more
            registered streams
          * ``event_time_world IN (t_now - window, t_now]`` — what the
            agent perceived in this experiential window
          * ``sentinel_state = 'passed'`` — never surface pending or
            quarantined slices to recall
          * ``consolidation_state <> 'released'`` — tombstoned slices
            stay invisible to the projection
          * ``salience_score >= min_salience`` — drop trivially-decayed
            rows before they bloat the candidate set

        Order: ``salience DESC, event_time DESC LIMIT limit``. The
        ranker (``substrate.recall.projection.rank_candidates``) reorders
        with the composite score downstream; the SQL ordering is just a
        principled way to keep the pool of candidates focused.
        """
        # Local import — keep the module-level import set lean and avoid
        # the recall package depending on storage at import time.
        from substrate.recall.projection import RecallCandidate

        rows = await conn.fetch(
            """
            SELECT sl.slice_id, sl.stream_id, st.name AS stream_name,
                   sl.payload, sl.event_time_world,
                   sl.time_start_world, sl.time_end_world,
                   sl.salience_score, sl.trust_score, sl.metadata,
                   sl.embedding
              FROM substrate_slices  sl
              JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE st.name = ANY($1::text[])
               AND sl.event_time_world > $2::timestamptz - $3::interval
               AND sl.event_time_world <= $2::timestamptz
               AND sl.sentinel_state = 'passed'
               AND sl.consolidation_state <> 'released'
               AND sl.salience_score >= $4
             ORDER BY sl.salience_score DESC, sl.event_time_world DESC
             LIMIT $5
            """,
            stream_names,
            t_now,
            time_window,
            min_salience,
            limit,
        )
        candidates: list[RecallCandidate] = []
        for r in rows:
            payload = r["payload"]
            # Unwrap text-modality payloads from {"text": "..."} → "..."
            # so the composer doesn't see a json blob it has to dig into.
            # Structured / signal payloads pass through as dicts.
            if isinstance(payload, dict) and set(payload.keys()) == {"text"} and isinstance(
                payload.get("text"), str
            ):
                payload = payload["text"]
            candidates.append(
                RecallCandidate(
                    slice_id=r["slice_id"],
                    address=Address(
                        stream_id=r["stream_id"],
                        time_start_world=r["time_start_world"],
                        time_end_world=r["time_end_world"],
                    ),
                    stream_name=r["stream_name"],
                    payload=payload,
                    event_time_world=r["event_time_world"],
                    salience_score=float(r["salience_score"]),
                    trust_score=(
                        float(r["trust_score"]) if r["trust_score"] is not None else None
                    ),
                    metadata=dict(r["metadata"] or {}),
                    embedding=(
                        list(r["embedding"]) if r["embedding"] is not None else None
                    ),
                )
            )
        return candidates

    async def set_embedding(
        self,
        conn: "asyncpg.Connection",
        slice_id: UUID,
        embedding: list[float],
    ) -> bool:
        """Persist an embedding for a slice. Idempotent under concurrent
        writers via the ``embedding IS NULL`` predicate — the second
        writer's UPDATE matches no rows and returns ``False``.

        Returns ``True`` if the row was written (this caller "won" the
        race), ``False`` if the slice already had an embedding or the
        slice wasn't found.
        """
        tag = await conn.execute(
            """
            UPDATE substrate_slices
               SET embedding = $2::vector
             WHERE slice_id = $1
               AND embedding IS NULL
            """,
            slice_id,
            embedding,
        )
        # asyncpg returns "UPDATE N" — wrote a row iff trailing 1.
        return tag.endswith(" 1")

    async def list_unembedded(
        self,
        conn: "asyncpg.Connection",
        *,
        limit: int,
    ) -> list[dict]:
        """Return up to ``limit`` rows that need embeddings.

        Filters to ``passed`` (Sentinel-approved), unembedded (column
        IS NULL), and not already marked failed (``metadata->>'embedding_failed'``
        not set). Newest-first so freshly-committed slices get embeddings
        promptly. Returns plain dicts (``slice_id``, ``payload``) — the
        Curator's emit loop only needs those two fields.
        """
        rows = await conn.fetch(
            """
            SELECT slice_id, payload
              FROM substrate_slices
             WHERE embedding IS NULL
               AND sentinel_state = 'passed'
               AND (metadata->>'embedding_failed') IS DISTINCT FROM 'true'
             ORDER BY ingest_time_world DESC
             LIMIT $1
            """,
            limit,
        )
        return [{"slice_id": r["slice_id"], "payload": r["payload"]} for r in rows]

    async def mark_embedding_failed(
        self,
        conn: "asyncpg.Connection",
        slice_id: UUID,
    ) -> None:
        """Persist ``metadata.embedding_failed = true`` for a slice that
        has exhausted its retry budget (Phase C spec §5.7 #5). The
        ``list_unembedded`` filter excludes such rows so the Curator
        stops re-trying.
        """
        await conn.execute(
            """
            UPDATE substrate_slices
               SET metadata = metadata || jsonb_build_object('embedding_failed', true)
             WHERE slice_id = $1
            """,
            slice_id,
        )

    async def count_embedding_failed(self, conn: "asyncpg.Connection") -> int:
        """How many slices are currently parked as ``embedding_failed``."""
        return await conn.fetchval(
            """
            SELECT count(*) FROM substrate_slices
             WHERE (metadata->>'embedding_failed') = 'true'
            """
        ) or 0

    async def reset_embedding_failed(
        self,
        conn: "asyncpg.Connection",
        *,
        limit: Optional[int] = None,
    ) -> int:
        """Clear the ``embedding_failed`` marker so parked slices re-enter the
        ``list_unembedded`` queue and get another embedding attempt.

        The marker is sticky-by-design (a slice that exhausts its retry budget
        is excluded forever so the Curator stops hammering a broken provider).
        But once the operator fixes the cause — a dim mismatch, an unreachable
        endpoint, a wrong model name — nothing un-parks the backlog. This is
        that escape hatch: ``thoth embed retry-failed`` clears all of them for
        immediate recovery; the Curator's auto-heal pass clears a small batch
        per long interval so a fixed config self-heals without intervention.

        ``limit`` bounds how many are cleared (newest-first); ``None`` clears
        every parked slice. Returns the number cleared.
        """
        if limit is None:
            row = await conn.fetchval(
                """
                WITH cleared AS (
                    UPDATE substrate_slices
                       SET metadata = metadata - 'embedding_failed'
                     WHERE (metadata->>'embedding_failed') = 'true'
                    RETURNING 1
                )
                SELECT count(*) FROM cleared
                """
            )
            return row or 0
        row = await conn.fetchval(
            """
            WITH targets AS (
                SELECT slice_id FROM substrate_slices
                 WHERE (metadata->>'embedding_failed') = 'true'
                 ORDER BY ingest_time_world DESC
                 LIMIT $1
            ), cleared AS (
                UPDATE substrate_slices s
                   SET metadata = s.metadata - 'embedding_failed'
                  FROM targets t
                 WHERE s.slice_id = t.slice_id
                RETURNING 1
            )
            SELECT count(*) FROM cleared
            """,
            limit,
        )
        return row or 0

    async def recall_stats(
        self,
        conn: "asyncpg.Connection",
        *,
        window: "timedelta",
    ) -> dict:
        """Aggregations for the inspect CLI.

        Returns:
          * total_calls / non_empty_calls / timed_out_calls / error_calls
            (over the window)
          * avg_duration_ms / avg_tokens / avg_candidates / avg_composed
          * total_slices / embedded_slices / unembedded_backlog
          * semantic_path_count / keyword_path_count / mixed_path_count
            (over the window — from substrate_recall_log.metadata)
        """
        # Recall-log aggregations.
        log_row = await conn.fetchrow(
            """
            SELECT
              COUNT(*)::int AS total_calls,
              COUNT(*) FILTER (WHERE composed_count > 0)::int AS non_empty_calls,
              COUNT(*) FILTER (WHERE timed_out)::int AS timed_out_calls,
              COUNT(*) FILTER (WHERE error_text IS NOT NULL)::int AS error_calls,
              COALESCE(AVG(duration_ms)::int, 0) AS avg_duration_ms,
              COALESCE(AVG(tokens_used)::int, 0) AS avg_tokens,
              COALESCE(AVG(candidates_count)::int, 0) AS avg_candidates,
              COALESCE(AVG(composed_count)::int, 0) AS avg_composed,
              COUNT(*) FILTER (
                  WHERE metadata->>'embedding_path' = 'semantic'
              )::int AS semantic_path_count,
              COUNT(*) FILTER (
                  WHERE metadata->>'embedding_path' = 'keyword'
              )::int AS keyword_path_count,
              COUNT(*) FILTER (
                  WHERE metadata->>'embedding_path' = 'mixed'
              )::int AS mixed_path_count
              FROM substrate_recall_log
             WHERE requested_at > now() - $1::interval
            """,
            window,
        )
        # Embedding-coverage aggregations.
        emb_row = await conn.fetchrow(
            """
            SELECT
              COUNT(*)::int AS total_slices,
              COUNT(*) FILTER (WHERE embedding IS NOT NULL)::int AS embedded_slices,
              COUNT(*) FILTER (
                  WHERE embedding IS NULL
                    AND sentinel_state = 'passed'
                    AND (metadata->>'embedding_failed') IS DISTINCT FROM 'true'
              )::int AS unembedded_backlog
              FROM substrate_slices
            """
        )
        return {**dict(log_row or {}), **dict(emb_row or {})}

    # ------------------------------------------------------------------
    # Force-reject: delete pending slices past their TTL.
    # ------------------------------------------------------------------

    async def force_reject_expired(
        self,
        conn: "asyncpg.Connection",
        *,
        limit: int = 100,
    ) -> list[Slice]:
        """Delete pending slices that have been pending longer than
        their stream's decay-profile ``pending_ttl``.

        Returns the deleted slices (via ``DELETE … RETURNING``) so the
        force-reject worker can emit an audit slice for each. The
        partition-key columns (``slice_id``, ``ingest_time_world``) are
        in the DELETE predicate so PG prunes to a single partition per
        matched row.

        Uses ``FOR UPDATE OF sl SKIP LOCKED`` in the CTE so concurrent
        Sentinel workers don't lose rows to a race.
        """
        rows = await conn.fetch(
            """
            WITH expired AS (
                SELECT sl.slice_id, sl.ingest_time_world
                  FROM substrate_slices         sl
                  JOIN substrate_streams        st ON st.stream_id   = sl.stream_id
                  JOIN substrate_decay_profiles dp ON dp.profile_id  = st.decay_profile_id
                 WHERE sl.sentinel_state = 'pending'
                   AND sl.pending_committed_at + dp.pending_ttl < now()
                 ORDER BY sl.pending_committed_at ASC
                 LIMIT $1
                 FOR UPDATE OF sl SKIP LOCKED
            )
            DELETE FROM substrate_slices sl
             USING expired e
             WHERE sl.slice_id          = e.slice_id
               AND sl.ingest_time_world = e.ingest_time_world
            RETURNING sl.*
            """,
            limit,
        )
        return [_slice_from_row(r) for r in rows]
