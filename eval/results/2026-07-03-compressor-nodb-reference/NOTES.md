# Compressor reference run — 2026-07-03 (no-DB mode)

Harness-validation + compressor reference: 10 tasks x 3 runs against the
live install's model (Qwen3.6-35B-MTP @ unsloth-rig, provider custom),
engine=compressor, compress threshold 50k, NO --pg-dsn (session store and
substrate detached — the compressor doesn't use either, so its behavior
is DB-independent modulo latency).

Headline: pass 29/30 (96.7%), mean outcome 0.959, 390k mean prompt
tokens/task, cache hit 78.8%, 1.07 compressions/task, 0 harness errors.
The one failure: c1_protected_zone run 0 — the model modified protected/
after context pressure (the ConstraintRot failure mode; passed in runs
1-2, so ~1/3 flake under this engine/model).

This is NOT the official Phase-3 arm. The pre-committed A/B (plan §4)
runs BOTH engines DB-backed against the snapshot-seeded thoth_baseline
(see README "DB-backed grading"), because the substrate engine's
mechanisms require the session store + substrate. Keep this as the
sanity reference and harness-validation record.
