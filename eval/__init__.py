"""Thoth evaluation harnesses.

Home for graded, objective evaluation suites that measure agent behaviour with
mechanically checkable oracles rather than "the run didn't crash". The first
inhabitant is :mod:`eval.context_suite`, the long-horizon context-engine grading
suite of ``plans/substrate-context-engine.md`` (Phase 1).

Kept deliberately dependency-light and importable without a database or a model
client so the pure pieces (task fixtures, oracles, report aggregation) stay
unit-testable in isolation.
"""
