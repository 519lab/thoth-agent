"""Recall L3/L4 extension — the ## Patterns and ## Self-model headers.

Added 2026-06-17: recall previously only surfaced L0 quotes + the L1 entity
header, so the substrate's higher-order abstractions (L3 patterns, L4
self-model observations) were unreachable from the foreground. These cover the
pure renderers and the recall() integration that prepends them.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.l0 import commit_slice
from substrate.l3 import store as l3_store
from substrate.l4 import store as l4_store
from substrate.recall import recall
from substrate.recall.composer import render_l3_header, render_l4_header


@pytest.fixture(autouse=True)
def _enable_mock_embeddings(monkeypatch):
    from substrate.recall import embeddings

    monkeypatch.setenv(embeddings.MOCK_ENV_VAR, "1")
    embeddings.reset_client_cache()


@pytest_asyncio.fixture
async def booted(thoth_db_initialized):
    sub = await Substrate.boot(
        config=SubstrateConfig(auto_migrate=False, start_subagents=False),
        start_subagents=False,
    )
    try:
        yield sub
    finally:
        await sub.shutdown()


async def _seed_passed_slice(substrate, text):
    stream = await substrate.streams.get_by_name("thoth.world.user_message.cli")
    await commit_slice(
        substrate, stream.stream_id, text,
        event_time_world=datetime.now(timezone.utc), born_passed=True,
    )


# ---------------------------------------------------------------------------
# Pure header renderers
# ---------------------------------------------------------------------------


def test_render_l3_header_empty():
    assert render_l3_header([]) == ""


def test_render_l3_header_all_blank_statements():
    # A list whose statements are all empty yields no header (just a count
    # line would be misleading).
    assert render_l3_header([{"kind": "theme", "statement": "  "}]) == ""


def test_render_l3_header_formats_patterns():
    out = render_l3_header([
        {"kind": "theme", "statement": "user prefers terse answers",
         "cites": ["a1b2c3", "d4e5f6"]},
        {"kind": "generalization", "statement": "PG bugs cluster on loop affinity"},
    ])
    assert "## Patterns (2)" in out
    assert "- [theme] user prefers terse answers (cites: a1b2c3, d4e5f6)" in out
    assert "- [generalization] PG bugs cluster on loop affinity" in out


def test_render_l4_header_empty():
    assert render_l4_header([]) == ""


def test_render_l4_header_formats_observations():
    out = render_l4_header([
        {"kind": "calibration", "subject": "self",
         "statement": "overestimates recall precision"},
        {"kind": "bias", "subject": "", "statement": "favours recent context"},
    ])
    assert "## Self-model (2)" in out
    assert "- [calibration] self: overestimates recall precision" in out
    # Empty subject → no "subject: " prefix.
    assert "- [bias] favours recent context" in out


# ---------------------------------------------------------------------------
# recall() integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_prepends_l3_header_when_patterns_match(booted, monkeypatch):
    import substrate.config as cfg
    monkeypatch.setattr(cfg, "RECALL_INCLUDE_L3", True)

    await l3_store.upsert_pattern(
        "the postgresql migration plan spans several phases", "generalization"
    )
    await _seed_passed_slice(booted, "we discussed the postgresql migration today")

    proj = await recall(booted, "postgresql migration plan")
    assert "## Patterns" in proj.text
    assert "postgresql migration plan" in proj.text


@pytest.mark.asyncio
async def test_recall_prepends_l4_header_when_observations_match(booted, monkeypatch):
    import substrate.config as cfg
    monkeypatch.setattr(cfg, "RECALL_INCLUDE_L4", True)

    await l4_store.record_observation(
        "calibration", "self", "tends to overestimate postgresql migration risk"
    )
    await _seed_passed_slice(booted, "we discussed the postgresql migration today")

    proj = await recall(booted, "postgresql migration risk")
    assert "## Self-model" in proj.text
    assert "overestimate postgresql migration risk" in proj.text


@pytest.mark.asyncio
async def test_recall_no_l3l4_headers_when_disabled(booted, monkeypatch):
    import substrate.config as cfg
    monkeypatch.setattr(cfg, "RECALL_INCLUDE_L3", False)
    monkeypatch.setattr(cfg, "RECALL_INCLUDE_L4", False)

    await l3_store.upsert_pattern("postgresql migration is multi-phase", "theme")
    await l4_store.record_observation(
        "calibration", "self", "overestimates postgresql migration risk"
    )
    await _seed_passed_slice(booted, "postgresql migration discussion")

    proj = await recall(booted, "postgresql migration")
    assert "## Patterns" not in proj.text
    assert "## Self-model" not in proj.text


@pytest.mark.asyncio
async def test_recall_l4_header_excludes_coherence(booted, monkeypatch):
    # The coherence vital sign must never leak into the self-model header —
    # it's the recall relevance-floor pin, surfaced separately.
    import substrate.config as cfg
    monkeypatch.setattr(cfg, "RECALL_INCLUDE_L4", True)

    await l4_store.upsert_coherence(
        "coherence steady for the postgresql migration work", score=0.9
    )
    await _seed_passed_slice(booted, "postgresql migration discussion")

    proj = await recall(booted, "postgresql migration")
    # Coherence observation is excluded by kind, so no self-model header from it.
    assert "## Self-model" not in proj.text
