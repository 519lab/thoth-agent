"""Thoth → substrate perception hooks.

Each hook validates inputs, dispatches to :func:`commit_slice` against the
appropriate auto-registered stream, and returns. Hooks **never** raise to
the Thoth caller — failures are logged and dropped (Phase A spec §6.2).

The hook layer exposes matched pairs:

* an async coroutine ``on_*_async(...)`` — preferred from any call site
  that's already in an async context (gateway loop, conversation loop,
  ACP server).
* a sync facade ``on_*(...)`` — used from sync call sites (cron, legacy
  CLI paths). Bridges via :func:`hermes_db.run_sync`.

The substrate instance is bound at boot via :func:`_bind`. Hooks called
before boot are silent no-ops (the ``_guard`` decorator checks).

Stream-name constants are local to this module — call sites reference
hook functions, not stream names, so namespace changes don't ripple.
"""

from __future__ import annotations

import functools
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg
    from substrate.facade import Substrate


_log = logging.getLogger("substrate.hooks")


# ---------------------------------------------------------------------------
# Stream-name constants. Auto-registered in ``Substrate.boot()`` per Phase A
# spec §9. Kept here so call sites import hook functions, not stream names.
# ---------------------------------------------------------------------------

# Per-source user-message streams. ``hermes.world.user_message.<source>``
# allows downstream consumers to filter by gateway (CLI vs Discord vs ACP).
NAME_USER_MESSAGE_PREFIX = "thoth.world.user_message"
NAME_ASSISTANT_RESPONSE = "thoth.self_action.assistant_response"
NAME_TOOL_CALL = "thoth.self_action.tool_call"
NAME_TOOL_RESULT = "thoth.self_state.tool_result"
NAME_SUBAGENT_SPAWN = "thoth.self_action.subagent_spawn"
NAME_SUBAGENT_RETURN = "thoth.self_state.subagent_return"
NAME_SESSION_LIFECYCLE = "thoth.self_state.session_lifecycle"
NAME_CRON_DISPATCH = "thoth.self_state.cron_dispatch"


# ---------------------------------------------------------------------------
# Module-global binding. ``Substrate.boot()`` calls ``_bind(self)`` so every
# hook reads the same Substrate. Set to ``None`` until boot — hooks called
# before then are silent no-ops.
# ---------------------------------------------------------------------------


_substrate: "Optional[Substrate]" = None


def _bind(substrate: "Substrate") -> None:
    """Bind the module-global Substrate. Idempotent — calling twice with
    the same instance is fine; calling with a different one rebinds (the
    use case is test fixtures swapping the substrate between tests).
    """
    global _substrate
    _substrate = substrate


def _unbind() -> None:
    """Clear the module-global. Used by ``Substrate.shutdown`` and by
    tests that need to verify no-op behavior pre-boot.
    """
    global _substrate
    _substrate = None


def _substrate_for_tests() -> "Optional[Substrate]":
    """Test-only read accessor for the bound substrate. Production code
    must NOT touch this — use the hook surface (``on_user_message_async``
    etc.) which goes through ``_guard`` and handles the unbound case.
    Tests need this to assert ``boot_writer`` binds and ``boot_worker``
    leaves the binding alone.
    """
    return _substrate


# ---------------------------------------------------------------------------
# Internal helpers: the ``_guard`` decorator + payload summariser.
# ---------------------------------------------------------------------------


def _guard(hook_name: str) -> Callable:
    """Decorator: silently no-op if substrate not booted; log + swallow on
    error. Works for both sync and async wrapped functions — the wrapper
    inspects the wrapped callable and returns the matching shape.

    Why no-op rather than raise: hooks are called inline from Thoth's hot
    path. A hook failure must NEVER bubble to a Thoth call site (Phase A
    spec §6.2). Silent dropping with a log is the contract.
    """

    def deco(fn: Callable) -> Callable:
        import asyncio

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                if _substrate is None:
                    return None
                try:
                    return await fn(*args, **kwargs)
                except Exception:
                    _substrate.log.exception(
                        "substrate.hook.error hook=%s", hook_name
                    )
                    return None

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            if _substrate is None:
                return None
            try:
                return fn(*args, **kwargs)
            except Exception:
                _substrate.log.exception(
                    "substrate.hook.error hook=%s", hook_name
                )
                return None

        return sync_wrapper

    return deco


