"""compute_outcome_score — innovation #1 (recall-replay eval harness).

Pure proxy table-test. No DB, no async. The windowed-UPDATE writer
(``write_recall_outcome``) is exercised by the pg-backed
``tests/substrate/recall/test_recall_outcome.py``.
"""

from __future__ import annotations

import pytest

from agent.turn_outcome import compute_outcome_score


@pytest.mark.parametrize(
    "completed, failed, interrupted, expected",
    [
        # Clean completion → 1.0.
        (True, False, False, 1.0),
        # Explicit failure → 0.0 even if "completed" is also set.
        (True, True, False, 0.0),
        (False, True, False, 0.0),
        # Interrupted → 0.0.
        (True, False, True, 0.0),
        # Never completed → 0.0.
        (False, False, False, 0.0),
    ],
)
def test_base_proxy_table(completed, failed, interrupted, expected):
    assert (
        compute_outcome_score(
            completed=completed,
            failed=failed,
            interrupted=interrupted,
        )
        == expected
    )


def test_tool_failure_penalty_docks_a_clean_turn():
    # 2 of 4 tool calls failed → ratio 0.5; penalty 0.5 → dock 0.25 → 0.75.
    score = compute_outcome_score(
        completed=True,
        failed=False,
        interrupted=False,
        tool_calls=4,
        tool_failures=2,
        tool_failure_penalty=0.5,
    )
    assert score == pytest.approx(0.75)


def test_all_tools_failed_clamps_to_zero():
    # ratio 1.0, penalty 1.0 → 1.0 - 1.0 = 0.0 (no negative).
    score = compute_outcome_score(
        completed=True,
        failed=False,
        interrupted=False,
        tool_calls=3,
        tool_failures=3,
        tool_failure_penalty=1.0,
    )
    assert score == 0.0


def test_penalty_clamps_at_zero_not_negative():
    # A wild penalty must never push below 0.0.
    score = compute_outcome_score(
        completed=True,
        failed=False,
        interrupted=False,
        tool_calls=2,
        tool_failures=2,
        tool_failure_penalty=5.0,
    )
    assert score == 0.0


def test_no_tool_calls_leaves_clean_turn_at_one():
    # No tool calls → no penalty, even with the default penalty weight.
    score = compute_outcome_score(
        completed=True,
        failed=False,
        interrupted=False,
        tool_calls=0,
        tool_failures=0,
    )
    assert score == 1.0


def test_failed_turn_ignores_tool_penalty_math():
    # Already 0.0 from failure → tool counters can't make it negative.
    score = compute_outcome_score(
        completed=False,
        failed=True,
        interrupted=False,
        tool_calls=4,
        tool_failures=1,
    )
    assert score == 0.0


def test_partial_tool_failures_stay_in_range():
    # 1 of 5 failed → 1.0 - 0.5*0.2 = 0.9.
    score = compute_outcome_score(
        completed=True,
        failed=False,
        interrupted=False,
        tool_calls=5,
        tool_failures=1,
        tool_failure_penalty=0.5,
    )
    assert score == pytest.approx(0.9)
    assert 0.0 <= score <= 1.0
