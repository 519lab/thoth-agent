"""write_recall_outcome + load_labeled_recalls — innovation #1 (pg).

DB-backed: exercises the ``(session_id, requested_at)`` windowed UPDATE, its
idempotency (the ``outcome_score IS NULL`` guard), the session/time scoping, and
the replay loader's round-trip over the per-candidate metadata record.

NOTE: pg-backed — NOT run by the harness's pure-test gate (a Postgres on 5433
may be a live DB). Run only against a disposable test database via the normal
test runner.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.recall.log import RecallLogRow


@pytest_asyncio.fixture
async def booted_substrate(thoth_db_initialized):
    sub = await Substrate.boot(
        config=SubstrateConfig(auto_migrate=False, start_subagents=False),
        start_subagents=False,
    )
    try:
        yield sub
    finally:
        await sub.shutdown()


async def _insert_recall_row(
    conn,
    *,
    session_id: str,
    requested_at: datetime,
    candidates: list | None = None,
) -> int:
    """Insert one recall_log row directly (bypassing the async writer) and
    return its log_id. ``candidates`` goes into metadata under the key the
    replay loader reads."""
    metadata = {"candidates": candidates or []}
    return await conn.fetchval(
        """
        INSERT INTO substrate_recall_log
            (requested_at, session_id, query_excerpt, candidates_count,
             composed_count, tokens_used, duration_ms, timed_out,
             error_text, metadata)
        VALUES ($1, $2, 'q', 1, 1, 10, 5, FALSE, NULL, $3)
        RETURNING log_id
        """,
        requested_at,
        session_id,
        metadata,
    )


@pytest.mark.asyncio
async def test_windowed_update_labels_turn_rows(booted_substrate):
    """The windowed UPDATE labels every unlabelled recall for the session at
    or after the turn start, and leaves earlier / other-session rows NULL."""
    import thoth_db
    from agent.turn_outcome import write_recall_outcome

    turn_start = datetime.now(timezone.utc)
    before = turn_start - timedelta(minutes=5)
    after = turn_start + timedelta(seconds=1)

    async with thoth_db.connection() as conn:
        id_before = await _insert_recall_row(
            conn, session_id="sess-A", requested_at=before
        )
        id_in_window = await _insert_recall_row(
            conn, session_id="sess-A", requested_at=after
        )
        id_other_session = await _insert_recall_row(
            conn, session_id="sess-B", requested_at=after
        )

    await write_recall_outcome(
        booted_substrate,
        session_id="sess-A",
        turn_started_at=turn_start,
        outcome_score=0.75,
    )

    async with thoth_db.connection() as conn:
        rows = {
            r["log_id"]: r["outcome_score"]
            for r in await conn.fetch(
                "SELECT log_id, outcome_score FROM substrate_recall_log "
                "WHERE log_id = ANY($1)",
                [id_before, id_in_window, id_other_session],
            )
        }

    assert rows[id_in_window] == pytest.approx(0.75)
    assert rows[id_before] is None  # before the turn window
    assert rows[id_other_session] is None  # different session


@pytest.mark.asyncio
async def test_update_is_idempotent(booted_substrate):
    """The ``outcome_score IS NULL`` guard makes a second write a no-op — a
    later turn whose window overlaps an already-labelled row must not clobber
    it."""
    import thoth_db
    from agent.turn_outcome import write_recall_outcome

    turn_start = datetime.now(timezone.utc)
    async with thoth_db.connection() as conn:
        log_id = await _insert_recall_row(
            conn,
            session_id="sess-idem",
            requested_at=turn_start + timedelta(seconds=1),
        )

    await write_recall_outcome(
        booted_substrate,
        session_id="sess-idem",
        turn_started_at=turn_start,
        outcome_score=1.0,
    )
    # Second write with a different score + an earlier window must not overwrite.
    await write_recall_outcome(
        booted_substrate,
        session_id="sess-idem",
        turn_started_at=turn_start - timedelta(minutes=1),
        outcome_score=0.0,
    )

    async with thoth_db.connection() as conn:
        score = await conn.fetchval(
            "SELECT outcome_score FROM substrate_recall_log WHERE log_id = $1",
            log_id,
        )
    assert score == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_no_session_id_is_noop(booted_substrate):
    """A missing session id can't scope the UPDATE → no-op, no error."""
    import thoth_db
    from agent.turn_outcome import write_recall_outcome

    turn_start = datetime.now(timezone.utc)
    async with thoth_db.connection() as conn:
        log_id = await _insert_recall_row(
            conn,
            session_id="sess-none",
            requested_at=turn_start + timedelta(seconds=1),
        )

    await write_recall_outcome(
        booted_substrate,
        session_id=None,
        turn_started_at=turn_start,
        outcome_score=1.0,
    )

    async with thoth_db.connection() as conn:
        score = await conn.fetchval(
            "SELECT outcome_score FROM substrate_recall_log WHERE log_id = $1",
            log_id,
        )
    assert score is None


