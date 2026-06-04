"""Storage-layer tests for Phase A: types + enums + Alembic seed verification.

Repository tests (round-trip via StreamRepo/DecayProfileRepo/SliceRepo) land
in Task 6 once those modules exist. Phase A Task 4 (the Alembic revision) is
verified here at the seeded-row level — the migration ran via the
``thoth_db_dsn`` fixture before this test sees the database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from substrate.storage.types import (
    Address,
    ConsolidationState,
    DecayProfile,
    Family,
    Lifecycle,
    Modality,
    SentinelState,
    Slice,
    Stream,
    TombstonePolicy,
)


# ---------------------------------------------------------------------------
# Dataclasses + enums — pure-Python, no DB needed.
# ---------------------------------------------------------------------------


class TestEnums:
    def test_family_string_values_match_check_constraint(self):
        assert {e.value for e in Family} == {
            "exteroceptive",
            "self_action",
            "self_state",
        }

    def test_modality_string_values_match_check_constraint(self):
        assert {e.value for e in Modality} == {
            "text",
            "structured_event",
            "binary_blob",
            "signal",
        }

    def test_lifecycle_string_values_match_check_constraint(self):
        assert {e.value for e in Lifecycle} == {
            "registered",
            "active",
            "paused",
            "retired",
        }

    def test_sentinel_state_string_values_match_check_constraint(self):
        assert {e.value for e in SentinelState} == {
            "pending",
            "passed",
            "quarantined",
        }

    def test_consolidation_state_string_values_match_check_constraint(self):
        assert {e.value for e in ConsolidationState} == {
            "unconsolidated",
            "partial",
            "consolidated",
            "released",
        }

    def test_tombstone_policy_string_values_match_check_constraint(self):
        assert {e.value for e in TombstonePolicy} == {"full", "thin", "none"}


class TestDataclasses:
    def test_address_is_hashable_and_frozen(self):
        stream_id = UUID("00000000-0000-4000-8000-000000000001")
        t0 = datetime(2026, 5, 23, tzinfo=timezone.utc)
        t1 = t0 + timedelta(seconds=1)
        a = Address(stream_id=stream_id, time_start_world=t0, time_end_world=t1)
        # frozen=True means dataclass forbids mutation.
        with pytest.raises(Exception):
            a.stream_id = UUID("00000000-0000-4000-8000-000000000002")  # type: ignore[misc]
        # Hashable so addresses can be set/dict keys.
        assert hash(a) == hash(a)

    def test_slice_address_returns_matching_address(self):
        stream_id = UUID("00000000-0000-4000-8000-000000000001")
        slice_id = UUID("00000000-0000-4000-9000-000000000001")
        t_start = datetime(2026, 5, 23, 10, 0, tzinfo=timezone.utc)
        t_end = datetime(2026, 5, 23, 10, 0, 1, tzinfo=timezone.utc)
        t_event = t_start
        t_perception = t_start
        t_ingest = t_start
        s = Slice(
            slice_id=slice_id,
            stream_id=stream_id,
            time_start_world=t_start,
            time_end_world=t_end,
            event_time_world=t_event,
            perception_time_world=t_perception,
            ingest_time_world=t_ingest,
            payload_modality=Modality.TEXT,
            sentinel_state=SentinelState.PENDING,
        )
        a = s.address()
        assert a.stream_id == stream_id
        assert a.time_start_world == t_start
        assert a.time_end_world == t_end

    def test_decay_profile_holds_timedeltas(self):
        # asyncpg's INTERVAL codec returns timedeltas; the dataclass
        # mirrors that shape.
        dp = DecayProfile(
            profile_id=UUID("00000000-0000-5000-8000-000000000001"),
            name="default-text",
            natural_half_life=timedelta(hours=1),
            consolidation_window=timedelta(minutes=10),
            reinforcement_bump=0.2,
            min_salience_to_retain=0.05,
        )
        assert isinstance(dp.natural_half_life, timedelta)
        assert dp.pending_ttl == timedelta(seconds=30)  # default
        assert dp.tombstone_policy is TombstonePolicy.THIN

    def test_stream_metadata_defaults_to_empty_dict(self):
        st = Stream(
            stream_id=UUID("00000000-0000-5000-9000-000000000001"),
            name="substrate.self_state",
            family=Family.SELF_STATE,
            modality=Modality.STRUCTURED_EVENT,
            source="substrate-daemon",
            organ="multiple",
            lifecycle_state=Lifecycle.ACTIVE,
            decay_profile_id=UUID("00000000-0000-5000-8000-000000000002"),
            registered_at=datetime.now(timezone.utc),
        )
        assert st.metadata == {}
        # Mutable default — verify it's a fresh dict per instance (not a
        # shared class-level dict, a classic dataclass footgun).
        st.metadata["x"] = 1
        st2 = Stream(
            stream_id=UUID("00000000-0000-5000-9000-000000000002"),
            name="other",
            family=Family.SELF_STATE,
            modality=Modality.STRUCTURED_EVENT,
            source="x",
            organ="y",
            lifecycle_state=Lifecycle.ACTIVE,
            decay_profile_id=UUID("00000000-0000-5000-8000-000000000002"),
            registered_at=datetime.now(timezone.utc),
        )
        assert st2.metadata == {}


# ---------------------------------------------------------------------------
# Alembic-seeded rows — verifies that the 20260523_0003 revision ran and
# inserted the four default decay profiles + the substrate.self_state stream.
# Uses the Phase 0 ``thoth_db_dsn`` fixture which already ran Alembic head.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decay_profiles_seeded(thoth_db_initialized):
    """The migration seeded the 4 default-per-modality profiles with
    stable v5 UUIDs and INTERVAL values that round-trip to timedelta.
    """
    import thoth_db

    async with thoth_db.connection() as conn:
        rows = await conn.fetch(
            """
            SELECT profile_id::text AS pid, name,
                   natural_half_life, consolidation_window,
                   pending_ttl, tombstone_policy, applies_to_modality
              FROM substrate_decay_profiles
             ORDER BY name
            """
        )
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {
        "default-binary",
        "default-signal",
        "default-structured",
        "default-text",
    }
    # Stable v5-style UUIDs: deterministic, NOT randomly generated.
    assert by_name["default-text"]["pid"] == "00000000-0000-5000-8000-000000000001"
    assert by_name["default-structured"]["pid"] == "00000000-0000-5000-8000-000000000002"
    assert by_name["default-binary"]["pid"] == "00000000-0000-5000-8000-000000000003"
    assert by_name["default-signal"]["pid"] == "00000000-0000-5000-8000-000000000004"
    # INTERVAL → timedelta — the central round-trip assertion.
    assert by_name["default-text"]["natural_half_life"] == timedelta(hours=1)
    assert by_name["default-structured"]["consolidation_window"] == timedelta(minutes=5)
    assert by_name["default-signal"]["pending_ttl"] == timedelta(seconds=5)
    # Modality-applied + tombstone defaults.
    assert by_name["default-text"]["applies_to_modality"] == "text"
    assert by_name["default-text"]["tombstone_policy"] == "thin"


@pytest.mark.asyncio
async def test_self_state_stream_seeded(thoth_db_initialized):
    """The migration seeded the ``substrate.self_state`` bootstrap stream
    so internal emissions (Sentinel batch audits, force-reject audits)
    have a target from the first boot.
    """
    import thoth_db

    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT stream_id::text AS sid, name, family, modality, source,
                   organ, lifecycle_state, decay_profile_id::text AS dpid,
                   metadata
              FROM substrate_streams
             WHERE name = 'substrate.self_state'
            """
        )
    assert row is not None
    assert row["sid"] == "00000000-0000-5000-9000-000000000001"
    assert row["family"] == "self_state"
    assert row["modality"] == "structured_event"
    assert row["lifecycle_state"] == "active"
    assert row["dpid"] == "00000000-0000-5000-8000-000000000002"  # default-structured
    # JSONB defaults — the codec returns dict (not a JSON string).
    assert row["metadata"] == {}


