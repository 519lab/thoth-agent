"""Phase-2d degradation matrix for SubstrateContextEngine.

2d hardens the eviction ladder against every way the substrate / session store
can be missing or broken, and pins the tail cases from plan §4 ("tail cases") /
§2.2 (Tier-2 fallback). The invariant across the whole matrix: a degraded
substrate NEVER raises out of ``compress()`` / a retrieval tool and NEVER leaves
the ladder unable to relieve pressure — Tier-1 (session store) and Tier-2 (the
organ) are independent of the substrate, so pressure is always relievable even
when the substrate is dark.

One test per matrix cell (§3 of the 2d plan):

  a. substrate never booted        → full ladder works, Tier-1 evicts, 0 slices
  b. session store unavailable      → Tier-1 finds nothing → Tier-2 covers it
  c. substrate bound, commit raises → pass completes, stubs stay, log-only
  d. context_expand, DB down        → clean JSON error, no raise, no reinforce
  e. repeated pressure loop         → bounded tracking sets, invariants hold
  f. mixed persisted/unpersisted    → persisted stubbed, rest skipped → Tier-2

PG-backed cells use ``thoth_db_initialized_sync`` (test PG on localhost:5433).
These are SYNCHRONOUS bodies — the engine runs on the sync compaction loop.
"""

import json

import pytest

from agent.context_compressor import EVICTION_STUB_PREFIX
from agent.context_engine_substrate import (
    _EXPANDED_HANDLES_CAP,
    _DBUnavailable,
    _make_handle,
)
from thoth_state import _AsyncSessionDB
from tests._helpers.sync_session_db import SyncSessionDB

