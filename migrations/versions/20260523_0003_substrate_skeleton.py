"""substrate skeleton — Phase A storage layer

Adds three substrate_* tables to the Thoth PG database:

* substrate_decay_profiles  — bundle of decay/consolidation/tombstone
                              settings; 4 default profiles seeded.
* substrate_streams         — registered perception streams; the
                              ``substrate.self_state`` bootstrap row is
                              seeded so internal emissions have a target
                              from the first boot.
* substrate_slices          — the high-volume slice table, RANGE-
                              partitioned monthly on ``ingest_time_world``
                              with a DEFAULT catch-all partition + 2
                              months of bootstrap partitions. Indexes are
                              created on the parent table so PG 17
                              propagates them to every present and future
                              partition.

Per the Phase A spec (§3) and the Phase 0 ADR:
  - JSONB columns use the asyncpg pool's per-connection codec — *never*
    use ``::jsonb`` casts (corrupts the prepared-statement type cache).
  - INTERVAL columns map to ``datetime.timedelta`` in Python.
  - All timestamps are TIMESTAMPTZ (TZ-aware in asyncpg).
  - Composite PK ``(slice_id, ingest_time_world)`` because PG requires
    the partition key to be part of every primary key constraint.

Revision ID: 20260523_0003
Revises: 20260522_0002
Create Date: 2026-05-23
"""
from __future__ import annotations

from datetime import date

from alembic import op


revision = "20260523_0003"
down_revision = "20260522_0002"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Stable v5 UUIDs for seeded rows. These are NOT randomly generated — the
# substrate references them by UUID literal at boot, so they must be
# identical on every machine. Format:
#
#   00000000-0000-5000-Xxxx-NNNNNNNNNNNN
#                  ^         ^
#                  |         per-row identifier (sequential)
#                  version 5 (name-based, even though we hand-pick rather
#                            than hash, the version nibble keeps the UUID
#                            valid)
#
#   X = 8 — variant-1 / RFC 4122 cluster, for decay profiles
#   X = 9 — variant-1 / RFC 4122 cluster, for streams (kept separate so
#           a substrate operator can tell "0-prefixed UUID -> seeded by
#           Alembic, not generated at runtime" at a glance)
# ---------------------------------------------------------------------------

PROFILE_DEFAULT_TEXT = "00000000-0000-5000-8000-000000000001"
PROFILE_DEFAULT_STRUCTURED = "00000000-0000-5000-8000-000000000002"
PROFILE_DEFAULT_BINARY = "00000000-0000-5000-8000-000000000003"
PROFILE_DEFAULT_SIGNAL = "00000000-0000-5000-8000-000000000004"

STREAM_SUBSTRATE_SELF_STATE = "00000000-0000-5000-9000-000000000001"


