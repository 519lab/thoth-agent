"""Unit tests for agent/context_telemetry.py (Phase 0b of
plans/substrate-context-engine.md).

Contract under test: guarded best-effort emission — correct payloads when
the substrate is bound, silent no-op when it isn't, and no exception ever
reaches the caller.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent import context_telemetry as ct


def _fake_agent(**overrides):
    agent = SimpleNamespace(
        session_prompt_tokens=1_000,
        session_completion_tokens=200,
        session_cache_read_tokens=800,
        session_cache_write_tokens=50,
        session_reasoning_tokens=10,
        session_estimated_cost_usd=0.05,
        session_api_calls=3,
        session_id="sess-1",
        model="test-model",
        provider="test-provider",
        platform="cli",
        _turn_tool_calls=4,
        _turn_tool_failures=1,
        iteration_budget=SimpleNamespace(used=3, max_total=90),
        context_compressor=SimpleNamespace(
            last_prompt_tokens=12_345, compression_count=1
        ),
    )
    for key, value in overrides.items():
        setattr(agent, key, value)
    return agent


@pytest.fixture
def captured(monkeypatch):
    """Bind a fake substrate and capture telemetry.write calls."""
    events = []

    async def fake_write(substrate, *, agent, event, payload=None, at=None, conn=None):
        events.append({"agent": agent, "event": event, "payload": payload})

    monkeypatch.setattr("substrate.telemetry.write", fake_write)
    monkeypatch.setattr("substrate.get_bound_substrate", lambda: object())
    monkeypatch.setattr("thoth_db.run_sync", lambda coro: asyncio.run(coro))
    return events


def test_noop_when_substrate_unbound(monkeypatch):
    monkeypatch.setattr("substrate.get_bound_substrate", lambda: None)

    def _boom(coro):  # run_sync must never be reached when unbound
        raise AssertionError("run_sync called with substrate unbound")

    monkeypatch.setattr("thoth_db.run_sync", _boom)

    agent = _fake_agent()
    ct.emit_turn_event(
        agent,
        snapshot=ct.snapshot_turn_counters(agent),
        exit_reason="text_response",
        api_calls=1,
        messages=[],
        interrupted=False,
        response_len=10,
        started_at=datetime.now(timezone.utc),
    )  # must not raise


def test_turn_event_payload_and_deltas(captured):
    agent = _fake_agent()
    snapshot = ct.snapshot_turn_counters(agent)

    # Simulate a turn: accumulators grow.
    agent.session_prompt_tokens += 5_000
    agent.session_completion_tokens += 400
    agent.session_cache_read_tokens += 4_000
    agent.session_cache_write_tokens += 100
    agent.session_reasoning_tokens += 20
    agent.session_estimated_cost_usd += 0.02
    agent.session_api_calls += 2

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    started = datetime.now(timezone.utc) - timedelta(seconds=3)

    ct.emit_turn_event(
        agent,
        snapshot=snapshot,
        exit_reason="text_response",
        api_calls=2,
        messages=messages,
        interrupted=False,
        response_len=4,
        started_at=started,
    )

    assert len(captured) == 1
    row = captured[0]
    assert row["agent"] == "context"
    assert row["event"] == "context.turn"

    payload = row["payload"]
    assert payload["exit_reason"] == "text_response"
    assert payload["api_calls"] == 2
    assert payload["session_id"] == "sess-1"
    assert payload["messages_total"] == 4
    assert payload["tool_result_msgs"] == 1
    assert payload["tool_call_turns"] == 1
    assert payload["tool_calls"] == 4
    assert payload["tool_failures"] == 1
    assert payload["budget_used"] == 3
    assert payload["budget_max"] == 90
    assert payload["context_tokens_end"] == 12_345
    assert payload["compression_count"] == 1
    assert payload["duration_s"] >= 3.0

    deltas = payload["deltas"]
    assert deltas["prompt_tokens"] == 5_000
    assert deltas["completion_tokens"] == 400
    assert deltas["cache_read_tokens"] == 4_000
    assert deltas["cache_write_tokens"] == 100
    assert deltas["reasoning_tokens"] == 20
    assert deltas["api_calls"] == 2
    assert deltas["cost_usd"] == pytest.approx(0.02)
    # 4000 cache-read of 5000 prompt delta = 80%
    assert payload["cache_hit_pct"] == pytest.approx(80.0)


def test_turn_event_no_cache_pct_when_no_prompt_delta(captured):
    agent = _fake_agent()
    snapshot = ct.snapshot_turn_counters(agent)
    ct.emit_turn_event(
        agent,
        snapshot=snapshot,
        exit_reason="interrupted_by_user",
        api_calls=0,
        messages=[],
        interrupted=True,
        response_len=0,
        started_at=datetime.now(timezone.utc),
    )
    payload = captured[0]["payload"]
    assert payload["cache_hit_pct"] is None
    assert payload["interrupted"] is True


def test_compression_event_payload(captured):
    agent = _fake_agent()
    ct.emit_compression_event(
        agent,
        trigger="threshold",
        messages_before=120,
        messages_after=30,
        tokens_before=90_000,
        tokens_after=25_000,
        duration_s=7.345,
        aborted=False,
        summary_fallback=False,
        old_session_id="sess-0",
        new_session_id="sess-1",
    )
    row = captured[0]
    assert row["event"] == "context.compressed"
    payload = row["payload"]
    assert payload["trigger"] == "threshold"
    assert payload["messages_before"] == 120
    assert payload["messages_after"] == 30
    assert payload["tokens_saved"] == 65_000
    assert payload["duration_s"] == pytest.approx(7.34, abs=0.01)
    assert payload["aborted"] is False
    assert payload["old_session_id"] == "sess-0"
    assert payload["new_session_id"] == "sess-1"
    assert payload["compression_count"] == 1


def test_compression_event_aborted_without_token_counts(captured):
    agent = _fake_agent()
    ct.emit_compression_event(
        agent,
        trigger="manual",
        messages_before=50,
        messages_after=50,
        tokens_before=None,
        tokens_after=None,
        duration_s=0.5,
        aborted=True,
    )
    payload = captured[0]["payload"]
    assert payload["aborted"] is True
    assert payload["tokens_saved"] is None


def test_write_failure_is_swallowed(monkeypatch):
    async def exploding_write(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr("substrate.telemetry.write", exploding_write)
    monkeypatch.setattr("substrate.get_bound_substrate", lambda: object())
    monkeypatch.setattr("thoth_db.run_sync", lambda coro: asyncio.run(coro))

    agent = _fake_agent()
    ct.emit_turn_event(
        agent,
        snapshot=ct.snapshot_turn_counters(agent),
        exit_reason="text_response",
        api_calls=1,
        messages=[],
        interrupted=False,
        response_len=1,
        started_at=datetime.now(timezone.utc),
    )  # must not raise


def test_snapshot_tolerates_missing_attrs():
    bare = SimpleNamespace()
    snapshot = ct.snapshot_turn_counters(bare)
    assert snapshot["prompt_tokens"] == 0
    assert snapshot["cost_usd"] == 0
    # emit with a bare agent must not raise either (unbound substrate path
    # is separate — here the payload build itself must tolerate bareness).
    ct.emit_turn_event(
        bare,
        snapshot=snapshot,
        exit_reason="x",
        api_calls=0,
        messages=[],
        interrupted=False,
        response_len=0,
        started_at=datetime.now(timezone.utc),
    )
