"""``thoth cost`` — always-on cost & latency rollup (innovation #4).

Answers "what did today cost?" and "how slow are turns?" from the
``agent_turn_cost`` table the conversation loop writes on every turn, plus
the substrate crew's ``substrate_agent_cost`` sibling. Read-only: initialises
the asyncpg pool, runs windowed aggregates, prints a fixed-format report.
Safe to run against a deployment that is already booted in another process.

Costs are estimates from the local price catalog (``agent/usage_pricing.py``),
never billing truth — turns whose route has no known price are counted
separately so the estimate's blind spot is visible.

Wired into Thoth's top-level argparse via :func:`register_subparser`, same
containment pattern as ``substrate/cli/inspect.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

# (label, trailing-window hours) rows for the default report.
_WINDOWS: tuple[tuple[str, float], ...] = (("24h", 24.0), ("7d", 168.0), ("30d", 720.0))


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add the ``thoth cost`` subcommand."""
    cost_parser = subparsers.add_parser(
        "cost",
        help="Show cost & latency rollups (tokens, estimated spend, turn times)",
        description="Windowed rollups from the per-turn cost/latency records "
        "the agent writes on every turn (agent_turn_cost) plus the substrate "
        "crew's own spend (substrate_agent_cost). Costs are estimates from "
        "the local price catalog, not billing truth.",
    )
    cost_parser.add_argument(
        "--hours",
        type=float,
        default=None,
        metavar="N",
        help="Report a single trailing window of N hours instead of 24h/7d/30d",
    )
    cost_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the raw rollup as JSON",
    )
    cost_parser.set_defaults(func=_cmd_cost)


def _fmt_tokens(n: Optional[int]) -> str:
    n = int(n or 0)
    if n >= 10_000_000:
        return f"{n / 1_000_000:.0f}M"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.0f}K"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_cost(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    return f"${v:,.2f}" if v >= 0.005 or v == 0.0 else f"${v:.4f}"


def _fmt_ms(v: Optional[float]) -> str:
    if v is None:
        return "-"
    v = float(v)
    if v >= 1000:
        return f"{v / 1000:.1f}s"
    return f"{v:.0f}ms"


def _cmd_cost(args: argparse.Namespace) -> int:
    import thoth_db

    if not thoth_db.ensure_pool_sync():
        print(
            "error: THOTH_PG_DSN is not set and no pool is initialised; "
            "configure it before running `thoth cost`.",
            file=sys.stderr,
        )
        # main() ignores handler return values, so exit explicitly — a
        # scripted `thoth cost` must not report success on failure.
        sys.exit(1)

    from agent.turn_cost import (
        fetch_model_breakdown,
        fetch_substrate_summary,
        fetch_turn_summary,
    )

    windows = ((f"{args.hours:g}h", float(args.hours)),) if args.hours else _WINDOWS
    detail_hours = windows[0][1]

    async def _gather() -> dict:
        report: dict = {"windows": {}}
        for label, hours in windows:
            report["windows"][label] = await fetch_turn_summary(hours=hours)
        report["by_model"] = await fetch_model_breakdown(hours=detail_hours)
        report["substrate"] = await fetch_substrate_summary(hours=detail_hours)
        return report

    try:
        report = thoth_db.run_sync(_gather())
    except Exception as exc:
        missing_table = "agent_turn_cost" in str(exc) and "does not exist" in str(exc)
        print(f"error: {exc}", file=sys.stderr)
        if missing_table:
            print(
                "hint: the agent_turn_cost table is missing — apply migrations "
                "(alembic upgrade head, or `thoth update` on an installed deployment).",
                file=sys.stderr,
            )
        sys.exit(1)
    finally:
        # One-shot CLI process — don't leave a dangling asyncpg pool behind.
        try:
            thoth_db.run_sync(thoth_db.close())
        except Exception:
            pass

    if args.as_json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print("Thoth cost & latency — estimates from the local price catalog\n")
    header = (
        f"{'Window':<8} {'Turns':>7} {'API calls':>10} {'Tokens in':>10} "
        f"{'out':>8} {'cache-r':>8} {'Est. cost':>10} {'p50 turn':>9} {'p95 turn':>9}"
    )
    print(header)
    print("-" * len(header))
    for label, _hours in windows:
        s = report["windows"][label]
        print(
            f"{label:<8} {int(s.get('turns') or 0):>7} "
            f"{int(s.get('api_calls') or 0):>10} "
            f"{_fmt_tokens(s.get('input_tokens')):>10} "
            f"{_fmt_tokens(s.get('output_tokens')):>8} "
            f"{_fmt_tokens(s.get('cache_read_tokens')):>8} "
            f"{_fmt_cost(s.get('cost_usd')):>10} "
            f"{_fmt_ms(s.get('p50_duration_ms')):>9} "
            f"{_fmt_ms(s.get('p95_duration_ms')):>9}"
        )

    by_model = report.get("by_model") or []
    if by_model:
        print(f"\nBy model ({windows[0][0]}):")
        for row in by_model:
            print(
                f"  {(row.get('model') or '(unknown)'):<42} "
                f"{int(row.get('turns') or 0):>6} turns  "
                f"{_fmt_tokens(row.get('total_tokens')):>8} tokens  "
                f"{_fmt_cost(row.get('cost_usd')):>10}"
            )

    crew = report.get("substrate") or {}
    if int(crew.get("calls") or 0) > 0:
        print(
            f"\nSubstrate crew ({windows[0][0]}): "
            f"{int(crew['calls']):,} LLM calls · "
            f"{_fmt_tokens(crew.get('total_tokens'))} tokens · "
            f"p95 call latency {_fmt_ms(crew.get('p95_latency_ms'))}"
        )

    unpriced = sum(
        int(report["windows"][label].get("unpriced_turns") or 0) for label, _ in windows[:1]
    )
    if unpriced:
        print(
            f"\nNote: {unpriced} turn(s) in the {windows[0][0]} window had no "
            "price for their route — the cost estimate is a floor, not a total."
        )
    return 0
