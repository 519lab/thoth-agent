"""Tests for the hermes→thoth env-var compatibility bridge (rename Phase 2)."""

import os
import subprocess
import sys
import textwrap

import hermes_env


# ── normalize_thoth_env: pure-dict unit tests (no os.environ pollution) ──

def test_only_hermes_mirrors_to_thoth():
    env = {"HERMES_PG_DSN": "postgres://x"}
    hermes_env.normalize_thoth_env(env)
    assert env["THOTH_PG_DSN"] == "postgres://x"
    assert env["HERMES_PG_DSN"] == "postgres://x"


def test_only_thoth_mirrors_to_hermes():
    env = {"THOTH_PG_DSN": "postgres://y"}
    hermes_env.normalize_thoth_env(env)
    assert env["HERMES_PG_DSN"] == "postgres://y"
    assert env["THOTH_PG_DSN"] == "postgres://y"


def test_both_set_thoth_wins():
    env = {"HERMES_API_KEY": "old", "THOTH_API_KEY": "new"}
    hermes_env.normalize_thoth_env(env)
    assert env["HERMES_API_KEY"] == "new"
    assert env["THOTH_API_KEY"] == "new"


def test_both_equal_is_noop():
    env = {"HERMES_X": "v", "THOTH_X": "v"}
    assert hermes_env.normalize_thoth_env(env) == 0


def test_empty_thoth_does_not_clobber_nonempty_hermes():
    env = {"HERMES_X": "real", "THOTH_X": ""}
    hermes_env.normalize_thoth_env(env)
    assert env["HERMES_X"] == "real"
    assert env["THOTH_X"] == "real"


def test_underscore_private_sentinels_skipped():
    env = {"_HERMES_GATEWAY": "1"}
    hermes_env.normalize_thoth_env(env)
    assert "_THOTH_GATEWAY" not in env
    assert "THOTH__GATEWAY" not in env


def test_home_vars_deferred_to_phase3():
    # HERMES_HOME / HERMES_HOME_MODE are owned by Phase 3; not mirrored here.
    env = {"HERMES_HOME": "/h", "HERMES_HOME_MODE": "0700"}
    hermes_env.normalize_thoth_env(env)
    assert "THOTH_HOME" not in env
    assert "THOTH_HOME_MODE" not in env


def test_idempotent():
    env = {"HERMES_X": "v"}
    hermes_env.normalize_thoth_env(env)
    assert hermes_env.normalize_thoth_env(env) == 0
    assert env == {"HERMES_X": "v", "THOTH_X": "v"}


def test_unrelated_keys_untouched():
    env = {"PATH": "/usr/bin", "HOME": "/home/x", "BWS_ACCESS_TOKEN": "t"}
    hermes_env.normalize_thoth_env(env)
    assert env == {"PATH": "/usr/bin", "HOME": "/home/x", "BWS_ACCESS_TOKEN": "t"}


def test_returns_count_of_writes():
    env = {"HERMES_A": "1", "THOTH_B": "2"}
    # A→THOTH_A (1 write), B→HERMES_B (1 write) = 2
    assert hermes_env.normalize_thoth_env(env) == 2


def test_defaults_to_os_environ(monkeypatch):
    monkeypatch.delenv("HERMES_PHASE2_PROBE", raising=False)
    monkeypatch.delenv("THOTH_PHASE2_PROBE", raising=False)
    monkeypatch.setenv("THOTH_PHASE2_PROBE", "z")
    hermes_env.normalize_thoth_env()
    assert os.environ["HERMES_PHASE2_PROBE"] == "z"


# ── sync_thoth_aliases: file-loaded value is authoritative over stale mirror ──

