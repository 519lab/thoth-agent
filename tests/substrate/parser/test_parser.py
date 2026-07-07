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
async def booted(thoth_db_initialized):
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
    monkeypatch.setenv("THOTH_SUBSTRATE_PARSER", "1")
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
    import thoth_db

    async with thoth_db.connection() as conn:
        if outcome:
            return await conn.fetch(
                "SELECT * FROM substrate_parser_log WHERE outcome=$1", outcome
            )
        return await conn.fetch("SELECT * FROM substrate_parser_log")


@pytest.mark.asyncio
async def test_parser_disabled_is_noop(booted, monkeypatch):
    monkeypatch.setenv("THOTH_SUBSTRATE_PARSER", "0")
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
    import thoth_db

    await _seed(booted, "sess-1", ["Greg works on Thoth"] + [f"m{i}" for i in range(5)])

    async def _fake_call(batch, *, client=None, model=None, substrate=None):
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
    async with thoth_db.connection() as conn:
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
    async with thoth_db.connection() as conn:
        selfstate = await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_telemetry WHERE event='parser.extracted'"
        )
    assert selfstate == 1


@pytest.mark.asyncio
async def test_parser_empty_still_consolidates(booted, monkeypatch):
    import thoth_db

    await _seed(booted, "sess-2", [f"m{i}" for i in range(6)])

    async def _empty(batch, *, client=None, model=None, substrate=None):
        return ParserResult()

    monkeypatch.setattr(extract, "call_parser_llm", _empty)
    await Parser(booted).tick()

    async with thoth_db.connection() as conn:
        consolidated = await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_slices WHERE consolidation_state='consolidated' "
            "AND metadata->>'session_id'='sess-2'"
        )
    assert consolidated == 6  # flipped even though nothing extractable
    assert len(await _parser_log_rows("empty")) == 1


@pytest.mark.asyncio
async def test_parser_timeout_leaves_slices_unconsolidated(booted, monkeypatch):
    import thoth_db

    await _seed(booted, "sess-3", [f"m{i}" for i in range(6)])

    async def _timeout(batch, *, client=None, model=None, substrate=None):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(extract, "call_parser_llm", _timeout)
    await Parser(booted).tick()

    async with thoth_db.connection() as conn:
        unconsolidated = await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_slices WHERE consolidation_state='unconsolidated' "
            "AND metadata->>'session_id'='sess-3'"
        )
    assert unconsolidated == 6  # retriable next tick
    assert len(await _parser_log_rows("timeout")) == 1


@pytest.mark.asyncio
async def test_parser_parse_error_consolidates_to_avoid_loop(booted, monkeypatch):
    import thoth_db

    await _seed(booted, "sess-4", [f"m{i}" for i in range(6)])

    async def _bad(batch, *, client=None, model=None, substrate=None):
        raise extract.ParseError("garbage JSON")

    monkeypatch.setattr(extract, "call_parser_llm", _bad)
    await Parser(booted).tick()

    async with thoth_db.connection() as conn:
        consolidated = await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_slices WHERE consolidation_state='consolidated' "
            "AND metadata->>'session_id'='sess-4'"
        )
    assert consolidated == 6  # avoid re-processing bad input forever
    assert len(await _parser_log_rows("parse_error")) == 1


@pytest.mark.asyncio
async def test_parser_llm_error_leaves_unconsolidated(booted, monkeypatch):
    import thoth_db

    await _seed(booted, "sess-5", [f"m{i}" for i in range(6)])

    async def _err(batch, *, client=None, model=None, substrate=None):
        raise ValueError("provider down")

    monkeypatch.setattr(extract, "call_parser_llm", _err)
    await Parser(booted).tick()

    async with thoth_db.connection() as conn:
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


async def _age_session(session_id: str, offset_seconds: float) -> None:
    """Move a seeded session's slices back in time (all time columns
    together so the event <= perception <= ingest CHECK holds)."""
    import thoth_db

    async with thoth_db.connection() as conn:
        await conn.execute(
            """
            UPDATE substrate_slices
               SET event_time_world      = now() - make_interval(secs => $2),
                   perception_time_world = now() - make_interval(secs => $2),
                   ingest_time_world     = now() - make_interval(secs => $2),
                   time_start_world      = now() - make_interval(secs => $2),
                   time_end_world        = now() - make_interval(secs => $2)
             WHERE metadata->>'session_id' = $1
            """,
            session_id,
            float(offset_seconds),
        )


@pytest.mark.asyncio
async def test_parser_session_selection_flushes_stale_small_sessions(
    booted, monkeypatch
):
    """Low-water flush (issue #287): a below-minimum session whose oldest
    pending slice exceeds PARSER_SESSION_FLUSH_AGE_SECONDS is selected
    anyway, so session tails drain instead of aging past the 7-day fetch
    horizon and becoming immortal."""
    monkeypatch.setenv("PARSER_SESSION_FLUSH_AGE_SECONDS", "60")

    await _seed(booted, "sess-stale-tail", ["a", "b"])  # < 5 pending
    await _age_session("sess-stale-tail", 120.0)  # older than flush age

    await _seed(booted, "sess-fresh-tail", ["c", "d"])  # < 5, fresh

    sessions = await Parser(booted)._select_sessions()
    assert "sess-stale-tail" in sessions
    assert "sess-fresh-tail" not in sessions


@pytest.mark.asyncio
async def test_parser_flush_does_not_reach_past_fetch_horizon(booted, monkeypatch):
    """A tail older than the 7-day fetch horizon stays unselected — that
    set belongs to the Curator's stranded drain, not the Parser."""
    monkeypatch.setenv("PARSER_SESSION_FLUSH_AGE_SECONDS", "60")

    await _seed(booted, "sess-ancient", ["a", "b"])
    await _age_session("sess-ancient", 8 * 86400.0)  # past the 7-day horizon

    sessions = await Parser(booted)._select_sessions()
    assert "sess-ancient" not in sessions
