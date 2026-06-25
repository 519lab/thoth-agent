"""Tests for the signal-based skill-review trigger (innovation #8).

The old trigger fired a background skill review on a fixed cadence
(``_iters_since_skill >= _skill_nudge_interval``), which churned out low-value
skill edits on smooth sessions.  The new trigger
(:func:`agent.background_review.should_review_skills_after_turn`) is
signal-based: it fires when ``_skill_review_signal`` was set during the turn
(a tool error, or a failed turn), OR-ed with a RAISED interval fallback so
long, signal-less sessions still get the occasional review.

Both runtime paths — the default chat_completions loop
(``agent/conversation_loop.py``) and the codex app-server loop
(``agent/codex_runtime.py``) — share this helper, and both fold their
turn-level failure signal (``failed`` / ``turn.error``) into
``_skill_review_signal`` before calling it.  These tests exercise the helper
directly for the three required scenarios and assert that each runtime path's
signal-folding routes into a spawned review, with ``_spawn_background_review``
mocked.
"""

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import agent.codex_runtime as codex_runtime
import agent.conversation_loop as conversation_loop
from agent.background_review import should_review_skills_after_turn

# The interval the agent is configured with in these tests.  The raised
# fallback ceiling is deliberately higher (mirrors agent_init: nudge * 3).
_NUDGE = 10
_FALLBACK = 30


def _make_agent(
    *,
    signal=False,
    iters=0,
    signal_mode=True,
    nudge=_NUDGE,
    fallback=_FALLBACK,
    has_skill_tool=True,
):
    """Build a minimal stand-in agent with just the trigger attributes."""
    return SimpleNamespace(
        _skill_review_signal=signal,
        _iters_since_skill=iters,
        _skill_review_signal_mode=signal_mode,
        _skill_nudge_interval=nudge,
        _skill_review_fallback_interval=fallback,
        valid_tool_names={"skill_manage"} if has_skill_tool else set(),
    )


# ── Core helper behaviour (the shared decision) ──────────────────────────


def test_signal_fires_below_interval():
    """A review signal fires the review even with iters below the interval."""
    agent = _make_agent(signal=True, iters=1)
    assert should_review_skills_after_turn(agent) is True
    # Firing resets the iteration counter and always clears the signal.
    assert agent._iters_since_skill == 0
    assert agent._skill_review_signal is False


def test_no_signal_below_interval_does_not_fire():
    """No signal + iters below the raised fallback = no review."""
    agent = _make_agent(signal=False, iters=_NUDGE)  # at old interval, below fallback
    assert should_review_skills_after_turn(agent) is False
    # Counter is NOT reset when nothing fires; signal stays cleared.
    assert agent._iters_since_skill == _NUDGE
    assert agent._skill_review_signal is False


def test_no_signal_above_raised_fallback_fires():
    """No signal but iters past the RAISED fallback = fallback review."""
    agent = _make_agent(signal=False, iters=_FALLBACK)
    assert should_review_skills_after_turn(agent) is True
    assert agent._iters_since_skill == 0


def test_signal_ignored_without_skill_tool():
    """No skill_manage tool means the review is never eligible."""
    agent = _make_agent(signal=True, iters=_FALLBACK, has_skill_tool=False)
    assert should_review_skills_after_turn(agent) is False
    # Signal is still cleared so it can't leak forward.
    assert agent._skill_review_signal is False


def test_signal_mode_off_restores_pure_interval():
    """With signal mode off, only the (un-raised) interval matters."""
    # A signal alone does NOT fire below the plain interval in pure mode.
    agent = _make_agent(signal=True, iters=1, signal_mode=False)
    assert should_review_skills_after_turn(agent) is False
    # At the plain interval it fires, regardless of signal.
    agent = _make_agent(signal=False, iters=_NUDGE, signal_mode=False)
    assert should_review_skills_after_turn(agent) is True
    assert agent._iters_since_skill == 0


# ── Both runtime paths actually wire up the shared helper ────────────────
#
# Guards against the reproduced post-turn blocks below drifting away from the
# production code: both runtimes must import the shared helper and fold their
# turn-level failure signal into _skill_review_signal.


def test_conversation_loop_uses_shared_helper_and_failed_signal():
    src = inspect.getsource(conversation_loop)
    assert "should_review_skills_after_turn(agent)" in src
    # A failed turn folds into the review signal.
    assert "agent._skill_review_signal = True" in src


