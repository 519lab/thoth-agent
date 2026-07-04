"""Phase-2c substrate integration for SubstrateContextEngine.

Phase 2c wires the Tier-1 eviction ladder (2b) into the substrate:

  * every evicted message mints ONE ``thoth.self_state.context_evicted``
    pointer slice — born consolidated, carrying the retrieval handle + a
    searchable/actionable ``text`` gist — so proactive recall can page evicted
    content back into the SAME session;
  * dereferencing a handle via ``context_expand`` reinforces its pointer slice;
  * all substrate access is best-effort + guarded, so eviction still works with
    no substrate bound.

PG-backed (test PG on localhost:5433). These are SYNCHRONOUS test bodies: the
engine runs on the sync compaction loop and bridges to the async substrate via
``thoth_db.run_sync``, so we drive it the same way and read back through
``run_sync``. The recall same-session carve-out lives in
``tests/substrate/recall/test_recall_window.py``; the stream-count assertion in
``tests/substrate/test_boot.py``.
"""

import json

import pytest

from agent.context_compressor import EVICTION_STUB_PREFIX
from agent.context_engine_substrate import _make_handle
from thoth_state import _AsyncSessionDB
from tests._helpers.sync_session_db import SyncSessionDB

# Reuse the 2b eviction suite's offline construction + fixture-building helpers
# so the two suites stay in lock-step on the conversation shape and thresholds.
from tests.agent.test_context_engine_substrate_eviction import (
    _build_conversation,
    _make_engine,
    _seed_tool_row,
)

_EVICTED_STREAM = "thoth.self_state.context_evicted"


@pytest.fixture
def db(thoth_db_initialized_sync):
    return SyncSessionDB(_AsyncSessionDB())


@pytest.fixture
def bound_substrate(thoth_db_initialized_sync):
    """A ``from_pool`` Substrate bound to the perception hooks with the
    ``context_evicted`` stream registered — the minimal setup the engine's
    ``get_bound_substrate()`` path needs, without the full boot side effects.
    """
    import thoth_db
    from substrate import Substrate
    from substrate.events import thoth_hooks
    from substrate.storage import (
        DEFAULT_STRUCTURED_PROFILE,
        Family,
        Modality,
    )

    sub = Substrate.from_pool(thoth_db.pool())

    async def _register():
        await sub.streams.register(
            name=_EVICTED_STREAM,
            family=Family.SELF_STATE,
            modality=Modality.STRUCTURED_EVENT,
            source="agent",
            organ="context_engine",
            decay_profile_id=DEFAULT_STRUCTURED_PROFILE,
        )

    thoth_db.run_sync(_register())
    thoth_hooks._bind(sub)
    try:
        yield sub
    finally:
        thoth_hooks._unbind()


def _fetch_evicted_slices() -> list[dict]:
    """Return every committed ``context_evicted`` slice (payload, metadata,
    consolidation_state), handle-ordered for deterministic assertions."""
    import thoth_db

    async def _go():
        async with thoth_db.connection() as conn:
            rows = await conn.fetch(
                """
                SELECT sl.payload, sl.metadata, sl.consolidation_state,
                       sl.salience_score, sl.sentinel_state
                  FROM substrate_slices  sl
                  JOIN substrate_streams st ON st.stream_id = sl.stream_id
                 WHERE st.name = $1
                 ORDER BY sl.payload->>'handle'
                """,
                _EVICTED_STREAM,
            )
            return [dict(r) for r in rows]

    return thoth_db.run_sync(_go())


def _recall_evicted(substrate, session_id: str) -> list:
    """Query ``recall_window`` for THIS session's eviction pointers WITHOUT
    running any Sentinel pass — the whole point of born_passed is that a fresh
    pointer is immediately eligible (``sentinel_state='passed'``). Uses the
    same carve-out path as the live proactive-recall pipeline (the
    ``context_evicted`` stream is exempt from same-session exclusion)."""
    import thoth_db
    from datetime import datetime, timedelta, timezone

    async def _go():
        async with thoth_db.connection() as conn:
            return await substrate.slices.recall_window(
                conn,
                t_now=datetime.now(timezone.utc) + timedelta(seconds=1),
                time_window=timedelta(hours=1),
                stream_names=[_EVICTED_STREAM],
                min_salience=0.0,
                limit=20,
                exclude_session_id=session_id,  # carve-out keeps our own pointers
            )

    return thoth_db.run_sync(_go())


