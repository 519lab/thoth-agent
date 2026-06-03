"""Reinforce-on-hit — Phase C Task 7 / spec §9.5."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.l0 import commit_slice
from substrate.recall import recall
from substrate.recall import api as recall_api
from substrate.storage import DEFAULT_TEXT_PROFILE, Family, Modality


@pytest.fixture(autouse=True)
def _enable_mock_embeddings(monkeypatch):
    from substrate.recall import embeddings

    monkeypatch.setenv(embeddings.MOCK_ENV_VAR, "1")
    embeddings.reset_client_cache()


@pytest.fixture(autouse=True)
def _reset_reinforce_lru():
    """Each test starts with a clean LRU so rate-limit assertions are
    deterministic."""
    recall_api._REINFORCE_LRU.clear()
    yield
    recall_api._REINFORCE_LRU.clear()


@pytest_asyncio.fixture
async def booted_substrate(hermes_db_initialized):
    sub = await Substrate.boot(
        config=SubstrateConfig(auto_migrate=False, start_subagents=False),
        start_subagents=False,
    )
    try:
        yield sub
    finally:
        await sub.shutdown()


async def _seed_passed_slice(substrate, *, text: str, t_now: datetime, salience: float = 0.5):
    import thoth_db

    stream = await substrate.streams.get_by_name("thoth.world.user_message.cli")
    addr = await commit_slice(
        substrate, stream.stream_id, text, event_time_world=t_now
    )
    # Only target THIS slice by payload — guards against multiple
    # pending slices in the test fixture racing for the same UPDATE.
    async with thoth_db.connection() as conn:
        await conn.execute(
            """
            UPDATE substrate_slices
               SET sentinel_state = 'passed',
                   trust_score = 0.95,
                   pending_committed_at = NULL,
                   salience_score = $2
             WHERE payload->>'text' = $1
               AND sentinel_state = 'pending'
            """,
            text,
            salience,
        )
    return addr


async def _get_salience(substrate, *, text: str) -> float:
    import thoth_db

    async with thoth_db.connection() as conn:
        return float(
            await conn.fetchval(
                "SELECT salience_score FROM substrate_slices WHERE payload->>'text' = $1",
                text,
            )
        )


@pytest.mark.asyncio
async def test_recall_bumps_salience_of_composed_slices(booted_substrate):
    """After recall(...), composed slices have their salience bumped."""
    t = datetime.now(timezone.utc)
    await _seed_passed_slice(booted_substrate, text="bumpable", t_now=t, salience=0.3)

    before = await _get_salience(booted_substrate, text="bumpable")
    proj = await recall(booted_substrate, "bumpable")
    assert len(proj.composed) >= 1
    after = await _get_salience(booted_substrate, text="bumpable")
    assert after > before


@pytest.mark.asyncio
async def test_recall_rate_limits_per_slice(booted_substrate, monkeypatch):
    """8 recall calls hitting the same slice within 60s → at most 6 bumps
    (default RECALL_REINFORCE_RATE_LIMIT_PER_MIN)."""
    from substrate import config as _cfg

    monkeypatch.setattr(_cfg, "RECALL_REINFORCE_RATE_LIMIT_PER_MIN", 6)

    t = datetime.now(timezone.utc)
    # salience above the default min_salience=0.05 so the candidate
    # makes it through recall_window AND the salience-bump cap (1.0)
    # doesn't immediately throttle the salience field.
    await _seed_passed_slice(
        booted_substrate, text="rate_limited", t_now=t, salience=0.3
    )

    # Track recall calls that actually composed the slice.
    call_count = 0
    for _ in range(8):
        proj = await recall(booted_substrate, "rate_limited")
        if proj.composed:
            call_count += 1
    assert call_count == 8
    # But only 6 bumps actually fired (LRU cap).
    sid = None
    for k, v in recall_api._REINFORCE_LRU.items():
        if len(v) > 0:
            sid = k
            break
    assert sid is not None
    assert len(recall_api._REINFORCE_LRU[sid]) <= 6


@pytest.mark.asyncio
async def test_recall_reinforce_failure_does_not_raise(booted_substrate, monkeypatch):
    """If reinforce_slice raises, recall() still returns the projection."""
    from substrate.l0 import api as l0_api

    async def _broken_reinforce(*args, **kwargs):
        raise RuntimeError("simulated reinforce failure")

    monkeypatch.setattr(l0_api, "reinforce_slice", _broken_reinforce)

    t = datetime.now(timezone.utc)
    await _seed_passed_slice(booted_substrate, text="survivor", t_now=t, salience=0.4)
    proj = await recall(booted_substrate, "survivor")
    # The projection still came back even though reinforcement failed.
    assert proj.text  # non-empty
    assert "survivor" in proj.text


@pytest.mark.asyncio
async def test_recall_does_not_bump_uncomposed_candidates(booted_substrate):
    """A candidate not in the composed list should NOT be reinforced."""
    t = datetime.now(timezone.utc)
    await _seed_passed_slice(booted_substrate, text="composed_one", t_now=t, salience=0.6)
    # Second slice is below the min_salience floor → never makes it to
    # ranking → never reinforced.
    await _seed_passed_slice(booted_substrate, text="uncomposed_one", t_now=t, salience=0.001)

    before_uncomposed = await _get_salience(booted_substrate, text="uncomposed_one")
    proj = await recall(booted_substrate, "composed", min_salience=0.05)
    # uncomposed_one was below salience floor; not in candidates_seen.
    after_uncomposed = await _get_salience(booted_substrate, text="uncomposed_one")
    # Decay-only mutation is allowed but reinforcement is not — salience
    # of the uncomposed should be unchanged (no Curator running here).
    assert after_uncomposed == pytest.approx(before_uncomposed, abs=1e-6)
