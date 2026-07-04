"""Tests for the Phase-2b Tier-1 eviction core of SubstrateContextEngine.

The eviction ladder (plan §2.2) turns oldest-first, durably-persisted tool
results into actionable *stubs* carrying a retrieval handle, in place, while the
byte-exact original stays in the Postgres session store. These tests pin every
§2.6 invariant the plan calls load-bearing:

  * tool_call ↔ tool_result pairing preserved; ``function.arguments`` untouched;
  * the newest user message is never evicted;
  * Tier-1 is a pure in-place body swap — message count, order, and role
    alternation are byte-identical to the input;
  * stub handles round-trip through ``context_expand`` to the exact original;
  * a second pass does not re-evict stubs (idempotent);
  * recently-expanded (hot) handles survive;
  * tool messages with no persisted row yet are skipped;
  * an eviction-only pass skips session rotation while a Tier-2 pass rotates;
  * the ``clear_at_least`` floor forces Tier-2 fall-through.

PG-backed tests use ``thoth_db_initialized_sync`` (test PG on localhost:5433)
and seed tool rows through the SyncSessionDB shim, mirroring the 2a suite.
"""

import json
import re
import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from agent.context_compressor import EVICTION_STUB_PREFIX
from agent.context_engine_substrate import (
    SubstrateContextEngine,
    _make_handle,
    _parse_handle,
)
from thoth_state import _AsyncSessionDB
from tests._helpers.sync_session_db import SyncSessionDB


# ---------------------------------------------------------------------------
# Construction helpers (offline — no model-metadata network lookups)
# ---------------------------------------------------------------------------

@contextmanager
def _no_ctx_probe(context_length: int = 200000):
    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=context_length,
    ):
        yield


def _make_engine(**kwargs) -> SubstrateContextEngine:
    kwargs.setdefault("model", "test/model")
    kwargs.setdefault("quiet_mode", True)
    with _no_ctx_probe():
        eng = SubstrateContextEngine(**kwargs)
    # Shrink the compaction knobs so a tiny fixture crosses the thresholds.
    # (The organ floors threshold at MINIMUM_CONTEXT_LENGTH; override directly.)
    eng.threshold_tokens = 3000                 # target = 0.6 * 3000 = 1800 tokens
    eng._compressor.tail_token_budget = 40      # tail protects only the min 3 msgs
    eng.protect_first_n = 1                     # head = system + 1
    eng._evict_min_chars = 200                  # small tool results still qualify
    eng._evict_min_reclaim = 50                 # low floor — eviction alone clears it
    return eng


def _assistant_call(cid: str, tool: str = "terminal", args: str = '{"cmd":"ls"}'):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": cid,
            "type": "function",
            "function": {"name": tool, "arguments": args},
        }],
    }


def _tool_result(cid: str, content: str, tool: str = "terminal"):
    return {"role": "tool", "tool_call_id": cid, "tool_name": tool, "content": content}


@pytest.fixture
def db(thoth_db_initialized_sync):
    return SyncSessionDB(_AsyncSessionDB())


def _seed_tool_row(db, session_id: str, cid: str, content: str, tool: str = "terminal") -> int:
    """Persist one tool row and return its message id (the handle target)."""
    return db.append_message(
        session_id, role="tool", content=content, tool_name=tool, tool_call_id=cid,
    )


def _build_conversation(big_a: str, big_b: str):
    """A 10-message conversation whose two large MIDDLE tool results (call_a
    idx 3, call_b idx 5) are eviction candidates.

    A third tool group (call_c, also large) sits just before the tail: the
    organ's tail-cut helper breaks the token walk at the last large tool
    result and ``_align_boundary_backward`` pulls the whole call_c group into
    the protected tail — so call_c is the sacrificial last group that keeps
    call_a and call_b squarely in the evictable middle band. (It must be large;
    a small call_c would shift the break point back onto call_b and protect it
    instead.) The raised test threshold keeps pressure-relief reachable despite
    call_c staying resident.
    """
    return [
        {"role": "system", "content": "System prompt"},        # 0 head
        {"role": "user", "content": "start the task"},          # 1 head
        _assistant_call("call_a"),                              # 2
        _tool_result("call_a", big_a),                          # 3 candidate
        _assistant_call("call_b"),                              # 4
        _tool_result("call_b", big_b),                          # 5 candidate
        _assistant_call("call_c"),                              # 6
        _tool_result("call_c", "CCC " + "c" * 4000),            # 7 last group → tail
        {"role": "user", "content": "newest question"},         # 8 tail (newest user)
        {"role": "assistant", "content": "here is the answer"}, # 9 tail
    ]


