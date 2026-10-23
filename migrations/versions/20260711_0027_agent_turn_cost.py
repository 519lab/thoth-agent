"""main-agent per-turn cost/latency sink (always-on visibility)

Append-only table for the MAIN agent's own cost/latency telemetry — one row
per completed ``run_conversation`` turn (canonical token deltas, estimated
cost, wall-clock duration, tagged by session/platform/model). It is the
main-loop counterpart of ``substrate_agent_cost`` (0024): that table answers
"what did the substrate spend running itself", this one answers "what did
talking to the user cost, and how long did turns take".

Deliberately NOT a ``substrate_*`` table: the writer is the conversation
loop, not a substrate sub-agent, and the awareness loop never reads it — no
``substrate_slices`` row, no consolidation backlog, no recall. Consumers are
operator surfaces only: ``thoth cost`` and the gateway ``/metrics`` endpoint.

``cost_usd`` is DOUBLE PRECISION, nullable: NULL means pricing was
unavailable for the route (``cost_status`` says why); 0.0 means genuinely
free/included. Costs here are estimates from ``agent/usage_pricing.py``,
never billing truth.

Revision ID: 20260711_0027
Revises: 20260701_0026
Create Date: 2026-07-11
"""
from __future__ import annotations

from alembic import op


revision = "20260711_0027"
down_revision = "20260701_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE agent_turn_cost (
            id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            agent              TEXT        NOT NULL DEFAULT 'main',
            session_id         TEXT,
            platform           TEXT        NOT NULL DEFAULT '',
            model              TEXT        NOT NULL DEFAULT '',
            provider           TEXT        NOT NULL DEFAULT '',
            input_tokens       BIGINT      NOT NULL DEFAULT 0,
            output_tokens      BIGINT      NOT NULL DEFAULT 0,
            cache_read_tokens  BIGINT      NOT NULL DEFAULT 0,
            cache_write_tokens BIGINT      NOT NULL DEFAULT 0,
            reasoning_tokens   BIGINT      NOT NULL DEFAULT 0,
            total_tokens       BIGINT      NOT NULL DEFAULT 0,
            api_calls          INTEGER     NOT NULL DEFAULT 0,
            cost_usd           DOUBLE PRECISION,
            cost_status        TEXT        NOT NULL DEFAULT 'unknown',
            duration_ms        INTEGER     NOT NULL DEFAULT 0,
            at                 TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Windowed rollups (thoth cost, /metrics) scan by time.
    op.execute("CREATE INDEX idx_agent_turn_cost_at ON agent_turn_cost (at)")
    # Per-session drill-down ("what did this conversation cost").
    op.execute(
        "CREATE INDEX idx_agent_turn_cost_session_at "
        "ON agent_turn_cost (session_id, at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_turn_cost")
