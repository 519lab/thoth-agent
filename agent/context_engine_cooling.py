"""Cooling-window context engine — Round 2 of the substrate-context-engine plan.

Round 1 (the :class:`SubstrateContextEngine`) evicts under *threshold pressure*:
the context grows until it crosses the compaction threshold, then the tiered
ladder fires. Round 2's hypothesis (plan ``plans/substrate-context-engine.md``,
"Round 2" note) is that the win is **proactive** management — shape content by
*age* near the tail so the context almost never fills, and the compressor
becomes an emergency backstop that fires ~never.

Mechanism: a tool result is *distilled in place* (body → gist + ``context_expand``
handle, exactly the parent's Tier-1 operation) once it has **cooled** — once
enough newer assistant tool-turns sit behind it that the model is very unlikely
to still be reading it. Distillation is age-triggered, small, and frequent
instead of pressure-triggered, large, and rare.

Why age, not pressure — the cache economics (this is the entire reason for the
design):

  * Editing the message history at position *p* invalidates the provider's
    prefix cache from *p* onward: the suffix after the edit must be re-sent at
    the ~1.25× write price instead of the ~0.1× cached-read price.
  * The round-1 lesson was: **never trickle-edit the deep prefix.** A pressure
    trigger that fires late can rewrite a large, old span — expensive.
  * A *cooling* edit is always a **near-tail** edit: the message being distilled
    is by construction only ``COOLING_WINDOW_TURNS`` assistant tool-turns behind
    the live tail. So the invalidated suffix is bounded to the cooling window,
    not the whole context. That bound is what makes many small age-based edits
    cheaper in aggregate than one big pressure-triggered restructure — and what
    keeps the compressor (the real prefix-shredder) from ever needing to run.

Composition: this engine **subclasses** :class:`SubstrateContextEngine` and
reuses its handle plumbing, stub/gist builders, hot-page protection, eviction
slices, dereference→reinforce loop, telemetry, and — crucially — its full
tiered ``compress()`` as the *backstop*. The only new behaviour is the age-based
distillation pass that runs first (and, in the common case, alone).

Env knobs:

  * ``CONTEXT_COOL_WINDOW_TURNS`` (default 5) — how many assistant-with-tool_calls
    turns must sit *after* a tool result before it is considered cooled.
  * ``CONTEXT_COOL_MIN_CHARS`` (default 1500) — size floor; below it a stub would
    reclaim little and isn't worth a (bounded) cache-invalidating edit.
  * ``CONTEXT_COOL=0`` — kill switch: the engine behaves exactly like the parent
    (no proactive preflight, ``compress()`` delegates straight to the ladder).

The inherited ``CONTEXT_EVICT=0`` / ``CONTEXT_ENGINE_SUBSTRATE_DISABLE_SLICES=1``
switches still apply (they gate the shared eviction/slice plumbing).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Tuple

from agent.context_compressor import (
    EVICTION_STUB_PREFIX,
    _CHARS_PER_TOKEN,
    _content_length_for_budget,
)
from agent.context_engine_substrate import (
    SubstrateContextEngine,
    _DBUnavailable,
    _env_int,
    _make_handle,
)
from agent.model_metadata import estimate_messages_tokens_rough

logger = logging.getLogger(__name__)

# Round-2 cooling tunables (plan "Round 2"). All env-overridable.

# A tool result is COOLED once at least this many assistant-with-tool_calls
# turns have happened AFTER it. Five turns is the plan's starting heuristic: far
# enough behind the tail that the model has almost certainly stopped reading the
# result, close enough that the in-place edit only invalidates a small suffix.
_DEFAULT_COOL_WINDOW_TURNS = 5

# Size floor: only distil tool results at least this many chars. Below it the
# stub (~200-300 chars) reclaims little — not worth even a bounded near-tail
# cache invalidation.
_DEFAULT_COOL_MIN_CHARS = 1_500


class CoolingContextEngine(SubstrateContextEngine):
    """Age-triggered proactive variant of :class:`SubstrateContextEngine`.

    Distils cooled tool results in place *before* the context reaches the
    compaction threshold, so the parent's tiered ``compress()`` — the emergency
    backstop — is expected to fire ~never (plan Round 2). Everything else
    (handles, stubs, eviction slices, reinforcement, telemetry, the Tier-0/1/2
    ladder) is inherited unchanged.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cool_window_turns = _env_int(
            "CONTEXT_COOL_WINDOW_TURNS", _DEFAULT_COOL_WINDOW_TURNS
        )
        self._cool_min_chars = _env_int(
            "CONTEXT_COOL_MIN_CHARS", _DEFAULT_COOL_MIN_CHARS
        )

    @property
    def name(self) -> str:
        return "cooling"

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    @staticmethod
    def _cool_disabled() -> bool:
        """True when ``CONTEXT_COOL=0`` — fall back to pure parent behaviour."""
        return os.environ.get("CONTEXT_COOL", "1") == "0"

    # ------------------------------------------------------------------
    # The cooling rule (structural half — no DB). Shared by the preflight
    # existence check and the distillation candidate scan.
    # ------------------------------------------------------------------

    def _cooled_predicate(self, msg: Dict[str, Any], assistant_tool_turns_after: int) -> bool:
        """Is ``msg`` a *structurally* cooled tool result?

        The cooling rule minus the durability check (which needs the DB and is
        deferred to the distillation pass, per plan Round 2). A message is
        structurally cooled when it is a ``role:"tool"`` message whose body is a
        string at least ``CONTEXT_COOL_MIN_CHARS`` long, is not already a stub,
        and has at least ``CONTEXT_COOL_WINDOW_TURNS`` assistant-with-tool_calls
        turns after it. The persistence + hot-page checks are applied later.
        """
        if msg.get("role") != "tool":
            return False
        content = msg.get("content")
        if not isinstance(content, str) or len(content) < self._cool_min_chars:
            return False
        if content.startswith(EVICTION_STUB_PREFIX):
            return False
        return assistant_tool_turns_after >= self._cool_window_turns

    def _has_structurally_cooled(self, messages: List[Dict[str, Any]]) -> bool:
        """Cheap existence check for a structurally-cooled candidate.

        O(messages), early-exit, zero allocation beyond the boundary ints — it
        runs every loop iteration via :meth:`should_compress_preflight`, so it
        must stay allocation-light. Walks tail→head counting the running number
        of assistant-with-tool_calls turns seen so far (i.e. those *after* the
        current index) and returns on the first tool message that qualifies.
        """
        head_end = self._compressor._align_boundary_forward(
            messages, self._compressor._protect_head_size(messages)
        )
        assistant_tool_turns_after = 0
        for i in range(len(messages) - 1, head_end - 1, -1):
            msg = messages[i]
            role = msg.get("role")
            if role == "tool":
                if self._cooled_predicate(msg, assistant_tool_turns_after):
                    return True
            elif role == "assistant" and msg.get("tool_calls"):
                assistant_tool_turns_after += 1
        return False

    def _structurally_cooled_indices(self, messages: List[Dict[str, Any]]) -> List[int]:
        """All structurally-cooled candidate indices, oldest-first (ascending).

        Same scan as :meth:`_has_structurally_cooled`, but collects every match
        (the distillation pass distils them all — no pressure target). Head
        (system + ``protect_first_n``) is protected; the cooling window itself
        keeps candidates off the live tail, and tool messages are never the
        newest user message, so those invariants hold structurally.
        """
        head_end = self._compressor._align_boundary_forward(
            messages, self._compressor._protect_head_size(messages)
        )
        out: List[int] = []
        assistant_tool_turns_after = 0
        for i in range(len(messages) - 1, head_end - 1, -1):
            msg = messages[i]
            role = msg.get("role")
            if role == "tool":
                if self._cooled_predicate(msg, assistant_tool_turns_after):
                    out.append(i)
            elif role == "assistant" and msg.get("tool_calls"):
                assistant_tool_turns_after += 1
        out.reverse()  # oldest-first, so the re-stabilised prefix is maximal
        return out

    # ------------------------------------------------------------------
    # Preflight — the proactive trigger. Wired to fire compress() BEFORE the
    # next API call whenever cooling work exists, independent of token pressure.
    # ------------------------------------------------------------------

    def should_compress_preflight(self, messages: List[Dict[str, Any]]) -> bool:
        """True when there is age-based distillation work to do this turn.

        Independent of token pressure — that is the whole point of the Round-2
        mechanism: shape content as it cools so the context never fills. The
        durability check is deferred to :meth:`compress` (unpersisted candidates
        cool again next pass once flushed), keeping this hot-path check DB-free.

        With ``CONTEXT_COOL=0`` this reverts to the parent's preflight (which
        delegates to the inner compressor's — ``False`` by default).
        """
        if self._cool_disabled():
            return super().should_compress_preflight(messages)
        return self._has_structurally_cooled(messages)

    # ------------------------------------------------------------------
    # compress() — distillation pass first, parent ladder as the backstop.
    # ------------------------------------------------------------------

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int = None,
        focus_topic: str = None,
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        """Age-based distillation, then the parent ladder only if pressure holds.

        Order (plan Round 2):

          1. **Distillation** — the parent's Tier-1 operation (in-place body →
             stub + handle + gist, one eviction slice per distilled message, hot
             pages protected) over the COOLED candidate set, with **no**
             ``clear_at_least`` floor and **no** pressure target: distil
             everything cooled. Passes are small and frequent by design, and
             the near-tail edit bounds the prefix-cache loss to the cooling
             window.
          2. **Backstop** — if, after distillation, the organ's *real* threshold
             pressure still holds (``should_compress`` on the current estimate),
             fall through to the parent's full tiered ``compress()`` (Tier 0/1/2).
             That path — and only that path — can increment ``compression_count``
             (the backstop-firings metric) and rotate the session. It is expected
             to fire ~never.

        When distillation alone kept us under threshold, ``_last_compress_eviction_only``
        is set (no session rotation) and the distilled list is returned.
        """
        # Kill switch: behave exactly like the parent (threshold-triggered ladder).
        if self._cool_disabled():
            return super().compress(
                messages, current_tokens=current_tokens,
                focus_topic=focus_topic, force=force,
            )

        # Reset the eviction-only seam + the organ's per-call summary-failure
        # flags up front: on the distillation-only path we never enter the
        # parent compress() (which is what normally clears them), so a stale flag
        # from a prior Tier-2 abort would otherwise mislead compress_context.
        self._last_compress_eviction_only = False
        self._compressor._last_compress_aborted = False
        self._compressor._last_summary_error = None
        self._compressor._last_aux_model_failure_error = None
        self._compressor._last_aux_model_failure_model = None

        # Distillation, like Tier 1, needs the durable session store (the handle
        # target) and a known lineage. Without either — PG down, disabled
        # install, not yet attached — there is nothing to distil restorably, so
        # delegate straight to the parent ladder (whose own DB gate then routes
        # to Tier-2). Behaviour is byte-identical to the parent in that regime.
        db = None
        if os.environ.get("CONTEXT_EVICT", "1") != "0" and self._session_id:
            try:
                db = self._resolve_db(None)
            except _DBUnavailable:
                db = None
        if db is None:
            return super().compress(
                messages, current_tokens=current_tokens,
                focus_topic=focus_topic, force=force,
            )

        # ---- Distillation pass (age-based; no target, no floor) ----
        self._eviction_pass_count += 1
        distilled, reclaimed, n_distilled, n_skipped_unpersisted = self._distill_cooled(
            messages, db,
        )

        post_est = estimate_messages_tokens_rough(distilled)
        # Keep the organ's token state honest so pressure math sees the
        # POST-distillation size.
        self._compressor.last_prompt_tokens = post_est

        # Emergency-backstop condition: does REAL threshold pressure still hold
        # after distillation? If so, distillation alone was not enough and we
        # escalate to the full ladder; otherwise this was a proactive,
        # rotation-free pass (the expected common case).
        pressure_holds = self._compressor.should_compress(post_est)

        # Mint one eviction pointer slice per distilled message (trigger=cooling)
        # so proactive recall can page distilled content back into THIS session.
        # survived_in_context mirrors the parent's semantics: True when the stubs
        # survive (distillation-only), False when the backstop's Tier-2 then
        # paraphrases them away. Best-effort + fully guarded.
        if self._pending_eviction_records:
            self._commit_eviction_slices(
                self._pending_eviction_records,
                survived_in_context=not pressure_holds,
            )

        self._emit_evicted_telemetry(
            trigger="cooling",
            reclaimed=reclaimed,
            evicted=n_distilled,
            skipped_unpersisted=n_skipped_unpersisted,
            escalated_to_tier2=pressure_holds,
        )

        if not pressure_holds:
            # Distillation kept us clear — no summarisation, no rotation.
            self._last_compress_eviction_only = True
            return distilled

        # ---- BACKSTOP: real pressure remains → parent's full tiered ladder ----
        # Feed the already-distilled list; the parent re-runs Tier 0/1/2, emits
        # its own context.evicted (trigger="threshold"), and — if Tier 2 fires —
        # increments compression_count and takes the rotation path.
        return super().compress(
            distilled, current_tokens=post_est,
            focus_topic=focus_topic, force=force,
        )

    def _distill_cooled(
        self, messages: List[Dict[str, Any]], db,
    ) -> Tuple[List[Dict[str, Any]], int, int, int]:
        """Distil every cooled, persisted, non-hot tool result to a stub.

        Returns ``(new_messages, reclaimed_tokens, n_distilled, n_skipped_unpersisted)``.

        The per-message operation is exactly the parent's Tier-1 (copy the list,
        replace only evicted tool-result *content* with an actionable stub +
        handle — never the role, ``tool_call_id``, or any assistant
        ``tool_calls``, so count/order/roles are byte-identical). The two
        differences from Tier-1: candidates come from the age-based cooling rule
        (not the pressure band), and there is no target/floor — we distil the
        whole cooled set. Unpersisted candidates are skipped (deferred, not lost:
        they cool again next pass once flushed); hot pages are protected.
        """
        result = [m.copy() for m in messages]
        # Fresh per pass — compress() flushes these to the substrate after it
        # knows whether the backstop escalated (mirrors the parent's staging).
        self._pending_eviction_records = []

        cooled = self._structurally_cooled_indices(result)
        if not cooled:
            return result, 0, 0, 0

        candidates: List[int] = []
        call_ids: List[str] = []
        for i in cooled:
            cid = result[i].get("tool_call_id")
            if not cid:
                continue  # can't mint a resolvable handle without a call id
            candidates.append(i)
            call_ids.append(cid)
        if not candidates:
            return result, 0, 0, 0

        # Resolve tool_call_id -> (session_id, message_id) in one query across
        # the lineage. Absent ids are not yet flushed to the store and must be
        # skipped: never distil content that isn't durably retrievable.
        lineage = self._session_lineage(db, self._session_id) if self._session_id else []
        try:
            resolved: Dict[str, tuple] = db.resolve_tool_call_message_ids(lineage, call_ids) or {}
        except Exception as exc:  # DB hiccup → distil nothing, let the backstop cope
            logger.warning("cooling handle resolution failed: %s", exc, exc_info=True)
            return result, 0, 0, 0

        tool_names = self._tool_name_index(result)

        reclaimed = 0
        n_distilled = 0
        n_skipped_unpersisted = 0
        records: List[Dict[str, Any]] = []

        for idx, cid in zip(candidates, call_ids):
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
            gist = self._compute_gist(orig)
            stub = self._make_stub(handle, tool_name, orig, gist)
            delta = max(
                0,
                _content_length_for_budget(orig) // _CHARS_PER_TOKEN
                - len(stub) // _CHARS_PER_TOKEN,
            )
            result[idx] = {**msg, "content": stub}
            reclaimed += delta
            n_distilled += 1
            records.append(
                {
                    "handle": handle,
                    "tool_name": tool_name,
                    "gist": gist,
                    "orig_len": len(orig),
                    # Distinguishes cooling-distilled pointers from the parent's
                    # threshold-triggered ones in the slice payload (Round 2).
                    "trigger": "cooling",
                }
            )

        self._pending_eviction_records = records
        return result, reclaimed, n_distilled, n_skipped_unpersisted

    async def _acommit_eviction_slices(
        self,
        substrate: Any,
        records: List[Dict[str, Any]],
        *,
        survived_in_context: bool,
    ) -> None:
        """Commit pointer slices, tagging each with its eviction ``trigger``.

        Overrides the parent solely to stamp ``trigger`` into the slice payload
        so Phase-3 analysis can separate cooling-distilled pointers
        (``trigger="cooling"``) from the backstop's threshold-triggered ones
        (records minted by the parent's Tier-1 carry no trigger → default
        ``"threshold"``). Every other field, the born-passed/consolidated
        semantics, and the guarded best-effort contract are identical to the
        parent's.
        """
        from datetime import datetime, timezone

        from agent.context_engine_substrate import _CONTEXT_EVICTED_STREAM
        from substrate.l0 import commit_slice

        stream = await substrate.streams.get_by_name(_CONTEXT_EVICTED_STREAM)
        if stream is None:
            logger.debug(
                "eviction stream %s not registered — skipping pointer slices",
                _CONTEXT_EVICTED_STREAM,
            )
            return
        now = datetime.now(timezone.utc)
        for rec in records:
            handle = rec["handle"]
            tool_name = rec["tool_name"]
            gist = rec["gist"]
            trigger = rec.get("trigger", "threshold")
            text = (
                f"{tool_name}: {gist} — "
                f'Retrieve exact: context_expand("{handle}")'
            )
            await commit_slice(
                substrate,
                stream.stream_id,
                {
                    "kind": "context_evicted",
                    "handle": handle,
                    "tool_name": tool_name,
                    "gist": gist,
                    "orig_len": rec["orig_len"],
                    "text": text,
                    "survived_in_context": survived_in_context,
                    "trigger": trigger,
                },
                event_time_world=now,
                metadata={
                    "session_id": self._session_id,
                    "source": "context_engine",
                    "trigger": trigger,
                },
                born_passed=True,
                born_consolidated=True,
            )


__all__ = ["CoolingContextEngine"]
