"""Unit tests for gateway/auto_continue.py.

Imports from the extracted module's canonical home (``gateway.auto_continue``)
rather than the ``gateway.run`` re-export, so the module is covered on its own.
The re-export surface on ``gateway.run`` is exercised by the existing
tests/gateway/test_restart_resume_pending.py.
"""

from datetime import datetime, timezone

from gateway.auto_continue import (
    _AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT,
    _auto_continue_freshness_window,
    _coerce_gateway_timestamp,
    _float_env,
    _is_fresh_gateway_interruption,
)


def test_coerce_timestamp_accepts_supported_shapes():
    dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert _coerce_gateway_timestamp(dt) == dt.timestamp()
    assert _coerce_gateway_timestamp(1_700_000_000) == 1_700_000_000.0
    assert _coerce_gateway_timestamp(1_700_000_000.5) == 1_700_000_000.5
    # Millisecond magnitudes are scaled down to seconds.
    assert _coerce_gateway_timestamp(1_700_000_000_000) == 1_700_000_000.0
    # ISO-8601 string with trailing Z.
    assert _coerce_gateway_timestamp("2024-01-01T00:00:00Z") == dt.timestamp()
    # Numeric string.
    assert _coerce_gateway_timestamp("1700000000") == 1_700_000_000.0


def test_coerce_timestamp_returns_none_for_unparseable():
    assert _coerce_gateway_timestamp(None) is None
    assert _coerce_gateway_timestamp("") is None
    assert _coerce_gateway_timestamp("not-a-timestamp") is None
    # bool is a subclass of int but must not be treated as epoch seconds.
    assert _coerce_gateway_timestamp(True) is None


def test_float_env_parses_and_falls_back(monkeypatch):
    monkeypatch.setenv("THOTH_TEST_FLOAT_ENV", "12.5")
    assert _float_env("THOTH_TEST_FLOAT_ENV", 1.0) == 12.5
    monkeypatch.setenv("THOTH_TEST_FLOAT_ENV", "abc")
    assert _float_env("THOTH_TEST_FLOAT_ENV", 1800) == 1800.0
    monkeypatch.delenv("THOTH_TEST_FLOAT_ENV", raising=False)
    assert _float_env("THOTH_TEST_FLOAT_ENV", 7.0) == 7.0


def test_freshness_window_reads_env_with_default(monkeypatch):
    monkeypatch.delenv("THOTH_AUTO_CONTINUE_FRESHNESS", raising=False)
    assert _auto_continue_freshness_window() == float(_AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT)
    monkeypatch.setenv("THOTH_AUTO_CONTINUE_FRESHNESS", "7200")
    assert _auto_continue_freshness_window() == 7200.0
    monkeypatch.setenv("THOTH_AUTO_CONTINUE_FRESHNESS", "garbage")
    assert _auto_continue_freshness_window() == float(_AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT)


def test_is_fresh_gateway_interruption_gate():
    # Within the window (injected now) -> fresh.
    assert _is_fresh_gateway_interruption(1000.0, now=1030.0, window_secs=60) is True
    # Beyond the window -> stale.
    assert _is_fresh_gateway_interruption(1000.0, now=2000.0, window_secs=60) is False
    # Unknown/absent timestamp is treated as fresh for backward compatibility.
    assert _is_fresh_gateway_interruption(None) is True
    assert _is_fresh_gateway_interruption("not-a-timestamp") is True
    # Non-positive window disables the gate (always fresh).
    assert _is_fresh_gateway_interruption(1000.0, now=9_999_999.0, window_secs=0) is True
