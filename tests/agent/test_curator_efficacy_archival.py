"""Efficacy-based archival in agent/curator.py (innovation #2).

Pure: ``apply_automatic_transitions`` reads the ``.usage.json`` sidecar and
walks agent-created skills with no DB. We mark skills ``created_by=agent`` so
they're curator-managed, place them in time, and assert the efficacy path
archives a chronically-failing-but-recently-used skill — while the inactivity
fallback still works and the flag defaults OFF (report-only).
"""

import importlib
from datetime import datetime, timedelta, timezone
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
def curator_env(tmp_path, monkeypatch):
    home = tmp_path / ".thoth"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("THOTH_HOME", str(home))
    # Ensure the efficacy flag isn't leaked in from the ambient env.
    monkeypatch.delenv("THOTH_SKILL_EFFICACY_ENABLED", raising=False)

    import tools.skill_usage as skill_usage
    import agent.curator as curator
    importlib.reload(skill_usage)
    importlib.reload(curator)

    # Pin the time windows so the test is deterministic regardless of config.
    monkeypatch.setattr(curator, "get_stale_after_days", lambda: 30)
    monkeypatch.setattr(curator, "get_archive_after_days", lambda: 90)
    return home, skill_usage, curator


_NOW = datetime(2026, 6, 24, tzinfo=timezone.utc)


def _mature_low_efficacy_record(now=_NOW):
    """A skill old + active enough to be 'mature', recently used, low EMA."""
    return {
        "created_by": "agent",
        "created_at": (now - timedelta(days=60)).isoformat(),
        "last_used_at": (now - timedelta(days=1)).isoformat(),  # recent! not idle
        "use_count": 10,
        "view_count": 0,
        "patch_count": 0,
        "state": "active",
        "pinned": False,
        "efficacy_ema": 0.1,
        "efficacy_samples": 8,
    }


def test_low_efficacy_mature_skill_archived_when_enabled(curator_env, monkeypatch):
    home, skill_usage, curator = curator_env
    _write_skill(home / "skills", "flaky")
    skill_usage.save_usage({"flaky": _mature_low_efficacy_record()})

    monkeypatch.setattr(curator, "efficacy_archival_enabled", lambda: True)
    monkeypatch.setattr(curator, "get_efficacy_floor", lambda: 0.35)
    monkeypatch.setattr(curator, "get_efficacy_min_samples", lambda: 5)

    counts = curator.apply_automatic_transitions(now=_NOW)

    assert counts["archived"] == 1
    assert counts["archived_low_efficacy"] == 1
    assert skill_usage.get_record("flaky")["state"] == "archived"


def test_efficacy_archival_off_by_default(curator_env):
    """Flag defaults OFF — a low-efficacy but recently-used skill survives."""
    home, skill_usage, curator = curator_env
    _write_skill(home / "skills", "flaky")
    skill_usage.save_usage({"flaky": _mature_low_efficacy_record()})

    # No flag set, no config -> efficacy_archival_enabled() is False.
    assert curator.efficacy_archival_enabled() is False
    counts = curator.apply_automatic_transitions(now=_NOW)

    assert counts.get("archived_low_efficacy", 0) == 0
    # Recently used, so the inactivity path leaves it active too.
    assert skill_usage.get_record("flaky")["state"] == "active"


def test_high_efficacy_skill_not_archived(curator_env, monkeypatch):
    home, skill_usage, curator = curator_env
    _write_skill(home / "skills", "good")
    rec = _mature_low_efficacy_record()
    rec["efficacy_ema"] = 0.9
    skill_usage.save_usage({"good": rec})

    monkeypatch.setattr(curator, "efficacy_archival_enabled", lambda: True)
    monkeypatch.setattr(curator, "get_efficacy_floor", lambda: 0.35)
    monkeypatch.setattr(curator, "get_efficacy_min_samples", lambda: 5)

    counts = curator.apply_automatic_transitions(now=_NOW)
    assert counts.get("archived_low_efficacy", 0) == 0
    assert skill_usage.get_record("good")["state"] == "active"