@pytest.mark.asyncio
async def test_load_labeled_recalls_round_trips_candidates(booted_substrate):
    """The replay loader pulls labelled rows + decodes per-candidate metadata,
    dropping unlabelled rows and rows without a candidate record."""
    import thoth_db
    from substrate.recall.replay import load_labeled_recalls

    now = datetime.now(timezone.utc)
    cand = [
        {
            "slice_id": "11111111-1111-1111-1111-111111111111",
            "salience": 0.4,
            "event_time": now.isoformat(),
            "relevance": 0.8,
            "path": "semantic",
        }
    ]
    async with thoth_db.connection() as conn:
        labeled_id = await _insert_recall_row(
            conn, session_id="sess-load", requested_at=now, candidates=cand
        )
        # Unlabelled → excluded.
        await _insert_recall_row(
            conn, session_id="sess-load", requested_at=now, candidates=cand
        )
        # No candidate record → excluded even once labelled.
        no_cand_id = await _insert_recall_row(
            conn, session_id="sess-load", requested_at=now, candidates=[]
        )
        await conn.execute(
            "UPDATE substrate_recall_log SET outcome_score = 1.0 "
            "WHERE log_id = ANY($1)",
            [labeled_id, no_cand_id],
        )

    async with thoth_db.connection() as conn:
        corpus = await load_labeled_recalls(conn, limit=50)

    by_id = {r.log_id: r for r in corpus}
    assert labeled_id in by_id
    assert no_cand_id not in by_id  # labelled but no candidates → dropped
    rec = by_id[labeled_id]
    assert rec.outcome_score == pytest.approx(1.0)
    assert len(rec.candidates) == 1
    assert rec.candidates[0].relevance == pytest.approx(0.8)
    assert rec.candidates[0].path == "semantic"


@pytest.mark.asyncio
async def test_recall_logs_per_candidate_metadata(booted_substrate):
    """End-to-end: a real recall() call stamps the per-candidate ranking inputs
    into metadata['candidates'] so the replay harness has something to load."""
    import asyncio

    import thoth_db
    from substrate.l0 import commit_slice
    from substrate.recall import recall

    stream = await booted_substrate.streams.get_by_name(
        "thoth.world.user_message.cli"
    )
    await commit_slice(
        booted_substrate,
        stream.stream_id,
        "windowed update keystone",
        event_time_world=datetime.now(timezone.utc),
    )
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE substrate_slices SET sentinel_state='passed', "
            "trust_score=0.95, pending_committed_at=NULL "
            "WHERE sentinel_state='pending'"
        )

    proj = await recall(
        booted_substrate, "windowed update keystone", session_id="sess-meta"
    )
    assert proj is not None

    row = None
    for _ in range(40):
        await asyncio.sleep(0.1)
        async with thoth_db.connection() as conn:
            row = await conn.fetchrow(
                "SELECT metadata FROM substrate_recall_log "
                "WHERE session_id = 'sess-meta' ORDER BY log_id DESC LIMIT 1"
            )
        if row is not None:
            break
    assert row is not None
    import json

    meta = row["metadata"]
    if isinstance(meta, (str, bytes)):
        meta = json.loads(meta)
    assert "candidates" in meta
    assert isinstance(meta["candidates"], list)
    if meta["candidates"]:
        entry = meta["candidates"][0]
        assert {"slice_id", "salience", "event_time", "relevance", "path"} <= set(
            entry
        )
