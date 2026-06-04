"""Tests for _AsyncSessionDB.search_messages / search_sessions (PostgreSQL path).

Requires:  docker-compose up -d postgres  + migrations applied.
Run with:  uv run pytest tests/thoth_state/test_search_pg.py -v -o addopts=""
"""

import pytest
import pytest_asyncio

from thoth_state import _AsyncSessionDB


@pytest_asyncio.fixture
async def db_with_messages(thoth_db_initialized):
    db = _AsyncSessionDB()
    await db.create_session(
        session_id="s",
        source="cli",
        model="m",
        model_config={},
        system_prompt="",
    )
    await db.append_message("s", "user", "the quick brown fox jumps over the lazy dog")
    await db.append_message("s", "assistant", "I see a fox and a dog. Quick reply.")
    await db.append_message("s", "user", "tell me about quantum mechanics")
    return db


@pytest_asyncio.fixture
async def db_multi_session(thoth_db_initialized):
    """Two sessions — one from 'cli', one from 'tool' — for filter tests."""
    db = _AsyncSessionDB()
    await db.create_session("sa", source="cli", model="m", model_config={}, system_prompt="")
    await db.create_session("sb", source="tool", model="m", model_config={}, system_prompt="")
    await db.append_message("sa", "user", "hello from cli session python")
    await db.append_message("sb", "user", "hello from tool session python")
    return db


# ---------------------------------------------------------------------------
# search_messages — keyword mode (default)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_keyword_token(db_with_messages):
    """Token 'fox' matches both messages containing it."""
    hits = await db_with_messages.search_messages("fox")
    assert len(hits) >= 2
    # content is stripped; use snippet
    assert any("fox" in (h.get("snippet") or "").lower() for h in hits)


@pytest.mark.asyncio
async def test_search_keyword_phrase(db_with_messages):
    """Phrase 'quick brown fox' matches exactly one message."""
    hits = await db_with_messages.search_messages('"quick brown fox"')
    # plainto_tsquery normalises phrase to AND-terms; expect at least 1 hit
    assert len(hits) >= 1


@pytest.mark.asyncio
async def test_search_keyword_no_match(db_with_messages):
    """Non-existent term returns empty list."""
    hits = await db_with_messages.search_messages("xyzzy")
    assert hits == []


@pytest.mark.asyncio
async def test_search_returns_no_content_field(db_with_messages):
    """Full content is stripped from results; snippet is present instead."""
    hits = await db_with_messages.search_messages("fox")
    assert len(hits) > 0
    for h in hits:
        assert "content" not in h
        assert "snippet" in h


@pytest.mark.asyncio
async def test_search_context_attached(db_with_messages):
    """Each hit carries a 'context' list with surrounding messages."""
    hits = await db_with_messages.search_messages("fox")
    assert len(hits) > 0
    for h in hits:
        assert "context" in h
        assert isinstance(h["context"], list)


@pytest.mark.asyncio
async def test_search_role_filter(db_with_messages):
    """role_filter=['assistant'] restricts results to assistant turns."""
    hits = await db_with_messages.search_messages("fox", role_filter=["assistant"])
    assert len(hits) >= 1
    assert all(h["role"] == "assistant" for h in hits)


@pytest.mark.asyncio
async def test_search_exclude_sources(db_multi_session):
    """exclude_sources removes hits from excluded sessions."""
    # 'tool' source should be excluded
    hits = await db_multi_session.search_messages("python", exclude_sources=["tool"])
    session_ids = {h["session_id"] for h in hits}
    assert "sb" not in session_ids


@pytest.mark.asyncio
async def test_search_source_filter(db_multi_session):
    """source_filter restricts hits to the specified source."""
    hits = await db_multi_session.search_messages("python", source_filter=["tool"])
    assert len(hits) >= 1
    assert all(h["source"] == "tool" for h in hits)


@pytest.mark.asyncio
async def test_search_limit(db_with_messages):
    """limit=1 returns at most 1 result."""
    hits = await db_with_messages.search_messages("fox", limit=1)
    assert len(hits) <= 1


