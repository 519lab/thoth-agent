"""Substrate configuration — read once at boot.

Phase A introduced the minimal SubstrateConfig dataclass. Phase C adds
the recall + embedding knobs (spec §5.6) as module-level constants
read at import time from ``THOTH_RECALL_*`` env vars (with sane
defaults). Module-level rather than a dataclass because the recall
pipeline + Curator embedding loop read these in hot paths and a
per-call dataclass lookup is unnecessary overhead.

Mutating these constants at runtime is unsupported — set the env vars
before importing the recall package.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SubstrateConfig:
    """Frozen at boot — every sub-agent reads from the same snapshot.

    Phase A fields are intentionally narrow. As Phase B+ adds the
    Curator and the LLM sub-agents, this struct grows with their
    per-agent toggles.
    """

    # If true, ``Substrate.boot()`` runs ``alembic upgrade head`` when
    # the database is behind the expected revision. If false (default),
    # boot raises so the operator can decide. Mirrors Thoth's
    # ``THOTH_AUTO_MIGRATE`` convention from the Phase 0 ADR.
    auto_migrate: bool = False

    # Sub-agent boot toggles. Used by tests via
    # ``Substrate.boot(start_subagents=False)`` to take deterministic
    # control of the tick loop; not exposed as env vars in Phase A.
    start_subagents: bool = True

    @classmethod
    def from_env(cls) -> "SubstrateConfig":
        """Read settings from the process environment.

        Booleans are 'truthy if set to 1/true/yes (case-insensitive),
        falsy otherwise' — matches Thoth's convention across
        ``thoth_db`` and ``thoth_bootstrap``.
        """
        return cls(
            auto_migrate=_envbool("THOTH_AUTO_MIGRATE", default=False),
            start_subagents=True,
        )


def _envbool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _envint(name: str, *, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _envfloat(name: str, *, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Phase C — recall knobs (spec §5.6).
# ---------------------------------------------------------------------------

# Pipeline budgets.
RECALL_TOKEN_BUDGET = _envint("THOTH_RECALL_TOKEN_BUDGET", default=1500)
RECALL_TIME_WINDOW_HOURS = _envfloat("THOTH_RECALL_TIME_WINDOW_HOURS", default=24.0)
RECALL_TIMEOUT_MS = _envint("THOTH_RECALL_TIMEOUT_MS", default=300)
RECALL_MIN_SALIENCE = _envfloat("THOTH_RECALL_MIN_SALIENCE", default=0.05)
RECALL_CANDIDATE_LIMIT = _envint("THOTH_RECALL_CANDIDATE_LIMIT", default=50)

# Exclude the current session's own slices from recall (added 2026-06-17,
# issue #178). recall_window is time-windowed but not session-aware, so during
# a live session it re-surfaces the current session's user/assistant turns —
# which are already verbatim in the conversation transcript the model is
# reasoning over. Recall's distinct value is *cross-session*: bringing back
# what is NOT in the current context. When on, recall() filters out slices
# whose metadata->>'session_id' equals the session it was called for (session-
# less slices are always kept). Tradeoff: also hides within-session content
# that has been compressed out of the live context — acceptable for now since
# the conversation loop manages in-session compression. Set to 0 to restore
# the session-blind behaviour.
RECALL_EXCLUDE_CURRENT_SESSION = _envbool(
    "THOTH_RECALL_EXCLUDE_CURRENT_SESSION", default=True
)

# Precision controls. After ranking, a
# candidate is injected only if its composite score clears BOTH floors:
#   * an absolute floor (drops near-zero, loosely-related slices), and
#   * a relative floor = fraction of the top candidate's score (adapts
#     across the semantic vs keyword score regimes — the strongest hit
#     always survives, the long weak tail is dropped).
RECALL_MIN_RELEVANCE = _envfloat("THOTH_RECALL_MIN_RELEVANCE", default=0.05)
# Tightened 0.4 → 0.5 (2026-06-17): keep only candidates within half the top
# score, so the long weakly-related tail is dropped instead of padding the
# block to the token ceiling. See the salience-weight reduction below — the
# two together stop the projection echoing the live transcript.
RECALL_RELATIVE_FLOOR = _envfloat("THOTH_RECALL_RELATIVE_FLOOR", default=0.5)
# MMR-style diversity: skip a candidate whose payload token-overlap
# (Jaccard) with an already-selected block exceeds this — kills
# near-duplicate excerpts. 0 disables dedup.
RECALL_DEDUP_THRESHOLD = _envfloat("THOTH_RECALL_DEDUP_THRESHOLD", default=0.8)
# Inline a "· why: <score> <path>" provenance tag on each composed block.
# Default off → clean block; the recall log records provenance regardless.
RECALL_SHOW_PROVENANCE = _envbool("THOTH_RECALL_SHOW_PROVENANCE", default=False)

# Phase D: L1 entity header in the recall projection (spec §7). When on,
# the projection prepends a "## Known entities" block (top entities by
# query-relevance + salience) ahead of the L0 quotes. Independent of
# THOTH_SUBSTRATE_RECALL — the header only manifests when recall is also on.
RECALL_INCLUDE_L1 = _envbool("RECALL_INCLUDE_L1", default=True)
RECALL_L1_LIMIT = _envint("RECALL_L1_LIMIT", default=5)

# L3 pattern + L4 self-model headers in the recall projection (added
# 2026-06-17). The substrate distils L0 episodes into L3 patterns
# (generalisations / themes) and L4 self-model observations, but recall
# previously only ever surfaced L0 quotes + the L1 entity header — the
# higher-order abstractions were unreachable from the foreground. When on,
# the projection prepends a "## Patterns" block (top patterns by query
# trigram-relevance + salience) and a "## Self-model" block (top non-coherence
# observations by query relevance), sharing the token budget ahead of the L0
# quotes. Independent of THOTH_SUBSTRATE_RECALL — the headers only manifest
# when recall is also on. Limits are small by design: these are dense,
# high-value summaries, so a few go a long way.
RECALL_INCLUDE_L3 = _envbool("RECALL_INCLUDE_L3", default=True)
RECALL_L3_LIMIT = _envint("RECALL_L3_LIMIT", default=3)
RECALL_INCLUDE_L4 = _envbool("RECALL_INCLUDE_L4", default=True)
RECALL_L4_LIMIT = _envint("RECALL_L4_LIMIT", default=2)

# Skill suggestion in the recall projection. Opt-in (default
# OFF) so it never adds noise to the per-turn block unless wanted; the
# `thoth substrate skills <query>` CLI works regardless.
RECALL_SUGGEST_SKILLS = _envbool("RECALL_SUGGEST_SKILLS", default=False)
RECALL_SKILL_LIMIT = _envint("RECALL_SKILL_LIMIT", default=3)

# Composite-score weights (must keep sum of three active terms in a
# reasonable range; the active path is salience + recency + ONE of
# similarity/keyword).
#
# Rebalanced 2026-06-17 (was sim=0.3, kw=0.3, sal=0.5, rec=0.2). Live
# testing showed salience dominating topical relevance: high-salience
# `assistant_response` slices were out-ranking the user messages and tool
# results that actually matched the query, so recall echoed the agent's own
# recent turns. Lowering salience (0.5 → 0.35) and raising the topical terms
# (0.3 → 0.4) makes the query-match the primary signal while salience/recency
# break ties. NOTE: these are the *operational* defaults read by
# ``recall()`` via the kwargs in ``substrate/recall/api.py``; the
# module-level ``DEFAULT_*`` in ``substrate/recall/projection.py`` are the
# pure-function library fallbacks (used only by direct callers/tests) and are
# deliberately left at the original values.
RECALL_SIMILARITY_WEIGHT = _envfloat("THOTH_RECALL_SIMILARITY_WEIGHT", default=0.4)
RECALL_KEYWORD_WEIGHT = _envfloat("THOTH_RECALL_KEYWORD_WEIGHT", default=0.4)
RECALL_SALIENCE_WEIGHT = _envfloat("THOTH_RECALL_SALIENCE_WEIGHT", default=0.35)
RECALL_RECENCY_WEIGHT = _envfloat("THOTH_RECALL_RECENCY_WEIGHT", default=0.15)
RECALL_RECENCY_HALF_LIFE_HOURS = _envfloat(
    "THOTH_RECALL_RECENCY_HALF_LIFE_HOURS", default=12.0
)

# Anti-thrashing: per-slice reinforcement cap per minute (spec §5.4).
RECALL_REINFORCE_RATE_LIMIT_PER_MIN = _envint(
    "THOTH_RECALL_REINFORCE_RATE_LIMIT_PER_MIN", default=6
)

# Minimum topical relevance (similarity/keyword match to the query, in
# [0, 1]) a recalled slice must have before recall reinforces it. Slices
# that entered the projection on salience/recency alone — with relevance
# below this floor — get NO reinforcement and their decay clock is left
# alone, so they age out instead of ratcheting their salience and
# re-injecting every turn (the recall feedback loop). Above the floor,
# the bump is scaled by relevance. 0.0 restores the old reinforce-all
# behaviour.
#
# Raised 0.05 → 0.2 (2026-06-17): 0.05 was low enough that a slice sharing
# almost any vocabulary with the query still cleared the floor and got
# reinforced, so same-topic `assistant_response` slices kept ratcheting their
# salience turn after turn — the feedback loop was technically mitigated but
# the threshold didn't bite. 0.2 requires a genuine topical match before a
# recall reinforces a slice.
RECALL_REINFORCE_MIN_RELEVANCE = _envfloat(
    "THOTH_RECALL_REINFORCE_MIN_RELEVANCE", default=0.2
)

# Recall log writer.
RECALL_LOG_QUEUE_DEPTH = _envint("THOTH_RECALL_LOG_QUEUE_DEPTH", default=1024)

# Embedding pipeline.
#
# ``RECALL_EMBEDDING_MODEL`` is an *override* knob, not a fallback.
# When unset (the default), the Curator passes ``model=None`` to
# ``substrate.recall.embeddings.embed()`` so it reads
# ``auxiliary.embedding.model`` from config.yaml — keeping the Curator's
# choice in lock-step with whatever provider/model the operator wired
# in (e.g. Ollama's ``nomic-embed-text``, Voyage's ``voyage-3``).
#
# The earlier hardcoded ``"text-embedding-3-small"`` default was the
# 2026-05-26 production embedding-failure bug: installs running local
# Ollama had ``auxiliary.embedding.model = nomic-embed-text`` in
# config.yaml, but the Curator overrode it with ``text-embedding-3-small``
# at every embed() call. Ollama doesn't know that model name → embed()
# returned ``[None]*N`` for every batch → 100% of slices marked
# ``embedding_failed`` after retry exhaustion.
#
# Set ``THOTH_RECALL_EMBEDDING_MODEL`` to pin a specific model
# independent of the rest of the auxiliary.embedding config — useful for
# A/B comparison or running a recall-specific model on a shared cluster.
RECALL_EMBEDDING_MODEL = os.environ.get("THOTH_RECALL_EMBEDDING_MODEL")
RECALL_EMBEDDING_DIM = _envint("THOTH_RECALL_EMBEDDING_DIM", default=1536)
RECALL_EMBEDDING_TIMEOUT_MS = _envint(
    "THOTH_RECALL_EMBEDDING_TIMEOUT_MS", default=800
)
# Separate, much larger budget for BACKGROUND embedding (Curator backfill +
# ``thoth embed reshape``) than for interactive recall-query embedding. The
# 800ms query timeout keeps recall responsive, but it's far too short for a
# batch backfill against a slow local model (a CPU-hosted Qwen3-Embedding can
# take >10s/call) — every batch would time out, get marked failed, and stall
# coverage at 0%. Defaults to ``max(query_timeout, 30s)``; override directly
# for very slow providers.
RECALL_EMBEDDING_BACKFILL_TIMEOUT_MS = _envint(
    "THOTH_RECALL_EMBEDDING_BACKFILL_TIMEOUT_MS",
    default=max(RECALL_EMBEDDING_TIMEOUT_MS, 30_000),
)
RECALL_EMBEDDING_QUEUE_DEPTH = _envint(
    "THOTH_RECALL_EMBEDDING_QUEUE_DEPTH", default=4096
)
RECALL_EMBEDDING_BATCH_SIZE = _envint(
    "THOTH_RECALL_EMBEDDING_BATCH_SIZE", default=32
)
RECALL_EMBEDDING_BACKFILL_INTERVAL_S = _envfloat(
    "THOTH_RECALL_EMBEDDING_BACKFILL_INTERVAL_S", default=30.0
)
RECALL_EMBEDDING_BACKFILL_MAX_RETRIES = _envint(
    "THOTH_RECALL_EMBEDDING_BACKFILL_MAX_RETRIES", default=3
)
# Auto-heal cadence for slices parked as ``embedding_failed``. The Curator
# clears a small batch of parked slices this often so a fixed embedding config
# (dim mismatch resolved, endpoint reachable again) self-heals without an
# operator running ``thoth embed retry-failed``. Long by default — it only
# probes when the fresh backlog is empty, so a still-broken provider re-parks
# the batch and is left alone until the next interval (no hammering). Set to 0
# to disable auto-heal entirely.
RECALL_EMBEDDING_RETRY_FAILED_INTERVAL_S = _envfloat(
    "THOTH_RECALL_EMBEDDING_RETRY_FAILED_INTERVAL_S", default=1800.0
)

# ---------------------------------------------------------------------------
# Recall coherence-pin knobs. THOTH_-only — nothing depends on these yet, so
# no back-compat alias is needed. (The Conductor's own coherence thresholds —
# THOTH_CONDUCTOR_COHERENCE_FLOOR / _RECOVERY — are read inline in
# conductor_policy.py via its local _env_float, matching how it reads its
# CONDUCTOR_BACKLOG_* knobs; they intentionally do not live here.)
# ---------------------------------------------------------------------------

# Recall: pin (always include) the coherence signal in the projection.
RECALL_COHERENCE_PIN = _envbool("THOTH_RECALL_COHERENCE_PIN", default=True)
# Recall: cap the coherence floor applied when pinning.
RECALL_COHERENCE_FLOOR_MAX = _envfloat(
    "THOTH_RECALL_COHERENCE_FLOOR_MAX", default=0.5
)


# Master toggle for the SubstrateMemoryProvider's prefetch (spec §6.1).
# Default ON: this fork installs the substrate as the primary memory
# backend; recall driving the per-turn <memory-context> is the point.
# Set THOTH_SUBSTRATE_RECALL=0 to fall back to the upstream built-in
# provider exclusively (useful for A/B comparison or debugging).
THOTH_SUBSTRATE_RECALL_ENABLED = _envbool(
    "THOTH_SUBSTRATE_RECALL", default=True
)


# Default stream set for recall (spec §4.3). User-message + assistant-
# response streams; explicitly excludes self-state. ``stream_filter=None``
# in the recall API resolves to this list at call time.
DEFAULT_RECALL_STREAMS: tuple[str, ...] = (
    "thoth.world.user_message.cli",
    "thoth.world.user_message.telegram",
    "thoth.world.user_message.discord",
    "thoth.world.user_message.slack",
    "thoth.world.user_message.whatsapp",
    "thoth.world.user_message.signal",
    "thoth.world.user_message.acp",
    "thoth.self_action.assistant_response",
    # Retrospective summaries (the Summarizer): dense, carry older context
    # forward so recall surfaces a summary instead of the faded originals.
    "thoth.self_action.summary",
)


__all__ = [
    "SubstrateConfig",
    "RECALL_TOKEN_BUDGET",
    "RECALL_TIME_WINDOW_HOURS",
    "RECALL_TIMEOUT_MS",
    "RECALL_MIN_SALIENCE",
    "RECALL_CANDIDATE_LIMIT",
    "RECALL_EXCLUDE_CURRENT_SESSION",
    "RECALL_MIN_RELEVANCE",
    "RECALL_RELATIVE_FLOOR",
    "RECALL_DEDUP_THRESHOLD",
    "RECALL_SHOW_PROVENANCE",
    "RECALL_INCLUDE_L1",
    "RECALL_L1_LIMIT",
    "RECALL_INCLUDE_L3",
    "RECALL_L3_LIMIT",
    "RECALL_INCLUDE_L4",
    "RECALL_L4_LIMIT",
    "RECALL_SUGGEST_SKILLS",
    "RECALL_SKILL_LIMIT",
    "RECALL_SIMILARITY_WEIGHT",
    "RECALL_KEYWORD_WEIGHT",
    "RECALL_SALIENCE_WEIGHT",
    "RECALL_RECENCY_WEIGHT",
    "RECALL_RECENCY_HALF_LIFE_HOURS",
    "RECALL_REINFORCE_RATE_LIMIT_PER_MIN",
    "RECALL_REINFORCE_MIN_RELEVANCE",
    "RECALL_LOG_QUEUE_DEPTH",
    "RECALL_EMBEDDING_MODEL",
    "RECALL_EMBEDDING_DIM",
    "RECALL_EMBEDDING_TIMEOUT_MS",
    "RECALL_EMBEDDING_BACKFILL_TIMEOUT_MS",
    "RECALL_EMBEDDING_QUEUE_DEPTH",
    "RECALL_EMBEDDING_BATCH_SIZE",
    "RECALL_EMBEDDING_BACKFILL_INTERVAL_S",
    "RECALL_EMBEDDING_BACKFILL_MAX_RETRIES",
    "RECALL_EMBEDDING_RETRY_FAILED_INTERVAL_S",
    "RECALL_COHERENCE_PIN",
    "RECALL_COHERENCE_FLOOR_MAX",
    "THOTH_SUBSTRATE_RECALL_ENABLED",
    "DEFAULT_RECALL_STREAMS",
]
