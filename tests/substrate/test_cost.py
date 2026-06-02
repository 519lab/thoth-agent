"""``substrate.cost`` — per-call auxiliary-model token/usage sink.

Covers the two public surfaces:

* ``record_usage`` — appends one ``substrate_agent_cost`` row (DB round-trip),
  and is best-effort (a failing pool is swallowed, never raised).
* ``acreate_and_record`` — a transparent ``chat.completions.create`` wrapper:
  returns the provider response unchanged, records the reported usage, guards
  missing/None usage, and skips recording entirely when ``substrate is None``.

The LLM client is faked, so these run offline and deterministically; the DB
assertions use the migrated test pool via the ``booted`` fixture.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

import hermes_db
from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate import cost


# ---------------------------------------------------------------------------
# Fixtures + fakes
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def booted(hermes_db_initialized):
    sub = await Substrate.boot(
        config=SubstrateConfig(auto_migrate=False, start_subagents=False),
        start_subagents=False,
    )
    try:
        yield sub
    finally:
        await sub.shutdown()


class _FakeUsage:
    def __init__(self, prompt, completion, total):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total


class _FakeResponse:
    def __init__(self, usage):
        self.usage = usage
        self.choices = ["sentinel"]  # proves identity pass-through


class _FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class _FakeClient:
    """Shaped like the OpenAI-compatible client: ``client.chat.completions``."""

    def __init__(self, response):
        completions = _FakeCompletions(response)
        self.chat = type("_Chat", (), {"completions": completions})()
        # Expose for assertions.
        self.completions = completions


async def _rows_for(agent: str) -> list[dict]:
    async with hermes_db.connection() as conn:
        return [
            dict(r)
            for r in await conn.fetch(
                "SELECT agent, model, prompt_tokens, completion_tokens, "
                "total_tokens, latency_ms FROM substrate_agent_cost "
                "WHERE agent = $1 ORDER BY at",
                agent,
            )
        ]


# ---------------------------------------------------------------------------
# record_usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_usage_appends_row(booted):
    await cost.record_usage(
        booted,
        agent="test-record",
        model="m-1",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        latency_ms=42,
    )

    rows = await _rows_for("test-record")
    assert len(rows) == 1
    assert rows[0] == {
        "agent": "test-record",
        "model": "m-1",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "latency_ms": 42,
    }


@pytest.mark.asyncio
async def test_record_usage_swallows_pool_errors():
    """Best-effort: a failing pool must never raise out of record_usage."""

    class _BoomPool:
        def acquire(self):
            raise RuntimeError("pool down")

    class _BoomSubstrate:
        pool = _BoomPool()

    # Must not raise.
    await cost.record_usage(
        _BoomSubstrate(),
        agent="test-boom",
        model="m",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        latency_ms=1,
    )


# ---------------------------------------------------------------------------
# acreate_and_record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acreate_records_usage_and_returns_response(booted):
    response = _FakeResponse(_FakeUsage(100, 40, 140))
    client = _FakeClient(response)

    returned = await cost.acreate_and_record(
        client,
        substrate=booted,
        agent="test-acreate",
        model="aux-model",
        messages=[{"role": "user", "content": "hi"}],
    )

    # Transparent pass-through — exact same object, untouched.
    assert returned is response
    assert returned.choices == ["sentinel"]
    # The create() call received the kwargs verbatim (model + messages).
    assert client.completions.calls == [
        {"model": "aux-model", "messages": [{"role": "user", "content": "hi"}]}
    ]

    rows = await _rows_for("test-acreate")
    assert len(rows) == 1
    row = rows[0]
    assert row["model"] == "aux-model"
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (
        100,
        40,
        140,
    )
    assert row["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_acreate_skips_recording_when_substrate_none(booted):
    response = _FakeResponse(_FakeUsage(7, 3, 10))
    client = _FakeClient(response)

    returned = await cost.acreate_and_record(
        client,
        substrate=None,
        agent="test-none",
        model="m",
    )

    # Still creates + returns the response…
    assert returned is response
    assert len(client.completions.calls) == 1
    # …but records nothing.
    assert await _rows_for("test-none") == []


@pytest.mark.asyncio
async def test_acreate_guards_missing_usage(booted):
    """A response without usage (or usage=None) records zeros, never raises."""
    response = _FakeResponse(usage=None)
    client = _FakeClient(response)

    returned = await cost.acreate_and_record(
        client,
        substrate=booted,
        agent="test-no-usage",
        model="m",
    )

    assert returned is response
    rows = await _rows_for("test-no-usage")
    assert len(rows) == 1
    assert (
        rows[0]["prompt_tokens"],
        rows[0]["completion_tokens"],
        rows[0]["total_tokens"],
    ) == (0, 0, 0)
