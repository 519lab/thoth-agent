---
title: "Substrate — Inspect, tune, and troubleshoot the cognitive substrate"
sidebar_label: "Substrate"
description: "Inspect, tune, and troubleshoot the cognitive substrate"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Substrate

Inspect, tune, and troubleshoot the cognitive substrate.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/substrate` |
| Version | `0.1.0` |
| Author | Thoth Substrate Edition (ggrace519) |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `substrate`, `memory`, `perception`, `recall`, `postgres`, `operator` |
| Related skills | [`thoth-agent`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-thoth-agent) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Thoth loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Cognitive Substrate Skill

Operator and power-user playbook for the substrate layer — the PostgreSQL-backed
perception/memory infrastructure that sits underneath the conversation loop in
this fork of Thoth (`519lab/thoth-agent`, the Substrate Edition). Use this
when you want to see what the agent is perceiving, why recall returned (or did
not return) a given slice, whether the background sub-agents are keeping up,
or what to do when substrate boot fails.

This skill does NOT cover design rationale. For the architecture — the L0–L4
layer stack, the sub-agents, the memory lifecycle, and the recall pipeline — see
`docs/architecture/substrate.md`. Here we stay procedural: commands, queries,
common workflows, troubleshooting.

## When to Use

- "Show me what was perceived in this session" / "what did the agent see?"
- "Why didn't recall surface X?" or "the memory-context block looks empty"
- "Are embeddings backfilling fast enough?" / "what is recall coverage?"
- "The substrate didn't boot — figure out why"
- "Tune the recall token budget / similarity weights / decay half-life"
- "Pause / resume a sub-agent" or "the curator is too aggressive"

If the user is asking about persistent memory (`thoth memory`), Honcho, the
skills registry, or session search FTS — those are upstream Thoth features
covered by the `thoth-agent` skill, not this one. The substrate is additive
infrastructure beneath them.

## Prerequisites

- `THOTH_PG_DSN` set and pointing at a PG 17+ instance with the `vector`,
  `pg_trgm`, and `pgcrypto` extensions. Verify with `thoth doctor`.
- Alembic at head (`uv run alembic -c migrations/alembic.ini current` should
  match the latest revision under `migrations/versions/`). If behind, run
  `uv run alembic -c migrations/alembic.ini upgrade head` or set
  `THOTH_AUTO_MIGRATE=1` so the substrate boot upgrades on first run.
- For embeddings / recall coverage: an OpenAI-compatible API key for
  `text-embedding-3-small` (set via the upstream auxiliary-client config —
  `thoth config set auxiliary.embedding.provider openai`).

## What's Running

Four sub-agents run as asyncio tasks in the Thoth process. The Conductor is
a data holder (Phase A stub — no policy yet); the others tick on their own
cadence. Each writes audit slices to `substrate.self_state` so you can replay
their decisions after the fact.

| Sub-agent | Tick | Job | Intensity floor |
|-----------|------|-----|-----------------|
| **Sentinel** | 200 ms | Polls pending slices, decides pass / quarantine. Phase A passes everything; real defense (prompt-injection, content poisoning) lands later. | FULL |
| **Curator** | configurable | Continuous decay + release. Slices fade per their decay-profile half-life; below threshold they release per the profile's tombstone policy (thin / full / none). Phase C: also backfills `text-embedding-3-small` 1536-d embeddings for unembedded passed slices. | LOW |
| **Force-reject** | 10 s | Drops pending slices past their decay-profile TTL. Bounds the pending queue even if Sentinel hangs. | LOW |
| **Partition-maintenance** | 24 h | Keeps a rolling window of 3 monthly partitions ahead of `now()` on `substrate_slices`. Calendar-bound, not load-bound. | FULL |

**Intensity dial** lives in the Conductor (`substrate.agents.conductor`). It's
a get/set holder in Phase A; Phase F adds the real policy. The Curator
respects its level (`OFF | LOW | MEDIUM | FULL`) and demotes anything between
OFF and LOW to LOW.

## Streams and Slices

A **stream** is a named source of perception. A **slice** is one event on that
stream. The 15 auto-registered streams cover every user message, every
assistant response, every tool call/result, every sub-agent spawn/return,
session lifecycle, and cron dispatch. Substrate self-state lands on
`substrate.self_state` (seeded by the migration). Every slice has:

- A `sentinel_state` (`pending` → `passed` | `quarantined`)
- A `consolidation_state` (`unconsolidated` | `released`)
- A `salience_score` the Curator decays over time
- A `payload` (and `payload_modality`) — the actual perceived content
- An `embedding` (1536-d pgvector) — populated by Curator backfill once
  `text-embedding-3-small` returns successfully

## Inspect Commands

The `thoth substrate` tree is read-only and connects to the
configured PG via the existing pool — safe to run against a live Thoth
process from another shell.

```bash
# default summary — streams, slice totals by state, pending queue, sub-agent list
thoth substrate

