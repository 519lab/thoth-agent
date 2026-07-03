"""CLI for the context-engine grading suite.

    python -m eval.context_suite.run --list
    python -m eval.context_suite.run --dry-run                 # validate oracles
    python -m eval.context_suite.run --engine compressor --model <m> --runs 3
    python -m eval.context_suite.run --engine substrate  --model <m> --runs 3

A baseline-vs-engine A/B is just the last two lines with the same ``--model``
and ``--tasks`` — flip ``--engine``. Each task-run is written as one JSONL row;
a ``summary.json`` (per-task + per-engine aggregates) and a printed table close
the run. See ``eval/context_suite/README.md`` for the metric→goal mapping.

Never touches a database (see runner safety notes). Point it at the test
instance's *seed* only by running the suite; it writes nothing to 5432/5433.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional

from eval.context_suite import tasks as tasks_mod
from eval.context_suite.runner import run_task

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Aggregation (pure — unit-tested)                                            #
# --------------------------------------------------------------------------- #


def _as_dict(result: Any) -> Dict[str, Any]:
    """Normalize a TaskResult (or already-dict row) to a plain dict."""
    if hasattr(result, "to_json"):
        return result.to_json()
    return dict(result)


def _agg_block(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate a set of task-run rows into headline metrics.

    Metrics chosen per plan §4:
    - pass_rate: fraction of runs whose oracle passed (task success).
    - mean_outcome: mean per-task ``outcome_score`` (existing signal).
    - mean_prompt_tokens_per_task: GROSS token usage (raw, cache-blind).
    - mean_cost_per_task: cache-ADJUSTED cost (provider accounting already
      prices cache reads cheaper) — the honest "efficient token usage" metric,
      since raw tokens mislead under prompt caching.
    - cache_hit_rate: cache_read / prompt_tokens across the block.
    - mean_compressions_per_task: how often the engine actually fired.
    """
    n = len(rows)
    if n == 0:
        return {
            "runs": 0, "pass_rate": 0.0, "mean_outcome": 0.0,
            "mean_prompt_tokens_per_task": 0.0, "mean_cost_per_task": 0.0,
            "cache_hit_rate": 0.0, "mean_compressions_per_task": 0.0,
            "errors": 0,
        }

    def tok(row: Dict[str, Any], key: str) -> float:
        return float((row.get("tokens") or {}).get(key, 0) or 0)

    total_prompt = sum(tok(r, "prompt_tokens") for r in rows)
    total_cache_read = sum(tok(r, "cache_read_tokens") for r in rows)
    return {
        "runs": n,
        "pass_rate": round(sum(1 for r in rows if r.get("passed")) / n, 4),
        "mean_outcome": round(mean(float(r.get("mean_outcome", 0) or 0) for r in rows), 4),
        "mean_prompt_tokens_per_task": round(total_prompt / n, 1),
        "mean_cost_per_task": round(sum(tok(r, "cost_usd") for r in rows) / n, 6),
        "cache_hit_rate": round(total_cache_read / total_prompt, 4) if total_prompt else 0.0,
        "mean_compressions_per_task": round(
            sum(int(r.get("compression_count", 0) or 0) for r in rows) / n, 2
        ),
        "errors": sum(1 for r in rows if r.get("error")),
    }


def summarize(results: List[Any]) -> Dict[str, Any]:
    """Build the ``summary.json`` payload: per-task, per-engine, and overall.

    Pure and deterministic given the rows — this is the aggregation math the
    unit tests exercise without any model call.
    """
    rows = [_as_dict(r) for r in results]

    per_task: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        per_task.setdefault(r["task_id"], {"runs": []})["runs"].append(
            {
                "engine": r.get("engine"),
                "run_index": r.get("run_index"),
                "passed": r.get("passed"),
                "mean_outcome": r.get("mean_outcome"),
                "compression_count": r.get("compression_count"),
                "prompt_tokens": (r.get("tokens") or {}).get("prompt_tokens"),
                "cost_usd": (r.get("tokens") or {}).get("cost_usd"),
                "error": r.get("error"),
            }
        )
    for tid, block in per_task.items():
        block["aggregate"] = _agg_block(
            [r for r in rows if r["task_id"] == tid]
        )

    engines = sorted({r.get("engine", "") for r in rows})
    per_engine = {eng: _agg_block([r for r in rows if r.get("engine") == eng])
                  for eng in engines}

    return {
        "n_rows": len(rows),
        "engines": engines,
        "per_task": per_task,
        "per_engine": per_engine,
        "overall": _agg_block(rows),
    }


# --------------------------------------------------------------------------- #
# Dry-run oracle self-check (no model)                                        #
# --------------------------------------------------------------------------- #


def _dry_run(task_ids: Optional[List[str]]) -> int:
    """Validate every selected oracle cheaply: it must pass on a known-good
    scenario and fail on a known-bad one — no agent, no tokens.

    Returns a process exit code (0 = all oracles behaved).
    """
    import tempfile
    import shutil
    from eval.context_suite.tasks import snapshot_tree

    selected = tasks_mod.get_tasks(task_ids)
    print(f"Dry-run oracle self-check over {len(selected)} task(s)\n")
    print(f"{'task':<26} {'family':<14} {'neg=fail':<10} {'pos=pass':<10} verdict")
    print("-" * 78)

    ok = True
    for task in selected:
        # Negative case must FAIL the oracle.
        ws_neg = Path(tempfile.mkdtemp(prefix=f"dry_neg_{task.id}_"))
        ws_pos = Path(tempfile.mkdtemp(prefix=f"dry_pos_{task.id}_"))
        try:
            task.setup(ws_neg)
            neg_res = task.oracle(
                ws_neg, task.make_negative(ws_neg, snapshot_tree(ws_neg))
            )
            task.setup(ws_pos)
            pos_res = task.oracle(
                ws_pos, task.make_positive(ws_pos, snapshot_tree(ws_pos))
            )
        finally:
            shutil.rmtree(ws_neg, ignore_errors=True)
            shutil.rmtree(ws_pos, ignore_errors=True)

        neg_good = not neg_res.passed  # we WANT the negative to fail
        pos_good = pos_res.passed
        verdict = "OK" if (neg_good and pos_good) else "BROKEN"
        if verdict != "OK":
            ok = False
        print(
            f"{task.id:<26} {task.family:<14} "
            f"{'fail✓' if neg_good else 'PASS✗':<10} "
            f"{'pass✓' if pos_good else 'FAIL✗':<10} {verdict}"
        )
        if verdict != "OK":
            print(f"    neg: {neg_res.details}")
            print(f"    pos: {pos_res.details}")

    print("\n" + ("All oracles behaved." if ok else "SOME ORACLES ARE BROKEN."))
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# Live run                                                                    #
# --------------------------------------------------------------------------- #


