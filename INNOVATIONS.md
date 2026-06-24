# Innovation Proposals — Thoth Agent
*Generated 2026-06-24 · based on commit 9904ea8d8*

> Source: an inventive-engineering review (parallel surveys of the substrate,
> gateway, agent loop, skills, and model layer + state-of-the-art research).
> Proposals are ranked. The **Promoted for build** section at the bottom names
> the winning set to plan and implement first.

## How this codebase stands today

Thoth is two cognitive systems stapled together: a genuinely novel
**PostgreSQL-native cognitive substrate** (L0→L4 perception→consolidation→recall
with a real sub-agent crew — Curator, Sentinel, Dreamer, Conductor) and a more
conventional **foreground skill-learning loop** (a forked background-review
agent). The engineering is unusually self-aware: the code comments read as an
honest incident log (a 414k-slice phantom backlog, 900 self-alarms/hr, a
200-dreams/hr runaway). Multi-provider model plumbing, 15+ messaging platforms,
and exact-pinned deps post-supply-chain-incident all signal production maturity.

The decisive gap is the same everywhere, and three independent survey passes
reached it separately: **everything is observed, almost nothing is evaluated.**
The self-improvement loop is end-to-end *open*. Thoth can perceive, consolidate,
recall, draft skills, and even run an uncorrelated LLM-judge — but no signal
anywhere measures whether a learned skill or a tuned weight actually made the
agent *better*. `substrate_recall_log` audits *what* was recalled, never
*whether it helped*; `skill_usage` counts loads, never outcomes; the Curator's
own comment admits "use=0 is not evidence a skill is valuable." Every ranking
weight is a hand-tuned static env constant.

## What the best in this space are doing

