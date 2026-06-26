"""Tests for ``substrate.cli.inspect`` — the debug subcommand.

Exercises the printer functions directly against the PG fixture so the
tests don't have to spin up the full ``thoth`` argparse tree. The
``register_subparser`` wiring is smoke-tested separately (it's a thin
shell of argparse calls).
"""

from __future__ import annotations

import argparse
import io
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.cli import inspect as inspect_mod
from substrate.events.thoth_hooks import on_user_message_async


@pytest_asyncio.fixture
async def booted_substrate(thoth_db_initialized):
    """Boot substrate with sub-agents off — the inspect tests are
    deterministic about queue depth, so we don't want Sentinel passing
    slices mid-test.
    """
    sub = await Substrate.boot(start_subagents=False)
    yield sub
    await sub.shutdown()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# register_subparser — smoke-test the argparse wiring.
# ---------------------------------------------------------------------------


def test_register_subparser_adds_inspect_tree():
    """The ``substrate`` parser and its inspection subparsers must be
    addable to a fresh top-level parser without errors. Post-flatten
    (PR #100) the commands sit directly under ``substrate`` rather than
    under a redundant ``inspect`` verb."""
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    inspect_mod.register_subparser(sub)

    # Successful parse of `substrate streams`.
    ns = parser.parse_args(["substrate", "streams"])
    assert ns.command == "substrate"
    assert callable(ns.func)


def test_register_subparser_slices_requires_stream_arg():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    inspect_mod.register_subparser(sub)

    with pytest.raises(SystemExit):
        parser.parse_args(["substrate", "slices"])  # --stream missing


# ---------------------------------------------------------------------------
# Default summary printer — covers the §10.2 output shape.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_print_summary_after_boot_contains_expected_sections(
    booted_substrate,
):
    """The default summary lists streams + slice counts + pending +
    sub-agents headings (spec §10.2)."""
    import thoth_db

    # Emit one user-message slice so the summary has non-zero counts.
    await on_user_message_async("sess-cli-1", "cli", "hello", _now_utc())

    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_summary(conn)
    out = buf.getvalue()

    assert "Substrate state @" in out
    assert "Streams:" in out
    assert "15 registered" in out  # the §9 set plus self_state
    assert "Slices:" in out
    assert "pending" in out
    assert "passed" in out
    assert "Pending queue:" in out
    # Liveness section: booted with subagents off + no worker running, so
    # every agent reads as down and the worker-down warning fires. This is
    # the signal that would have caught the 2026-05-26 outage.
    assert "Sub-agents (liveness):" in out
    assert "sentinel" in out
    assert "worker appears DOWN" in out


@pytest.mark.asyncio
async def test_print_streams_lists_autoregistered(booted_substrate):
    import thoth_db

    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_streams(conn)
    out = buf.getvalue()

    # Header + every §9 stream name appears in the listing.
    assert "name" in out and "modality" in out
    for name in (
        "thoth.world.user_message.cli",
        "thoth.world.user_message.telegram",
        "thoth.self_action.assistant_response",
        "thoth.self_action.tool_call",
        "thoth.self_state.tool_result",
        "thoth.self_state.session_lifecycle",
        "thoth.self_state.cron_dispatch",
        "substrate.self_state",
    ):
        assert name in out, f"stream {name} missing from streams listing"


@pytest.mark.asyncio
async def test_print_slices_for_named_stream(booted_substrate):
    """``inspect slices --stream NAME`` returns the most-recent slices for
    that stream and prints their addresses + payload preview."""
    import thoth_db

    for i in range(3):
        await on_user_message_async(
            f"sess-{i}", "cli", f"msg {i}", _now_utc()
        )

    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_slices(
                conn, stream_name="thoth.world.user_message.cli", limit=10
            )
    out = buf.getvalue()
    assert "Most-recent 3 slices" in out
    # Payload preview contains the wrapped TEXT shape.
    assert "text" in out


@pytest.mark.asyncio
async def test_print_slices_unknown_stream(booted_substrate):
    import thoth_db

    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_slices(
                conn, stream_name="hermes.does.not.exist", limit=5
            )
    out = buf.getvalue()
    assert "no slices" in out


