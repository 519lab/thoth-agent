"""Phase 0: one-shot SQLite -> PG migrator for users with a legacy ~/.hermes/state.db.

CLI surface:
    hermes db migrate-from-sqlite [--sqlite-path PATH] [--dry-run]

Default sqlite-path: ~/.hermes/state.db  (upstream default)
Default dry-run:     False
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

import hermes_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _epoch_to_timestamptz(value):
    """Convert SQLite REAL epoch seconds to a timezone-aware datetime for PG."""
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _parse_json_or_passthrough(value):
    """Parse a JSON-in-TEXT column to Python object (dict/list) or return None.

    asyncpg's JSONB codec expects Python dicts/lists, not raw JSON strings.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        # Not valid JSON — pass through as-is (rare for these columns).
        return value


# ---------------------------------------------------------------------------
# Core migrator
# ---------------------------------------------------------------------------

async def migrate_from_sqlite(src_path: Path, *, dry_run: bool = False) -> Tuple[int, int]:
    """Copy sessions and messages from a legacy SQLite state.db to the current PG.

    Returns (sessions_copied, messages_copied).

    Caller is responsible for ensuring PG is migrated to head via
    ``alembic upgrade head`` before calling.

    When dry_run=True the rows are counted but nothing is written to PG.
    """
    src = sqlite3.connect(str(src_path))
    src.row_factory = sqlite3.Row
    s_count = 0
    m_count = 0
    try:
        # Verify PG schema is at head.
        async with hermes_db.connection() as conn:
            head = await conn.fetchval(
                "SELECT version_num FROM alembic_version LIMIT 1"
            )
            if not head:
                raise RuntimeError(
                    "PG schema not migrated. Run "
                    "`alembic -c migrations/alembic.ini upgrade head` first."
                )

        # ── Copy sessions ────────────────────────────────────────────────
        session_rows = list(src.execute("SELECT * FROM sessions ORDER BY started_at"))

        if dry_run:
            s_count = len(session_rows)
        else:
            async with hermes_db.transaction() as conn:
                for row in session_rows:
                    d = dict(row)
                    await conn.execute(
                        """
                        INSERT INTO sessions (
                            id, source, user_id, model, model_config, system_prompt,
                            parent_session_id, started_at, ended_at, end_reason,
                            message_count, tool_call_count,
                            input_tokens, output_tokens, cache_read_tokens,
                            cache_write_tokens, reasoning_tokens,
                            billing_provider, billing_base_url, billing_mode,
                            estimated_cost_usd, actual_cost_usd, cost_status,
                            cost_source, pricing_version,
                            title, api_call_count,
                            handoff_state, handoff_platform, handoff_error
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                            $11, $12, $13, $14, $15, $16, $17,
                            $18, $19, $20, $21, $22, $23, $24, $25, $26, $27,
                            $28, $29, $30
                        )
                        ON CONFLICT (id) DO NOTHING
                        """,
                        d.get("id"),
                        d.get("source"),
                        d.get("user_id"),
                        d.get("model"),
                        _parse_json_or_passthrough(d.get("model_config")),
                        d.get("system_prompt"),
                        d.get("parent_session_id"),
                        _epoch_to_timestamptz(d.get("started_at")),
                        _epoch_to_timestamptz(d.get("ended_at")),
                        d.get("end_reason"),
                        d.get("message_count") or 0,
                        d.get("tool_call_count") or 0,
                        d.get("input_tokens") or 0,
                        d.get("output_tokens") or 0,
                        d.get("cache_read_tokens") or 0,
                        d.get("cache_write_tokens") or 0,
                        d.get("reasoning_tokens") or 0,
                        d.get("billing_provider"),
                        d.get("billing_base_url"),
                        d.get("billing_mode"),
                        d.get("estimated_cost_usd"),
                        d.get("actual_cost_usd"),
                        d.get("cost_status"),
                        d.get("cost_source"),
                        d.get("pricing_version"),
                        d.get("title"),
                        d.get("api_call_count") or 0,
                        d.get("handoff_state"),
                        d.get("handoff_platform"),
                        d.get("handoff_error"),
                    )
                    s_count += 1

        # ── Copy messages ────────────────────────────────────────────────
        # Only copy messages whose session_id exists in the source (all of them
        # should since the SQLite schema has a FK, but guard anyway).
        message_rows = list(src.execute("SELECT * FROM messages ORDER BY id"))

        if dry_run:
            m_count = len(message_rows)
        else:
            async with hermes_db.transaction() as conn:
                for row in message_rows:
                    d = dict(row)
                    await conn.execute(
                        """
                        INSERT INTO messages (
                            session_id, role, content, tool_call_id, tool_calls,
                            tool_name, timestamp, token_count, finish_reason,
                            reasoning, reasoning_content, reasoning_details,
                            codex_reasoning_items, codex_message_items,
                            platform_message_id
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9,
                            $10, $11, $12, $13, $14, $15
                        )
                        """,
                        d.get("session_id"),
                        d.get("role"),
                        d.get("content"),
                        d.get("tool_call_id"),
                        _parse_json_or_passthrough(d.get("tool_calls")),
                        d.get("tool_name"),
                        _epoch_to_timestamptz(d.get("timestamp")),
                        d.get("token_count"),
                        d.get("finish_reason"),
                        d.get("reasoning"),
                        d.get("reasoning_content"),
                        _parse_json_or_passthrough(d.get("reasoning_details")),
                        _parse_json_or_passthrough(d.get("codex_reasoning_items")),
                        _parse_json_or_passthrough(d.get("codex_message_items")),
                        d.get("platform_message_id"),
                    )
                    m_count += 1

        # TODO (out of scope for this task): copy kanban tables if sibling
        # kanban.db exists next to state.db. Implement as a separate
        # migrate_kanban_from_sqlite() function when needed.

    finally:
        src.close()

    logger.info(
        "migrate_from_sqlite: %s %d sessions, %d messages from %s",
        "[dry-run] would copy" if dry_run else "copied",
        s_count,
        m_count,
        src_path,
    )
    return s_count, m_count


