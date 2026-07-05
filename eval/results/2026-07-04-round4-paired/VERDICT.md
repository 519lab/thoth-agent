# Round 4 verdict — first PAIRED comparison at 5 runs/arm (2026-07-04)

Both arms carried the round-4 build: working-set recitation (engine-
agnostic), engine repairs A-E, slimmed gists. Arms re-seeded from the
same snapshot; 50 task-runs each; mechanism-verified (290 eviction
slices in arm B; 0 pagein events — expand-nudges were deliberately not
built this round). Verdict computed by paired_stats (BCa bootstrap +
sign-flip permutation, dual gate).

| metric | compressor+WS | cooling+WS | paired delta | 95% CI | p | verdict |
|---|---|---|---|---|---|---|
| pass rate | 47/50 (94%) | **48/50 (96%)** | +0.02 | [-0.08, +0.08] | 1.00 | INCONCLUSIVE |
| mean outcome | 0.9434 | 0.9311 | -0.012 | [-0.036, +0.010] | 0.30 | INCONCLUSIVE |
| prompt tokens/task | 441.4k | **400.3k (-9.3%)** | -41.1k | [-119.1k, +31.0k] | 0.29 | INCONCLUSIVE |
| backstop firings/task | 1.08 | **0.66 (-39%)** | -0.42 | [-1.46, **-0.02**] | 0.21 | INCONCLUSIVE* |
| duration/task | — | **-29s** | -29.0 | [-67.0, +1.6] | 0.096 | INCONCLUSIVE |

*CI entirely below zero, but the dual gate also requires p<0.05.

Failures — arm A: e2 r0, e2 r1, e1 r4. Arm B: c2 r1, e2 r2. All oracle
near-misses; e2 remains the flakiest task in the suite (4 of 5 total
failures across both arms are e2/e1 completeness misses).

## Decision (pre-committed dual gate)

**No certified win — the compressor formally remains the default.** No
metric passed both the CI and permutation gates at n=50 pairs.

## Honest reading — the picture has flipped

For the first time across four rounds, the challenger leads on pass rate
AND every efficiency metric simultaneously: +1 task passed, -9.3%
tokens, -39% backstop summarizations, -29s wall per task. Three metrics
sit near the significance boundary (backstop CI excludes zero;
duration p=0.096). The one negative point estimate (mean outcome
-0.012) coexists with a higher pass rate — it reflects tool-failure
penalty noise, not task failures.

Under rounds 1-3's unpaired eyeballing this would have been declared a
win. The paired protocol says: promising, unproven at this n. Power
analysis from the observed deltas suggests ~10-15 runs/arm would
certify the backstop and duration effects if they are real; the token
effect needs either more runs or lower variance (e1's heavy tail —
one 1.18M-token run — dominates the CI width).

## Options from here

1. Hold the pre-commit line: keep compressor, run 10-15 runs/arm when
   rig time is cheap to certify or kill the near-boundary effects.
2. Owner's discretion: adopt cooling as default on preponderance of
   evidence (leads everywhere, mechanically sound, zero degradation
   errors across 4 rounds), keeping the kill-switches.
3. Build the remaining gap-scan lever first (decision-time expand
   nudges — pagein is still 0) and grade once, powered.

## Standing after four rounds

The proactive system now demonstrably: evicts restorable content with
byte-exact handles (290 slices), never corrupts a pass (0 errors, all
invariants held across ~250 graded task-runs), cuts summarization
events ~40-60%, trends -9% tokens, and ties-or-beats pass rate. What it
has never done: get a model to dereference a handle unprompted, or
clear a pre-committed statistical bar.
