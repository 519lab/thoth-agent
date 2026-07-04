# Round 2 verdict — cooling-window engine vs compressor (2026-07-04)

Graded arm: CoolingContextEngine (age-based ingestion distillation,
window=5 tool-turns, floor=1500 chars, compressor as counted backstop),
10 tasks x 3 runs, live model, re-seeded thoth_baseline, mechanism-
verified. Baseline: the archived v2 compressor arm.

| Criterion (pre-committed) | Target | Cooling | Result |
|---|---|---|---|
| Pass rate | >= 29/30 | 27/30 (90%) | **FAIL** |
| Prompt tokens/task | <= 448.8k (90% of 498.6k) | **442.8k (−11.2%)** | PASS |
| Backstop firings/task | < 0.2 | 0.60 | **FAIL** |
| Memory probes | >= 9/9 | 9/9 | PASS |
| Constraint survival | >= 5/6 | 4/6 | **FAIL** |
| Errors | 0 | 0 | PASS |

Failures: e1r0 (900s watchdog timeout), c2r1, c2r2. Mean outcome 0.941
(tie with baseline). Cache hit 72.8% vs 73.7%.

## Decision (pre-committed)

**Round 2 LOSES** (3 of 6 criteria). The compressor remains the default
engine. The cooling branch (feat/substrate-context-engine-3-cooling)
stays unmerged alongside 2a-2d.

## Honest reading

- **The token promise was kept**: −11.2% prompt tokens at equal mean
  outcome — the only engine variant to clear a token criterion, and it
  did it while cutting Tier-2 summarizations 62% (1.60 → 0.60/task).
  Proactive ingestion-time shaping does what it says on costs.
- **Quality didn't hold**: c2_license_header failed 2/3 (the constraint
  requiring every created file to carry a header — plausibly hurt by
  gists replacing the early files the model would otherwise imitate),
  and one e1 run hit the watchdog. The recurring c2 weakness across all
  three engines suggests a task-hardness component, but the pre-commit
  doesn't allow that excuse.
- **The backstop budget (<0.2/task) was optimistic**: 0.6/task achieved
  is a 62% cut, not ~zero. The observed fall-throughs cluster on
  memory/constraint tasks where distillable mass is small — pressure
  arrives faster than content cools. A larger cooling window can't fix
  that; only pressure-independent shaping (smaller window, lower floor)
  or a raised threshold could, at unknown quality cost.
- Two watched items for any round 3: gist quality (first-120-chars is
  weak for structured content the model needs to imitate), and window/
  floor tuning against the timeout + c2 failures.

## Standing after two rounds

Compressor 29/30 @ 498.6k · Substrate 27/30 @ 483.1k · Cooling 27/30 @
442.8k. The trend line is real (tokens fall as management gets more
proactive) but quality has paid for it both times. The instrument and
the protocol are now cheap enough that a round 3 is an afternoon, not a
project — when there's a new idea for the quality side, not just knobs.
