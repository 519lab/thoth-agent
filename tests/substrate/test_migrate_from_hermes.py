"""Tests for the one-time substrate stream-name rename (Phase 6 cutover).

``thoth db migrate-from-hermes`` renames legacy ``hermes.*`` substrate stream
names to ``thoth.*`` in place. This is a clean cutover: no ``thoth.*`` twins
of the legacy ``hermes.*`` streams exist, so the rename never collides with the
UNIQUE(name) constraint. Slices reference their stream by ``stream_id`` and so
follow the rename automatically.

These run against the per-test ``thoth`` PG fixture (Alembic head already
applied by ``hermes_db_dsn``). The ``default-structured`` decay profile +
the ``substrate.self_state`` stream are seeded by the skeleton migration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from thoth_cli.db_commands import migrate_from_hermes

# Seeded by migrations/versions/20260523_0003_substrate_skeleton.py
PROFILE_DEFAULT_STRUCTURED = UUID("00000000-0000-5000-8000-000000000002")

STREAM_A = UUID("00000000-0000-4000-8000-0000000000a1")
STREAM_B = UUID("00000000-0000-4000-8000-0000000000a2")
SLICE_A = UUID("00000000-0000-4000-8000-0000000000b1")


async def _seed_streams(conn):
    """Insert two hermes.* streams + a slice referencing STREAM_A by id."""
    for stream_id, name in ((STREAM_A, "hermes.telegram"), (STREAM_B, "hermes.cli")):
        await conn.execute(
            """
            INSERT INTO substrate_streams
                (stream_id, name, family, modality, source, organ,
                 lifecycle_state, decay_profile_id)
            VALUES
                ($1, $2, 'exteroceptive', 'structured_event',
                 'test', 'test-organ', 'active', $3)
            """,
            stream_id,
            name,
            PROFILE_DEFAULT_STRUCTURED,
        )
    ts = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    await conn.execute(
        """
        INSERT INTO substrate_slices
            (slice_id, stream_id, time_start_world, time_end_world,
             event_time_world, perception_time_world, ingest_time_world,
             payload_modality, payload)
        VALUES
            ($1, $2, $3, $3, $3, $3, $3, 'structured_event', $4)
        """,
        SLICE_A,
        STREAM_A,
        ts,
        {"hello": "world"},
    )


@pytest.mark.asyncio
async def test_migrate_renames_and_slice_follows(hermes_db_initialized):
    """Rename rewrites hermes.* -> thoth.*; slices follow by stream_id."""
    import thoth_db

    async with thoth_db.transaction() as conn:
        await _seed_streams(conn)

    rows = await migrate_from_hermes()
    assert rows == 2

    async with thoth_db.connection() as conn:
        # (1) stream names are now thoth.*
        name_a = await conn.fetchval(
            "SELECT name FROM substrate_streams WHERE stream_id = $1", STREAM_A
        )
        name_b = await conn.fetchval(
            "SELECT name FROM substrate_streams WHERE stream_id = $1", STREAM_B
        )
        assert name_a == "thoth.telegram"
        assert name_b == "thoth.cli"
        # No hermes.* names remain.
        leftover = await conn.fetchval(
            "SELECT count(*) FROM substrate_streams WHERE name LIKE 'hermes.%'"
        )
        assert leftover == 0

        # (2) the slice still resolves to its (renamed) stream via stream_id.
        resolved_name = await conn.fetchval(
            """
            SELECT s.name
              FROM substrate_slices sl
              JOIN substrate_streams s ON s.stream_id = sl.stream_id
             WHERE sl.slice_id = $1
            """,
            SLICE_A,
        )
        assert resolved_name == "thoth.telegram"


@pytest.mark.asyncio
async def test_migrate_is_idempotent(hermes_db_initialized):
    """A second pass after a clean cutover matches zero rows."""
    import thoth_db

    async with thoth_db.transaction() as conn:
        await _seed_streams(conn)

    first = await migrate_from_hermes()
    assert first == 2

    # (3) re-running is a no-op.
    second = await migrate_from_hermes()
    assert second == 0


@pytest.mark.asyncio
async def test_dry_run_counts_without_writing(hermes_db_initialized):
    """dry_run returns the matching count and leaves names untouched."""
    import thoth_db

    async with thoth_db.transaction() as conn:
        await _seed_streams(conn)

    counted = await migrate_from_hermes(dry_run=True)
    assert counted == 2

    async with thoth_db.connection() as conn:
        still_hermes = await conn.fetchval(
            "SELECT count(*) FROM substrate_streams WHERE name LIKE 'hermes.%'"
        )
        assert still_hermes == 2
