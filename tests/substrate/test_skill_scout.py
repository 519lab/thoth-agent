"""SkillScout — drafts skills from recurring needs, gated + deduped + never installs.

Self-improvement Tier 1 (docs/plans/2026-05-28-substrate-self-improvement-forge.md).
The SkillScout must: only fire on a salient/recurring L3 need, skip needs already
covered by a skill or already proposed, stage a *pending* proposal (never install),
notify the user, respect a max-pending cap and change-gating, and survive a notify
failure. It also must remember a declined need so it doesn't re-spend the model.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.agents.skill_scout import SkillScout
from substrate.l3 import store as l3
from substrate.skill_proposals import author as author_mod
from substrate.skill_proposals import store as proposals
from substrate.skill_proposals.author import DraftedSkill


@pytest_asyncio.fixture
async def substrate(thoth_db_initialized):
    import thoth_db

    return Substrate.from_pool(thoth_db.pool())


@pytest_asyncio.fixture
async def salient_need():
    """A high-salience recurring L3 pattern — a candidate need."""
    import thoth_db

    pid, _ = await l3.upsert_pattern(
        "Greg repeatedly queries the UniFi controller for site/device status",
        "recurring_structure",
        cites=["e-unifi"],
    )
    async with thoth_db.connection() as conn:
        await conn.execute(
            "UPDATE l3_patterns SET salience_score = 0.85 WHERE id = $1", pid
        )
    return pid


def _enable(monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_SKILL_SCOUT", "1")
    monkeypatch.setenv("SKILL_SCOUT_INTERVAL_S", "0")  # isolate the change-gate
    # Default-off the Phase-2 evaluator for the Phase-1 behaviour tests so they
    # stay hermetic (no real evaluator client). Phase-2 tests set the mode +
    # stub the evaluator explicitly.
    monkeypatch.setenv("SKILL_EVALUATOR_MODE", "off")


def _stub_draft(monkeypatch, drafted):
    async def _fake(need_context, **kw):
        return drafted

    monkeypatch.setattr(author_mod, "draft_skill", _fake)


def _stub_eval(monkeypatch, verdict):
    """Patch the evaluator to return a fixed Verdict (or None)."""
    async def _fake(skill_md, need_context, **kw):
        return verdict

    monkeypatch.setattr("substrate.skill_proposals.evaluator.evaluate_skill", _fake)


def _stub_not_covered(monkeypatch):
    monkeypatch.setattr("substrate.skills_match.suggest_skills", lambda *a, **k: [])


def _capture_notify(monkeypatch):
    sent = []

    async def _fake(text):
        sent.append(text)
        return []

    monkeypatch.setattr("substrate.notify.notify_user", _fake)
    return sent


@pytest.mark.asyncio
async def test_proposes_from_salient_pattern(substrate, salient_need, monkeypatch):
    _enable(monkeypatch)
    _stub_not_covered(monkeypatch)
    sent = _capture_notify(monkeypatch)
    _stub_draft(
        monkeypatch,
        DraftedSkill(
            slug="unifi-site-query",
            title="Query UniFi sites",
            rationale="recurring manual task",
            skill_md="---\nname: unifi-site-query\ndescription: Query UniFi\n---\n# Steps\n1. ...",
        ),
    )

    await SkillScout(substrate).tick()

    p = await proposals.get_proposal("unifi-site-query")
    assert p is not None
    assert p.status == "pending"
    assert str(salient_need) in p.source_l3_ids   # precise provenance
    assert 0.84 <= p.salience <= 0.86
    assert len(sent) == 1 and "unifi-site-query" in sent[0]


@pytest.mark.asyncio
async def test_declined_draft_creates_no_proposal(substrate, salient_need, monkeypatch):
    _enable(monkeypatch)
    _stub_not_covered(monkeypatch)
    sent = _capture_notify(monkeypatch)
    _stub_draft(monkeypatch, None)  # model declined — not skill-worthy

    scout = SkillScout(substrate)
    await scout.tick()

    assert await proposals.list_proposals() == []
    assert sent == []
    # The declined need is remembered so we don't re-spend the model on it.
    assert scout._declined


@pytest.mark.asyncio
async def test_skips_need_already_covered_by_skill(substrate, salient_need, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(
        "substrate.skills_match.suggest_skills",
        lambda *a, **k: [{"name": "existing-skill", "overlap": 5}],
    )

    drafted_called = {"n": 0}

    async def _boom(need_context, **kw):
        drafted_called["n"] += 1
        raise AssertionError("draft_skill must not be called when already covered")

    monkeypatch.setattr(author_mod, "draft_skill", _boom)

    await SkillScout(substrate).tick()

    assert drafted_called["n"] == 0
    assert await proposals.list_proposals() == []


@pytest.mark.asyncio
async def test_max_pending_cap(substrate, salient_need, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("SKILL_SCOUT_MAX_PENDING", "1")
    # One pending proposal already → at the cap → tick bails before drafting.
    await proposals.insert_proposal(
        slug="already-pending", title="x", draft_content="x", salience=0.5
    )

    async def _boom(need_context, **kw):
        raise AssertionError("must not draft when at the pending cap")

    monkeypatch.setattr(author_mod, "draft_skill", _boom)
    _stub_not_covered(monkeypatch)

    await SkillScout(substrate).tick()

    assert await proposals.count_pending() == 1


@pytest.mark.asyncio
async def test_change_gating(substrate, salient_need, monkeypatch):
    """Runs once, then skips a static L3, then runs again after L3 grows."""
    monkeypatch.setenv("SKILL_SCOUT_INTERVAL_S", "0")  # isolate the watermark gate
    scout = SkillScout(substrate)

    assert await scout._should_run() is True    # first run sets the watermark
    assert await scout._should_run() is False   # no new L3 since → skip
    await l3.upsert_pattern("a brand new pattern", "theme")
    assert await scout._should_run() is True    # L3 grew → run again


@pytest.mark.asyncio
async def test_notify_failure_does_not_lose_proposal(substrate, salient_need, monkeypatch):
    _enable(monkeypatch)
    _stub_not_covered(monkeypatch)
    _stub_draft(
        monkeypatch,
        DraftedSkill(
            slug="resilient-skill",
            title="T",
            rationale="r",
            skill_md="---\nname: resilient-skill\ndescription: d\n---\n# body",
        ),
    )

    async def _raise(text):
        raise RuntimeError("gateway down")

    monkeypatch.setattr("substrate.notify.notify_user", _raise)

    # tick swallows the notify error; the proposal is still persisted.
    await SkillScout(substrate).tick()

    p = await proposals.get_proposal("resilient-skill")
    assert p is not None and p.status == "pending"


@pytest.mark.asyncio
async def test_disabled_by_default(substrate, salient_need, monkeypatch):
    monkeypatch.delenv("HERMES_SUBSTRATE_SKILL_SCOUT", raising=False)

    async def _boom(need_context, **kw):
        raise AssertionError("must not draft when the scout is disabled")

    monkeypatch.setattr(author_mod, "draft_skill", _boom)

    await SkillScout(substrate).tick()
    assert await proposals.list_proposals() == []


# ---------------------------------------------------------------------------
# Phase 2 — the frontier-model evaluator integration.
# ---------------------------------------------------------------------------

from substrate.skill_proposals.evaluator import Verdict  # noqa: E402


def _draft(slug):
    return DraftedSkill(
        slug=slug, title=f"T {slug}", rationale="recurring",
        skill_md=f"---\nname: {slug}\ndescription: d\n---\n# body\n1. step",
    )


@pytest.mark.asyncio
async def test_advisory_attaches_verdict_and_notifies(substrate, salient_need, monkeypatch):
    """Default mode is advisory: the verdict is attached + the user is still
    notified, regardless of the verdict (it never blocks)."""
    monkeypatch.setenv("HERMES_SUBSTRATE_SKILL_SCOUT", "1")
    monkeypatch.setenv("SKILL_SCOUT_INTERVAL_S", "0")
    monkeypatch.delenv("SKILL_EVALUATOR_MODE", raising=False)  # default = advisory
    _stub_not_covered(monkeypatch)
    _stub_draft(monkeypatch, _draft("advised-skill"))
    _stub_eval(monkeypatch, Verdict(verdict="flag", reasons=["a bit vague"], model="judge"))
    sent = _capture_notify(monkeypatch)

    await SkillScout(substrate).tick()

    p = await proposals.get_proposal("advised-skill")
    assert p.status == "pending"          # advisory never blocks
    assert p.eval_verdict == "flag"
    assert p.eval_reasons == ["a bit vague"]
    assert p.eval_model == "judge"
    assert len(sent) == 1 and "flag" in sent[0]  # verdict surfaced in the message


@pytest.mark.asyncio
async def test_gate_reject_auto_rejects_silently(substrate, salient_need, monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_SKILL_SCOUT", "1")
    monkeypatch.setenv("SKILL_SCOUT_INTERVAL_S", "0")
    monkeypatch.setenv("SKILL_EVALUATOR_MODE", "gate")
    _stub_not_covered(monkeypatch)
    _stub_draft(monkeypatch, _draft("danger-skill"))
    _stub_eval(monkeypatch, Verdict(verdict="reject", reasons=["destructive"], model="judge"))
    sent = _capture_notify(monkeypatch)

    scout = SkillScout(substrate)
    await scout.tick()

    p = await proposals.get_proposal("danger-skill")
    assert p is not None and p.status == "rejected"   # auto-rejected, row kept for audit
    assert p.decided_by == "evaluator"
    assert sent == []                                  # silent — no user notification
    assert scout._declined                             # won't re-pick


@pytest.mark.asyncio
async def test_gate_pass_notifies(substrate, salient_need, monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_SKILL_SCOUT", "1")
    monkeypatch.setenv("SKILL_SCOUT_INTERVAL_S", "0")
    monkeypatch.setenv("SKILL_EVALUATOR_MODE", "gate")
    _stub_not_covered(monkeypatch)
    _stub_draft(monkeypatch, _draft("clean-skill"))
    _stub_eval(monkeypatch, Verdict(verdict="pass", reasons=[], model="judge"))
    sent = _capture_notify(monkeypatch)

    await SkillScout(substrate).tick()

    p = await proposals.get_proposal("clean-skill")
    assert p.status == "pending" and p.eval_verdict == "pass"
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_evaluator_unavailable_still_proposes(substrate, salient_need, monkeypatch):
    """Evaluator returns None (no model / call failed) → Phase-1 path: proposal
    created with a null verdict and the user is still notified."""
    monkeypatch.setenv("HERMES_SUBSTRATE_SKILL_SCOUT", "1")
    monkeypatch.setenv("SKILL_SCOUT_INTERVAL_S", "0")
    monkeypatch.delenv("SKILL_EVALUATOR_MODE", raising=False)  # advisory
    _stub_not_covered(monkeypatch)
    _stub_draft(monkeypatch, _draft("unvetted-skill"))
    _stub_eval(monkeypatch, None)
    sent = _capture_notify(monkeypatch)

    await SkillScout(substrate).tick()

    p = await proposals.get_proposal("unvetted-skill")
    assert p.status == "pending" and p.eval_verdict is None
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_off_mode_skips_evaluator(substrate, salient_need, monkeypatch):
    monkeypatch.setenv("HERMES_SUBSTRATE_SKILL_SCOUT", "1")
    monkeypatch.setenv("SKILL_SCOUT_INTERVAL_S", "0")
    monkeypatch.setenv("SKILL_EVALUATOR_MODE", "off")
    _stub_not_covered(monkeypatch)
    _stub_draft(monkeypatch, _draft("unevaluated-skill"))
    sent = _capture_notify(monkeypatch)

    async def _boom(skill_md, need_context, **kw):
        raise AssertionError("evaluator must not run in off mode")

    monkeypatch.setattr("substrate.skill_proposals.evaluator.evaluate_skill", _boom)

    await SkillScout(substrate).tick()

    p = await proposals.get_proposal("unevaluated-skill")
    assert p.status == "pending" and p.eval_verdict is None
    assert len(sent) == 1
