import pytest
import pytest_asyncio
from thoth_state import _AsyncSessionDB


@pytest_asyncio.fixture
async def db(hermes_db_initialized):
    return _AsyncSessionDB()


# ── apply_telegram_topic_migration ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_telegram_topic_migration_is_noop(db):
    """Should complete without error and be idempotent (Alembic owns DDL)."""
    await db.apply_telegram_topic_migration()
    await db.apply_telegram_topic_migration()


# ── enable / disable / is_enabled ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enable_then_check_topic_mode(db):
    await db.enable_telegram_topic_mode(chat_id="-100123", user_id="42")
    assert await db.is_telegram_topic_mode_enabled(chat_id="-100123", user_id="42") is True


@pytest.mark.asyncio
async def test_disable_after_enable(db):
    await db.enable_telegram_topic_mode(chat_id="-100456", user_id="7")
    assert await db.is_telegram_topic_mode_enabled(chat_id="-100456", user_id="7") is True
    await db.disable_telegram_topic_mode(chat_id="-100456")
    assert await db.is_telegram_topic_mode_enabled(chat_id="-100456", user_id="7") is False


@pytest.mark.asyncio
async def test_is_enabled_returns_false_when_not_present(db):
    assert await db.is_telegram_topic_mode_enabled(chat_id="nonexistent", user_id="99") is False


@pytest.mark.asyncio
async def test_enable_with_capability_flags(db):
    await db.enable_telegram_topic_mode(
        chat_id="-100789",
        user_id="10",
        has_topics_enabled=True,
        allows_users_to_create_topics=False,
    )
    assert await db.is_telegram_topic_mode_enabled(chat_id="-100789", user_id="10") is True


@pytest.mark.asyncio
async def test_enable_is_idempotent(db):
    """Re-enabling should update metadata but stay enabled."""
    await db.enable_telegram_topic_mode(chat_id="-100111", user_id="5")
    await db.enable_telegram_topic_mode(chat_id="-100111", user_id="5")
    assert await db.is_telegram_topic_mode_enabled(chat_id="-100111", user_id="5") is True


# ── bind / get_binding ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bind_and_lookup_topic(db):
    await db.create_session(
        session_id="tg-bind-1", source="telegram", model="m", model_config={}, system_prompt=""
    )
    await db.bind_telegram_topic(
        chat_id="-100123", thread_id="999", user_id="42",
        session_key="sk1", session_id="tg-bind-1",
    )
    b = await db.get_telegram_topic_binding(chat_id="-100123", thread_id="999")
    assert b is not None
    assert b["session_id"] == "tg-bind-1"
    assert b["user_id"] == "42"
    assert b["managed_mode"] == "auto"


@pytest.mark.asyncio
async def test_get_binding_returns_none_when_absent(db):
    assert await db.get_telegram_topic_binding(chat_id="no", thread_id="no") is None


@pytest.mark.asyncio
async def test_bind_is_idempotent_same_topic(db):
    """Rebinding same topic → same session should succeed silently."""
    await db.create_session(
        session_id="tg-idem-1", source="telegram", model="m", model_config={}, system_prompt=""
    )
    await db.bind_telegram_topic(
        chat_id="c1", thread_id="t1", user_id="u1",
        session_key="sk", session_id="tg-idem-1",
    )
    await db.bind_telegram_topic(
        chat_id="c1", thread_id="t1", user_id="u1",
        session_key="sk", session_id="tg-idem-1",
    )
    b = await db.get_telegram_topic_binding(chat_id="c1", thread_id="t1")
    assert b["session_id"] == "tg-idem-1"


@pytest.mark.asyncio
async def test_bind_raises_if_session_linked_to_different_topic(db):
    """Linking a session that's already bound to a different (chat, thread) raises."""
    await db.create_session(
        session_id="tg-conflict-1", source="telegram", model="m", model_config={}, system_prompt=""
    )
    await db.bind_telegram_topic(
        chat_id="chat-a", thread_id="thread-a", user_id="u",
        session_key="sk", session_id="tg-conflict-1",
    )
    with pytest.raises(ValueError, match="already linked"):
        await db.bind_telegram_topic(
            chat_id="chat-b", thread_id="thread-b", user_id="u",
            session_key="sk", session_id="tg-conflict-1",
        )


# ── list_bindings_for_chat ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_bindings_for_chat(db):
    for i in range(3):
        await db.create_session(
            session_id=f"tg-list-{i}", source="telegram", model="m",
            model_config={}, system_prompt=""
        )
        await db.bind_telegram_topic(
            chat_id="chat-list", thread_id=str(100 + i), user_id="u",
            session_key=f"sk{i}", session_id=f"tg-list-{i}",
        )
    results = await db.list_telegram_topic_bindings_for_chat(chat_id="chat-list")
    assert len(results) == 3
    assert all(r["chat_id"] == "chat-list" for r in results)


@pytest.mark.asyncio
async def test_list_bindings_for_chat_empty(db):
    assert await db.list_telegram_topic_bindings_for_chat(chat_id="no-such-chat") == []


