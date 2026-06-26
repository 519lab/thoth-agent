# Database access and the single DB loop

This is the rule that prevents the bug class that produced **#117, #120,
#123, #124, #125, and #126**. Read it before touching anything that opens a
Postgres connection, calls `thoth_db.run_sync`, or runs DB code from the
gateway.

## TL;DR — the invariant

> **The asyncpg pool lives on exactly one event loop ("the DB loop"), and
> every DB operation must run on that loop. Never `await` a pooled connection
> from any other loop.**

`thoth_db` owns a single, continuously-running event loop on a dedicated
daemon thread (`thoth-db-loop`). The pool is bound to it. All access routes
there:

| You are…                                   | Use                                  |
| ------------------------------------------ | ------------------------------------ |
| Sync code (CLI, hooks, cron, kanban, etc.) | `thoth_db.run_sync(coro)`           |
| Async code on a **different** loop (the gateway's I/O loop `L_gw`) | `await thoth_db.run_on_pool_loop(coro)` |
| Async code already **on** the DB loop      | `await coro` directly                |
| The substrate **worker** subprocess        | its own `asyncio.run` loop + `reset_pool_for_new_loop()` (see below) |

If you follow the table, cross-loop errors are structurally impossible.

## Why this exists — the failure mode

asyncpg connections (and pools) are **bound to the event loop that created
them**. Using one from a different loop raises, intermittently and
confusingly:

```
asyncpg.exceptions.ConnectionDoesNotExistError: connection was closed in the middle of operation
asyncpg.exceptions.InterfaceError: cannot perform operation: another operation is in progress
RuntimeError: ... got Future <...> attached to a different loop
```

…usually surfacing as an orphaned `Future exception was never retrieved`
because the awaiting coroutine was abandoned cross-loop.

The gateway is the hard case: it runs **two** loops in one process.

- **`L_gw`** — the gateway's main I/O loop, started by
  `asyncio.run(start_gateway())`. Telegram/Slack polling, the handoff
  watcher, slash-command handlers, and the substrate-writer bootstrap run
  here.
- **The DB loop** — `thoth_db`'s loop, which owns the asyncpg pool.

`main()` calls `ensure_pool_sync()` for every subcommand, which creates the
pool on the DB loop *before* `L_gw` exists. The gateway's hot path (session
persistence, perception hooks, agent turns) is **sync** and bridges via
`run_sync` → DB loop → correct. But any **async-native** caller on `L_gw`
that `await`s the pool directly is cross-loop → boom.

That single structural fact produced every incident in the list above. The
earlier PRs each patched *one* crossing; **#126** fixed the structure: make
the DB loop run continuously and route everything to it.

## How it works (`thoth_db.py`)

- `_get_sync_loop()` lazily creates the DB loop and starts the
  `thoth-db-loop` daemon thread running `loop.run_forever()`. Because the
  loop runs continuously, **long-lived DB tasks survive** (e.g. the substrate
  writer's `RecallLogWriter._drain_loop`) instead of being orphaned when a
  one-shot `run_until_complete` returns.
- `run_sync(coro)` → `asyncio.run_coroutine_threadsafe(coro, db_loop).result()`.
  Works from any thread and from inside any *other* running loop. Calling it
  from **inside the DB loop thread** is a bug (it would deadlock) and raises.
- `run_on_pool_loop(coro)` → awaits directly if already on the pool's loop;
  otherwise schedules on the DB loop and awaits via `asyncio.wrap_future` so
  the caller's loop is never blocked. It **raises** if the pool is bound to
  an unexpected loop — a loud signal that something created the pool on the
  wrong loop.
- Both coerce any **awaitable** into a coroutine, because
  `run_coroutine_threadsafe` (unlike `run_until_complete`) rejects
  non-coroutine awaitables such as `pool().acquire()`.

The substrate **worker** subprocess is the one process that does *not* use
the DB loop: it is fully async (`asyncio.run`), calls
`reset_pool_for_new_loop()` to drop any inherited pool, and `await
thoth_db.init(dsn)` on its own loop. It never calls `run_sync`, so the
DB-loop thread never starts there. That's correct — a single-loop process
owns its pool directly.

## Rules of the road

**Do**

- Bridge sync→async DB with `thoth_db.run_sync(coro)`.
- From `L_gw` (or any loop that isn't the DB loop), route async DB work with
  `await thoth_db.run_on_pool_loop(coro)`.
- In the gateway, reach `SessionDB` through `self._session_db` — it is wrapped
  in `_PoolLoopRoutedSessionDB` (`gateway/run.py`), which routes every async
  method onto the DB loop for you. **Do not** unwrap it.

**Don't**

- ❌ `await thoth_db.connection()` / `await pool().acquire()` /
  `await some_session_db.method()` directly from `L_gw` or any non-DB loop.
- ❌ Call an `async def` SessionDB/DB method from **sync** code without
  `run_sync` (it returns an un-awaited coroutine — silently a no-op; this was
  #125).
- ❌ `asyncio.run(thoth_db.init(dsn))` or any per-call `new_event_loop()` for
  DB work — it binds the pool to a loop that's about to close.
- ❌ Bind the pool to `L_gw` to "fix" an async caller — that just moves the
  cross-loop break onto the sync hot path.

## Guardrails already in place

- `run_on_pool_loop` raises if the pool is on an unexpected loop.
- `run_sync` raises if called from inside the DB loop thread.
- Regression tests in `tests/test_thoth_db.py`: continuous loop on its own
  thread, `run_sync` from inside another running loop, awaitable acceptance,
  long-lived-task survival after a routed boot, and the reentrant guard.

## If you see a `ConnectionDoesNotExistError` again

It means new code reached the pool from the wrong loop. Find the caller (run
the gateway once with `PYTHONASYNCIODEBUG=1` — asyncio then attaches the
creation traceback to "Future exception was never retrieved" lines) and route
it per the table above. Do **not** silence the log line: an error here is
real mis-wiring, not noise.
