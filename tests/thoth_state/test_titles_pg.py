"""TDD tests for _AsyncSessionDB title methods (Phase 0 Task 9).

All tests run against the docker-compose postgres cluster via the
thoth_db_initialized fixture. Docker must be running:
    docker compose up -d postgres

Upstream behavior (matched from thoth_state.py:992-1148):
  - MAX_TITLE_LENGTH = 100
  - sanitize_title raises ValueError if cleaned title > 100 chars
  - Lineage suffix format is " #N" (not " (N)")
  - set_session_title raises ValueError on title conflict with another session
  - resolve_session_by_title prefers numbered variants over exact match
"""
import pytest
import pytest_asyncio
from thoth_state import _AsyncSessionDB


@pytest_asyncio.fixture
async def db(thoth_db_initialized):
    d = _AsyncSessionDB()
    await d.create_session(
        session_id="t1", source="cli", model="m", model_config={}, system_prompt=""
    )
    await d.create_session(
        session_id="t2",
        source="cli",
        model="m",
        model_config={},
        system_prompt="",
        parent_session_id="t1",
    )
    return d


# ── sanitize_title (static, no DB needed) ───────────────────────────────────

def test_sanitize_title_strips_whitespace():
    assert _AsyncSessionDB.sanitize_title("  hello   world  ") == "hello world"


def test_sanitize_title_empty_returns_none():
    assert _AsyncSessionDB.sanitize_title("") is None
    assert _AsyncSessionDB.sanitize_title("   ") is None
    assert _AsyncSessionDB.sanitize_title(None) is None


def test_sanitize_title_exactly_100_chars_ok():
    title = "x" * 100
    assert _AsyncSessionDB.sanitize_title(title) == title


def test_sanitize_title_over_100_raises():
    long = "x" * 101
    with pytest.raises(ValueError, match="too long"):
        _AsyncSessionDB.sanitize_title(long)


def test_sanitize_title_removes_control_chars():
    # ASCII control chars are stripped (not replaced with spaces)
    # \x01 and \x07 are removed, leaving no space between "hello" and "world"
    assert _AsyncSessionDB.sanitize_title("hello\x01\x07world") == "helloworld"
    # But \t, \n, \r are treated as whitespace and collapsed to a single space
    assert _AsyncSessionDB.sanitize_title("hello\x01 \x07 world") == "hello world"


def test_sanitize_title_collapses_tabs_and_newlines():
    assert _AsyncSessionDB.sanitize_title("a\tb\nc") == "a b c"


# ── set_session_title / get_session_title ───────────────────────────────────

@pytest.mark.asyncio
async def test_set_and_get_title(db):
    ok = await db.set_session_title("t1", "My Session")
    assert ok is True
    assert await db.get_session_title("t1") == "My Session"


@pytest.mark.asyncio
async def test_set_title_whitespace_only_clears_existing(db):
    """Whitespace-only / empty title clears whatever title is set.

    PR #23 changed ``set_session_title`` so empty input means "clear"
    rather than "no-op" — the dashboard's /retitle "" flow and
    ``tests/acp/test_session.py::test_list_sessions_prefers_title_then_preview``
    both rely on this. Returns True iff the row was actually touched
    (i.e. the session exists).
    """
    await db.set_session_title("t1", "Initial")
    assert await db.get_session_title("t1") == "Initial"
    # Whitespace clears the title to NULL — and reports the UPDATE ran.
    result = await db.set_session_title("t1", "   ")
    assert result is True
    assert await db.get_session_title("t1") is None
    # Empty input against a session with NO title leaves it NULL and
    # still reports True (the UPDATE matched a row).
    result = await db.set_session_title("t1", "")
    assert result is True
    assert await db.get_session_title("t1") is None
    # Unknown session id → no row matched → False.
    assert await db.set_session_title("nonexistent", "") is False


@pytest.mark.asyncio
async def test_set_title_conflict_raises(db):
    await db.set_session_title("t1", "Taken")
    with pytest.raises(ValueError, match="already in use"):
        await db.set_session_title("t2", "Taken")


@pytest.mark.asyncio
async def test_set_title_same_session_keeps_own_title(db):
    # A session can re-set its own title without raising
    await db.set_session_title("t1", "My Title")
    ok = await db.set_session_title("t1", "My Title")
    assert ok is True


@pytest.mark.asyncio
async def test_get_title_nonexistent_session_returns_none(db):
    result = await db.get_session_title("no-such-session")
    assert result is None


# ── get_session_by_title ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_session_by_title_found(db):
    await db.set_session_title("t1", "Unique Title")
    found = await db.get_session_by_title("Unique Title")
    assert found is not None
    assert found["id"] == "t1"


@pytest.mark.asyncio
async def test_get_session_by_title_not_found(db):
    result = await db.get_session_by_title("No Such Title")
    assert result is None


# ── resolve_session_by_title ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_session_by_title_exact(db):
    await db.set_session_title("t1", "Exact Title")
    sid = await db.resolve_session_by_title("Exact Title")
    assert sid == "t1"


@pytest.mark.asyncio
async def test_resolve_session_by_title_prefers_numbered_variant(db):
    # When a numbered variant " #2" exists, resolve prefers it over exact
    await db.set_session_title("t1", "Project")
    await db.set_session_title("t2", "Project #2")
    sid = await db.resolve_session_by_title("Project")
    assert sid == "t2"


@pytest.mark.asyncio
async def test_resolve_session_by_title_no_match_returns_none(db):
    result = await db.resolve_session_by_title("Nonexistent")
    assert result is None


# ── get_next_title_in_lineage ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_next_title_in_lineage_no_conflict(db):
    # No existing sessions with that title → returns the base name as-is
    nxt = await db.get_next_title_in_lineage("BrandNew")
    assert nxt == "BrandNew"


@pytest.mark.asyncio
async def test_get_next_title_in_lineage_with_base(db):
    await db.set_session_title("t1", "Project")
    nxt = await db.get_next_title_in_lineage("Project")
    assert nxt == "Project #2"


@pytest.mark.asyncio
async def test_get_next_title_in_lineage_increments(db):
    await db.set_session_title("t1", "Project")
    await db.set_session_title("t2", "Project #2")
    nxt = await db.get_next_title_in_lineage("Project")
    assert nxt == "Project #3"


@pytest.mark.asyncio
async def test_get_next_title_in_lineage_strips_existing_suffix(db):
    # Calling with "Project #2" should strip the suffix and use "Project" as base
    await db.set_session_title("t1", "Project")
    await db.set_session_title("t2", "Project #2")
    nxt = await db.get_next_title_in_lineage("Project #2")
    assert nxt == "Project #3"
