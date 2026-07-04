"""Tests for the Round-2 CoolingContextEngine.

The cooling engine distils tool results by AGE (a bounded near-tail edit) so the
context almost never reaches the compaction threshold; the parent's tiered
``compress()`` survives only as an emergency backstop. These tests pin:

  * the cooling rule (window / size-floor / stub-skip / head + newest-user
    protection) — the exact predicate that decides a candidate is cooled;
  * the proactive preflight (True iff structurally-cooled work exists, DB-free,
    idempotent after a pass);
  * the distillation pass (in-place restorable stub, one trigger-tagged pointer
    slice per distilled message, structure preserved, NO rotation, organ
    ``compression_count`` UNTOUCHED) — driven with NO threshold pressure, to
    prove it is age-triggered, not pressure-triggered;
  * unpersisted candidates deferred (skipped, not lost);
  * the backstop (distillation can't relieve real pressure → parent Tier-2 runs,
    ``compression_count`` increments, rotation allowed);
  * the ``CONTEXT_COOL=0`` kill switch (pure parent behaviour);
  * the §2.6 invariants (pairing / valid arguments JSON / newest user live).

PG-backed tests use ``thoth_db_initialized_sync`` (test PG on localhost:5433,
never 5432) and reuse the 2b/2c fixture idioms.
"""

import json
import re

import pytest

from agent.context_compressor import EVICTION_STUB_PREFIX
from agent.context_engine_cooling import CoolingContextEngine
from agent.context_engine_substrate import SubstrateContextEngine, _make_handle, _parse_handle
from agent.model_metadata import estimate_messages_tokens_rough
from thoth_state import _AsyncSessionDB
from tests._helpers.sync_session_db import SyncSessionDB

# Reuse the 2b eviction suite's offline construction + row-seeding helpers so the
# cooling suite stays in lock-step on conversation shape and thresholds.
from tests.agent.test_context_engine_substrate_eviction import (
    _assistant_call,
    _no_ctx_probe,
    _seed_tool_row,
    _stub_handle,
    _tool_result,
)

from unittest.mock import patch

_EVICTED_STREAM = "thoth.self_state.context_evicted"


# ---------------------------------------------------------------------------
# Construction + fixture builders
# ---------------------------------------------------------------------------

def _make_engine(**kwargs) -> CoolingContextEngine:
    kwargs.setdefault("model", "test/model")
    kwargs.setdefault("quiet_mode", True)
    with _no_ctx_probe():
        eng = CoolingContextEngine(**kwargs)
    # Same compaction knobs as the 2b suite so tiny fixtures cross thresholds.
    eng.threshold_tokens = 3000
    eng._compressor.tail_token_budget = 40
    eng.protect_first_n = 1
    eng._evict_min_chars = 200
    eng._evict_min_reclaim = 50
    # Cooling knobs: small floor so short fixtures qualify; default 5-turn window.
    eng._cool_window_turns = 5
    eng._cool_min_chars = 200
    return eng


def _build_cooling_convo(candidate: str, n_turns_after: int, filler: str = "z" * 20):
    """A conversation whose single evictable tool result (``call_a``, idx 3)
    has exactly ``n_turns_after`` assistant-with-tool_calls turns after it.

    The filler tool results are below the size floor, so ``call_a`` is the ONLY
    cooling candidate — its cooled/hot state is governed purely by the count of
    newer assistant tool-turns (the window rule under test). Total token mass is
    tiny (well under the 3000-token threshold), so distillation here happens
    with NO threshold pressure — proving the trigger is age, not pressure.
    """
    msgs = [
        {"role": "system", "content": "System prompt"},   # 0 head
        {"role": "user", "content": "start the task"},     # 1
        _assistant_call("call_a"),                         # 2
        _tool_result("call_a", candidate),                 # 3 candidate
    ]
    for k in range(n_turns_after):
        msgs.append(_assistant_call(f"call_{k}"))
        msgs.append(_tool_result(f"call_{k}", filler))
    msgs.append({"role": "user", "content": "newest question"})
    return msgs


