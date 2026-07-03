"""Context-engine grading suite — Phase 1 of ``plans/substrate-context-engine.md``.

The *primary instrument* for Goal 4 ("prove the substrate context engine >=
the current compressor"). Live A/B is nearly useless here — the live install
compressed once in 244 sessions — so the mechanism only shows up in
long-horizon sessions built by construction.

Three layers, split so the model-free pieces stay unit-testable:

- :mod:`eval.context_suite.tasks` — deterministic, seeded task fixtures plus
  OBJECTIVE oracles (parse the end state and diff it against ground truth
  regenerated from the fixtures; check probe answers; verify constraints held
  across the whole session). No model, no DB.
- :mod:`eval.context_suite.runner` — drives one task end-to-end against a real
  :class:`AIAgent` (constructed programmatically, workspace-scoped), threading
  turns like ``batch_runner`` and collecting per-task metrics. Timeouts and
  exceptions degrade to a failed ``TaskResult`` rather than crashing the suite.
- :mod:`eval.context_suite.run` — the ``python -m eval.context_suite.run`` CLI:
  ``--engine compressor`` vs ``--engine substrate`` on the same task set, same
  model. Writes JSONL rows + a ``summary.json`` and prints a readable table.

Only :mod:`~eval.context_suite.runner` imports the agent stack, and it does so
lazily inside its functions, so ``tasks`` and report aggregation import with no
heavyweight dependencies.
"""

from eval.context_suite.tasks import (  # noqa: F401  (re-export the stable surface)
    FAMILY_CONSTRAINT,
    FAMILY_END_STATE,
    FAMILY_MEMORY_PROBE,
    OracleResult,
    Probe,
    ProbeResult,
    Task,
    Transcript,
    get_tasks,
    list_tasks,
    snapshot_tree,
)

__all__ = [
    "FAMILY_CONSTRAINT",
    "FAMILY_END_STATE",
    "FAMILY_MEMORY_PROBE",
    "OracleResult",
    "Probe",
    "ProbeResult",
    "Task",
    "Transcript",
    "get_tasks",
    "list_tasks",
    "snapshot_tree",
]
