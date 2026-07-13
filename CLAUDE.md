# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What Thoth is

A self-improving AI agent: it records everything to a PostgreSQL-backed **cognitive substrate**, distills lasting memory, creates/improves skills from experience, and runs across a CLI/TUI, a messaging gateway (Telegram/Discord/Slack/etc.), and pluggable sandbox backends. Provider-agnostic over any OpenAI-compatible API. The command, config dir (`~/.thoth`), and database are all named `thoth`. Python 3.11, `uv`-managed.

## Commands

```bash
uv venv .venv --python 3.11 && source .venv/bin/activate
uv pip install -e ".[all,dev]"          # all extras + dev tools

thoth doctor                            # diagnostics
thoth chat -q "Hello"                   # one-shot; `thoth` alone starts the TUI
```

Console entry points (`pyproject.toml [project.scripts]`): `thoth` → `thoth_cli.main:main`, `thoth-agent` → `run_agent:_cli_main`, `thoth-acp` → `acp_adapter.entry:main`. The `hermes*` aliases were removed in the de-Hermes rename (see below).

**Tests — always use the wrapper, never bare `pytest`:**

```bash
scripts/run_tests.sh                                  # full suite, CI-parity
scripts/run_tests.sh tests/agent/                     # one directory
scripts/run_tests.sh tests/agent/test_foo.py          # single file
scripts/run_tests.sh tests/agent/test_foo.py -- -k test_x  # single test (pytest args after --)
```

