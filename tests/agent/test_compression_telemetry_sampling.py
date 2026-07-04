"""Round-4 forensic finding B, part 2: consistent-basis compression telemetry.

~30% of ``context.compressed`` rows reported impossible NEGATIVE ``tokens_saved``
because ``tokens_before`` and ``tokens_after`` were sampled on DIFFERENT bases —
``tokens_before`` came from the caller's path-dependent ``approx_tokens`` (either
the real ``last_prompt_tokens`` or a messages-only estimate), while
``tokens_after`` was a schema-inclusive ``estimate_request_tokens_rough``. When
``before`` was messages-only and ``after`` was schema-inclusive, tool-schema
overhead alone made ``after > before`` even though summarisation removed content.

``compress_context`` now samples BOTH sides with ``estimate_request_tokens_rough``
(the wrapper ignores the caller's ``approx_tokens`` for telemetry), so
``tokens_saved`` is a single-basis measurement that cannot go negative for a
genuine pass. These tests drive the wrapper directly with a fake engine + a
stubbed ``agent.context_telemetry`` module.

Pure — no PG, no network, no model.
"""

import sys
import types

from agent.conversation_compression import compress_context
from agent.model_metadata import estimate_request_tokens_rough


class _FakeEngine:
    """Minimal stand-in for AIAgent.context_compressor on the summarise path."""

    def __init__(self, compressed):
        self._compressed = compressed
        self._last_compress_aborted = False
        self._last_compress_eviction_only = False
        self._last_summary_error = None
        self._last_aux_model_failure_model = None
        self._last_aux_model_failure_error = None
        self.compression_count = 1
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0

    def compress(self, messages, *, current_tokens=None, focus_topic=None, force=False):
        return list(self._compressed)


class _FakeTodoStore:
    def format_for_injection(self):
        return ""


class _FakeAgent:
    def __init__(self, engine):
        self.context_compressor = engine
        self.session_id = "sess-x"
        self.model = "test/model"
        self.platform = "cli"
        self.tools = None
        self._memory_manager = None
        self._session_db = None
        self._todo_store = _FakeTodoStore()
        self._cached_system_prompt = "OLD SYSTEM PROMPT"
        self._compression_feasibility_checked = True

    def _emit_status(self, *a, **k):
        pass

    def _emit_warning(self, *a, **k):
        pass

    def _vprint(self, *a, **k):
        pass

    def _invalidate_system_prompt(self):
        pass

    def _build_system_prompt(self, system_message):
        return "NEW SYSTEM PROMPT"


def _install_telemetry_stub(monkeypatch):
    calls = []
    fake = types.ModuleType("agent.context_telemetry")

    def _emit_compression_event(agent, **kwargs):
        calls.append(kwargs)

    fake.emit_compression_event = _emit_compression_event
    monkeypatch.setitem(sys.modules, "agent.context_telemetry", fake)
    import agent as _agent_pkg
    monkeypatch.setattr(_agent_pkg, "context_telemetry", fake, raising=False)
    return calls


def _big_history(n=20):
    return [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": f"turn {i} " + ("x" * 400)}
        for i in range(n)
    ]


def test_tokens_saved_never_negative_for_normal_pass(monkeypatch):
    calls = _install_telemetry_stub(monkeypatch)
    messages = _big_history(20)
    compressed = [messages[0], {"role": "assistant", "content": "summary"}, messages[-1]]
    agent = _FakeAgent(_FakeEngine(compressed))

    # approx_tokens is a deliberately TINY messages-only value — the exact input
    # that made the OLD code (tokens_before=approx_tokens, tokens_after=schema-
    # inclusive estimate) report negative savings. The fix must ignore it for
    # telemetry sampling.
    out, _sp = compress_context(agent, messages, "sys", approx_tokens=10)

    assert len(out) == len(compressed)
    assert len(calls) == 1
    ev = calls[0]
    assert ev["aborted"] is False
    saved = ev["tokens_before"] - ev["tokens_after"]
    assert saved >= 0, ev  # single-basis measurement — never negative
    assert saved > 0  # a genuine summarisation pass shed real tokens
    # tokens_before is the schema-inclusive estimate of the ORIGINAL messages,
    # NOT the tiny approx_tokens the caller passed.
    assert ev["tokens_before"] == estimate_request_tokens_rough(
        messages, system_prompt="OLD SYSTEM PROMPT", tools=None
    )
    assert ev["tokens_before"] != 10


def test_aborted_pass_reports_zero_not_negative(monkeypatch):
    calls = _install_telemetry_stub(monkeypatch)
    messages = _big_history(20)
    engine = _FakeEngine(list(messages))  # returns input unchanged
    engine._last_compress_aborted = True
    engine._last_summary_error = "aux model failed"
    agent = _FakeAgent(engine)

    out, _sp = compress_context(agent, messages, "sys", approx_tokens=10)

    assert out == messages  # no-op
    assert len(calls) == 1
    ev = calls[0]
    assert ev["aborted"] is True
    assert ev["tokens_before"] - ev["tokens_after"] == 0  # before == after, never negative
