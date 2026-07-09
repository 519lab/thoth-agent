"""Unit tests for the platform senders in tools/send_message_tool.py.

The companion ``test_send_message_tool.py`` already covers Telegram/Discord/
Signal/Matrix-adapter and the higher-level ``_send_to_platform`` chunking +
``_handle_send`` flows. This file fills the large coverage gap (the module sat
at ~27%) on the remaining one-shot HTTP senders — Slack, WhatsApp, Email, SMS,
Mattermost, Matrix (CS-API), Home Assistant — plus the pure
helpers ``_describe_media_for_mirror`` / ``_telegram_retry_delay`` and the
``_send_to_platform`` routing table for the non-media platforms.

All tests are hermetic: aiohttp/httpx transports and smtplib are mocked, so no
network or external service is touched.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure project root importable (mirrors sibling test files).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gateway.config import Platform  # noqa: E402
from tools.send_message_tool import (  # noqa: E402
    _describe_media_for_mirror,
    _send_email,
    _send_homeassistant,
    _send_mattermost,
    _send_matrix,
    _send_slack,
    _send_sms,
    _send_to_platform,
    _send_whatsapp,
    _telegram_retry_delay,
    send_message_tool,
)


# ---------------------------------------------------------------------------
# Test doubles for aiohttp / httpx / aiohttp.FormData
# ---------------------------------------------------------------------------


def _aiohttp_mock(status, json_data=None, text="error body"):
    """Build an aiohttp ClientSession mock whose post()/put() yields a response.

    Returns ``(session, resp)``. Both session and resp behave as async context
    managers, matching ``async with aiohttp.ClientSession() as s: async with
    s.post(...) as resp:``.
    """
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    resp.text = AsyncMock(return_value=text)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.post = MagicMock(return_value=resp)
    session.put = MagicMock(return_value=resp)
    return session, resp



class _FakeFormData:
    """aiohttp.FormData stand-in recording add_field() calls."""

    def __init__(self, *_a, **_kw):
        self.fields = {}

    def add_field(self, name, value, **_kw):
        self.fields[name] = value


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pure helpers — _describe_media_for_mirror
# ---------------------------------------------------------------------------


class TestDescribeMediaForMirror:
    def test_empty(self):
        assert _describe_media_for_mirror([]) == ""
        assert _describe_media_for_mirror(None) == ""

    def test_voice(self):
        assert _describe_media_for_mirror([("note.ogg", True)]) == "[Sent voice message]"

    def test_image(self):
        assert _describe_media_for_mirror([("pic.PNG", False)]) == "[Sent image attachment]"

    def test_video(self):
        assert _describe_media_for_mirror([("clip.mp4", False)]) == "[Sent video attachment]"

    def test_audio_non_voice(self):
        # .mp3 is in _AUDIO_EXTS but not _VOICE_EXTS
        assert _describe_media_for_mirror([("song.mp3", False)]) == "[Sent audio attachment]"

    def test_ogg_not_flagged_voice_is_audio(self):
        # .ogg is voice-capable, but is_voice=False routes to audio.
        assert _describe_media_for_mirror([("clip.ogg", False)]) == "[Sent audio attachment]"

    def test_document_fallback(self):
        assert _describe_media_for_mirror([("report.pdf", False)]) == "[Sent document attachment]"

    def test_multiple(self):
        files = [("a.png", False), ("b.png", False), ("c.png", False)]
        assert _describe_media_for_mirror(files) == "[Sent 3 media attachments]"


# ---------------------------------------------------------------------------
# Pure helpers — _telegram_retry_delay
# ---------------------------------------------------------------------------


class TestTelegramRetryDelay:
    def test_retry_after_attr_used(self):
        exc = SimpleNamespace(retry_after=5)
        assert _telegram_retry_delay(exc, attempt=0) == 5.0

    def test_retry_after_negative_clamped_to_zero(self):
        exc = SimpleNamespace(retry_after=-3)
        assert _telegram_retry_delay(exc, attempt=0) == 0.0

    def test_retry_after_invalid_returns_one(self):
        exc = SimpleNamespace(retry_after="not-a-number")
        assert _telegram_retry_delay(exc, attempt=0) == 1.0

    def test_timeout_text_returns_none(self):
        assert _telegram_retry_delay(Exception("Request timed out"), attempt=1) is None
        assert _telegram_retry_delay(Exception("connection timeout"), attempt=1) is None

    @pytest.mark.parametrize("text", ["502 Bad Gateway", "429 too many requests",
                                       "503 service unavailable", "http 504"])
    def test_transient_5xx_exponential(self, text):
        # Exponential backoff = 2 ** attempt. Note: a 504 message containing the
        # word "timeout"/"timed out" is matched by the timeout branch first and
        # returns None — only the bare-code match reaches the backoff branch.
        assert _telegram_retry_delay(Exception(text), attempt=2) == 4.0

    def test_unknown_error_returns_none(self):
        assert _telegram_retry_delay(Exception("invalid chat id"), attempt=0) is None


# ---------------------------------------------------------------------------
# _send_slack
# ---------------------------------------------------------------------------


class TestSendSlack:
    def test_success_returns_ts(self):
        session, _ = _aiohttp_mock(200, {"ok": True, "ts": "1718.5"})
        with patch("aiohttp.ClientSession", return_value=session):
            result = _run(_send_slack("xoxb-tok", "C123", "hi"))
        assert result["success"] is True
        assert result["platform"] == "slack"
        assert result["message_id"] == "1718.5"

    def test_api_error_returns_error(self):
        session, _ = _aiohttp_mock(200, {"ok": False, "error": "channel_not_found"})
        with patch("aiohttp.ClientSession", return_value=session):
            result = _run(_send_slack("xoxb-tok", "C123", "hi"))
        assert "channel_not_found" in result["error"]

    def test_exception_returns_error(self):
        session, _ = _aiohttp_mock(200, {"ok": True})
        session.post = MagicMock(side_effect=RuntimeError("boom"))
        with patch("aiohttp.ClientSession", return_value=session):
            result = _run(_send_slack("xoxb-tok", "C123", "hi"))
        assert "Slack send failed" in result["error"]


# ---------------------------------------------------------------------------
# _send_whatsapp
# ---------------------------------------------------------------------------


class TestSendWhatsapp:
    def test_success(self):
        session, _ = _aiohttp_mock(200, {"messageId": "w-1"})
        with patch("aiohttp.ClientSession", return_value=session):
            result = _run(_send_whatsapp({"bridge_port": 4123}, "chat-1", "hi"))
        assert result["success"] is True
        assert result["message_id"] == "w-1"
        # bridge_port honoured in URL
        assert "4123" in session.post.call_args.args[0]

    def test_default_port_3000(self):
        session, _ = _aiohttp_mock(200, {"messageId": "w-2"})
        with patch("aiohttp.ClientSession", return_value=session):
            _run(_send_whatsapp({}, "chat-1", "hi"))
        assert "3000" in session.post.call_args.args[0]

    def test_non_200_returns_error(self):
        session, _ = _aiohttp_mock(502, text="bridge down")
        with patch("aiohttp.ClientSession", return_value=session):
            result = _run(_send_whatsapp({}, "chat-1", "hi"))
        assert "502" in result["error"]
        assert "bridge down" in result["error"]


# ---------------------------------------------------------------------------
# _send_email (smtplib mocked)
# ---------------------------------------------------------------------------


class TestSendEmail:
    def test_not_configured(self, monkeypatch):
        # conftest already unsets EMAIL_PASSWORD; extra empty -> not configured.
        result = _run(_send_email({}, "to@example.com", "hi"))
        assert "not configured" in result["error"].lower()

    def test_success(self, monkeypatch):
        monkeypatch.setenv("EMAIL_PASSWORD", "secret")
        server = MagicMock()
        with patch("smtplib.SMTP", return_value=server) as smtp_cls:
            result = _run(_send_email(
                {"address": "me@example.com", "smtp_host": "smtp.example.com"},
                "to@example.com", "body text",
            ))
        assert result["success"] is True
        assert result["platform"] == "email"
        smtp_cls.assert_called_once_with("smtp.example.com", 587)
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("me@example.com", "secret")
        server.send_message.assert_called_once()
        # MIME headers populated correctly.
        sent_msg = server.send_message.call_args.args[0]
        assert sent_msg["To"] == "to@example.com"
        assert sent_msg["Subject"] == "Thoth Agent"

    def test_invalid_port_defaults_587(self, monkeypatch):
        monkeypatch.setenv("EMAIL_PASSWORD", "secret")
        monkeypatch.setenv("EMAIL_SMTP_PORT", "not-an-int")
        server = MagicMock()
        with patch("smtplib.SMTP", return_value=server) as smtp_cls:
            _run(_send_email(
                {"address": "me@example.com", "smtp_host": "smtp.example.com"},
                "to@example.com", "x",
            ))
        assert smtp_cls.call_args.args[1] == 587

    def test_smtp_exception_returns_error(self, monkeypatch):
        monkeypatch.setenv("EMAIL_PASSWORD", "secret")
        server = MagicMock()
        server.send_message.side_effect = OSError("connection refused")
        with patch("smtplib.SMTP", return_value=server):
            result = _run(_send_email(
                {"address": "me@example.com", "smtp_host": "smtp.example.com"},
                "to@example.com", "x",
            ))
        assert "Email send failed" in result["error"]


# ---------------------------------------------------------------------------
# _send_sms (Twilio, aiohttp mocked)
# ---------------------------------------------------------------------------


class TestSendSms:
    def test_not_configured(self, monkeypatch):
        # No TWILIO_ACCOUNT_SID / TWILIO_PHONE_NUMBER set.
        result = _run(_send_sms("auth-token", "+15551234567", "hi"))
        assert "not configured" in result["error"].lower()

    def _configured(self, monkeypatch):
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACxxx")
        monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550000000")

    def test_success_returns_sid(self, monkeypatch):
        self._configured(monkeypatch)
        session, _ = _aiohttp_mock(201, {"sid": "SM123"})
        with patch("aiohttp.ClientSession", return_value=session), \
             patch("aiohttp.FormData", _FakeFormData):
            result = _run(_send_sms("auth-token", "+15551234567", "hi"))
        assert result["success"] is True
        assert result["message_id"] == "SM123"

    def test_markdown_stripped_from_body(self, monkeypatch):
        self._configured(monkeypatch)
        captured = {}

        def _factory(*_a, **_kw):
            fd = _FakeFormData()
            captured["fd"] = fd
            return fd

        session, _ = _aiohttp_mock(200, {"sid": "SM1"})
        # Heading must be at line-start — the strip regex is ^#{1,6}\s+ (MULTILINE).
        msg = "**bold** _ital_ `code`\n## Heading\n[link](http://x.com)"
        with patch("aiohttp.ClientSession", return_value=session), \
             patch("aiohttp.FormData", _factory):
            _run(_send_sms("auth-token", "+15551234567", msg))
        body = captured["fd"].fields["Body"]
        assert "**" not in body and "`" not in body and "#" not in body
        assert "(http://x.com)" not in body
        assert "bold" in body and "Heading" in body and "link" in body

    def test_api_error(self, monkeypatch):
        self._configured(monkeypatch)
        session, _ = _aiohttp_mock(400, {"message": "invalid 'To' number"})
        with patch("aiohttp.ClientSession", return_value=session), \
             patch("aiohttp.FormData", _FakeFormData):
            result = _run(_send_sms("auth-token", "bad", "hi"))
        assert "400" in result["error"]
        assert "invalid 'To' number" in result["error"]

    def test_exception_returns_error(self, monkeypatch):
        self._configured(monkeypatch)
        session, _ = _aiohttp_mock(200, {"sid": "x"})
        session.post = MagicMock(side_effect=RuntimeError("net"))
        with patch("aiohttp.ClientSession", return_value=session), \
             patch("aiohttp.FormData", _FakeFormData):
            result = _run(_send_sms("auth-token", "+1555", "hi"))
        assert "SMS send failed" in result["error"]


# ---------------------------------------------------------------------------
# _send_mattermost
# ---------------------------------------------------------------------------


class TestSendMattermost:
    def test_not_configured(self):
        result = _run(_send_mattermost(None, {}, "chan", "hi"))
        assert "not configured" in result["error"].lower()

    def test_success(self):
        session, _ = _aiohttp_mock(201, {"id": "post-1"})
        with patch("aiohttp.ClientSession", return_value=session):
            result = _run(_send_mattermost("tok", {"url": "https://mm.example.com"}, "chan", "hi"))
        assert result["success"] is True
        assert result["message_id"] == "post-1"

    def test_api_error(self):
        session, _ = _aiohttp_mock(500, text="server error")
        with patch("aiohttp.ClientSession", return_value=session):
            result = _run(_send_mattermost("tok", {"url": "https://mm.example.com"}, "chan", "hi"))
        assert "500" in result["error"]


# ---------------------------------------------------------------------------
# _send_matrix (Client-Server API; markdown not installed -> plain text branch)
# ---------------------------------------------------------------------------


class TestSendMatrix:
    def test_not_configured(self):
        result = _run(_send_matrix(None, {}, "!room:srv", "hi"))
        assert "not configured" in result["error"].lower()

    def test_success_plain_text_payload(self):
        session, _ = _aiohttp_mock(200, {"event_id": "$evt1"})
        with patch("aiohttp.ClientSession", return_value=session):
            result = _run(_send_matrix(
                "tok", {"homeserver": "https://matrix.example.com"}, "!room:srv", "# Heading"))
        assert result["success"] is True
        assert result["message_id"] == "$evt1"
        # markdown is not installed in this env -> no formatted_body added.
        payload = session.put.call_args.kwargs["json"]
        assert payload["body"] == "# Heading"
        assert "formatted_body" not in payload

    def test_room_id_url_encoded(self):
        session, _ = _aiohttp_mock(200, {"event_id": "$e"})
        with patch("aiohttp.ClientSession", return_value=session):
            _run(_send_matrix("tok", {"homeserver": "https://m.example.com"}, "!room:srv", "hi"))
        url = session.put.call_args.args[0]
        assert "%21room%3Asrv" in url  # '!' and ':' percent-encoded

    def test_api_error(self):
        session, _ = _aiohttp_mock(403, text="forbidden")
        with patch("aiohttp.ClientSession", return_value=session):
            result = _run(_send_matrix(
                "tok", {"homeserver": "https://m.example.com"}, "!room:srv", "hi"))
        assert "403" in result["error"]


# ---------------------------------------------------------------------------
# _send_homeassistant
# ---------------------------------------------------------------------------


class TestSendHomeAssistant:
    def test_not_configured(self):
        result = _run(_send_homeassistant(None, {}, "notify.me", "hi"))
        assert "not configured" in result["error"].lower()

    def test_success(self):
        session, _ = _aiohttp_mock(200)
        with patch("aiohttp.ClientSession", return_value=session):
            result = _run(_send_homeassistant("tok", {"url": "https://hass.example.com"}, "tgt", "hi"))
        assert result["success"] is True
        assert result["platform"] == "homeassistant"
        payload = session.post.call_args.kwargs["json"]
        assert payload["target"] == "tgt"

    def test_api_error(self):
        session, _ = _aiohttp_mock(401, text="unauthorized")
        with patch("aiohttp.ClientSession", return_value=session):
            result = _run(_send_homeassistant("tok", {"url": "https://hass.example.com"}, "tgt", "hi"))
        assert "401" in result["error"]


# ---------------------------------------------------------------------------
# _send_to_platform routing table (non-media platforms)
# ---------------------------------------------------------------------------


class TestSendToPlatformRouting:
    """Each non-media platform routes to its dedicated sender with the right args."""

    def _pconfig(self):
        return SimpleNamespace(enabled=True, token="tok", api_key="key", extra={"k": "v"})

    def test_routes_email(self):
        sender = AsyncMock(return_value={"success": True, "platform": "email"})
        with patch("tools.send_message_tool._send_email", sender):
            _run(_send_to_platform(Platform.EMAIL, self._pconfig(), "to@x.com", "msg"))
        sender.assert_awaited_once()
        assert sender.await_args.args == ({"k": "v"}, "to@x.com", "msg")

    def test_routes_sms(self):
        sender = AsyncMock(return_value={"success": True, "platform": "sms"})
        with patch("tools.send_message_tool._send_sms", sender):
            _run(_send_to_platform(Platform.SMS, self._pconfig(), "+1555", "msg"))
        sender.assert_awaited_once()
        # _send_sms(pconfig.api_key, chat_id, chunk)
        assert sender.await_args.args == ("key", "+1555", "msg")

    def test_routes_mattermost(self):
        sender = AsyncMock(return_value={"success": True})
        with patch("tools.send_message_tool._send_mattermost", sender):
            _run(_send_to_platform(Platform.MATTERMOST, self._pconfig(), "chan", "msg"))
        sender.assert_awaited_once()
        assert sender.await_args.args == ("tok", {"k": "v"}, "chan", "msg")

    def test_routes_homeassistant(self):
        sender = AsyncMock(return_value={"success": True})
        with patch("tools.send_message_tool._send_homeassistant", sender):
            _run(_send_to_platform(Platform.HOMEASSISTANT, self._pconfig(), "tgt", "msg"))
        sender.assert_awaited_once()
        assert sender.await_args.args == ("tok", {"k": "v"}, "tgt", "msg")

    def test_routes_bluebubbles(self):
        sender = AsyncMock(return_value={"success": True})
        with patch("tools.send_message_tool._send_bluebubbles", sender):
            _run(_send_to_platform(Platform.BLUEBUBBLES, self._pconfig(), "chat", "msg"))
        sender.assert_awaited_once()
        assert sender.await_args.args == ({"k": "v"}, "chat", "msg")

    def test_sender_error_short_circuits(self):
        sender = AsyncMock(return_value={"error": "nope"})
        with patch("tools.send_message_tool._send_email", sender):
            result = _run(_send_to_platform(Platform.EMAIL, self._pconfig(), "to@x.com", "msg"))
        assert result == {"error": "nope"}


class TestSendToPlatformMediaUnsupported:
    """Non-media platforms reject or warn on MEDIA attachments."""

    def _pconfig(self):
        return SimpleNamespace(enabled=True, token="tok", api_key="key", extra={})

    def test_media_only_returns_error(self, tmp_path):
        img = tmp_path / "x.png"
        img.write_bytes(b"x")
        result = _run(_send_to_platform(
            Platform.SLACK, self._pconfig(), "C1", "", media_files=[(str(img), False)]))
        assert "MEDIA delivery is currently only supported" in result["error"]

    def test_media_with_text_adds_warning(self, tmp_path):
        img = tmp_path / "x.png"
        img.write_bytes(b"x")
        sender = AsyncMock(return_value={"success": True, "platform": "slack"})
        with patch("tools.send_message_tool._send_slack", sender):
            result = _run(_send_to_platform(
                Platform.SLACK, self._pconfig(), "C1", "hello", media_files=[(str(img), False)]))
        assert result["success"] is True
        assert any("MEDIA attachments were omitted" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# send_message_tool entrypoint validation
# ---------------------------------------------------------------------------


class TestSendMessageToolValidation:
    def test_missing_target_returns_error(self):
        result = send_message_tool({"action": "send", "message": "hi"})
        assert '"error"' in result
        assert "required" in result

    def test_missing_message_returns_error(self):
        result = send_message_tool({"action": "send", "target": "telegram:-100"})
        assert '"error"' in result
        assert "required" in result