# all streams + per-stream slice counts
thoth substrate streams

# the most-recent N slices on a single stream
thoth substrate slices --stream thoth.world.user_message.cli --limit 20

# pending queue depth + oldest pending age
thoth substrate pending

# the 4 seeded decay profiles + their half-lives, TTLs, tombstone policies
thoth substrate profiles
```

### Curator (Phase B)

```bash
thoth substrate curator              # release / pending consolidation / recent emissions
thoth substrate curator histogram    # per-profile 10-bucket salience histogram
thoth substrate curator recent --limit 20   # last N curator.* self-state emissions
thoth substrate curator pressure     # per-stream density + 5m update rate
```

### Recall (Phase C)

```bash
thoth substrate recall               # last-hour call stats + embedding coverage
thoth substrate recall recent --limit 20
thoth substrate recall sample --session-id <thoth-session-id>
thoth substrate recall config        # current RECALL_* knobs
```

## Tables

All substrate state is in PostgreSQL — there is no SQLite anywhere in this
fork. Useful tables to know:

| Table | Purpose |
|-------|---------|
| `substrate_streams` | The 15 registered streams (name, family, modality, source, organ, decay_profile_id, lifecycle_state). |
| `substrate_slices` | Every perceived event (RANGE-partitioned monthly on `ingest_time_world`). Holds payload, salience, sentinel state, consolidation state, embedding. |
| `substrate_decay_profiles` | 4 seeded profiles (default text, default structured, etc.) defining `natural_half_life`, `consolidation_window`, `pending_ttl`, `tombstone_policy`. |
| `substrate_recall_log` | One row per `recall()` call — query, candidate count, returned count, latency, empty_reason. Append-only audit. |

The migration files at `migrations/versions/20260523_0003_substrate_skeleton.py`,
`20260525_0005_substrate_recall_log.py`, and `20260525_0006_substrate_slices_embedding.py`
define the canonical schema.

## Recall: Enabling and Validating

Recall ships **disabled by default** (`THOTH_SUBSTRATE_RECALL=0`). The
`SubstrateMemoryProvider` still registers — exercising the registration path
in CI — but its `prefetch()` returns `""` so the foreground's
`<memory-context>` block continues to come from the upstream built-in path.
This makes user-facing behavior byte-identical to upstream until the operator
opts in.

**To turn on:**

```bash
# .env or shell
export THOTH_SUBSTRATE_RECALL=1
# Restart the agent — the env var is read once at provider construction.
```

When enabled, the per-turn pipeline runs (timeout-bounded, default 300 ms):

1. `embed_query` (optional, separate 800 ms budget)
2. `recall_window` SQL — composite-score ranking over pgvector similarity,
   keyword Jaccard, salience, recency
3. `rank_candidates` (pure)
4. `compose_projection` (pure, token-budgeted — default 1500 tokens)
5. `reinforce_hits` (fire-and-forget; per-slice rate-limited at 6/min)
6. `log_recall` (enqueued to `substrate_recall_log`)

**Failures never reach the caller** — recall always returns a projection,
possibly empty with `empty_reason` set (`timeout`, `no_candidates`,
`token_budget_exhausted`, `error`).

**Validating after flip:**

```bash
# Are calls happening + landing in the log?
thoth substrate recall

# Look at a specific session's most-recent recall
thoth substrate recall sample --session-id <id>

# What's the embedding coverage? (Curator backfills async — climbs toward 100%)
thoth substrate recall    # "coverage" line in the summary
```

Recall against slices without embeddings falls back to keyword Jaccard, so
coverage is an optimization target, not a correctness gate.

## Common Operator Workflows

### "Show me what the agent perceived in this session"

```bash
# Find the session id (foreground)
thoth sessions list | head

# All user-message slices for the CLI source, ordered most-recent first
thoth substrate slices --stream thoth.world.user_message.cli --limit 50

# Assistant responses
thoth substrate slices --stream thoth.self_action.assistant_response --limit 50
```

For session-scoped filtering you currently need raw SQL — the `payload` JSON
column has a `session_id` key on most streams:

```sql
SELECT event_time_world, payload->>'text' AS text
  FROM substrate_slices
 WHERE stream_id = (SELECT stream_id FROM substrate_streams
                    WHERE name = 'thoth.world.user_message.cli')
   AND payload->>'session_id' = '<session-id>'
 ORDER BY event_time_world DESC;