# Truncate large tool outputs to keep JSONB rows small. The 256-char
# threshold is generous enough to keep short results intact (most CLI tool
# outputs) while bounding the worst case (full-file reads, search hits).
# Reflector / Curator (Phase B+) will do better summarisation; for now a
# truncation + length suffix preserves the shape without bloating storage.
_SUMMARY_MAX_CHARS = 256


def _summarize(value: Any) -> Any:
    """Truncate string-like values to ``_SUMMARY_MAX_CHARS`` with a
    length suffix; pass through ``None`` and dict/list payloads (they're
    presumed already JSON-shaped). Bytes are converted to a length-only
    marker — binary payloads belong on a blob ref, not in JSONB.
    """
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"bytes_len": len(value)}
    s = str(value)
    if len(s) <= _SUMMARY_MAX_CHARS:
        return s
    return s[:_SUMMARY_MAX_CHARS] + f"…({len(s)} chars)"


# ---------------------------------------------------------------------------
# Hook surface: each event is a matched (async, sync) pair.
# ---------------------------------------------------------------------------

# Order in this module mirrors spec §6.1.


# ── user_message ────────────────────────────────────────────────────────────


@_guard("on_user_message")
async def on_user_message_async(
    session_id: str,
    source: str,
    text: str,
    t_event: datetime,
) -> None:
    """Called from async user-message intake sites (gateway, conversation
    loop, ACP). Emits one TEXT slice on
    ``hermes.world.user_message.<source>``.
    """
    from substrate.l0 import commit_slice

    name = f"{NAME_USER_MESSAGE_PREFIX}.{source}"
    stream = await _substrate.streams.get_by_name(name)
    if stream is None:
        _substrate.log.warning(
            "substrate.hook.unknown_stream hook=on_user_message stream=%s",
            name,
        )
        return
    await commit_slice(
        _substrate,
        stream_id=stream.stream_id,
        payload=text,
        event_time_world=t_event,
        metadata={"session_id": session_id, "source": source},
    )


def on_user_message(
    session_id: str,
    source: str,
    text: str,
    t_event: datetime,
) -> None:
    """Sync facade — bridges via :func:`hermes_db.run_sync`. Use only
    from sync call sites; calling from inside an event loop raises (the
    underlying ``run_sync`` guard)."""
    if _substrate is None:
        return None
    import hermes_db

    try:
        hermes_db.run_sync(
            on_user_message_async(session_id, source, text, t_event)
        )
    except Exception:
        _substrate.log.exception("substrate.hook.error hook=on_user_message")


# ── assistant_response ─────────────────────────────────────────────────────


@_guard("on_assistant_response")
async def on_assistant_response_async(
    session_id: str,
    model: str,
    text: str,
    t_event: datetime,
) -> None:
    """Emit a TEXT slice on ``hermes.self_action.assistant_response``."""
    from substrate.l0 import commit_slice

    stream = await _substrate.streams.get_by_name(NAME_ASSISTANT_RESPONSE)
    if stream is None:
        _substrate.log.warning(
            "substrate.hook.unknown_stream hook=on_assistant_response stream=%s",
            NAME_ASSISTANT_RESPONSE,
        )
        return
    await commit_slice(
        _substrate,
        stream_id=stream.stream_id,
        payload=text,
        event_time_world=t_event,
        metadata={"session_id": session_id, "model": model},
    )


