"""TDD tests for _AsyncSessionDB session-lifecycle methods (Phase 0 Task 8).

All tests run against the docker-compose postgres cluster via the
hermes_db_initialized fixture. Docker must be running:
    docker compose up -d postgres
"""
import pytest
import pytest_asyncio
from thoth_state import _AsyncSessionDB


@pytest_asyncio.fixture
async def db(hermes_db_initialized):
    return _AsyncSessionDB()


@pytest.mark.asyncio
async def test_create_session_inserts_and_returns_id(db):
    sid = await db.create_session(
        session_id="s1",
        source="cli",
        model="claude-sonnet-4-6",
        model_config={"temperature": 0.7},
        system_prompt="you are helpful",
    )
    assert sid == "s1"
    row = await db.get_session("s1")
    assert row is not None
    assert row["source"] == "cli"
    assert row["model"] == "claude-sonnet-4-6"
    assert row["model_config"] == {"temperature": 0.7}
    assert row["system_prompt"] == "you are helpful"
    assert row["ended_at"] is None


@pytest.mark.asyncio
async def test_end_session_marks_end(db):
    await db.create_session(session_id="s2", source="cli", model="m", model_config={}, system_prompt="")
    await db.end_session("s2", "user_quit")
    row = await db.get_session("s2")
    assert row["ended_at"] is not None
    assert row["end_reason"] == "user_quit"


@pytest.mark.asyncio
async def test_reopen_session_clears_end(db):
    await db.create_session(session_id="s3", source="cli", model="m", model_config={}, system_prompt="")
    await db.end_session("s3", "x")
    await db.reopen_session("s3")
    row = await db.get_session("s3")
    assert row["ended_at"] is None
    assert row["end_reason"] is None


@pytest.mark.asyncio
async def test_update_system_prompt_replaces(db):
    await db.create_session(session_id="s4", source="cli", model="m", model_config={}, system_prompt="old")
    await db.update_system_prompt("s4", "new")
    row = await db.get_session("s4")
    assert row["system_prompt"] == "new"


@pytest.mark.asyncio
async def test_update_token_counts_accumulates(db):
    await db.create_session(session_id="s5", source="cli", model="m", model_config={}, system_prompt="")
    await db.update_token_counts(
        "s5", input_tokens=100, output_tokens=50, cache_read_tokens=0,
        cache_write_tokens=0, reasoning_tokens=0,
    )
    row = await db.get_session("s5")
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 50


@pytest.mark.asyncio
async def test_resolve_session_id_full(db):
    await db.create_session(session_id="abcdef1234", source="cli", model="m", model_config={}, system_prompt="")
    assert await db.resolve_session_id("abcdef1234") == "abcdef1234"


@pytest.mark.asyncio
async def test_resolve_session_id_prefix(db):
    await db.create_session(session_id="abcdef1234", source="cli", model="m", model_config={}, system_prompt="")
    # Behavior matches upstream: unambiguous prefix resolves.
    assert await db.resolve_session_id("abcdef") == "abcdef1234"


@pytest.mark.asyncio
async def test_resolve_session_id_missing(db):
    assert await db.resolve_session_id("nope") is None


@pytest.mark.asyncio
async def test_get_session_missing(db):
    assert await db.get_session("missing") is None


@pytest.mark.asyncio
async def test_ensure_session_creates_if_missing(db):
    # Behavior parity with upstream `ensure_session` — read `thoth_state.py:866-876`.
    await db.ensure_session(session_id="ensure1", source="cli", model="m", model_config={}, system_prompt="")
    assert (await db.get_session("ensure1")) is not None


@pytest.mark.asyncio
async def test_ensure_session_noop_if_present(db):
    await db.create_session(session_id="ensure2", source="cli", model="m", model_config={}, system_prompt="orig")
    await db.ensure_session(session_id="ensure2", source="cli", model="m", model_config={}, system_prompt="new")
    row = await db.get_session("ensure2")
    assert row["system_prompt"] == "orig"  # not overwritten


