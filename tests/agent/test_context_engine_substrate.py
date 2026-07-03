"""Tests for the Phase-2a SubstrateContextEngine.

Covers:
  * engine selection via config ``context.engine="substrate"`` at the
    ``init_agent`` seam;
  * delegation equivalence — compaction behaviour is byte-identical to a bare
    ContextCompressor on the same fixture (the 2a invariant);
  * verbatim handle round-trip against the PG session store (byte-exact fetch,
    ±window neighbors, content cap + truncation marker);
  * ``context_grep`` scoped to the current session (no cross-session leak);
  * error paths (malformed handle, missing message) return error strings, never
    raise.

PG-backed tests use the ``thoth_db_initialized_sync`` fixture (test PG on
localhost:5433) and the SyncSessionDB shim, mirroring
``tests/tools/test_session_search.py``.
"""

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor
from agent.context_engine import ContextEngine
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
        return SubstrateContextEngine(**kwargs)


def _make_compressor(**kwargs) -> ContextCompressor:
    kwargs.setdefault("model", "test/model")
    kwargs.setdefault("quiet_mode", True)
    with _no_ctx_probe():
        return ContextCompressor(**kwargs)


# ---------------------------------------------------------------------------
# Identity / handle grammar
# ---------------------------------------------------------------------------

class TestIdentity:
    def test_is_context_engine_named_substrate(self):
        engine = _make_engine()
        assert isinstance(engine, ContextEngine)
        assert engine.name == "substrate"

    def test_owns_internal_compressor_organ(self):
        engine = _make_engine()
        assert isinstance(engine._compressor, ContextCompressor)
        assert engine._compressor.name == "compressor"

    def test_tool_schemas_shape(self):
        engine = _make_engine()
        schemas = engine.get_tool_schemas()
        names = [s["name"] for s in schemas]
        assert names == ["context_expand", "context_grep"]
        # Handle format documented so the model can reuse handles from stubs.
        for s in schemas:
            assert "sid:<session_id>#m:<message_id>" in s["description"]
            assert "parameters" in s and s["parameters"]["type"] == "object"
        expand = schemas[0]
        assert expand["parameters"]["required"] == ["handle"]
        assert "window" in expand["parameters"]["properties"]
        grep = schemas[1]
        assert grep["parameters"]["required"] == ["pattern"]

    def test_handle_roundtrip_grammar(self):
        h = _make_handle("20260703_120000_ab12cd", 4242)
        assert h == "sid:20260703_120000_ab12cd#m:4242"
        assert _parse_handle(h) == ("20260703_120000_ab12cd", 4242)

    @pytest.mark.parametrize("bad", ["", "nope", "sid:x#m:", "m:5", "sid:x#m:abc", None, 5])
    def test_parse_handle_rejects_malformed(self, bad):
        assert _parse_handle(bad) is None


# ---------------------------------------------------------------------------
# Token-state delegation (single source of truth = the inner compressor)
# ---------------------------------------------------------------------------

class TestTokenStateDelegation:
    def test_token_fields_write_through_to_organ(self):
        engine = _make_engine()
        engine.last_prompt_tokens = 12345
        engine.last_completion_tokens = 67
        assert engine._compressor.last_prompt_tokens == 12345
        assert engine._compressor.last_completion_tokens == 67
        # ...and reads reflect the organ.
        engine._compressor.threshold_tokens = 999
        assert engine.threshold_tokens == 999

    def test_private_state_reads_fall_through(self):
        engine = _make_engine()
        # compress_context reads these off the engine via getattr — they must
        # resolve to the organ's attributes.
        assert engine._last_compress_aborted is False
        assert engine.abort_on_summary_failure is False
        assert engine.quiet_mode is True

    def test_update_from_response_delegates(self):
        engine = _make_engine()
        engine.update_from_response({"prompt_tokens": 5000, "completion_tokens": 100})
        assert engine.last_prompt_tokens == 5000
        assert engine._compressor.last_prompt_tokens == 5000

    def test_on_session_start_captures_session_id(self):
        engine = _make_engine()
        engine.on_session_start("sess_abc", platform="cli", model="test/model")
        assert engine._session_id == "sess_abc"
        # A compression-driven rotation updates it in place.
        engine.on_session_start("sess_def", boundary_reason="compression")
        assert engine._session_id == "sess_def"


# ---------------------------------------------------------------------------
# Delegation equivalence — behaviour identical to a bare ContextCompressor
# ---------------------------------------------------------------------------

