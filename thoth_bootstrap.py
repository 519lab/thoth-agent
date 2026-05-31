"""Windows UTF-8 bootstrap for Hermes entry points.

Python on Windows has two long-standing text-encoding footguns:

1. ``sys.stdout`` / ``sys.stderr`` are bound to the console code page
   (``cp1252`` on US-locale installs), so ``print("café")`` crashes with
   ``UnicodeEncodeError: 'charmap' codec can't encode character``.

2. Child processes spawned via ``subprocess`` don't know to use UTF-8
   unless ``PYTHONUTF8`` and/or ``PYTHONIOENCODING`` are set in their
   environment — so any Python subprocess (the execute_code sandbox,
   delegation children, linter subprocesses, etc.) inherits the same
   cp1252 defaults and hits the same UnicodeEncodeError.

This module fixes both on Windows *only* — POSIX is untouched.  It
should be imported at the very top of every Hermes entry point
(``hermes``, ``hermes-agent``, ``hermes-acp``, ``python -m gateway.run``,
``batch_runner.py``, ``cron/scheduler.py``) before any other imports
that might do file I/O or print to stdout.

What this module does on Windows:

  - Sets ``os.environ["PYTHONUTF8"] = "1"`` (PEP 540 UTF-8 mode) so
    every child process we spawn uses UTF-8 for ``open()`` and stdio.
  - Sets ``os.environ["PYTHONIOENCODING"] = "utf-8"`` for belt-and-
    suspenders — some tools read this instead of / in addition to
    ``PYTHONUTF8``.
  - Reconfigures ``sys.stdout`` / ``sys.stderr`` to UTF-8 in the current
    process, using the ``reconfigure()`` API (Python 3.7+).  This fixes
    ``print("café")`` in the parent without a re-exec.

What this module does NOT do:

  - It does not re-exec Python with ``-X utf8``, so ``open()`` calls in
    the *current* process still default to locale encoding.  Those need
    an explicit ``encoding="utf-8"`` at the call site (lint rule
    ``PLW1514`` / ``PYI058``).  Ruff is the right tool for that sweep.

What this module does on POSIX:

  - Nothing.  POSIX systems are already UTF-8 by default in 99% of cases,
    and we don't want to touch ``LANG``/``LC_*`` behavior that users may
    have configured intentionally.  If someone hits a C/POSIX locale on
    Linux, they can export ``PYTHONUTF8=1`` themselves — we won't override.

Idempotent: safe to call multiple times.  ``_bootstrap_once`` guards
against double-reconfigure.
"""

from __future__ import annotations

import os
import sys

_IS_WINDOWS = sys.platform == "win32"
_bootstrap_applied = False