def _stub_handle(stub_text: str) -> str:
    m = re.search(r'context_expand\("([^"]+)"\)', stub_text)
    assert m, f"stub has no retrievable handle: {stub_text!r}"
    return m.group(1)


# ---------------------------------------------------------------------------
# Tier-1 eviction — full compress() path, eviction-only (no Tier 2)
# ---------------------------------------------------------------------------

class TestTier1EvictionOnly:
    def _run(self, db, session_id="s_evict"):
        db.create_session(session_id, source="cli")
        big_a = "AAA " + ("a" * 4000)
        big_b = "BBB " + ("b" * 4000)
        mid_a = _seed_tool_row(db, session_id, "call_a", big_a)
        mid_b = _seed_tool_row(db, session_id, "call_b", big_b)
        engine = _make_engine()
        engine.on_session_start(session_id, platform="cli")
        msgs = _build_conversation(big_a, big_b)
        out = engine.compress(list(msgs))
        return engine, msgs, out, (mid_a, mid_b), (big_a, big_b), session_id

    def test_eviction_only_flag_and_no_tier2(self, db):
        engine, msgs, out, _, _, _ = self._run(db)
        # Tier 0+1 relieved pressure without summarising → eviction-only.
        assert engine._last_compress_eviction_only is True
        # The organ never ran, so its compaction counter is untouched.
        assert engine.compression_count == 0

    def test_message_count_and_order_unchanged(self, db):
        # Tier-1 is a pure in-place body swap (plan §2.6).
        engine, msgs, out, _, _, _ = self._run(db)
        assert len(out) == len(msgs)
        assert [m["role"] for m in out] == [m["role"] for m in msgs]

    def test_candidates_became_stubs(self, db):
        engine, msgs, out, _, _, _ = self._run(db)
        # Indices 3 and 5 (the two big middle tool results) are now stubs.
        assert out[3]["content"].startswith(EVICTION_STUB_PREFIX)
        assert out[5]["content"].startswith(EVICTION_STUB_PREFIX)
        # tool_call_id and role are preserved on the stubbed messages.
        assert out[3]["role"] == "tool" and out[3]["tool_call_id"] == "call_a"
        assert out[5]["role"] == "tool" and out[5]["tool_call_id"] == "call_b"

    def test_newest_user_message_never_evicted(self, db):
        engine, msgs, out, _, _, _ = self._run(db)
        # The newest user message (index 8) is untouched — it is in the
        # protected tail and is never a tool message anyway.
        assert out[8]["role"] == "user"
        assert out[8]["content"] == "newest question"
        assert EVICTION_STUB_PREFIX not in json.dumps(out[8])

    def test_tool_call_pairing_preserved(self, db):
        engine, msgs, out, _, _, _ = self._run(db)
        assistant_ids = {
            tc["id"]
            for m in out if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or [])
        }
        tool_result_ids = {m["tool_call_id"] for m in out if m.get("role") == "tool"}
        # Every tool result still has its matching assistant tool_call and v.v.
        # (call_c is the protected tail group; still paired, never stubbed.)
        assert tool_result_ids == assistant_ids == {"call_a", "call_b", "call_c"}

    def test_function_arguments_untouched_valid_json(self, db):
        engine, msgs, out, _, _, _ = self._run(db)
        for m in out:
            if m.get("role") != "assistant":
                continue
            for tc in m.get("tool_calls") or []:
                # Assistant tool_calls are never touched by Tier 1.
                json.loads(tc["function"]["arguments"])  # raises if mangled

    def test_stub_handle_round_trips_byte_exact(self, db):
        engine, msgs, out, ids, bigs, _ = self._run(db)
        big_a, big_b = bigs
        for idx, original in ((3, big_a), (5, big_b)):
            handle = _stub_handle(out[idx]["content"])
            assert _parse_handle(handle) is not None
            expanded = json.loads(engine.handle_tool_call(
                "context_expand", {"handle": handle}, db=db,
            ))
            assert expanded["content"] == original      # byte-exact original
            assert "truncated" not in expanded

    def test_stub_names_tool_and_original_length(self, db):
        engine, msgs, out, _, bigs, _ = self._run(db)
        stub = out[3]["content"]
        assert "terminal" in stub
        assert f"{len(bigs[0]):,} chars" in stub
        assert "context_expand(" in stub

    def test_stub_carries_structural_gist_not_body_prefix(self, db):
        # Round-3: the stub gist is the content-aware STRUCTURAL summary, not a
        # raw 120-char body prefix. The structural gist annotates counts and a
        # head section — the marker "head:" never appeared in the old prefix.
        engine, msgs, out, _, _, _ = self._run(db)
        stub = out[3]["content"]
        assert "head:" in stub
        assert "chars" in stub

    def test_stub_gist_preserves_file_header_lines(self, db):
        # The c2 license-header fix, end to end: an evicted *file read* keeps its
        # first lines (the license header) VERBATIM in the stub gist, so the
        # model can still imitate the header pattern after the body is evicted.
        db.create_session("s_hdr", source="cli")
        header = (
            "# Copyright 2026 Example Corp.\n"
            "# SPDX-License-Identifier: Apache-2.0\n"
            '"""Module docstring."""\n'
        )
        big = header + "\n".join(f"def fn{i}():\n    return {i}" for i in range(200))
        other = "BBB " + ("b" * 4000)
        _seed_tool_row(db, "s_hdr", "call_a", big, tool="read_file")
        _seed_tool_row(db, "s_hdr", "call_b", other)
        engine = _make_engine()
        engine.on_session_start("s_hdr", platform="cli")
        msgs = _build_conversation(big, other)
        # Mark call_a as a file read so the gist takes the file shape.
        msgs[2]["tool_calls"][0]["function"]["name"] = "read_file"
        msgs[3]["tool_name"] = "read_file"
        out = engine.compress(msgs)
        stub = out[3]["content"]
        assert stub.startswith(EVICTION_STUB_PREFIX)
        assert "# Copyright 2026 Example Corp." in stub
        assert "# SPDX-License-Identifier: Apache-2.0" in stub

    def test_tier0_leaves_bodies_for_restorable_tier1(self, db):
        # With a small protect_last_n the organ's Tier-0 pass would normally
        # summarise old tool results into lossy 1-line strings (no handle). The
        # engine disables that (summarize_tool_results=False) so Tier 1 evicts
        # them restorably: the candidate is a handle-stub that round-trips
        # byte-exact, NOT a paraphrase.
        db.create_session("s_t0", source="cli")
        big_a = "AAA " + ("a" * 4000)
        big_b = "BBB " + ("b" * 4000)
        _seed_tool_row(db, "s_t0", "call_a", big_a)
        _seed_tool_row(db, "s_t0", "call_b", big_b)
        engine = _make_engine()
        engine.protect_last_n = 2  # would expose call_a/call_b to a Tier-0 summary
        engine.on_session_start("s_t0", platform="cli")
        out = engine.compress(_build_conversation(big_a, big_b))
        assert out[3]["content"].startswith(EVICTION_STUB_PREFIX)
        handle = _stub_handle(out[3]["content"])
        expanded = json.loads(engine.handle_tool_call(
            "context_expand", {"handle": handle}, db=db,
        ))
        assert expanded["content"] == big_a  # byte-exact — never paraphrased

    def test_second_tier1_pass_reevicts_nothing(self, db):
        # Idempotency (plan §2.6): re-running Tier 1 over an already-stubbed
        # list mints no new stubs — existing stubs are not candidates. Driving
        # _evict_tier1 directly isolates this from Tier-2 fall-through (which a
        # full second compress() would trigger, since zero reclaim < floor).
        engine, msgs, out, _, _, _ = self._run(db)
        stub_a, stub_b = out[3]["content"], out[5]["content"]
        assert stub_a.startswith(EVICTION_STUB_PREFIX)
        result2, reclaimed2, n_evicted2, _ = engine._evict_tier1(
            list(out), db, target_tokens=0, start_est=10_000,
        )
        assert n_evicted2 == 0
        assert reclaimed2 == 0
        assert result2[3]["content"] == stub_a   # stub untouched
        assert result2[5]["content"] == stub_b