def test_sync_aliases_makes_loaded_key_authoritative():
    # Codex-found regression: a rotated legacy value must not be reverted by
    # the stale mirrored alias from an earlier normalization.
    env = {"HERMES_API_KEY": "old"}
    hermes_env.normalize_thoth_env(env)        # THOTH_API_KEY=old
    env["HERMES_API_KEY"] = "new"              # dotenv override=True reload
    hermes_env.sync_thoth_aliases(["HERMES_API_KEY"], env)
    assert env["HERMES_API_KEY"] == "new"
    assert env["THOTH_API_KEY"] == "new"       # twin updated, NOT reverted


def test_sync_aliases_thoth_side_authoritative():
    env = {"THOTH_API_KEY": "old"}
    hermes_env.normalize_thoth_env(env)        # HERMES_API_KEY=old
    env["THOTH_API_KEY"] = "new"
    hermes_env.sync_thoth_aliases(["THOTH_API_KEY"], env)
    assert env["HERMES_API_KEY"] == "new"
    assert env["THOTH_API_KEY"] == "new"


def test_sync_aliases_ignores_unknown_and_home():
    env = {"PATH": "/x", "HERMES_HOME": "/h"}
    assert hermes_env.sync_thoth_aliases(["PATH", "HERMES_HOME"], env) == 0
    assert "THOTH_HOME" not in env


# ── loader integration: a .env using THOTH_ resolves for HERMES_ readers ──

def test_load_dotenv_mirrors_thoth_to_hermes(tmp_path, monkeypatch):
    from thoth_cli import env_loader

    monkeypatch.delenv("HERMES_PHASE2_DOTENV", raising=False)
    monkeypatch.delenv("THOTH_PHASE2_DOTENV", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("THOTH_PHASE2_DOTENV=fromdotenv\n", encoding="utf-8")

    env_loader._load_dotenv_with_fallback(env_file, override=True)

    # A legacy reader using the HERMES_ spelling sees the THOTH_-set value.
    assert os.getenv("HERMES_PHASE2_DOTENV") == "fromdotenv"
    assert os.getenv("THOTH_PHASE2_DOTENV") == "fromdotenv"


def test_dotenv_hot_reload_does_not_revert_rotated_value(tmp_path, monkeypatch):
    """Gateway hot-reload regression (codex-found): a rotated legacy value in
    .env must take effect, not be reverted to the stale mirrored alias."""
    from thoth_cli import env_loader

    monkeypatch.delenv("HERMES_PHASE2_ROT", raising=False)
    monkeypatch.delenv("THOTH_PHASE2_ROT", raising=False)
    env_file = tmp_path / ".env"

    env_file.write_text("HERMES_PHASE2_ROT=old\n", encoding="utf-8")
    env_loader._load_dotenv_with_fallback(env_file, override=True)
    assert os.environ["THOTH_PHASE2_ROT"] == "old"  # mirrored on first load

    # Rotate the legacy key and reload (the hot-reload path uses override=True).
    env_file.write_text("HERMES_PHASE2_ROT=new\n", encoding="utf-8")
    env_loader._load_dotenv_with_fallback(env_file, override=True)
    assert os.environ["HERMES_PHASE2_ROT"] == "new"
    assert os.environ["THOTH_PHASE2_ROT"] == "new"  # NOT reverted to old


# ── subprocess inheritance: child sees both spellings after bootstrap ──

def test_subprocess_sees_both_spellings_after_bootstrap(tmp_path):
    """A child process that imports hermes_bootstrap normalizes its inherited
    env, so a parent that set only HERMES_X exposes THOTH_X to the child."""
    child = textwrap.dedent(
        """
        import os, hermes_bootstrap  # noqa: F401 — import side effect normalizes
        print(os.environ.get("HERMES_PHASE2_CHILD", "<none>"))
        print(os.environ.get("THOTH_PHASE2_CHILD", "<none>"))
        """
    )
    env = dict(os.environ)
    env.pop("THOTH_PHASE2_CHILD", None)
    env["HERMES_PHASE2_CHILD"] = "inherited"
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run(
        [sys.executable, "-c", child],
        env={**env, "PYTHONPATH": repo_root},
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert lines == ["inherited", "inherited"], out.stdout
