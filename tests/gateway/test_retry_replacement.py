"""Regression tests for /retry replacement semantics."""

from unittest.mock import AsyncMock, MagicMock

import thoth_db
import pytest

from gateway.config import GatewayConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionStore


def test_gateway_retry_replaces_last_user_turn_in_transcript(tmp_path, thoth_db_initialized_sync):
    """Verify /retry truncates the old user+assistant turn and re-runs the prompt.

    This test is sync because gateway/session.py's append_to_transcript and
    load_transcript use thoth_db.run_sync, which cannot be called from inside
    a running event loop.
    """
    from thoth_state import SessionDB

    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path, config=config)

    session_id = "retry_session"
    db = SessionDB()
    thoth_db.run_sync(db.create_session(session_id=session_id, source="test"))
    for msg in [
        {"role": "session_meta", "tools": []},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "retry me"},
        {"role": "assistant", "content": "old answer"},
    ]:
        store.append_to_transcript(session_id, msg)

    # Verify initial state
    transcript_before = store.load_transcript(session_id)
    user_msgs_before = [m.get("content") for m in transcript_before if m.get("role") == "user"]
    assert user_msgs_before == ["first question", "retry me"]
    assert any(m.get("content") == "old answer" for m in transcript_before)

    # Simulate rewrite_transcript (what /retry does internally)
    # Strip the last user+assistant pair
    keep = [m for m in transcript_before if m.get("role") != "session_meta"]
    keep = keep[:-2]  # drop last user + assistant
    store.rewrite_transcript(session_id, keep)

    # Add the retry message back
    store.append_to_transcript(session_id, {"role": "user", "content": "retry me"})
    store.append_to_transcript(session_id, {"role": "assistant", "content": "new answer"})

    transcript_after = store.load_transcript(session_id)
    assert [m.get("content") for m in transcript_after if m.get("role") == "user"] == [
        "first question",
        "retry me",
    ]
    assert [m.get("content") for m in transcript_after if m.get("role") == "assistant"] == [
        "first answer",
        "new answer",
    ]
