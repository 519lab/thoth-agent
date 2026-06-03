"""``thoth substrate`` — substrate state inspection commands.

Surface (flattened from earlier ``thoth substrate inspect <thing>``;
the redundant ``inspect`` verb was removed 2026-05-26):

    thoth substrate                       # default summary
    thoth substrate streams               # list streams + slice counts
    thoth substrate slices --stream NAME --limit 20
    thoth substrate pending               # current pending-queue depth
    thoth substrate profiles              # decay profiles
    thoth substrate curator [SUB]         # Curator subtree (Phase B)
    thoth substrate recall  [SUB]         # Recall subtree (Phase C)

The CLI does not boot the full substrate (no sub-agent loops) — it just
initialises the asyncpg pool, runs read-only queries against the substrate
tables, and prints a fixed-format report. Safe to run against a Thoth
deployment that is already booted in another process.

Wired into Thoth's top-level argparse via :func:`register_subparser`
called from ``thoth_cli/main.py``. Mutating/admin operations on
embeddings live under the separate ``thoth embed`` namespace; see
``substrate/cli/embed.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg


# ---------------------------------------------------------------------------
# Subparser registration — called from thoth_cli/main.py.
# ---------------------------------------------------------------------------


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add the ``thoth substrate`` subcommand tree to ``subparsers``.

    Matches the pattern used by other optional CLI modules (e.g.
    ``agent.lsp.cli.register_subparser``) so the import is contained and
    a failure to register doesn't break the rest of the CLI.
    """
    substrate_parser = subparsers.add_parser(
        "substrate",
        help="Inspect substrate state (streams, slices, curator, recall)",
        description="Read-only inspection of the substrate's perception "
        "streams, slices, decay profiles, pending queue, and the Curator + "
        "recall subsystems. With no subcommand, prints the default summary. "
        "Embedding admin (reshape, backfill) lives under ``thoth embed``.",
    )
    substrate_sub = substrate_parser.add_subparsers(dest="substrate_command")
    # Default for ``thoth substrate`` with no subcommand: print the summary
    # (the same content the old ``thoth substrate inspect`` printed).
    substrate_parser.set_defaults(func=_cmd_inspect_summary)

    substrate_sub.add_parser(
        "streams", help="List streams + per-stream slice counts"
    ).set_defaults(func=_cmd_inspect_streams)

    # ── Memory curation: pin / unpin / forget a slice (decay control) ──
    pin_p = substrate_sub.add_parser(
        "pin", help="Pin a slice (exempt from decay + release — 'never forget')"
    )
    pin_p.add_argument("slice_id")
    pin_p.set_defaults(func=_cmd_inspect_pin)
    unpin_p = substrate_sub.add_parser("unpin", help="Unpin a slice (resume normal decay)")
    unpin_p.add_argument("slice_id")
    unpin_p.set_defaults(func=_cmd_inspect_unpin)
    forget_p = substrate_sub.add_parser(
        "forget", help="Forget a slice (drop salience to 0 → Curator releases it)"
    )
    forget_p.add_argument("slice_id")
    forget_p.set_defaults(func=_cmd_inspect_forget)

    skills_p = substrate_sub.add_parser(
        "skills", help="Suggest bundled skills relevant to a query/topic"
    )
    skills_p.add_argument("query", nargs="+", help="Topic words to match against skills")
    skills_p.add_argument("--limit", type=int, default=5)
    skills_p.set_defaults(func=_cmd_inspect_skills)

    slices_p = substrate_sub.add_parser(
        "slices", help="List the most-recent N slices on a given stream"
    )
    slices_p.add_argument(
        "--stream",
        required=True,
        help="Stream name (e.g. hermes.world.user_message.cli)",
    )
    slices_p.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max number of slices to show (default 20)",
    )
    slices_p.set_defaults(func=_cmd_inspect_slices)

    substrate_sub.add_parser(
        "pending", help="Show pending-queue depth + oldest entry age"
    ).set_defaults(func=_cmd_inspect_pending)

    substrate_sub.add_parser(
        "profiles", help="List decay profiles"
    ).set_defaults(func=_cmd_inspect_profiles)

    substrate_sub.add_parser(
        "health",
        help="One-glance operator rollup: worker liveness, boot, coherence, "
        "L0–L4 counts, consolidation backlog",
        description="The substrate operator dashboard — aggregates sub-agent "
        "liveness, last boot, the Critic's coherence vital sign, per-layer "
        "object counts, and consolidation backlog into a single report. The "
        "first thing to run when asking 'is the substrate healthy?'.",
    ).set_defaults(func=_cmd_inspect_health)

    substrate_sub.add_parser(
        "agents",
        help="Sub-agent liveness (heartbeats from the worker subprocess)",
        description="Show each substrate sub-agent's last heartbeat, "
        "intensity, tick count, and live/stale/down status. Reads "
        "substrate_agent_heartbeat, which the worker subprocess upserts "
        "every ~10s. If every agent shows DOWN, the worker is not running.",
    ).set_defaults(func=_cmd_inspect_agents)

    substrate_sub.add_parser(
        "boot",
        help="Last substrate boot outcome per process role (writer/worker)",
        description="Show the last recorded boot status for each substrate "
        "process role. A writer-mode FAILED means the gateway/CLI is "
        "emitting no perception; a worker-mode FAILED means decay/Sentinel/"
        "embeddings aren't running. Reads the state_meta KV rows that "
        "bootstrap_substrate writes on every boot attempt.",
    ).set_defaults(func=_cmd_inspect_boot)

    # ── Phase B: curator subtree ──────────────────────────────────────
    curator_p = substrate_sub.add_parser(
        "curator",
        help="Inspect Curator state",
        description="Show Curator decay/release activity. Without a sub "
        "subcommand, prints the default summary.",
    )
    curator_sub = curator_p.add_subparsers(dest="curator_subcommand")
    curator_p.set_defaults(func=_cmd_inspect_curator_summary)

    curator_sub.add_parser(
        "summary", help="Curator summary (default)"
    ).set_defaults(func=_cmd_inspect_curator_summary)

    curator_sub.add_parser(
        "histogram", help="Per-profile salience histogram (10 buckets)"
    ).set_defaults(func=_cmd_inspect_curator_histogram)

    curator_recent = curator_sub.add_parser(
        "recent", help="Recent curator.* self-state emissions"
    )
    curator_recent.add_argument(
        "--limit", type=int, default=20, help="Max emissions to show (default 20)"
    )
    curator_recent.set_defaults(func=_cmd_inspect_curator_recent)

    curator_sub.add_parser(
        "pressure",
        help="Per-stream salience pressure (Conductor opportunity-forecast inputs)",
    ).set_defaults(func=_cmd_inspect_curator_pressure)

    # ── Phase C: recall subtree ───────────────────────────────────────
    recall_p = substrate_sub.add_parser(
        "recall",
        help="Inspect recall pipeline state",
        description="Show recent recall calls + embedding coverage + config.",
    )
    recall_sub = recall_p.add_subparsers(dest="recall_subcommand")
    recall_p.set_defaults(func=_cmd_inspect_recall_summary)

    recall_sub.add_parser(
        "summary", help="Recall summary (default) — last 1h call stats + coverage"
    ).set_defaults(func=_cmd_inspect_recall_summary)

    recall_recent = recall_sub.add_parser(
        "recent", help="Recent recall calls (substrate_recall_log)"
    )
    recall_recent.add_argument(
        "--limit", type=int, default=20, help="Max log rows to show (default 20)"
    )
    recall_recent.set_defaults(func=_cmd_inspect_recall_recent)

    recall_sample = recall_sub.add_parser(
        "sample", help="Last recall log row for a given session"
    )
    recall_sample.add_argument(
        "--session-id", required=True, help="Thoth session id"
    )
    recall_sample.set_defaults(func=_cmd_inspect_recall_sample)

    recall_sub.add_parser(
        "config", help="Dump RECALL_* config knobs"
    ).set_defaults(func=_cmd_inspect_recall_config)

    recall_validate = recall_sub.add_parser(
        "validate",
        help="Run a real recall + print the composed block and a readiness verdict",
        description="Go/no-go health probe: runs the same recall() the "
        "foreground would, prints the composed <memory-context> block, "
        "embedding coverage, and a READY/DEGRADED/NOT READY verdict. Useful "
        "after a worker outage to confirm recall isn't silently degraded.",
    )
    recall_validate.add_argument(
        "--query",
        default=None,
        help="Probe query (default: the most-recent user-message slice's text)",
    )
    recall_validate.add_argument(
        "--token-budget",
        type=int,
        default=None,
        help="Override the composer token budget for this probe",
    )
    recall_validate.set_defaults(func=_cmd_inspect_recall_validate)

    # ── Phase D: L1 + Parser subtrees ─────────────────────────────────
    l1_p = substrate_sub.add_parser(
        "l1", help="Inspect L1 (entities + relationships)"
    )
    l1_sub = l1_p.add_subparsers(dest="l1_subcommand")
    l1_p.set_defaults(func=_cmd_inspect_l1_entities)
    l1_ents = l1_sub.add_parser("entities", help="List L1 entities (default)")
    l1_ents.add_argument("--type", default=None, help="Filter by entity_type")
    l1_ents.add_argument("--limit", type=int, default=20)
    l1_ents.set_defaults(func=_cmd_inspect_l1_entities)
    l1_rels = l1_sub.add_parser("relationships", help="List L1 relationships")
    l1_rels.add_argument("--limit", type=int, default=20)
    l1_rels.set_defaults(func=_cmd_inspect_l1_relationships)
    # Curation (mutating) — dedup/merge fragmented memory + edit/forget.
    l1_dupes = l1_sub.add_parser("dupes", help="Suggest likely-duplicate entity pairs to merge")
    l1_dupes.add_argument("--threshold", type=float, default=0.6)
    l1_dupes.add_argument("--limit", type=int, default=20)
    l1_dupes.set_defaults(func=_cmd_inspect_l1_dupes)
    l1_merge = l1_sub.add_parser("merge", help="Merge one entity into another (by name)")
    l1_merge.add_argument("--from", dest="from_name", required=True)
    l1_merge.add_argument("--into", dest="into_name", required=True)
    l1_merge.set_defaults(func=_cmd_inspect_l1_merge)
    l1_forget = l1_sub.add_parser("forget", help="Delete an entity (by name)")
    l1_forget.add_argument("name")
    l1_forget.set_defaults(func=_cmd_inspect_l1_forget)
    l1_edit = l1_sub.add_parser("edit", help="Edit an entity's summary/name (by name)")
    l1_edit.add_argument("name")
    l1_edit.add_argument("--summary", default=None)
    l1_edit.add_argument("--rename", default=None, help="New canonical name")
    l1_edit.set_defaults(func=_cmd_inspect_l1_edit)

    parser_p = substrate_sub.add_parser(
        "parser", help="Inspect Parser activity (substrate_parser_log)"
    )
    parser_sub = parser_p.add_subparsers(dest="parser_subcommand")
    parser_p.set_defaults(func=_cmd_inspect_parser_summary)
    parser_sub.add_parser("summary", help="Parser summary (default)").set_defaults(
        func=_cmd_inspect_parser_summary
    )
    parser_recent = parser_sub.add_parser("recent", help="Recent parser_log rows")
    parser_recent.add_argument("--limit", type=int, default=20)
    parser_recent.set_defaults(func=_cmd_inspect_parser_recent)

    l2_p = substrate_sub.add_parser(
        "l2", help="Inspect L2 (associations + edit history)"
    )
    l2_sub = l2_p.add_subparsers(dest="l2_subcommand")
    l2_p.set_defaults(func=_cmd_inspect_l2_associations)
    l2_assoc = l2_sub.add_parser("associations", help="Densest / per-entity edges (default)")
    l2_assoc.add_argument("--entity", default=None, help="Filter to one entity (by name)")
    l2_assoc.add_argument("--limit", type=int, default=20)
    l2_assoc.set_defaults(func=_cmd_inspect_l2_associations)

    l3_p = substrate_sub.add_parser("l3", help="Inspect L3 (patterns)")
    l3_sub = l3_p.add_subparsers(dest="l3_subcommand")
    l3_p.set_defaults(func=_cmd_inspect_l3_patterns)
    l3_pat = l3_sub.add_parser("patterns", help="List L3 patterns (default)")
    l3_pat.add_argument("--kind", default=None, help="Filter by pattern kind")
    l3_pat.add_argument("--limit", type=int, default=20)
    l3_pat.set_defaults(func=_cmd_inspect_l3_patterns)

    l4_p = substrate_sub.add_parser(
        "l4", help="Inspect L4 (self-model: coherence + calibration)"
    )
    l4_sub = l4_p.add_subparsers(dest="l4_subcommand")
    l4_p.set_defaults(func=_cmd_inspect_l4)
    l4_obs = l4_sub.add_parser("observations", help="Recent L4 observations (default)")
    l4_obs.add_argument("--subject", default=None)
    l4_obs.add_argument("--limit", type=int, default=20)
    l4_obs.set_defaults(func=_cmd_inspect_l4)

    dreamer_p = substrate_sub.add_parser(
        "dreamer", help="Recent Dreamer explorations (persistent log)"
    )
    dreamer_p.add_argument("--limit", type=int, default=10)
    dreamer_p.set_defaults(func=_cmd_inspect_dreamer)

    conductor_p = substrate_sub.add_parser(
        "conductor", help="Conductor decisions + backlog forecast (learned rhythm)"
    )
    conductor_p.add_argument("--limit", type=int, default=15)
    conductor_p.set_defaults(func=_cmd_inspect_conductor)

    # ── Sub-agent worker subprocess ────────────────────────────────────
    # ``thoth substrate worker run`` blocks while running Sentinel +
    # Curator + ForceRejectWorker + PartitionMaintenanceWorker in a
    # dedicated process. Managed by the systemd unit
    # ``hermes-substrate-worker.service``; rare for operators to invoke
    # by hand. Lives in its own module because the run loop is meaty.
    from substrate.cli import worker as _worker_mod

    _worker_mod.register_subparser(substrate_sub)


