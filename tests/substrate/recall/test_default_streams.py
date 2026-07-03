"""Guard: what recall draws from by default (issue #182).

Recall must not surface the agent's own ``assistant_response`` slices —
re-injecting prior (possibly wrong) replies as memory-authority is the
self-echo / wrong-belief-reinjection failure. Those slices still feed
consolidation (Summarizer → ``summary``; Parser → L1/L3); recall surfaces the
distilled forms, drawn from user messages + summaries.
"""

from __future__ import annotations

from substrate.config import DEFAULT_RECALL_STREAMS


def test_assistant_response_excluded_from_default_recall_streams():
    assert "thoth.self_action.assistant_response" not in DEFAULT_RECALL_STREAMS


def test_user_messages_and_summaries_remain():
    # The inputs (what the user said) and the distillations (Summarizer output)
    # are the legitimate recall sources.
    assert "thoth.world.user_message.cli" in DEFAULT_RECALL_STREAMS
    assert "thoth.self_action.summary" in DEFAULT_RECALL_STREAMS


def test_no_self_state_streams_leak_in():
    # Self-state (tool results, lifecycle) is not recall material either —
    # with ONE deliberate exception: Phase-2c eviction pointers
    # (``thoth.self_state.context_evicted``). Those are not the agent's own
    # conclusions echoed back but restorable handles to content the agent
    # itself evicted, which must resurface in-session via proactive recall.
    leaked = {
        s for s in DEFAULT_RECALL_STREAMS
        if s.startswith("thoth.self_state.")
        and s != "thoth.self_state.context_evicted"
    }
    assert not leaked


def test_context_evicted_included_for_proactive_recall():
    # The Phase-2c proactive path: eviction pointers ARE a default recall
    # source so evicted content can page back into the same session.
    assert "thoth.self_state.context_evicted" in DEFAULT_RECALL_STREAMS