def on_assistant_response(
    session_id: str,
    model: str,
    text: str,
    t_event: datetime,
) -> None:
    if _substrate is None:
        return None
    import hermes_db

    try:
        hermes_db.run_sync(
            on_assistant_response_async(session_id, model, text, t_event)
        )
    except Exception:
        _substrate.log.exception(
            "substrate.hook.error hook=on_assistant_response"
        )


# ── tool_call / tool_result ────────────────────────────────────────────────


@_guard("on_tool_call")
async def on_tool_call_async(
    session_id: str,
    tool_name: str,
    args: dict,
    t_event: datetime,
) -> None:
    """Emit a STRUCTURED_EVENT slice on ``hermes.self_action.tool_call``."""
    from substrate.l0 import commit_slice

    stream = await _substrate.streams.get_by_name(NAME_TOOL_CALL)
    if stream is None:
        _substrate.log.warning(
            "substrate.hook.unknown_stream hook=on_tool_call stream=%s",
            NAME_TOOL_CALL,
        )
        return
    await commit_slice(
        _substrate,
        stream_id=stream.stream_id,
        payload={"tool": tool_name, "args": args or {}},
        event_time_world=t_event,
        metadata={"session_id": session_id},
    )


def on_tool_call(
    session_id: str,
    tool_name: str,
    args: dict,
    t_event: datetime,
) -> None:
    if _substrate is None:
        return None
    import hermes_db

    try:
        hermes_db.run_sync(
            on_tool_call_async(session_id, tool_name, args, t_event)
        )
    except Exception:
        _substrate.log.exception("substrate.hook.error hook=on_tool_call")


@_guard("on_tool_result")
async def on_tool_result_async(
    session_id: str,
    tool_name: str,
    result: Any,
    error: Any,
    t_event: datetime,
) -> None:
    """Emit a STRUCTURED_EVENT slice on ``hermes.self_state.tool_result``.

    ``result`` and ``error`` are passed through :func:`_summarize` so
    large strings/bytes don't bloat the JSONB row.
    """
    from substrate.l0 import commit_slice

    stream = await _substrate.streams.get_by_name(NAME_TOOL_RESULT)
    if stream is None:
        _substrate.log.warning(
            "substrate.hook.unknown_stream hook=on_tool_result stream=%s",
            NAME_TOOL_RESULT,
        )
        return
    await commit_slice(
        _substrate,
        stream_id=stream.stream_id,
        payload={
            "tool": tool_name,
            "result": _summarize(result),
            "error": _summarize(error),
        },
        event_time_world=t_event,
        metadata={"session_id": session_id},
    )


def on_tool_result(
    session_id: str,
    tool_name: str,
    result: Any,
    error: Any,
    t_event: datetime,
) -> None:
    if _substrate is None:
        return None
    import hermes_db

    try:
        hermes_db.run_sync(
            on_tool_result_async(session_id, tool_name, result, error, t_event)
        )
    except Exception:
        _substrate.log.exception("substrate.hook.error hook=on_tool_result")


# ── subagent_spawn / subagent_return ───────────────────────────────────────


@_guard("on_subagent_spawn")
async def on_subagent_spawn_async(
    parent_session_id: str,
    child_id: str,
    goal: str,
    t_event: datetime,
) -> None:
    """Emit a STRUCTURED_EVENT slice on
    ``hermes.self_action.subagent_spawn``."""
    from substrate.l0 import commit_slice

    stream = await _substrate.streams.get_by_name(NAME_SUBAGENT_SPAWN)
    if stream is None:
        _substrate.log.warning(
            "substrate.hook.unknown_stream hook=on_subagent_spawn stream=%s",
            NAME_SUBAGENT_SPAWN,
        )
        return
    await commit_slice(
        _substrate,
        stream_id=stream.stream_id,
        payload={"child_id": child_id, "goal": _summarize(goal)},
        event_time_world=t_event,
        metadata={"parent_session_id": parent_session_id},
    )


