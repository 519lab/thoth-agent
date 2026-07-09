"""Unit tests for gateway/session_key.py.

Imports from the extracted module's canonical home (``gateway.session_key``)
rather than the ``gateway.run`` re-export. The re-export surface on
``gateway.run`` is exercised by tests/gateway/test_background_process_notifications.py.
"""

from gateway.config import Platform
from gateway.session_key import _parse_session_key, _platform_config_key


def test_platform_config_key_maps_local_to_cli():
    assert _platform_config_key(Platform.LOCAL) == "cli"
    # Non-local platforms map to their enum value.
    assert _platform_config_key(Platform.TELEGRAM) == Platform.TELEGRAM.value


def test_parse_session_key_group_has_no_thread_id():
    result = _parse_session_key("agent:main:telegram:group:-100")
    assert result == {"platform": "telegram", "chat_type": "group", "chat_id": "-100"}


def test_parse_session_key_thread_carries_thread_id():
    result = _parse_session_key("agent:main:discord:thread:chan123:thread456")
    assert result == {
        "platform": "discord",
        "chat_type": "thread",
        "chat_id": "chan123",
        "thread_id": "thread456",
    }


def test_parse_session_key_group_suffix_is_not_thread_id():
    # A 6th element on a group/channel key is NOT a thread_id (may be a user_id).
    result = _parse_session_key("agent:main:slack:group:chan:user9")
    assert result is not None
    assert "thread_id" not in result


def test_parse_session_key_rejects_malformed():
    assert _parse_session_key("nope") is None
    assert _parse_session_key("agent:other:telegram:dm:1") is None
    assert _parse_session_key("agent:main:telegram:dm") is None  # too few parts
