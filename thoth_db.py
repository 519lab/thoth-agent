"""Process-wide asyncpg pool + sync bridge helpers for Hermes's PG storage.

Owns the single `asyncpg.Pool` instance. All DB-accessing code in Hermes
goes through `connection()` or `transaction()`. Sync callers use
`run_sync(coro)` until they can be refactored to async.

Initialize once per process at entry-point startup (`init(dsn)`).
Close at shutdown (`close()`). Pool size is tunable via env vars:
    HERMES_PG_POOL_MIN (default 4)
    HERMES_PG_POOL_MAX (default 64)
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Optional, TypeVar

import asyncpg

T = TypeVar("T")

_pool: Optional[asyncpg.Pool] = None
_pool_lock = threading.Lock()

# The single, continuously-running event loop that owns all DB access for
# this process ("the DB loop"). asyncpg pools bind to whichever loop creates
# them, so Hermes keeps ONE loop, bound to the pool, running forever on its
# own daemon thread. Every sync caller bridges to it via ``run_sync``
# (``run_coroutine_threadsafe``); async callers on a *different* loop (e.g.
# the gateway's main I/O loop) route to it via ``run_on_pool_loop``. Because
# the loop runs continuously, long-lived DB tasks scheduled on it (the
# substrate writer's recall-log drain) keep running instead of being orphaned
# when a one-shot ``run_until_complete`` returns — the root cause of the
# 2026-05-26/29 cross-loop pool incidents.
#
# The loop + thread start lazily on first use, so pure-async entry points
# that own their own loop (the substrate worker subprocess, which binds the
# pool to its ``asyncio.run`` loop) never start this thread.
_sync_loop: Optional[asyncio.AbstractEventLoop] = None
_db_thread: Optional[threading.Thread] = None
_sync_loop_lock = threading.Lock()


def _get_sync_loop() -> asyncio.AbstractEventLoop:
    """Return the always-running DB loop, starting its thread on first call."""
    global _sync_loop, _db_thread
    with _sync_loop_lock:
        if _sync_loop is None or _sync_loop.is_closed():
            _sync_loop = asyncio.new_event_loop()
            _db_thread = None  # a fresh loop needs a fresh thread to drive it
        if _db_thread is None or not _db_thread.is_alive():
            loop = _sync_loop

            def _run_db_loop() -> None:
                asyncio.set_event_loop(loop)
                loop.run_forever()

            _db_thread = threading.Thread(
                target=_run_db_loop, name="hermes-db-loop", daemon=True
            )
            _db_thread.start()
        return _sync_loop


def _apply_tcp_keepalive(conn) -> None:
    """Enable TCP keepalives on a freshly-created asyncpg pool connection.

    asyncpg disables SO_KEEPALIVE by default.  Without keepalives, when the
    remote end (postgres, docker bridge, NAT) closes a connection while it is
    idle in the pool, the OS never sends a RST to the client — the socket
    stays in CLOSE_WAIT with the FIN buffered but unread.  asyncpg's
    ``is_closed()`` only checks its internal protocol state (not the OS
    socket), so it returns False and the pool hands the dead connection out.
    The next caller gets ``ConnectionDoesNotExistError: connection was closed
    in the middle of operation`` and an orphaned "Future exception was never
    retrieved" log line.

    With keepalives enabled, the OS probes the peer after
    ``TCP_KEEPIDLE`` seconds of silence.  If ``TCP_KEEPCNT`` consecutive
    probes (``TCP_KEEPINTVL`` seconds apart) go unanswered, the OS closes the
    socket, asyncpg's transport fires ``connection_lost``, and the pool evicts
    the connection — all before any caller ever acquires it.

    Called from the pool's ``init`` hook so every connection (including
    replacements after recycling) gets keepalives.  Values are intentionally
    aggressive for a local/LAN postgres: 10s idle, 5s interval, 3 probes =
    dead connection detected in ≤25 seconds.  All tunable via env vars.
    """
    import socket as _socket

    try:
        raw = conn._transport.get_extra_info("socket")
        if raw is None:
            return
        raw.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
        keepidle = int(os.environ.get("HERMES_PG_KEEPIDLE_S", "10"))
        keepintvl = int(os.environ.get("HERMES_PG_KEEPINTVL_S", "5"))
        keepcnt = int(os.environ.get("HERMES_PG_KEEPCNT", "3"))
        # TCP_KEEPIDLE / TCP_KEEPINTVL / TCP_KEEPCNT are Linux-specific;
        # skip gracefully on macOS/Windows where they may not exist.
        if hasattr(_socket, "TCP_KEEPIDLE"):
            raw.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPIDLE, keepidle)
        if hasattr(_socket, "TCP_KEEPINTVL"):
            raw.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPINTVL, keepintvl)
        if hasattr(_socket, "TCP_KEEPCNT"):
            raw.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPCNT, keepcnt)
    except Exception:
        # Never crash pool creation over a keepalive failure.
        pass


async def _setup_jsonb_codec(conn):
    """Register JSONB codec so asyncpg returns Python objects for jsonb columns.

    Also registers a text-format codec for the pgvector ``vector`` type so
    asyncpg round-trips ``vector(N)`` columns as ``list[float]`` (Phase C —
    used by ``substrate_slices.embedding``). The vector extension may not
    be enabled yet on every database (pre-Phase-C deployments); we probe
    for it and skip registration silently if absent. This keeps the pool
    init path safe to run against older Alembic heads.

    Also enables TCP keepalives on the connection's socket so dead connections
    are detected by the OS and evicted from the pool before any caller
    acquires them (see ``_apply_tcp_keepalive`` for rationale).
    """
    _apply_tcp_keepalive(conn)
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    # Vector codec: optional. Skip when the extension isn't installed
    # (older deployments). Encoder formats as "[x,y,z,...]" — pgvector's
    # text input format. Decoder strips the brackets and splits on
    # commas. ~10 lines; no Python `pgvector` package required.
    try:
        await conn.set_type_codec(
            "vector",
            encoder=_encode_vector,
            decoder=_decode_vector,
            schema="public",
            format="text",
        )
    except (asyncpg.exceptions.UndefinedObjectError, ValueError):
        # Vector type isn't registered on this DB; skip silently. The
        # substrate recall pipeline will fail loudly when it tries to
        # write embeddings, which is the right place for the error.
        # asyncpg raises ValueError("unknown type: public.vector") when
        # introspection finds no matching entry in pg_type — older
        # deployments that haven't run the Phase C migration yet.
        pass


def _encode_vector(vec) -> str:
    """Encode a Python sequence of floats as pgvector's text input
    format, e.g. ``[0.1,0.2,0.3]``. Uses ``repr(float(x))`` so floats
    round-trip without rounding (Python's repr is exact for floats)."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _decode_vector(s: str) -> list:
    """Decode pgvector's text output ``[x,y,z,...]`` into a list of
    Python floats. asyncpg passes the raw string; we strip brackets and
    split."""
    inner = s.strip().strip("[]")
    if not inner:
        return []
    return [float(x) for x in inner.split(",")]


def _ssl_kwarg_for_dsn(dsn: str) -> dict:
    """Decide the ``ssl=`` kwarg for ``asyncpg.create_pool(...)``.

    Two distinct goals:

    1. **Production correctness.** Remote Postgres (Neon, Supabase, RDS,
       any DSN that doesn't resolve to a loopback / docker-compose host)
       should use asyncpg's default SSL negotiation. Operators who set
       ``sslmode=`` explicitly in the DSN have their preference honoured
       by asyncpg's own parsing — we don't override.

    2. **CI / local test stability.** When the DSN points at a known
       local plain-text Postgres (the docker-compose ``postgres`` service
       at ``localhost`` / ``127.0.0.1`` / ``postgres``) and the user did
       NOT specify ``sslmode=`` themselves, force ``ssl=False`` to skip
       asyncpg's SSL upgrade negotiation. That negotiation runs
       ``_create_ssl_connection`` even for ``sslmode=prefer``-then-
       downgrade, and the SSL context setup intermittently segfaults
       inside CPython's ssl module on GitHub Actions runners — taking
       the whole pytest worker down with no Python traceback. The local
       compose PG has no SSL anyway; this is the negotiation we want to
       skip.

    Returns ``{"ssl": False}`` when we want the skip, ``{}`` otherwise
    so we don't override asyncpg's default-or-DSN-derived behaviour.
    """
    dsn_lower = dsn.lower()
    # User-pinned sslmode in the DSN → respect it, no override.
    if "sslmode=" in dsn_lower:
        return {}
    # Extract the host segment between "@" and the trailing ":port/db".
    # urllib.parse handles all the edge cases (auth-with-@, ipv6, empty
    # password, etc.) so we don't have to reinvent them.
    try:
        from urllib.parse import urlparse
        host = (urlparse(dsn_lower).hostname or "").strip()
    except Exception:
        return {}
    if not host:
        return {}
    # Loopback aliases — always local.
    if host in ("localhost", "127.0.0.1", "::1"):
        return {"ssl": False}
    # Docker-compose service hostnames are single-label (no dots):
    # ``postgres``, ``postgres-test``, ``db``, etc. Public DNS names
    # always have at least one dot. This heuristic catches our two
    # compose services (production and test) plus any operator-defined
    # ones without overreaching to real hosts.
    if "." not in host:
        return {"ssl": False}
    return {}


async def init(
    dsn: str,
    *,
    min_size: Optional[int] = None,
    max_size: Optional[int] = None,
    command_timeout: int = 30,
) -> None:
    """Create the singleton pool. Idempotent: a second call is a no-op."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            return
        ms = min_size if min_size is not None else int(os.environ.get("HERMES_PG_POOL_MIN", "4"))
        Ms = max_size if max_size is not None else int(os.environ.get("HERMES_PG_POOL_MAX", "64"))
    # Recycle connections that have sat idle in the pool longer than this.
    # Primary dead-connection detection is via TCP keepalives (see
    # ``_apply_tcp_keepalive``) — the OS detects a half-open socket within
    # ~25 seconds and asyncpg evicts it automatically. This max_inactive
    # lifetime is a belt-and-suspenders fallback for connections that go
    # stale in other ways (e.g. postgres idle-session-timeout, server
    # restart). Env-tunable; 0 disables (asyncpg semantics).
    try:
        max_inactive = float(os.environ.get("HERMES_PG_POOL_MAX_INACTIVE_S", "120"))
    except ValueError:
        max_inactive = 120.0
    pool = await asyncpg.create_pool(
        dsn,
        min_size=ms,
        max_size=Ms,
        command_timeout=command_timeout,
        max_inactive_connection_lifetime=max_inactive,
        init=_setup_jsonb_codec,
        **_ssl_kwarg_for_dsn(dsn),
    )
    with _pool_lock:
        if _pool is None:
            _pool = pool
        else:
            await pool.close()  # lost the race; throw away the dup


async def close() -> None:
    global _pool
    with _pool_lock:
        p = _pool
        _pool = None
    if p is not None:
        await p.close()


def reset_pool_for_new_loop() -> None:
    """Discard the current pool synchronously, without awaiting ``close()``.

    asyncpg pools are bound to the event loop that created them. A process
    that runs its own loop via ``asyncio.run`` (e.g. ``hermes substrate
    worker run``) must not inherit the pool that a sync entry point —
    ``main()``'s ``ensure_pool_sync()`` — bound to ``_sync_loop``: every DB
    call on the new loop would raise ``got Future ... attached to a
    different loop``. ``Pool.terminate()`` is synchronous and loop-agnostic
    (it aborts the transports), so it is safe to call from the new loop's
    thread even though the stale pool belongs to ``_sync_loop``. The caller
    re-creates the pool on its own loop via ``init()``.
    """
    global _pool
    with _pool_lock:
        p, _pool = _pool, None
    if p is not None:
        p.terminate()


def pool() -> asyncpg.Pool:
    if _pool is not None:
        return _pool

    # Lazy bootstrap: if a DSN is in the environment but init() was never
    # called, initialise the pool on first use. This lets CLI subcommands
    # that don't touch the DB (e.g. ``hermes --help``, ``hermes version``)
    # run in environments without a live PG instance, while DB-touching
    # subcommands still get a working pool without an explicit init step
    # at the entry point.
    dsn = os.environ.get("HERMES_PG_DSN")
    if not dsn:
        raise RuntimeError("hermes_db.init() not called")

    # Lazy bootstrap is ONLY safe from a pure-sync context (no running event
    # loop on this thread). asyncpg pools are loop-bound: binding a fresh
    # pool to the persistent ``_sync_loop`` here while the caller is awaiting
    # on a *different* loop (an own-loop entry point like ``hermes substrate
    # worker run``, the gateway's main loop, a pytest-asyncio test) makes the
    # caller's next ``acquire()`` raise ``got Future ... attached to a
    # different loop`` — and ``_sync_loop.run_until_complete`` from inside a
    # running loop raises ``Cannot run the event loop while another loop is
    # running`` outright. Either way, a confusing crash. Convert it into a
    # clear, actionable error: async callers must ``await
    # hermes_db.init(dsn)`` on their own loop first (own-loop entry points
    # also call ``reset_pool_for_new_loop()`` to drop any inherited pool).
    try:
        asyncio.get_running_loop()
        inside_running_loop = True
    except RuntimeError:
        inside_running_loop = False
    if inside_running_loop:
        raise RuntimeError(
            "hermes_db.pool() accessed before init() from inside a running "
            "event loop. asyncpg pools are loop-bound — call `await "
            "hermes_db.init(dsn)` on this loop first. Own-loop entry points "
            "should also call hermes_db.reset_pool_for_new_loop() to drop "
            "any pool inherited from the sync bridge."
        )

    # Pure-sync context: drive the init on the always-running DB loop.
    run_sync(init(dsn))
    if _pool is None:
        raise RuntimeError("hermes_db.init() not called")
    return _pool


@asynccontextmanager
async def connection() -> AsyncIterator[asyncpg.Connection]:
    async with pool().acquire() as conn:
        yield conn


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    async with pool().acquire() as conn:
        async with conn.transaction():
            yield conn


def _as_coroutine(awaitable: Awaitable[T]):
    """Coerce any awaitable into a coroutine.

    ``asyncio.run_coroutine_threadsafe`` (unlike ``loop.run_until_complete``,
    which ``ensure_future``-wraps awaitables) rejects non-coroutine awaitables
    such as asyncpg's ``PoolAcquireContext`` (``run_sync(pool().acquire())``).
    Wrapping preserves the historical ``run_sync``/``run_on_pool_loop``
    contract of accepting any awaitable.
    """
    if asyncio.iscoroutine(awaitable):
        return awaitable

    async def _await_it():
        return await awaitable

    return _await_it()


def run_sync(coro: Awaitable[T]) -> T:
    """Bridge a sync caller to an async DB call.

    Submits ``coro`` to the always-running DB loop (``_get_sync_loop``) via
    ``asyncio.run_coroutine_threadsafe`` and blocks the calling thread until
    it completes. Works uniformly from any thread and from inside any *other*
    running loop (the gateway's main loop, pytest-asyncio test bodies, ACP
    server callbacks): the coroutine always runs on the DB loop, where the
    asyncpg pool is bound, so there is never a cross-loop operation.

    Calling this from *inside* the DB loop thread is a bug — the caller is
    already on the pool's loop and must ``await`` the coroutine directly — so
    we close the coroutine and raise rather than deadlock waiting on a loop
    that is busy waiting on us.

    NOTE: this function does NOT lazy-bootstrap the pool — auto-init lives in
    ``pool()`` for sync code paths that touch the DB outside an event loop,
    and ``ensure_pool_sync()`` for code that knows it's about to acquire
    connections. Mocked tests need nothing.
    """
    loop = _get_sync_loop()
    if threading.current_thread() is _db_thread:
        # Re-entrant: a coroutine already running on the DB loop called a
        # sync DB helper. Blocking on the DB loop from the DB loop deadlocks.
        if asyncio.iscoroutine(coro):
            coro.close()
        raise RuntimeError(
            "hermes_db.run_sync called from inside the DB loop thread; "
            "await the coroutine directly instead."
        )
    return asyncio.run_coroutine_threadsafe(_as_coroutine(coro), loop).result()


async def run_on_pool_loop(coro: Awaitable[T]) -> T:
    """Await a DB coroutine on the asyncpg pool's event loop.

    asyncpg connections are loop-bound: a coroutine that does
    ``async with hermes_db.connection() as conn: await conn.fetch(...)`` can
    only run on the loop the pool was created on. In a process that hosts DB
    access from more than one loop — e.g. the gateway runs its main loop
    ``L_gw`` via ``asyncio.run(start_gateway())`` while the pool lives on the
    always-running DB loop (``_get_sync_loop``) — an async-native caller on
    ``L_gw`` (the handoff watcher, ``/title`` / ``/resume`` / ``/branch``
    handlers, telegram-topic ops, the substrate-writer bootstrap) must not
    ``await`` the pool directly or it hits ``ConnectionDoesNotExistError`` /
    "another operation is in progress".

    This helper sends such a coroutine to the pool's loop:

    * Already on the pool's loop (a single-loop process like the substrate
      worker, or code already running on the DB loop): await it directly —
      no thread hop, no overhead.
    * On a different loop: schedule it on the DB loop and await the result
      via :func:`asyncio.wrap_future`, so the caller's loop is never blocked.
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    pool_loop = getattr(_pool, "_loop", None)
    # Single-loop topology, or already on the pool's loop: no hop needed.
    if running is not None and pool_loop is not None and running is pool_loop:
        return await coro
    loop = _get_sync_loop()
    if pool_loop is not None and pool_loop is not loop:
        # Pool is bound to a loop that is neither the caller's nor the DB
        # loop — routing would still issue a cross-loop operation.
        if asyncio.iscoroutine(coro):
            coro.close()
        raise RuntimeError(
            "run_on_pool_loop: the asyncpg pool is bound to an unexpected "
            "loop (neither the running loop nor the DB loop); cannot route "
            "the coroutine safely."
        )
    fut = asyncio.run_coroutine_threadsafe(_as_coroutine(coro), loop)
    if running is None:
        return fut.result()
    return await asyncio.wrap_future(fut)