# ---------------------------------------------------------------------------
# _evict_tier1 unit tests — skip/hot-page logic isolated from Tier-2
# ---------------------------------------------------------------------------

class TestEvictTier1Internals:
    def _fixture(self, db, session_id, seed_b=True):
        db.create_session(session_id, source="cli")
        big_a = "AAA " + ("a" * 4000)
        big_b = "BBB " + ("b" * 4000)
        mid_a = _seed_tool_row(db, session_id, "call_a", big_a)
        mid_b = _seed_tool_row(db, session_id, "call_b", big_b) if seed_b else None
        engine = _make_engine()
        engine.on_session_start(session_id, platform="cli")
        msgs = _build_conversation(big_a, big_b)
        return engine, msgs, mid_a, mid_b

    def test_unpersisted_message_is_skipped(self, db):
        # call_b has NO row in the store → must be skipped (never evict content
        # that isn't durably retrievable). target=0 forces the loop through all
        # candidates so the skip is exercised, not short-circuited.
        engine, msgs, mid_a, _ = self._fixture(db, "s_skip", seed_b=False)
        result, reclaimed, n_evicted, n_skipped = engine._evict_tier1(
            list(msgs), db, target_tokens=0, start_est=10_000,
        )
        assert n_skipped == 1
        assert n_evicted == 1
        assert result[3]["content"].startswith(EVICTION_STUB_PREFIX)   # call_a evicted
        assert result[5]["content"] == msgs[5]["content"]              # call_b intact

    def test_hot_page_handle_survives(self, db):
        # A handle dereferenced recently (context_expand) is exempt this pass.
        engine, msgs, mid_a, mid_b = self._fixture(db, "s_hot")
        hot_handle = _make_handle("s_hot", mid_a)
        engine._record_expanded(hot_handle)          # model paged call_a back in
        result, reclaimed, n_evicted, n_skipped = engine._evict_tier1(
            list(msgs), db, target_tokens=0, start_est=10_000,
        )
        assert result[3]["content"] == msgs[3]["content"]             # call_a hot → live
        assert result[5]["content"].startswith(EVICTION_STUB_PREFIX)  # call_b evicted

    def test_hot_page_expires_after_window(self, db):
        engine, msgs, mid_a, mid_b = self._fixture(db, "s_hotexp")
        engine._record_expanded(_make_handle("s_hotexp", mid_a))  # stamped at pass 0
        # Advance the eviction-pass clock past the hot window.
        engine._eviction_pass_count = engine._evict_hot_window + 1
        result, _, n_evicted, _ = engine._evict_tier1(
            list(msgs), db, target_tokens=0, start_est=10_000,
        )
        assert result[3]["content"].startswith(EVICTION_STUB_PREFIX)  # no longer hot
        assert n_evicted == 2

    def test_below_size_floor_not_evicted(self, db):
        db.create_session("s_floor", source="cli")
        small = "tiny output"
        _seed_tool_row(db, "s_floor", "call_a", small)
        big_b = "BBB " + ("b" * 4000)
        _seed_tool_row(db, "s_floor", "call_b", big_b)
        engine = _make_engine()
        engine._evict_min_chars = 1000
        engine.on_session_start("s_floor", platform="cli")
        msgs = _build_conversation(small, big_b)
        result, _, n_evicted, _ = engine._evict_tier1(
            list(msgs), db, target_tokens=0, start_est=10_000,
        )
        assert result[3]["content"] == small                          # under floor
        assert result[5]["content"].startswith(EVICTION_STUB_PREFIX)  # over floor
        assert n_evicted == 1


