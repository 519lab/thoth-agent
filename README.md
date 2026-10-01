<p align="center">
  <img src="assets/thoth-banner.png" alt="Thoth" width="100%">
</p>

<h1 align="center">Thoth</h1>

<p align="center"><i>The self-improving AI agent with a memory that actually remembers.</i></p>

<p align="center">
  <a href="https://github.com/519lab/thoth-agent/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/PostgreSQL-17-336791?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL 17">
  <img src="https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11">
</p>

**Thoth is an AI agent that learns.** Named for the scribe of the gods — keeper of knowledge, records, and judgment — it does what the name implies: it records what happens, distills it into lasting memory, and gets better with use. It creates skills from experience, improves them as it works, searches its own past conversations, and builds a deepening model of who you are across sessions — all on a PostgreSQL-backed cognitive substrate that is the source of truth for everything it knows.

Run it on a $5 VPS, a GPU cluster, or serverless infrastructure that costs nearly nothing when idle. It isn't tied to your laptop — talk to it from Telegram while it works on a cloud VM.

Use any model you want — [OpenRouter](https://openrouter.ai) (200+ models), [NovitaAI](https://novita.ai), [NVIDIA NIM](https://build.nvidia.com) (Nemotron), [Xiaomi MiMo](https://platform.xiaomimimo.com), [z.ai/GLM](https://z.ai), [Kimi/Moonshot](https://platform.moonshot.ai), [MiniMax](https://www.minimax.io), [Hugging Face](https://huggingface.co), [Nous Portal](https://portal.nousresearch.com), OpenAI, or your own endpoint. Switch with `thoth model` — no code changes, no lock-in.

<table>
<tr><td><b>A real terminal interface</b></td><td>Full TUI with multiline editing, slash-command autocomplete, conversation history, interrupt-and-redirect, and streaming tool output.</td></tr>
<tr><td><b>Lives where you do</b></td><td>Telegram, Discord, Slack, WhatsApp, Signal, and CLI — all from a single gateway process. Voice memo transcription, cross-platform conversation continuity.</td></tr>
<tr><td><b>A closed learning loop</b></td><td>Agent-curated memory with periodic nudges. Autonomous skill creation after complex tasks. Skills self-improve during use. Full-text session search with LLM summarization for cross-session recall. <a href="https://github.com/plastic-labs/honcho">Honcho</a> dialectic user modeling. Compatible with the <a href="https://agentskills.io">agentskills.io</a> open standard.</td></tr>
<tr><td><b>A cognitive substrate</b></td><td>Every message, action, and event is recorded as a slice in PostgreSQL, decayed and curated over time, and recalled by semantic + keyword + salience + recency scoring. Memory that consolidates instead of just accumulating.</td></tr>
<tr><td><b>Scheduled automations</b></td><td>Built-in cron scheduler with delivery to any platform. Daily reports, nightly backups, weekly audits — all in natural language, running unattended.</td></tr>
<tr><td><b>Delegates and parallelizes</b></td><td>Spawn isolated subagents for parallel workstreams. Write Python scripts that call tools via RPC, collapsing multi-step pipelines into zero-context-cost turns.</td></tr>
<tr><td><b>Runs anywhere, not just your laptop</b></td><td>Seven terminal backends — local, Docker, SSH, Singularity, Modal, Daytona, and Vercel Sandbox. Daytona and Modal offer serverless persistence — the agent's environment hibernates when idle and wakes on demand, costing nearly nothing between sessions.</td></tr>
<tr><td><b>Research-ready</b></td><td>Batch trajectory generation and trajectory compression for training the next generation of tool-calling models.</td></tr>
</table>

> **A note on the name:** the project is **Thoth**, and the command is `thoth` — every example in this README uses it. Installs keep their config under `~/.thoth`, and the database is named `thoth`.

---

## Quick Install

### Linux, macOS, WSL2, Termux

```bash
curl -fsSL https://raw.githubusercontent.com/519lab/thoth-agent/main/scripts/install.sh | bash
```

The installer brings up everything you need, including a `docker compose` PostgreSQL 17 service (port 5432, database `thoth`) and runs the schema migrations. Pass `--reset-db` if you want it to start from a clean database, or `--skip-postgres` to point at your own PostgreSQL.

### Windows (native, PowerShell) — Early Beta

> **Heads up:** Native Windows support is **early beta**. It installs and runs, but hasn't been road-tested as broadly as the Linux/macOS/WSL2 paths. Please [file issues](https://github.com/519lab/thoth-agent/issues) when you hit rough edges. For the most battle-tested Windows setup today, run the Linux/macOS one-liner above inside **WSL2**.

```powershell
iex (irm https://raw.githubusercontent.com/519lab/thoth-agent/main/scripts/install.ps1)
```

The installer handles everything: uv, Python 3.11, Node.js, ripgrep, ffmpeg, **and a portable Git Bash** (MinGit, unpacked to `%LOCALAPPDATA%\thoth\git` — no admin required, fully isolated from any system Git install) used to run shell commands. If you already have Git installed, it detects and uses that instead.

> **Android / Termux:** Termux installs a curated `.[termux]` extra because the full `.[all]` extra currently pulls Android-incompatible voice dependencies.
>
> **Windows paths:** Native Windows installs under `%LOCALAPPDATA%\thoth`; WSL2 installs under `~/.thoth` as on Linux. The only feature that currently needs WSL2 specifically is the browser-based dashboard chat pane (it uses a POSIX PTY — the classic CLI and gateway both run natively).

After installation:

```bash
source ~/.bashrc    # reload shell (or: source ~/.zshrc)
thoth               # start chatting!
```

---

## Database setup

Thoth uses **PostgreSQL 17** (with the `vector` and `pg_trgm` extensions) as the single source of truth for session transcripts, kanban state, and the substrate's perception slices — there is no SQLite anywhere. The installer brings up a `docker compose` PostgreSQL service automatically; the manual / production paths are below.

**For local development:**

```bash
docker compose up -d postgres
export THOTH_PG_DSN=postgresql://thoth:thoth@localhost:5432/thoth
uv run alembic -c migrations/alembic.ini upgrade head
```

**For production deploys:** point `THOTH_PG_DSN` at any PostgreSQL 17+ instance with the `vector` and `pg_trgm` extensions installed, and run `alembic upgrade head` as part of your deploy.

**Coming from an older SQLite-based install?** A one-shot importer is provided:

```bash
uv run thoth db migrate-from-sqlite --sqlite-path ~/.thoth/state.db   # add --dry-run to preview
```

---

## Cognitive substrate

The substrate is what makes Thoth more than a stateless chat loop. It's a PostgreSQL-backed **perception sink and recall source**: every user message, assistant response, tool call/result, sub-agent spawn/return, session-lifecycle event, and cron dispatch is emitted as a *slice* on a named *stream* (`thoth.world.user_message.cli`, `thoth.self_action.assistant_response`, and so on). Slices are stored in `substrate_slices` (RANGE-partitioned monthly on ingest time), curated continuously, and recalled on demand.

It runs alongside the agent via background workers and is designed to be safe: substrate failures are non-fatal, and the recall path is env-gated off by default. The schema migration is permanent, so back up your DB before the first run if you care about the data.

**Perception sink.** Three core tables (`substrate_streams`, `substrate_slices`, `substrate_decay_profiles`), auto-registered streams, and monthly partition carving. Background workers tick on startup: **Sentinel** (slice triage), **force-reject** (drops pending slices past their decay-profile TTL), and **partition-maintenance** (keeps a rolling window of monthly partitions ahead of `now()`).

**Curator.** A continuous decay + release loop. Slices fade per their decay profile's half-life; below the profile's `min_salience_to_retain` threshold they release per the tombstone policy (`thin` / `full` / `none`). Every decision is itself recorded as a self-state slice, so the system can reason about its own memory over time.

**Recall + embeddings.** An append-only `substrate_recall_log` audits every `recall()` call, and slices carry a pgvector `embedding` column. The Curator backfills semantic embeddings asynchronously; recall against not-yet-embedded slices falls back to keyword Jaccard. When enabled via `THOTH_SUBSTRATE_RECALL=1`, each turn's `<memory-context>` is composed from substrate slices using a composite score (vector similarity + keyword Jaccard + salience + recency, under a token budget), and the model gets a `substrate_recall_more` tool for explicit deeper searches.

### Inspecting substrate state

```bash
thoth substrate            # default summary (streams, slice counts, pending)
thoth substrate streams    # per-stream slice counts
thoth substrate slices --stream thoth.world.user_message.cli --limit 20
thoth substrate pending    # current pending-queue depth + oldest age
thoth substrate profiles   # seeded decay profiles
thoth substrate curator    # Curator decay/release activity
thoth substrate recall     # recall coverage + recent calls
```

If your DB is on an older Alembic revision when Thoth starts, boot raises a `RuntimeError` with the upgrade command to run; set `THOTH_AUTO_MIGRATE=1` to upgrade automatically on first boot. For the full design — the L0–L4 layer stack, the sub-agents, and the recall pipeline — see [`docs/architecture/substrate.md`](docs/architecture/substrate.md). Procedural operator docs ship as a bundled skill — load with `/substrate`.

## Cost & latency visibility

Every turn records its token usage, estimated cost, and wall-clock duration to the `agent_turn_cost` table (always on; disable with `THOTH_TURN_COST=0`). The substrate crew's own LLM spend lands in the sibling `substrate_agent_cost` table. Two surfaces read them:

```bash
thoth cost               # 24h / 7d / 30d rollup + per-model breakdown + crew spend
thoth cost --hours 6     # custom trailing window
thoth cost --json        # machine-readable
```

The gateway's API server also exposes `GET /metrics` (Prometheus text format, trailing-24h gauges: turns, tokens by kind, estimated USD, p50/p95 turn duration, substrate crew spend) — see the [API server docs](https://thoth.519lab.com/docs/user-guide/features/api-server). Costs are estimates from the local price catalog, never billing truth; turns on routes with unknown pricing are counted separately so the blind spot is visible.

---

## Getting Started

```bash
thoth              # Interactive CLI — start a conversation
thoth model        # Choose your LLM provider and model
thoth tools        # Configure which tools are enabled
thoth config set   # Set individual config values
thoth gateway      # Start the messaging gateway (Telegram, Discord, etc.)
thoth setup        # Run the full setup wizard (configures everything at once)
thoth update       # Update to the latest version
thoth doctor       # Diagnose any issues
```

### CLI vs Messaging quick reference

Thoth has two entry points: start the terminal UI with `thoth`, or run the gateway and talk to it from Telegram, Discord, Slack, WhatsApp, Signal, or Email. Once you're in a conversation, many slash commands are shared across both.

| Action | CLI | Messaging platforms |
|---------|-----|---------------------|
| Start chatting | `thoth` | `thoth gateway setup` + `thoth gateway start`, then message the bot |
| Start fresh conversation | `/new` or `/reset` | `/new` or `/reset` |
| Change model | `/model [provider:model]` | `/model [provider:model]` |
| Set a personality | `/personality [name]` | `/personality [name]` |
| Retry or undo the last turn | `/retry`, `/undo` | `/retry`, `/undo` |
| Compress context / check usage | `/compress`, `/usage`, `/insights [--days N]` | `/compress`, `/usage`, `/insights [days]` |
| Browse skills | `/skills` or `/<skill-name>` | `/<skill-name>` |
| Interrupt current work | `Ctrl+C` or send a new message | `/stop` or send a new message |
| Platform-specific status | `/platforms` | `/status`, `/sethome` |

Run `thoth --help` (or `thoth <command> --help`) for the full command surface.

---

## Development testing

The test suite uses `pytest-postgresql` against a **separate** PostgreSQL container so your real database on port 5432 is never touched.

**One-time setup:**

```bash
docker compose --profile test up -d postgres-test    # dedicated PG on port 5433, own volume
```

**Running tests locally:**

```bash
# Per-file isolation runner (matches CI). Picks up the test DB via
# tests/conftest.py:_TEST_PG_PORT (defaults to 5433).
PYTEST_XDIST_WORKER=run_local uv run python scripts/run_tests_parallel.py tests/substrate/

# Or a single file:
PYTEST_XDIST_WORKER=run_local uv run python -m pytest tests/substrate/test_commit.py \
    -o "addopts=" --timeout-method=thread --timeout=120
```

`PYTEST_XDIST_WORKER` must be set when running pytest directly (the parallel runner sets it per subprocess). The value is just a unique label — `pytest-postgresql` uses it to derive per-worker DB names so concurrent subprocesses don't race on the shared template DB. To target a different test PG, set `THOTH_TEST_POSTGRES_PORT` (or `POSTGRES_PORT`) before running pytest.

**Running tests in a Linux container (matches CI exactly):**

The `test-runner` docker-compose service runs the suite inside Debian + Python 3.11 + the `[all,dev]` extras — mirroring CI — so failures reproduce locally and Windows-host noise (POSIX permissions, `/mnt/c` paths, tilde expansion) disappears.

```bash
docker compose --profile test up -d postgres-test
docker compose --profile test build test-runner       # first time, ~3 min
scripts/run_tests_docker.sh                            # full suite
scripts/run_tests_docker.sh tests/substrate/test_commit.py
scripts/run_tests_docker.sh tests/substrate/ -- -v -k 'reinforce'
```

Source is bind-mounted so test edits don't trigger an image rebuild; the venv lives at `/opt/venv` inside the image to survive the bind mount.

---

## Migrating from OpenClaw

If you're coming from OpenClaw, Thoth can automatically import your settings, memories, skills, and API keys. The setup wizard (`thoth setup`) detects `~/.openclaw` and offers to migrate before configuration begins. Anytime after install:

```bash
thoth claw migrate              # Interactive migration (full preset)
thoth claw migrate --dry-run    # Preview what would be migrated
thoth claw migrate --preset user-data   # Migrate without secrets
thoth claw migrate --overwrite  # Overwrite existing conflicts
```

What gets imported: persona file (**SOUL.md**), memories (MEMORY.md / USER.md), user-created skills, command allowlist, messaging settings, allowlisted API keys, TTS assets, and workspace instructions (AGENTS.md). See `thoth claw migrate --help` for all options.

---

## Contributing

Contributions are welcome. Clone and go:

```bash
git clone https://github.com/519lab/thoth-agent.git
cd thoth-agent
./setup-thoth.sh     # installs uv, creates venv, installs .[all], symlinks ~/.local/bin/thoth
./thoth              # auto-detects the venv, no need to `source` first
```

Manual path (equivalent):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[all,dev]"
scripts/run_tests.sh
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for development setup and PR process, and [`CLAUDE.md`](CLAUDE.md) for the architecture and conventions agents (and humans) should follow.

---

## Community

- 📚 [Skills Hub](https://agentskills.io) — the `agentskills.io` open skill standard
- 🐛 [Issues](https://github.com/519lab/thoth-agent/issues)
- 🔌 [computer-use-linux](https://github.com/avifenesh/computer-use-linux) — Linux desktop-control MCP server with AT-SPI accessibility trees, Wayland/X11 input, screenshots, and compositor window targeting

---

## Acknowledgments

Thoth stands on the shoulders of **[Hermes](https://github.com/NousResearch/hermes-agent)**, the open-source agent created by **[Nous Research](https://nousresearch.com)**. The terminal interface, the messaging gateway, the skills system, the tool framework, the seven terminal backends — that foundation is their work, generously released under a permissive license so that others could build on it. Thoth exists because they chose to build in the open.

To the Nous Research team and to every contributor who shaped that foundation over its many releases: **thank you.** This project carries your work forward with deep gratitude and respect.

---

## License

Released under the **MIT License** — see [LICENSE](LICENSE).

The original Hermes copyright (© Nous Research) is retained in the license file, as MIT requires and as we're glad to do. New work in this project is contributed under the same MIT terms.