# ---------------------------------------------------------------------------
# Command handlers — each one is sync and bridges via hermes_db.run_sync.
# This matches the rest of the Thoth CLI, where command callbacks are
# synchronous and call asyncio.run / run_sync as needed.
# ---------------------------------------------------------------------------


def _cmd_inspect_summary(args: argparse.Namespace) -> int:
    return _run_inspect(_print_summary)


def _cmd_inspect_streams(args: argparse.Namespace) -> int:
    return _run_inspect(_print_streams)


def _cmd_inspect_slices(args: argparse.Namespace) -> int:
    return _run_inspect(
        lambda conn: _print_slices(conn, stream_name=args.stream, limit=args.limit)
    )


def _cmd_inspect_pending(args: argparse.Namespace) -> int:
    return _run_inspect(_print_pending)


def _cmd_inspect_profiles(args: argparse.Namespace) -> int:
    return _run_inspect(_print_profiles)


def _cmd_inspect_agents(args: argparse.Namespace) -> int:
    return _run_inspect(_print_agents)


def _cmd_inspect_health(args: argparse.Namespace) -> int:
    return _run_inspect(_print_health)


def _cmd_inspect_pin(args: argparse.Namespace) -> int:
    return _run_inspect(lambda conn: _do_pin(conn, args.slice_id, True))


def _cmd_inspect_unpin(args: argparse.Namespace) -> int:
    return _run_inspect(lambda conn: _do_pin(conn, args.slice_id, False))


def _cmd_inspect_forget(args: argparse.Namespace) -> int:
    return _run_inspect(lambda conn: _do_forget_slice(conn, args.slice_id))


def _cmd_inspect_skills(args: argparse.Namespace) -> int:
    """Suggest bundled skills for a topic — filesystem-only, no DB needed."""
    from substrate.skills_match import suggest_skills

    query = " ".join(args.query)
    results = suggest_skills(query, limit=args.limit, min_overlap=1)
    if not results:
        print(f"(no bundled skill matches {query!r})")
        return 0
    print(f"Skills relevant to {query!r}:")
    for s in results:
        desc = (s["description"] or "")[:90]
        print(f"  • {s['name']}  (match {s['overlap']})  — {desc}")
    return 0


def _cmd_inspect_boot(args: argparse.Namespace) -> int:
    return _run_inspect(_print_boot_status)


def _cmd_inspect_curator_summary(args: argparse.Namespace) -> int:
    return _run_inspect(_print_curator_summary)


def _cmd_inspect_curator_histogram(args: argparse.Namespace) -> int:
    return _run_inspect(_print_curator_histogram)


def _cmd_inspect_curator_recent(args: argparse.Namespace) -> int:
    return _run_inspect(
        lambda conn: _print_curator_recent(conn, limit=args.limit)
    )


def _cmd_inspect_curator_pressure(args: argparse.Namespace) -> int:
    return _run_inspect(_print_curator_pressure)


# ── Phase C: recall command dispatchers ──────────────────────────────


