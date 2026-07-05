"""Unit tests for decision-time expand nudges (agent/expand_nudge.py).

The expand-hint block re-states the recall-surfaced ``context_expand(...)``
eviction pointers as a short, capped, actionable user-role reminder at max
recency — reviving the dead reactive-retrieval path (``context.pagein`` measured
0 events / ~0.3 handle dereferences per task across four graded rounds; OPENDEV
arXiv 2603.05344 on decision-point user-role reminders).  These tests are
offline — no DB, no network, no model — using a fake agent + hand-built memory
blocks in the shipped stub grammar.
"""

import os

import pytest

from agent.expand_nudge import build_expand_nudge_block, _DEFAULT_NUDGE_MAX
from agent.memory_manager import (
    StreamingContextScrubber,
    sanitize_context,
    build_memory_context_block,
)


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #

class _FakeEngine:
    """Stand-in for the substrate/cooling engine exposing the hot-page set."""

    def __init__(self, name="substrate", expanded=None):
        self.name = name
        # Real engine holds an OrderedDict[handle -> pass_index]; the nudge only
        # needs the keys, so a plain dict is a faithful stand-in.
        if expanded is not None:
            self._expanded_handles = {h: 0 for h in expanded}


class _FakeAgent:
    """Minimal AIAgent stand-in — only the attr the nudge touches."""

    def __init__(self, engine=None):
        self.context_compressor = engine


def _pointer(handle, tool="browser_tool", gist="a short gist of the content"):
    """One recall-surfaced eviction pointer slice, in the shipped grammar."""
    return f'{tool}: {gist} — Retrieve exact: context_expand("{handle}")'


def _mem_block(*pointers):
    """Wrap pointer slices in a real <memory-context> fence."""
    return build_memory_context_block("\n".join(pointers))


def _messages():
    return [{"role": "user", "content": "do the task"}]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("THOTH_EXPAND_NUDGE", "CONTEXT_EXPAND_NUDGE_MAX"):
        monkeypatch.delenv(k, raising=False)
    yield


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

class TestParsing:
    def test_lists_both_pointers(self):
        agent = _FakeAgent(_FakeEngine())
        mem = _mem_block(
            _pointer("sid:abc#m:12", tool="browser_tool"),
            _pointer("sid:def#m:34", tool="shell_tool"),
        )
        block = build_expand_nudge_block(agent, mem, _messages())
        assert "<expand-hint>" in block and "</expand-hint>" in block
        assert 'context_expand("sid:abc#m:12")' in block
        assert 'context_expand("sid:def#m:34")' in block
        # System-note announces the count.
        assert "2 item(s)" in block

    def test_gist_echo_present_when_parseable(self):
        agent = _FakeAgent(_FakeEngine())
        mem = _mem_block(_pointer("sid:abc#m:12", tool="browser_tool", gist="page title XYZ"))
        block = build_expand_nudge_block(agent, mem, _messages())
        # Cheap echo: tool name + gist ride alongside the handle.
        assert "browser_tool" in block
        assert "page title XYZ" in block

    def test_dedupes_repeated_handle(self):
        agent = _FakeAgent(_FakeEngine())
        mem = _mem_block(_pointer("sid:abc#m:12"), _pointer("sid:abc#m:12"))
        block = build_expand_nudge_block(agent, mem, _messages())
        assert block.count('context_expand("sid:abc#m:12")') == 1
        assert "1 item(s)" in block


# --------------------------------------------------------------------------- #
# Cap
# --------------------------------------------------------------------------- #

class TestCap:
    def test_caps_at_default_first_n(self):
        agent = _FakeAgent(_FakeEngine())
        mem = _mem_block(*[_pointer(f"sid:s{i}#m:{i}") for i in range(5)])
        block = build_expand_nudge_block(agent, mem, _messages())
        listed = [ln for ln in block.splitlines() if ln.startswith("- context_expand(")]
        assert len(listed) == _DEFAULT_NUDGE_MAX == 3
        # First-N in projection order.
        assert 'context_expand("sid:s0#m:0")' in block
        assert 'context_expand("sid:s2#m:2")' in block
        assert 'context_expand("sid:s3#m:3")' not in block
        assert 'context_expand("sid:s4#m:4")' not in block

    def test_cap_env_override(self, monkeypatch):
        monkeypatch.setenv("CONTEXT_EXPAND_NUDGE_MAX", "2")
        agent = _FakeAgent(_FakeEngine())
        mem = _mem_block(*[_pointer(f"sid:s{i}#m:{i}") for i in range(5)])
        block = build_expand_nudge_block(agent, mem, _messages())
        listed = [ln for ln in block.splitlines() if ln.startswith("- context_expand(")]
        assert len(listed) == 2

    def test_bad_cap_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CONTEXT_EXPAND_NUDGE_MAX", "not-a-number")
        agent = _FakeAgent(_FakeEngine())
        mem = _mem_block(*[_pointer(f"sid:s{i}#m:{i}") for i in range(5)])
        block = build_expand_nudge_block(agent, mem, _messages())
        listed = [ln for ln in block.splitlines() if ln.startswith("- context_expand(")]
        assert len(listed) == _DEFAULT_NUDGE_MAX


# --------------------------------------------------------------------------- #
# Hot-page filtering
# --------------------------------------------------------------------------- #

