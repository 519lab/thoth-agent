"""Unit tests for the context-engine grading suite — NO model calls, NO DB.

Covers the four contracts that keep the suite trustworthy as a measurement
instrument (plan §4):

1. Determinism: every ``setup()`` writes byte-identical fixtures across two
   runs (a paired A/B on one seed is meaningless if fixtures drift).
2. Oracle soundness (negative): every oracle FAILS on a known-bad scenario;
   end-state and memory oracles additionally fail on an untouched workspace.
3. Oracle soundness (positive): every oracle PASSES on a known-good scenario
   (covers all 5 end-state tasks — well past the "at least 3" bar).
4. Report aggregation math + runner error-handling (a raising agent yields a
   failed TaskResult, never an exception).

Fixture sizes are shrunk via ``THOTH_EVAL_FIXTURE_SCALE`` — determinism and
oracle logic are size-independent, and small fixtures keep the suite fast.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from eval.context_suite import tasks as tasks_mod
from eval.context_suite.tasks import (
    FAMILY_CONSTRAINT,
    FAMILY_END_STATE,
    FAMILY_MEMORY_PROBE,
    Transcript,
    get_tasks,
    list_tasks,
    snapshot_tree,
)
from eval.context_suite import run as run_mod
from eval.context_suite import runner as runner_mod


@pytest.fixture(autouse=True)
def _small_fixtures(monkeypatch):
    """Shrink fixtures so the model-free tests stay well under the 30s cap."""
    monkeypatch.setattr(tasks_mod, "FIXTURE_SCALE", 0.2)


def _fresh_ws(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


# --------------------------------------------------------------------------- #
# Registry sanity                                                             #
# --------------------------------------------------------------------------- #


def test_registry_has_ten_tasks_across_three_families():
    all_tasks = get_tasks()
    assert len(all_tasks) == 10
    ids = [t.id for t in all_tasks]
    assert len(set(ids)) == 10, "task ids must be unique"
    fams = {t.family for t in all_tasks}
    assert fams == {FAMILY_END_STATE, FAMILY_MEMORY_PROBE, FAMILY_CONSTRAINT}
    counts = {f: sum(1 for t in all_tasks if t.family == f) for f in fams}
    assert counts[FAMILY_END_STATE] == 5
    assert counts[FAMILY_MEMORY_PROBE] == 3
    assert counts[FAMILY_CONSTRAINT] == 2
    # Every task is multi-turn.
    assert all(len(t.turns) >= 2 for t in all_tasks)


def test_get_tasks_filter_and_unknown():
    picked = get_tasks(["e1_merge_env_to_json", "c1_protected_zone"])
    assert [t.id for t in picked] == ["e1_merge_env_to_json", "c1_protected_zone"]
    with pytest.raises(KeyError):
        get_tasks(["does_not_exist"])


def test_list_tasks_shape():
    listing = list_tasks()
    assert len(listing) == 10
    for row in listing:
        assert {"id", "family", "title", "turns", "probes"} <= set(row)


# --------------------------------------------------------------------------- #
# 1. Determinism                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("task", get_tasks(), ids=lambda t: t.id)
def test_setup_is_deterministic(task):
    ws_a = _fresh_ws(f"det_a_{task.id}_")
    ws_b = _fresh_ws(f"det_b_{task.id}_")
    task.setup(ws_a)
    task.setup(ws_b)
    snap_a = snapshot_tree(ws_a)
    snap_b = snapshot_tree(ws_b)
    assert snap_a == snap_b, f"{task.id} setup() is not byte-deterministic"
    assert snap_a, f"{task.id} setup() wrote no fixtures"


# --------------------------------------------------------------------------- #
# 2. Oracle soundness — negative                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("task", get_tasks(), ids=lambda t: t.id)
def test_oracle_fails_on_known_bad(task):
    ws = _fresh_ws(f"neg_{task.id}_")
    task.setup(ws)
    initial = snapshot_tree(ws)
    bad = task.make_negative(ws, initial)
    res = task.oracle(ws, bad)
    assert res.passed is False, f"{task.id}: oracle passed a known-bad case"


@pytest.mark.parametrize(
    "task",
    [t for t in get_tasks() if t.family in (FAMILY_END_STATE, FAMILY_MEMORY_PROBE)],
    ids=lambda t: t.id,
)
def test_end_state_and_memory_fail_on_untouched(task):
    """A freshly set-up workspace with an empty transcript must not pass."""
    ws = _fresh_ws(f"untouched_{task.id}_")
    task.setup(ws)
    res = task.oracle(ws, Transcript(initial_files=snapshot_tree(ws)))
    assert res.passed is False, f"{task.id}: oracle passed an untouched workspace"


# --------------------------------------------------------------------------- #
# 3. Oracle soundness — positive                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("task", get_tasks(), ids=lambda t: t.id)
def test_oracle_passes_on_known_good(task):
    ws = _fresh_ws(f"pos_{task.id}_")
    task.setup(ws)
    initial = snapshot_tree(ws)
    good = task.make_positive(ws, initial)
    res = task.oracle(ws, good)
    assert res.passed is True, f"{task.id}: oracle rejected a known-good case ({res.details})"


def test_at_least_three_end_state_oracles_pass_on_golden():
    """Explicit coverage of the plan's 'at least 3 end-state' requirement."""
    end_state = [t for t in get_tasks() if t.family == FAMILY_END_STATE]
    assert len(end_state) >= 3
    passed = 0
    for task in end_state:
        ws = _fresh_ws(f"golden_{task.id}_")
        task.setup(ws)
        good = task.make_positive(ws, snapshot_tree(ws))
        if task.oracle(ws, good).passed:
            passed += 1
    assert passed == len(end_state)


