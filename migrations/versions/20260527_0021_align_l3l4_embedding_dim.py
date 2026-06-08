"""align l3/l4 embedding dim to substrate_slices

Migration 0020 created ``l3_patterns``/``l4_observations``.embedding at
``vector(THOTH_EMBEDDING_DIM)`` (default 1536), mirroring how 0009 created
``substrate_slices.embedding``. But if ``THOTH_EMBEDDING_DIM`` isn't set in
the env at apply time while the install actually embeds at a different dim
(e.g. nomic-embed's 768, which ``substrate_slices`` uses), the new columns
mismatch the embedding model. The Curator's L3/L4 embed-backfill then fails
the pgvector dimension check on every row — silently, since the curation pass
degrades on error — so semantic merge never runs and the upper layers stop
shrinking. (Observed in prod 2026-05-28: l3/l4 at vector(1536), slices at
vector(768), 0/9173 L3 embedded.)

This removes the footgun: it aligns the l3/l4 embedding dimension to
``substrate_slices.embedding`` — the source of truth, since the Curator embeds
every layer with the same model — instead of relying on the env being set.
Idempotent: a no-op when already aligned. Empty/mismatched embeddings are
cleared (incompatible across dims anyway) and the Curator re-backfills.

Revision ID: 20260527_0021
Revises: 20260527_0020
Create Date: 2026-05-28
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "20260527_0021"
down_revision = "20260527_0020"
branch_labels = None
depends_on = None


# Table names are internal constants (never user input); inline them rather
# than bind — ``:t::regclass`` confuses SQLAlchemy/asyncpg ('::' cast vs bind).
_VECTOR_DIM_TABLES = ("substrate_slices", "l3_patterns", "l4_observations")


def _vector_dim(conn, table: str) -> int | None:
    """Current dimension of ``<table>.embedding``, or None if absent/not a vector."""
    assert table in _VECTOR_DIM_TABLES, f"unexpected table {table!r}"
    row = conn.execute(
        text(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            f"WHERE attrelid = '{table}'::regclass AND attname = 'embedding' "
            "AND NOT attisdropped"
        )
    ).fetchone()
    if row is None:
        return None
    coltype = row[0] or ""
    if not coltype.startswith("vector("):
        return None
    try:
        return int(coltype[len("vector("):-1])
    except (ValueError, IndexError):
        return None


def _reshape(table: str, target: int) -> None:
    # Mirror migration 0009: drop the dim-bound index, null incompatible
    # embeddings, alter the column, recreate the index. Curator re-backfills.
    op.execute(f"DROP INDEX IF EXISTS {table}_embedding_cosine_idx")
    op.execute(f"UPDATE {table} SET embedding = NULL")
    op.execute(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector({target})")
    op.execute(
        f"CREATE INDEX {table}_embedding_cosine_idx "
        f"ON {table} USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )


def upgrade() -> None:
    bind = op.get_bind()
    target = _vector_dim(bind, "substrate_slices")
    if target is None:
        # No slices embedding column to align to (shouldn't happen post-0006);
        # leave l3/l4 as 0020 created them.
        return
    for table in ("l3_patterns", "l4_observations"):
        if _vector_dim(bind, table) != target:
            _reshape(table, target)
        # Ensure the cosine index exists even when no reshape was needed — a
        # partial manual fix (ALTER without recreating the index) can leave it
        # missing; this self-heals that.
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {table}_embedding_cosine_idx "
            f"ON {table} USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
        )


def downgrade() -> None:
    # No-op: 0020's downgrade drops the embedding columns entirely; there's
    # nothing dim-specific to revert here.
    pass
