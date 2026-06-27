"""recall outcome label — innovation #1 (recall-replay eval harness)

Adds a nullable ``outcome_score REAL`` to ``substrate_recall_log``. The
column is the *label* the offline replay harness
(``substrate/recall/replay.py``) trains/measures against: did the turn that
consumed this recall actually succeed?

It is written post-turn by ``agent/turn_outcome.py::write_recall_outcome``
via a ``(session_id, requested_at)`` windowed UPDATE — the recall log writer
is fire-and-forget and returns no ``log_id``, so the outcome can't carry a
row handle and we correlate by the turn-start timestamp instead.

NULL is the default and the resting state: historical rows and any recall
whose turn never resolved (crash, kill-switch off, no bound substrate) stay
NULL and are *excluded* from replay. The existing
``substrate_recall_log_session_time_idx`` on ``(session_id,
requested_at DESC)`` already covers both the windowed UPDATE and the replay
scans, so no new index is needed.

Reversible: ``downgrade`` drops the column.

Revision ID: 20260624_0025
Revises: 20260528_0024
Create Date: 2026-06-24
"""
from __future__ import annotations

from alembic import op


revision = "20260624_0025"
down_revision = "20260528_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE substrate_recall_log ADD COLUMN outcome_score REAL"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE substrate_recall_log DROP COLUMN IF EXISTS outcome_score"
    )
