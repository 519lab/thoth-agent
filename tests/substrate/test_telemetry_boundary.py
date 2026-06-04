"""Perceptual / non-perceptual boundary + telemetry sink.

Regression coverage for the ``substrate.self_state`` L0 feedback loop
(2026-05-26→27, 414k ghost slices): operational events the substrate emits
about its own decisions must be non-perceptual — recorded in
``substrate_telemetry``, never as slices that count toward the Conductor's
consolidation backlog.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.agents.conductor_policy import AdaptiveConductor
from substrate.l0 import commit_slice
from substrate.storage import DEFAULT_STRUCTURED_PROFILE, Family, Modality, is_perceptual
from substrate.telemetry import write as telemetry_write


@pytest_asyncio.fixture
async def substrate(thoth_db_initialized):
    import thoth_db

    return Substrate.from_pool(thoth_db.pool())


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def test_is_perceptual_boundary():
    """thoth.* is perception; substrate.* is operational telemetry."""
    assert is_perceptual("thoth.world.user_message.cli")
    assert is_perceptual("thoth.self_state.tool_result")
    assert is_perceptual("thoth.self_action.tool_call")
    assert not is_perceptual("substrate.self_state")
    # Prefix-based, so a future substrate.* stream is covered automatically.
    assert not is_perceptual("substrate.some_future_stream")


@pytest.mark.asyncio
async def test_telemetry_write_creates_row_not_slice(substrate):
    """``telemetry.write`` appends to substrate_telemetry and creates NO
    substrate_slices row — that's what keeps it out of the awareness loop."""
    import thoth_db

    async with thoth_db.connection() as conn:
        slices_before = await conn.fetchval("SELECT count(*) FROM substrate_slices")

    await telemetry_write(
        substrate,
        agent="curator",
        event="curator.release",
        payload={"slice_id": "abc-123", "tombstone_policy": "thin"},
    )

    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            "SELECT agent, event, payload FROM substrate_telemetry "
            "WHERE event = 'curator.release' ORDER BY at DESC LIMIT 1"
        )
        slices_after = await conn.fetchval("SELECT count(*) FROM substrate_slices")

    assert row is not None
    assert row["agent"] == "curator"
    assert row["event"] == "curator.release"
    assert row["payload"]["slice_id"] == "abc-123"
    assert slices_after == slices_before, "telemetry.write must not create a slice"


@pytest.mark.asyncio
async def test_read_load_ignores_substrate_streams(substrate):
    """The Conductor's backlog forecast must NOT count ``substrate.*`` slices.

    This is the keystone of the loop fix: when audit slices on
    ``substrate.self_state`` counted as consolidation backlog, the Conductor
    pinned the Parser HIGH against a backlog that could never drain (the
    audit slices carry no session_id) and emitted another dial each tick.
    """
    # 5 passed+unconsolidated slices on the non-perceptual substrate.self_state.
    self_state = await substrate.streams.get_by_name("substrate.self_state")
    for i in range(5):
        await commit_slice(
            substrate, self_state.stream_id, {"event": f"noise{i}"},
            event_time_world=_now_utc(), born_passed=True,
        )

    load = await AdaptiveConductor(substrate)._read_load()
    assert load["pending"] == 0, "substrate.* slices must not count as backlog"
    assert load["backlog_ratio"] == 0.0

    # Passed+unconsolidated slices on a PERCEPTUAL stream DO count.
    stream = await substrate.streams.register(
        name="hermes.test.read_load_perceptual",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
    )
    for i in range(3):
        await commit_slice(
            substrate, stream.stream_id, {"i": i},
            event_time_world=_now_utc(), born_passed=True,
        )

    load2 = await AdaptiveConductor(substrate)._read_load()
    assert load2["pending"] == 3, "perceptual slices must count as backlog"
