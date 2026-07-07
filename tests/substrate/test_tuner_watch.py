"""RecallTunerWatch — report-only auto-tune visibility (issue #288).

Contract under test: the daily tick loads the labeled corpus, runs the
pure tuner fit, and writes exactly one ``recall_tuner.report`` telemetry
row with the verdict — never a ``substrate_recall_weights`` row (promotion
stays human-gated via ``thoth substrate recall tune --apply``).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.agents import RecallTunerWatch


@pytest_asyncio.fixture
async def substrate(thoth_db_initialized):
    import thoth_db

    return Substrate.from_pool(thoth_db.pool())


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _seed_labeled_recall(
    *,
    outcome: float,
    n_candidates: int = 3,
    requested_at: datetime | None = None,
) -> None:
    """Insert one labeled recall_log row with per-candidate metadata in the
    exact shape the recall pipeline logs (and replay decodes)."""
    import thoth_db
    from uuid import uuid4

    at = requested_at or _now_utc()
    candidates = [
        {
            "slice_id": str(uuid4()),
            "salience": 0.5,
            "event_time": (at - timedelta(hours=i)).isoformat(),
            "relevance": 0.6 - 0.1 * i,
            "path": "semantic",
        }
        for i in range(n_candidates)
    ]
    async with thoth_db.connection() as conn:
        await conn.execute(
            """
            INSERT INTO substrate_recall_log
                (requested_at, session_id, query_excerpt, candidates_count,
                 composed_count, tokens_used, duration_ms, timed_out,
                 metadata, outcome_score)
            VALUES ($1, $2, 'q', $3, $3, 100, 50, FALSE, $4, $5)
            """,
            at,
            f"sess-{uuid4().hex[:6]}",
            n_candidates,
            {"candidates": candidates},
            outcome,
        )


async def _report_rows():
    import thoth_db

    async with thoth_db.connection() as conn:
        return await conn.fetch(
            "SELECT payload FROM substrate_telemetry WHERE event = 'recall_tuner.report'"
        )


def _payload(row) -> dict:
    raw = row["payload"]
    return raw if isinstance(raw, dict) else json.loads(raw)


@pytest.mark.asyncio
async def test_tick_writes_one_report_row(substrate, monkeypatch):
    monkeypatch.setenv("THOTH_SUBSTRATE_TUNER_WATCH", "1")
    for i in range(6):
        await _seed_labeled_recall(
            outcome=1.0 if i % 2 else 0.0,
            requested_at=_now_utc() - timedelta(minutes=i),
        )

    await RecallTunerWatch(substrate).tick()

    rows = await _report_rows()
    assert len(rows) == 1
    payload = _payload(rows[0])
    assert payload["corpus_size"] == 6
    assert isinstance(payload["guardrails"], list)
    # 6 labels is far below the 50 minimum — must not recommend.
    assert payload["recommend"] is False
    assert any("corpus too small" in g for g in payload["guardrails"])
    assert "baseline" in payload and "best" in payload


@pytest.mark.asyncio
async def test_tick_never_writes_weights(substrate, monkeypatch):
    """Report-only contract: substrate_recall_weights stays empty no matter
    what the fit concludes."""
    import thoth_db

    monkeypatch.setenv("THOTH_SUBSTRATE_TUNER_WATCH", "1")
    for i in range(6):
        await _seed_labeled_recall(
            outcome=float(i % 2),
            requested_at=_now_utc() - timedelta(minutes=i),
        )

    await RecallTunerWatch(substrate).tick()

    async with thoth_db.connection() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM substrate_recall_weights")
    assert n == 0


@pytest.mark.asyncio
async def test_tick_gated_by_env(substrate, monkeypatch):
    monkeypatch.setenv("THOTH_SUBSTRATE_TUNER_WATCH", "0")
    await _seed_labeled_recall(outcome=1.0)

    await RecallTunerWatch(substrate).tick()
    assert await _report_rows() == []


@pytest.mark.asyncio
async def test_tick_noop_when_intensity_off(substrate, monkeypatch):
    from substrate.agents.base import Level

    monkeypatch.setenv("THOTH_SUBSTRATE_TUNER_WATCH", "1")
    await _seed_labeled_recall(outcome=1.0)

    watch = RecallTunerWatch(substrate)
    watch.set_intensity(Level.OFF)
    await watch.tick()
    assert await _report_rows() == []


@pytest.mark.asyncio
async def test_tick_handles_empty_corpus(substrate, monkeypatch):
    """No labeled rows at all — still reports (corpus_size=0, guardrails),
    doesn't raise."""
    monkeypatch.setenv("THOTH_SUBSTRATE_TUNER_WATCH", "1")

    await RecallTunerWatch(substrate).tick()

    rows = await _report_rows()
    assert len(rows) == 1
    payload = _payload(rows[0])
    assert payload["corpus_size"] == 0
    assert payload["recommend"] is False
