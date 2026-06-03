"""Verify load_transcript returns PG messages without any JSONL file."""
from pathlib import Path

import thoth_db
import pytest

from gateway.session import SessionStore
from gateway.config import GatewayConfig


def test_load_transcript_returns_db_messages_when_no_jsonl(tmp_path, thoth_db_initialized_sync):
    """Reading a transcript must work from the DB alone — no JSONL fallback needed.

    Uses thoth_db.run_sync to set up DB state from a sync test (gateway/session.py
    also uses run_sync for append_to_transcript and load_transcript, which means this
    test must remain sync to avoid the run_inside-event-loop restriction).
    """
    from thoth_state import SessionDB

    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path, config=config)

    sid = "test-session-db-only"
    db = SessionDB()
    thoth_db.run_sync(db.create_session(session_id=sid, source="test"))
    store.append_to_transcript(sid, {"role": "user", "content": "hello", "timestamp": 1.0})
    store.append_to_transcript(sid, {"role": "assistant", "content": "world", "timestamp": 2.0})

    history = store.load_transcript(sid)
    assert len(history) == 2
    assert history[0]["content"] == "hello"
    assert history[1]["content"] == "world"
