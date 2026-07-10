"""Reproduction tests for #18370 fallout: lazy session creation regressions.

Tests cover:
1. Bug #18765 — gateway surfaces null response when agent did work
2. Prune — finalize_orphaned_compression_sessions catches ghost continuations
"""

import time

import pytest


# ===========================================================================
# Helpers
# ===========================================================================

def _make_session_db(thoth_db_initialized_sync=None):
    """Create a real SessionDB (PG-backed via thoth_db_initialized_sync fixture)."""
    from thoth_state import SessionDB
    return SessionDB()


async def _set_started_at_old_async(session_id: str, age_seconds: float = 800000) -> None:
    """Set a session's started_at to the past (for orphan-prune tests)."""
    import thoth_db
    from datetime import datetime, timezone
    old_dt = datetime.fromtimestamp(time.time() - age_seconds, tz=timezone.utc)
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE sessions SET started_at = $1 WHERE id = $2",
            old_dt,
            session_id,
        )


def _set_started_at_old(session_id: str, age_seconds: float = 800000) -> None:
    """Sync wrapper for _set_started_at_old_async."""
    import thoth_db
    thoth_db.run_sync(_set_started_at_old_async(session_id, age_seconds))


# ===========================================================================
# Bug #18765: Gateway surfaces null response
# ===========================================================================

class TestGatewaySurfacesNullResponse:
    """When the agent does work (api_calls > 0) but returns no final_response,
    the gateway must surface an error to the user instead of silently sending
    nothing. Tests exercise the production _normalize_empty_agent_response helper."""

    def test_partial_response_surfaces_error(self):
        """Agent returns partial=True with no response → user sees error."""
        from gateway.run import _normalize_empty_agent_response

        agent_result = {
            "final_response": None,
            "api_calls": 5,
            "partial": True,
            "interrupted": False,
            "error": "Model generated invalid tool call: nonexistent_tool",
        }

        response = agent_result.get("final_response") or ""
        response = _normalize_empty_agent_response(
            agent_result, response, history_len=10,
        )

        assert response != "", "Null response with api_calls>0 must be surfaced"
        assert "nonexistent_tool" in response

    def test_interrupted_response_stays_empty(self):
        """Interrupted agent → response stays empty (platform handles UX)."""
        from gateway.run import _normalize_empty_agent_response

        agent_result = {
            "final_response": None,
            "api_calls": 3,
            "partial": False,
            "interrupted": True,
        }

        response = agent_result.get("final_response") or ""
        response = _normalize_empty_agent_response(
            agent_result, response, history_len=10,
        )

        assert response == "", "Interrupted turns should not get synthetic responses"

    def test_failed_context_overflow(self):
        """Agent failed with context overflow → specific guidance message."""
        from gateway.run import _normalize_empty_agent_response

        agent_result = {
            "final_response": None,
            "api_calls": 0,
            "failed": True,
            "error": "400 Bad Request: context length exceeded",
        }

        response = agent_result.get("final_response") or ""
        response = _normalize_empty_agent_response(
            agent_result, response, history_len=60,
        )

        assert "context window" in response
        assert "/compact" in response

    def test_failed_generic_error(self):
        """Agent failed with non-context error → generic error message."""
        from gateway.run import _normalize_empty_agent_response

        agent_result = {
            "final_response": None,
            "api_calls": 0,
            "failed": True,
            "error": "500 Internal Server Error",
        }

        response = agent_result.get("final_response") or ""
        response = _normalize_empty_agent_response(
            agent_result, response, history_len=5,
        )

        assert "500 Internal Server Error" in response
        assert "/reset" in response

    def test_nonempty_response_passes_through(self):
        """Non-empty response is returned unchanged."""
        from gateway.run import _normalize_empty_agent_response

        agent_result = {"final_response": "Hello!", "api_calls": 1}
        response = "Hello!"
        result = _normalize_empty_agent_response(
            agent_result, response, history_len=5,
        )

        assert result == "Hello!"


# ===========================================================================
# Prune: finalize_orphaned_compression_sessions
# ===========================================================================

