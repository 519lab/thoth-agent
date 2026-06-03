"""L3 store + Pattern-finder + extract coercion + inspect (LLM mocked)."""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.l1 import store as l1
from substrate.l3 import extract, store as l3
from substrate.l3.schema import ParsedPattern, PatternResult
from substrate.agents.pattern_finder import PatternFinder
from substrate.cli import inspect as inspect_mod


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_pattern_merges_and_bumps_salience(hermes_db_initialized):
    pid1, c1 = await l3.upsert_pattern("Greg favours infra work", "generalization",
                                       cites=["a"], confidence=0.6)
    pid2, c2 = await l3.upsert_pattern("Greg favours infra work", "generalization",
                                       cites=["b"], confidence=0.9)
    assert c1 is True and c2 is False and pid1 == pid2
    pats = await l3.list_patterns()
    p = next(p for p in pats if p.id == pid1)
    assert p.salience_score == pytest.approx(0.6)  # 0.5 + 0.1 bump
    assert p.confidence == pytest.approx(0.9)
    assert set(p.cites) == {"a", "b"}


@pytest.mark.asyncio
async def test_get_patterns_for_query(hermes_db_initialized):
    await l3.upsert_pattern("PostgreSQL is the storage backend", "theme")
    await l3.upsert_pattern("the team prefers async python", "theme")
    hits = await l3.get_patterns_for_query("postgresql storage")
    assert any("PostgreSQL" in p.statement for p in hits)


# ---------------------------------------------------------------------------
# extract coercion
# ---------------------------------------------------------------------------


def test_coerce_normalises_and_drops_malformed():
    data = {
        "patterns": [
            {"statement": "X recurs", "kind": "Recurring-Structure", "confidence": 2},
            {"kind": "theme"},  # no statement → dropped
        ]
    }
    res = extract._coerce(data)
    assert len(res.patterns) == 1
    assert res.patterns[0].kind == "recurring_structure"
    assert res.patterns[0].confidence == 1.0


def test_coerce_rejects_non_object():
    with pytest.raises(extract.PatternError):
        extract._coerce([1, 2])


# ---------------------------------------------------------------------------
# Pattern-finder agent
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


@pytest.fixture(autouse=True)
def _pf_on(monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_PATTERNFINDER", "1")
    monkeypatch.setenv("PATTERNFINDER_MIN_ENTITIES", "2")
    monkeypatch.setattr(extract, "resolve_pattern_client", lambda: (object(), "mock"))


@pytest.mark.asyncio
async def test_pattern_finder_writes_patterns(booted, monkeypatch):
    # Seed enough L1 entities to clear the min threshold.
    await l1.upsert_entity("Greg", "person", summary="works on infra")
    await l1.upsert_entity("Thoth", "project", summary="the agent")
    await l1.upsert_entity("substrate", "concept", summary="memory layer")

    async def _fake(context, *, client=None, model=None, substrate=None):
        assert "Greg" in context  # the context block reaches the LLM
        return PatternResult(patterns=[
            ParsedPattern("Greg works on infra-heavy projects", "generalization",
                          entity_names=["Greg", "Thoth"], confidence=0.8),
        ])

    monkeypatch.setattr(extract, "call_pattern_llm", _fake)
    await PatternFinder(booted).tick()

    pats = await l3.list_patterns()
    assert any("infra-heavy" in p.statement for p in pats)
    p = pats[0]
    assert len(p.cites) == 2  # resolved Greg + Thoth entity ids


@pytest.mark.asyncio
async def test_pattern_finder_disabled_is_noop(booted, monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_PATTERNFINDER", "0")
    await l1.upsert_entity("A", "concept")
    await l1.upsert_entity("B", "concept")

    async def _boom(*a, **k):
        raise AssertionError("should not call LLM when disabled")

    monkeypatch.setattr(extract, "call_pattern_llm", _boom)
    await PatternFinder(booted).tick()
    assert await l3.list_patterns() == []


@pytest.mark.asyncio
async def test_pattern_finder_skips_below_min_entities(booted, monkeypatch):
    monkeypatch.setenv("PATTERNFINDER_MIN_ENTITIES", "10")
    await l1.upsert_entity("Lonely", "concept")

    async def _boom(*a, **k):
        raise AssertionError("should not call LLM below min entities")

    monkeypatch.setattr(extract, "call_pattern_llm", _boom)
    await PatternFinder(booted).tick()
    assert await l3.list_patterns() == []


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def test_register_subparser_l3():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    inspect_mod.register_subparser(sub)
    assert callable(parser.parse_args(["substrate", "l3", "patterns", "--limit", "3"]).func)


@pytest.mark.asyncio
async def test_print_l3_patterns(hermes_db_initialized):
    import thoth_db

    await l3.upsert_pattern("Async is preferred", "theme", confidence=0.7)
    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_l3_patterns(conn)
    assert "Async is preferred" in buf.getvalue() and "theme" in buf.getvalue()
