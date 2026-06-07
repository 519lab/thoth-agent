"""Tests for ``substrate.events.hermes_hooks``.

Covers spec §6 + §11.2 hook test cases:
* Each hook emits exactly one slice on the expected stream with the
  right payload + metadata.
* ``on_session_start_async`` shares the caller's transaction when
  ``conn=`` is passed (atomicity assertion).
* Hooks called before ``Substrate.boot()`` (no ``_bind`` yet) are
  silent no-ops.
* Hook failures inside ``commit_slice`` are caught and logged, not
  re-raised to the caller.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.events import hermes_hooks


@pytest_asyncio.fixture
async def booted_substrate(thoth_db_initialized):
    """A fully booted substrate (sub-agents disabled — tests don't want
    the Sentinel loop racing against assertions). Hook module is bound
    in ``Substrate.boot()``.
    """
    import thoth_db

    sub = await Substrate.boot(start_subagents=False)
    yield sub
    await sub.shutdown()
    # Reset the binding for subsequent tests that exercise the pre-boot
    # no-op behavior. ``shutdown`` already does this, but the explicit
    # call here makes the test ordering robust if shutdown is skipped.
    hermes_hooks._unbind()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Pre-boot no-op semantics — hooks called before ``_bind`` return None
# without raising. This is the design invariant from spec §6.1.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_noop_before_boot():
    # Ensure the binding is clear (other tests in this file boot the
    # substrate and bind it).
    hermes_hooks._unbind()
    result = await hermes_hooks.on_user_message_async(
        "s1", "cli", "hi", _now_utc()
    )
    assert result is None  # silent no-op


def test_sync_hook_noop_before_boot():
    hermes_hooks._unbind()
    # Returns None, doesn't raise even though substrate isn't booted.
    assert hermes_hooks.on_user_message("s1", "cli", "hi", _now_utc()) is None


# ---------------------------------------------------------------------------
# Per-hook happy paths.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_user_message_async_writes_slice(booted_substrate):
    """``on_user_message_async`` writes one slice on
    ``thoth.world.user_message.<source>`` with TEXT modality wrap and
    session/source metadata.
    """
    import thoth_db

    await hermes_hooks.on_user_message_async(
        "sess-1", "cli", "hello from a test", _now_utc()
    )

    async with thoth_db.connection() as conn:
        rows = await conn.fetch(
            """
            SELECT sl.payload, sl.metadata, st.name AS stream_name
              FROM substrate_slices sl
              JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE st.name = 'thoth.world.user_message.cli'
            """
        )
    assert len(rows) == 1
    assert rows[0]["payload"] == {"text": "hello from a test"}
    assert rows[0]["metadata"] == {"session_id": "sess-1", "source": "cli"}


@pytest.mark.asyncio
async def test_on_assistant_response_async_writes_slice(booted_substrate):
    import thoth_db

    await hermes_hooks.on_assistant_response_async(
        "sess-2", "claude-sonnet-4-6", "ok done", _now_utc()
    )
    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT sl.payload, sl.metadata
              FROM substrate_slices sl
              JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE st.name = 'thoth.self_action.assistant_response'
            """
        )
    assert row is not None
    assert row["payload"] == {"text": "ok done"}
    assert row["metadata"]["model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_on_tool_call_and_result_pair(booted_substrate):
    """One slice on tool_call + one slice on tool_result."""
    import thoth_db

    t = _now_utc()
    await hermes_hooks.on_tool_call_async(
        "sess-3", "bash", {"cmd": "ls"}, t
    )
    await hermes_hooks.on_tool_result_async(
        "sess-3", "bash", "file1\nfile2\n", None, t
    )

    async with thoth_db.connection() as conn:
        call_row = await conn.fetchrow(
            """
            SELECT payload FROM substrate_slices sl
             JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE st.name = 'thoth.self_action.tool_call'
            """
        )
        result_row = await conn.fetchrow(
            """
            SELECT payload FROM substrate_slices sl
             JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE st.name = 'thoth.self_state.tool_result'
            """
        )
    assert call_row["payload"] == {"tool": "bash", "args": {"cmd": "ls"}}
    assert result_row["payload"] == {
        "tool": "bash",
        "result": "file1\nfile2\n",
        "error": None,
    }


@pytest.mark.asyncio
async def test_on_tool_result_summarises_large_result(booted_substrate):
    """``_summarize`` truncates long strings + appends length suffix."""
    import thoth_db

    big = "x" * 1000
    await hermes_hooks.on_tool_result_async(
        "sess-4", "search", big, None, _now_utc()
    )
    async with thoth_db.connection() as conn:
        payload = await conn.fetchval(
            """
            SELECT payload FROM substrate_slices sl
             JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE st.name = 'thoth.self_state.tool_result'
            """
        )
    # Truncated to 256 chars + "(1000 chars)" suffix.
    assert payload["result"].startswith("x" * 256)
    assert "1000 chars" in payload["result"]


@pytest.mark.asyncio
async def test_on_subagent_spawn_and_return(booted_substrate):
    import thoth_db

    t = _now_utc()
    await hermes_hooks.on_subagent_spawn_async(
        "parent-1", "child-A", "investigate bug 42", t
    )
    await hermes_hooks.on_subagent_return_async(
        "parent-1", "child-A", "fixed and verified", t
    )
    async with thoth_db.connection() as conn:
        spawn = await conn.fetchrow(
            """
            SELECT payload FROM substrate_slices sl
             JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE st.name = 'thoth.self_action.subagent_spawn'
            """
        )
        ret = await conn.fetchrow(
            """
            SELECT sl.payload, sl.metadata FROM substrate_slices sl
             JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE st.name = 'thoth.self_state.subagent_return'
            """
        )
    assert spawn["payload"] == {"child_id": "child-A", "goal": "investigate bug 42"}
    assert ret["payload"] == {"child_id": "child-A", "summary": "fixed and verified"}
    # The return summary carries real NL worth consolidating: it's keyed to
    # the parent's session_id so the Parser folds it into the parent's
    # batch (parent_session_id retained for provenance).
    assert ret["metadata"]["session_id"] == "parent-1"
    assert ret["metadata"]["parent_session_id"] == "parent-1"


@pytest.mark.asyncio
async def test_on_session_start_async_writes_slice(booted_substrate):
    import thoth_db

    await hermes_hooks.on_session_start_async(
        "sess-5", "discord", "claude-haiku-4-5", _now_utc()
    )
    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT sl.payload, sl.metadata FROM substrate_slices sl
             JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE st.name = 'thoth.self_state.session_lifecycle'
               AND sl.payload->>'event' = 'session_start'
            """
        )
    assert row is not None
    assert row["payload"]["session_id"] == "sess-5"
    assert row["metadata"]["model"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_on_session_end_async_writes_slice(booted_substrate):
    import thoth_db

    await hermes_hooks.on_session_end_async("sess-6", "user_quit", _now_utc())
    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT payload FROM substrate_slices sl
             JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE st.name = 'thoth.self_state.session_lifecycle'
               AND payload->>'event' = 'session_end'
            """
        )
    assert row is not None
    assert row["payload"]["reason"] == "user_quit"


