---
sidebar_position: 5
title: "Cognitive Memory Substrate"
description: "Thoth's continuous perception → curation → recall memory layer — what it is, how it works, and how to turn it on."
---

# Cognitive Memory Substrate

Most agents forget everything the moment a conversation ends. Thoth's **substrate** is a real memory layer that runs *beneath* the conversation: it records what the agent perceives, lets most of it fade, consolidates the salient parts into structured knowledge, and surfaces what's relevant on each turn. It's modeled loosely on how a mind keeps its own memory — a continuous **perception → curation → recall** pipeline rather than a "remember a few facts" bolt-on.

The substrate is **additive and non-fatal**: it sits below the conversation loop and is allowed to fail silently. Recall is **off by default** — until you opt in, Thoth behaves exactly as it would without the substrate.

:::info Requirements
The substrate stores everything in **one PostgreSQL 17 instance** with the `vector` (pgvector), `pg_trgm`, and `pgcrypto` extensions — the same database that holds transcripts and kanban state. There is no SQLite. A standard Docker-Compose install already provisions this.
:::

---

## The mental model

### Slices, streams, families

The atom of the substrate is the **slice** — one perceived event. Slices arrive on named **streams**, and every stream belongs to a **family** recording *where* the perception came from:

| Family | Meaning | Examples |
|---|---|---|
| `exteroceptive` | Signals from outside the agent | user messages (CLI, Telegram, Discord…), sensor reads |
| `self_action` | The agent's own outbound actions | assistant responses, tool calls/results, sub-agent spawn/return |
| `self_state` | The agent's own internal state changes | session lifecycle, cron dispatch, every sub-agent decision |

This world/self split is what later lets the system reason about *itself* — its own maintenance decisions are recorded as `self_state` slices, so any decision can be replayed after the fact. Fifteen streams are auto-registered at boot.

Each slice carries a payload, a triage state (`pending → passed | quarantined`), a consolidation state (`unconsolidated → … → released`), a **salience score** that decays over time, and a 1536-dimension embedding backfilled asynchronously.

### The layer stack (L0–L4)

Meaning is distilled upward through five layers, each citing its source so it survives the raw event being released:

| Layer | Name | Contents |
|---|---|---|
| **L0** | Perception | Raw slices — every event, as perceived |
| **L1** | Entities & relationships | Entities + typed relationships, each citing the L0 slice they came from |
| **L2** | Associative graph | Weighted associations between L1 entities (co-occurrence / shared-neighbour structure) |
| **L3** | Patterns & abstractions | Generalizations and recurring themes across many extractions |
| **L4** | Self-model & calibration | What the agent knows about its own knowing — per-agent calibration + a cross-layer **coherence** score |

The key mechanic is the **consolidation handshake**: a raw L0 slice is only released *after* its meaning has been safely promoted to L1+. Memory isn't so much deleted as **promoted** — the event fades while its distilled meaning persists higher up. On a fresh install the upper layers fill in only once the agent has done enough work to consolidate.

---

## The background crew

Maintenance and cognition run as background workers, each gated and individually disableable.

**Core workers** (always on, in-process):

- **Sentinel** (200 ms tick) — triages new slices `pending → passed`. Content defense (prompt-injection / poisoning screening) is staged behind a flag, off by default.
- **Curator** — the decay + release + embedding loop (see below).
- **Force-reject** (10 s) — drops un-triaged slices past their TTL so the queue stays bounded.
- **Partition-maintenance** (24 h) — keeps monthly table partitions ahead of `now()`.

**Cognitive sub-agents** (on by default, each gated by a `THOTH_SUBSTRATE_*` env var) run in a separate **worker subprocess** and build the upper layers. They **no-op silently when no auxiliary model is configured**, so they cost nothing until you wire up a model:

| Env var (default `1`) | Sub-agent | Produces |
|---|---|---|
| `THOTH_SUBSTRATE_PARSER` | Parser | L1 entities / relationships |
| `THOTH_SUBSTRATE_ASSOCIATOR` | Associator | L2 associations |
| `THOTH_SUBSTRATE_PATTERNFINDER` | Pattern-finder | L3 patterns |
| `THOTH_SUBSTRATE_CRITIC` | Critic | L4 calibration + coherence |
| `THOTH_SUBSTRATE_REFLECTOR` | Reflector | L3/L4 synthesis |
| `THOTH_SUBSTRATE_DREAMER` | Dreamer | counterfactual exploration log |
| `THOTH_SUBSTRATE_CONDUCTOR` | Conductor | adaptive intensity dialing |
| `THOTH_SUBSTRATE_SUMMARIZER` | Summarizer | compresses older context |