@pytest.mark.asyncio
async def test_prune_empty_ghost_removes_old_unnamed_ended_tui_session(db):
    """Match upstream filter: source=tui, title IS NULL, ended, >24h old, no messages."""
    await db.create_session(session_id="ghost", source="tui", model="m", model_config={}, system_prompt="")
    await db.end_session("ghost", "x")
    # Force started_at into the past so the age filter matches.
    import thoth_db
    async with thoth_db.connection() as c:
        await c.execute(
            "UPDATE sessions SET started_at = now() - interval '25 hours' WHERE id = 'ghost'"
        )
    n = await db.prune_empty_ghost_sessions()
    assert n == 1
    assert (await db.get_session("ghost")) is None


@pytest.mark.asyncio
async def test_prune_empty_ghost_keeps_cli_sessions(db):
    """source != 'tui' should never be pruned even if otherwise empty/old/ended."""
    await db.create_session(session_id="cli_old", source="cli", model="m", model_config={}, system_prompt="")
    await db.end_session("cli_old", "x")
    import thoth_db
    async with thoth_db.connection() as c:
        await c.execute(
            "UPDATE sessions SET started_at = now() - interval '25 hours' WHERE id = 'cli_old'"
        )
    n = await db.prune_empty_ghost_sessions()
    assert n == 0
    assert (await db.get_session("cli_old")) is not None


@pytest.mark.asyncio
async def test_prune_empty_ghost_keeps_recent_session(db):
    """Sessions younger than 24h should not be pruned."""
    await db.create_session(session_id="recent_tui", source="tui", model="m", model_config={}, system_prompt="")
    await db.end_session("recent_tui", "x")
    # started_at is now() by default; age filter prevents pruning.
    n = await db.prune_empty_ghost_sessions()
    assert n == 0
    assert (await db.get_session("recent_tui")) is not None


@pytest.mark.asyncio
async def test_prune_empty_ghost_keeps_titled_session(db):
    """Sessions with a title are user-blessed; should never be pruned."""
    await db.create_session(session_id="titled_tui", source="tui", model="m", model_config={}, system_prompt="", title="My Session")
    await db.end_session("titled_tui", "x")
    import thoth_db
    async with thoth_db.connection() as c:
        await c.execute(
            "UPDATE sessions SET started_at = now() - interval '25 hours' WHERE id = 'titled_tui'"
        )
    n = await db.prune_empty_ghost_sessions()
    assert n == 0
    assert (await db.get_session("titled_tui")) is not None


@pytest.mark.asyncio
async def test_end_session_noop_if_already_ended(db):
    """First end_reason wins (behavior parity with upstream)."""
    await db.create_session(session_id="noop1", source="cli", model="m", model_config={}, system_prompt="")
    await db.end_session("noop1", "compression")
    await db.end_session("noop1", "should_not_overwrite")
    row = await db.get_session("noop1")
    assert row["end_reason"] == "compression"


@pytest.mark.asyncio
async def test_finalize_orphaned_compression_sessions(db):
    """Child with api_call_count=0 and a parent ended with 'compression' gets finalized."""
    # Create parent session and end it with compression
    await db.create_session(session_id="parent1", source="cli", model="m", model_config={}, system_prompt="")
    await db.end_session("parent1", "compression")

    # Create child session linked to parent, with a message (required by criteria)
    import thoth_db
    await db.create_session(
        session_id="child1", source="cli", model="m", model_config={}, system_prompt="",
        parent_session_id="parent1",
    )
    # Insert a message manually so EXISTS check passes
    async with thoth_db.connection() as conn:
        await conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES ($1, $2, $3)",
            "child1", "user", "orphan message",
        )
    # Backdate child's started_at to > 7 days ago so cutoff applies
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE sessions SET started_at = now() - interval '8 days' WHERE id = $1",
            "child1",
        )

    count = await db.finalize_orphaned_compression_sessions()
    assert count >= 1
    row = await db.get_session("child1")
    assert row["end_reason"] == "orphaned_compression"
    assert row["ended_at"] is not None