def _cmd_inspect_recall_summary(args: argparse.Namespace) -> int:
    from substrate.recall.cli_inspect import print_summary

    return _run_inspect(print_summary)


def _cmd_inspect_recall_recent(args: argparse.Namespace) -> int:
    from substrate.recall.cli_inspect import print_recent

    return _run_inspect(lambda conn: print_recent(conn, limit=args.limit))


def _cmd_inspect_recall_sample(args: argparse.Namespace) -> int:
    from substrate.recall.cli_inspect import print_sample

    return _run_inspect(
        lambda conn: print_sample(conn, session_id=args.session_id)
    )


def _cmd_inspect_recall_config(args: argparse.Namespace) -> int:
    from substrate.recall.cli_inspect import print_config

    return _run_inspect(print_config)


def _cmd_inspect_recall_validate(args: argparse.Namespace) -> int:
    from substrate.recall.cli_inspect import validate

    return _run_inspect(
        lambda conn: validate(
            conn, query=args.query, token_budget=args.token_budget
        )
    )


def _cmd_inspect_l1_entities(args: argparse.Namespace) -> int:
    kind = getattr(args, "type", None)
    limit = getattr(args, "limit", 20)
    return _run_inspect(lambda conn: _print_l1_entities(conn, kind=kind, limit=limit))


def _cmd_inspect_l1_relationships(args: argparse.Namespace) -> int:
    limit = getattr(args, "limit", 20)
    return _run_inspect(lambda conn: _print_l1_relationships(conn, limit=limit))


def _cmd_inspect_l1_dupes(args: argparse.Namespace) -> int:
    return _run_inspect(
        lambda conn: _print_l1_dupes(conn, threshold=args.threshold, limit=args.limit)
    )


def _cmd_inspect_l1_merge(args: argparse.Namespace) -> int:
    return _run_inspect(
        lambda conn: _do_l1_merge(conn, args.from_name, args.into_name)
    )


def _cmd_inspect_l1_forget(args: argparse.Namespace) -> int:
    return _run_inspect(lambda conn: _do_l1_forget(conn, args.name))


def _cmd_inspect_l1_edit(args: argparse.Namespace) -> int:
    return _run_inspect(
        lambda conn: _do_l1_edit(conn, args.name, summary=args.summary, rename=args.rename)
    )


def _cmd_inspect_parser_summary(args: argparse.Namespace) -> int:
    return _run_inspect(_print_parser_summary)


def _cmd_inspect_parser_recent(args: argparse.Namespace) -> int:
    limit = getattr(args, "limit", 20)
    return _run_inspect(lambda conn: _print_parser_recent(conn, limit=limit))


def _cmd_inspect_l2_associations(args: argparse.Namespace) -> int:
    entity = getattr(args, "entity", None)
    limit = getattr(args, "limit", 20)
    return _run_inspect(lambda conn: _print_l2_associations(conn, entity=entity, limit=limit))


def _cmd_inspect_l3_patterns(args: argparse.Namespace) -> int:
    kind = getattr(args, "kind", None)
    limit = getattr(args, "limit", 20)
    return _run_inspect(lambda conn: _print_l3_patterns(conn, kind=kind, limit=limit))


def _cmd_inspect_l4(args: argparse.Namespace) -> int:
    subject = getattr(args, "subject", None)
    limit = getattr(args, "limit", 20)
    return _run_inspect(lambda conn: _print_l4(conn, subject=subject, limit=limit))


def _cmd_inspect_dreamer(args: argparse.Namespace) -> int:
    limit = getattr(args, "limit", 10)
    return _run_inspect(lambda conn: _print_dreamer(conn, limit=limit))


def _cmd_inspect_conductor(args: argparse.Namespace) -> int:
    limit = getattr(args, "limit", 15)
    return _run_inspect(lambda conn: _print_conductor(conn, limit=limit))


def _run_inspect(action) -> int:
    """Shared boilerplate: ensure the pool, acquire a connection, hand it
    to the printer. Closes the pool on exit so the CLI doesn't leave a
    dangling asyncpg pool behind.
    """
    import hermes_db

    if not hermes_db.ensure_pool_sync():
        print(
            "error: THOTH_PG_DSN is not set and no pool is initialised; "
            "configure it before running `thoth substrate`.",
            file=sys.stderr,
        )
        return 1

    async def _go() -> int:
        async with hermes_db.connection() as conn:
            try:
                await action(conn)
            except Exception as exc:  # pragma: no cover — defensive
                print(f"error: {exc}", file=sys.stderr)
                return 1
        return 0

    try:
        return hermes_db.run_sync(_go())
    finally:
        # The Thoth CLI process is one-shot; release the pool so it
        # doesn't hold a connection open past the subcommand's exit.
        try:
            hermes_db.run_sync(hermes_db.close())
        except Exception:  # pragma: no cover — close is best-effort
            pass


# ---------------------------------------------------------------------------
# Printers — each takes an asyncpg.Connection and prints to stdout.
# ---------------------------------------------------------------------------


async def _print_summary(conn: "asyncpg.Connection") -> None:
    """Top-level summary — the format documented in spec §10.2."""
    now = datetime.now(timezone.utc)
    print(f"Substrate state @ {now.isoformat()}")
    print()

    streams = await _stream_counts(conn)
    n_total = len(streams)
    n_active = sum(1 for s in streams if s["lifecycle_state"] == "active")
    n_paused = sum(1 for s in streams if s["lifecycle_state"] == "paused")
    n_retired = sum(1 for s in streams if s["lifecycle_state"] == "retired")
    print(
        f"Streams: {n_total} registered "
        f"({n_active} active, {n_paused} paused, {n_retired} retired)"
    )

    slice_totals = await _slice_state_counts(conn)
    total_slices = sum(slice_totals.values())
    print(f"Slices:  {total_slices:,} total")
    for state in ("pending", "passed", "quarantined"):
        print(f"   {state:11s} {slice_totals.get(state, 0):,}")

    print()
    pending = await _pending_info(conn)
    print("Pending queue:")
    print(f"   depth = {pending['depth']}")
    if pending["oldest_age"] is not None:
        secs = pending["oldest_age"].total_seconds()
        print(f"   oldest pending = {secs:.1f}s ago")
    else:
        print("   oldest pending = (empty)")

    print()
    print("Sub-agents (liveness):")
    # Liveness is read from substrate_agent_heartbeat — the durable,
    # cross-process surface the worker subprocess upserts. The CLI runs in
    # a different process from the worker, so this is the ONLY way it can
    # tell a live worker from a dead one. (Pre-heartbeat, this section was
    # a static "all healthy" list — a dead worker was invisible.)
    rows = await _agent_liveness(conn)
    for line in _format_agent_lines(rows):
        print(f"   {line}")
    if _all_agents_down(rows):
        print()
        print(
            "   ⚠ substrate worker appears DOWN — no sub-agent has "
            "reported a heartbeat."
        )
        print(
            "     Start it with `thoth substrate worker run` (or the "
            "hermes-substrate-worker systemd unit)."
        )

    print()
    print("Last boot:")
    boot_status = await _boot_status_rows(conn)
    if not boot_status:
        print("   (no boot status recorded)")
    else:
        for line in _format_boot_lines(boot_status):
            print(f"   {line}")


async def _print_streams(conn: "asyncpg.Connection") -> None:
    streams = await _stream_counts(conn)
    if not streams:
        print("(no streams registered)")
        return
    print(f"{'name':50s}  {'family':14s}  {'modality':17s}  {'state':10s}  count")
    print("-" * 110)
    for s in streams:
        print(
            f"{s['name']:50s}  {s['family']:14s}  {s['modality']:17s}  "
            f"{s['lifecycle_state']:10s}  {s['slice_count']:>6d}"
        )


async def _print_slices(
    conn: "asyncpg.Connection", *, stream_name: str, limit: int
) -> None:
    rows = await conn.fetch(
        """
        SELECT sl.slice_id, sl.sentinel_state, sl.payload_modality,
               sl.event_time_world, sl.payload, sl.metadata
          FROM substrate_slices sl
          JOIN substrate_streams st ON st.stream_id = sl.stream_id
         WHERE st.name = $1
         ORDER BY sl.event_time_world DESC
         LIMIT $2
        """,
        stream_name,
        limit,
    )
    if not rows:
        print(f"(no slices for stream {stream_name!r})")
        return
    print(f"Most-recent {len(rows)} slices for {stream_name!r}:")
    for r in rows:
        ev = r["event_time_world"].isoformat() if r["event_time_world"] else "-"
        sid = str(r["slice_id"])
        state = r["sentinel_state"]
        modality = r["payload_modality"]
        payload_preview = _short_payload(r["payload"])
        print(f"  [{ev}] {sid}  {state:11s}  {modality:17s}  {payload_preview}")


