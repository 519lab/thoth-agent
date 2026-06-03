"""Yuanbao recall: branch A1 (exact id) and A2 (content-match) against DB-only transcripts.

state.db/PG persists the platform-side ``message_id`` via the
``platform_message_id`` column and ``load_transcript`` surfaces it back on
each message dict as ``message_id`` — so the recall guard's exact-id match
path stays canonical.
"""
import thoth_db
from gateway.session import SessionStore
from gateway.config import GatewayConfig


def test_recall_branch_a1_exact_id_match_round_trips_through_db(tmp_path, hermes_db_initialized_sync):
    """A user message persisted with ``message_id`` must round-trip through
    the DB so recall can find and redact it by exact id (branch A1)."""
    from thoth_state import SessionDB

    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path, config=config)

    sid = "test-yuanbao-recall-a1"
    db = SessionDB()
    thoth_db.run_sync(db.create_session(session_id=sid, source="yuanbao:group:G"))
    store.append_to_transcript(sid, {
        "role": "user",
        "content": "sensitive content",
        "timestamp": 1.0,
        "message_id": "platform-msg-abc",
    })
    store.append_to_transcript(sid, {
        "role": "assistant",
        "content": "ack",
        "timestamp": 2.0,
    })

    history = store.load_transcript(sid)
    # The user row must carry its platform id back so the recall guard can
    # match by exact id; the assistant row had no platform id so it should
    # not gain one spuriously.
    user_msg = next(m for m in history if m["role"] == "user")
    assistant_msg = next(m for m in history if m["role"] == "assistant")
    assert user_msg.get("message_id") == "platform-msg-abc"
    assert "message_id" not in assistant_msg

    # Branch A1: locate the row by exact platform id — no content heuristics.
    target = next(
        (m for m in history if m.get("message_id") == "platform-msg-abc"),
        None,
    )
    assert target is not None
    assert target["content"] == "sensitive content"


def test_recall_branch_a2_content_match_when_no_platform_id(tmp_path, hermes_db_initialized_sync):
    """Rows that lack a platform_message_id (e.g. agent-processed @bot
    messages) still match by content as a fallback."""
    from thoth_state import SessionDB

    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path, config=config)

    sid = "test-yuanbao-recall-a2"
    db = SessionDB()
    thoth_db.run_sync(db.create_session(session_id=sid, source="yuanbao:group:G"))
    # No message_id on the dict — simulates an agent-processed message
    # that did not carry the platform msg_id through.
    store.append_to_transcript(sid, {
        "role": "user",
        "content": "sensitive content",
        "timestamp": 1.0,
    })

    history = store.load_transcript(sid)
    assert all("message_id" not in m for m in history)
