"""substrate parser audit log (Phase D)

``substrate_parser_log`` — append-only audit of every Parser invocation,
parallel to Phase C's ``substrate_recall_log``. Operators read it via
``thoth substrate parser`` to see Parser cost, latency, and outcome
distribution. One row per (session, tick) LLM call.

Per the Phase D spec (2026-05-25-phase-d-l1-parser.md) §3.5. Revision
renumbered from the spec's ``0008`` to fit the actual chain
(``…0011 → 0012``).

Revision ID: 20260527_0012
Revises: 20260527_0011
Create Date: 2026-05-27
"""
from __future__ import annotations

from alembic import op


revision = "20260527_0012"
down_revision = "20260527_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE substrate_parser_log (
            id                     BIGSERIAL PRIMARY KEY,
            t_call                 TIMESTAMPTZ NOT NULL DEFAULT now(),
            session_id             TEXT,
            batch_size             INTEGER NOT NULL,
            entities_emitted       INTEGER NOT NULL DEFAULT 0,
            relationships_emitted  INTEGER NOT NULL DEFAULT 0,
            citations_emitted      INTEGER NOT NULL DEFAULT 0,
            slices_consolidated    INTEGER NOT NULL DEFAULT 0,
            latency_ms             INTEGER NOT NULL,
            prompt_tokens          INTEGER,
            completion_tokens      INTEGER,
            cost_usd               DOUBLE PRECISION,
            model                  TEXT NOT NULL DEFAULT '',
            outcome                TEXT NOT NULL
                CHECK (outcome IN ('ok','timeout','parse_error','llm_error','empty')),
            error                  TEXT,
            metadata               JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_parser_log_recent ON substrate_parser_log (t_call DESC)"
    )
    op.execute(
        "CREATE INDEX idx_parser_log_session "
        "ON substrate_parser_log (session_id, t_call DESC)"
    )
    op.execute(
        "CREATE INDEX idx_parser_log_outcome "
        "ON substrate_parser_log (outcome, t_call DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS substrate_parser_log")
