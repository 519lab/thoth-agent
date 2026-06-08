"""substrate L3/L4 curation — embeddings for semantic dedup + L4 salience/decay

The Curator now curates the *upper* layers too (not just L0 slices): it
semantically merges near-duplicate L3 patterns / L4 observations and
decays→releases stale ones. Before this, both tables were unbounded
append-only with exact-text-only dedup (`upsert_pattern`) or none at all
(`record_observation`), so LLM rewordings accumulated without limit —
~7.8k L3 / ~3.7k L4 from a few hundred entities.

This migration adds the columns that curation needs:

* ``l3_patterns.embedding`` / ``l4_observations.embedding`` —
  ``vector(THOTH_EMBEDDING_DIM)``, for cosine near-duplicate detection.
  Start NULL; the Curator's backfill populates them (mirrors the
  ``substrate_slices`` backfill, Phase C §5.7). Dimension follows
  ``THOTH_EMBEDDING_DIM`` exactly like ``substrate_slices`` (migration 0009).
* ``l4_observations.salience_score`` / ``last_seen_at`` — L4 had neither;
  decay needs a salience to fade and a last-seen to bump on re-find
  (L3 already has both).
* ``salience_updated_at`` on BOTH tables — the anchor for incremental
  exponential decay (``salience *= 0.5^(dt/half_life)``), mirroring
  ``substrate_slices.salience_updated_at``. Distinct from ``last_seen_at``
  so decay measures "time since salience last recomputed", not "since
  last re-found".

Revision ID: 20260527_0020
Revises: 20260527_0019
Create Date: 2026-05-28
"""
from __future__ import annotations

import os

from alembic import op


revision = "20260527_0020"
down_revision = "20260527_0019"
branch_labels = None
depends_on = None


# Mirror substrate_slices (migration 0009): default keeps 1536-d installs
# as-is; operators on a different model set THOTH_EMBEDDING_DIM before
# running alembic upgrade.
_DEFAULT_DIM = 1536


def _target_dim() -> int:
    raw = (os.environ.get("THOTH_EMBEDDING_DIM") or "").strip()
    if not raw:
        return _DEFAULT_DIM
    try:
        dim = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"THOTH_EMBEDDING_DIM must be an integer, got {raw!r}"
        ) from exc
    if dim < 1 or dim > 16000:
        raise ValueError(f"THOTH_EMBEDDING_DIM out of range (1..16000), got {dim}")
    return dim


def upgrade() -> None:
    dim = _target_dim()
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # L4 gains salience + last_seen (L3 already has both). Existing rows
    # default to salience 0.5 / last_seen now(), so they start decaying
    # from the migration forward rather than being released immediately.
    op.execute(
        "ALTER TABLE l4_observations "
        "ADD COLUMN salience_score DOUBLE PRECISION NOT NULL DEFAULT 0.5"
    )
    op.execute(
        "ALTER TABLE l4_observations "
        "ADD COLUMN last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()"
    )
    # Decay anchor on both tables (L0's substrate_slices has this; L3/L4 did not).
    op.execute(
        "ALTER TABLE l3_patterns "
        "ADD COLUMN salience_updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
    )
    op.execute(
        "ALTER TABLE l4_observations "
        "ADD COLUMN salience_updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
    )

    # Embeddings — nullable; the Curator backfills NULL rows on its tick.
    op.execute(f"ALTER TABLE l3_patterns ADD COLUMN embedding vector({dim})")
    op.execute(f"ALTER TABLE l4_observations ADD COLUMN embedding vector({dim})")
    op.execute(
        "CREATE INDEX l3_patterns_embedding_cosine_idx "
        "ON l3_patterns USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    )
    op.execute(
        "CREATE INDEX l4_observations_embedding_cosine_idx "
        "ON l4_observations USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    )
    op.execute(
        "CREATE INDEX idx_l4_salience "
        "ON l4_observations (salience_score DESC, last_seen_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_l4_salience")
    op.execute("DROP INDEX IF EXISTS l4_observations_embedding_cosine_idx")
    op.execute("DROP INDEX IF EXISTS l3_patterns_embedding_cosine_idx")
    op.execute("ALTER TABLE l4_observations DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE l3_patterns DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE l3_patterns DROP COLUMN IF EXISTS salience_updated_at")
    op.execute("ALTER TABLE l4_observations DROP COLUMN IF EXISTS salience_updated_at")
    op.execute("ALTER TABLE l4_observations DROP COLUMN IF EXISTS last_seen_at")
    op.execute("ALTER TABLE l4_observations DROP COLUMN IF EXISTS salience_score")
