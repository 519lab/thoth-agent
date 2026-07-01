# Demo — innovation: learned recall weights

Thoth's memory recall now literally learns from use: an offline tuner fits the
ranking weights (salience / recency / half-life) against the outcome-labelled
recall log, validates on a held-out newest split, and — behind guardrails —
promotes the fitted vector into the live recall path. Proposal #1 in
`INNOVATIONS.md`; closes the loop the report-only replay harness
(innovation #1 of the previous round) deliberately left open.

## How to run it

```bash
uv run python -m alembic -c migrations/alembic.ini upgrade head   # adds substrate_recall_weights

# After some normal use with recall + outcome labelling on (both default ON):
thoth substrate recall tune              # fit + verdict, applies nothing
thoth substrate recall tune --apply      # promote (only if every guardrail passes)
thoth substrate recall weights           # audit trail: history + which set is ACTIVE
thoth substrate recall weights --revert  # back to the config/env baseline
```

The live path picks up a promotion/revert within
`THOTH_RECALL_TUNED_WEIGHTS_TTL_S` (default 300 s) — no restart, which also
makes recall weights the first substrate knob adjustable mid-process.
Resolution order per field: explicitly-set `THOTH_RECALL_*_WEIGHT` env var
(operator hand override) → active tuned row → config default.
Kill-switch: `THOTH_RECALL_TUNED_WEIGHTS=0`.

Guardrails (all must pass before `--apply` does anything): ≥ 50 labelled
recalls (`THOTH_RECALL_TUNE_MIN_CORPUS`), both kept/dropped holdout
populations ≥ 5, holdout separation improvement ≥ 0.01 over baseline, and the
search is boxed to 0.25×–4× of baseline per weight (a tune, not a redesign).
A young install gets "don't apply, here's why" — never weights fit on noise.

## What works / verified

- Pure tuner (time-ordered split, coordinate descent recovering a planted
  better vector, guardrail refusals, bounds, determinism): 6 tests, pass.
- Live-path resolution (tuned row overrides config, env var outranks tuned,
  kill-switch, failed-read degradation): 5 tests, pass.
- Pre-existing replay + ranking suites still pass (26 total in the run).
- Imports, `alembic heads` (single head `20260701_0026`), and both new CLI
  help surfaces verified.
- PG-backed persistence tests (`test_recall_weights_store.py`: single-active
  invariant, revert, corrupt-row decode, history): pass against real
  Postgres. Full recall package green — 138 passed, 1 pre-existing skip
  (`uv run python -m pytest tests/substrate/recall/ -q`). The DB run also
  caught and fixed a real integration gap: the new migration had to be
  registered in `substrate/facade.py::_EXPECTED_REVISIONS` or boot would
  reject freshly-upgraded databases.

## What's stubbed

Nothing stubbed. Scope-limited by design: similarity/keyword weights stay at
baseline (the logged `relevance` is a fixed term, so the corpus carries no
cross-path re-ranking signal for them — same reasoning as the replay grid).

## Next increment

- An auto-tune worker on the Curator cadence (propose + notify, never
  auto-apply) once CLI-driven tuning has built trust.
- Per-slice graded labels (skill-evaluator verdicts) to upgrade the v1
  separation objective toward real NDCG.
- Use the same corpus to validate `RECALL_RERANK` / `RECALL_L3L4_SEMANTIC`,
  which have been waiting on exactly this evidence loop.
- Show the active tuned set in `thoth substrate recall config`.
