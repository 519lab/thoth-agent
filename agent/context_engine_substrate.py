"""Substrate context engine — Phase 2a skeleton with verbatim retrieval handles.

This is the top of a planned eviction ladder that unifies the conversation
context system with the substrate/session stores (design: see
``plans/substrate-context-engine.md``). The end-state idea is to treat the live
context window as a *cache* over the durable Postgres session store rather than
a window we lossily summarise: evicted content becomes a small, actionable stub
carrying a **retrieval handle**, while the byte-exact original stays in the
``messages`` table and the substrate indexes it for proactive recall.

Phased delivery — this file now covers **Phase 2a + 2b**:

  * **2a** — the engine skeleton plus the two verbatim retrieval handles
    (``context_expand`` / ``context_grep``) over the session store.
  * **2b (this PR)** — the tiered ``compress()`` eviction ladder: Tier-0
    structural prune (reusing the organ, minus its lossy tool-result summary),
    Tier-1 evict oldest-first tool results to actionable stubs carrying the
    handles 2a minted, threshold-triggered + batched to a ``clear_at_least``
    floor, with hot-page protection and an unpersisted-content skip; Tier-2
    falls back to the organ's summarise-compress. Eviction-only passes signal
    ``compress_context`` to skip session rotation (plan §2.2/§2.3/§2.6).
  * **2c (next)** — substrate integration (eviction slices, proactive recall
    surfacing, dereference → reinforce).
  * **2d** — Tier-2 absorption: the ``ContextCompressor`` organ becomes the
    degraded/overflow fallback rather than the mechanism.

Composition, not inheritance: the engine *owns* a ``ContextCompressor``
("Tier-2 organ") instead of subclassing it, so 2b/2c/2d can layer eviction on
top without entangling the two lifecycles. Because ``run_agent`` /
``conversation_compression`` read and write the compaction state directly on
the engine object (``last_prompt_tokens``, ``threshold_tokens``,
``compression_count``, plus internal ``_last_compress_aborted`` etc.), the
token-state fields are exposed as properties that read/write straight through
to the inner compressor — one source of truth — and any other attribute access
falls through to it via ``__getattr__``. That is what makes 2a delegation
byte-identical: there is no second copy of the state to drift.

Handle format (documented in the tool schemas so the model can reuse handles it
sees in stubs/recall in later phases):

    sid:<session_id>#m:<message_id>

A handle is exactly the ``(session_id, message_id)`` pair the session store is
keyed on — eviction copies nothing, it just points at the row that is already
there. Retrieval uses the same sync bridge and byte-exact fetch primitives as
``tools/session_search_tool.py`` (the ``_SyncDB`` / ``_ensure_sync_db`` pattern
that solved the async-port trap in PR #201).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from agent.context_compressor import (
    EVICTION_STUB_PREFIX,
    ContextCompressor,
    _CHARS_PER_TOKEN,
    _content_length_for_budget,
)
from agent.context_engine import ContextEngine
from agent.model_metadata import estimate_messages_tokens_rough
from agent.redact import redact_sensitive_text

logger = logging.getLogger(__name__)

# Handle grammar: ``sid:<session_id>#m:<message_id>``. Session ids are the
# timestamp+hex slugs the agent mints (``20260703_120000_ab12cd``) plus the
# odd test/legacy id, so allow any non-``#`` run for the session part and a
# plain integer for the message id.
_HANDLE_RE = re.compile(r"^sid:(?P<sid>[^#]+)#m:(?P<mid>\d+)$")

# Default ceiling on the content returned for a single expanded message. Large
# tool results in the live store run to ~88KB; returning them whole would blow
# the very context budget eviction exists to protect. Capped content carries an
# explicit marker + the handle so the model can page a narrower slice back in.
_DEFAULT_EXPAND_MAX_CHARS = 20_000

# How far up the ``parent_session_id`` chain ``context_grep`` walks to build the
# current conversation's lineage. Bounded so a pathological/cyclic chain can't
# turn a grep into an unbounded walk (the walk is a handful of ~1ms
# ``get_session`` reads, well within the "cheaply available" bar from plan §2.4).
_LINEAGE_MAX_DEPTH = 25

# ---------------------------------------------------------------------------
# Tier-1 eviction tunables (plan §2.2/§2.3). All env-overridable so live
# installs can dial the policy without a redeploy; the defaults below are the
# plan's starting heuristics.
# ---------------------------------------------------------------------------

# Verbatim-class size floor: only tool results at least this many chars are
# worth evicting. Below it, the stub (which itself costs ~200-300 chars) would
# reclaim little or nothing — not worth a cache-invalidating in-place edit.
_DEFAULT_EVICT_MIN_CHARS = 1_500

# Fraction of the compressor's compaction threshold an eviction pass drives the
# estimated context down to before it stops (the "pressure target"). 0.6 leaves
# comfortable headroom below the threshold so the next few turns don't
# immediately re-trigger. Oldest-first, so the re-stabilised prefix is maximal.
_DEFAULT_EVICT_TARGET_RATIO = 0.6

# A pass must reclaim at least this many tokens or it isn't worth the one-time
# prompt-cache suffix rewrite (plan §2.3). Default is max(15% of context, 20k);
# resolved per-engine from context_length in __init__. If the reachable
# candidates can't reach the floor, we still evict them (they help) and fall
# through to Tier 2 for the remainder.
_DEFAULT_EVICT_MIN_RECLAIM_FLOOR = 20_000
_DEFAULT_EVICT_MIN_RECLAIM_CONTEXT_FRACTION = 0.15

# Hot-page protection: never evict a message whose handle was dereferenced
# (``context_expand``) within the last N eviction passes. The Codex
# 53×-re-read incident is the failure mode recency-only eviction thrashes on.
_DEFAULT_EVICT_HOT_WINDOW = 50

# Cap on the recently-expanded-handle set (insertion-ordered; oldest drop
# first). Bounds memory on reference-heavy sessions.
_EXPANDED_HANDLES_CAP = 200


def _env_int(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back on any garbage."""
    try:
        val = int(os.environ.get(name, "").strip())
        return val if val > 0 else default
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        val = float(os.environ.get(name, "").strip())
        return val if val > 0 else default
    except (TypeError, ValueError):
        return default