class TestDelegationEquivalence:
    _PARAMS = dict(
        threshold_percent=0.85,
        protect_first_n=2,
        protect_last_n=2,
        config_context_length=100000,
    )

    def _messages(self, n):
        return [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
            for i in range(n)
        ]

    def test_should_compress_equivalent(self):
        engine = _make_engine(**self._PARAMS)
        bare = _make_compressor(**self._PARAMS)
        for tokens in (10000, 84999, 85000, 90000, 200000):
            assert engine.should_compress(tokens) == bare.should_compress(tokens)

    def test_compress_produces_identical_output(self):
        # No aux provider configured → both take the deterministic static
        # fallback path (abort_on_summary_failure defaults False). Output must
        # be identical message-for-message.
        engine = _make_engine(**self._PARAMS)
        bare = _make_compressor(**self._PARAMS)
        msgs = [{"role": "system", "content": "System prompt"}] + self._messages(12)

        out_engine = engine.compress(list(msgs))
        out_bare = bare.compress(list(msgs))

        assert out_engine == out_bare
        assert len(out_engine) < len(msgs)
        assert engine.compression_count == bare.compression_count == 1
        # The fallback path is exercised identically on both.
        assert engine._last_summary_fallback_used == bare._last_summary_fallback_used

    def test_has_content_to_compress_equivalent(self):
        engine = _make_engine(**self._PARAMS)
        bare = _make_compressor(**self._PARAMS)
        msgs = [{"role": "system", "content": "sp"}] + self._messages(12)
        assert engine.has_content_to_compress(msgs) == bare.has_content_to_compress(msgs)


# ---------------------------------------------------------------------------
# Engine selection at the init_agent seam
# ---------------------------------------------------------------------------