# --------------------------------------------------------------------------- #
# Transcript helpers                                                          #
# --------------------------------------------------------------------------- #


def test_transcript_last_answer_reads_after_final_user():
    tr = Transcript(messages=[
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "final question"},
        {"role": "assistant", "content": "the answer is 42"},
    ])
    assert tr.last_answer() == "the answer is 42"
    assert tr.assistant_texts() == ["a1", "the answer is 42"]


# --------------------------------------------------------------------------- #
# 4a. Report aggregation math                                                #
# --------------------------------------------------------------------------- #


def _row(task_id, engine, passed, outcome, prompt, cache_read, cost, compress, error=None):
    return {
        "task_id": task_id, "family": "end_state", "engine": engine,
        "model": "m", "run_index": 0, "passed": passed,
        "mean_outcome": outcome, "compression_count": compress,
        "duration_s": 1.0, "error": error,
        "tokens": {"prompt_tokens": prompt, "cache_read_tokens": cache_read,
                   "cost_usd": cost, "total_tokens": prompt},
    }


def test_agg_block_math():
    rows = [
        _row("t1", "A", True, 1.0, 1000, 500, 0.10, 1),
        _row("t2", "A", False, 0.0, 3000, 0, 0.30, 3),
    ]
    agg = run_mod._agg_block(rows)
    assert agg["runs"] == 2
    assert agg["pass_rate"] == 0.5
    assert agg["mean_outcome"] == 0.5
    assert agg["mean_prompt_tokens_per_task"] == 2000.0
    assert agg["mean_cost_per_task"] == 0.2
    # cache_hit_rate = total_cache_read(500) / total_prompt(4000)
    assert agg["cache_hit_rate"] == 0.125
    assert agg["mean_compressions_per_task"] == 2.0
    assert agg["errors"] == 0


def test_agg_block_empty():
    agg = run_mod._agg_block([])
    assert agg["runs"] == 0 and agg["pass_rate"] == 0.0


def test_summarize_groups_by_task_and_engine():
    rows = [
        _row("t1", "compressor", True, 1.0, 1000, 0, 0.1, 1),
        _row("t1", "substrate", True, 1.0, 800, 200, 0.05, 0),
        _row("t2", "compressor", False, 0.0, 2000, 0, 0.2, 2, error="boom"),
    ]
    summary = run_mod.summarize(rows)
    assert summary["n_rows"] == 3
    assert set(summary["engines"]) == {"compressor", "substrate"}
    assert set(summary["per_task"]) == {"t1", "t2"}
    assert len(summary["per_task"]["t1"]["runs"]) == 2
    assert summary["per_engine"]["compressor"]["runs"] == 2
    assert summary["per_engine"]["compressor"]["errors"] == 1
    assert summary["per_engine"]["substrate"]["pass_rate"] == 1.0
    assert summary["overall"]["runs"] == 3