async def _print_pending(conn: "asyncpg.Connection") -> None:
    info = await _pending_info(conn)
    print(f"depth: {info['depth']}")
    if info["oldest_age"] is not None:
        print(f"oldest age: {info['oldest_age'].total_seconds():.2f}s")
    else:
        print("oldest age: (no pending slices)")


async def _print_profiles(conn: "asyncpg.Connection") -> None:
    rows = await conn.fetch(
        """
        SELECT name, natural_half_life, consolidation_window,
               pending_ttl, tombstone_policy, applies_to_modality
          FROM substrate_decay_profiles
         ORDER BY name
        """
    )
    if not rows:
        print("(no decay profiles seeded — migration may not have run)")
        return
    print(
        f"{'name':22s}  {'half_life':12s}  {'consolidation':15s}  "
        f"{'pending_ttl':12s}  {'tombstone':10s}  modality"
    )
    print("-" * 100)
    for r in rows:
        print(
            f"{r['name']:22s}  "
            f"{_fmt_td(r['natural_half_life']):12s}  "
            f"{_fmt_td(r['consolidation_window']):15s}  "
            f"{_fmt_td(r['pending_ttl']):12s}  "
            f"{r['tombstone_policy']:10s}  "
            f"{r['applies_to_modality'] or '-'}"
        )


# ---------------------------------------------------------------------------
# Sub-agent liveness — reads substrate_agent_heartbeat (written by the
# worker subprocess) so the inspect CLI can tell a live worker from a dead
# one across processes.
# ---------------------------------------------------------------------------


# Staleness thresholds, expressed against the worker's ~10s heartbeat
# cadence (substrate.agents.base._HEARTBEAT_INTERVAL_S). A beat within 3
# cadences is "live"; up to ~9 is "stale" (the worker may be briefly
# blocked or the host paused); beyond that the agent is treated as down.
_AGENT_LIVE_S = 30.0
_AGENT_STALE_S = 90.0


# The roster the worker subprocess is expected to run (Phase A + B). Listing
# them explicitly means a *missing* agent (never beat → no row) shows as
# DOWN rather than silently vanishing from the report. ``is_sentinel`` is
# shown so an operator can see which agent carries the never-throttled floor.
_EXPECTED_AGENTS: tuple[tuple[str, bool], ...] = (
    ("sentinel", True),
    ("curator", False),
    ("force-reject", False),
    ("partition-maintenance", False),
    ("parser", False),  # Phase D — heartbeats even when its tick is env-gated off
    ("associator", False),  # Phase E1 — same staged-rollout shape
    ("pattern-finder", False),  # Phase E2 — same staged-rollout shape
    ("critic", False),  # Phase F — same staged-rollout shape
    ("conductor", False),  # Phase F — adaptive policy loop (gated)
    ("reflector", False),  # Phase F — L3/L4 synthesis (gated)
    ("dreamer", False),  # Phase F — counterfactual exploration (gated)
    ("summarizer", False),  # retrospective summarization (gated)
)


def _classify_agent(age_seconds: Optional[float]) -> str:
    """Map heartbeat age → ``live`` / ``stale`` / ``down``."""
    if age_seconds is None:
        return "down"
    if age_seconds <= _AGENT_LIVE_S:
        return "live"
    if age_seconds <= _AGENT_STALE_S:
        return "stale"
    return "down"


async def _agent_liveness(conn: "asyncpg.Connection") -> list[dict]:
    """Merge the heartbeat table against the expected roster.

    Returns one dict per agent (expected roster first, in order, then any
    unrecognised agents that have beaten — e.g. a future sub-agent). Each
    dict carries ``name, status, level, is_sentinel, age_seconds,
    tick_count, pid, host``. An agent that has never beaten gets a
    synthesised ``down`` entry with ``age_seconds=None``.

    A missing ``substrate_agent_heartbeat`` table (inspecting a DB that
    predates this migration) is treated as "every agent down" rather than
    an error — the report degrades gracefully.
    """
    try:
        rows = await conn.fetch(
            """
            SELECT agent_name, pid, host, level, is_sentinel, tick_count,
                   EXTRACT(EPOCH FROM (now() - last_beat_at))::float AS age_seconds
              FROM substrate_agent_heartbeat
            """
        )
    except Exception:  # undefined table on a pre-migration DB, etc.
        rows = []

    by_name = {r["agent_name"]: r for r in rows}
    out: list[dict] = []

    def _entry_from_row(name: str, is_sentinel_default: bool, r) -> dict:
        if r is None:
            return {
                "name": name,
                "status": "down",
                "level": None,
                "is_sentinel": is_sentinel_default,
                "age_seconds": None,
                "tick_count": None,
                "pid": None,
                "host": None,
            }
        age = float(r["age_seconds"]) if r["age_seconds"] is not None else None
        return {
            "name": name,
            "status": _classify_agent(age),
            "level": r["level"],
            "is_sentinel": r["is_sentinel"],
            "age_seconds": age,
            "tick_count": r["tick_count"],
            "pid": r["pid"],
            "host": r["host"],
        }

    for name, is_sentinel_default in _EXPECTED_AGENTS:
        out.append(_entry_from_row(name, is_sentinel_default, by_name.get(name)))

    # Any beating agent not in the expected roster (future sub-agents).
    expected_names = {n for n, _ in _EXPECTED_AGENTS}
    for r in rows:
        if r["agent_name"] not in expected_names:
            out.append(_entry_from_row(r["agent_name"], bool(r["is_sentinel"]), r))

    return out


def _all_agents_down(rows: list[dict]) -> bool:
    """True when no agent is live or stale — i.e. the worker is down."""
    return not any(r["status"] in ("live", "stale") for r in rows)


def _format_agent_lines(rows: list[dict]) -> list[str]:
    """One human-readable line per agent for the summary + agents views."""
    lines: list[str] = []
    for r in rows:
        name = r["name"]
        status = r["status"]
        if r["age_seconds"] is None:
            detail = "no heartbeat (worker not running?)"
            level = "—"
        else:
            level = (r["level"] or "—").upper()
            detail = (
                f"beat {r['age_seconds']:.1f}s ago   "
                f"ticks {r['tick_count']}   pid {r['pid']}"
            )
        sentinel_tag = " [sentinel floor=FULL]" if r["is_sentinel"] else ""
        lines.append(
            f"{name:22s} {status:5s}  {level:8s}  {detail}{sentinel_tag}"
        )
    return lines


async def _print_agents(conn: "asyncpg.Connection") -> None:
    """``thoth substrate agents`` — full sub-agent liveness report."""
    now = datetime.now(timezone.utc)
    print(f"Sub-agent liveness @ {now.isoformat()}")
    print(
        f"(live ≤ {_AGENT_LIVE_S:.0f}s · stale ≤ {_AGENT_STALE_S:.0f}s · "
        "worker heartbeat cadence ~10s)"
    )
    print()
    rows = await _agent_liveness(conn)
    for line in _format_agent_lines(rows):
        print(f"  {line}")
    print()
    if _all_agents_down(rows):
        print(
            "⚠ substrate worker appears DOWN — no sub-agent has reported a "
            "heartbeat."
        )
        print(
            "  Without it: slices stay pending forever, decay never runs, "
            "embeddings never backfill."
        )
        print(
            "  Start it with `thoth substrate worker run` (or the "
            "hermes-substrate-worker systemd unit)."
        )
    else:
        print("✓ substrate worker is reporting heartbeats.")


# ---------------------------------------------------------------------------
# Operator health rollup (Phase G) — one-glance "is the substrate healthy?".
# ---------------------------------------------------------------------------


