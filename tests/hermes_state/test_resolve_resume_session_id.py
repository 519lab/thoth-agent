"""Regression guard for #15000: --resume <id> after compression loses messages.

Context compression ends the current session and forks a new child session
(linked by ``parent_session_id``). The PG flush cursor is reset, so
only the latest descendant ends up with rows in the ``messages`` table —
the parent row has ``message_count = 0``. ``thoth --resume <parent_id>``
used to load zero rows and show a blank chat.

``_AsyncSessionDB.resolve_resume_session_id()`` walks the parent → child chain
and redirects to the first descendant that actually has messages. These
tests pin that behaviour.
"""
from datetime import datetime, timedelta, timezone

import thoth_db
import pytest
import pytest_asyncio

from thoth_state import _AsyncSessionDB


@pytest_asyncio.fixture
async def db(hermes_db_initialized):
    return _AsyncSessionDB()


async def _make_chain(db: _AsyncSessionDB, ids_with_parent):
    """Create sessions in order, forcing started_at so ordering is deterministic."""
    base_dt = datetime.now(timezone.utc) - timedelta(seconds=10_000)
    for i, (sid, parent) in enumerate(ids_with_parent):
        await db.create_session(sid, source="cli", parent_session_id=parent)
        started_at = base_dt + timedelta(seconds=i * 100)
        async with thoth_db.connection() as conn:
            await conn.execute(
                "UPDATE sessions SET started_at = $1 WHERE id = $2",
                started_at, sid,
            )


@pytest.mark.asyncio
async def test_redirects_from_empty_head_to_descendant_with_messages(db):
    # Reproducer shape from #15000: 6 sessions, only the 5th holds messages.
    await _make_chain(db, [
        ("head",   None),
        ("mid1",   "head"),
        ("mid2",   "mid1"),
        ("mid3",   "mid2"),
        ("bulk",   "mid3"),    # has messages
        ("tail",   "bulk"),    # empty tail after another compression
    ])
    for i in range(5):
        await db.append_message("bulk", role="user", content=f"msg {i}")

    assert await db.resolve_resume_session_id("head") == "bulk"


@pytest.mark.asyncio
async def test_returns_self_when_session_has_messages(db):
    await _make_chain(db, [("root", None), ("child", "root")])
    await db.append_message("root", role="user", content="hi")
    assert await db.resolve_resume_session_id("root") == "root"


@pytest.mark.asyncio
async def test_returns_self_when_no_descendant_has_messages(db):
    await _make_chain(db, [("root", None), ("child1", "root"), ("child2", "child1")])
    assert await db.resolve_resume_session_id("root") == "root"


@pytest.mark.asyncio
async def test_returns_self_for_isolated_session(db):
    await db.create_session("isolated", source="cli")
    assert await db.resolve_resume_session_id("isolated") == "isolated"


@pytest.mark.asyncio
async def test_returns_self_for_nonexistent_session(db):
    assert await db.resolve_resume_session_id("does_not_exist") == "does_not_exist"


@pytest.mark.asyncio
async def test_empty_session_id_passthrough(db):
    assert await db.resolve_resume_session_id("") == ""
    assert await db.resolve_resume_session_id(None) is None


@pytest.mark.asyncio
async def test_walks_from_middle_of_chain(db):
    # If the user happens to know an intermediate ID, we still find the msg-bearing descendant.
    await _make_chain(db, [("a", None), ("b", "a"), ("c", "b"), ("d", "c")])
    await db.append_message("d", role="user", content="x")
    assert await db.resolve_resume_session_id("b") == "d"
    assert await db.resolve_resume_session_id("c") == "d"


@pytest.mark.asyncio
async def test_prefers_most_recent_child_when_fork_exists(db):
    # If a session was somehow forked (two children), pick the latest one.
    # In practice, compression only produces single-chain shape, but the helper
    # should degrade gracefully.
    await _make_chain(db, [
        ("parent", None),
        ("older_fork", "parent"),
        ("newer_fork", "parent"),
    ])
    await db.append_message("newer_fork", role="user", content="x")
    assert await db.resolve_resume_session_id("parent") == "newer_fork"