def test_summarize_accepts_taskresult_objects():
    tr = runner_mod.TaskResult(
        task_id="t1", family="end_state", engine="A", model="m", run_index=0,
        passed=True, oracle_details="ok", mean_outcome=1.0,
        tokens={"prompt_tokens": 10, "cache_read_tokens": 0, "cost_usd": 0.0},
    )
    summary = run_mod.summarize([tr])
    assert summary["n_rows"] == 1
    assert summary["per_engine"]["A"]["pass_rate"] == 1.0


# --------------------------------------------------------------------------- #
# 4b. Runner tool-outcome accounting + error handling                        #
# --------------------------------------------------------------------------- #


def test_tool_outcomes_counts_calls_and_failures():
    messages = [
        {"role": "assistant", "tool_calls": [
            {"id": "1", "function": {"name": "terminal"}},
            {"id": "2", "function": {"name": "write_file"}},
        ]},
        {"role": "tool", "tool_call_id": "1",
         "content": '{"content": {"output": "ok", "error": null}}'},
        {"role": "tool", "tool_call_id": "2",
         "content": '{"error": "disk full"}'},
    ]
    calls, failures = runner_mod._tool_outcomes(messages)
    assert calls == 2
    assert failures == 1


class _RaisingAgent:
    """Stand-in AIAgent whose first turn raises — exercises error handling."""

    def __init__(self):
        self.context_compressor = None

    def run_conversation(self, *a, **k):
        raise RuntimeError("simulated provider explosion")


class _ScriptedAgent:
    """Model-free agent that returns a canned good answer on the final turn."""

    def __init__(self, final_question: str, good_answer: str):
        self._final_q = final_question
        self._good = good_answer
        self._messages: list = []
        self.session_prompt_tokens = 12000
        self.session_completion_tokens = 400
        self.session_cache_read_tokens = 6000
        self.session_cache_write_tokens = 100
        self.session_reasoning_tokens = 0
        self.session_total_tokens = 12400
        self.session_estimated_cost_usd = 0.021
        self.session_api_calls = 7

        class _Engine:
            compression_count = 3
            threshold_tokens = 999_999
        self.context_compressor = _Engine()

    def run_conversation(self, user_message, conversation_history=None, task_id=None):
        self._messages.append({"role": "user", "content": user_message})
        answer = self._good if user_message == self._final_q else "ok"
        self._messages.append({"role": "assistant", "content": answer})
        return {
            "messages": list(self._messages),
            "completed": True, "failed": False, "interrupted": False,
            "api_calls": 1,
        }


def test_run_task_raising_agent_yields_failed_result(monkeypatch):
    task = get_tasks(["e1_merge_env_to_json"])[0]
    monkeypatch.setattr(runner_mod, "_build_agent", lambda **kw: _RaisingAgent())
    result = runner_mod.run_task(task, model="fake-model", engine="compressor",
                                 timeout_s=30)
    assert result.passed is False
    assert result.error is not None
    assert "simulated provider explosion" in result.error
    assert result.task_id == task.id


def test_run_task_happy_path_without_model(monkeypatch):
    """Drive the full runner pipeline (env scoping, threading, oracle, probes,
    tokens) with a scripted agent — no model, no DB."""
    task = get_tasks(["m1_user_stated_facts"])[0]
    good = "The deployment key was DEPLOY-7F3A-2291 and the region was eu-west-2."
    monkeypatch.setattr(
        runner_mod, "_build_agent",
        lambda **kw: _ScriptedAgent(task.turns[-1], good),
    )
    result = runner_mod.run_task(task, model="fake-model", engine="substrate",
                                 timeout_s=30)
    assert result.error is None
    assert result.passed is True, result.oracle_details
    assert result.engine == "substrate"
    assert result.compression_count == 3
    assert result.tokens["prompt_tokens"] == 12000
    assert result.api_calls == 7
    assert 0.0 <= result.mean_outcome <= 1.0
    # The memory probe should have recorded a result.
    assert any(p["name"] == "reactive_pagein" for p in result.probes)


# --------------------------------------------------------------------------- #
# DB-backed grading mode (attach_db)                                           #
# --------------------------------------------------------------------------- #