```

### "Why didn't recall return X?"

1. `thoth substrate recall sample --session-id <id>` — look at
   the most recent recall row for that session. Check `empty_reason`,
   `returned_count`, and the candidate count.
2. If `empty_reason='no_candidates'`: the time window
   (`THOTH_RECALL_TIME_WINDOW_HOURS`, default 24) may be too tight, or the
   salience floor (`THOTH_RECALL_MIN_SALIENCE`, default 0.05) is excluding
   everything.
3. If candidates existed but X wasn't among them: check whether X's slice has
   an embedding (recall summary "coverage" line) and what its current salience
   is. Released slices (`consolidation_state='released'`) are excluded.
4. If `empty_reason='timeout'`: the 300 ms SQL budget was exhausted. Usually
   means the pgvector index needs maintenance (`REINDEX INDEX
   substrate_slices_embedding_idx`) or a recent partition lacks the index.

### "Are embeddings backfilling fast enough?"

```bash
# Summary line includes embedding coverage %
thoth substrate recall

# Recent curator emissions — look for curator.embed_batch successes/failures
thoth substrate curator recent --limit 50
```

If coverage is stuck below 100% and growing slowly, check:

- The auxiliary embedding provider is configured and reachable
  (`thoth config get auxiliary.embedding`)
- `THOTH_RECALL_EMBEDDING_BACKFILL_INTERVAL_S` (default 30 s) — lower this to
  embed more aggressively
- `THOTH_RECALL_EMBEDDING_BATCH_SIZE` (default 32) — raise if your provider
  supports it
- Slices that failed `THOTH_RECALL_EMBEDDING_BACKFILL_MAX_RETRIES` times
  (default 3) are persistently marked failed in metadata and dropped from the
  unembedded list — query the table for them:
  ```sql
  SELECT slice_id, metadata->'embed_failure'
    FROM substrate_slices
   WHERE metadata ? 'embed_failure';
  ```

## Tuning Knobs

Read once at boot — set in `.env` and restart. Full list in
`substrate/config.py`. Common ones:

| Env var | Default | What it controls |
|---------|---------|------------------|
| `THOTH_SUBSTRATE_RECALL` | `0` | Master toggle for substrate-backed `<memory-context>` |
| `THOTH_RECALL_TOKEN_BUDGET` | `1500` | Per-turn projection token cap |
| `THOTH_RECALL_TIME_WINDOW_HOURS` | `24` | Recall lookback window |
| `THOTH_RECALL_TIMEOUT_MS` | `300` | Per-call SQL timeout |
| `THOTH_RECALL_MIN_SALIENCE` | `0.05` | Salience floor for candidacy |
| `THOTH_RECALL_SIMILARITY_WEIGHT` | `0.3` | pgvector cosine weight in composite score |
| `THOTH_RECALL_KEYWORD_WEIGHT` | `0.3` | Keyword Jaccard weight |
| `THOTH_RECALL_SALIENCE_WEIGHT` | `0.5` | Current salience weight |
| `THOTH_RECALL_RECENCY_WEIGHT` | `0.2` | Recency decay weight |
| `THOTH_RECALL_RECENCY_HALF_LIFE_HOURS` | `12` | Recency exponential half-life |
| `THOTH_RECALL_REINFORCE_RATE_LIMIT_PER_MIN` | `6` | Per-slice reinforcement cap (anti-thrash) |
| `THOTH_AUTO_MIGRATE` | `0` | If `1`, substrate boot auto-runs `alembic upgrade head` |

## Troubleshooting

### Substrate boot raises `RuntimeError: substrate behind expected revision`

The DB is on an older Alembic revision than the substrate code expects. Fix:

```bash
uv run alembic -c migrations/alembic.ini upgrade head
# Or set THOTH_AUTO_MIGRATE=1 in .env so first boot upgrades automatically.
```

### Port collision on 5432

The default `docker compose` PG service binds host port 5432. If you already
run PG on the host, either stop the host service for development or override:

```bash
docker compose down postgres
# Bind to 5433 instead by setting POSTGRES_HOST_PORT before compose up,
# then point THOTH_PG_DSN at 5433.
```

The test container runs on port 5433 (`postgres-test` profile) and is
**always** used by the test suite — never the dev PG. Verify with
`docker compose ps`.

### Partition-maintenance lag

If you see slices landing in the DEFAULT partition (visible via `\d+
substrate_slices` in `psql`), the maintenance worker hasn't run or its tick
is stuck. It runs once at boot and every 24 h after. To force-run:

```sql
-- Manual: create the next-month partition by hand
SELECT substrate_create_partition_if_not_exists(date_trunc('month', now() + interval '1 month'));
```

(Helper function is defined in the partition-maintenance code path; see
`substrate/storage/partitions.py`.)

### Sub-agent not ticking / Curator quiet

```bash
# Check the Thoth process log for substrate sub-agent boot messages
tail -F ~/.thoth/logs/agent.log | grep -i substrate
```

Sub-agents are spawned by `Substrate.boot()`. If any raised on startup,
the boot path logs it but does NOT abort Thoth (substrate failures are
non-fatal). Look for "subagent failed to start" entries.

### Recall returning empty for everything

Confirm the provider is actually active:

```bash
# This env var must be 1 at process start
echo $THOTH_SUBSTRATE_RECALL
```

If it's `1` but recall is empty, check the provider registration log:

```bash
grep -i "SubstrateMemoryProvider" ~/.thoth/logs/agent.log | tail
```

If registration failed (Phase C provider import error), the upstream memory
path is still serving `<memory-context>` — you just won't get substrate
recall added on top.

## Verification

A healthy substrate, freshly booted, should pass these checks:

```bash
# 1. Streams present (15 — 7 user-message sources + 7 self-action/state + 1 substrate.self_state)
thoth substrate streams | wc -l    # ~17 lines (header + 15)

