# Substrate Context Engine — design sketch

*2026-07-03 · status: **EXECUTED — hypothesis LOST at Phase 3** (see outcome note below) · based on main @ 2541808b8*
*Research inputs: substrate internals map, context-engine internals map, live-DB baseline (2026-07-03), SOTA survey (mid-2026). All file:line refs verified against the current tree.*

> **Outcome (2026-07-03, same day):** all phases executed. Phase 0a fixes
> (#284–#289) and Phase 0b telemetry shipped; the graded suite + DB-backed
> A/B protocol were built (eval/context_suite); the full engine (2a–2d) was
> built and mechanism-verified. The pre-committed Phase 3 A/B (10 tasks ×
> 3 runs/arm, live model, snapshot-seeded arms): compressor 29/30 vs
> substrate 27/30; probes and constraint survival tied; tokens −3.1% and
> Tier-2 summarizations −54% for the substrate arm; zero errors both arms.
> Per §4's pre-committed criteria the challenger did not win → **the
> compressor remains the default engine; Phase 4 replacement did not
> proceed.** Full data: eval/results/2026-07-03-ab-v2/VERDICT.md (suite
> branch). Notable confound for any future attempt: the baseline arm also
> benefits from substrate recall injection, and it erased the constraint-
> family weakness that motivated part of the design. Untested headroom:
> task-boundary triggers, stub gist quality, larger-n runs.

## Goals (frozen — do not move these)

As pre-defined by Greg, verbatim:

1. **Unification of the context and substrate systems.**
2. **Intelligent context management using the substrate and short-term memory entries.**
3. **Measuring impact of changes (better model comprehension, efficient token usage, token savings (maybe?)).**
4. **Prove that the substrate system in concert with context management produces equal or higher performing model memory, less context usage, better understanding.**

End-state decision (already made): this engine **replaces** the `ContextCompressor` — no permanent engine selector. The compressor's summarization machinery survives only as an internal degraded-mode tier. The existing `context.engine` config seam is used during development/grading only; after the engine wins, the standalone compressor is deleted (de-Hermes discipline: complete, no back-compat).

---

## 1. The core idea

Treat the live conversation context as a **cache** over the persistent stores that already exist, instead of a window we lossily summarize.

Today, "compression" = LLM-summarize the middle of the conversation and discard it. The summary is a paraphrase; dropped detail is unrecoverable in-band (only `session_search` can find it, and the model rarely knows to look). Meanwhile the substrate ingests a *parallel, truncated* copy of the conversation (tool results cut to 256 chars) that the context system never coordinates with.

The new engine makes eviction **restorable**: evicted content is replaced in-context by a small, actionable stub carrying a retrieval handle; the full content stays byte-exact in Postgres; the substrate indexes it semantically so it can be *proactively* re-surfaced by recall or *reactively* paged back in by the model. Summarization is demoted from "the mechanism" to "the fallback tier."

This is the direction the field converged on in 2025–26. Independent confirmations: Manus's "restorable compression" (drop the body, always keep the URL/path), Anthropic context editing (tool-result clearing with placeholders + memory tool; their internal eval: **−84% tokens, +39% performance** vs baseline), Context-Folding (structured eviction beats summarization baselines at 10× smaller active context, +8.8 pts SWE-bench Verified), and LRE (verbatim retention beats paraphrase; −52% peak context at parity).

### What's genuinely already in place (from the research)

- **The substrate is already architecturally "cache over backing store"**: salient L0 slices within the recall window are the short-term tier; consolidated L1–L4 are the long-term tier; Curator decay/release is the eviction between them. We are adding the conversation context as the top of an existing hierarchy, not building one.
- **The verbatim backing store already exists**: every message — including full tool results — is persisted in the Postgres session store (`messages` table, FTS + trigram GIN indexed). `session_search` already retrieves exact message windows by id at ~0.8 ms. *(Verify at Phase-2 start: confirm tool-role messages persist full-fidelity through `thoth_state`.)*
- **The engine seam already exists**: `ContextEngine` ABC (`agent/context_engine.py:32`) with `get_tool_schemas()`/`handle_tool_call()` designed exactly for engine-owned retrieval tools (the docstring names `lcm_expand` et al.), plugin loading via `context.engine`, full session-lifecycle hooks.
- **The learning loop exists but is immature**: recall outcome labeling live since 6/25, 58 labels so far, all binary 0/1; `substrate_recall_weights` empty (tuner has never produced a row). Eviction policy must start heuristic and *earn* learnedness.
- **DB latency is a non-issue**: live probe shows vector top-K at 4.4 ms warm / 44 ms cold, session fetch 0.8 ms. Recall's 344 ms p50 is embedding + rerank **model calls**. The hot-path budget is about avoiding model calls, not PG tuning.

---

## 2. Architecture

### 2.1 Memory classes (every message/block gets exactly one)

| Class | Contents | Eviction treatment |
|---|---|---|
| **Pinned** | System prompt, standing constraints, active task (latest user message), todo snapshot, plan/recitation block | **Never evicted.** ConstraintRot finding: unpinned constraints go from 0% → 30–59% violation post-compaction. This class is a hard invariant, not a preference. |
| **Verbatim** | Tool results, file contents, diffs, code blocks, IDs/paths/configs, tool_call arguments | Evict body → **stub + handle**; restore byte-exact only. Never paraphrased. |
| **Semantic** | Old conversational turns, superseded reasoning, resolved subtask narration | Evictable via fold/summary; detail recoverable through handle if needed. |

Class assignment is structural (role + content shape), not model-judged — cheap and deterministic.

### 2.2 The eviction ladder (one engine, tiered)

**Tier 0 — structural prune (no LLM, no store).** Inherit and keep the compressor's Phase-1 organs (`_prune_old_tool_results`: md5 dedup of repeated tool outputs, image stripping, oversized tool-arg truncation — `agent/context_compressor.py:639-805`). Cheapest wins first.

**Tier 1 — evict-to-substrate with handle (the new mechanism).**
- **Unit**: oldest-first *tool results* beyond the protected tail, then whole resolved sub-trajectories (assistant+tool groups for completed subtasks — the fold unit). Tool results dominate token mass and are cheapest to re-materialize; every production system evicts them first.
- **Stub format** (in-context replacement, keeps the tool_use record intact so pairing invariants hold):
  `[evicted §h:{handle}: {tool} {args-gist} → {len} tokens. One-line gist: {gist}. Retrieve exact: context_expand("{handle}")]`
  Stubs must be *actionable* — exact tool syntax in the stub. SOTA finding: untrained models under-use passive placeholders (OpenAI post-trained models on compaction items for this reason); we compensate at scaffold level and **measure page-in rate** (§4).
- **Handle**: `(session_id, message_id)` into the session store — the content is already there; eviction *copies nothing*. The engine additionally commits one **eviction slice** to the substrate (new stream, e.g. `thoth.context.evicted`; direct `commit_slice`, bypassing the 256-char-truncating hook path) carrying the handle, the gist, and metadata (tool name, file paths touched). Cost: one PG INSERT (embedding backfills async via Curator — zero hot-path model calls).
- **Durability**: eviction slices are exempt from payload release (pinned, or a no-release decay profile) — the *slice* may decay in salience, but the handle target lives in the session store which is already durable.

**Tier 2 — fold/summarize (degraded + overflow fallback).** The compressor's `_generate_summary` machinery (structured Active-Task template, iterative rehydration, aux-model fallback chains — `context_compressor.py:913-1191`) survives as the tier used when (a) the substrate is unavailable (PG down, disabled install) or (b) semantic-class content must shrink beyond what stubbing achieves. Same engine, invisible to the loop.

### 2.3 Trigger policy — boundary-aligned, batched, cache-aware

- **Primary trigger**: task/subtask boundaries — a resolved todo item, a completed delegate_task, an explicit plan-step transition. SOTA is unambiguous here: boundary-aligned eviction beats pressure-triggered by +5 to +20 points (Context-Folding, CAT, Self-Compacting).
- **Backstop trigger**: token threshold (as today, provider-reported `last_prompt_tokens` with rough-estimate fallback).
- **Batching / `clear_at_least`**: never trickle. The prompt-cache math: editing history at position *p* forces a one-time suffix rewrite at 1.25× vs the 0.1× cached read — eviction pays back only if it reclaims enough tokens for enough subsequent turns. Each eviction pass must reclaim a configured minimum (start: ≥15% of context or ≥20k tokens) or not run. Oldest-first, so the re-stabilized prefix is maximal.
- **Hot-page protection**: track handle dereferences; content paged back in (or re-read via file tools) within the last N evictions is exempt. The Codex 53×-re-read incident is the failure mode: recency-only eviction thrashes on reference-heavy artifacts.

### 2.4 Retrieval — two paths back in

1. **Reactive (model-initiated)**: engine-owned tools via `get_tool_schemas()`:
   - `context_expand(handle, window?)` — byte-exact fetch from the session store (session_search plumbing; ~1 ms).
   - `context_grep(pattern)` — FTS over the current session's evicted content (the `messages` GIN indexes already exist).
2. **Proactive (substrate-initiated)**: eviction slices participate in normal recall. When the conversation returns to an evicted topic, the per-turn recall projection surfaces the gist + handle in the `<memory-context>` block — the model learns something was evicted *and* how to get it back without asking. This is our differentiator over Anthropic's server-side clearing (their placeholder is inert; ours is semantically indexed).

Existing `substrate_recall_more` stays as-is.

### 2.5 Eviction scoring — heuristic first, learned later

- **v1 (heuristic)**: score = f(class, age, size, dereference-count, subtask-resolved?). Deterministic, loggable, explainable.
- **v2 (learned, gated on data)**: LRE-style write-time keep-probability from cheap features, trained on our own logged outcomes — the exact pattern of the recall-weights loop (replay → tune → activate with guardrails, `substrate/recall/tuner.py`). Precondition: the outcome-labeling pipeline needs volume and *graded* (not binary) labels first — today's 58 binary labels can't train anything. v2 is a separate, later PR with its own go/no-go.

### 2.6 Invariants (from the code map — violate any of these and providers 400 or the loop wedges)

- tool_call ↔ tool_result pairing by id preserved through any rewrite (`_sanitize_tool_pairs` semantics).
- `function.arguments` remains valid JSON after truncation.
- Newest user message always live in the tail (issue #10896 class).
- Role alternation maintained; no consecutive same-role collisions.
- System prefix stays byte-stable between turns; eviction is a *sanctioned, batched* cache-miss event exactly as compression is today. The engine keeps maintaining `last_prompt_tokens` / `threshold_tokens` / `context_length` / `compression_count` (run_agent reads them directly).
- Session-rotation semantics on major eviction passes: reuse the `conversation_compression.py` rotation plumbing (`parent_session_id` lineage, `on_session_switch(reason="compression")`) — evicted handles must remain resolvable across rotation (handles carry their originating session_id, so they are).

---

## 3. Unification map (Goal 1)

What "unification" concretely means here:

| Today | After |
|---|---|
| Substrate hooks ingest a truncated (256-char) shadow of tool results | Engine commits full-fidelity eviction slices at eviction time; hook path unchanged for perception |
| Compression discards; substrate never told | Eviction *is* a substrate write; recall can resurface it |
| Recall injects at turn start only; unrelated to context pressure | Recall projection + eviction stubs share the handle namespace; proactive page-in |
| Two salience systems (compressor position heuristics vs substrate salience/decay) | One scoring vocabulary: eviction score feeds slice salience; dereference reinforces (existing `_reinforce_hits` pattern) |
| `session_search` is an island the model rarely uses | Its plumbing becomes the page-in fast path, advertised by every stub |

Non-goals (explicitly out of scope): changing L1–L4 consolidation, the worker crew, or the perception hook path; building a blob store (`payload_blob_ref` stays unimplemented — the session store is the verbatim store); any cross-session context injection beyond what recall already does.

---

## 4. Measurement plan (Goals 3 & 4)

### Phase 0 instrumentation (ships first, to main, regardless of the engine)

Emit via `substrate/telemetry.py` (the sanctioned non-perceptual sink — never slices):

- `context.turn` — per turn: prompt_tokens, provider-reported `cache_read_input_tokens`/`cache_creation_input_tokens`, message count, per-class token breakdown, iteration count.
- `context.evicted` — per pass: trigger (boundary/threshold/manual), tokens reclaimed, units evicted per class, handles minted, duration.
- `context.pagein` — handle dereferences: reactive (tool) vs proactive (recall-surfaced), latency, tokens restored.
- `context.compressed` — Tier-2 events (today's compressor emits nothing persistent; fix that now).
- Turn-exit diagnostics (`conversation_loop.py:4020`) mirrored as `context.turn_exit`.

### Metrics (mapped to the goals)

| Goal | Metric | Source |
|---|---|---|
| Efficient token usage | **Cost per resolved task** (not raw tokens — cache math makes raw tokens misleading), cache hit rate, peak & mean context size | `context.turn` + provider usage |
| Token savings (maybe) | Tokens per task vs baseline; the "maybe" is honest — savings must survive the 1.15× suffix-rewrite penalty; report both gross and cache-adjusted | telemetry |
| Better comprehension | (a) task success on the graded suite; (b) **probe questions**: LongMemEval-style questions about evicted-then-needed content, scored on whether the model pages in vs hallucinates; (c) **constraint survival**: standing-rule violation rate pre/post eviction (ConstraintRot protocol) | graded suite |
| Equal-or-higher memory performance | `outcome_score` distributions (existing `compute_outcome_score`), tool-failure ratio, **page-in precision** (dereferenced handles that were actually useful) and **stub-recall rate** (needed-content that was successfully paged in vs ignored — the measurement gap the SOTA survey flagged as unclosed anywhere) | suite + recall_log pattern |

### Harness

- **Graded suite is the primary instrument.** Live A/B is nearly useless for this: the live install compressed **once in 244 sessions** (p50 session = 16 messages). The mechanism only matters in long-horizon sessions, so the suite must be long-horizon by construction: batch_runner/mini_swe_runner extended with an **objective oracle** (neither has one today — "success" currently means "didn't crash"). Oracle options: golden-patch/test-pass for SWE-style tasks; scripted multi-step tasks with checkable end-state; plus the probe/constraint protocols above.
- **A/B mechanics**: same task set, same model, `context.engine` flipped (compressor vs substrate engine); paired comparison. Tail cases are mandatory, not optional: substrate-down mid-session, giant single tool outputs, 5+ eviction passes per session, adversarial "summarize-me-into-dropping-the-constraint" content.
- **Live corroboration** (secondary): telemetry distributions before/after enabling on the live install.

### Success criteria for Goal 4 (pre-committed)

The engine replaces the compressor iff, on the graded suite: task success ≥ compressor baseline; cost per task ≤ baseline; probe/constraint scores > baseline; and no tail-case regression (substrate-down runs ≥ compressor baseline, since Tier 2 *is* the compressor). If it loses, we keep the telemetry and delete the engine.

---

## 5. Phased delivery

Sequencing principle (agreed 2026-07-03): **scoring-relevant substrate fixes land and settle BEFORE baseline capture** — otherwise Goal 4's comparison measures fixes+engine, not the engine. And the graded suite never runs against the live install: suite runs write benchmark experience into real memory (slices, salience reinforcement, L1–L4 consolidation). Live supplies telemetry distributions only; the suite runs on the test instance (port 5433) seeded from a nightly-backup snapshot — realistic scale, zero contamination, re-seedable for perfect repeatability.

- **Phase 0a — Substrate at 100%** (gates everything): fix the three scoring-relevant issues — #287 (forgetting-alarm salience distortion), #288 (recall-weights tuner never ran), #289 (recall latency regression / rerank cost-benefit decision). Recall configuration must be *settled by data*, not left mid-experiment, before any baseline number is recorded. Hygiene issues #284 (partition indexes), #285 (idle conductor dialing), #286 (telemetry retention) land opportunistically — #286 preferably before long-running measurement accumulates, since Phase-0b telemetry writes to that table.
- **Phase 0b — Observability** (small PR, ships to main): the `context.*` telemetry events above; nothing behavioral.
- **Phase 1 — Grading system + baseline**: build the graded long-horizon suite + objective oracle; freeze the task set; snapshot the live DB → seed the test instance; record compressor baseline on the suite (snapshot-seeded test instance) while 1–2 weeks of live telemetry distributions accumulate in parallel.
- **Phase 2 — Engine** (branch; sub-milestones, each independently testable):
  2a. Verbatim handles + `context_expand`/`context_grep` over the session store (verify full-fidelity tool-result persistence first).
  2b. Tier-0/Tier-1 eviction with stubs, boundary+threshold triggers, batching, hot-page protection.
  2c. Substrate integration: eviction slices, proactive recall surfacing, dereference→reinforce.
  2d. Tier-2 absorption (compressor organs as fallback) + substrate-down degradation tests.
- **Phase 3 — Grade**: run the suite both ways on the same snapshot seed; publish the numbers against the pre-committed criteria; decision.
- **Phase 4 — Replace**: flip default, delete `ContextCompressor` as a standalone engine (keep absorbed organs), remove the selector docs, update docs-site. Complete migration, no residue.
- **Later / gated**: learned eviction scoring (v2, needs graded labels at volume); Slipstream-style async eviction validation; graded (non-binary) outcome labels.

## 6. Risks & open questions

1. **Models ignoring stubs** — the one failure mode with no published scaffold-level solution (frontier labs solved it with post-training). Mitigations: actionable stub syntax, system-prompt guidance, proactive recall surfacing. Measured explicitly (stub-recall rate); if it's bad, proactive surfacing carries more weight.
2. **Binary/thin outcome labels** — 58 binary labels today; the learned tuner has never fired. v2 scoring stays gated; investigate why `substrate_recall_weights` is still empty (tuner trigger?) as part of Phase 0.
3. **Session-store dependence** — the verbatim store is the session DB; installs running without PG lose Tier 1 entirely (Tier 2 covers them). Acceptable: substrate already requires PG.
4. **Duplication** — eviction slices vs the hook-ingested truncated shadow of the same content. Keep the eviction stream out of `DEFAULT_RECALL_STREAMS`' user-message set? No — it must be recallable; instead dedup at composition (MMR dedup already exists) and monitor.
5. **Compression-rare ≠ problem-rare** — live data says compression barely fires, which caps the live-visible upside. The bet is explicitly about long-horizon agentic sessions (the direction Thoth is growing), and the suite must prove value there, not on 16-message chats.

## 7. Live red flags — filed as issues 2026-07-03

Gate the baseline (fix in Phase 0a):
- **#287** — `pathological_forgetting_alarm` fired 1,602× and still firing on stuck 6/18–6/26 slices, pinning their salience at 1.0 → actively distorts recall ranking.
- **#288** — recall-weights tuner never produced a row (`substrate_recall_weights` empty) despite labeling live since 6/25 and migration 0026 applied; also: all 58 labels are binary 0/1.
- **#289** — recall latency regressed ~35 ms → 344 ms p50 / 1 s p95 since `RECALL_RERANK=true` (6/25); decide rerank's fate via the replay harness before baseline.

Hygiene (land opportunistically; #286 before long measurement runs):
- **#284** — `substrate_slices_202609` partition has zero indexes; all 7 parent-level indexes INVALID (same provisioning bug, likely).
- **#285** — Conductor dials every ~10s even when fully idle (136k events / 29 MB in 3.5 weeks).
- **#286** — no retention/pruning for `substrate_telemetry` + conductor log (30% of the live DB already).
