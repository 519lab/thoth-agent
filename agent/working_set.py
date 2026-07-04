"""Working-set recitation — re-state the standing task every turn.

WHY THIS EXISTS (the round-4 quality lever)
--------------------------------------------
A constraint or instruction placed *once* in the conversation history does
not stay obeyed.  Its influence decays with positional distance as the
transcript grows:

  * Published measurement (mem0's "context window is RAM" write-up /
    Gamage's 4,416-trial study): a standing constraint left only in history
    decays from ~73% compliance at turn 5 to ~33% by turn 16.  The *same*
    constraint re-injected into the recency window every call holds >90%.
  * Manus's agent-loop finding: rewriting the live task state into the
    recency window on every step ("recitation") is what keeps a ~50-tool-call
    loop on-task — it counters lost-in-the-middle attention decay.
  * Local graded-run forensics: our own constraint-family failures happened
    WITHOUT any compression event — the constraint was still present in the
    history yet ignored by turn N.  That is pure positional decay, which is
    exactly what recitation targets (compression is a different failure mode
    with a different fix).

THE MECHANISM
-------------
Every turn we rebuild a small ``<working-set>`` block carrying (a) the
session's original task + standing constraints, verbatim, and (b) the current
plan/progress snapshot, and we inject it into the *recency* window — appended
to the current turn's user message at API-call time, right after the memory
block.  Recency is where attention is strongest, so the standing context
cannot decay with distance: it is always "turn 0 old" from the model's point
of view.

This block rides on EVERY turn by design.  That repetition IS the mechanism
(recitation) — not waste.  See the worst-case token math below; the cost is
bounded and small relative to the quality it buys.

ENGINE-AGNOSTIC
---------------
This is a positional-decay fix, independent of *which* context engine is
active (default compressor, cooling, references, …).  Every engine leaves the
recency window as the strongest attention position, so recitation there helps
all of them.  Nothing here touches compression; it is deliberately a separate
lever.

CACHE SAFETY
------------
The system prompt is byte-stable across turns on purpose (see
``agent/system_prompt.py`` — even the timestamp is date-only) so upstream
prompt caches stay warm.  Recitation therefore MUST NOT ride in the system
prompt.  It rides in the same cache-safe per-turn injection point the memory
block uses: the current turn's user message, on an API-call-time *copy* only,
so nothing leaks into persisted session history and the cached system prefix
is never disturbed.

WORST-CASE PER-TURN TOKEN COST
------------------------------
  * Task text:   WORKING_SET_TASK_MAX_CHARS (default 2000 chars) head-capped
                 ≈ 500 tokens (~4 chars/token).
  * Fence + system-note + headings boilerplate: ≈ 60 tokens.
  * Todo snapshot: bounded by the active todo list — TodoStore only emits
                 pending/in-progress items, typically << 200 tokens.
  So a realistic worst case is ≈ 500 + 60 + ~200 ≈ 760 tokens/turn, and a
  typical case (short task, few todos) is ~150–300 tokens/turn.  On a
  cache-warm multi-turn conversation this is a small, fixed recency cost.

ENV KNOBS
---------
  * THOTH_WORKING_SET           — kill switch; "0"/"false"/"no" disables.
  * WORKING_SET_TASK_MAX_CHARS  — task-text cap (chars, head-preserved); default 2000.
  * WORKING_SET_MIN_TURNS       — min user turns before the block is emitted; default 3.
                                  Below this the conversation is trivial and
                                  recitation is not worth the tokens.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Attribute name where the captured original task text lives on the agent.
# The agent object persists across compression/session rotation within a
# conversation-loop run, so a value captured on turn 1 survives even after
# compression has replaced the live ``messages`` head with a summary.
_TASK_ATTR = "_working_set_task_text"

# Once the conversation has crossed the min-turns threshold, recitation stays
# ON for the rest of the conversation.  This latch is load-bearing across
# compression: after a compaction the *live* messages list is short again (a
# summary head + a few turns), yet the conversation is long-lived and
# positional decay is at its WORST — the moment recitation matters most.  A
# naive per-call turn count would wrongly suppress the block right there, so we
# arm once and stay armed on the persistent agent object.
_ARMED_ATTR = "_working_set_armed"

_DEFAULT_TASK_MAX_CHARS = 2000
_DEFAULT_MIN_TURNS = 3

# Prefixes/markers that identify a *synthetic* user message — an injected or
# preserved block rather than a genuine human turn.  These must not be mistaken
# for "the original task", and must not be counted as real user turns.
_SYNTHETIC_MARKERS = (
    "<memory-context>",
    "<working-set>",
    "[system note:",
    "[your active task list",  # TodoStore.format_for_injection() header
    # Compaction handoff summaries (context_compressor SUMMARY_PREFIX /
    # LEGACY_SUMMARY_PREFIX) land as role="user" messages but are NOT the
    # original task — a fresh (gateway) agent must not capture one as the task.
    "[context compaction",
    "[context summary]",
)

# Markers that PROVE the conversation was already compacted — i.e. it is
# long-lived even if the live message list is short.  Their presence arms
# recitation immediately, which is what handles the fresh (gateway) per-message
# agent whose in-memory arming latch was never set on this process.
_COMPACTION_MARKERS = (
    "[context compaction",
    "[context summary]",
)


def _has_compaction_marker(messages: List[Dict[str, Any]]) -> bool:
    """True if any user message is a compaction/summary handoff (long conversation)."""
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        low = content.lstrip().lower()
        if any(low.startswith(m) for m in _COMPACTION_MARKERS):
            return True
    return False


def _enabled() -> bool:
    """Kill switch — THOTH_WORKING_SET=0/false/no disables the whole feature."""
    val = os.environ.get("THOTH_WORKING_SET", "1").strip().lower()
    return val not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int) -> int:
    """Read a positive int env override; fall back to default on anything odd."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw.strip())
    except (ValueError, AttributeError):
        return default
    return val if val > 0 else default