@pytest.mark.asyncio
async def test_on_cron_fire_async_writes_slice(booted_substrate):
    import thoth_db

    await hermes_hooks.on_cron_fire_async("job-7", _now_utc())
    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT sl.payload, sl.metadata, sl.consolidation_state
              FROM substrate_slices sl
             JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE st.name = 'thoth.self_state.cron_dispatch'
            """
        )
    assert row is not None
    assert row["payload"] == {"job_id": "job-7"}
    # Cron events are session-less and unparseable, so they're born
    # 'consolidated' — never entering the (undrainable) parse backlog.
    assert row["consolidation_state"] == "consolidated"
    assert "session_id" not in (row["metadata"] or {})


@pytest.mark.asyncio
async def test_on_cron_fire_includes_name_and_schedule(booted_substrate):
    """The bare job_id is an opaque hex; the perception carries the human
    job name + schedule so it's meaningful in recall/self-model."""
    import thoth_db

    await hermes_hooks.on_cron_fire_async(
        "abc123", _now_utc(), job_name="nightly digest", schedule="every day at 06:00"
    )
    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT sl.payload FROM substrate_slices sl
             JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE st.name = 'thoth.self_state.cron_dispatch'
               AND sl.payload->>'job_id' = 'abc123'
            """
        )
    assert row is not None
    assert row["payload"] == {
        "job_id": "abc123",
        "name": "nightly digest",
        "schedule": "every day at 06:00",
    }


@pytest.mark.asyncio
async def test_on_cron_fire_excluded_from_parse_backlog(booted_substrate):
    """A born-consolidated cron slice is not counted as awaiting-parse:
    the Parser's selector keys on unconsolidated + session_id, and this
    slice is neither."""
    import thoth_db

    await hermes_hooks.on_cron_fire_async("job-8", _now_utc())
    async with thoth_db.connection() as conn:
        backlog = await conn.fetchval(
            """
            SELECT COUNT(*) FROM substrate_slices sl
             JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE st.name = 'thoth.self_state.cron_dispatch'
               AND sl.consolidation_state = 'unconsolidated'
               AND sl.metadata->>'session_id' IS NOT NULL
            """
        )
    assert backlog == 0


# ---------------------------------------------------------------------------
# Shared-txn semantics — on_session_start_async with conn=.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_session_start_shares_txn(booted_substrate):
    """Passing ``conn=`` joins the caller's transaction so a ROLLBACK
    discards both the (caller's) work and the slice INSERT.

    This is the atomicity contract exercised by
    ``SessionDB.create_session`` in production wiring.
    """
    import thoth_db

    try:
        async with thoth_db.transaction() as conn:
            await hermes_hooks.on_session_start_async(
                "sess-rollback",
                "cli",
                "model-x",
                _now_utc(),
                conn=conn,
            )
            # Force a ROLLBACK by raising inside the txn block.
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass

    async with thoth_db.connection() as conn:
        count = await conn.fetchval(
            """
            SELECT count(*) FROM substrate_slices sl
             JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE st.name = 'thoth.self_state.session_lifecycle'
               AND sl.metadata->>'session_id' = 'sess-rollback'
            """
        )
    assert count == 0, "slice should have been rolled back with outer txn"


# ---------------------------------------------------------------------------
# Error swallowing — hook never raises to caller.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_swallows_errors(booted_substrate, monkeypatch):
    """When the underlying ``commit_slice`` raises, the hook logs and
    returns None — the Thoth caller MUST NOT see the exception.
    """
    import substrate.events.hermes_hooks as mod

    async def _boom(*args, **kwargs):
        raise RuntimeError("commit_slice exploded in tests")

    monkeypatch.setattr("substrate.l0.commit_slice", _boom)

    # Should not raise; should return None.
    result = await mod.on_user_message_async("s", "cli", "x", _now_utc())
    assert result is None


# ---------------------------------------------------------------------------
# Unknown stream name — log + skip, don't raise.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_skips_when_stream_missing(thoth_db_initialized):
    """If the substrate is booted but the expected stream isn't
    registered (a corrupt-deploy edge case), the hook logs a warning
    and returns None rather than raising. The unknown-stream branch is
    inside ``commit_slice``'s caller, not ``commit_slice`` itself.
    """
    import thoth_db
    from substrate.facade import Substrate

    # Construct a substrate that DIDN'T auto-register §9 streams.
    sub = Substrate.from_pool(thoth_db.pool())
    hermes_hooks._bind(sub)
    try:
        result = await hermes_hooks.on_user_message_async(
            "s", "cli", "hi", _now_utc()
        )
        assert result is None
    finally:
        hermes_hooks._unbind()
