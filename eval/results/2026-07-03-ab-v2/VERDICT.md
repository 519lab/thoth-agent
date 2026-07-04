# Phase 3 verdict — substrate context engine vs compressor (2026-07-03)

Official DB-backed A/B per plans/substrate-context-engine.md §4/§5.
10 tasks x 3 runs per arm, live-install model (Qwen3.6-35B-MTP @
unsloth-rig), each arm from a freshly re-seeded thoth_baseline snapshot,
50k compress threshold, mechanism-verified (arm B minted 150 eviction
slices; recalls + context telemetry confirmed flowing).

| Metric | Compressor | Substrate | Criterion | Result |
|---|---|---|---|---|
| Pass rate | **96.7%** (29/30) | 90.0% (27/30) | success >= baseline | **FAIL** |
| Mean outcome score | 0.9412 | 0.9389 | (tie) | — |
| Prompt tokens/task | 498.6k | **483.1k** (−3.1%) | cost <= baseline | PASS |
| Cache hit rate | 73.7% | 74.4% | — | ~tie |
| Tier-2 summarizations/task | 1.60 | **0.73** (−54%) | — | mechanism works |
| Memory probes | 9/9 | 9/9 | probes > baseline | FAIL (tie) |
| Constraint survival | 5/6 | 5/6 | constraint > baseline | FAIL (tie) |
| Errors / harness failures | 0 | 0 | no tail regression | PASS |

Failures: compressor c2r2; substrate e2r1, c2r1, e1r2.

## Decision (pre-committed, plan §4)

**The engine LOSES.** Success is below baseline and neither comprehension
probes nor constraint survival beat it. Per the pre-committed rule, the
compressor stays the default engine; Phase 4 (replacement) does NOT
proceed. The engine branches (2a-2d) remain unmerged.

## Honest reading

- The margin is 2 tasks of 30 — weak statistical evidence of inferiority
  (binomially plausible under equal true rates). But the burden of proof
  was the challenger's, and nothing anywhere beat the baseline.
- The mechanisms all WORKED: eviction replaced half the summarization
  events, tokens dropped slightly, comprehension held, zero degradation
  errors across the matrix. The engine is safe — it just isn't better
  on this suite/model.
- Confound worth naming: the baseline arm ALSO benefits from the
  substrate (recall injection ran in both arms — the unification value
  partially landed on both sides of the A/B). The compressor-with-recall
  turned out stronger than expected (compare the v1-era no-DB constraint
  collapse that DB-backed recall erased).
- Untested headroom, if this is ever revisited: task-boundary triggers
  (2b shipped threshold-only), stub gist quality, eviction-slice
  rendering budget, and larger-n runs for statistical power.

## What survives regardless

Phase 0a substrate fixes, Phase 0b context telemetry, the graded suite
(this instrument), the DB-backed grading protocol with mechanism
verification, and the retrieval tools' plumbing knowledge.