# ---------------------------------------------------------------------------
# clear_at_least floor → Tier-2 fall-through
# ---------------------------------------------------------------------------

class TestClearAtLeastFloor:
    def test_subfloor_reclaim_falls_through_to_tier2(self, db):
        db.create_session("s_floor2", source="cli")
        big_a = "AAA " + ("a" * 4000)
        big_b = "BBB " + ("b" * 4000)
        _seed_tool_row(db, "s_floor2", "call_a", big_a)
        _seed_tool_row(db, "s_floor2", "call_b", big_b)
        engine = _make_engine()
        # Floor set impossibly high: even evicting everything can't reach it, so
        # the pass must escalate to Tier 2 rather than settle for eviction-only.
        engine._evict_min_reclaim = 10_000_000
        engine.on_session_start("s_floor2", platform="cli")
        msgs = _build_conversation(big_a, big_b)
        out = engine.compress(list(msgs))
        assert engine._last_compress_eviction_only is False   # NOT eviction-only
        assert engine.compression_count == 1                  # organ (Tier 2) ran
        # Tier 2 restructures — the middle collapses, so the list shrinks.
        assert len(out) < len(msgs)


# ---------------------------------------------------------------------------
# compress_context seam — eviction-only skips rotation, Tier-2 rotates
# ---------------------------------------------------------------------------

