"""Tests for the skill-efficacy signal in tools/skill_usage.py (innovation #2).

All pure — the efficacy signal lives in the ``.usage.json`` sidecar, so these
tests never touch Postgres. Covers:
  - EMA math (seed + decay)
  - old-file backfill (records predating the efficacy fields auto-upgrade)
  - the single-skill attribution gate (driven from the per-turn thread-local)
  - verdict copy + dependency-light accessor
"""

import importlib
from pathlib import Path

import pytest


def _write_skill(skills_dir: Path, name: str) -> None:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n# {name}\n",
        encoding="utf-8",
    )


@pytest.fixture
def usage(tmp_path, monkeypatch):
    """Isolated THOTH_HOME with a fresh skill_usage module per test."""
    home = tmp_path / ".thoth"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("THOTH_HOME", str(home))
    import tools.skill_usage as mod
    importlib.reload(mod)
    # Reset the per-thread loaded-skills tracker so prior tests don't bleed in.
    mod.reset_skills_loaded_this_turn()
    return home, mod


# ---------------------------------------------------------------------------
# _empty_record gains the new fields
# ---------------------------------------------------------------------------

def test_empty_record_has_efficacy_fields(usage):
    _home, mod = usage
    rec = mod._empty_record()
    assert rec["efficacy_ema"] is None
    assert rec["efficacy_samples"] == 0
    assert rec["eval_verdict"] is None


# ---------------------------------------------------------------------------
# EMA math
# ---------------------------------------------------------------------------

def test_first_sample_seeds_ema_outright(usage):
    _home, mod = usage
    mod.mark_agent_created("s")
    mod.record_efficacy("s", 0.8)
    rec = mod.get_record("s")
    assert rec["efficacy_ema"] == pytest.approx(0.8)
    assert rec["efficacy_samples"] == 1


def test_second_sample_applies_ema_decay(usage):
    _home, mod = usage
    mod.mark_agent_created("s")
    mod.record_efficacy("s", 1.0, alpha=0.2)   # seed -> 1.0
    mod.record_efficacy("s", 0.0, alpha=0.2)   # 0.2*0 + 0.8*1.0 = 0.8
    rec = mod.get_record("s")
    assert rec["efficacy_ema"] == pytest.approx(0.8)
    assert rec["efficacy_samples"] == 2


def test_ema_converges_toward_repeated_score(usage):
    _home, mod = usage
    mod.mark_agent_created("s")
    mod.record_efficacy("s", 1.0)              # seed
    for _ in range(50):
        mod.record_efficacy("s", 0.0)
    rec = mod.get_record("s")
    assert rec["efficacy_ema"] == pytest.approx(0.0, abs=1e-3)
    assert rec["efficacy_samples"] == 51


def test_out_of_range_scores_clamped(usage):
    _home, mod = usage
    mod.mark_agent_created("s")
    mod.record_efficacy("s", 5.0)     # clamps to 1.0
    assert mod.get_record("s")["efficacy_ema"] == pytest.approx(1.0)
    mod.record_efficacy("s", -3.0)    # clamps to 0.0; 0.2*0 + 0.8*1 = 0.8
    assert mod.get_record("s")["efficacy_ema"] == pytest.approx(0.8)


def test_bad_alpha_falls_back_to_default(usage):
    _home, mod = usage
    mod.mark_agent_created("s")
    mod.record_efficacy("s", 1.0)
    mod.record_efficacy("s", 0.0, alpha=0.0)   # invalid -> default 0.2
    assert mod.get_record("s")["efficacy_ema"] == pytest.approx(0.8)


def test_record_efficacy_never_raises_on_garbage(usage):
    _home, mod = usage
    mod.mark_agent_created("s")
    # Non-numeric score is swallowed; no record mutation, no exception.
    mod.record_efficacy("s", "not-a-number")
    assert mod.get_record("s")["efficacy_ema"] is None