def test_too_few_samples_not_archived(curator_env, monkeypatch):
    """Low EMA but below the confidence gate -> not archived on efficacy path."""
    home, skill_usage, curator = curator_env
    _write_skill(home / "skills", "unsure")
    rec = _mature_low_efficacy_record()
    rec["efficacy_samples"] = 2  # below min_samples=5
    skill_usage.save_usage({"unsure": rec})

    monkeypatch.setattr(curator, "efficacy_archival_enabled", lambda: True)
    monkeypatch.setattr(curator, "get_efficacy_floor", lambda: 0.35)
    monkeypatch.setattr(curator, "get_efficacy_min_samples", lambda: 5)

    counts = curator.apply_automatic_transitions(now=_NOW)
    assert counts.get("archived_low_efficacy", 0) == 0
    assert skill_usage.get_record("unsure")["state"] == "active"


def test_no_efficacy_signal_not_archived(curator_env, monkeypatch):
    """A mature skill with no EMA yet is never archived on the efficacy path."""
    home, skill_usage, curator = curator_env
    _write_skill(home / "skills", "untracked")
    rec = _mature_low_efficacy_record()
    rec["efficacy_ema"] = None
    rec["efficacy_samples"] = 0
    skill_usage.save_usage({"untracked": rec})

    monkeypatch.setattr(curator, "efficacy_archival_enabled", lambda: True)
    counts = curator.apply_automatic_transitions(now=_NOW)
    assert counts.get("archived_low_efficacy", 0) == 0
    assert skill_usage.get_record("untracked")["state"] == "active"


def test_young_low_efficacy_skill_not_archived(curator_env, monkeypatch):
    """Recently-created skill: EMA is a cold-start artifact, don't archive."""
    home, skill_usage, curator = curator_env
    _write_skill(home / "skills", "newbie")
    rec = _mature_low_efficacy_record()
    rec["created_at"] = (_NOW - timedelta(days=2)).isoformat()  # younger than stale window
    skill_usage.save_usage({"newbie": rec})

    monkeypatch.setattr(curator, "efficacy_archival_enabled", lambda: True)
    monkeypatch.setattr(curator, "get_efficacy_floor", lambda: 0.35)
    monkeypatch.setattr(curator, "get_efficacy_min_samples", lambda: 5)

    counts = curator.apply_automatic_transitions(now=_NOW)
    assert counts.get("archived_low_efficacy", 0) == 0
    assert skill_usage.get_record("newbie")["state"] == "active"


def test_pinned_low_efficacy_skill_never_archived(curator_env, monkeypatch):
    home, skill_usage, curator = curator_env
    _write_skill(home / "skills", "pinned-flaky")
    rec = _mature_low_efficacy_record()
    rec["pinned"] = True
    skill_usage.save_usage({"pinned-flaky": rec})

    monkeypatch.setattr(curator, "efficacy_archival_enabled", lambda: True)
    counts = curator.apply_automatic_transitions(now=_NOW)
    assert counts.get("archived_low_efficacy", 0) == 0
    assert skill_usage.get_record("pinned-flaky")["state"] == "active"


def test_inactivity_path_still_archives_as_fallback(curator_env, monkeypatch):
    """With efficacy enabled, a genuinely-idle good skill still archives via
    the inactivity fallback (efficacy path declines, inactivity catches it)."""
    home, skill_usage, curator = curator_env
    _write_skill(home / "skills", "idle-good")
    rec = _mature_low_efficacy_record()
    rec["efficacy_ema"] = 0.95  # good — efficacy path won't touch it
    rec["last_used_at"] = (_NOW - timedelta(days=120)).isoformat()  # idle > archive window
    skill_usage.save_usage({"idle-good": rec})

    monkeypatch.setattr(curator, "efficacy_archival_enabled", lambda: True)
    monkeypatch.setattr(curator, "get_efficacy_floor", lambda: 0.35)
    monkeypatch.setattr(curator, "get_efficacy_min_samples", lambda: 5)

    counts = curator.apply_automatic_transitions(now=_NOW)
    assert counts["archived"] == 1
    assert counts.get("archived_low_efficacy", 0) == 0  # archived via inactivity, not efficacy
    assert skill_usage.get_record("idle-good")["state"] == "archived"


def test_efficacy_flag_env_truthy(curator_env, monkeypatch):
    _home, _su, curator = curator_env
    monkeypatch.setenv("THOTH_SKILL_EFFICACY_ENABLED", "1")
    assert curator.efficacy_archival_enabled() is True
    monkeypatch.setenv("THOTH_SKILL_EFFICACY_ENABLED", "off")
    assert curator.efficacy_archival_enabled() is False