# Reuse the 2b eviction suite's offline construction + fixture helpers so the
# conversation shape and thresholds stay in lock-step across suites.
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
    ``context_evicted`` stream registered (mirrors the 2c fixture)."""
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


def _count_evicted_slices() -> int:
    import thoth_db

    async def _go():
        async with thoth_db.connection() as conn:
            return await conn.fetchval(
                "SELECT count(*) FROM substrate_slices sl "
                "JOIN substrate_streams st ON st.stream_id = sl.stream_id "
                "WHERE st.name = $1",
                _EVICTED_STREAM,
            )

    return thoth_db.run_sync(_go())


def _assert_pairing_and_json(msgs) -> None:
    """The §2.6 structural invariants that must survive every tier: every tool
    result is paired to an assistant tool_call, and no assistant
    ``function.arguments`` was mangled into invalid JSON."""
    assistant_ids = {
        tc["id"]
        for m in msgs
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    }
    tool_ids = {
        m["tool_call_id"]
        for m in msgs
        if m.get("role") == "tool" and m.get("tool_call_id")
    }
    assert tool_ids <= assistant_ids, "orphan tool result (pairing broken)"
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            json.loads(tc["function"]["arguments"])  # raises if mangled


# ---------------------------------------------------------------------------
# (a) Substrate never booted — ladder works, Tier-1 evicts, zero slices
# ---------------------------------------------------------------------------


def test_a_no_substrate_tier1_still_evicts_and_relieves(db):
    """No substrate bound at all: the full compress() ladder works, Tier-1
    evicts (session store is independent of the substrate), pressure is
    relieved eviction-only, and zero pointer slices are minted — no raise."""
    from substrate import get_bound_substrate
    from substrate.events import thoth_hooks

    thoth_hooks._unbind()
    assert get_bound_substrate() is None

    db.create_session("s_a", source="cli")
    big_a = "AAA " + ("a" * 4000)
    big_b = "BBB " + ("b" * 4000)
    _seed_tool_row(db, "s_a", "call_a", big_a)
    _seed_tool_row(db, "s_a", "call_b", big_b)

    engine = _make_engine()
    engine.on_session_start("s_a", platform="cli")
    out = engine.compress(_build_conversation(big_a, big_b))  # must not raise

    # Tier-1 STILL evicted (substrate-independent) and relieved pressure alone.
    assert out[3]["content"].startswith(EVICTION_STUB_PREFIX)
    assert out[5]["content"].startswith(EVICTION_STUB_PREFIX)
    assert engine._last_compress_eviction_only is True
    assert engine.compression_count == 0        # organ (Tier 2) never ran
    assert _count_evicted_slices() == 0         # nothing to point at
    _assert_pairing_and_json(out)


# ---------------------------------------------------------------------------
# (b) Session store unavailable — Tier-1 finds nothing → Tier-2 covers it
# ---------------------------------------------------------------------------


def test_b_no_persisted_rows_falls_through_to_tier2(db):
    """Session exists but has NO message rows: handle resolution returns empty,
    every candidate is skipped as un-retrievable, so Tier-1 evicts nothing and
    the pass escalates to Tier-2 — a usable smaller list, not-eviction-only."""
    db.create_session("s_norows", source="cli")  # created, but no tool rows seeded
    big_a = "AAA " + ("a" * 4000)
    big_b = "BBB " + ("b" * 4000)

    engine = _make_engine()
    engine.on_session_start("s_norows", platform="cli")
    out = engine.compress(_build_conversation(big_a, big_b))  # must not raise

    assert engine._last_compress_eviction_only is False   # Tier-2 covered it
    assert engine.compression_count == 1                  # organ ran
    assert len(out) < len(_build_conversation(big_a, big_b))
    _assert_pairing_and_json(out)


def test_b_resolve_raises_falls_through_to_tier2(db, monkeypatch):
    """A DB error DURING handle resolution (resolve_tool_call_message_ids raises)
    is swallowed inside Tier-1 (evict nothing) and the pass falls through to
    Tier-2 — never propagates."""
    db.create_session("s_dberr", source="cli")
    big_a = "AAA " + ("a" * 4000)
    big_b = "BBB " + ("b" * 4000)
    _seed_tool_row(db, "s_dberr", "call_a", big_a)
    _seed_tool_row(db, "s_dberr", "call_b", big_b)

    async def _boom(self, *a, **k):
        raise RuntimeError("session store DB error")

    monkeypatch.setattr(
        _AsyncSessionDB, "resolve_tool_call_message_ids", _boom, raising=True
    )

    engine = _make_engine()
    engine.on_session_start("s_dberr", platform="cli")
    out = engine.compress(_build_conversation(big_a, big_b))  # must not raise

    assert engine._last_compress_eviction_only is False   # Tier-2 covered it
    assert engine.compression_count == 1
    _assert_pairing_and_json(out)


# ---------------------------------------------------------------------------
# (c) Substrate bound but commit_slice raises — pass completes, log-only
# ---------------------------------------------------------------------------


def test_c_commit_slice_raises_pass_still_completes(bound_substrate, db, monkeypatch):
    """The substrate is bound but the pointer-slice commit blows up
    (commit_slice raises). The in-memory eviction already happened, so the pass
    must complete with stubs in place, no raise, and no committed slices —
    the failure is telemetry/log-only."""
    import substrate.l0 as l0

    async def _boom(*a, **k):
        raise RuntimeError("substrate write failed")

    monkeypatch.setattr(l0, "commit_slice", _boom, raising=True)

    db.create_session("s_commiterr", source="cli")
    big_a = "AAA " + ("a" * 4000)
    big_b = "BBB " + ("b" * 4000)
    _seed_tool_row(db, "s_commiterr", "call_a", big_a)
    _seed_tool_row(db, "s_commiterr", "call_b", big_b)

    engine = _make_engine()
    engine.on_session_start("s_commiterr", platform="cli")
    out = engine.compress(_build_conversation(big_a, big_b))  # must not raise

    assert out[3]["content"].startswith(EVICTION_STUB_PREFIX)  # evicted in-memory
    assert out[5]["content"].startswith(EVICTION_STUB_PREFIX)
    assert engine._last_compress_eviction_only is True         # ladder unaffected
    assert _count_evicted_slices() == 0                        # commit was dropped
    _assert_pairing_and_json(out)


# ---------------------------------------------------------------------------
# (d) context_expand with the DB down — clean error, no raise, no reinforce
# ---------------------------------------------------------------------------


def test_d_context_expand_db_down_returns_error_no_reinforce(monkeypatch):
    """When the session-store bridge is unreachable, context_expand returns a
    clean JSON error string (never raises) and does NOT attempt reinforcement
    (the failure is at db-resolution, before any handle work)."""
    engine = _make_engine()
    engine.on_session_start("s_down", platform="cli")

    # Simulate the bridge being down: _resolve_db raises _DBUnavailable exactly
    # as it does when thoth_db.pool() isn't initialised.
    def _down(_injected):
        raise _DBUnavailable("session store unavailable (Postgres pool not initialised)")

    monkeypatch.setattr(engine, "_resolve_db", _down, raising=True)

    reinforced = []
    monkeypatch.setattr(
        engine, "_reinforce_eviction_handle",
        lambda h: reinforced.append(h), raising=True,
    )

    result = engine.handle_tool_call(
        "context_expand", {"handle": _make_handle("s_down", 7)},
    )
    parsed = json.loads(result)                 # valid JSON, no raise
    assert "error" in parsed
    assert "unavailable" in parsed["error"]
    assert reinforced == []                     # no reinforcement attempted


# ---------------------------------------------------------------------------
# (e) Repeated pressure loop — bounded tracking sets, invariants hold
# ---------------------------------------------------------------------------


def test_e_repeated_compress_bounds_state_and_holds_invariants(db):
    """Five consecutive compress() passes on an evolving long session: internal
    tracking sets stay bounded, already-evicted stubs are never re-evicted, and
    the final list still satisfies the pairing/JSON invariants (§2.6). Substrate
    left unbound to isolate the ladder's own bookkeeping."""
    from substrate.events import thoth_hooks

    thoth_hooks._unbind()

    db.create_session("s_loop", source="cli")
    big_a = "AAA " + ("a" * 4000)
    big_b = "BBB " + ("b" * 4000)
    _seed_tool_row(db, "s_loop", "call_a", big_a)
    _seed_tool_row(db, "s_loop", "call_b", big_b)

    engine = _make_engine()
    engine.on_session_start("s_loop", platform="cli")

    msgs = _build_conversation(big_a, big_b)
    out = engine.compress(msgs)                 # pass 1 — eviction-only
    assert engine._last_compress_eviction_only is True
    # Newest-user invariant survives the eviction-only pass.
    assert any(m.get("content") == "newest question" for m in out)
    # Handles of the messages we stubbed on pass 1.
    stubbed_ids = {
        m["tool_call_id"] for m in out
        if m.get("role") == "tool"
        and isinstance(m.get("content"), str)
        and m["content"].startswith(EVICTION_STUB_PREFIX)
    }
    assert stubbed_ids == {"call_a", "call_b"}

    # Passes 2-5: feed the output back. Each must not raise, must hold the
    # structural invariants, and must never revert an existing stub to raw
    # content (stubs are never re-processed as fresh candidates).
    for _ in range(4):
        out = engine.compress(out)
        _assert_pairing_and_json(out)
        for m in out:
            if m.get("role") == "tool" and m.get("tool_call_id") in stubbed_ids:
                assert m["content"].startswith(EVICTION_STUB_PREFIX), \
                    "an evicted stub was reverted / re-processed"

    _assert_pairing_and_json(out)

    # Hot-page tracking set is hard-capped regardless of dereference volume —
    # a reference-heavy session can't grow it without bound.
    for i in range(_EXPANDED_HANDLES_CAP * 3):
        engine._record_expanded(_make_handle("s_loop", i))
    assert len(engine._expanded_handles) <= _EXPANDED_HANDLES_CAP


