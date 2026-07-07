"""Non-perceptual operational-telemetry sink — :func:`write`.

This is where the substrate records its *own* operational decisions
(Conductor dials, Sentinel batch summaries, Curator releases/alarms,
Reflector/Dreamer/Critic/Associator/PatternFinder/Summarizer/Parser
activity, force-reject audits). It is the deliberate counterpart to
``substrate.l0.api.commit_slice``:

* ``commit_slice`` writes **perception** — a slice the awareness loop
  ingests, parses, consolidates, recalls.
* ``telemetry.write`` writes **operational telemetry** — an append-only
  row in ``substrate_telemetry`` that the awareness loop *never reads*.

Why this exists: these events used to be committed as slices on the
perceptual ``substrate.self_state`` stream. Because the Conductor's
backlog forecast counted every ``passed + unconsolidated`` slice — and
audit slices carry no ``session_id`` so the Parser could never drain
them — that closed a self-sustaining feedback loop (2026-05-26→27 prod
incident: 414k ghost slices). Routing them here keeps them out of L0
entirely: no ``awaiting_parse`` increment, no consolidation backlog, no
Curator pending set, never visible to Sentinel/Conductor as input.

The schema-level guard that keeps a *future* component from re-opening
the loop is :func:`substrate.storage.streams.is_perceptual` — anything
on a ``substrate.*`` stream is excluded from awareness-loop queries.
This module is the positive destination for those excluded events.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg
    from substrate.facade import Substrate


_INSERT_SQL = """
    INSERT INTO substrate_telemetry (agent, event, payload, at)
    VALUES ($1, $2, $3, COALESCE($4, now()))
"""


async def write(
    substrate: "Substrate",
    *,
    agent: str,
    event: str,
    payload: Optional[dict] = None,
    at: Optional[datetime] = None,
    conn: "Optional[asyncpg.Connection]" = None,
) -> None:
    """Append one operational-telemetry row. Non-perceptual by design.

    ``agent`` — the emitting sub-agent's ``name`` (e.g. ``"conductor"``).
    ``event`` — the event kind (e.g. ``"conductor.dialed"``).
    ``payload`` — event-specific fields as a JSON-compatible dict. The
        ``event`` kind and the row timestamp are columns, so the payload
        should NOT duplicate them.
    ``at`` — event time; defaults to the PG server clock (``now()``).
    ``conn`` — optional connection to run the INSERT on a caller-owned
        transaction; otherwise a connection is acquired from the pool.

    Unlike ``commit_slice`` this never touches ``substrate_slices`` — so a
    telemetry write can never increment the consolidation backlog, enter
    the Curator's pending set, or be read back as perception. Callers that
    want best-effort semantics (the historical emit sites) should wrap the
    call in their own ``try/except``, matching the prior ``commit_slice``
    audit-emit behaviour.
    """
    row_payload = payload or {}
    if conn is not None:
        await conn.execute(_INSERT_SQL, agent, event, row_payload, at)
        return
    async with substrate.pool.acquire() as own_conn:
        await own_conn.execute(_INSERT_SQL, agent, event, row_payload, at)


# ---------------------------------------------------------------------------
# Retention (#286). The two operational-log tables (substrate_telemetry,
# substrate_conductor_log) are append-only and had no pruning — ~30% of the
# live DB after 3.5 weeks. Age-based deletion, run from the daily
# partition-maintenance cadence, keeps them bounded. High-volume kinds
# (conductor.dialed) get a shorter window than the rest.
# ---------------------------------------------------------------------------

_DEFAULT_TELEMETRY_RETENTION_DAYS = 30
_DEFAULT_CONDUCTOR_LOG_RETENTION_DAYS = 30
_DEFAULT_HOT_EVENTS = ("conductor.dialed",)
_DEFAULT_HOT_RETENTION_DAYS = 7


def _int_env(name: str, default: int) -> int:
    import os

    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


async def _delete_aged(
    conn: "asyncpg.Connection",
    table: str,
    days: int,
    *,
    event: Optional[str] = None,
    batch: int = 20000,
    max_total: int = 500000,
) -> int:
    """Bounded, batched age-based delete. Removes up to ``max_total`` rows this
    call in ``batch``-sized chunks (short locks per statement); the daily
    cadence clears any remainder. ``ctid`` gives an efficient LIMIT delete over
    the ``at`` index. Retention below 0 days is treated as "disabled"."""
    if days < 0:
        return 0
    where = "at < now() - make_interval(days => $1)"
    params: list = [int(days)]
    if event is not None:
        where += " AND event = $2"
        params.append(event)
    total = 0
    while total < max_total:
        tag = await conn.execute(
            f"DELETE FROM {table} WHERE ctid IN "
            f"(SELECT ctid FROM {table} WHERE {where} LIMIT {int(batch)})",
            *params,
        )
        n = int(tag.rsplit(" ", 1)[-1]) if tag.startswith("DELETE") else 0
        total += n
        if n < batch:
            break
    return total


async def prune(conn: "asyncpg.Connection") -> dict:
    """Age-based retention sweep over the operational-log tables. Returns
    per-target deletion counts. Best-effort — callers wrap in try/except.

    Windows are env-configurable (days): ``THOTH_SUBSTRATE_TELEMETRY_RETENTION_DAYS``
    (default 30), ``THOTH_SUBSTRATE_TELEMETRY_HOT_RETENTION_DAYS`` (default 7,
    for the high-volume kinds in ``_DEFAULT_HOT_EVENTS``), and
    ``THOTH_SUBSTRATE_CONDUCTOR_LOG_RETENTION_DAYS`` (default 30). Set any to a
    negative value to disable that sweep.
    """
    telemetry_days = _int_env(
        "THOTH_SUBSTRATE_TELEMETRY_RETENTION_DAYS", _DEFAULT_TELEMETRY_RETENTION_DAYS
    )
    hot_days = _int_env(
        "THOTH_SUBSTRATE_TELEMETRY_HOT_RETENTION_DAYS", _DEFAULT_HOT_RETENTION_DAYS
    )
    conductor_days = _int_env(
        "THOTH_SUBSTRATE_CONDUCTOR_LOG_RETENTION_DAYS",
        _DEFAULT_CONDUCTOR_LOG_RETENTION_DAYS,
    )
    deleted: dict = {}
    # Hot kinds on the shorter window first; the general sweep then trims the
    # rest (and any hot remnants older than the general window — harmless).
    for ev in _DEFAULT_HOT_EVENTS:
        deleted[f"telemetry:{ev}"] = await _delete_aged(
            conn, "substrate_telemetry", hot_days, event=ev
        )
    deleted["telemetry"] = await _delete_aged(
        conn, "substrate_telemetry", telemetry_days
    )
    deleted["conductor_log"] = await _delete_aged(
        conn, "substrate_conductor_log", conductor_days
    )
    return deleted


__all__ = ["write", "prune"]