class TestAttachDb:
    def _reset(self, monkeypatch):
        import eval.context_suite.runner as runner_mod

        monkeypatch.setattr(runner_mod, "_db_attached_dsn", None)
        # attach_db now really initialises the pool (the 2026-07-03 wiring
        # fix) — stub the bridge so unit tests with fake DSNs never open a
        # connection. The real init path is covered by the DB-backed smoke.
        import thoth_db

        monkeypatch.setattr(thoth_db, "run_sync", lambda coro: coro.close())
        return runner_mod

    def test_refuses_live_port(self, monkeypatch):
        runner_mod = self._reset(monkeypatch)
        with pytest.raises(RuntimeError, match="5432"):
            runner_mod.attach_db("postgresql://u:p@localhost:5432/thoth")

    def test_sets_env_and_boots_writer(self, monkeypatch):
        runner_mod = self._reset(monkeypatch)
        monkeypatch.delenv("THOTH_PG_DSN", raising=False)
        booted = {}

        def fake_boot(log=None, *, mode="writer"):
            booted["mode"] = mode
            return object()  # substrate handle

        import thoth_bootstrap

        monkeypatch.setattr(thoth_bootstrap, "bootstrap_substrate_sync", fake_boot)
        ok = runner_mod.attach_db("postgresql://u:p@localhost:5433/thoth_baseline")
        assert ok is True
        assert booted["mode"] == "writer"
        import os

        assert os.environ["THOTH_PG_DSN"].endswith(":5433/thoth_baseline")

    def test_degrades_when_substrate_boot_fails(self, monkeypatch):
        runner_mod = self._reset(monkeypatch)
        import thoth_bootstrap

        monkeypatch.setattr(
            thoth_bootstrap, "bootstrap_substrate_sync",
            lambda log=None, *, mode="writer": None,
        )
        ok = runner_mod.attach_db("postgresql://u:p@localhost:5433/thoth_baseline")
        assert ok is False  # session store may still work; substrate is off

    def test_second_attach_same_dsn_is_noop_different_raises(self, monkeypatch):
        runner_mod = self._reset(monkeypatch)
        import thoth_bootstrap

        monkeypatch.setattr(
            thoth_bootstrap, "bootstrap_substrate_sync",
            lambda log=None, *, mode="writer": object(),
        )
        dsn = "postgresql://u:p@localhost:5433/thoth_baseline"
        assert runner_mod.attach_db(dsn) is True
        assert runner_mod.attach_db(dsn) is True  # idempotent
        with pytest.raises(RuntimeError, match="different DSNs"):
            runner_mod.attach_db("postgresql://u:p@localhost:5433/other")


# --------------------------------------------------------------------------- #
# Watchdog: cooperative interrupt + non-blocking abandonment (2026-07-03 fix)  #
# --------------------------------------------------------------------------- #


