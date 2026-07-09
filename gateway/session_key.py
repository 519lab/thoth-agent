"""Session-key and platform-key parsing helpers for the gateway.

Pure helpers that map between platform identity and the gateway's session-key
string format:

- ``_platform_config_key`` — map a ``Platform`` enum to its config.yaml key, and
- ``_parse_session_key`` — parse an ``agent:main:{platform}:{chat_type}:{chat_id}``
  session key into its component parts.

Extracted verbatim from ``gateway/run.py`` (issue #311, gateway sprawl umbrella).
The group has no ``GatewayRunner``/``self`` coupling and no shared mutable module
state — its only dependency is the ``Platform`` enum — so this is a
behaviour-neutral move.  ``gateway.run`` re-imports both names, so existing call
sites, ``from gateway.run import ...`` in tests, and ``patch("gateway.run.<name>")``
targets continue to resolve unchanged.
"""

from gateway.config import Platform


def _platform_config_key(platform: "Platform") -> str:
    """Map a Platform enum to its config.yaml key (LOCAL→"cli", rest→enum value)."""
    return "cli" if platform == Platform.LOCAL else platform.value


def _parse_session_key(session_key: str) -> "dict | None":
    """Parse a session key into its component parts.

    Session keys follow the format
    ``agent:main:{platform}:{chat_type}:{chat_id}[:{extra}...]``.
    Returns a dict with ``platform``, ``chat_type``, ``chat_id``, and
    optionally ``thread_id`` keys, or None if the key doesn't match.

    The 6th element is only returned as ``thread_id`` for chat types where
    it is unambiguous (``dm`` and ``thread``).  For group/channel sessions
    the suffix may be a user_id (per-user isolation) rather than a
    thread_id, so we leave ``thread_id`` out to avoid mis-routing.
    """
    parts = session_key.split(":")
    if len(parts) >= 5 and parts[0] == "agent" and parts[1] == "main":
        result = {
            "platform": parts[2],
            "chat_type": parts[3],
            "chat_id": parts[4],
        }
        if len(parts) > 5 and parts[3] in {"dm", "thread"}:
            result["thread_id"] = parts[5]
        return result
    return None