@pytest.fixture
def db(thoth_db_initialized_sync):
    return SyncSessionDB(_AsyncSessionDB())


@pytest.fixture
def bound_substrate(thoth_db_initialized_sync):
    """A ``from_pool`` Substrate with the ``context_evicted`` stream registered
    and bound to the perception hooks — the minimal setup the engine's
    ``get_bound_substrate()`` path needs (copied from the 2c suite)."""
    import thoth_db
    from substrate import Substrate
    from substrate.events import thoth_hooks
    from substrate.storage import DEFAULT_STRUCTURED_PROFILE, Family, Modality

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


def _fetch_evicted_slices() -> list:
    """Every committed ``context_evicted`` slice, handle-ordered."""
    import thoth_db

    async def _go():
        async with thoth_db.connection() as conn:
            rows = await conn.fetch(
                """
                SELECT sl.payload, sl.metadata, sl.consolidation_state,
                       sl.sentinel_state
                  FROM substrate_slices  sl
                  JOIN substrate_streams st ON st.stream_id = sl.stream_id
                 WHERE st.name = $1
                 ORDER BY sl.payload->>'handle'
                """,
                _EVICTED_STREAM,
            )
            return [dict(r) for r in rows]

    return thoth_db.run_sync(_go())


# ---------------------------------------------------------------------------
# The cooling rule (structural — no DB)
# ---------------------------------------------------------------------------

class TestCoolingRule:
    def test_window_boundary_hot_at_4_cooled_at_5(self):
        # With a 5-turn window: a result with only 4 newer assistant tool-turns
        # is still HOT; the 5th turn cools it.
        engine = _make_engine()
        hot = _build_cooling_convo("A" * 500, n_turns_after=4)
        cooled = _build_cooling_convo("A" * 500, n_turns_after=5)
        assert engine._has_structurally_cooled(hot) is False
        assert engine._structurally_cooled_indices(hot) == []
        assert engine._has_structurally_cooled(cooled) is True
        assert engine._structurally_cooled_indices(cooled) == [3]

    def test_window_env_override(self):
        engine = _make_engine()
        engine._cool_window_turns = 3
        msgs = _build_cooling_convo("A" * 500, n_turns_after=3)
        assert engine._has_structurally_cooled(msgs) is True

    def test_size_floor_excludes_small_results(self):
        engine = _make_engine()
        engine._cool_min_chars = 1000
        below = _build_cooling_convo("A" * 500, n_turns_after=6)   # cooled but small
        above = _build_cooling_convo("A" * 1500, n_turns_after=6)
        assert engine._has_structurally_cooled(below) is False
        assert engine._has_structurally_cooled(above) is True

    def test_existing_stub_is_not_a_candidate(self):
        # Idempotency at the rule level: an already-distilled stub never cools.
        engine = _make_engine()
        msgs = _build_cooling_convo("A" * 500, n_turns_after=6)
        msgs[3]["content"] = f'{EVICTION_STUB_PREFIX}sid:s#m:1 — t (1 chars). Gist: x. Retrieve exact: context_expand("sid:s#m:1")]'
        assert engine._has_structurally_cooled(msgs) is False

    def test_protected_head_never_cools(self):
        # A candidate inside the protected head (system + protect_first_n) is
        # never a cooling candidate even with plenty of newer tool-turns.
        engine = _make_engine()
        engine.protect_first_n = 10  # head swallows the whole conversation front
        msgs = _build_cooling_convo("A" * 500, n_turns_after=6)
        assert engine._has_structurally_cooled(msgs) is False
        assert engine._structurally_cooled_indices(msgs) == []

    def test_newest_user_is_never_a_candidate(self):
        # Only role:"tool" messages are candidates, so the newest user message
        # (and any user message) is structurally excluded.
        engine = _make_engine()
        msgs = _build_cooling_convo("A" * 500, n_turns_after=6)
        idxs = engine._structurally_cooled_indices(msgs)
        assert all(msgs[i]["role"] == "tool" for i in idxs)
        assert (len(msgs) - 1) not in idxs  # newest user untouched


