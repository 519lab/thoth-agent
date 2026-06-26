# The cognitive substrate

This document explains **what the substrate is, how it is structured, and why
it is built the way it is.** It is the design/architecture companion to two
other resources:

- **`skills/substrate/SKILL.md`** (load in a session with `/substrate`) — the
  *operator* playbook: inspect commands, tuning knobs, troubleshooting. Read
  that when you want to *run* the substrate; read this when you want to
  *understand* it.
- The README **"Cognitive substrate"** section — the one-screen overview.

The original design rationale lives in a separate, private MVS ("minimum viable
substrate") spec — its numbered sections are the `§x.y` references scattered
through the substrate code. This file consolidates the parts of that rationale a
contributor working in *this* repo needs, so the "why" is captured in-tree.

---

## 1. Why it exists

Upstream Hermes is a stateless chat loop with a bolt-on persistent-memory
feature. The substrate replaces "remember a few facts" with a continuous
**perception → curation → recall** pipeline modeled loosely on how a mind keeps
its own memory: everything the agent perceives is recorded, most of it fades,
the salient parts consolidate into structured knowledge, and a budgeted recall
step surfaces what is relevant to the current turn.

Three design goals shape every decision below:

1. **Additive and non-fatal.** The substrate sits *beneath* the conversation
   loop and must never break it. Every slice write, every sub-agent, every
   recall call is allowed to fail silently; the conversation continues on the
   upstream code path. This is why recall is env-gated off by default
   (`THOTH_SUBSTRATE_RECALL=0`) — until an operator opts in, behavior is
   byte-identical to upstream.
2. **Single source of truth in PostgreSQL.** There is no SQLite anywhere in
   this fork. Transcripts, kanban state, and substrate perception all live in
   one PG 17 instance (with the `vector`, `pg_trgm`, and `pgcrypto`
   extensions). See [`database-event-loop.md`](./database-event-loop.md) for
   the single-DB-loop invariant that all substrate code must obey.
3. **Phased, observable rollout.** The substrate ships in phases (A–G, plus the
   self-improvement Forge). Later phases land as background sub-agents that are
   gated, heartbeated, and individually disableable, so an unfinished or
   misbehaving layer can be switched off without touching the rest.

---

## 2. The perception model: streams, slices, families

The substrate's atom is the **slice** — one perceived event. Slices arrive on
named **streams**, and every stream belongs to a **family** that records *where
the perception originated* (`substrate/storage/types.py`):

| Family | Meaning | Examples |
|---|---|---|
| `exteroceptive` | Signals from outside the agent | user messages (CLI, Telegram, Discord, …), sensor reads |
| `self_action` | The agent's own outbound actions | assistant responses, tool calls/results, sub-agent spawn/return |
| `self_state` | The agent's own internal state changes | session lifecycle, cron dispatch, and every sub-agent decision |

This world/self split is the substrate's version of the
exteroception / proprioception distinction — the system perceives *the world*
and *itself* on the same substrate, which is what later lets it reason about its
own memory (the Curator's release decisions are themselves `self_state` slices).

**15 streams** are auto-registered at boot (`Substrate.boot()`, the table in
`substrate/facade.py`): the user-message sources, the self-action streams, and
the lifecycle/state streams. The `substrate.self_state` stream — where the
substrate audits its own behaviour — is seeded by the schema migration rather
than the boot table.

Each slice carries (`substrate/storage/types.py`, `substrate/l0/`):

- a **`payload`** + **`payload_modality`** (`text`, `structured_event`,
  `binary_blob`, `signal`) — the perceived content;
- a **`sentinel_state`** (`pending → passed | quarantined`) — the triage gate;
- a **`consolidation_state`** (`unconsolidated → partial → consolidated →
  released`) — how far its meaning has been distilled upward;
- a **`salience_score`** the Curator decays over time;
- an **`embedding`** (1536-d pgvector, `text-embedding-3-small`) backfilled
  asynchronously by the Curator.

### Writing perception (L0)

`substrate.l0.api.commit_slice` (async) and `commit_slice_sync` (the
`thoth_db.run_sync` bridge for cron/CLI) are the **only** public write
surface. Thoth call sites emit perception through
`substrate/events/thoth_hooks.py`, which `Substrate.boot()` binds so the
conversation loop, gateway, and ACP server can record without importing
substrate internals. Read endpoints (the Sentinel batch tick, the force-reject
sweep) are deliberately *not* exported from L0 — they are internal machinery.

---

## 3. The layer stack (L0–L4)

The substrate is a stack of layers, each a thin structured abstraction over the
one below, each citing its source so meaning can survive the raw event being
released:

| Layer | Name | Produced by | Contents |
|---|---|---|---|
| **L0** | Perception | `commit_slice` (foreground) | Raw slices — every event, as perceived |
| **L1** | Entities & relationships | Parser | Entities + typed relationships, each *citing* the L0 slice it came from |
| **L2** | Associative graph | Associator | Weighted, typed associations between L1 entities (discovered co-occurrence / shared-neighbour structure), with an append-only edit history |
| **L3** | Patterns & abstractions | Pattern-finder, Reflector | Generalizations, recurring themes/structures across many L1 extractions |
| **L4** | Self-model & calibration | Critic | What the mind knows about its own knowing — per-sub-agent calibration + a cross-layer **coherence** score treated as a monitored vital sign |

The crucial mechanic is the **consolidation handshake** (`substrate/l1/`,
design §5.7): the Parser distils a `passed` L0 slice into L1 entities, and only
once its meaning is safely re-homed upward does the Curator become free to
release the raw slice. Memory is not deleted so much as *promoted* — the event
fades while its distilled meaning persists at a higher layer. Layers depend
bottom-up (Parser feeds everyone), so on a fresh install L1+ fills only after
the Parser has consolidated some L0.

---

## 4. The sub-agents

Background work runs as asyncio tasks. Two tiers:

### Core workers (Phase A–C, always on)

Spawned directly by `Substrate.boot()` inside the Thoth process:

| Sub-agent | Tick | Job |
|---|---|---|
| **Sentinel** | 200 ms | Triages `pending` slices → `passed` / `quarantined`. Phase A passes everything; content defense (prompt-injection / poisoning) is staged behind `THOTH_SUBSTRATE_SENTINEL_DEFENSE`, default OFF. |
| **Curator** | configurable | The decay + release + embedding loop (see §5). |
| **Force-reject** | 10 s | Drops `pending` slices past their decay-profile TTL — bounds the pending queue even if the Sentinel hangs. |
| **Partition-maintenance** | 24 h | Keeps a rolling window of monthly partitions ahead of `now()`. Calendar-bound, not load-bound. |

### Cognitive sub-agents (Phases D–G)

These build L1–L4 and run in a separate **worker subprocess**
(`thoth substrate worker run`, `substrate/cli/worker.py`). They are **ON by
default** but each is individually gated; setting its env var to `0` lets it
register + heartbeat but skips the work. LLM-driven agents no-op silently when
no auxiliary model is configured.

| Env var (default `1`) | Sub-agent | Produces |
|---|---|---|
| `THOTH_SUBSTRATE_PARSER` | Parser | L1 entities/relationships |
| `THOTH_SUBSTRATE_ASSOCIATOR` | Associator | L2 associations |
| `THOTH_SUBSTRATE_PATTERNFINDER` | Pattern-finder | L3 patterns |
| `THOTH_SUBSTRATE_CRITIC` | Critic | L4 calibration + coherence |
| `THOTH_SUBSTRATE_REFLECTOR` | Reflector | L3/L4 synthesis |
| `THOTH_SUBSTRATE_DREAMER` | Dreamer | counterfactual exploration log |
| `THOTH_SUBSTRATE_CONDUCTOR` | Conductor | adaptive intensity dialing |
| `THOTH_SUBSTRATE_SUMMARIZER` | Summarizer | compress older context |

The **Conductor** owns the intensity dial (`OFF | LOW | MEDIUM | FULL`) that
throttles how hard the other agents work. It has two halves:

- `StubConductor` (`substrate/agents/conductor.py`) — the **registry**: holds
  each agent's level and pushes changes to the running agents (enforcing each
  agent's floor, e.g. Sentinel stays FULL, Curator stays ≥ LOW).
- `AdaptiveConductor` (`substrate/agents/conductor_policy.py`, Phase F) — the
  **policy** that actually drives the registry. It is spawned at boot
  (`substrate/facade.py`) and ticks on observable load: when the consolidation
  backlog is high it raises the Parser and pauses the enrichment agents
  (Associator / Pattern-finder) so scarce cycles go to parsing; when the backlog
  drains, everyone returns to baseline LOW. It also reacts to the Critic's
  coherence vital: when coherence drops below `THOTH_CONDUCTOR_COHERENCE_FLOOR`
  it re-prioritizes corrective work (raising the Parser/integrity agents),
  holding that posture until coherence recovers past
  `THOTH_CONDUCTOR_COHERENCE_RECOVERY` (hysteresis). Gated by
  `THOTH_SUBSTRATE_CONDUCTOR` (default on; `0` restores the static
  earlier-phase behaviour).

What it does **not** yet do (deferred research, flagged in the Phase F PR): the
*learned* executive — opportunity forecasting, intensity-policy learning,
worklist scheduling, wake anticipation. And note the dimensions it steers on:
**consolidation backlog and coherence, but not auxiliary-model cost/latency.**
See §8 for what that implies.

Every sub-agent writes its decisions as `self_state` slices, so any decision can
be replayed after the fact (`thoth substrate <agent> recent`).

---

## 5. The memory lifecycle: decay, consolidation, release

Memory management is the Curator's continuous loop, parameterised by **decay
profiles** (`substrate_decay_profiles`, 4 seeded; `substrate/storage/decay_profiles.py`):

- **`natural_half_life`** — slices' `salience_score` fades exponentially.
- **`consolidation_window`** — how long meaning has to be promoted upward.
- **`pending_ttl`** — the force-reject deadline for un-triaged slices.
- **`tombstone_policy`** (`thin` / `full` / `none`) — what trace a released
  slice leaves behind.

When a slice's salience falls below the profile's retention threshold *and* its
meaning has consolidated upward (the §3 handshake), the Curator **releases** it
per the tombstone policy. Operators can override the machine: `pin` a slice so
it is never decayed or released (and is boosted in recall), or `forget` it to
drop its salience to 0 for release on the next cycle.

This is the throttle on storage growth: a busy session emits hundreds of slices,
and the Curator — not a hard cap — is what keeps the table bounded.

---

## 6. The recall pipeline

Recall is how the substrate feeds the foreground. When
`THOTH_SUBSTRATE_RECALL=1`, the `SubstrateMemoryProvider` composes each turn's
`<memory-context>` block from substrate slices instead of (on top of) the
upstream memory path. The per-turn pipeline (`substrate/recall/`,
timeout-bounded, default 300 ms) is:

1. **`embed_query`** — embed the current turn (optional, separate ~800 ms budget).
2. **`recall_window`** — SQL ranking over a composite score combining **pgvector
   cosine similarity + keyword Jaccard + current salience + recency** (each
   weighted; see the `THOTH_RECALL_*_WEIGHT` knobs), within a lookback window.
   When the Critic's coherence is low, recall **pins to coherence** — raising the
   relevance floor so only stronger candidates surface (gated by
   `THOTH_RECALL_COHERENCE_PIN`).
3. **`rank_candidates`** — pure ranking.
4. **`compose_projection`** — a pure, **token-budgeted greedy composer**
   (`substrate/recall/composer.py`, default 1500 tokens): adds candidates until
   the next one would exceed budget; a single oversized first candidate is
   truncated at the last newline and marked `[truncated]`.
5. **`reinforce_hits`** — fire-and-forget salience bump for surfaced slices,
   per-slice rate-limited (default 6/min) to prevent thrash.
6. **`log_recall`** — appends one audit row to `substrate_recall_log`.

Two properties make this safe to enable:

- **Failures never reach the caller.** Recall always returns a projection —
  possibly empty with an `empty_reason` (`timeout`, `no_candidates`,
  `token_budget_exhausted`, `error`).
- **Embeddings are an optimization, not a gate.** Recall against not-yet-embedded
  slices falls back to keyword Jaccard, so coverage climbing toward 100% improves
  precision but is never required for correctness.

Every call is auditable: `substrate_recall_log` records the query, candidate
count, returned count, latency, and empty-reason for each turn.

---

## 7. Storage & schema

- **`substrate_streams`** — the registered streams (name, family, modality,
  source, organ, decay_profile_id, lifecycle_state).
- **`substrate_slices`** — every event, **RANGE-partitioned monthly** on
  `ingest_time_world`. Partition-maintenance carves new partitions ahead of
  `now()`; slices landing in the DEFAULT partition are the symptom of a stalled
  maintenance tick.
- **`substrate_decay_profiles`** — the 4 seeded profiles (§5).
- **`substrate_recall_log`** — append-only recall audit (§6).
- **L1–L4 tables** — entities/relationships, associations (+ edit history),
  patterns, observations.

Canonical DDL lives in the Alembic migrations under `migrations/versions/`
(e.g. `…_substrate_skeleton.py`, `…_substrate_recall_log.py`,
`…_substrate_slices_embedding.py`). The schema migration is permanent — back up
the DB before the first run if the data matters. If the DB is behind the
expected revision at boot, the substrate raises with the upgrade command unless
`THOTH_AUTO_MIGRATE=1`.

---

## 8. Maturity & known limitations

The substrate is deployed and useful, but several pieces are intentionally
partial. If you are evaluating or extending it, these are the spots that "look
finished but aren't":

- **The full cognitive crew runs by default; you can now *see* its cost, but
  nothing throttles it on cost.** All eight cognitive sub-agents (§4) default to
  ON. They no-op when no auxiliary model is configured — but the moment one is,
  the whole crew runs, which is a real auxiliary-model **cost and latency
  surface**. Token usage is now observable: per-call usage is recorded to
  `substrate_agent_cost` and surfaced in `thoth substrate health`, so you can
  attribute spend per sub-agent rather than guess. What remains deferred is
  *acting* on it — there are no budgets, throttles, or a cost-aware governor; the
  Conductor still steers on consolidation backlog and coherence, not on token
  spend or wall-clock. The controls that exist are blunt: don't run the worker
  subprocess, set per-agent `THOTH_SUBSTRATE_*=0`, or hold the Conductor/agents
  at a lower intensity. Watch the per-agent cost and budget accordingly before
  pointing the crew at a paid endpoint.

- **The Conductor is real but shallow.** The deterministic backlog policy has
  landed (§4) — this is no longer a stub. But it reacts only to consolidation
  pressure; the *learned* executive (forecasting, policy learning, wake
  anticipation) is deferred research. Treat the Conductor as a load balancer,
  not yet an optimizer.

- **Coherence is now acted on — by two consumers, with the deeper signal still
  deferred.** The Critic emits a cross-layer coherence score and exposes
  `latest_coherence` (`substrate/l4/store.py`), surfaced in
  `thoth substrate health`. It is no longer observability-only: the Conductor
  re-prioritizes corrective work when coherence drops below
  `THOTH_CONDUCTOR_COHERENCE_FLOOR` and holds that posture until it recovers past
  `THOTH_CONDUCTOR_COHERENCE_RECOVERY` (hysteresis, §4), and recall **pins to
  coherence** — raising the relevance floor when coherence is low, gated by
  `THOTH_RECALL_COHERENCE_PIN` (§6). What remains deferred is the *grounding* of
  the signal itself: the richer L2-grounding coherence measure is still
  outstanding, so today's control inputs ride the current scalar
  (`substrate/agents/critic.py`).

- **Boot-time config is a sharp edge.** Because config is read once at boot
  (§9), `THOTH_SUBSTRATE_RECALL` and the other `THOTH_*` knobs are effectively
  irreversible mid-process — flipping recall on or off, or retuning weights,
  requires a restart. This is by design (hot-path constants) but routinely
  surprises operators.

None of these block the foreground — they are limits on how *autonomous* and
*self-governing* the substrate is, not on its safety. The §9 invariants are what
keep it safe regardless.

---

## 9. Design invariants for contributors

If you touch substrate code, these are the rules that keep it correct:

- **Never block or break the foreground.** Slice writes and recall are
  best-effort; swallow and log, don't propagate. Substrate exceptions at boot
  are logged but do not abort Thoth.
- **Obey the single DB loop.** All PG access goes through `thoth_db` per
  [`database-event-loop.md`](./database-event-loop.md). The worker subprocess
  runs its own loop and calls `reset_pool_for_new_loop()`.
- **Config is read once at boot.** `substrate/config.py` reads `THOTH_*` env
  vars at import time (module-level constants for the hot paths, a frozen
  `SubstrateConfig` for the rest). Mutating them mid-process is unsupported —
  set in `.env` and restart. (Consequently `THOTH_SUBSTRATE_RECALL` is
  effectively irreversible mid-process.)
- **Gate new layers.** Anything beyond the Phase A–C core ships behind its own
  `THOTH_SUBSTRATE_*` toggle, registers + heartbeats regardless of the gate,
  and degrades to a no-op when its dependency (auxiliary model, lower layer) is
  absent.
- **Self-improvement stays behind a human gate.** The SkillScout (Tier 1,
  default OFF) can *draft and propose* a skill from high-salience upper-layer
  needs, but never installs one — approval flows through the `skill_proposal`
  tool and the normal install-time security scan. See
  [`../plans/2026-05-28-substrate-self-improvement-forge.md`](../plans/2026-05-28-substrate-self-improvement-forge.md).

---

## 10. Where to go next

- **Operate it:** `skills/substrate/SKILL.md` / `/substrate` — inspect, tune,
  troubleshoot.
- **DB discipline:** [`database-event-loop.md`](./database-event-loop.md).
- **Self-improvement Forge:** [`../plans/2026-05-28-substrate-self-improvement-forge.md`](../plans/2026-05-28-substrate-self-improvement-forge.md).
- **Code:** `substrate/` — `l0`–`l4` (layers), `agents/` (sub-agents),
  `recall/` (the recall pipeline), `storage/` (repos + types), `facade.py`
  (boot/lifecycle), `config.py` (knobs).
