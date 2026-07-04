# Round 3 verdict — cooling + informative gists vs compressor (2026-07-04)

Graded arm: CoolingContextEngine with round-3 informative gists
(structural extraction incl. verbatim first-5-lines + batched main-model
summaries), 10 tasks x 3 runs, re-seeded thoth_baseline, live model.

| Criterion (pre-committed) | Target | Round 3 | Result |
|---|---|---|---|
| Pass rate | >= 29/30 | 27/30 (90%) | **FAIL** |
| Prompt tokens/task | <= 448.8k | 488.1k (−2.1% vs baseline) | **FAIL** |
| Backstop firings/task | < 0.2 | 0.67 | **FAIL** |
| Memory probes | >= 9/9 | 9/9 | PASS |
| Constraint survival | >= 5/6 | **5/6** | **PASS** |
| Errors | 0 | 0 | PASS |

Failures: e3r0, c2r0, e2r2 (scattered end-state/constraint flakes).
Mean outcome **0.9506 — the highest of any arm** (baseline 0.9412).
Cache hit 71.3%.

## Decision (pre-committed)

**Round 3 LOSES** (3 of 6). The compressor remains the default engine.

## Honest reading

- **The gist fix did its job**: constraint survival recovered from 4/6
  to 5/6 (c2 2/3, incl. a 37s clean pass), tying baseline — the family
  the informative gists targeted. Mean outcome is the best measured.
- **But it ate the token win**: richer stubs + unchanged backstop rate
  pushed tokens from 442.8k back to 488.1k (−11.2% → −2.1%). The two
  goals traded off almost exactly.
- **The suite's flake floor is now the binding constraint.** Every
  challenger lands 27/30, each failing a DIFFERENT scatter of tasks
  (timeouts, e2/e3 one-offs), while the one 29/30 compressor run sits
  within the same noise band. At n=30, a ~1-3 task flake floor makes
  "pass >= 29/30" nearly unbeatable by construction. Any round 4 should
  FIRST raise statistical power (5+ runs/arm, tightened tasks, retry-
  once-on-harness-flake policy) or the criteria will keep rejecting
  engines for noise.

## Standing after three rounds

| arm | pass | tokens/task | outcome | backstop/task |
|---|---|---|---|---|
| compressor (baseline) | 29/30 | 498.6k | 0.9412 | 1.60 |
| substrate (threshold eviction) | 27/30 | 483.1k | 0.9389 | 0.73 |
| cooling (age distillation) | 27/30 | **442.8k** | 0.9409 | **0.60** |
| cooling + informative gists | 27/30 | 488.1k | **0.9506** | 0.67 |

The pieces each proved their individual claim (eviction cuts
summarizations; cooling cuts tokens; informative gists restore quality)
— but no single configuration has held all wins simultaneously at this
sample size.