class TestDereferencedFilter:
    def test_filters_already_dereferenced_handle(self):
        # Engine reports sid:abc#m:12 already paged in -> excluded from nudge.
        agent = _FakeAgent(_FakeEngine(expanded={"sid:abc#m:12"}))
        mem = _mem_block(_pointer("sid:abc#m:12"), _pointer("sid:def#m:34"))
        block = build_expand_nudge_block(agent, mem, _messages())
        assert 'context_expand("sid:abc#m:12")' not in block
        assert 'context_expand("sid:def#m:34")' in block
        assert "1 item(s)" in block

    def test_empty_when_all_dereferenced(self):
        agent = _FakeAgent(_FakeEngine(expanded={"sid:abc#m:12"}))
        mem = _mem_block(_pointer("sid:abc#m:12"))
        assert build_expand_nudge_block(agent, mem, _messages()) == ""

    def test_no_filter_when_engine_lacks_set(self):
        # Non-substrate engine (no _expanded_handles attr) -> no filtering.
        agent = _FakeAgent(_FakeEngine(name="compressor"))
        # _FakeEngine w/ expanded=None does not set the attr.
        mem = _mem_block(_pointer("sid:abc#m:12"))
        block = build_expand_nudge_block(agent, mem, _messages())
        assert 'context_expand("sid:abc#m:12")' in block

    def test_no_engine_at_all_is_safe(self):
        agent = _FakeAgent(engine=None)
        mem = _mem_block(_pointer("sid:abc#m:12"))
        block = build_expand_nudge_block(agent, mem, _messages())
        assert 'context_expand("sid:abc#m:12")' in block


# --------------------------------------------------------------------------- #
# Empty / guards
# --------------------------------------------------------------------------- #

class TestEmptyAndGuards:
    def test_empty_when_no_memory_block(self):
        agent = _FakeAgent(_FakeEngine())
        assert build_expand_nudge_block(agent, "", _messages()) == ""

    def test_empty_when_no_pointers_in_block(self):
        agent = _FakeAgent(_FakeEngine())
        mem = build_memory_context_block("just some recalled prose, no handles here")
        assert build_expand_nudge_block(agent, mem, _messages()) == ""

    def test_empty_when_killed_by_env(self, monkeypatch):
        monkeypatch.setenv("THOTH_EXPAND_NUDGE", "0")
        agent = _FakeAgent(_FakeEngine())
        mem = _mem_block(_pointer("sid:abc#m:12"))
        assert build_expand_nudge_block(agent, mem, _messages()) == ""


# --------------------------------------------------------------------------- #
# Injection wiring (mirror the conversation_loop api-copy-only ordering)
# --------------------------------------------------------------------------- #

class TestInjectionWiring:
    def test_nudge_appended_after_working_set_and_memory(self):
        """Reproduce the loop's injection assembly: memory -> working-set ->
        expand-hint, with expand-hint LAST (max recency). API copy only."""
        from agent.working_set import build_working_set_block

        class _WSAgent(_FakeAgent):
            def __init__(self, engine):
                super().__init__(engine)
                self._todo_store = None
                self._working_set_task_text = "Standing task X."

        agent = _WSAgent(_FakeEngine())
        pointer = _pointer("sid:abc#m:12")
        # Long-enough convo so working-set arms (>= default 3 user turns).
        messages = [
            {"role": "user", "content": "Standing task X."},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "next"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "more"},
        ]
        persisted = messages[-1]
        original = persisted["content"]

        # --- mirror conversation_loop.py api-message assembly ---
        api_msg = persisted.copy()
        injections = []
        mem = _mem_block(pointer)
        if mem:
            injections.append(mem)
        ws = build_working_set_block(agent, messages)
        if ws:
            injections.append(ws)
        nudge = build_expand_nudge_block(agent, mem, messages)
        if nudge:
            injections.append(nudge)
        api_msg["content"] = api_msg["content"] + "\n\n" + "\n\n".join(injections)

        content = api_msg["content"]
        assert "<memory-context>" in content
        assert "<working-set>" in content
        assert "<expand-hint>" in content
        # expand-hint is LAST (most recent): after both memory and working-set.
        assert content.index("<expand-hint>") > content.index("<working-set>")
        assert content.index("<expand-hint>") > content.index("<memory-context>")
        # Persisted history untouched — nothing leaks to session storage.
        assert persisted["content"] == original
        assert "<expand-hint>" not in persisted["content"]


# --------------------------------------------------------------------------- #
# Fence + scrubber strip the expand-hint block from streamed output
# --------------------------------------------------------------------------- #

class TestScrubberStripsExpandHint:
    def test_oneshot_sanitize_strips_expand_hint_block(self):
        leaked = (
            "<expand-hint>\n"
            "[System note: 2 item(s) from earlier in this task were condensed to summaries.]\n"
            '- context_expand("sid:abc#m:12")  — browser_tool: secret gist\n'
            "</expand-hint>\n\nVisible answer"
        )
        assert sanitize_context(leaked).strip() == "Visible answer"

    def test_stream_scrubber_strips_expand_hint_split_across_deltas(self):
        s = StreamingContextScrubber()
        deltas = [
            "Answer:\n",
            "<expand-hint>\n[System note: 1 item(s) ",
            'from earlier]\n- context_expand("sid:abc#m:12") — secret ',
            "gist text\n",
            "</expand-hint> done",
        ]
        out = "".join(s.feed(d) for d in deltas) + s.flush()
        assert out == "Answer:\n done"
        assert "secret" not in out
        assert "context_expand" not in out

    def test_stream_scrubber_strips_all_three_fences_in_one_stream(self):
        s = StreamingContextScrubber()
        text = (
            "intro\n"
            "<memory-context>\nmem payload\n</memory-context>\n"
            "middle\n"
            "<working-set>\nws payload\n</working-set>\n"
            "tail\n"
            "<expand-hint>\nhint payload\n</expand-hint>\n"
            "outro"
        )
        out = s.feed(text) + s.flush()
        assert "mem payload" not in out
        assert "ws payload" not in out
        assert "hint payload" not in out
        assert "intro" in out and "middle" in out and "tail" in out and "outro" in out
