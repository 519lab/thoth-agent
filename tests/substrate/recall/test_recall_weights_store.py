"""substrate_recall_weights persistence — learned-recall-weights innovation.

PG-backed: save/activate round-trip, the single-active invariant (partial
unique index + demote-on-promote), revert, tolerant decode of corrupt rows,
and the audit-trail listing.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.recall import weights_store
from substrate.recall.replay import Weights


_W1 = Weights(similarity=0.4, keyword=0.4, salience=0.2, recency=0.2,
              half_life_hours=24.0)
_W2 = Weights(similarity=0.4, keyword=0.4, salience=0.5, recency=0.1,
              half_life_hours=6.0)


@pytest_asyncio.fixture
async def booted(thoth_db_initialized):
    sub = await Substrate.boot(
        config=SubstrateConfig(auto_migrate=False, start_subagents=False),
        start_subagents=False,
    )
    try:
        yield sub
    finally:
        await sub.shutdown()


@pytest.mark.asyncio
async def test_save_activate_round_trip(booted):
    import thoth_db

    async with thoth_db.connection() as conn:
        row_id = await weights_store.save(
            conn, weights=_W1, corpus_size=80, train_metric=0.9,
            holdout_metric=0.8, baseline_holdout_metric=0.0, activate=True,
        )
        active = await weights_store.get_active(conn)
    assert row_id
    assert active == _W1


@pytest.mark.asyncio
async def test_promote_demotes_previous_active(booted):
    """At most one active row, ever — promoting W2 demotes W1 instead of
    tripping the partial unique index."""
    import thoth_db

    async with thoth_db.connection() as conn:
        await weights_store.save(conn, weights=_W1, activate=True)
        await weights_store.save(conn, weights=_W2, activate=True)
        active = await weights_store.get_active(conn)
        n_active = await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_recall_weights WHERE active"
        )
        n_total = await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_recall_weights"
        )
    assert active == _W2
    assert n_active == 1
    assert n_total == 2  # history is append-only — demoted, not deleted


@pytest.mark.asyncio
async def test_revert_deactivates(booted):
    import thoth_db

    async with thoth_db.connection() as conn:
        await weights_store.save(conn, weights=_W1, activate=True)
        demoted = await weights_store.deactivate_all(conn)
        active = await weights_store.get_active(conn)
        demoted_again = await weights_store.deactivate_all(conn)
    assert demoted == 1
    assert active is None
    assert demoted_again == 0


@pytest.mark.asyncio
async def test_activate_by_id_and_missing_id(booted):
    import thoth_db

    async with thoth_db.connection() as conn:
        row_id = await weights_store.save(conn, weights=_W1, activate=False)
        assert await weights_store.get_active(conn) is None
        assert await weights_store.activate(conn, row_id) is True
        assert await weights_store.get_active(conn) == _W1
        # A nonexistent id demotes the current active and activates nothing —
        # report False so the CLI can say so.
        import uuid

        assert await weights_store.activate(conn, str(uuid.uuid4())) is False


@pytest.mark.asyncio
async def test_corrupt_row_decodes_to_none(booted):
    """A corrupt weights payload must degrade to the config baseline (None),
    never raise into the recall path."""
    import thoth_db

    async with thoth_db.connection() as conn:
        await conn.execute(
            "INSERT INTO substrate_recall_weights (weights, active) "
            "VALUES ('{\"salience\": \"garbage\"}'::jsonb, TRUE)"
        )
        active = await weights_store.get_active(conn)
    assert active is None


@pytest.mark.asyncio
async def test_history_newest_first_with_evidence(booted):
    import thoth_db

    async with thoth_db.connection() as conn:
        await weights_store.save(
            conn, weights=_W1, corpus_size=50, holdout_metric=0.5,
            baseline_holdout_metric=0.1, activate=False,
        )
        await weights_store.save(
            conn, weights=_W2, corpus_size=80, holdout_metric=0.7,
            baseline_holdout_metric=0.2, activate=True,
        )
        rows = await weights_store.history(conn, limit=10)
    assert len(rows) == 2
    assert rows[0]["weights"] == _W2 and rows[0]["active"] is True
    assert rows[1]["weights"] == _W1 and rows[1]["active"] is False
    assert rows[0]["corpus_size"] == 80
    assert rows[0]["holdout_metric"] == pytest.approx(0.7)