async def _layer_counts(conn: "asyncpg.Connection") -> dict:
    """Per-layer object counts. Upper-layer tables (l1_/l2_/l3_/l4_) may not
    exist on a pre-Phase-D DB; each is guarded so the rollup degrades."""
    async def _scalar(sql: str, default=0):
        try:
            return int(await conn.fetchval(sql) or 0)
        except Exception:
            return None  # table absent / not migrated this far

    # L0 counts are perceptual-only. ``substrate.*`` is the substrate's own
    # operational telemetry (now in ``substrate_telemetry``), not memory
    # awaiting consolidation — counting the historical ``substrate.self_state``
    # ghost rows here is what made `health` report a permanent 100% backlog.
    # See ``substrate.storage.streams.is_perceptual``. (The raw physical table
    # state stays visible via the default ``thoth substrate`` summary and
    # ``thoth substrate streams``.)
    try:
        l0 = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE sl.sentinel_state = 'passed')::int AS passed,
                COUNT(*) FILTER (
                    WHERE sl.consolidation_state = 'consolidated'
                )::int AS consolidated,
                COUNT(*) FILTER (
                    WHERE sl.consolidation_state = 'unconsolidated'
                      AND sl.sentinel_state = 'passed'
                )::int AS pending_parse
              FROM substrate_slices sl
              JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE st.name NOT LIKE 'substrate.%'
            """
        )
        l0_passed, l0_consolidated, l0_pending = (
            l0["passed"], l0["consolidated"], l0["pending_parse"],
        )
    except Exception:
        l0_passed = l0_consolidated = l0_pending = None

    return {
        "l0_passed": l0_passed,
        "l0_consolidated": l0_consolidated,
        "l0_pending_parse": l0_pending,
        "l1_entities": await _scalar("SELECT COUNT(*) FROM l1_entities"),
        "l1_relationships": await _scalar("SELECT COUNT(*) FROM l1_relationships"),
        "l2_associations": await _scalar("SELECT COUNT(*) FROM substrate_associations"),
        "l3_patterns": await _scalar("SELECT COUNT(*) FROM l3_patterns"),
        "l4_observations": await _scalar("SELECT COUNT(*) FROM l4_observations"),
    }


async def _parse_uuid(raw: str):
    from uuid import UUID

    try:
        return UUID(raw)
    except (ValueError, TypeError):
        print(f"not a valid slice id: {raw!r}")
        return None


async def _do_pin(conn, raw_id: str, pinned: bool) -> None:
    from substrate.l0.api import set_slice_pinned

    sid = await _parse_uuid(raw_id)
    if sid is None:
        return
    ok = await set_slice_pinned(sid, pinned, conn=conn)
    verb = "pinned" if pinned else "unpinned"
    print(f"{verb} slice {raw_id}" if ok else f"no slice {raw_id} (or no change)")


async def _do_forget_slice(conn, raw_id: str) -> None:
    from substrate.l0.api import forget_slice

    sid = await _parse_uuid(raw_id)
    if sid is None:
        return
    ok = await forget_slice(sid, conn=conn)
    print(
        f"forgot slice {raw_id} (salience→0; Curator releases it next cycle)"
        if ok else f"no slice {raw_id} (or already released)"
    )


async def _print_health(conn: "asyncpg.Connection") -> None:
    now = datetime.now(timezone.utc)
    print(f"Substrate health @ {now.isoformat()}")
    print()

    # 1. Worker liveness.
    agents = await _agent_liveness(conn)
    live = sum(1 for a in agents if a["status"] == "live")
    print(f"Worker: ", end="")
    if _all_agents_down(agents):
        print("⚠ DOWN — no sub-agent heartbeat (run `thoth substrate worker run`)")
    else:
        print(f"✓ up — {live}/{len(agents)} sub-agents live")

    # 2. Boot status.
    boot = await _boot_status_rows(conn)
    failed = sorted(m for m, st in boot.items() if st and not st.get("ok"))
    if failed:
        print(f"Boot:   ⚠ last boot FAILED for {', '.join(failed)}")
    elif boot:
        print(f"Boot:   ✓ {', '.join(sorted(boot))} OK")

    # 3. Coherence vital sign (Critic / L4). The Critic maintains a single
    # coherence row via upsert, so freshness is ``last_seen_at`` (bumped each
    # assessment) — NOT ``created_at`` (frozen at the row's first creation,
    # which would make "assessed Xm ago" grow forever even while healthy).
    try:
        coh = await conn.fetchrow(
            "SELECT score, last_seen_at FROM l4_observations WHERE kind='coherence' "
            "ORDER BY last_seen_at DESC LIMIT 1"
        )
    except Exception:
        coh = None
    if coh and coh["score"] is not None:
        age = _age_str(coh["last_seen_at"].isoformat(), now)
        print(f"Coherence: {coh['score']:.2f}  (assessed {age})")
    else:
        print("Coherence: (no assessment — Critic off or not yet run)")

    # 4. Layer counts.
    print()
    lc = await _layer_counts(conn)

    def _fmt(v):
        return "—" if v is None else f"{v:,}"

    print("Layers:")
    print(f"  L0 perception   passed {_fmt(lc['l0_passed'])}  "
          f"consolidated {_fmt(lc['l0_consolidated'])}  "
          f"awaiting-parse {_fmt(lc['l0_pending_parse'])}")
    print(f"  L1 knowledge    entities {_fmt(lc['l1_entities'])}  "
          f"relationships {_fmt(lc['l1_relationships'])}")
    print(f"  L2 associations {_fmt(lc['l2_associations'])}")
    print(f"  L3 patterns     {_fmt(lc['l3_patterns'])}")
    print(f"  L4 self-model   observations {_fmt(lc['l4_observations'])}")

    # 5. Backlog one-liner.
    pend = lc["l0_pending_parse"] or 0
    done = lc["l0_consolidated"] or 0
    if pend + done:
        ratio = pend / (pend + done)
        print()
        print(f"Consolidation backlog: {ratio:.0%} "
              f"({pend:,} awaiting parse / {pend + done:,} parseable)")

    # 6. Token usage — what the substrate spent running its own sub-agents.
    # Reads the non-perceptual substrate_agent_cost sink (one row per
    # auxiliary chat.completions.create, tagged by agent). Missing table on a
    # pre-cost DB degrades to "no token usage recorded" rather than erroring.
    await _print_health_token_usage(conn, now)


async def _print_health_token_usage(conn: "asyncpg.Connection", now: datetime) -> None:
    """Token-usage rollup for ``thoth substrate health``: per-agent SUM(total_
    tokens) + COUNT(*) over the last 1h and 24h, plus an overall 24h total.
    Best-effort — a missing/empty ``substrate_agent_cost`` prints a clean
    'no token usage recorded'."""
    try:
        rows = await conn.fetch(
            """
            SELECT agent,
                   COALESCE(SUM(total_tokens) FILTER (
                       WHERE at > now() - interval '1 hour'), 0)::bigint AS tok_1h,
                   COUNT(*) FILTER (
                       WHERE at > now() - interval '1 hour')::bigint AS calls_1h,
                   COALESCE(SUM(total_tokens) FILTER (
                       WHERE at > now() - interval '24 hours'), 0)::bigint AS tok_24h,
                   COUNT(*) FILTER (
                       WHERE at > now() - interval '24 hours')::bigint AS calls_24h
              FROM substrate_agent_cost
             WHERE at > now() - interval '24 hours'
             GROUP BY agent
             ORDER BY tok_24h DESC, agent ASC
            """
        )
    except Exception:
        rows = []

    print()
    print("Token usage:")
    if not rows:
        print("  (no token usage recorded)")
        return

    print(f"  {'agent':22s}  {'1h tokens':>12s}  {'1h calls':>8s}  "
          f"{'24h tokens':>12s}  {'24h calls':>9s}")
    total_1h = total_24h = calls_1h_total = calls_24h_total = 0
    for r in rows:
        total_1h += r["tok_1h"]
        total_24h += r["tok_24h"]
        calls_1h_total += r["calls_1h"]
        calls_24h_total += r["calls_24h"]
        print(
            f"  {r['agent']:22s}  {r['tok_1h']:>12,}  {r['calls_1h']:>8,}  "
            f"{r['tok_24h']:>12,}  {r['calls_24h']:>9,}"
        )
    print("  " + "-" * 70)
    print(
        f"  {'TOTAL':22s}  {total_1h:>12,}  {calls_1h_total:>8,}  "
        f"{total_24h:>12,}  {calls_24h_total:>9,}"
    )


# ---------------------------------------------------------------------------
# Substrate boot status — reads the state_meta KV rows that
# bootstrap_substrate writes on every boot attempt (success or failure),
# so an operator can see a *writer*-mode boot failure (which silently stops
# all perception) without grepping process logs.
# ---------------------------------------------------------------------------


# Key prefix must match hermes_bootstrap._BOOT_STATUS_KEY_PREFIX.
_BOOT_STATUS_KEY_PREFIX = "substrate.boot_status."
# Roster shown even when a mode has never booted, so a never-started worker
# is visible rather than simply absent.
_BOOT_MODES = ("writer", "worker")


def _age_str(iso: Optional[str], now: datetime) -> str:
    """Format an ISO-8601 timestamp as a coarse '12s ago' / '3.4m ago'."""
    if not iso:
        return "?"
    try:
        ts = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return "?"
    secs = max(0.0, (now - ts).total_seconds())
    if secs < 90:
        return f"{secs:.0f}s ago"
    if secs < 5400:
        return f"{secs / 60:.1f}m ago"
    return f"{secs / 3600:.1f}h ago"


async def _boot_status_rows(conn: "asyncpg.Connection") -> dict:
    """Return ``{mode: status_dict_or_None}`` parsed from state_meta. A
    missing table or unparseable value degrades to an empty/None entry
    rather than raising."""
    try:
        rows = await conn.fetch(
            "SELECT key, value FROM state_meta WHERE key LIKE $1",
            _BOOT_STATUS_KEY_PREFIX + "%",
        )
    except Exception:
        return {}
    out: dict = {}
    for r in rows:
        mode = r["key"][len(_BOOT_STATUS_KEY_PREFIX):]
        try:
            out[mode] = json.loads(r["value"]) if r["value"] else None
        except (ValueError, TypeError):
            out[mode] = None
    return out


def _format_boot_lines(status_by_mode: dict) -> list[str]:
    """One line per boot mode (expected roster first, then any extras)."""
    now = datetime.now(timezone.utc)
    lines: list[str] = []

    def _line(mode: str, st) -> str:
        if not st:
            return f"{mode:8s} (no boot recorded)"
        verdict = "OK" if st.get("ok") else "FAILED"
        age = _age_str(st.get("booted_at"), now)
        detail = f"pid {st.get('pid')}"
        if not st.get("ok") and st.get("error"):
            detail += f"   {st['error']}"
        return f"{mode:8s} {verdict:6s} {age:>10s}   {detail}"

    for mode in _BOOT_MODES:
        lines.append(_line(mode, status_by_mode.get(mode)))
    for mode, st in status_by_mode.items():
        if mode not in _BOOT_MODES:
            lines.append(_line(mode, st))
    return lines


async def _print_boot_status(conn: "asyncpg.Connection") -> None:
    """``thoth substrate boot`` — last boot outcome per process role."""
    now = datetime.now(timezone.utc)
    print(f"Substrate boot status @ {now.isoformat()}")
    print()
    status = await _boot_status_rows(conn)
    if not status:
        print(
            "  (no boot status recorded — bootstrap_substrate has not run "
            "against this database, or this deployment predates boot-status "
            "recording)"
        )
        return
    for line in _format_boot_lines(status):
        print(f"  {line}")
    failed = sorted(m for m, st in status.items() if st and not st.get("ok"))
    if failed:
        print()
        print(f"⚠ last boot FAILED for: {', '.join(failed)} — see process logs.")


# ---------------------------------------------------------------------------
# Phase D: L1 + Parser printers.
# ---------------------------------------------------------------------------


async def _print_l1_entities(conn, *, kind=None, limit=20) -> None:
    rows = await conn.fetch(
        """
        SELECT e.name, e.entity_type, e.summary, e.salience_score,
               e.last_seen_at,
               (SELECT COUNT(*) FROM l1_relationships r
                 WHERE r.subject_id = e.id OR r.object_id = e.id) AS rels,
               (SELECT COUNT(*) FROM l1_citations c WHERE c.entity_id = e.id) AS cites
          FROM l1_entities e
         WHERE ($1::text IS NULL OR e.entity_type = $1)
         ORDER BY e.salience_score DESC, e.last_seen_at DESC
         LIMIT $2
        """,
        kind,
        limit,
    )
    if not rows:
        print("(no L1 entities — is the Parser running with HERMES_SUBSTRATE_PARSER=1?)")
        return
    print(f"{'name':32s}  {'type':10s}  {'sal':>5s}  {'rels':>4s}  {'cites':>5s}  summary")
    print("-" * 100)
    for r in rows:
        summary = (r["summary"] or "")[:40]
        print(
            f"{r['name'][:32]:32s}  {r['entity_type']:10s}  "
            f"{r['salience_score']:>5.2f}  {r['rels']:>4d}  {r['cites']:>5d}  {summary}"
        )


async def _print_l1_relationships(conn, *, limit=20) -> None:
    rows = await conn.fetch(
        """
        SELECT s.name AS subj, r.predicate, o.name AS obj, r.confidence, r.last_seen_at
          FROM l1_relationships r
          JOIN l1_entities s ON s.id = r.subject_id
          JOIN l1_entities o ON o.id = r.object_id
         ORDER BY r.last_seen_at DESC
         LIMIT $1
        """,
        limit,
    )
    if not rows:
        print("(no L1 relationships)")
        return
    for r in rows:
        print(f"  {r['subj']} --{r['predicate']}--> {r['obj']}  (conf {r['confidence']:.2f})")


# ── L1 curation (mutating) ────────────────────────────────────────────


async def _resolve_entity(conn, name: str):
    """Resolve a single entity by exact name. Prints an error + returns
    None on no/ambiguous match."""
    from substrate.l1 import store

    matches = await store.find_entities_by_name(name, fuzzy=False, limit=5, conn=conn)
    if not matches:
        print(f"no entity named {name!r}")
        return None
    if len(matches) > 1:
        kinds = ", ".join(f"{m.name} ({m.entity_type})" for m in matches)
        print(f"ambiguous — {len(matches)} entities match {name!r}: {kinds}")
        return None
    return matches[0]


async def _print_l1_dupes(conn, *, threshold=0.6, limit=20) -> None:
    from substrate.l1 import store

    pairs = await store.duplicate_candidates(threshold=threshold, limit=limit, conn=conn)
    if not pairs:
        print(f"(no likely-duplicate entity pairs above similarity {threshold})")
        return
    print("Likely duplicates (review, then `l1 merge --from X --into Y`):")
    for p in pairs:
        print(
            f"  [{p['kind']}] {p['a_name']} ({p['a_cites']} cites) ~ "
            f"{p['b_name']} ({p['b_cites']} cites)  sim {p['sim']:.2f}"
        )


async def _do_l1_merge(conn, from_name: str, into_name: str) -> None:
    from substrate.l1 import store

    frm = await _resolve_entity(conn, from_name)
    into = await _resolve_entity(conn, into_name)
    if frm is None or into is None:
        return
    if frm.id == into.id:
        print("from and into are the same entity; nothing to merge")
        return
    ok = await store.merge_entities(frm.id, into.id, conn=conn)
    print(f"merged {from_name!r} into {into_name!r}" if ok else "merge failed")


async def _do_l1_forget(conn, name: str) -> None:
    from substrate.l1 import store

    ent = await _resolve_entity(conn, name)
    if ent is None:
        return
    ok = await store.forget_entity(ent.id, conn=conn)
    print(f"forgot {name!r}" if ok else "nothing forgotten")


async def _do_l1_edit(conn, name: str, *, summary=None, rename=None) -> None:
    from substrate.l1 import store

    ent = await _resolve_entity(conn, name)
    if ent is None:
        return
    if summary is None and rename is None:
        print("nothing to edit — pass --summary and/or --rename")
        return
    ok = await store.edit_entity(
        ent.id, summary=summary, canonical_name=rename, conn=conn
    )
    print(f"updated {name!r}" if ok else "no change")


async def _print_parser_summary(conn) -> None:
    now = datetime.now(timezone.utc)
    print(f"Parser state @ {now.isoformat()}")
    print()
    row = await conn.fetchrow(
        """
        SELECT COUNT(*)::int AS calls,
               COALESCE(SUM(entities_emitted),0)::int AS ents,
               COALESCE(SUM(relationships_emitted),0)::int AS rels,
               COALESCE(SUM(slices_consolidated),0)::int AS consolidated,
               COALESCE(AVG(latency_ms),0)::int AS avg_ms
          FROM substrate_parser_log
         WHERE t_call > now() - interval '24 hours'
        """
    )
    print("Last 24h:")
    print(f"  calls                {row['calls']}")
    print(f"  entities emitted     {row['ents']}")
    print(f"  relationships emitted {row['rels']}")
    print(f"  slices consolidated  {row['consolidated']}")
    print(f"  avg latency          {row['avg_ms']} ms")
    print()
    outcomes = await conn.fetch(
        """
        SELECT outcome, COUNT(*)::int AS n FROM substrate_parser_log
         WHERE t_call > now() - interval '24 hours' GROUP BY outcome ORDER BY n DESC
        """
    )
    print("Outcomes (24h):")
    if not outcomes:
        print("  (no parser activity — HERMES_SUBSTRATE_PARSER off, or worker down?)")
    for o in outcomes:
        print(f"  {o['outcome']:12s} {o['n']}")
    totals = await conn.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE consolidation_state='consolidated')::int AS consolidated,
            COUNT(*) FILTER (WHERE consolidation_state='unconsolidated' AND sentinel_state='passed')::int AS pending
          FROM substrate_slices
        """
    )
    print()
    print("L0 consolidation:")
    print(f"  consolidated slices  {totals['consolidated']}")
    print(f"  awaiting parse       {totals['pending']}")


