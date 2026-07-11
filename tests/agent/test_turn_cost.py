"""``agent.turn_cost`` — per-turn cost/latency recording (innovation #4).

Covers the three surfaces:

* the pure half — ``turn_cost_enabled`` env gate, ``snapshot_turn_cost``
  counter capture, and ``record_turn_cost``'s delta computation / skip
  logic / unknown-pricing handling (DB mocked out);
* the DB half — ``write_turn_cost`` + the windowed ``fetch_*`` rollups,
  round-tripped against the migrated test pool;
* best-effort contract — a broken pool degrades to a no-op, never raises.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent import turn_cost as tc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agent(**overrides) -> SimpleNamespace:
    base = dict(
        session_id="sess-1",
        platform="cli",
        model="test-model",
        provider="test-provider",
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_total_tokens=0,
        session_estimated_cost_usd=0.0,
        session_cost_status="estimated",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def captured_write(monkeypatch):
    """Replace the DB write with a capture stub; run_sync executes inline."""
    captured: list = []

    async def _fake_write(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(tc, "write_turn_cost", _fake_write)
    monkeypatch.setattr("thoth_db.run_sync", lambda coro: asyncio.run(coro))
    return captured


# ---------------------------------------------------------------------------
# Env gate
# ---------------------------------------------------------------------------


class TestEnabledGate:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("THOTH_TURN_COST", raising=False)
        assert tc.turn_cost_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF "])
    def test_disabled_values(self, monkeypatch, value):
        monkeypatch.setenv("THOTH_TURN_COST", value)
        assert tc.turn_cost_enabled() is False

    def test_explicit_on(self, monkeypatch):
        monkeypatch.setenv("THOTH_TURN_COST", "1")
        assert tc.turn_cost_enabled() is True


# ---------------------------------------------------------------------------
# Snapshot + delta recording (pure half; DB mocked)
# ---------------------------------------------------------------------------


class TestRecordTurnCost:
    def test_records_deltas_not_cumulative_totals(self, captured_write):
        agent = _agent(
            session_input_tokens=1000,
            session_output_tokens=200,
            session_total_tokens=1200,
            session_estimated_cost_usd=0.50,
        )
        snap = tc.snapshot_turn_cost(agent)
        # Simulate a turn: counters grow.
        agent.session_input_tokens = 1600
        agent.session_output_tokens = 350
        agent.session_cache_read_tokens = 4000
        agent.session_total_tokens = 5950
        agent.session_estimated_cost_usd = 0.62

        tc.record_turn_cost(agent, snap, api_calls=3)

        assert len(captured_write) == 1
        row = captured_write[0]
        assert row["input_tokens"] == 600
        assert row["output_tokens"] == 150
        assert row["cache_read_tokens"] == 4000
        assert row["total_tokens"] == 4750
        assert row["api_calls"] == 3
        assert row["cost_usd"] == pytest.approx(0.12)
        assert row["session_id"] == "sess-1"
        assert row["platform"] == "cli"
        assert row["model"] == "test-model"
        assert row["duration_ms"] >= 0

    def test_skips_turn_with_no_llm_traffic(self, captured_write):
        agent = _agent()
        snap = tc.snapshot_turn_cost(agent)
        tc.record_turn_cost(agent, snap, api_calls=0)
        assert captured_write == []

    def test_zero_token_turn_with_api_call_still_recorded(self, captured_write):
        # The codex app-server path doesn't feed token counters but the
        # turn row (duration) must still land.
        agent = _agent()
        snap = tc.snapshot_turn_cost(agent)
        tc.record_turn_cost(agent, snap, api_calls=1)
        assert len(captured_write) == 1
        assert captured_write[0]["total_tokens"] == 0

    def test_unknown_pricing_records_null_cost(self, captured_write):
        agent = _agent(session_cost_status="unknown")
        snap = tc.snapshot_turn_cost(agent)
        agent.session_total_tokens = 100
        tc.record_turn_cost(agent, snap, api_calls=1)
        assert captured_write[0]["cost_usd"] is None
        assert captured_write[0]["cost_status"] == "unknown"

    def test_kill_switch_disables_recording(self, captured_write, monkeypatch):
        monkeypatch.setenv("THOTH_TURN_COST", "0")
        agent = _agent(session_total_tokens=0)
        snap = tc.snapshot_turn_cost(agent)
        agent.session_total_tokens = 500
        tc.record_turn_cost(agent, snap, api_calls=2)
        assert captured_write == []

    def test_never_raises_even_when_bridge_explodes(self, monkeypatch):
        monkeypatch.setattr(
            "thoth_db.run_sync",
            lambda coro: (coro.close(), (_ for _ in ()).throw(RuntimeError("pool down")))[1],
        )
        agent = _agent(session_total_tokens=100)
        snap = tc.snapshot_turn_cost(agent)
        agent.session_total_tokens = 400
        tc.record_turn_cost(agent, snap, api_calls=1)  # must not raise

    def test_session_reset_yields_no_negative_deltas(self, captured_write):
        # If counters were reset mid-session (e.g. /reset), deltas clamp to 0.
        agent = _agent(session_input_tokens=5000, session_total_tokens=5000)
        snap = tc.snapshot_turn_cost(agent)
        agent.session_input_tokens = 100
        agent.session_total_tokens = 100
        tc.record_turn_cost(agent, snap, api_calls=1)
        assert captured_write[0]["input_tokens"] == 0
        assert captured_write[0]["total_tokens"] == 0


# ---------------------------------------------------------------------------
# DB round-trip — write + windowed rollups against the migrated test pool.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_and_fetch_roundtrip(thoth_db_initialized):
    for i in range(3):
        await tc.write_turn_cost(
            session_id="sess-rt",
            platform="cli",
            model="model-a" if i < 2 else "model-b",
            provider="prov",
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=10,
            cache_write_tokens=5,
            reasoning_tokens=2,
            total_tokens=167,
            api_calls=2,
            cost_usd=0.25 if i < 2 else None,
            cost_status="estimated" if i < 2 else "unknown",
            duration_ms=1000 * (i + 1),
        )

    summary = await tc.fetch_turn_summary(hours=1.0)
    assert summary["turns"] == 3
    assert summary["input_tokens"] == 300
    assert summary["total_tokens"] == 501
    assert summary["api_calls"] == 6
    assert summary["cost_usd"] == pytest.approx(0.50)
    assert summary["unpriced_turns"] == 1
    assert summary["p50_duration_ms"] == pytest.approx(2000)
    assert summary["p95_duration_ms"] == pytest.approx(2900)

    by_model = await tc.fetch_model_breakdown(hours=1.0)
    assert {r["model"] for r in by_model} == {"model-a", "model-b"}
    top = by_model[0]
    assert top["model"] == "model-a"  # priced spend sorts first
    assert top["turns"] == 2
    assert top["cost_usd"] == pytest.approx(0.50)


@pytest.mark.asyncio
async def test_window_excludes_old_rows(thoth_db_initialized):
    import thoth_db

    await tc.write_turn_cost(
        session_id="sess-old",
        platform="cli",
        model="m",
        provider="p",
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
        total_tokens=2,
        api_calls=1,
        cost_usd=1.0,
        cost_status="estimated",
        duration_ms=10,
    )
    async with thoth_db.transaction() as conn:
        await conn.execute(
            "UPDATE agent_turn_cost SET at = now() - interval '48 hours' "
            "WHERE session_id = 'sess-old'"
        )

    summary = await tc.fetch_turn_summary(hours=24.0)
    assert summary["turns"] == 0

    wide = await tc.fetch_turn_summary(hours=72.0)
    assert wide["turns"] == 1


@pytest.mark.asyncio
async def test_write_is_best_effort_without_pool():
    # No pool initialised in this test file's process state for this call
    # path: force the failure by pointing at a closed pool via monkeypatch
    # is overkill — write must swallow *any* exception, including "no pool".
    import thoth_db

    saved = thoth_db.pool
    try:
        def _boom():
            raise RuntimeError("no pool")

        thoth_db.pool = _boom
        await tc.write_turn_cost(
            session_id=None,
            platform="",
            model="",
            provider="",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            reasoning_tokens=0,
            total_tokens=1,
            api_calls=1,
            cost_usd=None,
            cost_status="unknown",
            duration_ms=1,
        )  # must not raise
    finally:
        thoth_db.pool = saved
