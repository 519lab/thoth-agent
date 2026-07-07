"""Paired-statistics analysis for the context-engine grading suite.

WHY THIS EXISTS
----------------
The grading suite (``eval.context_suite``) runs 10 tasks x 3 run_index each —
30 rows per arm. At that scale, a single flaky task (one timeout, one model
hiccup) moves the raw pass rate by ~3.3 points. Comparing *unpaired* pass
rates between a baseline arm and a variant arm is therefore close to
meaningless: the published protocol this module implements (arXiv 2511.19794,
"When +1% Is Not Enough") shows that single-run deltas of +0.5-2 points at
k=3 runs routinely get permutation p ~= 0.25 — indistinguishable from noise,
even though the raw numbers "look like a win". The 2026-07-03 substrate A/B
in this repo (``eval/results/2026-07-03-ab-v2/VERDICT.md``) hit exactly this
problem: a 2-of-30 task swing was reported as a loss with only prose hedging
("weak statistical evidence... binomially plausible under equal true
rates") because there was no rigorous instrument to quantify it.

The remedy applied here:

1. PAIR baseline and variant rows on ``(task_id, run_index)`` rather than
   comparing arm-level aggregates. Pairing cancels out per-task difficulty
   variance that would otherwise dominate an unpaired comparison — the same
   task/run pair ran under both arms, so the delta isolates the arm effect.
2. Analyze the resulting per-pair DELTAS (variant - baseline), never the raw
   arm values.
3. Gate the verdict on TWO independent tools, both computed only from the
   deltas, both distribution-free (no normality assumption on the raw
   metric):
   - A BCa (bias-corrected and accelerated) bootstrap confidence interval on
     the mean delta. BCa corrects the naive percentile bootstrap for both
     bias (the bootstrap distribution's median need not equal the observed
     statistic) and skew (the correction rate need not be symmetric) — both
     of which are common with n=30 and metrics like ``compression_count``
     that pile up at 0.
   - A sign-flip (a.k.a. paired-permutation) test: under the null hypothesis
     that baseline and variant are exchangeable *within each pair*, flipping
     the sign of any pair's delta is equally likely. Enumerating (here:
     Monte-Carlo sampling) the sign-flip distribution of the mean gives an
     exact-under-the-null p-value with no distributional assumptions at all.

WORKED INTERPRETATION EXAMPLE (this is the failure mode this tool fixes):
    pass-rate delta of -0.067 (2/30 tasks) with p=0.24 -> INCONCLUSIVE, not a
    loss. The raw numbers say "variant lost 2 tasks" but the paired
    permutation test says that swing is well within what 30 paired
    Bernoulli-ish trials produce by chance alone under the null of no true
    effect — reporting it as a "loss" (as an unpaired read would) overstates
    the evidence. Only report LOSS when the CI *and* the permutation test
    agree the effect has a sign, at the 95% / p<0.05 level.

VERDICT TABLE
-------------
Each metric has a declared "goodness direction" (see ``METRIC_DIRECTIONS``):
higher-is-better (``passed``, ``mean_outcome``) or lower-is-better
(``prompt_tokens``, ``compression_count``, ``duration_s``). The verdict per
metric is:

    higher-is-better:  WIN  if ci_lo > 0 and p < 0.05
                        LOSS if ci_hi < 0 and p < 0.05
    lower-is-better:   WIN  if ci_hi < 0 and p < 0.05   (variant used less)
                        LOSS if ci_lo > 0 and p < 0.05   (variant used more)
    otherwise:         INCONCLUSIVE (both CI and p are still reported)

DETERMINISM
-----------
Every random draw (bootstrap resamples, sign flips) comes from a
``random.Random`` seeded per-metric from ``(seed, metric_name)`` (default
seed 42), so a report is byte-for-byte reproducible across runs and
independent of which subset of metrics was requested.

STDLIB ONLY: json, math, random, argparse, pathlib, statistics. No numpy,
no scipy — the BCa machinery (inverse-normal CDF, jackknife acceleration)
is implemented from scratch below.

CLI
---
    python -m eval.context_suite.paired_stats BASELINE VARIANT \\
        [--metric passed mean_outcome ...] [--resamples 10000] [--seed 42] \\
        [--json out.json]

``BASELINE``/``VARIANT`` are each either a single ``results_*.jsonl`` file,
or a directory containing one or more of them (the newest by mtime is used).
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Metric declarations                                                        #
# --------------------------------------------------------------------------- #

#: Direction of "goodness" for each analyzed metric. ``passed`` (0/1) and
#: ``mean_outcome`` are higher-is-better; token/compression/duration cost
#: metrics are lower-is-better. Declared once here so the verdict logic never
#: has to guess.
METRIC_DIRECTIONS: Dict[str, str] = {
    "passed": "higher",
    "mean_outcome": "higher",
    "prompt_tokens": "lower",
    "compression_count": "lower",
    "duration_s": "lower",
}

#: Default metric set analyzed when ``--metric`` is not passed on the CLI.
DEFAULT_METRICS: Tuple[str, ...] = (
    "passed",
    "mean_outcome",
    "prompt_tokens",
    "compression_count",
    "duration_s",
)

CI_ALPHA = 0.05  # 95% CI
_P_LOW = 0.05  # significance threshold for the sign-flip permutation test


# --------------------------------------------------------------------------- #
# Loading + pairing (judge-agnostic: only task_id/run_index are required)     #
# --------------------------------------------------------------------------- #


def load_results(source: str | Path) -> Tuple[List[Dict[str, Any]], Path]:
    """Load one results source into a list of row dicts.

    ``source`` is either a single JSONL file, or a directory — in which case
    every ``results_*.jsonl`` file in that directory is considered and the
    newest one (by mtime) is used. Returns ``(rows, resolved_file_path)``.
    """
    path = Path(source)
    if path.is_dir():
        candidates = sorted(path.glob("results_*.jsonl"), key=lambda f: f.stat().st_mtime)
        if not candidates:
            raise FileNotFoundError(
                f"No results_*.jsonl files found in directory: {path}"
            )
        resolved = candidates[-1]
    elif path.is_file():
        resolved = path
    else:
        raise FileNotFoundError(f"Results source not found: {path}")

    rows: List[Dict[str, Any]] = []
    with resolved.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows, resolved


def _row_key(row: Dict[str, Any]) -> Tuple[Any, Any]:
    return (row.get("task_id"), row.get("run_index"))


def pair_rows(
    baseline_rows: Sequence[Dict[str, Any]],
    variant_rows: Sequence[Dict[str, Any]],
) -> Tuple[List[Tuple[Dict[str, Any], Dict[str, Any]]], List[str]]:
    """Pair rows on ``(task_id, run_index)``.

    Returns ``(pairs, warnings)``. Unpaired leftovers (rows present in one
    arm's key set but not the other — e.g. mismatched run counts) are
    reported as warnings, not errors: the intersection is still analyzed.
    Duplicate keys within one source (should not happen in a well-formed
    results file) keep the LAST occurrence and are also warned about.
    """
    def index(rows: Sequence[Dict[str, Any]]) -> Dict[Tuple[Any, Any], Dict[str, Any]]:
        idx: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
        for r in rows:
            idx[_row_key(r)] = r
        return idx

    b_idx = index(baseline_rows)
    v_idx = index(variant_rows)

    common_keys = sorted(
        set(b_idx) & set(v_idx), key=lambda k: (str(k[0]), k[1] if k[1] is not None else -1)
    )
    pairs = [(b_idx[k], v_idx[k]) for k in common_keys]

    warnings: List[str] = []
    only_b = sorted(set(b_idx) - set(v_idx), key=lambda k: (str(k[0]), k[1] if k[1] is not None else -1))
    only_v = sorted(set(v_idx) - set(b_idx), key=lambda k: (str(k[0]), k[1] if k[1] is not None else -1))
    if only_b:
        warnings.append(
            f"{len(only_b)} baseline row(s) have no variant match (unpaired, excluded): {only_b}"
        )
    if only_v:
        warnings.append(
            f"{len(only_v)} variant row(s) have no baseline match (unpaired, excluded): {only_v}"
        )
    if len(baseline_rows) != len(b_idx):
        warnings.append(
            "baseline source has duplicate (task_id, run_index) keys; kept the last occurrence of each"
        )
    if len(variant_rows) != len(v_idx):
        warnings.append(
            "variant source has duplicate (task_id, run_index) keys; kept the last occurrence of each"
        )
    return pairs, warnings


def _get_metric_value(row: Dict[str, Any], metric: str) -> Optional[float]:
    """Extract one metric's value from a row, or ``None`` if absent.

    Judge-agnostic: a row only needs ``task_id``/``run_index`` plus whichever
    of these fields it happens to carry. ``passed`` is coerced to 0.0/1.0;
    ``prompt_tokens`` is pulled out of the nested ``tokens`` dict; everything
    else is a top-level field.
    """
    if metric == "passed":
        v = row.get("passed")
        return None if v is None else (1.0 if v else 0.0)
    if metric == "prompt_tokens":
        tokens = row.get("tokens") or {}
        v = tokens.get("prompt_tokens")
        return None if v is None else float(v)
    v = row.get(metric)
    return None if v is None else float(v)


def metric_deltas(
    pairs: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]], metric: str
) -> List[float]:
    """Per-pair deltas (variant - baseline) for one metric.

    A pair is silently skipped (not an error) if either side lacks the
    metric — this is what makes the tool judge-agnostic across engines that
    may not all populate every field.
    """
    deltas: List[float] = []
    for b, v in pairs:
        bv = _get_metric_value(b, metric)
        vv = _get_metric_value(v, metric)
        if bv is None or vv is None:
            continue
        deltas.append(vv - bv)
    return deltas


# --------------------------------------------------------------------------- #
# Inverse-normal CDF (probit) — stdlib substitute for scipy.stats.norm.ppf    #
# --------------------------------------------------------------------------- #


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via ``math.erf`` (exact, stdlib-only)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (probit function).

    Peter Acklam's rational approximation — the standard stdlib-only
    substitute for ``scipy.stats.norm.ppf``. Relative error < 1.15e-9 over
    the open interval (0, 1), which is ample precision for BCa bias/
    acceleration corrections (we never need normal-tail precision beyond
    that; the permutation test below supplies the actual p-value empirically).
    """
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf

    a = (
        -3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
        1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
        6.680131188771972e01, -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
        -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
        3.754408661907416e00,
    )
    p_low = 0.02425
    p_high = 1.0 - p_low

    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
        )
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
            (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    )


