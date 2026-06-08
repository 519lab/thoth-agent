"""substrate slices embedding column — configurable dimension

Phase C migration 0006 pinned ``substrate_slices.embedding`` to
``vector(1536)`` to match OpenAI's ``text-embedding-3-small``. That
hard-codes operators into the OpenAI ecosystem (and OpenAI-compatible
proxies that happen to expose the same model). For local-only or
non-OpenAI providers — Ollama's ``nomic-embed-text`` (768-d),
``mxbai-embed-large`` (1024-d), Voyage ``voyage-3`` (1024-d), Google
Gemini ``gemini-embedding-001`` (768/1536/3072), etc. — that pin
prevents using the substrate at all.

This migration reads ``THOTH_EMBEDDING_DIM`` from the environment
(default 1536 — back-compat with every existing install) and reshapes
the column + ivfflat index to that dimension when they don't match.

Existing embeddings at the OLD dim are NOT convertible to the new dim
— different model spaces, different magnitudes. The migration sets the
column to NULL across the table when a dim change is needed; the
Curator's embedding-backfill loop (Phase C §5.7) picks up NULL-embedding
slices on its next tick and re-embeds at the new dim.

Operators changing dim AFTER first install need to:

  1. Stop hermes (so the Curator isn't mid-write):
        hermes gateway stop  # or just exit the CLI
  2. Set the new env var:
        export THOTH_EMBEDDING_DIM=768
  3. Re-run migrations (alembic detects this rev applied but
     ``upgrade_in_place`` reads the env each call, so a no-op upgrade
     followed by a manual re-apply via ``alembic stamp`` + repeat is
     needed — OR run the ALTER block below by hand against PG.
     A ``hermes substrate reembed-dim N`` CLI command is the proper
     long-term answer; not in this PR's scope).
  4. Restart hermes; Curator backfills new embeddings on the next tick
     (~30s of activity).

Cost note: clearing embeddings forces re-embed of every slice. With a
local model that's free. With a cloud model it's the per-token cost of
re-running embed() over every slice's payload text.

Revision ID: 20260526_0009
Revises: 20260526_0008  (Phase D parser audit — landed earlier today,
                         see PR llm-cognitive-thought#3 for the spec; the
                         hermes-agent side of Phase D will land separately)
Create Date: 2026-05-26

NOTE on down_revision: if Phase D's 0008 hasn't merged when this
migration ships, swap to ``down_revision = "20260525_0006"`` (the last
landed Phase C migration). Detected at apply-time via Alembic's normal
chain validation.
"""
from __future__ import annotations

import os

from alembic import op
from sqlalchemy import text


revision = "20260526_0009"
# Phase D's 20260526_0008 (substrate_parser_log) is drafted in the spec
# repo but not yet implemented in this repo. Until it lands here, this
# migration chains directly off Phase C's last migration (0006).
down_revision = "20260525_0006"
branch_labels = None
depends_on = None


# Default keeps existing 1536-d installs as no-ops. Operators wanting a
# different dim set THOTH_EMBEDDING_DIM in env BEFORE running alembic
# upgrade (or the bundled install.sh, which inherits the env).
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
        # pgvector itself caps at 16000. Anything below 1 is nonsense.
        raise ValueError(
            f"THOTH_EMBEDDING_DIM out of range (1..16000), got {dim}"
        )
    return dim


def _current_dim(conn) -> int | None:
    """Read the current vector column dimension from pg_catalog. Returns
    None if the column doesn't exist or isn't a vector type (shouldn't
    happen — Phase C 0006 created it — but defensive)."""
    row = conn.execute(
        text(
            """
            SELECT format_type(atttypid, atttypmod) AS coltype
              FROM pg_attribute
             WHERE attrelid = 'substrate_slices'::regclass
               AND attname  = 'embedding'
               AND NOT attisdropped
            """
        )
    ).fetchone()
    if row is None:
        return None
    # format_type returns strings like 'vector(1536)'. Parse the int.
    coltype = row[0] or ""
    if not coltype.startswith("vector("):
        return None
    try:
        return int(coltype[len("vector("):-1])
    except (ValueError, IndexError):
        return None


def upgrade() -> None:
    target = _target_dim()
    bind = op.get_bind()
    current = _current_dim(bind)

    if current == target:
        # Either: env var matches the existing schema (no work), OR
        # this is a fresh install where 0006 already created vector(1536)
        # and THOTH_EMBEDDING_DIM is unset (default 1536). No-op.
        return

    if current is None:
        # Defensive: 0006 should have created the column. If it's
        # missing, recreate at the configured dim instead of failing.
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute(
            f"ALTER TABLE substrate_slices ADD COLUMN embedding vector({target})"
        )
        op.execute(
            "CREATE INDEX substrate_slices_embedding_cosine_idx "
            "ON substrate_slices USING ivfflat (embedding vector_cosine_ops) "
            "WITH (lists = 100)"
        )
        return

    # Dim change. Drop the index (it's dim-bound), null the existing
    # embeddings (incompatible with the new dim), alter the column, and
    # recreate the index. Curator backfill re-populates over time.
    op.execute("DROP INDEX IF EXISTS substrate_slices_embedding_cosine_idx")
    op.execute("UPDATE substrate_slices SET embedding = NULL")
    op.execute(
        f"ALTER TABLE substrate_slices ALTER COLUMN embedding TYPE vector({target})"
    )
    op.execute(
        "CREATE INDEX substrate_slices_embedding_cosine_idx "
        "ON substrate_slices USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    )


def downgrade() -> None:
    """Downgrade restores vector(1536) and clears embeddings. Same
    re-embed cycle as upgrade if the install was running at a different
    dim."""
    bind = op.get_bind()
    current = _current_dim(bind)
    if current is None or current == _DEFAULT_DIM:
        return

    op.execute("DROP INDEX IF EXISTS substrate_slices_embedding_cosine_idx")
    op.execute("UPDATE substrate_slices SET embedding = NULL")
    op.execute(
        f"ALTER TABLE substrate_slices ALTER COLUMN embedding TYPE vector({_DEFAULT_DIM})"
    )
    op.execute(
        "CREATE INDEX substrate_slices_embedding_cosine_idx "
        "ON substrate_slices USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    )