class TestFinalizeOrphanedCompressionSessions:
    """The prune migration marks ghost compression continuations as ended."""

    def test_marks_ghost_continuation_with_compression_parent(self, thoth_db_initialized_sync):
        """Ghost session with compression-ended parent + messages → finalized."""
        import thoth_db
        db = _make_session_db()

        # Parent session (ended by compression — this is the key condition)
        thoth_db.run_sync(db.create_session(session_id="parent", source="tui", model="test"))
        thoth_db.run_sync(db.end_session("parent", "compression"))

        # Ghost continuation (has messages, never finalized)
        thoth_db.run_sync(db.create_session(
            session_id="ghost-cont",
            source="tui",
            model="test",
            parent_session_id="parent",
        ))
        thoth_db.run_sync(db.append_message("ghost-cont", role="user", content="hello"))
        thoth_db.run_sync(db.append_message("ghost-cont", role="assistant", content="hi"))

        # Make it old enough (fake started_at)
        _set_started_at_old("ghost-cont")

        count = thoth_db.run_sync(db.finalize_orphaned_compression_sessions())
        assert count == 1

        session = thoth_db.run_sync(db.get_session("ghost-cont"))
        assert session["ended_at"] is not None
        assert session["end_reason"] == "orphaned_compression"

    def test_skips_session_without_parent(self, thoth_db_initialized_sync):
        """Ghost session without parent_session_id is NOT a compression
        continuation — should not be touched by this prune."""
        import thoth_db
        db = _make_session_db()

        thoth_db.run_sync(db.create_session(session_id="ghost-notitle", source="tui", model="test"))
        thoth_db.run_sync(db.append_message("ghost-notitle", role="user", content="test"))
        _set_started_at_old("ghost-notitle")

        count = thoth_db.run_sync(db.finalize_orphaned_compression_sessions())
        assert count == 0

    def test_skips_recent_sessions(self, thoth_db_initialized_sync):
        """Sessions younger than 7 days are not touched."""
        import thoth_db
        db = _make_session_db()

        # Create parent first to satisfy FK constraint
        thoth_db.run_sync(db.create_session(session_id="some-parent", source="tui", model="test"))
        thoth_db.run_sync(db.create_session(
            session_id="recent",
            source="tui",
            model="test",
            parent_session_id="some-parent",
        ))
        thoth_db.run_sync(db.append_message("recent", role="user", content="hello"))
        # started_at is now() — within 7 days

        count = thoth_db.run_sync(db.finalize_orphaned_compression_sessions())
        assert count == 0

    def test_skips_sessions_with_end_reason(self, thoth_db_initialized_sync):
        """Properly finalized sessions (even without api_call_count) are skipped."""
        import thoth_db
        db = _make_session_db()

        # Create parent first to satisfy FK constraint
        thoth_db.run_sync(db.create_session(session_id="parent", source="tui", model="test"))
        thoth_db.run_sync(db.end_session("parent", "compression"))

        thoth_db.run_sync(db.create_session(
            session_id="already-ended",
            source="tui",
            model="test",
            parent_session_id="parent",
        ))
        thoth_db.run_sync(db.append_message("already-ended", role="user", content="hello"))
        thoth_db.run_sync(db.end_session("already-ended", "user_exit"))
        _set_started_at_old("already-ended")

        count = thoth_db.run_sync(db.finalize_orphaned_compression_sessions())
        assert count == 0

    def test_skips_session_with_non_compression_parent(self, thoth_db_initialized_sync):
        """Child session whose parent was NOT ended by compression should
        not be touched — it's not from the compression continuation path."""
        import thoth_db
        db = _make_session_db()

        # Parent ended by user_exit, not compression
        thoth_db.run_sync(db.create_session(session_id="parent", source="tui", model="test"))
        thoth_db.run_sync(db.end_session("parent", "user_exit"))

        thoth_db.run_sync(db.create_session(
            session_id="child",
            source="tui",
            model="test",
            parent_session_id="parent",
        ))
        thoth_db.run_sync(db.append_message("child", role="user", content="hello"))
        _set_started_at_old("child")

        count = thoth_db.run_sync(db.finalize_orphaned_compression_sessions())
        assert count == 0

    def test_skips_sessions_without_messages(self, thoth_db_initialized_sync):
        """Empty sessions (no messages) are NOT targeted by this prune —
        those are handled by prune_empty_ghost_sessions()."""
        import thoth_db
        db = _make_session_db()

        # Create parent first to satisfy FK constraint
        thoth_db.run_sync(db.create_session(session_id="parent", source="tui", model="test"))
        thoth_db.run_sync(db.end_session("parent", "compression"))

        thoth_db.run_sync(db.create_session(
            session_id="empty-ghost",
            source="tui",
            model="test",
            parent_session_id="parent",
        ))
        # No messages appended
        _set_started_at_old("empty-ghost")

        count = thoth_db.run_sync(db.finalize_orphaned_compression_sessions())
        assert count == 0

    def test_titled_ghost_with_parent_is_caught(self, thoth_db_initialized_sync):
        """Ghost continuation that HAS a title (propagated from parent by
        _compress_context) is still caught via parent with end_reason='compression'."""
        import thoth_db
        db = _make_session_db()

        # Create parent first — ended by compression
        thoth_db.run_sync(db.create_session(session_id="parent", source="tui", model="test"))
        thoth_db.run_sync(db.set_session_title("parent", "Chat"))
        thoth_db.run_sync(db.end_session("parent", "compression"))

        thoth_db.run_sync(db.create_session(
            session_id="titled-ghost",
            source="tui",
            model="test",
            parent_session_id="parent",
        ))
        thoth_db.run_sync(db.set_session_title("titled-ghost", "Chat (2)"))
        thoth_db.run_sync(db.append_message("titled-ghost", role="user", content="continued..."))
        _set_started_at_old("titled-ghost")

        count = thoth_db.run_sync(db.finalize_orphaned_compression_sessions())
        assert count == 1

        session = thoth_db.run_sync(db.get_session("titled-ghost"))
        assert session["end_reason"] == "orphaned_compression"
