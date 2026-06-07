"""Tests for `thoth hermes` — legacy Hermes Agent → Thoth importer."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

import thoth_cli.hermes_import as hi


@pytest.fixture(autouse=True)
def _no_backup(monkeypatch):
    """Skip the real pre-migration zip — not under test here, and slow."""
    import thoth_cli.backup as backup

    monkeypatch.setattr(backup, "create_pre_migration_backup", lambda *a, **k: None)


def _thoth_home(monkeypatch, path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(hi, "get_thoth_home", lambda: path)
    return path


def _args(**kw):
    base = {"source": None, "dry_run": False, "overwrite": False, "yes": True}
    base.update(kw)
    return types.SimpleNamespace(**base)


def _make_hermes(root: Path) -> Path:
    """A realistic legacy ~/.hermes: portable data + code/caches that must NOT port."""
    h = root / ".hermes"
    (h / "memories").mkdir(parents=True)
    (h / "memories" / "note.md").write_text("a memory")
    (h / "skills" / "x").mkdir(parents=True)
    (h / "skills" / "x" / "skill.md").write_text("a skill")
    (h / "config.yaml").write_text("model:\n  default: x\n")
    (h / ".env").write_text("HERMES_PG_DSN=postgres://x\n")
    (h / "SOUL.md").write_text("soul")
    # Must NOT be copied:
    (h / "hermes-agent").mkdir()
    (h / "hermes-agent" / "run_agent.py").write_text("# code")
    (h / "venv" / "bin").mkdir(parents=True)
    (h / "cache").mkdir()
    (h / "logs").mkdir()
    (h / "gateway.pid").write_text("123")
    return h


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------


def test_detect_residue_true_when_present_and_distinct(monkeypatch, tmp_path):
    _thoth_home(monkeypatch, tmp_path / "thoth")
    (tmp_path / ".hermes").mkdir()
    assert hi.detect_hermes_residue(home=tmp_path) is True


def test_detect_residue_false_when_absent(monkeypatch, tmp_path):
    _thoth_home(monkeypatch, tmp_path / "thoth")
    assert hi.detect_hermes_residue(home=tmp_path) is False


def test_detect_residue_false_when_symlink_to_thoth_home(monkeypatch, tmp_path):
    thoth = _thoth_home(monkeypatch, tmp_path / "thoth")
    link = tmp_path / ".hermes"
    link.symlink_to(thoth, target_is_directory=True)
    # ~/.hermes resolves to the active Thoth home → not residue.
    assert hi.detect_hermes_residue(home=tmp_path) is False


# --------------------------------------------------------------------------
# migrate
# --------------------------------------------------------------------------


def test_migrate_copies_allowlisted_only(monkeypatch, tmp_path, capsys):
    hermes = _make_hermes(tmp_path)
    thoth = _thoth_home(monkeypatch, tmp_path / "thoth")

    rc = hi._cmd_migrate(_args(source=str(hermes)))
    assert rc == 0

    # Portable items imported.
    assert (thoth / "config.yaml").read_text().startswith("model:")
    assert (thoth / ".env").exists()
    assert (thoth / "memories" / "note.md").read_text() == "a memory"
    assert (thoth / "skills" / "x" / "skill.md").exists()
    assert (thoth / "SOUL.md").exists()
    # Code / venv / caches / runtime state NEVER imported.
    for forbidden in ("hermes-agent", "venv", "cache", "logs", "gateway.pid"):
        assert not (thoth / forbidden).exists(), forbidden


def test_migrate_dry_run_changes_nothing(monkeypatch, tmp_path):
    hermes = _make_hermes(tmp_path)
    thoth = _thoth_home(monkeypatch, tmp_path / "thoth")

    rc = hi._cmd_migrate(_args(source=str(hermes), dry_run=True))
    assert rc == 0
    assert not (thoth / "config.yaml").exists()
    assert not (thoth / "memories").exists()


def test_migrate_skip_exists_then_overwrite(monkeypatch, tmp_path):
    hermes = _make_hermes(tmp_path)
    thoth = _thoth_home(monkeypatch, tmp_path / "thoth")
    (thoth / "config.yaml").write_text("PRE-EXISTING")

    # Default: existing item is skipped, not clobbered.
    hi._cmd_migrate(_args(source=str(hermes)))
    assert (thoth / "config.yaml").read_text() == "PRE-EXISTING"

    # --overwrite replaces it.
    hi._cmd_migrate(_args(source=str(hermes), overwrite=True))
    assert (thoth / "config.yaml").read_text().startswith("model:")


def test_migrate_refuses_same_dir(monkeypatch, tmp_path):
    home = _thoth_home(monkeypatch, tmp_path / "thoth")
    rc = hi._cmd_migrate(_args(source=str(home)))
    assert rc == 1


def test_migrate_missing_source(monkeypatch, tmp_path):
    _thoth_home(monkeypatch, tmp_path / "thoth")
    rc = hi._cmd_migrate(_args(source=str(tmp_path / "nope")))
    assert rc == 1


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------


def test_cleanup_archives(monkeypatch, tmp_path):
    hermes = _make_hermes(tmp_path)
    _thoth_home(monkeypatch, tmp_path / "thoth")

    rc = hi._cmd_cleanup(_args(source=str(hermes)))
    assert rc == 0
    assert not hermes.exists()
    assert (tmp_path / ".hermes.pre-migration").is_dir()


def test_cleanup_dry_run_keeps_dir(monkeypatch, tmp_path):
    hermes = _make_hermes(tmp_path)
    _thoth_home(monkeypatch, tmp_path / "thoth")

    rc = hi._cmd_cleanup(_args(source=str(hermes), dry_run=True))
    assert rc == 0
    assert hermes.is_dir()
    assert not (tmp_path / ".hermes.pre-migration").exists()
