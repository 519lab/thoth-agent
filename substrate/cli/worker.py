"""``thoth substrate worker run`` — substrate sub-agent worker subprocess.

Runs Sentinel + Curator + ForceRejectWorker + PartitionMaintenanceWorker
(plus the Conductor handle) in a dedicated process with its own asyncpg
pool and event loop. Started by systemd alongside the gateway; without
it, sub-agent tick loops never fire (slices accumulate as ``pending``
and never get embedded, salience decay never runs).

Why a separate process: the gateway hosts BOTH a perception emitter
(async hooks on the main loop) AND ``hermes_db.run_sync`` callers
(session/telegram handlers offloading to a worker-thread ``_sync_loop``).
Both share one ``asyncpg.Pool`` singleton, but asyncpg connections are
loop-bound. Driving sub-agent ticks on the gateway's main loop while
``run_sync`` is using the same pool from the worker thread causes
intermittent ``cannot switch to state N; another operation (M) is in
progress`` errors (the 2026-05-26 production incident). Splitting
sub-agents into their own subprocess gives each process exactly one
loop and one pool — no cross-loop contention possible.

Surface:

    thoth substrate worker run    # blocks until SIGINT/SIGTERM

Lifecycle:

* Init asyncpg pool from ``HERMES_PG_DSN``.
* Boot substrate in worker mode (no hook bind, no recall log — those
  belong to the writer processes).
* Wait on a stop event until SIGINT/SIGTERM, then clean shutdown.
* Exit non-zero on init failure so systemd ``Restart=on-failure``
  re-spawns us; that's the desired behaviour because without the
  worker no Curator decay / embedding backfill / Sentinel decisions
  happen.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add the ``thoth substrate worker`` subcommand tree.

    Wired into the existing ``substrate`` parser by
    :func:`substrate.cli.inspect.register_subparser`.
    """
    worker_p = subparsers.add_parser(
        "worker",
        help="Substrate sub-agent worker (Sentinel + Curator subprocess)",
        description="Background process that runs the substrate sub-agent "
        "tick loops (Sentinel, Curator, ForceRejectWorker, "
        "PartitionMaintenanceWorker). Started by the systemd unit "
        "``hermes-substrate-worker.service`` alongside the gateway. "
        "Without this process running, slices accumulate pending forever "
        "and embeddings never backfill.",
    )
    worker_sub = worker_p.add_subparsers(dest="worker_command")

    run_p = worker_sub.add_parser(
        "run",
        help="Run the substrate sub-agent worker until interrupted",
        description="Blocking foreground run. Used by the systemd unit; "
        "operators rarely invoke this directly unless debugging.",
    )
    run_p.set_defaults(func=_cmd_worker_run)

    worker_p.set_defaults(func=_cmd_worker_help)


def _cmd_worker_help(args: argparse.Namespace) -> int:
    print(
        "usage: thoth substrate worker run",
        file=sys.stderr,
    )
    return 2


def _cmd_worker_run(args: argparse.Namespace) -> int:
    """Sync entry — drives the async worker loop until SIGINT/SIGTERM."""
    return asyncio.run(_run_worker_async())


async def _run_worker_async() -> int:
    import logging
    import os

    import hermes_db

    # Configure logging to stderr; systemd captures into journal.
    logging.basicConfig(
        level=os.environ.get("HERMES_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("substrate.worker")

    dsn = os.environ.get("THOTH_PG_DSN")
    if not dsn:
        log.error(
            "THOTH_PG_DSN not set; the substrate worker requires PG. "
            "Set it in ~/.hermes/.env or the systemd unit's EnvironmentFile."
        )
        return 1

    log.info("substrate worker starting — dsn=%s", _redacted(dsn))
    # ``hermes_cli.main.main()`` eagerly binds the global asyncpg pool to
    # ``hermes_db._sync_loop`` via ``ensure_pool_sync()``. This worker runs
    # its own ``asyncio.run`` loop, and asyncpg pools are loop-bound — so we
    # must drop any inherited pool and re-init on THIS loop. Otherwise every
    # DB call (alembic check, stream registration, sub-agent ticks) raises
    # ``got Future ... attached to a different loop`` and the worker
    # crash-loops on boot.
    hermes_db.reset_pool_for_new_loop()
    try:
        await hermes_db.init(dsn)
    except Exception:
        log.exception("hermes_db.init failed; exiting 1")
        return 1

    from hermes_bootstrap import bootstrap_substrate

    substrate = await bootstrap_substrate(log=log, mode="worker")
    if substrate is None:
        log.error("bootstrap_substrate(mode=worker) returned None; exiting 1")
        await hermes_db.close()
        return 1

    # Wait on a stop event tripped by SIGINT/SIGTERM.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows / non-POSIX — fall back to default Ctrl-C handling.
            pass

    log.info(
        "substrate worker running — %d sub-agent(s) ticking",
        len(getattr(substrate, "_subagents", {})),
    )

    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass

    log.info("substrate worker stopping — shutdown signal received")
    try:
        await substrate.shutdown()
    except Exception:
        log.exception("substrate.shutdown raised; continuing to pool close")
    try:
        await hermes_db.close()
    except Exception:
        log.exception("hermes_db.close raised; exiting anyway")
    log.info("substrate worker stopped cleanly")
    return 0


def _redacted(dsn: str) -> str:
    """Hide the password in DSN logs."""
    # postgresql://user:pw@host:port/db → postgresql://user:***@host:port/db
    try:
        from urllib.parse import urlparse, urlunparse

        u = urlparse(dsn)
        if u.password:
            netloc = (
                f"{u.username}:***@{u.hostname}"
                f"{':' + str(u.port) if u.port else ''}"
            )
            return urlunparse((u.scheme, netloc, u.path, u.params, u.query, u.fragment))
    except Exception:
        pass
    return dsn
