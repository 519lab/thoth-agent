"""Tests for ``hermes substrate recall validate`` — the go/no-go probe.

The probe runs a real ``recall()`` against the current L0 and prints the
composed <memory-context> block plus a READY/DEGRADED/NOT READY verdict.
It's the repeatable form of Phase C's deferred manual smoke test.
"""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.l0 import commit_slice
from substrate.recall import cli_inspect
from substrate.cli import inspect as inspect_mod


@pytest.fixture(autouse=True)
def _enable_mock_embeddings(monkeypatch):
    from substrate.recall import embeddings

    monkeypatch.setenv(embeddings.MOCK_ENV_VAR, "1")
    embeddings.reset_client_cache()


@pytest_asyncio.fixture
async def booted_substrate(hermes_db_initialized):
    sub = await Substrate.boot(
        config=SubstrateConfig(auto_migrate=False, start_subagents=False),
        start_subagents=False,
    )
    try:
        yield sub
    finally:
        await sub.shutdown()


async def _seed_passed_slice(substrate, *, text: str) -> None:
    import hermes_db

    stream = await substrate.streams.get_by_name("thoth.world.user_message.cli")
    await commit_slice(
        substrate, stream.stream_id, text, event_time_world=datetime.now(timezone.utc)
    )
    async with hermes_db.connection() as conn:
        await conn.execute(
            """
            UPDATE substrate_slices
               SET sentinel_state = 'passed', trust_score = 0.95,
                   pending_committed_at = NULL
             WHERE sentinel_state = 'pending'
            """
        )


def test_register_subparser_recall_validate():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    inspect_mod.register_subparser(sub)
    ns = parser.parse_args(
        ["substrate", "recall", "validate", "--query", "x", "--token-budget", "100"]
    )
    assert ns.query == "x"
    assert ns.token_budget == 100
    assert callable(ns.func)


@pytest.mark.asyncio
async def test_validate_not_ready_when_empty(booted_substrate):
    """No perception in the window → NOT READY verdict."""
    import hermes_db

    buf = io.StringIO()
    async with hermes_db.connection() as conn:
        with redirect_stdout(buf):
            await cli_inspect.validate(conn, query="anything")
    out = buf.getvalue()
    assert "Recall validation @" in out
    assert "Verdict: NOT READY" in out
    assert "No perception in the recall window" in out


@pytest.mark.asyncio
async def test_validate_composes_block_with_slices(booted_substrate):
    """With passed slices, the probe composes a real block. Coverage is 0%
    here (seeded slices carry no embedding), so the verdict is DEGRADED —
    recall works via the keyword path — and the composed block is shown."""
    import hermes_db

    for text in (
        "the moon landing was in 1969",
        "apollo 11 carried three astronauts",
        "neil armstrong stepped onto the surface",
    ):
        await _seed_passed_slice(booted_substrate, text=text)

    buf = io.StringIO()
    async with hermes_db.connection() as conn:
        with redirect_stdout(buf):
            await cli_inspect.validate(conn, query="moon landing apollo")
    out = buf.getvalue()
    assert "Composed <memory-context> block:" in out
    assert "Verdict:" in out
    # A block composed → not NOT READY; coverage 0% → DEGRADED.
    assert "Verdict: DEGRADED" in out
    assert "recall composed a" in out
    # The probe surfaced at least one seeded slice's content.
    assert "moon landing" in out or "apollo" in out


def test_validate_verdict_pure():
    """The verdict helper, exercised without a DB."""

    class _Proj:
        def __init__(self, text, composed=(), empty_reason=None, tokens_used=0):
            self.text = text
            self.composed = list(composed)
            self.empty_reason = empty_reason
            self.tokens_used = tokens_used

    # No slices at all → NOT READY.
    v, _ = cli_inspect._validate_verdict(
        enabled="1", total_slices=0, reachable=0, coverage=0.0, proj=_Proj("")
    )
    assert v == "NOT READY"

    # Composed + good coverage → READY.
    v, _ = cli_inspect._validate_verdict(
        enabled="1", total_slices=10, reachable=5, coverage=95.0,
        proj=_Proj("block", composed=[1], tokens_used=12),
    )
    assert v == "READY"

    # Composed but low coverage → DEGRADED.
    v, notes = cli_inspect._validate_verdict(
        enabled="1", total_slices=10, reachable=5, coverage=10.0,
        proj=_Proj("block", composed=[1], tokens_used=12),
    )
    assert v == "DEGRADED"
    assert any("coverage is low" in n for n in notes)

    # Disabled → note about the env var.
    _, notes = cli_inspect._validate_verdict(
        enabled="0", total_slices=10, reachable=5, coverage=95.0,
        proj=_Proj("block", composed=[1]),
    )
    assert any("HERMES_SUBSTRATE_RECALL=0" in n for n in notes)
