"""Unit tests for gateway/replay.py.

Imports from the extracted module's canonical home (``gateway.replay``) rather
than the ``gateway.run`` re-export, so the module is covered on its own. The
re-export surface on ``gateway.run`` is exercised by the existing
tests/gateway/test_replay_entry_fields.py and test_restart_resume_pending.py.
"""

from gateway.replay import (
    _ASSISTANT_REPLAY_FIELDS,
    _build_replay_entry,
    _last_transcript_timestamp,
)


def test_replay_fields_contract():
    assert _ASSISTANT_REPLAY_FIELDS == (
        "reasoning",
        "reasoning_content",
        "reasoning_details",
        "codex_reasoning_items",
        "codex_message_items",
        "finish_reason",
    )


def test_build_replay_entry_preserves_and_drops():
    msg = {
        "reasoning": "thinking",
        "finish_reason": "stop",
        "reasoning_details": [],   # falsy -> dropped
        "codex_message_items": None,  # falsy -> dropped
    }
    entry = _build_replay_entry("assistant", "hello", msg)
    assert entry["role"] == "assistant"
    assert entry["content"] == "hello"
    assert entry["reasoning"] == "thinking"
    assert entry["finish_reason"] == "stop"
    assert "reasoning_details" not in entry
    assert "codex_message_items" not in entry


def test_build_replay_entry_keeps_empty_reasoning_content_sentinel():
    # Empty-string reasoning_content is a meaningful thinking-mode sentinel.
    entry = _build_replay_entry("assistant", "x", {"reasoning_content": ""})
    assert entry["reasoning_content"] == ""
    # ...but None is still dropped.
    entry_none = _build_replay_entry("assistant", "x", {"reasoning_content": None})
    assert "reasoning_content" not in entry_none


def test_build_replay_entry_non_assistant_carries_no_fields():
    entry = _build_replay_entry("user", "hi", {"reasoning": "should-not-appear"})
    assert entry == {"role": "user", "content": "hi"}


def test_last_transcript_timestamp():
    assert _last_transcript_timestamp(None) is None
    assert _last_transcript_timestamp([]) is None
    # Metadata-only rows are skipped; the last usable row's timestamp wins.
    history = [
        {"role": "user", "timestamp": 100.0},
        {"role": "assistant", "timestamp": 200.0},
        {"role": "session_meta", "timestamp": 999.0},
    ]
    assert _last_transcript_timestamp(history) == 200.0
    # A legacy row without a timestamp yields None (caller treats as fresh).
    assert _last_transcript_timestamp([{"role": "assistant"}]) is None