def ensure_pool_sync() -> bool:
    """Initialise the pool from ``HERMES_PG_DSN`` if it hasn't been already.

    Returns True if a pool is available after the call, False if no DSN is
    configured and no pool exists. Safe to call from any sync context;
    must NOT be called from inside a running event loop.

    Intended for sync entry points that are about to call ``run_sync`` on
    a DB-touching coroutine. Calling this once at the top of e.g. an
    ACP ``_get_db`` ensures the inner ``async with connection()`` doesn't
    hit the "init not called" error.
    """
    if _pool is not None:
        return True
    dsn = os.environ.get("HERMES_PG_DSN")
    if not dsn:
        return False
    # Reject calls from inside ANY running loop on this thread (not just
    # our own persistent ``_sync_loop``). pytest-asyncio creates a
    # per-test loop that's distinct from our persistent one but still
    # owns the thread; ``run_until_complete`` would raise "Cannot run
    # the event loop while another loop is running" if we tried to
    # bootstrap synchronously from inside such a test.
    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False
    if running:
        raise RuntimeError(
            "hermes_db.ensure_pool_sync called from inside a running event "
            "loop; await hermes_db.init(dsn) directly instead."
        )
    # Drive the init on the always-running DB loop; run_sync owns the
    # coroutine's lifecycle (and closes it on the re-entrant guard path).
    run_sync(init(dsn))
    return _pool is not None
