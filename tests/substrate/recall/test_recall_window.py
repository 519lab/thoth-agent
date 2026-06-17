"""SliceRepo.recall_window — Phase C Task 4 / spec §9.2.

Tests the time-windowed, multi-stream, salience-floor SQL the recall
pipeline depends on. Each test seeds slices via ``commit_slice`` (the
public write path), then queries via ``recall_window`` and asserts the
returned shape.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.l0 import commit_slice
from substrate.recall.projection import RecallCandidate
from substrate.storage import (
    DEFAULT_STRUCTURED_PROFILE,
    DEFAULT_TEXT_PROFILE,
    Family,
    Modality,
)


@pytest_asyncio.fixture
async def substrate(thoth_db_initialized):
    import thoth_db

    return Substrate.from_pool(thoth_db.pool())


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _pass_all_pending(substrate) -> None:
    """Helper: flip every pending slice to ``passed`` so it's eligible
    for recall. The real Sentinel does this on a tick; tests bypass."""
    import thoth_db

    async with thoth_db.connection() as conn:
        await conn.execute(
            """
            UPDATE substrate_slices
               SET sentinel_state = 'passed',
                   trust_score = 0.95,
                   pending_committed_at = NULL
             WHERE sentinel_state = 'pending'
            """
        )


@pytest.mark.asyncio
async def test_recall_window_orders_by_salience_then_recency(substrate):
    """Higher salience wins on equal recency; more recent wins on equal salience."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.recall_order",
        family=Family.SELF_STATE,
        modality=Modality.TEXT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    t = _now_utc()
    # Three slices: same recency (within 1 sec), different salience.
    await commit_slice(substrate, stream.stream_id, "low salience", event_time_world=t)
    await commit_slice(substrate, stream.stream_id, "high salience", event_time_world=t)
    await commit_slice(substrate, stream.stream_id, "mid salience", event_time_world=t)
    await _pass_all_pending(substrate)

    # Bump salience scores by hand: high → 0.9, mid → 0.6, low stays at 1.0 default...
    # Reset everything to known scores so the ordering check is deterministic.
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE substrate_slices SET salience_score = 0.3 WHERE payload->>'text' = 'low salience'"
        )
        await conn.execute(
            "UPDATE substrate_slices SET salience_score = 0.9 WHERE payload->>'text' = 'high salience'"
        )
        await conn.execute(
            "UPDATE substrate_slices SET salience_score = 0.6 WHERE payload->>'text' = 'mid salience'"
        )

        candidates = await substrate.slices.recall_window(
            conn,
            t_now=t + timedelta(seconds=1),
            time_window=timedelta(hours=1),
            stream_names=[stream.name],
            min_salience=0.0,
            limit=10,
        )
    payloads = [c.payload for c in candidates]
    assert payloads == ["high salience", "mid salience", "low salience"]


@pytest.mark.asyncio
async def test_recall_window_unwraps_text_payload(substrate):
    """Text-modality payloads stored as ``{"text": "..."}`` arrive as bare strings."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.recall_unwrap",
        family=Family.SELF_STATE,
        modality=Modality.TEXT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    t = _now_utc()
    await commit_slice(substrate, stream.stream_id, "hello world", event_time_world=t)
    await _pass_all_pending(substrate)

    async with thoth_db.connection() as conn:
        candidates = await substrate.slices.recall_window(
            conn,
            t_now=t + timedelta(seconds=1),
            time_window=timedelta(hours=1),
            stream_names=[stream.name],
            min_salience=0.0,
            limit=10,
        )
    assert len(candidates) == 1
    assert candidates[0].payload == "hello world"
    assert isinstance(candidates[0].payload, str)


@pytest.mark.asyncio
async def test_recall_window_preserves_structured_payload(substrate):
    """Structured-event payloads stay as dicts (no auto-unwrap)."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.recall_structured",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )
    t = _now_utc()
    await commit_slice(
        substrate, stream.stream_id, {"foo": "bar", "n": 1}, event_time_world=t
    )
    await _pass_all_pending(substrate)

    async with thoth_db.connection() as conn:
        candidates = await substrate.slices.recall_window(
            conn,
            t_now=t + timedelta(seconds=1),
            time_window=timedelta(hours=1),
            stream_names=[stream.name],
            min_salience=0.0,
            limit=10,
        )
    assert len(candidates) == 1
    assert candidates[0].payload == {"foo": "bar", "n": 1}