def apply_windows_utf8_bootstrap() -> bool:
    """Apply the Windows UTF-8 bootstrap if we're on Windows.

    Returns True if bootstrap was applied (i.e. we're on Windows and
    haven't already done this), False otherwise.  The return value is
    advisory — callers normally don't need it, but tests may want to
    assert the path was taken.

    Idempotent: subsequent calls after the first are a no-op.
    """
    global _bootstrap_applied

    if not _IS_WINDOWS:
        return False
    if _bootstrap_applied:
        return False

    # 1. Child processes inherit these and run in UTF-8 mode.
    #    We use setdefault() rather than overwriting so the user can
    #    explicitly opt out by setting PYTHONUTF8=0 in their environment
    #    (or PYTHONIOENCODING=something-else) if they really want to.
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # 2. Reconfigure the current process's stdio to UTF-8.  Needed
    #    because os.environ changes don't retroactively rebind sys.stdout
    #    — those were bound at interpreter startup based on the console
    #    code page.  ``reconfigure`` is a TextIOWrapper method since 3.7.
    #
    #    errors="replace" means that if we ever *read* something from
    #    stdin that isn't UTF-8 (unlikely but possible with piped input
    #    from legacy tools), we'll get U+FFFD replacement chars rather
    #    than a crash.  Output is pure UTF-8.
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # Not a TextIOWrapper (could be redirected to a BytesIO in
            # tests, or a non-standard stream in some embedded cases).
            # Skip silently — the env-var fix is still in effect for
            # child processes, which is the bigger win.
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # Already closed, or someone replaced it with something
            # non-reconfigurable.  Non-fatal.
            pass

    # stdin is reconfigured separately with errors="replace" too — input
    # from a legacy pipe shouldn't crash the process.
    stdin = getattr(sys, "stdin", None)
    if stdin is not None:
        reconfigure = getattr(stdin, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    _bootstrap_applied = True
    return True


# Apply on import — entry points just need ``import hermes_bootstrap``
# (or ``from hermes_bootstrap import apply_windows_utf8_bootstrap``) at
# the very top of their module, before importing anything else.  The
# import side effect does the right thing.
apply_windows_utf8_bootstrap()

# hermes→thoth env bridge (rename Phase 2): mirror HERMES_* <-> THOTH_* in
# os.environ so either spelling works, BEFORE any HERMES_*/THOTH_* read. This
# is the universal hook — every shipped entry point imports hermes_bootstrap
# first (same invariant the Windows bootstrap relies on). Pure-stdlib + guarded
# so a partial install can never brick startup. (load_hermes_dotenv re-runs it
# after the .env is loaded; see hermes_cli/env_loader.py.)
try:
    from thoth_env import normalize_thoth_env as _normalize_thoth_env
    from thoth_env import normalize_thoth_home_env as _normalize_thoth_home_env

    # Home keys first (rename Phase 3) so HERMES_HOME/THOTH_HOME agree before
    # any home read; then the general env mirror (Phase 2). Both are pure env
    # ops (no filesystem) — symlink/migration happens only in install/update.
    _normalize_thoth_home_env()
    _normalize_thoth_env()
except Exception:
    pass


# ---------------------------------------------------------------------------
# PG pool bootstrap helper (Phase 0 Task 22)
# ---------------------------------------------------------------------------

_db_initialized = False


def init_db_sync() -> None:
    """Bootstrap helper: init PG pool from HERMES_PG_DSN; register atexit close.

    Idempotent — subsequent calls after the first are a no-op.  Sync entry
    points (run_agent.py, cli.py, cron/scheduler.py, acp_adapter/entry.py) call
    this once at the top of main().  Pure-async entry points (gateway/run.py)
    call ``await hermes_db.init(dsn)`` directly inside their asyncio.run().

    Raises RuntimeError if HERMES_PG_DSN is not set in the environment.

    Drives the init via ``hermes_db.run_sync`` so the pool binds to the
    always-running DB loop rather than a transient ``asyncio.run`` loop.
    asyncpg pools are loop-bound; an ``asyncio.run(hermes_db.init(dsn))``
    call would create a fresh loop, bind the pool to it, then close that
    loop on return — leaving every subsequent ``run_sync`` call holding a
    pool against a dead loop. The DB loop runs for the lifetime of the
    process, so the pool stays valid across all ``run_sync`` calls.
    """
    global _db_initialized
    import atexit
    import os
    import thoth_db

    # Re-check ``_pool`` directly each call. Some test fixtures close
    # the pool between tests (e.g. ``hermes_db_initialized_sync`` in
    # ``tests/conftest.py``); the module-level ``_db_initialized`` flag
    # would otherwise short-circuit and leave the next test running
    # against a closed pool.
    if _db_initialized and thoth_db._pool is not None:
        return

    dsn = os.environ.get("HERMES_PG_DSN")
    if not dsn:
        raise RuntimeError(
            "HERMES_PG_DSN must be set; export from .env or configure in environment"
        )
    if thoth_db._pool is None:
        thoth_db.run_sync(thoth_db.init(dsn))

    def _close_pool_atexit():
        if thoth_db._pool is not None:
            try:
                thoth_db.run_sync(thoth_db.close())
            except Exception:
                # Interpreter teardown: the DB loop thread may already be
                # gone. Postgres reaps the idle connections regardless.
                pass

    atexit.register(_close_pool_atexit)
    _db_initialized = True


# ---------------------------------------------------------------------------
# Substrate bootstrap helper (Phase A Task 14)
# ---------------------------------------------------------------------------
#
# Called after init_db_sync / hermes_db.init has populated the asyncpg pool.
# Pure-async entry points (gateway/run.py) ``await bootstrap_substrate()``
# from inside asyncio.run(); sync entry points use the sync wrapper which
# bridges via hermes_db.run_sync.
#
# Bootstrap failure is non-fatal for writer-mode callers: a logged error,
# no substrate emission, Hermes runs as before (Phase A spec §0 — substrate
# failures must not crash Hermes). Worker-mode boot failure is critical (no
# decay / Sentinel / embeddings) and the worker subprocess exits non-zero
# so systemd restarts it.
#
# Either way, the outcome is recorded durably so an operator can SEE it
# without grepping logs: ``hermes substrate boot`` (and the default
# summary) read the last boot status per mode from the ``state_meta`` KV
# table. Before this, a writer-mode boot failure silently stopped all
# perception with no cross-process signal.

_substrate_booted = False
_substrate_handle: "object | None" = None

# In-process cache of the last boot status per mode, for programmatic
# health checks (``get_boot_status()``) that must not touch the DB.
_last_boot_status: "dict[str, dict]" = {}

# state_meta key prefix; one row per mode (writer / worker) so the two
# process roles don't clobber each other's status.
_BOOT_STATUS_KEY_PREFIX = "substrate.boot_status."


def get_boot_status(mode: "str | None" = None):
    """Return the last in-process boot status.

    With ``mode`` → that mode's status dict (or ``None`` if this process
    hasn't attempted that mode). Without ``mode`` → the whole
    ``{mode: status}`` map. Process-local: reflects only boots attempted
    in *this* process. The cross-process view is the ``state_meta`` rows
    surfaced by ``hermes substrate boot``.
    """
    if mode is not None:
        return _last_boot_status.get(mode)
    return dict(_last_boot_status)


async def _record_boot_status(mode: str, *, ok: bool, error_text=None, log=None) -> None:
    """Best-effort record of a boot outcome to the ``state_meta`` KV table
    (and the in-process cache). Never raises — a failure to record status
    must not turn a successful boot into a failure, nor mask the original
    error on the failure path. If the pool is down we simply can't record,
    but then the inspect CLI can't read it either, so nothing is lost.
    """
    import datetime
    import json
    import os
    import socket

    status = {
        "mode": mode,
        "ok": ok,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "error": error_text,
        "booted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    _last_boot_status[mode] = status

    try:
        import thoth_db

        async with thoth_db.connection() as conn:
            await conn.execute(
                """
                INSERT INTO state_meta (key, value) VALUES ($1, $2)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                _BOOT_STATUS_KEY_PREFIX + mode,
                json.dumps(status),
            )
    except Exception:
        if log is not None:
            log.debug(
                "substrate.boot_status.record_failed mode=%s", mode, exc_info=True
            )


async def bootstrap_substrate(log=None, *, mode: str = "writer"):
    """Boot the substrate. Idempotent.

    ``mode`` selects the process role:

    * ``"writer"`` (default) — perception emitters + recall reads. The
      gateway, chat CLI, ACP adapter, and cron runner all want this.
      Streams auto-register, hooks bind, recall log writer starts;
      sub-agent tick loops do NOT start.
    * ``"worker"`` — sub-agent tick loops only. Use exclusively from
      the dedicated ``hermes substrate worker run`` subprocess.

    Returns the Substrate instance (or ``None`` if boot failed). Failures
    are logged as warnings and the function returns ``None`` so the
    caller can proceed without substrate (writer-mode callers degrade
    to no-emit; worker-mode callers should exit with non-zero).

    Tests that need a deterministic substrate construct it directly via
    ``Substrate.from_pool``; this helper is for production startup.
    """
    global _substrate_booted, _substrate_handle
    if _substrate_booted:
        return _substrate_handle
    import logging

    log = log or logging.getLogger("substrate.bootstrap")
    try:
        from substrate import Substrate

        if mode == "writer":
            substrate = await Substrate.boot_writer(log=log)
        elif mode == "worker":
            substrate = await Substrate.boot_worker(log=log)
        else:
            raise ValueError(
                f"bootstrap_substrate: unknown mode {mode!r}; "
                "expected 'writer' or 'worker'"
            )
        _substrate_handle = substrate
        _substrate_booted = True
        await _record_boot_status(mode, ok=True, log=log)
        return substrate
    except Exception as exc:
        # Loud, mode-appropriate logging — a boot failure is never benign.
        if mode == "worker":
            log.error(
                "substrate.bootstrap.failed mode=worker — the sub-agent "
                "worker cannot start; decay, Sentinel decisions, and "
                "embedding backfill will NOT run until this is fixed",
                exc_info=True,
            )
        else:
            log.error(
                "substrate.bootstrap.failed mode=%s — Hermes keeps running "
                "but emits NO perception (no recall, no L0 growth) until "
                "this is fixed and the process restarted",
                mode,
                exc_info=True,
            )
        await _record_boot_status(
            mode, ok=False, error_text=f"{type(exc).__name__}: {exc}", log=log
        )
        return None


def bootstrap_substrate_sync(log=None, *, mode: str = "writer"):
    """Sync facade for ``bootstrap_substrate``.

    Bridges via :func:`hermes_db.run_sync`. Must NOT be called from inside
    a running event loop — async entry points should ``await
    bootstrap_substrate(log, mode=...)`` directly.
    """
    import thoth_db

    return thoth_db.run_sync(bootstrap_substrate(log=log, mode=mode))