# ---------------------------------------------------------------------------
# Preflight — the proactive trigger (DB-free, cheap, idempotent)
# ---------------------------------------------------------------------------

class TestPreflight:
    def test_false_on_hot_history(self):
        engine = _make_engine()
        assert engine.should_compress_preflight(_build_cooling_convo("A" * 500, 4)) is False

    def test_true_once_a_candidate_cools(self):
        engine = _make_engine()
        assert engine.should_compress_preflight(_build_cooling_convo("A" * 500, 5)) is True

    def test_false_again_after_distillation(self, db):
        # Idempotent: once the cooled result is distilled to a stub, preflight
        # reports no more work (the stub is excluded by the rule).
        db.create_session("s_pf", source="cli")
        big = "A" * 4004
        _seed_tool_row(db, "s_pf", "call_a", big)
        engine = _make_engine()
        engine.on_session_start("s_pf", platform="cli")
        msgs = _build_cooling_convo(big, n_turns_after=5)
        assert engine.should_compress_preflight(msgs) is True
        out = engine.compress(list(msgs))
        assert out[3]["content"].startswith(EVICTION_STUB_PREFIX)
        assert engine.should_compress_preflight(out) is False

    def test_kill_switch_forces_false(self):
        engine = _make_engine()
        cooled = _build_cooling_convo("A" * 500, 6)
        with patch.dict("os.environ", {"CONTEXT_COOL": "0"}):
            assert engine.should_compress_preflight(cooled) is False


# ---------------------------------------------------------------------------
# Distillation pass — age-triggered, NO pressure, structure preserved
# ---------------------------------------------------------------------------

class TestDistillationPass:
    def _run(self, db, session_id="s_dist"):
        db.create_session(session_id, source="cli")
        big = "AAA " + ("a" * 4000)  # 4004 chars, well over the floor
        mid = _seed_tool_row(db, session_id, "call_a", big)
        engine = _make_engine()
        engine.on_session_start(session_id, platform="cli")
        msgs = _build_cooling_convo(big, n_turns_after=5)
        out = engine.compress(list(msgs))
        return engine, msgs, out, mid, big

    def test_distills_with_no_threshold_pressure(self, db):
        # The whole point of Round 2: a cooled result is distilled even though
        # the context is nowhere near the threshold (a pressure-triggered Tier-1
        # would evict nothing here). Distillation-only ⇒ no Tier-2, no rotation.
        engine, msgs, out, _, _ = self._run(db)
        # Sanity: we were well under threshold, so this was NOT pressure-driven.
        assert engine.last_prompt_tokens < engine.threshold_tokens
        assert out[3]["content"].startswith(EVICTION_STUB_PREFIX)
        assert engine._last_compress_eviction_only is True   # no rotation flag
        assert engine.compression_count == 0                 # organ untouched

    def test_stub_handle_round_trips_byte_exact(self, db):
        engine, msgs, out, _, big = self._run(db)
        handle = _stub_handle(out[3]["content"])
        assert _parse_handle(handle) is not None
        expanded = json.loads(engine.handle_tool_call("context_expand", {"handle": handle}, db=db))
        assert expanded["content"] == big        # byte-exact original
        assert "truncated" not in expanded

    def test_count_order_roles_unchanged(self, db):
        engine, msgs, out, _, _ = self._run(db)
        assert len(out) == len(msgs)
        assert [m["role"] for m in out] == [m["role"] for m in msgs]

    def test_newest_user_live_and_untouched(self, db):
        engine, msgs, out, _, _ = self._run(db)
        assert out[-1]["role"] == "user"
        assert out[-1]["content"] == "newest question"
        assert EVICTION_STUB_PREFIX not in json.dumps(out[-1])

    def test_pairing_and_valid_arguments_json(self, db):
        # §2.6 invariants: pairing preserved; assistant arguments untouched JSON.
        engine, msgs, out, _, _ = self._run(db)
        assistant_ids = {
            tc["id"]
            for m in out if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or [])
        }
        tool_ids = {m["tool_call_id"] for m in out if m.get("role") == "tool"}
        assert tool_ids == assistant_ids
        for m in out:
            for tc in m.get("tool_calls") or []:
                json.loads(tc["function"]["arguments"])  # raises if mangled

    def test_one_pointer_slice_with_cooling_trigger(self, bound_substrate, db):
        # One trigger-tagged pointer slice per distilled message. The trigger
        # ("cooling") is stamped into BOTH the payload and metadata so Phase-3
        # analysis can separate cooling-distilled pointers from threshold ones.
        db.create_session("s_slice", source="cli")
        big = "AAA " + ("a" * 4000)
        _seed_tool_row(db, "s_slice", "call_a", big)
        engine = _make_engine()
        engine.on_session_start("s_slice", platform="cli")
        engine.compress(_build_cooling_convo(big, n_turns_after=5))

        rows = _fetch_evicted_slices()
        assert len(rows) == 1  # only call_a cooled
        r = rows[0]
        assert r["payload"]["kind"] == "context_evicted"
        assert r["payload"]["handle"].startswith("sid:s_slice#m:")
        assert r["payload"]["trigger"] == "cooling"
        assert r["metadata"]["trigger"] == "cooling"
        assert r["payload"]["survived_in_context"] is True  # distillation-only
        assert r["consolidation_state"] == "consolidated"
        assert r["sentinel_state"] == "passed"  # born-passed, immediately recallable
        assert f'context_expand("{r["payload"]["handle"]}")' in r["payload"]["text"]