@pytest.mark.asyncio
async def test_recall_window_respects_time_window(substrate):
    """Slices outside the window are excluded."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.recall_window_filter",
        family=Family.SELF_STATE,
        modality=Modality.TEXT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    t = _now_utc()
    await commit_slice(substrate, stream.stream_id, "recent", event_time_world=t)
    # Old slice — back-date via direct UPDATE since commit_slice rejects
    # future skew but accepts the past freely.
    await commit_slice(
        substrate,
        stream.stream_id,
        "old",
        event_time_world=t - timedelta(hours=48),
    )
    await _pass_all_pending(substrate)

    async with thoth_db.connection() as conn:
        candidates = await substrate.slices.recall_window(
            conn,
            t_now=t + timedelta(seconds=1),
            time_window=timedelta(hours=24),  # 24h window excludes the 48h-old slice
            stream_names=[stream.name],
            min_salience=0.0,
            limit=10,
        )
    payloads = {c.payload for c in candidates}
    assert "recent" in payloads
    assert "old" not in payloads


@pytest.mark.asyncio
async def test_recall_window_excludes_released_and_pending(substrate):
    """Released + pending + quarantined slices are all excluded."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.recall_state_filter",
        family=Family.SELF_STATE,
        modality=Modality.TEXT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    t = _now_utc()
    await commit_slice(substrate, stream.stream_id, "passed", event_time_world=t)
    await commit_slice(substrate, stream.stream_id, "released", event_time_world=t)
    await commit_slice(substrate, stream.stream_id, "quarantined", event_time_world=t)
    # Leave one as 'pending' — don't pass it.
    await commit_slice(substrate, stream.stream_id, "pending", event_time_world=t)
    await _pass_all_pending(substrate)

    async with thoth_db.connection() as conn:
        # Flip the "released" one back from passed → released.
        await conn.execute(
            """
            UPDATE substrate_slices
               SET consolidation_state = 'released', payload = NULL
             WHERE payload->>'text' = 'released'
            """
        )
        await conn.execute(
            """
            UPDATE substrate_slices
               SET sentinel_state = 'quarantined'
             WHERE payload->>'text' = 'quarantined'
            """
        )
        # Re-mark the "pending" one (the helper passed it).
        await conn.execute(
            """
            UPDATE substrate_slices
               SET sentinel_state = 'pending', trust_score = NULL
             WHERE payload->>'text' = 'pending'
            """
        )
        candidates = await substrate.slices.recall_window(
            conn,
            t_now=t + timedelta(seconds=1),
            time_window=timedelta(hours=1),
            stream_names=[stream.name],
            min_salience=0.0,
            limit=10,
        )
    payloads = {c.payload for c in candidates}
    assert payloads == {"passed"}


@pytest.mark.asyncio
async def test_recall_window_respects_min_salience(substrate):
    """Slices below ``min_salience`` are excluded."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.recall_minsal",
        family=Family.SELF_STATE,
        modality=Modality.TEXT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    t = _now_utc()
    await commit_slice(substrate, stream.stream_id, "bright", event_time_world=t)
    await commit_slice(substrate, stream.stream_id, "dim", event_time_world=t)
    await _pass_all_pending(substrate)

    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE substrate_slices SET salience_score = 0.9 WHERE payload->>'text' = 'bright'"
        )
        await conn.execute(
            "UPDATE substrate_slices SET salience_score = 0.01 WHERE payload->>'text' = 'dim'"
        )
        candidates = await substrate.slices.recall_window(
            conn,
            t_now=t + timedelta(seconds=1),
            time_window=timedelta(hours=1),
            stream_names=[stream.name],
            min_salience=0.05,
            limit=10,
        )
    payloads = {c.payload for c in candidates}
    assert payloads == {"bright"}


@pytest.mark.asyncio
async def test_recall_window_excludes_named_session(substrate):
    """``exclude_session_id`` drops slices from that session; other sessions
    and session-less slices (NULL id) are kept. NULL exclude → no filtering."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.recall_session_filter",
        family=Family.SELF_STATE,
        modality=Modality.TEXT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    t = _now_utc()
    await commit_slice(substrate, stream.stream_id, "live-1", event_time_world=t)
    await commit_slice(substrate, stream.stream_id, "live-2", event_time_world=t)
    await commit_slice(substrate, stream.stream_id, "other-session", event_time_world=t)
    await commit_slice(substrate, stream.stream_id, "session-less", event_time_world=t)
    await _pass_all_pending(substrate)

    async with thoth_db.connection() as conn:
        # Tag sessions in metadata (no dedicated column — recall groups on
        # metadata->>'session_id'). "session-less" deliberately left untagged.
        await conn.execute(
            "UPDATE substrate_slices "
            "SET metadata = COALESCE(metadata,'{}'::jsonb) || '{\"session_id\":\"live\"}'::jsonb "
            "WHERE payload->>'text' IN ('live-1','live-2')"
        )
        await conn.execute(
            "UPDATE substrate_slices "
            "SET metadata = COALESCE(metadata,'{}'::jsonb) || '{\"session_id\":\"other\"}'::jsonb "
            "WHERE payload->>'text' = 'other-session'"
        )

        kwargs = dict(
            t_now=t + timedelta(seconds=1),
            time_window=timedelta(hours=1),
            stream_names=[stream.name],
            min_salience=0.0,
            limit=10,
        )
        excluded = await substrate.slices.recall_window(
            conn, exclude_session_id="live", **kwargs
        )
        unfiltered = await substrate.slices.recall_window(
            conn, exclude_session_id=None, **kwargs
        )

    excluded_payloads = {c.payload for c in excluded}
    # The live session's slices are gone; other session + session-less survive.
    assert "live-1" not in excluded_payloads
    assert "live-2" not in excluded_payloads
    assert "other-session" in excluded_payloads
    assert "session-less" in excluded_payloads
    # NULL exclude_session_id disables the filter — everything comes back.
    assert {c.payload for c in unfiltered} == {
        "live-1", "live-2", "other-session", "session-less"
    }


