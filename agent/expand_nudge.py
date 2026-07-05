"""Decision-time expand nudges — make evicted-content pointers ACTIONABLE.

WHY THIS EXISTS (the round-5 quality lever)
-------------------------------------------
The substrate/cooling context engines evict verbose tool results to byte-exact
*pointer slices* and surface them through recall.  Each pointer carries a
passive restore line — ``Retrieve exact: context_expand("sid:...#m:...")`` —
buried inside the ``<memory-context>`` recall block.  The intent is a reactive
"page it back in when you need it" leg: when the model needs the exact evicted
content it dereferences the handle and the engine emits a ``context.pagein``
telemetry event.

That leg is DEAD.  Measured across four graded rounds:

  * The model dereferences retrieval handles only ~0.3 times/task.
  * ``context.pagein`` telemetry shows **0 events** — the reactive path never
    fires — even though recall surfaces the pointers heavily (290 eviction
    slices minted in one round, ~141 composed into projections in an earlier
    one).  The pointers are present; the model just ignores the passive
    ``context_expand(...)`` line sitting in the middle of the recall block.

WHY A PASSIVE POINTER LOSES (the evidence)
------------------------------------------
  * OPENDEV terminal-agent paper (arXiv 2603.05344):
      (a) USER-role reminders placed at *max recency* get noticeably higher
          compliance than the same text buried in a system message;
      (b) a short, single-purpose reminder injected immediately BEFORE the
          decision point beats a long system-prompt section;
      (c) reminders show diminishing/negative returns past 3–4 simultaneous
          items — so the count must be CAPPED per turn.
  * Anthropic's own cookbook confirms models under-use passive cleared-content
    placeholders by design (no restore protocol ships in-model) — exactly the
    0-pagein forensic finding above.

THE MECHANISM
-------------
Every turn, after the memory-context and working-set blocks are assembled, we
parse the *already-built* memory block for the recall-surfaced eviction
pointers, drop any the model already dereferenced this session (hot pages), cap
the rest at :data:`CONTEXT_EXPAND_NUDGE_MAX`, and inject a short single-purpose
``<expand-hint>`` block as the LAST (most-recent) user-message content at
API-call time.  Max recency + short + single-purpose + capped == the OPENDEV
recipe.  The nudge only re-states pointers recall already surfaced; it invents
nothing.

ENGINE-AWARENESS
----------------
The nudge is only meaningful for a *restorable-eviction* engine (substrate /
cooling) — the ones that mint ``context_expand`` handles.  Rather than sniff the
engine name, we gate on the pointers being *present in the memory block*: if the
grammar is there, the surfacing engine put it there and the ``context_expand``
tool is live.  When such an engine exposes its dereferenced-handle set
(``_expanded_handles`` — an ``OrderedDict`` of handle -> eviction-pass index used
for hot-page protection; see ``agent/context_engine_substrate.py``) we filter
those out so we never nudge for content the model already paged in.  A
non-substrate engine simply won't expose the attr and won't surface the grammar,
so the nudge no-ops.

CACHE SAFETY
------------
Identical to the working-set block: this rides the API-call-time user-message
*copy* only (see ``agent/conversation_loop.py``), so nothing is persisted to
session history and the byte-stable cached system prefix is never disturbed.

WORST-CASE PER-TURN TOKEN COST
------------------------------
Bounded by the cap.  Per listed item: the handle (~20 chars) + tool name +
an optional gist echo head-capped at :data:`_GIST_ECHO_MAX_CHARS` (120 chars)
≈ 45 tokens.  Fence + system-note boilerplate ≈ 55 tokens.  At the default cap
of 3 items that is ≈ 55 + 3*45 ≈ 190 tokens/turn worst case; a typical turn with
1–2 un-dereferenced pointers is ~100–140 tokens.  The block is emitted only when
recall actually surfaced pointers, so quiet turns cost zero.

ENV KNOBS
---------
  * THOTH_EXPAND_NUDGE        — kill switch; "0"/"false"/"no"/"off" disables.
  * CONTEXT_EXPAND_NUDGE_MAX  — max pointers listed per turn (default 3, the
                                OPENDEV 3–4 reminder ceiling).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_DEFAULT_NUDGE_MAX = 3

# Head-cap for the optional gist echo so a multi-line eviction gist can't blow
# up the nudge (the cap is the whole point — see OPENDEV finding (b)/(c)).
_GIST_ECHO_MAX_CHARS = 120

# The eviction-handle grammar is fixed (see SubstrateContextEngine._make_stub /
# the pointer-slice text ``<tool>: <gist> — Retrieve exact: context_expand(...)``
# in agent/context_engine_substrate.py, and _HANDLE_RE == r"sid:[^#]+#m:\d+").
# We match the ``context_expand("sid:...#m:...")`` call as it appears verbatim in
# the recall block.  sid carries no ``#`` (the ``#m:`` is the separator) and no
# ``"`` (it's inside the quotes), so the character classes below are exact.
_HANDLE_IN_EXPAND_RE = re.compile(
    r'context_expand\(\s*"(?P<handle>sid:[^"#]+#m:\d+)"\s*\)'
)

# Best-effort richer capture for the OPTIONAL gist echo: the surfaced pointer
# slice reads ``<tool_name>: <gist> — Retrieve exact: context_expand("<handle>")``
# (agent/context_engine_substrate.py:_persist_* / _make_stub).  We grab the
# tool name and gist that sit on the same line just before the retrieve call.
# Non-greedy + line-bounded so it degrades to "no echo" rather than over-capturing
# if recall reformats the projection.
_STUB_ECHO_RE = re.compile(
    r'(?P<tool>[A-Za-z0-9_.\-]+):\s*'
    r'(?P<gist>[^\n]*?)\s*[—-]\s*Retrieve exact:\s*'
    r'context_expand\(\s*"(?P<handle>sid:[^"#]+#m:\d+)"\s*\)'
)


def _enabled() -> bool:
    """Kill switch — THOTH_EXPAND_NUDGE=0/false/no/off disables the whole feature."""
    val = os.environ.get("THOTH_EXPAND_NUDGE", "1").strip().lower()
    return val not in {"0", "false", "no", "off", ""}


def _nudge_max() -> int:
    """Per-turn cap on listed pointers (CONTEXT_EXPAND_NUDGE_MAX, default 3).

    The cap enforces OPENDEV finding (c): past 3–4 simultaneous reminders
    compliance flattens or drops.  Anything non-positive/garbage falls back to
    the default rather than emitting an unbounded (or empty) list.
    """
    raw = os.environ.get("CONTEXT_EXPAND_NUDGE_MAX")
    if raw is None:
        return _DEFAULT_NUDGE_MAX
    try:
        val = int(raw.strip())
    except (ValueError, AttributeError):
        return _DEFAULT_NUDGE_MAX
    return val if val > 0 else _DEFAULT_NUDGE_MAX


def _dereferenced_handles(agent: Any) -> set:
    """Return the set of handles the model already paged in this session.

    Reads the active engine's hot-page set — ``_expanded_handles`` on
    SubstrateContextEngine (and its CoolingContextEngine subclass): an
    ``OrderedDict`` mapping handle -> eviction-pass index.  ``set(OrderedDict)``
    yields its keys.  A non-substrate engine won't expose the attr, so we
    return an empty set and simply don't filter.
    """
    engine = getattr(agent, "context_compressor", None)
    raw = getattr(engine, "_expanded_handles", None)
    if not raw:
        return set()
    try:
        return set(raw)  # dict/OrderedDict -> keys; any iterable of handles
    except TypeError:  # pragma: no cover - defensive; attr should be iterable
        return set()


def _distinct_in_order(handles: List[str]) -> List[str]:
    """Dedupe preserving first-seen (projection) order."""
    seen: set = set()
    out: List[str] = []
    for h in handles:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def _gist_echo_map(memory_block_text: str) -> Dict[str, str]:
    """Best-effort handle -> "``<tool>``: ``<gist>``" echo, head-capped.

    Optional by contract — if the projection doesn't match the stub grammar the
    map is simply missing that handle and we list the bare pointer.
    """
    echoes: Dict[str, str] = {}
    for m in _STUB_ECHO_RE.finditer(memory_block_text):
        handle = m.group("handle")
        if handle in echoes:
            continue
        tool = m.group("tool").strip()
        gist = " ".join(m.group("gist").split())  # collapse whitespace
        if len(gist) > _GIST_ECHO_MAX_CHARS:
            gist = gist[:_GIST_ECHO_MAX_CHARS].rstrip() + "…"
        echo = f"{tool}: {gist}" if gist else tool
        echoes[handle] = echo
    return echoes


def build_expand_nudge_block(
    agent: Any,
    memory_block_text: str,
    messages: List[Dict[str, Any]],
) -> str:
    """Build the fenced ``<expand-hint>`` decision-time nudge, or "" to skip.

    Called at API-call time from the conversation loop, AFTER the memory-context
    and working-set blocks, so the nudge lands at max recency — right before the
    model's next decision (OPENDEV finding (a)/(b): a short single-purpose
    user-role reminder at the decision point earns far more compliance than a
    passive pointer buried mid-block, which is why the reactive ``context.pagein``
    path measured 0 events before this lever).

    ``memory_block_text`` is the already-built ``<memory-context>`` fenced string
    (passed straight from the loop) so the nudge re-surfaces the exact same
    recall pointers rather than re-querying anything.  ``messages`` is accepted
    for signature parity with the sibling block builders and future use; the
    nudge derives entirely from the memory block + engine hot-page state.

    Returns "" (no injection) when:
      * the feature is killed via THOTH_EXPAND_NUDGE, or
      * there is no memory block this turn (recall surfaced nothing), or
      * the memory block carries no ``context_expand`` pointers (a non-restorable
        engine, or nothing evicted), or
      * every surfaced pointer was already dereferenced (all hot).

    The pointers-present gate doubles as the engine guard: the grammar only
    appears when a restorable-eviction engine (substrate/cooling) put it there,
    which means ``context_expand`` is a live tool.
    """
    if not _enabled():
        return ""
    if not memory_block_text:
        return ""

    # Parse the recall-surfaced eviction pointers straight out of the memory
    # block, in projection order.
    handles = _distinct_in_order(_HANDLE_IN_EXPAND_RE.findall(memory_block_text))
    if not handles:
        return ""

    # Drop handles the model already paged in this session (hot pages) — never
    # nudge for content it has already retrieved.  Empty set for non-substrate
    # engines == no filtering.
    already = _dereferenced_handles(agent)
    if already:
        handles = [h for h in handles if h not in already]
        if not handles:
            return ""

    # Cap per OPENDEV: take the FIRST N distinct un-dereferenced handles in
    # projection order (recall already ranked them, so first == most salient).
    handles = handles[: _nudge_max()]

    # Optional gist echo — cheap to parse back out of the same block; degrades
    # to bare handles when the projection doesn't match the stub grammar.
    echoes = _gist_echo_map(memory_block_text)

    n = len(handles)
    parts = [
        "<expand-hint>",
        f"[System note: {n} item(s) from earlier in this task were condensed to "
        "summaries. If you need their EXACT contents to answer correctly, "
        "retrieve before responding — do not guess from the summary:]",
    ]
    for h in handles:
        echo = echoes.get(h)
        if echo:
            parts.append(f'- context_expand("{h}")  — {echo}')
        else:
            parts.append(f'- context_expand("{h}")')
    parts.append("</expand-hint>")
    return "\n".join(parts)