def test_bundled_skill_not_recorded(usage):
    home, mod = usage
    # A bundled skill (in .bundled_manifest) is provenance-excluded from the
    # sidecar entirely — record_efficacy is a no-op for it.
    (home / "skills" / ".bundled_manifest").write_text("bundled-x:deadbeef\n", encoding="utf-8")
    mod.record_efficacy("bundled-x", 0.9)
    assert mod.load_usage().get("bundled-x") is None


# ---------------------------------------------------------------------------
# Old-file backfill — records predating the efficacy fields auto-upgrade
# ---------------------------------------------------------------------------

def test_old_record_backfills_efficacy_fields(usage):
    _home, mod = usage
    # Simulate a sidecar written before innovation #2 — no efficacy keys.
    mod.save_usage({
        "legacy": {
            "created_by": "agent",
            "use_count": 3,
            "state": "active",
        }
    })
    rec = mod.get_record("legacy")
    assert rec["efficacy_ema"] is None
    assert rec["efficacy_samples"] == 0
    assert rec["eval_verdict"] is None


def test_record_efficacy_upgrades_old_file_in_place(usage):
    _home, mod = usage
    mod.save_usage({
        "legacy": {"created_by": "agent", "use_count": 1, "state": "active"}
    })
    mod.record_efficacy("legacy", 0.6)
    rec = mod.get_record("legacy")
    assert rec["efficacy_ema"] == pytest.approx(0.6)
    assert rec["efficacy_samples"] == 1
    # Pre-existing fields are untouched.
    assert rec["use_count"] == 1


# ---------------------------------------------------------------------------
# Single-skill attribution gate (the per-turn thread-local set)
# ---------------------------------------------------------------------------

def test_bump_use_notes_skill_loaded(usage):
    _home, mod = usage
    mod.mark_agent_created("s")
    mod.reset_skills_loaded_this_turn()
    mod.bump_use("s")
    assert mod.skills_loaded_this_turn() == {"s"}


def test_reset_clears_loaded_set(usage):
    _home, mod = usage
    mod.note_skill_loaded("a")
    assert mod.skills_loaded_this_turn() == {"a"}
    mod.reset_skills_loaded_this_turn()
    assert mod.skills_loaded_this_turn() == set()


def test_single_skill_turn_attributes_one_skill(usage):
    """Mimic the post-turn gate: attribute only when exactly one skill loaded."""
    _home, mod = usage
    mod.mark_agent_created("s")
    mod.reset_skills_loaded_this_turn()
    mod.bump_use("s")
    loaded = mod.skills_loaded_this_turn()
    assert len(loaded) == 1
    for name in loaded:
        mod.record_efficacy(name, 1.0)
    assert mod.get_record("s")["efficacy_samples"] == 1


def test_multi_skill_turn_is_skipped_by_the_gate(usage):
    """When two skills load, the len()==1 gate skips attribution (TODO: split)."""
    _home, mod = usage
    mod.mark_agent_created("a")
    mod.mark_agent_created("b")
    mod.reset_skills_loaded_this_turn()
    mod.bump_use("a")
    mod.bump_use("b")
    loaded = mod.skills_loaded_this_turn()
    assert len(loaded) == 2
    # The conversation loop gate is `if len(loaded) == 1` — so nothing is
    # attributed here. Assert neither skill got a sample.
    if len(loaded) == 1:  # pragma: no cover - documents the gate
        for name in loaded:
            mod.record_efficacy(name, 1.0)
    assert mod.get_record("a")["efficacy_ema"] is None
    assert mod.get_record("b")["efficacy_ema"] is None


# ---------------------------------------------------------------------------
# Verdict copy + accessor
# ---------------------------------------------------------------------------

def test_set_and_get_eval_verdict(usage):
    _home, mod = usage
    mod.mark_agent_created("s")
    mod.set_eval_verdict("s", "flag")
    assert mod.get_eval_verdict("s") == "flag"


def test_eval_verdict_normalises_unknown_values(usage):
    _home, mod = usage
    mod.mark_agent_created("s")
    mod.set_eval_verdict("s", "WAT")
    assert mod.get_eval_verdict("s") is None


def test_get_eval_verdict_missing_skill(usage):
    _home, mod = usage
    assert mod.get_eval_verdict("nope") is None