# ---------------------------------------------------------------------------
# Schema-inclusive relief check (round-4 forensic finding B, part 1)
# ---------------------------------------------------------------------------

class TestSchemaOverheadReliefCheck:
    """Finding B(1): the loop triggers compress() on ``last_prompt_tokens`` from
    real API usage — INCLUDING ~20-30k of tool-schema tokens — but the cooling
    relief check compared a MESSAGES-ONLY estimate. That ~25k dead-band made a
    distillation-only pass look "relieved" while the real prompt was still over
    threshold, so the loop re-entered compress() every turn (churn). The fix
    carries the schema+system overhead (schema-inclusive ``current_tokens`` minus
    a messages-only estimate of the same input) into the pressure basis and into
    ``last_prompt_tokens``.
    """

    def _seed_convo(self, db, session_id):
        db.create_session(session_id, source="cli")
        big = "AAA " + ("a" * 4000)
        _seed_tool_row(db, session_id, "call_a", big)
        engine = _make_engine()
        engine.on_session_start(session_id, platform="cli")
        return engine, _build_cooling_convo(big, n_turns_after=5)

    def test_no_false_relief_when_schema_overhead_holds_pressure(self, db):
        # Distillation gets the MESSAGES-only size well under threshold, but the
        # schema-inclusive trigger token count is far over it — the pass must NOT
        # report eviction-only relief; it must escalate to the backstop, and
        # last_prompt_tokens must end schema-inclusive (>= threshold).
        engine, msgs = self._seed_convo(db, "s_bover")
        threshold = engine.threshold_tokens
        schema_inclusive = threshold + 30_000  # ~real last_prompt_tokens w/ schemas

        captured = {}

        def _spy(self, messages, *, current_tokens=None, focus_topic=None,
                 force=False, **kw):
            # Backstop invoked ⇒ the relief check did NOT falsely relieve.
            captured["current_tokens"] = current_tokens
            captured["last_prompt_tokens"] = self._compressor.last_prompt_tokens
            return messages  # don't run real Tier-2 — isolate the decision

        with patch.object(SubstrateContextEngine, "compress", _spy):
            engine.compress(list(msgs), current_tokens=schema_inclusive)

        assert "current_tokens" in captured  # escalated to the backstop
        assert captured["current_tokens"] >= threshold  # overhead carried in
        assert captured["last_prompt_tokens"] >= threshold  # schema-inclusive
        assert engine._last_compress_eviction_only is False

    def test_genuine_relief_reports_relief_and_carries_overhead(self, db):
        # Schema overhead present but the total stays under threshold → genuine
        # relief: eviction-only True, and last_prompt_tokens = messages-only
        # estimate of the distilled list PLUS the captured overhead.
        engine, msgs = self._seed_convo(db, "s_brelief")
        pre_msgs_est = estimate_messages_tokens_rough(msgs)
        overhead = 800  # small schema+system overhead, keeps us under threshold
        current_tokens = pre_msgs_est + overhead

        out = engine.compress(list(msgs), current_tokens=current_tokens)

        assert engine._last_compress_eviction_only is True
        post_msgs_est = estimate_messages_tokens_rough(out)
        assert engine.last_prompt_tokens == post_msgs_est + overhead
        assert engine.last_prompt_tokens < engine.threshold_tokens