async def _print_parser_recent(conn, *, limit=20) -> None:
    rows = await conn.fetch(
        """
        SELECT t_call, session_id, batch_size, entities_emitted,
               relationships_emitted, slices_consolidated, latency_ms, outcome, error
          FROM substrate_parser_log ORDER BY t_call DESC LIMIT $1
        """,
        limit,
    )
    if not rows:
        print("(no parser_log rows yet)")
        return
    for r in rows:
        ts = r["t_call"].isoformat() if r["t_call"] else "-"
        extra = f" err={r['error']}" if r["error"] else ""
        print(
            f"  [{ts}] {r['outcome']:11s} batch={r['batch_size']} "
            f"ents={r['entities_emitted']} rels={r['relationships_emitted']} "
            f"consol={r['slices_consolidated']} {r['latency_ms']}ms{extra}"
        )


async def _print_l2_associations(conn, *, entity=None, limit=20) -> None:
    if entity:
        rows = await conn.fetch(
            """
            SELECT s.name AS src, o.name AS dst, a.edge_type, a.weight
              FROM substrate_associations a
              JOIN l1_entities s ON s.id = a.src_id
              JOIN l1_entities o ON o.id = a.dst_id
             WHERE s.name ILIKE $1 OR o.name ILIKE $1
             ORDER BY a.weight DESC LIMIT $2
            """,
            f"%{entity}%",
            limit,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT s.name AS src, o.name AS dst, a.edge_type, a.weight
              FROM substrate_associations a
              JOIN l1_entities s ON s.id = a.src_id
              JOIN l1_entities o ON o.id = a.dst_id
             ORDER BY a.weight DESC LIMIT $1
            """,
            limit,
        )
    if not rows:
        print("(no L2 associations — is the Associator running with "
              "HERMES_SUBSTRATE_ASSOCIATOR=1?)")
        return
    for r in rows:
        print(
            f"  {r['src']} <-> {r['dst']}  [{r['edge_type']}]  weight {r['weight']:.1f}"
        )


async def _print_l3_patterns(conn, *, kind=None, limit=20) -> None:
    rows = await conn.fetch(
        """
        SELECT kind, statement, salience_score, confidence,
               jsonb_array_length(cites) AS n_cites, last_seen_at
          FROM l3_patterns
         WHERE ($1::text IS NULL OR kind = $1)
         ORDER BY salience_score DESC, last_seen_at DESC
         LIMIT $2
        """,
        kind,
        limit,
    )
    if not rows:
        print("(no L3 patterns — is the Pattern-finder running with "
              "HERMES_SUBSTRATE_PATTERNFINDER=1?)")
        return
    for r in rows:
        print(
            f"  [{r['kind']:18s}] sal={r['salience_score']:.2f} "
            f"conf={r['confidence']:.2f} cites={r['n_cites']}  {r['statement']}"
        )


async def _print_conductor(conn, *, limit=15) -> None:
    try:
        latest = await conn.fetchrow(
            "SELECT backlog_ratio, forecast FROM substrate_conductor_log "
            "ORDER BY at DESC LIMIT 1"
        )
        rows = await conn.fetch(
            "SELECT at, backlog_ratio, forecast, targets FROM substrate_conductor_log "
            "ORDER BY at DESC LIMIT $1",
            limit,
        )
    except Exception:
        print("(no conductor log — is the Conductor running with "
              "HERMES_SUBSTRATE_CONDUCTOR=1?)")
        return
    if latest is not None:
        print(f"Backlog forecast (EMA): {latest['forecast']:.2%}  "
              f"(latest observed {latest['backlog_ratio']:.2%})")
        print()
    if not rows:
        print("(no conductor decisions logged yet)")
        return
    print("Recent decisions:")
    for r in rows:
        ts = r["at"].isoformat() if r["at"] else "-"
        targets = r["targets"] or {}
        parser_t = targets.get("parser", "?")
        print(f"  [{ts}] backlog {r['backlog_ratio']:.0%} → parser={parser_t}")


async def _print_dreamer(conn, *, limit=10) -> None:
    try:
        rows = await conn.fetch(
            "SELECT seed, exploration, created_at FROM substrate_dreamer_log "
            "ORDER BY created_at DESC LIMIT $1",
            limit,
        )
    except Exception:
        print("(no dreamer log — is the Dreamer running with "
              "HERMES_SUBSTRATE_DREAMER=1?)")
        return
    if not rows:
        print("(no dreamer explorations yet)")
        return
    for r in rows:
        ts = r["created_at"].isoformat() if r["created_at"] else "-"
        print(f"  [{ts}] seed: {r['seed'][:70]}")
        print(f"      {r['exploration'][:200]}")


async def _print_l4(conn, *, subject=None, limit=20) -> None:
    coherence = await conn.fetchrow(
        "SELECT score, statement, created_at FROM l4_observations "
        "WHERE kind='coherence' ORDER BY created_at DESC LIMIT 1"
    )
    if coherence and coherence["score"] is not None:
        print(f"Coherence (latest): {coherence['score']:.2f}  —  {coherence['statement']}")
    else:
        print("Coherence (latest): (none — is the Critic running with "
              "HERMES_SUBSTRATE_CRITIC=1?)")
    print()
    rows = await conn.fetch(
        """
        SELECT kind, subject, statement, score, created_at
          FROM l4_observations
         WHERE ($1::text IS NULL OR subject = $1)
         ORDER BY created_at DESC LIMIT $2
        """,
        subject,
        limit,
    )
    if not rows:
        print("(no L4 observations)")
        return
    print("Recent observations:")
    for r in rows:
        score = f" [{r['score']:.2f}]" if r["score"] is not None else ""
        print(f"  ({r['kind']}/{r['subject']}){score}  {r['statement']}")


# ---------------------------------------------------------------------------
# Query helpers shared across printers.
# ---------------------------------------------------------------------------


async def _stream_counts(conn: "asyncpg.Connection") -> list[dict]:
    """Streams joined with their slice counts, ordered by name. Empty
    streams (no slices yet) still appear with count 0."""
    rows = await conn.fetch(
        """
        SELECT st.stream_id, st.name, st.family, st.modality,
               st.lifecycle_state,
               COUNT(sl.slice_id) AS slice_count
          FROM substrate_streams st
          LEFT JOIN substrate_slices sl ON sl.stream_id = st.stream_id
         GROUP BY st.stream_id
         ORDER BY st.name
        """
    )
    return [dict(r) for r in rows]


async def _slice_state_counts(conn: "asyncpg.Connection") -> dict[str, int]:
    """``sentinel_state → count`` across all slices."""
    rows = await conn.fetch(
        """
        SELECT sentinel_state, COUNT(*) AS n
          FROM substrate_slices
         GROUP BY sentinel_state
        """
    )
    return {r["sentinel_state"]: int(r["n"]) for r in rows}


async def _pending_info(conn: "asyncpg.Connection") -> dict:
    """Depth of the pending queue + oldest pending age (or None)."""
    row = await conn.fetchrow(
        """
        SELECT COUNT(*)::int AS depth,
               MIN(pending_committed_at) AS oldest
          FROM substrate_slices
         WHERE sentinel_state = 'pending'
        """
    )
    depth = int(row["depth"] or 0)
    oldest_age: Optional[timedelta] = None
    if depth > 0 and row["oldest"] is not None:
        oldest_age = datetime.now(timezone.utc) - row["oldest"]
    return {"depth": depth, "oldest_age": oldest_age}


def _short_payload(payload) -> str:
    """One-line payload preview for the slices printer."""
    if payload is None:
        return "(no payload — blob ref?)"
    s = str(payload)
    if len(s) > 80:
        return s[:80] + "…"
    return s


# ---------------------------------------------------------------------------
# Phase B printers — Curator summary, histogram, recent, pressure.
# ---------------------------------------------------------------------------


async def _print_curator_summary(conn: "asyncpg.Connection") -> None:
    """Default ``thoth substrate curator`` output. Matches the
    format documented in Phase B spec §9.2."""
    now = datetime.now(timezone.utc)
    print(f"Curator state @ {now.isoformat()}")
    print()

    # Release stats — count by tombstone_policy. Released slices keep
    # their consolidation_state='released' even after payload is nulled.
    rel = await conn.fetch(
        """
        SELECT dp.tombstone_policy AS policy, COUNT(sl.slice_id)::int AS n
          FROM substrate_slices sl
          JOIN substrate_streams st ON st.stream_id = sl.stream_id
          JOIN substrate_decay_profiles dp ON dp.profile_id = st.decay_profile_id
         WHERE sl.consolidation_state = 'released'
         GROUP BY dp.tombstone_policy
        """
    )
    rel_by_policy = {r["policy"]: r["n"] for r in rel}
    rel_total = sum(rel_by_policy.values())
    print(f"Released:                   {rel_total:,} slices")
    print("  by policy:")
    for policy in ("thin", "full", "none"):
        label = policy + (" (default)" if policy == "thin" else "")
        print(f"    {label:24s}{rel_by_policy.get(policy, 0):,}")

    print()

    # Pending consolidation — anything passed + unconsolidated.
    pending = await conn.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (
                WHERE sl.sentinel_state = 'passed'
                  AND sl.consolidation_state = 'unconsolidated'
            )::int AS total,
            COUNT(*) FILTER (
                WHERE sl.sentinel_state = 'passed'
                  AND sl.consolidation_state = 'unconsolidated'
                  AND sl.salience_score < 0.1
            )::int AS low_salience,
            COUNT(*) FILTER (
                WHERE sl.sentinel_state = 'passed'
                  AND sl.consolidation_state = 'unconsolidated'
                  AND sl.ingest_time_world + (dp.consolidation_window * 0.9) < now()
                  AND sl.ingest_time_world + dp.consolidation_window > now()
            )::int AS near_window
          FROM substrate_slices sl
          JOIN substrate_streams st ON st.stream_id = sl.stream_id
          JOIN substrate_decay_profiles dp ON dp.profile_id = st.decay_profile_id
         -- perceptual streams only; substrate.* is operational telemetry
         -- (substrate_telemetry), not memory pending consolidation.
         WHERE st.name NOT LIKE 'substrate.%'
        """
    )
    print(f"Pending consolidation:        {pending['total']:,} slices")
    print(f"  with salience < 0.1            {pending['low_salience']:,}")
    print(
        f"  approaching consolidation_window (within 10% of profile setting): "
        f"{pending['near_window']:,}"
    )

    print()

    # Recent curator emissions over last hour. Counts per event kind.
    # Curator emissions are operational telemetry (substrate_telemetry),
    # not perceptual slices.
    recent = await conn.fetch(
        """
        SELECT event, COUNT(*)::int AS n
          FROM substrate_telemetry
         WHERE at > now() - interval '1 hour'
           AND event LIKE 'curator.%'
         GROUP BY event
        """
    )
    recent_by_event = {r["event"]: r["n"] for r in recent}
    rel_n = recent_by_event.get("curator.release", 0)
    alarm_n = recent_by_event.get(
        "curator.pathological_forgetting_alarm", 0
    )
    print(f"Recent curator.release emissions: {rel_n} in last hour")
    print(f"Recent pathological-forgetting alarms: {alarm_n} in last hour")