- **Skill-library self-improvement with efficacy feedback** — Voyager's ablation
  showed the skill library is worth a ~15× speedup *because* skills are verified
  against environment feedback before entering the library; PolySkill separates
  abstract goal from concrete impl for reuse. Thoth writes skills but never
  verifies efficacy.
  ([self-improving agents](https://yoheinakajima.com/better-ways-to-build-self-improving-ai-agents/))
- **Reranking as the cheap retrieval win** — vector search returns the right
  candidates in the wrong order; a second-pass reranker re-scores before context
  assembly. Thoth weights non-comparable Jaccard and cosine scores equally and
  never reranks.
  ([Agent Memory Techniques](https://github.com/NirDiamant/Agent_Memory_Techniques))
- **Memory consolidation / decay / contradiction-detection** as the live frontier
  — temporal versioning, scheduled consolidation sweeps, decay modulated by
  relevance. Thoth's Curator decay loop is still a Phase-B stub (all slices sit
  at salience 1.0).
  ([mem0 State of Agent Memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026))
- **OpenTelemetry GenAI semantic conventions** are now the observability standard
  — `invoke_agent` / `execute_tool` / vector-query spans with token/cost
  attributes. Thoth has minimal structured logging and no trace context.
  ([OTel AI agent observability](https://opentelemetry.io/blog/2025/ai-agent-observability/),
  [MLflow GenAI semconv](https://mlflow.org/docs/latest/genai/tracing/opentelemetry/genai-semconv/))

---

## Proposals (ranked)

### 1. Close the loop: a recall-replay eval harness that turns `substrate_recall_log` into learned ranking ⭐ PROMOTED
**Category:** architecture / wow · **Impact 5 · Novelty 5 · Effort 2 · Fit 5**

**The idea.** The substrate already logs every recall — query, candidate set,
chosen path, provenance, coherence (`substrate/recall/log.py`). It's a goldmine
sitting unused as a pure audit trail. Add one thing it lacks — an *outcome label*
per recall (did the turn that consumed this context succeed?) — and you can
replay history offline to (a) measure recall quality and (b) fit the four ranking
weights (`salience 0.5 / similarity 0.3 / keyword 0.3 / recency 0.2`,
`projection.py:92-96`) instead of hand-tuning them in production. This is the
keystone: learned ranking, efficacy-based curation, and safe autonomy past the
human gate are all blocked on this one missing measurement.

**Inspired by.** Generative Agents' reflection-ablation methodology + offline RL
replay; original to Thoth in that the log infrastructure already exists.

**Implementation sketch.** (1) Add a nullable `outcome_score` to
`substrate_recall_log`, written post-turn from cheap proxies already computed in
`conversation_loop` — turn completed without error / no user re-ask / positive
tool-success rate from `batch_runner.py`. (2) New `substrate/recall/replay.py`:
load N logged recalls, re-run `rank_candidates()` (already a pure function —
`projection.py:183-241`) under candidate weight vectors, score against outcome
labels (NDCG over "useful" slices). (3) A grid/Bayesian sweep emits a tuned
weight vector; surface as a report, not auto-applied. **First win is the harness;
auto-tuning is the follow-up.**

**Effort.** 2–3 days for the harness + outcome labeling; weight-fitting another
1–2. Main risk: outcome-proxy noise — mitigate by starting with the strongest
signal (explicit user correction / re-ask).

**First step.** Add `outcome_score` column + write the proxy in the post-turn
block; that alone makes the existing log analyzable.

### 2. Skill efficacy signal — measure whether a loaded skill correlated with success ⭐ PROMOTED
**Category:** feature · **Impact 5 · Novelty 4 · Effort 3 · Fit 5**

**The idea.** `tools/skill_usage.py` tracks `use_count`/`last_used_at`, but "used"
means "loaded into the prompt," not "helped." The Curator
(`agent/curator.py:353`) archives on 90-day *inactivity*, never *uselessness*,
and says so. Close this: join skill-load events to the same turn-outcome signal
from Proposal 1, so each skill accrues an efficacy estimate. Then (a) the
`skill_proposals/evaluator.py` verdict — currently a dead-end advisory to a human
— feeds skill ranking, and (b) the Curator archives low-efficacy skills, not just
idle ones.

**Inspired by.** Voyager skill-library verification; PolySkill reuse signals.

**Implementation sketch.** Extend `skill_usage` with a rolling `efficacy_ema`. In
the post-turn hook, attribute the turn outcome to skills loaded that turn. Wire
`evaluator.py`'s pass/flag/reject into the `skills_match` ranker (keyword-only
today — see #5). Change `curator.py` archival predicate from `last_used < 90d` to
`efficacy_ema < floor AND mature`.

**Effort.** 3 days. Risk: attribution when multiple skills load — start with
single-skill turns, expand with credit-splitting later.

**First step.** Add the `efficacy_ema` field + attribution write; report skills
ranked by it before changing any archival behavior.

### 3. Add a reranker pass to recall (and embed the L3/L4 headers) ⭐ PROMOTED
**Category:** performance / feature · **Impact 4 · Novelty 3 · Effort 2 · Fit 5**

**The idea.** Two concrete retrieval defects: (a) Jaccard (tiny, length-
asymmetric, no stemming) and clamped cosine are summed at ~equal weight (~0.3
each) despite living on non-comparable scales; (b) the *abstraction* layers
retrieve worse than the raw episodes beneath them — L3/L4 headers rank by
**trigram + salience**, not embeddings (`api.py:524-563`). Add a single rerank
pass over the top-K candidates and embed the abstraction headers. Reranking is
the highest-ROI retrieval upgrade in the 2025 literature precisely because it
fixes ordering cheaply.

**Inspired by.** [Agent Memory Techniques / reranking patterns](https://github.com/NirDiamant/Agent_Memory_Techniques)
— "right candidates, wrong order."

**Implementation sketch.** After `recall_window` SQL returns candidates, rerank
top-K (a local cross-encoder via `sentence-transformers`, or reuse the aux
LLM-judge already wired for `evaluator.py` — no new dependency). Replace the
additive Jaccard+cosine term with the reranker's unified relevance. Give L3/L4
headers embeddings via the existing Curator backfill path. Keep behind
`THOTH_SUBSTRATE_RECALL_RERANK` for A/B against the harness in #1.

**Effort.** 2 days local-reranker; <1 day if reusing the aux model. Risk: 300ms
recall budget — cap K small (≤15), measure with #1.

**First step.** Embed L3/L4 headers (reuses existing infra, immediate win) before
adding the reranker.

### 4. Per-tick watchdog: actuate on the liveness data you already collect ⭐ PROMOTED
**Category:** ops · **Impact 4 · Novelty 2 · Effort 2 · Fit 5**

**The idea.** The sub-agent crew has *excellent* stuck-worker **detection** —
heartbeat + `tick_count` upsert distinguishes "frozen but alive" from healthy
(`substrate/agents/base.py`). But `base.py:215` awaits `tick()` with **no
per-tick timeout**, and the heartbeat is driven from the *same coroutine* — so a
hung DB/LLM call freezes the work *and* the heartbeat, and nothing acts on the
staleness (`stop_and_wait` only guards shutdown). Detection without actuation.
Add the watchdog.

**Inspired by.** Original — derived from the gap both deep-dives flagged;
standard supervised-actor pattern.

**Implementation sketch.** Wrap the tick in
`asyncio.wait_for(self.tick(), timeout=per_intensity_ceiling)`; on `TimeoutError`,
log structured + increment a stall counter + skip rather than block. Move
heartbeat to an independent task so liveness is reported even while a tick hangs.
Add a supervisor that restarts a crew subprocess whose `tick_count` is frozen
past a threshold (today it relies on systemd).

**Effort.** 1–2 days. Risk: choosing timeouts that don't false-trip the Curator's
legitimately long backfills — derive from observed P99.

**First step.** Wrap `tick()` in `wait_for` with a generous ceiling and log
timeouts — pure safety, no behavior change.

### 5. Embedding-based skill matching (the embedding stack is right next door)
**Category:** feature · **Impact 3 · Novelty 3 · Effort 2 · Fit 5**

**The idea.** `substrate/skills_match.py:68-92` matches and dedups skills by
**keyword overlap only** — while a full embedding pipeline sits one module over.
Semantically near-duplicate skills (the exact thing the churn-biased
background-reviewer in #8 produces) slip past keyword dedup. Embed skill
descriptions and match/dedup by cosine.

**Inspired by.** PolySkill goal-vs-impl separation; standard semantic dedup.

**Implementation sketch.** On skill write, embed `name + description` via the
existing `embeddings.py`; store on the skill record. Replace keyword Jaccard in
`skills_match` with cosine + a dedup gate at author time
(`skill_proposals/author.py`) that rejects a draft within ε of an existing skill.

**Effort.** 2 days. Risk: embedding-endpoint coupling — reuse the substrate's
config and Jaccard fallback.

**First step.** Add the embedding column + backfill existing skills.

### 6. Wire cost-aware model routing (the cost data is already fetched and thrown away)
**Category:** ops / performance · **Impact 4 · Novelty 2 · Effort 2 · Fit 4**

**The idea.** `agent/models_dev.py:73-76` *fetches* `cost_input`/`cost_output`
per model and **never reads them**. Aux-task tiering is a hardcoded per-provider
dict (`auxiliary_client.py:261-282`) and `"auto"` silently uses the full main
model regardless of price. Rank the aux tier by actual cost and let an expensive
main model auto-downgrade for cheap tasks (compression, embeddings, the substrate
crew). This is real money on a 24/7 multi-platform gateway.

**Inspired by.** Original — derived from the dead cost fields; standard
model-router practice.

**Implementation sketch.** Build a cost-ranked candidate list from the
already-fetched metadata; make aux resolution pick the cheapest model meeting a
capability floor (context length, vision). Add an opt-in `THOTH_COST_AWARE_AUX`.
Log the per-task model+cost via #7's spans.

**Effort.** 2 days. Risk: a too-cheap model degrading compression/judge quality —
gate by task class, validate against #1's harness.

**First step.** Surface a "current routing vs cost-optimal" report from the
fetched metadata — quantify the savings before changing routing.

### 7. Instrument the agent loop with OpenTelemetry GenAI semantic conventions
**Category:** ops · **Impact 4 · Novelty 3 · Effort 3 · Fit 4**

**The idea.** Observability is the documented weak spot — minimal structured
logging, no trace context, no request/session correlation across the
model→recall→tool chain. Adopt the now-standard OTel GenAI semconv:
`invoke_agent`, `execute_tool`, vector-query spans with token/cost/latency
attributes. You cannot tune what you cannot see — this is the substrate under
Proposals 1, 2, and 6, and it's now a vendor-portable standard rather than a
bespoke metrics layer.

**Inspired by.** [OTel GenAI semantic conventions](https://opentelemetry.io/blog/2025/ai-agent-observability/),
[MLflow GenAI semconv](https://mlflow.org/docs/latest/genai/tracing/opentelemetry/genai-semconv/)
— the `plugins/observability/` dir already hints this was intended.

**Implementation sketch.** Add `opentelemetry-sdk` + GenAI instrumentation; wrap
the model call in `conversation_loop`, the recall pipeline, and
`model_tools.handle_function_call` with semconv spans carrying
session_id/model/token/cost. Export OTLP (Grafana/Loki already collect LLM
traces). Keep no-op when no collector is configured.

**Effort.** 3 days for the core spans. Risk: span overhead on the hot path —
sample, and gate behind an env flag.

**First step.** Instrument just the main model call with one `invoke_agent` span —
proves the pipe and immediately gives token/cost/latency per turn.

### 8. Stop the background-reviewer from churning skills it doesn't need to ⭐ PROMOTED
**Category:** feature (correctness) · **Impact 3 · Novelty 3 · Effort 1 · Fit 5**

**The idea.** The clearest self-improvement anti-pattern in the codebase: the
foreground review prompt (`agent/background_review.py:45-69`) tells the fork
*"most sessions produce at least one skill update… a pass that does nothing is a
missed learning opportunity, not a neutral outcome."* That **biases the system
toward writing skills whether or not warranted** — manufacturing exactly the
low-value, near-duplicate skills #2 and #5 then have to detect and archive. A
self-improving agent should treat "no change needed" as a first-class, common,
correct outcome.

**Inspired by.** Voyager's "only add a skill when it verifiably helps"; original
observation.

**Implementation sketch.** Rewrite the prompt so no-op is explicitly valid and
evidence-gated (propose a skill only on a repeated pattern or a corrected failure
this session). Once #2 exists, gate writes on a minimum projected-efficacy bar.
Pair with the trigger: the bare 10-API-call counter
(`conversation_loop.py:4150`) should fire on *signal* (a failure, a correction, a
novel tool sequence), not a fixed tick.

**Effort.** Half a day for the prompt; the trigger change ~1 day.

**First step.** Edit the prompt to make "no update this pass" a stated success — a
near-zero-risk change with immediate effect on skill-churn.

---

## Killed ideas (and why)
- **Refactor `auxiliary_client.py` (5.3k lines) into a provider-plugin registry**
  — real duplication, but it's a refactor, not invention; do it opportunistically.
- **Flip the coverage `fail_under` floor / fix the 230 triaged test failures** —
  generic, and the triage doc is dated 2026-05-25 and likely partly stale given
  the live coverage initiative; verify-then-cleanup, not a proposal.
- **Replace regex-based error classification with structured codes** — worthwhile
  hardening, but incremental plumbing, not high-leverage.
- **Make the Dreamer "load-bearing" by asserting dreams into L1–L4** — tempting
  wow-factor, but it would corrupt memory with hallucinations; the safe version is
  "feed *vetted* dreams as L3/skill *seeds*," which folds into #1/#2's evaluation
  gate rather than standing alone.
- **Distributed lock for cron overlap** — a genuine bug (two gateways double-fire),
  but a targeted fix, not an innovation.

## Suggested order of attack
**#1 is the keystone — build it first**; it's low-effort because the log already
exists, and it unblocks the measurement that #2, #3, and #6 all want to validate
against. Land the quick wins in parallel since they're independent and
near-zero-risk: **#8** (prompt edit, half a day) and **#4** (tick watchdog,
safety-only). Then **#3** (reranker, A/B'd against #1's harness) and **#2** (skill
efficacy, which consumes #1's outcome signal). **#7** (OTel) is connective tissue
worth starting early since #1/#2/#6 all benefit from per-turn spans.

---

## Promoted for build

The winning set to plan and implement, in dependency order. Theme: **close the
self-improvement loop, then harden the crew that runs it.**

| # | Proposal | Why it's in | Depends on |
|---|----------|-------------|------------|
| **1** | Recall-replay eval harness + outcome label | Keystone — unblocks all learned behavior; lowest effort/highest leverage | — |
| **8** | Fix background-review skill-churn bias | Near-zero-risk quick win; removes a documented anti-pattern feeding the loop | — |
| **4** | Per-tick watchdog (actuate on liveness) | Independent reliability win; detection already exists, only actuation missing | — |
| **3** | Recall reranker + embed L3/L4 headers | Direct retrieval-quality lift, A/B-validated by #1 | #1 (for measurement) |
| **2** | Skill-efficacy signal + efficacy-based curation | Completes the closed loop; consumes #1's outcome signal | #1 |

**Deferred (strong, but next wave):** #5 (embedding skill dedup), #6 (cost-aware
routing), #7 (OTel instrumentation — worth pulling forward if observability is
wanted under the build).