# ---------------------------------------------------------------------------
# Eviction → pointer slice
# ---------------------------------------------------------------------------


def test_eviction_commits_one_pointer_slice_per_message(bound_substrate, db):
    """A Tier-1 pass over two evictable tool results mints exactly two pointer
    slices — born consolidated, with the handle/tool/gist/orig_len/text payload
    fields and session_id metadata the recall + dereference paths depend on."""
    db.create_session("s_c", source="cli")
    big_a = "AAA " + ("a" * 4000)
    big_b = "BBB " + ("b" * 4000)
    _seed_tool_row(db, "s_c", "call_a", big_a)
    _seed_tool_row(db, "s_c", "call_b", big_b)

    engine = _make_engine()
    engine.on_session_start("s_c", platform="cli")
    out = engine.compress(_build_conversation(big_a, big_b))

    # Both middle tool results became stubs (2b behaviour preserved).
    assert out[3]["content"].startswith(EVICTION_STUB_PREFIX)
    assert out[5]["content"].startswith(EVICTION_STUB_PREFIX)

    rows = _fetch_evicted_slices()
    assert len(rows) == 2  # one pointer per evicted message
    for r in rows:
        payload = r["payload"]
        assert payload["kind"] == "context_evicted"
        assert payload["handle"].startswith("sid:s_c#m:")
        assert payload["tool_name"] == "terminal"
        assert payload["orig_len"] == len(big_a)  # both fixtures are 4004 chars
        assert payload["gist"]  # non-empty redacted gist
        # The searchable/actionable one-liner the composer/keyword path reads.
        assert payload["tool_name"] in payload["text"]
        assert f'context_expand("{payload["handle"]}")' in payload["text"]
        # Pointers: consolidated (never Parser fodder), correct provenance.
        assert r["consolidation_state"] == "consolidated"
        assert r["metadata"]["session_id"] == "s_c"
        assert r["metadata"]["source"] == "context_engine"


def test_eviction_slices_born_passed_immediately_recallable(bound_substrate, db):
    """Phase 2d: pointer slices are committed born_passed, so they are
    ``sentinel_state='passed'`` the instant compress() returns and surface in
    ``recall_window`` with NO Sentinel tick — closing the evict-then-recall gap
    for adjacent turns. An eviction-only pass records survived_in_context=True.
    """
    db.create_session("s_bp", source="cli")
    big_a = "AAA " + ("a" * 4000)
    big_b = "BBB " + ("b" * 4000)
    _seed_tool_row(db, "s_bp", "call_a", big_a)
    _seed_tool_row(db, "s_bp", "call_b", big_b)

    engine = _make_engine()
    engine.on_session_start("s_bp", platform="cli")
    engine.compress(_build_conversation(big_a, big_b))
    # Sanity: this pass was eviction-only (Tier 2 never ran).
    assert engine._last_compress_eviction_only is True

    rows = _fetch_evicted_slices()
    assert len(rows) == 2
    for r in rows:
        # Born passed → immediately eligible; NOT sitting sentinel-pending.
        assert r["sentinel_state"] == "passed"
        assert r["consolidation_state"] == "consolidated"
        # Eviction alone relieved pressure → the stubs survived in context.
        assert r["payload"]["survived_in_context"] is True

    # End-to-end: they surface via recall_window with no _pass_all_pending step.
    candidates = _recall_evicted(bound_substrate, "s_bp")
    handles = {c.payload.get("handle") for c in candidates}
    assert len(handles) == 2  # both evicted pointers recallable this turn