@pytest.mark.asyncio
async def test_print_pending_reports_zero_initially(booted_substrate):
    """With sub-agents off and no commits, pending queue is empty."""
    import thoth_db

    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_pending(conn)
    out = buf.getvalue()
    assert "depth: 0" in out
    assert "no pending slices" in out


@pytest.mark.asyncio
async def test_print_pending_reports_age_after_emit(booted_substrate):
    """A fresh commit shows up with depth=1 and an age in seconds."""
    import thoth_db

    await on_user_message_async("sess-p", "cli", "queued", _now_utc())
    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_pending(conn)
    out = buf.getvalue()
    assert "depth: 1" in out
    assert "oldest age:" in out
    assert "s" in out  # seconds suffix


@pytest.mark.asyncio
async def test_print_profiles_lists_4_seeded(booted_substrate):
    import thoth_db

    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_profiles(conn)
    out = buf.getvalue()
    for profile in ("default-text", "default-structured", "default-binary", "default-signal"):
        assert profile in out


# ---------------------------------------------------------------------------
# Sub-agent liveness — substrate_agent_heartbeat.
# ---------------------------------------------------------------------------


def test_register_subparser_agents():
    """The ``substrate agents`` subcommand parses cleanly."""
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    inspect_mod.register_subparser(sub)
    ns = parser.parse_args(["substrate", "agents"])
    assert ns.command == "substrate"
    assert callable(ns.func)


async def _seed_heartbeat(conn, name, *, age_seconds, level="full", is_sentinel=False):
    await conn.execute(
        """
        INSERT INTO substrate_agent_heartbeat
            (agent_name, pid, host, level, is_sentinel,
             tick_count, started_at, last_beat_at)
        VALUES ($1, 4242, 'testhost', $2, $3, 99, now(),
                now() - ($4 || ' seconds')::interval)
        ON CONFLICT (agent_name) DO UPDATE SET
            last_beat_at = EXCLUDED.last_beat_at, level = EXCLUDED.level
        """,
        name,
        level,
        is_sentinel,
        str(age_seconds),
    )


@pytest.mark.asyncio
async def test_print_agents_all_down_when_no_worker(booted_substrate):
    """With no heartbeats (worker never started), every expected agent
    reads DOWN and the worker-down warning fires."""
    import thoth_db

    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_agents(conn)
    out = buf.getvalue()
    assert "Sub-agent liveness @" in out
    for name in ("sentinel", "curator", "force-reject", "partition-maintenance"):
        assert name in out
    assert "no heartbeat" in out
    assert "worker appears DOWN" in out


@pytest.mark.asyncio
async def test_agent_liveness_classifies_by_age(booted_substrate):
    """Fresh beat → live, ~45s → stale, ~120s → down, missing → down."""
    import thoth_db

    async with thoth_db.connection() as conn:
        await _seed_heartbeat(conn, "sentinel", age_seconds=1, is_sentinel=True)
        await _seed_heartbeat(conn, "curator", age_seconds=45, level="low")
        await _seed_heartbeat(conn, "force-reject", age_seconds=120, level="low")
        # partition-maintenance left absent on purpose.
        rows = await inspect_mod._agent_liveness(conn)

    by_name = {r["name"]: r for r in rows}
    assert by_name["sentinel"]["status"] == "live"
    assert by_name["sentinel"]["is_sentinel"] is True
    assert by_name["curator"]["status"] == "stale"
    assert by_name["force-reject"]["status"] == "down"
    assert by_name["partition-maintenance"]["status"] == "down"
    assert by_name["partition-maintenance"]["age_seconds"] is None
    # Not "all down": sentinel + curator are live/stale.
    assert inspect_mod._all_agents_down(rows) is False


@pytest.mark.asyncio
async def test_print_agents_reports_live(booted_substrate):
    """All four agents beating recently → live + the OK line, no warning."""
    import thoth_db

    async with thoth_db.connection() as conn:
        await _seed_heartbeat(conn, "sentinel", age_seconds=2, is_sentinel=True)
        await _seed_heartbeat(conn, "curator", age_seconds=3, level="low")
        await _seed_heartbeat(conn, "force-reject", age_seconds=4, level="low")
        await _seed_heartbeat(conn, "partition-maintenance", age_seconds=5)

    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_agents(conn)
    out = buf.getvalue()
    assert "live" in out
    assert "reporting heartbeats" in out
    assert "worker appears DOWN" not in out
    # The sentinel floor annotation surfaces.
    assert "sentinel floor=FULL" in out


