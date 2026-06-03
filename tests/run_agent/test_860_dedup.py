"""Tests for issue #860 — session transcript deduplication.

Verifies that:
1. _flush_messages_to_session_db uses _last_flushed_db_idx to avoid re-writing
2. Multiple _persist_session calls don't duplicate messages
3. append_to_transcript(skip_db=True) skips the session DB write but writes JSONL
4. The gateway doesn't double-write messages the agent already persisted

Phase 0 port note: SessionDB became ``_AsyncSessionDB`` (re-exported as
``SessionDB``). Methods like ``create_session`` and ``get_messages`` are
now coroutines that have to be driven through ``thoth_db.run_sync`` from
sync test bodies; the ``thoth_db_initialized_sync`` fixture binds the
asyncpg pool to the per-test PG database (and runs Alembic upgrade head)
so those calls land in a migrated schema. The historical ``db_path=``
kwarg is accepted-and-ignored by ``_AsyncSessionDB.__init__``.
"""

import os
from unittest.mock import patch

import pytest

import thoth_db


# ---------------------------------------------------------------------------
# Test: _flush_messages_to_session_db only writes new messages
# ---------------------------------------------------------------------------

class TestFlushDeduplication:
    """Verify _flush_messages_to_session_db tracks what it already wrote."""

    def _make_agent(self, session_db):
        """Create a minimal AIAgent with a real session DB."""
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                session_db=session_db,
                session_id="test-session-860",
                skip_context_files=True,
                skip_memory=True,
            )
        # Simulate lazy session creation (normally done by run_conversation)
        agent._ensure_db_session()
        return agent

    def test_flush_writes_only_new_messages(self, thoth_db_initialized_sync):
        """First flush writes all new messages, second flush writes none."""
        from thoth_state import SessionDB

        db = SessionDB()
        agent = self._make_agent(db)

        conversation_history = [
            {"role": "user", "content": "old message"},
        ]
        messages = list(conversation_history) + [
            {"role": "user", "content": "new question"},
            {"role": "assistant", "content": "new answer"},
        ]

        # First flush — should write 2 new messages
        agent._flush_messages_to_session_db(messages, conversation_history)

        rows = thoth_db.run_sync(db.get_messages(agent.session_id))
        assert len(rows) == 2, f"Expected 2 messages, got {len(rows)}"

        # Second flush with SAME messages — should write 0 new messages
        agent._flush_messages_to_session_db(messages, conversation_history)

        rows = thoth_db.run_sync(db.get_messages(agent.session_id))
        assert len(rows) == 2, f"Expected still 2 messages after second flush, got {len(rows)}"

    def test_flush_writes_incrementally(self, thoth_db_initialized_sync):
        """Messages added between flushes are written exactly once."""
        from thoth_state import SessionDB

        db = SessionDB()
        agent = self._make_agent(db)

        conversation_history = []
        messages = [
            {"role": "user", "content": "hello"},
        ]

        # First flush — 1 message
        agent._flush_messages_to_session_db(messages, conversation_history)
        rows = thoth_db.run_sync(db.get_messages(agent.session_id))
        assert len(rows) == 1

        # Add more messages
        messages.append({"role": "assistant", "content": "hi there"})
        messages.append({"role": "user", "content": "follow up"})

        # Second flush — should write only 2 new messages
        agent._flush_messages_to_session_db(messages, conversation_history)
        rows = thoth_db.run_sync(db.get_messages(agent.session_id))
        assert len(rows) == 3, f"Expected 3 total messages, got {len(rows)}"

    def test_persist_session_multiple_calls_no_duplication(
        self, thoth_db_initialized_sync
    ):
        """Multiple _persist_session calls don't duplicate DB entries."""
        from thoth_state import SessionDB

        db = SessionDB()
        agent = self._make_agent(db)

        conversation_history = [{"role": "user", "content": "old"}]
        messages = list(conversation_history) + [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]

        # Simulate multiple persist calls (like the agent's many exit paths)
        for _ in range(5):
            agent._persist_session(messages, conversation_history)

        rows = thoth_db.run_sync(db.get_messages(agent.session_id))
        assert len(rows) == 4, f"Expected 4 messages, got {len(rows)} (duplication bug!)"

    def test_flush_reset_after_compression(self, thoth_db_initialized_sync):
        """After compression creates a new session, flush index resets."""
        from thoth_state import SessionDB

        db = SessionDB()
        agent = self._make_agent(db)

        # Write some messages
        messages = [
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "reply1"},
        ]
        agent._flush_messages_to_session_db(messages, [])

        old_session = agent.session_id
        assert agent._last_flushed_db_idx == 2

        # Simulate what _compress_context does: new session, reset idx
        agent.session_id = "compressed-session-new"
        thoth_db.run_sync(
            db.create_session(session_id=agent.session_id, source="test")
        )
        agent._last_flushed_db_idx = 0

        # Now flush compressed messages to new session
        compressed_messages = [
            {"role": "user", "content": "summary of conversation"},
        ]
        agent._flush_messages_to_session_db(compressed_messages, [])

        new_rows = thoth_db.run_sync(db.get_messages(agent.session_id))
        assert len(new_rows) == 1

        # Old session should still have its 2 messages
        old_rows = thoth_db.run_sync(db.get_messages(old_session))
        assert len(old_rows) == 2


