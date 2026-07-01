# Demo — innovation: cost-aware Conductor (budget-governed cognition)

The substrate's Conductor now has a metabolic limit: it reads the crew's own
trailing-hour token spend from `substrate_agent_cost` and governs sub-agent
intensities on it. Proposal #3 in `INNOVATIONS.md`.

## How to run it

```bash
# Opt in — unset (the default) preserves prior behaviour exactly.
export THOTH_SUBSTRATE_HOURLY_TOKEN_BUDGET=200000   # tokens/hour for the whole crew
export THOTH_CONDUCTOR_BUDGET_SOFT_RATIO=0.8        # optional, default 0.8

thoth   # run normally; the Conductor ticks in the substrate worker
```

Watch it act:

```bash
thoth substrate conductor    # decision log — targets now reflect budget posture
thoth substrate health       # per-agent spend that feeds the governor
```

Behaviour:
- spend < 80% of budget → policy unchanged (backlog + coherence as before)
- spend ≥ 80% → escalation suppressed: HIGH dials cap to MODERATE, Dreamer pauses
- spend ≥ 100% → whole crew throttles to floor levels (Sentinel keeps its FULL
  floor via the registry) until the trailing hour drains

The spend + budget ratio are emitted in the `conductor.dialed` telemetry event.

## What works / verified

- `_compute_targets` budget layer (hard cap > coherence latch > backlog,
  soft-cap suppression, None-ratio passthrough): covered by 3 new pure unit
  tests — all pass (`tests/substrate/agents/test_adaptive_conductor.py`).
- All 8 pre-existing + new pure policy tests pass.
- DB-backed integration tests (spend read only when a budget is set, live
  tick under a blown budget dialing the crew to floor): pass against real
  Postgres — full suite 19/19
  (`uv run python -m pytest tests/substrate/agents/test_adaptive_conductor.py -q`).

## What's stubbed

Nothing. The change is self-contained: one signal read + a pure policy layer.

## Next increment

- Per-agent budgets (Dreamer vs Parser value-for-spend differ hugely).
- Steer on `latency_ms` (also already recorded) for slow aux endpoints.
- Surface budget posture in `thoth substrate` default summary.