class TestEngineSelection:
    """config context.engine="substrate" → agent.context_compressor is the engine."""

    def _build_agent(self, engine_name):
        from unittest.mock import MagicMock
        from run_agent import AIAgent

        cfg = {"context": {"engine": engine_name}}
        tool_defs = [{
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "search",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        with (
            patch("thoth_cli.config.load_config", return_value=cfg),
            patch("run_agent.get_tool_definitions", return_value=tool_defs),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            a = AIAgent(
                api_key="test-key-1234567890",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        return a

    def test_substrate_engine_selected(self):
        agent = self._build_agent("substrate")
        assert isinstance(agent.context_compressor, SubstrateContextEngine)
        assert agent.context_compressor.name == "substrate"

    def test_default_is_compressor(self):
        agent = self._build_agent("compressor")
        assert isinstance(agent.context_compressor, ContextCompressor)
        assert agent.context_compressor.name == "compressor"

    def test_retrieval_tools_injected_by_default(self):
        # enabled_toolsets defaults to None → context engine tools ARE injected.
        agent = self._build_agent("substrate")
        tool_names = {
            t.get("function", {}).get("name")
            for t in agent.tools
            if isinstance(t, dict)
        }
        assert {"context_expand", "context_grep"} <= tool_names
        assert {"context_expand", "context_grep"} <= agent._context_engine_tool_names


# ---------------------------------------------------------------------------
# Verbatim handle round-trip (PG-backed)
# ---------------------------------------------------------------------------

@pytest.fixture
def db(thoth_db_initialized_sync):
    return SyncSessionDB(_AsyncSessionDB())


class TestContextExpand:
    def test_byte_exact_large_tool_message(self, db):
        """A ~50KB tool-role message round-trips byte-exact when under the cap."""
        db.create_session("s_expand", source="cli")
        big = "TOOLDATA-" + ("x" * 50_000)
        mid = db.append_message(
            "s_expand", role="tool", content=big, tool_name="terminal",
            tool_call_id="call_1",
        )
        # Big cap so the 50KB content is not truncated — proves byte-exactness.
        engine = _make_engine(expand_max_chars=200_000)
        handle = _make_handle("s_expand", mid)
        out = json.loads(engine.handle_tool_call("context_expand", {"handle": handle}, db=db))

        assert out["handle"] == handle
        assert out["role"] == "tool"
        assert out["tool_name"] == "terminal"
        assert out["content"] == big  # byte-exact
        assert "truncated" not in out

    def test_content_cap_truncates_with_marker(self, db):
        db.create_session("s_cap", source="cli")
        big = "y" * 50_000
        mid = db.append_message("s_cap", role="tool", content=big, tool_name="read_file")
        engine = _make_engine(expand_max_chars=20_000)
        handle = _make_handle("s_cap", mid)
        out = json.loads(engine.handle_tool_call("context_expand", {"handle": handle}, db=db))

        assert out["truncated"] is True
        assert out["content"].startswith("y" * 20_000)
        assert "truncated" in out["content"]  # explicit marker text
        assert handle in out["content"]        # marker repeats the handle
        assert len(out["content"]) < len(big)  # actually shorter than original

    def test_window_returns_neighbors(self, db):
        db.create_session("s_win", source="cli")
        ids = [
            db.append_message("s_win", role="user", content="first question"),
            db.append_message("s_win", role="assistant", content="middle answer"),
            db.append_message("s_win", role="user", content="third question"),
        ]
        engine = _make_engine()
        handle = _make_handle("s_win", ids[1])  # anchor on the middle message
        out = json.loads(engine.handle_tool_call(
            "context_expand", {"handle": handle, "window": 1}, db=db,
        ))

        assert out["content"] == "middle answer"
        assert "neighbors" in out
        neighbor_handles = {n["handle"] for n in out["neighbors"]}
        assert neighbor_handles == {
            _make_handle("s_win", ids[0]),
            _make_handle("s_win", ids[2]),
        }

    def test_window_zero_no_neighbors(self, db):
        db.create_session("s_win0", source="cli")
        a = db.append_message("s_win0", role="user", content="alpha")
        db.append_message("s_win0", role="assistant", content="beta")
        engine = _make_engine()
        out = json.loads(engine.handle_tool_call(
            "context_expand", {"handle": _make_handle("s_win0", a)}, db=db,
        ))
        assert out["content"] == "alpha"
        assert "neighbors" not in out


class TestContextExpandErrors:
    def test_malformed_handle_returns_error(self, db):
        engine = _make_engine()
        out = json.loads(engine.handle_tool_call(
            "context_expand", {"handle": "not-a-handle"}, db=db,
        ))
        assert "error" in out
        assert "malformed" in out["error"]

    def test_missing_message_returns_error(self, db):
        db.create_session("s_missing", source="cli")
        engine = _make_engine()
        handle = _make_handle("s_missing", 999_999)
        out = json.loads(engine.handle_tool_call("context_expand", {"handle": handle}, db=db))
        assert "error" in out
        assert "no message found" in out["error"]

    def test_unknown_tool_name_returns_error(self, db):
        engine = _make_engine()
        out = json.loads(engine.handle_tool_call("context_nope", {}, db=db))
        assert "error" in out


# ---------------------------------------------------------------------------
# context_grep — FTS scoped to the current session lineage
# ---------------------------------------------------------------------------

class TestContextGrep:
    def test_grep_returns_handle_and_snippet(self, db):
        db.create_session("s_grep", source="cli")
        db.append_message("s_grep", role="user", content="please investigate the pumpernickel bug")
        target = db.append_message(
            "s_grep", role="assistant", content="the pumpernickel loader crashed on boot",
        )
        engine = _make_engine()
        out = json.loads(engine.handle_tool_call(
            "context_grep", {"pattern": "pumpernickel"}, db=db, current_session_id="s_grep",
        ))
        assert out["count"] >= 1
        handles = {m["handle"] for m in out["matches"]}
        assert _make_handle("s_grep", target) in handles
        # Every match carries a snippet and a valid handle.
        for m in out["matches"]:
            assert _parse_handle(m["handle"]) is not None
            assert "snippet" in m

    def test_grep_scoped_to_current_session(self, db):
        # Two unrelated sessions share a search term; grep on A must not leak B.
        db.create_session("s_a", source="cli")
        db.append_message("s_a", role="assistant", content="quokka sighting in session A")
        db.create_session("s_b", source="cli")
        db.append_message("s_b", role="assistant", content="quokka sighting in session B")
        engine = _make_engine()
        out = json.loads(engine.handle_tool_call(
            "context_grep", {"pattern": "quokka"}, db=db, current_session_id="s_a",
        ))
        assert out["count"] >= 1
        for m in out["matches"]:
            sid, _ = _parse_handle(m["handle"])
            assert sid == "s_a"

    def test_grep_includes_parent_lineage(self, db):
        # A compression rotation makes s_child a child of s_parent; grep on the
        # child must still find the parent's evicted content.
        db.create_session("s_parent", source="cli")
        p_msg = db.append_message(
            "s_parent", role="tool", content="antidisestablishment config dumped", tool_name="terminal",
        )
        db.create_session("s_child", source="cli", parent_session_id="s_parent")
        db.append_message("s_child", role="user", content="continue working")
        engine = _make_engine()
        out = json.loads(engine.handle_tool_call(
            "context_grep", {"pattern": "antidisestablishment"}, db=db,
            current_session_id="s_child",
        ))
        handles = {m["handle"] for m in out["matches"]}
        assert _make_handle("s_parent", p_msg) in handles

    def test_grep_uses_captured_session_id(self, db):
        # No current_session_id passed → falls back to on_session_start capture.
        db.create_session("s_cap2", source="cli")
        t = db.append_message("s_cap2", role="assistant", content="flibbertigibbet result")
        engine = _make_engine()
        engine.on_session_start("s_cap2", platform="cli")
        out = json.loads(engine.handle_tool_call(
            "context_grep", {"pattern": "flibbertigibbet"}, db=db,
        ))
        handles = {m["handle"] for m in out["matches"]}
        assert _make_handle("s_cap2", t) in handles

    def test_grep_empty_pattern_errors(self, db):
        engine = _make_engine()
        out = json.loads(engine.handle_tool_call(
            "context_grep", {"pattern": "   "}, db=db, current_session_id="s_x",
        ))
        assert "error" in out