def _make_handle(session_id: str, message_id: Any) -> str:
    """Build the canonical handle string for a ``(session_id, message_id)`` pair."""
    return f"sid:{session_id}#m:{message_id}"


def _parse_handle(handle: str) -> Optional[tuple]:
    """Parse a handle into ``(session_id, message_id:int)``.

    Returns ``None`` for anything that isn't a well-formed handle so callers
    can turn it into a clean error string rather than raising.
    """
    if not isinstance(handle, str):
        return None
    m = _HANDLE_RE.match(handle.strip())
    if not m:
        return None
    return m.group("sid"), int(m.group("mid"))


class _DBUnavailable(Exception):
    """Raised internally when the session store can't be reached; converted to a
    clean error-string tool result (never propagated to the loop)."""


class SubstrateContextEngine(ContextEngine):
    """Context engine that caches conversation context over the session store.

    Phase 2a: a thin skeleton. All compaction behaviour delegates to an internal
    :class:`ContextCompressor` (the "Tier-2 organ"); the only new surface is the
    ``context_expand`` / ``context_grep`` retrieval tools that fetch byte-exact
    stored messages by handle. Tier-0/Tier-1 eviction, stubs, and substrate
    slices arrive in Phase 2b/2c (see module docstring and plan §2.2).
    """

    def __init__(
        self,
        *args: Any,
        expand_max_chars: int = _DEFAULT_EXPAND_MAX_CHARS,
        **kwargs: Any,
    ) -> None:
        # Own a ContextCompressor with the exact constructor contract the
        # default engine uses — every positional/keyword arg the caller would
        # pass to ContextCompressor is forwarded verbatim, so a substrate engine
        # is a drop-in for the compressor at construction time.
        self._compressor = ContextCompressor(*args, **kwargs)
        self._expand_max_chars = max(1_000, int(expand_max_chars))
        # Current conversation session id, captured at on_session_start (and on
        # each compression-driven rotation). context_grep scopes to this id's
        # lineage; context_expand doesn't need it (handles carry their own sid).
        self._session_id: Optional[str] = None

        # -- Tier-1 eviction policy (plan §2.2/§2.3) ----------------------
        # Set to True by compress() when Tier 0+1 relieved pressure WITHOUT a
        # Tier-2 summarisation. conversation_compression.compress_context reads
        # it (via getattr on the engine) to skip session rotation: stub
        # eviction preserves message structure, so the conversation continues
        # in the same session. Reset at the top of every compress() call.
        self._last_compress_eviction_only: bool = False

        self._evict_min_chars = _env_int(
            "CONTEXT_EVICT_MIN_CHARS", _DEFAULT_EVICT_MIN_CHARS
        )
        self._evict_target_ratio = _env_float(
            "CONTEXT_EVICT_TARGET_RATIO", _DEFAULT_EVICT_TARGET_RATIO
        )
        self._evict_hot_window = _env_int(
            "CONTEXT_EVICT_HOT_WINDOW", _DEFAULT_EVICT_HOT_WINDOW
        )
        # clear_at_least floor: env override wins, else max(15% of context,
        # 20k). context_length comes from the organ (may be 0 before a model
        # is resolved — the max() keeps the floor sane regardless).
        _floor_env = os.environ.get("CONTEXT_EVICT_MIN_RECLAIM", "").strip()
        if _floor_env:
            self._evict_min_reclaim = _env_int(
                "CONTEXT_EVICT_MIN_RECLAIM", _DEFAULT_EVICT_MIN_RECLAIM_FLOOR
            )
        else:
            self._evict_min_reclaim = max(
                int(self._compressor.context_length
                    * _DEFAULT_EVICT_MIN_RECLAIM_CONTEXT_FRACTION),
                _DEFAULT_EVICT_MIN_RECLAIM_FLOOR,
            )

        # Hot-page tracking: handle -> eviction-pass index at which the model
        # last dereferenced it via context_expand. Insertion-ordered and
        # capped; a handle is "hot" (protected) while
        # ``_eviction_pass_count - expanded_at < _evict_hot_window``.
        self._expanded_handles: "OrderedDict[str, int]" = OrderedDict()
        self._eviction_pass_count: int = 0

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "substrate"

    # ------------------------------------------------------------------
    # Token / compaction state — delegated to the inner compressor so there is
    # exactly one source of truth. run_agent + conversation_compression read
    # AND write these directly on the engine object, so each is a read/write
    # property straight through to the organ.
    # ------------------------------------------------------------------

    @property
    def last_prompt_tokens(self) -> int:
        return self._compressor.last_prompt_tokens

    @last_prompt_tokens.setter
    def last_prompt_tokens(self, value: int) -> None:
        self._compressor.last_prompt_tokens = value

    @property
    def last_completion_tokens(self) -> int:
        return self._compressor.last_completion_tokens

    @last_completion_tokens.setter
    def last_completion_tokens(self, value: int) -> None:
        self._compressor.last_completion_tokens = value

    @property
    def last_total_tokens(self) -> int:
        return self._compressor.last_total_tokens

    @last_total_tokens.setter
    def last_total_tokens(self, value: int) -> None:
        self._compressor.last_total_tokens = value

    @property
    def threshold_tokens(self) -> int:
        return self._compressor.threshold_tokens

    @threshold_tokens.setter
    def threshold_tokens(self, value: int) -> None:
        self._compressor.threshold_tokens = value

    @property
    def context_length(self) -> int:
        return self._compressor.context_length

    @context_length.setter
    def context_length(self, value: int) -> None:
        self._compressor.context_length = value

    @property
    def compression_count(self) -> int:
        return self._compressor.compression_count

    @compression_count.setter
    def compression_count(self, value: int) -> None:
        self._compressor.compression_count = value

    @property
    def threshold_percent(self) -> float:
        return self._compressor.threshold_percent

    @threshold_percent.setter
    def threshold_percent(self, value: float) -> None:
        self._compressor.threshold_percent = value

    @property
    def protect_first_n(self) -> int:
        return self._compressor.protect_first_n

    @protect_first_n.setter
    def protect_first_n(self, value: int) -> None:
        self._compressor.protect_first_n = value

    @property
    def protect_last_n(self) -> int:
        return self._compressor.protect_last_n

    @protect_last_n.setter
    def protect_last_n(self, value: int) -> None:
        self._compressor.protect_last_n = value

    def __getattr__(self, name: str) -> Any:
        """Fall through unknown attribute reads to the inner compressor.

        This exposes the compressor's private compaction state
        (``_last_compress_aborted``, ``_last_summary_error``,
        ``_previous_summary``, ``abort_on_summary_failure``, ``quiet_mode`` …)
        on the engine, which ``conversation_compression.compress_context``
        reads via ``getattr(agent.context_compressor, ...)``. Only fires when
        normal lookup misses, so the properties/methods above always win.
        """
        # Guard against recursion before ``_compressor`` is assigned in __init__.
        if name == "_compressor":
            raise AttributeError(name)
        return getattr(self._compressor, name)

    # ------------------------------------------------------------------
    # Core compaction interface. Token tracking + should_compress delegate to
    # the organ; compress() is the tiered eviction ladder (Phase 2b) with the
    # organ as the Tier-2 fallback.
    # ------------------------------------------------------------------

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        return self._compressor.update_from_response(usage)

    def should_compress(self, prompt_tokens: int = None) -> bool:
        return self._compressor.should_compress(prompt_tokens)

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int = None,
        focus_topic: str = None,
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        """Tiered compaction: structural prune → evict-to-stub → summarise.

        The eviction ladder from plan §2.2 — cheapest, most reversible tier
        first, escalating only on residual pressure:

          * **Tier 0** — the organ's structural prune (``_prune_old_tool_results``:
            md5 dedup, image stripping, oversized tool-arg truncation). No LLM,
            no store.
          * **Tier 1** — evict oldest-first tool results outside the protected
            head/tail to an actionable *stub* carrying a retrieval handle. The
            byte-exact original stays in the session store; only the in-memory
            body is replaced. Message count/order/roles are preserved
            (pairing + alternation invariants, plan §2.6).
          * **Tier 2** — if Tier 0+1 didn't clear the pressure target (or
            couldn't reclaim the ``clear_at_least`` floor), fall through to the
            organ's summarise-compress on the already-stubbed list. This is
            today's compression behaviour, demoted to a fallback.

        ``compress()`` is only entered under threshold pressure, so Tier 1
        evicts greedily until the pressure target is met or candidates are
        exhausted — never trickle (the prompt-cache math in plan §2.3).

        When Tier 0+1 alone relieved pressure, ``_last_compress_eviction_only``
        is set so ``compress_context`` skips session rotation (structure was
        preserved). When Tier 2 runs, it stays False and the normal
        rotation path proceeds unchanged.
        """
        # Reset the eviction-only seam AND the organ's per-call summary-failure
        # flags. On the eviction-only path we never enter the organ's
        # compress() (which is what normally clears these), so a stale
        # _last_compress_aborted from a PRIOR Tier-2 abort would otherwise make
        # compress_context think this pass aborted. Clear them up front.
        self._last_compress_eviction_only = False
        self._compressor._last_compress_aborted = False
        self._compressor._last_summary_error = None
        self._compressor._last_aux_model_failure_error = None
        self._compressor._last_aux_model_failure_model = None

        # Eviction requires the durable session store (the handle target) and a
        # known session lineage. If either is missing — PG down, disabled
        # install, or not yet attached to a session — fall straight to Tier 2,
        # which IS the compressor: behaviour is byte-identical to Phase 2a.
        db = None
        if os.environ.get("CONTEXT_EVICT", "1") != "0" and self._session_id:
            try:
                db = self._resolve_db(None)
            except _DBUnavailable:
                db = None
        if db is None:
            return self._compressor.compress(
                messages, current_tokens=current_tokens,
                focus_topic=focus_topic, force=force,
            )

        start_est = current_tokens or estimate_messages_tokens_rough(messages)
        target_tokens = int(self.threshold_tokens * self._evict_target_ratio)

        # ---- Tier 0: structural prune (organ machinery) ----
        # summarize_tool_results=False: keep only the cheap, non-lossy work
        # (exact-dup dedup, image-payload stripping, oversized tool-arg
        # truncation). Tool-result BODIES are left intact for Tier 1 to evict
        # *restorably* to a stub+handle — Tier 0's own lossy 1-line summaries
        # would strand them with no way back (plan §2.2).
        tier0, _pruned = self._compressor._prune_old_tool_results(
            messages,
            protect_tail_count=self.protect_last_n,
            protect_tail_tokens=self._compressor.tail_token_budget,
            summarize_tool_results=False,
        )

        # ---- Tier 1: evict oldest-first tool results to stubs ----
        self._eviction_pass_count += 1
        evicted_list, reclaimed, n_evicted, n_skipped_unpersisted = self._evict_tier1(
            tier0, db, target_tokens, start_est,
        )

        post_est = estimate_messages_tokens_rough(evicted_list)
        # Keep the organ's token state honest so pressure math (should_compress,
        # run_agent display) sees the POST-eviction size, not the stale pre one.
        self._compressor.last_prompt_tokens = post_est

        # Did eviction alone relieve pressure? Two gates (plan §2.3): the
        # pressure target must be met AND the pass must have reclaimed the
        # clear_at_least floor (a sub-floor reclaim isn't worth the cache
        # rewrite on its own → escalate to a bigger Tier-2 restructure).
        pressure_relieved = post_est <= target_tokens
        floor_met = reclaimed >= self._evict_min_reclaim

        self._emit_evicted_telemetry(
            trigger="threshold" if not force else "manual",
            reclaimed=reclaimed,
            evicted=n_evicted,
            skipped_unpersisted=n_skipped_unpersisted,
            escalated_to_tier2=not (n_evicted > 0 and pressure_relieved and floor_met),
        )

        if n_evicted > 0 and pressure_relieved and floor_met:
            # Eviction-only success — no summarisation, no rotation.
            self._last_compress_eviction_only = True
            return evicted_list

        # ---- Tier 2: summarise-compress on the already-stubbed list ----
        # Feed the post-eviction estimate so the organ logs/accounts against
        # the real current size. The organ increments compression_count and
        # manages its own summary-failure flags from here.
        return self._compressor.compress(
            evicted_list, current_tokens=post_est,
            focus_topic=focus_topic, force=force,
        )

    # ------------------------------------------------------------------
    # Tier-1 eviction internals
    # ------------------------------------------------------------------

    def _evict_tier1(
        self,
        messages: List[Dict[str, Any]],
        db,
        target_tokens: int,
        start_est: int,
    ) -> Tuple[List[Dict[str, Any]], int, int, int]:
        """Evict oldest-first tool results to stubs; return the rewritten list.

        Returns ``(new_messages, reclaimed_tokens, n_evicted, n_skipped_unpersisted)``.

        Structure is preserved exactly: we copy each message and replace only
        the *content* of evicted tool results — never the role, never the
        ``tool_call_id``, never any assistant ``tool_calls``/``arguments``. So
        message count, order, and role alternation are identical to the input
        (plan §2.6). We stop as soon as the estimated size crosses the pressure
        target (greedy, oldest-first) or candidates are exhausted.
        """
        n = len(messages)
        result = [m.copy() for m in messages]

        # Protected zones (reuse the organ's boundary helpers so the head/tail
        # shape matches Tier 2 exactly): [0, head_end) head, [tail_start, n)
        # tail. The tail cut is token-budgeted AND guarantees the newest user
        # message stays live (organ's #10896 fix), so we never touch it.
        head_end = self._compressor._protect_head_size(result)
        head_end = self._compressor._align_boundary_forward(result, head_end)
        tail_start = self._compressor._find_tail_cut_by_tokens(result, head_end)

        # Candidate tool results in the middle band, above the size floor,
        # not already stubs. Collect indices oldest-first (ascending).
        candidates: List[int] = []
        call_ids: List[str] = []
        for i in range(head_end, tail_start):
            msg = result[i]
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or len(content) < self._evict_min_chars:
                continue
            if content.startswith(EVICTION_STUB_PREFIX):
                continue  # already evicted — idempotent under repeated passes
            cid = msg.get("tool_call_id")
            if not cid:
                continue  # can't mint a resolvable handle without a call id
            candidates.append(i)
            call_ids.append(cid)

        if not candidates:
            return result, 0, 0, 0

        # Resolve tool_call_id -> (session_id, message_id) in ONE query across
        # the current conversation lineage. Absent ids are not yet flushed to
        # the store (persistence lags tool execution — see
        # resolve_tool_call_message_ids docstring) and must be skipped: never
        # evict content that isn't durably retrievable.
        lineage = self._session_lineage(db, self._session_id) if self._session_id else []
        resolved: Dict[str, tuple] = {}
        try:
            resolved = db.resolve_tool_call_message_ids(lineage, call_ids) or {}
        except Exception as exc:  # DB hiccup → evict nothing, let Tier 2 handle it
            logger.warning("eviction handle resolution failed: %s", exc, exc_info=True)
            return result, 0, 0, 0

        # Build a call_id -> tool_name map from the assistant tool_calls so the
        # stub names the tool even when the in-memory tool message lacks it.
        tool_names = self._tool_name_index(result)

        running_est = start_est
        reclaimed = 0
        n_evicted = 0
        n_skipped_unpersisted = 0

        for idx, cid in zip(candidates, call_ids):
            if running_est <= target_tokens:
                break  # pressure target met — stop (greedy, oldest-first)
            row = resolved.get(cid)
            if row is None:
                n_skipped_unpersisted += 1
                continue
            sid, mid = row
            handle = _make_handle(sid, mid)
            if self._is_hot(handle):
                continue  # recently paged back in — leave it live
            msg = result[idx]
            orig = msg.get("content") or ""
            tool_name = (
                msg.get("tool_name")
                or msg.get("name")
                or tool_names.get(cid)
                or "tool"
            )
            stub = self._make_stub(handle, tool_name, orig)
            # Reclaimed tokens = original body minus the stub we leave behind.
            delta = max(
                0,
                _content_length_for_budget(orig) // _CHARS_PER_TOKEN
                - len(stub) // _CHARS_PER_TOKEN,
            )
            result[idx] = {**msg, "content": stub}
            reclaimed += delta
            running_est -= delta
            n_evicted += 1

        return result, reclaimed, n_evicted, n_skipped_unpersisted

    @staticmethod
    def _tool_name_index(messages: List[Dict[str, Any]]) -> Dict[str, str]:
        """Map ``tool_call_id -> tool_name`` from assistant ``tool_calls``.

        Mirrors the organ's ``_prune_old_tool_results`` call-id index so stubs
        can name the tool even when the tool-role message carries no name.
        """
        out: Dict[str, str] = {}
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    cid = tc.get("id", "")
                    fn = tc.get("function", {}) or {}
                    name = fn.get("name") if isinstance(fn, dict) else None
                else:
                    cid = getattr(tc, "id", "") or ""
                    fn = getattr(tc, "function", None)
                    name = getattr(fn, "name", None) if fn else None
                if cid and name:
                    out[cid] = name
        return out

    def _make_stub(self, handle: str, tool_name: str, original: str) -> str:
        """Build the one-line, actionable eviction stub (plan §2.2).

        Format (as shipped)::

            [evicted tool result §sid:<sid>#m:<mid> — <tool> (<n> chars).
             Gist: <first ~120 chars sanitised>. Retrieve exact:
             context_expand("sid:<sid>#m:<mid>")]

        Actionable by design: the exact ``context_expand`` call is IN the stub,
        because untrained models under-use passive placeholders (plan §2.2/§6).
        The gist is redacted (no secrets leak into the persisted stub) and
        flattened to a single line.
        """
        gist = redact_sensitive_text(original[:400])
        gist = re.sub(r"\s+", " ", gist).strip()[:120]
        if not gist:
            gist = "(empty)"
        return (
            f"{EVICTION_STUB_PREFIX}{handle} — {tool_name} "
            f"({len(original):,} chars). Gist: {gist}. "
            f'Retrieve exact: context_expand("{handle}")]'
        )

    def _is_hot(self, handle: str) -> bool:
        """True if ``handle`` was dereferenced within the last N eviction passes.

        Hot pages (content the model keeps paging back in / re-reading) are
        exempt from eviction so we don't thrash on reference-heavy artifacts.
        """
        expanded_at = self._expanded_handles.get(handle)
        if expanded_at is None:
            return False
        return (self._eviction_pass_count - expanded_at) < self._evict_hot_window

    def _record_expanded(self, handle: str) -> None:
        """Note a handle dereference for hot-page protection (insertion-ordered,
        capped). Called from ``context_expand`` handling."""
        # Refresh recency: move to the end, stamp the current pass index.
        self._expanded_handles.pop(handle, None)
        self._expanded_handles[handle] = self._eviction_pass_count
        while len(self._expanded_handles) > _EXPANDED_HANDLES_CAP:
            self._expanded_handles.popitem(last=False)

    def _emit_evicted_telemetry(self, **fields: Any) -> None:
        """Emit one ``context.evicted`` event per Tier-1 pass, best-effort.

        ``agent.context_telemetry`` ships on the Phase-0b branch and may be
        absent in this worktree — guarded so eviction never depends on it.
        """
        try:
            from agent import context_telemetry  # arrives with Phase-0b
        except ImportError:
            return
        try:
            context_telemetry.emit(
                "context.evicted",
                session_id=self._session_id,
                pass_index=self._eviction_pass_count,
                **fields,
            )
        except Exception as exc:  # telemetry must never break the loop
            logger.debug("context.evicted telemetry emit failed: %s", exc)

    def should_compress_preflight(self, messages: List[Dict[str, Any]]) -> bool:
        return self._compressor.should_compress_preflight(messages)

    def has_content_to_compress(self, messages: List[Dict[str, Any]]) -> bool:
        return self._compressor.has_content_to_compress(messages)

    def update_model(self, *args: Any, **kwargs: Any) -> None:
        return self._compressor.update_model(*args, **kwargs)

    def get_status(self) -> Dict[str, Any]:
        return self._compressor.get_status()

    # ------------------------------------------------------------------
    # Lifecycle — capture the session id (for context_grep scoping) and
    # otherwise delegate to the organ.
    # ------------------------------------------------------------------

    def on_session_start(self, session_id: str, **kwargs) -> None:
        # Called at session start AND on every compression-driven rotation
        # (conversation_compression passes boundary_reason="compression" plus
        # the new session_id), so this stays current across rotations. Handles
        # already minted keep resolving because they carry their own sid.
        self._session_id = session_id or self._session_id
        try:
            self._compressor.on_session_start(session_id, **kwargs)
        except Exception as exc:  # organ default is a no-op; never fatal here
            logger.debug("inner compressor on_session_start raised: %s", exc)

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        return self._compressor.on_session_end(session_id, messages)

    def on_session_reset(self) -> None:
        # A real /new or /reset ends the conversation — drop hot-page state and
        # the eviction-pass counter so a fresh session starts clean. (Handles
        # from the ended session would never be evicted again anyway.)
        self._expanded_handles.clear()
        self._eviction_pass_count = 0
        self._last_compress_eviction_only = False
        return self._compressor.on_session_reset()

    # ------------------------------------------------------------------
    # Retrieval tools (the Phase-2a surface).
    # ------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Two engine-owned retrieval tools, in flat OpenAI schema form.

        agent_init wraps each as ``{"type": "function", "function": <schema>}``
        before appending to ``agent.tools`` — matching every other engine/tool
        that ships a flat ``{name, description, parameters}`` schema.
        """
        handle_doc = (
            "A handle is `sid:<session_id>#m:<message_id>` — exactly the "
            "(session_id, message_id) pair the durable session store is keyed "
            "on. You will see handles like this inside eviction stubs and "
            "recall-surfaced memory once eviction is enabled; pass them here to "
            "page the byte-exact original back in."
        )
        return [
            {
                "name": "context_expand",
                "description": (
                    "Fetch the byte-exact stored message for a context handle "
                    "(optionally with surrounding messages). Use this to page "
                    "back in content that was evicted from the live context — "
                    "the full original lives verbatim in the Postgres session "
                    "store, not a lossy summary.\n\n"
                    f"{handle_doc}\n\n"
                    "Returns the message's role, exact content, and tool_name "
                    "(for tool results). With window>0 it also returns the "
                    "immediately surrounding messages as `neighbors`, each with "
                    "its own handle. Very large content is capped (a marker "
                    "notes the truncation and repeats the handle so you can "
                    "re-expand a narrower ask). No LLM call — a ~1ms DB fetch."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "handle": {
                            "type": "string",
                            "description": (
                                "The context handle to expand, in the form "
                                "`sid:<session_id>#m:<message_id>`."
                            ),
                        },
                        "window": {
                            "type": "integer",
                            "description": (
                                "Number of messages to also return on EACH side "
                                "of the handle's message (default 0 = just the "
                                "one message). Clamped to [0, 20]. Use 1-2 to "
                                "recover the immediate exchange around an "
                                "evicted turn."
                            ),
                            "default": 0,
                        },
                    },
                    "required": ["handle"],
                },
            },
            {
                "name": "context_grep",
                "description": (
                    "Full-text search over THIS conversation's own message "
                    "history (the current session plus its parent-session "
                    "lineage) in the durable session store, including messages "
                    "that were evicted from the live context. Returns matched "
                    "messages with a highlighted snippet and a `handle` you can "
                    "pass to context_expand for the byte-exact full content.\n\n"
                    f"{handle_doc}\n\n"
                    "Scoped to the current conversation only — it will not "
                    "surface other sessions (use session_search for that). "
                    "Postgres tsvector FTS: multi-word queries are AND by "
                    "default; use OR / quoted phrases / prefix* as needed. No "
                    "LLM call."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": (
                                "Full-text query to find in this conversation's "
                                "history (keywords, quoted phrases, or boolean "
                                "expressions)."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Max matches to return (default 5, max 20)."
                            ),
                            "default": 5,
                        },
                    },
                    "required": ["pattern"],
                },
            },
        ]

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs) -> str:
        """Dispatch context_expand / context_grep. Always returns a JSON string.

        ``db`` and ``current_session_id`` may be injected via kwargs (tests do
        this); in production ``tool_executor`` only passes ``messages=`` so the
        DB is built here from the process-wide pool exactly as session_search
        does, and the session id comes from ``on_session_start``.
        """
        args = args or {}
        try:
            db = self._resolve_db(kwargs.get("db"))
        except _DBUnavailable as exc:
            return json.dumps({"error": str(exc)})

        if name == "context_expand":
            return self._handle_expand(db, args)
        if name == "context_grep":
            return self._handle_grep(db, args, kwargs.get("current_session_id"))
        return json.dumps({"error": f"Unknown context engine tool: {name}"})

    # ------------------------------------------------------------------
    # Retrieval implementation
    # ------------------------------------------------------------------

    def _resolve_db(self, injected: Any):
        """Return a sync-callable session DB, or raise :class:`_DBUnavailable`.

        Reuses the exact plumbing session_search uses: wrap an injected raw
        async ``_AsyncSessionDB`` (or leave an already-sync one alone) via
        ``_ensure_sync_db``; otherwise bootstrap one from the process-wide pool.
        """
        from tools.session_search_tool import _SyncDB, _ensure_sync_db

        if injected is not None:
            return _ensure_sync_db(injected)
        try:
            import thoth_db
            thoth_db.pool()  # raises RuntimeError if the pool isn't initialised
            from thoth_state import _AsyncSessionDB
            return _SyncDB(_AsyncSessionDB())
        except RuntimeError as exc:
            raise _DBUnavailable(
                "session store unavailable (Postgres pool not initialised) — "
                "cannot resolve context handles"
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive
            raise _DBUnavailable(f"session store unavailable: {exc}") from exc

    def _cap_content(self, content: Any, handle: str) -> tuple:
        """Return ``(text, truncated: bool)`` capped at ``_expand_max_chars``.

        Content under the cap is returned byte-exact (the whole point of
        verbatim handles). Over the cap, we keep the head and append a marker
        that names the dropped byte count and repeats the handle so the model
        can page a narrower slice back in later.
        """
        if content is None:
            return "", False
        if not isinstance(content, str):
            content = str(content)
        if len(content) <= self._expand_max_chars:
            return content, False
        dropped = len(content) - self._expand_max_chars
        marker = (
            f"\n\n[context_expand: content truncated — {dropped:,} of "
            f"{len(content):,} chars omitted. The full text remains stored "
            f"verbatim; re-request a narrower slice with "
            f'context_expand("{handle}") or read the source artifact directly.]'
        )
        return content[: self._expand_max_chars] + marker, True

    def _shape_message(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Shape a message row into a result dict: {handle, role, content, tool_name?}."""
        handle = _make_handle(row.get("session_id"), row.get("id"))
        capped, truncated = self._cap_content(row.get("content"), handle)
        out: Dict[str, Any] = {
            "handle": handle,
            "role": row.get("role"),
            "content": capped,
        }
        if truncated:
            out["truncated"] = True
        if row.get("tool_name"):
            out["tool_name"] = row.get("tool_name")
        return out

    def _handle_expand(self, db, args: Dict[str, Any]) -> str:
        handle = args.get("handle")
        parsed = _parse_handle(handle)
        if not parsed:
            return json.dumps({
                "error": (
                    f"malformed handle {handle!r} — expected "
                    "'sid:<session_id>#m:<message_id>'"
                )
            })
        session_id, message_id = parsed

        # Hot-page protection: the model asking to page this handle back in is a
        # dereference — record it (canonical form) so Tier-1 eviction won't
        # re-evict it for the next _evict_hot_window passes.
        self._record_expanded(_make_handle(session_id, message_id))

        window = args.get("window", 0)
        try:
            window = int(window)
        except (TypeError, ValueError):
            window = 0
        window = max(0, min(window, 20))

        try:
            view = db.get_messages_around(session_id, message_id, window=window)
        except Exception as exc:
            logger.warning("context_expand get_messages_around failed: %s", exc, exc_info=True)
            return json.dumps({"error": f"failed to load message for handle {handle}: {exc}"})

        rows = view.get("window") or []
        if not rows:
            return json.dumps({
                "error": (
                    f"no message found for handle {handle} "
                    "(session or message id not in the session store)"
                )
            })

        anchor = next((r for r in rows if r.get("id") == message_id), None)
        if anchor is None:
            # get_messages_around only returns a non-empty window when the
            # anchor exists, so this is defensive.
            return json.dumps({
                "error": f"no message found for handle {handle}"
            })

        result = self._shape_message(anchor)
        if window > 0:
            neighbors = [self._shape_message(r) for r in rows if r.get("id") != message_id]
            if neighbors:
                result["neighbors"] = neighbors
        return json.dumps(result, ensure_ascii=False, default=str)

    def _session_lineage(self, db, session_id: str) -> List[str]:
        """Walk ``parent_session_id`` from ``session_id`` up to the lineage root.

        Returns ``[session_id, parent, grandparent, …]`` (current first),
        bounded by ``_LINEAGE_MAX_DEPTH`` and cycle-guarded. Any DB hiccup
        degrades gracefully to whatever lineage was gathered so far (at minimum
        the current session), matching plan §2.4's "else current session".
        """
        lineage: List[str] = []
        seen: set = set()
        cur = session_id
        while cur and cur not in seen and len(lineage) < _LINEAGE_MAX_DEPTH:
            seen.add(cur)
            lineage.append(cur)
            try:
                meta = db.get_session(cur)
            except Exception as exc:
                logger.debug("context_grep lineage walk stopped at %s: %s", cur, exc)
                break
            cur = (meta or {}).get("parent_session_id")
        return lineage

    def _handle_grep(self, db, args: Dict[str, Any], current_session_id: Optional[str]) -> str:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            return json.dumps({"error": "context_grep requires a non-empty pattern"})
        pattern = pattern.strip()

        limit = args.get("limit", 5)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(limit, 20))

        session_id = current_session_id or self._session_id
        if not session_id:
            return json.dumps({
                "error": (
                    "context_grep has no current session to scope to "
                    "(engine not attached to a session yet)"
                )
            })

        lineage = set(self._session_lineage(db, session_id))

        try:
            # search_messages has no session filter, so over-fetch and scope to
            # the lineage in-process. Include tool output — evicted tool results
            # are the primary reason to grep this conversation's own history.
            raw = db.search_messages(
                query=pattern,
                role_filter=["user", "assistant", "tool"],
                limit=max(limit * 5, 25),
            )
        except Exception as exc:
            logger.warning("context_grep search_messages failed: %s", exc, exc_info=True)
            return json.dumps({"error": f"search failed: {exc}"})

        matches = []
        for row in raw or []:
            if row.get("session_id") not in lineage:
                continue
            handle = _make_handle(row.get("session_id"), row.get("id"))
            entry = {
                "handle": handle,
                "role": row.get("role"),
                "snippet": row.get("snippet") or "",
            }
            if row.get("tool_name"):
                entry["tool_name"] = row.get("tool_name")
            matches.append(entry)
            if len(matches) >= limit:
                break

        return json.dumps({
            "pattern": pattern,
            "matches": matches,
            "count": len(matches),
        }, ensure_ascii=False, default=str)


__all__ = ["SubstrateContextEngine"]