def _print_table(rows: List[Dict[str, Any]]) -> None:
    print()
    print(
        f"{'task':<24} {'engine':<11} {'pass':<5} {'outcome':<8} "
        f"{'prompt_tok':<11} {'cost$':<9} {'compress':<9} {'dur_s':<7} error"
    )
    print("-" * 100)
    for r in rows:
        tokens = r.get("tokens") or {}
        err = (r.get("error") or "").splitlines()[0][:24] if r.get("error") else ""
        print(
            f"{r['task_id']:<24} {str(r.get('engine','')):<11} "
            f"{('yes' if r.get('passed') else 'no'):<5} "
            f"{r.get('mean_outcome', 0):<8} "
            f"{int(tokens.get('prompt_tokens', 0) or 0):<11} "
            f"{tokens.get('cost_usd', 0):<9} "
            f"{int(r.get('compression_count', 0) or 0):<9} "
            f"{r.get('duration_s', 0):<7} {err}"
        )


def _live_run(args: argparse.Namespace) -> int:
    selected = tasks_mod.get_tasks(args.tasks)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    jsonl_path = out_dir / f"results_{args.engine}_{stamp}.jsonl"

    print(
        f"Running {len(selected)} task(s) x {args.runs} run(s) on engine "
        f"'{args.engine}', model '{args.model}'\n  → {jsonl_path}"
    )

    rows: List[Dict[str, Any]] = []
    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for run_index in range(args.runs):
            for task in selected:
                result = run_task(
                    task,
                    model=args.model,
                    base_url=args.base_url,
                    api_key=args.api_key,
                    provider=args.provider,
                    engine=args.engine,
                    max_iterations=args.max_iterations,
                    quiet=not args.verbose,
                    compress_threshold_tokens=args.compress_threshold_tokens,
                    run_index=run_index,
                    timeout_s=args.timeout,
                )
                row = result.to_json()
                rows.append(row)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                status = "PASS" if row["passed"] else "FAIL"
                print(f"  [{status}] {task.id} (run {run_index}) "
                      f"outcome={row['mean_outcome']} "
                      f"compress={row['compression_count']} "
                      f"{('ERR: ' + row['error'].splitlines()[0]) if row['error'] else ''}")

    summary = summarize(rows)
    summary_path = out_dir / f"summary_{args.engine}_{stamp}.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    _print_table(rows)
    print("\nAggregate (this engine):")
    print(json.dumps(summary["per_engine"].get(args.engine, {}), indent=2))
    print(f"\nWrote {summary_path}")
    return 0


# --------------------------------------------------------------------------- #
# Argument parsing                                                            #
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m eval.context_suite.run",
        description="Graded long-horizon context-engine evaluation suite.",
    )
    p.add_argument("--tasks", nargs="*", default=None,
                   help="Task ids to run (default: all). See --list.")
    p.add_argument("--engine", default="compressor",
                   help="Context engine (compressor|substrate|...) — sets "
                        "THOTH_CONTEXT_ENGINE per AIAgent.")
    p.add_argument("--model", default="anthropic/claude-sonnet-4.6",
                   help="Model id for the agent.")
    p.add_argument("--base-url", default=None, help="API base URL.")
    p.add_argument("--api-key", default=None, help="API key.")
    p.add_argument("--provider", default=None, help="Provider hint.")
    p.add_argument("--runs", type=int, default=1,
                   help="Repeat each task N times (variance).")
    p.add_argument("--max-iterations", type=int, default=40,
                   help="Max tool-calling iterations per turn.")
    p.add_argument("--compress-threshold-tokens", type=int, default=50_000,
                   help="Force compression once a turn's prompt exceeds this "
                        "(0/negative = use the model's real window).")
    p.add_argument("--timeout", type=int, default=900,
                   help="Per-task wall-clock cap (seconds).")
    p.add_argument("--out", default="eval/results",
                   help="Directory for JSONL + summary.json output.")
    p.add_argument("--verbose", action="store_true", help="Verbose agent logs.")
    p.add_argument("--list", action="store_true", help="List tasks and exit.")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate oracles (setup + known-good/bad) with NO model.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.list:
        print(f"{'id':<26} {'family':<14} {'turns':<6} {'probes':<7} title")
        print("-" * 90)
        for t in tasks_mod.list_tasks():
            print(f"{t['id']:<26} {t['family']:<14} {t['turns']:<6} "
                  f"{t['probes']:<7} {t['title']}")
        return 0

    if args.dry_run:
        return _dry_run(args.tasks)

    if args.compress_threshold_tokens is not None and args.compress_threshold_tokens <= 0:
        args.compress_threshold_tokens = None  # opt out → real window
    return _live_run(args)


if __name__ == "__main__":
    sys.exit(main())
