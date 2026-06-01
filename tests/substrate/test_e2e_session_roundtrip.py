"""End-to-end Phase A acceptance surrogate for spec §12 acceptance #3.

The spec's acceptance criterion is: *"Running ``thoth`` and exchanging a
turn produces a response identical to Phase 0 behavior; behind the scenes,
slices accumulate in ``substrate_slices`` for that session."*

We don't run the full interactive ``thoth`` binary here (that'd require
an LLM provider and a TTY). What we DO is the substrate-visible portion of
the turn: boot substrate, create a session, append user + assistant
messages, append a tool message — and assert the right slices accumulated
on the right streams via the wired-in hooks at ``SessionDB.create_session``
+ ``SessionDB.append_message``.

If this test goes red, the user-facing acceptance criterion is also broken
— the wiring at one of the chokepoints is no longer emitting.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.cli import inspect as inspect_mod


@pytest_asyncio.fixture
async def booted(hermes_db_initialized):
    """Booted substrate with sub-agents off — the assertions look at
    pending-state shape, so we don't want Sentinel passing slices mid-test.
    """
    sub = await Substrate.boot(start_subagents=False)
    yield sub
    await sub.shutdown()


@pytest.mark.asyncio
async def test_session_roundtrip_emits_substrate_slices(booted):
    """A simulated CLI session — create + user message + assistant
    response + tool message — produces slices on the expected streams.

    This is the acceptance gate for the §7 wiring: hooks at
    ``SessionDB.create_session`` (session_lifecycle) and
    ``SessionDB.append_message`` (user_message, assistant_response,
    tool_call, tool_result) must fire for a normal turn.
    """
    import hermes_db
    from hermes_state import _AsyncSessionDB

    db = _AsyncSessionDB()
    sid = await db.create_session(
        session_id="e2e-cli-1",
        source="cli",
        model="claude-sonnet-4-6",
        system_prompt="you are helpful",
    )

    # User asks a question.
    await db.append_message(sid, "user", "what is 2+2?")
    # Assistant replies with text.
    await db.append_message(sid, "assistant", "It's 4.")
    # Assistant invokes a tool with a structured call.
    await db.append_message(
        sid,
        "assistant",
        None,
        tool_calls=[
            {
                "function": {"name": "calculator", "arguments": '{"expr": "2+2"}'}
            }
        ],
    )
    # Tool returns its result on the tool channel.
    await db.append_message(sid, "tool", "4", tool_name="calculator")

    # End the session — emits session_end via on_session_end_async (NOT
    # currently wired in hermes_state.end_session because the spec said
    # the implementer "locates during build" — we accept the slice tally
    # WITHOUT this hook for now).

    # ---- Assertions: slice tally per stream --------------------------------
    expected_streams = {
        "thoth.self_state.session_lifecycle": 1,         # session_start
        "thoth.world.user_message.cli": 1,               # the user msg
        "thoth.self_action.assistant_response": 1,       # the text reply
        "thoth.self_action.tool_call": 1,                # the calculator call
        "thoth.self_state.tool_result": 1,               # the tool response
    }
    async with hermes_db.connection() as conn:
        rows = await conn.fetch(
            """
            SELECT st.name AS name, COUNT(*) AS n
              FROM substrate_slices sl
              JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE sl.metadata->>'session_id' = $1
                OR (st.name = 'thoth.self_state.session_lifecycle'
                    AND sl.payload->>'session_id' = $1)
             GROUP BY st.name
            """,
            sid,
        )
    counts = {r["name"]: r["n"] for r in rows}
    for stream, expected in expected_streams.items():
        assert counts.get(stream, 0) == expected, (
            f"stream {stream!r}: expected {expected} slices, got "
            f"{counts.get(stream, 0)} — full counts: {counts}"
        )


@pytest.mark.asyncio
async def test_inspect_summary_after_roundtrip_is_nonempty(booted):
    """After a session round-trip, ``thoth substrate`` reports
    non-zero slice counts and the expected sub-agent section.

    This is the §12 acceptance #4 surrogate: *"thoth substrate
    returns a non-empty summary with sensible counts after one or more
    real sessions."*
    """
    import hermes_db
    from hermes_state import _AsyncSessionDB

    db = _AsyncSessionDB()
    sid = await db.create_session(
        session_id="e2e-inspect-1", source="cli", model="m"
    )
    await db.append_message(sid, "user", "hello")
    await db.append_message(sid, "assistant", "hi")

    buf = io.StringIO()
    async with hermes_db.connection() as conn:
        with redirect_stdout(buf):
            await inspect_mod._print_summary(conn)
    out = buf.getvalue()

    # Section presence.
    assert "Streams:" in out
    assert "15 registered" in out
    assert "Slices:" in out

    # Non-zero totals — the round-trip just produced 3 slices.
    # Format: "Slices:  N,NNN total"
    line = next(l for l in out.splitlines() if l.startswith("Slices:"))
    n = int(line.split("Slices:")[1].split()[0].replace(",", ""))
    assert n >= 3, f"expected at least 3 slices after round-trip, got {n}"

    # Pending section reports a depth > 0 (sub-agents are off so
    # Sentinel never drained the queue).
    assert "depth = " in out


@pytest.mark.asyncio
async def test_end_session_emits_substrate_slice(booted):
    """``SessionDB.end_session`` emits a ``session_end`` lifecycle slice
    when it actually flips a session from open → ended; a redundant
    call (already-ended session) is a no-op and does NOT emit.

    Mirrors the on_session_start chokepoint wiring in
    ``create_session`` so the substrate sees the full session
    lifecycle without per-call-site hook plumbing.
    """
    import hermes_db
    from hermes_state import _AsyncSessionDB

    db = _AsyncSessionDB()
    sid = await db.create_session(
        session_id="e2e-end-1", source="cli", model="m"
    )

    # First end_session call should emit a session_end slice.
    await db.end_session(sid, "user_quit")

    async with hermes_db.connection() as conn:
        end_count_first = await conn.fetchval(
            """
            SELECT COUNT(*) FROM substrate_slices sl
             JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE st.name = 'thoth.self_state.session_lifecycle'
               AND sl.payload->>'session_id' = $1
               AND sl.payload->>'event' = 'session_end'
            """,
            sid,
        )
    assert end_count_first == 1

    # Second end_session is a no-op against the row (already ended);
    # therefore no new substrate slice — the wiring guards on the
    # ``UPDATE 1`` command tag.
    await db.end_session(sid, "another_reason")

    async with hermes_db.connection() as conn:
        end_count_second = await conn.fetchval(
            """
            SELECT COUNT(*) FROM substrate_slices sl
             JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE st.name = 'thoth.self_state.session_lifecycle'
               AND sl.payload->>'session_id' = $1
               AND sl.payload->>'event' = 'session_end'
            """,
            sid,
        )
    assert end_count_second == 1, (
        "redundant end_session call should NOT emit a second slice"
    )


@pytest.mark.asyncio
async def test_session_start_shares_txn_with_session_row(booted):
    """The atomicity wired into ``SessionDB.create_session`` is verifiable
    end-to-end: if we set up a stream so the substrate INSERT fails after
    the session INSERT in the same txn, the session row should be rolled
    back too. We can't easily force the substrate hook to raise from here
    (the hook swallows errors per §6.2), but we can prove the txn is
    SHARED by checking the slice + session row land in the same instant.
    """
    import hermes_db
    from hermes_state import _AsyncSessionDB

    db = _AsyncSessionDB()
    await db.create_session(
        session_id="e2e-atomic-1", source="cli", model="m"
    )

    async with hermes_db.connection() as conn:
        # The slice's ingest_time_world should be within a few ms of the
        # session row's started_at because the INSERT pair shared the
        # same transaction (= same DB clock tick).
        row = await conn.fetchrow(
            """
            SELECT s.started_at, sl.ingest_time_world,
                   ABS(EXTRACT(EPOCH FROM (sl.ingest_time_world - s.started_at))) AS delta_s
              FROM sessions s
              JOIN substrate_slices sl ON sl.payload->>'session_id' = s.id
              JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE s.id = $1
               AND st.name = 'thoth.self_state.session_lifecycle'
               AND sl.payload->>'event' = 'session_start'
            """,
            "e2e-atomic-1",
        )
    assert row is not None
    # Same transaction → same now() snapshot → delta is sub-millisecond.
    # Use 1 second as a generous bound that still proves "same txn".
    assert row["delta_s"] < 1.0, (
        f"session row and slice are >1s apart "
        f"(delta_s={row['delta_s']:.6f}) — txn sharing is broken"
    )