# ── get_binding_by_session ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_binding_by_session(db):
    await db.create_session(
        session_id="tg-by-sess-1", source="telegram", model="m",
        model_config={}, system_prompt=""
    )
    await db.bind_telegram_topic(
        chat_id="cx", thread_id="tx", user_id="ux",
        session_key="sk", session_id="tg-by-sess-1",
    )
    b = await db.get_telegram_topic_binding_by_session(session_id="tg-by-sess-1")
    assert b is not None
    assert b["chat_id"] == "cx"
    assert b["thread_id"] == "tx"


@pytest.mark.asyncio
async def test_get_binding_by_session_returns_none_when_absent(db):
    assert await db.get_telegram_topic_binding_by_session(session_id="no-session") is None


# ── is_telegram_session_linked_to_topic ──────────────────────────────────────

@pytest.mark.asyncio
async def test_is_session_linked(db):
    await db.create_session(
        session_id="tg-linked-1", source="telegram", model="m",
        model_config={}, system_prompt=""
    )
    assert await db.is_telegram_session_linked_to_topic(session_id="tg-linked-1") is False
    await db.bind_telegram_topic(
        chat_id="clink", thread_id="tlink", user_id="ulink",
        session_key="sk", session_id="tg-linked-1",
    )
    assert await db.is_telegram_session_linked_to_topic(session_id="tg-linked-1") is True


@pytest.mark.asyncio
async def test_is_session_linked_nonexistent(db):
    assert await db.is_telegram_session_linked_to_topic(session_id="ghost") is False


# ── list_unlinked_telegram_sessions_for_user ─────────────────────────────────

@pytest.mark.asyncio
async def test_list_unlinked_sessions(db):
    """Sessions not bound to any topic should appear in the unlinked list."""
    await db.create_session(
        session_id="tg-unlinked-1", source="telegram", user_id="u42",
        model="m", model_config={}, system_prompt=""
    )
    await db.create_session(
        session_id="tg-unlinked-2", source="telegram", user_id="u42",
        model="m", model_config={}, system_prompt=""
    )
    results = await db.list_unlinked_telegram_sessions_for_user(
        chat_id="any", user_id="u42"
    )
    ids = [r["id"] for r in results]
    assert "tg-unlinked-1" in ids
    assert "tg-unlinked-2" in ids


@pytest.mark.asyncio
async def test_list_unlinked_excludes_bound_sessions(db):
    """Sessions already bound to a topic must not appear in the unlinked list."""
    await db.create_session(
        session_id="tg-bound-excl", source="telegram", user_id="u99",
        model="m", model_config={}, system_prompt=""
    )
    await db.create_session(
        session_id="tg-free-excl", source="telegram", user_id="u99",
        model="m", model_config={}, system_prompt=""
    )
    await db.bind_telegram_topic(
        chat_id="chat-excl", thread_id="thread-excl", user_id="u99",
        session_key="sk", session_id="tg-bound-excl",
    )
    results = await db.list_unlinked_telegram_sessions_for_user(
        chat_id="chat-excl", user_id="u99"
    )
    ids = [r["id"] for r in results]
    assert "tg-bound-excl" not in ids
    assert "tg-free-excl" in ids


@pytest.mark.asyncio
async def test_list_unlinked_respects_limit(db):
    for i in range(5):
        await db.create_session(
            session_id=f"tg-limit-{i}", source="telegram", user_id="ulimit",
            model="m", model_config={}, system_prompt=""
        )
    results = await db.list_unlinked_telegram_sessions_for_user(
        chat_id="any", user_id="ulimit", limit=3
    )
    assert len(results) <= 3


@pytest.mark.asyncio
async def test_list_unlinked_preview_field_present(db):
    """Each result should have a 'preview' key (may be empty string)."""
    await db.create_session(
        session_id="tg-preview-1", source="telegram", user_id="uprev",
        model="m", model_config={}, system_prompt=""
    )
    results = await db.list_unlinked_telegram_sessions_for_user(
        chat_id="any", user_id="uprev"
    )
    for r in results:
        assert "preview" in r


# ── disable clears bindings ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disable_clears_bindings_by_default(db):
    await db.enable_telegram_topic_mode(chat_id="chat-clr", user_id="u")
    await db.create_session(
        session_id="tg-clr-1", source="telegram", model="m",
        model_config={}, system_prompt=""
    )
    await db.bind_telegram_topic(
        chat_id="chat-clr", thread_id="t-clr", user_id="u",
        session_key="sk", session_id="tg-clr-1",
    )
    assert await db.get_telegram_topic_binding(chat_id="chat-clr", thread_id="t-clr") is not None
    await db.disable_telegram_topic_mode(chat_id="chat-clr")
    assert await db.get_telegram_topic_binding(chat_id="chat-clr", thread_id="t-clr") is None


@pytest.mark.asyncio
async def test_disable_preserve_bindings_when_clear_false(db):
    await db.enable_telegram_topic_mode(chat_id="chat-keep", user_id="u")
    await db.create_session(
        session_id="tg-keep-1", source="telegram", model="m",
        model_config={}, system_prompt=""
    )
    await db.bind_telegram_topic(
        chat_id="chat-keep", thread_id="t-keep", user_id="u",
        session_key="sk", session_id="tg-keep-1",
    )
    await db.disable_telegram_topic_mode(chat_id="chat-keep", clear_bindings=False)
    # Binding should still exist
    b = await db.get_telegram_topic_binding(chat_id="chat-keep", thread_id="t-keep")
    assert b is not None
    assert b["session_id"] == "tg-keep-1"
