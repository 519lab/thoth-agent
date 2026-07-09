#!/usr/bin/env python3
"""
PostgreSQL State Store for Thoth Agent (Phase 0+).

Provides persistent session storage backed by PostgreSQL 17 + pgvector.
Stores session metadata, full message history, and model configuration for
CLI and gateway sessions.

Key design decisions:
- asyncpg connection pool managed by thoth_db
- Per-call connection acquisition (no per-instance state)
- Compression-triggered session splitting via parent_session_id chains
- Batch runner and RL trajectories are NOT stored here (separate systems)
- Session source tagging ('cli', 'telegram', 'discord', etc.) for filtering
"""

import json
import logging
import re
import time
from pathlib import Path

import thoth_db  # PG pool & helpers; safe to import even when pool not initialized.

from agent.memory_manager import sanitize_context
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def format_session_db_unavailable(prefix: str = "Session database not available") -> str:
    """Return a user-facing message when the session database is unavailable.

    Kept as a backward-compatible stub: several call sites in cli.py,
    gateway/run.py, and agent/ import this function. With PostgreSQL as
    the only backend, init failures surface as exceptions rather than a
    stored error string; this function returns a generic message.
    """
    return f"{prefix}."


# Module-level once-per-process flag for the "substrate not bound" warn
# in _emit_substrate_message_hook. Without this, a hot append_message
# loop spams the log line for every message. False at import time;
# flipped to True after first warn fires.
_SUBSTRATE_UNBOUND_WARNED = False


# ---------------------------------------------------------------------------
# Substrate perception hook bridge — Phase A §7 wiring.
#
# Lives at the SessionDB.append_message chokepoint because it's the single
# function every Thoth call path goes through to persist a turn. Looking
# up source/model from the session row keeps the per-call-site changes to
# zero — the conversation loop, gateway intake, ACP server, etc. don't
# need to learn the substrate's hook API.
# ---------------------------------------------------------------------------


async def _emit_substrate_message_hook(
    conn,
    session_id: str,
    role: str,
    content: Optional[str],
    tool_calls: Any,
    tool_name: Optional[str],
) -> None:
    """Best-effort substrate emission for an append_message call.

    Never raises — hook errors are logged and dropped per Phase A spec
    §6.2 (hooks must never bubble to a Thoth caller). Shares ``conn``
    so the slice INSERT joins the message INSERT's transaction.
    """
    try:
        from datetime import datetime, timezone

        from substrate.events import thoth_hooks
    except Exception as _imp_exc:  # pragma: no cover — substrate import failure
        # Was a silent return. Surface it so operators can spot a broken
        # install (missing substrate package, partial site-packages, etc.)
        # instead of staring at empty user_message streams.
        logger.warning(
            "substrate.hooks import failed in _emit_substrate_message_hook: %s",
            _imp_exc,
        )
        return

    # No-op when substrate isn't booted (CLI subcommands that touch
    # SessionDB without going through bootstrap_substrate).
    if thoth_hooks._substrate is None:
        # Was also silent. Single log per process via a module-level
        # flag so we don't spam every append_message call.
        global _SUBSTRATE_UNBOUND_WARNED
        if not _SUBSTRATE_UNBOUND_WARNED:
            _SUBSTRATE_UNBOUND_WARNED = True
            logger.warning(
                "substrate hooks NOT firing — thoth_hooks._substrate is None "
                "(substrate boot probably skipped or failed; user/tool/assistant "
                "stream coverage will be 0). First append_message at "
                "session=%s role=%s; further occurrences suppressed.",
                session_id, role,
            )
        return

    # Per-message trace at DEBUG. Only visible with ``thoth -v``.
    # The session-summary INFO line at on_session_end is the
    # steady-state signal for "is perception working?"; this DEBUG
    # line is for active debugging of a specific session's coverage.
    logger.debug(
        "substrate.hook.attempt role=%s session=%s content_len=%s "
        "tool_calls=%s tool_name=%s",
        role, session_id, len(content) if content else 0,
        bool(tool_calls), tool_name,
    )

    t_event = datetime.now(timezone.utc)

    # Source + model live on the session row; one extra fetch per
    # append. The substrate cares about the wire-level metadata but
    # doesn't need it in the inner messages-write path, so we fetch
    # lazily here rather than threading it through every caller.
    try:
        srow = await conn.fetchrow(
            "SELECT source, model FROM sessions WHERE id = $1",
            session_id,
        )
    except Exception:
        srow = None
    source = (srow["source"] if srow else None) or "unknown"
    model = (srow["model"] if srow else None) or ""

    try:
        if role == "user":
            if content:
                await thoth_hooks.on_user_message_async(
                    session_id, source, content, t_event
                )
        elif role == "assistant":
            # An assistant turn may carry both text content AND tool_calls.
            # Emit the response slice for the text portion (when present)
            # and a tool_call slice per scheduled call.
            if content:
                await thoth_hooks.on_assistant_response_async(
                    session_id, model, content, t_event
                )
            if tool_calls:
                calls = tool_calls if isinstance(tool_calls, list) else [tool_calls]
                for call in calls:
                    name, args = _extract_tool_call(call)
                    if name is None:
                        continue
                    await thoth_hooks.on_tool_call_async(
                        session_id, name, args, t_event
                    )
        elif role == "tool":
            # A tool message carries the result. We don't get a structured
            # error field here; the conversation loop passes failure text
            # in ``content`` for the model to consume — pass it through as
            # ``result`` and leave ``error=None``.
            await thoth_hooks.on_tool_result_async(
                session_id,
                tool_name or "unknown",
                content,
                None,
                t_event,
            )
    except Exception:  # noqa: BLE001 — defensive belt-and-suspenders
        logger.warning(
            "substrate message-hook emission raised; continuing",
            exc_info=True,
        )


def _extract_tool_call(call: Any) -> tuple[Optional[str], dict]:
    """Pull (tool_name, args_dict) out of a single tool_call entry.

    Tool-call shapes vary by provider — the upstream Thoth
    representation typically wraps OpenAI-style ``{"function": {"name":
    ..., "arguments": "..."}}``, sometimes pre-parsed to a dict.
    Returns ``(None, {})`` if the shape can't be recognised so the hook
    silently skips the emission rather than asserting on shape.
    """
    import json

    if isinstance(call, dict):
        fn = call.get("function") if isinstance(call.get("function"), dict) else None
        if fn is not None:
            name = fn.get("name")
            args = fn.get("arguments")
        else:
            name = call.get("name") or call.get("tool")
            args = call.get("arguments") or call.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {"_raw": args}
        if not isinstance(args, dict):
            args = {}
        return name, args
    return None, {}