def test_eviction_slice_records_survival_false_when_tier2_escalates(bound_substrate, db):
    """When Tier 2 runs in the SAME pass (its summary paraphrases the stubs
    away), the pointer slices are STILL committed (the only rediscovery path —
    not gated off) but flagged ``survived_in_context=False`` so Phase-3 analysis
    can tell the regimes apart (plan §4)."""
    db.create_session("s_esc", source="cli")
    big_a = "AAA " + ("a" * 4000)
    big_b = "BBB " + ("b" * 4000)
    _seed_tool_row(db, "s_esc", "call_a", big_a)
    _seed_tool_row(db, "s_esc", "call_b", big_b)

    engine = _make_engine()
    # Floor impossibly high → eviction can't clear it → escalate to Tier 2.
    engine._evict_min_reclaim = 10_000_000
    engine.on_session_start("s_esc", platform="cli")
    engine.compress(_build_conversation(big_a, big_b))

    assert engine._last_compress_eviction_only is False   # Tier 2 ran
    assert engine.compression_count == 1

    rows = _fetch_evicted_slices()
    assert len(rows) == 2  # slices committed anyway (rediscovery path kept)
    for r in rows:
        assert r["payload"]["survived_in_context"] is False
        assert r["sentinel_state"] == "passed"  # still born_passed


def test_eviction_slices_kill_switch_mints_nothing(bound_substrate, db, monkeypatch):
    """CONTEXT_ENGINE_SUBSTRATE_DISABLE_SLICES=1 disables pointer minting for
    operators, while eviction itself still works (the ladder is substrate-
    optional)."""
    monkeypatch.setenv("CONTEXT_ENGINE_SUBSTRATE_DISABLE_SLICES", "1")
    db.create_session("s_ks", source="cli")
    big_a = "AAA " + ("a" * 4000)
    big_b = "BBB " + ("b" * 4000)
    _seed_tool_row(db, "s_ks", "call_a", big_a)
    _seed_tool_row(db, "s_ks", "call_b", big_b)

    engine = _make_engine()
    engine.on_session_start("s_ks", platform="cli")
    out = engine.compress(_build_conversation(big_a, big_b))

    # Eviction still happened in-memory ...
    assert out[3]["content"].startswith(EVICTION_STUB_PREFIX)
    assert out[5]["content"].startswith(EVICTION_STUB_PREFIX)
    # ... but no pointer slices were minted.
    assert _fetch_evicted_slices() == []


def test_eviction_without_substrate_commits_nothing(db):
    """Substrate unbound → eviction still works, mints zero pointer slices, and
    never raises (the ladder is substrate-optional)."""
    import thoth_db
    from substrate import get_bound_substrate
    from substrate.events import thoth_hooks

    thoth_hooks._unbind()  # belt-and-braces: ensure nothing is bound
    assert get_bound_substrate() is None

    db.create_session("s_u", source="cli")
    big_a = "AAA " + ("a" * 4000)
    big_b = "BBB " + ("b" * 4000)
    _seed_tool_row(db, "s_u", "call_a", big_a)
    _seed_tool_row(db, "s_u", "call_b", big_b)

    engine = _make_engine()
    engine.on_session_start("s_u", platform="cli")
    out = engine.compress(_build_conversation(big_a, big_b))  # must not raise

    assert out[3]["content"].startswith(EVICTION_STUB_PREFIX)  # evicted anyway
    assert out[5]["content"].startswith(EVICTION_STUB_PREFIX)

    async def _count():
        async with thoth_db.connection() as conn:
            return await conn.fetchval(
                "SELECT count(*) FROM substrate_slices sl "
                "JOIN substrate_streams st ON st.stream_id = sl.stream_id "
                "WHERE st.name = $1",
                _EVICTED_STREAM,
            )

    assert thoth_db.run_sync(_count()) == 0


# ---------------------------------------------------------------------------
# Dereference → reinforce
# ---------------------------------------------------------------------------