def _is_synthetic(content: Any) -> bool:
    """True if a user-role message is an injected/preserved block, not a human turn."""
    if not isinstance(content, str):
        return True  # non-string content (tool blocks etc.) is never "the task"
    stripped = content.lstrip()
    if not stripped:
        return True
    low = stripped.lower()
    return any(low.startswith(m) or m in low for m in _SYNTHETIC_MARKERS)


def _first_substantive_user_text(messages: List[Dict[str, Any]]) -> Optional[str]:
    """Return the earliest genuine (non-synthetic) user message text, or None."""
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if _is_synthetic(content):
            continue
        return content.strip()
    return None


def _count_user_turns(messages: List[Dict[str, Any]]) -> int:
    """Count genuine human user turns (ignoring injected/synthetic user messages)."""
    return sum(
        1
        for msg in messages
        if msg.get("role") == "user" and not _is_synthetic(msg.get("content"))
    )


def _head_truncate(text: str, max_chars: int) -> str:
    """Head-preserved truncation: keep the START of the task (the ask + constraints).

    The opening of a task statement carries the goal and the standing
    constraints; the tail is usually elaboration.  So we preserve the head and
    mark the elision, rather than a middle-out or tail cut.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n[... task text truncated ...]"


def _capture_task_text(agent: Any, messages: List[Dict[str, Any]]) -> Optional[str]:
    """Capture the original task text ONCE and cache it on the agent.

    Capture-once is load-bearing: compression rotates the session id and may
    replace ``messages``'s head with a summary, so relying on the live list is
    not enough.  The agent object outlives those rotations *within* a loop run,
    so the value first seen (on turn 1, before any compression) is what we keep.

    Returns the cached full (untruncated) task text, or None if nothing genuine
    has been said yet.
    """
    existing = getattr(agent, _TASK_ATTR, None)
    if existing:
        return existing
    captured = _first_substantive_user_text(messages)
    if captured:
        try:
            setattr(agent, _TASK_ATTR, captured)
        except Exception:  # pragma: no cover - defensive; agent should be writable
            logger.debug("working_set: could not cache task text on agent", exc_info=True)
    return captured


def build_working_set_block(agent: Any, messages: List[Dict[str, Any]]) -> str:
    """Build the fenced ``<working-set>`` recitation block, or "" to skip.

    Called at API-call time from the conversation loop, immediately after the
    memory-context block, so the recited task lands in the recency window.

    Returns "" (no injection) when:
      * the feature is killed via THOTH_WORKING_SET, or
      * the conversation is still trivially short (< WORKING_SET_MIN_TURNS
        genuine user turns) — no token spend on small talk, or
      * no genuine task text has been captured yet.

    NOTE: task capture happens *before* the min-turns gate so the original
    task is recorded on turn 1 (pre-compression) even though the block itself
    is not emitted until the conversation is long enough to need recitation.
    """
    if not _enabled():
        return ""

    # Capture first (turn 1, pre-compression) regardless of whether we emit.
    task_text = _capture_task_text(agent, messages)
    if not task_text:
        return ""

    # Gate on conversation length — recitation only earns its tokens once the
    # transcript is long enough for positional decay to bite.  Uses an
    # arm-once latch so a post-compression short live-list does not re-suppress
    # the block (see _ARMED_ATTR).
    if not getattr(agent, _ARMED_ATTR, False):
        min_turns = _env_int("WORKING_SET_MIN_TURNS", _DEFAULT_MIN_TURNS)
        # A compaction marker means the conversation is already long (the live
        # list just looks short post-compaction) — arm regardless of turn count.
        if _count_user_turns(messages) < min_turns and not _has_compaction_marker(messages):
            return ""
        try:
            setattr(agent, _ARMED_ATTR, True)
        except Exception:  # pragma: no cover - defensive
            logger.debug("working_set: could not arm on agent", exc_info=True)

    max_chars = _env_int("WORKING_SET_TASK_MAX_CHARS", _DEFAULT_TASK_MAX_CHARS)
    task_section = _head_truncate(task_text, max_chars)

    parts = [
        "<working-set>",
        "[System note: standing task context — re-stated each turn so it "
        "cannot decay with distance. Not new user input.]",
        "## Original task & standing constraints",
        task_section,
    ]

    # Current plan / progress — reuse the existing TodoStore snapshot so the
    # working set and the post-compression plan injection stay in one voice.
    todo_snapshot = None
    todo_store = getattr(agent, "_todo_store", None)
    if todo_store is not None:
        try:
            todo_snapshot = todo_store.format_for_injection()
        except Exception:  # pragma: no cover - defensive
            logger.debug("working_set: todo snapshot failed", exc_info=True)
    if todo_snapshot:
        parts.append("## Current plan / progress")
        parts.append(todo_snapshot)

    parts.append("</working-set>")
    return "\n".join(parts)