# 2. Decay profiles seeded (4)
thoth substrate profiles | wc -l    # ~6 lines (header + 4)

# 3. Pending queue not growing without bound (depth should stay low under steady use)
thoth substrate pending

# 4. After a few turns of conversation, slices > 0
thoth substrate    # "Slices: N total" should be non-zero

# 5. After Curator has run at least once, recent curator emissions exist
thoth substrate curator recent
```

If any of these fail on a fresh install: re-run `alembic upgrade head`, then
restart Thoth.

## Pitfalls

- **Never run pytest against the real `thoth` PG on port 5432.** The test
  suite uses a dedicated `postgres-test` container on port 5433 (or whatever
  `THOTH_TEST_POSTGRES_PORT` is set to). `PYTEST_XDIST_WORKER` must be set
  when running pytest directly — `pytest-postgresql` uses it to derive
  per-worker DB names so concurrent subprocesses don't race on the shared
  template DB.
- **The substrate is write-mostly in steady state.** A long-running Thoth
  emits hundreds of slices per active session. If you see PG storage growing
  fast, the Curator is the throttle — verify it's ticking
  (`thoth substrate curator recent`) and the
  `consolidation_window` on your decay profiles is reasonable.
- **`THOTH_SUBSTRATE_RECALL=1` is irreversible mid-process.** The
  `SubstrateMemoryProvider` checks the env var at construction time. To
  disable, set `THOTH_SUBSTRATE_RECALL=0` in `.env` and restart.
- **Phase A's Conductor is a stub.** Setting an intensity level does not yet
  change scheduling — it's stored, not consumed. The plumbing is there so
  Phase F can land the real policy without a sub-agent refactor.
- **Substrate failures are deliberately non-fatal.** The conversation keeps
  going even if every slice write fails. If you see "recall returned empty"
  consistently, check the Thoth log for substrate exceptions before
  concluding the data isn't there.

## Observability & layer commands (Phases D–G)

Start here when asking "is the substrate healthy?":

- **`thoth substrate health`** — one-glance operator rollup: worker
  liveness, last boot, the Critic's coherence vital sign, per-layer
  L0–L4 counts, and consolidation backlog. The first thing to run.
- **`thoth substrate agents`** — per-sub-agent liveness (live/stale/down
  by heartbeat age). All DOWN ⇒ the worker subprocess isn't running.
- **`thoth substrate boot`** — last boot outcome per process role.
- **`thoth substrate recall validate`** — runs a real recall and prints the
  composed `<memory-context>` block + a READY/DEGRADED/NOT-READY verdict.
- **`thoth substrate l1 entities|relationships`** — L1 knowledge (Parser).
- **`thoth substrate parser summary|recent`** — Parser activity + outcomes.
- **`thoth substrate l2 associations`** — L2 graph (Associator).
- **`thoth substrate l3 patterns`** — L3 generalizations (Pattern-finder).
- **`thoth substrate l4 observations`** — L4 self-model + coherence (Critic).

**Curation** (let users shape what the substrate keeps):

- **`thoth substrate pin <slice_id>`** / **`unpin`** — pin a memory so the
  Curator never decays or releases it (the "never forget this" override);
  pinning also lifts its salience so it surfaces in recall.
- **`thoth substrate forget <slice_id>`** — drop a memory's salience to 0
  so the Curator releases it on its next cycle.
- **`thoth substrate l1 dupes`** — review likely-duplicate entities, then
  **`l1 merge --from X --into Y`** to consolidate fragmented memory.
- **`thoth substrate l1 forget <name>`** / **`l1 edit <name> --summary …`**
  — delete or correct an entity.

Recall precision is tunable via `THOTH_RECALL_MIN_RELEVANCE` /
`_RELATIVE_FLOOR` (drop loosely-related context) + `_DEDUP_THRESHOLD`
(near-duplicate excerpts) + `RECALL_SHOW_PROVENANCE=1` (inline "why
injected"); see `thoth substrate recall config` / `recall validate`.

The cognitive sub-agents are **ON by default** — set the env var to `0` to
disable a given one. Each still registers + heartbeats regardless; the gate
only controls whether its tick does work. LLM-driven agents no-op silently
when no auxiliary provider is configured.

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

All require the worker subprocess (`thoth substrate worker run`) to be
running. They depend bottom-up (Parser feeds the rest), so on a fresh
install L1+ fills only after the Parser has consolidated some L0.

`THOTH_SUBSTRATE_SENTINEL_DEFENSE` (Sentinel content defense — quarantine
of suspected prompt-injection) is the one feature still **default OFF**: a
false-positive silently drops a slice from recall, so enable + tune it
against your own traffic during local testing.

### SkillScout — self-authored skills (Tier 1, default OFF)

`THOTH_SUBSTRATE_SKILL_SCOUT` (default **OFF**) enables the SkillScout: it
mines the upper layers for a recurring, high-salience need, asks the auxiliary
model to draft a `SKILL.md` for it, stages it as a **pending proposal**
(`substrate_skill_proposals`), and messages the user to review it in chat. It
**never installs a skill** — approval via the `skill_proposal` tool
(`list` / `show` / `approve` / `reject`) is the human gate; `approve` promotes
the draft through the normal `skill_manage create` path (frontmatter validation
+ security scan + collision check) and marks it agent-created so the skills
Curator (`agent/curator.py`) maintains it. See
`docs/plans/2026-05-28-substrate-self-improvement-forge.md`.

| Knob | Default | Meaning |
|---|---|---|
| `THOTH_SUBSTRATE_SKILL_SCOUT` | `0` | Master toggle (opt-in) |
| `SKILL_SCOUT_INTERVAL_S` | `3600` | Min seconds between runs (also change-gated on L3) |
| `SKILL_SCOUT_SALIENCE_FLOOR` | `0.7` | Min L3 salience for a candidate need |
| `SKILL_SCOUT_MAX_PENDING` | `3` | Cap on open proposals (don't flood the user) |
| `SKILL_SCOUT_DEDUP_MIN_OVERLAP` | `3` | Keyword overlap that counts as "already covered by a skill" |
| `SKILL_SCOUT_TIMEOUT_S` | `40` | Drafting (and evaluator) LLM call timeout |
| `auxiliary.skill_author.{provider,model}` | auto | Drafting model (config; falls back to the auto provider chain) |

**Phase 2 — the evaluator (defense-in-depth):** when the scout drafts a skill, a
*frontier-model evaluator* (`substrate/skill_proposals/evaluator.py`) judges it
against a guardrail + design + intent rubric before you see it, attaching a
`pass`/`flag`/`reject` verdict to the proposal (shown in `skill_proposal show`).
It complements — never replaces — the deterministic install-time scan
(`tools/skills_guard.py`) and your approval: it can flag or auto-reject, but never
auto-installs. Treats the draft as untrusted input (injection-resistant).

| Knob | Default | Meaning |
|---|---|---|
| `SKILL_EVALUATOR_MODE` | `advisory` | `off` (skip) / `advisory` (verdict attached + shown, never blocks) / `gate` (a `reject` verdict silently auto-rejects the draft + logs telemetry; `pass`/`flag` still notify you) |
| `auxiliary.skill_evaluator.{provider,model}` | auto | Evaluator model — point at a **different/stronger** model than `skill_author` (uncorrelated failure modes) |
| `SKILL_EVALUATOR_TEMPERATURE` | `0.0` | Judge temperature (deterministic by default) |

**Refinements still deferred (flagged in the phase PRs):** the L2-grounding
coherence signal (needs L1/L2 decay), per-stream-family Sentinel trust +
embedding/LLM detection + re-Sentineling, multi-horizon Conductor
forecasting / policy-learning, foreground-attention definition (MVS §8.6),
and entity merge/dedup.
