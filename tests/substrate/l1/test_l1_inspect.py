"""Phase D inspect surface — `thoth substrate l1` + `thoth substrate parser`."""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout

import pytest

from substrate.cli import inspect as inspect_mod
from substrate.l1 import store


def test_register_subparser_l1_and_parser():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    inspect_mod.register_subparser(sub)
    assert callable(parser.parse_args(["substrate", "l1", "entities", "--limit", "3"]).func)
    assert callable(parser.parse_args(["substrate", "l1", "relationships"]).func)
    assert callable(parser.parse_args(["substrate", "parser"]).func)
    assert callable(parser.parse_args(["substrate", "parser", "recent", "--limit", "5"]).func)


@pytest.mark.asyncio
async def test_print_l1_entities_empty(thoth_db_initialized):
    import thoth_db

    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_l1_entities(conn)
    assert "no L1 entities" in buf.getvalue()


@pytest.mark.asyncio
async def test_print_l1_entities_lists(thoth_db_initialized):
    import thoth_db

    subj, _ = await store.upsert_entity("Greg", "person", summary="maintainer")
    obj, _ = await store.upsert_entity("Thoth", "project")
    await store.upsert_relationship(subj, "works_on", obj)

    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_l1_entities(conn)
        out = buf.getvalue()
    assert "Greg" in out and "person" in out

    buf2 = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf2):
            await inspect_mod._print_l1_relationships(conn)
    assert "works_on" in buf2.getvalue()


@pytest.mark.asyncio
async def test_print_parser_summary_and_recent(thoth_db_initialized):
    import thoth_db

    async with thoth_db.connection() as conn:
        await conn.execute(
            """
            INSERT INTO substrate_parser_log
                (session_id, batch_size, entities_emitted, relationships_emitted,
                 slices_consolidated, latency_ms, model, outcome)
            VALUES ('sess-z', 20, 3, 2, 20, 1500, 'mock', 'ok')
            """
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            await inspect_mod._print_parser_summary(conn)
        summary = buf.getvalue()
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            await inspect_mod._print_parser_recent(conn, limit=10)
        recent = buf2.getvalue()
    assert "Parser state" in summary and "calls" in summary and "ok" in summary
    assert "ok" in recent and "ents=3" in recent
