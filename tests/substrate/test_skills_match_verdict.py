"""Verdict-weighted ranking in substrate/skills_match.py (innovation #2).

Pure: ``suggest_skills`` scans a bundled-skills root (filesystem) and joins the
evaluator verdict by name from the ``.usage.json`` sidecar (a single sidecar
read, no DB). We build a temp skills root + temp THOTH_HOME and assert:
  - a ``reject`` verdict excludes a skill from suggestions
  - a ``flag`` verdict down-weights it below an equal-overlap ``pass`` peer
  - skills with no verdict rank at full weight
  - the min_overlap gate still applies to RAW overlap
"""

import importlib
from pathlib import Path

import pytest


def _write_skill(root: Path, name: str, description: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


@pytest.fixture
def match_env(tmp_path, monkeypatch):
    """Temp bundled-skills root + temp THOTH_HOME for the verdict sidecar."""
    skills_root = tmp_path / "bundled_skills"
    skills_root.mkdir()
    home = tmp_path / ".thoth"
    (home / "skills").mkdir(parents=True)

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("THOTH_HOME", str(home))
    monkeypatch.setenv("THOTH_SKILLS_ROOT", str(skills_root))

    import tools.skill_usage as skill_usage
    import substrate.skills_match as skills_match
    importlib.reload(skill_usage)
    importlib.reload(skills_match)
    # The skills_match scan is lru_cached per root — clear it so each test's
    # freshly-written skills are picked up.
    skills_match.scan_skills.cache_clear()
    return skills_root, skill_usage, skills_match


_CONTEXT = "deploy kubernetes cluster rollout"


def test_reject_verdict_excludes_skill(match_env):
    root, skill_usage, skills_match = match_env
    _write_skill(root, "deploy-kube", "deploy kubernetes cluster rollout helper")
    skill_usage.save_usage({"deploy-kube": {"created_by": "agent", "eval_verdict": "reject"}})
    skills_match.scan_skills.cache_clear()

    hits = skills_match.suggest_skills(_CONTEXT, root=str(root), limit=5, min_overlap=2)
    names = {h["name"] for h in hits}
    assert "deploy-kube" not in names


def test_pass_and_unrecorded_rank_full_weight(match_env):
    root, skill_usage, skills_match = match_env
    _write_skill(root, "passed", "deploy kubernetes cluster rollout helper")
    _write_skill(root, "unrecorded", "deploy kubernetes cluster rollout helper")
    skill_usage.save_usage({"passed": {"created_by": "agent", "eval_verdict": "pass"}})
    skills_match.scan_skills.cache_clear()

    hits = skills_match.suggest_skills(_CONTEXT, root=str(root), limit=5, min_overlap=2)
    names = {h["name"] for h in hits}
    assert "passed" in names
    assert "unrecorded" in names


def test_flag_verdict_downweights_below_pass_peer(match_env):
    """Two skills with identical overlap; the flagged one ranks below the
    passing one once verdict weighting is applied."""
    root, skill_usage, skills_match = match_env
    # Identical descriptions -> identical raw token overlap with the context.
    _write_skill(root, "alpha-pass", "deploy kubernetes cluster rollout")
    _write_skill(root, "zeta-flag", "deploy kubernetes cluster rollout")
    skill_usage.save_usage({
        "alpha-pass": {"created_by": "agent", "eval_verdict": "pass"},
        "zeta-flag": {"created_by": "agent", "eval_verdict": "flag"},
    })
    skills_match.scan_skills.cache_clear()

    hits = skills_match.suggest_skills(_CONTEXT, root=str(root), limit=2, min_overlap=2)
    names = [h["name"] for h in hits]
    assert names[0] == "alpha-pass"
    assert names[1] == "zeta-flag"
    # Both share the same raw overlap; weighting only reorders.
    assert hits[0]["overlap"] == hits[1]["overlap"]


def test_flag_can_drop_below_limit_against_passing_peers(match_env):
    """With limit=1, a flagged skill loses the single slot to a passing peer
    of equal raw overlap."""
    root, skill_usage, skills_match = match_env
    _write_skill(root, "alpha-pass", "deploy kubernetes cluster rollout")
    _write_skill(root, "zeta-flag", "deploy kubernetes cluster rollout")
    skill_usage.save_usage({
        "alpha-pass": {"created_by": "agent", "eval_verdict": "pass"},
        "zeta-flag": {"created_by": "agent", "eval_verdict": "flag"},
    })
    skills_match.scan_skills.cache_clear()

    hits = skills_match.suggest_skills(_CONTEXT, root=str(root), limit=1, min_overlap=2)
    assert [h["name"] for h in hits] == ["alpha-pass"]


def test_min_overlap_gate_on_raw_overlap(match_env):
    """A skill below min_overlap is excluded even with a pass verdict —
    weighting never promotes something that didn't clear the raw bar."""
    root, skill_usage, skills_match = match_env
    # Only one shared token ("deploy") -> overlap 1, below min_overlap=2.
    _write_skill(root, "thin", "deploy widgets quickly")
    skill_usage.save_usage({"thin": {"created_by": "agent", "eval_verdict": "pass"}})
    skills_match.scan_skills.cache_clear()

    hits = skills_match.suggest_skills(_CONTEXT, root=str(root), limit=5, min_overlap=2)
    assert "thin" not in {h["name"] for h in hits}


def test_verdict_factor_helper(match_env):
    _root, skill_usage, skills_match = match_env
    skill_usage.save_usage({
        "p": {"created_by": "agent", "eval_verdict": "pass"},
        "f": {"created_by": "agent", "eval_verdict": "flag"},
        "r": {"created_by": "agent", "eval_verdict": "reject"},
    })
    assert skills_match._verdict_factor("p") == pytest.approx(1.0)
    assert skills_match._verdict_factor("f") == pytest.approx(0.7)
    assert skills_match._verdict_factor("r") == pytest.approx(0.0)
    assert skills_match._verdict_factor("missing") == pytest.approx(1.0)
