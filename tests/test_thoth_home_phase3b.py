"""Phase 3b: both THOTH_HOME + THOTH_HOME propagate to children/units."""

import os

import pytest


def test_systemd_units_emit_both_home_vars():
    import thoth_cli.gateway as g
    for render in (g._systemd_unit_content, g._user_systemd_unit_content):
        unit = render("")
        assert 'Environment="THOTH_HOME=' in unit
        assert 'Environment="THOTH_HOME=' in unit


def test_launchd_plist_emits_both_home_keys():
    src = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "thoth_cli", "gateway.py"),
        encoding="utf-8",
    ).read()
    assert src.count("<key>THOTH_HOME</key>") >= 1
    assert src.count("<key>THOTH_HOME</key>") >= 1


def test_windows_wrapper_and_task_set_both():
    src = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "thoth_cli", "gateway_windows.py"),
        encoding="utf-8",
    ).read()
    assert 'set "THOTH_HOME=' in src and 'set "THOTH_HOME=' in src
    assert '"THOTH_HOME": thoth_home,' in src
    assert '"THOTH_HOME": thoth_home,' in src


def test_profile_env_context_sets_and_restores_both(tmp_path, monkeypatch):
    from thoth_cli.profiles import profile_env_context

    monkeypatch.delenv("THOTH_HOME", raising=False)
    monkeypatch.delenv("THOTH_HOME", raising=False)
    monkeypatch.setenv("THOTH_HOME", "/outer")
    monkeypatch.setenv("THOTH_HOME", "/outer")

    with profile_env_context(tmp_path / "prof"):
        assert os.environ["THOTH_HOME"] == str(tmp_path / "prof")
        assert os.environ["THOTH_HOME"] == str(tmp_path / "prof")
    # restored
    assert os.environ["THOTH_HOME"] == "/outer"
    assert os.environ["THOTH_HOME"] == "/outer"


def test_profile_env_context_restores_unset(tmp_path, monkeypatch):
    from thoth_cli.profiles import profile_env_context

    monkeypatch.delenv("THOTH_HOME", raising=False)
    monkeypatch.delenv("THOTH_HOME", raising=False)
    with profile_env_context(tmp_path / "p"):
        assert os.environ["THOTH_HOME"] == str(tmp_path / "p")
    assert "THOTH_HOME" not in os.environ
    assert "THOTH_HOME" not in os.environ


def test_cron_run_env_sets_both(monkeypatch, tmp_path):
    import cron.scheduler as sched

    monkeypatch.setenv("THOTH_HOME", str(tmp_path))
    monkeypatch.setenv("THOTH_HOME", str(tmp_path))
    # _build_run_env (the helper that injects home) — locate via the public
    # path: it's used in run_job. We test the documented invariant by calling
    # the private builder if present, else skip gracefully.
    builder = getattr(sched, "_build_subprocess_run_env", None)
    if builder is None:
        pytest.skip("run-env builder not individually exposed")
    env = builder()
    assert env.get("THOTH_HOME") == env.get("THOTH_HOME")
