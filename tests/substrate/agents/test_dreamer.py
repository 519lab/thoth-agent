"""Phase F Dreamer — counterfactual exploration + persistent log (LLM mocked)."""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.l1 import store as l1
from substrate.l3 import store as l3
from substrate.agents import dreamer as dreamer_mod
from substrate.agents.dreamer import Dreamer
from substrate.cli import inspect as inspect_mod


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


@pytest.fixture(autouse=True)
def _dreamer_on(monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_DREAMER", "1")
    monkeypatch.setattr(Dreamer, "_resolve_client", staticmethod(lambda: (object(), "mock")))


@pytest.mark.asyncio
async def test_append_and_list_dreams(hermes_db_initialized):
    await dreamer_mod.append_dream("seed A", "exploration A")
    await dreamer_mod.append_dream("seed B", "exploration B")
    dreams = await dreamer_mod.list_dreams(limit=10)
    assert len(dreams) == 2
    assert dreams[0]["seed"] == "seed B"  # most recent first


@pytest.mark.asyncio
async def test_dreamer_explores_from_pattern_seed(booted, monkeypatch):
    await l3.upsert_pattern("deploys cluster on fridays", "theme")

    async def _fake(seed, *, client=None, model=None, substrate=None):
        assert "fridays" in seed
        return "What if friday deploys correlate with weekend incident spikes?"

    monkeypatch.setattr(dreamer_mod, "_dream", _fake)
    await Dreamer(booted).tick()

    dreams = await dreamer_mod.list_dreams()
    assert len(dreams) == 1
    assert "weekend incident" in dreams[0]["exploration"]


@pytest.mark.asyncio
async def test_dreamer_seeds_from_entities_when_no_patterns(booted, monkeypatch):
    await l1.upsert_entity("Greg", "person")
    await l1.upsert_entity("Thoth", "project")

    captured = {}

    async def _fake(seed, *, client=None, model=None, substrate=None):
        captured["seed"] = seed
        return "an exploration"

    monkeypatch.setattr(dreamer_mod, "_dream", _fake)
    await Dreamer(booted).tick()
    assert "What connects" in captured["seed"]


@pytest.mark.asyncio
async def test_dreamer_disabled_is_noop(booted, monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_DREAMER", "0")
    await l3.upsert_pattern("x", "theme")

    async def _boom(*a, **k):
        raise AssertionError("should not dream when disabled")

    monkeypatch.setattr(dreamer_mod, "_dream", _boom)
    await Dreamer(booted).tick()
    assert await dreamer_mod.list_dreams() == []


@pytest.mark.asyncio
async def test_inspect_dreamer(hermes_db_initialized):
    import thoth_db

    await dreamer_mod.append_dream("a seed", "a vivid exploration")
    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_dreamer(conn)
    assert "a seed" in buf.getvalue() and "vivid exploration" in buf.getvalue()