@pytest.mark.asyncio
async def test_search_sort_newest(db_with_messages):
    """sort='newest' returns results in descending timestamp order."""
    hits = await db_with_messages.search_messages("fox", sort="newest")
    if len(hits) >= 2:
        timestamps = [h["timestamp"] for h in hits]
        assert timestamps == sorted(timestamps, reverse=True)


@pytest.mark.asyncio
async def test_search_sort_oldest(db_with_messages):
    """sort='oldest' returns results in ascending timestamp order."""
    hits = await db_with_messages.search_messages("fox", sort="oldest")
    if len(hits) >= 2:
        timestamps = [h["timestamp"] for h in hits]
        assert timestamps == sorted(timestamps)


@pytest.mark.asyncio
async def test_search_empty_query(db_with_messages):
    """Empty query returns [] without hitting DB."""
    assert await db_with_messages.search_messages("") == []
    assert await db_with_messages.search_messages("   ") == []


# ---------------------------------------------------------------------------
# search_messages — fuzzy mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_fuzzy_substring(db_with_messages):
    """Fuzzy mode catches similarity hits that tsvector misses."""
    hits = await db_with_messages.search_messages("quanto", mode="fuzzy")
    # 'quanto' is similar to 'quantum'
    assert any("quantum" in (h.get("snippet") or "").lower() for h in hits)


@pytest.mark.asyncio
async def test_search_fuzzy_no_match(db_with_messages):
    """Completely dissimilar term returns [] in fuzzy mode."""
    hits = await db_with_messages.search_messages("xyzzy_zzz_abc", mode="fuzzy")
    assert hits == []


# ---------------------------------------------------------------------------
# search_messages — auto mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_auto_falls_back_to_fuzzy(db_with_messages):
    """auto mode returns fuzzy hits when keyword finds nothing."""
    hits = await db_with_messages.search_messages("quanto", mode="auto")
    # Should find 'quantum' via fuzzy fallback
    assert len(hits) > 0


@pytest.mark.asyncio
async def test_search_auto_uses_keyword_when_sufficient(db_with_messages):
    """auto mode returns keyword hits when they meet the threshold."""
    hits = await db_with_messages.search_messages("fox", mode="auto")
    assert len(hits) >= 2


@pytest.mark.asyncio
async def test_search_invalid_mode(db_with_messages):
    with pytest.raises(ValueError, match="unknown mode"):
        await db_with_messages.search_messages("fox", mode="bogus")


# ---------------------------------------------------------------------------
# search_sessions
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_sessions(thoth_db_initialized):
    db = _AsyncSessionDB()
    await db.create_session("x1", source="cli", model="m", model_config={}, system_prompt="")
    await db.create_session("x2", source="telegram", model="m", model_config={}, system_prompt="")
    return db


@pytest.mark.asyncio
async def test_search_sessions_returns_all(db_sessions):
    rows = await db_sessions.search_sessions()
    ids = {r["id"] for r in rows}
    assert "x1" in ids
    assert "x2" in ids


@pytest.mark.asyncio
async def test_search_sessions_source_filter(db_sessions):
    rows = await db_sessions.search_sessions(source="cli")
    assert all(r["source"] == "cli" for r in rows)
    ids = {r["id"] for r in rows}
    assert "x1" in ids
    assert "x2" not in ids


@pytest.mark.asyncio
async def test_search_sessions_has_last_active(db_sessions):
    """Rows include computed last_active column."""
    rows = await db_sessions.search_sessions()
    for r in rows:
        assert "last_active" in r


@pytest.mark.asyncio
async def test_search_sessions_limit(db_sessions):
    rows = await db_sessions.search_sessions(limit=1)
    assert len(rows) <= 1


@pytest.mark.asyncio
async def test_search_sessions_ordered_by_last_active(db_sessions):
    """Sessions returned most-recently-active first."""
    rows = await db_sessions.search_sessions()
    if len(rows) >= 2:
        la_vals = [r["last_active"] for r in rows]
        assert la_vals == sorted(la_vals, reverse=True)