@pytest.mark.asyncio
async def test_substrate_slices_is_partitioned(thoth_db_initialized):
    """``substrate_slices`` is a partitioned table (PG ``relkind = 'p'``)
    with at least the default partition + 2 month partitions present
    after migration.
    """
    import thoth_db

    async with thoth_db.connection() as conn:
        relkind = await conn.fetchval(
            "SELECT relkind::text FROM pg_class WHERE relname = 'substrate_slices'"
        )
        # 'p' = partitioned table; 'r' = ordinary table. The latter would
        # mean the PARTITION BY RANGE clause was dropped or never applied.
        # ``::text`` cast because PG's ``"char"`` (single-byte) type comes
        # back from asyncpg as ``bytes``, which is awkward to compare; the
        # cast normalises to a Python ``str``.
        assert relkind == "p"

        partitions = await conn.fetch(
            """
            SELECT child.relname AS name
              FROM pg_inherits  i
              JOIN pg_class     parent ON parent.oid = i.inhparent
              JOIN pg_class     child  ON child.oid  = i.inhrelid
             WHERE parent.relname = 'substrate_slices'
             ORDER BY child.relname
            """
        )
    names = [r["name"] for r in partitions]
    assert "substrate_slices_default" in names
    # At least the current month is created by the migration. (The
    # exact YYYYMM partition name depends on the migration's run date.)
    assert any(n.startswith("substrate_slices_2") and n != "substrate_slices_default"
               for n in names), f"no month partitions found in {names}"
