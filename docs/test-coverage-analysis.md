# Test Coverage Analysis & Roadmap

A structural survey of where the test suite is thin, plus the tooling now
wired up to measure it. This is a living document — update the priority
list as gaps get closed.

## How to measure coverage

The suite runs one pytest subprocess per file
(`scripts/run_tests_parallel.py`), so coverage runs in **parallel mode**:
each subprocess writes a uniquely-suffixed `.coverage.*` file, combined at
the end.

```bash
# Whole suite with coverage (prints a report + writes coverage.xml):
python scripts/run_tests_parallel.py --coverage
# or: HERMES_TEST_COVERAGE=1 python scripts/run_tests_parallel.py

# A subset (fast iteration on one area):
python scripts/run_tests_parallel.py --coverage tests/tools tests/agent
```

Config lives in `[tool.coverage.*]` in `pyproject.toml`. CI runs this via
the **`Coverage`** workflow (`.github/workflows/coverage.yml`), which is
**non-blocking by design**: it surfaces the number on every PR (job
summary + `coverage.xml` artifact) without gating the merge. Once the team
agrees a floor, set `fail_under` in `[tool.coverage.report]` and/or add a
ratchet, then promote the job to a required check.

## Methodology of the gap survey

712 first-party source modules (~30k LOC in top-level files alone) against
1,277 test files. Signals used: (1) which source modules are ever
imported/exercised by a test, (2) test-LOC-to-source-LOC ratio per area,
(3) test footprint of the large monolithic modules. These are structural
proxies; the `--coverage` run above gives exact line/branch numbers.

## Headline findings

1. **No coverage measurement existed** before this — CI ran pass/fail
   only. (Addressed by the tooling above.)
2. **65 source modules are never imported by any test.** Highest
   criticality by size:

   | Module | LOC | Why it matters |
   |---|---|---|
   | `agent/tool_executor.py` | 910 | Core tool-dispatch path |
   | `agent/conversation_compression.py` | 636 | Context-window management |
   | `agent/background_review.py` | 587 | Background agent behavior |
   | `agent/message_sanitization.py` | 444 | Input sanitization (security) — **now covered, see below** |
   | `agent/codex_runtime.py` | 448 | Runtime adapter |
   | `tools/file_state.py` | 332 | File mutation tracking |
   | `tools/path_security.py` | 43 | Path-traversal guard (security) — **now covered, see below** |

3. **Security-critical helpers were untested.** `tools/path_security.py`
   (the shared traversal guard used by `skill_manager_tool`, `skills_tool`,
   `cronjob_tools`, `credential_files`) and `agent/message_sanitization.py`
   had zero tests. A regression in the former is a sandbox escape.
4. **`tui_gateway` is the weakest area** (test/source LOC ratio **0.21**).
   `server.py` is a 6,782-line monolith with only protocol-level tests;
   `transport.py`, `ws.py`, `event_publisher.py`, `slash_worker.py` have no
   direct tests.
5. **Large top-level modules with thin tests:** `mini_swe_runner.py` (735
   LOC, ~60 LOC of tests), `batch_runner.py` (1,321 / 2 importers),
   `trajectory_compressor.py` (1,508 / 4 importers).

Per-area test/source LOC ratios: `cron` 2.12, `gateway` 1.32, `tools`
1.22, `acp_adapter` 1.10, `hermes_cli` 0.86, `substrate` 0.78, `agent`
0.75, **`tui_gateway` 0.21**.

## Prioritized roadmap

1. ✅ **Coverage tooling in CI** (non-blocking baseline) — done.
2. ✅ **Security helpers** — `tests/tools/test_path_security.py` (100%) and
   `tests/agent/test_message_sanitization.py` (~89%) — done as the first
   concrete slice.
3. **Core execution path:** direct tests for `agent/tool_executor.py` and
   `agent/conversation_compression.py`.
4. **`tui_gateway` transport layer:** cover `transport.py` / `ws.py` /
   `event_publisher.py`; begin decomposing/testing `server.py`.
5. **Backfill thin monoliths:** `mini_swe_runner.py`, `batch_runner.py`,
   `trajectory_compressor.py`.
6. **Agree a `fail_under` floor** and flip the coverage job to required.
