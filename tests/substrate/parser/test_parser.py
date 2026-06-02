"""Phase D Parser sub-agent — tick behaviour with the LLM mocked.

The LLM call (``extract.call_parser_llm``) and client resolution
(``extract.resolve_parser_client``) are monkeypatched, so these run
offline and deterministically. They cover the env/intensity gates, the
happy path (extract → persist → consolidate → self-state + audit), and
every degrade path (empty / timeout / parse_error / llm_error).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.l0 import commit_slice
from substrate.l1 import extract
from substrate.l1.schema import ParsedEntity, ParsedRelationship, ParserResult
from substrate.agents.parser import Parser


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
def _parser_on(monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_PARSER", "1")
    # Resolve a dummy client so the tick proceeds to call_parser_llm (which
    # tests monkeypatch). Tests that want the gate off override the env var.
    monkeypatch.setattr(extract, "resolve_parser_client", lambda: (object(), "mock-model"))


async def _seed(substrate, session_id, texts):
    """Commit slices directly as passed (born_passed) tagged with a session."""
    stream = await substrate.streams.get_by_name("thoth.world.user_message.cli")
    for t in texts:
        await commit_slice(
            substrate, stream.stream_id, t,
            event_time_world=datetime.now(timezone.utc),
            metadata={"session_id": session_id, "source": "cli"},
            born_passed=True,
        )


async def _parser_log_rows(outcome=None):
    import hermes_db

    async with hermes_db.connection() as conn:
        if outcome:
            return await conn.fetch(
                "SELECT * FROM substrate_parser_log WHERE outcome=$1", outcome
            )
        return await conn.fetch("SELECT * FROM substrate_parser_log")


@pytest.mark.asyncio
async def test_parser_disabled_is_noop(booted, monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_PARSER", "0")
    called = {"n": 0}

    async def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("should not be called when disabled")

    monkeypatch.setattr(extract, "call_parser_llm", _boom)
    await _seed(booted, "sess-x", [f"m{i}" for i in range(6)])
    await Parser(booted).tick()
    assert called["n"] == 0
    assert await _parser_log_rows() == []


@pytest.mark.asyncio
async def test_parser_intensity_off_is_noop(booted, monkeypatch):
    from substrate.agents.base import Level

    async def _boom(*a, **k):
        raise AssertionError("should not be called when OFF")

    monkeypatch.setattr(extract, "call_parser_llm", _boom)
    await _seed(booted, "sess-x", [f"m{i}" for i in range(6)])
    p = Parser(booted)
    p.set_intensity(Level.OFF)
    await p.tick()
    assert await _parser_log_rows() == []


@pytest.mark.asyncio
async def test_parser_extracts_persists_consolidates(booted, monkeypatch):
    import hermes_db

    await _seed(booted, "sess-1", ["Greg works on Thoth"] + [f"m{i}" for i in range(5)])

    async def _fake_call(batch, *, client=None, model=None):
        sid = batch[0].slice_id
        return ParserResult(
            entities=[
                ParsedEntity("Greg", "person", "maintainer", source_slice_ids=[sid], quote="Greg"),
                ParsedEntity("Thoth", "project", "the agent", source_slice_ids=[sid], quote="Thoth"),
            ],
            relationships=[
                ParsedRelationship("Greg", "person", "works_on", "Thoth", "project",
                                   confidence=0.9, source_slice_ids=[sid]),
            ],
        )

    monkeypatch.setattr(extract, "call_parser_llm", _fake_call)
    await Parser(booted).tick()

    # L1 written.
    from substrate.l1 import store

    greg = await store.find_entities_by_name("Greg", entity_type="person")
    assert greg and greg[0].name == "Greg"
    rels = await store.list_relationships_for_entity(greg[0].id, direction="out")
    assert any(r.predicate == "works_on" for r in rels)

    # Slices consolidated.
    async with hermes_db.connection() as conn:
        unconsolidated = await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_slices WHERE consolidation_state='unconsolidated' "
            "AND metadata->>'session_id'='sess-1'"
        )
        consolidated = await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_slices WHERE consolidation_state='consolidated' "
            "AND metadata->>'session_id'='sess-1'"
        )
    assert unconsolidated == 0 and consolidated == 6

    # Audit log + parser.extracted telemetry row.
    ok = await _parser_log_rows("ok")
    assert len(ok) == 1 and ok[0]["entities_emitted"] == 2 and ok[0]["slices_consolidated"] == 6
    async with hermes_db.connection() as conn:
        selfstate = await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_telemetry WHERE event='parser.extracted'"
        )
    assert selfstate == 1


@pytest.mark.asyncio
async def test_parser_empty_still_consolidates(booted, monkeypatch):
    import hermes_db

    await _seed(booted, "sess-2", [f"m{i}" for i in range(6)])

    async def _empty(batch, *, client=None, model=None):
        return ParserResult()

    monkeypatch.setattr(extract, "call_parser_llm", _empty)
    await Parser(booted).tick()

    async with hermes_db.connection() as conn:
        consolidated = await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_slices WHERE consolidation_state='consolidated' "
            "AND metadata->>'session_id'='sess-2'"
        )
    assert consolidated == 6  # flipped even though nothing extractable
    assert len(await _parser_log_rows("empty")) == 1


@pytest.mark.asyncio
async def test_parser_timeout_leaves_slices_unconsolidated(booted, monkeypatch):
    import hermes_db

    await _seed(booted, "sess-3", [f"m{i}" for i in range(6)])

    async def _timeout(batch, *, client=None, model=None):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(extract, "call_parser_llm", _timeout)
    await Parser(booted).tick()

    async with hermes_db.connection() as conn:
        unconsolidated = await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_slices WHERE consolidation_state='unconsolidated' "
            "AND metadata->>'session_id'='sess-3'"
        )
    assert unconsolidated == 6  # retriable next tick
    assert len(await _parser_log_rows("timeout")) == 1


@pytest.mark.asyncio
async def test_parser_parse_error_consolidates_to_avoid_loop(booted, monkeypatch):
    import hermes_db

    await _seed(booted, "sess-4", [f"m{i}" for i in range(6)])

    async def _bad(batch, *, client=None, model=None):
        raise extract.ParseError("garbage JSON")

    monkeypatch.setattr(extract, "call_parser_llm", _bad)
    await Parser(booted).tick()

    async with hermes_db.connection() as conn:
        consolidated = await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_slices WHERE consolidation_state='consolidated' "
            "AND metadata->>'session_id'='sess-4'"
        )
    assert consolidated == 6  # avoid re-processing bad input forever
    assert len(await _parser_log_rows("parse_error")) == 1


@pytest.mark.asyncio
async def test_parser_llm_error_leaves_unconsolidated(booted, monkeypatch):
    import hermes_db

    await _seed(booted, "sess-5", [f"m{i}" for i in range(6)])

    async def _err(batch, *, client=None, model=None):
        raise ValueError("provider down")

    monkeypatch.setattr(extract, "call_parser_llm", _err)
    await Parser(booted).tick()

    async with hermes_db.connection() as conn:
        unconsolidated = await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_slices WHERE consolidation_state='unconsolidated' "
            "AND metadata->>'session_id'='sess-5'"
        )
    assert unconsolidated == 6
    assert len(await _parser_log_rows("llm_error")) == 1


@pytest.mark.asyncio
async def test_parser_session_selection_honours_min(booted, monkeypatch):
    await _seed(booted, "sess-big", [f"m{i}" for i in range(6)])  # >= 5
    await _seed(booted, "sess-small", ["a", "b"])  # < 5
    sessions = await Parser(booted)._select_sessions()
    assert "sess-big" in sessions
    assert "sess-small" not in sessions
