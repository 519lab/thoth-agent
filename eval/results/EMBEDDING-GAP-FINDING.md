# Finding: the grading harness has been measuring the proactive engine with its recall leg amputated (2026-07-05)

Discovered while smoke-testing round-5 expand nudges. The nudge is built,
tested, and correct — but it could not fire, and the root cause is
upstream of the nudge and affects **all four prior graded rounds**.

## Chain, proven on the real system

1. Nudge smoke on `m2_fixture_fact` (aggressive cooling): 6 eviction
   pointer slices minted, but `context.pagein = 0`, `context_expand`
   calls = 0, and **recalls composing pointers = 0**.
2. Recall this session: 5 recalls, 9 candidates, **0 composed** — the
   projection was essentially empty even of regular content.
3. Direct check: **every slice minted during grading has a NULL
   embedding** — context_evicted, tool_result, user_message, all of it
   (0 embedded of 63 fresh slices).
4. Cause: the grading harness attaches substrate in **writer mode**
   (`bootstrap_substrate_sync(mode="writer")`), which boots streams +
   hooks + recall-log writer but **does NOT start the Curator worker**
   that backfills embeddings. Compounding it, no embedding provider is
   configured in the eval environment (`auxiliary.embedding: NONE`),
   so even running the backfill inline would no-op without a real
   provider.
5. Consequence: semantic recall (`embedding <=> query`) can match
   nothing new; it falls back to keyword Jaccard, which rarely surfaces
   the eviction pointers. The proactive-recall leg — the design's
   differentiator over inert placeholders — has been **structurally
   dead in grading**, and the nudge (which parses recall-surfaced
   pointers) therefore never had input.

## Scope: eval-only, production is fine

Live install: 6,343 of 6,469 slices embedded (98%) — production runs
the Curator worker with a real embedding provider, so proactive recall
works there. The snapshot-seeded `thoth_baseline` retains embeddings on
its *pre-seeded* slices (from the live dump); only slices minted *during
grading* are NULL. So the harness has been under-measuring the
substrate/cooling engine's proactive benefit while production has it
intact.

## Implication for the four verdicts

Rounds 1-4 compared summarize-vs-proactive with the proactive engine's
recall surfacing crippled. The engine still tied-or-led on tokens,
backstops, and pass rate on its eviction/handle mechanics alone. Its
recall-and-page-back-in leg was never actually exercised. The round-4
"leads everywhere, certifies nothing" result stands, but the proactive
thesis has not yet had a fair test.

## The fork (Greg's call — needs an infra decision)

To make proactive recall testable in grading, the harness needs the
same embedding capability production has:

- **Option A — real embedding provider in the eval.** Point the harness
  at an embeddings endpoint (Ollama `nomic-embed-text` locally, or
  OpenAI `text-embedding-3-small`, or whatever the live worker uses) and
  run the Curator embedding-backfill inline between turns (call
  `_maybe_emit_embeddings`, which already exists). Most production-
  faithful; requires choosing/wiring the provider.
- **Option B — embed eviction pointers synchronously at mint.** Product
  change: the one slice type whose entire value is same-session semantic
  recall gets embedded at commit instead of deferred to the async
  Curator. Helps production too (fresh gateway writer before the worker
  spins up). Still needs a provider, but scoped to the hot pointers.
- **Option C — run the Curator worker alongside the harness** (the
  `thoth substrate worker run` process against the grading DB) so the
  eval mirrors production exactly. Heaviest, most faithful.

Recommendation: A (provider + inline backfill) for a faithful round-5
re-grade, and B as a standalone product improvement regardless.

The expand-nudge itself is committed (`feat/context-expand-nudges`,
merged to `round4/integration`); it's a dormant no-op until recall
surfaces pointers, so it does no harm while this is resolved.