# ---------------------------------------------------------------------------
# Unpersisted candidates deferred (skipped, not lost)
# ---------------------------------------------------------------------------

class TestUnpersistedDeferred:
    def test_unpersisted_candidate_is_skipped_not_distilled(self, db):
        # call_a is cooled + over the floor but has NO row in the store yet →
        # skipped this pass (never distil un-retrievable content); it will cool
        # again next pass once flushed.
        db.create_session("s_unp", source="cli")
        big = "AAA " + ("a" * 4000)
        # deliberately DO NOT seed call_a
        engine = _make_engine()
        engine.on_session_start("s_unp", platform="cli")
        msgs = _build_cooling_convo(big, n_turns_after=5)
        result, reclaimed, n_distilled, n_skipped = engine._distill_cooled(list(msgs), db)
        assert n_distilled == 0
        assert n_skipped == 1
        assert result[3]["content"] == big  # left intact, not lost
        # Re-running once the row exists distils it (deferred, not dropped).
        _seed_tool_row(db, "s_unp", "call_a", big)
        result2, _, n_distilled2, n_skipped2 = engine._distill_cooled(list(msgs), db)
        assert n_distilled2 == 1
        assert n_skipped2 == 0
        assert result2[3]["content"].startswith(EVICTION_STUB_PREFIX)


# ---------------------------------------------------------------------------
# Hot-page protection carries over from the parent
# ---------------------------------------------------------------------------

class TestHotPageProtection:
    def test_recently_expanded_handle_survives_distillation(self, db):
        db.create_session("s_hot", source="cli")
        big = "AAA " + ("a" * 4000)
        mid = _seed_tool_row(db, "s_hot", "call_a", big)
        engine = _make_engine()
        engine.on_session_start("s_hot", platform="cli")
        engine._record_expanded(_make_handle("s_hot", mid))  # model paged it back in
        msgs = _build_cooling_convo(big, n_turns_after=5)
        result, _, n_distilled, _ = engine._distill_cooled(list(msgs), db)
        assert n_distilled == 0
        assert result[3]["content"] == big  # hot → left live


# ---------------------------------------------------------------------------
# Backstop — distillation can't relieve real pressure → parent Tier-2 runs
# ---------------------------------------------------------------------------

