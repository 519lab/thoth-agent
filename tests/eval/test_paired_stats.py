"""Unit tests for ``eval.context_suite.paired_stats`` — NO model calls, NO DB.

All test data is synthetic JSONL built in-process (see ``_write_jsonl`` /
``_rows``); nothing here runs the grading suite itself. Every scenario is
fully deterministic: both the synthetic data generation and the analysis RNG
are seeded, so there is no reliance on "should pass most of the time" — a
statistical claim in a test would be flaky, so each assertion follows
directly from a fixed computation instead.

Covers (plan): synthetic true-effect -> WIN; identical arms -> INCONCLUSIVE;
one flaky task -> INCONCLUSIVE; BCa sanity (CI contains the mean, degenerate
all-equal deltas don't crash); pairing (mismatched run counts warn + analyze
the intersection; missing metric skipped gracefully); determinism (same seed
-> identical report dict); lower-is-better direction respected for tokens.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from eval.context_suite import paired_stats as ps

# Small-ish resample count keeps the whole file well under the 30s cap while
# still exercising the real BCa + permutation machinery (not a stub).
RESAMPLES = 2000


def _row(
    task_id: str,
    run_index: int,
    *,
    passed: bool = True,
    mean_outcome: float = 0.7,
    prompt_tokens: Optional[float] = 10_000.0,
    compression_count: Optional[int] = 2,
    duration_s: Optional[float] = 50.0,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "task_id": task_id,
        "run_index": run_index,
        "passed": passed,
        "mean_outcome": mean_outcome,
        "duration_s": duration_s,
        "compression_count": compression_count,
    }
    if prompt_tokens is not None:
        row["tokens"] = {"prompt_tokens": prompt_tokens}
    return row


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> Path:
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


TASKS = [f"t{i}" for i in range(10)]
RUNS = range(3)


# --------------------------------------------------------------------------- #
# Synthetic generator: true +0.2 outcome effect on every pair -> WIN          #
# --------------------------------------------------------------------------- #


def test_true_positive_effect_wins_with_tiny_p(tmp_path):
    baseline = [_row(t, r, mean_outcome=0.6) for r in RUNS for t in TASKS]
    variant = [_row(t, r, mean_outcome=0.8) for r in RUNS for t in TASKS]
    bpath = _write_jsonl(tmp_path / "results_baseline_1.jsonl", baseline)
    vpath = _write_jsonl(tmp_path / "results_variant_1.jsonl", variant)

    report = ps.build_report(bpath, vpath, metrics=["mean_outcome"], resamples=RESAMPLES, seed=42)
    m = report["metrics"][0]
    assert m["n_pairs"] == 30
    assert m["mean_delta"] == pytest.approx(0.2)
    assert m["ci_lo"] > 0
    assert m["p_value"] < 0.01
    assert m["verdict"] == "WIN"


def test_identical_arms_are_inconclusive(tmp_path):
    # Independent per-row noise drawn from the SAME distribution on both
    # arms: a real (non-degenerate) spread of deltas whose true mean is 0.
    rng_b = random.Random(7)
    rng_v = random.Random(99)
    baseline = [_row(t, r, mean_outcome=0.7 + rng_b.uniform(-0.05, 0.05)) for r in RUNS for t in TASKS]
    variant = [_row(t, r, mean_outcome=0.7 + rng_v.uniform(-0.05, 0.05)) for r in RUNS for t in TASKS]
    bpath = _write_jsonl(tmp_path / "results_baseline_2.jsonl", baseline)
    vpath = _write_jsonl(tmp_path / "results_variant_2.jsonl", variant)

    report = ps.build_report(bpath, vpath, metrics=["mean_outcome"], resamples=RESAMPLES, seed=42)
    m = report["metrics"][0]
    assert m["verdict"] == "INCONCLUSIVE"
    assert m["p_value"] > 0.05
    assert m["ci_lo"] < 0 < m["ci_hi"]


def test_one_flaky_task_is_inconclusive(tmp_path):
    # 9 tasks tie exactly; one task ("t0") is flaky — its 3 runs differ by a
    # small, non-constant amount. The overall signal must stay noise-level.
    baseline = []
    variant = []
    flaky_deltas = {0: 0.3, 1: -0.2, 2: 0.1}
    for r in RUNS:
        for t in TASKS:
            baseline.append(_row(t, r, mean_outcome=0.7))
            bump = flaky_deltas[r] if t == "t0" else 0.0
            variant.append(_row(t, r, mean_outcome=0.7 + bump))
    bpath = _write_jsonl(tmp_path / "results_baseline_3.jsonl", baseline)
    vpath = _write_jsonl(tmp_path / "results_variant_3.jsonl", variant)

    report = ps.build_report(bpath, vpath, metrics=["mean_outcome"], resamples=RESAMPLES, seed=42)
    m = report["metrics"][0]
    assert m["n_pairs"] == 30
    assert m["verdict"] == "INCONCLUSIVE"
    assert m["p_value"] > 0.05


# --------------------------------------------------------------------------- #
# BCa sanity                                                                  #
# --------------------------------------------------------------------------- #


def test_bca_ci_contains_the_observed_mean():
    rng = random.Random(123)
    deltas = [rng.gauss(0.15, 0.4) for _ in range(30)]
    mean_delta = sum(deltas) / len(deltas)
    ci_lo, ci_hi = ps.bca_ci(deltas, RESAMPLES, ps.CI_ALPHA, random.Random(1))
    assert ci_lo <= mean_delta <= ci_hi


def test_bca_degenerate_all_equal_deltas_zero_no_crash():
    deltas = [0.0] * 30
    ci_lo, ci_hi = ps.bca_ci(deltas, RESAMPLES, ps.CI_ALPHA, random.Random(1))
    assert ci_lo == ci_hi == 0.0
    p = ps.sign_flip_p_value(deltas, RESAMPLES, random.Random(1))
    assert p == 1.0


def test_bca_degenerate_all_equal_deltas_nonzero_no_crash():
    # Every pair ties at a nonzero constant: CI collapses to that point, and
    # the sign-flip test still finds it significant (flipping any subset of
    # a nonzero constant moves the mean away from it).
    deltas = [0.3] * 30
    ci_lo, ci_hi = ps.bca_ci(deltas, RESAMPLES, ps.CI_ALPHA, random.Random(1))
    assert ci_lo == ci_hi == pytest.approx(0.3)
    p = ps.sign_flip_p_value(deltas, RESAMPLES, random.Random(1))
    assert p < 0.05


def test_bca_single_pair_no_crash():
    ci_lo, ci_hi = ps.bca_ci([0.42], RESAMPLES, ps.CI_ALPHA, random.Random(1))
    assert ci_lo == ci_hi == 0.42


def test_bca_empty_no_crash():
    ci_lo, ci_hi = ps.bca_ci([], RESAMPLES, ps.CI_ALPHA, random.Random(1))
    assert ci_lo != ci_lo and ci_hi != ci_hi  # both NaN


# --------------------------------------------------------------------------- #
# Pairing                                                                     #
# --------------------------------------------------------------------------- #


def test_mismatched_run_counts_warn_and_analyze_intersection(tmp_path):
    # Baseline has 3 tasks x 3 runs (9 rows); variant only has runs 0-1 for
    # the same 3 tasks (6 rows) plus an extra task baseline never ran.
    baseline = [_row(t, r) for t in ["a", "b", "c"] for r in range(3)]
    variant = [_row(t, r) for t in ["a", "b", "c"] for r in range(2)]
    variant.append(_row("extra_task", 0))
    bpath = _write_jsonl(tmp_path / "results_baseline_4.jsonl", baseline)
    vpath = _write_jsonl(tmp_path / "results_variant_4.jsonl", variant)

    pairs, warnings = ps.pair_rows(baseline, variant)
    assert len(pairs) == 6  # 3 tasks x runs {0,1}
    assert any("baseline row(s) have no variant match" in w for w in warnings)
    assert any("variant row(s) have no baseline match" in w for w in warnings)

    report = ps.build_report(bpath, vpath, metrics=["mean_outcome"], resamples=RESAMPLES, seed=42)
    assert report["n_pairs"] == 6
    assert len(report["warnings"]) == 2


def test_missing_metric_skipped_gracefully(tmp_path):
    baseline = [_row(t, r, compression_count=None) for r in RUNS for t in TASKS]
    variant = [_row(t, r, compression_count=None) for r in RUNS for t in TASKS]
    # _row always sets the key (possibly to None); simulate a genuinely
    # absent field as some real engines would emit.
    for row in baseline + variant:
        del row["compression_count"]
    bpath = _write_jsonl(tmp_path / "results_baseline_5.jsonl", baseline)
    vpath = _write_jsonl(tmp_path / "results_variant_5.jsonl", variant)

    report = ps.build_report(
        bpath, vpath, metrics=["compression_count"], resamples=RESAMPLES, seed=42
    )
    m = report["metrics"][0]
    assert m["n_pairs"] == 0
    assert m["verdict"] == "SKIPPED"
    assert "note" in m


# --------------------------------------------------------------------------- #
# Determinism                                                                 #
# --------------------------------------------------------------------------- #


def test_same_seed_yields_identical_report(tmp_path):
    baseline = [_row(t, r, mean_outcome=0.6) for r in RUNS for t in TASKS]
    variant = [_row(t, r, mean_outcome=0.75) for r in RUNS for t in TASKS]
    bpath = _write_jsonl(tmp_path / "results_baseline_6.jsonl", baseline)
    vpath = _write_jsonl(tmp_path / "results_variant_6.jsonl", variant)

    r1 = ps.build_report(bpath, vpath, resamples=RESAMPLES, seed=42)
    r2 = ps.build_report(bpath, vpath, resamples=RESAMPLES, seed=42)
    assert r1 == r2


def test_different_seed_can_differ_but_same_metric_subset_is_stable(tmp_path):
    # Analyzing a subset of metrics must reproduce the same numbers as
    # analyzing all of them (each metric's RNG is independent of the others).
    baseline = [_row(t, r, mean_outcome=0.6) for r in RUNS for t in TASKS]
    variant = [_row(t, r, mean_outcome=0.75) for r in RUNS for t in TASKS]
    bpath = _write_jsonl(tmp_path / "results_baseline_7.jsonl", baseline)
    vpath = _write_jsonl(tmp_path / "results_variant_7.jsonl", variant)

    full = ps.build_report(bpath, vpath, resamples=RESAMPLES, seed=42)
    subset = ps.build_report(bpath, vpath, metrics=["mean_outcome"], resamples=RESAMPLES, seed=42)
    full_outcome = next(m for m in full["metrics"] if m["metric"] == "mean_outcome")
    assert full_outcome == subset["metrics"][0]


# --------------------------------------------------------------------------- #
# Lower-is-better direction (tokens, compression, duration)                  #
# --------------------------------------------------------------------------- #


def test_lower_is_better_direction_for_tokens(tmp_path):
    baseline = [_row(t, r, prompt_tokens=10_000.0) for r in RUNS for t in TASKS]
    variant = [_row(t, r, prompt_tokens=8_000.0) for r in RUNS for t in TASKS]  # fewer = better
    bpath = _write_jsonl(tmp_path / "results_baseline_8.jsonl", baseline)
    vpath = _write_jsonl(tmp_path / "results_variant_8.jsonl", variant)

    report = ps.build_report(bpath, vpath, metrics=["prompt_tokens"], resamples=RESAMPLES, seed=42)
    m = report["metrics"][0]
    assert m["direction"] == "lower"
    assert m["mean_delta"] == pytest.approx(-2000.0)
    assert m["ci_hi"] < 0
    assert m["p_value"] < 0.05
    assert m["verdict"] == "WIN"  # variant used fewer tokens: WIN for a lower-is-better metric


def test_lower_is_better_direction_reversed_is_a_loss(tmp_path):
    baseline = [_row(t, r, duration_s=50.0) for r in RUNS for t in TASKS]
    variant = [_row(t, r, duration_s=90.0) for r in RUNS for t in TASKS]  # slower = worse
    bpath = _write_jsonl(tmp_path / "results_baseline_9.jsonl", baseline)
    vpath = _write_jsonl(tmp_path / "results_variant_9.jsonl", variant)

    report = ps.build_report(bpath, vpath, metrics=["duration_s"], resamples=RESAMPLES, seed=42)
    m = report["metrics"][0]
    assert m["direction"] == "lower"
    assert m["ci_lo"] > 0
    assert m["verdict"] == "LOSS"


# --------------------------------------------------------------------------- #
# Loading: file vs directory (newest results_*.jsonl wins)                   #
# --------------------------------------------------------------------------- #


def test_load_results_from_directory_picks_newest_file(tmp_path):
    import os
    import time

    old = _write_jsonl(tmp_path / "results_old_20260101_000000.jsonl", [_row("a", 0)])
    new = _write_jsonl(tmp_path / "results_new_20260102_000000.jsonl", [_row("b", 0)])
    # Force an unambiguous mtime ordering regardless of filesystem clock granularity.
    now = time.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now, now))

    rows, resolved = ps.load_results(tmp_path)
    assert resolved == new
    assert rows == [_row("b", 0)]


def test_load_results_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ps.load_results(tmp_path / "does_not_exist")


# --------------------------------------------------------------------------- #
# Unknown metric guard                                                        #
# --------------------------------------------------------------------------- #


def test_unknown_metric_raises(tmp_path):
    baseline = [_row("a", 0)]
    variant = [_row("a", 0)]
    bpath = _write_jsonl(tmp_path / "results_baseline_10.jsonl", baseline)
    vpath = _write_jsonl(tmp_path / "results_variant_10.jsonl", variant)
    with pytest.raises(ValueError):
        ps.build_report(bpath, vpath, metrics=["not_a_real_metric"])
