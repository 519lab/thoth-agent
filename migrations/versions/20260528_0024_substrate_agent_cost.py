"""substrate per-call cost/usage sink (non-perceptual)

Append-only table for the substrate's own LLM cost/usage telemetry — one
row per ``chat.completions.create`` made by a sub-agent (token counts +
wall-clock latency, tagged by agent + model). This is operational
observability for "what did the substrate spend running itself", not
perception.

Like ``substrate_telemetry``, this table is the non-perceptual sink: the
substrate writes to it but the awareness loop NEVER reads from it. There is
no ``substrate_slices`` row, so a usage write can never increment the
consolidation backlog, enter the Curator's pending set, be counted by the
Conductor's load forecast, or be read back as recall — the same L0-feedback
boundary that motivated ``substrate_telemetry`` (2026-05-26→27 prod
incident: 414k ghost ``substrate.self_state`` slices). The schema-level
guard is ``substrate.storage.streams.is_perceptual``; this table sits on the
excluded side of it, alongside the per-agent ``substrate_*_log`` tables.

Revision ID: 20260528_0024
Revises: 20260528_0023
Create Date: 2026-06-02
"""
from __future__ import annotations

from alembic import op


revision = "20260528_0024"
down_revision = "20260528_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # gen_random_uuid() is built-in from PG 13+ (matches the sibling
    # substrate tables' id convention).
    op.execute(
        """
        CREATE TABLE substrate_agent_cost (
            id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            agent             TEXT        NOT NULL,
            model             TEXT        NOT NULL DEFAULT '',
            prompt_tokens     INTEGER     NOT NULL DEFAULT 0,
            completion_tokens INTEGER     NOT NULL DEFAULT 0,
            total_tokens      INTEGER     NOT NULL DEFAULT 0,
            latency_ms        INTEGER     NOT NULL DEFAULT 0,
            at                TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Recent-first scans (operator "tail"-style inspect / cost rollups).
    op.execute(
        "CREATE INDEX idx_substrate_agent_cost_at "
        "ON substrate_agent_cost (at)"
    )
    # Per-agent time-ordered rollups (cost attribution by sub-agent).
    op.execute(
        "CREATE INDEX idx_substrate_agent_cost_agent_at "
        "ON substrate_agent_cost (agent, at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS substrate_agent_cost")
