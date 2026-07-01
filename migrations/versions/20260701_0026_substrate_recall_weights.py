"""substrate_recall_weights — versioned, promotable recall weight sets

The offline tuner (``substrate/recall/tuner.py``) fits recall ranking
weights against the labelled recall log (migration 0025's ``outcome_score``)
and this table is where a fitted vector becomes operational state: one row
per candidate weight set with the evidence that produced it (corpus size,
train/holdout separation metrics, the baseline it beat).

At most one row is ``active`` — enforced by a partial unique index — and the
live recall path (``substrate/recall/api.py``) reads it through a short-TTL
cache, falling back to the config/env baseline when no row is active or the
read fails. History is append-only: promotion demotes, never deletes, so the
table doubles as the tuning audit trail.

Reversible: ``downgrade`` drops the table.

Revision ID: 20260701_0026
Revises: 20260624_0025
Create Date: 2026-07-01
"""
from __future__ import annotations

from alembic import op


revision = "20260701_0026"
down_revision = "20260624_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE substrate_recall_weights (
            id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
            weights                 JSONB       NOT NULL,
            source                  TEXT        NOT NULL DEFAULT 'cli',
            corpus_size             INTEGER     NOT NULL DEFAULT 0,
            train_metric            REAL,
            holdout_metric          REAL,
            baseline_holdout_metric REAL,
            active                  BOOLEAN     NOT NULL DEFAULT FALSE
        )
        """
    )
    # At most one active weight set, ever.
    op.execute(
        "CREATE UNIQUE INDEX idx_substrate_recall_weights_active "
        "ON substrate_recall_weights (active) WHERE active"
    )
    # Newest-first history listing.
    op.execute(
        "CREATE INDEX idx_substrate_recall_weights_created "
        "ON substrate_recall_weights (created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE substrate_recall_weights")
