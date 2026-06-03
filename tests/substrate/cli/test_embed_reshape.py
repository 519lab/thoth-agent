"""Tests for ``thoth embed reshape <DIM>``.

The reshape command is the most invasive substrate operation: it drops
an index, NULLs every embedding, ALTERs a column type, recreates the
index, then re-embeds inline. These tests exercise the in-process
pieces (arg parsing, current-dim detection, reshape SQL, backfill loop)
against the docker-compose test PG. The embedding provider is mocked
so we don't need an OpenAI key in CI.
"""

from __future__ import annotations

import argparse
from uuid import uuid4

import pytest
import pytest_asyncio

from substrate.cli import embed as embed_cli


# ---------------------------------------------------------------------------
# Helpers — seed slices the reshape command can re-embed.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seeded_substrate(hermes_db_initialized):
    """Seed a stream + a handful of slices with NULL embeddings."""
    import thoth_db
    from substrate import Substrate
    from substrate.l0 import commit_slice
    from substrate.storage import DEFAULT_TEXT_PROFILE, Family, Modality

    sub = Substrate.from_pool(thoth_db.pool())
    stream = await sub.streams.register(
        name="hermes.test.embed_reshape",
        family=Family.EXTEROCEPTIVE,
        modality=Modality.TEXT,
        source="test",
        organ="pytest",
        decay_profile_id=DEFAULT_TEXT_PROFILE,
    )
    # 5 slices — small enough to fit one batch, large enough that
    # batch-size-aware code paths get exercised.
    for i in range(5):
        await commit_slice(
            sub, stream.stream_id, f"slice {i}",
            event_time_world=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
        )
    # Flip pending → passed so the reshape's count queries see them.
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE substrate_slices SET sentinel_state = 'passed', "
            "pending_committed_at = NULL WHERE stream_id = $1",
            stream.stream_id,
        )
    return sub


# ---------------------------------------------------------------------------
# _current_schema_dim — reads the live vector(N) col size.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_current_schema_dim_reads_pg(seeded_substrate):
    """Reads the actual column dim — default install is 1536."""
    dim = await embed_cli._current_schema_dim()
    assert dim == 1536


# ---------------------------------------------------------------------------
# _reshape_async — full integration including SQL + (mocked) re-embed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reshape_noop_when_dim_matches(seeded_substrate, capsys):
    """target == current → skip reshape entirely, return 0."""
    rc = await embed_cli._reshape_async(
        target=1536, interactive=False, reembed=False, batch_size=10
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "already vector(1536)" in out


@pytest.mark.asyncio
async def test_reshape_changes_column_dim_and_clears(seeded_substrate, monkeypatch):
    """target != current → drop index, NULL embeddings, ALTER, recreate.
    Verify the column is now vector(768) and embeddings are NULL."""
    import thoth_db
    from substrate.recall import embeddings as _embed

    # Stub the provider so re-embed produces deterministic 768-d vectors.
    monkeypatch.setenv(_embed.MOCK_ENV_VAR, "1")
    # Mock returns 1536-d vectors by default — patch the constant for
    # the test so the mock path emits 768-d vectors matching the new dim.
    monkeypatch.setattr(_embed, "EMBEDDING_DIM", 768)
    _embed.reset_schema_dim_cache()

    rc = await embed_cli._reshape_async(
        target=768, interactive=False, reembed=True, batch_size=10
    )
    assert rc == 0

    # Verify schema changed.
    new_dim = await embed_cli._current_schema_dim()
    assert new_dim == 768

    # Verify embeddings were re-populated (mock path is synchronous +
    # deterministic; all 5 seeded slices on our stream should now have
    # non-NULL 768-d vectors).
    async with thoth_db.connection() as conn:
        rows = await conn.fetch(
            "SELECT embedding FROM substrate_slices "
            "WHERE stream_id IN "
            "  (SELECT stream_id FROM substrate_streams "
            "   WHERE name = 'hermes.test.embed_reshape')"
        )
    assert len(rows) == 5
    assert all(r["embedding"] is not None for r in rows)
    assert all(len(r["embedding"]) == 768 for r in rows)


@pytest.mark.asyncio
async def test_reshape_no_reembed_leaves_nulls(seeded_substrate):
    """--no-reembed: reshape happens, embeddings stay NULL, Curator
    backfills later."""
    import thoth_db

    rc = await embed_cli._reshape_async(
        target=1024, interactive=False, reembed=False, batch_size=10
    )
    assert rc == 0

    new_dim = await embed_cli._current_schema_dim()
    assert new_dim == 1024

    async with thoth_db.connection() as conn:
        rows = await conn.fetch(
            "SELECT embedding FROM substrate_slices "
            "WHERE stream_id IN "
            "  (SELECT stream_id FROM substrate_streams "
            "   WHERE name = 'hermes.test.embed_reshape')"
        )
    assert len(rows) == 5
    assert all(r["embedding"] is None for r in rows)


@pytest.mark.asyncio
async def test_reshape_includes_upper_layers(seeded_substrate):
    """reshape moves l3_patterns/l4_observations embedding too, not just
    substrate_slices. Regression: leaving them at the old dim stalls the
    Curator's L3/L4 backfill (1536-vs-768 mismatch incident)."""
    import thoth_db

    rc = await embed_cli._reshape_async(
        target=1024, interactive=False, reembed=False, batch_size=10
    )
    assert rc == 0
    async with thoth_db.connection() as conn:
        for tbl in ("substrate_slices", "l3_patterns", "l4_observations"):
            dim = await embed_cli._table_vector_dim(conn, tbl)
            assert dim == 1024, f"{tbl} not reshaped to 1024 (got {dim})"


# ---------------------------------------------------------------------------
# Arg parser sanity — verify required+optional flags surface.
# ---------------------------------------------------------------------------


def test_cmd_embed_reshape_drives_sync_loop(hermes_db_initialized_sync):
    """Regression: the sync entrypoint must drive the coro via
    thoth_db.run_sync (the loop the asyncpg pool is bound to), not a fresh
    event loop — otherwise asyncpg raises 'another operation is in progress'
    / 'attached to a different loop'. Exercise with a no-op (target == current
    dim) so it runs the connection path without mutating the schema."""
    args = argparse.Namespace(dim=1536, yes=True, no_reembed=True, batch_size=10)
    rc = embed_cli._cmd_embed_reshape(args)
    assert rc == 0  # would raise a cross-loop RuntimeError before the fix


def test_reshape_parser_requires_dim():
    """``thoth embed reshape`` without DIM should fail with non-zero exit."""
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest="cmd")
    embed_cli.register_subparser(sp)
    with pytest.raises(SystemExit):
        p.parse_args(["embed", "reshape"])


def test_reshape_parser_accepts_dim():
    """Happy path: parsing succeeds with all the flags."""
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest="cmd")
    embed_cli.register_subparser(sp)
    args = p.parse_args(["embed", "reshape", "768", "--yes", "--no-reembed", "--batch-size", "100"])
    assert args.dim == 768
    assert args.yes is True
    assert args.no_reembed is True
    assert args.batch_size == 100
