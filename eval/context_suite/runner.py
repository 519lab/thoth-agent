"""Drive one grading task end-to-end against a real AIAgent.

Responsibilities (kept out of ``tasks`` so that module stays model-free):

- Build a fresh, throwaway workspace under the system temp dir, run the task's
  ``setup()``, and snapshot it (``initial_files``) so constraint oracles can
  diff against the pre-run state.
- Construct an :class:`AIAgent` programmatically the way ``batch_runner`` does
  (``base_url`` / ``api_key`` / ``model`` / ``max_iterations`` + ``skip_memory``
  / ``skip_context_files``), scoped to the temp workspace so file tools operate
  there, and select the context engine via the ``THOTH_CONTEXT_ENGINE`` seam.
- Thread the task's turns sequentially, passing each turn's returned
  ``messages`` as the next turn's ``conversation_history`` (the batch_runner
  pattern), collecting per-turn outcome-score inputs.
- Read token totals from the agent's ``session_*`` accumulators, the
  compression count from its context engine, and wall-clock duration.
- Run the oracle + probes and package everything into a :class:`TaskResult`.

Robustness contract: a timeout or any exception yields a *failed* TaskResult,
never an uncaught exception — one bad task must not sink the suite.

Safety: by default this points at NO database (``skip_memory=True``, no
``session_db`` — inert with respect to both instances). DB-backed grading is
explicit opt-in via :func:`attach_db`, which refuses port 5432 (the live
install) and binds the process to one snapshot-seeded grading DSN — required
to grade the substrate engine, whose handles/eviction slices/proactive recall
only exist with a session store + bound substrate.

Not thread-safe for *concurrent* tasks: it ``chdir``s the process into the
workspace (so relative file-tool paths resolve there) and mutates a few env
vars, restoring both afterward. Run tasks sequentially (the CLI does).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from eval.context_suite.tasks import Task, Transcript, snapshot_tree

logger = logging.getLogger(__name__)

# Default per-task wall-clock cap. Long-horizon tasks with large fixtures and
# up to 40 iterations are slow; a hung provider call must still be reclaimable.
DEFAULT_TASK_TIMEOUT_S = 900
CANCEL_GRACE_S = 30  # cooperative-exit window after a watchdog interrupt


@dataclass
class TurnMetric:
    """Per-turn signals feeding ``compute_outcome_score``."""

    index: int
    completed: bool
    failed: bool
    interrupted: bool
    tool_calls: int
    tool_failures: int
    api_calls: int
    outcome_score: float


@dataclass
class TaskResult:
    """Everything measured for one task-run (one engine, one repeat)."""

    task_id: str
    family: str
    engine: str
    model: str
    run_index: int
    passed: bool
    oracle_details: str
    mean_outcome: float
    per_turn: List[Dict[str, Any]] = field(default_factory=list)
    tokens: Dict[str, float] = field(default_factory=dict)
    compression_count: int = 0
    api_calls: int = 0
    duration_s: float = 0.0
    probes: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Tool-outcome accounting (mirrors batch_runner's success heuristic)          #
# --------------------------------------------------------------------------- #


def _tool_outcomes(messages: List[Dict[str, Any]]) -> tuple[int, int]:
    """Count (tool_calls, tool_failures) over ``messages``.

    Failure detection mirrors ``batch_runner._extract_tool_stats``: a tool
    result is a failure only on an explicit error signal (non-null ``error``
    field, ``success: false``, empty content, or a leading ``Error:``) — a
    non-zero exit code alone is not a failure, since the model can self-correct.
    """
    call_names: Dict[str, str] = {}
    calls = 0
    failures = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                calls += 1
                call_names[tc.get("id", "")] = (
                    tc.get("function", {}) or {}
                ).get("name", "")
        elif msg.get("role") == "tool":
            content = msg.get("content", "")
            is_failure = False
            try:
                parsed = json.loads(content) if isinstance(content, str) else content
                if isinstance(parsed, dict):
                    if parsed.get("error") is not None:
                        is_failure = True
                    inner = parsed.get("content")
                    if isinstance(inner, dict) and inner.get("error") is not None:
                        is_failure = True
                    if parsed.get("success") is False:
                        is_failure = True
            except (json.JSONDecodeError, ValueError, TypeError):
                if not content:
                    is_failure = True
                elif str(content).strip().lower().startswith("error:"):
                    is_failure = True
            if is_failure:
                failures += 1
    return calls, failures


# --------------------------------------------------------------------------- #
# Environment scoping                                                          #
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _scoped_env(workspace: Path, engine: str):
    """Chdir into the workspace and set the engine/boundary/yolo env vars.

    Restores the previous cwd and env values on exit so the process is left
    exactly as found (the CLI reuses the process across tasks and runs).
    """
    keys = ("THOTH_CONTEXT_ENGINE", "THOTH_ACTIVE_ROOT", "THOTH_YOLO_MODE")
    prev_env = {k: os.environ.get(k) for k in keys}
    prev_cwd = os.getcwd()
    try:
        os.environ["THOTH_CONTEXT_ENGINE"] = engine
        os.environ["THOTH_ACTIVE_ROOT"] = str(workspace.resolve())
        # Headless: no interactive approval prompts can block the run. The
        # workspace also lives under the system temp dir, which the boundary
        # already treats as inside — belt and suspenders.
        os.environ["THOTH_YOLO_MODE"] = "1"
        os.chdir(workspace)
        yield
    finally:
        os.chdir(prev_cwd)
        for k, v in prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --------------------------------------------------------------------------- #
# The core run                                                                 #
# --------------------------------------------------------------------------- #


_db_attached_dsn: Optional[str] = None


def attach_db(pg_dsn: str) -> bool:
    """Point the process at a grading database and boot the substrate writer.

    The substrate context engine's differentiators (durable eviction
    handles, eviction slices, proactive recall) only exist with a session
    store + bound substrate — without a DSN the engine silently degrades
    to Tier-2 (summarize-only) and an A/B measures nothing. This attaches
    the snapshot-seeded grading DB (scripts/seed-context-baseline-db.sh;
    the test cluster on :5433 — NEVER the live install).

    Process-wide and one-shot by design: the DB pool binds to one DSN per
    process, so one suite invocation grades against one database. Returns
    True when the substrate writer booted, False when it degraded (the
    session store may still work — the substrate boot is best-effort,
    mirroring production startup).
    """
    global _db_attached_dsn
    if _db_attached_dsn is not None:
        if _db_attached_dsn != pg_dsn:
            raise RuntimeError(
                "attach_db called twice with different DSNs — one process "
                "grades against one database; start a new process to switch."
            )
        return True
    if ":5432/" in pg_dsn or pg_dsn.rstrip("/").endswith(":5432"):
        raise RuntimeError(
            "refusing DSN on port 5432 (the live install); grading runs "
            "only against the snapshot-seeded test cluster."
        )
    os.environ["THOTH_PG_DSN"] = pg_dsn
    _db_attached_dsn = pg_dsn
    from thoth_bootstrap import bootstrap_substrate_sync

    substrate = bootstrap_substrate_sync(mode="writer")
    if substrate is None:
        logger.warning(
            "substrate writer failed to boot for %s — session store may "
            "still attach; eviction slices / proactive recall will be OFF",
            pg_dsn.split("@")[-1],
        )
    return substrate is not None


def _build_agent(
    *,
    model: str,
    base_url: Optional[str],
    api_key: Optional[str],
    provider: Optional[str],
    max_iterations: int,
    quiet: bool,
    compress_threshold_tokens: Optional[int],
    with_db: bool = False,
):
    """Construct an AIAgent for the eval (lazy import keeps ``tasks`` light)."""
    from run_agent import AIAgent  # heavy import — deferred to call time

    agent = AIAgent(
        base_url=base_url,
        api_key=api_key,
        provider=provider,
        model=model,
        max_iterations=max_iterations,
        save_trajectories=False,
        quiet_mode=quiet,
        verbose_logging=not quiet,
        skip_context_files=True,  # don't inject SOUL.md/AGENTS.md
        # DB-backed grading (attach_db): the session store must persist
        # messages (eviction handles resolve against it) and the memory
        # manager must run (proactive recall injection). Both engines get
        # the same setting per arm so the comparison stays fair.
        skip_memory=not with_db,
    )
    # Force compression to actually fire at eval fixture sizes: the real model
    # window (e.g. 200k) would need enormous fixtures. Shrinking ``threshold_
    # tokens`` on the already-built engine triggers compression once the prompt
    # crosses our chosen budget, without touching the min-context-window guard
    # (which ran against the real ``context_length`` at construction).
    if compress_threshold_tokens:
        engine = getattr(agent, "context_compressor", None)
        if engine is not None and hasattr(engine, "threshold_tokens"):
            engine.threshold_tokens = int(compress_threshold_tokens)

    # Kill the post-turn background-review forks for grading runs. With a DB
    # attached (skip_memory=False) these fire after turns and spawn threads
    # that OUTLIVE the task: they keep calling the model (unmetered tokens
    # that would pollute the arm's accounting) and race the runner's chdir
    # across workspaces — observed in the 2026-07-03 arm-A wedge as an e1-era
    # thread looping on ``cd <deleted e1 workspace>`` during e5. Grading
    # measures the CONTEXT ENGINE, not the self-improvement loop; zeroing the
    # nudge intervals disables both forks at their gates
    # (conversation_loop._should_review_memory / should_review_skills_after_turn).
    agent._memory_nudge_interval = 0
    agent._skill_nudge_interval = 0
    return agent


def _run_turns(
    agent, task: Task, control: Optional[Dict[str, Any]] = None
) -> tuple[List[TurnMetric], List[Dict[str, Any]]]:
    """Send every turn with threaded history; return per-turn metrics + msgs."""
    from agent.turn_outcome import compute_outcome_score

    history: Optional[List[Dict[str, Any]]] = None
    metrics: List[TurnMetric] = []
    final_messages: List[Dict[str, Any]] = []

    for i, user_message in enumerate(task.turns):
        if control is not None and control.get("cancelled"):
            # Watchdog fired: the current turn was interrupted via
            # agent.interrupt(); do not start further turns.
            break
        prev_len = len(history) if history else 0
        result = agent.run_conversation(
            user_message,
            conversation_history=history,
            task_id=f"{task.id}_t{i}",
        )
        messages = result.get("messages", []) or []
        final_messages = messages
        history = messages  # thread forward exactly like batch_runner

        # Per-turn tool outcomes: prefer the agent's per-turn counters when the
        # running branch exposes them (accurate through compression); otherwise
        # slice the new messages this turn appended.
        turn_calls = getattr(agent, "_turn_tool_calls", None)
        turn_fails = getattr(agent, "_turn_tool_failures", None)
        if turn_calls is None or turn_fails is None:
            new_slice = messages[prev_len:] if prev_len <= len(messages) else []
            turn_calls, turn_fails = _tool_outcomes(new_slice)

        completed = bool(result.get("completed"))
        failed = bool(result.get("failed"))
        interrupted = bool(result.get("interrupted"))
        score = compute_outcome_score(
            completed=completed,
            failed=failed,
            interrupted=interrupted,
            tool_calls=int(turn_calls or 0),
            tool_failures=int(turn_fails or 0),
        )
        metrics.append(
            TurnMetric(
                index=i,
                completed=completed,
                failed=failed,
                interrupted=interrupted,
                tool_calls=int(turn_calls or 0),
                tool_failures=int(turn_fails or 0),
                api_calls=int(result.get("api_calls", 0) or 0),
                outcome_score=score,
            )
        )
    return metrics, final_messages


def _collect_tokens(agent) -> Dict[str, float]:
    """Session-cumulative token/cost totals — the task totals for this agent."""
    def g(attr: str) -> float:
        return float(getattr(agent, attr, 0) or 0)

    return {
        "prompt_tokens": g("session_prompt_tokens"),
        "completion_tokens": g("session_completion_tokens"),
        "cache_read_tokens": g("session_cache_read_tokens"),
        "cache_write_tokens": g("session_cache_write_tokens"),
        "reasoning_tokens": g("session_reasoning_tokens"),
        "total_tokens": g("session_total_tokens"),
        "cost_usd": round(g("session_estimated_cost_usd"), 6),
    }


def _run_task_inner(
    task: Task,
    *,
    model: str,
    base_url: Optional[str],
    api_key: Optional[str],
    provider: Optional[str],
    engine: str,
    max_iterations: int,
    quiet: bool,
    compress_threshold_tokens: Optional[int],
    run_index: int,
    workspace: Path,
    with_db: bool = False,
    control: Optional[Dict[str, Any]] = None,
) -> TaskResult:
    """Body of a single task run (executed inside the timeout watchdog).

    ``control`` is the watchdog's cooperative-cancellation channel: the body
    publishes its live agent under ``control["agent"]`` as soon as it exists,
    and checks ``control["cancelled"]`` between turns.
    """
    started = time.time()

    task.setup(workspace)
    initial_files = snapshot_tree(workspace)

    with _scoped_env(workspace, engine):
        agent = _build_agent(
            model=model,
            base_url=base_url,
            api_key=api_key,
            provider=provider,
            max_iterations=max_iterations,
            quiet=quiet,
            compress_threshold_tokens=compress_threshold_tokens,
            with_db=with_db,
        )
        if control is not None:
            control["agent"] = agent
        metrics, messages = _run_turns(agent, task, control)

    transcript = Transcript(
        messages=messages, turns=list(task.turns), initial_files=initial_files
    )
    oracle_res = task.oracle(workspace, transcript)

    probe_results: List[Dict[str, Any]] = []
    for probe in task.probes:
        try:
            pr = probe.check(workspace, transcript)
            probe_results.append(
                {"name": pr.name, "passed": pr.passed, "detail": pr.detail}
            )
        except Exception as exc:  # a probe must never fail the task
            probe_results.append(
                {"name": probe.name, "passed": False, "detail": f"probe error: {exc}"}
            )

    tokens = _collect_tokens(agent)
    engine_obj = getattr(agent, "context_compressor", None)
    compression_count = int(getattr(engine_obj, "compression_count", 0) or 0)
    api_calls = int(getattr(agent, "session_api_calls", 0) or 0) or sum(
        m.api_calls for m in metrics
    )
    mean_outcome = (
        sum(m.outcome_score for m in metrics) / len(metrics) if metrics else 0.0
    )

    return TaskResult(
        task_id=task.id,
        family=task.family,
        engine=engine,
        model=model,
        run_index=run_index,
        passed=oracle_res.passed,
        oracle_details=oracle_res.details,
        mean_outcome=round(mean_outcome, 4),
        per_turn=[asdict(m) for m in metrics],
        tokens=tokens,
        compression_count=compression_count,
        api_calls=api_calls,
        duration_s=round(time.time() - started, 2),
        probes=probe_results,
        error=None,
    )


def run_task(
    task: Task,
    *,
    model: str,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    provider: Optional[str] = None,
    engine: str = "compressor",
    max_iterations: int = 40,
    quiet: bool = True,
    compress_threshold_tokens: Optional[int] = 50_000,
    run_index: int = 0,
    timeout_s: int = DEFAULT_TASK_TIMEOUT_S,
    keep_workspace: bool = False,
    with_db: bool = False,
) -> TaskResult:
    """Run one task and return a :class:`TaskResult` (never raises).

    A timeout or any exception is folded into a *failed* result so a single bad
    task can't crash a batch. ``compress_threshold_tokens`` shrinks the engine's
    trigger so compression fires at eval fixture sizes (set ``None`` to use the
    model's real window). ``with_db=True`` requires a prior :func:`attach_db`
    call — it enables session persistence + memory so the substrate engine's
    handles and recall actually operate.
    """
    workspace = Path(tempfile.mkdtemp(prefix=f"ctxsuite_{task.id}_"))

    def _fail(msg: str) -> TaskResult:
        return TaskResult(
            task_id=task.id, family=task.family, engine=engine, model=model,
            run_index=run_index, passed=False, oracle_details=msg,
            mean_outcome=0.0, error=msg,
        )

    control: Dict[str, Any] = {"cancelled": False, "agent": None}
    # Manual executor lifecycle — deliberately NOT a ``with`` block. The
    # 2026-07-03 arm-A wedge: ``ThreadPoolExecutor.__exit__`` is
    # ``shutdown(wait=True)``, which blocks on the very worker the watchdog
    # just timed out (a hung provider stream held it for 15+ minutes while
    # no result row could be written). Cancellation is now cooperative-then-
    # abandon: interrupt the agent (aborts in-flight tools, stops the loop at
    # its next check), give it a grace window to return a partial result,
    # then ``shutdown(wait=False)`` so the suite moves on regardless.
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            _run_task_inner,
            task,
            model=model,
            base_url=base_url,
            api_key=api_key,
            provider=provider,
            engine=engine,
            max_iterations=max_iterations,
            quiet=quiet,
            compress_threshold_tokens=compress_threshold_tokens,
            run_index=run_index,
            workspace=workspace,
            with_db=with_db,
            control=control,
        )
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeout:
            logger.warning(
                "task %s timed out after %ss — interrupting agent",
                task.id, timeout_s,
            )
            control["cancelled"] = True
            agent = control.get("agent")
            if agent is not None:
                try:
                    agent.interrupt("grading watchdog timeout")
                except Exception:
                    logger.debug("watchdog interrupt failed", exc_info=True)
            try:
                # Grace window: a cooperative exit still yields a (partial)
                # result with real metrics instead of a bare timeout row.
                return future.result(timeout=CANCEL_GRACE_S)
            except FutureTimeout:
                logger.warning(
                    "task %s did not exit within the %ss grace window — "
                    "abandoning its thread (interrupt stays set, so it "
                    "cannot keep calling the model once unblocked)",
                    task.id, CANCEL_GRACE_S,
                )
                return _fail(f"timeout after {timeout_s}s (+{CANCEL_GRACE_S}s grace)")
    except Exception as exc:  # construction / setup / oracle failure
        logger.error("task %s crashed: %s", task.id, exc, exc_info=True)
        return _fail(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
    finally:
        pool.shutdown(wait=False)
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)


__all__ = [
    "TaskResult",
    "TurnMetric",
    "attach_db",
    "run_task",
    "DEFAULT_TASK_TIMEOUT_S",
]