class TestBackstop:
    def _build_pressured(self, candidate: str):
        """A history whose bulk is non-distillable assistant TEXT, so even after
        distilling the one cooled tool result the estimate stays over threshold
        and the backstop (parent Tier-2) must fire."""
        msgs = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "start"},
            _assistant_call("call_a"),
            _tool_result("call_a", candidate),          # idx 3 — cooled, distilled
        ]
        for k in range(5):  # 5 assistant tool-turns after → cools call_a
            msgs.append(_assistant_call(f"call_{k}"))
            msgs.append(_tool_result(f"call_{k}", "z" * 20))
        # Non-tool bulk: three big assistant texts keep pressure on after
        # distillation (Tier-1 can't evict them; only Tier-2 can summarise them).
        for j in range(3):
            msgs.append({"role": "assistant", "content": f"analysis {j} " + ("m" * 6000)})
        msgs.append({"role": "user", "content": "newest question"})
        return msgs

    def test_backstop_runs_tier2_and_increments_count(self, db):
        db.create_session("s_bk", source="cli")
        big = "AAA " + ("a" * 4000)
        _seed_tool_row(db, "s_bk", "call_a", big)
        engine = _make_engine()
        engine.on_session_start("s_bk", platform="cli")
        msgs = self._build_pressured(big)
        # Sanity: distillation alone cannot get under threshold here.
        out = engine.compress(list(msgs))
        assert engine.compression_count == 1                 # Tier-2 backstop fired
        assert engine._last_compress_eviction_only is False  # rotation path allowed
        assert len(out) < len(msgs)                          # Tier-2 restructured

    def test_backstop_slice_records_survival_false(self, bound_substrate, db):
        # The cooled result was still distilled (its pointer slice committed),
        # but because Tier-2 then ran in the same pass its stub is paraphrased
        # away, so the pointer records survived_in_context=False (plan §4).
        db.create_session("s_bk2", source="cli")
        big = "AAA " + ("a" * 4000)
        _seed_tool_row(db, "s_bk2", "call_a", big)
        engine = _make_engine()
        engine.on_session_start("s_bk2", platform="cli")
        engine.compress(self._build_pressured(big))
        assert engine.compression_count == 1
        rows = _fetch_evicted_slices()
        assert len(rows) == 1
        assert rows[0]["payload"]["trigger"] == "cooling"
        assert rows[0]["payload"]["survived_in_context"] is False


# ---------------------------------------------------------------------------
# Kill switch — CONTEXT_COOL=0 ⇒ pure parent behaviour
# ---------------------------------------------------------------------------

class TestKillSwitch:
    def test_compress_delegates_to_parent(self, db):
        db.create_session("s_ks", source="cli")
        big = "AAA " + ("a" * 4000)
        _seed_tool_row(db, "s_ks", "call_a", big)
        engine = _make_engine()
        engine.on_session_start("s_ks", platform="cli")
        msgs = _build_cooling_convo(big, n_turns_after=5)
        # With the kill switch on, compress() must hand straight to the parent
        # ladder — no cooling distillation logic runs at all.
        with patch.dict("os.environ", {"CONTEXT_COOL": "0"}):
            with patch.object(
                SubstrateContextEngine, "compress", return_value=["DELEGATED"]
            ) as m:
                out = engine.compress(list(msgs))
        m.assert_called_once()
        assert out == ["DELEGATED"]

    def test_preflight_delegates_to_parent(self):
        engine = _make_engine()
        cooled = _build_cooling_convo("A" * 500, 6)
        # Parent preflight (inner compressor's) is False by default → kill switch
        # makes the cooling engine inert on a history it would otherwise flag.
        assert engine.should_compress_preflight(cooled) is True
        with patch.dict("os.environ", {"CONTEXT_COOL": "0"}):
            assert engine.should_compress_preflight(cooled) is False


# ---------------------------------------------------------------------------
# Degraded — no session / no DB ⇒ delegate to the parent ladder (backstop)
# ---------------------------------------------------------------------------

class TestDegraded:
    def test_no_session_delegates_to_parent(self):
        engine = _make_engine()
        assert engine._session_id is None
        with patch.object(
            SubstrateContextEngine, "compress", return_value=["X"]
        ) as m:
            out = engine.compress([{"role": "user", "content": "hi"}])
        m.assert_called_once()
        assert out == ["X"]
        assert engine._last_compress_eviction_only is False
