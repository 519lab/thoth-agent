"""Public recall API — Phase C Tasks 7 + 10 / spec §4.

``recall(...)`` is the async entry point that the
:class:`SubstrateMemoryProvider` calls. It orchestrates the pipeline:

  1. embed_query (optional, timeout-bounded)
  2. recall_window SQL (timeout-bounded; the only step that can timeout
     the whole call — embedding + ranking are bounded by their own
     budgets but the SQL is the load-bearing latency contributor)
  3. rank_candidates (pure-function)
  4. compose_projection (pure-function, token-budgeted)
  5. reinforce_hits (fire-and-forget per-slice via Phase B reinforce_slice)
  6. log_recall (enqueue to RecallLogWriter, non-blocking)

Failures NEVER reach the caller — the function always returns a
:class:`RecallProjection`, possibly empty with ``empty_reason`` set.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from substrate import config as _cfg
from substrate.recall.composer import (
    compose_projection,
    render_l1_header,
    render_l3_header,
    render_l4_header,
)
from substrate.recall.embeddings import embed_query
from substrate.recall.log import RecallLogRow
from substrate.recall.projection import (
    RecallCandidate,
    RecallProjection,
    rank_candidates,
    rank_candidates_scored,
)

if TYPE_CHECKING:  # pragma: no cover
    from substrate.facade import Substrate


_log = logging.getLogger("substrate.recall.api")


# ---------------------------------------------------------------------------
# In-process reinforcement rate-limit (spec §5.4).
#
# Bounded by an LRU of recent timestamps per slice_id. ``_REINFORCE_LRU``
# is process-wide — correct for single-process Thoth (the gateway loops
# 128 AIAgents inside one process). Multi-process scale-out would need
# this to move to PG; that's Phase G.
# ---------------------------------------------------------------------------


_REINFORCE_LRU: dict[UUID, list[float]] = {}
_REINFORCE_LRU_MAX_SIZE = 1024


# ---------------------------------------------------------------------------
# Coherence-pin cache (Task: pin coherence to recall).
#
# When substrate coherence dips, recall raises its relevance floor so only
# high-confidence context reaches the foreground. Reading the coherence vital
# sign hits L4, so we cache it behind a short monotonic-clock TTL to keep the
# recall hot path off that query. Unavailable coherence is treated as 1.0
# (no pinning) — a missing/erroring read never narrows recall.
# ---------------------------------------------------------------------------


_COHERENCE_CACHE_TTL_S = 30.0
# (value, monotonic_deadline). ``value`` is the cached coherence in [0,1];
# 1.0 means "no pinning". Module-level so it's shared across the in-process
# AIAgent fleet.
_COHERENCE_CACHE: tuple[float, float] = (1.0, 0.0)


async def _cached_coherence() -> float:
    """Return the latest substrate coherence in [0,1], cached for
    ``_COHERENCE_CACHE_TTL_S`` against a monotonic clock.

    Unavailable / erroring coherence resolves to 1.0 (no pinning). Uses
    ``time.monotonic()`` deliberately — wall-clock jumps (NTP / suspend)
    must not extend or collapse the TTL."""
    global _COHERENCE_CACHE
    now = time.monotonic()
    value, deadline = _COHERENCE_CACHE
    if now < deadline:
        return value

    coherence = 1.0
    try:
        from substrate.l4 import store as l4

        obs = await l4.latest_coherence()
        if obs is not None and obs.score is not None:
            coherence = float(obs.score)
    except Exception as exc:  # pragma: no cover — defensive
        _log.debug("recall coherence read failed: %s", exc)
        coherence = 1.0

    _COHERENCE_CACHE = (coherence, now + _COHERENCE_CACHE_TTL_S)
    return coherence


def _reset_coherence_cache() -> None:
    """Test hook — drop the cached coherence so the next read re-queries."""
    global _COHERENCE_CACHE
    _COHERENCE_CACHE = (1.0, 0.0)


def _evict_lru_if_full() -> None:
    """Drop the oldest entry when the LRU dict grows past the cap."""
    if len(_REINFORCE_LRU) <= _REINFORCE_LRU_MAX_SIZE:
        return
    # dict iteration order in Python 3.7+ is insertion order — first
    # key is the oldest. Pop it.
    oldest = next(iter(_REINFORCE_LRU))
    _REINFORCE_LRU.pop(oldest, None)


def _reinforce_allowed(slice_id: UUID, now: float) -> bool:
    """Check + record a reinforcement under the per-slice rate cap.

    Returns True if the caller may proceed with the reinforcement;
    False if the slice has already received the maximum bumps in the
    last 60 seconds. Has a side effect — when True, records the new
    timestamp.
    """
    history = _REINFORCE_LRU.get(slice_id, [])
    # Drop timestamps older than 60s.
    history = [t for t in history if t > now - 60.0]
    if len(history) >= _cfg.RECALL_REINFORCE_RATE_LIMIT_PER_MIN:
        _REINFORCE_LRU[slice_id] = history
        return False
    history.append(now)
    _REINFORCE_LRU[slice_id] = history
    _evict_lru_if_full()
    return True


def _summarise_embedding_path(
    query_embedding: Optional[list[float]],
    composed: list[RecallCandidate],
) -> str:
    """Tag the recall call with the embedding-path it used (spec §5.2).

    Returns one of: 'semantic' (all composed had embeddings + query
    had embedding), 'keyword' (no embeddings used at all), 'mixed'
    (some composed had embeddings, others didn't), or 'empty' (no
    composed candidates)."""
    if not composed:
        return "empty"
    if query_embedding is None:
        return "keyword"
    embedded = sum(1 for c in composed if c.embedding is not None)
    if embedded == len(composed):
        return "semantic"
    if embedded == 0:
        return "keyword"
    return "mixed"


async def _reinforce_hits(
    substrate: "Substrate",
    composed: list[RecallCandidate],
    relevance_by_id: "dict[UUID, float]",
) -> int:
    """Fire reinforcement for each composed slice, weighted by its
    topical relevance to the query and subject to the per-slice rate cap.
    Failures are logged + swallowed (the recall pipeline never raises).

    A slice whose relevance is below ``RECALL_REINFORCE_MIN_RELEVANCE``
    (it entered the projection on salience/recency, not topical match) is
    skipped entirely — no bump AND no decay-clock reset — so it ages out
    instead of being frozen alive by being recalled. This is what breaks
    the recall→reinforce→rank feedback loop. Above the floor, the bump is
    scaled by relevance so the strongest matches are reinforced most.

    Returns the number of reinforcements actually applied (observability)."""
    from substrate.l0.api import reinforce_slice

    now = time.time()
    applied = 0
    for c in composed:
        slice_id = c.slice_id
        relevance = relevance_by_id.get(slice_id, 0.0)
        if relevance < _cfg.RECALL_REINFORCE_MIN_RELEVANCE:
            continue
        if not _reinforce_allowed(slice_id, now):
            continue
        try:
            await reinforce_slice(substrate, slice_id, scale=relevance)
            applied += 1
        except Exception as exc:
            _log.warning(
                "reinforce after recall failed for slice %s: %s",
                slice_id,
                exc,
            )
    return applied


# ---------------------------------------------------------------------------
# Public surface — recall + sync facade.
# ---------------------------------------------------------------------------


async def recall(
    substrate: "Substrate",
    query: str,
    *,
    session_id: Optional[str] = None,
    t_now: Optional[datetime] = None,
    token_budget: Optional[int] = None,
    time_window: Optional[timedelta] = None,
    stream_filter: Optional[list[str]] = None,
    min_salience: Optional[float] = None,
    candidate_limit: Optional[int] = None,
    recall_timeout_ms: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> RecallProjection:
    """Compose a salience-weighted, time-windowed, token-budgeted text
    projection of L0 slices relevant to ``query``.

    Defaults are sourced from ``substrate.config`` (env-tunable per
    spec §5.6); pass explicit kwargs to override per-call.

    Always returns a ``RecallProjection``. On any internal failure the
    returned projection has ``text=""`` and ``empty_reason`` set
    explaining why (no_candidates / budget_zero / all_truncated /
    timeout / db_error). The caller (SubstrateMemoryProvider) never
    needs to try/except — substrate failures never reach Thoth's call
    site (mirrors the Phase A hook discipline).
    """
    t_now = t_now or datetime.now(timezone.utc)
    token_budget = (
        token_budget if token_budget is not None else _cfg.RECALL_TOKEN_BUDGET
    )
    time_window = (
        time_window if time_window is not None
        else timedelta(hours=_cfg.RECALL_TIME_WINDOW_HOURS)
    )
    stream_filter = stream_filter or list(_cfg.DEFAULT_RECALL_STREAMS)
    min_salience = (
        min_salience if min_salience is not None else _cfg.RECALL_MIN_SALIENCE
    )
    candidate_limit = (
        candidate_limit if candidate_limit is not None
        else _cfg.RECALL_CANDIDATE_LIMIT
    )
    recall_timeout_ms = (
        recall_timeout_ms if recall_timeout_ms is not None
        else _cfg.RECALL_TIMEOUT_MS
    )

    # Drop the live session's own slices from the candidate pool when
    # RECALL_EXCLUDE_CURRENT_SESSION is on (issue #178) — they're already in
    # the transcript, so recalling them just echoes the conversation. Only
    # applies when we know which session we're recalling for.
    exclude_session_id = (
        session_id
        if (session_id and _cfg.RECALL_EXCLUDE_CURRENT_SESSION)
        else None
    )

    t_start = time.monotonic()

    # 1+2. Embed the query and fetch candidates — both wrapped in the
    # recall_timeout_ms budget. We run them sequentially because the
    # SQL needs no embedding input; the embedding is for ranking after
    # the SQL returns.
    try:
        candidates = await asyncio.wait_for(
            _fetch_candidates(
                substrate,
                t_now=t_now,
                time_window=time_window,
                stream_names=stream_filter,
                min_salience=min_salience,
                limit=candidate_limit,
                exclude_session_id=exclude_session_id,
            ),
            timeout=recall_timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        duration_ms = int((time.monotonic() - t_start) * 1000)
        proj = RecallProjection(
            text="", tokens_used=0, composed=[], candidates_seen=0,
            duration_ms=duration_ms, timed_out=True, empty_reason="timeout",
        )
        _safe_enqueue_log(
            substrate, t_now, session_id, query, proj, metadata,
            error_text="recall window timed out",
        )
        return proj
    except Exception as exc:
        duration_ms = int((time.monotonic() - t_start) * 1000)
        _log.warning("recall db error: %s", exc)
        proj = RecallProjection(
            text="", tokens_used=0, composed=[], candidates_seen=0,
            duration_ms=duration_ms, timed_out=False, empty_reason="db_error",
        )
        _safe_enqueue_log(
            substrate, t_now, session_id, query, proj, metadata,
            error_text=str(exc),
        )
        return proj

    # 1b. Embed the query (best-effort; None on failure → keyword path
    # forced for all candidates). Bounded by its own timeout from the
    # embeddings module. ``RECALL_EMBEDDING_MODEL`` is the override knob
    # (None by default) — when unset, ``embed_query`` reads
    # ``auxiliary.embedding.model`` from config. Forcing a model name
    # here would override the operator's provider choice and 404 on
    # non-OpenAI endpoints. See substrate/config.py.
    eq_kwargs = {"timeout_ms": _cfg.RECALL_EMBEDDING_TIMEOUT_MS}
    if _cfg.RECALL_EMBEDDING_MODEL is not None:
        eq_kwargs["model"] = _cfg.RECALL_EMBEDDING_MODEL
    try:
        query_embedding = await embed_query(query, **eq_kwargs)
    except Exception as exc:
        _log.debug("query embedding failed: %s", exc)
        query_embedding = None

    # 3. Rank (scored, so we can apply a relevance floor + record why).
    scored = rank_candidates_scored(
        candidates,
        query,
        query_embedding,
        t_now=t_now,
        similarity_weight=_cfg.RECALL_SIMILARITY_WEIGHT,
        keyword_overlap_weight=_cfg.RECALL_KEYWORD_WEIGHT,
        salience_weight=_cfg.RECALL_SALIENCE_WEIGHT,
        recency_weight=_cfg.RECALL_RECENCY_WEIGHT,
        recency_half_life_hours=_cfg.RECALL_RECENCY_HALF_LIFE_HOURS,
    )

    # 3z. Optional LLM-judge rerank of the top-K (innovation #3). Inserted
    # after the scalar ranker and before the relevance floor so the judge sees
    # the strongest candidates and the floor then prunes whatever survives the
    # reorder. Gated behind RECALL_RERANK (default off); on ANY failure rerank()
    # returns the scored order unchanged — recall never raises.
    if _cfg.RECALL_RERANK and scored:
        from substrate.recall.rerank import rerank

        judge = _build_recall_reranker_judge(substrate)
        if judge is not None:
            k = max(0, min(_cfg.RECALL_RERANK_K, len(scored)))
            head = await rerank(query, scored[:k], judge=judge)
            scored = head + scored[k:]

    # 3a0. Coherence pin — when the substrate's self-assessed identity
    # health dips, raise the relevance floor so only high-confidence
    # context reaches the foreground (low coherence => thin, precise
    # recall). ``coherence``/``coherence_floor`` are recorded below so
    # operators can explain a thin block. Gated behind
    # RECALL_COHERENCE_PIN (default on); when off, coherence stays 1.0 and
    # coherence_floor is 0.0 — byte-identical floor to pre-pin behaviour.
    coherence = 1.0
    coherence_floor = 0.0
    if _cfg.RECALL_COHERENCE_PIN:
        coherence = await _cached_coherence()
        deficit = max(0.0, 1.0 - coherence)
        coherence_floor = deficit * _cfg.RECALL_COHERENCE_FLOOR_MAX

    # 3a. Relevance floor — precision over volume. Keep only candidates
    # clearing BOTH an absolute floor (drop near-zero) and a relative
    # floor (fraction of the top score — adapts to the semantic/keyword
    # score regime; the strongest hit always survives), plus the
    # coherence floor (0.0 when the pin is off / coherence is healthy).
    # This is what stops the substrate dumping loosely-related context.
    if scored:
        top = scored[0].score
        rel_floor = _cfg.RECALL_RELATIVE_FLOOR * top
        floor = max(_cfg.RECALL_MIN_RELEVANCE, rel_floor, coherence_floor)
        kept = [sc for sc in scored if sc.score >= floor] or scored[:1]
    else:
        kept = []
    ranked = [sc.candidate for sc in kept]
    provenance = {sc.candidate.slice_id: f"{sc.score:.2f} {sc.path}" for sc in kept}
    # Topical relevance per kept slice — recall reinforcement is weighted by
    # this so salience-only survivors aren't pumped further (breaks the loop).
    relevance_by_id = {sc.candidate.slice_id: sc.relevance for sc in kept}

    # 3b. Fetch bounded higher-layer headers (best-effort — a missing layer
    # or DB hiccup degrades to no header, never an error). They share the
    # token budget: prepended ahead of the L0 quotes in cognitive order
    # (entities → patterns → self-model → episodes), so the L0 composer gets
    # whatever budget is left. L1 is Phase D; L3/L4 added 2026-06-17 so recall
    # can reach the substrate's abstractions, not just raw episodes.
    headers: list[str] = []
    has_query = bool((query or "").strip())
    if _cfg.RECALL_INCLUDE_L1 and has_query:
        try:
            h = await _build_l1_header(query)
            if h:
                headers.append(h)
        except Exception as exc:  # pragma: no cover — defensive
            _log.debug("recall L1 header fetch failed: %s", exc)
    if _cfg.RECALL_INCLUDE_L3 and has_query:
        try:
            h = await _build_l3_header(query, query_embedding=query_embedding)
            if h:
                headers.append(h)
        except Exception as exc:  # pragma: no cover — defensive
            _log.debug("recall L3 header fetch failed: %s", exc)
    if _cfg.RECALL_INCLUDE_L4 and has_query:
        try:
            h = await _build_l4_header(query, query_embedding=query_embedding)
            if h:
                headers.append(h)
        except Exception as exc:  # pragma: no cover — defensive
            _log.debug("recall L4 header fetch failed: %s", exc)
    header_text = "\n\n".join(headers)
    header_tokens = max(1, len(header_text) // 4) if header_text else 0

    # 4. Compose (L0 quotes get the budget left after the higher-layer
    # headers). Dedup near-duplicate excerpts; provenance recorded always,
    # shown inline only when RECALL_SHOW_PROVENANCE (clean block by default).
    l0_budget = max(0, token_budget - header_tokens)
    text, composed, tokens = compose_projection(
        ranked,
        token_budget=l0_budget,
        dedup_threshold=_cfg.RECALL_DEDUP_THRESHOLD,
        provenance=provenance,
        show_provenance=_cfg.RECALL_SHOW_PROVENANCE,
    )
    if header_text:
        text = header_text + ("\n\n" + text if text else "")
        tokens += header_tokens

    # 4b. Opt-in skill suggestion — append a compact
    # "## Relevant skills" footer when the query maps to bundled skills.
    # Default OFF so it never adds noise unless wanted; best-effort.
    if _cfg.RECALL_SUGGEST_SKILLS and (query or "").strip():
        try:
            from substrate.skills_match import suggest_skills

            hits = suggest_skills(query, limit=_cfg.RECALL_SKILL_LIMIT)
            if hits:
                footer = "## Relevant skills\n" + "\n".join(
                    f"- {h['name']}" for h in hits
                )
                text = (text + "\n\n" + footer) if text else footer
                tokens += max(1, len(footer) // 4)
        except Exception as exc:  # pragma: no cover — best-effort
            _log.debug("recall skill-suggestion failed: %s", exc)

    # 5. Reinforce hits (no await on failure — fire-and-forget).
    # Note: we await here for testability; the actual work is per-slice
    # rate-limited so this is short. A future async-only refactor can
    # promote to asyncio.create_task.
    try:
        await _reinforce_hits(substrate, composed, relevance_by_id)
    except Exception as exc:
        _log.warning("reinforce hits batch failed: %s", exc)

    duration_ms = int((time.monotonic() - t_start) * 1000)

    # Derive empty_reason for observability.
    if text:
        empty_reason = None
    elif token_budget == 0:
        empty_reason = "budget_zero"
    elif not candidates:
        empty_reason = "no_candidates"
    else:
        empty_reason = "all_truncated"

    proj = RecallProjection(
        text=text,
        tokens_used=tokens,
        composed=composed,
        candidates_seen=len(candidates),
        duration_ms=duration_ms,
        timed_out=False,
        empty_reason=empty_reason,
    )

    # 6. Log. The metadata blob captures embedding-path tag for the
    # operator-validation window (spec §5.2).
    embedding_path = _summarise_embedding_path(query_embedding, composed)
    extra_meta = dict(metadata or {})
    extra_meta.update(
        empty_reason=empty_reason,
        embedding_path=embedding_path,
        # Per-candidate record for the offline recall-replay harness
        # (innovation #1). The full candidate objects aren't persisted
        # anywhere, so the replay sweep has nothing to re-rank against unless
        # we stash the ranking inputs here. JSONB metadata only — no schema
        # change. ``relevance`` is the fixed similarity term (the harness
        # sweeps salience/recency weights against it); ``path`` records which
        # similarity path produced it (semantic vs keyword).
        candidates=[
            {
                "slice_id": str(sc.candidate.slice_id),
                "salience": float(sc.candidate.salience_score),
                "event_time": sc.candidate.event_time_world.isoformat(),
                "relevance": float(sc.relevance),
                "path": sc.path,
            }
            for sc in kept
        ],
        # "Why injected" — the score + path for each composed slice, so
        # `recall recent` / `recall validate` can explain the block even
        # when provenance isn't shown inline.
        provenance={
            str(c.slice_id): provenance.get(c.slice_id) for c in composed
        },
        candidates_kept=len(ranked),
        # Coherence pin provenance — lets `recall recent` / `recall validate`
        # explain a thin block as "coherence was low, floor was raised"
        # rather than a silent recall miss. Stuffed into the existing
        # metadata JSON (no DB column / migration).
        coherence=coherence,
        coherence_floor=coherence_floor,
    )
    _safe_enqueue_log(
        substrate, t_now, session_id, query, proj, extra_meta, error_text=None,
    )
    return proj


# ---------------------------------------------------------------------------
# Recall reranker judge (innovation #3).
#
# Builds the LLM-judge that substrate.recall.rerank.rerank() calls. Reuses the
# aux text client (no new dependency) under the ``recall_reranker`` task name;
# the whole top-K window is batched into ONE chat call that returns a JSON
# array of 0-based indices in best-first order. The judge raises on any
# failure (missing client, timeout, malformed output) — rerank() swallows it
# and falls back to the scored order, so recall never raises.
# ---------------------------------------------------------------------------


# Per-judge-call ceiling. Generous enough for the small aux models but bounded
# so a stalled provider can't hold the recall hot path. Reuses the recall
# embedding timeout budget shape (ms → s) rather than inventing a new knob.
_RERANK_TIMEOUT_S = max(1.0, _cfg.RECALL_EMBEDDING_TIMEOUT_MS / 1000.0 * 4)


def _build_recall_reranker_judge(substrate: "Substrate"):
    """Return an async judge ``(query, excerpts) -> list[int]`` backed by the
    aux text client, or ``None`` when no aux provider is configured (rerank is
    then skipped). The judge is timeout-bound and parses a JSON index array;
    any error propagates to ``rerank()`` which falls back to the scored order."""
    from agent.auxiliary_client import get_async_text_auxiliary_client

    client, model = get_async_text_auxiliary_client("recall_reranker")
    if client is None:
        return None

    async def _judge(query: str, excerpts: list[str]) -> list[int]:
        from substrate import cost
        from substrate.l1.extract import _strip_fences

        listing = "\n".join(f"[{i}] {ex}" for i, ex in enumerate(excerpts))
        prompt = (
            "You are reranking recalled memory excerpts by how directly each "
            "one helps answer the user's query. Return ONLY a JSON array of the "
            "excerpt indices in best-first order (most relevant first), every "
            "index exactly once.\n\n"
            f"Query: {query}\n\n"
            f"Excerpts:\n{listing}\n\n"
            'Example output: [2, 0, 1]'
        )
        resp = await asyncio.wait_for(
            cost.acreate_and_record(
                client,
                substrate=substrate,
                agent="recall_reranker",
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            ),
            timeout=_RERANK_TIMEOUT_S,
        )
        raw = resp.choices[0].message.content or ""
        import json

        data = json.loads(_strip_fences(raw))
        if not isinstance(data, list):
            raise ValueError("reranker did not return a JSON array")
        return [int(x) for x in data]

    return _judge


async def _build_l1_header(query: str) -> str:
    """Fetch the top L1 entities for *query* (+ up to 2 citations each) and
    render the ``## Known entities`` block. Returns "" when L1 is empty or
    nothing matches. Used by :func:`recall` (Phase D §7)."""
    from substrate.l1 import store as l1_store

    entities = await l1_store.get_entities_for_query(
        query, limit=_cfg.RECALL_L1_LIMIT
    )
    if not entities:
        return ""
    rendered: list[dict] = []
    for e in entities:
        cites = await l1_store.list_citations_for_entity(e.id, limit=2)
        rendered.append(
            {
                "name": e.name,
                "entity_type": e.entity_type,
                "summary": e.summary,
                "cites": [str(c.slice_id)[:6] for c in cites],
            }
        )
    return render_l1_header(rendered)


async def _build_l3_header(
    query: str, *, query_embedding: Optional[list[float]] = None
) -> str:
    """Fetch the top L3 patterns for *query* and render the ``## Patterns``
    block. Returns "" when L3 is empty or nothing matches. Used by
    :func:`recall`.

    When ``RECALL_L3L4_SEMANTIC`` is on and a ``query_embedding`` is available,
    patterns are ordered by cosine distance over their (already-backfilled)
    embedding column; otherwise the trigram+salience path is used (innovation
    #3)."""
    from substrate.l3 import store as l3_store

    patterns = await l3_store.get_patterns_for_query(
        query,
        limit=_cfg.RECALL_L3_LIMIT,
        query_embedding=query_embedding if _cfg.RECALL_L3L4_SEMANTIC else None,
    )
    if not patterns:
        return ""
    rendered = [
        {
            "kind": p.kind,
            "statement": p.statement,
            # Shorten citation slice-ids and cap at 3 so the block stays compact.
            "cites": [str(c)[:6] for c in (p.cites or [])][:3],
        }
        for p in patterns
    ]
    return render_l3_header(rendered)


async def _build_l4_header(
    query: str, *, query_embedding: Optional[list[float]] = None
) -> str:
    """Fetch the top L4 self-model observations for *query* and render the
    ``## Self-model`` block. Returns "" when L4 is empty or nothing matches.
    Excludes the coherence vital sign (handled separately as the recall
    relevance-floor pin). Used by :func:`recall`.

    When ``RECALL_L3L4_SEMANTIC`` is on and a ``query_embedding`` is available,
    observations are ordered by cosine distance over their (already-backfilled)
    embedding column; otherwise the trigram+salience path is used (innovation
    #3)."""
    from substrate.l4 import store as l4_store

    observations = await l4_store.get_observations_for_query(
        query,
        limit=_cfg.RECALL_L4_LIMIT,
        query_embedding=query_embedding if _cfg.RECALL_L3L4_SEMANTIC else None,
    )
    if not observations:
        return ""
    rendered = [
        {"kind": o.kind, "subject": o.subject, "statement": o.statement}
        for o in observations
    ]
    return render_l4_header(rendered)


async def _fetch_candidates(
    substrate: "Substrate",
    *,
    t_now: datetime,
    time_window: timedelta,
    stream_names: list[str],
    min_salience: float,
    limit: int,
    exclude_session_id: Optional[str] = None,
) -> list[RecallCandidate]:
    """Acquire a connection and run the recall_window query.

    Separated from ``recall()`` so the timeout wrap is clean — the
    connection-acquisition + SQL execution are both inside the
    asyncio.wait_for boundary.
    """
    import thoth_db

    async with thoth_db.connection() as conn:
        return await substrate.slices.recall_window(
            conn,
            t_now=t_now,
            time_window=time_window,
            stream_names=stream_names,
            min_salience=min_salience,
            limit=limit,
            exclude_session_id=exclude_session_id,
        )


def _safe_enqueue_log(
    substrate: "Substrate",
    t_now: datetime,
    session_id: Optional[str],
    query: str,
    proj: RecallProjection,
    metadata: Optional[dict],
    *,
    error_text: Optional[str],
) -> None:
    """Enqueue a recall_log row, swallowing any error (the log writer
    may not be attached, e.g. in unit tests that bypass Substrate.boot)."""
    writer = getattr(substrate, "recall_log", None)
    if writer is None:
        return
    try:
        writer.enqueue(
            RecallLogRow(
                requested_at=t_now,
                session_id=session_id,
                query_excerpt=(query or "")[:200],
                candidates_count=proj.candidates_seen,
                composed_count=len(proj.composed),
                tokens_used=proj.tokens_used,
                duration_ms=proj.duration_ms,
                timed_out=proj.timed_out,
                error_text=error_text,
                metadata=dict(metadata or {}),
            )
        )
    except Exception as exc:
        _log.debug("recall log enqueue failed: %s", exc)


def recall_sync(
    substrate: "Substrate",
    query: str,
    *,
    session_id: Optional[str] = None,
    t_now: Optional[datetime] = None,
    token_budget: Optional[int] = None,
    time_window: Optional[timedelta] = None,
    stream_filter: Optional[list[str]] = None,
    min_salience: Optional[float] = None,
    candidate_limit: Optional[int] = None,
    recall_timeout_ms: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> RecallProjection:
    """Sync facade — bridges to the async ``recall`` via
    :func:`thoth_db.run_sync`. Must NOT be called from inside a
    running event loop (the underlying ``run_sync`` raises).
    """
    import thoth_db

    return thoth_db.run_sync(
        recall(
            substrate,
            query,
            session_id=session_id,
            t_now=t_now,
            token_budget=token_budget,
            time_window=time_window,
            stream_filter=stream_filter,
            min_salience=min_salience,
            candidate_limit=candidate_limit,
            recall_timeout_ms=recall_timeout_ms,
            metadata=metadata,
        )
    )


__all__ = ["recall", "recall_sync"]
