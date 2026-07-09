"""Unit tests for gateway/text_sanitize.py.

These import from the extracted module's canonical home (``gateway.text_sanitize``)
rather than the ``gateway.run`` re-export, so the module is covered on its own and
a future accidental removal of a helper is caught here. The re-export surface on
``gateway.run`` is separately exercised by tests/gateway/test_telegram_noise_filter.py.
"""

from gateway.config import Platform
from gateway.text_sanitize import (
    _gateway_platform_value,
    _gateway_provider_error_reply,
    _looks_like_gateway_provider_error,
    _normalize_empty_agent_response,
    _prepare_gateway_status_message,
    _redact_gateway_user_facing_secrets,
    _sanitize_gateway_final_response,
    _telegramize_command_mentions,
)


def test_platform_value_normalizes_enums_and_strings():
    assert _gateway_platform_value(Platform.TELEGRAM) == "telegram"
    assert _gateway_platform_value("  TELEGRAM  ") == "telegram"
    assert _gateway_platform_value(None) == ""


def test_redact_strips_known_secret_shapes():
    raw = "key sk-abcdefghijklmnop and Bearer abcdefghijklmnopqrstuvwx token"
    redacted = _redact_gateway_user_facing_secrets(raw)
    assert "sk-abcdefghijklmnop" not in redacted
    assert "[REDACTED]" in redacted
    # The Bearer prefix group is preserved; only the token body is masked.
    assert "Bearer [REDACTED]" in redacted


def test_looks_like_provider_error_is_shape_and_length_gated():
    assert _looks_like_gateway_provider_error("API call failed: HTTP 500") is True
    # Long assistant prose that merely mentions a status code is not an error envelope.
    assert _looks_like_gateway_provider_error("HTTP 404 means not found. " + "x" * 500) is False
    assert _looks_like_gateway_provider_error("") is False


def test_provider_error_reply_branches():
    assert "authentication" in _gateway_provider_error_reply("invalid api key").lower()
    assert "rate-limiting" in _gateway_provider_error_reply("HTTP 429 quota").lower()
    assert "rejected" in _gateway_provider_error_reply("request was blocked: moderation").lower()
    # Generic fallback when nothing matches.
    assert "failed after retries" in _gateway_provider_error_reply("something odd").lower()


def test_final_response_only_rewrites_telegram():
    raw = "API call failed after 3 retries: HTTP 400 cybersecurity risk. req_id=req_1"
    tg = _sanitize_gateway_final_response(Platform.TELEGRAM, raw)
    assert "cybersecurity risk" not in tg.lower()
    assert "req_1" not in tg
    # Other platforms keep the raw text unchanged.
    assert _sanitize_gateway_final_response(Platform.DISCORD, raw) == raw


def test_status_message_suppresses_noise_returns_none():
    assert _prepare_gateway_status_message(Platform.TELEGRAM, "warn", "⏳ Retrying in 4.2s") is None
    # Empty/whitespace collapses to None regardless of platform.
    assert _prepare_gateway_status_message(Platform.DISCORD, "warn", "   ") is None


def test_telegramize_command_mentions_passthrough_and_rewrite():
    # Non-telegram platforms are untouched.
    assert _telegramize_command_mentions("run /My-Cmd now", Platform.DISCORD) == "run /My-Cmd now"
    # Telegram command names are normalized to valid (lowercase/underscore) slugs.
    out = _telegramize_command_mentions("try /My-Cmd", Platform.TELEGRAM)
    assert out.startswith("try /")
    assert "/My-Cmd" not in out


def test_normalize_empty_agent_response_branches():
    # A non-empty response is returned verbatim.
    assert _normalize_empty_agent_response({}, "hello") == "hello"
    # Context-window failures steer the user to /compact or /reset.
    ctx = _normalize_empty_agent_response(
        {"failed": True, "error": "400 context length exceeded"}, "", history_len=99
    )
    assert "/compact" in ctx and "context window" in ctx.lower()
    # Generic failure surfaces a truncated error and /reset hint.
    fail = _normalize_empty_agent_response({"failed": True, "error": "boom"}, "")
    assert "boom" in fail and "/reset" in fail
    # Work happened but no text came back.
    empty = _normalize_empty_agent_response({"api_calls": 2}, "")
    assert "no response was generated" in empty.lower()
    # Partial completion is reported as stopped.
    partial = _normalize_empty_agent_response({"api_calls": 1, "partial": True, "error": "halted"}, "")
    assert "stopped" in partial.lower() and "halted" in partial