class _AsyncSessionDB:
    """Phase 0 PG-backed replacement for the legacy SQLite `SessionDB`.

    Method signatures mirror `_LegacySessionDBSqlite` so call-site refactors in
    Task 22 are a search-and-replace. Each cluster lands in its own task
    (Tasks 8–18). Until ported, methods raise NotImplementedError so any
    accidental wiring is caught loudly.

    The class has no per-instance connection state; all methods are async and
    acquire a connection from `thoth_db.pool()` for the duration of the call.
    Callers in async contexts call methods directly; callers in sync contexts
    wrap with `thoth_db.run_sync(...)`.
    """

    def __init__(self, db_path: Optional[Path] = None, **_legacy_kwargs: Any) -> None:
        # db_path and other legacy SQLite kwargs are accepted for backward
        # compatibility with call sites that still pass the old signature
        # (e.g. acp_adapter/session.py and several test suites). They are
        # ignored — storage is the process-wide asyncpg pool, not a file path.
        if db_path is not None or _legacy_kwargs:
            logger.debug(
                "_AsyncSessionDB ignoring legacy SQLite kwargs (db_path=%r, others=%s)",
                db_path,
                sorted(_legacy_kwargs.keys()),
            )

    # === Session lifecycle (Task 8) ===

    async def create_session(self, session_id: str, source: str, **kwargs) -> str:
        """Insert a session row; silently ignores conflicts (idempotent).

        Emits a ``thoth.self_state.session_lifecycle`` perception slice
        in the same transaction (Phase A §7 — atomic with the session
        INSERT so the substrate's view of session-start can never disagree
        with the session row). Hook failure is logged + swallowed and
        does not bubble back to the caller.
        """
        from datetime import datetime, timezone

        model = kwargs.get("model")
        model_config = kwargs.get("model_config") or {}
        system_prompt = kwargs.get("system_prompt", "")
        user_id = kwargs.get("user_id")
        parent_session_id = kwargs.get("parent_session_id")
        title = kwargs.get("title")
        t_event = datetime.now(timezone.utc)
        async with thoth_db.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO sessions
                    (id, source, user_id, model, model_config, system_prompt,
                     parent_session_id, title)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (id) DO NOTHING
                """,
                session_id, source, user_id, model,
                model_config, system_prompt,
                parent_session_id, title,
            )
            # Substrate session-lifecycle emission, shared txn. The hook
            # is a no-op when substrate hasn't been booted; failures inside
            # the hook are logged but never re-raised to here.
            try:
                from substrate.events.thoth_hooks import on_session_start_async
                await on_session_start_async(
                    session_id, source, model or "",
                    t_event, conn=conn,
                )
            except Exception:  # noqa: BLE001 — defensive belt-and-suspenders
                logger.warning(
                    "substrate on_session_start hook raised; ignoring",
                    exc_info=True,
                )
        return session_id

    async def end_session(self, session_id: str, end_reason: str) -> None:
        """Mark session ended. No-op if already ended (first end_reason wins).

        Emits a ``thoth.self_state.session_lifecycle`` perception slice
        IFF the UPDATE actually flipped a row from ``ended_at IS NULL``
        to ended. This avoids emitting on every redundant end_session
        call (e.g. retry paths that double-end). Hook failures are
        logged + swallowed inside the helper.
        """
        from datetime import datetime, timezone

        async with thoth_db.transaction() as conn:
            tag = await conn.execute(
                "UPDATE sessions SET ended_at = now(), end_reason = $2 "
                "WHERE id = $1 AND ended_at IS NULL",
                session_id, end_reason,
            )
            # Only emit when we actually transitioned the session — asyncpg's
            # ``UPDATE 1`` / ``UPDATE 0`` tag tells us which.
            if tag.endswith(" 1"):
                try:
                    from substrate.events.thoth_hooks import on_session_end_async
                    await on_session_end_async(
                        session_id, end_reason, datetime.now(timezone.utc)
                    )
                except Exception:  # noqa: BLE001 — defensive
                    logger.warning(
                        "substrate on_session_end hook raised; ignoring",
                        exc_info=True,
                    )

    async def reopen_session(self, session_id: str) -> None:
        """Clear ended_at/end_reason so a session can be resumed."""
        async with thoth_db.connection() as conn:
            await conn.execute(
                "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = $1",
                session_id,
            )

    async def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        """Store the full assembled system prompt snapshot."""
        async with thoth_db.connection() as conn:
            await conn.execute(
                "UPDATE sessions SET system_prompt = $2 WHERE id = $1",
                session_id, system_prompt,
            )

    async def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: float = None,
        actual_cost_usd: float = None,
        cost_status: str = None,
        cost_source: str = None,
        pricing_version: str = None,
        billing_provider: str = None,
        billing_base_url: str = None,
        billing_mode: str = None,
        api_call_count: int = 0,
        absolute: bool = False,
    ) -> None:
        """Update token counters.

        When *absolute* is False (default), values are incremented (per-call
        deltas). When *absolute* is True, values are set directly (gateway path
        with cumulative totals). Mirrors upstream behavior column-for-column.
        """
        # asyncpg cannot infer the type of parameters used only in CASE/COALESCE
        # with NULL. Cast $7 and $8 (cost columns, DOUBLE PRECISION) explicitly.
        if absolute:
            sql = """
                UPDATE sessions SET
                    input_tokens        = $2,
                    output_tokens       = $3,
                    cache_read_tokens   = $4,
                    cache_write_tokens  = $5,
                    reasoning_tokens    = $6,
                    estimated_cost_usd  = COALESCE($7::double precision, 0),
                    actual_cost_usd     = CASE
                                             WHEN $8::double precision IS NULL
                                             THEN actual_cost_usd
                                             ELSE $8::double precision
                                         END,
                    cost_status         = COALESCE($9,  cost_status),
                    cost_source         = COALESCE($10, cost_source),
                    pricing_version     = COALESCE($11, pricing_version),
                    billing_provider    = COALESCE(billing_provider, $12),
                    billing_base_url    = COALESCE(billing_base_url, $13),
                    billing_mode        = COALESCE(billing_mode, $14),
                    model               = COALESCE(model, $15),
                    api_call_count      = $16
                WHERE id = $1
            """
        else:
            sql = """
                UPDATE sessions SET
                    input_tokens        = input_tokens        + $2,
                    output_tokens       = output_tokens       + $3,
                    cache_read_tokens   = cache_read_tokens   + $4,
                    cache_write_tokens  = cache_write_tokens  + $5,
                    reasoning_tokens    = reasoning_tokens    + $6,
                    estimated_cost_usd  = COALESCE(estimated_cost_usd, 0)
                                         + COALESCE($7::double precision, 0),
                    actual_cost_usd     = CASE
                                             WHEN $8::double precision IS NULL
                                             THEN actual_cost_usd
                                             ELSE COALESCE(actual_cost_usd, 0)
                                                  + $8::double precision
                                         END,
                    cost_status         = COALESCE($9,  cost_status),
                    cost_source         = COALESCE($10, cost_source),
                    pricing_version     = COALESCE($11, pricing_version),
                    billing_provider    = COALESCE(billing_provider, $12),
                    billing_base_url    = COALESCE(billing_base_url, $13),
                    billing_mode        = COALESCE(billing_mode, $14),
                    model               = COALESCE(model, $15),
                    api_call_count      = api_call_count      + $16
                WHERE id = $1
            """
        async with thoth_db.connection() as conn:
            await conn.execute(
                sql,
                session_id,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_write_tokens,
                reasoning_tokens,
                estimated_cost_usd,
                actual_cost_usd,
                cost_status,
                cost_source,
                pricing_version,
                billing_provider,
                billing_base_url,
                billing_mode,
                model,
                api_call_count,
            )

    async def ensure_session(self, *args, **kwargs):
        """Ensure a session row exists. INSERT OR IGNORE semantics.

        Mirrors upstream: if the session already exists, this is a no-op
        (existing row is not modified). Keyword-only when called with
        ``session_id=`` kwarg; positional ``session_id`` also accepted.
        """
        # Support both positional and keyword-style callers.
        if args:
            session_id = args[0]
            source = args[1] if len(args) > 1 else kwargs.pop("source", "unknown")
        else:
            session_id = kwargs.pop("session_id")
            source = kwargs.pop("source", "unknown")
        if (await self.get_session(session_id)) is not None:
            return
        await self.create_session(session_id=session_id, source=source, **kwargs)

    async def prune_empty_ghost_sessions(self, sessions_dir=None) -> int:
        """Remove empty TUI ghost sessions (no messages, no title, >24hr old).

        Matches upstream behavior (thoth_state.py:893-921). Only prunes
        sessions that are: source='tui', title IS NULL, ended,
        started >24 hours ago, and have zero messages. The optional
        sessions_dir FS-walk removes on-disk session files for pruned IDs.

        Returns the count of removed DB rows.
        """
        async with thoth_db.connection() as conn:
            rows = await conn.fetch(
                """
                WITH empty AS (
                    SELECT id FROM sessions s
                     WHERE s.source = 'tui'
                       AND s.title IS NULL
                       AND s.ended_at IS NOT NULL
                       AND s.started_at < now() - interval '24 hours'
                       AND NOT EXISTS (
                           SELECT 1 FROM messages m WHERE m.session_id = s.id
                       )
                )
                DELETE FROM sessions WHERE id IN (SELECT id FROM empty)
                RETURNING id
                """
            )
        removed_ids = [r["id"] for r in rows]
        if sessions_dir and removed_ids:
            self._remove_session_files(sessions_dir, removed_ids)
        return len(removed_ids)

    async def finalize_orphaned_compression_sessions(self) -> int:
        """Mark orphaned compression-continuation sessions as ended.

        Targets child sessions that were never finalized: parent is ended with
        reason='compression', child has messages but no end_reason/ended_at and
        api_call_count=0, and child is older than 7 days. Non-destructive —
        preserves all messages and sets end_reason='orphaned_compression'.

        Mirrors upstream logic in thoth_state.py:907-944.
        """
        async with thoth_db.connection() as conn:
            rows = await conn.fetch(
                """
                UPDATE sessions
                SET ended_at   = now(),
                    end_reason = 'orphaned_compression'
                WHERE api_call_count = 0
                  AND end_reason IS NULL
                  AND ended_at IS NULL
                  AND started_at < now() - interval '7 days'
                  AND parent_session_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM sessions p
                      WHERE p.id = sessions.parent_session_id
                        AND p.end_reason = 'compression'
                        AND p.ended_at IS NOT NULL
                  )
                  AND EXISTS (
                      SELECT 1 FROM messages m
                      WHERE m.session_id = sessions.id
                  )
                RETURNING id
                """
            )
        return len(rows)

    async def get_session(self, session_id: str):
        """Return a session row as a dict, or None if not found.

        JSONB columns (model_config, tool_calls, reasoning_details, etc.) are
        automatically decoded to Python objects by the pool-level JSONB codec
        registered in thoth_db._setup_jsonb_codec.
        """
        async with thoth_db.connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sessions WHERE id = $1", session_id
            )
        if row is None:
            return None
        return dict(row)

    async def resolve_session_id(self, session_id_or_prefix: str):
        """Resolve exact or uniquely-prefixed session ID.

        Returns exact ID when it exists. Otherwise treats input as a prefix and
        returns the single matching ID if unambiguous. Returns None for no
        matches or ambiguous prefixes. Mirrors upstream LIKE ESCAPE behavior;
        special LIKE chars in the prefix are escaped so they match literally.
        """
        async with thoth_db.connection() as conn:
            # Exact match first.
            row = await conn.fetchrow(
                "SELECT id FROM sessions WHERE id = $1", session_id_or_prefix
            )
            if row:
                return row["id"]
            # Prefix match — escape LIKE metacharacters so they match literally.
            escaped = (
                session_id_or_prefix
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            rows = await conn.fetch(
                "SELECT id FROM sessions WHERE id LIKE $1 || '%' ESCAPE '\\' "
                "ORDER BY started_at DESC LIMIT 2",
                escaped,
            )
        if len(rows) == 1:
            return rows[0]["id"]
        return None

    # === Titles (Task 9) ===

    # Must match SessionDB.MAX_TITLE_LENGTH (verified against upstream: 100)
    MAX_TITLE_LENGTH = 100

    @staticmethod
    def sanitize_title(title: Optional[str]) -> Optional[str]:
        """Validate and sanitize a session title.

        Mirrors upstream SessionDB.sanitize_title exactly:
        - Strips leading/trailing whitespace
        - Removes ASCII control characters (0x00-0x1F, 0x7F) except \\t, \\n, \\r
        - Removes problematic Unicode control characters
        - Collapses internal whitespace runs to single spaces
        - Normalizes empty/whitespace-only strings to None
        - Raises ValueError if cleaned title exceeds MAX_TITLE_LENGTH
        """
        if not title:
            return None

        # Remove ASCII control characters (keep \\t=0x09, \\n=0x0A, \\r=0x0D for whitespace norm)
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', title)

        # Remove problematic Unicode control characters
        cleaned = re.sub(
            r'[​-‏ -‮⁠-⁩﻿￼￹-￻]',
            '', cleaned,
        )

        # Collapse internal whitespace runs and strip
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()

        if not cleaned:
            return None

        if len(cleaned) > _AsyncSessionDB.MAX_TITLE_LENGTH:
            raise ValueError(
                f"Title too long ({len(cleaned)} chars, max {_AsyncSessionDB.MAX_TITLE_LENGTH})"
            )

        return cleaned

    async def set_session_title(self, session_id: str, title: str) -> bool:
        """Set, update, or clear a session's title.

        Returns True if the session row was actually touched.

        Raises ValueError if the title is already in use by another
        session or fails validation (too long, invalid characters).

        Empty/whitespace-only strings clear the existing title — the
        row's ``title`` column is set to NULL so callers like
        ``list_sessions_rich`` fall back to the message preview. This
        is what the dashboard /retitle "" flow and the test
        ``test_list_sessions_prefers_title_then_preview`` rely on; the
        pre-Phase-0 sqlite implementation silently no-op'd, which we
        treat as a bug rather than contract.
        """
        sanitized = self.sanitize_title(title)
        if sanitized is None:
            async with thoth_db.connection() as conn:
                result = await conn.execute(
                    "UPDATE sessions SET title = NULL WHERE id = $1",
                    session_id,
                )
            return result.split()[-1] == "1"

        async with thoth_db.connection() as conn:
            # Check uniqueness (allow the same session to keep its own title)
            conflict = await conn.fetchrow(
                "SELECT id FROM sessions WHERE title = $1 AND id != $2",
                sanitized, session_id,
            )
            if conflict:
                raise ValueError(
                    f"Title '{sanitized}' is already in use by session {conflict['id']}"
                )
            result = await conn.execute(
                "UPDATE sessions SET title = $1 WHERE id = $2",
                sanitized, session_id,
            )
        # asyncpg execute() returns e.g. 'UPDATE 1' or 'UPDATE 0'
        return result.split()[-1] == "1"

    async def get_session_title(self, session_id: str) -> Optional[str]:
        """Get the title for a session, or None."""
        async with thoth_db.connection() as conn:
            return await conn.fetchval(
                "SELECT title FROM sessions WHERE id = $1", session_id
            )

    async def get_session_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        """Look up a session by exact title. Returns session dict or None."""
        async with thoth_db.connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sessions WHERE title = $1", title
            )
        return dict(row) if row else None

    async def resolve_session_by_title(self, title: str) -> Optional[str]:
        """Resolve a title to a session ID, preferring the latest in a lineage.

        If the exact title exists, returns that session's ID.
        If numbered variants ("title #N") also exist, returns the most recent one.
        If exact exists AND numbered variants exist, returns the most recent numbered.
        """
        # Try exact match
        async with thoth_db.connection() as conn:
            exact_row = await conn.fetchrow(
                "SELECT id FROM sessions WHERE title = $1", title
            )

        # Search for numbered variants: "title #2", "title #3", etc.
        # Escape LIKE special chars in title to prevent false positives
        escaped = title.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with thoth_db.connection() as conn:
            numbered = await conn.fetch(
                "SELECT id, started_at FROM sessions "
                "WHERE title LIKE $1 || ' #%' ESCAPE '\\' "
                "ORDER BY started_at DESC",
                escaped,
            )

        if numbered:
            # Return the most recent numbered variant
            return numbered[0]["id"]
        elif exact_row:
            return exact_row["id"]
        return None

    async def get_next_title_in_lineage(self, base_title: str) -> str:
        """Generate the next title in a lineage (e.g. "my session" → "my session #2").

        Strips any existing " #N" suffix to find the base name, then finds
        the highest existing number and increments.
        """
        # Strip existing #N suffix to find the true base
        m = re.match(r'^(.*?) #(\d+)$', base_title)
        base = m.group(1) if m else base_title

        # Fetch all titles matching base or "base #N"
        # Escape LIKE special chars to prevent false positives
        escaped = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with thoth_db.connection() as conn:
            rows = await conn.fetch(
                "SELECT title FROM sessions WHERE title = $1 OR title LIKE $1 || ' #%' ESCAPE '\\'",
                escaped,
            )

        if not rows:
            return base  # No conflict — use the base name as-is

        # Find the highest existing number
        pat = re.compile(r'^.* #(\d+)$')
        max_num = 1  # The unnumbered original counts as #1
        for r in rows:
            mo = pat.match(r["title"])
            if mo:
                max_num = max(max_num, int(mo.group(1)))

        return f"{base} #{max_num + 1}"

    # === Compression (Task 10) ===
    async def get_compression_tip(self, session_id: str) -> str:
        """Walk the compression-continuation chain forward and return the tip.

        A compression continuation is a child session where:
        1. The parent's ``end_reason = 'compression'``
        2. The child was created AFTER the parent was ended (started_at >= ended_at)

        The second condition distinguishes compression continuations from
        delegate subagents or branch children, which can also have a
        ``parent_session_id`` but were created while the parent was still live.

        Returns the session_id of the latest continuation in the chain, or the
        input ``session_id`` if it isn't part of a compression chain (or if the
        input itself doesn't exist).
        """
        current = session_id
        # Bound the walk defensively — compression chains this deep are
        # pathological and shouldn't happen in practice. 100 = plenty.
        for _ in range(100):
            async with thoth_db.connection() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT id FROM sessions
                    WHERE parent_session_id = $1
                      AND started_at >= (
                          SELECT ended_at FROM sessions
                          WHERE id = $2 AND end_reason = 'compression'
                      )
                    ORDER BY started_at DESC
                    LIMIT 1
                    """,
                    current,
                    current,
                )
            if row is None:
                return current
            current = row["id"]
        return current

    # === Listings (Task 11) ===

    async def list_sessions_rich(
        self,
        source: str = None,
        exclude_sources: List[str] = None,
        limit: int = 20,
        offset: int = 0,
        include_children: bool = False,
        project_compression_tips: bool = True,
        order_by_last_active: bool = False,
    ) -> List[Dict[str, Any]]:
        """List sessions with preview (first user message) and last active timestamp.

        Returns dicts with keys: id, source, model, title, started_at, ended_at,
        message_count, preview (first 60 chars of first user message),
        last_active (timestamp of last message).

        Mirrors upstream SessionDB.list_sessions_rich behaviour exactly.
        Uses LATERAL for the per-session preview subquery and a recursive CTE
        when order_by_last_active=True.
        """
        where_clauses: List[str] = []
        params: list = []

        if not include_children:
            # Show root sessions and branch sessions (whose parent ended with
            # end_reason='branched' before the child was created), while hiding
            # sub-agent runs and compression continuations.
            where_clauses.append(
                "(s.parent_session_id IS NULL"
                " OR EXISTS (SELECT 1 FROM sessions p"
                "            WHERE p.id = s.parent_session_id"
                "            AND p.end_reason = 'branched'"
                "            AND s.started_at >= p.ended_at))"
            )

        if source:
            params.append(source)
            where_clauses.append(f"s.source = ${len(params)}")
        if exclude_sources:
            placeholders = ", ".join(f"${len(params) + j + 1}" for j in range(len(exclude_sources)))
            params.extend(exclude_sources)
            where_clauses.append(f"s.source NOT IN ({placeholders})")

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        # Preview subquery: first user message, newlines collapsed, truncated to 63 chars.
        # PG equivalent of SQLite's REPLACE(REPLACE(x, X'0A', ' '), X'0D', ' ').
        _preview_subq = """
            SELECT SUBSTR(REGEXP_REPLACE(m.content, E'[\\n\\r]', ' ', 'g'), 1, 63)
              FROM messages m
             WHERE m.session_id = s.id
               AND m.role = 'user'
               AND m.content IS NOT NULL
             ORDER BY m.timestamp, m.id
             LIMIT 1
        """
        _last_active_subq = """
            SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id
        """

        if order_by_last_active:
            # Recursive CTE walks compression-continuation chains forward to compute
            # effective_last_active at SQL level so LIMIT/OFFSET stay efficient.
            cte_seed_where = where_sql  # reuse same WHERE for CTE seed
            query = f"""
                WITH RECURSIVE chain(root_id, cur_id) AS (
                    SELECT s.id, s.id FROM sessions s {cte_seed_where}
                    UNION ALL
                    SELECT c.root_id, child.id
                    FROM chain c
                    JOIN sessions parent ON parent.id = c.cur_id
                    JOIN sessions child ON child.parent_session_id = c.cur_id
                    WHERE parent.end_reason = 'compression'
                      AND child.started_at >= parent.ended_at
                ),
                chain_max AS (
                    SELECT
                        root_id,
                        MAX(COALESCE(
                            (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = cur_id),
                            (SELECT started_at FROM sessions ss WHERE ss.id = cur_id)
                        )) AS effective_last_active
                    FROM chain
                    GROUP BY root_id
                )
                SELECT s.*,
                    COALESCE(
                        ({_preview_subq}),
                        ''
                    ) AS _preview_raw,
                    COALESCE(
                        ({_last_active_subq}),
                        s.started_at
                    ) AS last_active,
                    COALESCE(cm.effective_last_active, s.started_at) AS _effective_last_active
                FROM sessions s
                LEFT JOIN chain_max cm ON cm.root_id = s.id
                {where_sql}
                ORDER BY _effective_last_active DESC, s.started_at DESC, s.id DESC
                LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """
            # WHERE params apply twice in the SQL — once in the CTE seed,
            # once in the outer SELECT — but PG ``$N`` placeholders are
            # positional (one value serves every reference). Pass once.
            # (SQLite's ``?`` placeholders are positional-positional and
            # consume one arg per ``?``; this was the legacy shape.)
            all_params = params + [limit, offset]
        else:
            query = f"""
                SELECT s.*,
                    COALESCE(
                        ({_preview_subq}),
                        ''
                    ) AS _preview_raw,
                    COALESCE(
                        ({_last_active_subq}),
                        s.started_at
                    ) AS last_active
                FROM sessions s
                {where_sql}
                ORDER BY s.started_at DESC
                LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """
            all_params = params + [limit, offset]

        async with thoth_db.connection() as conn:
            rows = await conn.fetch(query, *all_params)

        sessions = []
        for row in rows:
            s = dict(row)
            raw = (s.pop("_preview_raw", "") or "").strip()
            if raw:
                text = raw[:60]
                s["preview"] = text + ("..." if len(raw) > 60 else "")
            else:
                s["preview"] = ""
            s.pop("_effective_last_active", None)
            sessions.append(s)

        # Project compression roots forward to their live continuation tip.
        if project_compression_tips and not include_children:
            projected = []
            for s in sessions:
                if s.get("end_reason") != "compression":
                    projected.append(s)
                    continue
                tip_id = await self.get_compression_tip(s["id"])
                if tip_id == s["id"]:
                    projected.append(s)
                    continue
                tip_row = await self._get_session_rich_row(tip_id)
                if not tip_row:
                    projected.append(s)
                    continue
                # Preserve the root's started_at for stable sort order, but
                # surface the tip's identity and activity data.
                merged = dict(s)
                for key in (
                    "id", "ended_at", "end_reason", "message_count",
                    "tool_call_count", "title", "last_active", "preview",
                    "model", "system_prompt",
                ):
                    if key in tip_row:
                        merged[key] = tip_row[key]
                merged["_lineage_root_id"] = s["id"]
                projected.append(merged)
            sessions = projected

        return sessions

    async def _get_session_rich_row(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single session with the same enriched columns as list_sessions_rich.

        Returns preview (first 60 chars of first user message) and last_active
        (timestamp of most recent message, falling back to started_at).
        Returns None if the session doesn't exist.
        """
        query = """
            SELECT s.*,
                COALESCE(
                    (SELECT SUBSTR(REGEXP_REPLACE(m.content, E'[\\n\\r]', ' ', 'g'), 1, 63)
                       FROM messages m
                      WHERE m.session_id = s.id
                        AND m.role = 'user'
                        AND m.content IS NOT NULL
                      ORDER BY m.timestamp, m.id
                      LIMIT 1),
                    ''
                ) AS _preview_raw,
                COALESCE(
                    (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                    s.started_at
                ) AS last_active
            FROM sessions s
            WHERE s.id = $1
        """
        async with thoth_db.connection() as conn:
            row = await conn.fetchrow(query, session_id)
        if not row:
            return None
        s = dict(row)
        raw = (s.pop("_preview_raw", "") or "").strip()
        if raw:
            text = raw[:60]
            s["preview"] = text + ("..." if len(raw) > 60 else "")
        else:
            s["preview"] = ""
        return s

    # === Message I/O (Task 12) ===

    async def append_message(
        self,
        session_id: str,
        role: str,
        content: str = None,
        tool_name: str = None,
        tool_calls: Any = None,
        tool_call_id: str = None,
        token_count: int = None,
        finish_reason: str = None,
        reasoning: str = None,
        reasoning_content: str = None,
        reasoning_details: Any = None,
        codex_reasoning_items: Any = None,
        codex_message_items: Any = None,
        platform_message_id: str = None,
    ) -> int:
        """Append a message to a session. Returns the message row ID.

        Also increments the session's message_count (and tool_call_count
        if role is 'tool' or tool_calls is present).
        """
        # Pre-compute tool call count (mirrors upstream logic)
        num_tool_calls = 0
        if tool_calls is not None:
            num_tool_calls = len(tool_calls) if isinstance(tool_calls, list) else 1

        async with thoth_db.transaction() as conn:
            msg_id = await conn.fetchval(
                """
                INSERT INTO messages (
                    session_id, role, content, tool_call_id, tool_calls,
                    tool_name, token_count, finish_reason, reasoning,
                    reasoning_content, reasoning_details, codex_reasoning_items,
                    codex_message_items, platform_message_id
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                RETURNING id
                """,
                session_id, role, content, tool_call_id, tool_calls,
                tool_name, token_count, finish_reason, reasoning,
                reasoning_content, reasoning_details, codex_reasoning_items,
                codex_message_items, platform_message_id,
            )
            if num_tool_calls > 0:
                await conn.execute(
                    """UPDATE sessions
                       SET message_count = message_count + 1,
                           tool_call_count = tool_call_count + $2
                       WHERE id = $1""",
                    session_id, num_tool_calls,
                )
            else:
                await conn.execute(
                    "UPDATE sessions SET message_count = message_count + 1 WHERE id = $1",
                    session_id,
                )
            # Substrate perception emission (Phase A §7 — wired at the
            # single message-persist chokepoint). Shares the txn so the
            # slice and the message row commit together. Failures are
            # logged + swallowed inside the helper so message persistence
            # is never blocked by a substrate problem.
            #
            # The defensive try/except is kept (was added during the
            # 2026-05-26 missing-streams triage) because the hook is
            # contract-promised never-raises but operators have been
            # bitten by silent swallows before; a stray uncaught
            # exception would now surface as
            # ``substrate.hook.raised_uncaught`` at ERROR level instead
            # of being lost. The diagnostic INFO brackets that lived
            # here briefly were demoted in chore/demote-diag... — the
            # session-summary log in ``on_session_end`` is the
            # steady-state signal.
            try:
                await _emit_substrate_message_hook(
                    conn, session_id, role, content, tool_calls, tool_name,
                )
            except Exception:
                logger.exception(
                    "substrate.hook.raised_uncaught role=%s session=%s — "
                    "the hook is documented as never-raises; this is a bug",
                    role, session_id,
                )
        return msg_id

    async def replace_messages(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """Atomically replace every message for a session.

        Used by transcript-rewrite flows such as /retry, /undo, and /compress.
        """
        async with thoth_db.transaction() as conn:
            await conn.execute(
                "DELETE FROM messages WHERE session_id = $1", session_id
            )
            await conn.execute(
                "UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = $1",
                session_id,
            )

            total_messages = 0
            total_tool_calls = 0
            for msg in messages:
                role = msg.get("role", "unknown")
                tool_calls = msg.get("tool_calls")
                reasoning_details = msg.get("reasoning_details") if role == "assistant" else None
                codex_reasoning_items = (
                    msg.get("codex_reasoning_items") if role == "assistant" else None
                )
                codex_message_items = (
                    msg.get("codex_message_items") if role == "assistant" else None
                )
                # Accept either `platform_message_id` (new explicit name) or
                # `message_id` (an older convention on message dicts).
                platform_msg_id = (
                    msg.get("platform_message_id") or msg.get("message_id")
                )

                await conn.execute(
                    """
                    INSERT INTO messages (
                        session_id, role, content, tool_call_id, tool_calls,
                        tool_name, token_count, finish_reason, reasoning,
                        reasoning_content, reasoning_details, codex_reasoning_items,
                        codex_message_items, platform_message_id
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                    """,
                    session_id,
                    role,
                    msg.get("content"),
                    msg.get("tool_call_id"),
                    tool_calls,
                    msg.get("tool_name"),
                    msg.get("token_count"),
                    msg.get("finish_reason"),
                    msg.get("reasoning") if role == "assistant" else None,
                    msg.get("reasoning_content") if role == "assistant" else None,
                    reasoning_details,
                    codex_reasoning_items,
                    codex_message_items,
                    platform_msg_id,
                )
                total_messages += 1
                if tool_calls is not None:
                    total_tool_calls += (
                        len(tool_calls) if isinstance(tool_calls, list) else 1
                    )

            await conn.execute(
                "UPDATE sessions SET message_count = $2, tool_call_count = $3 WHERE id = $1",
                session_id, total_messages, total_tool_calls,
            )

    async def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Load all messages for a session, ordered by insertion order."""
        async with thoth_db.connection() as conn:
            rows = await conn.fetch(
                "SELECT * FROM messages WHERE session_id = $1 ORDER BY id",
                session_id,
            )
        return [dict(r) for r in rows]

    async def get_messages_around(
        self,
        session_id: str,
        around_message_id: int,
        window: int = 5,
    ) -> Dict[str, Any]:
        """Load a window of messages anchored on a specific message id.

        Returns a dict with:
          - ``window``: up to ``window`` messages before the anchor, the anchor
            itself, and up to ``window`` messages after, ordered by id ascending.
          - ``messages_before``: count of messages strictly before the anchor
            still in the session (== window unless we hit the start).
          - ``messages_after``: count of messages strictly after the anchor
            still in the session (== window unless we hit the end).
        """
        if window < 0:
            window = 0
        async with thoth_db.connection() as conn:
            anchor_exists = await conn.fetchrow(
                "SELECT 1 FROM messages WHERE id = $1 AND session_id = $2 LIMIT 1",
                around_message_id, session_id,
            )
            if not anchor_exists:
                return {"window": [], "messages_before": 0, "messages_after": 0}

            # before_rows: anchor + everything before it, DESC (so LIMIT takes closest)
            before_rows = await conn.fetch(
                "SELECT * FROM messages "
                "WHERE session_id = $1 AND id <= $2 "
                "ORDER BY id DESC LIMIT $3",
                session_id, around_message_id, window + 1,
            )
            after_rows = await conn.fetch(
                "SELECT * FROM messages "
                "WHERE session_id = $1 AND id > $2 "
                "ORDER BY id ASC LIMIT $3",
                session_id, around_message_id, window,
            )

        # before_rows is DESC; reverse so ASC, then concatenate after_rows.
        rows = list(reversed(before_rows)) + list(after_rows)
        result = [dict(r) for r in rows]

        # before_rows includes the anchor itself; subtract 1 for strict-before count.
        messages_before = max(0, len(before_rows) - 1)
        messages_after = len(after_rows)
        return {
            "window": result,
            "messages_before": messages_before,
            "messages_after": messages_after,
        }

    async def get_anchored_view(
        self,
        session_id: str,
        around_message_id: int,
        window: int = 5,
        bookend: int = 3,
        keep_roles: Optional[Tuple[str, ...]] = ("user", "assistant"),
    ) -> Dict[str, Any]:
        """Return an anchored window plus session bookends.

        Built on top of ``get_messages_around``. Three slices:

          - ``window``: messages immediately surrounding the anchor. Filtered
            to ``keep_roles`` (tool-response noise dropped by default), EXCEPT
            the anchor itself is always preserved regardless of role.
          - ``bookend_start``: first ``bookend`` user/assistant messages of the
            session — but only those whose id is strictly before the window's
            first message id. Empty when the window already overlaps the session head.
            Empty-content messages (tool-call-only assistant turns) are skipped.
          - ``bookend_end``: last ``bookend`` user/assistant messages of the
            session, same non-overlap rule at the tail.
        """
        if bookend < 0:
            bookend = 0

        # Reuse the primitive — handles anchor-existence, and boundary counts.
        primitive = await self.get_messages_around(
            session_id, around_message_id, window=window
        )
        window_rows = primitive["window"]
        if not window_rows:
            return {
                "window": [],
                "messages_before": 0,
                "messages_after": 0,
                "bookend_start": [],
                "bookend_end": [],
            }

        # Apply role filter to the window, but never drop the anchor itself.
        if keep_roles is not None:
            keep_set = set(keep_roles)
            filtered_window = [
                m for m in window_rows
                if m.get("id") == around_message_id or m.get("role") in keep_set
            ]
        else:
            filtered_window = window_rows

        window_min_id = window_rows[0]["id"]
        window_max_id = window_rows[-1]["id"]

        bookend_start_rows: List[Dict[str, Any]] = []
        bookend_end_rows: List[Dict[str, Any]] = []
        if bookend > 0:
            # Build role filter clause for bookends
            role_filter = ""
            role_params: list = []
            if keep_roles is not None:
                placeholders = ", ".join(
                    f"${i + 3}" for i in range(len(keep_roles))
                )
                role_filter = f" AND role IN ({placeholders})"
                role_params = list(keep_roles)

            async with thoth_db.connection() as conn:
                start_limit_idx = 3 + len(role_params)
                start_rows = await conn.fetch(
                    f"SELECT * FROM messages "
                    f"WHERE session_id = $1 AND id < $2{role_filter} "
                    f"AND length(content) > 0 "
                    f"ORDER BY id ASC LIMIT ${start_limit_idx}",
                    session_id, window_min_id, *role_params, bookend,
                )
                end_limit_idx = 3 + len(role_params)
                end_rows = await conn.fetch(
                    f"SELECT * FROM messages "
                    f"WHERE session_id = $1 AND id > $2{role_filter} "
                    f"AND length(content) > 0 "
                    f"ORDER BY id DESC LIMIT ${end_limit_idx}",
                    session_id, window_max_id, *role_params, bookend,
                )
                # end_rows came back DESC for the LIMIT cap; flip to ASC.
                end_rows = list(reversed(end_rows))

            bookend_start_rows = [dict(r) for r in start_rows]
            bookend_end_rows = [dict(r) for r in end_rows]

        return {
            "window": filtered_window,
            "messages_before": primitive["messages_before"],
            "messages_after": primitive["messages_after"],
            "bookend_start": bookend_start_rows,
            "bookend_end": bookend_end_rows,
        }

    async def resolve_resume_session_id(self, session_id: str) -> str:
        """Redirect a resume target to the descendant session that holds the messages.

        Walks ``parent_session_id`` forward from ``session_id`` and returns the
        first descendant in the chain that has at least one message row. If the
        original session already has messages, or no descendant has any, the
        original ``session_id`` is returned unchanged.
        """
        if not session_id:
            return session_id

        try:
            async with thoth_db.connection() as conn:
                row = await conn.fetchrow(
                    "SELECT 1 FROM messages WHERE session_id = $1 LIMIT 1",
                    session_id,
                )
        except Exception:
            return session_id
        if row is not None:
            return session_id

        # Walk descendants: at each step, pick the most-recently-started child.
        current = session_id
        seen = {current}
        for _ in range(32):
            try:
                async with thoth_db.connection() as conn:
                    child_row = await conn.fetchrow(
                        "SELECT id FROM sessions "
                        "WHERE parent_session_id = $1 "
                        "ORDER BY started_at DESC, id DESC LIMIT 1",
                        current,
                    )
            except Exception:
                return session_id
            if child_row is None:
                return session_id
            child_id = child_row["id"]
            if not child_id or child_id in seen:
                return session_id
            seen.add(child_id)
            try:
                async with thoth_db.connection() as conn:
                    msg_row = await conn.fetchrow(
                        "SELECT 1 FROM messages WHERE session_id = $1 LIMIT 1",
                        child_id,
                    )
            except Exception:
                return session_id
            if msg_row is not None:
                return child_id
            current = child_id
        return session_id

    async def _session_lineage_root_to_tip(self, session_id: str) -> List[str]:
        """Walk the parent_session_id chain from the root down to session_id."""
        if not session_id:
            return [session_id]

        chain = []
        current = session_id
        seen: set = set()
        for _ in range(100):
            if not current or current in seen:
                break
            seen.add(current)
            chain.append(current)
            async with thoth_db.connection() as conn:
                row = await conn.fetchrow(
                    "SELECT parent_session_id FROM sessions WHERE id = $1",
                    current,
                )
            if row is None:
                break
            current = row["parent_session_id"]
        return list(reversed(chain)) or [session_id]

    async def get_messages_as_conversation(
        self, session_id: str, include_ancestors: bool = False
    ) -> List[Dict[str, Any]]:
        """Load messages in the OpenAI conversation format (role + content dicts).

        Used by the gateway to restore conversation history.
        """
        session_ids = [session_id]
        if include_ancestors:
            session_ids = await self._session_lineage_root_to_tip(session_id)

        placeholders = ", ".join(f"${i + 1}" for i in range(len(session_ids)))
        async with thoth_db.connection() as conn:
            rows = await conn.fetch(
                f"SELECT role, content, tool_call_id, tool_calls, tool_name, "
                f"finish_reason, reasoning, reasoning_content, reasoning_details, "
                f"codex_reasoning_items, codex_message_items, platform_message_id "
                f"FROM messages WHERE session_id IN ({placeholders}) ORDER BY id",
                *session_ids,
            )

        messages = []
        for row in rows:
            content = row["content"]
            if row["role"] in {"user", "assistant"} and isinstance(content, str):
                content = sanitize_context(content).strip()
            msg: Dict[str, Any] = {"role": row["role"], "content": content}
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["tool_name"]:
                msg["tool_name"] = row["tool_name"]
            if row["tool_calls"] is not None:
                msg["tool_calls"] = row["tool_calls"]
            # Surface the platform-side message id so platform-specific flows
            # can match by external identifier. Exposed as ``message_id`` for
            # backward compatibility with the JSONL transcript shape.
            if row["platform_message_id"]:
                msg["message_id"] = row["platform_message_id"]
            # Restore reasoning fields on assistant messages.
            if row["role"] == "assistant":
                if row["finish_reason"]:
                    msg["finish_reason"] = row["finish_reason"]
                if row["reasoning"]:
                    msg["reasoning"] = row["reasoning"]
                if row["reasoning_content"] is not None:
                    msg["reasoning_content"] = row["reasoning_content"]
                if row["reasoning_details"] is not None:
                    msg["reasoning_details"] = row["reasoning_details"]
                if row["codex_reasoning_items"] is not None:
                    msg["codex_reasoning_items"] = row["codex_reasoning_items"]
                if row["codex_message_items"] is not None:
                    msg["codex_message_items"] = row["codex_message_items"]
            if include_ancestors and _AsyncSessionDB._is_duplicate_replayed_user_message(messages, msg):
                continue
            messages.append(msg)
        return messages

    @staticmethod
    def _is_duplicate_replayed_user_message(messages: List[Dict[str, Any]], msg: Dict[str, Any]) -> bool:
        if msg.get("role") != "user":
            return False
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            return False
        for prev in reversed(messages):
            if prev.get("role") == "user" and prev.get("content") == content:
                return True
            if prev.get("role") == "assistant" and (prev.get("content") or prev.get("tool_calls")):
                return False
        return False

    # === Search (Task 13) ===

    async def search_messages(
        self,
        query: str,
        source_filter: List[str] = None,
        exclude_sources: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
        sort: str = None,
        mode: str = "keyword",
    ) -> List[Dict[str, Any]]:
        """Full-text search across session messages.

        Modes:
          - ``keyword`` (default): uses ``content_tsv @@ plainto_tsquery`` over
            the GIN tsvector index. AND-semantics by default; phrase-quotes
            are preserved by plainto_tsquery. Falls back to an empty list on
            tsquery syntax errors.
          - ``fuzzy``: uses ``content % query`` via pg_trgm similarity (GIN
            trigram index). Useful for typos, partial terms, CJK substring
            matching that was handled by the SQLite trigram FTS5 table.
          - ``auto``: tries ``keyword`` first; if fewer than ``limit // 2``
            hits are returned, re-runs as ``fuzzy`` and merges (deduped) on
            top.

        Returns these columns:
          id, session_id, role, snippet, content (dropped below), timestamp,
          tool_name, source, model, session_started.
        Context (±1 message) is appended as ``context`` on each row; full
        ``content`` is removed from the result to save tokens.

        ``sort`` controls temporal ordering:
          - ``None``: rank-only (relevance).
          - ``"newest"``: timestamp DESC, rank tiebreak.
          - ``"oldest"``: timestamp ASC, rank tiebreak.

        Upstream signature ported exactly (source_filter, exclude_sources,
        role_filter, limit, offset, sort). ``mode`` is a PG-only addition.
        """
        if not query or not query.strip():
            return []

        # Normalise sort
        if isinstance(sort, str):
            sort_norm = sort.strip().lower()
            if sort_norm not in ("newest", "oldest"):
                sort_norm = None
        else:
            sort_norm = None

        async def _run_keyword(q: str, lim: int, off: int) -> List[Dict[str, Any]]:
            params: list = []
            where: list = []

            params.append(q)
            where.append(f"m.content_tsv @@ plainto_tsquery('english', ${len(params)})")
            rank_expr = f"ts_rank(m.content_tsv, plainto_tsquery('english', ${len(params)}))"

            if source_filter is not None:
                params.append(list(source_filter))
                where.append(f"s.source = ANY(${len(params)})")

            if exclude_sources is not None:
                params.append(list(exclude_sources))
                where.append(f"s.source <> ALL(${len(params)})")

            if role_filter:
                roles = list(role_filter)
                params.append(roles)
                where.append(f"m.role = ANY(${len(params)})")

            if sort_norm == "newest":
                order_by = f"ORDER BY m.timestamp DESC, {rank_expr} DESC"
            elif sort_norm == "oldest":
                order_by = f"ORDER BY m.timestamp ASC, {rank_expr} DESC"
            else:
                order_by = f"ORDER BY {rank_expr} DESC"

            params.extend([lim, off])
            limit_ph = len(params) - 1
            offset_ph = len(params)

            sql = f"""
                SELECT
                    m.id,
                    m.session_id,
                    m.role,
                    ts_headline('english', m.content,
                                plainto_tsquery('english', $1),
                                'StartSel=>>>,StopSel=<<<,MaxWords=40,MinWords=10') AS snippet,
                    m.content,
                    m.timestamp,
                    m.tool_name,
                    s.source,
                    s.model,
                    s.started_at AS session_started
                FROM messages m
                JOIN sessions s ON s.id = m.session_id
                WHERE {' AND '.join(where)}
                {order_by}
                LIMIT ${limit_ph} OFFSET ${offset_ph}
            """
            async with thoth_db.connection() as conn:
                try:
                    rows = await conn.fetch(sql, *params)
                except Exception:
                    return []
            return [dict(r) for r in rows]

        async def _run_fuzzy(q: str, lim: int, off: int) -> List[Dict[str, Any]]:
            params: list = []
            where: list = []

            # Use word_similarity (<%): matches query term against any word in
            # content. More lenient than whole-string similarity (%) — catches
            # typos/partial terms like "quanto" → "quantum" where the full-string
            # similarity is below the default 0.3 threshold.
            params.append(q)
            where.append(f"${len(params)} <% m.content")
            rank_expr = f"word_similarity(${len(params)}, m.content)"

            if source_filter is not None:
                params.append(list(source_filter))
                where.append(f"s.source = ANY(${len(params)})")

            if exclude_sources is not None:
                params.append(list(exclude_sources))
                where.append(f"s.source <> ALL(${len(params)})")

            if role_filter:
                roles = list(role_filter)
                params.append(roles)
                where.append(f"m.role = ANY(${len(params)})")

            if sort_norm == "newest":
                order_by = f"ORDER BY m.timestamp DESC, {rank_expr} DESC"
            elif sort_norm == "oldest":
                order_by = f"ORDER BY m.timestamp ASC, {rank_expr} DESC"
            else:
                order_by = f"ORDER BY {rank_expr} DESC"

            params.extend([lim, off])
            limit_ph = len(params) - 1
            offset_ph = len(params)

            sql = f"""
                SELECT
                    m.id,
                    m.session_id,
                    m.role,
                    substring(m.content for 200) AS snippet,
                    m.content,
                    m.timestamp,
                    m.tool_name,
                    s.source,
                    s.model,
                    s.started_at AS session_started
                FROM messages m
                JOIN sessions s ON s.id = m.session_id
                WHERE {' AND '.join(where)}
                {order_by}
                LIMIT ${limit_ph} OFFSET ${offset_ph}
            """
            async with thoth_db.connection() as conn:
                try:
                    rows = await conn.fetch(sql, *params)
                except Exception:
                    return []
            return [dict(r) for r in rows]

        if mode == "keyword":
            matches = await _run_keyword(query, limit, offset)
        elif mode == "fuzzy":
            matches = await _run_fuzzy(query, limit, offset)
        elif mode == "auto":
            matches = await _run_keyword(query, limit, offset)
            if len(matches) < limit // 2:
                fuzzy_matches = await _run_fuzzy(query, limit, offset)
                seen_ids = {m["id"] for m in matches}
                for fm in fuzzy_matches:
                    if fm["id"] not in seen_ids:
                        matches.append(fm)
                        seen_ids.add(fm["id"])
                matches = matches[:limit]
        else:
            raise ValueError(f"unknown mode: {mode!r}")

        # Add surrounding context (±1 message) for each match
        for match in matches:
            try:
                msg_id = match["id"]
                session_id = match["session_id"]
                async with thoth_db.connection() as conn:
                    ctx_rows = await conn.fetch(
                        """
                        WITH target AS (
                            SELECT session_id, timestamp, id
                            FROM messages
                            WHERE id = $1
                        )
                        SELECT role, content FROM (
                            SELECT m.id, m.timestamp, m.role, m.content
                            FROM messages m
                            JOIN target t ON t.session_id = m.session_id
                            WHERE (m.timestamp < t.timestamp)
                               OR (m.timestamp = t.timestamp AND m.id < t.id)
                            ORDER BY m.timestamp DESC, m.id DESC
                            LIMIT 1
                        ) _before
                        UNION ALL
                        SELECT role, content FROM messages WHERE id = $1
                        UNION ALL
                        SELECT role, content FROM (
                            SELECT m.id, m.timestamp, m.role, m.content
                            FROM messages m
                            JOIN target t ON t.session_id = m.session_id
                            WHERE (m.timestamp > t.timestamp)
                               OR (m.timestamp = t.timestamp AND m.id > t.id)
                            ORDER BY m.timestamp ASC, m.id ASC
                            LIMIT 1
                        ) _after
                        """,
                        msg_id,
                    )
                context_msgs = []
                for r in ctx_rows:
                    raw = r["content"]
                    if isinstance(raw, list):
                        text_parts = [
                            p.get("text", "") for p in raw
                            if isinstance(p, dict) and p.get("type") == "text"
                        ]
                        preview = " ".join(t for t in text_parts if t).strip() or "[multimodal content]"
                    elif isinstance(raw, str):
                        preview = raw
                    else:
                        preview = ""
                    context_msgs.append({"role": r["role"], "content": preview[:200]})
                match["context"] = context_msgs
            except Exception:
                match["context"] = []

        # Drop full content (snippet is enough)
        for match in matches:
            match.pop("content", None)

        return matches

    async def search_sessions(
        self,
        source: str = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List sessions, optionally filtered by source.

        Returns rows enriched with a computed ``last_active`` column (latest
        message timestamp, falling back to ``started_at``), ordered by
        most-recently-used first. Ported faithfully from SQLite upstream.
        """
        async with thoth_db.connection() as conn:
            if source:
                rows = await conn.fetch(
                    """
                    SELECT s.*,
                           COALESCE(m.last_active, s.started_at) AS last_active
                    FROM sessions s
                    LEFT JOIN (
                        SELECT session_id, MAX(timestamp) AS last_active
                        FROM messages
                        GROUP BY session_id
                    ) m ON m.session_id = s.id
                    WHERE s.source = $1
                    ORDER BY last_active DESC, s.started_at DESC, s.id DESC
                    LIMIT $2 OFFSET $3
                    """,
                    source, limit, offset,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT s.*,
                           COALESCE(m.last_active, s.started_at) AS last_active
                    FROM sessions s
                    LEFT JOIN (
                        SELECT session_id, MAX(timestamp) AS last_active
                        FROM messages
                        GROUP BY session_id
                    ) m ON m.session_id = s.id
                    ORDER BY last_active DESC, s.started_at DESC, s.id DESC
                    LIMIT $1 OFFSET $2
                    """,
                    limit, offset,
                )
        return [dict(r) for r in rows]

    # === Counts & export (Task 14) ===
    async def session_count(self, source: str = None) -> int:
        """Count sessions, optionally filtered by source."""
        async with thoth_db.connection() as conn:
            if source is None:
                count = await conn.fetchval("SELECT COUNT(*) FROM sessions")
            else:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM sessions WHERE source = $1", source
                )
        return count

    async def message_count(self, session_id: str = None) -> int:
        """Count messages, optionally for a specific session."""
        async with thoth_db.connection() as conn:
            if session_id is None:
                count = await conn.fetchval("SELECT COUNT(*) FROM messages")
            else:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM messages WHERE session_id = $1", session_id
                )
        return count

    async def export_session(self, session_id: str):
        """Export a single session with all its messages as a dict."""
        session = await self.get_session(session_id)
        if not session:
            return None
        messages = await self.get_messages(session_id)
        return {**session, "messages": messages}

    async def export_all(self, source: str = None) -> list:
        """
        Export all sessions (with messages) as a list of dicts.
        Suitable for writing to a JSONL file for backup/analysis.
        """
        sessions = await self.search_sessions(source=source, limit=100000)
        results = []
        for session in sessions:
            messages = await self.get_messages(session["id"])
            results.append({**session, "messages": messages})
        return results

    # === Deletion / pruning (Task 15) ===

    @staticmethod
    def _remove_session_files(sessions_dir: Optional[Path], session_id: str) -> None:
        """Remove on-disk transcript files for a session.

        Cleans up ``{session_id}.json``, ``{session_id}.jsonl``, and any
        ``request_dump_{session_id}_*.json`` files left by the gateway.
        Silently skips files that don't exist and swallows OSError so a
        filesystem hiccup never blocks a DB operation.
        """
        if sessions_dir is None:
            return
        for suffix in (".json", ".jsonl"):
            p = sessions_dir / f"{session_id}{suffix}"
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        # request_dump files use session_id as a prefix component
        try:
            for p in sessions_dir.glob(f"request_dump_{session_id}_*.json"):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError:
            pass

    async def clear_messages(self, session_id: str) -> None:
        """Delete all messages for a session and reset its counters."""
        async with thoth_db.connection() as conn:
            await conn.execute(
                "DELETE FROM messages WHERE session_id = $1", session_id
            )
            await conn.execute(
                "UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = $1",
                session_id,
            )

    async def delete_session(
        self,
        session_id: str,
        sessions_dir: Optional[Path] = None,
    ) -> bool:
        """Delete a session and all its messages.

        Child sessions are orphaned (parent_session_id set to NULL) rather
        than cascade-deleted, so they remain accessible independently.
        When *sessions_dir* is provided, also removes on-disk transcript
        files (``.json`` / ``.jsonl`` / ``request_dump_*``) for the deleted
        session. Returns True if the session was found and deleted.
        """
        found = False
        async with thoth_db.connection() as conn:
            # Check if session exists
            result = await conn.fetchval(
                "SELECT 1 FROM sessions WHERE id = $1", session_id
            )
            if result is None:
                return False

            # Orphan child sessions so FK constraint is satisfied
            await conn.execute(
                "UPDATE sessions SET parent_session_id = NULL "
                "WHERE parent_session_id = $1",
                session_id,
            )
            # Delete messages (FK is ON DELETE CASCADE in case, but explicit for safety)
            await conn.execute("DELETE FROM messages WHERE session_id = $1", session_id)
            # Delete session
            await conn.execute("DELETE FROM sessions WHERE id = $1", session_id)
            found = True

        if found:
            self._remove_session_files(sessions_dir, session_id)
        return found

    async def prune_sessions(
        self,
        older_than_days: int = 90,
        source: str = None,
        sessions_dir: Optional[Path] = None,
    ) -> int:
        """Delete sessions older than N days. Returns count of deleted sessions.

        Only prunes ended sessions (not active ones).  Child sessions outside
        the prune window are orphaned (parent_session_id set to NULL) rather
        than cascade-deleted.  When *sessions_dir* is provided, also removes
        on-disk transcript files (``.json`` / ``.jsonl`` /
        ``request_dump_*``) for every pruned session, outside the DB
        transaction.
        """
        import time

        cutoff = time.time() - (older_than_days * 86400)
        removed_ids: list[str] = []

        async with thoth_db.connection() as conn:
            if source:
                cursor = await conn.fetch(
                    """SELECT id FROM sessions
                       WHERE started_at < to_timestamp($1)
                       AND ended_at IS NOT NULL AND source = $2""",
                    cutoff,
                    source,
                )
            else:
                cursor = await conn.fetch(
                    """SELECT id FROM sessions
                       WHERE started_at < to_timestamp($1) AND ended_at IS NOT NULL""",
                    cutoff,
                )
            session_ids = {row["id"] for row in cursor}

            if not session_ids:
                return 0

            # Orphan any sessions whose parent is about to be deleted
            placeholders = ",".join([f"${i}" for i in range(1, len(session_ids) + 1)])
            await conn.execute(
                f"UPDATE sessions SET parent_session_id = NULL "
                f"WHERE parent_session_id IN ({placeholders})",
                *session_ids,
            )

            for sid in session_ids:
                await conn.execute("DELETE FROM messages WHERE session_id = $1", sid)
                await conn.execute("DELETE FROM sessions WHERE id = $1", sid)
                removed_ids.append(sid)

        # Clean up on-disk files outside the DB transaction
        for sid in removed_ids:
            self._remove_session_files(sessions_dir, sid)

        return len(removed_ids)

    # === Meta (Task 16) ===
    async def get_meta(self, key: str):
        """Read a value from the state_meta key/value store."""
        async with thoth_db.connection() as conn:
            return await conn.fetchval("SELECT value FROM state_meta WHERE key = $1", key)

    async def set_meta(self, key: str, value: str) -> None:
        """Write a value to the state_meta key/value store."""
        async with thoth_db.connection() as conn:
            await conn.execute(
                """
                INSERT INTO state_meta (key, value) VALUES ($1, $2)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                key, value,
            )

    # === Telegram topics (Task 17) ===

    async def apply_telegram_topic_migration(self) -> None:
        """No-op under Alembic; DDL is in 20260522_0001 initial migration."""
        return

    async def enable_telegram_topic_mode(
        self,
        *,
        chat_id: str,
        user_id: str,
        has_topics_enabled: Optional[bool] = None,
        allows_users_to_create_topics: Optional[bool] = None,
    ) -> None:
        """Enable Telegram DM topic mode for one private chat/user."""

        def _to_int(value: Optional[bool]) -> Optional[int]:
            if value is None:
                return None
            return 1 if value else 0

        async with thoth_db.connection() as conn:
            await conn.execute(
                """
                INSERT INTO telegram_dm_topic_mode (
                    chat_id, user_id, enabled, activated_at, updated_at,
                    has_topics_enabled, allows_users_to_create_topics,
                    capability_checked_at
                ) VALUES ($1, $2, 1, now(), now(), $3, $4, now())
                ON CONFLICT (chat_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    enabled = 1,
                    updated_at = now(),
                    has_topics_enabled = EXCLUDED.has_topics_enabled,
                    allows_users_to_create_topics = EXCLUDED.allows_users_to_create_topics,
                    capability_checked_at = now()
                """,
                str(chat_id),
                str(user_id),
                _to_int(has_topics_enabled),
                _to_int(allows_users_to_create_topics),
            )

    async def disable_telegram_topic_mode(
        self,
        *,
        chat_id: str,
        clear_bindings: bool = True,
    ) -> None:
        """Disable Telegram DM topic mode for one private chat.

        When ``clear_bindings`` is True (default) the (chat_id, thread_id)
        bindings for this chat are also cleared so re-enabling later
        starts from a clean slate.
        """
        async with thoth_db.connection() as conn:
            await conn.execute(
                "UPDATE telegram_dm_topic_mode SET enabled = 0, updated_at = now() "
                "WHERE chat_id = $1",
                str(chat_id),
            )
            if clear_bindings:
                await conn.execute(
                    "DELETE FROM telegram_dm_topic_bindings WHERE chat_id = $1",
                    str(chat_id),
                )

    async def is_telegram_topic_mode_enabled(self, *, chat_id: str, user_id: str) -> bool:
        """Return whether Telegram DM topic mode is enabled for this chat/user."""
        async with thoth_db.connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT enabled FROM telegram_dm_topic_mode
                WHERE chat_id = $1 AND user_id = $2
                """,
                str(chat_id),
                str(user_id),
            )
        if row is None:
            return False
        return bool(row["enabled"])

    async def get_telegram_topic_binding(
        self,
        *,
        chat_id: str,
        thread_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the session binding for a Telegram DM topic, if present."""
        async with thoth_db.connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM telegram_dm_topic_bindings
                WHERE chat_id = $1 AND thread_id = $2
                """,
                str(chat_id),
                str(thread_id),
            )
        return dict(row) if row else None

    async def list_telegram_topic_bindings_for_chat(
        self,
        *,
        chat_id: str,
    ) -> List[Dict[str, Any]]:
        """All Telegram DM topic bindings for one chat, newest first."""
        async with thoth_db.connection() as conn:
            rows = await conn.fetch(
                "SELECT * FROM telegram_dm_topic_bindings "
                "WHERE chat_id = $1 ORDER BY updated_at DESC",
                str(chat_id),
            )
        return [dict(row) for row in rows]

    async def get_telegram_topic_binding_by_session(
        self,
        *,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the Telegram DM topic binding for a given session_id, if present.

        Uses the UNIQUE INDEX on telegram_dm_topic_bindings(session_id) for an
        efficient reverse lookup.
        """
        async with thoth_db.connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM telegram_dm_topic_bindings WHERE session_id = $1",
                str(session_id),
            )
        return dict(row) if row else None

    async def bind_telegram_topic(
        self,
        *,
        chat_id: str,
        thread_id: str,
        user_id: str,
        session_key: str,
        session_id: str,
        managed_mode: str = "auto",
    ) -> None:
        """Bind one Telegram DM topic thread to one Thoth session.

        A Thoth session may only be linked to one Telegram topic in MVP.
        Rebinding the same topic to the same session is idempotent; trying to
        link the same session to a different topic raises ValueError.
        """
        chat_id = str(chat_id)
        thread_id = str(thread_id)
        user_id = str(user_id)
        session_key = str(session_key)
        session_id = str(session_id)

        async with thoth_db.connection() as conn:
            existing = await conn.fetchrow(
                """
                SELECT chat_id, thread_id FROM telegram_dm_topic_bindings
                WHERE session_id = $1
                """,
                session_id,
            )
            if existing is not None:
                if str(existing["chat_id"]) != chat_id or str(existing["thread_id"]) != thread_id:
                    raise ValueError("session is already linked to another Telegram topic")

            await conn.execute(
                """
                INSERT INTO telegram_dm_topic_bindings (
                    chat_id, thread_id, user_id, session_key, session_id,
                    managed_mode, linked_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, now(), now())
                ON CONFLICT (chat_id, thread_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    session_key = EXCLUDED.session_key,
                    session_id = EXCLUDED.session_id,
                    managed_mode = EXCLUDED.managed_mode,
                    updated_at = now()
                """,
                chat_id,
                thread_id,
                user_id,
                session_key,
                session_id,
                managed_mode,
            )

    async def is_telegram_session_linked_to_topic(self, *, session_id: str) -> bool:
        """Return True if a Thoth session is already bound to any Telegram DM topic."""
        async with thoth_db.connection() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM telegram_dm_topic_bindings WHERE session_id = $1 LIMIT 1",
                str(session_id),
            )
        return row is not None

    async def list_unlinked_telegram_sessions_for_user(
        self,
        *,
        chat_id: str,
        user_id: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """List previous Telegram sessions for this user that are not bound to a topic."""
        async with thoth_db.connection() as conn:
            rows = await conn.fetch(
                """
                SELECT s.*,
                    COALESCE(
                        (SELECT SUBSTRING(
                                    REGEXP_REPLACE(m.content, E'[\\r\\n]+', ' ', 'g'),
                                    1, 63
                               )
                         FROM messages m
                         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                         ORDER BY m.timestamp, m.id LIMIT 1),
                        ''
                    ) AS _preview_raw,
                    COALESCE(
                        (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                        s.started_at
                    ) AS last_active
                FROM sessions s
                WHERE s.source = 'telegram'
                  AND s.user_id = $1
                  AND NOT EXISTS (
                      SELECT 1 FROM telegram_dm_topic_bindings b
                      WHERE b.session_id = s.id
                  )
                ORDER BY last_active DESC, s.started_at DESC
                LIMIT $2
                """,
                str(user_id),
                int(limit),
            )

        sessions: List[Dict[str, Any]] = []
        for row in rows:
            session = dict(row)
            raw = str(session.pop("_preview_raw", "") or "").strip()
            session["preview"] = raw[:60] + ("..." if len(raw) > 60 else "") if raw else ""
            sessions.append(session)
        return sessions

    # === Maintenance (Task 18) ===

    async def vacuum(self) -> None:
        """Run VACUUM ANALYZE on the PG database.

        VACUUM cannot run inside a transaction. ``thoth_db.connection()``
        acquires a plain connection without starting a transaction, so this
        is safe as long as the caller does not wrap in
        ``thoth_db.transaction()``.
        """
        async with thoth_db.connection() as conn:
            await conn.execute("VACUUM ANALYZE")

    async def maybe_auto_prune_and_vacuum(
        self,
        retention_days: int = 90,
        min_interval_hours: int = 24,
        vacuum: bool = True,
        sessions_dir=None,
    ) -> Dict[str, Any]:
        """Idempotent auto-maintenance: prune old sessions + optional VACUUM.

        Records the last run timestamp in state_meta so subsequent calls
        within ``min_interval_hours`` no-op. Designed to be called once at
        startup from long-lived entrypoints (CLI, gateway, cron scheduler).

        Never raises. On any failure, logs a warning and returns a dict
        with ``"error"`` set.

        Returns a dict with keys:
          - ``"skipped"`` (bool) — true if within min_interval_hours of last run
          - ``"pruned"`` (int)   — number of sessions deleted
          - ``"vacuumed"`` (bool) — true if VACUUM ran
          - ``"error"`` (str, optional) — present only on failure
        """
        result: Dict[str, Any] = {"skipped": False, "pruned": 0, "vacuumed": False}
        try:
            last_raw = await self.get_meta("last_auto_prune")
            now = time.time()
            if last_raw:
                try:
                    last_ts = float(last_raw)
                    if now - last_ts < min_interval_hours * 3600:
                        result["skipped"] = True
                        return result
                except (TypeError, ValueError):
                    pass  # corrupt meta; treat as no prior run

            pruned = await self.prune_sessions(
                older_than_days=retention_days,
                sessions_dir=sessions_dir,
            )
            result["pruned"] = pruned

            # Only VACUUM if we actually freed rows — VACUUM on a tight DB
            # is wasted I/O. Threshold keeps small DBs from paying the cost.
            if vacuum and pruned > 0:
                try:
                    await self.vacuum()
                    result["vacuumed"] = True
                except Exception as exc:
                    logger.warning("state.db VACUUM failed: %s", exc)

            # Record the attempt even if pruned == 0, so we don't retry
            # every startup within the min_interval_hours window.
            await self.set_meta("last_auto_prune", str(now))

            if pruned > 0:
                logger.info(
                    "state.db auto-maintenance: pruned %d session(s) older than %d days%s",
                    pruned,
                    retention_days,
                    " + VACUUM" if result["vacuumed"] else "",
                )
        except Exception as exc:
            logger.warning("state.db auto-maintenance failed: %s", exc)
            result["error"] = str(exc)

        return result

    # === Handoff (Task 18) ===
    #
    # State machine:
    #   None        — no handoff in flight
    #   "pending"   — CLI requested handoff, gateway hasn't picked it up yet
    #   "running"   — gateway is processing (session switch + synthetic turn)
    #   "completed" — gateway successfully delivered the synthetic turn
    #   "failed"    — gateway hit an error; reason in handoff_error
    #
    # The CLI writes "pending" then poll-waits for terminal state. The gateway
    # watcher transitions pending → running → {completed, failed}.

    async def request_handoff(self, session_id: str, platform: str) -> bool:
        """Mark a session as pending handoff to the given platform.

        Returns True if the row was found and not already in flight; False if
        the session is already in a non-terminal handoff state (i.e. pending
        or running).  Re-requesting from a terminal state (completed / failed)
        is allowed and resets the columns.
        """
        async with thoth_db.connection() as conn:
            row = await conn.fetchrow(
                """
                UPDATE sessions
                   SET handoff_state    = 'pending',
                       handoff_platform = $1,
                       handoff_error    = NULL
                 WHERE id = $2
                   AND (handoff_state IS NULL
                        OR handoff_state IN ('completed', 'failed'))
                RETURNING id
                """,
                platform,
                session_id,
            )
        return row is not None

    async def get_handoff_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Read the current handoff state for a session.

        Returns ``{"state", "platform", "error"}`` or None if the session
        does not exist.
        """
        try:
            async with thoth_db.connection() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT handoff_state, handoff_platform, handoff_error
                      FROM sessions
                     WHERE id = $1
                    """,
                    session_id,
                )
            if row is None:
                return None
            return {
                "state": row["handoff_state"],
                "platform": row["handoff_platform"],
                "error": row["handoff_error"],
            }
        except Exception:
            return None

    async def list_pending_handoffs(self) -> List[Dict[str, Any]]:
        """Return all sessions in handoff_state='pending', oldest first.

        Used by the gateway's handoff watcher.
        """
        try:
            async with thoth_db.connection() as conn:
                rows = await conn.fetch(
                    """
                    SELECT *
                      FROM sessions
                     WHERE handoff_state = 'pending'
                     ORDER BY started_at ASC
                    """
                )
            return [dict(r) for r in rows]
        except Exception:
            return []

    async def claim_handoff(self, session_id: str) -> bool:
        """Atomically transition pending → running. Returns True if claimed."""
        async with thoth_db.connection() as conn:
            row = await conn.fetchrow(
                """
                UPDATE sessions
                   SET handoff_state = 'running'
                 WHERE id = $1 AND handoff_state = 'pending'
                RETURNING id
                """,
                session_id,
            )
        return row is not None

    async def complete_handoff(self, session_id: str) -> None:
        """Mark a handoff as completed."""
        async with thoth_db.connection() as conn:
            await conn.execute(
                """
                UPDATE sessions
                   SET handoff_state = 'completed',
                       handoff_error = NULL
                 WHERE id = $1
                """,
                session_id,
            )

    async def fail_handoff(self, session_id: str, error: str) -> None:
        """Mark a handoff as failed and record the reason."""
        async with thoth_db.connection() as conn:
            await conn.execute(
                """
                UPDATE sessions
                   SET handoff_state = 'failed',
                       handoff_error = $1
                 WHERE id = $2
                """,
                error[:500],
                session_id,
            )

    def close(self) -> None:
        """No-op: _AsyncSessionDB has no per-instance connection to close.

        The process-wide pool is managed by thoth_db.init()/thoth_db.close().
        This method exists so call sites that used to call db.close() on the
        legacy SQLite SessionDB continue to work without modification.
        """


# Public name: _AsyncSessionDB is the only implementation.
SessionDB = _AsyncSessionDB