async def _print_curator_histogram(conn: "asyncpg.Connection") -> None:
    """10-bucket salience histogram per active profile.

    Bucket 0 covers [0.0, 0.1); bucket 9 covers [0.9, 1.0]. Released
    slices are excluded (their salience is 0 by definition and would
    dominate bucket 0).
    """
    rows = await conn.fetch(
        """
        WITH bucketed AS (
            SELECT dp.name AS profile,
                   LEAST(9, GREATEST(0, FLOOR(sl.salience_score * 10)::int)) AS bucket
              FROM substrate_slices sl
              JOIN substrate_streams st ON st.stream_id = sl.stream_id
              JOIN substrate_decay_profiles dp ON dp.profile_id = st.decay_profile_id
             WHERE sl.consolidation_state <> 'released'
               AND sl.sentinel_state = 'passed'
               -- perceptual streams only (substrate.* is operational telemetry).
               AND st.name NOT LIKE 'substrate.%'
        )
        SELECT profile, bucket, COUNT(*)::int AS n
          FROM bucketed
         GROUP BY profile, bucket
         ORDER BY profile, bucket
        """
    )
    if not rows:
        print("(no active passed slices to bucket)")
        return

    by_profile: dict[str, dict[int, int]] = {}
    for r in rows:
        by_profile.setdefault(r["profile"], {})[r["bucket"]] = r["n"]

    bucket_labels = [
        "0.0–0.1", "0.1–0.2", "0.2–0.3", "0.3–0.4", "0.4–0.5",
        "0.5–0.6", "0.6–0.7", "0.7–0.8", "0.8–0.9", "0.9–1.0",
    ]
    for profile, buckets in by_profile.items():
        total = sum(buckets.values())
        print(f"{profile}  (total = {total})")
        for i, label in enumerate(bucket_labels):
            n = buckets.get(i, 0)
            bar = "█" * min(50, n)
            print(f"  {label}  {n:>6d}  {bar}")
        print()