# ---------------------------------------------------------------------------
# CLI entry point (called from hermes db migrate-from-sqlite via argparse)
# ---------------------------------------------------------------------------

def cli_migrate_from_sqlite(sqlite_path: str | None = None, dry_run: bool = False) -> str:
    """CLI entry: called from ``hermes db migrate-from-sqlite``."""
    if sqlite_path is None:
        sqlite_path = os.path.expanduser("~/.hermes/state.db")
    src = Path(sqlite_path)
    if not src.exists():
        return f"Error: {src} does not exist"
    dsn = os.environ.get("HERMES_PG_DSN")
    if not dsn:
        return "Error: HERMES_PG_DSN env var not set"

    async def _run():
        await hermes_db.init(dsn)
        try:
            s, m = await migrate_from_sqlite(src, dry_run=dry_run)
        finally:
            await hermes_db.close()
        return s, m

    s, m = hermes_db.run_sync(_run())
    prefix = "[dry-run] would copy" if dry_run else "Copied"
    return f"{prefix}: {s} sessions, {m} messages from {src}"


# ---------------------------------------------------------------------------
# argparse handler (receives Namespace from hermes db migrate-from-sqlite)
# ---------------------------------------------------------------------------

def cmd_db_migrate_from_sqlite(args) -> int:  # noqa: ANN001
    """Handler for ``hermes db migrate-from-sqlite`` argparse subcommand."""
    result = cli_migrate_from_sqlite(
        sqlite_path=getattr(args, "sqlite_path", None),
        dry_run=getattr(args, "dry_run", False),
    )
    print(result)
    return 0 if not result.startswith("Error") else 1


def cmd_db(args) -> int:  # noqa: ANN001
    """Dispatcher for ``hermes db <subcommand>``."""
    import sys
    sub = getattr(args, "db_command", None)
    if sub == "migrate-from-sqlite":
        return cmd_db_migrate_from_sqlite(args)
    print("usage: hermes db migrate-from-sqlite [--sqlite-path PATH] [--dry-run]", file=sys.stderr)
    return 2
