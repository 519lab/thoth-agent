"""substrate agent heartbeat — sub-agent liveness telemetry

Adds ``substrate_agent_heartbeat``: one row per substrate sub-agent
(``sentinel``, ``curator``, ``force-reject``, ``partition-maintenance``,
…), upserted on a fixed ~10s cadence by the sub-agent run loop. This is
the cross-process liveness surface the ``thoth substrate`` inspect CLI
reads to tell a *live* worker subprocess from a *dead* one.

Why a dedicated table rather than an L0 ``substrate.self_state`` slice:
a heartbeat is operational telemetry, not a *perception* the mind should
re-contact, reinforce, or consolidate. Folding it into L0 would flood the
perception store (Sentinel ticks at 0.2s) and pollute the Curator's
salience/pressure queries. A single-row last-writer-wins UPSERT keyed on
``agent_name`` is cheap, self-heals on worker restart (the new pid simply
overwrites the row), and is exactly the "sub-agent operational state"
read surface Phase F's real Conductor will consume (design §5.6, §6.3).
Substantive sub-agent *decisions* still go to ``substrate.self_state`` as
before; only the bare liveness beat lives here.

The ``last_beat_at`` default of ``now()`` (and the UPSERT's use of PG's
clock rather than the host clock) keeps staleness math skew-free: the
inspect CLI classifies live/stale/down purely from ``now() - last_beat_at``
evaluated server-side.

Revision ID: 20260527_0010
Revises: 20260526_0009
Create Date: 2026-05-27
"""
from __future__ import annotations

from alembic import op


revision = "20260527_0010"
down_revision = "20260526_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE substrate_agent_heartbeat (
            agent_name    TEXT         PRIMARY KEY,
            pid           INTEGER      NOT NULL,
            host          TEXT         NOT NULL,
            level         TEXT         NOT NULL,
            is_sentinel   BOOLEAN      NOT NULL DEFAULT FALSE,
            tick_count    BIGINT       NOT NULL DEFAULT 0,
            started_at    TIMESTAMPTZ  NOT NULL,
            last_beat_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS substrate_agent_heartbeat")