def test_classify_agent_boundaries():
    """Pure classification helper — no DB."""
    assert inspect_mod._classify_agent(None) == "down"
    assert inspect_mod._classify_agent(0.0) == "live"
    assert inspect_mod._classify_agent(inspect_mod._AGENT_LIVE_S) == "live"
    assert inspect_mod._classify_agent(inspect_mod._AGENT_LIVE_S + 1) == "stale"
    assert inspect_mod._classify_agent(inspect_mod._AGENT_STALE_S) == "stale"
    assert inspect_mod._classify_agent(inspect_mod._AGENT_STALE_S + 1) == "down"


# ---------------------------------------------------------------------------
# Phase C: recall inspect subcommand.
# ---------------------------------------------------------------------------


def test_register_subparser_recall_subtree():
    """The recall subparser tree parses cleanly. Post-flatten
    (PR #100) ``recall`` sits directly under ``substrate``."""
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    inspect_mod.register_subparser(sub)

    # Default 'recall' → summary.
    ns = parser.parse_args(["substrate", "recall"])
    assert callable(ns.func)

    # recall recent --limit 5.
    ns = parser.parse_args(["substrate", "recall", "recent", "--limit", "5"])
    assert ns.limit == 5

    # recall sample requires session-id.
    ns = parser.parse_args(
        ["substrate", "recall", "sample", "--session-id", "sess-A"]
    )
    assert ns.session_id == "sess-A"

    # recall config takes no args.
    ns = parser.parse_args(["substrate", "recall", "config"])
    assert callable(ns.func)


@pytest.mark.asyncio
async def test_print_recall_summary_empty_substrate(booted_substrate):
    """With no recall calls yet, summary still produces the right format."""
    import thoth_db
    from substrate.recall.cli_inspect import print_summary

    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await print_summary(conn)
    out = buf.getvalue()
    assert "Recall state" in out
    assert "calls" in out
    assert "Embedding coverage" in out
    assert "total slices" in out


@pytest.mark.asyncio
async def test_print_recall_summary_after_recall(booted_substrate, monkeypatch):
    """After enqueuing a fake recall log row, summary reflects the count."""
    import thoth_db
    from datetime import datetime, timezone
    from substrate.recall.cli_inspect import print_summary

    async with thoth_db.connection() as conn:
        await conn.execute(
            """
            INSERT INTO substrate_recall_log
                (requested_at, session_id, query_excerpt,
                 candidates_count, composed_count, tokens_used,
                 duration_ms, timed_out, error_text, metadata)
            VALUES (now(), 'sess-x', 'hello', 5, 3, 120, 80, false, NULL, $1::jsonb)
            """,
            '{"embedding_path": "semantic"}',
        )
    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await print_summary(conn)
    out = buf.getvalue()
    assert "calls           1" in out


@pytest.mark.asyncio
async def test_print_recall_recent_after_log_row(booted_substrate):
    """recent prints any seeded log row with the right session id."""
    import thoth_db
    from substrate.recall.cli_inspect import print_recent

    async with thoth_db.connection() as conn:
        await conn.execute(
            """
            INSERT INTO substrate_recall_log
                (requested_at, session_id, query_excerpt,
                 candidates_count, composed_count, tokens_used,
                 duration_ms, timed_out, error_text, metadata)
            VALUES (now(), 'sess-recent', 'q', 1, 1, 10, 5, false, NULL, '{}'::jsonb)
            """
        )
    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await print_recent(conn, limit=10)
    out = buf.getvalue()
    assert "sess-recent" in out


@pytest.mark.asyncio
async def test_print_recall_config(booted_substrate):
    """config dumps the RECALL_* knobs."""
    import thoth_db
    from substrate.recall.cli_inspect import print_config

    buf = io.StringIO()
    async with thoth_db.connection() as conn:
        with redirect_stdout(buf):
            await print_config(conn)
    out = buf.getvalue()
    assert "RECALL_TOKEN_BUDGET" in out
    assert "RECALL_EMBEDDING_MODEL" in out
    assert "THOTH_SUBSTRATE_RECALL" in out