class TestWatchdog:
    def _task(self):
        from eval.context_suite.tasks import get_tasks

        return get_tasks(["e1_merge_env_to_json"])[0]

    def test_timeout_interrupts_agent_and_returns(self, monkeypatch):
        """Watchdog fires -> agent.interrupt() called -> cooperative exit
        inside the grace window returns without blocking on the thread."""
        import eval.context_suite.runner as runner_mod

        interrupted = {"called": False}

        class FakeAgent:
            def interrupt(self, message=""):
                interrupted["called"] = True

        def fake_inner(task, *, control=None, **kwargs):
            control["agent"] = FakeAgent()
            # Block past the (tiny) timeout, exiting only on cancellation —
            # the cooperative path.
            for _ in range(200):
                if control.get("cancelled"):
                    return runner_mod.TaskResult(
                        task_id=task.id, family=task.family, engine="compressor",
                        model="m", run_index=0, passed=False,
                        oracle_details="cancelled", mean_outcome=0.0,
                        error="cancelled",
                    )
                time.sleep(0.05)
            raise AssertionError("never cancelled")

        monkeypatch.setattr(runner_mod, "_run_task_inner", fake_inner)
        started = time.time()
        result = runner_mod.run_task(
            self._task(), model="m", engine="compressor", timeout_s=1
        )
        elapsed = time.time() - started
        assert interrupted["called"] is True
        assert result.passed is False
        assert "cancelled" in (result.error or "")
        assert elapsed < 1 + runner_mod.CANCEL_GRACE_S  # cooperative, not full grace

    def test_truly_hung_thread_is_abandoned_not_awaited(self, monkeypatch):
        """A thread that ignores cancellation (hung provider stream) is
        abandoned after the grace window — run_task must NOT block on it
        (the 2026-07-03 arm-A wedge: executor __exit__ waited forever)."""
        import threading

        import eval.context_suite.runner as runner_mod

        release = threading.Event()

        def fake_inner(task, *, control=None, **kwargs):
            release.wait(timeout=60)  # simulates a hung stream; ignores cancel
            return None

        monkeypatch.setattr(runner_mod, "_run_task_inner", fake_inner)
        monkeypatch.setattr(runner_mod, "CANCEL_GRACE_S", 1)
        started = time.time()
        result = runner_mod.run_task(
            self._task(), model="m", engine="compressor", timeout_s=1
        )
        elapsed = time.time() - started
        release.set()  # let the leaked thread die promptly for test hygiene
        assert result.passed is False
        assert "timeout" in (result.error or "")
        assert elapsed < 10  # returned at ~timeout+grace, never awaited the thread

    def test_build_agent_disables_background_review_forks(self, monkeypatch):
        """Grading agents must not spawn post-turn review threads: they
        outlive the task, race the runner's chdir across workspaces, and
        burn unmetered tokens (observed in the 2026-07-03 arm-A wedge)."""
        import sys
        from types import SimpleNamespace

        import eval.context_suite.runner as runner_mod

        class StubAgent:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self._memory_nudge_interval = 5   # non-zero defaults
                self._skill_nudge_interval = 7
                self.context_compressor = SimpleNamespace(threshold_tokens=100_000)

        monkeypatch.setitem(
            sys.modules, "run_agent", SimpleNamespace(AIAgent=StubAgent)
        )
        agent = runner_mod._build_agent(
            model="m", base_url=None, api_key=None, provider=None,
            max_iterations=5, quiet=True, compress_threshold_tokens=50_000,
        )
        assert agent._memory_nudge_interval == 0
        assert agent._skill_nudge_interval == 0
        assert agent.context_compressor.threshold_tokens == 50_000


# --------------------------------------------------------------------------- #
# Embedding-gap fix: provider-env loading + per-turn backfill (2026-07-05)     #
# EMBEDDING-GAP-FINDING.md — make the eval mirror production so proactive      #
# recall is actually exercisable in grading.                                   #
# --------------------------------------------------------------------------- #


class TestEmbeddingProviderEnvLoading:
    """``_ensure_embedding_provider_env`` / ``_load_embedding_keys_from_env_file``."""

    def test_loads_only_provider_keys_from_dotenv(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "# a comment\n"
            'export OPENROUTER_API_KEY="or-secret-123"\n'
            "OPENAI_API_KEY=oai-secret-456\n"
            "SOME_OTHER_SECRET=nope\n"
            "MALFORMED LINE NO EQUALS\n"
        )
        loaded = runner_mod._load_embedding_keys_from_env_file(env)
        assert loaded == {
            "OPENROUTER_API_KEY": "or-secret-123",
            "OPENAI_API_KEY": "oai-secret-456",
        }

    def test_missing_file_returns_empty(self, tmp_path):
        assert runner_mod._load_embedding_keys_from_env_file(tmp_path / "nope.env") == {}

    def test_ensure_populates_env_and_resets_client(self, tmp_path, monkeypatch):
        # No provider key present (conftest strips them); load from a fake file.
        for k in runner_mod._EMBED_PROVIDER_KEYS:
            monkeypatch.delenv(k, raising=False)
        env = tmp_path / ".env"
        env.write_text("OPENROUTER_API_KEY=from-file-key\n")

        reset_calls = {"n": 0}
        from substrate.recall import embeddings

        monkeypatch.setattr(
            embeddings, "reset_client_cache",
            lambda: reset_calls.__setitem__("n", reset_calls["n"] + 1),
        )

        ok = runner_mod._ensure_embedding_provider_env(env_path=env)

        assert ok is True
        assert os.environ["OPENROUTER_API_KEY"] == "from-file-key"
        assert reset_calls["n"] == 1  # lazy client dropped so the key is picked up

    def test_ensure_warns_when_no_key_anywhere(self, tmp_path, monkeypatch):
        for k in runner_mod._EMBED_PROVIDER_KEYS:
            monkeypatch.delenv(k, raising=False)
        from substrate.recall import embeddings

        monkeypatch.setattr(embeddings, "reset_client_cache", lambda: None)

        # Spy on logger.warning directly — deterministic regardless of pytest
        # caplog handler/propagation state under a combined multi-file run.
        warnings: list = []
        monkeypatch.setattr(
            runner_mod.logger, "warning",
            lambda msg, *a, **k: warnings.append(msg % a if a else msg),
        )

        ok = runner_mod._ensure_embedding_provider_env(env_path=tmp_path / "absent.env")

        assert ok is False  # degrades, does not crash
        assert any("EMBEDDING-BLIND" in w for w in warnings)

    def test_ensure_keeps_existing_env_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "already-here")
        from substrate.recall import embeddings

        monkeypatch.setattr(embeddings, "reset_client_cache", lambda: None)
        # env_path points nowhere; existing key must be honoured without a load.
        ok = runner_mod._ensure_embedding_provider_env(env_path=Path("/does/not/exist.env"))
        assert ok is True
        assert os.environ["OPENAI_API_KEY"] == "already-here"