# ---------------------------------------------------------------------------
# (f) Mixed pass — persisted stubbed, unpersisted skipped, Tier-2 remainder
# ---------------------------------------------------------------------------


def test_f_mixed_persisted_unpersisted_substrate_down(db):
    """Some candidates are durably persisted, some are not, and the substrate is
    down. The persisted one is stubbed, the unpersisted one is skipped (never
    evict content that can't be retrieved), and — because a single stub can't
    relieve the pressure — Tier-2 covers the remainder. No slices, no raise."""
    from substrate import get_bound_substrate
    from substrate.events import thoth_hooks

    thoth_hooks._unbind()
    assert get_bound_substrate() is None

    db.create_session("s_mixed", source="cli")
    big_a = "AAA " + ("a" * 4000)
    big_b = "BBB " + ("b" * 4000)
    _seed_tool_row(db, "s_mixed", "call_a", big_a)   # persisted
    # call_b deliberately NOT seeded → unpersisted → must be skipped.

    engine = _make_engine()
    engine.on_session_start("s_mixed", platform="cli")

    # Granular view: drive Tier-1 directly (target=0 forces the full candidate
    # walk) to observe the stub/skip split before Tier-2 restructures indices.
    result, reclaimed, n_evicted, n_skipped = engine._evict_tier1(
        list(_build_conversation(big_a, big_b)), db,
        target_tokens=0, start_est=10_000,
    )
    assert result[3]["content"].startswith(EVICTION_STUB_PREFIX)  # call_a stubbed
    assert result[5]["content"] == big_b                          # call_b skipped
    assert n_evicted == 1 and n_skipped == 1

    # End-to-end: the same mixed pass through full compress() escalates to
    # Tier-2 for the remainder and mints no slices (substrate down).
    engine2 = _make_engine()
    engine2.on_session_start("s_mixed", platform="cli")
    out = engine2.compress(_build_conversation(big_a, big_b))  # must not raise
    assert engine2._last_compress_eviction_only is False       # Tier-2 covered it
    assert engine2.compression_count == 1
    assert _count_evicted_slices() == 0
    _assert_pairing_and_json(out)