def on_subagent_spawn(
    parent_session_id: str,
    child_id: str,
    goal: str,
    t_event: datetime,
) -> None:
    if _substrate is None:
        return None
    import hermes_db

    try:
        hermes_db.run_sync(
            on_subagent_spawn_async(parent_session_id, child_id, goal, t_event)
        )
    except Exception:
        _substrate.log.exception("substrate.hook.error hook=on_subagent_spawn")


@_guard("on_subagent_return")
async def on_subagent_return_async(
    parent_session_id: str,
    child_id: str,
    summary: str,
    t_event: datetime,
) -> None:
    """Emit a STRUCTURED_EVENT slice on
    ``hermes.self_state.subagent_return``."""
    from substrate.l0 import commit_slice

    stream = await _substrate.streams.get_by_name(NAME_SUBAGENT_RETURN)
    if stream is None:
        _substrate.log.warning(
            "substrate.hook.unknown_stream hook=on_subagent_return stream=%s",
            NAME_SUBAGENT_RETURN,
        )
        return
    await commit_slice(
        _substrate,
        stream_id=stream.stream_id,
        payload={"child_id": child_id, "summary": _summarize(summary)},
        event_time_world=t_event,
        metadata={"parent_session_id": parent_session_id},
    )


def on_subagent_return(
    parent_session_id: str,
    child_id: str,
    summary: str,
    t_event: datetime,
) -> None:
    if _substrate is None:
        return None
    import hermes_db

    try:
        hermes_db.run_sync(
            on_subagent_return_async(parent_session_id, child_id, summary, t_event)
        )
    except Exception:
        _substrate.log.exception(
            "substrate.hook.error hook=on_subagent_return"
        )


# ── session_start / session_end ────────────────────────────────────────────


@_guard("on_session_start")
async def on_session_start_async(
    session_id: str,
    source: str,
    model: str,
    t_event: datetime,
    *,
    conn: "Optional[asyncpg.Connection]" = None,
) -> None:
    """Emit a STRUCTURED_EVENT slice on
    ``hermes.self_state.session_lifecycle``.

    If ``conn`` is passed, the substrate INSERT joins the caller's
    transaction so the session row + the substrate slice commit
    atomically. This is the intended use from
    ``SessionDB.create_session`` (Phase A spec §7).
    """
    from substrate.l0 import commit_slice

    stream = await _substrate.streams.get_by_name(NAME_SESSION_LIFECYCLE)
    if stream is None:
        _substrate.log.warning(
            "substrate.hook.unknown_stream hook=on_session_start stream=%s",
            NAME_SESSION_LIFECYCLE,
        )
        return
    await commit_slice(
        _substrate,
        stream_id=stream.stream_id,
        payload={
            "event": "session_start",
            "session_id": session_id,
            "source": source,
            "model": model,
        },
        event_time_world=t_event,
        metadata={"session_id": session_id, "source": source, "model": model},
        conn=conn,
    )


def on_session_start(
    session_id: str,
    source: str,
    model: str,
    t_event: datetime,
) -> None:
    """Sync facade. NOTE: no ``conn`` parameter — sync callers can't
    meaningfully share a transaction with the substrate write (the
    asyncpg.Connection is async-only)."""
    if _substrate is None:
        return None
    import hermes_db

    try:
        hermes_db.run_sync(
            on_session_start_async(session_id, source, model, t_event)
        )
    except Exception:
        _substrate.log.exception("substrate.hook.error hook=on_session_start")