def upgrade() -> None:
    # ---- 1. substrate_decay_profiles --------------------------------------
    #
    # Created first because substrate_streams.decay_profile_id is FK to it.
    # tombstone_policy CHECK plus the cross-column CHECK forces every
    # 'none'-policy profile to record a justification (auditability).
    op.execute(
        """
        CREATE TABLE substrate_decay_profiles (
            profile_id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            name                         TEXT        NOT NULL UNIQUE,
            natural_half_life            INTERVAL    NOT NULL,
            consolidation_window         INTERVAL    NOT NULL,
            reinforcement_bump           REAL        NOT NULL CHECK (reinforcement_bump BETWEEN 0 AND 1),
            min_salience_to_retain       REAL        NOT NULL CHECK (min_salience_to_retain BETWEEN 0 AND 1),
            release_after_consolidation  BOOLEAN     NOT NULL DEFAULT TRUE,
            summary_decay_multiplier     REAL        NOT NULL DEFAULT 2.0 CHECK (summary_decay_multiplier > 0),
            pending_ttl                  INTERVAL    NOT NULL DEFAULT INTERVAL '30 seconds',
            tombstone_policy             TEXT        NOT NULL DEFAULT 'thin'
                CHECK (tombstone_policy IN ('full','thin','none')),
            tombstone_none_justification TEXT,
            applies_to_modality          TEXT,
            CHECK (
                tombstone_policy <> 'none'
                OR (tombstone_none_justification IS NOT NULL AND length(tombstone_none_justification) > 0)
            )
        )
        """
    )

    # Seed 4 default profiles, one per modality. IDs are deterministic so
    # auto-registered streams in substrate.facade.Substrate.boot() can
    # reference them by literal without a name → ID lookup.
    op.execute(
        f"""
        INSERT INTO substrate_decay_profiles
            (profile_id, name, natural_half_life, consolidation_window,
             reinforcement_bump, min_salience_to_retain, applies_to_modality,
             pending_ttl, tombstone_policy)
        VALUES
            ('{PROFILE_DEFAULT_TEXT}', 'default-text',
                INTERVAL '1 hour',     INTERVAL '10 minutes', 0.20, 0.05, 'text',
                INTERVAL '30 seconds', 'thin'),
            ('{PROFILE_DEFAULT_STRUCTURED}', 'default-structured',
                INTERVAL '30 minutes', INTERVAL '5 minutes',  0.20, 0.05, 'structured_event',
                INTERVAL '15 seconds', 'thin'),
            ('{PROFILE_DEFAULT_BINARY}', 'default-binary',
                INTERVAL '12 hours',   INTERVAL '1 hour',     0.30, 0.10, 'binary_blob',
                INTERVAL '2 minutes',  'thin'),
            ('{PROFILE_DEFAULT_SIGNAL}', 'default-signal',
                INTERVAL '5 minutes',  INTERVAL '1 minute',   0.10, 0.02, 'signal',
                INTERVAL '5 seconds',  'thin')
        ON CONFLICT (profile_id) DO NOTHING
        """
    )

    # ---- 2. substrate_streams ---------------------------------------------
    op.execute(
        """
        CREATE TABLE substrate_streams (
            stream_id        UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            name             TEXT         NOT NULL UNIQUE,
            family           TEXT         NOT NULL
                CHECK (family IN ('exteroceptive','self_action','self_state')),
            modality         TEXT         NOT NULL
                CHECK (modality IN ('text','structured_event','binary_blob','signal')),
            source           TEXT         NOT NULL,
            organ            TEXT         NOT NULL,
            lifecycle_state  TEXT         NOT NULL
                CHECK (lifecycle_state IN ('registered','active','paused','retired')),
            registered_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
            retired_at       TIMESTAMPTZ,
            decay_profile_id UUID         NOT NULL REFERENCES substrate_decay_profiles(profile_id),
            metadata         JSONB        NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )

    # Family lookup — small set of values, but family-aware scans (e.g.
    # "all self_state streams in this profile") are common in the inspect
    # CLI and in Phase B+ Curator routing.
    op.execute(
        "CREATE INDEX substrate_streams_family_idx ON substrate_streams (family)"
    )
    # Lifecycle partial index — the hot read pattern is "find every active
    # stream right now"; partial keeps the index tiny by excluding paused
    # and retired streams.
    op.execute(
        """
        CREATE INDEX substrate_streams_lifecycle_idx
            ON substrate_streams (lifecycle_state)
            WHERE lifecycle_state IN ('registered','active')
        """
    )

    # Seed the substrate self-state stream. Stable UUID, name namespace
    # 'substrate.*' (separate from 'hermes.*' user-facing streams).
    op.execute(
        f"""
        INSERT INTO substrate_streams
            (stream_id, name, family, modality, source, organ, lifecycle_state, decay_profile_id)
        VALUES
            ('{STREAM_SUBSTRATE_SELF_STATE}',
             'substrate.self_state',
             'self_state', 'structured_event',
             'substrate-daemon', 'multiple', 'active',
             '{PROFILE_DEFAULT_STRUCTURED}')
        ON CONFLICT (stream_id) DO NOTHING
        """
    )

    # ---- 3. substrate_slices — partitioned -------------------------------
    #
    # Partition strategy: RANGE on ingest_time_world (PG-side
    # ``now()`` at insert). Monthly partitions because the access pattern
    # is "recent slices for this stream" — month-aligned reads sweep one
    # partition. The DEFAULT partition catches anything outside the
    # explicitly created months so writes never fail on a missing
    # partition; the partition-maintenance worker (Phase A Task 11) keeps
    # the default empty by carving out month partitions ahead of time.
    #
    # Composite PK (slice_id, ingest_time_world) — PG requires the
    # partition key to be part of every PRIMARY KEY. slice_id alone is
    # globally unique (it's a v4 UUID); the ingest_time_world part is
    # mechanical, included so partition pruning works on UPDATE and
    # DELETE paths.
    op.execute(
        """
        CREATE TABLE substrate_slices (
            slice_id                  UUID         NOT NULL DEFAULT gen_random_uuid(),
            stream_id                 UUID         NOT NULL REFERENCES substrate_streams(stream_id),

            time_start_world          TIMESTAMPTZ  NOT NULL,
            time_end_world            TIMESTAMPTZ  NOT NULL,
            time_start_experiential   TIMESTAMPTZ,
            time_end_experiential     TIMESTAMPTZ,
            event_time_world          TIMESTAMPTZ  NOT NULL,
            perception_time_world     TIMESTAMPTZ  NOT NULL,
            ingest_time_world         TIMESTAMPTZ  NOT NULL DEFAULT now(),

            payload                   JSONB,
            payload_blob_ref          TEXT,
            payload_modality          TEXT         NOT NULL
                CHECK (payload_modality IN ('text','structured_event','binary_blob','signal')),

            sentinel_state            TEXT         NOT NULL DEFAULT 'pending'
                CHECK (sentinel_state IN ('pending','passed','quarantined')),
            sentinel_reason           TEXT,
            pending_committed_at      TIMESTAMPTZ,

            trust_score               REAL,

            salience_score            REAL         NOT NULL DEFAULT 1.0
                CHECK (salience_score BETWEEN 0 AND 1),
            salience_updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),

            summary_of                JSONB,

            consolidation_state       TEXT         NOT NULL DEFAULT 'unconsolidated'
                CHECK (consolidation_state IN ('unconsolidated','partial','consolidated','released')),
            consolidated_to           JSONB,

            metadata                  JSONB        NOT NULL DEFAULT '{}'::jsonb,

            PRIMARY KEY (slice_id, ingest_time_world),

            CHECK (event_time_world      <= perception_time_world),
            CHECK (perception_time_world <= ingest_time_world)
        )
        PARTITION BY RANGE (ingest_time_world)
        """
    )

    # Default partition — safety net. Writes outside the carved-out month
    # partitions land here rather than failing. The maintenance worker
    # keeps this empty in steady state by ensuring 3 months of partitions
    # exist ahead of every ``now()``.
    op.execute(
        """
        CREATE TABLE substrate_slices_default
            PARTITION OF substrate_slices DEFAULT
        """
    )

    # Bootstrap partitions: the current month + the next month at
    # migration time. The maintenance worker is responsible for keeping
    # the rolling window ahead from here on.
    today = date.today()
    for partition_name, lo, hi in _month_ranges(today, ahead_months=1):
        op.execute(
            f"""
            CREATE TABLE {partition_name}
                PARTITION OF substrate_slices
                FOR VALUES FROM ('{lo.isoformat()}') TO ('{hi.isoformat()}')
            """
        )

    # ---- Indexes on the parent table -------------------------------------
    #
    # PG 17 propagates indexes from the parent to every present and
    # future child partition automatically. Defining them on the parent
    # means the maintenance worker doesn't need to remember to re-create
    # indexes when it carves out a new month.

    # Per-stream time-range queries — dominant Phase B+ read pattern.
    # DESC matches "recent slices first" requests.
    op.execute(
        """
        CREATE INDEX substrate_slices_stream_time_idx
            ON substrate_slices (stream_id, time_start_world DESC, time_end_world DESC)
        """
    )

    # Pending-queue scan (Sentinel batch tick + force-reject). Partial
    # index because pending slices are short-lived (sub-minute steady
    # state) and small in count — keeps the index tiny.
    op.execute(
        """
        CREATE INDEX substrate_slices_pending_idx
            ON substrate_slices (pending_committed_at)
            WHERE sentinel_state = 'pending'
        """
    )

    # Forward-compat: consolidation worker scans (Phase B+ Curator).
    op.execute(
        """
        CREATE INDEX substrate_slices_consolidation_idx
            ON substrate_slices (stream_id, ingest_time_world)
            WHERE consolidation_state IN ('unconsolidated','partial')
        """
    )

    # JSONB metadata lookup via @> containment — used by the inspect CLI
    # and likely by Phase B+ Reflector queries. ``jsonb_path_ops`` is
    # smaller and faster than the default ``jsonb_ops`` for the
    # containment-only pattern we'll use here.
    op.execute(
        """
        CREATE INDEX substrate_slices_metadata_gin_idx
            ON substrate_slices USING GIN (metadata jsonb_path_ops)
        """
    )


def downgrade() -> None:
    # Drop in reverse dependency order. Slices first (FK to streams),
    # streams second (FK to decay_profiles), profiles last. The DEFAULT
    # and month partitions are dropped implicitly when their parent is
    # dropped.
    op.execute("DROP TABLE IF EXISTS substrate_slices CASCADE")
    op.execute("DROP TABLE IF EXISTS substrate_streams CASCADE")
    op.execute("DROP TABLE IF EXISTS substrate_decay_profiles CASCADE")


# ---------------------------------------------------------------------------
# Internal helpers — partition-name + range math used by upgrade().
# The same helper logic also lives in substrate/storage/partitions.py for
# the runtime maintenance worker; the two implementations are deliberately
# kept in sync (DRY would require importing substrate code into the
# migration, which Alembic loads via its own path — too brittle).
# ---------------------------------------------------------------------------


def _month_ranges(reference: date, ahead_months: int) -> list[tuple[str, date, date]]:
    """Return ``(partition_name, lo_inclusive, hi_exclusive)`` tuples
    covering ``reference``'s month and the next ``ahead_months`` months.

    Names follow the convention ``substrate_slices_YYYYMM`` so partition
    pruning by name (for ``DROP TABLE substrate_slices_YYYYMM`` in a
    future retention policy) is unambiguous.
    """
    ranges: list[tuple[str, date, date]] = []
    year, month = reference.year, reference.month
    for _ in range(ahead_months + 1):
        lo = date(year, month, 1)
        if month == 12:
            hi = date(year + 1, 1, 1)
            year, month = year + 1, 1
        else:
            hi = date(year, month + 1, 1)
            month += 1
        ranges.append((f"substrate_slices_{lo.year:04d}{lo.month:02d}", lo, hi))
    return ranges
