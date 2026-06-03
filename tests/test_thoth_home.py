"""Tests for the ~/.thoth home-dir resolution (rename Phase 3, foundation)."""

import os
import sys
from pathlib import Path

import pytest

import thoth_constants
import thoth_env


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point Path.home() at a clean tmp dir and clear home env vars + override."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() on POSIX uses $HOME; on Windows it uses USERPROFILE.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("THOTH_HOME", raising=False)
    # Ensure no ContextVar override is active.
    if thoth_constants.get_hermes_home_override():
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


def test_hermes_home_env_fallback(fake_home, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/tmp/custom_hermes")
    assert thoth_constants.get_thoth_home() == Path("/tmp/custom_hermes")


def test_thoth_home_beats_hermes_home(fake_home, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/tmp/legacy")
    monkeypatch.setenv("THOTH_HOME", "/tmp/canonical")
    assert thoth_constants.get_thoth_home() == Path("/tmp/canonical")


def test_get_hermes_home_disk_default_new_install(fake_home):
    assert thoth_constants.get_thoth_home() == fake_home / ".thoth"


def test_get_hermes_home_legacy_install(fake_home):
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


# ── normalize_thoth_home_env + propagate helpers ────────────────────────────

def test_normalize_home_only_hermes():
    env = {"HERMES_HOME": "/h"}
    thoth_env.normalize_thoth_home_env(env)
    assert env == {"HERMES_HOME": "/h", "THOTH_HOME": "/h"}


def test_normalize_home_only_thoth():
    env = {"THOTH_HOME": "/t"}
    thoth_env.normalize_thoth_home_env(env)
    assert env == {"HERMES_HOME": "/t", "THOTH_HOME": "/t"}


def test_normalize_home_thoth_wins():
    env = {"HERMES_HOME": "/old", "THOTH_HOME": "/new"}
    thoth_env.normalize_thoth_home_env(env)
    assert env["HERMES_HOME"] == "/new" and env["THOTH_HOME"] == "/new"


def test_normalize_home_empty_thoth_guard():
    env = {"HERMES_HOME": "/real", "THOTH_HOME": ""}
    thoth_env.normalize_thoth_home_env(env)
    assert env["HERMES_HOME"] == "/real" and env["THOTH_HOME"] == "/real"


def test_normalize_home_idempotent():
    env = {"THOTH_HOME": "/t"}
    thoth_env.normalize_thoth_home_env(env)
    assert thoth_env.normalize_thoth_home_env(env) == 0


def test_propagate_home_sets_both():
    env = {"PATH": "/x"}
    thoth_env.propagate_thoth_home_into(env, "/profile")
    assert env["HERMES_HOME"] == "/profile" and env["THOTH_HOME"] == "/profile"


def test_propagate_home_copy_does_not_mutate_base():
    base = {"HERMES_HOME": "/parent", "THOTH_HOME": "/parent"}
    child = thoth_env.propagate_thoth_home(base, "/child")
    assert child["HERMES_HOME"] == "/child" and child["THOTH_HOME"] == "/child"
    assert base["HERMES_HOME"] == "/parent"  # base untouched


def test_main_import_user_env_over_shell_with_hermes_home(fake_home, monkeypatch):
    """User .env must override stale shell values after main import, with the
    new THOTH-aware home resolution (regression for the Phase 3a resolver)."""
    import importlib
    import sys

    home = fake_home / "h"
    home.mkdir()
    (home / ".env").write_text(
        "OPENAI_BASE_URL=https://new.example/v1\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("THOTH_HOME", raising=False)
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