def test_context_expand_reinforces_eviction_slice(bound_substrate, db):
    """A ``context_expand`` dereference bumps the salience of the matching
    eviction pointer slice (plan §2.4). We pre-decay the slice below 1.0 so the
    bump is observable (fresh slices are born at the 1.0 cap)."""
    import thoth_db

    sub = bound_substrate
    db.create_session("s_ref", source="cli")
    big = "AAA " + ("a" * 4000)
    mid = _seed_tool_row(db, "s_ref", "call_r", big)
    handle = _make_handle("s_ref", mid)

    async def _seed_and_decay():
        from datetime import datetime, timezone

        from substrate.l0 import commit_slice

        stream = await sub.streams.get_by_name(_EVICTED_STREAM)
        await commit_slice(
            sub,
            stream.stream_id,
            {"kind": "context_evicted", "handle": handle, "text": "x"},
            event_time_world=datetime.now(timezone.utc),
            metadata={"session_id": "s_ref", "source": "context_engine"},
            born_consolidated=True,
        )
        # Pass it (so recall/reinforce see a live slice) and pre-decay to 0.5.
        async with thoth_db.connection() as conn:
            await conn.execute(
                "UPDATE substrate_slices "
                "   SET sentinel_state = 'passed', trust_score = 0.9, "
                "       salience_score = 0.5, salience_updated_at = now() "
                " WHERE payload->>'handle' = $1",
                handle,
            )

    thoth_db.run_sync(_seed_and_decay())

    engine = _make_engine()
    engine.on_session_start("s_ref", platform="cli")
    # Dereference the handle. Reinforcement fires synchronously (run_sync) inside
    # handle_tool_call, so it has completed by the time this returns.
    result = json.loads(
        engine.handle_tool_call("context_expand", {"handle": handle}, db=db)
    )
    assert result["content"] == big  # byte-exact expand still works

    async def _salience():
        async with thoth_db.connection() as conn:
            return await conn.fetchval(
                "SELECT salience_score FROM substrate_slices "
                "WHERE payload->>'handle' = $1",
                handle,
            )

    assert thoth_db.run_sync(_salience()) > 0.5  # dereference bumped it


# ---------------------------------------------------------------------------
# Reactive-path telemetry
# ---------------------------------------------------------------------------


def test_context_expand_emits_pagein_telemetry(db, monkeypatch):
    """Each ``context_expand`` call emits one ``context.pagein`` event with the
    reactive-path fields. ``agent.context_telemetry`` ships on another branch,
    so we inject a fake module (mirroring the 2b telemetry-seam test)."""
    import sys
    import types

    calls = []
    fake = types.ModuleType("agent.context_telemetry")
    fake.emit = lambda event, **fields: calls.append((event, fields))
    monkeypatch.setitem(sys.modules, "agent.context_telemetry", fake)
    import agent as _agent_pkg

    monkeypatch.setattr(_agent_pkg, "context_telemetry", fake, raising=False)

    db.create_session("s_pi", source="cli")
    big = "AAA " + ("a" * 4000)
    mid = _seed_tool_row(db, "s_pi", "call_p", big)
    handle = _make_handle("s_pi", mid)

    engine = _make_engine()
    engine.on_session_start("s_pi", platform="cli")
    engine.handle_tool_call("context_expand", {"handle": handle}, db=db)

    pageins = [fields for (event, fields) in calls if event == "context.pagein"]
    assert len(pageins) == 1
    fields = pageins[0]
    assert fields["tool"] == "context_expand"
    assert fields["handle_or_pattern"] == handle
    assert fields["served_bytes"] > 0
    assert fields["truncated"] is False
    assert fields["source"] == "reactive"
    assert fields["session_id"] == "s_pi"


def test_dereference_reinforce_no_pointer_is_noop(bound_substrate, db):
    """Expanding a handle that has no eviction pointer slice must not raise —
    the reinforce path is a clean no-op when the lookup misses."""
    db.create_session("s_np", source="cli")
    big = "AAA " + ("a" * 4000)
    mid = _seed_tool_row(db, "s_np", "call_x", big)
    handle = _make_handle("s_np", mid)

    engine = _make_engine()
    engine.on_session_start("s_np", platform="cli")
    result = json.loads(
        engine.handle_tool_call("context_expand", {"handle": handle}, db=db)
    )
    assert result["content"] == big  # succeeds; reinforce found nothing to bump
