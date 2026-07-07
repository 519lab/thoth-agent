"""Retention/pruning for the operational-log tables (#286)."""

from __future__ import annotations

import pytest

from substrate.telemetry import prune


@pytest.mark.asyncio
async def test_prune_removes_aged_rows_keeps_recent(thoth_db_initialized):
    """Age-based sweep drops telemetry/conductor_log rows past their window,
    with a shorter window for high-volume kinds, and keeps recent rows."""
    import thoth_db

    async with thoth_db.connection() as conn:
        await conn.execute(
            """
            INSERT INTO substrate_telemetry (agent, event, payload, at) VALUES
              ('curator','curator.release','{}', now() - interval '40 days'),
              ('curator','curator.release','{}', now() - interval '10 days'),
              ('conductor','conductor.dialed','{}', now() - interval '40 days'),
              ('conductor','conductor.dialed','{}', now() - interval '10 days'),
              ('conductor','conductor.dialed','{}', now() - interval '2 days')
            """
        )
        await conn.execute(
            """
            INSERT INTO substrate_conductor_log (backlog_ratio, forecast, targets, at)
            VALUES (0.1, 0.1, '{}', now() - interval '40 days'),
                   (0.1, 0.1, '{}', now() - interval '5 days')
            """
        )

        result = await prune(conn)

        stale_general = await conn.fetchval(
            "SELECT count(*) FROM substrate_telemetry "
            "WHERE event <> 'conductor.dialed' AND at < now() - interval '30 days'"
        )
        stale_hot = await conn.fetchval(
            "SELECT count(*) FROM substrate_telemetry "
            "WHERE event = 'conductor.dialed' AND at < now() - interval '7 days'"
        )
        stale_clog = await conn.fetchval(
            "SELECT count(*) FROM substrate_conductor_log "
            "WHERE at < now() - interval '30 days'"
        )
        recent_general = await conn.fetchval(
            "SELECT count(*) FROM substrate_telemetry "
            "WHERE event = 'curator.release' AND at > now() - interval '15 days'"
        )
        recent_hot = await conn.fetchval(
            "SELECT count(*) FROM substrate_telemetry "
            "WHERE event = 'conductor.dialed' AND at > now() - interval '3 days'"
        )

    # Nothing past its retention window survives.
    assert stale_general == 0
    assert stale_hot == 0
    assert stale_clog == 0
    # Recent rows are untouched (10-day general < 30d, 2-day hot < 7d).
    assert recent_general == 1
    assert recent_hot == 1
    # The two aged conductor.dialed rows (40d, 10d) were pruned on the 7d window.
    assert result["telemetry:conductor.dialed"] == 2
    assert result["conductor_log"] == 1


@pytest.mark.asyncio
async def test_prune_retention_disabled_with_negative(thoth_db_initialized, monkeypatch):
    """A negative retention disables that sweep (nothing deleted)."""
    import thoth_db

    monkeypatch.setenv("THOTH_SUBSTRATE_CONDUCTOR_LOG_RETENTION_DAYS", "-1")
    async with thoth_db.connection() as conn:
        await conn.execute(
            "INSERT INTO substrate_conductor_log (backlog_ratio, forecast, targets, at) "
            "VALUES (0.1, 0.1, '{}', now() - interval '400 days')"
        )
        result = await prune(conn)
        remaining = await conn.fetchval(
            "SELECT count(*) FROM substrate_conductor_log WHERE at < now() - interval '100 days'"
        )
    assert result["conductor_log"] == 0
    assert remaining == 1
