"""Tests for _AsyncSessionDB.list_sessions_rich and _get_session_rich_row (Task 11).

Covers: empty result, single session with messages, pagination, source filter,
most-recent-first ordering, child-session exclusion, exclude_sources filter,
and the per-session preview/last_active shape. The order_by_last_active and
project_compression_tips paths are exercised at a smoke level; the full
compression-chain projection is covered more thoroughly in test_compression_pg.py.
"""

import thoth_db
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from thoth_state import _AsyncSessionDB


@pytest_asyncio.fixture
async def db(hermes_db_initialized):
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


async def _set_started_at(session_id: str, started_at: datetime):
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE sessions SET started_at = $1 WHERE id = $2",
            started_at, session_id,
        )


# ---------------------------------------------------------------------------
# Basic result shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_empty(db):
    """Empty database returns empty list."""
    rows = await db.list_sessions_rich(limit=10)
    assert rows == []


@pytest.mark.asyncio
async def test_list_single_session_shape(db):
    """Single session returns correct keys including preview and last_active."""
    await db.create_session(
        session_id="S1", source="cli", model="m", model_config={}, system_prompt=""
    )
    await _insert_message("S1", "user", "Hello world", offset_ms=100)

    rows = await db.list_sessions_rich(limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == "S1"
    assert r["source"] == "cli"
    assert "preview" in r
    assert "last_active" in r
    # Preview should be the first user message text (up to 60 chars)
    assert "Hello world" in r["preview"]
    # last_active must be a datetime (or comparable)
    assert r["last_active"] is not None


@pytest.mark.asyncio
async def test_preview_truncates_at_60_chars(db):
    """preview field truncates to 60 chars and appends '...' for longer text."""
    await db.create_session(
        session_id="P1", source="cli", model="m", model_config={}, system_prompt=""
    )
    long_msg = "A" * 80
    await _insert_message("P1", "user", long_msg, offset_ms=10)

    rows = await db.list_sessions_rich(limit=10)
    assert len(rows) == 1
    preview = rows[0]["preview"]
    assert preview.endswith("...")
    # 60 chars of text + "..." = 63 chars max
    assert len(preview) <= 63


@pytest.mark.asyncio
async def test_preview_short_message_no_ellipsis(db):
    """Short messages do not get truncation ellipsis."""
    await db.create_session(
        session_id="P2", source="cli", model="m", model_config={}, system_prompt=""
    )
    await _insert_message("P2", "user", "Short", offset_ms=10)

    rows = await db.list_sessions_rich(limit=10)
    assert rows[0]["preview"] == "Short"


@pytest.mark.asyncio
async def test_preview_empty_when_no_messages(db):
    """Session with no messages has empty preview string."""
    await db.create_session(
        session_id="P3", source="cli", model="m", model_config={}, system_prompt=""
    )
    rows = await db.list_sessions_rich(limit=10)
    assert rows[0]["preview"] == ""


@pytest.mark.asyncio
async def test_preview_uses_first_user_message_not_assistant(db):
    """Preview comes from the first *user* message, ignoring assistant messages."""
    await db.create_session(
        session_id="PU", source="cli", model="m", model_config={}, system_prompt=""
    )
    await _insert_message("PU", "assistant", "Hi there!", offset_ms=10)
    await _insert_message("PU", "user", "User speaks", offset_ms=20)

    rows = await db.list_sessions_rich(limit=10)
    assert "User speaks" in rows[0]["preview"]


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_returns_most_recent_first(db):
    """Default ordering is started_at DESC (most recent first)."""
    base = datetime.now(timezone.utc) - timedelta(seconds=1000)
    for i in range(3):
        await db.create_session(
            session_id=f"L{i}", source="cli", model="m", model_config={}, system_prompt=""
        )
        await _set_started_at(f"L{i}", base + timedelta(seconds=i * 10))

    rows = await db.list_sessions_rich(limit=10)
    ids = [r["id"] for r in rows]
    assert ids == ["L2", "L1", "L0"]


# ---------------------------------------------------------------------------
# Source filter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_respects_source_filter(db):
    """source= filter returns only sessions with matching source."""
    await db.create_session(
        session_id="A", source="cli", model="m", model_config={}, system_prompt=""
    )
    await db.create_session(
        session_id="B", source="telegram", model="m", model_config={}, system_prompt=""
    )
    cli_only = await db.list_sessions_rich(limit=10, source="cli")
    assert [r["id"] for r in cli_only] == ["A"]


@pytest.mark.asyncio
async def test_list_respects_exclude_sources_filter(db):
    """exclude_sources= filter removes sessions with any of the listed sources."""
    await db.create_session(
        session_id="X1", source="cli", model="m", model_config={}, system_prompt=""
    )
    await db.create_session(
        session_id="X2", source="telegram", model="m", model_config={}, system_prompt=""
    )
    await db.create_session(
        session_id="X3", source="api", model="m", model_config={}, system_prompt=""
    )
    rows = await db.list_sessions_rich(limit=10, exclude_sources=["cli", "telegram"])
    assert [r["id"] for r in rows] == ["X3"]


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pagination_limit(db):
    """limit= caps the number of returned rows."""
    base = datetime.now(timezone.utc) - timedelta(seconds=500)
    for i in range(5):
        await db.create_session(
            session_id=f"PAG{i}", source="cli", model="m", model_config={}, system_prompt=""
        )
        await _set_started_at(f"PAG{i}", base + timedelta(seconds=i))

    rows = await db.list_sessions_rich(limit=3)
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_pagination_offset(db):
    """limit= + offset= pages through results correctly."""
    base = datetime.now(timezone.utc) - timedelta(seconds=500)
    for i in range(4):
        await db.create_session(
            session_id=f"OFF{i}", source="cli", model="m", model_config={}, system_prompt=""
        )
        await _set_started_at(f"OFF{i}", base + timedelta(seconds=i))

    page1 = await db.list_sessions_rich(limit=2, offset=0)
    page2 = await db.list_sessions_rich(limit=2, offset=2)
    p1_ids = [r["id"] for r in page1]
    p2_ids = [r["id"] for r in page2]
    # Most-recent-first: OFF3, OFF2 | OFF1, OFF0
    assert p1_ids == ["OFF3", "OFF2"]
    assert p2_ids == ["OFF1", "OFF0"]


# ---------------------------------------------------------------------------
# Child session exclusion (include_children)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_child_sessions_excluded_by_default(db):
    """Subagent child sessions are hidden unless include_children=True."""
    base = datetime.now(timezone.utc) - timedelta(seconds=100)
    await db.create_session(
        session_id="ROOT", source="cli", model="m", model_config={}, system_prompt=""
    )
    await _set_started_at("ROOT", base)
    # Create a child session that started while parent was still live
    # (no ended_at on parent yet → child is a delegate subagent)
    await db.create_session(
        session_id="CHILD", source="cli", model="m", model_config={}, system_prompt="",
        parent_session_id="ROOT",
    )
    await _set_started_at("CHILD", base + timedelta(seconds=10))

    # CHILD started while ROOT was alive (ROOT has no ended_at), so CHILD
    # is a subagent — it should be hidden by default.
    # ROOT has parent_session_id=NULL so ROOT is always shown.
    # CHILD has parent_session_id=ROOT and ROOT is not ended with 'branched',
    # so CHILD is hidden unless include_children=True.
    rows_default = await db.list_sessions_rich(limit=10)
    ids_default = [r["id"] for r in rows_default]
    assert "CHILD" not in ids_default
    assert "ROOT" in ids_default

    rows_with = await db.list_sessions_rich(limit=10, include_children=True)
    ids_with = [r["id"] for r in rows_with]
    assert "CHILD" in ids_with
    assert "ROOT" in ids_with


# ---------------------------------------------------------------------------
# order_by_last_active
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_order_by_last_active(db):
    """order_by_last_active=True sorts by most-recent message, not started_at."""
    base = datetime.now(timezone.utc) - timedelta(seconds=1000)

    # Create OLDER session first
    await db.create_session(
        session_id="OLD", source="cli", model="m", model_config={}, system_prompt=""
    )
    await _set_started_at("OLD", base)

    # Create NEWER session second
    await db.create_session(
        session_id="NEW", source="cli", model="m", model_config={}, system_prompt=""
    )
    await _set_started_at("NEW", base + timedelta(seconds=10))

    # OLD session gets a recent message → it should surface first in last_active order
    await _insert_message("OLD", "user", "Late message", offset_ms=500)

    rows = await db.list_sessions_rich(limit=10, order_by_last_active=True)
    ids = [r["id"] for r in rows]
    assert ids[0] == "OLD"  # Most recently active despite earlier start


# ---------------------------------------------------------------------------
# _get_session_rich_row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_session_rich_row_basic(db):
    """_get_session_rich_row returns same enriched shape as list_sessions_rich."""
    await db.create_session(
        session_id="GRR1", source="cli", model="m", model_config={}, system_prompt=""
    )
    await _insert_message("GRR1", "user", "Hello from rich", offset_ms=50)

    row = await db._get_session_rich_row("GRR1")
    assert row is not None
    assert row["id"] == "GRR1"
    assert "preview" in row
    assert "Hello from rich" in row["preview"]
    assert "last_active" in row


@pytest.mark.asyncio
async def test_get_session_rich_row_missing_returns_none(db):
    """_get_session_rich_row returns None for a non-existent session."""
    result = await db._get_session_rich_row("does_not_exist")
    assert result is None
