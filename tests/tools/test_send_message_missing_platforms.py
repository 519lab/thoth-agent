"""Tests for _send_mattermost, _send_matrix, _send_homeassistant."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from tools.send_message_tool import (
    _send_homeassistant,
    _send_mattermost,
    _send_matrix,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_aiohttp_resp(status, json_data=None, text_data=None):
    """Build a minimal async-context-manager mock for an aiohttp response."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text_data or "")
    return resp


def _make_aiohttp_session(resp):
    """Wrap a response mock in a session mock that supports async-with for post/put."""
    request_ctx = MagicMock()
    request_ctx.__aenter__ = AsyncMock(return_value=resp)
    request_ctx.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=request_ctx)
    session.put = MagicMock(return_value=request_ctx)

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    return session_ctx, session


# ---------------------------------------------------------------------------
# _send_mattermost
# ---------------------------------------------------------------------------


class TestSendMattermost:
    def test_success(self):
        resp = _make_aiohttp_resp(201, json_data={"id": "post123"})
        session_ctx, session = _make_aiohttp_session(resp)

        with patch("aiohttp.ClientSession", return_value=session_ctx), \
             patch.dict(os.environ, {"MATTERMOST_URL": "", "MATTERMOST_TOKEN": ""}, clear=False):
            extra = {"url": "https://mm.example.com"}
            result = asyncio.run(_send_mattermost("tok-abc", extra, "channel1", "hello"))

        assert result == {"success": True, "platform": "mattermost", "chat_id": "channel1", "message_id": "post123"}
        session.post.assert_called_once()
        call_kwargs = session.post.call_args
        assert call_kwargs[0][0] == "https://mm.example.com/api/v4/posts"
        assert call_kwargs[1]["headers"]["Authorization"] == "Bearer tok-abc"
        assert call_kwargs[1]["json"] == {"channel_id": "channel1", "message": "hello"}

    def test_http_error(self):
        resp = _make_aiohttp_resp(400, text_data="Bad Request")
        session_ctx, _ = _make_aiohttp_session(resp)

        with patch("aiohttp.ClientSession", return_value=session_ctx):
            result = asyncio.run(_send_mattermost(
                "tok", {"url": "https://mm.example.com"}, "ch", "hi"
            ))

        assert "error" in result
        assert "400" in result["error"]
        assert "Bad Request" in result["error"]

    def test_missing_config(self):
        with patch.dict(os.environ, {"MATTERMOST_URL": "", "MATTERMOST_TOKEN": ""}, clear=False):
            result = asyncio.run(_send_mattermost("", {}, "ch", "hi"))

        assert "error" in result
        assert "MATTERMOST_URL" in result["error"] or "not configured" in result["error"]

    def test_env_var_fallback(self):
        resp = _make_aiohttp_resp(200, json_data={"id": "p99"})
        session_ctx, session = _make_aiohttp_session(resp)

        with patch("aiohttp.ClientSession", return_value=session_ctx), \
             patch.dict(os.environ, {"MATTERMOST_URL": "https://mm.env.com", "MATTERMOST_TOKEN": "env-tok"}, clear=False):
            result = asyncio.run(_send_mattermost("", {}, "ch", "hi"))

        assert result["success"] is True
        call_kwargs = session.post.call_args
        assert "https://mm.env.com" in call_kwargs[0][0]
        assert call_kwargs[1]["headers"]["Authorization"] == "Bearer env-tok"


# ---------------------------------------------------------------------------
# _send_matrix
# ---------------------------------------------------------------------------