The **Conductor** is a load balancer for the crew: it dials each agent's intensity (`OFF → LOW → MEDIUM → FULL`) based on observable load. When the consolidation backlog is high it raises the Parser and pauses the enrichment agents so scarce cycles go to parsing; when the backlog drains, everyone returns to baseline. It also reacts to the Critic's **coherence** vital, re-prioritizing corrective work when coherence drops (with hysteresis so it doesn't flap).

---

## The memory lifecycle

The **Curator** runs a continuous decay loop parameterised by **decay profiles** (natural half-life, consolidation window, pending TTL, tombstone policy). A slice's salience fades exponentially; when it falls below the retention threshold **and** its meaning has consolidated upward, the Curator **releases** it. This — not a hard size cap — is what keeps storage bounded under a busy session.

You can override the machine:

- **`pin`** a slice so it's never decayed or released (and is boosted in recall).
- **`forget`** a slice to drop its salience to zero for release on the next cycle.

---

## Recall: how it feeds a turn

When recall is enabled, the substrate composes each turn's `<memory-context>` from substrate slices. The per-turn pipeline is timeout-bounded (default 300 ms) and roughly:

1. **Embed the query** (optional, separate budget).
2. **Rank a recall window** by a composite score: **pgvector cosine similarity + keyword overlap + current salience + recency**, each weighted by a `THOTH_RECALL_*_WEIGHT` knob. When coherence is low, recall raises its relevance floor so only stronger candidates surface.
3. **Compose a token-budgeted projection** (default 1500 tokens) — a greedy composer that adds candidates until the budget is hit.
4. **Reinforce hits** — a small, rate-limited salience bump for surfaced slices.
5. **Log** one audit row to `substrate_recall_log`.

Two properties make this safe to turn on:

- **Failures never reach the conversation.** Recall always returns a projection — possibly empty, with an `empty_reason` (`timeout`, `no_candidates`, `token_budget_exhausted`, `error`).
- **Embeddings are an optimization, not a gate.** Recall against not-yet-embedded slices falls back to keyword matching, so it works immediately and only sharpens as embedding coverage climbs.

---

## Turning it on

The substrate **perceives** out of the box (slices are recorded), but two things are opt-in: **recall** into the conversation, and the **cognitive worker** that builds L1–L4.

```bash
# 1. Enable recall (off by default). Set in your .env and restart —
#    substrate config is read once at boot.
echo "THOTH_SUBSTRATE_RECALL=1" >> ~/.thoth/.env

# 2. Run the cognitive worker subprocess (builds L1–L4 in the background).
thoth substrate worker run

# 3. Inspect what it's doing.
thoth substrate health            # layer counts, coherence, per-agent cost
thoth substrate parser recent     # replay any sub-agent's recent decisions
```

For the full operator playbook — inspect commands, tuning knobs, and troubleshooting — load the bundled **substrate skill** in a session with `/substrate`.

:::caution Config is read once at boot
`THOTH_SUBSTRATE_RECALL` and the other `THOTH_*` substrate knobs are read at startup and are effectively fixed for the life of the process. To flip recall on/off or retune weights, edit `.env` and **restart**.
:::

---

## Cost & limitations

The substrate is deployed and useful, but a few things are intentionally partial — worth knowing before you point it at a paid model:

- **The cognitive crew has a real cost surface.** All eight cognitive sub-agents default to ON. They no-op with no auxiliary model — but the moment one is configured, the whole crew runs against it. Per-call token usage is recorded to `substrate_agent_cost` and shown in `thoth substrate health`, so you can attribute spend per sub-agent. What *doesn't* exist yet is a cost governor: nothing throttles the crew on token spend (the Conductor steers on backlog and coherence, not cost). The blunt controls are: don't run the worker, set per-agent `THOTH_SUBSTRATE_*=0`, or hold the crew at a lower intensity. **Watch the per-agent cost and budget accordingly.**
- **The Conductor is a load balancer, not yet an optimizer.** It reacts to consolidation pressure and coherence; the *learned* executive (forecasting, policy learning) is deferred.
- **Coherence is acted on, but the signal is still a scalar.** The Critic's coherence score drives the Conductor and recall's relevance floor today; a richer, L2-grounded measure is still outstanding.

None of these affect the conversation — they're limits on how *autonomous* the substrate is, not on its safety. Slice writes and recall are always best-effort and never block or break a turn.

---

## See also

- **Operate it:** the bundled [substrate skill](/docs/user-guide/skills/bundled/substrate/substrate-substrate) (`/substrate`) — inspect, tune, troubleshoot.
- **[Persistent Memory](/docs/user-guide/features/memory)** — the simpler MEMORY.md / session-search layer the substrate complements.
- **[Memory Providers](/docs/user-guide/features/memory-providers)** — external memory backends (Honcho, Mem0, …).
- **Architecture deep-dive:** `docs/architecture/substrate.md` in the repository, for the full design rationale and contributor invariants.
