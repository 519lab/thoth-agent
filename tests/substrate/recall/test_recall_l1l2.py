"""Phase D recall extension — the ## Known entities header (spec §7)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.config import SubstrateConfig
from substrate.l0 import commit_slice
from substrate.l1 import store
from substrate.recall import recall
from substrate.recall.composer import render_l1_header


@pytest.fixture(autouse=True)
def _enable_mock_embeddings(monkeypatch):
    from substrate.recall import embeddings

    monkeypatch.setenv(embeddings.MOCK_ENV_VAR, "1")
    embeddings.reset_client_cache()


@pytest_asyncio.fixture
async def booted(hermes_db_initialized):
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
        substrate, stream.stream_id, text, event_time_world=datetime.now(timezone.utc),
        born_passed=True,
    )


# ---------------------------------------------------------------------------
# Pure header renderer
# ---------------------------------------------------------------------------


def test_render_l1_header_empty():
    assert render_l1_header([]) == ""


def test_render_l1_header_formats_entities():
    out = render_l1_header([
        {"name": "Greg", "entity_type": "person", "summary": "maintainer",
         "cites": ["a1b2c3", "d4e5f6"]},
        {"name": "Hermes", "entity_type": "project", "summary": "", "cites": []},
    ])
    assert "## Known entities (2)" in out
    assert "- Greg (person) — maintainer (cites: a1b2c3, d4e5f6)" in out
    assert "- Hermes (project)" in out


# ---------------------------------------------------------------------------
# recall() integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_prepends_l1_header_when_entities_match(booted, monkeypatch):
    monkeypatch.setenv("RECALL_INCLUDE_L1", "1")
    import substrate.config as cfg
    monkeypatch.setattr(cfg, "RECALL_INCLUDE_L1", True)

    await store.upsert_entity(
        "PostgreSQL migration", "concept", summary="moving Hermes to PG"
    )
    await _seed_passed_slice(booted, "we discussed the postgresql migration today")

    proj = await recall(booted, "postgresql migration")
    assert "## Known entities" in proj.text
    assert "PostgreSQL migration" in proj.text


@pytest.mark.asyncio
async def test_recall_no_header_when_disabled(booted, monkeypatch):
    monkeypatch.setenv("RECALL_INCLUDE_L1", "0")
    import substrate.config as cfg
    monkeypatch.setattr(cfg, "RECALL_INCLUDE_L1", False)

    await store.upsert_entity("PostgreSQL migration", "concept", summary="pg")
    await _seed_passed_slice(booted, "we discussed the postgresql migration today")

    proj = await recall(booted, "postgresql migration")
    assert "## Known entities" not in proj.text


@pytest.mark.asyncio
async def test_recall_no_header_when_no_entities(booted, monkeypatch):
    monkeypatch.setenv("RECALL_INCLUDE_L1", "1")
    import substrate.config as cfg
    monkeypatch.setattr(cfg, "RECALL_INCLUDE_L1", True)

    # L1 empty; recall over a seeded slice still works, just no header.
    await _seed_passed_slice(booted, "plain message with no entities stored")
    proj = await recall(booted, "plain message")
    assert "## Known entities" not in proj.text
