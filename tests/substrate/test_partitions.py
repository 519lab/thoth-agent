"""Tests for ``substrate.storage.partitions``.

Verifies:
* ``ensure_partitions`` is idempotent.
* Writes to ``substrate_slices`` route to the correct month partition.
* Writes outside the carved-out range land in ``substrate_slices_default``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

import pytest

from substrate.storage.partitions import (
    ensure_partitions,
    find_underindexed_partitions,
    list_existing_partitions,
    _month_ranges,
)


# ---------------------------------------------------------------------------
# Pure-Python helper math.
# ---------------------------------------------------------------------------


class TestMonthRanges:
    def test_three_consecutive_months_no_year_wrap(self):
        refs = _month_ranges(date(2026, 5, 15), ahead_months=2)
        assert refs == [
            ("substrate_slices_202605", date(2026, 5, 1), date(2026, 6, 1)),
            ("substrate_slices_202606", date(2026, 6, 1), date(2026, 7, 1)),
            ("substrate_slices_202607", date(2026, 7, 1), date(2026, 8, 1)),
        ]

    def test_year_wrap_at_december(self):
        # Starting in Nov 2026 with ahead=2 crosses into Jan 2027.
        refs = _month_ranges(date(2026, 11, 1), ahead_months=2)
        names = [r[0] for r in refs]
        assert names == [
            "substrate_slices_202611",
            "substrate_slices_202612",
            "substrate_slices_202701",
        ]
        # The hi of Dec is Jan-1 of next year (verifies the wrap math).
        assert refs[1][2] == date(2027, 1, 1)

    def test_ahead_months_zero_yields_one_partition(self):
        refs = _month_ranges(date(2026, 5, 15), ahead_months=0)
        assert len(refs) == 1
        assert refs[0][0] == "substrate_slices_202605"


# ---------------------------------------------------------------------------
# DB-side: ensure_partitions creates and is idempotent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_partitions_creates_three_months(thoth_db_initialized):
    """A fresh call with the default ``ahead_months=2`` creates the
    current month + 2 ahead. The migration already created current + 1;
    one extra month should land here."""
    import thoth_db

    today = date(2026, 8, 15)  # deterministic — independent of system clock
    async with thoth_db.connection() as conn:
        names = await ensure_partitions(conn, ahead_months=2, today=today)
    assert names == [
        "substrate_slices_202608",
        "substrate_slices_202609",
        "substrate_slices_202610",
    ]
    async with thoth_db.connection() as conn:
        existing = await list_existing_partitions(conn)
    for name in names:
        assert name in existing, f"{name} not in {existing}"


@pytest.mark.asyncio
async def test_ensure_partitions_idempotent(thoth_db_initialized):
    """Calling ``ensure_partitions`` twice with the same reference date
    is a no-op on the second call — no DDL errors, no duplicates.
    """
    import thoth_db

    today = date(2026, 9, 1)
    async with thoth_db.connection() as conn:
        first = await ensure_partitions(conn, ahead_months=1, today=today)
        second = await ensure_partitions(conn, ahead_months=1, today=today)
    assert first == second


@pytest.mark.asyncio
async def test_write_routes_to_correct_month_partition(thoth_db_initialized):
    """A row inserted with ``ingest_time_world`` in month M lands in
    ``substrate_slices_YYYYMM``. We verify via ``tableoid::regclass``
    which PG fills in with the actual partition relation name.
    """
    import thoth_db

    # Use an arbitrary month that's neither in the migration's bootstrap
    # range nor today's system month — forces us to carve out a new one.
    target_month = date(2027, 3, 1)
    async with thoth_db.connection() as conn:
        await ensure_partitions(conn, ahead_months=0, today=target_month)
        # Manual INSERT — sidestep commit_slice (Task 7) which doesn't
        # exist yet. We use the substrate.self_state stream + default-
        # structured profile both seeded by the migration.
        await conn.execute(
            """
            INSERT INTO substrate_slices
                (slice_id, stream_id, time_start_world, time_end_world,
                 event_time_world, perception_time_world, ingest_time_world,
                 payload_modality, payload)
            VALUES
                ($1, $2, $3, $3, $3, $3, $3, 'structured_event', $4)
            """,
            UUID("00000000-0000-4000-8000-000000000abc"),
            UUID("00000000-0000-5000-9000-000000000001"),  # substrate.self_state
            datetime(2027, 3, 15, 12, 0, tzinfo=timezone.utc),
            {"test": True},
        )
        row = await conn.fetchrow(
            """
            SELECT tableoid::regclass::text AS partition
              FROM substrate_slices
             WHERE slice_id = $1
            """,
            UUID("00000000-0000-4000-8000-000000000abc"),
        )
    assert row is not None
    assert row["partition"] == "substrate_slices_202703"


@pytest.mark.asyncio
async def test_write_outside_range_lands_in_default(thoth_db_initialized):
    """A row with an ``ingest_time_world`` in a far-past month (no carved
    partition) lands in ``substrate_slices_default`` rather than failing
    the INSERT — the default partition is the safety net.
    """
    import thoth_db

    # 1980 is well before any carved partition.
    async with thoth_db.connection() as conn:
        await conn.execute(
            """
            INSERT INTO substrate_slices
                (slice_id, stream_id, time_start_world, time_end_world,
                 event_time_world, perception_time_world, ingest_time_world,
                 payload_modality, payload)
            VALUES
                ($1, $2, $3, $3, $3, $3, $3, 'structured_event', $4)
            """,
            UUID("00000000-0000-4000-8000-000000000def"),
            UUID("00000000-0000-5000-9000-000000000001"),
            datetime(1980, 1, 1, tzinfo=timezone.utc),
            {"ancient": True},
        )
        row = await conn.fetchrow(
            """
            SELECT tableoid::regclass::text AS partition
              FROM substrate_slices
             WHERE slice_id = $1
            """,
            UUID("00000000-0000-4000-8000-000000000def"),
        )
    assert row is not None
    assert row["partition"] == "substrate_slices_default"


@pytest.mark.asyncio
async def test_partition_indexes_inherited_from_parent(thoth_db_initialized):
    """PG 17 propagates parent-table indexes to all partitions. Verify by
    checking that ``substrate_slices_default`` has indexes named after the
    parent's index pattern (PG mangles them — e.g.
    ``substrate_slices_default_stream_id_time_start_world_time_end_w_idx``).
    """
    import thoth_db

    async with thoth_db.connection() as conn:
        rows = await conn.fetch(
            """
            SELECT indexname
              FROM pg_indexes
             WHERE tablename = 'substrate_slices_default'
            """
        )
    names = {r["indexname"] for r in rows}
    # PG truncates / mangles inherited names, so we check by substring.
    # Stream/time-range index should be present.
    assert any("stream" in n and "time" in n for n in names), names
    # Pending partial index should be present.
    assert any("pending" in n for n in names), names


@pytest.mark.asyncio
async def test_find_underindexed_partitions_detects_invalid_parent_index(
    thoth_db_initialized,
):
    """Reproduces #284: an INVALID parent partitioned index (created ON ONLY,
    with no child indexes attached) leaves every child partition one index
    short. The detector flags them; a healthy schema reports clean."""
    from datetime import date as _date

    import thoth_db

    today = _date(2026, 8, 15)
    async with thoth_db.connection() as conn:
        await ensure_partitions(conn, ahead_months=1, today=today)
        # Healthy: every partition inherits the full parent index set.
        assert await find_underindexed_partitions(conn) == []

        # ON ONLY = a partitioned parent index with no children attached —
        # exactly the invalid state behind #284's zero-index partition.
        await conn.execute(
            "CREATE INDEX slices_underidx_probe "
            "ON ONLY substrate_slices (trust_score)"
        )
        flagged = await find_underindexed_partitions(conn)

    assert flagged, "expected under-indexed partitions after an ON ONLY parent index"
    assert all(p["index_count"] == p["expected"] - 1 for p in flagged)
