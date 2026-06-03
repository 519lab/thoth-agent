import pytest
import pytest_asyncio
from thoth_state import _AsyncSessionDB


@pytest_asyncio.fixture
async def db(hermes_db_initialized):
    d = _AsyncSessionDB()
    await d.create_session(
        session_id="h1",
        source="cli",
        model="m",
        model_config={},
        system_prompt="",
    )
    return d


@pytest.mark.asyncio
async def test_handoff_request_sets_pending(db):
    assert await db.request_handoff("h1", "telegram") is True
    state = await db.get_handoff_state("h1")
    assert state is not None
    assert state["state"] == "pending"
    assert state["platform"] == "telegram"
    assert state["error"] is None


@pytest.mark.asyncio
async def test_handoff_claim_then_complete(db):
    await db.request_handoff("h1", "telegram")
    # claim transitions pending → running
    assert await db.claim_handoff("h1") is True
    state = await db.get_handoff_state("h1")
    assert state["state"] == "running"
    # complete transitions running → completed
    await db.complete_handoff("h1")
    state = await db.get_handoff_state("h1")
    assert state["state"] == "completed"
    assert state["error"] is None


@pytest.mark.asyncio
async def test_handoff_claim_no_pending_returns_false(db):
    # No request — claim should fail
    assert await db.claim_handoff("h1") is False


@pytest.mark.asyncio
async def test_handoff_claim_already_claimed_returns_false(db):
    await db.request_handoff("h1", "telegram")
    assert await db.claim_handoff("h1") is True
    # Second claim should return False — CAS prevents double-claim
    assert await db.claim_handoff("h1") is False


@pytest.mark.asyncio
async def test_handoff_fail_records_error(db):
    await db.request_handoff("h1", "telegram")
    await db.claim_handoff("h1")
    await db.fail_handoff("h1", "API timeout")
    state = await db.get_handoff_state("h1")
    assert state["state"] == "failed"
    assert state["error"] == "API timeout"


@pytest.mark.asyncio
async def test_list_pending_handoffs(db):
    await db.create_session(
        session_id="h2",
        source="cli",
        model="m",
        model_config={},
        system_prompt="",
    )
    await db.request_handoff("h1", "telegram")
    pending = await db.list_pending_handoffs()
    ids = [p["id"] for p in pending]
    assert "h1" in ids
    assert "h2" not in ids


@pytest.mark.asyncio
async def test_handoff_request_from_terminal_state_resets(db):
    """Re-requesting from completed/failed transitions back to pending."""
    await db.request_handoff("h1", "telegram")
    await db.claim_handoff("h1")
    await db.complete_handoff("h1")
    # Should be allowed to re-request from completed
    assert await db.request_handoff("h1", "slack") is True
    state = await db.get_handoff_state("h1")
    assert state["state"] == "pending"
    assert state["platform"] == "slack"


@pytest.mark.asyncio
async def test_handoff_request_blocked_while_pending(db):
    """Re-requesting from pending (non-terminal) returns False."""
    await db.request_handoff("h1", "telegram")
    # Already pending — cannot re-request
    assert await db.request_handoff("h1", "slack") is False


@pytest.mark.asyncio
async def test_get_handoff_state_nonexistent_session(db):
    state = await db.get_handoff_state("does-not-exist")
    assert state is None


@pytest.mark.asyncio
async def test_fail_handoff_truncates_long_error(db):
    await db.request_handoff("h1", "telegram")
    long_error = "x" * 600
    await db.fail_handoff("h1", long_error)
    state = await db.get_handoff_state("h1")
    assert state["state"] == "failed"
    assert len(state["error"]) == 500


@pytest.mark.asyncio
async def test_vacuum_runs(db):
    # Just verify it doesn't raise — VACUUM ANALYZE on a fresh PG is cheap.
    await db.vacuum()
