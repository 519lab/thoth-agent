"""Tests for the ~/.thoth home-dir resolution (rename Phase 3, foundation)."""

import os
import sys
from pathlib import Path

import pytest

import thoth_constants


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point Path.home() at a clean tmp dir and clear home env vars + override."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() on POSIX uses $HOME; on Windows it uses USERPROFILE.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("THOTH_HOME", raising=False)
    # Ensure no ContextVar override is active.
    if thoth_constants.get_thoth_home_override():
        pytest.skip("a HERMES_HOME override is active in this process")
    assert Path.home() == tmp_path
    return tmp_path


# ── _disk_default_home ──────────────────────────────────────────────────────

def test_disk_default_new_install_is_thoth(fake_home):
    assert thoth_constants._disk_default_home() == fake_home / ".thoth"


def test_disk_default_legacy_only_is_hermes(fake_home):
    (fake_home / ".hermes").mkdir()
    assert thoth_constants._disk_default_home() == fake_home / ".hermes"


def test_disk_default_prefers_thoth_when_both_exist(fake_home):
    (fake_home / ".hermes").mkdir()
    (fake_home / ".thoth").mkdir()
    assert thoth_constants._disk_default_home() == fake_home / ".thoth"


@pytest.mark.skipif(sys.platform == "win32", reason="symlink perms on Windows")
def test_disk_default_thoth_symlink_to_hermes(fake_home):
    (fake_home / ".hermes").mkdir()
    os.symlink(fake_home / ".hermes", fake_home / ".thoth", target_is_directory=True)
    got = thoth_constants._disk_default_home()
    assert got == fake_home / ".thoth"
    assert got.resolve() == (fake_home / ".hermes").resolve()


# ── get_thoth_home resolution order ────────────────────────────────────────

def test_thoth_home_env_wins(fake_home, monkeypatch):
    monkeypatch.setenv("THOTH_HOME", "/tmp/custom_thoth")
    assert thoth_constants.get_thoth_home() == Path("/tmp/custom_thoth")


def test_legacy_hermes_home_env_is_ignored(fake_home, monkeypatch):
    # Phase 2 dropped the HERMES_HOME fallback: the accessor reads THOTH_HOME
    # only and falls through to the disk default when it is unset.
    monkeypatch.setenv("HERMES_HOME", "/tmp/custom_hermes")
    assert thoth_constants.get_thoth_home() == fake_home / ".thoth"


def test_thoth_home_env_wins_over_legacy(fake_home, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/tmp/legacy")
    monkeypatch.setenv("THOTH_HOME", "/tmp/canonical")
    assert thoth_constants.get_thoth_home() == Path("/tmp/canonical")


def test_get_thoth_home_disk_default_new_install(fake_home):
    assert thoth_constants.get_thoth_home() == fake_home / ".thoth"


def test_get_thoth_home_legacy_install(fake_home):
    (fake_home / ".hermes").mkdir()
    assert thoth_constants.get_thoth_home() == fake_home / ".hermes"


# ── get_default_thoth_root ─────────────────────────────────────────────────

def test_default_root_profile_mode_thoth(fake_home, monkeypatch):
    (fake_home / ".thoth").mkdir()
    monkeypatch.setenv("THOTH_HOME", str(fake_home / ".thoth" / "profiles" / "coder"))
    assert thoth_constants.get_default_thoth_root() == fake_home / ".thoth"


def test_default_root_docker_custom(fake_home, monkeypatch):
    monkeypatch.setenv("THOTH_HOME", "/opt/data")
    assert thoth_constants.get_default_thoth_root() == Path("/opt/data")


def test_default_root_docker_profile(fake_home, monkeypatch):
    monkeypatch.setenv("THOTH_HOME", "/opt/data/profiles/coder")
    assert thoth_constants.get_default_thoth_root() == Path("/opt/data")


# ── get_subprocess_home honors THOTH_HOME ───────────────────────────────────

def test_subprocess_home_uses_thoth_home(fake_home, monkeypatch):
    th = fake_home / ".thoth"
    (th / "home").mkdir(parents=True)
    monkeypatch.setenv("THOTH_HOME", str(th))
    assert thoth_constants.get_subprocess_home() == str(th / "home")


def test_main_import_user_env_over_shell_with_thoth_home(fake_home, monkeypatch):
    """User .env must override stale shell values after main import, with the
    new THOTH-aware home resolution (regression for the Phase 3a resolver)."""
    import importlib
    import sys

    home = fake_home / "h"
    home.mkdir()
    (home / ".env").write_text(
        "OPENAI_BASE_URL=https://new.example/v1\n", encoding="utf-8"
    )
    monkeypatch.setenv("THOTH_HOME", str(home))
    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")

    sys.modules.pop("thoth_cli.main", None)
    importlib.import_module("thoth_cli.main")

    assert os.getenv("OPENAI_BASE_URL") == "https://new.example/v1"


def test_load_dotenv_legacy_install_resolves_hermes_env(fake_home):
    """Regression: with NO home env vars and only ~/.hermes on disk (no
    ~/.thoth), load_thoth_dotenv must resolve the .env from ~/.hermes — not
    fall through to a non-existent ~/.thoth. Guards the disk-probe in
    get_thoth_home() (a direct THOTH_HOME-or-HERMES_HOME-or-~/.thoth shortcut
    would skip it and miss the legacy .env)."""
    from thoth_cli.env_loader import load_thoth_dotenv

    hermes = fake_home / ".hermes"
    hermes.mkdir()
    (hermes / ".env").write_text("HERMES_PHASE3_LEGACY=present\n", encoding="utf-8")
    try:
        loaded = load_thoth_dotenv()
        assert hermes / ".env" in loaded
        assert os.getenv("HERMES_PHASE3_LEGACY") == "present"
    finally:
        os.environ.pop("HERMES_PHASE3_LEGACY", None)
        os.environ.pop("THOTH_PHASE3_LEGACY", None)