# ---------------------------------------------------------------------------
# Test: append_to_transcript skip_db parameter
# ---------------------------------------------------------------------------

class TestAppendToTranscriptSkipDb:
    """Verify skip_db=True skips the SQLite write."""

    def test_skip_db_prevents_sqlite_write(self, tmp_path, thoth_db_initialized_sync):
        """With skip_db=True and a real session DB, message does NOT land in the DB."""
        from gateway.config import GatewayConfig
        from gateway.session import SessionStore
        from thoth_state import SessionDB

        db = SessionDB()

        config = GatewayConfig()
        with patch("gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(sessions_dir=tmp_path, config=config)
        store._db = db
        store._loaded = True

        session_id = "test-skip-db-real"
        thoth_db.run_sync(db.create_session(session_id=session_id, source="test"))

        msg = {"role": "assistant", "content": "hello world"}
        store.append_to_transcript(session_id, msg, skip_db=True)

        # Session DB should NOT have the message
        rows = thoth_db.run_sync(db.get_messages(session_id))
        assert len(rows) == 0, f"Expected 0 DB rows with skip_db=True, got {len(rows)}"

    def test_default_writes_to_sqlite(self, tmp_path, thoth_db_initialized_sync):
        """Without skip_db, message appears in the session DB."""
        from gateway.config import GatewayConfig
        from gateway.session import SessionStore
        from thoth_state import SessionDB

        db = SessionDB()

        config = GatewayConfig()
        with patch("gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(sessions_dir=tmp_path, config=config)
        store._db = db
        store._loaded = True

        session_id = "test-default-write"
        thoth_db.run_sync(db.create_session(session_id=session_id, source="test"))

        msg = {"role": "user", "content": "test message"}
        store.append_to_transcript(session_id, msg)

        # Session DB should have the message
        rows = thoth_db.run_sync(db.get_messages(session_id))
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Test: _last_flushed_db_idx initialization
# ---------------------------------------------------------------------------

class TestFlushIdxInit:
    """Verify _last_flushed_db_idx is properly initialized."""

    def test_init_zero(self):
        """Agent starts with _last_flushed_db_idx = 0."""
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        assert agent._last_flushed_db_idx == 0

    def test_no_session_db_noop(self):
        """Without session_db, flush is a no-op and doesn't crash."""
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        messages = [{"role": "user", "content": "test"}]
        agent._flush_messages_to_session_db(messages, [])
        # Should not crash, idx should remain 0
        assert agent._last_flushed_db_idx == 0
