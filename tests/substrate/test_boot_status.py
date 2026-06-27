"""Tests for substrate boot-status recording + the inspect surface.

``bootstrap_substrate`` records every boot attempt (success or failure)
to the ``state_meta`` KV table and an in-process cache, so a writer-mode
boot failure — which silently stops all perception — is visible via
``thoth substrate boot`` and the default summary instead of only in
process logs.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest
import pytest_asyncio

import thoth_bootstrap
from substrate.cli import inspect as inspect_mod


@pytest.fixture(autouse=True)
def _reset_bootstrap_globals():
    """Save/restore the bootstrap module globals so a boot attempted in one
    test doesn't leak its ``_substrate_booted`` short-circuit (or cached
    status) into the next."""
    saved_booted = thoth_bootstrap._substrate_booted
    saved_handle = thoth_bootstrap._substrate_handle
    saved_status = dict(thoth_bootstrap._last_boot_status)
    thoth_bootstrap._substrate_booted = False
    thoth_bootstrap._substrate_handle = None
    thoth_bootstrap._last_boot_status = {}
    try:
        yield
    finally:
        thoth_bootstrap._substrate_booted = saved_booted
        thoth_bootstrap._substrate_handle = saved_handle
        thoth_bootstrap._last_boot_status = saved_status


async def _clear_boot_rows(conn) -> None:
    await conn.execute(
        "DELETE FROM state_meta WHERE key LIKE $1",
        thoth_bootstrap._BOOT_STATUS_KEY_PREFIX + "%",
    )


# ---------------------------------------------------------------------------
# _record_boot_status / get_boot_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_boot_status_writes_state_meta(thoth_db_initialized):
    import thoth_db

    await thoth_bootstrap._record_boot_status("writer", ok=True)

    async with thoth_db.connection() as conn:
        value = await conn.fetchval(
            "SELECT value FROM state_meta WHERE key = $1",
            thoth_bootstrap._BOOT_STATUS_KEY_PREFIX + "writer",
        )
    import json

    parsed = json.loads(value)
    assert parsed["ok"] is True
    assert parsed["mode"] == "writer"
    assert parsed["pid"] > 0
    assert parsed["host"]
    # In-process cache mirrors the write.
    assert thoth_bootstrap.get_boot_status("writer")["ok"] is True
    assert "writer" in thoth_bootstrap.get_boot_status()


@pytest.mark.asyncio
async def test_record_boot_status_never_raises_without_db(monkeypatch):
    """If the DB write fails, the status is still cached in-process and no
    exception escapes — recording must never turn a good boot bad."""
    import thoth_db

    def _broken_connection():
        raise RuntimeError("pool is down")

    monkeypatch.setattr(thoth_db, "connection", _broken_connection)
    # Must not raise despite the broken pool.
    await thoth_bootstrap._record_boot_status(
        "worker", ok=False, error_text="boom"
    )
    cached = thoth_bootstrap.get_boot_status("worker")
    assert cached["ok"] is False
    assert cached["error"] == "boom"


# ---------------------------------------------------------------------------
# bootstrap_substrate success / failure recording
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_records_success(thoth_db_initialized, monkeypatch):
    import thoth_db
    import substrate.events.thoth_hooks as hh

    sentinel = object()

    async def _fake_boot_writer(*args, **kwargs):
        return sentinel

    monkeypatch.setattr("substrate.Substrate.boot_writer", _fake_boot_writer)
    # A real writer boot binds the perception hooks; the bind postcondition
    # (issue #180) requires get_bound_substrate() to resolve, so simulate it.
    monkeypatch.setattr(hh, "_substrate", sentinel)

    result = await thoth_bootstrap.bootstrap_substrate(mode="writer")
    assert result is sentinel
    assert thoth_bootstrap.get_boot_status("writer")["ok"] is True

    async with thoth_db.connection() as conn:
        value = await conn.fetchval(
            "SELECT value FROM state_meta WHERE key = $1",
            thoth_bootstrap._BOOT_STATUS_KEY_PREFIX + "writer",
        )
    import json

    assert json.loads(value)["ok"] is True


@pytest.mark.asyncio
async def test_bootstrap_records_failure(thoth_db_initialized, monkeypatch):
    import thoth_db

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated boot failure")

    monkeypatch.setattr("substrate.Substrate.boot_writer", _boom)

    result = await thoth_bootstrap.bootstrap_substrate(mode="writer")
    # Failure is non-fatal for writer mode: returns None, Thoth proceeds.
    assert result is None
    status = thoth_bootstrap.get_boot_status("writer")
    assert status["ok"] is False
    assert "RuntimeError" in status["error"]
    assert "simulated boot failure" in status["error"]

    async with thoth_db.connection() as conn:
        value = await conn.fetchval(
            "SELECT value FROM state_meta WHERE key = $1",
            thoth_bootstrap._BOOT_STATUS_KEY_PREFIX + "writer",
        )
    import json

    assert json.loads(value)["ok"] is False


# ---------------------------------------------------------------------------
# inspect surface
# ---------------------------------------------------------------------------


def test_register_subparser_boot():
    import argparse

    parser = argparse.ArgumentParser(prog="thoth")
    sub = parser.add_subparsers(dest="command")
    inspect_mod.register_subparser(sub)
    ns = parser.parse_args(["substrate", "boot"])
    assert callable(ns.func)


def test_age_str_formats():
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    assert inspect_mod._age_str(None, now) == "?"
    assert inspect_mod._age_str("not-a-date", now) == "?"
    assert inspect_mod._age_str(
        (now - timedelta(seconds=10)).isoformat(), now
    ).endswith("s ago")
    assert inspect_mod._age_str(
        (now - timedelta(minutes=5)).isoformat(), now
    ).endswith("m ago")
    assert inspect_mod._age_str(
        (now - timedelta(hours=3)).isoformat(), now
    ).endswith("h ago")


@pytest.mark.asyncio
async def test_print_boot_status_empty(thoth_db_initialized):
    import thoth_db

    async with thoth_db.connection() as conn:
        await _clear_boot_rows(conn)
        buf = io.StringIO()
        with redirect_stdout(buf):
            await inspect_mod._print_boot_status(conn)
    out = buf.getvalue()
    assert "no boot status recorded" in out


@pytest.mark.asyncio
async def test_print_boot_status_failed(thoth_db_initialized):
    import thoth_db
    import json

    async with thoth_db.connection() as conn:
        await _clear_boot_rows(conn)
        await conn.execute(
            "INSERT INTO state_meta (key, value) VALUES ($1, $2)",
            thoth_bootstrap._BOOT_STATUS_KEY_PREFIX + "writer",
            json.dumps(
                {
                    "mode": "writer",
                    "ok": False,
                    "pid": 4242,
                    "host": "h",
                    "error": "RuntimeError: alembic head mismatch",
                    "booted_at": "2026-05-27T11:59:00+00:00",
                }
            ),
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            await inspect_mod._print_boot_status(conn)
    out = buf.getvalue()
    assert "FAILED" in out
    assert "alembic head mismatch" in out
    assert "last boot FAILED for: writer" in out
    # The worker mode never booted → shown as "no boot recorded".
    assert "no boot recorded" in out


@pytest.mark.asyncio
async def test_summary_includes_last_boot_section(thoth_db_initialized):
    import thoth_db
    import json

    async with thoth_db.connection() as conn:
        await _clear_boot_rows(conn)
        await conn.execute(
            "INSERT INTO state_meta (key, value) VALUES ($1, $2)",
            thoth_bootstrap._BOOT_STATUS_KEY_PREFIX + "writer",
            json.dumps(
                {
                    "mode": "writer",
                    "ok": True,
                    "pid": 1,
                    "host": "h",
                    "error": None,
                    "booted_at": "2026-05-27T11:59:00+00:00",
                }
            ),
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            await inspect_mod._print_summary(conn)
    out = buf.getvalue()
    assert "Last boot:" in out
    assert "writer" in out
    assert "OK" in out


# ---------------------------------------------------------------------------
# Writer-boot bind postcondition (issue #180)
#
# A writer boot MUST leave get_bound_substrate() non-None — that's the
# per-process global the SubstrateMemoryProvider reads. If boot_writer returns
# without binding (no exception), bootstrap_substrate must treat it as a
# FAILURE rather than a silent success that leaves recall quietly disabled.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writer_boot_unbound_is_treated_as_failure(monkeypatch):
    """boot_writer returns but get_bound_substrate() is None → bootstrap
    returns None and records ok=False (loud failure, not silent success)."""
    import substrate.events.thoth_hooks as hh

    async def _fake_boot_writer(log=None):
        return object()  # a "substrate", but hooks never bound

    monkeypatch.setattr("substrate.facade.Substrate.boot_writer", _fake_boot_writer)
    # Simulate the silent-unbind: the hook global stays None after boot.
    monkeypatch.setattr(hh, "_substrate", None)

    result = await thoth_bootstrap.bootstrap_substrate(mode="writer")

    assert result is None
    status = thoth_bootstrap.get_boot_status("writer")
    assert status is not None and status["ok"] is False
    # Not cached as booted — a later retry can re-attempt.
    assert thoth_bootstrap._substrate_booted is False


@pytest.mark.asyncio
async def test_writer_boot_bound_succeeds(monkeypatch):
    """boot_writer that leaves get_bound_substrate() non-None → bootstrap
    returns the handle and records ok=True (happy path still works)."""
    import substrate.events.thoth_hooks as hh

    sentinel = object()

    async def _fake_boot_writer(log=None):
        return sentinel

    monkeypatch.setattr("substrate.facade.Substrate.boot_writer", _fake_boot_writer)
    # Simulate a successful bind: the hook global resolves after boot.
    monkeypatch.setattr(hh, "_substrate", sentinel)

    result = await thoth_bootstrap.bootstrap_substrate(mode="writer")

    assert result is sentinel
    status = thoth_bootstrap.get_boot_status("writer")
    assert status is not None and status["ok"] is True
    assert thoth_bootstrap._substrate_booted is True
