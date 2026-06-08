"""SubstrateMemoryProvider — Phase C Task 10 / spec §9.7."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from agent.memory_manager import MemoryManager
from agent.memory_providers.substrate import SubstrateMemoryProvider
from substrate import Substrate, get_bound_substrate
from substrate.config import SubstrateConfig
from substrate.l0 import commit_slice


@pytest.fixture(autouse=True)
def _enable_mock_embeddings(monkeypatch):
    from substrate.recall import embeddings

    monkeypatch.setenv(embeddings.MOCK_ENV_VAR, "1")
    embeddings.reset_client_cache()


@pytest_asyncio.fixture
async def booted_substrate(thoth_db_initialized):
    sub = await Substrate.boot(
        config=SubstrateConfig(auto_migrate=False, start_subagents=False),
        start_subagents=False,
    )
    try:
        yield sub
    finally:
        await sub.shutdown()


@pytest.fixture
def fresh_provider(monkeypatch):
    """Return a freshly-initialised provider per test, with env-var
    state already applied via monkeypatch."""
    def _make(*, enabled: bool = False) -> SubstrateMemoryProvider:
        if enabled:
            monkeypatch.setenv("THOTH_SUBSTRATE_RECALL", "1")
        else:
            monkeypatch.setenv("THOTH_SUBSTRATE_RECALL", "0")
        p = SubstrateMemoryProvider()
        p.initialize(session_id="test-session")
        return p

    return _make


@pytest.mark.asyncio
async def test_provider_registers_under_substrate_name(booted_substrate, fresh_provider):
    """MemoryManager accepts the provider; name is 'substrate'."""
    mm = MemoryManager()
    provider = fresh_provider(enabled=True)
    mm.add_provider(provider)
    assert mm.get_provider("substrate") is provider


@pytest.mark.asyncio
async def test_provider_coexists_with_external(booted_substrate, fresh_provider):
    """Substrate provider should NOT count as 'external' — an actual
    external plugin can still register alongside it."""
    mm = MemoryManager()
    substrate_provider = fresh_provider(enabled=True)
    mm.add_provider(substrate_provider)

    # Fake external plugin.
    class FakePlugin(SubstrateMemoryProvider):
        @property
        def name(self) -> str:
            return "fake-plugin"

    plugin = FakePlugin()
    plugin.initialize(session_id="test")
    mm.add_provider(plugin)
    assert mm.get_provider("substrate") is substrate_provider
    assert mm.get_provider("fake-plugin") is plugin


@pytest.mark.asyncio
async def test_provider_disabled_returns_empty(booted_substrate, fresh_provider):
    """THOTH_SUBSTRATE_RECALL=0 → prefetch returns '' regardless of substrate state."""
    # Seed some content so a wrong implementation would return non-empty.
    stream = await booted_substrate.streams.get_by_name("thoth.world.user_message.cli")
    await commit_slice(
        booted_substrate, stream.stream_id, "secret data",
        event_time_world=datetime.now(timezone.utc),
    )
    import thoth_db
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE substrate_slices SET sentinel_state='passed', trust_score=0.95, pending_committed_at=NULL WHERE sentinel_state='pending'"
        )

    p = fresh_provider(enabled=False)
    assert p.prefetch("anything") == ""
    assert p.system_prompt_block() == ""
    assert p.get_tool_schemas() == []


@pytest.mark.asyncio
async def test_provider_enabled_routes_to_recall(booted_substrate, fresh_provider, monkeypatch):
    """THOTH_SUBSTRATE_RECALL=1 + substrate booted → prefetch routes
    to recall() and returns its text.

    We use a stub for recall_sync because the real pipeline's pool
    binding conflicts with pytest-asyncio's per-test loop when called
    via run_sync from a worker thread (the loop affinity model is
    'one loop per pool'). The contract being tested here is the
    provider's enable-gating + routing, not the recall pipeline itself
    (that's covered by tests/substrate/recall/test_recall_api.py).
    """
    from substrate.recall.projection import RecallProjection

    captured = {}

    def _fake_recall_sync(substrate, query, **kwargs):
        captured["query"] = query
        captured["session_id"] = kwargs.get("session_id")
        return RecallProjection(
            text=f"recalled: {query}",
            tokens_used=10,
            composed=[],
            candidates_seen=1,
            duration_ms=5,
            timed_out=False,
            empty_reason=None,
        )

    monkeypatch.setattr("substrate.recall.api.recall_sync", _fake_recall_sync)

    p = fresh_provider(enabled=True)
    text = p.prefetch("widgets")
    assert text == "recalled: widgets"
    assert captured["query"] == "widgets"


def test_provider_handles_substrate_not_booted(monkeypatch):
    """is_available() returns False when no substrate is bound."""
    # In a fresh test process without Substrate.boot, get_bound_substrate
    # returns None. We monkeypatch to simulate this in case prior tests
    # left bound state.
    monkeypatch.setattr(
        "substrate.events.hermes_hooks._substrate", None
    )
    p = SubstrateMemoryProvider()
    p.initialize(session_id="test")
    assert p.is_available() is False


@pytest.mark.asyncio
async def test_provider_sync_turn_is_noop(booted_substrate, fresh_provider):
    """sync_turn must not write any new substrate slice — Phase A hooks
    already did. We assert by counting slices before/after."""
    import thoth_db

    p = fresh_provider(enabled=True)
    async with thoth_db.connection() as conn:
        before = await conn.fetchval("SELECT COUNT(*) FROM substrate_slices")
    p.sync_turn("user said", "assistant said", session_id="x")
    async with thoth_db.connection() as conn:
        after = await conn.fetchval("SELECT COUNT(*) FROM substrate_slices")
    assert after == before


@pytest.mark.asyncio
async def test_provider_tool_recall_more_routes_with_bigger_budget(
    booted_substrate, fresh_provider, monkeypatch
):
    """substrate_recall_more wraps recall_sync with a larger token
    budget and a wider time window (1 week default)."""
    from datetime import timedelta

    from substrate.recall.projection import RecallProjection

    captured = {}

    def _fake_recall_sync(substrate, query, **kwargs):
        captured.update(kwargs)
        captured["query"] = query
        return RecallProjection(
            text=f"deep recall: {query}",
            tokens_used=20, composed=[], candidates_seen=1,
            duration_ms=5, timed_out=False, empty_reason=None,
        )

    monkeypatch.setattr("substrate.recall.api.recall_sync", _fake_recall_sync)
    p = fresh_provider(enabled=True)
    result = p.handle_tool_call(
        "substrate_recall_more", {"topic": "widgets", "time_window_hours": 72}
    )
    assert "widgets" in result
    assert captured["query"] == "widgets"
    assert captured["token_budget"] == 3000
    assert captured["time_window"] == timedelta(hours=72)


@pytest.mark.asyncio
async def test_provider_tool_recall_more_default_window(
    booted_substrate, fresh_provider, monkeypatch
):
    """Default time_window_hours is 168 (1 week)."""
    from datetime import timedelta
    from substrate.recall.projection import RecallProjection

    captured = {}

    def _fake_recall_sync(substrate, query, **kwargs):
        captured.update(kwargs)
        return RecallProjection(
            text="ok", tokens_used=1, composed=[], candidates_seen=0,
            duration_ms=1, timed_out=False, empty_reason=None,
        )

    monkeypatch.setattr("substrate.recall.api.recall_sync", _fake_recall_sync)
    p = fresh_provider(enabled=True)
    p.handle_tool_call("substrate_recall_more", {"topic": "x"})
    assert captured["time_window"] == timedelta(hours=168)


@pytest.mark.asyncio
async def test_provider_tool_recall_more_no_topic(booted_substrate, fresh_provider):
    """Missing topic surfaces an inline error string (the model sees it)."""
    p = fresh_provider(enabled=True)
    result = p.handle_tool_call("substrate_recall_more", {})
    assert "topic is required" in result


@pytest.mark.asyncio
async def test_provider_isolated_from_failures(booted_substrate, fresh_provider, monkeypatch):
    """Force the recall pipeline to raise; provider returns '' (MemoryManager
    keeps going)."""
    # Monkeypatch recall_sync to raise. The provider catches and returns "".
    def _broken_recall(*args, **kwargs):
        raise RuntimeError("simulated recall failure")

    monkeypatch.setattr("substrate.recall.api.recall_sync", _broken_recall)
    p = fresh_provider(enabled=True)
    out = p.prefetch("x")
    assert out == ""
