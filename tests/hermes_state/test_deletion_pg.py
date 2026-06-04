"""Tests for _AsyncSessionDB deletion and pruning methods (Task 15).

Covers: clear_messages, delete_session (cascade + return value + filesystem cleanup),
and prune_sessions (age threshold + source filter + orphaning + filesystem cleanup).
"""

import thoth_db
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from thoth_state import _AsyncSessionDB


@pytest_asyncio.fixture
async def db(thoth_db_initialized):
    return _AsyncSessionDB()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _insert_message(session_id: str, role: str, content: str, offset_ms: int = 0):
    """Insert a raw message row with a deterministic timestamp."""
    ts = datetime.now(timezone.utc) + timedelta(milliseconds=offset_ms)
    async with thoth_db.connection() as conn:
        await conn.execute(
            """
            INSERT INTO messages (session_id, role, content, timestamp)
            VALUES ($1, $2, $3, $4)
            """,
            session_id, role, content, ts,
        )


async def _get_message_count(session_id: str) -> int:
    """Get message count for a session."""
    async with thoth_db.connection() as conn:
        result = await conn.fetchval(
            "SELECT COUNT(*) FROM messages WHERE session_id = $1",
            session_id,
        )
    return result


async def _session_exists(session_id: str) -> bool:
    """Check if a session exists."""
    async with thoth_db.connection() as conn:
        result = await conn.fetchval(
            "SELECT 1 FROM sessions WHERE id = $1",
            session_id,
        )
    return result is not None


# ---------------------------------------------------------------------------
# clear_messages
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clear_messages_deletes_messages(db):
    """clear_messages deletes all messages for a session."""
    await db.create_session(session_id="d1", source="cli", model="m", model_config={}, system_prompt="")
    await _insert_message("d1", "user", "hi")
    await _insert_message("d1", "assistant", "hello")
    assert await _get_message_count("d1") == 2

    await db.clear_messages("d1")

    assert await _get_message_count("d1") == 0
    assert await _session_exists("d1") is True  # Session should still exist


@pytest.mark.asyncio
async def test_clear_messages_resets_counters(db):
    """clear_messages resets message_count and tool_call_count."""
    await db.create_session(session_id="d2", source="cli", model="m", model_config={}, system_prompt="")
    await _insert_message("d2", "user", "test")

    # Manually update counters to test reset
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE sessions SET message_count = 5, tool_call_count = 3 WHERE id = $1",
            "d2",
        )

    await db.clear_messages("d2")

    async with thoth_db.connection() as conn:
        result = await conn.fetchrow(
            "SELECT message_count, tool_call_count FROM sessions WHERE id = $1",
            "d2",
        )
    assert result["message_count"] == 0
    assert result["tool_call_count"] == 0


@pytest.mark.asyncio
async def test_clear_messages_nonexistent(db):
    """clear_messages on nonexistent session is a no-op."""
    # Should not raise
    await db.clear_messages("nonexistent")


# ---------------------------------------------------------------------------
# delete_session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_session_removes_session_and_messages(db):
    """delete_session deletes session and all its messages."""
    await db.create_session(session_id="d3", source="cli", model="m", model_config={}, system_prompt="")
    await _insert_message("d3", "user", "hi")
    await _insert_message("d3", "assistant", "hello")

    result = await db.delete_session("d3")

    assert result is True
    assert await _session_exists("d3") is False
    assert await _get_message_count("d3") == 0


@pytest.mark.asyncio
async def test_delete_session_orphans_children(db):
    """delete_session orphans child sessions (sets parent_session_id to NULL)."""
    # Create parent
    await db.create_session(session_id="parent", source="cli", model="m", model_config={}, system_prompt="")

    # Create child with parent reference
    await db.create_session(
        session_id="child",
        source="cli",
        model="m",
        model_config={},
        system_prompt="",
        parent_session_id="parent",
    )

    await db.delete_session("parent")

    # Child should still exist but parent_session_id should be NULL
    async with thoth_db.connection() as conn:
        result = await conn.fetchrow(
            "SELECT parent_session_id FROM sessions WHERE id = $1",
            "child",
        )
    assert result is not None
    assert result["parent_session_id"] is None


@pytest.mark.asyncio
async def test_delete_session_nonexistent(db):
    """delete_session returns False for nonexistent session."""
    result = await db.delete_session("nonexistent")
    assert result is False


@pytest.mark.asyncio
async def test_delete_session_filesystem_cleanup(db, tmp_path):
    """delete_session cleans up on-disk transcript files."""
    await db.create_session(session_id="d4", source="cli", model="m", model_config={}, system_prompt="")

    # Create fake transcript files
    sessions_dir = tmp_path
    (sessions_dir / "d4.json").write_text("{}")
    (sessions_dir / "d4.jsonl").write_text("")
    (sessions_dir / "request_dump_d4_0.json").write_text("{}")
    (sessions_dir / "request_dump_d4_1.json").write_text("{}")
    (sessions_dir / "other_file.json").write_text("{}")  # Should NOT be deleted

    result = await db.delete_session("d4", sessions_dir=sessions_dir)

    assert result is True
    assert not (sessions_dir / "d4.json").exists()
    assert not (sessions_dir / "d4.jsonl").exists()
    assert not (sessions_dir / "request_dump_d4_0.json").exists()
    assert not (sessions_dir / "request_dump_d4_1.json").exists()
    assert (sessions_dir / "other_file.json").exists()  # Untouched


@pytest.mark.asyncio
async def test_delete_session_no_filesystem_dir(db):
    """delete_session works without sessions_dir (skips filesystem cleanup)."""
    await db.create_session(session_id="d5", source="cli", model="m", model_config={}, system_prompt="")
    await _insert_message("d5", "user", "test")

    result = await db.delete_session("d5")  # No sessions_dir

    assert result is True
    assert await _session_exists("d5") is False