def _percentile(sorted_vals: Sequence[float], p: float) -> float:
    """Linear-interpolated percentile of a pre-sorted sequence, ``p`` in [0,1]."""
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    idx = p * (n - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_vals[int(idx)]
    frac = idx - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


# --------------------------------------------------------------------------- #
# BCa bootstrap CI                                                            #
# --------------------------------------------------------------------------- #


def bca_ci(
    deltas: Sequence[float],
    resamples: int,
    alpha: float,
    rng: random.Random,
) -> Tuple[float, float]:
    """Bias-corrected and accelerated (BCa) bootstrap CI on the mean delta.

    Standard BCa recipe (Efron & Tibshirani, *An Introduction to the
    Bootstrap*, ch. 14):

    1. Bootstrap: draw ``resamples`` resamples (with replacement, size n)
       from ``deltas`` and compute each resample's mean -> the bootstrap
       distribution of the statistic.
    2. Bias-correction z0: ``Phi^-1(proportion of bootstrap means < observed
       mean)``. Nonzero z0 means the bootstrap distribution is off-center
       from the observed statistic (common with skewed/bounded metrics like
       0/1 pass rates or compression counts piled at 0).
    3. Acceleration a: from the jackknife (leave-one-pair-out) means, via
       the standard skewness-based formula. Nonzero a corrects for the rate
       at which the standard error changes across the distribution.
    4. Adjusted percentiles: map the nominal alpha/2, 1-alpha/2 normal
       quantiles through z0 and a to get the actual percentiles to read off
       the bootstrap distribution.

    Degenerate inputs are handled without crashing: n<2 or an exactly
    constant ``deltas`` (no spread to bootstrap, e.g. all pairs tie) collapse
    the CI to the point estimate ``(mean, mean)`` rather than dividing by
    zero in the jackknife acceleration or feeding ``Phi^-1(0)`` into z0.
    """
    n = len(deltas)
    if n == 0:
        return (float("nan"), float("nan"))
    theta_hat = statistics.mean(deltas)
    if n < 2 or all(d == deltas[0] for d in deltas):
        return (theta_hat, theta_hat)

    boot_thetas = sorted(
        statistics.mean(deltas[rng.randrange(n)] for _ in range(n)) for _ in range(resamples)
    )
    if boot_thetas[0] == boot_thetas[-1]:
        # Every resample landed on the same mean (e.g. huge n, tiny spread
        # rounded away) — nothing to correct, report the point.
        return (theta_hat, theta_hat)

    prop_less = sum(1 for t in boot_thetas if t < theta_hat) / resamples
    # Clip away from the {0,1} boundary so Phi^-1 stays finite; the clip
    # width matches bootstrap resolution (can't resolve finer than 1/B anyway).
    eps = 1.0 / (2 * resamples)
    prop_less = min(max(prop_less, eps), 1.0 - eps)
    z0 = _norm_ppf(prop_less)

    jack_means = [statistics.mean(deltas[:i] + deltas[i + 1 :]) for i in range(n)]
    jack_bar = statistics.mean(jack_means)
    num = sum((jack_bar - jm) ** 3 for jm in jack_means)
    den = 6.0 * (sum((jack_bar - jm) ** 2 for jm in jack_means) ** 1.5)
    a = num / den if den != 0 else 0.0

    z_lo = _norm_ppf(alpha / 2.0)
    z_hi = _norm_ppf(1.0 - alpha / 2.0)

    def _adjusted_percentile(z: float) -> float:
        denom = 1.0 - a * (z0 + z)
        if denom == 0:
            return 0.5  # degenerate acceleration: fall back to the median
        return _norm_cdf(z0 + (z0 + z) / denom)

    alpha1 = min(max(_adjusted_percentile(z_lo), 0.0), 1.0)
    alpha2 = min(max(_adjusted_percentile(z_hi), 0.0), 1.0)

    lo = _percentile(boot_thetas, alpha1)
    hi = _percentile(boot_thetas, alpha2)
    if lo > hi:
        lo, hi = hi, lo
    return (lo, hi)


# --------------------------------------------------------------------------- #
# Sign-flip (paired permutation) test                                        #
# --------------------------------------------------------------------------- #


def sign_flip_p_value(
    deltas: Sequence[float], permutations: int, rng: random.Random
) -> float:
    """Two-sided sign-flip permutation test p-value for the mean delta.

    Under the null hypothesis that baseline and variant are exchangeable
    within each pair, each pair's delta is equally likely to have the
    opposite sign. ``T_obs = mean(deltas)``; for ``P`` Monte-Carlo
    permutations, flip each pair's sign with probability 1/2 independently
    and recompute ``T*``. The p-value is
    ``(1 + #{|T*| >= |T_obs|}) / (1 + P)`` — the "+1" (add-one smoothing)
    guarantees ``p`` is never exactly 0 and matches the standard
    permutation-test convention of counting the observed statistic itself
    as one of the P+1 samples under the null.
    """
    n = len(deltas)
    if n == 0:
        return float("nan")
    t_obs = abs(statistics.mean(deltas))
    count = 0
    for _ in range(permutations):
        t_star = abs(statistics.mean(d if rng.random() < 0.5 else -d for d in deltas))
        if t_star >= t_obs:
            count += 1
    return (1 + count) / (1 + permutations)


# --------------------------------------------------------------------------- #
# Per-metric analysis + verdict                                              #
# --------------------------------------------------------------------------- #


def _verdict(direction: str, ci_lo: float, ci_hi: float, p_value: float) -> str:
    significant = p_value < _P_LOW
    if direction == "higher":
        if significant and ci_lo > 0:
            return "WIN"
        if significant and ci_hi < 0:
            return "LOSS"
    else:  # lower is better
        if significant and ci_hi < 0:
            return "WIN"
        if significant and ci_lo > 0:
            return "LOSS"
    return "INCONCLUSIVE"


def analyze_metric(
    deltas: Sequence[float], metric: str, resamples: int, seed: int
) -> Dict[str, Any]:
    """Full analysis (CI + permutation test + verdict) for one metric's deltas.

    RNG is seeded per-metric from ``(seed, metric)`` so a report is
    reproducible and each metric's random draws are independent of which
    other metrics were requested in the same run.
    """
    direction = METRIC_DIRECTIONS[metric]
    n = len(deltas)
    if n == 0:
        return {
            "metric": metric,
            "direction": direction,
            "n_pairs": 0,
            "mean_delta": None,
            "ci_lo": None,
            "ci_hi": None,
            "p_value": None,
            "verdict": "SKIPPED",
            "note": "no paired rows carried this metric on both sides",
        }

    rng = random.Random(f"{seed}::{metric}")
    mean_delta = statistics.mean(deltas)
    ci_lo, ci_hi = bca_ci(list(deltas), resamples, CI_ALPHA, rng)
    p_value = sign_flip_p_value(list(deltas), resamples, rng)
    verdict = _verdict(direction, ci_lo, ci_hi, p_value)
    return {
        "metric": metric,
        "direction": direction,
        "n_pairs": n,
        "mean_delta": mean_delta,
        "ci_lo": ci_lo,
        "ci_hi": ci_hi,
        "p_value": p_value,
        "verdict": verdict,
    }


# --------------------------------------------------------------------------- #
# Report assembly                                                             #
# --------------------------------------------------------------------------- #


def build_report(
    baseline: str | Path,
    variant: str | Path,
    metrics: Optional[Sequence[str]] = None,
    resamples: int = 10_000,
    seed: int = 42,
) -> Dict[str, Any]:
    """Load, pair, and analyze two results sources. Pure function — the
    entire deterministic core of this tool; the CLI is a thin wrapper.
    """
    baseline_rows, baseline_path = load_results(baseline)
    variant_rows, variant_path = load_results(variant)
    pairs, warnings = pair_rows(baseline_rows, variant_rows)

    metric_list = list(metrics) if metrics else list(DEFAULT_METRICS)
    unknown = [m for m in metric_list if m not in METRIC_DIRECTIONS]
    if unknown:
        raise ValueError(
            f"Unknown metric(s): {unknown}. Known metrics: {sorted(METRIC_DIRECTIONS)}"
        )

    metric_results = []
    for m in metric_list:
        deltas = metric_deltas(pairs, m)
        metric_results.append(analyze_metric(deltas, m, resamples, seed))

    return {
        "baseline_source": str(baseline_path),
        "variant_source": str(variant_path),
        "n_baseline_rows": len(baseline_rows),
        "n_variant_rows": len(variant_rows),
        "n_pairs": len(pairs),
        "seed": seed,
        "resamples": resamples,
        "warnings": warnings,
        "metrics": metric_results,
    }


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def _fmt(v: Optional[float], nd: int = 4) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{v:.{nd}f}"


def print_report(report: Dict[str, Any]) -> None:
    print(f"baseline: {report['baseline_source']} ({report['n_baseline_rows']} rows)")
    print(f"variant:  {report['variant_source']} ({report['n_variant_rows']} rows)")
    print(f"paired:   {report['n_pairs']} pair(s)  seed={report['seed']}  "
          f"resamples={report['resamples']}")
    for w in report["warnings"]:
        print(f"WARNING: {w}")
    print()
    print(f"{'metric':<18} {'dir':<7} {'n':<5} {'mean_delta':<12} "
          f"{'ci95_lo':<12} {'ci95_hi':<12} {'p':<8} verdict")
    print("-" * 92)
    for m in report["metrics"]:
        print(
            f"{m['metric']:<18} {m['direction']:<7} {m['n_pairs']:<5} "
            f"{_fmt(m['mean_delta']):<12} {_fmt(m['ci_lo']):<12} {_fmt(m['ci_hi']):<12} "
            f"{_fmt(m['p_value']):<8} {m['verdict']}"
        )
        if m["verdict"] == "SKIPPED":
            print(f"    ({m.get('note', '')})")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m eval.context_suite.paired_stats",
        description=(
            "Paired baseline-vs-variant statistics for the context-engine "
            "grading suite (BCa bootstrap CI + sign-flip permutation test)."
        ),
    )
    p.add_argument("baseline", help="Baseline results file or directory.")
    p.add_argument("variant", help="Variant results file or directory.")
    p.add_argument(
        "--metric", nargs="*", default=None, choices=list(METRIC_DIRECTIONS),
        help=f"Metrics to analyze (default: all of {list(DEFAULT_METRICS)}).",
    )
    p.add_argument("--resamples", type=int, default=10_000,
                    help="Bootstrap resamples AND permutation count (default 10000).")
    p.add_argument("--seed", type=int, default=42, help="RNG seed (default 42).")
    p.add_argument("--json", default=None, help="Optional path to write the JSON report.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(
        args.baseline,
        args.variant,
        metrics=args.metric,
        resamples=args.resamples,
        seed=args.seed,
    )
    print_report(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