@pytest.mark.asyncio
async def test_recall_window_returns_embedding_when_present(substrate):
    """When ``embedding`` is populated the candidate carries it as a 1536-d list."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.recall_embedding",
        family=Family.SELF_STATE,
        modality=Modality.TEXT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    t = _now_utc()
    await commit_slice(substrate, stream.stream_id, "with embedding", event_time_world=t)
    await commit_slice(substrate, stream.stream_id, "no embedding", event_time_world=t)
    await _pass_all_pending(substrate)

    embedding = [0.1] * 1536

    async with thoth_db.connection() as conn:
        # Set the embedding for one slice via set_embedding.
        rows = await conn.fetch(
            "SELECT slice_id FROM substrate_slices WHERE payload->>'text' = 'with embedding'"
        )
        await substrate.slices.set_embedding(conn, rows[0]["slice_id"], embedding)

        candidates = await substrate.slices.recall_window(
            conn,
            t_now=t + timedelta(seconds=1),
            time_window=timedelta(hours=1),
            stream_names=[stream.name],
            min_salience=0.0,
            limit=10,
        )
    by_payload = {c.payload: c for c in candidates}
    assert by_payload["with embedding"].embedding is not None
    assert len(by_payload["with embedding"].embedding) == 1536
    assert by_payload["with embedding"].embedding[0] == pytest.approx(0.1, abs=1e-5)
    assert by_payload["no embedding"].embedding is None


@pytest.mark.asyncio
async def test_set_embedding_idempotent_under_concurrent_writers(substrate):
    """The ``embedding IS NULL`` predicate makes set_embedding a no-op
    on the second call — preventing a slow writer from clobbering a
    fast writer's result."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.set_embed_idempotent",
        family=Family.SELF_STATE,
        modality=Modality.TEXT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    t = _now_utc()
    await commit_slice(substrate, stream.stream_id, "to embed", event_time_world=t)
    await _pass_all_pending(substrate)

    first = [0.1] * 1536
    second = [0.9] * 1536

    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            "SELECT slice_id FROM substrate_slices WHERE payload->>'text' = 'to embed'"
        )
        wrote_first = await substrate.slices.set_embedding(conn, row["slice_id"], first)
        wrote_second = await substrate.slices.set_embedding(conn, row["slice_id"], second)
        stored = await conn.fetchval(
            "SELECT embedding FROM substrate_slices WHERE slice_id = $1",
            row["slice_id"],
        )
    assert wrote_first is True
    assert wrote_second is False
    # Stored value matches the first writer's vector.
    assert stored[0] == pytest.approx(0.1, abs=1e-5)


@pytest.mark.asyncio
async def test_list_unembedded_orders_newest_first(substrate):
    """``list_unembedded`` returns passed + unembedded slices, newest-first,
    and excludes ones marked ``embedding_failed=true``."""
    import thoth_db

    stream = await substrate.streams.register(
        name="hermes.test.list_unembedded",
        family=Family.SELF_STATE,
        modality=Modality.TEXT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    t = _now_utc()
    await commit_slice(substrate, stream.stream_id, "first", event_time_world=t - timedelta(seconds=10))
    await commit_slice(substrate, stream.stream_id, "second", event_time_world=t - timedelta(seconds=5))
    await commit_slice(substrate, stream.stream_id, "third", event_time_world=t)
    await commit_slice(substrate, stream.stream_id, "failed", event_time_world=t)
    await _pass_all_pending(substrate)

    async with thoth_db.connection() as conn:
        # Mark one as failed.
        failed_row = await conn.fetchrow(
            "SELECT slice_id FROM substrate_slices WHERE payload->>'text' = 'failed'"
        )
        await substrate.slices.mark_embedding_failed(conn, failed_row["slice_id"])

        rows = await substrate.slices.list_unembedded(conn, limit=10)
    # Filter to only the rows we inserted (other tests may have left rows behind).
    payloads = [
        (r["payload"].get("text") if isinstance(r["payload"], dict) else None)
        for r in rows
    ]
    for needle in ("first", "second", "third"):
        assert needle in payloads
    assert "failed" not in payloads