# ---------------------------------------------------------------------------
# prune_sessions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prune_sessions_by_age(db):
    """prune_sessions deletes sessions older than N days."""
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(days=100)

    # Create old ended session
    await db.create_session(session_id="old_session", source="cli", model="m", model_config={}, system_prompt="")
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE sessions SET started_at = $1, ended_at = $2 WHERE id = $3",
            old_time, old_time + timedelta(seconds=1), "old_session",
        )

    # Create recent session
    await db.create_session(session_id="new_session", source="cli", model="m", model_config={}, system_prompt="")
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE sessions SET started_at = $1, ended_at = $2 WHERE id = $3",
            now, now + timedelta(seconds=1), "new_session",
        )

    count = await db.prune_sessions(older_than_days=90)

    assert count == 1
    assert await _session_exists("old_session") is False
    assert await _session_exists("new_session") is True


@pytest.mark.asyncio
async def test_prune_sessions_only_ended(db):
    """prune_sessions only prunes ended sessions (skips active ones)."""
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(days=100)

    # Create old ended session
    await db.create_session(session_id="old_ended", source="cli", model="m", model_config={}, system_prompt="")
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE sessions SET started_at = $1, ended_at = $2 WHERE id = $3",
            old_time, old_time + timedelta(seconds=1), "old_ended",
        )

    # Create old active session (no ended_at)
    await db.create_session(session_id="old_active", source="cli", model="m", model_config={}, system_prompt="")
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE sessions SET started_at = $1 WHERE id = $2",
            old_time, "old_active",
        )

    count = await db.prune_sessions(older_than_days=90)

    assert count == 1
    assert await _session_exists("old_ended") is False
    assert await _session_exists("old_active") is True


@pytest.mark.asyncio
async def test_prune_sessions_by_source(db):
    """prune_sessions filters by source when provided."""
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(days=100)

    # Create old cli session
    await db.create_session(session_id="old_cli", source="cli", model="m", model_config={}, system_prompt="")
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE sessions SET started_at = $1, ended_at = $2 WHERE id = $3",
            old_time, old_time + timedelta(seconds=1), "old_cli",
        )

    # Create old telegram session
    await db.create_session(session_id="old_telegram", source="telegram", model="m", model_config={}, system_prompt="")
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE sessions SET started_at = $1, ended_at = $2 WHERE id = $3",
            old_time, old_time + timedelta(seconds=1), "old_telegram",
        )

    count = await db.prune_sessions(older_than_days=90, source="cli")

    assert count == 1
    assert await _session_exists("old_cli") is False
    assert await _session_exists("old_telegram") is True


@pytest.mark.asyncio
async def test_prune_sessions_orphans_children(db):
    """prune_sessions orphans children of sessions being pruned."""
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(days=100)

    # Create old parent session
    await db.create_session(session_id="old_parent", source="cli", model="m", model_config={}, system_prompt="")
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE sessions SET started_at = $1, ended_at = $2 WHERE id = $3",
            old_time, old_time + timedelta(seconds=1), "old_parent",
        )

    # Create child with parent reference
    await db.create_session(
        session_id="child",
        source="cli",
        model="m",
        model_config={},
        system_prompt="",
        parent_session_id="old_parent",
    )

    count = await db.prune_sessions(older_than_days=90)

    assert count == 1
    assert await _session_exists("old_parent") is False
    assert await _session_exists("child") is True

    # Child should be orphaned
    async with thoth_db.connection() as conn:
        result = await conn.fetchrow(
            "SELECT parent_session_id FROM sessions WHERE id = $1",
            "child",
        )
    assert result["parent_session_id"] is None


@pytest.mark.asyncio
async def test_prune_sessions_filesystem_cleanup(db, tmp_path):
    """prune_sessions cleans up on-disk files for pruned sessions."""
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(days=100)

    # Create old session
    await db.create_session(session_id="old_s", source="cli", model="m", model_config={}, system_prompt="")
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE sessions SET started_at = $1, ended_at = $2 WHERE id = $3",
            old_time, old_time + timedelta(seconds=1), "old_s",
        )

    # Create fake files
    sessions_dir = tmp_path
    (sessions_dir / "old_s.json").write_text("{}")
    (sessions_dir / "old_s.jsonl").write_text("")
    (sessions_dir / "request_dump_old_s_0.json").write_text("{}")
    (sessions_dir / "other.json").write_text("{}")

    count = await db.prune_sessions(older_than_days=90, sessions_dir=sessions_dir)

    assert count == 1
    assert not (sessions_dir / "old_s.json").exists()
    assert not (sessions_dir / "old_s.jsonl").exists()
    assert not (sessions_dir / "request_dump_old_s_0.json").exists()
    assert (sessions_dir / "other.json").exists()


@pytest.mark.asyncio
async def test_prune_sessions_empty(db):
    """prune_sessions returns 0 when no sessions match."""
    count = await db.prune_sessions(older_than_days=90)
    assert count == 0


@pytest.mark.asyncio
async def test_prune_sessions_multiple(db):
    """prune_sessions deletes multiple matching sessions."""
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(days=100)

    # Create 3 old sessions
    for i in range(3):
        await db.create_session(session_id=f"old_{i}", source="cli", model="m", model_config={}, system_prompt="")
        async with thoth_db.connection() as conn:
            await conn.execute(
                "UPDATE sessions SET started_at = $1, ended_at = $2 WHERE id = $3",
                old_time, old_time + timedelta(seconds=1), f"old_{i}",
            )

    count = await db.prune_sessions(older_than_days=90)

    assert count == 3
    for i in range(3):
        assert await _session_exists(f"old_{i}") is False