class TestCompressContextRotationSeam:
    """The seam is a single getattr in compress_context; drive it with a
    stubbed engine.compress so we control the flag without needing PG here."""

    def _fake_agent(self, engine):
        agent = MagicMock()
        agent.context_compressor = engine
        agent.session_id = "sess_orig"
        agent.model = "test/model"
        agent.platform = "cli"
        agent.tools = []
        agent._memory_manager = None
        agent._compression_feasibility_checked = True
        agent._session_init_model_config = None
        agent._build_system_prompt = lambda s: "SYSTEM PROMPT"
        agent._cached_system_prompt = None
        agent._todo_store.format_for_injection.return_value = ""  # no todo append
        agent.commit_memory_session = lambda m: None
        # A truthy session_db whose ops return plain (non-awaitable) values so
        # the rotation block runs cleanly on the Tier-2 path.
        sdb = MagicMock()
        sdb.get_session_title.return_value = None
        sdb.end_session.return_value = None
        sdb.create_session.return_value = None
        sdb.update_system_prompt.return_value = None
        agent._session_db = sdb
        return agent

    def test_eviction_only_pass_does_not_rotate(self):
        from agent.conversation_compression import compress_context
        engine = _make_engine()

        def fake_compress(messages, **kw):
            engine._last_compress_eviction_only = True   # Tier 0+1 sufficed
            engine._compressor._last_compress_aborted = False
            return list(messages)                        # in-place, same count

        engine.compress = fake_compress
        agent = self._fake_agent(engine)
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
        out, sp = compress_context(agent, list(msgs), "sys")
        # Same session, same message count — no rotation, no todo snapshot.
        assert agent.session_id == "sess_orig"
        assert len(out) == len(msgs)
        assert sp == "SYSTEM PROMPT"

    def test_tier2_pass_rotates_session(self):
        from agent.conversation_compression import compress_context
        engine = _make_engine()

        def fake_compress(messages, **kw):
            engine._last_compress_eviction_only = False  # Tier 2 ran
            engine._compressor._last_compress_aborted = False
            return list(messages)[:1]                    # summarised → shorter

        engine.compress = fake_compress
        agent = self._fake_agent(engine)
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
        out, sp = compress_context(agent, list(msgs), "sys")
        # Normal summarisation path rotated the session id.
        assert agent.session_id != "sess_orig"


# ---------------------------------------------------------------------------
# Degraded fall-through — no session / DB → pure Tier-2 (2a-identical)
# ---------------------------------------------------------------------------

class TestTelemetrySeam:
    """The ``context.evicted`` emit is behind a try/except ImportError because
    ``agent.context_telemetry`` ships on the Phase-0b branch and is absent
    here. When it IS present the pass must emit one event with the pass stats;
    when absent, eviction must still work (covered implicitly everywhere else).
    """

    def test_emits_context_evicted_when_module_present(self, db, monkeypatch):
        calls = []
        fake = types.ModuleType("agent.context_telemetry")
        fake.emit = lambda event, **fields: calls.append((event, fields))
        monkeypatch.setitem(sys.modules, "agent.context_telemetry", fake)
        import agent as _agent_pkg
        monkeypatch.setattr(_agent_pkg, "context_telemetry", fake, raising=False)

        db.create_session("s_tel", source="cli")
        big_a = "AAA " + ("a" * 4000)
        big_b = "BBB " + ("b" * 4000)
        _seed_tool_row(db, "s_tel", "call_a", big_a)
        _seed_tool_row(db, "s_tel", "call_b", big_b)
        engine = _make_engine()
        engine.on_session_start("s_tel", platform="cli")
        engine.compress(_build_conversation(big_a, big_b))

        assert len(calls) == 1
        event, fields = calls[0]
        assert event == "context.evicted"
        assert fields["evicted"] == 2
        assert fields["reclaimed"] > 0
        assert fields["session_id"] == "s_tel"
        assert fields["escalated_to_tier2"] is False


class TestDegradedFallThrough:
    def test_no_session_id_delegates_to_organ(self):
        # Engine not attached to a session → eviction disabled, pure delegation.
        engine = _make_engine()
        assert engine._session_id is None
        with patch.object(engine._compressor, "compress", return_value=["X"]) as m:
            result = engine.compress([{"role": "user", "content": "hi"}])
        m.assert_called_once()
        assert result == ["X"]
        assert engine._last_compress_eviction_only is False

    def test_env_disable_evicts_nothing(self, db):
        db.create_session("s_off", source="cli")
        big_a = "AAA " + ("a" * 4000)
        _seed_tool_row(db, "s_off", "call_a", big_a)
        engine = _make_engine()
        engine.on_session_start("s_off", platform="cli")
        with patch.dict("os.environ", {"CONTEXT_EVICT": "0"}):
            with patch.object(engine._compressor, "compress", return_value=["X"]) as m:
                result = engine.compress(_build_conversation(big_a, "b" * 4000))
        m.assert_called_once()
        assert result == ["X"]