The wrapper enforces hermetic CI parity: blanks all `*_API_KEY`/`*_TOKEN` vars, `TZ=UTC`, `LANG=C.UTF-8`, `PYTHONHASHSEED=0`, a temp `~/.thoth`, and per-file subprocess isolation (each test file gets a fresh interpreter, so module-level dicts/sets/ContextVars can't leak — there is no autouse state-reset fixture). `tests/conftest.py` re-enforces the hermetic env for any stray direct invocation. **In this WSL environment bare `pytest` on PATH is broken** — if you can't use the wrapper, run `uv run python -m pytest ...` from the activated venv.

**Lint / typecheck / DB:**

```bash
ruff check .                            # only PLW1514 (unspecified-encoding) is enabled — see gotchas
ty check                                # ADVISORY ONLY — astral `ty` 0.0.21 panics on tools/checkpoint_manager.py and emits ~7000 diagnostics; not a passing gate, don't treat failures as blocking
docker compose up -d postgres          # local PG 17 (vector + pg_trgm), port 5432, db `thoth`
uv run alembic -c migrations/alembic.ini upgrade head
```

## Architecture

**Core loop** (`run_agent.py`, `AIAgent`, ~12k LOC). `run_conversation()` (around line 4060) is a **synchronous** while-loop: build system prompt → call the OpenAI-compatible API → if the response has `tool_calls`, dispatch each via `handle_function_call()` (`model_tools.py`) and append results, then loop; else persist the session and return. Bounded by `max_iterations` and an `iteration_budget`, with interrupt checks and a one-turn budget grace call. Messages are OpenAI-format; reasoning lives in `assistant_msg["reasoning"]`. `AIAgent.__init__` takes ~60 params — read the file rather than guessing the signature.

**Tools** (`tools/`). **Self-registering**: each tool module calls `registry.register(...)` at import time (`tools/registry.py` is the dependency-free core; see its module docstring). `model_tools.py` triggers discovery by importing the tool modules, then owns dispatch. Tools are grouped into **toolsets** (`toolsets.py`) that platforms enable/disable. Terminal execution has pluggable backends under `tools/environments/` (local, docker, ssh, singularity, modal, daytona).

**Cognitive substrate** (`substrate/`) — the flagship subsystem and the current active workstream. A PG-backed perception sink + memory layer running alongside the loop. Every message/action/event becomes a *slice* on a named *stream*, stored in RANGE-partitioned `substrate_slices`, decayed/curated over time (Curator), and recalled by a composite score (similarity + keyword Jaccard + salience + recency) under a token budget. Layers `l0`–`l4`; pgvector 1536-d embeddings. Recall is gated by `THOTH_SUBSTRATE_RECALL` (`substrate/config.py` — **now defaults on**; set `=0` to fall back to the upstream built-in memory). **Substrate failures must be non-fatal** — all its I/O is wrapped so nothing propagates to the conversation loop; recall returns an empty projection rather than raising. Substrate *design* rationale lives in the external `llm-cognitive-thought` spec repo — don't add new substrate design docs here.

**Gateway** (`gateway/`) — one process serving many messaging platforms (`gateway/platforms/`), with session routing, cron dispatch, and background-process watchers. **Kanban** (`thoth_cli/kanban.py` + `tools/kanban_tools.py` + `plugins/kanban/`) — a durable multi-worker task board backed by the same Postgres pool (a stale docstring still calls it "SQLite-backed"; it is not); the dispatcher runs inside the gateway by default and spawns worker profiles. Board is a hard isolation boundary; tenant is a soft namespace within it.

**Other surfaces:** `cli.py` (Rich + prompt_toolkit TUI, data-driven skin/theme engine), `acp_adapter/` (VS Code/Zed/JetBrains), `plugins/` (memory providers, model providers, context engines, observability, image-gen, kanban).

## Non-obvious rules

- **Core persistence is PostgreSQL-only.** Sessions (`thoth_state.SessionDB`), kanban, and substrate all live in PG via `thoth_db`'s asyncpg pool — never add SQLite to those. (Kanban's `sqlite3.Connection` type hints are the `_PgConnection` compat shim over PG, not real SQLite.) SQLite *does* still exist in three non-core, opt-in places — the gateway Responses-API `ResponseStore` (`gateway/platforms/api_server.py`) and the `holographic`/`retaindb` memory plugins — so don't read this rule as "no SQLite anywhere in the tree."
- **The asyncpg pool is bound to ONE event loop.** `thoth_db` runs a single "DB loop" on a daemon thread. Sync code bridges via `thoth_db.run_sync(coro)` (`thoth_db.py:396`); async code on another loop (e.g. the gateway's I/O loop) via `await thoth_db.run_on_pool_loop(coro)` (`thoth_db.py:429`). Awaiting a pooled connection from the wrong loop is real mis-wiring, never log noise — see `docs/architecture/database-event-loop.md`.
- **Never touch the live DB from tests or dev runs.** LIVE Postgres is port **5432** (container `thoth-postgres-1`); the TEST instance is port **5433** (`thoth-postgres-test-1`). The suite runs against a snapshot-seeded TEST DB only, never live. Before *any* manual DB command, verify the target host/port/db name explicitly — and note that the `thoth` CLI loads `~/.thoth/.env`, whose DSN **overrides inline env vars and points at LIVE**, so an inline `THOTH_PG_DSN=...` will not protect you. Give destructive/live DB commands to Greg to run; don't run them yourself.
- **Never break prompt caching.** Do not alter past context, change toolsets, or reload memories/rebuild the system prompt mid-conversation (the sole exception is context compression). Slash commands that mutate system-prompt state must be cache-aware: default to deferred invalidation, opt-in `--now` for immediate. Corollary: **tool-call message IDs must be deterministic across replay** — a fresh/random id changes the message prefix and silently invalidates the cache. See `_deterministic_call_id()` in `agent/transports/codex_event_projector.py` for the pattern (reuse the upstream item id; fall back to a content hash, never a random uuid).
- **Gateway/adapter instance-attribute reads should use `getattr(self, "_attr", default)` fallbacks.** Many gateway tests build adapters/runners via `object.__new__(Cls)`, skipping `__init__`, so a bare `self._attr` raises `AttributeError` under test. See `_is_user_authorized()` in `gateway/platforms/discord.py` and `_make_bare_runner()` in `tests/gateway/test_discord_bot_auth_bypass.py`.
- **Profile-safe paths.** Never hardcode `~/.thoth` or `Path.home() / ".thoth"`. Use `get_thoth_home()` for state I/O and `display_thoth_home()` for user-facing messages, both from `thoth_constants` — Thoth supports fully isolated profiles via the `THOTH_HOME` env var. Tests that mock `Path.home()` must also set `THOTH_HOME`.
- **de-Hermes is (nearly) complete — new code uses `thoth`/`THOTH_` only.** No `hermes`/`HERMES_` handles, no back-compat shims or aliases. The only legitimate remaining `hermes` references are genuinely external: the `hermes-parser`/estree toolchain and Nous "Hermes" model names — carve those out, don't bulk-rename them.
- **No change-detector tests.** Don't assert on model-catalog contents, config version literals, or enumeration counts — they break on every routine data update. Test behavior and invariants instead.
- **`ruff` runs only `PLW1514`** (require explicit `encoding=` on text I/O). Bare `open()`/`read_text()`/`write_text()` defaults to the locale encoding and silently corrupts non-ASCII on Windows. Cross-platform correctness is load-bearing across this codebase.
- **Dependencies are exact-pinned** (`==X.Y.Z`, no ranges) as supply-chain hardening; only every-session packages go in `dependencies`, provider-specific ones live in extras and lazy-install via `tools/lazy_deps.py`. When bumping, change the pin AND regenerate `uv.lock`.

## Workflow

- `main` is branch-protected: branch first, open a PR, and wait for Greg's approval — never self-merge.
- Open a tracking issue before starting a fix and close it from the PR via `Closes #N`.
- Pushing/PRs require the `ggrace519` gh account (`gh auth switch` if the active one is wrong); GPG-signed commits need Greg to prime the gpg agent first.