@_guard("on_session_end")
async def on_session_end_async(
    session_id: str,
    reason: str,
    t_event: datetime,
) -> None:
    """Emit a STRUCTURED_EVENT slice on
    ``hermes.self_state.session_lifecycle``."""
    from substrate.l0 import commit_slice

    stream = await _substrate.streams.get_by_name(NAME_SESSION_LIFECYCLE)
    if stream is None:
        _substrate.log.warning(
            "substrate.hook.unknown_stream hook=on_session_end stream=%s",
            NAME_SESSION_LIFECYCLE,
        )
        return
    await commit_slice(
        _substrate,
        stream_id=stream.stream_id,
        payload={
            "event": "session_end",
            "session_id": session_id,
            "reason": reason,
        },
        event_time_world=t_event,
        metadata={"session_id": session_id, "reason": reason},
    )

    # Steady-state observability — one INFO line per session that
    # summarises perception coverage. Operators scanning agent.log can
    # confirm at a glance that the substrate is hearing the
    # conversation without trawling per-message DEBUG traces. Best
    # effort: query failures fall back to a minimal summary so the
    # session_end emit above isn't blocked.
    try:
        import hermes_db
        async with hermes_db.connection() as conn:
            rows = await conn.fetch(
                """
                SELECT st.name, count(sl.slice_id) AS n
                  FROM substrate_streams st
                  LEFT JOIN substrate_slices sl
                    ON sl.stream_id = st.stream_id
                   AND sl.metadata->>'session_id' = $1
                 WHERE st.name IN (
                       'thoth.world.user_message.cli',
                       'thoth.world.user_message.telegram',
                       'thoth.world.user_message.discord',
                       'thoth.world.user_message.slack',
                       'thoth.world.user_message.whatsapp',
                       'thoth.world.user_message.signal',
                       'thoth.world.user_message.acp',
                       'thoth.self_action.assistant_response',
                       'thoth.self_action.tool_call',
                       'thoth.self_state.tool_result'
                 )
                 GROUP BY st.name
                 HAVING count(sl.slice_id) > 0
                """,
                session_id,
            )
        counts = ", ".join(f"{r['name'].split('.')[-1]}={r['n']}" for r in rows) or "none"
        _substrate.log.info(
            "substrate.session.summary session=%s reason=%s slices: %s",
            session_id, reason, counts,
        )
    except Exception:
        _substrate.log.warning(
            "substrate.session.summary query failed for session=%s",
            session_id, exc_info=True,
        )


def on_session_end(
    session_id: str,
    reason: str,
    t_event: datetime,
) -> None:
    if _substrate is None:
        return None
    import hermes_db

    try:
        hermes_db.run_sync(
            on_session_end_async(session_id, reason, t_event)
        )
    except Exception:
        _substrate.log.exception("substrate.hook.error hook=on_session_end")


# ── cron_fire ──────────────────────────────────────────────────────────────


@_guard("on_cron_fire")
async def on_cron_fire_async(job_id: str, t_event: datetime) -> None:
    """Emit a STRUCTURED_EVENT slice on
    ``hermes.self_state.cron_dispatch``."""
    from substrate.l0 import commit_slice

    stream = await _substrate.streams.get_by_name(NAME_CRON_DISPATCH)
    if stream is None:
        _substrate.log.warning(
            "substrate.hook.unknown_stream hook=on_cron_fire stream=%s",
            NAME_CRON_DISPATCH,
        )
        return
    await commit_slice(
        _substrate,
        stream_id=stream.stream_id,
        payload={"job_id": job_id},
        event_time_world=t_event,
        metadata={"job_id": job_id},
    )


def on_cron_fire(job_id: str, t_event: datetime) -> None:
    """Cron is sync at the call site — this is the primary entry point."""
    if _substrate is None:
        return None
    import hermes_db

    try:
        hermes_db.run_sync(on_cron_fire_async(job_id, t_event))
    except Exception:
        _substrate.log.exception("substrate.hook.error hook=on_cron_fire")


__all__ = [
    "_bind",
    "_unbind",
    "on_assistant_response",
    "on_assistant_response_async",
    "on_cron_fire",
    "on_cron_fire_async",
    "on_session_end",
    "on_session_end_async",
    "on_session_start",
    "on_session_start_async",
    "on_subagent_return",
    "on_subagent_return_async",
    "on_subagent_spawn",
    "on_subagent_spawn_async",
    "on_tool_call",
    "on_tool_call_async",
    "on_tool_result",
    "on_tool_result_async",
    "on_user_message",
    "on_user_message_async",
]