class TestSendMatrix:
    def test_success(self):
        resp = _make_aiohttp_resp(200, json_data={"event_id": "$abc123"})
        session_ctx, session = _make_aiohttp_session(resp)

        with patch("aiohttp.ClientSession", return_value=session_ctx), \
             patch.dict(os.environ, {"MATRIX_HOMESERVER": "", "MATRIX_ACCESS_TOKEN": ""}, clear=False):
            extra = {"homeserver": "https://matrix.example.com"}
            result = asyncio.run(_send_matrix("syt_tok", extra, "!room:example.com", "hello matrix"))

        assert result == {
            "success": True,
            "platform": "matrix",
            "chat_id": "!room:example.com",
            "message_id": "$abc123",
        }
        session.put.assert_called_once()
        call_kwargs = session.put.call_args
        url = call_kwargs[0][0]
        assert url.startswith("https://matrix.example.com/_matrix/client/v3/rooms/%21room%3Aexample.com/send/m.room.message/")
        assert call_kwargs[1]["headers"]["Authorization"] == "Bearer syt_tok"
        payload = call_kwargs[1]["json"]
        assert payload["msgtype"] == "m.text"
        assert payload["body"] == "hello matrix"

    def test_http_error(self):
        resp = _make_aiohttp_resp(403, text_data="Forbidden")
        session_ctx, _ = _make_aiohttp_session(resp)

        with patch("aiohttp.ClientSession", return_value=session_ctx):
            result = asyncio.run(_send_matrix(
                "tok", {"homeserver": "https://matrix.example.com"},
                "!room:example.com", "hi"
            ))

        assert "error" in result
        assert "403" in result["error"]
        assert "Forbidden" in result["error"]

    def test_missing_config(self):
        with patch.dict(os.environ, {"MATRIX_HOMESERVER": "", "MATRIX_ACCESS_TOKEN": ""}, clear=False):
            result = asyncio.run(_send_matrix("", {}, "!room:example.com", "hi"))

        assert "error" in result
        assert "MATRIX_HOMESERVER" in result["error"] or "not configured" in result["error"]

    def test_env_var_fallback(self):
        resp = _make_aiohttp_resp(200, json_data={"event_id": "$ev1"})
        session_ctx, session = _make_aiohttp_session(resp)

        with patch("aiohttp.ClientSession", return_value=session_ctx), \
             patch.dict(os.environ, {
                 "MATRIX_HOMESERVER": "https://matrix.env.com",
                 "MATRIX_ACCESS_TOKEN": "env-tok",
             }, clear=False):
            result = asyncio.run(_send_matrix("", {}, "!r:env.com", "hi"))

        assert result["success"] is True
        url = session.put.call_args[0][0]
        assert "matrix.env.com" in url

    def test_txn_id_is_unique_across_calls(self):
        """Each call should generate a distinct transaction ID in the URL."""
        txn_ids = []

        def capture(*args, **kwargs):
            url = args[0]
            txn_ids.append(url.rsplit("/", 1)[-1])
            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(return_value=_make_aiohttp_resp(200, json_data={"event_id": "$x"}))
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        session = MagicMock()
        session.put = capture
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)

        extra = {"homeserver": "https://matrix.example.com"}

        import time
        with patch("aiohttp.ClientSession", return_value=session_ctx):
            asyncio.run(_send_matrix("tok", extra, "!r:example.com", "first"))
        time.sleep(0.002)
        with patch("aiohttp.ClientSession", return_value=session_ctx):
            asyncio.run(_send_matrix("tok", extra, "!r:example.com", "second"))

        assert len(txn_ids) == 2
        assert txn_ids[0] != txn_ids[1]


# ---------------------------------------------------------------------------
# _send_homeassistant
# ---------------------------------------------------------------------------


class TestSendHomeAssistant:
    def test_success(self):
        resp = _make_aiohttp_resp(200)
        session_ctx, session = _make_aiohttp_session(resp)

        with patch("aiohttp.ClientSession", return_value=session_ctx), \
             patch.dict(os.environ, {"HASS_URL": "", "HASS_TOKEN": ""}, clear=False):
            extra = {"url": "https://hass.example.com"}
            result = asyncio.run(_send_homeassistant("hass-tok", extra, "mobile_app_phone", "alert!"))

        assert result == {"success": True, "platform": "homeassistant", "chat_id": "mobile_app_phone"}
        session.post.assert_called_once()
        call_kwargs = session.post.call_args
        assert call_kwargs[0][0] == "https://hass.example.com/api/services/notify/notify"
        assert call_kwargs[1]["headers"]["Authorization"] == "Bearer hass-tok"
        assert call_kwargs[1]["json"] == {"message": "alert!", "target": "mobile_app_phone"}

    def test_http_error(self):
        resp = _make_aiohttp_resp(401, text_data="Unauthorized")
        session_ctx, _ = _make_aiohttp_session(resp)

        with patch("aiohttp.ClientSession", return_value=session_ctx):
            result = asyncio.run(_send_homeassistant(
                "bad-tok", {"url": "https://hass.example.com"},
                "target", "msg"
            ))

        assert "error" in result
        assert "401" in result["error"]
        assert "Unauthorized" in result["error"]

    def test_missing_config(self):
        with patch.dict(os.environ, {"HASS_URL": "", "HASS_TOKEN": ""}, clear=False):
            result = asyncio.run(_send_homeassistant("", {}, "target", "msg"))

        assert "error" in result
        assert "HASS_URL" in result["error"] or "not configured" in result["error"]

    def test_env_var_fallback(self):
        resp = _make_aiohttp_resp(200)
        session_ctx, session = _make_aiohttp_session(resp)

        with patch("aiohttp.ClientSession", return_value=session_ctx), \
             patch.dict(os.environ, {"HASS_URL": "https://hass.env.com", "HASS_TOKEN": "env-tok"}, clear=False):
            result = asyncio.run(_send_homeassistant("", {}, "notify_target", "hi"))

        assert result["success"] is True
        url = session.post.call_args[0][0]
        assert "hass.env.com" in url

