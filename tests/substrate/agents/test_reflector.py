"""Phase F Reflector — L3/L4 synthesis (LLM mocked)."""

from __future__ import annotations

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.l3 import store as l3
from substrate.l4 import store as l4
from substrate.agents import reflector as reflector_mod
from substrate.agents.reflector import Reflection, Reflector, ReflectorResult


def test_coerce_normalises_layer_and_clamps():
    data = {"reflections": [
        {"statement": "infra-heavy knowledge", "layer": "L4", "confidence": 5},
        {"statement": "recurring deploy theme", "layer": "l3"},
        {"layer": "l4"},  # no statement → dropped
    ]}
    res = reflector_mod._coerce(data)
    assert len(res.reflections) == 2
    assert res.reflections[0].layer == "l4" and res.reflections[0].confidence == 1.0
    assert res.reflections[1].layer == "l3"


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
def _reflector_on(monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_REFLECTOR", "1")
    monkeypatch.setattr(reflector_mod, "resolve_reflector_client",
                        lambda: (object(), "mock"))


@pytest.mark.asyncio
async def test_reflector_writes_l3_and_l4(booted, monkeypatch):
    # Seed L3 material so _build_context has something to reflect on.
    await l3.upsert_pattern("deploys happen on fridays", "theme")

    async def _fake(context, *, client=None, model=None, substrate=None):
        assert "deploys happen" in context
        return ReflectorResult(reflections=[
            Reflection("the agent over-indexes on deploy topics", "l4", "bias", 0.8),
            Reflection("friday deployment is a recurring theme", "l3", "theme", 0.7),
        ])

    monkeypatch.setattr(reflector_mod, "_synthesize", _fake)
    await Reflector(booted).tick()

    l4_notes = await l4.list_observations(subject="self")
    assert any("over-indexes" in o.statement for o in l4_notes)
    patterns = await l3.list_patterns()
    assert any("friday deployment" in p.statement for p in patterns)


@pytest.mark.asyncio
async def test_reflector_skips_without_material(booted, monkeypatch):
    # No L3 patterns → _build_context returns no material → no LLM call.
    async def _boom(*a, **k):
        raise AssertionError("should not synthesize without material")

    monkeypatch.setattr(reflector_mod, "_synthesize", _boom)
    await Reflector(booted).tick()
    assert await l4.list_observations() == []


@pytest.mark.asyncio
async def test_reflector_disabled_is_noop(booted, monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_REFLECTOR", "0")
    await l3.upsert_pattern("something", "theme")

    async def _boom(*a, **k):
        raise AssertionError("should not run when disabled")

    monkeypatch.setattr(reflector_mod, "_synthesize", _boom)
    await Reflector(booted).tick()
    assert await l4.list_observations() == []
