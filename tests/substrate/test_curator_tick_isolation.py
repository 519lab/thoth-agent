"""A failing Curator tick stage must not starve the rest of the tick.

Regression for 2026-06: a ~2-min window of ``_apply_natural_decay`` errors
(transient lock/connection contention during an embedding storm) propagated
out of ``tick()``; the base loop swallowed the *whole* tick, so release, alarm
and embedding were silently skipped every one of those cycles. Each stage is
now isolated so one failure logs and the others still run.

Pure unit test — no DB; every stage is stubbed.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from substrate.agents import Curator


def _make_curator():
    return Curator(MagicMock())


@pytest.mark.asyncio
async def test_failing_decay_stage_does_not_skip_later_stages(monkeypatch):
    cur = _make_curator()
    ran: list[str] = []

    async def _boom():
        ran.append("decay")
        raise RuntimeError("transient decay error")

    def _recorder(name):
        async def _stage():
            ran.append(name)
        return _stage

    monkeypatch.setattr(cur, "_apply_natural_decay", _boom)
    monkeypatch.setattr(cur, "_release_stage", _recorder("release"))
    monkeypatch.setattr(cur, "_alarm_stage", _recorder("alarm"))
    monkeypatch.setattr(cur, "_maybe_emit_embeddings", _recorder("embed"))
    monkeypatch.setattr(cur, "_maybe_retry_failed_embeddings", _recorder("retry"))
    monkeypatch.setattr(cur, "_maybe_curate_upper_layers", _recorder("curate"))

    # Must NOT raise — the failure is isolated and logged.
    await cur.tick()

    # decay was attempted and failed, but every later stage still ran.
    assert ran == ["decay", "release", "alarm", "embed", "retry", "curate"]


@pytest.mark.asyncio
async def test_run_stage_logs_and_swallows(monkeypatch):
    cur = _make_curator()
    logged: list = []
    cur._log = MagicMock()
    cur._log.exception = lambda *a, **k: logged.append((a, k))

    async def _boom():
        raise ValueError("nope")

    await cur._run_stage("decay", _boom)  # does not raise
    assert logged, "stage failure was not logged"
    assert "curator.stage.error" in logged[0][0][0]
    assert logged[0][0][1] == "decay"  # stage name passed through


@pytest.mark.asyncio
async def test_all_stages_run_on_happy_path(monkeypatch):
    cur = _make_curator()
    ran: list[str] = []

    def _recorder(name):
        async def _stage():
            ran.append(name)
        return _stage

    monkeypatch.setattr(cur, "_apply_natural_decay", _recorder("decay"))
    monkeypatch.setattr(cur, "_release_stage", _recorder("release"))
    monkeypatch.setattr(cur, "_alarm_stage", _recorder("alarm"))
    monkeypatch.setattr(cur, "_maybe_emit_embeddings", _recorder("embed"))
    monkeypatch.setattr(cur, "_maybe_retry_failed_embeddings", _recorder("retry"))
    monkeypatch.setattr(cur, "_maybe_curate_upper_layers", _recorder("curate"))

    await cur.tick()
    assert ran == ["decay", "release", "alarm", "embed", "retry", "curate"]
