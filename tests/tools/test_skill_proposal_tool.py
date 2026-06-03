"""``skill_proposal`` tool — the in-chat approval gate for self-authored skills.

approve must promote the draft via skill_manage (validated + sandboxed), mark it
agent-created, and flip the proposal to approved; reject flips to rejected; show
renders the staged SKILL.md + provenance. Skills are written to a tmp dir so the
test never touches the real ~/.hermes/skills.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import thoth_db
from substrate.skill_proposals import store
from tools import skill_proposal_tool as tool


def _seed(slug, *, content=None, salience=0.8, l3_ids=None,
          eval_verdict=None, eval_reasons=None, eval_model=None):
    content = content or (
        f"---\nname: {slug}\ndescription: A test skill for {slug}\n---\n"
        f"# {slug}\n\n1. Do the thing.\n"
    )
    thoth_db.run_sync(
        store.insert_proposal(
            slug=slug,
            title=f"Title {slug}",
            draft_content=content,
            rationale="recurring need",
            source_l3_ids=l3_ids or ["11111111-1111-1111-1111-111111111111"],
            salience=salience,
            eval_verdict=eval_verdict,
            eval_reasons=eval_reasons,
            eval_model=eval_model,
        )
    )


@pytest.fixture
def _skill_sandbox(tmp_path):
    """Redirect skill writes + agent-created marking + reload to the sandbox."""
    with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]), \
         patch("tools.skill_usage.mark_agent_created") as mark, \
         patch("agent.skill_commands.reload_skills", return_value={}):
        yield tmp_path, mark


def test_list_and_show(thoth_db_initialized_sync, _skill_sandbox):
    _seed("alpha-skill", l3_ids=["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"])

    listed = json.loads(tool.skill_proposal("list"))
    assert listed["success"] and listed["pending"] == 1
    assert listed["proposals"][0]["slug"] == "alpha-skill"

    shown = json.loads(tool.skill_proposal("show", "alpha-skill"))
    assert shown["success"]
    assert shown["skill_md"].startswith("---")
    assert "salience: 0.80" in shown["provenance"]
    assert "aaaaaaaa" in shown["provenance"]   # provenance surfaces the L3 id


def test_show_unknown_returns_error(thoth_db_initialized_sync, _skill_sandbox):
    res = json.loads(tool.skill_proposal("show", "ghost"))
    assert res["success"] is False


def test_approve_installs_marks_and_flips(thoth_db_initialized_sync, _skill_sandbox):
    sandbox, mark = _skill_sandbox
    _seed("install-me")

    res = json.loads(tool.skill_proposal("approve", "install-me"))
    assert res["success"], res

    # Skill written to the sandbox (validated + security-scanned by skill_manage).
    assert (sandbox / "install-me" / "SKILL.md").exists()
    # Marked agent-created so the skills Curator adopts it.
    mark.assert_called_once_with("install-me")
    # Proposal flipped to approved with a decider stamp.
    p = thoth_db.run_sync(store.get_proposal("install-me"))
    assert p.status == "approved" and p.decided_by == "user"


def test_approve_twice_is_rejected(thoth_db_initialized_sync, _skill_sandbox):
    _seed("once-only")
    assert json.loads(tool.skill_proposal("approve", "once-only"))["success"]
    again = json.loads(tool.skill_proposal("approve", "once-only"))
    assert again["success"] is False
    assert "already approved" in again["error"]


def test_approve_failure_leaves_pending(thoth_db_initialized_sync, _skill_sandbox):
    """A malformed draft fails skill_manage validation → status stays pending
    (so it can be fixed/retried), not silently approved."""
    _seed("bad-skill", content="no frontmatter here, just text")

    res = json.loads(tool.skill_proposal("approve", "bad-skill"))
    assert res["success"] is False
    assert "Install failed" in res["error"]
    p = thoth_db.run_sync(store.get_proposal("bad-skill"))
    assert p.status == "pending"


def test_reject_flips_and_blocks_reproposal(thoth_db_initialized_sync, _skill_sandbox):
    _seed("nope-skill")
    res = json.loads(tool.skill_proposal("reject", "nope-skill"))
    assert res["success"]
    p = thoth_db.run_sync(store.get_proposal("nope-skill"))
    assert p.status == "rejected" and p.decided_by == "user"
    # has_similar now true → the SkillScout won't re-propose this slug.
    assert thoth_db.run_sync(store.has_similar("nope-skill")) is True


def test_unknown_action_and_missing_slug(thoth_db_initialized_sync, _skill_sandbox):
    assert json.loads(tool.skill_proposal("frobnicate"))["success"] is False
    assert json.loads(tool.skill_proposal("approve"))["success"] is False


# --- Phase 2: evaluator verdict surfacing ---------------------------------

def test_show_and_list_render_verdict(thoth_db_initialized_sync, _skill_sandbox):
    _seed("flagged-skill", eval_verdict="flag",
          eval_reasons=["scope creep"], eval_model="judge-x")

    shown = json.loads(tool.skill_proposal("show", "flagged-skill"))
    assert shown["evaluation"]["verdict"] == "flag"
    assert shown["evaluation"]["reasons"] == ["scope creep"]
    assert shown["evaluation"]["model"] == "judge-x"

    listed = json.loads(tool.skill_proposal("list"))
    item = next(i for i in listed["proposals"] if i["slug"] == "flagged-skill")
    assert item["eval_verdict"] == "flag"


def test_approve_flagged_installs_but_warns(thoth_db_initialized_sync, _skill_sandbox):
    """The evaluator is defense-in-depth, not a hard gate: a flagged proposal
    still installs on human approval, but the result carries a warning."""
    sandbox, _ = _skill_sandbox
    _seed("flagged-but-ok", eval_verdict="flag", eval_reasons=["minor concern"])

    res = json.loads(tool.skill_proposal("approve", "flagged-but-ok"))
    assert res["success"] is True
    assert (sandbox / "flagged-but-ok" / "SKILL.md").exists()
    assert "warning" in res and "flag" in res["warning"]


def test_show_without_verdict_omits_evaluation(thoth_db_initialized_sync, _skill_sandbox):
    _seed("plain-skill")  # no verdict
    shown = json.loads(tool.skill_proposal("show", "plain-skill"))
    assert "evaluation" not in shown