async def _print_curator_recent(
    conn: "asyncpg.Connection", *, limit: int
) -> None:
    """Most-recent N curator.* emissions from substrate_telemetry."""
    rows = await conn.fetch(
        """
        SELECT at, event, payload
          FROM substrate_telemetry
         WHERE event LIKE 'curator.%'
         ORDER BY at DESC
         LIMIT $1
        """,
        limit,
    )
    if not rows:
        print("(no curator emissions found)")
        return
    print(f"Most-recent {len(rows)} curator emissions:")
    for r in rows:
        ev = r["event"]
        ts = r["at"].isoformat() if r["at"] else "-"
        if ev == "curator.release":
            extra = (
                f"slice={r['payload'].get('slice_id', '?')[:8]} "
                f"policy={r['payload'].get('tombstone_policy')} "
                f"salience={r['payload'].get('salience_at_release'):.4f}"
            )
        elif ev == "curator.pathological_forgetting_alarm":
            extra = (
                f"slice={r['payload'].get('slice_id', '?')[:8]} "
                f"age={r['payload'].get('age_seconds')}s "
                f"window={r['payload'].get('consolidation_window_seconds')}s "
                f"bumped→{r['payload'].get('bumped_to'):.4f}"
            )
        else:
            extra = str(r["payload"])
        print(f"  [{ts}] {ev}  {extra}")


async def _print_curator_pressure(conn: "asyncpg.Connection") -> None:
    """Per-stream salience pressure — density + update rate.

    Surfaces design §5.6's Conductor opportunity-forecast inputs. Phase
    B doesn't consume programmatically; the values are surfaced so
    operators can develop intuition before Phase F's real Conductor.
    """
    rows = await conn.fetch(
        """
        SELECT st.name,
               COALESCE(AVG(sl.salience_score), 0)::real AS density,
               COUNT(sl.slice_id)::int AS count,
               COUNT(*) FILTER (
                   WHERE sl.salience_updated_at > now() - interval '5 minutes'
               )::int AS update_rate
          FROM substrate_streams st
          LEFT JOIN substrate_slices sl
                 ON sl.stream_id = st.stream_id
                AND sl.consolidation_state <> 'released'
         WHERE st.lifecycle_state = 'active'
         GROUP BY st.name
         ORDER BY st.name
        """
    )
    if not rows:
        print("(no active streams)")
        return
    print(
        f"{'stream':50s}  {'count':>8s}  {'density':>9s}  {'5m_updates':>11s}"
    )
    print("-" * 90)
    for r in rows:
        print(
            f"{r['name']:50s}  {r['count']:>8d}  "
            f"{r['density']:>9.4f}  {r['update_rate']:>11d}"
        )


def _fmt_td(value) -> str:
    """Format a timedelta as a short ``2h15m`` / ``45s`` / ``500ms`` style.

    Used by the profiles printer where INTERVAL columns come back as
    :class:`timedelta` (asyncpg native).
    """
    if value is None:
        return "-"
    if isinstance(value, timedelta):
        total_seconds = int(value.total_seconds())
        if total_seconds == 0:
            ms = int(value.microseconds / 1000)
            return f"{ms}ms"
        if total_seconds < 60:
            return f"{total_seconds}s"
        if total_seconds < 3600:
            return f"{total_seconds // 60}m"
        hours = total_seconds // 3600
        mins = (total_seconds % 3600) // 60
        if mins:
            return f"{hours}h{mins}m"
        return f"{hours}h"
    return str(value)


__all__ = ["register_subparser"]
