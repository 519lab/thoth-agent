# Context-engine grading suite (Phase 1)

The **primary instrument** for Goal 4 of `plans/substrate-context-engine.md`:
prove a candidate context engine performs **≥ the current compressor** on
long-horizon sessions. Live A/B is nearly useless here — the live install
compressed *once in 244 sessions* — so this suite manufactures the long-horizon
pressure by construction and grades the result with **objective oracles**
(neither `batch_runner` nor `mini_swe_runner` has one; today "success" just
means "didn't crash").

## What's in the box

- **10 tasks** across three families (`tasks.py`), each multi-turn, deterministic
  (seeded — two runs write byte-identical fixtures), and **large by
  construction** (~58–99k tokens of readable fixtures, so real compression
  fires):
  - **End-state (5)** — mechanically checkable file engineering. The oracle
    regenerates ground truth from the (unchanged) fixtures and diffs the agent's
    output: merge many `.env` → `merged.json`; TODO census → `SUMMARY.md` with
    `file:line`; rename a JSON key across many files; CSV → `records.json`;
    dedup+sort a big log.
  - **Memory-probe (3)** — a fact is established in an early turn (user statement
    or an early-read file), then heavy middle work applies context pressure, then
    a late turn needs the fact. The oracle regexes the **final answer** for it —
    comprehension *after* compression (plan §4 "probe questions").
  - **Constraint-survival (2)** — turn 1 states a standing rule ("never modify
    `protected/`", "every created file starts with header X"); later turns pile
    on pressure that would naturally violate it. The oracle inspects the whole
    workspace + transcript to prove the rule held across the entire session
    (the ConstraintRot protocol).
- **`runner.py`** — drives one task against a real `AIAgent`, workspace-scoped,
  threading turns like `batch_runner`, collecting per-task metrics. Timeouts and
  exceptions become a *failed* `TaskResult`, never a crash. **Never touches a
  database** (`skip_memory=True`, no `session_db`).
- **`run.py`** — the CLI.

## Running it

```bash
# See the task set / validate the oracles cheaply (NO model, NO tokens):
python -m eval.context_suite.run --list
python -m eval.context_suite.run --dry-run

# Baseline (current compressor):
python -m eval.context_suite.run --engine compressor \
    --model <model> --base-url <url> --api-key <key> \
    --runs 3 --out eval/results

# Candidate engine — SAME tasks, SAME model, flip --engine:
python -m eval.context_suite.run --engine substrate \
    --model <model> --base-url <url> --api-key <key> \
    --runs 3 --out eval/results
```

A/B = those last two invocations. Compare the two `summary_*.json` files (or the
per-engine block each prints). `--runs N` repeats every task for variance.
Filter with `--tasks e1_merge_env_to_json m1_user_stated_facts …`.

### DB-backed grading (required for the substrate engine)

The substrate engine's differentiators — durable eviction handles, eviction
slices, proactive recall — only exist with a session store and a booted
substrate. Without `--pg-dsn` it silently degrades to Tier-2 (summarize-only)
and the A/B measures nothing. The official comparison therefore runs BOTH arms
DB-backed, against the snapshot-seeded grading database:

```bash
# 1. (Re-)seed the grading DB from the newest valid nightly dump (test
#    cluster :5433; refuses live 5432 and 0-byte failed backups):
scripts/seed-context-baseline-db.sh

# 2. Arm A — compressor, DB-backed:
python -m eval.context_suite.run --engine compressor \
    --pg-dsn postgresql://thoth:thoth@localhost:5433/thoth_baseline \
    --model <model> --base-url <url> --api-key <key> --runs 3 --out eval/results

# 3. RE-SEED so arm B starts from the identical snapshot (suite runs write
#    sessions/slices into the grading DB — that's the point, but fairness
#    demands identical starting state):
scripts/seed-context-baseline-db.sh

# 4. Arm B — substrate engine, same tasks/model/snapshot:
python -m eval.context_suite.run --engine substrate \
    --pg-dsn postgresql://thoth:thoth@localhost:5433/thoth_baseline \
    --model <model> --base-url <url> --api-key <key> --runs 3 --out eval/results
```

One process = one DSN (`attach_db` binds the pool once and refuses port 5432).
The no-DSN mode remains for harness smoke tests and compressor-only checks.

### Unit tests (no model, no DB)

```bash
uv run --no-sync python -m pytest tests/eval/ -q
```

## The engine-selection seam

The runner selects the engine **per `AIAgent`** via the `THOTH_CONTEXT_ENGINE`
environment variable (set/reset around each construction). This is the single
tiny change to existing code: `agent/agent_init.py` now reads
`THOTH_CONTEXT_ENGINE` before the `context.engine` config value (env wins when
set; config remains the default). It's the plan §5 "config seam used during
development/grading only" — no permanent selector, deleted once the engine wins.

## Forcing compression

The real model window (e.g. 200k) would need absurd fixtures to trigger
compression. Instead the runner shrinks the built engine's `threshold_tokens`
(default `--compress-threshold-tokens 50000`) *after* construction, so
compression fires once a turn's prompt crosses the budget — the fixtures
(~58–99k readable tokens) comfortably exceed it. Pass `0` to use the model's
real window. `THOTH_EVAL_FIXTURE_SCALE` (default `1.0`) scales fixture sizes for
cheap smoke runs.

## Metrics → the pre-committed success criteria (plan §4)

Each task-run (one JSONL row) records: oracle pass/fail + details; per-turn
`outcome_score` (from `compute_outcome_score` on completed/failed/interrupted +
tool_calls/tool_failures) and its mean; token totals (prompt / completion /
cache read / cache write / reasoning / total) and cost from the agent's
`session_*` accumulators; `compression_count`; api_calls; wall duration; and
probe results. `summary.json` aggregates per-task, per-engine, and overall.

| Plan §4 goal | Metric here | Field |
|---|---|---|
| **Task success ≥ baseline** | oracle pass rate | `per_engine[*].pass_rate` |
| **Equal-or-higher memory perf** | mean `outcome_score` | `mean_outcome`, `per_engine[*].mean_outcome` |
| **Efficient token usage (cost per task ≤ baseline)** | **cache-adjusted** cost/task (raw tokens mislead under prompt caching) | `per_engine[*].mean_cost_per_task` |
| **Token savings (maybe)** | gross prompt tokens/task + cache hit rate | `mean_prompt_tokens_per_task`, `cache_hit_rate` |
| **Better comprehension** | memory-probe pass rate (recall after compression) | memory-family rows' `passed` |
| **Constraint survival** | constraint-family pass rate (rule held all session) | constraint-family rows' `passed` |
| **How the model coped** | probes: `reactive_pagein` (did it page evicted content back in?), `protected_write_attempt` | `probes[*]` |
| **Mechanism actually engaged** | compressions per task | `mean_compressions_per_task` |

**Go/no-go (plan §4, pre-committed):** the candidate replaces the compressor iff,
on the same seed: `pass_rate ≥` baseline, `mean_cost_per_task ≤` baseline,
memory/constraint pass rates `>` baseline, and no tail-case regression
(substrate-down runs ≥ compressor baseline, since Tier 2 *is* the compressor).
If it loses, keep the telemetry, delete the engine.

## Safety

The suite writes nothing to Postgres — not the live instance (5432) nor the test
instance (5433). It runs entirely on throwaway temp workspaces with
`skip_memory=True`. (Phase 1 per plan §5 is the *grading* system; seeding the
test instance from a nightly-backup snapshot for a live-scale substrate is a
separate operational step, not performed by this harness.)
