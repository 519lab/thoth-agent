"""Skill suggestion (#6) — match context to bundled skills + CLI + recall."""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout

import pytest
import pytest_asyncio

from substrate import skills_match
from substrate.cli import inspect as inspect_mod


@pytest.fixture
def fake_skills(tmp_path, monkeypatch):
    """A tiny bundled-skills tree so matching is deterministic + offline."""
    def _skill(cat, name, desc, tags):
        d = tmp_path / cat / name
        d.mkdir(parents=True)
        tagline = ", ".join(tags)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n"
            f"metadata:\n  hermes:\n    tags: [{tagline}]\n---\nbody\n",
            encoding="utf-8",
        )

    _skill("devops", "kubernetes-deploy", "Deploy and roll back Kubernetes workloads", ["kubernetes", "deploy"])
    _skill("data", "postgres-tuning", "Tune PostgreSQL query performance and indexes", ["postgres", "sql"])
    _skill("creative", "haiku-writer", "Compose haiku poetry", ["poetry"])
    monkeypatch.setenv("THOTH_SKILLS_ROOT", str(tmp_path))
    skills_match.scan_skills.cache_clear()
    yield str(tmp_path)
    skills_match.scan_skills.cache_clear()


def test_scan_skills_parses_frontmatter(fake_skills):
    cat = skills_match.scan_skills(fake_skills)
    names = {s["name"] for s in cat}
    assert names == {"kubernetes-deploy", "postgres-tuning", "haiku-writer"}


def test_suggest_ranks_by_overlap(fake_skills):
    hits = skills_match.suggest_skills(
        "help me deploy a kubernetes service", root=fake_skills, min_overlap=1
    )
    assert hits and hits[0]["name"] == "kubernetes-deploy"
    assert all(h["name"] != "haiku-writer" for h in hits)  # irrelevant excluded


def test_suggest_empty_on_no_match(fake_skills):
    assert skills_match.suggest_skills("xyzzy nothing relevant", root=fake_skills) == []


def test_suggest_empty_root_is_safe(tmp_path):
    # Non-existent / empty root → no crash, empty result.
    assert skills_match.suggest_skills("anything", root=str(tmp_path / "missing")) == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_register_subparser_skills():
    parser = argparse.ArgumentParser(prog="thoth")
    sub = parser.add_subparsers(dest="command")
    inspect_mod.register_subparser(sub)
    ns = parser.parse_args(["substrate", "skills", "kubernetes", "deploy"])
    assert ns.query == ["kubernetes", "deploy"] and callable(ns.func)


def test_cli_skills_lists_matches(fake_skills):
    args = argparse.Namespace(query=["postgres", "performance"], limit=5)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = inspect_mod._cmd_inspect_skills(args)
    assert rc == 0
    assert "postgres-tuning" in buf.getvalue()


def test_cli_skills_no_match(fake_skills):
    args = argparse.Namespace(query=["zzzznothing"], limit=5)
    buf = io.StringIO()
    with redirect_stdout(buf):
        inspect_mod._cmd_inspect_skills(args)
    assert "no bundled skill matches" in buf.getvalue()


# ---------------------------------------------------------------------------
# recall opt-in section
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_embeddings(monkeypatch):
    from substrate.recall import embeddings

    monkeypatch.setenv(embeddings.MOCK_ENV_VAR, "1")
    embeddings.reset_client_cache()


@pytest_asyncio.fixture
async def booted(thoth_db_initialized):
    from substrate import Substrate
    from substrate.config import SubstrateConfig

    sub = await Substrate.boot(
        config=SubstrateConfig(auto_migrate=False, start_subagents=False),
        start_subagents=False,
    )
    try:
        yield sub
    finally:
        await sub.shutdown()


@pytest.mark.asyncio
async def test_recall_appends_skills_when_enabled(booted, fake_skills, monkeypatch):
    import substrate.config as cfg
    from datetime import datetime, timezone
    from substrate.l0 import commit_slice
    from substrate.recall import recall

    monkeypatch.setattr(cfg, "RECALL_INCLUDE_L1", False)
    monkeypatch.setattr(cfg, "RECALL_SUGGEST_SKILLS", True)

    stream = await booted.streams.get_by_name("thoth.world.user_message.cli")
    await commit_slice(
        booted, stream.stream_id, "we need to deploy the kubernetes cluster",
        event_time_world=datetime.now(timezone.utc), born_passed=True,
    )
    proj = await recall(booted, "kubernetes deploy")
    assert "## Relevant skills" in proj.text
    assert "kubernetes-deploy" in proj.text


@pytest.mark.asyncio
async def test_recall_no_skills_when_disabled(booted, fake_skills, monkeypatch):
    import substrate.config as cfg
    from datetime import datetime, timezone
    from substrate.l0 import commit_slice
    from substrate.recall import recall

    monkeypatch.setattr(cfg, "RECALL_INCLUDE_L1", False)
    monkeypatch.setattr(cfg, "RECALL_SUGGEST_SKILLS", False)

    stream = await booted.streams.get_by_name("thoth.world.user_message.cli")
    await commit_slice(
        booted, stream.stream_id, "deploy kubernetes",
        event_time_world=datetime.now(timezone.utc), born_passed=True,
    )
    proj = await recall(booted, "kubernetes deploy")
    assert "## Relevant skills" not in proj.text