class _CountingAgent:
    """Minimal agent: one canned turn result per call, no tools, no DB."""

    def __init__(self):
        self.context_compressor = None

    def run_conversation(self, user_message, conversation_history=None, task_id=None):
        msgs = list(conversation_history or [])
        msgs += [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": "ok"},
        ]
        return {
            "messages": msgs,
            "completed": True, "failed": False, "interrupted": False,
            "api_calls": 1,
        }


class _FakeTask:
    def __init__(self, n_turns: int):
        self.id = "fake_task"
        self.turns = [f"turn {i}" for i in range(n_turns)]


class TestPerTurnBackfillWiring:
    """``_run_turns`` drives the embedding backfill once per turn, guarded by
    ``with_db`` and the ``THOTH_EVAL_EMBED_BACKFILL`` knob."""

    def _patch_backfill(self, monkeypatch):
        calls = {"n": 0}
        monkeypatch.setattr(
            runner_mod, "backfill_unembedded_slices_sync",
            lambda substrate, **kw: calls.__setitem__("n", calls["n"] + 1),
        )
        # A non-None substrate so the guard doesn't short-circuit.
        monkeypatch.setattr(runner_mod, "_db_substrate", object())
        return calls

    def test_backfill_runs_once_per_turn_with_db(self, monkeypatch):
        calls = self._patch_backfill(monkeypatch)
        monkeypatch.delenv("THOTH_EVAL_EMBED_BACKFILL", raising=False)
        metrics, _ = runner_mod._run_turns(
            _CountingAgent(), _FakeTask(3), control=None, with_db=True
        )
        assert len(metrics) == 3
        assert calls["n"] == 3

    def test_backfill_skipped_without_db(self, monkeypatch):
        calls = self._patch_backfill(monkeypatch)
        runner_mod._run_turns(
            _CountingAgent(), _FakeTask(3), control=None, with_db=False
        )
        assert calls["n"] == 0

    def test_backfill_disabled_by_env_knob(self, monkeypatch):
        calls = self._patch_backfill(monkeypatch)
        monkeypatch.setenv("THOTH_EVAL_EMBED_BACKFILL", "0")
        runner_mod._run_turns(
            _CountingAgent(), _FakeTask(3), control=None, with_db=True
        )
        assert calls["n"] == 0

    def test_backfill_failure_never_fails_turns(self, monkeypatch):
        def _boom(substrate, **kw):
            raise RuntimeError("embed backend down")

        monkeypatch.setattr(runner_mod, "backfill_unembedded_slices_sync", _boom)
        monkeypatch.setattr(runner_mod, "_db_substrate", object())
        monkeypatch.delenv("THOTH_EVAL_EMBED_BACKFILL", raising=False)
        # Must not raise despite the backfill blowing up every turn.
        metrics, _ = runner_mod._run_turns(
            _CountingAgent(), _FakeTask(2), control=None, with_db=True
        )
        assert len(metrics) == 2