def test_codex_runtime_uses_shared_helper_and_error_signal():
    src = inspect.getsource(codex_runtime)
    assert "should_review_skills_after_turn(agent)" in src
    # turn.error is the codex path's failure signal.
    assert "turn.error is not None" in src
    assert "agent._skill_review_signal = True" in src


# ── Runtime path 1: chat_completions loop (conversation_loop.py) ──────────
#
# The default path folds a `failed` turn into _skill_review_signal, then calls
# the shared helper and spawns the review when it fires.  We reproduce exactly
# that post-turn block (the only path-specific logic) and assert the spawn.


def _conversation_loop_post_turn(agent, *, failed, final_response, interrupted):
    """Mirror of conversation_loop.run_conversation's skill-review block."""
    if failed:
        agent._skill_review_signal = True
    should_review_skills = should_review_skills_after_turn(agent)
    if final_response and not interrupted and should_review_skills:
        agent._spawn_background_review(
            messages_snapshot=[],
            review_memory=False,
            review_skills=True,
        )
    return should_review_skills


def test_conversation_loop_failed_turn_spawns_review():
    """A failed turn is hard evidence: it folds into the signal and fires."""
    agent = _make_agent(signal=False, iters=1)
    agent._spawn_background_review = MagicMock()
    fired = _conversation_loop_post_turn(
        agent, failed=True, final_response="done", interrupted=False
    )
    assert fired is True
    agent._spawn_background_review.assert_called_once()


def test_conversation_loop_clean_turn_below_interval_no_spawn():
    """Clean turn, below the raised fallback: no signal, no spawn."""
    agent = _make_agent(signal=False, iters=_NUDGE)
    agent._spawn_background_review = MagicMock()
    fired = _conversation_loop_post_turn(
        agent, failed=False, final_response="done", interrupted=False
    )
    assert fired is False
    agent._spawn_background_review.assert_not_called()


def test_conversation_loop_uneventful_above_fallback_spawns_review():
    """Long, signal-less session past the fallback still reviews."""
    agent = _make_agent(signal=False, iters=_FALLBACK)
    agent._spawn_background_review = MagicMock()
    fired = _conversation_loop_post_turn(
        agent, failed=False, final_response="done", interrupted=False
    )
    assert fired is True
    agent._spawn_background_review.assert_called_once()


# ── Runtime path 2: codex app-server loop (codex_runtime.py) ──────────────
#
# The codex path bypasses tool_executor, so a turn.error is its failure
# signal.  It folds turn.error into _skill_review_signal, then calls the same
# helper and spawns the review.  Reproduce that block and assert the spawn.


def _codex_runtime_post_turn(agent, turn):
    """Mirror of codex_runtime.run_codex_app_server_turn's skill-review block."""
    if turn.error is not None:
        agent._skill_review_signal = True
    should_review_skills = should_review_skills_after_turn(agent)
    if turn.final_text and not turn.interrupted and should_review_skills:
        agent._spawn_background_review(
            messages_snapshot=[],
            review_memory=False,
            review_skills=True,
        )
    return should_review_skills


def test_codex_turn_error_spawns_review_below_interval():
    """A codex turn error folds into the signal and fires below interval."""
    agent = _make_agent(signal=False, iters=1)
    agent._spawn_background_review = MagicMock()
    turn = SimpleNamespace(error="boom", final_text="partial", interrupted=False)
    fired = _codex_runtime_post_turn(agent, turn)
    assert fired is True
    agent._spawn_background_review.assert_called_once()


def test_codex_clean_turn_below_interval_no_spawn():
    """Clean codex turn below the raised fallback: no signal, no spawn."""
    agent = _make_agent(signal=False, iters=_NUDGE)
    agent._spawn_background_review = MagicMock()
    turn = SimpleNamespace(error=None, final_text="done", interrupted=False)
    fired = _codex_runtime_post_turn(agent, turn)
    assert fired is False
    agent._spawn_background_review.assert_not_called()


def test_codex_uneventful_above_fallback_spawns_review():
    """Long, signal-less codex session past the fallback still reviews."""
    agent = _make_agent(signal=False, iters=_FALLBACK)
    agent._spawn_background_review = MagicMock()
    turn = SimpleNamespace(error=None, final_text="done", interrupted=False)
    fired = _codex_runtime_post_turn(agent, turn)
    assert fired is True
    agent._spawn_background_review.assert_called_once()
