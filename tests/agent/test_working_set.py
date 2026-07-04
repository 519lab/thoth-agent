"""Unit tests for working-set recitation (agent/working_set.py).

The working-set block re-states the session's original task + current plan into
the recency window every turn so a standing constraint can't decay with
positional distance (mem0 "context window is RAM" / Manus recitation).  These
tests are offline — no DB, no network — using a fake agent + hand-built
message lists.
"""

import os

import pytest

from agent.working_set import build_working_set_block, _TASK_ATTR
from agent.memory_manager import StreamingContextScrubber, sanitize_context


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class _FakeTodoStore:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def format_for_injection(self):
        return self._snapshot


class _FakeAgent:
    """Minimal stand-in for AIAgent — only the attrs working_set touches."""

    def __init__(self, todo_snapshot=None):
        self._todo_store = _FakeTodoStore(todo_snapshot)
        # No _working_set_task_text initially — captured on first build.


def _user(text):
    return {"role": "user", "content": text}


def _assistant(text):
    return {"role": "assistant", "content": text}


def _long_convo(first_task, n_turns=4):
    """A conversation with n genuine user turns (>= default min of 3)."""
    msgs = [_user(first_task)]
    for i in range(1, n_turns):
        msgs.append(_assistant(f"reply {i}"))
        msgs.append(_user(f"follow-up {i}"))
    return msgs


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Ensure knobs start from defaults for each test."""
    for k in ("THOTH_WORKING_SET", "WORKING_SET_MIN_TURNS", "WORKING_SET_TASK_MAX_CHARS"):
        monkeypatch.delenv(k, raising=False)
    yield


# --------------------------------------------------------------------------- #
# Block content
# --------------------------------------------------------------------------- #

class TestBlockContent:
    def test_contains_first_user_message_verbatim_not_second(self):
        agent = _FakeAgent()
        msgs = _long_convo("Build the parser and NEVER use regex.")
        block = build_working_set_block(agent, msgs)
        assert "<working-set>" in block
        assert "</working-set>" in block
        assert "Build the parser and NEVER use regex." in block
        # The second/later user turns must not be recited — only the original.
        assert "follow-up 1" not in block
        assert "## Original task & standing constraints" in block

    def test_todo_snapshot_included_when_present(self):
        snap = "[Your active task list was preserved]\n- [>] 1. write parser (in_progress)"
        agent = _FakeAgent(todo_snapshot=snap)
        block = build_working_set_block(agent, _long_convo("Do the thing."))
        assert "## Current plan / progress" in block
        assert "write parser" in block

    def test_todo_section_absent_when_empty(self):
        agent = _FakeAgent(todo_snapshot=None)
        block = build_working_set_block(agent, _long_convo("Do the thing."))
        assert "## Current plan / progress" not in block
        assert "Do the thing." in block

    def test_empty_when_under_min_turns(self):
        agent = _FakeAgent()
        # Only 1 genuine user turn — below default WORKING_SET_MIN_TURNS=3.
        block = build_working_set_block(agent, [_user("just a quick question")])
        assert block == ""

    def test_min_turns_env_override(self, monkeypatch):
        monkeypatch.setenv("WORKING_SET_MIN_TURNS", "2")
        agent = _FakeAgent()
        msgs = [_user("first task"), _assistant("ok"), _user("second")]
        block = build_working_set_block(agent, msgs)
        assert "first task" in block

    def test_empty_when_killed_by_env(self, monkeypatch):
        monkeypatch.setenv("THOTH_WORKING_SET", "0")
        agent = _FakeAgent()
        block = build_working_set_block(agent, _long_convo("A standing constraint."))
        assert block == ""

    def test_synthetic_first_message_is_skipped(self):
        """An injected memory/todo user block must not be mistaken for the task."""
        agent = _FakeAgent()
        msgs = [
            _user("<memory-context>\nrecalled stuff\n</memory-context>"),
            _assistant("ok"),
            _user("[Your active task list was preserved]\n- [ ] 1. x (pending)"),
            _assistant("ok"),
            _user("The REAL task: refactor the loop."),
            _assistant("ok"),
            _user("keep going"),
            _assistant("ok"),
            _user("and finish"),
        ]
        block = build_working_set_block(agent, msgs)
        assert "The REAL task: refactor the loop." in block
        assert "recalled stuff" not in block


# --------------------------------------------------------------------------- #
# Truncation
# --------------------------------------------------------------------------- #

class TestTruncation:
    def test_head_preserved_at_cap(self, monkeypatch):
        monkeypatch.setenv("WORKING_SET_TASK_MAX_CHARS", "50")
        agent = _FakeAgent()
        # Head deliberately exceeds the 50-char cap so the tail is fully cut.
        head = "HEAD-MARKER goal and constraints spelled out here at length "
        tail = "TAIL-MARKER should be dropped " * 20
        task = head + tail
        block = build_working_set_block(agent, _long_convo(task))
        assert "HEAD-MARKER" in block
        assert "TAIL-MARKER" not in block
        assert "truncated" in block.lower()

    def test_no_truncation_marker_when_under_cap(self):
        agent = _FakeAgent()
        block = build_working_set_block(agent, _long_convo("short task"))
        assert "truncated" not in block.lower()


# --------------------------------------------------------------------------- #
# Capture survives compression / session rotation
# --------------------------------------------------------------------------- #

class TestCaptureSurvivesCompression:
    def test_task_cached_on_agent_after_first_build(self):
        agent = _FakeAgent()
        build_working_set_block(agent, _long_convo("Original standing task."))
        assert getattr(agent, _TASK_ATTR) == "Original standing task."

    def test_capture_survives_messages_replaced_by_summary(self):
        """Simulate compression: the live messages list is replaced with a
        summary that no longer contains the original task.  The block must
        still recite the original from the agent-cached attribute."""
        agent = _FakeAgent()
        # Turn-1 build captures the original (pre-compression).
        build_working_set_block(agent, _long_convo("Original standing task: no globals."))

        # Compression rotates the session and replaces messages with a summary
        # head — the original user message is gone from the live list.
        compressed = [
            _user("[System note: prior context summarized] earlier work done"),
            _assistant("continuing"),
            _user("next step"),
            _assistant("ok"),
            _user("and another"),
        ]
        block = build_working_set_block(agent, compressed)
        assert "Original standing task: no globals." in block
        # The summary marker itself must not become the recited task text.
        recited = block.split("## Original task & standing constraints", 1)[1]
        assert "prior context summarized" not in recited

    def test_real_compaction_summary_prefix_is_not_captured_as_task(self):
        """A fresh (gateway) agent loading a compacted history must skip the
        real context_compressor summary marker and capture the genuine task."""
        from agent.context_compressor import SUMMARY_PREFIX

        agent = _FakeAgent()  # no pre-cached task (fresh per-message agent)
        msgs = [
            {"role": "user", "content": SUMMARY_PREFIX + " earlier work summary"},
            _assistant("resuming"),
            _user("Genuine task: ship the feature."),
            _assistant("ok"),
            _user("continue"),
        ]
        block = build_working_set_block(agent, msgs)
        assert "Genuine task: ship the feature." in block
        assert "earlier work summary" not in block

    def test_capture_is_once_not_overwritten(self):
        agent = _FakeAgent()
        build_working_set_block(agent, _long_convo("FIRST captured task."))
        # A later list whose earliest genuine user msg differs must NOT clobber.
        later = _long_convo("A DIFFERENT first message.")
        block = build_working_set_block(agent, later)
        assert "FIRST captured task." in block
        assert "A DIFFERENT first message." not in block


# --------------------------------------------------------------------------- #
# Injection wiring (mirror the memory-block api-copy-only behavior)
# --------------------------------------------------------------------------- #

class TestInjectionWiring:
    def test_api_copy_carries_fence_after_memory_block_history_clean(self):
        """Reproduce the conversation_loop injection idiom: the API-time copy of
        the current user message gets memory + working-set appended; the
        persisted message stays clean; working-set comes AFTER memory."""
        from agent.memory_manager import build_memory_context_block

        agent = _FakeAgent()
        messages = _long_convo("Standing task X.")
        current_turn_user_idx = len(messages) - 1
        persisted = messages[current_turn_user_idx]
        original_content = persisted["content"]

        # --- mirror conversation_loop.py api-message assembly ---
        api_msg = persisted.copy()
        injections = []
        mem = build_memory_context_block("recalled fact")
        if mem:
            injections.append(mem)
        ws = build_working_set_block(agent, messages)
        if ws:
            injections.append(ws)
        base = api_msg.get("content", "")
        api_msg["content"] = base + "\n\n" + "\n\n".join(injections)

        # API copy sees both fences.
        assert "<memory-context>" in api_msg["content"]
        assert "<working-set>" in api_msg["content"]
        # Working-set is AFTER the memory block.
        assert api_msg["content"].index("<working-set>") > api_msg["content"].index("<memory-context>")
        # Persisted history is untouched — no leak into session storage.
        assert persisted["content"] == original_content
        assert "<working-set>" not in persisted["content"]
        assert "<memory-context>" not in persisted["content"]


# --------------------------------------------------------------------------- #
# Scrubber strips the working-set fence from streamed output
# --------------------------------------------------------------------------- #

class TestScrubberStripsWorkingSet:
    def test_oneshot_sanitize_strips_working_set_block(self):
        leaked = (
            "<working-set>\n"
            "[System note: standing task context — re-stated each turn.]\n"
            "## Original task & standing constraints\nsecret task\n"
            "</working-set>\n\nVisible answer"
        )
        assert sanitize_context(leaked).strip() == "Visible answer"

    def test_stream_scrubber_strips_working_set_split_across_deltas(self):
        s = StreamingContextScrubber()
        deltas = [
            "Hello\n",
            "<working-set>\n[System note: standing ",
            "task context]\n## Original task\nsecret ",
            "constraint text\n",
            "</working-set> world",
        ]
        out = "".join(s.feed(d) for d in deltas) + s.flush()
        assert out == "Hello\n world"
        assert "secret" not in out
        assert "standing" not in out

    def test_stream_scrubber_strips_both_fences_in_one_stream(self):
        s = StreamingContextScrubber()
        text = (
            "intro\n"
            "<memory-context>\nmem payload\n</memory-context>\n"
            "middle\n"
            "<working-set>\nws payload\n</working-set>\n"
            "outro"
        )
        out = s.feed(text) + s.flush()
        assert "mem payload" not in out
        assert "ws payload" not in out
        assert "intro" in out and "middle" in out and "outro" in out

    def test_stream_scrubber_leaves_inline_mention_alone(self):
        """A non-block-boundary inline mention of the tag is not a real fence."""
        s = StreamingContextScrubber()
        text = "the <working-set> tag is inline here"
        out = s.feed(text) + s.flush()
        assert out == "the <working-set> tag is inline here"
